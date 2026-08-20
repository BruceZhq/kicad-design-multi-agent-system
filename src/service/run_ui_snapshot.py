"""Deterministic, text-free UI state derived from retained run events."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from agents.ratsnestpro.temporal.contracts import CANONICAL_STEPS

_ROLE_LABELS = {
    "supervisor": "Supervisor",
    "architect": "Architect",
    "parts_specialist": "Parts Specialist",
    "hardware_engineer": "Hardware Engineer",
    "reviewer": "Reviewer",
}
_STEP_DETAIL = re.compile(r"^step\s+(\d+)/(\d+)$", re.IGNORECASE)
_COMPLETED_DETAIL = re.compile(r"^(\d+)/(\d+)\s+steps$", re.IGNORECASE)
_RUNNING_STATUSES = {
    "attached",
    "in_progress",
    "ready",
    "retrying",
    "running",
    "started",
    "team_ready",
}
_COMPLETED_STATUSES = {"completed", "release_ready", "success"}
_WARNING_STATUSES = {"delivered_with_issues", "partial", "unavailable", "warning"}
_BLOCKED_STATUSES = {
    "blocked",
    "cancelled",
    "dispatch_error",
    "error",
    "execution_blocked",
    "failed",
    "stopped",
    "timed_out",
}
_ARTIFACT_FIELDS = (
    "artifact_id",
    "name",
    "kind",
    "media_type",
    "size_bytes",
    "sha256",
    "object_key",
)


def _role_from_phase(phase: str) -> str | None:
    normalized = phase.strip().casefold()
    if normalized in {"intent-router", "supervisor", "artifact-publish"}:
        return "supervisor"
    if normalized == "architect":
        return "architect"
    if normalized == "parts-specialist":
        return "parts_specialist"
    if normalized.startswith("hardware-engineer"):
        return "hardware_engineer"
    if normalized == "reviewer":
        return "reviewer"
    if normalized.startswith("specialist:") and normalized.partition(":")[2]:
        return normalized
    return None


def _role_from_agent(agent: str) -> str | None:
    normalized = " ".join(agent.strip().casefold().replace("_", " ").split())
    return {
        "supervisor": "supervisor",
        "intent router": "supervisor",
        "architect": "architect",
        "parts specialist": "parts_specialist",
        "hardware engineer": "hardware_engineer",
        "reviewer": "reviewer",
    }.get(normalized)


def _public_status(status: str) -> str:
    normalized = status.strip().casefold()
    if normalized in _COMPLETED_STATUSES:
        return "completed"
    if normalized in _WARNING_STATUSES:
        return "warning"
    if normalized in _BLOCKED_STATUSES:
        return "blocked"
    if normalized in _RUNNING_STATUSES or normalized == "checkpointed":
        return "running"
    return normalized or "waiting"


def _sse_payload(payload: str) -> dict[str, Any] | None:
    for line in payload.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            value = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    return None


def _application_event(envelope: Mapping[str, Any]) -> dict[str, Any] | None:
    content = envelope.get("content")
    if envelope.get("type") == "artifact_manifest" and isinstance(content, dict):
        return {"kind": "artifact_manifest", **content}
    if envelope.get("type") != "message" or not isinstance(content, dict):
        return None
    custom_data = content.get("custom_data")
    return custom_data if isinstance(custom_data, dict) else None


def _hardware_tool_call(message: Mapping[str, Any]) -> str | None:
    if message.get("type") != "ai":
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    matches = [
        call
        for call in tool_calls
        if isinstance(call, dict)
        and call.get("name") == "ratsnest_temporal_hardware_workflow"
        and isinstance(call.get("id"), str)
        and call["id"]
    ]
    return str(matches[0]["id"]) if len(matches) == 1 else None


def _hardware_tool_result(message: Mapping[str, Any], tool_call_id: str) -> dict[str, Any] | None:
    """Extract a strict allowlist from one authenticated hardware tool response."""

    if message.get("type") != "tool" or message.get("tool_call_id") != tool_call_id:
        return None
    raw = message.get("content")
    if not isinstance(raw, str) or len(raw) > 200_000:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    completed = value.get("completed_steps")
    total = value.get("total_steps")
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 0:
        completed = None
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        total = None
    temporal = value.get("temporal")
    temporal = temporal if isinstance(temporal, dict) else {}
    last_step = temporal.get("last_step")
    if not isinstance(last_step, str) or last_step not in CANONICAL_STEPS:
        last_step = None
    status = str(value.get("outcome") or value.get("status") or "error")[:80]
    error = " ".join(str(value.get("error", "")).split())[:500]
    blockers = value.get("release_blockers")
    safe_blockers = (
        [" ".join(str(item).split())[:500] for item in blockers[:20]]
        if isinstance(blockers, list)
        else []
    )
    event: dict[str, Any] = {
        "kind": "workflow_event",
        "phase": "hardware-engineer",
        "status": status,
        "event_type": "pipeline_finished",
        "completed_steps": completed,
        "total_steps": total,
        "error": error,
        "release_blockers": safe_blockers,
    }
    if last_step is not None:
        event["step_id"] = last_step
        event["step_index"] = CANONICAL_STEPS.index(last_step) + 1
    temporal_status = temporal.get("status")
    if isinstance(temporal_status, str):
        event["temporal_status"] = temporal_status[:80]
    return event


def _safe_artifacts(manifest: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if manifest is None:
        return []
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [
        {key: item[key] for key in _ARTIFACT_FIELDS if key in item}
        for item in artifacts
        if isinstance(item, dict)
    ]


def _delivery_status(*values: object) -> str | None:
    severity = {
        "release_ready": 0,
        "delivered_with_issues": 1,
        "execution_blocked": 2,
    }
    statuses = [str(value) for value in values if str(value) in severity]
    return max(statuses, key=severity.__getitem__) if statuses else None


def build_ui_snapshot(
    events: Iterable[tuple[int, str]],
    *,
    snapshot_cursor: int,
    run_status: str,
    artifact_manifest: Mapping[str, Any] | None = None,
    delivery_status: str | None = None,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """Build one bounded projection without exposing conversation or LLM text."""

    rows = sorted(
        ((int(event_id), payload) for event_id, payload in events if int(event_id) <= snapshot_cursor),
        key=lambda item: item[0],
    )
    coverage_start = rows[0][0] if rows else None
    role_statuses: dict[str, dict[str, Any]] = {
        role: {
            "role": role,
            "label": label,
            "status": "waiting",
            "phase": None,
            "last_event_id": None,
        }
        for role, label in _ROLE_LABELS.items()
    }
    current_role: str | None = None
    current_phase: str | None = None
    pipeline: dict[str, Any] = {
        "status": "not_started",
        "completed_steps": None,
        "total_steps": None,
        "current_step": None,
        "current_step_index": None,
    }
    reviewer: dict[str, Any] = {
        "status": "waiting",
        "phase": None,
        "report_path": None,
        "last_event_id": None,
    }
    recent: list[dict[str, Any]] = []
    event_manifest: Mapping[str, Any] | None = None
    pending_hardware_call: tuple[str, int] | None = None
    tool_delivery_status: str | None = None
    tool_delivery_errors: list[str] = []

    for event_id, payload in rows:
        envelope = _sse_payload(payload)
        if envelope is None:
            pending_hardware_call = None
            continue
        content = envelope.get("content")
        message = content if envelope.get("type") == "message" and isinstance(content, dict) else {}
        prior_call = pending_hardware_call
        pending_hardware_call = None
        event = None
        if prior_call is not None and event_id == prior_call[1] + 1:
            event = _hardware_tool_result(message, prior_call[0])
            if event is not None:
                outcome = str(event.get("status", ""))
                if outcome in {"execution_blocked", "delivered_with_issues", "release_ready"}:
                    tool_delivery_status = outcome
                error = str(event.get("error", ""))
                blockers = event.get("release_blockers", [])
                if error:
                    tool_delivery_errors.append(error)
                if isinstance(blockers, list):
                    tool_delivery_errors.extend(str(item) for item in blockers if item)
        hardware_call_id = _hardware_tool_call(message)
        if hardware_call_id is not None:
            pending_hardware_call = (hardware_call_id, event_id)
        if event is None:
            event = _application_event(envelope)
        if event is None:
            continue
        kind = str(event.get("kind", ""))
        if kind == "artifact_manifest":
            event_manifest = event
            recent.append(
                {
                    "event_id": event_id,
                    "kind": "artifact_manifest",
                    "role": "supervisor",
                    "phase": "artifact-publish",
                    "status": str(event.get("delivery_status", "published")),
                    "detail": None,
                    "occurred_at": event.get("occurred_at"),
                    "step_index": None,
                    "total_steps": None,
                }
            )
            continue
        if kind not in {"workflow_event", "llm_output"}:
            continue

        phase = str(event.get("phase", ""))
        status = str(event.get("status", "in_progress"))
        role = _role_from_phase(phase) or _role_from_agent(str(event.get("agent", "")))
        if role is not None:
            current_role = role
        if phase:
            current_phase = phase

        if kind == "workflow_event" and role is not None:
            public_status = _public_status(status)
            if role not in role_statuses:
                role_statuses[role] = {
                    "role": role,
                    "label": role.partition(":")[2],
                    "status": "waiting",
                    "phase": None,
                    "last_event_id": None,
                }
            role_statuses[role].update(
                {
                    "status": public_status,
                    "phase": phase or None,
                    "last_event_id": event_id,
                }
            )
            if role == "reviewer":
                reviewer.update(
                    {
                        "status": public_status,
                        "phase": phase or None,
                        "last_event_id": event_id,
                    }
                )
                detail = str(event.get("detail", "")).strip()
                if detail and status in _COMPLETED_STATUSES | _BLOCKED_STATUSES:
                    reviewer["report_path"] = detail

        step_id = event.get("step_id")
        step_index = event.get("step_index")
        total_steps = event.get("total_steps")
        completed_steps = event.get("completed_steps")
        event_type = str(event.get("event_type", ""))
        completed_detail = _COMPLETED_DETAIL.fullmatch(
            str(event.get("detail", "")).strip()
        )
        is_pipeline = (
            phase == "hardware-engineer:temporal"
            or event_type.startswith("pipeline_")
            or (phase == "hardware-engineer" and completed_detail is not None)
        )
        if is_pipeline:
            if not isinstance(step_index, int) or not isinstance(total_steps, int):
                detail_match = _STEP_DETAIL.fullmatch(str(event.get("detail", "")).strip())
                if detail_match is not None:
                    step_index = int(detail_match.group(1))
                    total_steps = int(detail_match.group(2))
            if completed_detail is not None:
                completed_steps = int(completed_detail.group(1))
                total_steps = int(completed_detail.group(2))
                if not isinstance(step_index, int) and completed_steps > 0:
                    step_index = completed_steps
                    if status in _BLOCKED_STATUSES and completed_steps < total_steps:
                        step_index += 1
            if (
                not (isinstance(step_id, str) and step_id)
                and isinstance(step_index, int)
                and 1 <= step_index <= len(CANONICAL_STEPS)
            ):
                step_id = CANONICAL_STEPS[step_index - 1]
            pipeline["status"] = status
            if isinstance(completed_steps, int):
                pipeline["completed_steps"] = completed_steps
            if isinstance(total_steps, int):
                pipeline["total_steps"] = total_steps
            if isinstance(step_index, int):
                pipeline["current_step_index"] = step_index
            if isinstance(step_id, str) and step_id:
                pipeline["current_step"] = step_id
                current_phase = step_id

        recent.append(
            {
                "event_id": event_id,
                "kind": "pipeline_step" if is_pipeline else ("llm_output" if kind == "llm_output" else "workflow"),
                "role": role,
                "phase": phase or None,
                "status": status,
                "detail": (
                    str(event.get("error", event.get("detail", "")))[:500]
                    if kind == "workflow_event"
                    and (event.get("error") or event.get("detail"))
                    else None
                ),
                "occurred_at": event.get("occurred_at") or event.get("created_at"),
                "step_index": step_index if isinstance(step_index, int) else None,
                "total_steps": total_steps if isinstance(total_steps, int) else None,
            }
        )

    manifest = artifact_manifest or event_manifest
    artifacts = _safe_artifacts(manifest)
    manifest_errors = manifest.get("errors", []) if manifest is not None else []
    errors = [str(value) for value in manifest_errors] if isinstance(manifest_errors, list) else []
    errors.extend(tool_delivery_errors)
    delivery = {
        "status": _delivery_status(
            delivery_status
            if delivery_status is not None
            else None,
            manifest.get("delivery_status") if manifest is not None else None,
            tool_delivery_status,
        ),
        "manifest_id": manifest.get("manifest_id") if manifest is not None else None,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "errors": list(dict.fromkeys(errors)),
    }
    if run_status in {"completed", "failed", "cancelled", "timed_out"}:
        pipeline["status"] = pipeline["status"] if pipeline["status"] != "not_started" else run_status
        supervisor = role_statuses["supervisor"]
        if manifest is not None and supervisor["status"] == "running":
            # Older runs did not emit the final Supervisor workflow event.  A
            # persisted terminal run plus its artifact manifest is sufficient
            # evidence that orchestration reached its delivery close-out.
            supervisor["status"] = "completed"

    return {
        "schema_version": 1,
        "snapshot_cursor": snapshot_cursor,
        "coverage_start_event_id": coverage_start,
        "coverage_complete": snapshot_cursor == 0 or coverage_start == 1,
        "current_role": current_role,
        "current_phase": current_phase,
        "role_statuses": list(role_statuses.values()),
        "pipeline": pipeline,
        "recent_events": recent[-max(1, recent_limit) :],
        "delivery": delivery,
        "reviewer": reviewer,
    }
