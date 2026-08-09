"""Hardware Coding Agent — proposes repairs, restricted to a typed whitelist.

In the parametric family the only legal repair is adjusting a validated family
parameter, so the whitelist is a single operation ``set_param`` over the
Atmega328Params fields. The Coder cannot write files, run shell, or touch
anything else. The LLM may add a root-cause *diagnosis* and *propose* actions,
but every action is validated against the whitelist and re-validated through
the params contract before it is applied.
"""

from __future__ import annotations

import json
import re

from ratsnestpro.agents.llm import (
    LLMClient,
    LlmError,
    LlmMode,
    parse_mode,
    resolve_client,
)
from ratsnestpro.domain.contracts import AgentDecision, RepairAction, VerificationReport
from ratsnestpro.families import Atmega328Params
from ratsnestpro.verification.expectations import Expectations

ALLOWED_OPERATION = "set_param"
ALLOWED_PARAMS = set(Atmega328Params.model_fields.keys())

_SYSTEM = (
    "You are the Hardware Coding Agent for a parameterized ATmega328 board. "
    "Given the failing verification findings, the current parameters, and the "
    "target expectations, diagnose the root cause and propose repairs. You may "
    "ONLY use the operation 'set_param' with arguments {name, value} where name "
    f"is one of {sorted(ALLOWED_PARAMS)}. You cannot edit files or run tools. "
    "Respond with STRICT JSON only: "
    '{"diagnosis": str, "give_up": bool, "actions": '
    '[{"operation": "set_param", "arguments": {"name": str, "value": any}, '
    '"rationale": str}]}'
)


def _parse_json_block(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(s[start : end + 1])


def _validate_action(action: RepairAction) -> None:
    if action.operation != ALLOWED_OPERATION:
        raise LlmError(f"operation {action.operation!r} is not permitted")
    name = action.arguments.get("name")
    if name not in ALLOWED_PARAMS:
        raise LlmError(f"parameter {name!r} is not in the whitelist")
    if "value" not in action.arguments:
        raise LlmError("set_param requires a 'value'")


def apply_actions(params: Atmega328Params, actions: list[RepairAction]) -> Atmega328Params:
    """Apply whitelisted set_param actions and re-validate through the contract."""
    data = params.model_dump()
    for action in actions:
        _validate_action(action)
        data[str(action.arguments["name"])] = action.arguments["value"]
    return Atmega328Params(**data)  # type: ignore[arg-type]


class Coder:
    def diagnose(
        self,
        report: VerificationReport,
        params: Atmega328Params,
        target: Expectations,
        mode: str | LlmMode = LlmMode.OFFLINE,
        client: LLMClient | None = None,
        kb: object | None = None,
    ) -> AgentDecision:
        mode = parse_mode(mode)
        resolved = resolve_client(mode, client)
        if resolved is None:
            return self._deterministic(report, params, target)
        try:
            return self._live(report, params, target, resolved, kb)
        except (LlmError, ValueError, KeyError, TypeError) as exc:
            if mode == LlmMode.REQUIRED:
                raise LlmError(f"required EricAI repair failed: {exc}") from exc
            return self._deterministic(report, params, target)

    # -- deterministic diagnosis ------------------------------------------ #

    def _deterministic(
        self, report: VerificationReport, params: Atmega328Params, target: Expectations
    ) -> AgentDecision:
        failing = {
            g.gate for g in report.gates if not g.passed and g.status.value == "failed"
        }
        actions: list[RepairAction] = []
        notes: list[str] = []

        if "six_decoupling" in failing and params.decoupling_count != target.decoupling_count:
            actions.append(_set("decoupling_count", target.decoupling_count,
                                 "align decoupling count to target"))
            notes.append("decoupling count mismatch")

        if "voltage" in failing and params.ldo_output_v != target.supply_voltage_v:
            actions.append(_set("ldo_output_v", target.supply_voltage_v,
                                 "align supply rail to target"))
            notes.append("supply voltage mismatch")

        if "crystal_load" in failing and params.crystal_mhz != target.crystal_freq_mhz:
            actions.append(_set("crystal_mhz", target.crystal_freq_mhz,
                                 "align crystal frequency to target"))
            # Keep the cross-rule satisfied: 16 MHz needs 5 V.
            if target.crystal_freq_mhz >= 16 and params.ldo_output_v < 4.5:
                actions.append(_set("ldo_output_v", 5.0,
                                     "16 MHz requires a 5.0 V supply"))
            notes.append("crystal frequency mismatch")

        give_up = not actions
        diagnosis = (
            "; ".join(notes) if notes else "no repairable parameter mismatch identified"
        )
        return AgentDecision(diagnosis=diagnosis, actions=actions, give_up=give_up)

    # -- live diagnosis ---------------------------------------------------- #

    def _live(
        self,
        report: VerificationReport,
        params: Atmega328Params,
        target: Expectations,
        client: LLMClient,
        kb: object | None = None,
    ) -> AgentDecision:
        system = _SYSTEM
        if kb is not None and hasattr(kb, "retrieve_text"):
            rule_ids = " ".join(f.rule_id for f in report.findings)
            context = kb.retrieve_text(f"repair {rule_ids}", top_k=3, role="repair")
            if context:
                system = f"{_SYSTEM}\n\nReference knowledge (advisory):\n{context}"
        payload = json.dumps(
            {
                "findings": [
                    {"rule_id": f.rule_id, "severity": f.severity.value, "summary": f.summary}
                    for f in report.findings
                ],
                "current_params": params.model_dump(),
                "target": target.model_dump(),
            }
        )
        raw = client.complete(system, payload)
        data = _parse_json_block(raw)
        actions = [
            RepairAction(
                operation=str(a.get("operation", "")),
                arguments=dict(a.get("arguments", {})),
                rationale=str(a.get("rationale", "")),
            )
            for a in (data.get("actions", []) or [])
            if isinstance(a, dict)
        ]
        for action in actions:  # fail closed on any non-whitelisted action
            _validate_action(action)
        return AgentDecision(
            diagnosis=str(data.get("diagnosis", "")),
            actions=actions,
            give_up=bool(data.get("give_up", False)),
        )


def _set(name: str, value: object, rationale: str) -> RepairAction:
    return RepairAction(
        operation=ALLOWED_OPERATION, arguments={"name": name, "value": value}, rationale=rationale
    )
