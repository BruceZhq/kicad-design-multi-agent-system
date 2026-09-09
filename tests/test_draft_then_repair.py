"""Draft scheduling must never waive release truth or restart a valid prefix."""
from pathlib import Path

import pytest
from pydantic import BaseModel

from ratsnestpro.orchestration import pipeline as p
from ratsnestpro.repair.draft import deferred_checks, dependency_fingerprint, finalize_draft, recover_draft_transaction
from service.ahe_event import ahe_event_record


@pytest.mark.parametrize("event", [
    "draft_issues_deferred", "draft_final_repair_started",
    "draft_dependencies_invalidated", "draft_final_repair_verified",
    "draft_final_repair_needs_attention",
])
def test_all_draft_events_survive_real_bridge_without_private_evidence(event):
    from ratsnestpro.repair.draft import _emit_draft_event

    records = []
    _emit_draft_event(
        lambda payload: records.append(ahe_event_record(payload, workflow_id="bridge-test")),
        event, step="manufacture", revision=3, owner="selection",
        failures="private design evidence", issue_count=2,
    )
    assert records[0]["revision"] == 3
    assert records[0]["draft"] == {"owner": "selection", "issue_count": 2}


class Artifact(BaseModel):
    value: int = 1


def test_symbol_only_repair_retains_layout_but_physical_and_net_changes_do_not():
    import copy
    from types import SimpleNamespace
    from ratsnestpro.repair.draft import _layout_inputs_unchanged

    part = SimpleNamespace(ref="U1", value="MCU", mpn="MCU", footprint="QFP:64",
                           role="mcu", symbol="original")
    original = {p.PipelineStep.SELECTION: SimpleNamespace(parts=[part]),
                p.PipelineStep.SCH_CONNECTIONS: Artifact(value=1)}
    current = SimpleNamespace(artifacts=copy.deepcopy(original))
    current.artifacts[p.PipelineStep.SELECTION].parts[0].symbol = "verified_pin_type_correction"
    assert _layout_inputs_unchanged(original, current)
    current.artifacts[p.PipelineStep.SCH_CONNECTIONS] = Artifact(value=2)
    assert not _layout_inputs_unchanged(original, current)
    current.artifacts[p.PipelineStep.SCH_CONNECTIONS] = Artifact(value=1)
    current.artifacts[p.PipelineStep.SELECTION].parts[0].footprint = "QFP:48"
    assert not _layout_inputs_unchanged(original, current)


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
    events = []
    step = FailingStep()
    state = p.PipelineState("draft")
    p.Pipeline([step]).run(state, p.PipelineContext(
        draft_first=True, artifact_first=True, repair_release_issues=True,
        design_repair_attempts=2, repair_attempts=2,
        on_ahe_event=lambda event: events.append(ahe_event_record(event, workflow_id="draft-test")),
    ))
    assert state.completed == [p.PipelineStep.REQUIREMENTS]
    assert state.blocked and not state.execution_blocked
    assert step.repairs == 0
    assert state.draft_execution["issues"]["requirements"]["checks"][0]["ok"] is False
    deferred = [e for e in events if e["event"] == "draft_issues_deferred"]
    assert deferred[0]["draft"] == {"issue_count": 1, "release_ready": False}


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


def test_old_continuation_cannot_refresh_allowance():
    from ratsnestpro.repair.draft import authorize_repair_continuation

    state = p.PipelineState("draft")
    state.draft_execution["repair_passes"] = 3
    authorize_repair_continuation(state, "first")
    state.draft_execution["repair_passes"] = 6
    authorize_repair_continuation(state, "second")
    state.draft_execution["repair_passes"] = 9
    authorize_repair_continuation(state, "first")
    assert state.draft_execution["allowance_start_passes"] == 6
    assert state.draft_execution["repair_passes"] == 9


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


