"""Convert runtime AHE events into privacy-safe evolution observations."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evolution.contracts import (
    CandidateStatus,
    EvolutionCandidate,
    EvolutionObservation,
    HarnessIdentity,
    ObservationOutcome,
)


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """Private runtime identity used only while deriving safe fingerprints."""

    tenant_id: str
    project_id: str
    run_id: str
    source_event_seq: int
    harness: HarnessIdentity
    profile_reference: str
    profile_digest: str


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _hmac(key: bytes, domain: str, value: Any) -> str:
    return hmac.new(
        key,
        domain.encode() + b"\0" + _canonical(value),
        hashlib.sha256,
    ).hexdigest()


def _event_detail(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("failure", "gap"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _failure_signature(event: dict[str, Any], detail: dict[str, Any]) -> str | None:
    signature = str(detail.get("signature", "")).strip()
    if signature:
        return signature
    repair = event.get("repair")
    if not isinstance(repair, dict):
        return None
    failure_ids = repair.get("failure_ids")
    if not isinstance(failure_ids, list) or not failure_ids:
        return None
    return str(failure_ids[0]).rsplit(":", 1)[-1].strip() or None


def _outcome(event_type: str, event: dict[str, Any]) -> ObservationOutcome:
    if event_type == "capability_gap_resolved":
        return ObservationOutcome.RESOLVED
    if event_type == "hard_constraint_conflict":
        return ObservationOutcome.HARD_CONFLICT
    repair = event.get("repair")
    status = str(repair.get("status", "")) if isinstance(repair, dict) else ""
    return {
        "improved": ObservationOutcome.IMPROVED,
        "verified": ObservationOutcome.VERIFIED,
        "rejected": ObservationOutcome.REJECTED,
        "error": ObservationOutcome.ERROR,
    }.get(status, ObservationOutcome.OBSERVED)


def observation_from_ahe_event(
    event: dict[str, Any],
    context: ObservationContext,
    *,
    fingerprint_key: bytes,
    observed_at: datetime | None = None,
) -> EvolutionObservation:
    """Build one observation without retaining raw identity or diagnostic text."""

    if len(fingerprint_key) < 32:
        raise ValueError("evolution fingerprint key must contain at least 32 bytes")
    if context.source_event_seq < 0:
        raise ValueError("source_event_seq cannot be negative")
    if event.get("kind") != "ahe_event":
        raise ValueError("only ahe_event payloads can become evolution observations")
    event_type = str(event.get("event", "")).strip()
    step = str(event.get("step", "")).strip()
    if not event_type or not step:
        raise ValueError("AHE event and step are required")

    detail = _event_detail(event)
    repair = event.get("repair") if isinstance(event.get("repair"), dict) else {}
    replan = event.get("replan") if isinstance(event.get("replan"), dict) else {}
    evidence_digest = _hmac(fingerprint_key, "evidence-v1", event)
    scope_fingerprint = _hmac(
        fingerprint_key,
        "scope-v1",
        [context.tenant_id, context.run_id],
    )
    project_fingerprint = _hmac(
        fingerprint_key,
        "project-v1",
        [context.tenant_id, context.project_id],
    )
    public_identity = {
        "sourceEventSeq": context.source_event_seq,
        "harnessManifestDigest": context.harness.manifest_digest,
        "scopeFingerprint": scope_fingerprint,
        "eventType": event_type,
        "evidenceDigest": evidence_digest,
    }
    required_capability = str(detail.get("required_capability", "")).strip() or None
    strategy = (
        str(repair.get("strategy", "")).strip()
        or str(replan.get("rollback_to", "")).strip()
        or None
    )
    values: dict[str, Any] = {
        "observation_id": hashlib.sha256(_canonical(public_identity)).hexdigest(),
        "source_event_seq": context.source_event_seq,
        "harness_version_id": context.harness.version_id,
        "harness_channel": context.harness.channel,
        "harness_manifest_digest": context.harness.manifest_digest,
        "profile_reference": context.profile_reference,
        "profile_digest": context.profile_digest,
        "scope_fingerprint": scope_fingerprint,
        "project_fingerprint": project_fingerprint,
        "event_type": event_type,
        "failure_signature": _failure_signature(event, detail),
        "step": step,
        "check_name": str(detail.get("check_name", "")).strip() or None,
        "category": str(detail.get("category", "")).strip() or None,
        "recoverability": str(detail.get("recoverability", "")).strip() or None,
        "strategy": strategy,
        "required_capability": required_capability,
        "outcome": _outcome(event_type, event),
        "revision": max(0, int(event.get("revision", 0) or 0)),
        "evidence_digest": evidence_digest,
    }
    if observed_at is not None:
        values["observed_at"] = observed_at
    return EvolutionObservation.model_validate(values)


def aggregate_candidates(
    observations: Iterable[EvolutionObservation],
    *,
    minimum_projects: int = 2,
) -> list[EvolutionCandidate]:
    """Promote recurring unresolved gaps to candidates, never to code changes."""

    if minimum_projects < 2:
        raise ValueError("cross-project candidates require at least two projects")
    active: dict[tuple[str, str, str], dict[str, list[EvolutionObservation]]] = {}
    ordered = sorted(
        observations,
        key=lambda item: (item.observed_at, item.source_event_seq, item.observation_id),
    )
    for item in ordered:
        if not item.failure_signature:
            continue
        key = (
            item.harness_version_id,
            item.harness_manifest_digest,
            item.failure_signature,
        )
        by_project = active.setdefault(key, {})
        if item.event_type == "capability_gap_resolved":
            by_project.pop(item.project_fingerprint, None)
        elif item.event_type == "capability_gap":
            by_project.setdefault(item.project_fingerprint, []).append(item)

    candidates: list[EvolutionCandidate] = []
    for (version_id, manifest_digest, signature), by_project in sorted(active.items()):
        relevant = [item for values in by_project.values() for item in values]
        if not relevant:
            continue
        representative = relevant[-1]
        identity = {
            "baseManifestDigest": manifest_digest,
            "failureSignature": signature,
            "step": representative.step,
            "checkName": representative.check_name,
        }
        project_count = len(by_project)
        candidates.append(
            EvolutionCandidate(
                candidate_id=hashlib.sha256(_canonical(identity)).hexdigest(),
                base_harness_version_id=version_id,
                base_manifest_digest=manifest_digest,
                failure_signature=signature,
                step=representative.step,
                check_name=representative.check_name,
                category=representative.category,
                required_capability=representative.required_capability,
                profile_references=sorted({item.profile_reference for item in relevant}),
                observation_ids=sorted({item.observation_id for item in relevant}),
                occurrence_count=len(relevant),
                project_count=project_count,
                status=(
                    CandidateStatus.ELIGIBLE
                    if project_count >= minimum_projects
                    else CandidateStatus.OBSERVED
                ),
            )
        )
    return candidates
