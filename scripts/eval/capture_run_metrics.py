"""Read-only provider-usage snapshot; no service imports, credentials, or estimates.

Usage: python scripts/eval/capture_run_metrics.py RUN_DIRECTORY --workflow-id ID
       [--capture SINGLE_WORKFLOW_SSE_OR_JSONL ...]

Captures must belong to the selected workflow. Outer role records do not always
carry a workflow ID, so their attribution relies on this caller-supplied scope.
Only llm_output records count; message/token events are not additional calls.
Supported captures: direct llm_output JSONL, or Python Runtime SSE with
type=message and content.custom_data.kind=llm_output. Java Run-event envelopes
are not a supported input format.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOKEN_PATHS = {
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "total_tokens": ("total_tokens",),
    "cache_read_input_tokens": ("input_token_details", "cache_read"),
    "reasoning_output_tokens": ("output_token_details", "reasoning"),
}


def _usage(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("response_metadata")
    usage = metadata.get("usage_metadata") if isinstance(metadata, dict) else None
    return usage if isinstance(usage, dict) else {}


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {}
    for name, path in TOKEN_PATHS.items():
        values = []
        for record in records:
            value: Any = _usage(record)
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if type(value) is int and value >= 0:
                values.append(value)
        fields[name] = {
            "observed_sum": sum(values) if values else None,
            "known_records": len(values),
            "missing_records": len(records) - len(values),
            "complete_for_observed_records": bool(records) and len(values) == len(records),
        }
    return {"observed_calls": len(records), "tokens": fields}


def _read_records(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    """Read our JSONL or Python Runtime's one-data-line-per-event SSE contract."""
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    records = []
    for index, line in enumerate(lines):
        raw = line.strip()
        if not raw or raw.startswith((":", "event:", "id:", "retry:")):
            continue
        if raw.startswith("data:"):
            raw = raw[5:].strip()
        if raw == "[DONE]":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and not line.endswith("\n"):
                warnings.append(f"Ignored incomplete trailing record: {path.name}")
                continue
            raise ValueError(f"Invalid JSON at {path}:{index + 1}") from exc
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "message":
            message = payload.get("content")
            payload = message.get("custom_data") if isinstance(message, dict) else None
        if isinstance(payload, dict) and payload.get("kind") == "llm_output":
            records.append(payload)
    return records


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _file_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
    }


def _checkpoint_snapshot(run_directory: Path) -> dict[str, Any]:
    state_path = run_directory / "pipeline_state.json"
    result_path = run_directory / "pipeline_result.json"
    state = _read_object(state_path) or {}
    result = _read_object(result_path) or {}
    state_receipt = state.get("checkpoint_receipt") or {}
    result_receipt = result.get("checkpoint_receipt") or {}
    canonical = {key: value for key, value in state.items() if key != "checkpoint_receipt"}
    actual_digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state_hash = state_receipt.get("state_sha256")
    result_hash = result_receipt.get("state_sha256")
    if not result:
        freshness = "missing"
    elif not state_hash or not result_hash:
        freshness = "unverifiable_missing_receipt"
    elif state_hash != actual_digest:
        freshness = "unverifiable_invalid_state_digest"
    elif state_receipt != result_receipt:
        freshness = "stale_receipt_mismatch"
    else:
        freshness = "matches_workspace_checkpoint_not_proof_of_workflow_completion"
    return {
        "scope": "Shared workspace snapshot; checkpoint has no workflow binding. "
        "Neither filesystem presence nor a matching result proves current-workflow completion.",
        "state_file": _file_metadata(state_path),
        "result_file": _file_metadata(result_path),
        "state_receipt": state_receipt or None,
        "state_digest_valid": state_hash == actual_digest if state_hash else None,
        "result_receipt": result_receipt or None,
        "result_freshness": freshness,
        "artifact_quality_metrics": None,
    }


