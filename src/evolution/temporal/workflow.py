"""Durable evolution workflow ending at a human-governed draft candidate."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from evolution.temporal.contracts import (
    EVALUATE_CANDIDATE_ACTIVITY,
    EVOLUTION_WORKFLOW_NAME,
    PROPOSE_PATCH_ACTIVITY,
)


@workflow.defn(name=EVOLUTION_WORKFLOW_NAME)
class HarnessEvolutionWorkflow:
    """Validate and evaluate a proposal; never merge or deploy it."""

    def __init__(self) -> None:
        self._decision: str | None = None
        self._decision_reason = ""
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
        if not command.get("patch_plan") or not command.get("patch_bundle"):
            self._update(status="running", phase="patch_plan_generation")
            proposal = await workflow.execute_activity(
                PROPOSE_PATCH_ACTIVITY,
                command,
                result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=self._retry_policy(),
            )
            command["patch_plan"] = proposal["plan"]
            command["patch_bundle"] = proposal["bundle"]
        self._update(status="running", phase="sandbox_evaluation")
        candidate_report = await workflow.execute_activity(
            EVALUATE_CANDIDATE_ACTIVITY,
            command,
            result_type=dict,
            start_to_close_timeout=timedelta(minutes=10),
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
                    "automatic_merge": False,
                    "automatic_push": False,
                    "automatic_deploy": False,
                }

        self._update(status="approved_for_external_review", phase="complete")
        return {
            "status": "approved_for_external_review",
            "reason": self._decision_reason,
            "candidate_report": candidate_report,
            "automatic_merge": False,
            "automatic_push": False,
            "automatic_deploy": False,
        }
