"""Temporal Activities that advance the canonical RatsNestPro pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agents.ratsnestpro.temporal.contracts import (
    COMPENSATE_ACTIVITY,
    EXECUTE_STEP_ACTIVITY,
    READ_RESULT_ACTIVITY,
    compact_pipeline_result,
    safe_name,
)

_TRANSIENT_TYPES = {
    "transient_io_error",
    "subprocess_timeout",
}
_TRANSIENT_TEXT = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "internal server error",
    "service unavailable",
    "connection aborted",
    "connection reset",
    "connection refused",
    "rate limit",
    "too many requests",
    " 429",
    " 502",
    " 503",
    " 504",
    "could not lock run directory",
)


def _workspace_root() -> Path:
    return Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")).expanduser().resolve()


def verify_workspace_writable() -> None:
    """Fail before polling Temporal when the shared run directory is not writable."""

    runs = _workspace_root() / "runs"
    try:
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".worker-write-probe-", dir=runs) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        raise RuntimeError(
            f"RatsNestPro run workspace is not writable: {runs}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _within_workspace(value: str | Path) -> Path:
    root = _workspace_root()
    candidate = Path(value).expanduser().resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes RatsNestPro workspace: {candidate}")
    return candidate


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _manifest(command: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Persist the large immutable request once instead of once per Activity."""

    manifest_value = command.get("manifest_path")
    if manifest_value:
        path = _within_workspace(str(manifest_value))
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Temporal run manifest must contain a JSON object")
        if value.get("workflow_id") != command.get("workflow_id"):
            raise ValueError("Temporal run manifest workflow identity mismatch")
        if value.get("requirement_hash") != command.get("requirement_hash"):
            raise ValueError("Temporal run manifest requirement digest mismatch")
        return path, value

    requirement = str(command.get("requirement", "")).strip()
    if not requirement:
        raise ValueError("first pipeline Activity requires a non-empty requirement")
    run_name = safe_name(str(command.get("run_name", "")), "design")
    workflow_id = str(command.get("workflow_id", "")).strip()
    expected_hash = str(command.get("requirement_hash", "")).strip()
    actual_hash = hashlib.sha256(requirement.encode("utf-8")).hexdigest()
    if not workflow_id or expected_hash != actual_hash:
        raise ValueError("Temporal first-step identity or requirement digest is invalid")
    manifest_id = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:20]
    path = (
        _workspace_root()
        / "runs"
        / run_name
        / f"temporal_input-{manifest_id}.json"
    )
    value = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "requirement_hash": actual_hash,
        "requirement": requirement,
        "run_name": str(command.get("run_name", run_name)),
        "display_run_name": str(command.get("display_run_name", run_name)),
        "execution_scope": str(command.get("execution_scope", "legacy")),
        "project_name": str(command.get("project_name", "board")),
        "llm_mode": str(command.get("llm_mode", "required")),
        "model_name": (
            str(command["model_name"]) if command.get("model_name") else None
        ),
        "model_type": (
            str(command["model_type"]) if command.get("model_type") else None
        ),
        "ahe_budget": (
            dict(command["ahe_budget"])
            if isinstance(command.get("ahe_budget"), dict)
            else {}
        ),
    }
    _atomic_json(path, value)
    return path, value


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            pass
        # The Python parent may exit before Java/KiCad descendants. Always
        # probe the process group after the grace period.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            await process.wait()
        return

    # Windows process groups do not make terminate()/kill() recursive.
    if process.returncode is None:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await killer.wait()
    if process.returncode is None:
        process.kill()
        await process.wait()


