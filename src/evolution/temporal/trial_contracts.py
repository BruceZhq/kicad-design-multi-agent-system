"""Strict HTTP and activity contracts for one governed evolution trial."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evolution.contracts import EvolutionCandidate, HarnessManifest
from evolution.optimizer import PatchBundle, PatchPlan
from evolution.temporal.contracts import FIXED_EVAL_IDS, FIXED_SUITE_MANIFESTS

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_TRIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def evolution_workflow_id(trial_id: str) -> str:
    if not _TRIAL_ID_PATTERN.fullmatch(trial_id):
        raise ValueError("trial_id is invalid")
    return f"ratsnest-evolution-{trial_id}"


class TrialModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        populate_by_name=True,
    )


class EvaluationSuiteSpec(TrialModel):
    eval_id: str = Field(min_length=1, max_length=80)
    suite_kind: str = Field(pattern=r"^(optimization|holdout|adversarial)$")
    manifest_ref: str = Field(min_length=1, max_length=300)
    suite_digest: str = Field(pattern=_DIGEST_PATTERN)
    sealed: bool


class EvolutionTrialInput(TrialModel):
    candidate: EvolutionCandidate
    harness_manifest: HarnessManifest
    patch_plan: PatchPlan
    patch_bundle: PatchBundle
    proposal_id: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    eval_ids: list[str] = Field(min_length=5, max_length=5)
    evaluation_suites: list[EvaluationSuiteSpec] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_fixed_input(self) -> EvolutionTrialInput:
        if tuple(self.eval_ids) != FIXED_EVAL_IDS:
            raise ValueError("eval_ids must use the fixed governed evaluation set")
        observed_suites = tuple(
            (
                item.eval_id,
                item.suite_kind,
                item.manifest_ref,
                item.sealed,
            )
            for item in self.evaluation_suites
        )
        if observed_suites != FIXED_SUITE_MANIFESTS:
            raise ValueError("evaluation_suites must use the fixed governed manifests")
        if self.candidate.candidate_id != self.patch_plan.candidate_id:
            raise ValueError("candidate and patch plan do not match")
        if self.patch_plan.candidate_id != self.patch_bundle.candidate_id:
            raise ValueError("patch plan and patch bundle do not match")
        if self.patch_plan.base_commit != self.patch_bundle.base_commit:
            raise ValueError("patch plan and patch bundle commits do not match")
        if self.candidate.base_manifest_digest != self.harness_manifest.manifest_digest:
            raise ValueError("candidate and base harness manifest do not match")
        if self.patch_plan.base_commit != self.harness_manifest.source_commit:
            raise ValueError("patch plan is not pinned to the base manifest commit")
        expected_proposal_digest = canonical_digest(
            {
                "bundle": self.patch_bundle.model_dump(mode="json", by_alias=True),
                "plan": self.patch_plan.model_dump(mode="json", by_alias=True),
            }
        )
        if self.proposal_digest != expected_proposal_digest:
            raise ValueError("proposal_digest does not bind the patch plan and bundle")
        return self

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", by_alias=True))


class EvolutionTrialStartRequest(TrialModel):
    trial_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,200}$")
    candidate_id: str = Field(min_length=1, max_length=128)
    base_harness_version_id: str = Field(min_length=1, max_length=120)
    base_manifest_digest: str = Field(pattern=_DIGEST_PATTERN)
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    optimization_suite_digest: str = Field(pattern=_DIGEST_PATTERN)
    holdout_suite_digest: str = Field(pattern=_DIGEST_PATTERN)
    adversarial_suite_digest: str = Field(pattern=_DIGEST_PATTERN)
    callback_path: str = Field(min_length=1, max_length=500)
    trial_input: EvolutionTrialInput

    @model_validator(mode="after")
    def validate_envelope(self) -> EvolutionTrialStartRequest:
        expected_callback = f"/internal/v1/evolution/trials/{self.trial_id}/result"
        if self.callback_path != expected_callback:
            raise ValueError("callback_path is not the fixed control-plane path")
        if self.candidate_id != self.trial_input.candidate.candidate_id:
            raise ValueError("candidate_id does not match trial_input")
        if self.base_harness_version_id != self.trial_input.candidate.base_harness_version_id:
            raise ValueError("base_harness_version_id does not match trial_input")
        if self.base_manifest_digest != self.trial_input.harness_manifest.manifest_digest:
            raise ValueError("base_manifest_digest does not match trial_input")
        if self.input_digest != self.trial_input.digest():
            raise ValueError("input_digest does not match canonical trial_input")
        suite_digests = {
            item.suite_kind: item.suite_digest
            for item in self.trial_input.evaluation_suites
        }
        if suite_digests != {
            "optimization": self.optimization_suite_digest,
            "holdout": self.holdout_suite_digest,
            "adversarial": self.adversarial_suite_digest,
        }:
            raise ValueError("suite envelope digests do not match executable manifests")
        return self

    def suite_digest(self) -> str:
        return canonical_digest(
            {
                "adversarialSuiteDigest": self.adversarial_suite_digest,
                "evaluationSuites": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in self.trial_input.evaluation_suites
                ],
                "evalIds": list(FIXED_EVAL_IDS),
                "holdoutSuiteDigest": self.holdout_suite_digest,
                "optimizationSuiteDigest": self.optimization_suite_digest,
            }
        )


def trial_request_from_command(command: dict[str, Any]) -> EvolutionTrialStartRequest:
    """Discard workflow-private fields before strict API-contract validation."""

    return EvolutionTrialStartRequest.model_validate(
        {name: command[name] for name in EvolutionTrialStartRequest.model_fields}
    )
