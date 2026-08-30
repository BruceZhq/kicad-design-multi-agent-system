from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.exceptions import ActivityError, RetryState

from agents.ratsnestpro import ratsnestpro_agent
from agents.ratsnestpro.temporal import activities, client
from agents.ratsnestpro.temporal import workflow as temporal_workflow
from agents.ratsnestpro.temporal.contracts import (
    CANONICAL_STEPS,
    READ_RESULT_ACTIVITY,
)
from agents.ratsnestpro.tools import (
    PipelineCheckpointRegressionError,
    _load_pipeline_state,
    _requirement_contract_payload,
    _run_pcb_pipeline_unlocked,
    _write_pipeline_state,
)
from ratsnestpro.orchestration.pipeline import (
    PipelineState,
    PipelineStep,
    StepResult,
)
from ratsnestpro.orchestration.pipeline_contracts import RouteResult


def _release_blocked_hardware(*, infrastructure_blocked: bool = False) -> dict[str, Any]:
    steps = [
        {
            "name": step,
            "blocked": False,
            "execution_blocked": False,
            "failed_checks": [],
        }
        for step in CANONICAL_STEPS
    ]
    route_signals = steps[CANONICAL_STEPS.index("route_signals")]
    route_signals.update(
        {
            "blocked": True,
            "execution_blocked": infrastructure_blocked,
            "failed_checks": [
                {
                    "name": "ground_plane_materialized",
                    "severity": "error",
                    "blocks_execution": infrastructure_blocked,
                }
            ],
        }
    )
    manufacture = steps[CANONICAL_STEPS.index("manufacture")]
    manufacture.update(
        {
            "blocked": True,
            "execution_blocked": False,
            "failed_checks": [
                {
                    "name": "physical_requirements_release_ready",
                    "severity": "error",
                    "blocks_execution": False,
                }
            ],
        }
    )
    return {
        "release_ready": False,
        "execution_blocked": infrastructure_blocked,
        "completed_steps": len(CANONICAL_STEPS),
        "steps": steps,
    }


def test_langgraph_selects_earliest_release_only_blocker() -> None:
    state = {
        "incremental_resume": True,
        "hardware": _release_blocked_hardware(),
    }

    assert (
        ratsnestpro_agent._release_repair_resume_step(state)  # type: ignore[arg-type]
        == "route_signals"
    )


def test_langgraph_resumes_execution_blocked_step() -> None:
    state = {
        "incremental_resume": True,
        "hardware": _release_blocked_hardware(infrastructure_blocked=True),
    }

    assert (
        ratsnestpro_agent._release_repair_resume_step(state)  # type: ignore[arg-type]
        == "route_signals"
    )


def test_langgraph_resumes_next_step_after_partial_verified_prefix() -> None:
    state = {
        "incremental_resume": True,
        "hardware": {
            "release_ready": False,
            "completed_steps": 8,
            "steps": _release_blocked_hardware()["steps"][:8],
        },
    }

    assert (
        ratsnestpro_agent._release_repair_resume_step(state)  # type: ignore[arg-type]
        == CANONICAL_STEPS[8]
    )


def test_langgraph_uses_durable_checkpoint_when_hardware_summary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_name = "workspace-run"
    run_dir = tmp_path / "runs" / run_name
    run_dir.mkdir(parents=True)
    payload = _checkpoint_payload()
    payload["steps"] = payload["steps"][:8]
    (run_dir / "pipeline_state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    state = {
        "incremental_resume": True,
        "workspace_run_name": run_name,
        "hardware": {},
    }

    assert (
        ratsnestpro_agent._release_repair_resume_step(state)  # type: ignore[arg-type]
        == CANONICAL_STEPS[8]
    )


