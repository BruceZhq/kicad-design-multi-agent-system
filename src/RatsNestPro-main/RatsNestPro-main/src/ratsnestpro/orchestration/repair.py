"""Semi-auto / auto repair loop.

Given starting parameters and a target Expectations, the loop verifies the
design, and while it is blocked asks the Coder for a whitelisted repair,
applies it, and re-verifies. It fails closed when the Coder gives up, when an
iteration makes no progress (proposed params equal the current params), or when
the iteration budget is exhausted. A no-progress repeat also triggers a
strategy change flag so callers can escalate.

Semi-auto vs auto is expressed by the ``on_step`` callback: return False to
stop before applying the next repair (a human declined), True/None to continue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ratsnestpro.agents.coding import Coder, apply_actions
from ratsnestpro.agents.llm import LLMClient, LlmError, LlmMode, parse_mode
from ratsnestpro.domain.contracts import AgentDecision, VerificationReport
from ratsnestpro.families import Atmega328Params, build_ir
from ratsnestpro.verification import verify_design
from ratsnestpro.verification.expectations import Expectations


@dataclass
class RepairStep:
    iteration: int
    decision: AgentDecision
    params_before: dict
    params_after: dict


@dataclass
class RepairResult:
    success: bool
    params: Atmega328Params
    report: VerificationReport
    iterations: int
    steps: list[RepairStep] = field(default_factory=list)
    reason: str = ""


# on_step(iteration, decision, next_params) -> bool | None
OnStep = Callable[[int, AgentDecision, Atmega328Params], bool | None]


def run_repair(
    params: Atmega328Params,
    target: Expectations,
    max_iter: int = 5,
    mode: str | LlmMode = LlmMode.OFFLINE,
    client: LLMClient | None = None,
    on_step: OnStep | None = None,
    coder: Coder | None = None,
) -> RepairResult:
    mode = parse_mode(mode)
    coder = coder or Coder()
    steps: list[RepairStep] = []

    report = verify_design(build_ir(params), target)
    if not report.blocked:
        return RepairResult(True, params, report, 0, steps, "already satisfies target")

    last_signature: tuple | None = None
    repeat_count = 0

    for i in range(1, max_iter + 1):
        try:
            decision = coder.diagnose(report, params, target, mode=mode, client=client)
        except LlmError as exc:
            return RepairResult(False, params, report, i - 1, steps, f"diagnosis failed: {exc}")

        if decision.give_up or not decision.actions:
            return RepairResult(False, params, report, i - 1, steps, "coder gave up")

        # Detect a repeated (non-progressing) strategy and escalate.
        signature = tuple(
            (a.arguments.get("name"), str(a.arguments.get("value"))) for a in decision.actions
        )
        if signature == last_signature:
            repeat_count += 1
            if repeat_count >= 2:
                return RepairResult(
                    False, params, report, i - 1, steps, "no progress (repeated strategy)"
                )
        else:
            repeat_count = 0
        last_signature = signature

        try:
            new_params = apply_actions(params, decision.actions)
        except LlmError as exc:
            return RepairResult(False, params, report, i - 1, steps, f"illegal repair: {exc}")

        if new_params == params:
            return RepairResult(
                False, params, report, i - 1, steps, "no progress (params unchanged)"
            )

        # Semi-auto gate: let a human decline before applying.
        if on_step is not None and on_step(i, decision, new_params) is False:
            return RepairResult(False, params, report, i - 1, steps, "stopped by on_step")

        steps.append(
            RepairStep(
                iteration=i,
                decision=decision,
                params_before=params.model_dump(),
                params_after=new_params.model_dump(),
            )
        )
        params = new_params
        report = verify_design(build_ir(params), target)
        if not report.blocked:
            return RepairResult(True, params, report, i, steps, "repaired")

    return RepairResult(False, params, report, max_iter, steps, "iteration budget exhausted")
