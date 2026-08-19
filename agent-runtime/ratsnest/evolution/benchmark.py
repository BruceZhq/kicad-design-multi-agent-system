"""Benchmark corpus: seeded-defect variants with free ground truth.

Each variant = golden board + seed ops + expected post-repair values.
Because defects are injected into a known-good board, ground truth costs
nothing (design doc §4.5 L1 tier): after a repair run we can check whether
each seeded defect was actually reverted, independent of the scorecard.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ratsnest.config import REPO_ROOT, Config
from ratsnest.design_edit import Patcher
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.schemas import PatchPlan, RepairOp, RepairOpType

GOLDEN = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"


def _op_value(ref: str, value: str) -> RepairOp:
    return RepairOp(op=RepairOpType.set_value, ref=ref,
                    params={"value": value}, finding_id="seed")


def _op_prop(ref: str, name: str, value: str) -> RepairOp:
    return RepairOp(op=RepairOpType.set_property, ref=ref,
                    params={"name": name, "value": value}, finding_id="seed")


# variant -> (seed ops, expected values after a perfect repair run)
VARIANTS: dict[str, dict] = {
    # the standard three-defect board: wrong divider, wrong LED R, no MPNs
    "divider_led_mpn": {
        "ops": [
            _op_value("R1", "1.5k"),
            _op_value("R3", "10"),
            *[_op_prop(r, "MPN", "") for r in ("U1", "R1", "R2", "R3", "D1")],
        ],
        "expected_values": {"R1": "3k", "R3": "330"},
    },
    # same divider defect but on an LM1117-ADJ — strategy v0's vref_table has
    # no LM1117 entry, so the incumbent CANNOT detect/repair it. This is the
    # headroom the AHE experiment exploits (candidate adds the vref entry).
    "lm1117_divider": {
        "ops": [
            _op_prop("U1", "Value", "LM1117-ADJ"),
            _op_value("R1", "1.5k"),
        ],
        "expected_values": {"R1": "3k"},
    },
}


def materialize(variant: str, workdir: Path) -> Path:
    spec = VARIANTS[variant]
    dst = Path(workdir) / variant
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(GOLDEN, dst)
    (dst / "analysis.json").unlink(missing_ok=True)
    result = Patcher().apply(PatchPlan(run_id=f"seed-{variant}", ops=spec["ops"]), dst)
    if not result.applied:
        raise RuntimeError(f"seeding {variant} failed: {result.error}")
    return dst


def unrepaired_defects(variant: str, project_dir: Path,
                       config: Config | None = None) -> list[str]:
    """Ground truth: which seeded defects were NOT repaired back to golden."""
    out = KicadHappyAdapter(config).analyze_schematic(project_dir)
    values = {c["reference"]: c.get("value", "")
              for c in (out.model_extra or {}).get("components", [])}
    missed = []
    for ref, expected in VARIANTS[variant]["expected_values"].items():
        if values.get(ref) != expected:
            missed.append(f"{ref}={values.get(ref)!r} (expected {expected!r})")
    return missed
