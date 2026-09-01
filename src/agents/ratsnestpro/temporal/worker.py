"""Dedicated low-concurrency Temporal worker for CPU-heavy EDA Activities."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio.worker import Worker

from agents.ratsnestpro.temporal.activities import (
    compensate_pipeline_run,
    execute_pipeline_step,
    read_pipeline_checkpoint,
    read_pipeline_result,
    verify_workspace_writable,
)
from agents.ratsnestpro.temporal.client import connect_temporal
from agents.ratsnestpro.temporal.workflow import RatsNestHardwareWorkflow
from core import settings


async def main() -> None:
    verify_workspace_writable()
    client = await connect_temporal()
    worker = Worker(
        client,
        task_queue=settings.RATSNESTPRO_TEMPORAL_TASK_QUEUE,
        workflows=[RatsNestHardwareWorkflow],
        activities=[
            execute_pipeline_step,
            read_pipeline_checkpoint,
            read_pipeline_result,
            compensate_pipeline_run,
        ],
        max_concurrent_activities=settings.RATSNESTPRO_TEMPORAL_WORKER_CONCURRENCY,
        graceful_shutdown_timeout=timedelta(
            seconds=settings.RATSNESTPRO_TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS
        ),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
