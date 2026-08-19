"""Patch applier tests: real edits on a real board, verified by the analyzer."""

from ratsnest.design_edit import Patcher
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.schemas import PatchPlan, RepairOp, RepairOpType


def _divider(out):
    reg = [f for f in out.findings if f.rule_id == "PR-DET"][0]
    return (reg.model_extra or {})["feedback_divider"]


def test_set_value_changes_analyzer_result(golden_project):
    plan = PatchPlan(run_id="t", ops=[
        RepairOp(op=RepairOpType.set_value, ref="R1",
                 params={"value": "1.5k"}, finding_id="test"),
    ])
    result = Patcher().apply(plan, golden_project)
    assert result.applied and not result.rolled_back
    key = "demo_board.kicad_sch"
    assert result.changed_files[key]["before"] != result.changed_files[key]["after"]

    out = KicadHappyAdapter().analyze_schematic(golden_project)
    assert _divider(out)["r_top"]["ohms"] == 1500.0


def test_set_property_adds_mpn(golden_project):
    plan = PatchPlan(run_id="t", ops=[
        RepairOp(op=RepairOpType.set_property, ref="R3",
                 params={"name": "MPN", "value": "TEST-MPN-42"}, finding_id="test"),
    ])
    assert Patcher().apply(plan, golden_project).applied
    out = KicadHappyAdapter().analyze_schematic(golden_project)
    comps = {c["reference"]: c for c in (out.model_extra or {})["components"]}
    assert comps["R3"]["mpn"] == "TEST-MPN-42"


def test_unknown_reference_fails_whole_plan_without_writing(golden_project):
    sch = golden_project / "demo_board.kicad_sch"
    before = sch.read_text(encoding="utf-8")
    plan = PatchPlan(run_id="t", ops=[
        RepairOp(op=RepairOpType.set_value, ref="R99",
                 params={"value": "1k"}, finding_id="test"),
    ])
    result = Patcher().apply(plan, golden_project)
    assert not result.applied
    assert "R99" in (result.error or "")
    assert sch.read_text(encoding="utf-8") == before  # untouched


def test_unsupported_op_rejected(golden_project):
    plan = PatchPlan(run_id="t", ops=[
        RepairOp(op=RepairOpType.add_component, ref="C9", finding_id="test"),
    ])
    result = Patcher().apply(plan, golden_project)
    assert not result.applied and "not supported" in (result.error or "")
