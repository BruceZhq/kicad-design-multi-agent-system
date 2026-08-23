from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from evolution.runner import load_content_addressed_suites, run_recorded_evaluation
from observability import safe_attributes

ROOT = Path(__file__).resolve().parents[2]


def test_public_recorded_gate_reports_agent_quality_metrics() -> None:
    report = run_recorded_evaluation(
        ROOT,
        [
            "evals/suites/holdout.v1.json",
            "evals/suites/adversarial.v1.json",
        ],
    )

    assert report.metrics.case_count == 3
    assert report.metrics.pass_rate == 1.0
    assert report.metrics.tool_call_accuracy == 1.0
    assert report.metrics.state_transition_accuracy == 1.0
    assert report.metrics.goal_completion_rate == 1.0
    assert report.metrics.gate_accuracy == 1.0
    assert report.metrics.false_release_count == 0


def test_historical_suite_exposes_false_release_instead_of_hiding_it() -> None:
    report = run_recorded_evaluation(ROOT, ["evals/suites/optimization.v1.json"])

    assert report.metrics.passed_cases == 1
    assert report.metrics.case_count == 2
    assert report.metrics.false_release_count == 1


def test_evidence_digest_prevents_fixture_rewriting(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "evals", tmp_path / "evals")
    evidence = tmp_path / "evals" / "sealed" / "fixtures" / "constraint-preservation.v1.json"
    evidence.write_text(evidence.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="evidence digest mismatch"):
        load_content_addressed_suites(
            tmp_path,
            ["evals/suites/holdout.v1.json"],
        )


def test_telemetry_drops_sensitive_and_high_cardinality_attributes() -> None:
    cleaned = safe_attributes(
        {
            "agent.id": "ratsnestpro-multi-agent",
            "agent.prompt": "private requirement",
            "tenant.id": "tenant-private",
            "request_id": "request-private",
            "db.statement": "select secret",
            "agent.tool.name": "kicad.cli.drc",
        }
    )

    assert cleaned == {
        "agent.id": "ratsnestpro-multi-agent",
        "agent.tool.name": "kicad.cli.drc",
    }
