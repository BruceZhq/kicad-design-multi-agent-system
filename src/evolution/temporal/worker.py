"""Dedicated low-concurrency worker for governed harness evolution trials."""

from __future__ import annotations

import asyncio
import os

from temporalio.worker import Worker

from agents.ratsnestpro.temporal.client import connect_temporal
from evolution.temporal.activities import (
    attest_result_activity,
    build_failure_report_activity,
    deliver_result_activity,
    evaluate_candidate_activity,
)
from evolution.temporal.contracts import EVOLUTION_SANDBOX_TASK_QUEUE, EVOLUTION_TASK_QUEUE
from evolution.temporal.workflow import HarnessEvolutionWorkflow


async def main() -> None:
    client = await connect_temporal()
    role = os.getenv("RATSNEST_EVOLUTION_WORKER_ROLE", "").strip()
    if role == "controller":
        worker = Worker(
            client,
            task_queue=os.getenv(
                "RATSNEST_EVOLUTION_TEMPORAL_TASK_QUEUE",
                EVOLUTION_TASK_QUEUE,
            ),
            workflows=[HarnessEvolutionWorkflow],
            activities=[
                build_failure_report_activity,
                attest_result_activity,
                deliver_result_activity,
            ],
            max_concurrent_activities=1,
        )
    elif role == "sandbox-coordinator":
        worker = Worker(
            client,
            task_queue=EVOLUTION_SANDBOX_TASK_QUEUE,
            workflows=[],
            activities=[evaluate_candidate_activity],
            max_concurrent_activities=1,
        )
    else:
        raise RuntimeError(
            "RATSNEST_EVOLUTION_WORKER_ROLE must be controller or sandbox-coordinator"
        )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
