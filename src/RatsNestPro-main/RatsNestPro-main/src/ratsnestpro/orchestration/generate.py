"""Deterministic one-way generation: params -> IR -> schematic -> verify.

This is the phase-1 loop with no LLM and no repair. It builds the reviewed
Circuit IR for the given (validated) parameters, materializes a real
``.kicad_sch``, runs the deterministic gates (plus kicad-cli ERC when
available), and writes an auditable run directory:

    <out>/plan.json          the immutable DesignPlan (approval boundary)
    <out>/<project>.kicad_sch  the generated schematic
    <out>/<project>.kicad_pro  a minimal project file (GUI convenience)
    <out>/gate_report.json   the VerificationReport
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ratsnestpro.domain.contracts import DesignPlan, RequirementSpec, VerificationReport
from ratsnestpro.eda.materialize import materialize_design
from ratsnestpro.families import Atmega328Params, build_ir, build_plan, expectations_for
from ratsnestpro.verification import verify_design

_MINIMAL_PRO = {
    "board": {"design_settings": {}},
    "meta": {"filename": "", "version": 1},
    "sheets": [],
}


@dataclass
class GenerationResult:
    out_dir: Path
    plan_path: Path
    schematic_path: Path
    report_path: Path
    report: VerificationReport
    blocked: bool

    @property
    def summary(self) -> str:
        status = "BLOCKED" if self.blocked else "OK (deterministic gates passed)"
        passed = sum(1 for g in self.report.gates if g.passed)
        return f"{status} - {passed}/{len(self.report.gates)} gates passed"


def build_design_plan(
    requirement_text: str,
    params: Atmega328Params,
    project_name: str = "atmega328_dev_board",
) -> DesignPlan:
    ir = build_ir(params)
    board = build_plan(params)
    return DesignPlan(
        requirement=RequirementSpec(raw_text=requirement_text, project_name=project_name),
        circuit=ir,
        board=board,
        params=params.model_dump(),
    )


def generate_design(
    requirement_text: str,
    params: Atmega328Params | None = None,
    out_dir: str | Path = "runs/design",
    project_name: str = "atmega328_dev_board",
    run_erc: bool = True,
    explicit_cli: str | None = None,
) -> GenerationResult:
    params = params or Atmega328Params()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    plan = build_design_plan(requirement_text, params, project_name)

    # Materialize the schematic.
    doc = materialize_design(plan.circuit, plan.board, supply_net="3V3")
    sch_path = out / f"{project_name}.kicad_sch"
    doc.save(sch_path)

    # Minimal project file so the schematic opens as a project in KiCad.
    pro_path = out / f"{project_name}.kicad_pro"
    pro = dict(_MINIMAL_PRO)
    pro["meta"] = {"filename": f"{project_name}.kicad_pro", "version": 1}
    pro_path.write_text(json.dumps(pro, indent=2), encoding="utf-8")

    # Verify (ERC only if requested).
    expectations = expectations_for(params)
    report = verify_design(
        plan.circuit,
        expectations,
        sch_path=str(sch_path) if run_erc else None,
        explicit_cli=explicit_cli,
    )

    # Write auditable artifacts.
    plan_path = out / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    report_path = out / "gate_report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    return GenerationResult(
        out_dir=out,
        plan_path=plan_path,
        schematic_path=sch_path,
        report_path=report_path,
        report=report,
        blocked=report.blocked,
    )
