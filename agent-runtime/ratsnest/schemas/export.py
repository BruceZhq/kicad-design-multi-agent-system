"""Export JSON Schema for every contract model into <repo>/schemas/.

The Java control plane's codegen consumes these. Run after any models.py change:

    python -m ratsnest.schemas.export
"""

from __future__ import annotations

import json
from pathlib import Path

from ratsnest.config import REPO_ROOT
from ratsnest.schemas import models

EXPORTED = [
    models.Finding,
    models.AnalyzerOutput,
    models.DesignSpec,
    models.VerificationGate,
    models.Scorecard,
    models.EvaluationResult,
    models.RepairOp,
    models.RepairHint,
    models.PatchPlan,
    models.PatchResult,
    models.TrajectoryEvent,
    models.RunConfig,
    models.IterationRecord,
    models.RunRecord,
    models.RepairMapping,
    models.SuppressionRule,
    models.StrategyBundle,
    models.ExperimentReport,
]


def export_all(out_dir: Path | None = None) -> list[Path]:
    out_dir = out_dir or (REPO_ROOT / "schemas")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for model in EXPORTED:
        path = out_dir / f"{model.__name__}.schema.json"
        schema = model.model_json_schema()
        schema["$comment"] = f"contract_version={models.CONTRACT_VERSION}"
        path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in export_all():
        print(p)
