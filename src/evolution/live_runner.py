"""Run fixed Agent cases against the deployed HTTP/SSE runtime.

The runner persists only bounded workflow facts. Prompts, model responses,
reasoning, user identities, credentials, and local project paths are never
written to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.ratsnestpro.profiles.registry import REGISTRY


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class LiveCase(_Model):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$")
    title: str | None = Field(default=None, min_length=1, max_length=160)
    tier: Literal["foundation", "subsystem", "controller", "integrated"] | None = None
    verified_asset_ids: list[str] = Field(default_factory=list, max_length=32)
    category: Literal[
        "intent_routing",
        "rag_grounding",
        "tool_orchestration",
        "release_gate",
        "recovery_idempotency",
        "prompt_injection",
        "eda_pipeline",
    ]
    prompt: str = Field(min_length=1, max_length=100_000)
    expected_intents: list[str] = Field(min_length=1, max_length=4)
    required_phases: list[str] = Field(default_factory=list, max_length=16)
    forbidden_phases: list[str] = Field(default_factory=list, max_length=16)
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=32)
    required_artifacts: list[str] = Field(default_factory=list, max_length=32)
    expected_terminal: Literal["completed", "waiting_for_input"] = "completed"
    expect_release_ready: bool | None = False
    profile_reference: str | None = None
    replay: Literal["none", "same", "conflict"] = "none"
    timeout_seconds: float = Field(default=900, ge=1, le=36_000)
    agent_config: dict[str, Any] = Field(default_factory=dict)
    arm: Literal["multi_agent", "single_agent"] | None = None
    pair_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$",
    )
    expected_tool_calls: list[str] | None = Field(default=None, max_length=64)
    expected_handoffs: list[str] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def paired_fields_are_atomic(self) -> LiveCase:
        if (self.arm is None) != (self.pair_id is None):
            raise ValueError("arm and pairId must be declared together")
        if self.arm == "single_agent" and self.expected_handoffs:
            raise ValueError("single-agent controls cannot declare role handoffs")
        return self


class FrozenExecution(_Model):
    """Evaluator-declared environment identity bound into a paired report."""

    model: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=120)
    environment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class BlindReviewLabel(_Model):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$")
    accepted: bool
    rubric_version: str = Field(min_length=1, max_length=80)
    reviewer_id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_scores: dict[str, int] | None = None
    blocking_findings: int | None = Field(default=None, ge=0, le=100)
    notes_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounded_rubric_scores(self) -> BlindReviewLabel:
        if self.rubric_scores is None:
            return self
        if not self.rubric_scores or len(self.rubric_scores) > 12:
            raise ValueError("rubricScores must contain between 1 and 12 dimensions")
        for dimension, score in self.rubric_scores.items():
            if not dimension or len(dimension) > 80 or not 1 <= score <= 5:
                raise ValueError("rubricScores require bounded names and scores from 1 to 5")
        return self


class BlindReviewManifest(_Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    labels: list[BlindReviewLabel] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_cases(self) -> BlindReviewManifest:
        ids = [label.case_id for label in self.labels]
        if len(ids) != len(set(ids)):
            raise ValueError("blind-review labels must have unique case IDs")
        return self


class LivePlan(_Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(min_length=1, max_length=120)
    cases: list[LiveCase] = Field(min_length=1, max_length=100)
    frozen_execution: FrozenExecution | None = None
    asset_manifest_path: str | None = Field(default=None, max_length=300)
    asset_manifest_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def unique_cases(self) -> LivePlan:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("live evaluation case IDs must be unique")
        paired = [case for case in self.cases if case.pair_id is not None]
        if paired and self.frozen_execution is None:
            raise ValueError("paired plans require frozenExecution")
        if (self.asset_manifest_path is None) != (self.asset_manifest_digest is None):
            raise ValueError("assetManifestPath and assetManifestDigest must be declared together")
        if any(case.verified_asset_ids for case in self.cases) and self.asset_manifest_path is None:
            raise ValueError("verifiedAssetIds require a content-addressed asset manifest")
        pairs: dict[str, list[LiveCase]] = {}
        for case in paired:
            pairs.setdefault(str(case.pair_id), []).append(case)
        for pair_id, pair_cases in pairs.items():
            if len(pair_cases) != 2 or {case.arm for case in pair_cases} != {
                "multi_agent",
                "single_agent",
            }:
                raise ValueError(f"pair {pair_id!r} must contain exactly one case per arm")
            left, right = pair_cases
            comparable = lambda case: {  # noqa: E731 - bounded validator projection
                "category": case.category,
                "title": case.title,
                "tier": case.tier,
                "verifiedAssetIds": case.verified_asset_ids,
                "prompt": case.prompt,
                "expectedIntents": case.expected_intents,
                "expectedTerminal": case.expected_terminal,
                "expectReleaseReady": case.expect_release_ready,
                "profileReference": case.profile_reference,
                "agentConfig": case.agent_config,
                "timeoutSeconds": case.timeout_seconds,
            }
            if comparable(left) != comparable(right):
                raise ValueError(f"pair {pair_id!r} does not share frozen case inputs")
        return self


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _native_path(path: Path) -> Path:
    """Use Win32's extended form when content-addressed paths exceed MAX_PATH."""

    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _git_identity(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return commit, dirty


def _profile_selection(reference: str | None) -> dict[str, str] | None:
    if reference is None:
        return None
    for snapshot in REGISTRY.all():
        if snapshot.reference == reference:
            return snapshot.selection().model_dump(mode="json")
    raise ValueError(f"unknown capability profile: {reference}")


def _usage_tokens(event: dict[str, Any]) -> int:
    metadata = event.get("response_metadata", {})
    if not isinstance(metadata, dict):
        return 0
    usage = metadata.get("usage_metadata") or metadata.get("token_usage") or metadata.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "total_token_count"):
        value = usage.get(key)
        if isinstance(value, int):
            return max(0, value)
    return sum(
        max(0, value)
        for key, value in usage.items()
        if isinstance(value, int) and key in {"input_tokens", "output_tokens"}
    )


def _artifact_facts(manifest: dict[str, Any], root: Path) -> tuple[list[dict[str, Any]], bool]:
    facts: list[dict[str, Any]] = []
    all_valid = True
    artifact_root = (root / "data" / "ratsnestpro" / "artifacts").resolve()
    for raw in manifest.get("artifacts", []):
        if not isinstance(raw, dict):
            all_valid = False
            continue
        name = str(raw.get("name", ""))[:160]
        digest = str(raw.get("sha256", ""))
        object_key = str(raw.get("object_key", ""))
        valid = False
        try:
            candidate = (artifact_root / object_key).resolve()
            candidate.relative_to(artifact_root)
            native_candidate = _native_path(candidate)
            valid = (
                native_candidate.is_file()
                and len(digest) == 64
                and _sha256_bytes(native_candidate.read_bytes()) == digest
            )
        except (OSError, ValueError):
            valid = False
        facts.append({"name": name, "sha256": digest, "valid": valid})
        all_valid = all_valid and valid
    return facts, all_valid


def _artifact_path(root: Path, object_key: str) -> Path:
    """Resolve one published object without allowing an artifact-root escape."""

    artifact_root = (root / "data" / "ratsnestpro" / "artifacts").resolve()
    candidate = (artifact_root / object_key).resolve()
    candidate.relative_to(artifact_root)
    return _native_path(candidate)


