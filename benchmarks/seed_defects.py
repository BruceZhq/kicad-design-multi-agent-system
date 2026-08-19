"""Seed known defects into the golden demo board -> benchmarks/seeded/.

Defects (each maps to a repair the strategy knows how to plan):
  D1: R1 3k -> 1.5k    feedback divider now gives Vout=3.125V on the +5V rail
  D2: R3 330 -> 10     LED current jumps to ~300mA (way past 20mA)
  D3: strip MPNs       sourcing/datasheet coverage collapses

Ground truth is free: the golden board is the known-good original.
Usage: python seed_defects.py [variant_name]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-runtime"))

from ratsnest.config import REPO_ROOT  # noqa: E402
from ratsnest.design_edit import Patcher  # noqa: E402
from ratsnest.schemas import PatchPlan, RepairOp, RepairOpType  # noqa: E402

GOLDEN = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"
SEEDED = REPO_ROOT / "benchmarks" / "seeded"


def seed(variant: str = "demo_board_defective") -> Path:
    dst = SEEDED / variant
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(GOLDEN, dst)
    (dst / "analysis.json").unlink(missing_ok=True)

    ops = [
        RepairOp(op=RepairOpType.set_value, ref="R1",
                 params={"value": "1.5k"}, finding_id="seed:D1"),
        RepairOp(op=RepairOpType.set_value, ref="R3",
                 params={"value": "10"}, finding_id="seed:D2"),
    ]
    for ref in ("U1", "R1", "R2", "R3", "D1"):
        ops.append(RepairOp(op=RepairOpType.set_property, ref=ref,
                            params={"name": "MPN", "value": ""},
                            finding_id="seed:D3"))
    plan = PatchPlan(run_id="seeder", ops=ops)
    result = Patcher().apply(plan, dst)
    if not result.applied:
        raise SystemExit(f"seeding failed: {result.error}")
    print(f"seeded {len(ops)} defect ops -> {dst}")
    return dst


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "demo_board_defective")
