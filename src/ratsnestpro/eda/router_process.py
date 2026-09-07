"""Bounded Freerouting subprocess observation; no CAD or release-gate changes."""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class RouterProcessResult:
    returncode: int
    stdout: str
    failure_kind: str = ""
    normalization_warnings: int = 0
    completed_passes: int = 0


def routing_command(executable: str, dsn: str, ses: str, max_passes: str) -> list[str]:
    # Width exceptions are materialized and checked by our CAD harness. The
    # router's independent fanout/necking heuristics have no per-net approval
    # or whole-chain length contract and must not silently narrow other tracks.
    return [executable, "-de", dsn, "-do", ses, "-mp", str(max_passes),
            "--router.fanout.enabled=false", "--router.automatic_neckdown=false",
            "--router.neck_width_um=0"]


def run_router(args: list[str], *, timeout: float,
               no_progress_seconds: float = 30.0,
               normalization_limit: int = 1024) -> RouterProcessResult:
    """Abort pathological normalization, not merely a slow or incomplete route.

    A repeated recursion-limit warning plus no completed pass is the trigger.
    Ordinary difficult routing retains its full wall-clock budget. Output is
    drained continuously into a bounded tail so log floods cannot exhaust RAM.
    """
    started = time.monotonic()
    observed = {"warnings": 0, "since_pass": 0, "passes": 0, "progress": started}
    tail: deque[str] = deque(maxlen=40)
    lock = threading.Lock()
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace")

    def read_output():
        assert process.stdout is not None
        for line in process.stdout:
            with lock:
                tail.append(line[-2000:])
                if "max normalization depth" in line:
                    observed["warnings"] += 1
                    observed["since_pass"] += 1
                if "Auto-router pass #" in line and "was completed" in line:
                    observed["passes"] += 1
                    observed["since_pass"] = 0
                    observed["progress"] = time.monotonic()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    failure = ""
    try:
        while process.poll() is None:
            now = time.monotonic()
            with lock:
                stalled = (observed["since_pass"] >= normalization_limit
                           and now - observed["progress"] >= no_progress_seconds)
            if stalled:
                failure = "router_normalization_livelock"
                break
            if now - started >= timeout:
                failure = "router_timeout"
                break
            time.sleep(min(0.25, max(0.01, timeout / 10)))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    with lock:
        return RouterProcessResult(process.returncode, "".join(tail), failure,
                                   observed["warnings"], observed["passes"])