def _artifact_manifest_identity(
    manifest: dict[str, Any], root: Path
) -> dict[str, Any] | None:
    fields = (
        "artifact_id",
        "kind",
        "media_type",
        "name",
        "object_key",
        "sha256",
        "size_bytes",
    )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return None
    try:
        canonical = [
            {field: artifact[field] for field in fields}
            for artifact in artifacts
            if isinstance(artifact, dict)
        ]
    except KeyError:
        return None
    if len(canonical) != len(artifacts):
        return None
    canonical.sort(key=lambda item: str(item["artifact_id"]))
    calculated = _sha256_bytes(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    expected = str(manifest.get("manifest_digest") or "")
    _, files_valid = _artifact_facts(manifest, root)
    valid = len(expected) == 64 and calculated == expected and files_valid
    return {
        "manifestId": str(manifest.get("manifest_id") or "")[:80] or None,
        "manifestDigest": expected[:64] or None,
        "artifactCount": len(canonical),
        "storageBackend": str(manifest.get("storage_backend") or "")[:40] or None,
        "valid": valid,
    }


def _failure_facts(result: dict[str, Any]) -> list[dict[str, str]]:
    """Return bounded, stable failure identities without persisting diagnostics."""

    facts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    ledger = result.get("issue_ledger")
    if isinstance(ledger, list):
        for item in ledger[:256]:
            if not isinstance(item, dict):
                continue
            stage = str(item.get("step") or "unknown")[:80]
            check = str(item.get("name") or "unknown")[:120]
            severity = str(item.get("severity") or "error")[:40]
            identity = (stage, check, severity)
            if identity in seen:
                continue
            seen.add(identity)
            facts.append(
                {
                    "stage": stage,
                    "check": check,
                    "severity": severity,
                    "signature": _sha256_bytes(
                        "\0".join(identity).encode("utf-8")
                    ),
                }
            )
    if facts:
        return facts

    blockers = result.get("release_blockers")
    if not isinstance(blockers, list):
        return facts
    for value in blockers[:256]:
        parts = str(value).split(":", 2)
        stage = (parts[0] or "release_gate")[:80]
        check = (parts[1] if len(parts) > 1 else "unclassified")[:120]
        identity = (stage, check, "error")
        if identity in seen:
            continue
        seen.add(identity)
        facts.append(
            {
                "stage": stage,
                "check": check,
                "severity": "error",
                "signature": _sha256_bytes("\0".join(identity).encode("utf-8")),
            }
        )
    return facts


def _pipeline_release_evidence(
    manifest: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Read only content-addressed release facts from the published pipeline result."""

    unavailable = {
        "pipelineResultValid": False,
        "pipelineComplete": None,
        "releaseReady": None,
        "releaseBlockerCount": None,
        "ercErrors": None,
        "drcErrors": None,
        "unconnected": None,
        "routingUnconnected": None,
        "drcUnconnected": None,
        "ercClean": None,
        "drcClean": None,
        "zeroUnconnected": None,
        "routingComplete": None,
        "productionBomPresent": None,
        "procurementBomPresent": None,
        "coreArtifactsPresent": None,
        "artifactIdentity": None,
        "failureFacts": [],
        "strictGatePassed": False,
    }
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return unavailable
    selected = next(
        (
            item
            for item in artifacts
            if isinstance(item, dict)
            and str(item.get("name", "")).casefold().endswith("pipeline_result.json")
        ),
        None,
    )
    if selected is None:
        return unavailable
    digest = str(selected.get("sha256", ""))
    try:
        path = _artifact_path(root, str(selected.get("object_key", "")))
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return unavailable
        raw = path.read_bytes()
        if len(digest) != 64 or _sha256_bytes(raw) != digest:
            return unavailable
        result = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return unavailable
    if not isinstance(result, dict):
        return unavailable

    verification = result.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    erc = verification.get("erc")
    erc = erc if isinstance(erc, dict) else {}
    drc = verification.get("drc")
    drc = drc if isinstance(drc, dict) else {}
    routing = result.get("routing")
    routing = routing if isinstance(routing, dict) else {}
    blockers = result.get("release_blockers")
    blocker_count = len(blockers) if isinstance(blockers, list) else None
    erc_clean = (
        erc.get("applicable") is True
        and erc.get("available") is True
        and erc.get("ran") is True
        and erc.get("errors") == 0
    )
    drc_clean = (
        drc.get("applicable") is True
        and drc.get("available") is True
        and drc.get("ran") is True
        and drc.get("errors") == 0
    )
    zero_unconnected = drc.get("unconnected") == 0 and routing.get("unconnected") == 0
    drc_unconnected = drc.get("unconnected") if isinstance(drc.get("unconnected"), int) else None
    routing_unconnected = (
        routing.get("unconnected") if isinstance(routing.get("unconnected"), int) else None
    )
    unconnected = (
        max(drc_unconnected, routing_unconnected)
        if drc_unconnected is not None and routing_unconnected is not None
        else None
    )
    routing_complete = (
        routing.get("method") == "freerouting"
        and routing.get("unconnected") == 0
        and bool(routing.get("dsn_path"))
        and bool(routing.get("ses_path"))
    )
    artifact_facts, artifacts_valid = _artifact_facts(manifest, root)
    artifact_identity = _artifact_manifest_identity(manifest, root)
    artifact_names = [str(item.get("name", "")).casefold() for item in artifact_facts]
    production_bom_present = any(
        name.endswith("_production_bom.csv") for name in artifact_names
    )
    procurement_bom_present = any(
        name.endswith("_procurement_bom.csv") for name in artifact_names
    )
    core_artifacts_present = artifacts_valid and all(
        any(name.endswith(required) for name in artifact_names)
        for required in (
            "pipeline_result.json",
            ".kicad_sch",
            ".kicad_pcb",
            ".dsn",
            ".ses",
            "_cpl.csv",
        )
    ) and production_bom_present and procurement_bom_present
    pipeline_complete = (
        result.get("completed_steps") == 17
        and result.get("total_steps") == 17
        and result.get("execution_complete") is True
        and result.get("execution_blocked") is False
    )
    release_ready = (
        result.get("release_ready") is True
        and result.get("outcome") == "release_ready"
    )
    strict = (
        pipeline_complete
        and release_ready
        and blocker_count == 0
        and erc_clean
        and drc_clean
        and zero_unconnected
        and routing_complete
        and core_artifacts_present
        and artifact_identity is not None
        and artifact_identity["valid"] is True
    )
    return {
        "pipelineResultValid": True,
        "pipelineComplete": pipeline_complete,
        "releaseReady": release_ready,
        "releaseBlockerCount": blocker_count,
        "ercErrors": erc.get("errors") if isinstance(erc.get("errors"), int) else None,
        "drcErrors": drc.get("errors") if isinstance(drc.get("errors"), int) else None,
        "unconnected": unconnected,
        "routingUnconnected": routing_unconnected,
        "drcUnconnected": drc_unconnected,
        "ercClean": erc_clean,
        "drcClean": drc_clean,
        "zeroUnconnected": zero_unconnected,
        "routingComplete": routing_complete,
        "productionBomPresent": production_bom_present,
        "procurementBomPresent": procurement_bom_present,
        "coreArtifactsPresent": core_artifacts_present,
        "artifactIdentity": artifact_identity,
        "failureFacts": _failure_facts(result),
        "strictGatePassed": strict,
    }


def _capture_stream(
    client: httpx.Client,
    *,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    root: Path,
    arm: str | None = None,
) -> dict[str, Any]:
    started = monotonic()
    events: list[dict[str, Any]] = []
    phases: list[str] = []
    tools: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_call_indexes: dict[str, int] = {}
    handoffs: list[dict[str, Any]] = []
    seen_handoffs: set[tuple[str, str, str, str, str]] = set()
    intent = ""
    done = False
    human_input = False
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    completed_steps = 0
    llm_tokens = 0
    hitl_request_times: list[float] = []
    hitl_response_times: list[float] = []
    with client.stream("POST", endpoint, json=payload, headers=headers) as response:
        status_code = response.status_code
        if status_code >= 400:
            response.read()
            return {
                "httpStatus": status_code,
                "durationSeconds": round(monotonic() - started, 3),
                "done": False,
                "humanInput": False,
                "errors": [f"http_{status_code}"],
                "intent": "",
                "phases": [],
                "tools": [],
                "toolCalls": [],
                "handoffs": None if arm == "single_agent" else [],
                "handoffErrorCount": None if arm == "single_agent" else 0,
                "hitl": {
                    "requestCount": 0,
                    "responseCount": None,
                    "responseLatencySeconds": None,
                },
                "completedSteps": 0,
                "llmTokens": 0,
                "deliveryStatus": None,
                "artifacts": [],
                "artifactsValid": True,
                "releaseEvidence": _pipeline_release_evidence({}, root),
                "eventDigest": _canonical_digest([]),
            }
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            value = line[6:]
            if value == "[DONE]":
                done = True
                continue
            try:
                envelope = json.loads(value)
            except json.JSONDecodeError:
                errors.append("invalid_sse_json")
                continue
            if not isinstance(envelope, dict):
                continue
            envelope_type = str(envelope.get("type", ""))
            if envelope_type == "error":
                errors.append("agent_stream_error")
            if envelope_type == "ag_ui":
                human_input = True
                hitl_request_times.append(monotonic())
            custom: dict[str, Any] = {}
            if envelope_type == "artifact_manifest" and isinstance(envelope.get("content"), dict):
                manifest = dict(envelope["content"])
            elif envelope_type == "message" and isinstance(envelope.get("content"), dict):
                message = envelope["content"]
                for call in message.get("tool_calls", []):
                    if isinstance(call, dict) and call.get("name"):
                        tool = str(call["name"])[:160]
                        tools.append(tool)
                        arguments = call.get("args")
                        call_id = str(call.get("id", ""))
                        entry = next(
                            (
                                item
                                for item in tool_calls
                                if item["tool"] == tool
                                and item["customEvidence"]
                                and item["callIdDigest"] is None
                            ),
                            None,
                        )
                        if entry is None:
                            entry = {
                                "sequence": len(tool_calls) + 1,
                                "tool": tool,
                                "resultStatus": None,
                                "postconditionSatisfied": None,
                                "customEvidence": False,
                            }
                            tool_calls.append(entry)
                        entry.update(
                            {
                                "callIdDigest": (
                                    _sha256_bytes(call_id.encode("utf-8"))
                                    if call_id
                                    else None
                                ),
                                "argumentKeys": (
                                    sorted(str(key) for key in arguments)[:64]
                                    if isinstance(arguments, dict)
                                    else []
                                ),
                                "argumentsSchemaValid": (
                                    True if isinstance(arguments, dict) else False
                                ),
                            }
                        )
                        if call_id:
                            tool_call_indexes[call_id] = tool_calls.index(entry)
                if message.get("type") == "tool" and message.get("tool_call_id"):
                    call_id = str(message["tool_call_id"])
                    index = tool_call_indexes.get(call_id)
                    if index is not None:
                        try:
                            tool_result = json.loads(str(message.get("content", "")))
                        except json.JSONDecodeError:
                            tool_result = None
                        if isinstance(tool_result, dict):
                            tool_calls[index]["resultStatus"] = str(
                                tool_result.get("status", "unknown")
                            )[:80]
                if isinstance(message.get("custom_data"), dict):
                    custom = message["custom_data"]
            if custom.get("kind") == "workflow_event":
                phase = str(custom.get("phase", ""))[:160]
                status = str(custom.get("status", ""))[:80]
                event_type = str(custom.get("event_type", ""))
                if phase and event_type != "handoff":
                    phases.append(phase)
                event: dict[str, Any] = {"kind": "workflow_event", "phase": phase, "status": status}
                if custom.get("event_type") == "intent_decision":
                    intent = str(custom.get("intent", ""))[:40]
                    event["intent"] = intent
                if custom.get("event_type") == "tool_call" and custom.get("tool"):
                    tool = str(custom["tool"])[:160]
                    tools.append(tool)
                    event.update({"tool": tool, "outcome": str(custom.get("outcome", ""))[:80]})
                    entry = next(
                        (
                            item
                            for item in reversed(tool_calls)
                            if item["tool"] == tool and not item["customEvidence"]
                        ),
                        None,
                    )
                    if entry is None:
                        entry = {
                            "sequence": len(tool_calls) + 1,
                            "tool": tool,
                            "callIdDigest": None,
                            "argumentKeys": [],
                            "argumentsSchemaValid": None,
                            "resultStatus": None,
                            "postconditionSatisfied": None,
                            "customEvidence": False,
                        }
                        tool_calls.append(entry)
                    entry["customEvidence"] = True
                    entry["resultStatus"] = str(custom.get("outcome", "unknown"))[:80]
                    if isinstance(custom.get("arguments_schema_valid"), bool):
                        entry["argumentsSchemaValid"] = custom["arguments_schema_valid"]
                    if isinstance(custom.get("postcondition_satisfied"), bool):
                        entry["postconditionSatisfied"] = custom[
                            "postcondition_satisfied"
                        ]
                step_count = custom.get("completed_steps")
                if isinstance(step_count, int):
                    completed_steps = max(completed_steps, step_count)
                    event["completedSteps"] = step_count
                events.append(event)
                if custom.get("event_type") == "human_input_response":
                    hitl_response_times.append(monotonic())
                if event_type == "handoff":
                    handoff = {
                        "handoffId": str(custom.get("handoff_id", ""))[:160],
                        "producer": str(custom.get("producer", ""))[:120],
                        "consumer": str(custom.get("consumer", ""))[:120],
                        "status": str(custom.get("handoff_status", "unknown"))[:80],
                        "payloadDigest": str(custom.get("payload_digest", ""))[:64] or None,
                    }
                    handoff_key = (
                        handoff["handoffId"],
                        handoff["producer"],
                        handoff["consumer"],
                        handoff["status"],
                        str(handoff["payloadDigest"] or ""),
                    )
                    if handoff_key not in seen_handoffs:
                        seen_handoffs.add(handoff_key)
                        handoffs.append(handoff)
            elif custom.get("kind") == "llm_output":
                llm_tokens += _usage_tokens(custom)
                events.append(
                    {
                        "kind": "llm_output",
                        "phase": str(custom.get("phase", ""))[:160],
                        "status": str(custom.get("status", ""))[:80],
                    }
                )
            elif custom.get("kind") == "ahe_event":
                events.append(
                    {
                        "kind": "ahe_event",
                        "event": str(custom.get("event", ""))[:80],
                        "step": str(custom.get("step", ""))[:120],
                    }
                )

    artifact_facts, artifacts_valid = _artifact_facts(manifest, root) if manifest else ([], True)
    release_evidence = _pipeline_release_evidence(manifest, root)
    pipeline_steps = release_evidence.get("pipelineComplete")
    if pipeline_steps is True:
        completed_steps = max(completed_steps, 17)
    delivery_status = manifest.get("delivery_status") if manifest else None
    event_facts = {
        "events": events,
        "done": done,
        "humanInput": human_input,
        "deliveryStatus": delivery_status,
        "artifacts": artifact_facts,
        "toolCalls": tool_calls,
        "handoffs": handoffs,
        "releaseEvidence": release_evidence,
    }
    hitl_latency = None
    if hitl_request_times and hitl_response_times:
        hitl_latency = round(max(0.0, hitl_response_times[0] - hitl_request_times[0]), 3)
    if arm == "single_agent" and handoffs:
        errors.append("unexpected_single_agent_handoff")
    handoff_facts: list[dict[str, Any]] | None = None if arm == "single_agent" else handoffs or None
    return {
        "httpStatus": status_code,
        "durationSeconds": round(monotonic() - started, 3),
        "done": done,
        "humanInput": human_input,
        "errors": sorted(set(errors)),
        "intent": intent,
        "phases": list(dict.fromkeys(phases)),
        "tools": sorted(set(tools)),
        "toolCalls": tool_calls,
        "handoffs": handoff_facts,
        "handoffErrorCount": (
            None
            if handoff_facts is None
            else sum(item["status"] not in {"accepted", "completed", "ok"} for item in handoff_facts)
        ),
        "hitl": {
            "requestCount": len(hitl_request_times),
            "responseCount": len(hitl_response_times) if hitl_response_times else None,
            "responseLatencySeconds": hitl_latency,
        },
        "completedSteps": completed_steps,
        "llmTokens": llm_tokens,
        "deliveryStatus": delivery_status,
        "artifacts": artifact_facts,
        "artifactsValid": artifacts_valid,
        "releaseEvidence": release_evidence,
        "eventDigest": _canonical_digest(event_facts),
    }


def _grade(
    case: LiveCase, observed: dict[str, Any], replay: dict[str, Any] | None
) -> dict[str, bool | None]:
    phases = set(observed["phases"])
    tools = set(observed["tools"])
    artifact_names = [str(item["name"]).casefold() for item in observed["artifacts"]]
    terminal_ok = (
        observed["httpStatus"] == 200
        and not observed["errors"]
        and (
            observed["humanInput"]
            if case.expected_terminal == "waiting_for_input"
            else observed["done"] and not observed["humanInput"]
        )
    )
    artifact_ok = all(
        any(name.endswith(required.casefold()) for name in artifact_names)
        for required in case.required_artifacts
    ) and (observed["artifactsValid"] if case.required_artifacts else True)
    release_ready = observed["deliveryStatus"] == "release_ready"
    release_ok = (
        True
        if case.expect_release_ready is None
        else release_ready == case.expect_release_ready
    )
    if release_ready:
        release_ok = (
            release_ok
            and artifact_ok
            and bool(observed["artifacts"])
            and observed["artifactsValid"]
            and observed.get("releaseEvidence", {}).get("strictGatePassed") is True
        )
    eda_pipeline_ok = True
    if case.category == "eda_pipeline":
        required_eda_artifacts = (
            "pipeline_result.json",
            ".kicad_sch",
            ".kicad_pcb",
        )
        completed_steps = observed.get("completedSteps", 0)
        eda_pipeline_ok = (
            isinstance(completed_steps, int)
            and completed_steps >= 17
            and observed.get("deliveryStatus")
            in {"completed_with_issues", "delivered_with_issues", "release_ready"}
            and all(
                any(name.endswith(required) for name in artifact_names)
                for required in required_eda_artifacts
            )
            and observed["artifactsValid"]
        )
    replay_ok = True
    if case.replay == "same":
        replay_ok = bool(
            replay
            and replay["httpStatus"] == 200
            and replay["eventDigest"] == observed["eventDigest"]
        )
    elif case.replay == "conflict":
        replay_ok = bool(replay and replay["httpStatus"] == 409)
    tool_evidence_ok: bool | None = None
    if case.expected_tool_calls is not None:
        observed_calls = observed.get("toolCalls", [])
        tool_evidence_ok = (
            [str(item.get("tool", "")) for item in observed_calls]
            == case.expected_tool_calls
            and all(
                item.get("argumentsSchemaValid") is True
                and item.get("resultStatus") is not None
                and item.get("postconditionSatisfied") is not None
                for item in observed_calls
            )
        )
    handoff_evidence_ok: bool | None = None
    if case.expected_handoffs is not None:
        observed_handoffs = observed.get("handoffs")
        handoff_evidence_ok = bool(
            observed_handoffs is not None
            and [str(item.get("handoffId", "")) for item in observed_handoffs]
            == case.expected_handoffs
            and observed.get("handoffErrorCount") == 0
        )
    return {
        "intent": observed["intent"] in case.expected_intents,
        "requiredPhases": set(case.required_phases) <= phases,
        "forbiddenPhases": not (set(case.forbidden_phases) & phases),
        "requiredTools": set(case.required_tools) <= tools,
        "forbiddenTools": not (set(case.forbidden_tools) & tools),
        "terminal": terminal_ok,
        "artifacts": artifact_ok,
        "releaseGate": release_ok,
        "edaPipeline": eda_pipeline_ok,
        "replay": replay_ok,
        "toolEvidence": tool_evidence_ok,
        "handoffEvidence": handoff_evidence_ok,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = path.with_suffix(".md")
    metrics = report["metrics"]
    lines = [
        "# Live Agent evaluation",
        "",
        "> Scope: real HTTP/SSE runs with sanitized workflow evidence. This is not a manufacturing approval.",
        "",
        f"- Plan: `{report['planId']}` (`{report['planDigest']}`)",
        f"- Source commit: `{report['sourceCommit']}`",
        f"- Cases: {metrics['passedCases']}/{metrics['caseCount']}",
        f"- Pass rate: {metrics['passRate']:.3f}",
        f"- Intent accuracy: {metrics['intentAccuracy']:.3f}",
        f"- Tool-contract accuracy: {metrics['toolContractAccuracy']:.3f}",
        f"- Gate accuracy: {metrics['gateAccuracy']:.3f}",
        f"- False releases: {metrics['falseReleaseCount']}",
        f"- Strict release evidence: {metrics['strictReleaseEvidenceRate']}",
        f"- ERC clean: {metrics['ercCleanRate']}",
        f"- DRC clean: {metrics['drcCleanRate']}",
        f"- Zero unconnected: {metrics['zeroUnconnectedRate']}",
        f"- Freerouting complete: {metrics['routingCompletionRate']}",
        f"- ERC / DRC / unconnected totals: {metrics['ercErrorCount']} / "
        f"{metrics['drcErrorCount']} / {metrics['unconnectedCount']}",
        f"- Artifact identity verified: {metrics['artifactIdentityRate']}",
        "",
        "| Case | Category | Intent | Status | Tools | Duration | Result |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for item in report["cases"]:
        lines.append(
            f"| `{item['caseId']}` | {item['category']} | {item['observed']['intent'] or '-'} | "
            f"{item['observed']['deliveryStatus'] or item['expectedTerminal']} | "
            f"{len(item['observed']['tools'])} | {item['observed']['durationSeconds']:.1f}s | "
            f"{'PASS' if item['passed'] else 'FAIL'} |"
        )
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_frozen_execution(
    plan: LivePlan,
    *,
    model: str | None,
    provider: str | None,
    environment_digest: str | None,
    config_digest: str | None,
) -> str | None:
    frozen = plan.frozen_execution
    if frozen is None:
        return model
    actual = {
        "model": model,
        "provider": provider,
        "environmentDigest": environment_digest,
        "configDigest": config_digest,
    }
    expected = frozen.model_dump(mode="json", by_alias=True)
    mismatches = [
        key for key, value in expected.items() if actual.get(key) != value
    ]
    if mismatches:
        raise ValueError(
            "paired evaluation runtime does not match frozenExecution: "
            + ", ".join(sorted(mismatches))
        )
    return model


def _validate_asset_manifest(root: Path, plan: LivePlan) -> set[str]:
    """Fail closed before network calls when a declared asset snapshot drifts."""

    if plan.asset_manifest_path is None or plan.asset_manifest_digest is None:
        return set()
    candidate = (root / plan.asset_manifest_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("assetManifestPath must stay inside the repository root") from exc
    raw = candidate.read_bytes()
    actual_digest = _sha256_bytes(raw)
    if actual_digest != plan.asset_manifest_digest:
        raise ValueError(
            "asset manifest digest mismatch: "
            f"expected {plan.asset_manifest_digest}, got {actual_digest}"
        )
    document = json.loads(raw)
    bindings = document.get("bindings") if isinstance(document, dict) else None
    if not isinstance(bindings, list):
        raise ValueError("asset manifest bindings must be an array")
    verified: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError("asset manifest bindings must be objects")
        asset_id = binding.get("assetId")
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("every asset binding requires assetId")
        if asset_id in verified:
            raise ValueError(f"duplicate assetId in asset manifest: {asset_id}")
        required = (
            "symbol",
            "footprint",
            "symbolPinCount",
            "footprintPadCount",
            "symbolLibrarySha256",
            "footprintFileSha256",
        )
        if any(key not in binding for key in required) or binding.get("pinPadCompatible") is not True:
            raise ValueError(f"asset {asset_id!r} is not a closed verified binding")
        if any(
            not isinstance(binding.get(key), int) or int(binding[key]) < 0
            for key in ("symbolPinCount", "footprintPadCount")
        ):
            raise ValueError(f"asset {asset_id!r} has invalid pin/pad counts")
        if any(
            not isinstance(binding.get(key), str)
            or len(str(binding[key])) != 64
            or any(character not in "0123456789abcdef" for character in str(binding[key]))
            for key in ("symbolLibrarySha256", "footprintFileSha256")
        ):
            raise ValueError(f"asset {asset_id!r} has invalid file digests")
        verified.add(asset_id)
    requested = {
        asset_id
        for case in plan.cases
        for asset_id in case.verified_asset_ids
    }
    missing = requested - verified
    if missing:
        raise ValueError(
            "plan references unknown verifiedAssetIds: " + ", ".join(sorted(missing))
        )
    if (
        plan.frozen_execution is not None
        and plan.frozen_execution.environment_digest != actual_digest
    ):
        raise ValueError("frozenExecution.environmentDigest is not the asset manifest digest")
    return verified


def _load_blind_reviews(
    path: Path | None,
    *,
    plan_digest: str,
) -> dict[str, BlindReviewLabel]:
    if path is None:
        return {}
    manifest = BlindReviewManifest.model_validate_json(path.read_bytes())
    if manifest.plan_digest != plan_digest:
        raise ValueError("blind-review manifest is not bound to this plan digest")
    return {label.case_id: label for label in manifest.labels}


def run_plan(
    *,
    root: Path,
    plan_path: Path,
    endpoint: str,
    output: Path,
    selected_cases: set[str],
    model: str | None,
    auth_token: str | None,
    single_agent_endpoint: str | None = None,
    provider: str | None = None,
    environment_digest: str | None = None,
    config_digest: str | None = None,
    blind_review_path: Path | None = None,
) -> dict[str, Any]:
    plan_bytes = plan_path.read_bytes()
    plan = LivePlan.model_validate_json(plan_bytes)
    _validate_asset_manifest(root, plan)
    model = _validate_frozen_execution(
        plan,
        model=model,
        provider=provider,
        environment_digest=environment_digest,
        config_digest=config_digest,
    )
    plan_digest = _sha256_bytes(plan_bytes)
    blind_reviews = _load_blind_reviews(
        blind_review_path,
        plan_digest=plan_digest,
    )
    unknown_review_cases = set(blind_reviews) - {case.case_id for case in plan.cases}
    if unknown_review_cases:
        raise ValueError(
            "blind-review manifest contains unknown case IDs: "
            + ", ".join(sorted(unknown_review_cases))
        )
    source_commit, dirty = _git_identity(root)
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    results: list[dict[str, Any]] = []
    # Live evaluation targets the explicitly supplied Agent Runtime endpoint.
    # Do not let workstation-level HTTP(S)_PROXY settings silently redirect a
    # loopback request and turn a healthy local service into a proxy 502.
    with httpx.Client(timeout=None, trust_env=False) as client:
        for case in plan.cases:
            if selected_cases and case.case_id not in selected_cases:
                continue
            request_id = f"p2-{case.case_id}-{uuid4().hex[:12]}"
            thread_id = f"p2-thread-{uuid4().hex}"
            agent_config = {
                "intent_llm_enabled": False,
                "project_name": f"p2-{case.case_id}",
                "run_name": f"p2-{case.case_id}-{uuid4().hex[:8]}",
                **case.agent_config,
            }
            profile = _profile_selection(case.profile_reference)
            if profile is not None:
                agent_config["capability_profile"] = profile
            frozen_input_digest = _canonical_digest(
                {
                    "pairId": case.pair_id,
                    "prompt": case.prompt,
                    "model": model,
                    "profileReference": case.profile_reference,
                    "timeoutSeconds": case.timeout_seconds,
                    "agentConfig": case.agent_config,
                    "frozenExecution": (
                        plan.frozen_execution.model_dump(mode="json", by_alias=True)
                        if plan.frozen_execution is not None
                        else None
                    ),
                }
            )
            payload: dict[str, Any] = {
                "message": case.prompt,
                "thread_id": thread_id,
                "user_id": "p2-live-eval",
                "request_id": request_id,
                "timeout_seconds": case.timeout_seconds,
                "agent_config": agent_config,
                "stream_tokens": False,
            }
            if model:
                payload["model"] = model
            case_endpoint = endpoint
            if case.arm == "single_agent":
                if not single_agent_endpoint:
                    raise ValueError("single-agent cases require single_agent_endpoint")
                case_endpoint = single_agent_endpoint
            observed = _capture_stream(
                client,
                endpoint=case_endpoint,
                payload=payload,
                headers=headers,
                root=root,
                arm=case.arm,
            )
            replay: dict[str, Any] | None = None
            if case.replay == "same":
                replay = _capture_stream(
                    client,
                    endpoint=case_endpoint,
                    payload=payload,
                    headers=headers,
                    root=root,
                    arm=case.arm,
                )
            elif case.replay == "conflict":
                conflicting = {**payload, "message": case.prompt + "\nconflicting replay"}
                replay = _capture_stream(
                    client,
                    endpoint=case_endpoint,
                    payload=conflicting,
                    headers=headers,
                    root=root,
                    arm=case.arm,
                )
            checks = _grade(case, observed, replay)
            blind_label = blind_reviews.get(case.case_id)
            result = {
                "caseId": case.case_id,
                "category": case.category,
                "arm": case.arm,
                "pairId": case.pair_id,
                "frozenInputDigest": frozen_input_digest,
                "expectedTerminal": case.expected_terminal,
                "requestFingerprint": _sha256_bytes(request_id.encode("utf-8")),
                "observed": observed,
                "replay": replay,
                "checks": checks,
                "humanAcceptance": (
                    None
                    if blind_label is None
                    else {
                        "accepted": blind_label.accepted,
                        "rubricVersion": blind_label.rubric_version,
                        "reviewerIdHash": blind_label.reviewer_id_hash,
                        "rubricScores": blind_label.rubric_scores,
                        "blockingFindings": blind_label.blocking_findings,
                        "notesDigest": blind_label.notes_digest,
                    }
                ),
                "passed": all(value is not False for value in checks.values()),
            }
            results.append(result)
            report = _report(plan, plan_bytes, source_commit, dirty, results)
            _write_report(output, report)
            print(
                f"{case.case_id}: {'PASS' if result['passed'] else 'FAIL'} "
                f"intent={observed['intent'] or '-'} tools={len(observed['tools'])} "
                f"duration={observed['durationSeconds']:.1f}s",
                flush=True,
            )
    return _report(plan, plan_bytes, source_commit, dirty, results)


def _nullable_rate(values: list[bool | None]) -> float | None:
    observed = [value for value in values if value is not None]
    if not observed:
        return None
    return sum(value is True for value in observed) / len(observed)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _release_metric(items: list[dict[str, Any]], key: str) -> float | None:
    eda_items = [item for item in items if item.get("category") == "eda_pipeline"]
    if not eda_items:
        return None
    return sum(
        item.get("observed", {}).get("releaseEvidence", {}).get(key) is True
        for item in eda_items
    ) / len(eda_items)


def _eda_check_metric(items: list[dict[str, Any]], key: str) -> float | None:
    eda_items = [item for item in items if item.get("category") == "eda_pipeline"]
    if not eda_items:
        return None
    return sum(item.get("checks", {}).get(key) is True for item in eda_items) / len(
        eda_items
    )


def _release_count(items: list[dict[str, Any]], key: str) -> int | None:
    eda_items = [item for item in items if item.get("category") == "eda_pipeline"]
    values = [
        item.get("observed", {}).get("releaseEvidence", {}).get(key)
        for item in eda_items
    ]
    if not values or any(not isinstance(value, int) for value in values):
        return None
    return sum(values)


def _artifact_identity_rate(items: list[dict[str, Any]]) -> float | None:
    eda_items = [item for item in items if item.get("category") == "eda_pipeline"]
    if not eda_items:
        return None
    valid = 0
    for item in eda_items:
        identity = (
            item.get("observed", {})
            .get("releaseEvidence", {})
            .get("artifactIdentity")
        )
        valid += isinstance(identity, dict) and identity.get("valid") is True
    return valid / len(eda_items)


def _failure_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, int] = {}
    signatures: dict[str, dict[str, Any]] = {}
    for item in items:
        facts = item.get("observed", {}).get("releaseEvidence", {}).get("failureFacts", [])
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            stage = str(fact.get("stage") or "unknown")[:80]
            check = str(fact.get("check") or "unknown")[:120]
            severity = str(fact.get("severity") or "error")[:40]
            signature = str(fact.get("signature") or "")[:64]
            if len(signature) != 64:
                continue
            stages[stage] = stages.get(stage, 0) + 1
            entry = signatures.setdefault(
                signature,
                {
                    "signature": signature,
                    "stage": stage,
                    "check": check,
                    "severity": severity,
                    "count": 0,
                },
            )
            entry["count"] += 1
    return {
        "byStage": dict(sorted(stages.items())),
        "bySignature": sorted(
            signatures.values(), key=lambda item: (-item["count"], item["signature"])
        ),
    }


def _arm_metrics(items: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    durations = [
        float(item["observed"]["durationSeconds"])
        for item in items
        if isinstance(item.get("observed", {}).get("durationSeconds"), (int, float))
    ]
    reported_releases = [
        (
            None
            if item["observed"].get("deliveryStatus") is None
            else item["observed"].get("deliveryStatus") == "release_ready"
        )
        for item in items
    ]
    human = [
        None if item.get("humanAcceptance") is None else bool(item["humanAcceptance"]["accepted"])
        for item in items
    ]
    phase_contract = [
        (
            None
            if not isinstance(item.get("checks"), dict)
            else bool(item["checks"].get("requiredPhases"))
            and bool(item["checks"].get("forbiddenPhases"))
        )
        for item in items
    ]
    tool_contract = [
        (
            None
            if not isinstance(item.get("checks"), dict)
            else bool(item["checks"].get("requiredTools"))
            and bool(item["checks"].get("forbiddenTools"))
        )
        for item in items
    ]
    tool_calls = [
        call
        for item in items
        for call in item.get("observed", {}).get("toolCalls", [])
        if isinstance(call, dict)
    ]
    argument_checks = [
        call.get("argumentsSchemaValid")
        for call in tool_calls
        if isinstance(call.get("argumentsSchemaValid"), bool)
    ]
    postcondition_checks = [
        call.get("postconditionSatisfied")
        for call in tool_calls
        if isinstance(call.get("postconditionSatisfied"), bool)
    ]
    handoffs = [
        handoff
        for item in items
        for handoff in (item.get("observed", {}).get("handoffs") or [])
        if isinstance(handoff, dict)
    ]
    handoff_errors = [
        item.get("observed", {}).get("handoffErrorCount")
        for item in items
        if isinstance(item.get("observed", {}).get("handoffErrorCount"), int)
    ]
    eda_items = [item for item in items if item.get("category") == "eda_pipeline"]
    hitl_request_counts = [
        item.get("observed", {}).get("hitl", {}).get("requestCount")
        for item in items
        if isinstance(item.get("observed", {}).get("hitl", {}).get("requestCount"), int)
    ]
    ordered_durations = sorted(durations)
    return {
        "caseCount": len(items),
        "transportCompletionRate": _nullable_rate(
            [
                item.get("observed", {}).get("httpStatus") == 200
                if isinstance(item.get("observed", {}).get("httpStatus"), int)
                else None
                for item in items
            ]
        ),
        "protocolCompletionRate": _nullable_rate(
            [
                item.get("checks", {}).get("terminal")
                if isinstance(item.get("checks", {}).get("terminal"), bool)
                else None
                for item in items
            ]
        ),
        "pipeline17StepCompletionRate": _nullable_rate(
            [
                item.get("observed", {}).get("completedSteps", 0) >= 17
                if isinstance(item.get("observed", {}).get("completedSteps"), int)
                else None
                for item in eda_items
            ]
        ),
        "strictTaskSuccessRate": _nullable_rate(
            [item.get("passed") if isinstance(item.get("passed"), bool) else None for item in items]
        ),
        "releaseReadyRate": _release_metric(items, "strictGatePassed"),
        "reportedReleaseReadyRate": _nullable_rate(reported_releases),
        "releaseReadyEvidenceRate": _release_metric(items, "releaseReady"),
        "strictReleaseEvidenceRate": _release_metric(items, "strictGatePassed"),
        "ercErrorCount": _release_count(items, "ercErrors"),
        "drcErrorCount": _release_count(items, "drcErrors"),
        "unconnectedCount": _release_count(items, "unconnected"),
        "ercCleanRate": _release_metric(items, "ercClean"),
        "drcCleanRate": _release_metric(items, "drcClean"),
        "zeroUnconnectedRate": _release_metric(items, "zeroUnconnected"),
        "routingCompletionRate": _release_metric(items, "routingComplete"),
        "pipelineResultEvidenceRate": _release_metric(items, "pipelineResultValid"),
        "coreArtifactClosureRate": _release_metric(items, "coreArtifactsPresent"),
        "artifactIdentityRate": _artifact_identity_rate(items),
        "artifactGatePassRate": _eda_check_metric(items, "artifacts"),
        "releaseStatusObservationCoverage": (
            sum(value is not None for value in reported_releases) / len(reported_releases)
            if reported_releases
            else None
        ),
        "humanAcceptanceRate": _nullable_rate(human),
        "humanReviewCoverage": (
            sum(value is not None for value in human) / len(human) if human else None
        ),
        "phaseContractErrorRate": (
            None
            if (phase_rate := _nullable_rate(phase_contract)) is None
            else 1.0 - phase_rate
        ),
        "toolContractErrorRate": (
            None
            if (tool_rate := _nullable_rate(tool_contract)) is None
            else 1.0 - tool_rate
        ),
        "meanDurationSeconds": _mean(durations),
        "medianDurationSeconds": median(durations) if durations else None,
        "p95DurationSeconds": (
            ordered_durations[ceil(0.95 * len(ordered_durations)) - 1]
            if len(ordered_durations) >= 5
            else None
        ),
        "hitlInterventionRate": _nullable_rate(
            [
                (
                    item.get("observed", {}).get("hitl", {}).get("requestCount", 0) > 0
                    if isinstance(item.get("observed", {}).get("hitl"), dict)
                    else None
                )
                for item in items
            ]
        ),
        "hitlRequestCount": sum(hitl_request_counts) if hitl_request_counts else None,
        "toolArgumentSchemaValidityRate": _nullable_rate(argument_checks),
        "toolPostconditionPassRate": _nullable_rate(postcondition_checks),
        "observedToolCallCount": len(tool_calls),
        # A single-agent arm has no role boundary by design. Reporting zero
        # would incorrectly imply that a handoff contract was exercised.
        "handoffEvidenceStatus": "not_applicable" if arm == "single_agent" else "observed" if handoffs else "missing",
        "observedHandoffCount": None if arm == "single_agent" else len(handoffs) if handoffs else None,
        "handoffErrorCount": (
            None
            if arm == "single_agent" or not handoff_errors
            else sum(handoff_errors)
        ),
        "handoffErrorRate": (
            None
            if arm == "single_agent" or not handoffs or not handoff_errors
            else sum(handoff_errors) / len(handoffs)
        ),
    }


def _metric_delta(
    multi: dict[str, Any],
    single: dict[str, Any],
    key: str,
) -> float | None:
    left = multi.get(key)
    right = single.get(key)
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return float(left) - float(right)


def _paired_comparison(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for item in results:
        pair_id = item.get("pairId")
        arm = item.get("arm")
        if pair_id and arm in {"single_agent", "multi_agent"}:
            pairs.setdefault(str(pair_id), {})[str(arm)] = item
    outcomes: list[dict[str, Any]] = []
    for pair_id, arms in sorted(pairs.items()):
        arm_outcomes: dict[str, Any] = {}
        for arm in ("single_agent", "multi_agent"):
            item = arms.get(arm)
            if item is None:
                arm_outcomes[arm] = None
                continue
            observed = item["observed"]
            status = observed.get("deliveryStatus")
            required_phases = item.get("checks", {}).get("requiredPhases")
            forbidden_phases = item.get("checks", {}).get("forbiddenPhases")
            required_tools = item.get("checks", {}).get("requiredTools")
            forbidden_tools = item.get("checks", {}).get("forbiddenTools")
            arm_outcomes[arm] = {
                "caseId": item["caseId"],
                "transportCompleted": (
                    observed.get("httpStatus") == 200
                    if isinstance(observed.get("httpStatus"), int)
                    else None
                ),
                "protocolCompleted": item.get("checks", {}).get("terminal"),
                "completedSteps": observed.get("completedSteps"),
                "strictTaskSuccess": item.get("passed"),
                "releaseReady": observed.get("releaseEvidence", {}).get(
                    "strictGatePassed"
                ),
                "reportedReleaseReady": None
                if status is None
                else status == "release_ready",
                "strictReleaseEvidence": observed.get("releaseEvidence", {}).get(
                    "strictGatePassed"
                ),
                "humanAccepted": (
                    None
                    if item.get("humanAcceptance") is None
                    else bool(item["humanAcceptance"]["accepted"])
                ),
                "durationSeconds": observed.get("durationSeconds"),
                "hitlIntervened": (
                    observed.get("hitl", {}).get("requestCount", 0) > 0
                    if isinstance(observed.get("hitl"), dict)
                    else None
                ),
                "phaseContractOk": (
                    required_phases and forbidden_phases
                    if isinstance(required_phases, bool) and isinstance(forbidden_phases, bool)
                    else None
                ),
                "toolContractOk": (
                    required_tools and forbidden_tools
                    if isinstance(required_tools, bool) and isinstance(forbidden_tools, bool)
                    else None
                ),
            }
        outcomes.append(
            {
                "pairId": pair_id,
                "complete": set(arms) == {"single_agent", "multi_agent"},
                "arms": arm_outcomes,
            }
        )
    complete = [item for item in outcomes if item["complete"]]
    complete_pair_ids = {str(item["pairId"]) for item in complete}
    paired_arm_metrics = {
        arm: _arm_metrics(
            [
                item
                for item in results
                if item.get("arm") == arm and str(item.get("pairId")) in complete_pair_ids
            ],
            arm,
        )
        for arm in ("single_agent", "multi_agent")
    }
    single = paired_arm_metrics["single_agent"]
    multi = paired_arm_metrics["multi_agent"]
    delta_keys = (
        "transportCompletionRate",
        "protocolCompletionRate",
        "pipeline17StepCompletionRate",
        "strictTaskSuccessRate",
        "releaseReadyRate",
        "reportedReleaseReadyRate",
        "releaseReadyEvidenceRate",
        "strictReleaseEvidenceRate",
        "ercErrorCount",
        "drcErrorCount",
        "unconnectedCount",
        "ercCleanRate",
        "drcCleanRate",
        "zeroUnconnectedRate",
        "routingCompletionRate",
        "pipelineResultEvidenceRate",
        "coreArtifactClosureRate",
        "artifactIdentityRate",
        "artifactGatePassRate",
        "humanAcceptanceRate",
        "phaseContractErrorRate",
        "toolContractErrorRate",
        "meanDurationSeconds",
        "medianDurationSeconds",
        "p95DurationSeconds",
        "hitlInterventionRate",
        "toolArgumentSchemaValidityRate",
        "toolPostconditionPassRate",
    )
    return {
        "pairCount": len(pairs),
        "completePairCount": len(complete),
        "incompletePairCount": sum(not item["complete"] for item in outcomes),
        "deltaDenominatorCompletePairs": len(complete),
        "deltaConvention": "multi_agent_minus_single_agent",
        "completePairArmMetrics": paired_arm_metrics,
        "metricDeltas": {
            key: _metric_delta(multi, single, key) for key in delta_keys
        },
        "pairs": outcomes,
    }


def _report(
    plan: LivePlan,
    plan_bytes: bytes,
    source_commit: str,
    dirty: bool,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    count = len(results)
    passed = sum(bool(item["passed"]) for item in results)
    check_rate = lambda key: (  # noqa: E731 - compact metric projection
        sum(bool(item["checks"][key]) for item in results) / count if count else 0.0
    )
    evidence_rate = lambda key: (  # noqa: E731 - N/A is excluded, never imputed
        (
            sum(bool(value) for value in values) / len(values)
            if values
            else None
        )
        if (values := [item["checks"][key] for item in results if item["checks"][key] is not None])
        else None
    )
    false_releases = sum(
        item["observed"]["deliveryStatus"] == "release_ready" and not item["checks"]["releaseGate"]
        for item in results
    )
    eda_results = [item for item in results if item["category"] == "eda_pipeline"]
    human_labels = [
        item["humanAcceptance"] for item in results if item["humanAcceptance"] is not None
    ]
    handoff_counts = [
        item["observed"]["handoffErrorCount"]
        for item in results
        if item["observed"].get("handoffErrorCount") is not None
    ]
    observed_tool_calls = [
        call
        for item in results
        for call in item["observed"].get("toolCalls", [])
        if isinstance(call, dict)
    ]
    schema_observations = [
        call.get("argumentsSchemaValid")
        for call in observed_tool_calls
        if call.get("argumentsSchemaValid") is not None
    ]
    postcondition_observations = [
        call.get("postconditionSatisfied")
        for call in observed_tool_calls
        if call.get("postconditionSatisfied") is not None
    ]
    result_status_observations = [
        call.get("resultStatus") is not None for call in observed_tool_calls
    ]
    arm_metrics = {
        arm: _arm_metrics(
            [item for item in results if item.get("arm") == arm],
            arm,
        )
        for arm in ("single_agent", "multi_agent")
    }
    paired_comparison = _paired_comparison(results)
    return {
        "schemaVersion": "1.0",
        "scope": "live_http_sse_agent_evaluation_not_manufacturing_approval",
        "planId": plan.plan_id,
        "planDigest": _sha256_bytes(plan_bytes),
        "sourceCommit": source_commit,
        "sourceDirty": dirty,
        "frozenExecution": (
            plan.frozen_execution.model_dump(mode="json", by_alias=True)
            if plan.frozen_execution is not None
            else None
        ),
        "createdAt": datetime.now(UTC).isoformat(),
        "cases": results,
        "armMetrics": arm_metrics,
        "pairedComparison": paired_comparison,
        "failureSummary": _failure_summary(results),
        "metrics": {
            "caseCount": count,
            "passedCases": passed,
            "passRate": passed / count if count else 0.0,
            "transportCompletionRate": (
                sum(item["observed"]["httpStatus"] == 200 for item in results) / count
                if count
                else 0.0
            ),
            "protocolCompletionRate": check_rate("terminal"),
            "pipelineCompletionRate": (
                sum(item["observed"].get("completedSteps", 0) >= 17 for item in eda_results)
                / len(eda_results)
                if eda_results
                else None
            ),
            "reportedReleaseReadyRate": (
                sum(
                    item["observed"].get("deliveryStatus") == "release_ready"
                    for item in results
                )
                / count
                if count
                else 0.0
            ),
            "releaseReadyRate": _release_metric(results, "strictGatePassed"),
            "releaseReadyEvidenceRate": _release_metric(results, "releaseReady"),
            "strictReleaseEvidenceRate": _release_metric(results, "strictGatePassed"),
            "ercErrorCount": _release_count(results, "ercErrors"),
            "drcErrorCount": _release_count(results, "drcErrors"),
            "unconnectedCount": _release_count(results, "unconnected"),
            "ercCleanRate": _release_metric(results, "ercClean"),
            "drcCleanRate": _release_metric(results, "drcClean"),
            "zeroUnconnectedRate": _release_metric(results, "zeroUnconnected"),
            "routingCompletionRate": _release_metric(results, "routingComplete"),
            "pipelineResultEvidenceRate": _release_metric(
                results, "pipelineResultValid"
            ),
            "coreArtifactClosureRate": _release_metric(
                results, "coreArtifactsPresent"
            ),
            "artifactIdentityRate": _artifact_identity_rate(results),
            "artifactGatePassRate": _eda_check_metric(results, "artifacts"),
            "intentAccuracy": check_rate("intent"),
            "toolContractAccuracy": (
                (check_rate("requiredTools") + check_rate("forbiddenTools")) / 2
            ),
            "gateAccuracy": check_rate("releaseGate"),
            "edaPipelineAccuracy": check_rate("edaPipeline"),
            "toolEvidenceAccuracy": evidence_rate("toolEvidence"),
            # These rates describe only emitted runtime evidence. Missing
            # evidence remains N/A and is never silently counted as success.
            "observedToolCallCount": len(observed_tool_calls),
            "toolArgumentSchemaValidityRate": (
                sum(value is True for value in schema_observations)
                / len(schema_observations)
                if schema_observations
                else None
            ),
            "toolPostconditionPassRate": (
                sum(value is True for value in postcondition_observations)
                / len(postcondition_observations)
                if postcondition_observations
                else None
            ),
            "toolResultStatusCoverageRate": (
                sum(result_status_observations) / len(result_status_observations)
                if result_status_observations
                else None
            ),
            "handoffEvidenceAccuracy": evidence_rate("handoffEvidence"),
            "handoffErrorCount": sum(handoff_counts) if handoff_counts else None,
            "humanAcceptanceRate": (
                sum(bool(label["accepted"]) for label in human_labels) / len(human_labels)
                if human_labels
                else None
            ),
            "humanReviewedCaseCount": len(human_labels),
            "hitlRequestCount": sum(
                item["observed"].get("hitl", {}).get("requestCount", 0)
                for item in results
            ),
            "hitlResponseCount": (
                sum(
                    int(item["observed"].get("hitl", {}).get("responseCount", 0) or 0)
                    for item in results
                )
                if any(
                    item["observed"].get("hitl", {}).get("responseCount") is not None
                    for item in results
                )
                else None
            ),
            "pairCount": paired_comparison["pairCount"],
            "completePairCount": paired_comparison["completePairCount"],
            "falseReleaseCount": false_releases,
            "totalLlmTokens": sum(item["observed"]["llmTokens"] for item in results),
            "totalWallClockSeconds": sum(item["observed"]["durationSeconds"] for item in results),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, default=Path("evals/live/cases.v1.json"))
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8080/ratsnestpro-multi-agent/stream",
    )
    parser.add_argument(
        "--single-agent-endpoint",
        default="http://127.0.0.1:8080/ratsnestpro-single-agent-eval/stream",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--provider")
    parser.add_argument("--environment-digest")
    parser.add_argument("--config-digest")
    parser.add_argument("--blind-review-manifest", type=Path)
    parser.add_argument("--auth-token-env", default="AUTH_SECRET")
    parser.add_argument("--min-pass-rate", type=float, default=0.85)
    parser.add_argument("--max-false-release-count", type=int, default=0)
    args = parser.parse_args()
    root = args.root.resolve()
    plan_path = args.plan if args.plan.is_absolute() else root / args.plan
    output = args.output if args.output.is_absolute() else root / args.output
    blind_review_path = args.blind_review_manifest
    if blind_review_path is not None and not blind_review_path.is_absolute():
        blind_review_path = root / blind_review_path
    report = run_plan(
        root=root,
        plan_path=plan_path,
        endpoint=args.endpoint,
        output=output,
        selected_cases=set(args.case),
        model=args.model,
        auth_token=os.getenv(args.auth_token_env),
        single_agent_endpoint=args.single_agent_endpoint,
        provider=args.provider,
        environment_digest=args.environment_digest,
        config_digest=args.config_digest,
        blind_review_path=blind_review_path,
    )
    metrics = report["metrics"]
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    return int(
        metrics["passRate"] < args.min_pass_rate
        or metrics["falseReleaseCount"] > args.max_false_release_count
    )


if __name__ == "__main__":
    raise SystemExit(main())