@pytest.mark.parametrize("continuation", [False, True])
def test_final_repair_reuses_ancestors_and_rebuilds_dependent_manufacture(monkeypatch, tmp_path, continuation):
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
    if continuation:
        from ratsnestpro.repair.draft import authorize_repair_continuation

        state.draft_execution["repair_passes"] = 3
        authorize_repair_continuation(state, "user-confirmation-1")
        state.draft_execution["repair_passes"] = 4
        authorize_repair_continuation(state, "user-confirmation-1")
        assert state.draft_execution["allowance_start_passes"] == 3
        assert state.draft_execution["repair_passes"] == 4
    steps = []
    for name in p.CANONICAL_ORDER:
        item = Repairable()
        item.step = name
        steps.append(item)
        monkeypatch.setitem(p.ARTIFACT_MODELS, name, Artifact)
        state.artifacts[name] = Artifact()
        state.results.append(p.StepResult(step=name))
    monkeypatch.setattr(p, "ALL_STEPS", steps)
    events = []
    finalize_draft(state, p.PipelineContext(out_dir=str(tmp_path),
        on_ahe_event=lambda event: events.append(ahe_event_record(event, workflow_id="final-test"))))
    assert any(e["event"] == "draft_final_repair_started" for e in events)
    assert any(e["event"] == "draft_final_repair_verified" for e in events)
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


@pytest.mark.parametrize("real_binding", [True, False])
def test_independent_cad_owner_never_bypasses_missing_assets(real_binding):
    from ratsnestpro.repair.draft import _independent_cad_owner

    state = p.PipelineState("draft")
    for name in p.CANONICAL_ORDER:
        state.artifacts[name] = Artifact()
        state.results.append(p.StepResult(step=name))
    selection = state.results[p._ORDER_INDEX[p.PipelineStep.SELECTION]]
    selection.checks = [p.CheckResult(
        name="prepared_component_manifest", ok=False, blocks_execution=True,
        reason_code="independent_package_evidence_missing",
        evidence={"component_diagnostics": [{
            "blockers": ["independent_package_evidence_missing"],
            "available_source_kinds": ["verified_local_kicad_binding"] if real_binding else [],
        }]},
    )]
    route_index = p._ORDER_INDEX[p.PipelineStep.ROUTE_SIGNALS]
    state.results[route_index].checks = [p.CheckResult(name="signals_routed", ok=False)]
    assert _independent_cad_owner(state) == (route_index if real_binding else None)


def test_document_observations_survive_cad_rollback(tmp_path):
    ctx = p.PipelineContext(out_dir=str(tmp_path))
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("baseline")
    snapshot = p._snapshot_candidate_files(ctx, "observations")
    evidence = tmp_path / "technical-evidence" / "receipt.json"
    evidence.parent.mkdir()
    evidence.write_text('{"status":"unverified"}')
    pcb.write_text("candidate")
    p._restore_candidate_files(ctx, snapshot)
    assert pcb.read_text() == "baseline"
    assert evidence.read_text() == '{"status":"unverified"}'


def test_final_repair_keeps_cad_improvement_while_evidence_waits(monkeypatch, tmp_path):
    calls = []

    class Step(p.PipelineStepBase):
        def propose(self, state, ctx, knowledge):
            return Artifact(value=2), False

        def check(self, state, artifact):
            if self.step == p.PipelineStep.SELECTION:
                return [p.CheckResult(name="prepared_component_manifest", ok=False,
                    blocks_execution=True, reason_code="independent_package_evidence_missing",
                    origin=p.FailureOrigin.EXTERNAL_EVIDENCE,
                    evidence={"component_diagnostics": [{
                        "blockers": ["independent_package_evidence_missing"],
                        "available_source_kinds": ["verified_local_kicad_binding"],
                    }]})]
            return [p.CheckResult(name="route_gap", ok=(
                self.step != p.PipelineStep.ROUTE_SIGNALS or artifact.value == 2))]

        def repair(self, state, ctx, knowledge, artifact, checks):
            calls.append(self.step)
            return Artifact(value=2), False

    state = p.PipelineState("draft")
    steps = []
    for name in p.CANONICAL_ORDER:
        step = Step()
        step.step = name
        steps.append(step)
        state.artifacts[name] = Artifact()
        state.results.append(p.StepResult(step=name))
        monkeypatch.setitem(p.ARTIFACT_MODELS, name, Artifact)
    monkeypatch.setattr(p, "ALL_STEPS", steps)
    monkeypatch.setattr(p, "_prepare_and_persist_components", lambda artifact, *a, **k: (artifact, None))
    finalize_draft(state, p.PipelineContext(out_dir=str(tmp_path)))
    assert p.PipelineStep.ROUTE_SIGNALS in calls
    assert state.artifact(p.PipelineStep.ROUTE_SIGNALS).value == 2
    assert len(state.results) == 17
    assert state.draft_execution["phase"] == "needs_attention"
    assert state.draft_execution["manufacturing_refresh_required"]
