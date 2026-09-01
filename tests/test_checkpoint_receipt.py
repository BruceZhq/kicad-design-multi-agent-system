from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agents.ratsnestpro.temporal import workflow as temporal_workflow
from agents.ratsnestpro.temporal.contracts import (
    READ_CHECKPOINT_ACTIVITY,
    checkpoint_receipt,
    compact_pipeline_result,
)
from agents.ratsnestpro.tools import (
    PipelineCheckpointRegressionError,
    _write_pipeline_state,
)
from ratsnestpro.orchestration.pipeline import (
    PipelineState,
    PipelineStep,
    StepResult,
)


def _state(prefix_length: int, *, revision: int = 0) -> PipelineState:
    state = PipelineState(
        requirement_text="Build a board",
        project_name="board",
        revision=revision,
    )
    state.results.extend(
        StepResult(step=step) for step in list(PipelineStep)[:prefix_length]
    )
    return state


def _persisted_receipt(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))["checkpoint_receipt"]


def test_checkpoint_generation_is_monotonic_and_parent_linked(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    state = _state(1)

    _write_pipeline_state(path, state.requirement_text, state)
    first = _persisted_receipt(path)
    state.results.append(StepResult(step=PipelineStep.TOPOLOGY))
    _write_pipeline_state(path, state.requirement_text, state)
    second = _persisted_receipt(path)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert second["parent_state_sha256"] == first["state_sha256"]
    assert second["committed_step_index"] == 2
    assert state.checkpoint_generation == 2
    assert state.checkpoint_state_sha256 == second["state_sha256"]


def test_new_revision_can_commit_an_explicit_rollback(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    state = _state(3)
    _write_pipeline_state(path, state.requirement_text, state)
    before = _persisted_receipt(path)

    rollback = copy.deepcopy(state)
    rollback.revision += 1
    rollback.results = rollback.results[:1]
    _write_pipeline_state(path, rollback.requirement_text, rollback)
    after = _persisted_receipt(path)

    assert after["generation"] == before["generation"] + 1
    assert after["state_revision"] == 1
    assert after["committed_step_index"] == 1
    assert after["next_step"] == PipelineStep.TOPOLOGY.value
    assert after["transition_kind"] == "rollback"
    assert after["parent_state_sha256"] == before["state_sha256"]


def test_checkpoint_cas_rejects_a_stale_writer(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    committed = _state(1)
    _write_pipeline_state(path, committed.requirement_text, committed)
    stale = copy.deepcopy(committed)

    committed.results.append(StepResult(step=PipelineStep.TOPOLOGY))
    _write_pipeline_state(path, committed.requirement_text, committed)
    latest = _persisted_receipt(path)

    stale.results.append(StepResult(step=PipelineStep.TOPOLOGY))
    with pytest.raises(PipelineCheckpointRegressionError, match="stale writer"):
        _write_pipeline_state(path, stale.requirement_text, stale)

    assert _persisted_receipt(path) == latest


def test_receipt_validation_controls_compact_result_projection(tmp_path: Path) -> None:
    path = tmp_path / "pipeline_state.json"
    state = _state(1)
    _write_pipeline_state(path, state.requirement_text, state)
    raw = _persisted_receipt(path)

    validated = checkpoint_receipt(raw)
    assert validated == raw
    compact = compact_pipeline_result(
        {
            "status": "ok",
            "completed_steps": 1,
            "checkpoint_receipt": raw,
        },
        PipelineStep.REQUIREMENTS.value,
    )
    assert compact["checkpoint_receipt"] == raw
    assert len(compact["checkpoint_digest"]) == 64

    invalid = {**raw, "next_step": PipelineStep.SELECTION.value}
    assert checkpoint_receipt(invalid) is None
    invalid_compact = compact_pipeline_result(
        {"status": "ok", "checkpoint_receipt": invalid},
        PipelineStep.REQUIREMENTS.value,
    )
    assert "checkpoint_receipt" not in invalid_compact


def test_reconcile_checkpoint_never_replaces_a_newer_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "status": "error",
        "pipeline_state_path": "pipeline_state.json",
        "checkpoint_receipt": {"generation": 2},
    }
    responses = iter(
        [
            {
                "status": "ok",
                "completed_steps": 3,
                "checkpoint_receipt": {"generation": 3},
            },
            {
                "status": "ok",
                "completed_steps": 1,
                "checkpoint_receipt": {"generation": 1},
            },
        ]
    )

    async def fake_execute_activity(
        activity_name: str,
        command: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert activity_name == READ_CHECKPOINT_ACTIVITY
        assert command == {
            "run_name": "run-1",
            "pipeline_state_path": "pipeline_state.json",
        }
        return next(responses)

    monkeypatch.setattr(
        temporal_workflow.workflow,
        "execute_activity",
        fake_execute_activity,
    )
    workflow = temporal_workflow.RatsNestHardwareWorkflow()

    newer = asyncio.run(workflow._reconcile_checkpoint({"run_name": "run-1"}, current))
    older = asyncio.run(workflow._reconcile_checkpoint({"run_name": "run-1"}, current))

    assert newer["completed_steps"] == 3
    assert newer["checkpoint_receipt"]["generation"] == 3
    assert older == current
