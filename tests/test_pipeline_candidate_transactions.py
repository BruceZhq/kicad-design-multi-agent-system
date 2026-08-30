from __future__ import annotations

from pathlib import Path

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.domain.contracts import RequirementSpec
from ratsnestpro.orchestration.ahe import (
    RecoveryAction,
    RecoveryDecision,
    ReplanRecord,
)
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    PipelineStepBase,
    _recovery_action_fingerprint,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectionPlan, TopologyPlan


def test_recovery_dedup_uses_executable_instructions() -> None:
    reroute = RecoveryDecision(
        action=RecoveryAction.LOCAL_REPAIR,
        target_step=PipelineStep.ROUTE_SIGNALS.value,
        tool_name="replan_current_step",
        strategy="retry router",
        tool_args={"repair_instructions": "Run Freerouting again."},
    )
    close_gaps = reroute.model_copy(
        update={
            "strategy": "close exact gaps",
            "tool_args": {
                "repair_instructions": "Add the three DRC-reported copper segments."
            },
        }
    )

    assert _recovery_action_fingerprint(reroute) != _recovery_action_fingerprint(
        close_gaps
    )


class _RequirementsStep(PipelineStepBase):
    step = PipelineStep.REQUIREMENTS

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return RequirementSpec(
            raw_text=state.requirement_text,
            project_name=state.project_name,
        ), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [CheckResult(name="requirements", ok=True)]


class _RejectedLocalTopology(PipelineStepBase):
    step = PipelineStep.TOPOLOGY

    def __init__(self, path: Path) -> None:
        self.path = path

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        self.path.write_text("baseline", encoding="utf-8")
        return TopologyPlan(rails=["3V3"], rationale="baseline"), False

    def replan(self, state, ctx, knowledge, artifact, feedback):  # type: ignore[no-untyped-def]
        self.path.write_text("rejected-candidate", encoding="utf-8")
        return TopologyPlan(rails=["3V3"], rationale="candidate"), True

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        message = "bad" if artifact.rationale == "baseline" else "candidate is much worse"
        return [CheckResult(name="topology_gate", ok=False, message=message)]


class _LocalRecoveryClient:
    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        return RecoveryDecision(
            action=RecoveryAction.LOCAL_REPAIR,
            target_step=PipelineStep.TOPOLOGY.value,
            strategy="try a bounded candidate",
            hypothesis="the current topology may be repairable",
            expected_observation="topology_gate improves",
            confidence=0.9,
        ).model_dump_json()


def test_rejected_local_recovery_restores_state_and_files(tmp_path: Path) -> None:
    design_file = tmp_path / "design.txt"
    state = PipelineState(requirement_text="immutable")

    Pipeline([_RequirementsStep(), _RejectedLocalTopology(design_file)]).run(
        state,
        PipelineContext(
            mode=LlmMode.REQUIRED,
            client=_LocalRecoveryClient(),
            out_dir=str(tmp_path),
            agentic_recovery_enabled=True,
            ahe_enabled=False,
        ),
    )

    topology = state.artifact(PipelineStep.TOPOLOGY)
    assert isinstance(topology, TopologyPlan)
    assert topology.rationale == "baseline"
    assert design_file.read_text(encoding="utf-8") == "baseline"
    assert state.recovery_history[0].status == "rejected"
    assert all(
        turn.candidate_baseline is None for turn in state.recovery_history
    )


class _ReplannedTopology(PipelineStepBase):
    step = PipelineStep.TOPOLOGY

    def __init__(self, path: Path) -> None:
        self.path = path

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        self.path.write_text("baseline", encoding="utf-8")
        return TopologyPlan(rails=["3V3"], rationale="baseline"), False

    def replan(self, state, ctx, knowledge, artifact, feedback):  # type: ignore[no-untyped-def]
        self.path.write_text("candidate", encoding="utf-8")
        return TopologyPlan(rails=["3V3"], rationale="candidate"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        return [CheckResult(name="topology", ok=True)]


class _BlockedSelection(PipelineStepBase):
    step = PipelineStep.SELECTION

    def propose(self, state, ctx, knowledge):  # type: ignore[no-untyped-def]
        return SelectionPlan(rationale="selection"), False

    def check(self, state, artifact):  # type: ignore[no-untyped-def]
        topology = state.artifact(PipelineStep.TOPOLOGY)
        assert isinstance(topology, TopologyPlan)
        message = (
            "bad"
            if topology.rationale == "baseline"
            else "candidate is much worse"
        )
        return [CheckResult(name="selection_gate", ok=False, message=message)]

    def rollback_target(self, state, artifact, checks):  # type: ignore[no-untyped-def]
        return PipelineStep.TOPOLOGY


def test_stagnated_upstream_replan_rolls_back_complete_prefix(tmp_path: Path) -> None:
    design_file = tmp_path / "design.txt"
    state = PipelineState(requirement_text="immutable")

    Pipeline(
        [_RequirementsStep(), _ReplannedTopology(design_file), _BlockedSelection()]
    ).run(
        state,
        PipelineContext(
            out_dir=str(tmp_path),
            ahe_enabled=True,
            max_replan_attempts=1,
        ),
    )

    assert state.completed == [
        PipelineStep.REQUIREMENTS,
        PipelineStep.TOPOLOGY,
        PipelineStep.SELECTION,
    ]
    topology = state.artifact(PipelineStep.TOPOLOGY)
    assert isinstance(topology, TopologyPlan)
    assert topology.rationale == "baseline"
    assert design_file.read_text(encoding="utf-8") == "baseline"
    assert state.replan_history[-1].status == "stagnated"
    assert state.replan_history[-1].candidate_baseline is None
    assert not any(record.status == "scheduled" for record in state.replan_history)


def test_legacy_scheduled_replan_cannot_inject_feedback() -> None:
    state = PipelineState(requirement_text="immutable")
    legacy = ReplanRecord(
        trigger_step=PipelineStep.SELECTION.value,
        rollback_to=PipelineStep.TOPOLOGY.value,
        attempt=1,
        status="scheduled",
        before_score=(1, 1, 10),
        feedback="stale route feedback",
    )
    state.replan_history.append(legacy)

    Pipeline([_RequirementsStep()]).run(state, PipelineContext())

    assert legacy.status == "deferred"
    assert legacy.after_score == legacy.before_score
