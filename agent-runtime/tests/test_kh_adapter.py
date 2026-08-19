"""Adapter + scorecard tests against the real analyzer and demo board."""

import pytest

from ratsnest.kh_adapter import KicadHappyAdapter, compute_scorecard
from ratsnest.kh_adapter.runner import AdapterError, find_root_schematic
from ratsnest.schemas import Finding


def test_find_root_schematic(golden_project):
    assert find_root_schematic(golden_project).name == "demo_board.kicad_sch"


def test_analyze_schematic_returns_valid_envelope(golden_project):
    out = KicadHappyAdapter().analyze_schematic(golden_project)
    assert out.analyzer_type == "schematic"
    assert out.schema_version.startswith("1.")
    assert len(out.findings) > 0
    # golden board has zero error-severity findings
    assert sum(1 for f in out.findings if f.severity == "error") == 0
    # the regulator detection must be present with its feedback divider payload
    regs = [f for f in out.findings if f.rule_id == "PR-DET"]
    assert regs, "regulator not detected on golden board"
    fb = (regs[0].model_extra or {}).get("feedback_divider")
    assert fb and fb["r_top"]["ref"] == "R1" and fb["r_bottom"]["ref"] == "R2"


def test_analyze_missing_project_raises(tmp_path):
    with pytest.raises(AdapterError):
        KicadHappyAdapter().analyze_schematic(tmp_path)


def test_scorecard_formula():
    findings = [Finding(severity="error"), Finding(severity="warning"),
                Finding(severity="warning"), Finding(severity="info")]
    sc = compute_scorecard(findings, weights={"error": 30, "warning": 3},
                           erc_passed=False)
    # 100 - 30 - 6 - 15 = 49
    assert sc.score == 49.0
    assert sc.severity_counts == {"error": 1, "warning": 2, "info": 1}
    assert sc.deductions["erc_fail"] == 15.0


def test_scorecard_clamps_at_zero():
    findings = [Finding(severity="error") for _ in range(10)]
    assert compute_scorecard(findings).score == 0.0
