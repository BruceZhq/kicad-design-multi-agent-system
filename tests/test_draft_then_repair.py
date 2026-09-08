"""Draft scheduling must never waive release truth or restart a valid prefix."""
from pathlib import Path

import pytest
from pydantic import BaseModel

from ratsnestpro.orchestration import pipeline as p
from ratsnestpro.repair.draft import deferred_checks, dependency_fingerprint, finalize_draft, recover_draft_transaction


class Artifact(BaseModel):
    value: int = 1


class FailingStep(p.PipelineStepBase):
    step = p.PipelineStep.REQUIREMENTS
    repairs = 0
    execution_failure = False

    def propose(self, state, ctx, knowledge):
        return Artifact(), False

    def check(self, state, artifact):
        return [p.CheckResult(name="design_issue", ok=False,
                              blocks_execution=self.execution_failure)]

    def repair(self, *args):
        self.repairs += 1
        raise AssertionError("draft may not repair")


def test_draft_retains_errors_without_repair_or_false_pass():
    step = FailingStep()
    state = p.PipelineState("draft")
    p.Pipeline([step]).run(state, p.PipelineContext(
        draft_first=True, artifact_first=True, repair_release_issues=True,
        design_repair_attempts=2, repair_attempts=2,
    ))
    assert state.completed == [p.PipelineStep.REQUIREMENTS]
    assert state.blocked and not state.execution_blocked
    assert step.repairs == 0
    assert state.draft_execution["issues"]["requirements"]["checks"][0]["ok"] is False


def test_unusable_input_stops_without_spending_repair_calls():
    step = FailingStep()
    step.execution_failure = True
    state = p.PipelineState("draft")
    p.Pipeline([step]).run(state, p.PipelineContext(draft_first=True, artifact_first=True))
    assert state.execution_blocked and step.repairs == 0


@pytest.mark.parametrize("blockers,source,allowed", [
    (["independent_package_evidence_missing"], ["verified_local_kicad_binding"], True),
    (["independent_package_evidence_missing", "pad_mismatch"], ["verified_local_kicad_binding"], False),
    (["independent_package_evidence_missing"], [], False),
])
def test_only_real_compatible_assets_can_defer_independent_evidence(blockers, source, allowed):
    check = p.CheckResult(name="prepared_component_manifest", ok=False, blocks_execution=True,
        reason_code="independent_package_evidence_missing", evidence={"component_diagnostics": [
            {"blockers": blockers, "available_source_kinds": source}]})
    result = deferred_checks(p.PipelineStep.SELECTION, Artifact(), [check])[0]
    assert result.blocks_execution is not allowed
    assert not result.ok and check.blocks_execution  # release check remains untouched


def test_in_place_cad_change_invalidates_dependents(tmp_path):
    class Board(BaseModel):
        pcb_path: str
    path = tmp_path / "board.kicad_pcb"
    path.write_text("old")
    artifact = Board(pcb_path=str(path))
    before = dependency_fingerprint(p.PipelineStep.LAYOUT_WRITE, artifact)
    path.write_text("new")
    assert before != dependency_fingerprint(p.PipelineStep.LAYOUT_WRITE, artifact)


@pytest.mark.parametrize("checked,error,allowed", [(True, "", True), (False, "export failed", False)])
def test_only_parsed_schematic_mismatch_can_be_deferred(tmp_path, checked, error, allowed):
    class Schematic(BaseModel):
        sch_path: str
        connectivity_checked: bool
        connectivity_error: str
    path = tmp_path / "board.kicad_sch"
    path.write_text("existing schematic")
    artifact = Schematic(sch_path=str(path), connectivity_checked=checked, connectivity_error=error)
    check = p.CheckResult(name="design_ir_matches_kicad_netlist", ok=False, blocks_execution=True)
    actual = deferred_checks(p.PipelineStep.ERC, artifact, [check])[0]
    assert actual.blocks_execution is not allowed
    assert not actual.ok


