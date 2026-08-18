"""Low-risk patch planning under the repository's immutable governance policy."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from evolution.contracts import EvolutionCandidate, EvolutionModel, HarnessManifest

_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_MAX_PATCH_FILE_BYTES = 64 * 1024
_MAX_PATCH_BUNDLE_BYTES = 256 * 1024
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_SEALED_PATCH_PATHS = {
    "tests/evolution/conftest.py",
    "tests/evolution/test_evolution_core.py",
    "tests/evolution/test_sandbox.py",
}


class Invariant(EvolutionModel):
    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2_000)
    mutable_by_evolution: bool


class ChangePolicy(EvolutionModel):
    allowed_low_risk_globs: list[str] = Field(min_length=1, max_length=128)
    denied_globs: list[str] = Field(min_length=1, max_length=128)
    max_changed_files: int = Field(ge=1, le=100)
    max_added_lines: int = Field(ge=1, le=10_000)
    allow_automatic_merge: bool
    allow_automatic_production_promotion: bool


class GovernancePolicy(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=32)
    invariants: list[Invariant] = Field(min_length=1, max_length=128)
    change_policy: ChangePolicy


class PatchChange(EvolutionModel):
    operation: Literal["create", "modify"]
    path: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=2_000)
    estimated_added_lines: int = Field(default=0, ge=0, le=10_000)


class PatchPlan(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(min_length=1, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    summary: str = Field(min_length=1, max_length=4_000)
    changes: list[PatchChange] = Field(min_length=1, max_length=100)
    preserved_invariants: list[str] = Field(min_length=1, max_length=128)
    public_eval_case_ids: list[str] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_paths(self) -> PatchPlan:
        paths = [item.path for item in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("a patch plan cannot repeat a path")
        return self


class CandidateFilePatch(EvolutionModel):
    """One complete UTF-8 file; executable patches and partial diffs are forbidden."""

    operation: Literal["create", "replace"]
    path: str = Field(min_length=1, max_length=500)
    expected_old_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=_MAX_PATCH_FILE_BYTES)
    encoding: Literal["utf-8"] = "utf-8"

    @model_validator(mode="after")
    def validate_file_contract(self) -> CandidateFilePatch:
        normalized = _normalized_relative_path(self.path)
        if normalized != self.path:
            raise ValueError("patch path must already be normalized")
        if "\x00" in self.content:
            raise ValueError("patch content cannot contain NUL")
        encoded = self.content.encode("utf-8", errors="strict")
        if len(encoded) > _MAX_PATCH_FILE_BYTES:
            raise ValueError("patch file exceeds the UTF-8 byte limit")
        if hashlib.sha256(encoded).hexdigest() != self.content_sha256:
            raise ValueError("contentSha256 does not match UTF-8 content")
        if self.operation == "create" and self.expected_old_sha256 is not None:
            raise ValueError("create must not declare expectedOldSha256")
        if self.operation == "replace" and self.expected_old_sha256 is None:
            raise ValueError("replace requires expectedOldSha256")
        return self


class PatchBundle(EvolutionModel):
    """Materializable proposal tied to one candidate and one pinned commit."""

    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(min_length=1, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    files: list[CandidateFilePatch] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_bundle(self) -> PatchBundle:
        paths = [item.path.casefold() for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("a patch bundle cannot repeat a path")
        if sum(len(item.content.encode("utf-8")) for item in self.files) > _MAX_PATCH_BUNDLE_BYTES:
            raise ValueError("patch bundle exceeds the UTF-8 byte limit")
        return self


class PatchProposal(EvolutionModel):
    plan: PatchPlan
    bundle: PatchBundle

    @model_validator(mode="after")
    def matching_plan_and_bundle(self) -> PatchProposal:
        if self.plan.candidate_id != self.bundle.candidate_id:
            raise ValueError("proposal candidate identities do not match")
        if self.plan.base_commit != self.bundle.base_commit:
            raise ValueError("proposal base commits do not match")
        plan_files = {(item.path, "replace" if item.operation == "modify" else "create") for item in self.plan.changes}
        bundle_files = {(item.path, item.operation) for item in self.bundle.files}
        if plan_files != bundle_files:
            raise ValueError("patch bundle files do not exactly match the approved plan")
        return self


class WorktreePlan(EvolutionModel):
    repository: str
    directory: str
    branch: Literal[None] = None
    create_command: list[str]
    automatic_merge: Literal[False] = False
    automatic_push: Literal[False] = False
    automatic_deploy: Literal[False] = False


class PublicCaseResult(EvolutionModel):
    case_id: str = Field(min_length=1, max_length=120)
    passed: bool
    failed_graders: list[str] = Field(default_factory=list, max_length=7)


class PublicEvalSummary(EvolutionModel):
    """Aggregate-only input: deliberately has no inputRef or case content."""

    case_results: list[PublicCaseResult] = Field(default_factory=list, max_length=256)
    improvement_count: int = Field(default=0, ge=0)
    regression_count: int = Field(default=0, ge=0)


class OptimizerRequest(EvolutionModel):
    candidate: EvolutionCandidate
    harness_manifest: HarnessManifest
    public_eval_summary: PublicEvalSummary
    repository_context: dict[str, str] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def bounded_context(self) -> OptimizerRequest:
        size = sum(len(key) + len(value) for key, value in self.repository_context.items())
        if size > 80_000:
            raise ValueError("optimizer repository context exceeds 80,000 characters")
        return self


def default_policy_path(repository_root: Path) -> Path:
    return repository_root.resolve() / "config" / "harness" / "invariants.v1.json"


def load_governance_policy(path: Path) -> GovernancePolicy:
    return GovernancePolicy.model_validate_json(path.read_text(encoding="utf-8"))


def _normalized_relative_path(value: str) -> str:
    candidate = value.replace("\\", "/").strip()
    if (
        not candidate
        or candidate.startswith("/")
        or ":" in candidate
        or "\x00" in candidate
        or re.match(r"^[A-Za-z]:", candidate)
    ):
        raise ValueError(f"patch path must be repository-relative: {value!r}")
    parts = PurePosixPath(candidate).parts
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        for part in parts
    ):
        raise ValueError(f"patch path contains an unsafe segment: {value!r}")
    return "/".join(parts)


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    if fnmatch.fnmatchcase(path, normalized):
        return True
    # ``fnmatch`` treats ``/**/`` as requiring one subdirectory.  Governance
    # globs intentionally use it for zero-or-more directories.
    while "/**/" in normalized:
        normalized = normalized.replace("/**/", "/", 1)
        if fnmatch.fnmatchcase(path, normalized):
            return True
    return False


def validate_patch_plan(
    plan: PatchPlan,
    *,
    candidate: EvolutionCandidate,
    harness_manifest: HarnessManifest,
    policy: GovernancePolicy,
) -> PatchPlan:
    """Fail closed before a patch-producing agent receives a worktree."""

    if plan.candidate_id != candidate.candidate_id:
        raise ValueError("patch plan candidate identity mismatch")
    if candidate.base_manifest_digest != harness_manifest.manifest_digest:
        raise ValueError("candidate is not pinned to the supplied harness manifest")
    if plan.base_commit != harness_manifest.source_commit:
        raise ValueError("patch plan is not based on the pinned harness commit")
    if len(plan.changes) > policy.change_policy.max_changed_files:
        raise ValueError("patch plan exceeds maxChangedFiles")
    added_lines = sum(item.estimated_added_lines for item in plan.changes)
    if added_lines > policy.change_policy.max_added_lines:
        raise ValueError("patch plan exceeds maxAddedLines")

    immutable_ids = {item.id for item in policy.invariants if not item.mutable_by_evolution}
    if not immutable_ids.issubset(plan.preserved_invariants):
        missing = sorted(immutable_ids.difference(plan.preserved_invariants))
        raise ValueError(f"patch plan does not preserve invariants: {missing}")

    for change in plan.changes:
        path = _normalized_relative_path(change.path)
        if _is_sealed_path(path):
            raise ValueError(f"sealed evaluation path cannot be patched: {path}")
        if any(_matches(path, pattern) for pattern in policy.change_policy.denied_globs):
            raise ValueError(f"governance policy denies patch path: {path}")
        if not any(
            _matches(path, pattern) for pattern in policy.change_policy.allowed_low_risk_globs
        ):
            raise ValueError(f"path is outside the low-risk allowlist: {path}")
    if policy.change_policy.allow_automatic_merge:
        raise ValueError("v1 governance must not allow automatic merge")
    if policy.change_policy.allow_automatic_production_promotion:
        raise ValueError("v1 governance must not allow automatic production promotion")
    return plan


def validate_patch_bundle(
    bundle: PatchBundle,
    *,
    plan: PatchPlan,
    policy: GovernancePolicy,
) -> PatchBundle:
    """Apply the same path policy to the materialized, content-bearing bundle."""

    PatchProposal(plan=plan, bundle=bundle)
    if len(bundle.files) > policy.change_policy.max_changed_files:
        raise ValueError("patch bundle exceeds maxChangedFiles")
    for item in bundle.files:
        path = _normalized_relative_path(item.path)
        if _is_sealed_path(path):
            raise ValueError(f"sealed evaluation path cannot be patched: {path}")
        if any(_matches(path, pattern) for pattern in policy.change_policy.denied_globs):
            raise ValueError(f"governance policy denies patch path: {path}")
        if not any(
            _matches(path, pattern) for pattern in policy.change_policy.allowed_low_risk_globs
        ):
            raise ValueError(f"path is outside the low-risk allowlist: {path}")
    return bundle


def _is_sealed_path(path: str) -> bool:
    normalized = path.casefold()
    return (
        normalized == "evals/sealed"
        or normalized.startswith("evals/sealed/")
        or normalized in _SEALED_PATCH_PATHS
    )


def build_worktree_plan(
    plan: PatchPlan,
    *,
    repository_root: Path,
    worktree_root: Path,
) -> WorktreePlan:
    """Prepare argv for an isolated worktree without mutating Git or the filesystem."""

    repository = repository_root.resolve()
    sandbox_root = worktree_root.resolve()
    if repository.drive.casefold() != sandbox_root.drive.casefold():
        raise ValueError("evolution worktree must use the repository drive")
    if (
        sandbox_root == repository
        or sandbox_root in repository.parents
        or repository in sandbox_root.parents
    ):
        raise ValueError("evolution worktree root must be outside the repository")
    if not _COMMIT_RE.fullmatch(plan.base_commit):
        raise ValueError("invalid base commit")
    readable = re.sub(r"[^a-zA-Z0-9-]", "-", plan.candidate_id)[:12].strip("-")
    suffix = f"{readable}-{hashlib.sha256(plan.candidate_id.encode()).hexdigest()[:8]}"
    directory = (sandbox_root / suffix).resolve()
    if sandbox_root not in directory.parents:
        raise ValueError("evolution worktree path escaped its sandbox root")
    return WorktreePlan(
        repository=str(repository),
        directory=str(directory),
        create_command=[
            "git",
            "-C",
            str(repository),
            "-c",
            "core.autocrlf=false",
            "worktree",
            "add",
            "--detach",
            str(directory),
            plan.base_commit,
        ],
    )


def optimizer_prompt(request: OptimizerRequest, policy: GovernancePolicy) -> str:
    """Expose only public cases and approved paths to the patch planner."""

    safe_context: dict[str, str] = {}
    for raw_path, content in request.repository_context.items():
        path = _normalized_relative_path(raw_path)
        if _is_sealed_path(path):
            raise ValueError(f"optimizer context denied path (sealed): {path}")
        if any(_matches(path, pattern) for pattern in policy.change_policy.denied_globs):
            raise ValueError(f"optimizer context cannot include denied path: {path}")
        if not any(
            _matches(path, pattern) for pattern in policy.change_policy.allowed_low_risk_globs
        ):
            raise ValueError(f"optimizer context is outside the allowlist: {path}")
        safe_context[path] = content
    materialization_context = {
        path: {
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for path, content in safe_context.items()
    }
    payload = {
        "candidate": request.candidate.model_dump(mode="json", by_alias=True),
        "harness": request.harness_manifest.model_dump(mode="json", by_alias=True),
        "publicEvalSummary": request.public_eval_summary.model_dump(mode="json", by_alias=True),
        "repositoryContext": materialization_context,
        "allowedLowRiskGlobs": policy.change_policy.allowed_low_risk_globs,
        "immutableInvariants": [
            item.model_dump(mode="json", by_alias=True)
            for item in policy.invariants
            if not item.mutable_by_evolution
        ],
    }
    return (
        "Produce one minimal PatchProposal. Every file must be complete UTF-8 text; "
        "copy expectedOldSha256 from repositoryContext for replacements. Do not emit "
        "commands or diffs. Do not read sealed holdout cases, "
        "change release truth, delete tests, merge, deploy, or introduce "
        "board/project-specific branches. Preserve every immutable invariant.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


async def propose_patch_proposal(
    request: OptimizerRequest,
    *,
    policy: GovernancePolicy,
    model_name: Any,
) -> PatchProposal:
    """Request a strict whole-file proposal; applying it remains a separate activity."""

    from core.llm import get_model_for_plain_call

    model = get_model_for_plain_call(model_name).with_structured_output(PatchProposal)
    result = await model.ainvoke(optimizer_prompt(request, policy))
    proposal = result if isinstance(result, PatchProposal) else PatchProposal.model_validate(result)
    validate_patch_plan(
        proposal.plan,
        candidate=request.candidate,
        harness_manifest=request.harness_manifest,
        policy=policy,
    )
    validate_patch_bundle(proposal.bundle, plan=proposal.plan, policy=policy)
    return proposal
