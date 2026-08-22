"""Privacy-safe, replay-idempotent records for AHE runtime events."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from service.durable_event_stream import (
    RedisEventReader,
    RedisEventStreamConfig,
    publish_event_best_effort,
)

AHE_EVENT_KIND = "ahe_event"
AHE_EVENT_CHANNEL = "ahe-events"
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+/-]{0,199}$")
_MAX_LIST_ITEMS = 128
_MAX_IDENTIFIER_CHARS = 200

_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "failure": (
        "failure_id",
        "signature",
        "step",
        "check_name",
        "category",
        "recoverability",
        "required_capability",
        "affected_refs",
        "origin",
        "reason_code",
    ),
    "repair": (
        "patch_id",
        "kind",
        "step",
        "strategy",
        "attempt",
        "failure_ids",
        "status",
        "before_score",
        "after_score",
        "baseline_fingerprint",
    ),
    "gap": (
        "gap_id",
        "signature",
        "step",
        "check_name",
        "category",
        "required_capability",
        "status",
    ),
    "replan": (
        "replan_id",
        "trigger_step",
        "rollback_to",
        "attempt",
        "failure_ids",
        "status",
        "before_score",
        "after_score",
        "baseline_fingerprint",
    ),
    "attribution": (
        "action",
        "reason_code",
        "origin",
        "independent_project_count",
        "independent_run_count",
    ),
}
_STRUCTURAL_IDENTIFIER_FIELDS = {
    "failure_id",
    "signature",
    "step",
    "check_name",
    "category",
    "recoverability",
    "required_capability",
    "origin",
    "reason_code",
    "patch_id",
    "kind",
    "strategy",
    "status",
    "baseline_fingerprint",
    "gap_id",
    "replan_id",
    "trigger_step",
    "rollback_to",
    "action",
}
_STRUCTURAL_IDENTIFIER_LIST_FIELDS = {"affected_refs", "failure_ids"}


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_IDENTIFIER_CHARS]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item) for item in value[:_MAX_LIST_ITEMS]]
    return None


def _safe_detail_value(field: str, value: Any) -> Any:
    bounded = _bounded_value(value)
    if field in _STRUCTURAL_IDENTIFIER_FIELDS:
        if bounded == "":
            return None
        if not isinstance(bounded, str) or not _IDENTIFIER_RE.fullmatch(bounded):
            raise ValueError(f"AHE field {field} is not a structural identifier")
    elif field in _STRUCTURAL_IDENTIFIER_LIST_FIELDS:
        if not isinstance(bounded, list) or any(
            not isinstance(item, str) or not _IDENTIFIER_RE.fullmatch(item)
            for item in bounded
        ):
            raise ValueError(f"AHE field {field} contains unsafe identifiers")
    return bounded


def sanitize_ahe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Allow only structural diagnostics; prompts and free-form text are dropped."""

    if event.get("kind") != AHE_EVENT_KIND:
        raise ValueError("only ahe_event payloads may enter the AHE bridge")
    event_name = str(event.get("event", "")).strip()
    step = str(event.get("step", "")).strip()
    if not _EVENT_RE.fullmatch(event_name) or not _IDENTIFIER_RE.fullmatch(step):
        raise ValueError("AHE event and step must be bounded identifiers")
    safe: dict[str, Any] = {
        "kind": AHE_EVENT_KIND,
        "event": event_name,
        "step": step[:_MAX_IDENTIFIER_CHARS],
        "revision": max(0, int(event.get("revision", 0) or 0)),
    }
    for detail_name, allowed_fields in _DETAIL_FIELDS.items():
        detail = event.get(detail_name)
        if not isinstance(detail, dict):
            continue
        filtered: dict[str, Any] = {}
        for key in allowed_fields:
            if key not in detail:
                continue
            bounded = _safe_detail_value(key, detail[key])
            if bounded is not None:
                filtered[key] = bounded
        if filtered:
            safe[detail_name] = filtered
    return safe


def ahe_event_record(event: dict[str, Any], *, workflow_id: str) -> dict[str, Any]:
    """Create a stable record ID so Temporal retries collapse to one event."""

    if not workflow_id.strip():
        raise ValueError("workflow_id is required for an AHE event record")
    safe = sanitize_ahe_event(event)
    canonical = json.dumps(
        safe,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    record_id = hashlib.sha256(
        f"ahe-event-v1\0{workflow_id}\0{canonical}".encode()
    ).hexdigest()
    return {
        **safe,
        "schema_version": 1,
        "record_id": record_id,
        "created_at": datetime.now(UTC).isoformat(),
    }


def stream_ahe_event_record(
    record: dict[str, Any],
    *,
    audit_path: str | None = None,
) -> dict[str, Any]:
    event = dict(record)
    if audit_path:
        event["audit_ref"] = Path(audit_path).name
    return event


def append_ahe_event(path: Path, record: dict[str, Any]) -> None:
    """Append an audit record with one OS write; replay is deduped by record_id."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def publish_ahe_event_best_effort(
    config: RedisEventStreamConfig,
    *,
    workflow_id: str,
    record: dict[str, Any],
    audit_path: str | None = None,
    client: Any | None = None,
) -> str | None:
    return publish_event_best_effort(
        config,
        workflow_id=workflow_id,
        channel=AHE_EVENT_CHANNEL,
        record=stream_ahe_event_record(record, audit_path=audit_path),
        client=client,
    )


class RedisAheEventReader(RedisEventReader):
    @classmethod
    def connect(
        cls,
        config: RedisEventStreamConfig,
        workflow_id: str,
    ) -> RedisAheEventReader:
        from redis.asyncio import Redis

        from service.durable_event_stream import event_stream_keys

        client = Redis.from_url(
            config.url,
            socket_connect_timeout=config.socket_timeout_seconds,
            socket_timeout=config.socket_timeout_seconds,
            retry_on_timeout=False,
        )
        return cls(
            client,
            event_stream_keys(
                config.key_prefix,
                workflow_id,
                channel=AHE_EVENT_CHANNEL,
            ),
            expected_kind=AHE_EVENT_KIND,
        )
