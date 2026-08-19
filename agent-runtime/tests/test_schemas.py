"""Contract round-trip and identity tests."""

import json

from ratsnest.schemas import (
    AnalyzerOutput,
    Finding,
    PatchPlan,
    RepairMapping,
    RepairOp,
    RepairOpType,
    Scorecard,
    StrategyBundle,
    TrajectoryEvent,
)


def test_finding_passthrough_preserves_unknown_fields():
    raw = {
        "detector": "feedback_divider",
        "rule_id": "FS-001",
        "severity": "warning",
        "components": ["R8", "R9"],
        "computed_vout": 3.87,
        "report_context": {"component": "U2"},
    }
    f = Finding.model_validate(raw)
    dumped = f.model_dump()
    assert dumped["computed_vout"] == 3.87  # unknown field survived
    assert f.components_involved() == ["R8", "R9", "U2"]
    assert f.finding_id() == "FS-001:R8,R9,U2"


def test_analyzer_output_envelope_roundtrip():
    raw = {
        "analyzer_type": "schematic",
        "schema_version": "1.3.0",
        "summary": {"components": 12},
        "findings": [{"detector": "x", "severity": "error"}],
        "trust_summary": {"deterministic": 1},
        "extra_top_level": [1, 2],
    }
    out = AnalyzerOutput.model_validate(raw)
    round_tripped = json.loads(out.model_dump_json())
    assert round_tripped["extra_top_level"] == [1, 2]
    assert round_tripped["findings"][0]["severity"] == "error"


def test_strategy_version_id_is_content_addressed():
    s1 = StrategyBundle(name="a", scorecard_weights={"error": 30, "warning": 3})
    s2 = StrategyBundle(name="a", scorecard_weights={"warning": 3, "error": 30})
    s3 = StrategyBundle(name="a", scorecard_weights={"error": 31, "warning": 3})
    assert s1.version_id() == s2.version_id()  # key order irrelevant
    assert s1.version_id() != s3.version_id()  # content change changes id
    assert s1.version_id().startswith("strat_")


def test_patch_plan_ops_traceable_to_findings():
    op = RepairOp(op=RepairOpType.set_value, ref="R8",
                  params={"value": "16k"}, finding_id="FS-001:R8,R9")
    plan = PatchPlan(run_id="run_x", iteration=1, ops=[op])
    dumped = json.loads(plan.model_dump_json())
    assert dumped["ops"][0]["op"] == "set_value"
    assert dumped["ops"][0]["finding_id"] == "FS-001:R8,R9"


def test_trajectory_event_atdp_shape():
    evt = TrajectoryEvent(run_id="run_x", node="plan_repairs",
                          observation={"findings": 3},
                          action={"ops": 2}, outcome={"applied": True})
    d = evt.model_dump()
    for key in ("observation", "agent_state", "action", "outcome", "reward",
                "metadata", "ts"):
        assert key in d
    assert d["reward"] is None  # late-bound by default


def test_repair_mapping_matches_by_rule_or_detector():
    m = RepairMapping(match_rule_id="PU-001", repair_type="i2c_pullup")
    assert m.match_rule_id == "PU-001"
    assert m.enabled


def test_scorecard_serialization():
    sc = Scorecard(score=61.0, severity_counts={"error": 1, "warning": 3},
                   deductions={"error": 30.0, "warning": 9.0},
                   findings_total=4, strategy_version_id="strat_abc")
    assert json.loads(sc.model_dump_json())["score"] == 61.0
