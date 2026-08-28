"""Governed, tenant-isolated cross-run Harness experience memory.

Only an authenticated opaque scope may influence cross-run promotion. Human
run/project names remain presentation metadata and never enter this ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ratsnestpro.orchestration.ahe import (
    GOVERNED_HARNESS_REASON_CODES,
    CapabilityGap,
)
from service.governance_scope import TrustedGovernanceScope

_MAX_RECORDS = 10_000
_FINGERPRINT_DOMAIN = "ratsnest-ehe-v3"
_PRIVATE_IDENTITY_FIELDS = {
    "run_name",
    "project_name",
    "requirement",
    "requirement_hash",
    "keywords",
    "scope_fingerprint",
    "project_fingerprint",
    "governance_scope_token",
}


def _fingerprint(kind: str, *values: str) -> str:
    normalized = "\n".join(value.strip().casefold() for value in values)
    return hashlib.sha256(
        f"{_FINGERPRINT_DOMAIN}\0{kind}\0{normalized}".encode()
    ).hexdigest()


def _feature_fingerprints(value: str) -> set[str]:
    return {
        _fingerprint("feature", token)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", value)
    }


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_replace(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_publish(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        except OSError:
            # Some supported Windows/workspace filesystems reject hard links.
            # The event ID is content-addressed, so replacing an equivalent
            # record is still idempotent while preserving an atomic publish.
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class EheMemory:
    """Durable observation and gap ledger for one tenant/Harness partition."""

    def __init__(
        self,
        root: Path,
        governance_scope: TrustedGovernanceScope | None = None,
    ) -> None:
        self.governance_scope = governance_scope
        if governance_scope is None:
            partition = root / "u"
        else:
            governance_partition = _fingerprint(
                "governance_partition",
                governance_scope.tenant_scope,
                governance_scope.harness_version_id,
                governance_scope.harness_manifest_digest,
            )[:24]
            partition = root / "g" / governance_partition
        self.root = partition
        self.events_dir = partition / "e"
        self.verified_dir = partition / "v"
        self.gaps_dir = partition / "g"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.verified_dir.mkdir(parents=True, exist_ok=True)
        self.gaps_dir.mkdir(parents=True, exist_ok=True)

    @property
    def governance_eligible(self) -> bool:
        return self.governance_scope is not None

    def _scope_payload(self) -> dict[str, Any]:
        scope = self.governance_scope
        if scope is None:
            return {"governance_eligible": False}
        return {
            "governance_eligible": True,
            "tenant_scope": scope.tenant_scope,
            "project_scope": scope.project_scope,
            "run_scope": scope.run_scope,
            "harness_version_id": scope.harness_version_id,
            "harness_manifest_digest": scope.harness_manifest_digest,
        }

    def record(self, event: dict[str, Any], **_: Any) -> None:
        """Record safe telemetry; untrusted observations can never be counted."""

        safe_event = {
            key: value
            for key, value in event.items()
            if key not in _PRIVATE_IDENTITY_FIELDS
        }
        identity = {"scope": self._scope_payload(), "event": safe_event}
        event_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            **safe_event,
            **self._scope_payload(),
            "schema_version": 3,
            "event_id": event_id,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        _atomic_publish(self.events_dir / f"{event_id[:40]}.json", payload)

    def _events(self) -> list[dict[str, Any]]:
        events = [
            value
            for path in self.events_dir.glob("*.json")
            if (value := _read_object(path)) is not None
        ]
        return sorted(
            events,
            key=lambda item: (
                str(item.get("recorded_at", "")),
                str(item.get("event_id", "")),
            ),
        )[-_MAX_RECORDS:]

    def _event_is_current_partition(self, event: dict[str, Any]) -> bool:
        scope = self.governance_scope
        return bool(
            scope is not None
            and event.get("governance_eligible") is True
            and event.get("tenant_scope") == scope.tenant_scope
            and event.get("harness_version_id") == scope.harness_version_id
            and event.get("harness_manifest_digest")
            == scope.harness_manifest_digest
        )

    @staticmethod
    def _strict_harness_observation(event: dict[str, Any]) -> bool:
        failure = event.get("failure")
        attribution = event.get("attribution")
        affected_refs = (
            failure.get("affected_refs")
            if isinstance(failure, dict)
            else None
        )
        return bool(
            event.get("event") == "harness_defect_observed"
            and isinstance(failure, dict)
            and failure.get("origin") == "harness"
            and failure.get("recoverability") == "harness_observation"
            and failure.get("reason_code") in GOVERNED_HARNESS_REASON_CODES
            and isinstance(affected_refs, list)
            and affected_refs
            and all(isinstance(ref, str) and ref for ref in affected_refs)
            and isinstance(attribution, dict)
            and attribution.get("action") == "observe_harness"
            and attribution.get("origin") == "harness"
            and attribution.get("reason_code")
            == "harness_defect_not_yet_cross_run_reproducible"
        )

    def _trusted_scope_pairs(self) -> set[tuple[str, str]]:
        trusted: set[tuple[str, str]] = set()
        for path in self.verified_dir.glob("*.json"):
            item = _read_object(path)
            if not item or not self._event_is_current_partition(item):
                continue
            evidence = item.get("evidence")
            if not isinstance(evidence, dict):
                continue
            independent = evidence.get("independent_review")
            release = evidence.get("release_ready")
            if (
                isinstance(independent, dict)
                and independent.get("passed") is True
                and isinstance(release, dict)
                and release.get("passed") is True
            ):
                trusted.add(
                    (str(item.get("run_scope", "")), str(item.get("project_scope", "")))
                )
        return trusted

    def strategy_score(self, signature: str, strategy: str) -> float | None:
        if not self.governance_eligible:
            return None
        success = 0
        failure_count = 0
        projects: set[str] = set()
        trusted_scopes = self._trusted_scope_pairs()
        for event in self._events():
            pair = (str(event.get("run_scope", "")), str(event.get("project_scope", "")))
            if not self._event_is_current_partition(event) or pair not in trusted_scopes:
                continue
            repair = event.get("repair")
            if (
                not isinstance(repair, dict)
                or repair.get("kind") != "harness"
                or repair.get("strategy") != strategy
                or not any(
                    str(item).endswith(signature)
                    for item in repair.get("failure_ids", [])
                )
            ):
                continue
            projects.add(pair[1])
            if repair.get("status") == "verified":
                success += 1
            elif repair.get("status") in {"rejected", "error"}:
                failure_count += 1
        if success + failure_count < 2 or len(projects) < 2:
            return None
        return (success + 1.0) / (success + failure_count + 2.0)

    def replan_score(self, trigger_step: str, rollback_to: str) -> float | None:
        if not self.governance_eligible:
            return None
        success = 0
        failure_count = 0
        projects: set[str] = set()
        trusted_scopes = self._trusted_scope_pairs()
        for event in self._events():
            pair = (str(event.get("run_scope", "")), str(event.get("project_scope", "")))
            if not self._event_is_current_partition(event) or pair not in trusted_scopes:
                continue
            replan = event.get("replan")
            if not isinstance(replan, dict):
                continue
            if (
                replan.get("trigger_step") != trigger_step
                or replan.get("rollback_to") != rollback_to
                or replan.get("status")
                not in {"recovered", "stagnated", "exhausted"}
            ):
                continue
            projects.add(pair[1])
            if replan.get("status") == "recovered":
                success += 1
            else:
                failure_count += 1
        if success + failure_count < 2 or len(projects) < 2:
            return None
        return (success + 1.0) / (success + failure_count + 2.0)

    def candidate_summary(self) -> list[dict[str, Any]]:
        if not self.governance_eligible:
            return []
        grouped: dict[str, dict[str, Any]] = {}
        projects_by_signature: dict[str, dict[str, set[str]]] = {}
        refs_by_signature: dict[str, dict[str, set[str]]] = {}
        for event in self._events():
            if not self._event_is_current_partition(event):
                continue
            if event.get("event") == "capability_gap_resolved":
                detail = event.get("gap")
                attribution = event.get("attribution")
                if (
                    isinstance(detail, dict)
                    and detail.get("signature")
                    and isinstance(attribution, dict)
                    and attribution.get("action") == "resolve_capability_gap"
                    and attribution.get("origin") == "harness"
                    and attribution.get("reason_code")
                    == "verified_harness_capability_gap_resolved"
                ):
                    projects_by_signature.setdefault(
                        str(detail["signature"]), {}
                    ).pop(str(event.get("project_scope", "")), None)
                    refs_by_signature.setdefault(
                        str(detail["signature"]), {}
                    ).pop(str(event.get("project_scope", "")), None)
                continue
            if not self._strict_harness_observation(event):
                continue
            failure = event["failure"]
            signature = str(failure.get("signature", ""))
            project = str(event.get("project_scope", ""))
            run = str(event.get("run_scope", ""))
            if not signature or not project or not run:
                continue
            projects_by_signature.setdefault(signature, {}).setdefault(project, set()).add(run)
            refs_by_signature.setdefault(signature, {}).setdefault(project, set()).update(
                str(ref) for ref in failure.get("affected_refs", [])
            )
            grouped.setdefault(
                signature,
                {
                    "signature": signature,
                    "step": failure.get("step"),
                    "check_name": failure.get("check_name"),
                    "category": failure.get("category"),
                    "reason_code": failure.get("reason_code"),
                    "status": "observed",
                },
            )
        result: list[dict[str, Any]] = []
        for signature, item in grouped.items():
            project_runs = projects_by_signature.get(signature, {})
            if not project_runs:
                continue
            projects = sorted(project_runs)
            runs = sorted({run for values in project_runs.values() for run in values})
            result.append(
                {
                    **item,
                    "occurrences": len(projects),
                    "projects": projects,
                    "runs": runs,
                    "project_affected_refs": {
                        project: sorted(refs)
                        for project, refs in refs_by_signature.get(
                            signature,
                            {},
                        ).items()
                    },
                    "run_count": len(runs),
                    "status": (
                        "candidate"
                        if len(projects) >= 2 and len(runs) >= 2
                        else "observed"
                    ),
                }
            )
        return sorted(
            result,
            key=lambda item: (-int(item["occurrences"]), str(item["signature"])),
        )

    def harness_recurrence(self, signature: str) -> tuple[int, int]:
        candidate = next(
            (item for item in self.candidate_summary() if item["signature"] == signature),
            None,
        )
        if candidate is None:
            return 0, 0
        return int(candidate["run_count"]), int(candidate["occurrences"])

    def candidate_snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "scope": "tenant_harness_partition" if self.governance_eligible else "unscoped",
            "governance_eligible": self.governance_eligible,
            "source": "persistent_ehe_event_store",
            "candidates": self.candidate_summary(),
        }

    def _gap_path(self, signature: str, project_scope: str | None = None) -> Path:
        scope = self.governance_scope
        if scope is None or not re.fullmatch(r"[0-9a-f]{20}", signature):
            raise ValueError("trusted governance scope and stable gap signature required")
        project = project_scope or scope.project_scope
        if not re.fullmatch(r"[0-9a-f]{16,64}", project):
            raise ValueError("trusted opaque project scope required")
        return self.gaps_dir / project / f"{signature}.json"

    def open_gap(
        self,
        gap: CapabilityGap,
        *,
        project_scopes: Iterable[str] | None = None,
        affected_refs_by_project: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        scope = self.governance_scope
        if scope is None:
            raise ValueError("trusted governance scope required")
        projects = set(project_scopes or (scope.project_scope,))
        for project_scope in projects:
            affected_refs = sorted(set(
                (affected_refs_by_project or {}).get(
                    project_scope,
                    gap.affected_refs,
                )
            ))
            if not affected_refs:
                continue
            path = self._gap_path(gap.signature, project_scope)
            existing = _read_object(path)
            if existing and existing.get("status") == "open":
                existing_gap = existing.get("gap")
                if isinstance(existing_gap, dict):
                    try:
                        parsed_gap = CapabilityGap.model_validate(existing_gap)
                    except ValueError:
                        parsed_gap = None
                    if parsed_gap is not None:
                        merged_refs = sorted(set(
                            parsed_gap.affected_refs
                        ) | set(affected_refs))
                        if merged_refs != parsed_gap.affected_refs:
                            _atomic_replace(
                                path,
                                {
                                    **existing,
                                    "updated_at": datetime.now(UTC).isoformat(),
                                    "gap": parsed_gap.model_copy(update={
                                        "affected_refs": merged_refs,
                                    }).model_dump(mode="json"),
                                },
                            )
                continue
            _atomic_replace(
                path,
                {
                    "schema_version": 1,
                    **self._scope_payload(),
                    "project_scope": project_scope,
                    "status": "open",
                    "opened_at": datetime.now(UTC).isoformat(),
                    "gap": gap.model_copy(update={
                        "affected_refs": affected_refs,
                    }).model_dump(mode="json"),
                },
            )

    def active_gaps(self) -> list[CapabilityGap]:
        scope = self.governance_scope
        if scope is None:
            return []
        result: list[CapabilityGap] = []
        for path in sorted((self.gaps_dir / scope.project_scope).glob("*.json")):
            item = _read_object(path)
            if not item or item.get("status") != "open":
                continue
            if not self._event_is_current_partition(item):
                continue
            gap = item.get("gap")
            if not isinstance(gap, dict):
                continue
            try:
                result.append(CapabilityGap.model_validate(gap))
            except ValueError:
                continue
        return result

    def close_gap(
        self,
        signature: str,
        *,
        affected_refs: Iterable[str],
    ) -> bool:
        if not self.governance_eligible:
            return False
        path = self._gap_path(signature)
        item = _read_object(path)
        if not item or item.get("status") != "open":
            return False
        gap = item.get("gap")
        expected_refs = set(affected_refs)
        if (
            not isinstance(gap, dict)
            or not expected_refs
            or set(gap.get("affected_refs", [])) != expected_refs
        ):
            return False
        _atomic_replace(
            path,
            {
                **item,
                "status": "closed",
                "closed_at": datetime.now(UTC).isoformat(),
            },
        )
        return True

    def promote_verified_run(
        self,
        *,
        requirement: str,
        resolved_issues: list[dict[str, str]],
        selected_roles: list[str],
        human_amendment: bool,
        independent_review_passed: bool = False,
        release_ready_evidence: bool = False,
        **_: Any,
    ) -> Path:
        scope = self.governance_scope
        if scope is None:
            raise ValueError("trusted governance scope is required for promotion")
        if not independent_review_passed:
            raise ValueError("independent review evidence is required for promotion")
        if not release_ready_evidence:
            raise ValueError("release-ready evidence is required for promotion")
        identity = hashlib.sha256(
            json.dumps(
                {
                    "run_scope": scope.run_scope,
                    "project_scope": scope.project_scope,
                    "resolved_issues": resolved_issues,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 3,
            "kind": "verified_release_experience",
            "experience_id": identity,
            "recorded_at": datetime.now(UTC).isoformat(),
            **self._scope_payload(),
            "feature_fingerprints": sorted(
                _feature_fingerprints(requirement + "\n" + " ".join(selected_roles))
            )[:300],
            "selected_roles": sorted(set(selected_roles)),
            "resolved_issues": resolved_issues,
            "human_amendment": human_amendment,
            "evidence": {
                "pipeline_steps": 17,
                "independent_review": {"passed": True, "source": "reviewer"},
                "release_ready": {"passed": True, "source": "hardware_pipeline"},
            },
        }
        target = self.verified_dir / f"{identity[:40]}.json"
        _atomic_replace(target, payload)
        return target

    def search_verified(self, query: str, *, limit: int = 3) -> list[dict[str, Any]]:
        query_tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", query)
        }
        query_fingerprints = _feature_fingerprints(query)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for path in sorted(self.verified_dir.glob("*.json"))[-_MAX_RECORDS:]:
            item = _read_object(path)
            if not item or not self._event_is_current_partition(item):
                continue
            features = {str(value) for value in item.get("feature_fingerprints", [])}
            roles = {str(value).lower() for value in item.get("selected_roles", [])}
            score = len(query_tokens.intersection(roles)) + len(
                query_fingerprints.intersection(features)
            )
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("recorded_at", ""))))
        return [
            {**item, "score": score}
            for score, item in ranked[: max(1, limit)]
        ]