def capture_metrics(
    run_directory: Path, workflow_id: str, captures: list[Path] | None = None
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    run_directory = run_directory.resolve()
    if not run_directory.is_dir():
        raise ValueError(f"Run directory does not exist: {run_directory}")
    suffix = hashlib.sha256(workflow_id.encode()).hexdigest()[:20]
    transcript = run_directory / f"llm_outputs-{suffix}.jsonl"
    input_path = run_directory / f"temporal_input-{suffix}.json"
    workflow_input = _read_object(input_path)
    if workflow_input and workflow_input.get("workflow_id") != workflow_id:
        raise ValueError("Persisted input does not match the selected workflow ID")
    warnings: list[str] = []
    sources = []
    by_id: dict[str, dict[str, Any]] = {}
    duplicates = rejected = 0
    for path in [transcript, *(captures or [])]:
        path = path.resolve()
        sources.append(_file_metadata(path))
        if not path.is_file():
            if path != transcript:
                raise ValueError(f"Capture file does not exist: {path}")
            warnings.append("Workflow transcript is absent; provider usage is unknown.")
            continue
        for record in _read_records(path, warnings):
            if (record.get("workflow_id") not in (None, "", workflow_id)) or (
                record.get("transcript_ref") not in (None, "", transcript.name)
            ):
                rejected += 1
                continue
            record_id = record.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                rejected += 1
                warnings.append(f"Excluded llm_output without record_id: {path.name}")
                continue
            if record_id in by_id:
                duplicates += 1
                previous = by_id[record_id]
                if _usage(previous) and _usage(record) and _usage(previous) != _usage(record):
                    raise ValueError(f"Conflicting provider usage for record_id {record_id}")
                if not _usage(previous) and _usage(record):
                    by_id[record_id] = record
            else:
                by_id[record_id] = record
    records = list(by_id.values())
    groups = {}
    for field in ("model", "phase", "agent"):
        labels = sorted({str(record.get(field) or "unknown") for record in records})
        groups[f"by_{field}"] = {
            label: _summary([r for r in records if str(r.get(field) or "unknown") == label])
            for label in labels
        }
    timestamps = []
    for record in records:
        try:
            timestamp = datetime.fromisoformat(record["created_at"])
            if timestamp.tzinfo is None:
                raise ValueError("Timestamp lacks timezone")
            timestamps.append(timestamp.astimezone(UTC))
        except (KeyError, ValueError, TypeError):
            warnings.append(f"Missing/invalid created_at for record_id {record['record_id']}")
    checkpoint = _checkpoint_snapshot(run_directory)
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_directory": str(run_directory),
        "read_started_at": started_at,
        "read_finished_at": datetime.now(UTC).isoformat(),
        "scope": "Partial observed completed provider calls, not full Run/workflow totals. "
        "Workspace JSONL may omit outer role calls, failed calls, and in-flight calls. "
        "Optional captures are caller-declared single-workflow captures; untagged outer "
        "records cannot be independently workflow-bound. Live files are not an atomic snapshot.",
        "sources": sources,
        "persisted_workflow_input_present": workflow_input is not None,
        "deduplicated_records": duplicates,
        "rejected_records": rejected,
        "provider_usage": {**_summary(records), **groups},
        "subset_note": "cache_read is included in input_tokens; reasoning is included in "
        "output_tokens. Neither is added to total_tokens. Missing values remain null; "
        "provider total_tokens is not reconstructed from input/output.",
        "first_observed_completion_at": min(timestamps).isoformat() if timestamps else None,
        "last_observed_completion_at": max(timestamps).isoformat() if timestamps else None,
        "workflow_elapsed_seconds": None,
        "cost": None,
        "cost_note": "Not calculated: no verified provider pricing/billing evidence supplied.",
        "workspace_checkpoint": checkpoint,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument(
        "--capture",
        type=Path,
        action="append",
        default=[],
        help="Single-workflow direct JSONL or Python Runtime SSE capture (repeatable)",
    )
    args = parser.parse_args()
    try:
        report = capture_metrics(args.run_directory, args.workflow_id, args.capture)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Metrics capture failed: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
