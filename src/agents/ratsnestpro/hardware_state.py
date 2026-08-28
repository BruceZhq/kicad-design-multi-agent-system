"""Compact Hardware Engineer history stored in LangGraph checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agents.ratsnestpro.artifact_publisher import normalize_delivery_status


def _attempt_summary(result: Mapping[str, Any], *, attempt: int) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "outcome": normalize_delivery_status(result.get("outcome")),
        "completed_steps": int(result.get("completed_steps", 0) or 0),
        "total_steps": int(result.get("total_steps", 17) or 17),
        "release_ready": result.get("release_ready") is True,
        "execution_backend": str(result.get("execution_backend", "unknown")),
        "run_directory": str(result.get("run_directory", "")),
        "actual_file_count": len(result.get("actual_files", [])),
        "release_blocker_count": len(result.get("release_blockers", [])),
    }


def compact_hardware_attempts(
    attempts: list[dict[str, Any]],
    latest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return at most two summaries; the separate ``hardware`` state stays full."""

    compact: list[dict[str, Any]] = []
    for index, item in enumerate([*attempts, *([latest] if latest else [])], start=1):
        raw_attempt = item.get("attempt", index)
        attempt = int(raw_attempt) if str(raw_attempt).isdigit() else index
        compact.append(_attempt_summary(item, attempt=attempt))
    return compact[-2:]


def next_hardware_attempt_number(attempts: list[dict[str, Any]]) -> int:
    numbers = [
        int(item.get("attempt", index))
        for index, item in enumerate(attempts, start=1)
        if str(item.get("attempt", index)).isdigit()
    ]
    return max(numbers, default=0) + 1


def actual_artifacts(state: Mapping[str, Any]) -> list[str]:
    """Use the full latest result; compact summaries are audit metadata only."""

    attempts = list(state.get("hardware_attempts", []))
    if state.get("hardware"):
        attempts.append(state["hardware"])
    candidates: list[str] = []
    for attempt in attempts:
        candidates.extend(str(path) for path in attempt.get("actual_files", []))
        pipeline_result_path = str(attempt.get("pipeline_result_path", ""))
        if pipeline_result_path:
            candidates.append(pipeline_result_path)
    report_path = str(state.get("review", {}).get("report_path", ""))
    if report_path:
        candidates.append(report_path)
    return list(dict.fromkeys(path for path in candidates if Path(path).is_file()))
