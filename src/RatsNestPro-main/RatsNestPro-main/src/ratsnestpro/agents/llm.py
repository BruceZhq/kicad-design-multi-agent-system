"""LLM client abstraction and the EricAI wrapper.

RatsNestPro's LLM is EricAI (`openai/gpt-oss-120b`), an OpenAI-compatible
gateway with SSO device-code auth — no API key. The ``ericai`` package is only
available on the Ericsson intranet, so it is imported lazily: the deterministic
core installs and tests fully offline, and only ``required``/``auto`` modes
that actually reach the model need the package.

Modes
-----
offline   : never call the model; deterministic path only.
auto      : use EricAI when reachable; on failure fall back to deterministic.
required  : must use EricAI; missing package / unreachable / invalid output
            fails closed (raises), never silently falls back.
"""

from __future__ import annotations

import os
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_MODEL = "openai/gpt-oss-120b"

# EricAI persists a device-code AuthenticationRecord in a file whose name is a
# hardcoded CWD-relative constant (".ericai_authrecord"). Because it is
# relative, a login done in one directory is invisible to a run started from
# another, which re-triggers the interactive device-code flow. We keep a stable
# per-user copy and seed/refresh the CWD copy around client construction so a
# single login is reused silently across runs and working directories.
_AUTH_RECORD_NAME = ".ericai_authrecord"
_CANONICAL_AUTH_RECORD = Path.home() / _AUTH_RECORD_NAME
# Domains that must bypass the corporate proxy to reach the EricAI gateway.
_NO_PROXY_DOMAINS = ".gic.ericsson.se,.sero.gic.ericsson.se,localhost,127.0.0.1"


def _ensure_no_proxy() -> None:
    """Make sure the EricAI gateway domains bypass the proxy (idempotent)."""
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        missing = [d for d in _NO_PROXY_DOMAINS.split(",") if d not in current]
        if missing:
            os.environ[var] = ",".join(filter(None, [current, *missing]))


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
    "ericai": LlmMode.REQUIRED,
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
    """Minimal chat interface. Implementations must return the assistant text."""

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str: ...


class EricAIClient:
    """Lazy wrapper around ``ericai.EricAI``. Constructed only when a live call
    is actually requested, so the dependency stays optional."""

    def __init__(self, model: str = DEFAULT_MODEL, timeout: int = 180) -> None:
        self.model = model
        self.timeout = timeout
        self.max_tokens = 8192
        self._client = None

    def _ensure(self) -> None:
        if self._client is not None:
            return
        _ensure_no_proxy()
        # Seed the CWD auth record from the persistent per-user copy so EricAI
        # can silently reuse an existing login (the record path is CWD-relative
        # and not env-overridable). MSAL's own token cache is already user-level.
        cwd_record = Path.cwd() / _AUTH_RECORD_NAME
        try:
            if _CANONICAL_AUTH_RECORD.is_file() and not cwd_record.is_file():
                shutil.copy2(_CANONICAL_AUTH_RECORD, cwd_record)
        except OSError:
            pass
        try:
            from ericai import EricAI  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - intranet-only dependency
            raise LlmError(
                "ericai is not installed. Install it from the Ericsson internal "
                "index and log in with `ericai --ericsson-test-connectivity`."
            ) from exc
        self._client = EricAI(timeout=self.timeout)
        # Persist the (possibly refreshed) record back to the per-user copy so
        # the next run — from any directory — reuses this login without a prompt.
        try:
            if cwd_record.is_file():
                shutil.copy2(cwd_record, _CANONICAL_AUTH_RECORD)
        except OSError:
            pass

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        self._ensure()
        assert self._client is not None
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=self.max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as exc:  # pragma: no cover - network dependent
            raise LlmError(f"EricAI request failed: {exc}") from exc


def resolve_client(mode: LlmMode, client: LLMClient | None) -> LLMClient | None:
    """Return a usable client for the mode, or None to signal the deterministic
    path. Raises in ``required`` mode when no client can be obtained."""
    if mode == LlmMode.OFFLINE:
        return None
    if client is not None:
        return client
    candidate = EricAIClient()
    if mode == LlmMode.REQUIRED:
        # Verify the dependency is importable now, failing closed early.
        candidate._ensure()
    return candidate
