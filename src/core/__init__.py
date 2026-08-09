from typing import Any

from core.settings import settings

__all__ = ["settings", "get_model", "get_model_for_plain_call"]


def __getattr__(name: str) -> Any:
    """Avoid loading every provider SDK when a worker only needs settings."""

    if name in {"get_model", "get_model_for_plain_call"}:
        from core.llm import get_model, get_model_for_plain_call

        return {
            "get_model": get_model,
            "get_model_for_plain_call": get_model_for_plain_call,
        }[name]
    raise AttributeError(name)
