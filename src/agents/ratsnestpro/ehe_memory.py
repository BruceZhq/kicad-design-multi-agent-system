"""Safe cross-run experience memory for Evolutionary Harness Engineering.

EHE learns strategy outcomes and capability gaps. It never edits source code at
runtime; promotion means a proven generic strategy receives scheduling priority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_MAX_RECORDS = 10_000
_FINGERPRINT_DOMAIN = "ratsnest-ehe-v2"
_PRIVATE_IDENTITY_FIELDS = {
    "run_name",
    "project_name",
    "requirement",
    "requirement_hash",
    "keywords",
    "scope_fingerprint",
    "project_fingerprint",
}


def _fingerprint(kind: str, *values: str) -> str:
    normalized = "\n".join(value.strip().casefold() for value in values)
    return hashlib.sha256(
        f"{_FINGERPRINT_DOMAIN}\0{kind}\0{normalized}".encode()
    ).hexdigest()


def _scope_fingerprint(run_name: str) -> str:
    return _fingerprint("scope", run_name)


def _feature_fingerprints(value: str) -> set[str]:
    return {
        _fingerprint("feature", token)
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", value)
    }


class EheMemory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.events_dir = root / "events"
        self.verified_dir = root / "verified"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.verified_dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        event: dict[str, Any],
        *,
        run_name: str,
        project_name: str,
        requirement: str,
    ) -> None:
        scope_fingerprint = _scope_fingerprint(run_name)
        project_fingerprint = _fingerprint("project", project_name)
        safe_event = {
            key: value
            for key, value in event.items()
            if key not in _PRIVATE_IDENTITY_FIELDS
        }
        identity = {
            "scope_fingerprint": scope_fingerprint,
            "project_fingerprint": project_fingerprint,
            "event": safe_event,
        }
        event_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            **safe_event,
            "schema_version": 2,
            "event_id": event_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_fingerprint": scope_fingerprint,
            "project_fingerprint": project_fingerprint,
        }
        target = self.events_dir / f"{event_id}.json"
        if target.is_file():
            return
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            # Hard-link publication is atomic and cannot overwrite a duplicate
            # event created by another at-least-once Activity execution.
            os.link(temporary, target)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)

    def _events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in self.events_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                events.append(value)
        return sorted(
            events,
            key=lambda item: (
                str(item.get("recorded_at", "")),
                str(item.get("event_id", "")),
            ),
        )[-_MAX_RECORDS:]

    @staticmethod
    def _event_fingerprints(event: dict[str, Any]) -> tuple[str, str]:
        """Read v2 fingerprints while accepting legacy on-disk records."""

        scope = str(event.get("scope_fingerprint", ""))
        project = str(event.get("project_fingerprint", ""))
        if not scope:
            scope = _fingerprint(
                "scope",
                str(event.get("run_name", "")),
                str(event.get("requirement_hash", "")),
            )
        if not project:
            project = _fingerprint("project", str(event.get("project_name", "")))
        return scope, project

    def _trusted_scope_pairs(self) -> set[tuple[str, str]]:
        trusted: set[tuple[str, str]] = set()
        for path in self.verified_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence")
            if not isinstance(evidence, dict):
                continue
            independent = evidence.get("independent_review")
            release = evidence.get("release_ready")
            if not (
                isinstance(independent, dict)
                and independent.get("passed") is True
                and isinstance(release, dict)
                and release.get("passed") is True
            ):
                continue
            trusted.add(self._event_fingerprints(item))
        return trusted

    def strategy_score(self, signature: str, strategy: str) -> float | None:
        """Return a smoothed cross-run success score when evidence is sufficient."""

        success = 0
        failure = 0
        projects: set[str] = set()
        trusted_scopes = self._trusted_scope_pairs()
        for event in self._events():
            scope, project = self._event_fingerprints(event)
            if (scope, project) not in trusted_scopes:
                continue
            repair = event.get("repair")
            if not isinstance(repair, dict) or repair.get("strategy") != strategy:
                continue
            failure_ids = repair.get("failure_ids", [])
            if not any(str(item).endswith(signature) for item in failure_ids):
                continue
            projects.add(project)
            status = repair.get("status")
            if status == "verified":
                success += 1
            elif status in {"rejected", "error"}:
                failure += 1
        if success + failure < 2 or len(projects) < 2:
            return None
        return (success + 1.0) / (success + failure + 2.0)

    def replan_score(self, trigger_step: str, rollback_to: str) -> float | None:
        """Score one generic upstream-replan edge across distinct projects."""

        success = 0
        failure = 0
        projects: set[str] = set()
        trusted_scopes = self._trusted_scope_pairs()
        for event in self._events():
            scope, project = self._event_fingerprints(event)
            if (scope, project) not in trusted_scopes:
                continue
            replan = event.get("replan")
            if not isinstance(replan, dict):
                continue
            if (
                replan.get("trigger_step") != trigger_step
                or replan.get("rollback_to") != rollback_to
            ):
                continue
            status = replan.get("status")
            if status not in {"recovered", "stagnated", "exhausted"}:
                continue
            projects.add(project)
            if status == "recovered":
                success += 1
            else:
                failure += 1
        if success + failure < 2 or len(projects) < 2:
            return None
        return (success + 1.0) / (success + failure + 2.0)

    def candidate_summary(self) -> list[dict[str, Any]]:
        """Aggregate capability gaps without turning observations into code."""

        grouped: dict[str, dict[str, Any]] = {}
        active_projects: dict[str, set[str]] = {}
        for event in self._events():
            event_name = event.get("event")
            if event_name not in {"capability_gap", "capability_gap_resolved"}:
                continue
            detail = (
                event.get("failure")
                if event_name == "capability_gap"
                else event.get("gap")
            )
            if not isinstance(detail, dict):
                continue
            signature = str(detail.get("signature", ""))
            if not signature:
                continue
            _, project = self._event_fingerprints(event)
            projects = active_projects.setdefault(signature, set())
            if event_name == "capability_gap_resolved":
                projects.discard(project)
                continue
            projects.add(project)
            item = grouped.setdefault(
                signature,
                {
                    "signature": signature,
                    "step": detail.get("step"),
                    "check_name": detail.get("check_name"),
                    "category": detail.get("category"),
                    "status": "observed",
                },
            )
        result: list[dict[str, Any]] = []
        for signature, item in grouped.items():
            projects = sorted(
                project for project in active_projects.get(signature, set()) if project
            )
            if not projects:
                continue
            item["occurrences"] = len(projects)
            item["projects"] = projects
            if len(projects) >= 2:
                item["status"] = "candidate"
            result.append(item)
        return sorted(
            result,
            key=lambda item: (-int(item["occurrences"]), str(item["signature"])),
        )

    def candidate_snapshot(self) -> dict[str, Any]:
        """Describe the cross-run candidate pool without implying run-local gaps."""

        return {
            "schema_version": 1,
            "scope": "cross_run",
            "source": "persistent_ehe_event_store",
            "candidates": self.candidate_summary(),
        }

    def promote_verified_run(
        self,
        *,
        run_name: str,
        project_name: str,
        requirement: str,
        resolved_issues: list[dict[str, str]],
        selected_roles: list[str],
        human_amendment: bool,
        independent_review_passed: bool = False,
        release_ready_evidence: bool = False,
    ) -> Path:
        """Persist only release-verified experience, never an unreviewed guess."""

        if not independent_review_passed:
            raise ValueError("independent review evidence is required for promotion")
        if not release_ready_evidence:
            raise ValueError("release-ready evidence is required for promotion")
        scope_fingerprint = _scope_fingerprint(run_name)
        project_fingerprint = _fingerprint("project", project_name)
        identity = hashlib.sha256(
            json.dumps(
                {
                    "scope_fingerprint": scope_fingerprint,
                    "project_fingerprint": project_fingerprint,
                    "resolved_issues": resolved_issues,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 2,
            "kind": "verified_release_experience",
            "experience_id": identity,
            "recorded_at": datetime.now(UTC).isoformat(),
            "scope_fingerprint": scope_fingerprint,
            "project_fingerprint": project_fingerprint,
            "feature_fingerprints": sorted(
                _feature_fingerprints(
                    requirement + "\n" + " ".join(selected_roles)
                )
            )[:300],
            "selected_roles": sorted(set(selected_roles)),
            "resolved_issues": resolved_issues,
            "human_amendment": human_amendment,
            "evidence": {
                "pipeline_steps": 17,
                "independent_review": {
                    "passed": True,
                    "source": "reviewer",
                },
                "release_ready": {
                    "passed": True,
                    "source": "hardware_pipeline",
                },
            },
        }
        target = self.verified_dir / f"{identity}.json"
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def search_verified(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Retrieve verified local experience with deterministic token overlap."""

        query_tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", query)
        }
        query_fingerprints = _feature_fingerprints(query)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for path in sorted(self.verified_dir.glob("*.json"))[-10_000:]:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            keywords = {str(value).lower() for value in item.get("keywords", [])}
            features = {
                str(value) for value in item.get("feature_fingerprints", [])
            }
            roles = {
                str(value).lower() for value in item.get("selected_roles", [])
            }
            score = len(query_tokens.intersection(keywords | roles)) + len(
                query_fingerprints.intersection(features)
            )
            if score:
                ranked.append((score, item))
        ranked.sort(
            key=lambda pair: (
                -pair[0],
                str(pair[1].get("recorded_at", "")),
            )
        )
        return [
            {**item, "score": score}
            for score, item in ranked[: max(1, limit)]
        ]
