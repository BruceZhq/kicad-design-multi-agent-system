"""Durable evolution workflow ending at a human-governed draft candidate."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from evolution.temporal.contracts import (
    ATTEST_RESULT_ACTIVITY,
    BUILD_FAILURE_REPORT_ACTIVITY,
    DELIVER_RESULT_ACTIVITY,
    EVALUATE_CANDIDATE_ACTIVITY,
    EVOLUTION_SANDBOX_TASK_QUEUE,
    EVOLUTION_WORKFLOW_NAME,
)


@workflow.defn(name=EVOLUTION_WORKFLOW_NAME)
class HarnessEvolutionWorkflow:
    """Validate and evaluate a proposal; never merge or deploy it."""

    def __init__(self) -> None:
        self._decision: str | None = None
        self._decision_reason = ""
        self._identity: dict[str, str] = {}
        self._progress: dict[str, Any] = {
            "status": "created",
            "phase": "not_started",
            "version": 0,
        }

    def _update(self, **values: Any) -> None:
        self._progress = {
            **self._progress,
            **values,
            "version": int(self._progress.get("version", 0)) + 1,
        }

    @workflow.query
    def progress(self) -> dict[str, Any]:
        return dict(self._progress)

    @workflow.query
    def identity(self) -> dict[str, str]:
        return dict(self._identity)

    @workflow.signal
    def approve(self, reason: str = "") -> None:
        self._decision = "approved"
        self._decision_reason = reason[:2_000]
        self._update(status="approval_received")

    @workflow.signal
    def reject(self, reason: str = "") -> None:
        self._decision = "rejected"
        self._decision_reason = reason[:2_000]
        self._update(status="rejection_received")

    @workflow.signal
    def cancel(self, reason: str = "") -> None:
        self._decision = "cancelled"
        self._decision_reason = reason[:2_000]
        self._update(status="cancel_requested")

    @staticmethod
    def _retry_policy() -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=15),
            maximum_attempts=2,
            non_retryable_error_types=[
                "EvolutionPolicyError",
                "EvolutionEvaluationError",
            ],
        )

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        command = dict(input)
        self._identity = dict(command["workflow_identity"])
        self._update(status="running", phase="sandbox_evaluation")
        try:
            candidate_report = await workflow.execute_activity(
                EVALUATE_CANDIDATE_ACTIVITY,
                command,
                result_type=dict,
                task_queue=EVOLUTION_SANDBOX_TASK_QUEUE,
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=self._retry_policy(),
            )
        except ActivityError:
            candidate_report = await workflow.execute_activity(
                BUILD_FAILURE_REPORT_ACTIVITY,
                command,
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=self._retry_policy(),
            )
        self._update(status="running", phase="result_attestation")
        authoritative_result = await workflow.execute_activity(
            ATTEST_RESULT_ACTIVITY,
            {**command, "candidate_report": candidate_report},
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=self._retry_policy(),
        )
        self._update(
            status="running",
            phase="callback_delivery",
            authoritative_result=authoritative_result,
        )
        callback_delivery = await workflow.execute_activity(
            DELIVER_RESULT_ACTIVITY,
            {**command, "authoritative_result": authoritative_result},
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=self._retry_policy(),
        )
        if candidate_report.get("verdict") != "passed" or not candidate_report.get(
            "cleanupSucceeded"
        ):
            self._update(status="rejected", phase="eval_gate")
            return {
                "status": "rejected",
                "reason": "candidate did not pass fixed sandbox evaluation",
                "candidate_report": candidate_report,
                "authoritative_result": authoritative_result,
                "callback_delivery": callback_delivery,
                "automatic_merge": False,
                "automatic_push": False,
                "automatic_deploy": False,
            }

        # Java owns the externally authenticated approval/rollout state machine.
        # Keep the patch marker so histories that already reached the legacy
        # signal wait can still replay with their original command sequence.
        if workflow.patched("external-approval-owned-by-control-plane-v1"):
            self._update(status="awaiting_external_approval", phase="complete")
            return {
                "status": "awaiting_external_approval",
                "reason": "evaluation proof delivered; control-plane approval required",
                "candidate_report": candidate_report,
                "authoritative_result": authoritative_result,
                "callback_delivery": callback_delivery,
                "automatic_merge": False,
                "automatic_push": False,
                "automatic_deploy": False,
            }

        if input.get("require_human_approval", True):
            self._update(status="awaiting_approval", phase="human_gate")
            await workflow.wait_condition(lambda: self._decision is not None)
            if self._decision != "approved":
                self._update(status=self._decision or "rejected", phase="human_gate")
                return {
                    "status": self._decision or "rejected",
                    "reason": self._decision_reason,
                    "candidate_report": candidate_report,
                    "authoritative_result": authoritative_result,
                    "callback_delivery": callback_delivery,
                    "automatic_merge": False,
                    "automatic_push": False,
                    "automatic_deploy": False,
                }

        self._update(status="approved_for_external_review", phase="complete")
        return {
            "status": "approved_for_external_review",
            "reason": self._decision_reason,
            "candidate_report": candidate_report,
            "authoritative_result": authoritative_result,
            "callback_delivery": callback_delivery,
            "automatic_merge": False,
            "automatic_push": False,
            "automatic_deploy": False,
        }
