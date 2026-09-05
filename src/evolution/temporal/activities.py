"""Side-effect boundaries for one governed evolution trial."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from core import settings
from evolution.contracts import EvolutionCandidate, HarnessManifest
from evolution.kubernetes_sandbox import (
    KubernetesSandboxConfigurationError,
    KubernetesSandboxExecutor,
)
from evolution.optimizer import (
    PatchBundle,
    PatchPlan,
    default_policy_path,
    load_governance_policy,
)
from evolution.sandbox import (
    CandidateEvalReport,
    governed_eval_commands,
    materialize_and_evaluate_candidate,
    patch_digest,
)
from evolution.temporal.contracts import (
    ATTEST_RESULT_ACTIVITY,
    BUILD_FAILURE_REPORT_ACTIVITY,
    DELIVER_RESULT_ACTIVITY,
    EVALUATE_CANDIDATE_ACTIVITY,
    FIXED_EVAL_IDS,
)
from evolution.temporal.proof import build_authoritative_result
from evolution.temporal.trial_contracts import canonical_json, trial_request_from_command
from service.internal_auth import create_internal_token


def _policy_path(repository_root: Path, value: Any) -> Path:
    expected = default_policy_path(repository_root)
    actual = Path(str(value or expected)).resolve()
    if actual != expected:
        raise ValueError("evolution policy must use config/harness/invariants.v1.json")
    return actual


def _trusted_local_identity(manifest: HarnessManifest) -> tuple[str, str]:
    image = os.environ.get("RATSNEST_EVOLUTION_EXECUTOR_IMAGE_DIGEST", "").strip()
    toolchain = os.environ.get("RATSNEST_EVOLUTION_TOOLCHAIN_DIGEST", "").strip()
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
        or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", toolchain)
        or image != manifest.runtime_image_digest
        or toolchain != manifest.toolchain_digest
    ):
        raise ValueError("local evaluator identity does not match the pinned manifest")
    return image, toolchain


@activity.defn(name=EVALUATE_CANDIDATE_ACTIVITY)
async def evaluate_candidate_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Materialize and evaluate a proposal; never accept a caller-authored report."""

    mode = os.environ.get("RATSNEST_EVOLUTION_SANDBOX_MODE", "").strip()
    if mode == "kubernetes_job":
        try:
            report = await KubernetesSandboxExecutor().evaluate(command)
        except KubernetesSandboxConfigurationError as exc:
            raise ApplicationError(
                f"{type(exc).__name__}: {exc}",
                type="EvolutionPolicyError",
                non_retryable=True,
            ) from exc
        return report.model_dump(mode="json", by_alias=True)
    if mode != "local_process" or os.environ.get(
        "RATSNEST_EVOLUTION_ALLOW_LOCAL_SANDBOX", ""
    ).strip().casefold() != "true":
        raise ApplicationError(
            "candidate execution requires kubernetes_job mode; local_process is dev-only",
            type="EvolutionPolicyError",
            non_retryable=True,
        )
    try:
        repository_root, sandbox_root = _configured_paths()
        policy = load_governance_policy(_policy_path(repository_root, command.get("policy_path")))
        candidate = EvolutionCandidate.model_validate(command["candidate"])
        manifest = HarnessManifest.model_validate(command["harness_manifest"])
        executor_image_digest, toolchain_digest = _trusted_local_identity(manifest)
        plan = PatchPlan.model_validate(command["patch_plan"])
        bundle = PatchBundle.model_validate(command["patch_bundle"])
        report = materialize_and_evaluate_candidate(
            candidate=candidate,
            harness_manifest=manifest,
            plan=plan,
            bundle=bundle,
            policy=policy,
            repository_root=repository_root,
            sandbox_root=sandbox_root,
            eval_ids=FIXED_EVAL_IDS,
            eval_registry=governed_eval_commands(command["evaluation_suites"]),
        )
        report = report.model_copy(update={
            "executor_image_digest": executor_image_digest,
            "toolchain_digest": toolchain_digest,
        })
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionPolicyError",
            non_retryable=True,
        ) from exc
    return report.model_dump(mode="json", by_alias=True)