def test_runtime_recovery_continues_terminal_temporal_from_durable_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_name = "workspace-run"
    run_dir = tmp_path / "runs" / run_name
    run_dir.mkdir(parents=True)
    payload = _checkpoint_payload()
    payload["steps"] = payload["steps"][:3]
    (run_dir / "pipeline_state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    captured: dict[str, Any] = {}

    async def fake_status(_run_ref: dict[str, Any]) -> str:
        return "timed_out"

    async def fake_dispatch(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "mode": "temporal",
            "status": "started",
            "request_id": kwargs["request_id"],
            "workflow_id": "continued-workflow",
            "workspace_run_name": kwargs["workspace_run_name"],
        }

    monkeypatch.setattr(client, "hardware_workflow_execution_status", fake_status)
    monkeypatch.setattr(client, "dispatch_hardware_workflow", fake_dispatch)
    monkeypatch.setattr(client, "temporal_enabled", lambda: True)
    monkeypatch.setattr(ratsnestpro_agent, "_hardware_requirement", lambda _state: "build")
    monkeypatch.setattr(ratsnestpro_agent, "_workflow_event", lambda *_args, **_kwargs: None)
    state = {
        "incremental_resume": False,
        "run_name": "display-run",
        "workspace_run_name": run_name,
        "execution_scope": "internal",
        "project_name": "board",
        "hardware": {},
        "hardware_attempts": [],
        "capability_profile": {},
        "hardware_dispatch": {
            "mode": "temporal",
            "status": "wait_error",
            "request_id": "request-1",
            "workflow_id": "timed-out-workflow",
            "workspace_run_name": run_name,
        },
    }

    update = asyncio.run(
        ratsnestpro_agent.hardware_dispatch_phase(
            state,  # type: ignore[arg-type]
            {"configurable": {"request_id": "request-1"}},  # type: ignore[arg-type]
        )
    )

    assert captured["resume_from_step"] == CANONICAL_STEPS[3]
    assert captured["requirement"] == payload["requirement"]
    assert captured["request_id"] != "request-1"
    assert update["hardware_dispatch"]["request_id"] == "request-1"
    assert update["hardware_dispatch"]["continuation_index"] == 1
    assert (
        update["hardware_dispatch"]["resumed_from_workflow_id"]
        == "timed-out-workflow"
    )


def test_runtime_recovery_attaches_to_running_temporal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_status(_run_ref: dict[str, Any]) -> str:
        return "running"

    async def unexpected_dispatch(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a running workflow must not be duplicated")

    monkeypatch.setattr(client, "hardware_workflow_execution_status", fake_status)
    monkeypatch.setattr(client, "dispatch_hardware_workflow", unexpected_dispatch)
    monkeypatch.setattr(ratsnestpro_agent, "_workflow_event", lambda *_args, **_kwargs: None)
    existing = {
        "mode": "temporal",
        "status": "started",
        "request_id": "request-1",
        "workflow_id": "running-workflow",
        "workspace_run_name": "workspace-run",
    }
    state = {
        "run_name": "display-run",
        "workspace_run_name": "workspace-run",
        "project_name": "board",
        "hardware_dispatch": existing,
    }

    update = asyncio.run(
        ratsnestpro_agent.hardware_dispatch_phase(
            state,  # type: ignore[arg-type]
            {"configurable": {"request_id": "request-1"}},  # type: ignore[arg-type]
        )
    )

    assert update == {"hardware_dispatch": existing}


def test_full_checkpoint_with_final_erc_blocker_resumes_at_erc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_name = "workspace-run"
    run_dir = tmp_path / "runs" / run_name
    run_dir.mkdir(parents=True)
    checkpoint_payload = _checkpoint_payload()
    for step in checkpoint_payload["steps"]:
        step["blocked"] = False
        step["execution_blocked"] = False
        step["failed_checks"] = []
    (run_dir / "pipeline_state.json").write_text(
        json.dumps(checkpoint_payload),
        encoding="utf-8",
    )
    (run_dir / "pipeline_result.json").write_text(
        json.dumps(
            {
                "release_ready": False,
                "verification": {
                    "erc": {
                        "errors": 0,
                        "warning_classifications": {
                            "lib_symbol_mismatch": {
                                "resolution": {"status": "blocked"}
                            }
                        },
                    },
                    "drc": {"errors": 0, "unconnected": 0},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    assert (
        ratsnestpro_agent._release_repair_resume_step(  # type: ignore[arg-type]
            {
                "incremental_resume": True,
                "workspace_run_name": run_name,
                "hardware": {},
            }
        )
        == "erc"
    )


def test_pipeline_loader_accepts_the_same_final_erc_resume_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    payload = _checkpoint_payload()
    for step in payload["steps"]:
        step["blocked"] = False
        step["execution_blocked"] = False
        step["failed_checks"] = []
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "pipeline_result.json").write_text(
        json.dumps(
            {
                "release_ready": False,
                "verification": {
                    "erc": {
                        "errors": 0,
                        "warning_classifications": {
                            "lib_symbol_mismatch": {
                                "resolution": {"status": "blocked"}
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    erc_index = CANONICAL_STEPS.index("erc")
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(erc_index)

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    restored = _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ERC,
        resume_token="workflow-final-erc",
    )

    assert restored.completed == list(PipelineStep)[:erc_index]
    assert captured["invalidate_from_step"] == PipelineStep.ERC


def _checkpoint_payload(*, marker_token: str | None = None) -> dict[str, Any]:
    requirement = "Build a board"
    payload = {
        "requirement": requirement,
        "requirement_contract": _requirement_contract_payload(requirement),
        "project_name": "board",
        "revision": 7,
        "intermediate_artifacts": {},
        "steps": _release_blocked_hardware()["steps"],
    }
    if marker_token is not None:
        payload["release_resume"] = {
            "step": "route_signals",
            "token_digest": hashlib.sha256(marker_token.encode("utf-8")).hexdigest(),
        }
    return payload


def _prefix_state(length: int, *, revision: int = 8) -> PipelineState:
    state = PipelineState(
        requirement_text="Build a board",
        project_name="board",
        revision=revision,
    )
    state.results.extend(
        StepResult(step=step) for step in list(PipelineStep)[:length]
    )
    return state


def test_checkpoint_resume_invalidates_only_from_failed_step_and_bumps_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    checkpoint.write_text(
        json.dumps(_checkpoint_payload()),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(CANONICAL_STEPS.index("route_signals"))

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ROUTE_SIGNALS,
        resume_token="workflow-1",
    )

    assert captured["invalidate_from_step"] == PipelineStep.ROUTE_SIGNALS
    assert captured["revision"] == 8
    assert captured["release_resume_step"] == "route_signals"
    assert captured["release_resume_token_digest"] == hashlib.sha256(
        b"workflow-1"
    ).hexdigest()


def test_explicit_resume_retains_failed_artifact_as_repair_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    payload = _checkpoint_payload()
    route_index = CANONICAL_STEPS.index("route_signals")
    payload["steps"][route_index]["used_llm"] = True
    payload["intermediate_artifacts"]["route_signals"] = RouteResult(
        method="freerouting",
        total_connections=62,
        routed_connections=59,
        unconnected=3,
        dsn_path="board.dsn",
        ses_path="board.ses",
    ).model_dump(mode="json")
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        "agents.ratsnestpro.tools.restore_pipeline_state",
        lambda **_kwargs: _prefix_state(route_index),
    )

    restored = _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ROUTE_SIGNALS,
        resume_token="workflow-retain-route-candidate",
    )

    candidate, used_llm = restored.resume_candidates[PipelineStep.ROUTE_SIGNALS]
    assert isinstance(candidate, RouteResult)
    assert candidate.unconnected == 3
    assert candidate.dsn_path == "board.dsn"
    assert used_llm is True


def test_execution_blocked_checkpoint_retries_the_failed_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    payload = _checkpoint_payload()
    route_index = CANONICAL_STEPS.index("route_signals")
    payload["steps"] = payload["steps"][: route_index + 1]
    payload["steps"][-1]["blocked"] = True
    payload["steps"][-1]["execution_blocked"] = True
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(route_index)

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    restored = _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ROUTE_SIGNALS,
        resume_token="workflow-2",
    )

    assert restored.completed == list(PipelineStep)[:route_index]
    assert captured["invalidate_from_step"] == PipelineStep.ROUTE_SIGNALS


def test_partial_checkpoint_continues_at_next_canonical_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    payload = _checkpoint_payload()
    payload["steps"] = payload["steps"][:8]
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(8)

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep(CANONICAL_STEPS[8]),
        resume_token="workflow-3",
    )

    assert captured["invalidate_from_step"] == PipelineStep(CANONICAL_STEPS[8])


def test_checkpoint_resume_rejects_an_incomplete_upstream_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    checkpoint.write_text(json.dumps(_checkpoint_payload()), encoding="utf-8")
    monkeypatch.setattr(
        "agents.ratsnestpro.tools.restore_pipeline_state",
        lambda **_kwargs: _prefix_state(13),
    )

    with pytest.raises(ValueError, match="exact verified prefix"):
        _load_pipeline_state(
            checkpoint,
            "Build a board",
            "board",
            resume_from_step=PipelineStep.ROUTE_SIGNALS,
            resume_token="workflow-1",
        )


def test_checkpoint_writer_rejects_an_older_revision(tmp_path: Path) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    _write_pipeline_state(
        checkpoint,
        "Build a board",
        _prefix_state(1, revision=3),
    )

    with pytest.raises(PipelineCheckpointRegressionError, match="stale"):
        _write_pipeline_state(
            checkpoint,
            "Build a board",
            _prefix_state(2, revision=2),
        )


def test_resume_without_checkpoint_fails_instead_of_rebuilding_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agents.ratsnestpro.tools._run_dir", lambda _name: tmp_path)
    monkeypatch.setattr("agents.ratsnestpro.tools._workspace_root", lambda: tmp_path)

    result = json.loads(
        _run_pcb_pipeline_unlocked(
            "Build a board",
            run_name="run-1",
            project_name="board",
            llm_mode="offline",
            until_step="route_signals",
            resume_from_step="route_signals",
            resume_token="workflow-1",
        )
    )

    assert result["status"] == "error"
    assert result["error_type"] == "configuration_error"
    assert "existing verified pipeline checkpoint" in result["error"]


def test_same_resume_activity_retry_does_not_invalidate_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    checkpoint.write_text(
        json.dumps(_checkpoint_payload(marker_token="workflow-1")),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(15, revision=7)

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ROUTE_SIGNALS,
        resume_token="workflow-1",
    )

    assert captured["invalidate_from_step"] is None
    assert captured["revision"] == 7


def test_new_revision_accepts_checkpoint_already_truncated_by_cancelled_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    payload = _checkpoint_payload(marker_token="old-workflow")
    resume_index = CANONICAL_STEPS.index("route_signals")
    payload["steps"] = payload["steps"][: resume_index - 2]
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_restore(**kwargs: Any) -> PipelineState:
        captured.update(kwargs)
        return _prefix_state(resume_index - 2, revision=7)

    monkeypatch.setattr("agents.ratsnestpro.tools.restore_pipeline_state", fake_restore)

    restored = _load_pipeline_state(
        checkpoint,
        "Build a board",
        "board",
        resume_from_step=PipelineStep.ROUTE_SIGNALS,
        resume_token="new-workflow",
    )

    assert restored.completed == list(PipelineStep)[: resume_index - 2]
    assert captured["invalidate_from_step"] is None
    assert captured["release_resume_token_digest"] == hashlib.sha256(
        b"new-workflow"
    ).hexdigest()


def test_checkpoint_resume_rejects_noncanonical_or_non_earliest_step(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "pipeline_state.json"
    checkpoint.write_text(
        json.dumps(_checkpoint_payload()),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="earliest failed or incomplete step"):
        _load_pipeline_state(
            checkpoint,
            "Build a board",
            "board",
            resume_from_step=PipelineStep.MANUFACTURE,
            resume_token="workflow-1",
        )
    with pytest.raises(ValueError, match="canonical pipeline step"):
        temporal_workflow._resume_start_index("not-a-step")


def test_temporal_workflow_starts_at_actual_resume_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_steps: list[str] = []
    commands: list[dict[str, Any]] = []
    fixed_now = datetime(2026, 8, 26, tzinfo=UTC)

    def fake_start_activity(
        _activity: str,
        command: dict[str, Any],
        **_kwargs: Any,
    ) -> Any:
        async def result() -> dict[str, Any]:
            step = str(command["step"])
            executed_steps.append(step)
            commands.append(dict(command))
            return {
                "status": "ok",
                "target_reached": True,
                "execution_blocked": False,
                "completed_steps": CANONICAL_STEPS.index(step) + 1,
                "manifest_path": "manifest.json",
                "manifest_digest": "a" * 64,
                "pipeline_result_path": "pipeline_result.json",
                "run_directory": "run",
            }

        return result()

    async def fake_execute_activity(
        activity_name: str,
        _command: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert activity_name == READ_RESULT_ACTIVITY
        return {"status": "ok", "release_ready": True}

    monkeypatch.setattr(temporal_workflow.workflow, "now", lambda: fixed_now)
    monkeypatch.setattr(
        temporal_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="workflow-1"),
    )
    monkeypatch.setattr(
        temporal_workflow.workflow,
        "start_activity",
        fake_start_activity,
    )
    monkeypatch.setattr(
        temporal_workflow.workflow,
        "execute_activity",
        fake_execute_activity,
    )
    workflow = temporal_workflow.RatsNestHardwareWorkflow()

    result = asyncio.run(
        workflow.run(
            {
                "run_id": "request-1",
                "requirement": "Build a board",
                "requirement_hash": hashlib.sha256(b"Build a board").hexdigest(),
                "run_name": "run-1",
                "project_name": "board",
                "resume_from_step": "route_signals",
                "retry_attempts": 1,
                "step_timeout_seconds": 120,
                "routing_timeout_seconds": 120,
                "workflow_timeout_seconds": 600,
            }
        )
    )

    assert executed_steps == list(CANONICAL_STEPS[14:])
    assert "manifest_digest" not in commands[0]
    assert all(command["manifest_digest"] == "a" * 64 for command in commands[1:])
    assert result["release_ready"] is True
    assert workflow.progress()["completed_steps"] == len(CANONICAL_STEPS)


def test_temporal_manifest_binds_resume_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    requirement = "Build a board"
    command = {
        "workflow_id": "workflow-1",
        "requirement": requirement,
        "requirement_hash": hashlib.sha256(requirement.encode()).hexdigest(),
        "run_name": "run-1",
        "project_name": "board",
        "resume_from_step": "route_signals",
    }

    path, manifest = activities._manifest(command)
    manifest_digest = activities._manifest_content_digest(manifest)

    assert manifest["resume_from_step"] == "route_signals"
    activities._manifest(
        {
            "manifest_path": str(path),
            "workflow_id": "workflow-1",
            "requirement_hash": command["requirement_hash"],
            "resume_from_step": "route_signals",
            "manifest_digest": manifest_digest,
        }
    )
    with pytest.raises(ValueError, match="resume step mismatch"):
        activities._manifest(
            {
                "manifest_path": str(path),
                "workflow_id": "workflow-1",
                "requirement_hash": command["requirement_hash"],
                "resume_from_step": "manufacture",
                "manifest_digest": manifest_digest,
            }
        )

    tampered = {**manifest, "project_name": "other-board"}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="content digest mismatch"):
        activities._manifest(
            {
                "manifest_path": str(path),
                "workflow_id": "workflow-1",
                "requirement_hash": command["requirement_hash"],
                "resume_from_step": "route_signals",
                "manifest_digest": manifest_digest,
            }
        )

    path.write_text(
        json.dumps({**manifest, "requirement": "Tampered requirement"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requirement digest mismatch"):
        activities._manifest(
            {
                "manifest_path": str(path),
                "workflow_id": "workflow-1",
                "requirement_hash": command["requirement_hash"],
                "resume_from_step": "route_signals",
                "manifest_digest": manifest_digest,
            }
        )


def test_legacy_dispatch_supplies_bounded_resume_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "temporal_enabled", lambda: False)

    run_ref = asyncio.run(
        client.dispatch_hardware_workflow(
            request_id="request-1",
            requirement="Build a board",
            run_name="run-1",
            workspace_run_name="workspace-1",
            execution_scope="internal",
            project_name="board",
            llm_mode="required",
            model_name=None,
            model_type=None,
            attempt=1,
            resume_from_step="route_signals",
        )
    )

    assert run_ref["input"]["resume_from_step"] == "route_signals"
    assert run_ref["input"]["resume_token"] == "request-1"


def test_temporal_dispatch_omits_empty_resume_from_identity_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        async def start_workflow(
            self,
            _run: Any,
            workflow_input: dict[str, Any],
            **_kwargs: Any,
        ) -> None:
            captured.update(workflow_input)

    async def fake_connect() -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(client, "temporal_enabled", lambda: True)
    monkeypatch.setattr(client, "connect_temporal", fake_connect)

    asyncio.run(
        client.dispatch_hardware_workflow(
            request_id="request-1",
            requirement="Build a board",
            run_name="run-1",
            workspace_run_name="workspace-1",
            execution_scope="internal",
            project_name="board",
            llm_mode="required",
            model_name=None,
            model_type=None,
            attempt=1,
        )
    )

    assert "resume_from_step" not in captured


def test_temporal_failure_patch_preserves_legacy_compensation_command() -> None:
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="ratsnest.execute_pipeline_step",
        activity_id="15",
        retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED,
    )

    legacy = temporal_workflow._activity_failure_contract(error, detailed=False)
    detailed = temporal_workflow._activity_failure_contract(error, detailed=True)

    assert legacy[1] == f"ActivityError: {error}"
    assert legacy[2] == "activity retries exhausted"
    assert detailed[1].startswith("Temporal Activity failed after bounded retries:")
