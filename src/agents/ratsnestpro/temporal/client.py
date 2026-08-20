"""LangGraph-facing Temporal client with an explicit legacy adapter."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from agents.ratsnestpro.temporal.contracts import (
    ahe_event_filename,
    hardware_workflow_id,
    hardware_workflow_identity,
    llm_transcript_filename,
    requirement_digest,
    safe_name,
    verify_hardware_workflow_identity,
)
from agents.ratsnestpro.temporal.workflow import RatsNestHardwareWorkflow
from core import settings
from service.ahe_event import RedisAheEventReader, stream_ahe_event_record
from service.durable_event_stream import RedisEventStreamConfig
from service.llm_output import stream_llm_output_record
from service.llm_output_stream import (
    LlmOutputRedisConfig,
    RedisLlmOutputReader,
)

ProgressCallback = Callable[[dict[str, Any]], None]
_temporal_client: Client | None = None
_temporal_client_lock = asyncio.Lock()


class TemporalWorkflowIdentityConflict(RuntimeError):
    """A Workflow ID exists but cannot safely be attached to this input."""


def temporal_enabled() -> bool:
    return bool(settings.RATSNESTPRO_TEMPORAL_ENABLED)


async def connect_temporal() -> Client:
    global _temporal_client
    if _temporal_client is not None:
        return _temporal_client
    api_key = (
        settings.TEMPORAL_API_KEY.get_secret_value()
        if settings.TEMPORAL_API_KEY is not None
        else None
    )
    async with _temporal_client_lock:
        if _temporal_client is None:
            _temporal_client = await Client.connect(
                settings.TEMPORAL_ADDRESS,
                namespace=settings.TEMPORAL_NAMESPACE,
                api_key=api_key,
                tls=settings.TEMPORAL_TLS,
            )
        return _temporal_client


async def signal_hardware_workflow(run_ref: dict[str, Any], action: str) -> dict[str, Any]:
    """Send a cooperative control signal without cancelling an HTTP wait task."""

    signals = {
        "pause": "request_pause",
        "resume": "request_resume",
        "cancel": "request_cancel",
    }
    try:
        signal_name = signals[action]
    except KeyError as exc:
        raise ValueError(f"unsupported hardware workflow action: {action}") from exc
    if run_ref.get("mode") != "temporal" or not run_ref.get("workflow_id"):
        raise ValueError("hardware workflow signals require a Temporal workflow ID")
    client = await connect_temporal()
    handle = client.get_workflow_handle(str(run_ref["workflow_id"]))
    await handle.signal(signal_name)
    return {
        "status": "signal_sent",
        "action": action,
        "workflow_id": str(run_ref["workflow_id"]),
    }


async def signal_hardware_workflow_by_request_id(
    request_id: str,
    action: str,
) -> dict[str, Any]:
    """Signal the one Hardware Workflow owned by ``request_id``.

    A missing workflow is expected when cancellation arrives before the graph
    reaches Hardware Engineer. Transport, permission and availability failures
    remain visible to the caller so cancellation is never falsely acknowledged.
    """

    signals = {"pause": "request_pause", "resume": "request_resume", "cancel": "request_cancel"}
    try:
        signal_name = signals[action]
    except KeyError as exc:
        raise ValueError(f"unsupported hardware workflow action: {action}") from exc
    workflow_id = hardware_workflow_id(request_id)
    client = await connect_temporal()
    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.signal(signal_name)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return {
                "status": "not_found",
                "action": action,
                "workflow_id": workflow_id,
            }
        raise
    return {
        "status": "signal_sent",
        "action": action,
        "workflow_id": workflow_id,
    }


async def dispatch_hardware_workflow(
    *,
    request_id: str,
    requirement: str,
    run_name: str,
    workspace_run_name: str,
    execution_scope: str,
    project_name: str,
    llm_mode: str,
    model_name: str | None,
    model_type: str | None,
    attempt: int,
    ahe_budget: dict[str, int] | None = None,
    tenant_scope: str = "",
    project_scope: str = "",
    run_scope: str = "",
    harness_version_id: str = "",
    harness_manifest_digest: str = "",
    governance_scope_token: str = "",
) -> dict[str, Any]:
    """Start once, or attach when a LangGraph checkpoint replays dispatch."""

    if not temporal_enabled():
        return {
            "mode": "legacy",
            "status": "ready",
            "request_id": request_id,
            "input": {
                "requirement": requirement,
                "run_name": workspace_run_name,
                "project_name": project_name,
                "llm_mode": llm_mode,
                "model_name": model_name,
                "model_type": model_type,
                "ahe_budget": ahe_budget or {},
            },
            "run_name": run_name,
            "workspace_run_name": workspace_run_name,
            "execution_scope": execution_scope,
        }

    client = await connect_temporal()
    workflow_id = hardware_workflow_id(request_id)
    profile_wall_seconds = max(
        60,
        int((ahe_budget or {}).get("max_wall_clock_minutes", 60)) * 60,
    )
    workflow_timeout_seconds = min(
        settings.RATSNESTPRO_TEMPORAL_WORKFLOW_TIMEOUT_SECONDS,
        profile_wall_seconds,
    )
    workflow_input = {
        "run_id": request_id,
        "requirement": requirement,
        "requirement_hash": requirement_digest(requirement),
        "run_name": workspace_run_name,
        "display_run_name": run_name,
        "execution_scope": execution_scope,
        "project_name": project_name,
        "llm_mode": llm_mode,
        "model_name": model_name,
        "model_type": model_type,
        "ahe_budget": ahe_budget or {},
        "tenant_scope": tenant_scope,
        "project_scope": project_scope,
        "run_scope": run_scope,
        "harness_version_id": harness_version_id,
        "harness_manifest_digest": harness_manifest_digest,
        "governance_scope_token": governance_scope_token,
        "retry_attempts": settings.RATSNESTPRO_TEMPORAL_RETRY_ATTEMPTS,
        "step_timeout_seconds": settings.RATSNESTPRO_TEMPORAL_STEP_TIMEOUT_SECONDS,
        "routing_timeout_seconds": settings.RATSNESTPRO_TEMPORAL_ROUTING_TIMEOUT_SECONDS,
        "heartbeat_seconds": settings.RATSNESTPRO_TEMPORAL_HEARTBEAT_SECONDS,
        "workflow_timeout_seconds": workflow_timeout_seconds,
    }
    expected_identity = hardware_workflow_identity(workflow_input)
    status = "started"
    try:
        await client.start_workflow(
            RatsNestHardwareWorkflow.run,
            workflow_input,
            id=workflow_id,
            task_queue=settings.RATSNESTPRO_TEMPORAL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            execution_timeout=timedelta(
                seconds=(workflow_timeout_seconds + 300)
            ),
        )
    except WorkflowAlreadyStartedError as exc:
        handle = client.get_workflow_handle(workflow_id)
        try:
            actual_identity = await asyncio.wait_for(
                handle.query(RatsNestHardwareWorkflow.identity),
                timeout=min(
                    15.0,
                    float(settings.RATSNESTPRO_AGENT_CALL_TIMEOUT_SECONDS),
                ),
            )
            verify_hardware_workflow_identity(expected_identity, actual_identity)
        except Exception as identity_exc:  # noqa: BLE001 - fail closed at durable boundary
            raise TemporalWorkflowIdentityConflict(
                f"refusing to attach to Temporal workflow {workflow_id}: {identity_exc}"
            ) from exc
        status = "attached"

    return {
        "mode": "temporal",
        "status": status,
        "request_id": request_id,
        "workflow_id": workflow_id,
        "run_name": run_name,
        "workspace_run_name": workspace_run_name,
        "execution_scope": execution_scope,
        "project_name": project_name,
        "attempt": attempt,
    }


async def _await_legacy(run_ref: dict[str, Any]) -> dict[str, Any]:
    from agents.ratsnestpro.tools import ratsnest_run_pcb_pipeline

    command = dict(run_ref.get("input", {}))
    raw = await asyncio.to_thread(ratsnest_run_pcb_pipeline, **command)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("legacy pipeline returned a non-object JSON value")
    return value


def _llm_transcript_path(run_ref: dict[str, Any]) -> Path:
    root = Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")).expanduser().resolve()
    run_name = safe_name(
        str(run_ref.get("workspace_run_name", run_ref.get("run_name", ""))),
        "design",
    )
    return root / "runs" / run_name / llm_transcript_filename(str(run_ref["workflow_id"]))


def _ahe_event_path(run_ref: dict[str, Any]) -> Path:
    root = Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")).expanduser().resolve()
    run_name = safe_name(
        str(run_ref.get("workspace_run_name", run_ref.get("run_name", ""))),
        "design",
    )
    return root / "runs" / run_name / ahe_event_filename(str(run_ref["workflow_id"]))


def _forward_jsonl_records(
    path: Path,
    cursor: int,
    callback: ProgressCallback | None,
    seen_record_ids: set[str],
    *,
    expected_kind: str,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> int:
    """Forward complete JSONL records while safely skipping partial writes."""

    if callback is None or not path.is_file():
        return cursor
    with path.open("rb") as handle:
        handle.seek(cursor)
        while True:
            line_start = handle.tell()
            line = handle.readline()
            if not line:
                return handle.tell()
            if not line.endswith(b"\n"):
                return line_start
            cursor = handle.tell()
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("kind") != expected_kind:
                continue
            record_id = str(record.get("record_id", ""))
            if record_id:
                if record_id in seen_record_ids:
                    continue
                seen_record_ids.add(record_id)
            callback(transform(record))


def _forward_llm_outputs(
    path: Path,
    cursor: int,
    callback: ProgressCallback | None,
    seen_record_ids: set[str],
) -> int:
    return _forward_jsonl_records(
        path,
        cursor,
        callback,
        seen_record_ids,
        expected_kind="llm_output",
        transform=lambda record: stream_llm_output_record(
            record,
            transcript_path=str(path),
        ),
    )


def _forward_ahe_events(
    path: Path,
    cursor: int,
    callback: ProgressCallback | None,
    seen_record_ids: set[str],
) -> int:
    return _forward_jsonl_records(
        path,
        cursor,
        callback,
        seen_record_ids,
        expected_kind="ahe_event",
        transform=lambda record: stream_ahe_event_record(
            record,
            audit_path=str(path),
        ),
    )


def _llm_stream_config() -> LlmOutputRedisConfig:
    return LlmOutputRedisConfig(
        enabled=settings.RATSNESTPRO_LLM_STREAM_ENABLED,
        url=(
            settings.REDIS_URL.get_secret_value()
            if settings.REDIS_URL is not None
            else None
        ),
        key_prefix=settings.REDIS_KEY_PREFIX,
        maxlen=settings.RATSNESTPRO_LLM_STREAM_MAXLEN,
        ttl_seconds=settings.RATSNESTPRO_LLM_STREAM_TTL_SECONDS,
        socket_timeout_seconds=settings.RATSNESTPRO_LLM_STREAM_SOCKET_TIMEOUT_SECONDS,
    )


def _ahe_stream_config() -> RedisEventStreamConfig:
    return RedisEventStreamConfig(
        enabled=settings.REDIS_URL is not None,
        url=(
            settings.REDIS_URL.get_secret_value()
            if settings.REDIS_URL is not None
            else None
        ),
        key_prefix=settings.REDIS_KEY_PREFIX,
        maxlen=settings.RATSNESTPRO_LLM_STREAM_MAXLEN,
        ttl_seconds=settings.RATSNESTPRO_LLM_STREAM_TTL_SECONDS,
        socket_timeout_seconds=settings.RATSNESTPRO_LLM_STREAM_SOCKET_TIMEOUT_SECONDS,
    )


async def _forward_redis_llm_outputs(
    reader: RedisLlmOutputReader,
    cursor: str,
    callback: ProgressCallback | None,
    seen_record_ids: set[str],
) -> str:
    if callback is None:
        return cursor
    last_id, records = await reader.read_after(cursor)
    for _, record in records:
        record_id = str(record.get("record_id", ""))
        if record_id and record_id in seen_record_ids:
            continue
        if record_id:
            seen_record_ids.add(record_id)
        callback(stream_llm_output_record(record))
    return last_id


async def _forward_redis_ahe_events(
    reader: RedisAheEventReader,
    cursor: str,
    callback: ProgressCallback | None,
    seen_record_ids: set[str],
) -> str:
    if callback is None:
        return cursor
    last_id, records = await reader.read_after(cursor)
    for _, record in records:
        record_id = str(record.get("record_id", ""))
        if record_id and record_id in seen_record_ids:
            continue
        if record_id:
            seen_record_ids.add(record_id)
        callback(stream_ahe_event_record(record))
    return last_id


async def await_hardware_workflow(
    run_ref: dict[str, Any],
    *,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Wait durably; cancelling this local wait never cancels the workflow."""

    if run_ref.get("mode") == "legacy":
        return await _await_legacy(run_ref)

    workflow_id = str(run_ref["workflow_id"])
    client = await connect_temporal()
    handle = client.get_workflow_handle(workflow_id)
    result_task = asyncio.create_task(handle.result())
    last_version = -1
    transcript_path = _llm_transcript_path(run_ref)
    ahe_path = _ahe_event_path(run_ref)
    transcript_cursor = int(run_ref.get("llm_jsonl_cursor", 0) or 0)
    ahe_jsonl_cursor = int(run_ref.get("ahe_jsonl_cursor", 0) or 0)
    stream_cursor = str(run_ref.get("llm_stream_last_id", "0-0") or "0-0")
    ahe_stream_cursor = str(run_ref.get("ahe_stream_last_id", "0-0") or "0-0")
    seen_llm_record_ids: set[str] = set()
    seen_ahe_record_ids: set[str] = set()
    stream_config = _llm_stream_config()
    ahe_stream_config = _ahe_stream_config()
    stream_reader: RedisLlmOutputReader | None = None
    ahe_stream_reader: RedisAheEventReader | None = None
    if on_progress is not None and stream_config.enabled and stream_config.url:
        try:
            stream_reader = RedisLlmOutputReader.connect(stream_config, workflow_id)
        except Exception:  # invalid/unavailable Redis configuration falls back to JSONL
            stream_reader = None
    if on_progress is not None and ahe_stream_config.enabled and ahe_stream_config.url:
        try:
            ahe_stream_reader = RedisAheEventReader.connect(
                ahe_stream_config,
                workflow_id,
            )
        except Exception:  # invalid/unavailable Redis configuration falls back to JSONL
            ahe_stream_reader = None
    poll_seconds = max(0.1, float(settings.RATSNESTPRO_TEMPORAL_POLL_SECONDS))

    async def forward_outputs() -> None:
        nonlocal ahe_jsonl_cursor, ahe_stream_cursor, ahe_stream_reader
        nonlocal stream_reader, stream_cursor, transcript_cursor
        if stream_reader is not None:
            try:
                stream_cursor = await _forward_redis_llm_outputs(
                    stream_reader,
                    stream_cursor,
                    on_progress,
                    seen_llm_record_ids,
                )
                run_ref["llm_stream_last_id"] = stream_cursor
            except Exception:  # Redis loss falls through to the JSONL audit copy
                with suppress(Exception):
                    await stream_reader.close()
                stream_reader = None
        if ahe_stream_reader is not None:
            try:
                ahe_stream_cursor = await _forward_redis_ahe_events(
                    ahe_stream_reader,
                    ahe_stream_cursor,
                    on_progress,
                    seen_ahe_record_ids,
                )
                run_ref["ahe_stream_last_id"] = ahe_stream_cursor
            except Exception:  # Redis loss falls through to the JSONL audit copy
                with suppress(Exception):
                    await ahe_stream_reader.close()
                ahe_stream_reader = None
        transcript_cursor = _forward_llm_outputs(
            transcript_path,
            transcript_cursor,
            on_progress,
            seen_llm_record_ids,
        )
        run_ref["llm_jsonl_cursor"] = transcript_cursor
        ahe_jsonl_cursor = _forward_ahe_events(
            ahe_path,
            ahe_jsonl_cursor,
            on_progress,
            seen_ahe_record_ids,
        )
        run_ref["ahe_jsonl_cursor"] = ahe_jsonl_cursor

    try:
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(result_task), timeout=poll_seconds)
                await forward_outputs()
                if not isinstance(result, dict):
                    raise ValueError("Temporal workflow returned a non-object result")
                return result
            except TimeoutError:
                await forward_outputs()
                try:
                    progress = await handle.query(RatsNestHardwareWorkflow.progress)
                except Exception:  # query loss must not cancel durable execution
                    continue
                version = int(progress.get("version", 0) or 0)
                if on_progress is not None and version != last_version:
                    on_progress(progress)
                    last_version = version
    except asyncio.CancelledError:
        result_task.cancel()
        with suppress(asyncio.CancelledError):
            await result_task
        raise
    finally:
        if stream_reader is not None:
            with suppress(Exception):
                await stream_reader.close()
        if ahe_stream_reader is not None:
            with suppress(Exception):
                await ahe_stream_reader.close()
