"""Side-effect boundaries for one governed evolution trial."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from evolution.contracts import EvolutionCandidate, HarnessManifest
from evolution.optimizer import (
    OptimizerRequest,
    PatchBundle,
    PatchPlan,
    default_policy_path,
    load_governance_policy,
    propose_patch_proposal,
)
from evolution.sandbox import materialize_and_evaluate_candidate
from evolution.temporal.contracts import (
    EVALUATE_CANDIDATE_ACTIVITY,
    PROPOSE_PATCH_ACTIVITY,
)

_FIXED_EVAL_IDS = ("python-compile", "evolution-core")


@activity.defn(name=PROPOSE_PATCH_ACTIVITY)
async def propose_patch_plan_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Ask the existing model factory for a strict whole-file proposal."""

    try:
        from pydantic import TypeAdapter

        from schema.models import AllModelEnum

        repository_root, _ = _configured_paths()
        requested_root = command.get("repository_root")
        if requested_root and Path(str(requested_root)).resolve() != repository_root:
            raise ValueError("repositoryRoot does not match the configured evolution repository")
        policy = load_governance_policy(_policy_path(repository_root, command.get("policy_path")))
        request = OptimizerRequest.model_validate(command["optimizer_request"])
        model_name = TypeAdapter(AllModelEnum).validate_python(command["model_name"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionPolicyError",
            non_retryable=True,
        ) from exc
    proposal = await propose_patch_proposal(request, policy=policy, model_name=model_name)
    return proposal.model_dump(mode="json", by_alias=True)


def _policy_path(repository_root: Path, value: Any) -> Path:
    expected = default_policy_path(repository_root)
    actual = Path(str(value or expected)).resolve()
    if actual != expected:
        raise ValueError("evolution policy must use config/harness/invariants.v1.json")
    return actual


@activity.defn(name=EVALUATE_CANDIDATE_ACTIVITY)
async def evaluate_candidate_activity(command: dict[str, Any]) -> dict[str, Any]:
    """Materialize and evaluate a proposal; never accept a caller-authored report."""

    try:
        repository_root, sandbox_root = _configured_paths()
        policy = load_governance_policy(_policy_path(repository_root, command.get("policy_path")))
        candidate = EvolutionCandidate.model_validate(command["candidate"])
        manifest = HarnessManifest.model_validate(command["harness_manifest"])
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
            eval_ids=_FIXED_EVAL_IDS,
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}",
            type="EvolutionPolicyError",
            non_retryable=True,
        ) from exc
    return report.model_dump(mode="json", by_alias=True)


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