def _tail(path: Path, limit: int = 2_000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        return handle.read(limit).decode("utf-8", errors="replace")


async def _run_child(command: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ratsnest-temporal-") as temporary:
        command_path = Path(temporary) / "command.json"
        result_path = Path(temporary) / "result.json"
        command_path.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
        process_options: dict[str, Any] = {}
        if os.name == "posix":
            process_options["start_new_session"] = True
        else:
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        stdout_path = Path(temporary) / "stdout.log"
        stderr_path = Path(temporary) / "stderr.log"
        with stdout_path.open("wb") as stdout_handle, stderr_path.open(
            "wb"
        ) as stderr_handle:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "agents.ratsnestpro.temporal.step_runner",
                str(command_path),
                str(result_path),
                stdout=stdout_handle,
                stderr=stderr_handle,
                **process_options,
            )
            wait_task = asyncio.create_task(process.wait())
            started = asyncio.get_running_loop().time()
            try:
                while not wait_task.done():
                    elapsed = asyncio.get_running_loop().time() - started
                    if elapsed >= timeout_seconds:
                        await _terminate_process_tree(process)
                        await wait_task
                        raise ApplicationError(
                            f"pipeline child exceeded {timeout_seconds:.0f}s",
                            type="TransientPipelineError",
                        )
                    activity.heartbeat(
                        {"pid": process.pid, "elapsed_seconds": round(elapsed, 1)}
                    )
                    heartbeat_seconds = max(
                        0.5,
                        float(command.get("heartbeat_seconds", 15)) / 2,
                    )
                    await asyncio.wait(
                        {wait_task},
                        timeout=min(
                            5.0,
                            heartbeat_seconds,
                            timeout_seconds - elapsed,
                        ),
                    )
            except asyncio.CancelledError:
                await _terminate_process_tree(process)
                await wait_task
                raise

        if not result_path.is_file():
            detail = _tail(stderr_path) or _tail(stdout_path)
            raise ApplicationError(
                f"pipeline child returned {process.returncode} without JSON: {detail}",
                type="TransientPipelineError",
            )
        value = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ApplicationError(
                "pipeline child returned a non-object JSON value",
                type="PermanentPipelineError",
                non_retryable=True,
            )
        return value


def _transient_error(payload: dict[str, Any]) -> bool:
    error_type = str(payload.get("error_type", "")).casefold()
    error = str(payload.get("error", "")).casefold()
    return (
        error_type in _TRANSIENT_TYPES
        or any(token in error for token in _TRANSIENT_TEXT)
        or re.search(r"\b5\d\d\b", error) is not None
    )


@activity.defn(name=EXECUTE_STEP_ACTIVITY)
async def execute_pipeline_step(command: dict[str, Any]) -> dict[str, Any]:
    try:
        step = str(command["step"])
        manifest_path, manifest = _manifest(command)
    except PermissionError as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="WorkspacePermissionError",
            non_retryable=True,
        ) from exc
    except OSError as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="TransientPipelineError",
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="PermanentPipelineError",
            non_retryable=True,
        ) from exc
    runner_command = {
        **manifest,
        "step": step,
    }
    payload = await _run_child(
        runner_command,
        timeout_seconds=max(1.0, float(command.get("local_timeout_seconds", 600))),
    )
    if payload.get("status") == "error":
        message = str(payload.get("error", "pipeline execution failure"))
        if _transient_error(payload):
            raise ApplicationError(message, type="TransientPipelineError")
        raise ApplicationError(
            message,
            type="PermanentPipelineError",
            non_retryable=True,
        )
    summary = compact_pipeline_result(payload, step)
    summary["manifest_path"] = str(manifest_path)
    summary["activity_attempt"] = activity.info().attempt
    return summary


@activity.defn(name=READ_RESULT_ACTIVITY)
async def read_pipeline_result(command: dict[str, Any]) -> dict[str, Any]:
    value = str(command.get("pipeline_result_path", ""))
    if not value:
        return {
            "status": "error",
            "outcome": "execution_blocked",
            "error": "pipeline result path was not produced",
            "release_blockers": ["pipeline result path was not produced"],
        }
    path = _within_workspace(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApplicationError(str(exc), type="TransientPipelineError") from exc
    if not isinstance(payload, dict):
        raise ApplicationError(
            "pipeline_result.json must contain an object",
            type="PermanentPipelineError",
            non_retryable=True,
        )
    return payload


@activity.defn(name=COMPENSATE_ACTIVITY)
async def compensate_pipeline_run(command: dict[str, Any]) -> dict[str, Any]:
    """Saga compensation preserves artifacts and records a resumable incident."""

    run_directory = str(command.get("run_directory", ""))
    if run_directory:
        target_dir = _within_workspace(run_directory)
    else:
        target_dir = (
            _workspace_root()
            / "runs"
            / safe_name(str(command.get("run_name", "")), "design")
        )
    target = target_dir / "temporal_recovery.json"
    payload = {
        "schema_version": 1,
        "status": "preserved_for_resume",
        "recorded_at": datetime.now(UTC).isoformat(),
        "workflow_id": str(command.get("workflow_id", "")),
        "failed_step": str(command.get("failed_step", "")),
        "completed_steps": int(command.get("completed_steps", 0) or 0),
        "reason": str(command.get("reason", "workflow interrupted")),
        "artifacts_deleted": False,
    }
    try:
        _atomic_json(target, payload)
    except PermissionError as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="WorkspacePermissionError",
            non_retryable=True,
        ) from exc
    return {"status": "preserved_for_resume", "recovery_path": str(target)}
