from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.ratsnestpro import ratsnestpro_agent, tools
from ratsnestpro.agents import LlmError
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.orchestration.pipeline_contracts import SelectionPlan
from ratsnestpro.orchestration.release_invariants import (
    build_release_invariant_manifest,
)


class _DeterministicReport:
    def gate(self, _name: str) -> None:
        return None


def test_deterministic_report_is_published_before_llm_advisory(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "review-order"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))

    deterministic = SimpleNamespace(
        schematic_path=None,
        pcb_path=None,
        blocked=False,
        advisory_markdown="# Advisory review\n\nDeterministic baseline.",
        result=SimpleNamespace(
            report=_DeterministicReport(),
            source="deterministic",
        ),
    )
    monkeypatch.setattr(tools, "review_project", lambda *_args, **_kwargs: deterministic)
    monkeypatch.setattr(tools, "_ToolkitLlmClient", lambda **_kwargs: object())

    report_path = workspace / "reviews" / "ordered-review.md"

    class _FailingReviewer:
        def review(self, *_args, **_kwargs):
            assert report_path.is_file()
            assert "# Independent review-gate verdict" in report_path.read_text(
                encoding="utf-8"
            )
            raise LlmError("advisory provider timed out")

    monkeypatch.setattr(tools, "Reviewer", _FailingReviewer)

    result = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            report_name="ordered-review.md",
            llm_mode="required",
            model_name="test-model",
        )
    )

    assert result["status"] == "ok"
    assert result["independent_review_verdict"]["verdict"] == "PASS"
    assert result["release_verdict"]["verdict"] == "NOT_EVALUATED"
    assert result["report_path"] == str(report_path)
    assert result["advisory_review"]["status"] == "unavailable"
    assert "advisory provider timed out" in result["advisory_review"]["error"]
    assert report_path.is_file()
    assert "LLM advisory enrichment was unavailable" in report_path.read_text(
        encoding="utf-8"
    )


def _deterministic_review() -> SimpleNamespace:
    return SimpleNamespace(
        schematic_path=None,
        pcb_path=None,
        blocked=False,
        advisory_markdown="# Advisory review\n\nDeterministic baseline.",
        result=SimpleNamespace(
            report=_DeterministicReport(),
            source="deterministic",
        ),
    )


def _write_valid_release_receipt(project, requirement: str = "Create a PCB."):
    pcb_path = project / "board.kicad_pcb"
    PcbBoard.blank().save(pcb_path)
    state = {
        "requirement": requirement,
        "project_name": "board",
        "intermediate_artifacts": {
            "selection": SelectionPlan(parts=[]).model_dump(mode="json"),
        },
    }
    (project / "pipeline_state.json").write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    manifest = build_release_invariant_manifest(
        project_name="board",
        requirement=requirement,
        pcb_path=pcb_path,
        findings=[],
        blockers=[],
    )
    manifest_path = project / "board.release_invariants.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return pcb_path, manifest_path


def _no_cli_verification(*_args):
    unavailable = {
        "applicable": False,
        "available": False,
        "ran": False,
        "errors": None,
        "warnings": None,
        "report_path": None,
        "by_type": {},
    }
    return {"erc": dict(unavailable), "drc": {**unavailable, "unconnected": None}}


def test_integrated_review_fails_closed_without_release_invariants(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "integrated-missing"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )

    result = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            llm_mode="offline",
            upstream_release_ready=True,
            upstream_release_blockers=[],
        )
    )

    assert result["status"] == "blocked"
    assert result["independent_review_verdict"]["verdict"] == "PASS"
    assert result["release_verdict"]["verdict"] == "BLOCKED"
    assert result["release_invariants"]["status"] == "missing"


def test_integrated_review_requires_all_three_release_scopes(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "integrated-pass"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )
    monkeypatch.setattr(tools, "_verification", _no_cli_verification)
    _, manifest_path = _write_valid_release_receipt(project)
    release_identity = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["release_identity"]

    result = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            llm_mode="offline",
            upstream_release_ready=True,
            upstream_release_blockers=[],
            upstream_release_identity=release_identity,
        )
    )

    assert result["status"] == "ok"
    assert result["independent_review_verdict"]["verdict"] == "PASS"
    assert result["release_verdict"]["verdict"] == "PASS"
    assert result["release_invariants"]["status"] == "passed"

    missing_identity = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            llm_mode="offline",
            upstream_release_ready=True,
            upstream_release_blockers=[],
        )
    )
    assert missing_identity["release_verdict"]["verdict"] == "BLOCKED"
    assert "missing or invalid" in " ".join(
        missing_identity["release_verdict"]["reasons"]
    )


