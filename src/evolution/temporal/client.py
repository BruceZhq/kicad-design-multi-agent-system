"""Idempotent control-plane client for governed evolution trials."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from agents.ratsnestpro.temporal.client import connect_temporal
from evolution.temporal.activities import deliver_authoritative_result
from evolution.temporal.contracts import EVOLUTION_TASK_QUEUE
from evolution.temporal.trial_contracts import (
    EvolutionTrialStartRequest,
    canonical_digest,
    evolution_workflow_id,
)
from evolution.temporal.workflow import HarnessEvolutionWorkflow


class EvolutionTrialIdentityConflict(RuntimeError):
    """A trial ID already belongs to a different immutable input."""


def _identity(request: EvolutionTrialStartRequest) -> dict[str, str]:
    return {
        "trialId": request.trial_id,
        "workflowId": evolution_workflow_id(request.trial_id),
        "candidateId": request.candidate_id,
        "baseHarnessVersionId": request.base_harness_version_id,
        "inputDigest": request.input_digest,
        "baseManifestDigest": request.base_manifest_digest,
        "evalSuiteDigest": request.suite_digest(),
        "requestDigest": canonical_digest(request.model_dump(mode="json", by_alias=True)),
        "tenantId": "",
        "projectId": "",
    }


async def start_evolution_trial(
    request: EvolutionTrialStartRequest,
    *,
    tenant_id: str,
    project_id: str,
) -> dict[str, Any]:
    client = await connect_temporal()
    workflow_id = evolution_workflow_id(request.trial_id)
    expected_identity = {**_identity(request), "tenantId": tenant_id, "projectId": project_id}
    command = request.model_dump(mode="json", by_alias=False)
    command.update(request.trial_input.model_dump(mode="json", by_alias=False))
    command.update(
        {
            "workflow_identity": expected_identity,
            "require_human_approval": False,
        }
    )
    attached = False
    redelivered = False
    try:
        await client.start_workflow(
            HarnessEvolutionWorkflow.run,
            command,
            id=workflow_id,
            task_queue=os.environ.get(
                "RATSNEST_EVOLUTION_TEMPORAL_TASK_QUEUE",
                EVOLUTION_TASK_QUEUE,
            ),
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            execution_timeout=timedelta(minutes=45),
        )
    except WorkflowAlreadyStartedError as exc:
        handle = client.get_workflow_handle(workflow_id)
        actual_identity = await handle.query(HarnessEvolutionWorkflow.identity)
        if actual_identity != expected_identity:
            raise EvolutionTrialIdentityConflict(
                f"trial {request.trial_id} already exists with a different immutable input"
            ) from exc
        attached = True
        progress = await handle.query(HarnessEvolutionWorkflow.progress)
        authoritative_result = progress.get("authoritative_result")
        if isinstance(authoritative_result, dict):
            await deliver_authoritative_result(
                {**command, "authoritative_result": authoritative_result}
            )
            redelivered = True
    return {
        "trial_id": request.trial_id,
        "workflow_id": workflow_id,
        "status": "redelivered" if redelivered else ("attached" if attached else "started"),
    }


async def evolution_trial_status(
    trial_id: str,
    *,
    tenant_id: str,
    project_id: str,
) -> dict[str, Any]:
    client = await connect_temporal()
    workflow_id = evolution_workflow_id(trial_id)
    handle = client.get_workflow_handle(workflow_id)
    try:
        identity = await handle.query(HarnessEvolutionWorkflow.identity)
        if identity.get("tenantId") != tenant_id or identity.get("projectId") != project_id:
            raise EvolutionTrialIdentityConflict("trial scope does not match the internal caller")
        progress = await handle.query(HarnessEvolutionWorkflow.progress)
        description = await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise LookupError(f"evolution trial not found: {trial_id}") from exc
        raise
    execution_status = getattr(description.status, "name", str(description.status)).lower()
    result: dict[str, Any] | None = None
    if execution_status == "completed":
        value = await handle.result()
        if not isinstance(value, dict):
            raise ValueError("evolution workflow returned a non-object result")
        result = value
    return {
        "trial_id": trial_id,
        "workflow_id": workflow_id,
        "status": execution_status,
        "progress": progress,
        "result": result,
    }
