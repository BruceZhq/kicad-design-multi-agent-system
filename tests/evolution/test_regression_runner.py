from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import evolution.regression_runner as runner

ROOT = Path(__file__).resolve().parents[2]


def test_governed_regression_manifests_are_content_addressed_and_partitioned() -> None:
    suites = [runner.load_suite(ROOT, reference) for reference in runner.DEFAULT_SUITES]

    assert [suite["suiteKind"] for suite in suites] == [
        "optimization",
        "holdout",
        "adversarial",
    ]
    assert [suite["sealed"] for suite in suites] == [False, True, True]
    assert all(runner.calculate_suite_digest(suite) == suite["suiteDigest"] for suite in suites)


def test_source_drift_is_rejected_before_pytest_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "evals" / "regression" / "test_case.py"
    source.parent.mkdir(parents=True)
    source.write_text("def test_case():\n    assert True\n", encoding="utf-8")
    document = {
        "schemaVersion": "1.0",
        "suiteId": "drift-test",
        "suiteKind": "optimization",
        "sealed": False,
        "cases": [
            {
                "caseId": "optimization.drift",
                "stage": "release_gate",
                "source": "evals/regression/test_case.py",
                "sourceSha256": "0" * 64,
                "nodeId": "evals/regression/test_case.py::test_case",
                "timeoutSeconds": 10,
            }
        ],
    }
    document["suiteDigest"] = runner.calculate_suite_digest(document)
    manifest = tmp_path / "evals" / "regression" / "optimization.v1.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    called = False

    def fail_if_called(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(runner.subprocess, "run", fail_if_called)
    with pytest.raises(ValueError, match="source digest mismatch"):
        runner.run_suites(tmp_path, ["evals/regression/optimization.v1.json"])
    assert called is False


def test_sealed_suite_executes_from_trusted_root_not_candidate_checkout(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "candidate-marker.txt").write_text("candidate\n", encoding="utf-8")
    trusted_root = tmp_path / "trusted"
    source = trusted_root / "evals" / "sealed" / "regression" / "test_case.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from pathlib import Path\n\n"
        "def test_case():\n"
        "    assert (Path.cwd() / 'candidate-marker.txt').read_text().strip() == 'candidate'\n"
        "    assert not (Path.cwd() / 'evals' / 'sealed').exists()\n",
        encoding="utf-8",
    )
    source_ref = "evals/sealed/regression/test_case.py"
    document = {
        "schemaVersion": "1.0",
        "suiteId": "trusted-root-test",
        "suiteKind": "holdout",
        "sealed": True,
        "cases": [
            {
                "caseId": "holdout.trusted-root",
                "stage": "release_gate",
                "source": source_ref,
                "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "nodeId": f"{source_ref}::test_case",
                "timeoutSeconds": 10,
            }
        ],
    }
    document["suiteDigest"] = runner.calculate_suite_digest(document)
    manifest = trusted_root / "evals" / "sealed" / "regression" / "holdout.v1.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    report = runner.run_suites(
        candidate_root,
        ["evals/sealed/regression/holdout.v1.json"],
        suite_root=trusted_root,
    )

    assert report["passed"] is True


def test_baseline_candidate_comparison_fails_closed_on_regression() -> None:
    manifest = [{"suiteDigest": "a" * 64}]
    baseline = {
        "suiteManifests": manifest,
        "cases": [
            {"caseId": "one", "passed": True},
            {"caseId": "two", "passed": False},
        ],
    }
    candidate = {
        "suiteManifests": manifest,
        "cases": [
            {"caseId": "one", "passed": False},
            {"caseId": "two", "passed": True},
        ],
    }

    comparison = runner.compare_reports(baseline, candidate)

    assert comparison["improvedCases"] == ["two"]
    assert comparison["regressedCases"] == ["one"]
    assert comparison["candidatePassed"] is False


def test_control_plane_digest_mismatch_fails_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "load_suite",
        lambda root, reference: {"suiteDigest": "a" * 64},
    )
    monkeypatch.setattr(
        runner,
        "run_suites",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    with pytest.raises(ValueError, match="configured suite digest"):
        runner.main(
            [
                "--root",
                str(ROOT),
                "--suite",
                "evals/regression/optimization.v1.json",
                "--expected-suite-digest",
                "b" * 64,
            ]
        )


def test_exact_junit_evidence_rejects_zero_exit_without_the_requested_node(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.xml"
    forged = tmp_path / "forged.xml"
    forged.write_text(
        '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )

    assert runner._exact_junit_execution(
        missing, "evals/sealed/regression/test_case.py::test_expected"
    ) is False
    assert runner._exact_junit_execution(
        forged, "evals/sealed/regression/test_case.py::test_expected"
    ) is False


def test_exact_junit_evidence_accepts_one_executed_requested_node(tmp_path: Path) -> None:
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="test_case" name="test_expected"/>'
        "</testsuite>",
        encoding="utf-8",
    )

    assert runner._exact_junit_execution(
        report, "evals/sealed/regression/test_case.py::test_expected"
    ) is True
