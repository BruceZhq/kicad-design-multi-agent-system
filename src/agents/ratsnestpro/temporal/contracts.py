"""Small, serialization-safe contracts shared by Temporal client and worker."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, TypedDict

CANONICAL_STEPS: tuple[str, ...] = (
    "requirements",
    "topology",
    "selection",
    "schematic_connections",
    "schematic_pinmap",
    "schematic_layout",
    "schematic_materialize",
    "erc",
    "layout_partition",
    "layout_critical",
    "layout_general",
    "layout_write",
    "route_plan",
    "route_planes",
    "route_signals",
    "route_fab",
    "manufacture",
)
ROUTING_STEPS = frozenset({"route_plan", "route_planes", "route_signals", "route_fab"})

EXECUTE_STEP_ACTIVITY = "ratsnest.execute_pipeline_step"
READ_RESULT_ACTIVITY = "ratsnest.read_pipeline_result"
READ_CHECKPOINT_ACTIVITY = "ratsnest.read_pipeline_checkpoint"
COMPENSATE_ACTIVITY = "ratsnest.compensate_pipeline_run"
WORKFLOW_NAME = "ratsnest.hardware-engineer.v1"

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class CheckpointReceipt(TypedDict):
    """Small authoritative receipt for one committed pipeline checkpoint."""

    schema_version: int
    generation: int
    state_revision: int
    committed_step_index: int
    next_step: str
    state_sha256: str
    artifact_manifest_digest: str
    transition_kind: Literal["forward", "rollback", "invalidate"]
    parent_state_sha256: str
    replan_id: str


def checkpoint_receipt(value: Any) -> CheckpointReceipt | None:
    """Validate a persisted receipt without trusting a partial projection."""

    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return None
    try:
        generation = int(value["generation"])
        revision = int(value["state_revision"])
        committed = int(value["committed_step_index"])
    except (KeyError, TypeError, ValueError):
        return None
    transition = str(value.get("transition_kind", ""))
    state_sha256 = str(value.get("state_sha256", ""))
    artifact_digest = str(value.get("artifact_manifest_digest", ""))
    if (
        generation < 1
        or revision < 0
        or committed < 0
        or committed > len(CANONICAL_STEPS)
        or transition not in {"forward", "rollback", "invalidate"}
        or not re.fullmatch(r"[0-9a-f]{64}", state_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact_digest)
    ):
        return None
    next_step = str(value.get("next_step", ""))
    expected_next = (
        CANONICAL_STEPS[committed]
        if committed < len(CANONICAL_STEPS)
        else ""
    )
    if next_step != expected_next:
        return None
    return CheckpointReceipt(
        schema_version=1,
        generation=generation,
        state_revision=revision,
        committed_step_index=committed,
        next_step=next_step,
        state_sha256=state_sha256,
        artifact_manifest_digest=artifact_digest,
        transition_kind=transition,  # type: ignore[typeddict-item]
        parent_state_sha256=str(value.get("parent_state_sha256", "")),
        replan_id=str(value.get("replan_id", "")),
    )


def safe_name(value: str, fallback: str) -> str:
    """Use the same bounded filename policy as the pipeline integration."""

    cleaned = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return cleaned[:80] or fallback


def requirement_digest(requirement: str) -> str:
    return hashlib.sha256(requirement.encode("utf-8")).hexdigest()


def llm_transcript_filename(workflow_id: str) -> str:
    """Name a per-workflow transcript without exposing the workflow ID on disk."""

    digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:20]
    return f"llm_outputs-{digest}.jsonl"


def ahe_event_filename(workflow_id: str) -> str:
    """Name the per-workflow AHE audit log without exposing its identity."""

    digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:20]
    return f"ahe_events-{digest}.jsonl"


def hardware_workflow_id(request_id: str) -> str:
    """Return the single durable Hardware Workflow ID owned by one SaaS run."""

    value = request_id.strip()
    if not value:
        raise ValueError("request_id is required for Temporal workflow identity")
    # The suffix prevents collisions when normalization or the Temporal length
    # bound makes two external IDs look alike.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"ratsnest-hw-{safe_name(value, 'run')[:80]}-{digest}"


_WORKFLOW_IDENTITY_FIELDS: tuple[str, ...] = (
    "run_id",
    "requirement_hash",
    "run_name",
    "display_run_name",
    "execution_scope",
    "project_name",
    "llm_mode",
    "model_name",
    "model_type",
    "reasoning_effort",
    "vision_model_name",
    "vision_reasoning_effort",
    "ahe_budget",
    "approved_component_replacements",
    "tenant_id",
    "project_id",
    "principal_id",
    "tenant_scope",
    "project_scope",
    "run_scope",
    "harness_version_id",
    "harness_manifest_digest",
    "governance_scope_token",
    "resume_from_step",
)


def hardware_workflow_identity(input: dict[str, Any]) -> dict[str, Any]:
    """Hash immutable business input used to decide whether attach is safe.

    Operational retry and timeout settings are deliberately excluded: a worker
    deployment may change them while the existing durable execution must still
    be reattached. Raw requirements and tenant identifiers are never returned.
    """

    canonical_input = {
        field: input.get(field)
        for field in _WORKFLOW_IDENTITY_FIELDS
        if field in input
    }
    canonical = json.dumps(
        canonical_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "digest": hashlib.sha256(canonical).hexdigest(),
    }


def verify_hardware_workflow_identity(
    expected: dict[str, Any], actual: Any
) -> None:
    """Fail closed when a duplicate Workflow ID belongs to different input."""

    expected_digest = str(expected.get("digest", ""))
    actual_digest = str(actual.get("digest", "")) if isinstance(actual, dict) else ""
    if (
        expected.get("schema_version") != 1
        or not expected_digest
        or not isinstance(actual, dict)
        or actual.get("schema_version") != 1
        or actual_digest != expected_digest
    ):
        raise ValueError(
            "existing Temporal workflow identity does not match this run "
            f"(expected={expected_digest[:12] or 'missing'}, "
            f"actual={actual_digest[:12] or 'missing'})"
        )


def compact_pipeline_result(payload: dict[str, Any], expected_step: str) -> dict[str, Any]:
    """Keep per-step Event History small while retaining routing evidence."""

    blockers = payload.get("release_blockers", [])
    issues = payload.get("issue_ledger", [])
    artifacts = payload.get("artifacts", [])
    compact = {
        "status": str(payload.get("status", "error")),
        "outcome": str(payload.get("outcome", "execution_blocked")),
        "expected_step": expected_step,
        "requested_until_step": payload.get("requested_until_step"),
        "target_reached": payload.get("step_target_reached") is True,
        "completed_steps": int(payload.get("completed_steps", 0) or 0),
        "total_steps": int(
            payload.get("total_steps", len(CANONICAL_STEPS)) or len(CANONICAL_STEPS)
        ),
        "execution_complete": payload.get("execution_complete") is True,
        "execution_blocked": payload.get("execution_blocked") is True,
        "release_ready": payload.get("release_ready") is True,
        "release_blocker_count": len(blockers) if isinstance(blockers, list) else 0,
        "issue_count": len(issues) if isinstance(issues, list) else 0,
        "run_directory": str(payload.get("run_directory", "")),
        "pipeline_state_path": str(payload.get("pipeline_state_path", "")),
        "pipeline_result_path": str(payload.get("pipeline_result_path", "")),
        "artifacts": (
            [str(item) for item in artifacts[:64]] if isinstance(artifacts, list) else []
        ),
        "error": str(payload.get("error", "")),
        "error_type": str(payload.get("error_type", "")),
    }
    receipt = checkpoint_receipt(payload.get("checkpoint_receipt"))
    if receipt is not None:
        compact["checkpoint_receipt"] = receipt
    compact["checkpoint_digest"] = hashlib.sha256(
        json.dumps(compact, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return compact
