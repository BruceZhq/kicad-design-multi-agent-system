"""Agent layer (EricAI-driven). The LLM reads, judges, explains, and proposes;
deterministic code decides and executes."""

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
    "DEFAULT_MODEL",
    "EricAIClient",
    "LLMClient",
    "LlmError",
    "LlmMode",
    "ReviewResult",
    "Reviewer",
    "TriageItem",
    "parse_mode",
]
