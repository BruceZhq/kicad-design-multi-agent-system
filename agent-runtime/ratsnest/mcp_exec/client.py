"""Minimal MCP stdio client (JSON-RPC 2.0, newline-delimited).

This is a Data-Proxy interception point (paper [1] §4): every tools/call is
emitted as an ATDP TrajectoryEvent when a Recorder is attached — the MCP tool
stream becomes learnable trajectory data, not just execution.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ratsnest.data_proxy import Recorder

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, command: list[str], cwd: Path | None = None,
                 env: dict[str, str] | None = None,
                 recorder: Recorder | None = None,
                 iteration: int = 0,
                 timeout: float = 120.0):
        self.command = command
        self.cwd = str(cwd) if cwd else None
        self.env = env
        self.recorder = recorder
        self.iteration = iteration
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._stderr_tail: deque[str] = deque(maxlen=40)

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        import os
        full_env = {**os.environ, **(self.env or {})}
        self.proc = subprocess.Popen(
            self.command, cwd=self.cwd, env=full_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ratsnest", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.proc = None

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:  # type: ignore[union-attr]
                self._stderr_tail.append(line.rstrip())
        except Exception:
            pass

    # -- JSON-RPC ------------------------------------------------------------
    def _send(self, payload: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise McpError(
                f"MCP server not running; stderr tail: "
                f"{' | '.join(list(self._stderr_tail)[-5:])}")
        self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> Any:
        self._id += 1
        req_id = self._id
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method,
                    "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise McpError(
                    f"MCP server closed stdout during {method}; stderr tail: "
                    f"{' | '.join(list(self._stderr_tail)[-5:])}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray non-protocol output
            if msg.get("id") != req_id:
                continue  # notification or unrelated message
            if "error" in msg:
                raise McpError(f"{method} -> {msg['error']}")
            return msg.get("result")
        raise McpError(f"timeout waiting for {method} response")

    # -- MCP surface -----------------------------------------------------------
    def list_tools(self) -> list[str]:
        result = self._request("tools/list", {})
        return [t["name"] for t in result.get("tools", [])]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None,
                  ) -> dict[str, Any]:
        """tools/call with ATDP interception. Returns the parsed payload."""
        arguments = arguments or {}
        started = time.monotonic()
        error: str | None = None
        payload: dict[str, Any] = {}
        try:
            result = self._request("tools/call",
                                   {"name": name, "arguments": arguments})
            payload = _parse_tool_result(result)
            if result.get("isError") or payload.get("success") is False:
                error = str(payload.get("errorDetails")
                            or payload.get("message") or payload)[:300]
        except McpError as exc:
            error = str(exc)[:300]
            raise
        finally:
            if self.recorder is not None:
                self.recorder.emit(
                    "mcp_tool", self.iteration,
                    action={"tool": name, "arguments": arguments},
                    outcome={"ok": error is None, "error": error,
                             "elapsed_s": round(time.monotonic() - started, 2)},
                    metadata={"server": "kicad-mcp"},
                )
        if error is not None:
            raise McpError(f"tool {name} failed: {error}")
        return payload


def _parse_tool_result(result: dict) -> dict[str, Any]:
    """MCP tool results carry content blocks; the KiCad server returns JSON text."""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
                return {"value": parsed}
            except json.JSONDecodeError:
                return {"text": text}
    return {}
