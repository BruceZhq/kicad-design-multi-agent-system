"""LLM client abstraction for deterministic and OpenAI-compatible review paths.

Modes
-----
offline   : never call a model; use the deterministic path only.
auto      : use the configured endpoint when available; otherwise fall back.
required  : require a configured, reachable endpoint and fail closed on errors.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Protocol, runtime_checkable

import httpx

DEFAULT_MODEL = "gpt-4o-mini"


class LlmMode(StrEnum):
    OFFLINE = "offline"
    AUTO = "auto"
    REQUIRED = "required"


_ALIASES = {
    "off": LlmMode.OFFLINE,
    "disabled": LlmMode.OFFLINE,
    "none": LlmMode.OFFLINE,
    "auto": LlmMode.AUTO,
    "live": LlmMode.REQUIRED,
    "require": LlmMode.REQUIRED,
    "required": LlmMode.REQUIRED,
}


def parse_mode(value: str | LlmMode | None) -> LlmMode:
    if value is None:
        return LlmMode.OFFLINE
    if isinstance(value, LlmMode):
        return value
    key = value.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    try:
        return LlmMode(key)
    except ValueError:
        return LlmMode.OFFLINE


class LlmError(RuntimeError):
    """Raised when a required LLM call cannot be completed."""


@runtime_checkable
class LLMClient(Protocol):
    """Minimal chat interface. Implementations return assistant text."""

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str: ...


def _api_endpoint(base_url: str, resource: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(f"/{resource}"):
        return base
    return f"{base}/{resource}"


class OpenAICompatibleClient:
    """Small synchronous adapter for an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 180,
    ) -> None:
        self.model = model or os.getenv("COMPATIBLE_MODEL") or DEFAULT_MODEL
        self.base_url = base_url or os.getenv("COMPATIBLE_BASE_URL", "")
        self.api_key = api_key or os.getenv("COMPATIBLE_API_KEY", "")
        self.timeout = timeout
        self.max_tokens = 8192

    def validate(self) -> None:
        if not self.base_url:
            raise LlmError(
                "COMPATIBLE_BASE_URL is required for live review; "
                "use offline mode for deterministic-only review."
            )

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.validate()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            response = httpx.post(
                _api_endpoint(self.base_url, "chat/completions"),
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return str(content or "")
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(f"OpenAI-compatible request failed: {exc}") from exc


def resolve_client(mode: LlmMode, client: LLMClient | None) -> LLMClient | None:
    """Return a client for the mode, or None for the deterministic path."""
    if mode == LlmMode.OFFLINE:
        return None
    if client is not None:
        return client
    candidate = OpenAICompatibleClient()
    if mode == LlmMode.REQUIRED:
        candidate.validate()
    return candidate
