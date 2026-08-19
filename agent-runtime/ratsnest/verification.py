"""Stage 3 production verification and release-gate aggregation."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from ratsnest.catalog import load_catalog
from ratsnest.circuit_math import BUCK_TOPOLOGY, solve_circuit
from ratsnest.config import Config
from ratsnest.crews.circuit_families import build_canonical_plan
from ratsnest.crews.contracts import BoardPlan, PlannedDesign
from ratsnest.crews.design_agents import CircuitArchitect
from ratsnest.design_edit.kicad_cli import run_drc_gate, run_erc_gate
from ratsnest.evolution import StrategyRegistry
from ratsnest.kicad_env import bootstrap_kicad
from ratsnest.manufacturing import catalog_issues, read_manifest
from ratsnest.schemas import (
    DesignSpec,
    Finding,
    GateStatus,
    VerificationGate,
)
from ratsnest.spice import run_spice_gate


def _write_evidence(project_dir: Path, name: str,
                    payload: dict[str, Any]) -> str:
    directory = project_dir / "verification"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path.relative_to(project_dir))


def _checked_gate(project_dir: Path, name: str, issues: list[str],
                  summary: str, metrics: dict[str, Any] | None = None,
                  tool: str = "RatsNest deterministic checker",
                  ) -> VerificationGate:
    evidence = _write_evidence(
        project_dir, name, {"issues": issues, "metrics": metrics or {}})
    return VerificationGate(
        name=name,
        status=GateStatus.passed if not issues else GateStatus.failed,
        summary=summary if not issues else "; ".join(issues[:4]),
        tool=tool, evidence=[evidence], metrics=metrics or {})


def _load_contracts(
        project_dir: Path) -> tuple[BoardPlan, DesignSpec, PlannedDesign | None]:
    plan = BoardPlan.model_validate_json(
        (project_dir / "boardplan.json").read_text(encoding="utf-8"))
    spec = DesignSpec.model_validate_json(
        (project_dir / "designspec.json").read_text(encoding="utf-8"))
    approved = None
    approved_path = project_dir / "approved_plan.json"
    if approved_path.is_file():
        approved = PlannedDesign.model_validate_json(
            approved_path.read_text(encoding="utf-8"))
        if approved.board_plan.model_dump(mode="json") != plan.model_dump(mode="json"):
            raise ValueError("materialized BoardPlan differs from approved plan")
        if approved.design_spec != spec:
            raise ValueError("materialized DesignSpec differs from approved plan")
    return plan, spec, approved


def _catalog_gate(project_dir: Path, plan: BoardPlan,
                  spec: DesignSpec, approved: PlannedDesign | None,
                  config: Config) -> VerificationGate:
    issues = catalog_issues(plan)
    backend = approved.backend if approved is not None else None
    if backend is not None and backend != "crew":
        issues.append(
            f"backend {backend!r} is compatibility-only; production release requires crew")
    try:
        registry = StrategyRegistry(config.strategies_dir)
        if approved is None:
            _, strategy = registry.load_active()
        else:
            strategy = registry.load_exact(
                approved.strategy_name, approved.strategy_version_id)
        solved = solve_circuit(spec, strategy, config)
        canonical = build_canonical_plan(spec, solved)
        CircuitArchitect.validate_candidate(plan, canonical)
    except Exception as exc:
        issues.append(f"solver-authoritative BoardPlan validation failed: {exc}")
    boards = sorted(project_dir.glob("*.kicad_pcb"))
    if not boards:
        issues.append("materialized PCB is missing")
    try:
        from ratsnest.eda import get_state
        state = get_state(project_dir, config)
        actual = {item["ref"]: item for item in state["components"]}
        expected = {component.ref: component for component in plan.components
                    if component.on_board}
        if set(actual) != set(expected):
            issues.append(
                f"schematic refs differ from plan: missing={sorted(set(expected)-set(actual))}, "
                f"extra={sorted(set(actual)-set(expected))}")
        for ref in sorted(set(actual) & set(expected)):
            if actual[ref].get("value") != expected[ref].value:
                issues.append(f"{ref}: schematic value differs from approved catalog value")
    except Exception as exc:
        issues.append(f"schematic/catalog reconciliation failed: {exc}")

    if boards and bootstrap_kicad(config.kicad_python):
        try:
            import pcbnew
            board = pcbnew.LoadBoard(str(boards[0]))
            actual_refs = {item.GetReference() for item in board.GetFootprints()}
            expected_refs = {component.ref for component in plan.components
                             if component.on_board}
            if actual_refs != expected_refs:
                issues.append(
                    f"PCB refs differ from plan: missing={sorted(expected_refs-actual_refs)}, "
                    f"extra={sorted(actual_refs-expected_refs)}")
        except Exception as exc:
            issues.append(f"PCB/catalog reconciliation failed: {exc}")
    elif boards:
        issues.append("pcbnew is unavailable for physical catalog reconciliation")
    return _checked_gate(
        project_dir, "catalog", issues,
        "approved plan and materialized design match the trusted catalog",
        metrics={"catalog_version": load_catalog().version,
                 "planned_components": len(plan.components)})


def _bom_gate(project_dir: Path, plan: BoardPlan) -> VerificationGate:
    issues: list[str] = []
    csv_path = project_dir / "bom.csv"
    manifest_path = project_dir / "manufacturing_manifest.json"
    if not csv_path.is_file():
        issues.append("bom.csv is missing")
    if not manifest_path.is_file():
        issues.append("manufacturing_manifest.json is missing")
    manifest = None
    if not issues:
        try:
            manifest = read_manifest(project_dir)
            expected = {component.ref: component for component in plan.components
                        if component.in_bom}
            lines = {ref: line for line in manifest.bom for ref in line.refs}
            if set(lines) != set(expected):
                issues.append("manifest BOM refs differ from approved BoardPlan")
            for ref in sorted(set(lines) & set(expected)):
                line, component = lines[ref], expected[ref]
                if line.catalog_id != component.catalog_id:
                    issues.append(f"{ref}: BOM catalog id mismatch")
                if line.mpn != component.properties.get("MPN"):
                    issues.append(f"{ref}: BOM MPN mismatch")
                if line.quantity != 1:
                    issues.append(f"{ref}: unexpected BOM quantity")
            with csv_path.open(newline="", encoding="utf-8") as stream:
                csv_refs = {ref for row in csv.DictReader(stream)
                            for ref in row.get("refs", "").split(",") if ref}
            if csv_refs != set(expected):
                issues.append("bom.csv refs differ from approved BoardPlan")
        except Exception as exc:
            issues.append(f"BOM reconciliation failed: {exc}")
    return _checked_gate(
        project_dir, "bom", issues,
        "BOM and manufacturing manifest match the approved catalog",
        metrics={"line_items": len(manifest.bom) if manifest else 0})


def _thermal_gate(project_dir: Path, plan: BoardPlan,
                  spec: DesignSpec) -> VerificationGate:
    issues: list[str] = []
    limits = plan.design_limits
    metrics: dict[str, Any] = {}
    if limits is None:
        issues.append("typed thermal limits are missing")
    else:
        margin = limits.max_junction_c - limits.estimated_junction_c
        metrics.update({
            "controller_loss_w": limits.controller_loss_w,
            "estimated_controller_junction_c": limits.estimated_junction_c,
            "design_junction_limit_c": limits.max_junction_c,
            "controller_margin_c": round(margin, 3),
            "estimated_efficiency_pct": limits.estimated_efficiency_pct,
        })
        if margin < 0:
            issues.append("controller junction estimate exceeds the design limit")
        if plan.topology == BUCK_TOPOLOGY:
            catalog = load_catalog()
            diode = catalog.entry("onsemi.nrvbs540t3g")
            inductor = catalog.entry("coilcraft.mss1210h-683med")
            duty = limits.duty_cycle or 0
            diode_loss = ((1 - duty) * spec.output_current_a
                          * float(diode.ratings["forward_voltage_v"]))
            diode_junction = (spec.ambient_temperature_c + diode_loss
                              * float(diode.ratings["theta_ja_c_per_w"]))
            inductor_rise = (20.0 * (spec.output_current_a
                                     / float(inductor.ratings["irms_20c_a"])) ** 2)
            metrics.update({
                "estimated_diode_junction_c": round(diode_junction, 3),
                "estimated_inductor_temperature_c": round(
                    spec.ambient_temperature_c + inductor_rise, 3),
            })
            if diode_junction > 125.0:
                issues.append("catch diode junction estimate exceeds 125C")
            if spec.ambient_temperature_c + inductor_rise > 105.0:
                issues.append("inductor temperature estimate exceeds 105C")
    return _checked_gate(
        project_dir, "thermal", issues,
        "derated controller and power-stage temperatures are within limits",
        metrics=metrics)


def _emc_gate(project_dir: Path, plan: BoardPlan,
              config: Config) -> VerificationGate:
    issues: list[str] = []
    metrics: dict[str, Any] = {}
    boards = sorted(project_dir.glob("*.kicad_pcb"))
    if not boards:
        return _checked_gate(
            project_dir, "emc", ["PCB is missing"],
            "basic layout/EMC checks passed")
    if not bootstrap_kicad(config.kicad_python):
        return VerificationGate(
            name="emc", status=GateStatus.unavailable,
            summary="pcbnew is unavailable", tool="pcbnew")
    try:
        import pcbnew
        board = pcbnew.LoadBoard(str(boards[0]))
        board.BuildConnectivity()
        unconnected = int(
            board.GetConnectivity().GetUnconnectedCount(False))
        metrics["unconnected_items"] = unconnected
        if unconnected:
            issues.append(f"PCB has {unconnected} unconnected item(s)")

        footprints = {item.GetReference(): item for item in board.GetFootprints()}

        def distance(left: str, right: str) -> float:
            a, b = footprints[left].GetPosition(), footprints[right].GetPosition()
            return math.hypot(
                pcbnew.ToMM(a.x - b.x), pcbnew.ToMM(a.y - b.y))

        for capacitor, limit in (("C1", 22.0), ("C2", 35.0)):
            if capacitor not in footprints or "U1" not in footprints:
                issues.append(f"{capacitor}/U1 placement cannot be measured")
                continue
            measured = distance(capacitor, "U1")
            metrics[f"{capacitor.lower()}_to_u1_mm"] = round(measured, 3)
            if measured > limit:
                issues.append(f"{capacitor} is {measured:.1f}mm from U1 (limit {limit:g}mm)")

        lengths: dict[str, float] = {}
        minimum_widths: dict[str, float] = {}
        narrow_lengths: dict[str, float] = {}
        for track in board.GetTracks():
            if track.GetClass() == "PCB_VIA":
                continue
            net = str(track.GetNetname())
            lengths[net] = lengths.get(net, 0.0) + pcbnew.ToMM(track.GetLength())
            width = pcbnew.ToMM(track.GetWidth())
            minimum_widths[net] = min(minimum_widths.get(net, width), width)
        metrics["net_lengths_mm"] = {
            net: round(value, 3) for net, value in sorted(lengths.items())}
        metrics["minimum_track_widths_mm"] = {
            net: round(value, 3) for net, value in sorted(minimum_widths.items())}

        physical = {component.ref for component in plan.components
                    if component.on_board}
        endpoint_counts: dict[str, int] = {}
        for connection in plan.connections:
            if connection.ref in physical:
                endpoint_counts[connection.net] = endpoint_counts.get(connection.net, 0) + 1
        for net, count in endpoint_counts.items():
            if count > 1 and net not in lengths:
                issues.append(f"routable net {net} has no copper tracks")
        for net, rule in plan.net_classes.items():
            actual = minimum_widths.get(net)
            if actual is None:
                continue
            narrow_length = sum(
                pcbnew.ToMM(track.GetLength()) for track in board.GetTracks()
                if track.GetClass() != "PCB_VIA"
                and str(track.GetNetname()) == net
                and pcbnew.ToMM(track.GetWidth()) + 1e-6 < rule.track_width_mm)
            narrow_lengths[net] = narrow_length
            fraction = narrow_length / lengths[net] if lengths.get(net) else 0.0
            # Freerouting may neck down briefly at smaller connector pads.
            # Bound both the neck width and its share of total route length.
            if (actual + 1e-6 < 0.7 * rule.track_width_mm
                    or fraction > 0.15):
                issues.append(
                    f"net {net} has excessive neck-down: min={actual:.3f}mm, "
                    f"fraction={fraction:.1%}")
        metrics["neck_down_length_mm"] = {
            net: round(value, 3)
            for net, value in sorted(narrow_lengths.items())}

        if plan.topology == BUCK_TOPOLOGY:
            switch_length = lengths.get("SW", 0.0)
            feedback_length = lengths.get("FB", 0.0)
            metrics["switch_node_length_mm"] = round(switch_length, 3)
            metrics["feedback_length_mm"] = round(feedback_length, 3)
            if switch_length > 45.0:
                issues.append("SW copper length exceeds the 45mm basic EMC limit")
            if feedback_length > 60.0:
                issues.append("feedback copper length exceeds the 60mm limit")
            for ref in ("R1", "R2"):
                if ref in footprints and "U1" in footprints:
                    measured = distance(ref, "U1")
                    metrics[f"{ref.lower()}_to_u1_mm"] = round(measured, 3)
                    if measured > 28.0:
                        issues.append(f"{ref} is too far from U1 feedback pin")
    except Exception as exc:
        issues.append(f"pcbnew layout inspection failed: {exc}")
    return _checked_gate(
        project_dir, "emc", issues,
        "basic power-loop, feedback, decoupling, and copper checks passed",
        metrics=metrics, tool="pcbnew deterministic layout checker")


def _gate_findings(gates: dict[str, VerificationGate],
                   required: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for name in required:
        gate = gates.get(name)
        if gate is not None and gate.status == GateStatus.passed:
            continue
        status = gate.status.value if gate else "missing"
        summary = gate.summary if gate else f"required gate {name} was not executed"
        findings.append(Finding.model_validate({
            "detector": "ratsnest_production_verifier",
            "rule_id": f"RN-GATE-{name.upper()}",
            "severity": "error",
            "confidence": "deterministic",
            "summary": f"{name} gate {status}: {summary}",
            "recommendation": "resolve the gate evidence before release",
            "gate": name,
            "gate_status": status,
        }))
    return findings


def verify_production(project_dir: Path, config: Config | None = None,
                      ) -> tuple[dict[str, VerificationGate], list[Finding]]:
    """Run all required gates for a v2 production BoardPlan.

    Legacy repair-only projects have no BoardPlan and retain the original
    checker/ERC behavior; they are never represented as production-ready.
    """
    config = config or Config.load()
    project_dir = Path(project_dir)
    if not (project_dir / "boardplan.json").is_file():
        return {}, []
    try:
        plan, spec, approved = _load_contracts(project_dir)
    except Exception as exc:
        gate = VerificationGate(
            name="catalog", status=GateStatus.error,
            summary=f"production contracts are invalid: {exc}",
            tool="Pydantic contract validator")
        return {"catalog": gate}, _gate_findings(
            {"catalog": gate}, ["catalog"])

    gates = {
        "catalog": _catalog_gate(project_dir, plan, spec, approved, config),
        "bom": _bom_gate(project_dir, plan),
        "erc": run_erc_gate(project_dir, config),
        "drc": run_drc_gate(project_dir, config),
        "spice": run_spice_gate(project_dir, plan, spec, config),
        "thermal": _thermal_gate(project_dir, plan, spec),
        "emc": _emc_gate(project_dir, plan, config),
    }
    required = list(plan.required_gates)
    for name in required:
        if name not in gates:
            gates[name] = VerificationGate(
                name=name, status=GateStatus.unavailable,
                summary="required gate has no registered implementation")
    summary_path = project_dir / "verification" / "summary.json"
    summary_path.write_text(json.dumps({
        "required_gates": required,
        "required_gates_passed": all(
            gates[name].status == GateStatus.passed for name in required),
        "gates": {name: gate.model_dump(mode="json")
                  for name, gate in gates.items()},
    }, indent=2), encoding="utf-8")
    for gate in gates.values():
        if "verification/summary.json" not in gate.evidence:
            gate.evidence.append("verification/summary.json")
    return gates, _gate_findings(gates, required)
