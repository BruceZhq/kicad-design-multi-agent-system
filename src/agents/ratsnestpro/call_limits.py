"""Small, provider-agnostic deadlines for non-Temporal agent calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


class AgentCallDeadlineExceeded(TimeoutError):
    """Raised when one bounded model or tool call exceeds its deadline."""


async def await_with_deadline[T](
    operation: Awaitable[T],
    *,
    timeout_seconds: float,
    operation_name: str,
) -> T:
    """Await one operation without allowing it to stall the graph indefinitely."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        return await asyncio.wait_for(operation, timeout=timeout_seconds)
    except TimeoutError as exc:
        raise AgentCallDeadlineExceeded(
            f"{operation_name} exceeded {timeout_seconds:g}s deadline"
        ) from exc
