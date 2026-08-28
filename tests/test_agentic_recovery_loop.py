from __future__ import annotations

from pydantic import BaseModel

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.ahe import RecoveryAction, RecoveryDecision
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    PipelineStepBase,
)


class _Artifact(BaseModel):
    value: str


class _RequirementsStep(PipelineStepBase):
    step = PipelineStep.REQUIREMENTS

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return _Artifact(value="requirements-frozen"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [CheckResult(name="requirements", ok=True)]


class _RecoverableTopologyStep(PipelineStepBase):
    step = PipelineStep.TOPOLOGY

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return _Artifact(value="bad"), False

    def replan(self, state, ctx, knowledge, artifact, feedback):  # type: ignore[no-untyped-def]
        assert "Agentic recovery plan" in feedback
        return _Artifact(value="good"), True

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [
            CheckResult(
                name="topology_gate",
                ok=artifact.value == "good",
                message="topology still violates the source requirement",
            )
        ]


class _RecoveryClient:
    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        return RecoveryDecision(
            action=RecoveryAction.LOCAL_REPAIR,
            target_step=PipelineStep.TOPOLOGY.value,
            strategy="revise topology from the immutable requirement",
            tool_args={"repair_instructions": "replace the rejected topology"},
            hypothesis="the topology proposal, not the gate, is wrong",
            expected_observation="topology_gate passes",
            success_checks=["topology_gate"],
            confidence=0.9,
        ).model_dump_json()


class _HarnessRefreshTopologyStep(PipelineStepBase):
    step = PipelineStep.TOPOLOGY

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        self.calls += 1
        return _Artifact(value="good" if self.calls > 1 else "stale"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [
            CheckResult(
                name="harness_observation",
                ok=artifact.value == "good",
                message="the persisted observation is stale",
            )
        ]


class _HarnessInvestigationClient:
    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        return RecoveryDecision(
            action=RecoveryAction.INVESTIGATE_HARNESS,
            target_step=PipelineStep.TOPOLOGY.value,
            strategy="refresh deterministic evidence",
            hypothesis="the cached verifier observation is stale",
            expected_observation="harness_observation passes after re-execution",
            success_checks=["harness_observation"],
            confidence=0.9,
        ).model_dump_json()


def test_blocked_step_enters_plan_act_observe_recovery_before_terminal() -> None:
    events: list[dict[str, object]] = []
    state = PipelineState(requirement_text="keep the requirement immutable")
    Pipeline([_RequirementsStep(), _RecoverableTopologyStep()]).run(
        state,
        PipelineContext(
            mode=LlmMode.REQUIRED,
            client=_RecoveryClient(),
            agentic_recovery_enabled=True,
            ahe_enabled=False,
            on_ahe_event=events.append,
        ),
    )

    assert state.completed == [PipelineStep.REQUIREMENTS, PipelineStep.TOPOLOGY]
    assert not state.blocked
    assert len(state.recovery_history) == 1
    turn = state.recovery_history[0]
    assert turn.decision.action == RecoveryAction.LOCAL_REPAIR
    assert turn.status == "verified"
    assert turn.after_score == (0, 0, 0)
    assert [
        event["event"]
        for event in events
        if str(event["event"]).startswith("recovery_")
    ] == [
        "recovery_planned",
        "recovery_action_started",
        "recovery_observed",
    ]


def test_harness_investigation_reexecutes_producer_instead_of_cached_gate() -> None:
    topology = _HarnessRefreshTopologyStep()
    state = PipelineState(requirement_text="keep the requirement immutable")

    Pipeline([_RequirementsStep(), topology]).run(
        state,
        PipelineContext(
            mode=LlmMode.REQUIRED,
            client=_HarnessInvestigationClient(),
            agentic_recovery_enabled=True,
            ahe_enabled=False,
        ),
    )

    assert topology.calls == 2
    assert not state.blocked
    assert state.recovery_history[0].status == "verified"
