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
from time import monotonic
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agents.ratsnestpro.temporal.contracts import (
    CANONICAL_STEPS,
    COMPENSATE_ACTIVITY,
    EXECUTE_STEP_ACTIVITY,
    READ_CHECKPOINT_ACTIVITY,
    READ_RESULT_ACTIVITY,
    checkpoint_receipt,
    compact_pipeline_result,
    safe_name,
)
from core import settings
from observability import operation_span, record_pipeline_step
from ratsnestpro.orchestration.component_resolution import verified_replacements_by_ref
from service.governance_scope import (
    TrustedGovernanceScope,
    verify_governance_scope_token,
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

_GOVERNANCE_FIELDS = (
    "tenant_scope",
    "project_scope",
    "run_scope",
    "harness_version_id",
    "harness_manifest_digest",
)


def _verified_governance_scope(
    value: dict[str, Any],
) -> TrustedGovernanceScope | None:
    token = str(value.get("governance_scope_token", "")).strip()
    if not token:
        return None
    if settings.RATSNEST_INTERNAL_SIGNING_SECRET is None:
        return None
    try:
        scope = verify_governance_scope_token(
            token,
            secret=settings.RATSNEST_INTERNAL_SIGNING_SECRET.get_secret_value(),
        )
    except ValueError:
        return None
    for field in _GOVERNANCE_FIELDS:
        if str(value.get(field, "")) != str(getattr(scope, field)):
            return None
    return scope


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


def _canonical_resume_step(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in CANONICAL_STEPS:
        raise ValueError("resume_from_step must be a canonical pipeline step")
    return value


def _manifest_content_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        requirement = str(value.get("requirement", ""))
        actual_requirement_hash = hashlib.sha256(
            requirement.encode("utf-8")
        ).hexdigest()
        if (
            value.get("requirement_hash") != actual_requirement_hash
            or actual_requirement_hash != command.get("requirement_hash")
        ):
            raise ValueError("Temporal run manifest requirement digest mismatch")
        replacement_secret = (
            settings.RATSNEST_INTERNAL_SIGNING_SECRET.get_secret_value()
            if settings.RATSNEST_INTERNAL_SIGNING_SECRET is not None
            else None
        )
        if value.get("approved_component_replacements"):
            value["approved_component_replacements"] = {
                ref: replacement.model_dump(mode="json")
                for ref, replacement in verified_replacements_by_ref(
                    value.get("approved_component_replacements"),
                    secret=replacement_secret,
                ).items()
            }
        manifest_resume = _canonical_resume_step(value.get("resume_from_step"))
        command_resume = _canonical_resume_step(command.get("resume_from_step"))
        if manifest_resume != command_resume:
            raise ValueError("Temporal run manifest resume step mismatch")
        command_scope = _verified_governance_scope(command)
        scope_matches_manifest = bool(
            command_scope is not None
            and all(
                str(value.get(field, "")) == str(getattr(command_scope, field))
                for field in _GOVERNANCE_FIELDS
            )
        )
        if not scope_matches_manifest:
            value = {
                key: item
                for key, item in value.items()
                if key not in {*_GOVERNANCE_FIELDS, "governance_scope_token"}
            }
        expected_manifest_digest = str(command.get("manifest_digest", "")).strip()
        if (
            expected_manifest_digest
            and _manifest_content_digest(value) != expected_manifest_digest
        ):
            raise ValueError("Temporal run manifest content digest mismatch")
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
    governance_scope = _verified_governance_scope(command)
    resume_from_step = _canonical_resume_step(command.get("resume_from_step"))
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
    replacement_secret = (
        settings.RATSNEST_INTERNAL_SIGNING_SECRET.get_secret_value()
        if settings.RATSNEST_INTERNAL_SIGNING_SECRET is not None
        else None
    )
    if command.get("approved_component_replacements"):
        value["approved_component_replacements"] = {
            ref: replacement.model_dump(mode="json")
            for ref, replacement in verified_replacements_by_ref(
                command.get("approved_component_replacements"),
                secret=replacement_secret,
            ).items()
        }
    if resume_from_step:
        value["resume_from_step"] = resume_from_step
    if governance_scope is not None:
        value.update(
            {
                **{
                    field: str(getattr(governance_scope, field))
                    for field in _GOVERNANCE_FIELDS
                },
            }
        )
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


async def _execute_pipeline_step(command: dict[str, Any]) -> dict[str, Any]:
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
    runner_command: dict[str, Any] = {
        **manifest,
        "step": step,
    }
    governance_scope = _verified_governance_scope(command)
    if governance_scope is not None and all(
        str(manifest.get(field, "")) == str(getattr(governance_scope, field))
        for field in _GOVERNANCE_FIELDS
    ):
        runner_command.update(
            {
                **{
                    field: str(getattr(governance_scope, field))
                    for field in _GOVERNANCE_FIELDS
                },
                "governance_scope_token": str(command["governance_scope_token"]),
            }
        )
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
    summary["manifest_digest"] = _manifest_content_digest(manifest)
    summary["activity_attempt"] = activity.info().attempt
    state_path = str(summary.get("pipeline_state_path", ""))
    if state_path:
        try:
            state_payload = json.loads(
                _within_workspace(state_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError):
            state_payload = {}
        if isinstance(state_payload, dict):
            receipt = checkpoint_receipt(state_payload.get("checkpoint_receipt"))
            if receipt is not None:
                summary["checkpoint_receipt"] = receipt
    return summary


@activity.defn(name=EXECUTE_STEP_ACTIVITY)
async def execute_pipeline_step(command: dict[str, Any]) -> dict[str, Any]:
    """Execute one durable step with explicit Agent/Temporal telemetry."""

    step = str(command.get("step", "unknown"))[:96]
    try:
        attempt = activity.info().attempt
    except RuntimeError:
        attempt = 1
    started = monotonic()
    outcome = "error"
    try:
        with operation_span(
            "agent.pipeline.step",
            {"workflow.step": step, "workflow.step.attempt": attempt},
        ) as span:
            result = await _execute_pipeline_step(command)
            outcome = str(result.get("status", "unknown"))[:96]
            span.set_attribute("workflow.step.outcome", outcome)
            return result
    finally:
        record_pipeline_step(
            step=step,
            outcome=outcome,
            attempt=attempt,
            duration_seconds=monotonic() - started,
        )


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


@activity.defn(name=READ_CHECKPOINT_ACTIVITY)
async def read_pipeline_checkpoint(command: dict[str, Any]) -> dict[str, Any]:
    """Reconcile Temporal progress with the latest committed state receipt."""

    state_value = str(command.get("pipeline_state_path", "")).strip()
    if state_value:
        path = _within_workspace(state_value)
    else:
        run_name = safe_name(str(command.get("run_name", "")), "design")
        path = _workspace_root() / "runs" / run_name / "pipeline_state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "missing", "pipeline_state_path": str(path)}
    except (OSError, ValueError, TypeError) as exc:
        raise ApplicationError(
            f"checkpoint reconciliation failed: {type(exc).__name__}: {exc}",
            type="TransientPipelineError",
        ) from exc
    if not isinstance(payload, dict):
        raise ApplicationError(
            "pipeline_state.json must contain an object",
            type="PermanentPipelineError",
            non_retryable=True,
        )
    receipt = checkpoint_receipt(payload.get("checkpoint_receipt"))
    if receipt is None:
        return {
            "status": "legacy",
            "completed_steps": int(payload.get("completed_steps", 0) or 0),
            "pipeline_state_path": str(path),
            "run_directory": str(path.parent),
        }
    return {
        "status": "ok",
        "completed_steps": receipt["committed_step_index"],
        "expected_step": receipt["next_step"],
        "pipeline_state_path": str(path),
        "pipeline_result_path": str(path.with_name("pipeline_result.json")),
        "run_directory": str(path.parent),
        "checkpoint_receipt": receipt,
    }


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
