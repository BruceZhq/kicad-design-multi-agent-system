"""Agent framework — the disassembly substrate.

An Agent is a named role inside a crew that owns a set of skills (functions
mounted from the vendored projects) and a strategy slice, and whose every
action is recorded as an ATDP event carrying the agent's identity. This is
what turns kicad-happy and KiCAD-MCP-Server from "two modules we call" into
members of the multi-agent system: their code becomes agent skills; identity,
telemetry, and evolvable configuration live here.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ratsnest.data_proxy import Recorder


class AgentError(RuntimeError):
    pass


class Agent:
    crew: str = "unassigned"
    name: str = "agent"

    def __init__(self, recorder: Recorder | None = None,
                 strategy_slice: dict[str, Any] | None = None,
                 iteration: int = 0):
        self.recorder = recorder
        self.strategy_slice = strategy_slice or {}
        self.iteration = iteration

    def act(self, action_name: str, fn: Callable[[], Any],
            observation: dict[str, Any] | None = None,
            action_detail: dict[str, Any] | None = None) -> Any:
        """Execute one skill invocation with ATDP capture (never silent)."""
        started = time.monotonic()
        error: str | None = None
        try:
            return fn()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            raise
        finally:
            if self.recorder is not None:
                self.recorder.emit(
                    f"{self.crew}.{self.name}", self.iteration,
                    observation=observation or {},
                    action={"skill": action_name, **(action_detail or {})},
                    outcome={"ok": error is None, "error": error,
                             "elapsed_s": round(time.monotonic() - started, 2)},
                    metadata={"agent": self.name, "crew": self.crew},
                )
