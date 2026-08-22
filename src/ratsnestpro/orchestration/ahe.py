"""Contracts and policy for task-local AHE and cross-task EHE learning.

This module deliberately contains no KiCad-specific mutation code. Pipeline
steps own domain repairs; the controller records them through these contracts.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from ratsnestpro.domain.contracts import ContractModel


class FailureCategory(StrEnum):
    TRANSIENT_TOOL = "transient_tool"
    STRUCTURED_OUTPUT = "structured_output"
    EVIDENCE_GAP = "evidence_gap"
    SELECTION = "selection"
    CONNECTIVITY = "connectivity"
    PLACEMENT = "placement"
    ROUTING = "routing"
    VERIFICATION = "verification"
    HARD_CONSTRAINT = "hard_constraint"
    UNKNOWN = "unknown"


class Recoverability(StrEnum):
    RETRYABLE = "retryable"
    LOCALLY_REPAIRABLE = "locally_repairable"
    REVISION_REQUIRED = "revision_required"
    HITL_REQUIRED = "hitl_required"
    HARNESS_OBSERVATION = "harness_observation"
    CAPABILITY_GAP = "capability_gap"
    HARD_CONFLICT = "hard_conflict"


class FailureOrigin(StrEnum):
    DESIGN = "design"
    INFRASTRUCTURE = "infrastructure"
    HARNESS = "harness"
    EXTERNAL_EVIDENCE = "external_evidence"
    UNKNOWN = "unknown"


class FailureAction(StrEnum):
    RETRY = "retry"
    REVISION = "revision"
    HITL = "hitl"
    OBSERVE_HARNESS = "observe_harness"
    CAPABILITY_GAP = "capability_gap"
    STOP = "stop"


GOVERNED_HARNESS_REASON_CODES = frozenset({
    "generic_capability_closure_contradiction",
    "verified_pin_alias_resolution_lost",
})


class FailureEnvelope(ContractModel):
    failure_id: str
    signature: str
    step: str
    check_name: str
    category: FailureCategory
    recoverability: Recoverability
    origin: FailureOrigin = FailureOrigin.UNKNOWN
    reason_code: str = "check_failed"
    required_capability: str | None = None
    message: str = ""
    affected_refs: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class RepairRecord(ContractModel):
    patch_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal["harness", "design"] = "harness"
    step: str
    strategy: str
    attempt: int = Field(ge=1)
    failure_ids: list[str] = Field(default_factory=list)
    status: Literal["improved", "verified", "rejected", "error"]
    before_score: tuple[int, int, int]
    after_score: tuple[int, int, int]
    detail: str = ""
    baseline_fingerprint: str = ""


class ReplanRecord(ContractModel):
    replan_id: str = Field(default_factory=lambda: str(uuid4()))
    trigger_step: str
    rollback_to: str
    attempt: int = Field(ge=1)
    failure_ids: list[str] = Field(default_factory=list)
    status: Literal[
        "scheduled",
        "recovered",
        "stagnated",
        "exhausted",
        "deferred",
    ]
    before_score: tuple[int, int, int]
    after_score: tuple[int, int, int] | None = None
    feedback: str = ""
    baseline_fingerprint: str = ""


class CapabilityGap(ContractModel):
    gap_id: str
    signature: str
    step: str
    check_name: str
    category: FailureCategory
    message: str = ""
    required_capability: str
    affected_refs: list[str] = Field(default_factory=list)
    status: Literal["observed", "candidate", "promoted", "rejected"] = "observed"


class FailureAttribution(ContractModel):
    action: FailureAction
    reason_code: str
    origin: FailureOrigin
    independent_run_count: int = Field(default=1, ge=1)
    independent_project_count: int = Field(default=1, ge=1)


_REF_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z]{1,4}\d+[A-Z]?)(?![A-Za-z0-9_])")
_HARD_CHECK_RE = re.compile(
    r"^(?:immutable_|fixed_constraint_|forbidden_requirement|identity_violation)"
)
_TRANSIENT_RE = re.compile(
    r"timeout|temporar|rate.limit|connection.(?:refused|reset)|service.unavailable",
    re.I,
)
_STRUCTURED_OUTPUT_RE = re.compile(
    r"structured.output|invalid.json|json_invalid|EOF while parsing|"
    r"unterminated string|schema validation",
    re.I,
)
_EVIDENCE_RE = re.compile(r"datasheet|evidence|source|catalog", re.I)


def _category(step: str, check_name: str, message: str) -> FailureCategory:
    combined = f"{check_name} {message}"
    if _HARD_CHECK_RE.search(check_name):
        return FailureCategory.HARD_CONSTRAINT
    if _STRUCTURED_OUTPUT_RE.search(combined):
        return FailureCategory.STRUCTURED_OUTPUT
    if _EVIDENCE_RE.search(check_name):
        return FailureCategory.EVIDENCE_GAP
    if _TRANSIENT_RE.search(combined):
        return FailureCategory.TRANSIENT_TOOL
    if step == "selection":
        return FailureCategory.SELECTION
    if step.startswith("schematic_") or step in {"erc", "schematic_connections"}:
        return FailureCategory.CONNECTIVITY
    if step.startswith("layout_"):
        return FailureCategory.PLACEMENT
    if step.startswith("route_"):
        return FailureCategory.ROUTING
    if step == "manufacture" or "drc" in check_name.lower() or "erc" in check_name.lower():
        return FailureCategory.VERIFICATION
    return FailureCategory.UNKNOWN


def make_failure(
    *,
    step: str,
    check_name: str,
    message: str,
    repair_available: bool,
    origin: FailureOrigin | None = None,
    reason_code: str = "",
    affected_refs: list[str] | None = None,
) -> FailureEnvelope:
    """Normalize one failed check into a stable, board-independent signature."""

    category = _category(step, check_name, message)
    resolved_origin = origin or (
        FailureOrigin.INFRASTRUCTURE
        if category in {
            FailureCategory.TRANSIENT_TOOL,
            FailureCategory.STRUCTURED_OUTPUT,
        }
        else FailureOrigin.EXTERNAL_EVIDENCE
        if category == FailureCategory.EVIDENCE_GAP
        else FailureOrigin.DESIGN
        if category
        in {
            FailureCategory.SELECTION,
            FailureCategory.CONNECTIVITY,
            FailureCategory.PLACEMENT,
            FailureCategory.ROUTING,
            FailureCategory.VERIFICATION,
            FailureCategory.HARD_CONSTRAINT,
        }
        else FailureOrigin.UNKNOWN
    )
    if category == FailureCategory.HARD_CONSTRAINT:
        recoverability = Recoverability.HARD_CONFLICT
    elif resolved_origin == FailureOrigin.INFRASTRUCTURE or category in {
        FailureCategory.TRANSIENT_TOOL,
        FailureCategory.STRUCTURED_OUTPUT,
    }:
        recoverability = Recoverability.RETRYABLE
    elif resolved_origin == FailureOrigin.HARNESS:
        # One run can establish an observation, not a systemic capability gap.
        recoverability = Recoverability.HARNESS_OBSERVATION
    elif repair_available:
        recoverability = Recoverability.LOCALLY_REPAIRABLE
    elif resolved_origin == FailureOrigin.DESIGN:
        recoverability = Recoverability.REVISION_REQUIRED
    elif resolved_origin == FailureOrigin.EXTERNAL_EVIDENCE:
        recoverability = Recoverability.HITL_REQUIRED
    else:
        recoverability = Recoverability.HITL_REQUIRED
    refs = list(dict.fromkeys(affected_refs or _REF_RE.findall(message)))
    stable_reason_code = reason_code.strip() or "check_failed"
    signature_source = (
        f"{step}|{check_name}|{category.value}|{stable_reason_code}"
    )
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()[:20]
    return FailureEnvelope(
        failure_id=f"{step}:{check_name}:{signature}",
        signature=signature,
        step=step,
        check_name=check_name,
        category=category,
        recoverability=recoverability,
        origin=resolved_origin,
        reason_code=stable_reason_code,
        required_capability=_required_capability(category),
        message=message,
        affected_refs=refs,
        evidence={
            "check_name": check_name,
            "reason_code": stable_reason_code,
            "message": message,
        },
    )


def _required_capability(category: FailureCategory) -> str:
    return {
        FailureCategory.SELECTION: "component_selection_repair",
        FailureCategory.CONNECTIVITY: "schematic_connectivity_repair",
        FailureCategory.PLACEMENT: "pcb_placement_repair",
        FailureCategory.ROUTING: "pcb_routing_repair",
        FailureCategory.VERIFICATION: "verification_report_repair",
        FailureCategory.EVIDENCE_GAP: "grounded_evidence_recovery",
        FailureCategory.TRANSIENT_TOOL: "resilient_tool_retry",
        FailureCategory.STRUCTURED_OUTPUT: "structured_output_recovery",
    }.get(category, "unclassified_hardware_repair")


def attribute_failure(
    failure: FailureEnvelope,
    *,
    independent_run_count: int = 1,
    independent_project_count: int = 1,
) -> FailureAttribution:
    """Choose a governed action from explicit provenance and recurrence evidence."""

    if independent_run_count < 1 or independent_project_count < 1:
        raise ValueError("failure attribution counts must be positive")
    if failure.recoverability == Recoverability.HARD_CONFLICT:
        action = FailureAction.STOP
        reason = "hard_constraint_conflict"
    elif failure.origin == FailureOrigin.INFRASTRUCTURE:
        action = FailureAction.RETRY
        reason = "infrastructure_transient"
    elif failure.origin == FailureOrigin.DESIGN:
        action = FailureAction.REVISION
        reason = "ordinary_design_issue"
    elif failure.origin == FailureOrigin.EXTERNAL_EVIDENCE:
        action = FailureAction.HITL
        reason = "evidence_or_substitution_decision_required"
    elif failure.origin == FailureOrigin.HARNESS:
        if independent_run_count >= 2 and independent_project_count >= 2:
            action = FailureAction.CAPABILITY_GAP
            reason = "cross_run_reproducible_harness_defect"
        else:
            action = FailureAction.OBSERVE_HARNESS
            reason = "harness_defect_not_yet_cross_run_reproducible"
    else:
        action = FailureAction.HITL
        reason = "origin_unclassified_fail_closed"
    return FailureAttribution(
        action=action,
        reason_code=reason,
        origin=failure.origin,
        independent_run_count=independent_run_count,
        independent_project_count=independent_project_count,
    )


def make_capability_gap(failure: FailureEnvelope) -> CapabilityGap:
    if failure.recoverability != Recoverability.CAPABILITY_GAP:
        raise ValueError("only a cross-run attributed harness defect is a capability gap")
    return CapabilityGap(
        gap_id=f"gap:{failure.signature}",
        signature=failure.signature,
        step=failure.step,
        check_name=failure.check_name,
        category=failure.category,
        message=failure.message,
        required_capability=(
            failure.required_capability or "unclassified_hardware_repair"
        ),
        affected_refs=failure.affected_refs,
    )


def ahe_event(
    event: str,
    *,
    step: str,
    revision: int,
    failure: FailureEnvelope | None = None,
    repair: RepairRecord | None = None,
    gap: CapabilityGap | None = None,
    replan: ReplanRecord | None = None,
    attribution: FailureAttribution | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "ahe_event",
        "event": event,
        "step": step,
        "revision": revision,
    }
    if failure is not None:
        payload["failure"] = failure.model_dump(mode="json")
    if repair is not None:
        payload["repair"] = repair.model_dump(mode="json")
    if gap is not None:
        payload["gap"] = gap.model_dump(mode="json")
    if replan is not None:
        payload["replan"] = replan.model_dump(mode="json")
    if attribution is not None:
        payload["attribution"] = attribution.model_dump(mode="json")
    return payload