@activity.defn(name=BUILD_FAILURE_REPORT_ACTIVITY)
async def build_failure_report_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Turn an exhausted evaluator failure into a bounded, rejectable report."""

    try:
        request = trial_request_from_command(command)
        trial_input = request.trial_input
        report = CandidateEvalReport(
            candidate_id=request.candidate_id,
            base_commit=trial_input.patch_plan.base_commit,
            patch_digest=patch_digest(trial_input.patch_bundle),
            verdict="error",
            worktree_created=False,
            materialized_files=[],
            command_results=[],
            error="candidate evaluation activity failed after bounded retries",
            cleanup_succeeded=False,
            executor_mode=(
                "kubernetes_job"
                if os.environ.get("RATSNEST_EVOLUTION_REQUIRED_EXECUTOR_MODE", "")
                == "kubernetes_job"
                else "local_process"
            ),
            executor_image_digest=request.trial_input.harness_manifest.runtime_image_digest,
            toolchain_digest=request.trial_input.harness_manifest.toolchain_digest,
        )
        return report.model_dump(mode="json", by_alias=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionPolicyError",
            non_retryable=True,
        ) from exc


@activity.defn(name=ATTEST_RESULT_ACTIVITY)
async def attest_result_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Create a durable proof from actual server-side evaluation output."""

    try:
        secret = _internal_secret()
        report = command["candidate_report"]
        return build_authoritative_result(command, report, secret=secret)
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionEvaluationError",
            non_retryable=True,
        ) from exc


@activity.defn(name=DELIVER_RESULT_ACTIVITY)
async def deliver_result_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Deliver the signed result only to the configured control plane."""

    return await deliver_authoritative_result(command)


async def deliver_authoritative_result(command: dict[str, Any]) -> dict[str, Any]:
    """Idempotently deliver an already-created proof without regenerating it."""

    try:
        request = trial_request_from_command(command)
        result = dict(command["authoritative_result"])
        identity = dict(command["workflow_identity"])
        tenant_id = str(identity["tenantId"])
        project_id = str(identity["projectId"])
        body = canonical_json(result)
        path = request.callback_path
        token = create_internal_token(
            secret=_internal_secret(),
            issuer="ratsnest-agent-runtime",
            audience="ratsnest-control-plane",
            subject="evolution-worker",
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=request.trial_id,
            method="POST",
            path=path,
            body=body,
        )
        base_url = os.environ.get(
            "RATSNEST_EVOLUTION_CONTROL_PLANE_URL",
            "http://control-plane:8080",
        ).rstrip("/")
        async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
            response = await client.post(
                path,
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
        if 400 <= response.status_code < 500:
            raise ApplicationError(
                f"control plane rejected evolution result with HTTP {response.status_code}",
                type="EvolutionCallbackRejected",
                non_retryable=True,
            )
        response.raise_for_status()
        return {"delivered": True, "statusCode": response.status_code}
    except ApplicationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionPolicyError",
            non_retryable=True,
        ) from exc


def _internal_secret() -> str:
    secret = settings.RATSNEST_INTERNAL_SIGNING_SECRET
    if secret is None:
        raise ValueError("internal signing secret is not configured")
    value = secret.get_secret_value()
    if len(value.encode("utf-8")) < 32:
        raise ValueError("internal signing secret must contain at least 32 bytes")
    return value


def _configured_paths() -> tuple[Path, Path]:
    repository = Path(
        os.environ.get(
            "RATSNEST_EVOLUTION_REPOSITORY_ROOT",
            str(Path(__file__).resolve().parents[3]),
        )
    ).resolve(strict=True)
    sandbox = Path(
        os.environ.get(
            "RATSNEST_EVOLUTION_SANDBOX_ROOT",
            str(repository.parent / ".ratsnest-evolution-sandbox"),
        )
    ).resolve()
    if os.name == "nt" and repository.drive.casefold() != "e:":
        raise ValueError("the Windows evolution repository must be on E:")
    if repository.drive.casefold() != sandbox.drive.casefold():
        raise ValueError("the evolution sandbox must use the repository drive")
    return repository, sandbox
