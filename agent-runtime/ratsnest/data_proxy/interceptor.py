"""Agentic Data Proxy — capture half (paper [1] §4).

Every orchestrator node emits an ATDP TrajectoryEvent through one Recorder:
append-only JSONL on disk (the learning substrate for AHE triggers), plus an
optional best-effort HTTP sink to the Java control plane. Capture never
breaks the run: sink failures are swallowed by design.

Late-bound rewards ([1] §3): `attach_reward` rewrites an already-persisted
event's reward field while preserving the original causal record fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ratsnest.schemas import TrajectoryEvent

try:
    import httpx
except ImportError:  # httpx is optional; JSONL sink always works
    httpx = None  # type: ignore[assignment]


class Recorder:
    def __init__(self, run_dir: Path, run_id: str,
                 control_plane_url: str | None = None,
                 base_metadata: dict[str, Any] | None = None,
                 initial_step: int = 0):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.path = self.run_dir / "trajectory.jsonl"
        self.control_plane_url = control_plane_url
        # run-level provenance (strategy version etc.) stamped on every event
        self.base_metadata = base_metadata or {}
        self._step = max(0, int(initial_step))
        if self.path.is_file():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    self._step = max(
                        self._step, int(json.loads(line).get("step", 0)))
            except (OSError, ValueError, json.JSONDecodeError):
                # A damaged local mirror must not disable the remote trajectory.
                pass

    @property
    def step(self) -> int:
        return self._step

    def emit(self, node: str, iteration: int = 0,
             observation: dict[str, Any] | None = None,
             agent_state: dict[str, Any] | None = None,
             action: dict[str, Any] | None = None,
             outcome: dict[str, Any] | None = None,
             reward: float | None = None,
             metadata: dict[str, Any] | None = None) -> TrajectoryEvent:
        self._step += 1
        event = TrajectoryEvent(
            run_id=self.run_id, iteration=iteration, step=self._step, node=node,
            observation=observation or {}, agent_state=agent_state or {},
            action=action or {}, outcome=outcome or {}, reward=reward,
            metadata={**self.base_metadata, **(metadata or {})},
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        self._post(event)
        return event

    def attach_reward(self, event_id: str, reward: float) -> bool:
        """Late-bound reward: update one event in place (JSONL rewrite)."""
        if not self.path.exists():
            return False
        lines = self.path.read_text(encoding="utf-8").splitlines()
        changed = False
        for i, line in enumerate(lines):
            rec = json.loads(line)
            if rec.get("event_id") == event_id:
                rec["reward"] = reward
                lines[i] = json.dumps(rec)
                changed = True
                break
        if changed:
            self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return changed

    def _post(self, event: TrajectoryEvent) -> None:
        if not (self.control_plane_url and httpx):
            return
        try:
            import os
            headers = {}
            token = os.environ.get("RATSNEST_SERVICE_TOKEN")
            if token:  # jwt-mode control plane: service-to-service auth
                headers["X-RatsNest-Service-Token"] = token
            httpx.post(f"{self.control_plane_url.rstrip('/')}/api/atdp/events",
                       json=json.loads(event.model_dump_json()),
                       headers=headers, timeout=2.0)
        except Exception:
            pass  # capture must never break the run
