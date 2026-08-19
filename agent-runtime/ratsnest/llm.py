"""The agent brain: multi-provider LLM client with typed-contract discipline.

Providers (RATSNEST_LLM_PROVIDER):
  anthropic   Anthropic Messages API (default)
  openai      any OpenAI-compatible /chat/completions endpoint — covers
              DeepSeek, Qwen/DashScope, Moonshot/Kimi, GLM/Zhipu, vLLM, ...
  ollama      local Ollama (openai protocol, no key required)
Presets fill base URLs for: deepseek, qwen, moonshot, zhipu, ollama.

Architecture invariant: the LLM PROPOSES, tools EXECUTE, checkers VERIFY,
AHE evolves, the control plane governs. Every call is an ATDP event
(`llm.<agent>`); callers validate every completion against a Pydantic
contract and fall back to their deterministic path — unless
RATSNEST_LLM=require, in which case a missing/failed brain raises instead of
degrading (for deployments that must be LLM-driven end to end).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ratsnest.config import Config
from ratsnest.data_proxy import Recorder

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# provider presets: (protocol, default_base_url, default_model)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("anthropic", "https://api.anthropic.com", "claude-sonnet-5"),
    "openai": ("openai", "https://api.openai.com", "gpt-4o-mini"),
    "deepseek": ("openai", "https://api.deepseek.com", "deepseek-chat"),
    "qwen": ("openai",
             "https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus"),
    "moonshot": ("openai", "https://api.moonshot.cn", "moonshot-v1-8k"),
    "zhipu": ("openai", "https://open.bigmodel.cn/api/paas", "glm-4-plus"),
    "ollama": ("openai", "http://localhost:11434", "llama3.1"),
}


class BrainRequiredError(RuntimeError):
    """Raised in require mode when the brain is unavailable or fails."""


class LlmClient:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None,
                 iteration: int = 0, timeout: float | None = None):
        self.config = config or Config.load()
        self.recorder = recorder
        self.iteration = iteration
        self.timeout = (float(timeout) if timeout is not None
                        else self.config.llm_timeout_seconds)
        preset = PROVIDERS.get(self.config.llm_provider,
                               PROVIDERS["anthropic"])
        self.protocol = preset[0]
        self.base_url = (self.config.llm_base_url or preset[1]).rstrip("/")
        self.model = self.config.llm_model or preset[2]
        self.calls_used = 0
        self.total_tokens_used = 0

    @property
    def available(self) -> bool:
        if not self.config.llm_enabled or httpx is None:
            return False
        if self.config.llm_provider == "ollama":
            return True  # local, keyless
        return bool(self.config.llm_api_key)

    @property
    def required(self) -> bool:
        return self.config.llm_required

    def complete_json(self, agent: str, system: str, user: str,
                      max_tokens: int = 2000) -> dict[str, Any] | None:
        """One brain invocation -> parsed JSON dict, or None on failure
        (raises BrainRequiredError instead when RATSNEST_LLM=require)."""
        started = time.monotonic()
        error: str | None = None
        usage: dict[str, Any] = {}
        parsed: dict[str, Any] | None = None
        attempts = 0
        model = self.config.llm_model_routes.get(agent, self.model)
        requested_tokens = min(
            max(1, int(max_tokens)), self.config.llm_max_tokens_per_call)
        try:
            if not self.available:
                error = ("no usable brain "
                         f"(provider={self.config.llm_provider}, key set: "
                         f"{bool(self.config.llm_api_key)})")
            elif self.calls_used >= self.config.llm_max_calls:
                error = "LLM call budget exhausted"
            elif self.total_tokens_used >= self.config.llm_max_total_tokens:
                error = "LLM token budget exhausted"
            else:
                remaining = (self.config.llm_max_total_tokens
                             - self.total_tokens_used)
                requested_tokens = min(requested_tokens, max(1, remaining))
                text = ""
                for attempt in range(self.config.llm_retries + 1):
                    if self.calls_used >= self.config.llm_max_calls:
                        error = "LLM call budget exhausted during retry"
                        break
                    attempts += 1
                    self.calls_used += 1
                    try:
                        if self.protocol == "anthropic":
                            text, usage, error = self._call_anthropic(
                                system, user, requested_tokens, model)
                        else:
                            text, usage, error = self._call_openai(
                                system, user, requested_tokens, model)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    if error is None or not _retryable(error):
                        break
                    if attempt < self.config.llm_retries:
                        time.sleep(min(2.0, 0.25 * (2 ** attempt)))

                used = _usage_tokens(usage)
                self.total_tokens_used += used
                if self.total_tokens_used > self.config.llm_max_total_tokens:
                    error = "LLM response exceeded the total token budget"
                if error is None:
                    parsed = extract_json(text)
                    if parsed is None:
                        error = "no JSON object in completion"
            if error is None and parsed is None:
                parsed = extract_json(text)
                if parsed is None:
                    error = "no JSON object in completion"
        finally:
            if self.recorder is not None:
                self.recorder.emit(
                    f"llm.{agent}", self.iteration,
                    agent_state={"brain": "llm"},
                    action={"provider": self.config.llm_provider,
                            "model": model,
                            "system_chars": len(system),
                            "prompt_chars": len(user),
                            "requested_tokens": requested_tokens},
                    outcome={"ok": error is None, "error": error,
                             "usage": usage,
                             "attempts": attempts,
                             "calls_used": self.calls_used,
                             "total_tokens_used": self.total_tokens_used,
                             "elapsed_s": round(time.monotonic() - started, 2)},
                    metadata={"agent": agent, "crew": "brain"},
                )
        if parsed is None and self.required:
            raise BrainRequiredError(
                f"brain call failed for {agent}: {error}")
        return parsed

    # -- protocol adapters -----------------------------------------------------

    def _call_anthropic(self, system: str, user: str, max_tokens: int,
                        model: str):
        response = httpx.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.config.llm_api_key or "",
                     "authorization": f"Bearer {self.config.llm_api_key}",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens,
                  "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=self.timeout)
        if response.status_code != 200:
            return "", {}, f"http {response.status_code}: {response.text[:200]}"
        data = response.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        return text, data.get("usage", {}), None

    def _call_openai(self, system: str, user: str, max_tokens: int,
                     model: str):
        headers = {"content-type": "application/json"}
        if self.config.llm_api_key:
            headers["authorization"] = f"Bearer {self.config.llm_api_key}"
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers=headers,
            json={"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=self.timeout)
        if response.status_code != 200:
            return "", {}, f"http {response.status_code}: {response.text[:200]}"
        data = response.json()
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}).get("content", "")
                if choices else "")
        return text, data.get("usage", {}), None


def _retryable(error: str) -> bool:
    match = re.match(r"http\s+(\d+)", error.lower())
    if match:
        status = int(match.group(1))
        return status in {408, 425, 429} or status >= 500
    return any(name in error for name in (
        "Timeout", "ConnectError", "ReadError", "RemoteProtocolError"))


def _usage_tokens(usage: dict[str, Any]) -> int:
    if not isinstance(usage, dict):
        return 0
    if isinstance(usage.get("total_tokens"), (int, float)):
        return max(0, int(usage["total_tokens"]))
    fields = ("input_tokens", "output_tokens",
              "prompt_tokens", "completion_tokens")
    return sum(max(0, int(usage.get(field, 0) or 0)) for field in fields)


def extract_json(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object out of a completion (fences tolerated)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
