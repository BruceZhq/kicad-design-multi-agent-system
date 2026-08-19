"""Server-authored, content-bound result proof for one evolution trial."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime
from typing import Any

from evolution.sandbox import CandidateEvalReport, patch_digest
from evolution.temporal.contracts import FIXED_EVAL_IDS
from evolution.temporal.trial_contracts import (
    canonical_digest,
    canonical_json,
    evolution_workflow_id,
    trial_request_from_command,
)

_ATTESTATION_DOMAIN = b"ratsnest-evolution-result-v1\0"
_ATTESTATION_KEY_ID = "ratsnest-internal-hs256-v1"


def build_authoritative_result(
    command: dict[str, Any],
    report_value: dict[str, Any],
    *,
    secret: str,
) -> dict[str, Any]:
    """Validate actual activity output and sign the complete callback payload."""

    request = trial_request_from_command(command)
    report = CandidateEvalReport.model_validate(report_value)
    trial_input = request.trial_input
    manifest = trial_input.harness_manifest
    if manifest.calculated_manifest_digest() != manifest.manifest_digest:
        raise ValueError("base harness manifest digest is invalid")
    if report.candidate_id != request.candidate_id:
        raise ValueError("evaluation report candidate does not match the trial")
    if report.base_commit != manifest.source_commit:
        raise ValueError("evaluation report commit does not match the base manifest")
    actual_patch_digest = patch_digest(trial_input.patch_bundle)
    if report.patch_digest != actual_patch_digest:
        raise ValueError("evaluation report patch digest does not match the trial input")

    authoritative_report = report.model_dump(mode="json", by_alias=True)
    command_ids = tuple(item.eval_id for item in report.command_results)
    required_executor = os.environ.get(
        "RATSNEST_EVOLUTION_REQUIRED_EXECUTOR_MODE", ""
    ).strip()
    executor_attested = required_executor in {"local_process", "kubernetes_job"} and (
        report.executor_mode == required_executor
    )
    guardrail_passed = (
        report.verdict == "passed"
        and executor_attested
        and report.cleanup_succeeded
        and command_ids == FIXED_EVAL_IDS
        and all(item.passed for item in report.command_results)
        and not report.automatic_merge
        and not report.automatic_push
        and not report.automatic_deploy
    )
    verdict = {
        "passed": "PASSED" if guardrail_passed else "FAILED",
        "failed": "FAILED",
        "policy_rejected": "POLICY_REJECTED",
        "error": "ENVIRONMENT_ISSUE",
    }[report.verdict]
    proof_payload = {
        "trial_id": request.trial_id,
        "candidate_id": request.candidate_id,
        "candidate_digest": canonical_digest(
            trial_input.candidate.model_dump(mode="json", by_alias=True)
        ),
        "base_harness_version_id": request.base_harness_version_id,
        "base_manifest_digest": request.base_manifest_digest,
        "input_digest": request.input_digest,
        "temporal_workflow_id": evolution_workflow_id(request.trial_id),
        "optimization_suite_digest": request.optimization_suite_digest,
        "holdout_suite_digest": request.holdout_suite_digest,
        "adversarial_suite_digest": request.adversarial_suite_digest,
        "eval_suite_digest": request.suite_digest(),
        "patch_digest": actual_patch_digest,
        "report_digest": canonical_digest(authoritative_report),
        "verdict": verdict,
        "guardrail_passed": guardrail_passed,
        "authoritative_report": authoritative_report,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    canonical = canonical_json(proof_payload)
    signature = hmac.new(
        secret.encode("utf-8"),
        _ATTESTATION_DOMAIN + canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        **proof_payload,
        "attestation": {
            "algorithm": "HMAC-SHA256",
            "key_id": _ATTESTATION_KEY_ID,
            "payload_sha256": hashlib.sha256(canonical).hexdigest(),
            "signature": signature,
        },
    }
