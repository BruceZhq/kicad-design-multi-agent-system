"""Agent layer (EricAI-driven). The LLM reads, judges, explains, and proposes;
deterministic code decides and executes."""

from ratsnestpro.agents.architect import Architect, ArchitectResult
from ratsnestpro.agents.coding import ALLOWED_PARAMS, Coder, apply_actions
from ratsnestpro.agents.llm import (
    DEFAULT_MODEL,
    EricAIClient,
    LLMClient,
    LlmError,
    LlmMode,
    parse_mode,
)
from ratsnestpro.agents.reviewer import Reviewer, ReviewResult, TriageItem

__all__ = [
    "ALLOWED_PARAMS",
    "DEFAULT_MODEL",
    "Architect",
    "ArchitectResult",
    "Coder",
    "EricAIClient",
    "LLMClient",
    "LlmError",
    "LlmMode",
    "ReviewResult",
    "Reviewer",
    "TriageItem",
    "apply_actions",
    "parse_mode",
]
