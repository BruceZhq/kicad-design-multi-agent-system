"""Dedicated low-concurrency worker for governed harness evolution trials."""

from __future__ import annotations

import asyncio
import os

from temporalio.worker import Worker

from agents.ratsnestpro.temporal.client import connect_temporal
from evolution.temporal.activities import (
    evaluate_candidate_activity,
    propose_patch_plan_activity,
)
from evolution.temporal.contracts import EVOLUTION_TASK_QUEUE
from evolution.temporal.workflow import HarnessEvolutionWorkflow


async def main() -> None:
    client = await connect_temporal()
    worker = Worker(
        client,
        task_queue=os.getenv(
            "RATSNEST_EVOLUTION_TEMPORAL_TASK_QUEUE",
            EVOLUTION_TASK_QUEUE,
        ),
        workflows=[HarnessEvolutionWorkflow],
        activities=[
            propose_patch_plan_activity,
            evaluate_candidate_activity,
        ],
        max_concurrent_activities=1,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
