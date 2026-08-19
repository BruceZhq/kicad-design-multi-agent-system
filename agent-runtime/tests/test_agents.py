"""Synthesizer + repair planner tests on the real defective board."""

import shutil
import sys
from pathlib import Path

import pytest

from ratsnest.agents import plan_repairs, synthesize
from ratsnest.agents.synthesizer import parse_rail_voltage
from ratsnest.config import REPO_ROOT
from ratsnest.evolution import StrategyRegistry
from ratsnest.kh_adapter import KicadHappyAdapter

sys.path.insert(0, str(REPO_ROOT / "benchmarks"))


@pytest.fixture(scope="module")
def defective_eval(tmp_path_factory):
    import seed_defects
    golden = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"
    dst = tmp_path_factory.mktemp("boards") / "defective"
    shutil.copytree(golden, dst)
    (dst / "analysis.json").unlink(missing_ok=True)
    # seed in place using the same ops as the benchmark seeder
    from ratsnest.design_edit import Patcher
    from ratsnest.schemas import PatchPlan, RepairOp, RepairOpType
    ops = [RepairOp(op=RepairOpType.set_value, ref="R1", params={"value": "1.5k"}, finding_id="s"),
           RepairOp(op=RepairOpType.set_value, ref="R3", params={"value": "10"}, finding_id="s")]
    ops += [RepairOp(op=RepairOpType.set_property, ref=r,
                     params={"name": "MPN", "value": ""}, finding_id="s")
            for r in ("U1", "R1", "R2", "R3", "D1")]
    assert Patcher().apply(PatchPlan(run_id="t", ops=ops), dst).applied
    outputs = KicadHappyAdapter().analyze_all(dst)
    _, strategy = StrategyRegistry().load_active()
    return synthesize(outputs, strategy, dst), strategy


def test_parse_rail_voltage():
    assert parse_rail_voltage("+5V") == 5.0
    assert parse_rail_voltage("+3V3") == 3.3
    assert parse_rail_voltage("12V") == 12.0
    assert parse_rail_voltage("VBUS") is None


def test_synthesizer_augments_vout_mismatch(defective_eval):
    ev, _ = defective_eval
    vout = [f for f in ev.findings if f.rule_id == "RN-VOUT-001"]
    assert len(vout) == 1
    extra = vout[0].model_extra or {}
    assert extra["vref"] == 1.25
    assert extra["target_vout"] == 5.0
    assert abs(extra["computed_vout"] - 3.125) < 1e-6
    assert vout[0].severity == "error"


def test_synthesizer_applies_suppressions(defective_eval):
    ev, _ = defective_eval
    assert not any(f.rule_id in ("RS-001", "DS-002") for f in ev.findings)
    assert ev.scorecard.suppressed_total >= 1


def test_planner_produces_full_patch_plan(defective_eval):
    ev, strategy = defective_eval
    plan, hints, escalations = plan_repairs(ev, strategy, run_id="t")
    by_ref = {(op.op.value, op.ref): op for op in plan.ops}
    # divider fix: R1 back to 3k (E24 snap of 1k*(5/1.25-1)=3000)
    assert by_ref[("set_value", "R1")].params["value"] == "3k"
    # LED fix: R3 >= 320 ohms -> E24 330
    assert by_ref[("set_value", "R3")].params["value"] == "330"
    # MPN fill for all five parts, using post-fix values for R1/R3
    mpn_ops = [op for op in plan.ops if op.op.value == "set_property"]
    assert {op.ref for op in mpn_ops} == {"U1", "R1", "R2", "R3", "D1"}
    r1_mpn = next(op for op in mpn_ops if op.ref == "R1")
    assert r1_mpn.params["value"] == "RC0805FR-073KL"  # 3k MPN, not 1.5k
    # every op traceable to a finding
    assert all(op.finding_id for op in plan.ops)
    assert len(hints) >= 3


def test_unmapped_findings_escalate_not_improvise(defective_eval):
    ev, strategy = defective_eval
    bundle = strategy.model_copy(deep=True)
    bundle.repair_mappings = [m for m in bundle.repair_mappings
                              if m.match_rule_id != "LR-001"]
    plan, _, escalations = plan_repairs(ev, bundle, run_id="t")
    assert not any(op.ref == "R3" and op.op.value == "set_value" for op in plan.ops)
    assert any(f.rule_id == "LR-001" for f in escalations)
