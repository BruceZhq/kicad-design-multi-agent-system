"""Pure retry classification shared by bounded agent tool calls."""

from __future__ import annotations

from typing import Any

TRANSIENT_TOOL_STATUSES = frozenset(
    {"temporarily_unavailable", "timeout", "internal_error"}
)
TRANSIENT_TOOL_ERROR_TYPES = frozenset(
    {
        "connection_error",
        "network_error",
        "provider_unavailable",
        "rate_limit",
        "timeout",
        "transient_io_error",
    }
)


def is_transient_tool_result(result: dict[str, Any], *, empty: bool = False) -> bool:
    """Return true only for retryable transport/provider outcomes or required emptiness."""

    if empty:
        return True
    return (
        str(result.get("status", "")).casefold() in TRANSIENT_TOOL_STATUSES
        or str(result.get("error_type", "")).casefold()
        in TRANSIENT_TOOL_ERROR_TYPES
    )