def test_finalization_rechecks_without_accepting_draft_flags(monkeypatch):
    state = p.PipelineState("draft")
    steps = []
    for name in p.CANONICAL_ORDER:
        item = FailingStep()
        item.step = name
        steps.append(item)
        state.artifacts[name] = Artifact()
        state.results.append(p.StepResult(step=name, blocked=False))
    monkeypatch.setattr(p, "ALL_STEPS", steps)
    # Exhausted final budget must never mark a nominal 17-step draft verified.
    state.draft_execution["repair_passes"] = 3
    finalize_draft(state, p.PipelineContext())
    assert state.draft_execution["phase"] == "needs_attention"


def test_draft_resume_rechecks_but_keeps_deferred_prefix(monkeypatch):
    step = FailingStep()
    monkeypatch.setattr(p, "ALL_STEPS", [step])
    monkeypatch.setitem(p.ARTIFACT_MODELS, p.PipelineStep.REQUIREMENTS, Artifact)
    restored = p.restore_pipeline_state(
        requirement_text="draft", project_name="board",
        intermediate_artifacts={"requirements": {"value": 1}},
        steps=[{"name": "requirements", "blocked": True, "execution_blocked": False}],
        artifact_first=True, draft_first=True,
        draft_execution={"phase": "draft"},
    )
    assert restored.completed == [p.PipelineStep.REQUIREMENTS]
    assert restored.blocked and not restored.execution_blocked


def test_final_repair_reuses_ancestors_and_rebuilds_dependent_manufacture(monkeypatch, tmp_path):
    calls = []

    class Repairable(p.PipelineStepBase):
        def propose(self, state, ctx, knowledge):
            calls.append((self.step.value, "propose"))
            return Artifact(value=2), False

        def check(self, state, artifact):
            return [p.CheckResult(name="route_gap", ok=(
                self.step != p.PipelineStep.ROUTE_SIGNALS or artifact.value == 2))]

        def repair(self, state, ctx, knowledge, artifact, checks):
            assert not ctx.draft_first
            assert "Final engineering repair" in knowledge
            calls.append((self.step.value, "repair"))
            return Artifact(value=2), False

    state = p.PipelineState("draft")
    steps = []
    for name in p.CANONICAL_ORDER:
        item = Repairable()
        item.step = name
        steps.append(item)
        monkeypatch.setitem(p.ARTIFACT_MODELS, name, Artifact)
        state.artifacts[name] = Artifact()
        state.results.append(p.StepResult(step=name))
    monkeypatch.setattr(p, "ALL_STEPS", steps)
    finalize_draft(state, p.PipelineContext(out_dir=str(tmp_path)))
    assert state.draft_execution["phase"] == "verified"
    assert not state.blocked
    assert ("route_signals", "repair") in calls
    assert ("manufacture", "propose") in calls
    assert all(p._ORDER_INDEX[p.PipelineStep(name)] >= p._ORDER_INDEX[p.PipelineStep.ROUTE_SIGNALS]
               for name, _ in calls)


def test_interrupted_final_transaction_restores_draft_not_budget(monkeypatch, tmp_path):
    monkeypatch.setitem(p.ARTIFACT_MODELS, p.PipelineStep.REQUIREMENTS, Artifact)
    state = p.PipelineState("draft")
    state.artifacts[p.PipelineStep.REQUIREMENTS] = Artifact()
    state.results = [p.StepResult(step=p.PipelineStep.REQUIREMENTS)]
    ctx = p.PipelineContext(out_dir=str(tmp_path), draft_first=True)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("original")
    baseline = p._capture_candidate_baseline(state, ctx, "interrupted")
    state.draft_execution = {"active_candidate": baseline.model_dump(mode="json"), "repair_passes": 2}
    state.results.clear()
    pcb.write_text("partial candidate")
    recover_draft_transaction(state, ctx)
    assert pcb.read_text() == "original"
    assert state.completed == [p.PipelineStep.REQUIREMENTS]
    assert state.draft_execution["repair_passes"] == 2
    assert "active_candidate" not in state.draft_execution
