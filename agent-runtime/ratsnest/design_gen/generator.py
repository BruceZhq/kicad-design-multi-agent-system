"""Design generator: DesignSpec -> KiCad project on disk.

All electrical values are solved from the SAME strategy assets the repair
loop uses (Vref table, E-series snapping, MPN patterns, LED Vf) — generation
and repair share one evolvable knowledge base, so an AHE promotion improves
both paths at once.
"""

from __future__ import annotations

import json
from pathlib import Path

from ratsnest.circuit_math import LDO_TOPOLOGY, GenerationError, solve_circuit
from ratsnest.config import Config
from ratsnest.design_gen.templates import build_regulator_board, rail_name
from ratsnest.schemas import DesignSpec, StrategyBundle


def generate_project(spec: DesignSpec, out_dir: Path,
                     strategy: StrategyBundle,
                     config: Config | None = None) -> Path:
    """Write <out_dir>/<project>.kicad_sch + .kicad_pro (+ designspec.json)."""
    config = config or Config.load()
    solved = solve_circuit(spec, strategy, config)
    if solved.topology != LDO_TOPOLOGY:
        raise GenerationError(
            "template backend is limited to the LDO development schematic; "
            "use the crew backend for Buck or production verification")
    values, mpns, include_led = (
        solved.values, solved.mpns, solved.include_led)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sch = build_regulator_board(
        project=spec.project_name,
        vin_rail=rail_name(spec.input_voltage),
        vout_rail=rail_name(spec.output_voltage),
        values=values, mpns=mpns, include_led=include_led,
        title=spec.requirement_text[:80] or spec.project_name,
    )
    (out_dir / f"{spec.project_name}.kicad_sch").write_text(sch, encoding="utf-8")
    (out_dir / f"{spec.project_name}.kicad_pro").write_text(
        json.dumps({"meta": {"filename": f"{spec.project_name}.kicad_pro",
                             "version": 1}}, indent=2), encoding="utf-8")
    (out_dir / "designspec.json").write_text(
        spec.model_dump_json(indent=2), encoding="utf-8")
    return out_dir


class TemplateBackend:
    """DesignBackend adapter for the deterministic template writer."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        return generate_project(spec, out_dir, strategy, self.config)
