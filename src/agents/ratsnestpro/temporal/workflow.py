"""Durable Hardware Engineer workflow; orchestration only, no EDA imports."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

from agents.ratsnestpro.temporal.contracts import (
    CANONICAL_STEPS,
    COMPENSATE_ACTIVITY,
    EXECUTE_STEP_ACTIVITY,
    READ_RESULT_ACTIVITY,
    ROUTING_STEPS,
    WORKFLOW_NAME,
    hardware_workflow_identity,
)


@workflow.defn(name=WORKFLOW_NAME)
class RatsNestHardwareWorkflow:
    """Advance the existing checkpointed pipeline one canonical step at a time."""

    def __init__(self) -> None:
        self._paused = False
        self._cancel_requested = False
        self._current_activity: asyncio.Task[Any] | None = None
        self._identity: dict[str, Any] = {"schema_version": 1, "digest": ""}
        self._progress: dict[str, Any] = {
            "version": 0,
            "status": "created",
            "phase": "not_started",
            "completed_steps": 0,
            "total_steps": len(CANONICAL_STEPS),
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
    def identity(self) -> dict[str, Any]:
        """Return only the canonical input digest used for safe reattachment."""

        return dict(self._identity)

    @workflow.signal
    def request_pause(self) -> None:
        self._paused = True
        self._update(status="paused")

    @workflow.signal
    def request_resume(self) -> None:
        self._paused = False
        self._update(status="running")

    @workflow.signal
    def request_cancel(self) -> None:
        self._cancel_requested = True
        self._paused = False
        self._update(status="cancel_requested")
        if self._current_activity is not None and not self._current_activity.done():
            self._current_activity.cancel()

    def _retry_policy(self, attempts: int) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=max(1, attempts),
            non_retryable_error_types=["PermanentPipelineError"],
        )

    async def _compensate(
        self,
        input: dict[str, Any],
        *,
        failed_step: str,
        completed_steps: int,
        run_directory: str,
        reason: str,
    ) -> dict[str, Any]:
        command = {
            "workflow_id": workflow.info().workflow_id,
            "run_name": str(input.get("run_name", "design")),
            "failed_step": failed_step,
            "completed_steps": completed_steps,
            "run_directory": run_directory,
            "reason": reason,
        }
        try:
            result = await asyncio.shield(
                workflow.execute_activity(
                    COMPENSATE_ACTIVITY,
                    command,
                    result_type=dict,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            )
            return dict(result)
        except Exception as exc:  # noqa: BLE001 - preserve the root failure
            return {
                "status": "compensation_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def _final_result(
        self,
        input: dict[str, Any],
        summary: dict[str, Any],
        *,
        terminal_status: str,
        compensation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result_path = str(summary.get("pipeline_result_path", ""))
        if result_path:
            payload = await workflow.execute_activity(
                READ_RESULT_ACTIVITY,
                {"pipeline_result_path": result_path},
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=self._retry_policy(2),
            )
        else:
            error = str(summary.get("error", "pipeline stopped without a result file"))
            payload = {
                "status": "error",
                "outcome": "execution_blocked",
                "completed_steps": int(summary.get("completed_steps", 0) or 0),
                "total_steps": len(CANONICAL_STEPS),
                "execution_complete": False,
                "release_ready": False,
                "error": error,
                "error_type": str(summary.get("error_type", "")),
                "release_blockers": [error],
                "run_directory": str(summary.get("run_directory", "")),
                "artifacts": list(summary.get("artifacts", [])),
            }
        payload["temporal"] = {
            "workflow_id": workflow.info().workflow_id,
            "status": terminal_status,
            "last_step": str(summary.get("expected_step", "")),
            "activity_attempt": int(summary.get("activity_attempt", 0) or 0),
        }
        if compensation is not None:
            payload["temporal"]["compensation"] = compensation
        return payload

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        self._identity = hardware_workflow_identity(input)
        retry_attempts = max(1, int(input.get("retry_attempts", 3)))
        ordinary_timeout = max(1, int(input.get("step_timeout_seconds", 600)))
        routing_timeout = max(ordinary_timeout, int(input.get("routing_timeout_seconds", 1800)))
        heartbeat = max(1, int(input.get("heartbeat_seconds", 15)))
        workflow_timeout = max(
            60, int(input.get("workflow_timeout_seconds", 7_200))
        )
        deadline = workflow.now() + timedelta(seconds=workflow_timeout)
        manifest_path = ""
        last_summary: dict[str, Any] = {
            "completed_steps": 0,
            "run_directory": "",
            "expected_step": "",
        }
        current_step = "not_started"
        self._update(status="running", phase=current_step)

        try:
            for index, step in enumerate(CANONICAL_STEPS, start=1):
                if self._paused:
                    await workflow.wait_condition(
                        lambda: not self._paused or self._cancel_requested
                    )
                if self._cancel_requested:
                    compensation = await self._compensate(
                        input,
                        failed_step=step,
                        completed_steps=int(last_summary.get("completed_steps", 0) or 0),
                        run_directory=str(last_summary.get("run_directory", "")),
                        reason="cancel requested through workflow signal",
                    )
                    self._update(status="cancelled", phase=step)
                    return await self._final_result(
                        input,
                        last_summary,
                        terminal_status="cancelled",
                        compensation=compensation,
                    )

                current_step = step
                remaining = int((deadline - workflow.now()).total_seconds())
                if remaining <= 60:
                    last_summary = {
                        **last_summary,
                        "expected_step": step,
                        "error": "Hardware workflow reached its bounded execution deadline",
                        "error_type": "workflow_timeout",
                    }
                    compensation = await self._compensate(
                        input,
                        failed_step=step,
                        completed_steps=int(last_summary.get("completed_steps", 0) or 0),
                        run_directory=str(last_summary.get("run_directory", "")),
                        reason="bounded workflow deadline reached",
                    )
                    self._update(status="timed_out", phase=step)
                    return await self._final_result(
                        input,
                        last_summary,
                        terminal_status="timed_out",
                        compensation=compensation,
                    )
                configured_timeout = (
                    routing_timeout if step in ROUTING_STEPS else ordinary_timeout
                )
                timeout = max(1, min(configured_timeout, remaining - 60))
                schedule_budget = max(
                    timeout + 30,
                    min(
                        (timeout + 30) * retry_attempts + 120,
                        remaining - 30,
                    ),
                )
                command: dict[str, Any] = {
                    "step": step,
                    "local_timeout_seconds": timeout,
                    "heartbeat_seconds": heartbeat,
                    "workflow_id": workflow.info().workflow_id,
                    "requirement_hash": str(input["requirement_hash"]),
                    "tenant_scope": str(input.get("tenant_scope", "")),
                    "project_scope": str(input.get("project_scope", "")),
                    "run_scope": str(input.get("run_scope", "")),
                    "harness_version_id": str(
                        input.get("harness_version_id", "")
                    ),
                    "harness_manifest_digest": str(
                        input.get("harness_manifest_digest", "")
                    ),
                    "governance_scope_token": str(
                        input.get("governance_scope_token", "")
                    ),
                }
                if manifest_path:
                    command["manifest_path"] = manifest_path
                else:
                    command.update(
                        {
                            "requirement": str(input["requirement"]),
                            "run_name": str(input["run_name"]),
                            "display_run_name": str(
                                input.get("display_run_name", input["run_name"])
                            ),
                            "execution_scope": str(input.get("execution_scope", "legacy")),
                            "project_name": str(input["project_name"]),
                            "llm_mode": str(input.get("llm_mode", "required")),
                            "model_name": input.get("model_name"),
                            "model_type": input.get("model_type"),
                            "ahe_budget": input.get("ahe_budget", {}),
                        }
                    )
                self._update(status="running", phase=step, detail=f"step {index}/17")
                self._current_activity = workflow.start_activity(
                    EXECUTE_STEP_ACTIVITY,
                    command,
                    result_type=dict,
                    start_to_close_timeout=timedelta(seconds=timeout + 30),
                    schedule_to_close_timeout=timedelta(
                        seconds=schedule_budget
                    ),
                    heartbeat_timeout=timedelta(seconds=min(heartbeat * 2, timeout)),
                    retry_policy=self._retry_policy(retry_attempts),
                    cancellation_type=(
                        ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                    ),
                )
                try:
                    summary = await self._current_activity
                finally:
                    self._current_activity = None
                last_summary = summary
                manifest_path = str(summary.get("manifest_path", manifest_path))
                completed = int(summary.get("completed_steps", 0) or 0)
                self._update(
                    status="checkpointed",
                    phase=step,
                    completed_steps=completed,
                    activity_attempt=int(summary.get("activity_attempt", 0) or 0),
                )
                if self._cancel_requested:
                    compensation = await self._compensate(
                        input,
                        failed_step=step,
                        completed_steps=completed,
                        run_directory=str(summary.get("run_directory", "")),
                        reason="cancel requested after checkpoint completion",
                    )
                    self._update(status="cancelled", phase=step)
                    return await self._final_result(
                        input,
                        summary,
                        terminal_status="cancelled",
                        compensation=compensation,
                    )
                if (
                    summary.get("status") == "error"
                    or summary.get("execution_blocked") is True
                    or not summary.get("target_reached")
                ):
                    compensation = await self._compensate(
                        input,
                        failed_step=step,
                        completed_steps=completed,
                        run_directory=str(summary.get("run_directory", "")),
                        reason="pipeline returned an execution stop",
                    )
                    self._update(status="stopped", phase=step)
                    return await self._final_result(
                        input,
                        summary,
                        terminal_status="stopped",
                        compensation=compensation,
                    )

            self._update(
                status="completed",
                phase=current_step,
                completed_steps=len(CANONICAL_STEPS),
            )
            return await self._final_result(input, last_summary, terminal_status="completed")
        except ActivityError as exc:
            if self._cancel_requested:
                compensation = await self._compensate(
                    input,
                    failed_step=current_step,
                    completed_steps=int(last_summary.get("completed_steps", 0) or 0),
                    run_directory=str(last_summary.get("run_directory", "")),
                    reason="cancel signal interrupted the current Activity",
                )
                self._update(status="cancelled", phase=current_step)
                return await self._final_result(
                    input,
                    last_summary,
                    terminal_status="cancelled",
                    compensation=compensation,
                )
            compensation = await self._compensate(
                input,
                failed_step=current_step,
                completed_steps=int(last_summary.get("completed_steps", 0) or 0),
                run_directory=str(last_summary.get("run_directory", "")),
                reason=f"ActivityError: {exc}",
            )
            self._update(status="failed", phase=current_step, detail="activity retries exhausted")
            return {
                "status": "error",
                "outcome": "execution_blocked",
                "completed_steps": int(last_summary.get("completed_steps", 0) or 0),
                "total_steps": len(CANONICAL_STEPS),
                "execution_complete": False,
                "release_ready": False,
                "error": f"Temporal Activity failed after bounded retries: {exc}",
                "release_blockers": ["Temporal Activity failed after bounded retries"],
                "run_directory": str(last_summary.get("run_directory", "")),
                "artifacts": list(last_summary.get("artifacts", [])),
                "temporal": {
                    "workflow_id": workflow.info().workflow_id,
                    "status": "failed",
                    "last_step": current_step,
                    "compensation": compensation,
                },
            }
        except asyncio.CancelledError:
            compensation = await self._compensate(
                input,
                failed_step=current_step,
                completed_steps=int(last_summary.get("completed_steps", 0) or 0),
                run_directory=str(last_summary.get("run_directory", "")),
                reason=(
                    "cancel signal interrupted the current Activity"
                    if self._cancel_requested
                    else "Temporal workflow cancellation"
                ),
            )
            if self._cancel_requested:
                self._update(status="cancelled", phase=current_step)
                return await self._final_result(
                    input,
                    last_summary,
                    terminal_status="cancelled",
                    compensation=compensation,
                )
            self._update(
                status="cancelled",
                phase=current_step,
                compensation_status=str(compensation.get("status", "unknown")),
            )
            raise
