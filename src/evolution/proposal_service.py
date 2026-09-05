"""Bounded proposal generation for an eligible governed-evolution candidate."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from core import settings
from evolution.contracts import EvolutionCandidate, EvolutionModel, HarnessManifest
from evolution.optimizer import (
    GovernancePolicy,
    OptimizerRequest,
    PatchProposal,
    PublicEvalSummary,
    propose_patch_proposal,
    validate_optimizer_context_path,
)
from evolution.temporal.trial_contracts import canonical_digest

_MAX_CONTEXT_FILE_BYTES = 64 * 1024
_MAX_CONTEXT_BYTES = 80_000
_POLICY_PATH = "config/harness/invariants.v1.json"


class EvolutionProposalRequest(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    proposal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: EvolutionCandidate
    harness_manifest: HarnessManifest
    public_eval_summary: PublicEvalSummary = Field(default_factory=PublicEvalSummary)
    repository_context_paths: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_identity(self) -> EvolutionProposalRequest:
        if self.candidate.status != "eligible":
            raise ValueError("only an eligible candidate can receive an optimizer proposal")
        if self.candidate.base_manifest_digest != self.harness_manifest.manifest_digest:
            raise ValueError("candidate and harness manifest do not match")
        if self.harness_manifest.dirty:
            raise ValueError("optimizer proposals require a clean pinned base")
        if self.harness_manifest.calculated_manifest_digest() != self.harness_manifest.manifest_digest:
            raise ValueError("base harness manifest digest is invalid")
        if len(self.repository_context_paths) != len(set(self.repository_context_paths)):
            raise ValueError("repository context paths must be unique")
        return self


class EvolutionProposalResponse(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    proposal_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: PatchProposal


async def generate_proposal(request: EvolutionProposalRequest) -> EvolutionProposalResponse:
    """Read only approved files from the pinned commit, then ask the optimizer."""

    repository = _repository_root()
    policy = _policy_at_commit(repository, request.harness_manifest)
    context = _pinned_context(
        repository,
        request.harness_manifest.source_commit,
        request.repository_context_paths,
        policy,
    )
    proposal = await propose_patch_proposal(
        OptimizerRequest(
            candidate=request.candidate,
            harness_manifest=request.harness_manifest,
            public_eval_summary=request.public_eval_summary,
            repository_context=context,
        ),
        policy=policy,
        model_name=settings.DEFAULT_MODEL,
    )
    proposal_value = proposal.model_dump(mode="json", by_alias=True)
    return EvolutionProposalResponse(
        proposal_id=request.proposal_id,
        candidate_id=request.candidate.candidate_id,
        base_manifest_digest=request.harness_manifest.manifest_digest,
        proposal_digest=canonical_digest(proposal_value),
        proposal=proposal,
    )


def _repository_root() -> Path:
    return Path(
        os.environ.get(
            "RATSNEST_EVOLUTION_REPOSITORY_ROOT",
            str(Path(__file__).resolve().parents[2]),
        )
    ).resolve(strict=True)


def _policy_at_commit(repository: Path, manifest: HarnessManifest) -> GovernancePolicy:
    raw = _git_object(repository, manifest.source_commit, _POLICY_PATH, _MAX_CONTEXT_FILE_BYTES)
    if hashlib.sha256(raw).hexdigest() != manifest.policy_digest:
        raise ValueError("pinned governance policy digest does not match the manifest")
    return GovernancePolicy.model_validate_json(raw)


def _pinned_context(
    repository: Path,
    commit: str,
    paths: list[str],
    policy: GovernancePolicy,
) -> dict[str, str]:
    context: dict[str, str] = {}
    total = 0
    for value in paths:
        path = validate_optimizer_context_path(value, policy)
        raw = _git_object(repository, commit, path, _MAX_CONTEXT_FILE_BYTES)
        total += len(raw)
        if total > _MAX_CONTEXT_BYTES:
            raise ValueError("optimizer repository context exceeds 80,000 bytes")
        context[path] = raw.decode("utf-8", errors="strict")
    return context


def _git_object(repository: Path, commit: str, path: str, limit: int) -> bytes:
    identity = f"{commit}:{path}"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in {"comspec", "path", "pathext", "systemroot", "windir"}
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    size = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-s", identity],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        env=environment,
    ).stdout.decode("ascii", errors="strict").strip()
    if not size.isdigit() or int(size) > limit:
        raise ValueError(f"optimizer context file exceeds the size limit: {path}")
    return subprocess.run(
        ["git", "-C", str(repository), "show", identity],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        env=environment,
    ).stdout
