"""Lazy public exports for the agent registry.

Importing a nested module such as ``agents.ratsnestpro.temporal.workflow`` first
executes this package. Keeping the registry eager would pull LangGraph and HTTP
clients into Temporal's deterministic workflow sandbox even though the workflow
does not use them.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.agents import (
        DEFAULT_AGENT,
        AgentGraph,
        get_agent,
        get_all_agent_info,
        load_agent,
    )

__all__ = [
    "get_agent",
    "load_agent",
    "get_all_agent_info",
    "DEFAULT_AGENT",
    "AgentGraph",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module("agents.agents"), name)
