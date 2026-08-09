"""Auditable, provider-visible LLM output records.

This module deliberately records only content returned by the provider.  It
does not attempt to reconstruct or expose a model's hidden chain-of-thought.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")
_REASONING_BLOCK_TYPES = frozenset({"reasoning", "reasoning_content", "thinking"})
_TEXT_BLOCK_TYPES = frozenset({"text", "output_text"})
_METADATA_KEYS = frozenset(
    {
        "finish_reason",
        "model",
        "model_name",
        "stop_reason",
        "system_fingerprint",
        "token_usage",
        "usage",
    }
)
_MAX_PERSISTED_FIELD_CHARS = 1_000_000
_MAX_STREAM_FIELD_CHARS = 120_000


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_value_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "thinking", "summary"):
            if key in value:
                return _value_text(value[key])
    return ""


def response_text(message: Any) -> str:
    """Return final answer text while excluding explicit reasoning blocks."""

    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            block_type = str(block.get("type", "")).casefold()
            if block_type in _REASONING_BLOCK_TYPES:
                continue
            if not block_type or block_type in _TEXT_BLOCK_TYPES:
                parts.append(_value_text(block))
    return "".join(parts)


def provider_reasoning_content(message: Any) -> str:
    """Return only reasoning text explicitly supplied by the model provider."""

    content = getattr(message, "content", None)
    if isinstance(content, list):
        blocks = [
            _value_text(block)
            for block in content
            if isinstance(block, dict)
            and str(block.get("type", "")).casefold() in _REASONING_BLOCK_TYPES
        ]
        if any(blocks):
            return "".join(blocks)

    for metadata_name in ("additional_kwargs", "response_metadata"):
        metadata = getattr(message, metadata_name, None)
        if not isinstance(metadata, dict):
            continue
        for key in _REASONING_KEYS:
            reasoning = _value_text(metadata.get(key))
            if reasoning:
                return reasoning
    return ""


def _response_metadata(message: Any) -> dict[str, Any]:
    raw = getattr(message, "response_metadata", None)
    metadata = (
        {key: value for key, value in raw.items() if key in _METADATA_KEYS}
        if isinstance(raw, dict)
        else {}
    )
    usage = getattr(message, "usage_metadata", None)
    if usage:
        metadata["usage_metadata"] = usage
    return metadata


def llm_output_record(
    message: Any,
    *,
    phase: str,
    agent: str,
    model: str,
) -> dict[str, Any]:
    """Build a transport-neutral record for one completed model call."""

    content = response_text(message)
    reasoning = provider_reasoning_content(message)
    persisted_truncated = (
        len(content) > _MAX_PERSISTED_FIELD_CHARS
        or len(reasoning) > _MAX_PERSISTED_FIELD_CHARS
    )
    return {
        "kind": "llm_output",
        "schema_version": 1,
        "record_id": uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "phase": phase,
        "agent": agent,
        "model": model,
        "status": "completed",
        "content": content[:_MAX_PERSISTED_FIELD_CHARS],
        "reasoning": reasoning[:_MAX_PERSISTED_FIELD_CHARS],
        "reasoning_visibility": "provider_explicit" if reasoning else "not_provided",
        "response_metadata": _response_metadata(message),
        "persisted_truncated": persisted_truncated,
    }


def stream_llm_output_record(
    record: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> dict[str, Any]:
    """Bound one SSE event while retaining a reference to its full transcript."""

    event = dict(record)
    content = str(event.get("content", ""))
    reasoning = str(event.get("reasoning", ""))
    stream_truncated = (
        len(content) > _MAX_STREAM_FIELD_CHARS
        or len(reasoning) > _MAX_STREAM_FIELD_CHARS
    )
    event["content"] = content[:_MAX_STREAM_FIELD_CHARS]
    event["reasoning"] = reasoning[:_MAX_STREAM_FIELD_CHARS]
    event["stream_truncated"] = stream_truncated
    if transcript_path:
        event["transcript_ref"] = Path(transcript_path).name
    return event


def append_llm_output(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record with a single OS write for process safety."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