def test_integrated_review_rejects_mismatched_upstream_release_identity(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "integrated-identity-mismatch"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )
    monkeypatch.setattr(tools, "_verification", _no_cli_verification)
    _, manifest_path = _write_valid_release_receipt(project)
    release_identity = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )["release_identity"]
    release_identity["pcb_sha256"] = "0" * 64

    result = json.loads(
        tools.ratsnest_review_kicad_project(
            str(project),
            llm_mode="offline",
            upstream_release_ready=True,
            upstream_release_blockers=[],
            upstream_release_identity=release_identity,
        )
    )

    assert result["status"] == "blocked"
    assert result["release_verdict"]["verdict"] == "BLOCKED"
    assert "does not match" in " ".join(result["release_verdict"]["reasons"])


def test_integrated_review_rejects_empty_legacy_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "integrated-forged"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )
    (project / "board.release_invariants.json").write_text(
        json.dumps({
            "schema_version": "ratsnestpro.release-invariants.v1",
            "requirement_release_ready": True,
            "requirement_release_blockers": [],
            "invariants": {},
            "findings": [],
        }),
        encoding="utf-8",
    )

    result = json.loads(tools.ratsnest_review_kicad_project(
        str(project),
        llm_mode="offline",
        upstream_release_ready=True,
        upstream_release_blockers=[],
    ))

    assert result["status"] == "blocked"
    assert result["release_invariants"]["status"] == "invalid"


def test_integrated_review_rejects_receipt_after_pcb_change(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / "integrated-stale"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )
    monkeypatch.setattr(tools, "_verification", _no_cli_verification)
    pcb_path, _ = _write_valid_release_receipt(project)
    pcb_path.write_text(
        pcb_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    result = json.loads(tools.ratsnest_review_kicad_project(
        str(project),
        llm_mode="offline",
        upstream_release_ready=True,
        upstream_release_blockers=[],
    ))

    assert result["status"] == "blocked"
    assert "SHA-256 is stale" in " ".join(
        result["release_invariants"]["blockers"]
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_name", "other-board", "project mismatch"),
        ("requirement", "Build a different PCB.", "source digest is stale"),
    ],
)
def test_integrated_review_rejects_receipt_identity_mismatch(
    tmp_path,
    monkeypatch,
    field: str,
    value: str,
    message: str,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "runs" / f"integrated-{field}"
    project.mkdir(parents=True)
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        tools,
        "review_project",
        lambda *_args, **_kwargs: _deterministic_review(),
    )
    monkeypatch.setattr(tools, "_verification", _no_cli_verification)
    _write_valid_release_receipt(project)
    state_path = project / "pipeline_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = value
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = json.loads(tools.ratsnest_review_kicad_project(
        str(project),
        llm_mode="offline",
        upstream_release_ready=True,
        upstream_release_blockers=[],
    ))

    assert result["status"] == "blocked"
    assert message in " ".join(result["release_invariants"]["blockers"])


def test_hardware_validation_rejects_upstream_blockers(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(ratsnestpro_agent, "_workspace_root", lambda: workspace)
    artifacts = [
        "board.kicad_sch",
        "board.kicad_pcb",
        "board.dsn",
        "board.ses",
    ]
    for artifact in artifacts:
        (workspace / artifact).write_text("fixture", encoding="utf-8")
    result = {
        "status": "ok",
        "outcome": "release_ready",
        "execution_complete": True,
        "release_ready": True,
        "release_blockers": ["manufacturing release blocker"],
        "completed_steps": 17,
        "total_steps": 17,
        "artifacts": artifacts,
        "routing": {"method": "freerouting", "unconnected": 0},
        "verification": {
            "erc": {
                "applicable": True,
                "available": True,
                "ran": True,
                "errors": 0,
            },
            "drc": {
                "applicable": True,
                "available": True,
                "ran": True,
                "errors": 0,
                "unconnected": 0,
            },
        },
    }

    validated = ratsnestpro_agent._validate_hardware_result(result)

    assert validated["release_ready"] is False
    assert validated["outcome"] == "delivered_with_issues"
    assert "manufacturing release blocker" in validated["release_blockers"]
