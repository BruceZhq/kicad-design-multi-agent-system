from typing import Any

from core.settings import settings

__all__ = [
    "InferencePurpose",
    "settings",
    "get_model",
    "get_model_for_plain_call",
    "get_model_for_purpose",
]


def __getattr__(name: str) -> Any:
    """Avoid loading every provider SDK when a worker only needs settings."""

    if name in {"InferencePurpose", "get_model", "get_model_for_plain_call", "get_model_for_purpose"}:
        from core.llm import (
            InferencePurpose,
            get_model,
            get_model_for_plain_call,
            get_model_for_purpose,
        )

        return {
            "get_model": get_model,
            "get_model_for_plain_call": get_model_for_plain_call,
            "get_model_for_purpose": get_model_for_purpose,
            "InferencePurpose": InferencePurpose,
        }[name]
    raise AttributeError(name)
