from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.ratsnestpro.tools import (
    PipelineCheckpointRegressionError,
    _write_pipeline_state,
)
from ratsnestpro.domain.contracts import RequirementSpec, Severity
from ratsnestpro.orchestration.pipeline import (
    ALL_STEPS,
    CheckResult,
    PipelineState,
    PipelineStep,
    StepResult,
    restore_pipeline_state,
)


def test_artifact_first_restore_keeps_release_only_checkpoint_when_checks_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = RequirementSpec(raw_text="Build a board", project_name="board")
    validator = ALL_STEPS[0]
    monkeypatch.setattr(
        validator,
        "check",
        lambda _state, _artifact: [
            CheckResult(
                name="new_release_gate",
                ok=False,
                severity=Severity.ERROR,
                blocks_execution=False,
                message="release evidence changed",
            )
        ],
    )

    state = restore_pipeline_state(
        requirement_text="Build a board",
        project_name="board",
        intermediate_artifacts={
            "requirements": artifact.model_dump(mode="json"),
        },
        steps=[
            {
                "name": "requirements",
                "used_llm": True,
                "blocked": True,
                "execution_blocked": False,
                "summary": "saved checkpoint",
                "failed_checks": [
                    {
                        "name": "old_release_gate",
                        "severity": "error",
                        "message": "old release evidence",
                        "blocks_execution": False,
                    }
                ],
            }
        ],
        artifact_first=True,
    )

    assert state.completed == [PipelineStep.REQUIREMENTS]
    assert state.results[0].blocked is True
    assert state.results[0].execution_blocked is False
    assert state.results[0].error_checks[0].name == "new_release_gate"


def test_artifact_first_restore_rejects_new_execution_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = RequirementSpec(raw_text="Build a board", project_name="board")
    validator = ALL_STEPS[0]
    monkeypatch.setattr(
        validator,
        "check",
        lambda _state, _artifact: [
            CheckResult(
                name="artifact_missing",
                ok=False,
                severity=Severity.ERROR,
                blocks_execution=True,
                message="mechanical input is no longer usable",
            )
        ],
    )

    state = restore_pipeline_state(
        requirement_text="Build a board",
        project_name="board",
        intermediate_artifacts={
            "requirements": artifact.model_dump(mode="json"),
        },
        steps=[
            {
                "name": "requirements",
                "used_llm": True,
                "blocked": False,
                "execution_blocked": False,
                "summary": "saved checkpoint",
                "failed_checks": [],
            }
        ],
        artifact_first=True,
    )

    assert state.completed == []
    assert PipelineStep.REQUIREMENTS in state.resume_candidates


def test_same_revision_checkpoint_cannot_move_backwards(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    requirement = "Build a board"
    committed = PipelineState(requirement_text=requirement, project_name="board")
    committed.results.extend(
        [
            StepResult(step=PipelineStep.REQUIREMENTS),
            StepResult(step=PipelineStep.TOPOLOGY),
        ]
    )
    _write_pipeline_state(path, requirement, committed)

    stale = PipelineState(requirement_text=requirement, project_name="board")
    stale.results.append(StepResult(step=PipelineStep.REQUIREMENTS))

    with pytest.raises(PipelineCheckpointRegressionError, match="2 to 1"):
        _write_pipeline_state(path, requirement, stale)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["completed_steps"] == 2


def test_new_checkpoint_revision_may_explicitly_invalidate_steps(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    requirement = "Build a board"
    committed = PipelineState(requirement_text=requirement, project_name="board")
    committed.results.extend(
        [
            StepResult(step=PipelineStep.REQUIREMENTS),
            StepResult(step=PipelineStep.TOPOLOGY),
        ]
    )
    _write_pipeline_state(path, requirement, committed)

    revised = PipelineState(
        requirement_text=requirement,
        project_name="board",
        revision=1,
    )
    revised.results.append(StepResult(step=PipelineStep.REQUIREMENTS))
    _write_pipeline_state(path, requirement, revised)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["completed_steps"] == 1
    assert payload["revision"] == 1
