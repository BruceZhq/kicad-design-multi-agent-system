"""Typed seams of the AHE multi-agent system.

Architecture invariant: the LLM PROPOSES, tools EXECUTE, checkers VERIFY,
AHE evolves, the control plane governs. These protocols are the load-bearing
contracts of that sentence:

  LlmBrain       what an agent needs from its brain. RequirementAgent,
                 CircuitArchitect, SchematicDesigner, PcbDesigner,
                 RepairAgent, repair reasoning, and EvolutionAgent depend on
                 this interface rather than a provider-specific client. The
                 closed repair loop additionally advances the concrete
                 client's iteration counter for trajectory metadata.
  DesignBackend  a thing that turns a DesignSpec into a KiCad project
                 (template writer, creator crew, MCP executor).

Protocols are structural: `LlmClient` and every test FakeLlm satisfy
LlmBrain without inheriting anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ratsnest.schemas import DesignSpec, StrategyBundle


@runtime_checkable
class LlmBrain(Protocol):
    """One brain invocation: JSON contract in, parsed JSON (or None) out."""

    @property
    def available(self) -> bool: ...

    def complete_json(self, agent: str, system: str, user: str,
                      max_tokens: int = 2000) -> dict[str, Any] | None: ...


@runtime_checkable
class DesignBackend(Protocol):
    """DesignSpec -> KiCad project on disk, governed by the strategy."""

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle): ...
