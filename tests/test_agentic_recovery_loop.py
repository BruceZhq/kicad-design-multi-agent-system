from __future__ import annotations

from pydantic import BaseModel

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.ahe import RecoveryAction, RecoveryDecision
from ratsnestpro.orchestration.entity_repairs import (
    AffectedTerminal,
    EntityRepairCategory,
    EntityRepairPlan,
    RepairExecutionPolicy,
)
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


class _EvidenceOwnedConnections(PipelineStepBase):
    step = PipelineStep.SCH_CONNECTIONS

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return _Artifact(value="bad"), False

    def replan(  # type: ignore[no-untyped-def]
        self,
        state,
        ctx,
        knowledge,
        artifact,
        feedback,
    ):
        assert '"affected_nets":["GND"]' in feedback
        assert '"ref":"U3"' in feedback
        return _Artifact(value="good"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [CheckResult(name="source", ok=True)]


class _PassingStep(PipelineStepBase):
    def __init__(self, step: PipelineStep) -> None:
        self.step = step

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return _Artifact(value="pass"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [CheckResult(name="pass", ok=True)]


class _EvidenceOwnedErc(PipelineStepBase):
    step = PipelineStep.ERC

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        source = state.artifact(PipelineStep.SCH_CONNECTIONS)
        assert isinstance(source, _Artifact)
        return _Artifact(value=source.value), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        plan = EntityRepairPlan(
            finding_type="pin_to_pin",
            source_section="violations",
            category=EntityRepairCategory.SCHEMATIC_CONNECTIVITY,
            rollback_step=PipelineStep.SCH_CONNECTIONS.value,
            strategy="repair_pin_electrical_conflict_in_design_ir",
            execution_policy=RepairExecutionPolicy.BOUNDED_CANDIDATE,
            affected_refs=["U3", "#PWR01"],
            affected_pins=[
                AffectedTerminal(ref="U3", number="7", kind="pin"),
                AffectedTerminal(ref="#PWR01", number="1", kind="pin"),
            ],
            affected_nets=["GND"],
            reason="incompatible electrical pin types share one net",
        )
        return [CheckResult(
            name="kicad_cli_erc",
            ok=artifact.value == "good",
            message="pin_to_pin",
            evidence={"entity_repair_plans": [plan.model_dump(mode="json")]},
        )]

    def rollback_target(self, state, artifact, checks):  # type: ignore[no-untyped-def]
        return PipelineStep.SCH_CONNECTIONS


class _EvidenceAwareRecoveryClient:
    calls = 0

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls += 1
        if self.calls == 1:
            assert "suggested_deterministic_owner" in user
            return ('{"engineering_queries":[{"tool":"artifact",'
                    '"step":"schematic_connections"}]}')
        assert "UNTRUSTED ENGINEERING TOOL OBSERVATIONS" in user
        return RecoveryDecision(
            action=RecoveryAction.REPLAN_UPSTREAM,
            target_step="schematic_connections",
            hypothesis="source IR, confirmed by inspection, owns the electrical conflict",
            strategy="repair pin-level connections using the ERC evidence",
            expected_observation="the same ERC finding disappears",
        ).model_dump_json()


def test_entity_complete_erc_evidence_guides_but_does_not_disable_reflection() -> None:
    client = _EvidenceAwareRecoveryClient()
    state = PipelineState(requirement_text="immutable")

    Pipeline([
        _RequirementsStep(),
        _PassingStep(PipelineStep.TOPOLOGY),
        _PassingStep(PipelineStep.SELECTION),
        _EvidenceOwnedConnections(),
        _PassingStep(PipelineStep.SCH_PINMAP),
        _PassingStep(PipelineStep.SCH_LAYOUT),
        _PassingStep(PipelineStep.SCH_MATERIALIZE),
        _EvidenceOwnedErc(),
    ]).run(
        state,
        PipelineContext(
            mode=LlmMode.REQUIRED,
            client=client,
            agentic_recovery_enabled=True,
            ahe_enabled=True,
            max_replan_attempts=1,
        ),
    )

    assert client.calls == 2
    assert not state.blocked
    assert state.recovery_history[0].used_llm is True
    assert state.recovery_history[0].decision.target_step == "schematic_connections"
