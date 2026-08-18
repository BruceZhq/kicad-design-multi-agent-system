"""Strict, serialization-safe contracts for governed harness evolution."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_OCI_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


def utc_now() -> datetime:
    return datetime.now(UTC)


class EvolutionModel(BaseModel):
    """Strict contract base with one canonical camelCase JSON representation."""

    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class HarnessChannel(StrEnum):
    STABLE = "stable"
    CANARY = "canary"
    PREVIOUS_STABLE = "previous_stable"
    EVALUATION = "evaluation"
    DEVELOPMENT = "development"


class HarnessManifest(EvolutionModel):
    """Immutable identity emitted by ``scripts/build_harness_manifest.ps1``."""

    schema_version: Literal["1.0"] = "1.0"
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree_digest: str = Field(pattern=_DIGEST_PATTERN)
    dirty: bool
    bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    runtime_image_digest: str | None = Field(default=None, pattern=_OCI_DIGEST_PATTERN)
    toolchain_digest: str | None = Field(
        default=None,
        pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
    )
    manifest_digest: str = Field(pattern=_DIGEST_PATTERN)

    def calculated_manifest_digest(self) -> str:
        payload = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={"manifest_digest"},
        )
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HarnessIdentity(EvolutionModel):
    version_id: str = Field(min_length=1, max_length=120)
    channel: HarnessChannel
    manifest_digest: str = Field(pattern=_DIGEST_PATTERN)


def resolve_harness_identity(
    run_input: dict[str, Any] | None = None,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    require_explicit: bool = False,
) -> HarnessIdentity:
    """Resolve and attest an immutable run identity against this runtime.

    An explicit identity is the control plane's run pin.  Environment values
    identify the deployed runtime image.  When both exist they must match;
    choosing one over the other would let a stable run execute on a canary Pod
    (or vice versa) while retaining false provenance.
    """

    values = run_input or {}
    explicit = _explicit_harness_identity(values)
    if require_explicit and explicit is None:
        raise ValueError("an explicit harness identity is required")

    env = os.environ if environ is None else environ
    environment = _environment_harness_identity(env)
    if explicit is not None and environment is not None and explicit != environment:
        raise ValueError("run harness identity does not match this runtime")
    identity = explicit or environment
    if identity is None:
        raise ValueError("harness identity is not configured")
    return identity


def _explicit_harness_identity(values: dict[str, Any]) -> HarnessIdentity | None:
    candidates: list[HarnessIdentity] = []

    direct = values.get("harness_version") or values.get("harnessVersion")
    if direct is not None:
        candidates.append(_identity_from_mapping(direct, "harness_version"))

    runtime_config = values.get("runtime_config") or values.get("runtimeConfig")
    if runtime_config is not None:
        if not isinstance(runtime_config, dict):
            raise ValueError("runtime_config must be an object")
        nested = runtime_config.get("harness_version") or runtime_config.get("harnessVersion")
        if nested is not None:
            candidates.append(_identity_from_mapping(nested, "runtime_config.harness_version"))

    flat_keys = {
        "version_id": values.get("harness_version_id") or values.get("harnessVersionId"),
        "channel": values.get("harness_channel") or values.get("harnessChannel"),
        "manifest_digest": values.get("harness_manifest_digest")
        or values.get("harnessManifestDigest"),
    }
    if any(value is not None for value in flat_keys.values()):
        candidates.append(_identity_from_mapping(flat_keys, "flat harness identity"))

    if not candidates:
        return None
    identity = candidates[0]
    if any(candidate != identity for candidate in candidates[1:]):
        raise ValueError("conflicting explicit harness identities")
    return identity


def _environment_harness_identity(
    environ: dict[str, str] | os._Environ[str],
) -> HarnessIdentity | None:
    values = {
        "version_id": environ.get("RATSNEST_HARNESS_VERSION_ID"),
        "channel": environ.get("RATSNEST_HARNESS_CHANNEL"),
        "manifest_digest": environ.get("RATSNEST_HARNESS_MANIFEST_DIGEST"),
    }
    if not any(value for value in values.values()):
        return None
    return _identity_from_mapping(values, "runtime environment")


def _identity_from_mapping(value: Any, source: str) -> HarnessIdentity:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be an object")
    version_id = value.get("id") or value.get("version_id") or value.get("versionId")
    channel = value.get("channel")
    manifest_digest = value.get("manifest_digest") or value.get("manifestDigest")
    if not version_id or not channel or not manifest_digest:
        raise ValueError(f"{source} must include id, channel and manifest_digest")
    return HarnessIdentity(
        version_id=str(version_id),
        channel=channel,
        manifest_digest=str(manifest_digest),
    )


class ObservationOutcome(StrEnum):
    OBSERVED = "observed"
    RESOLVED = "resolved"
    IMPROVED = "improved"
    VERIFIED = "verified"
    REJECTED = "rejected"
    ERROR = "error"
    HARD_CONFLICT = "hard_conflict"


class EvolutionObservation(EvolutionModel):
    """Privacy-safe fact derived from one AHE event."""

    schema_version: Literal["1.0"] = "1.0"
    observation_id: str = Field(pattern=_DIGEST_PATTERN)
    source_event_seq: int = Field(ge=0)
    harness_version_id: str = Field(min_length=1, max_length=120)
    harness_channel: HarnessChannel
    harness_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_reference: str = Field(min_length=1, max_length=120)
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    scope_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    project_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    failure_signature: str | None = Field(default=None, min_length=1, max_length=128)
    step: str = Field(min_length=1, max_length=120)
    check_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=80)
    recoverability: str | None = Field(default=None, max_length=80)
    strategy: str | None = Field(default=None, max_length=160)
    required_capability: str | None = Field(default=None, max_length=160)
    outcome: ObservationOutcome
    revision: int = Field(ge=0)
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime = Field(default_factory=utc_now)


class CandidateStatus(StrEnum):
    OBSERVED = "observed"
    ELIGIBLE = "eligible"
    EVALUATING = "evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    CANARY = "canary"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    STALE = "stale"


class EvolutionCandidate(EvolutionModel):
    """A cross-project problem candidate, never an executable patch."""

    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(pattern=_DIGEST_PATTERN)
    base_harness_version_id: str = Field(min_length=1, max_length=120)
    base_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    failure_signature: str = Field(min_length=1, max_length=128)
    step: str = Field(min_length=1, max_length=120)
    check_name: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=80)
    required_capability: str | None = Field(default=None, max_length=160)
    profile_references: list[str] = Field(min_length=1, max_length=16)
    observation_ids: list[str] = Field(min_length=1, max_length=10_000)
    occurrence_count: int = Field(ge=1)
    project_count: int = Field(ge=1)
    status: CandidateStatus
    risk_tier: Literal["low", "medium", "high", "prohibited"] = "low"
    change_kind: Literal[
        "unclassified",
        "prompt",
        "skill",
        "tool_description",
        "policy",
        "router",
        "parser",
        "tool_adapter",
        "recovery_strategy",
        "validator",
        "harness_code",
    ] = "unclassified"
    created_at: datetime = Field(default_factory=utc_now)


class EvalSuite(StrEnum):
    OPTIMIZATION = "optimization"
    HISTORICAL = "historical"
    HOLDOUT = "holdout"
    ADVERSARIAL = "adversarial"


class DeliveryOutcome(StrEnum):
    EXECUTION_BLOCKED = "execution_blocked"
    DELIVERED_WITH_ISSUES = "delivered_with_issues"
    RELEASE_READY = "release_ready"


class GraderId(StrEnum):
    INTENT = "intent"
    TRAJECTORY = "trajectory"
    ARTIFACT = "artifact"
    RELEASE_TRUTH = "release_truth"
    RECOVERY = "recovery"
    SECURITY = "security"
    COST = "cost"


class EvalExpectation(EvolutionModel):
    allowed_outcomes: list[DeliveryOutcome] = Field(min_length=1, max_length=3)
    expected_intent: Literal["build", "review", "continue", "irrelevant"] | None = None
    required_artifacts: list[str] = Field(default_factory=list, max_length=64)
    min_completed_steps: int = Field(default=0, ge=0, le=17)
    require_execution_complete: bool | None = None
    require_independent_review: bool = False
    require_ahe_recovery: bool = False
    expected_role_sequence: list[str] = Field(default_factory=list, max_length=16)
    max_ahe_repairs: int | None = Field(default=None, ge=0, le=100)
    max_llm_tokens: int | None = Field(default=None, ge=0, le=10_000_000)
    max_wall_clock_seconds: float | None = Field(default=None, ge=0, le=604_800)


class EvalCaseManifest(EvolutionModel):
    """One content-addressable, replayable harness evaluation case."""

    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=200)
    suite: EvalSuite
    input_ref: str = Field(min_length=1, max_length=500)
    profile_reference: str = Field(min_length=1, max_length=120)
    profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    baseline_harness_digest: str = Field(pattern=_DIGEST_PATTERN)
    sealed: bool = False
    invariants: list[str] = Field(min_length=1, max_length=32)
    grader_ids: list[GraderId] = Field(min_length=1, max_length=7)
    expectation: EvalExpectation
    fault_injection: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_sealed_suite(self) -> EvalCaseManifest:
        if self.suite in {EvalSuite.HOLDOUT, EvalSuite.ADVERSARIAL} and not self.sealed:
            raise ValueError("holdout and adversarial cases must be sealed")
        return self


class ArtifactEvidence(EvolutionModel):
    path: str = Field(min_length=1, max_length=500)
    exists: bool
    valid: bool
    sha256: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class RunEvidence(EvolutionModel):
    """Small recorded outcome consumed by deterministic graders."""

    outcome: DeliveryOutcome
    intent_mode: Literal["build", "review", "continue", "irrelevant"]
    execution_complete: bool
    completed_steps: int = Field(ge=0, le=17)
    total_steps: int = Field(default=17, ge=1, le=17)
    artifacts: list[ArtifactEvidence] = Field(default_factory=list, max_length=128)
    release_blockers: list[str] = Field(default_factory=list, max_length=128)
    independent_review: Literal["passed", "failed", "not_run"] = "not_run"
    role_sequence: list[str] = Field(default_factory=list, max_length=32)
    ahe_repair_count: int = Field(default=0, ge=0, le=1_000)
    recovered_from_fault: bool = False
    llm_tokens: int = Field(default=0, ge=0)
    wall_clock_seconds: float = Field(default=0, ge=0)
    invariant_results: dict[str, bool] = Field(default_factory=dict)


class GraderResult(EvolutionModel):
    grader_id: GraderId
    passed: bool
    score: float = Field(ge=0, le=1)
    details: list[str] = Field(default_factory=list, max_length=128)


class CaseEvaluation(EvolutionModel):
    case_id: str
    passed: bool
    grader_results: list[GraderResult]


class EvalMetrics(EvolutionModel):
    case_count: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    grader_count: int = Field(ge=0)
    passed_graders: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    total_llm_tokens: int = Field(ge=0)
    total_wall_clock_seconds: float = Field(ge=0)


class EvalReport(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    harness: HarnessIdentity
    cases: list[CaseEvaluation]
    metrics: EvalMetrics
    created_at: datetime = Field(default_factory=utc_now)


class EvalComparison(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    baseline_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    candidate_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    improved_cases: list[str] = Field(default_factory=list)
    regressed_cases: list[str] = Field(default_factory=list)
    unchanged_cases: list[str] = Field(default_factory=list)
    cost_guard_passed: bool
    candidate_passed: bool
    token_delta: int
    wall_clock_delta_seconds: float
