"""Run persistence: runs/<run_id>/ with run.json + trajectory.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

from ratsnest.config import Config
from ratsnest.schemas import RunRecord


class RunStore:
    def __init__(self, runs_dir: Path | None = None):
        self.runs_dir = Path(runs_dir or Config.load().runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        d = self.runs_dir / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, record: RunRecord) -> Path:
        path = self.run_dir(record.run_id) / "run.json"
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return path

    def load(self, run_id: str) -> RunRecord:
        raw = (self.runs_dir / run_id / "run.json").read_text(encoding="utf-8")
        return RunRecord.model_validate(json.loads(raw))

    def list_runs(self) -> list[str]:
        return sorted(p.name for p in self.runs_dir.iterdir()
                      if (p / "run.json").exists())
