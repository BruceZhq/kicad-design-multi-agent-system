"""Stage 3 production-family contracts and seeded-defect benchmarks."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml

from ratsnest.catalog import load_catalog
from ratsnest.circuit_math import (
    BUCK_TOPOLOGY,
    LDO_TOPOLOGY,
    GenerationError,
    UnsupportedRequirementError,
    solve_circuit,
)
from ratsnest.config import REPO_ROOT, Config
from ratsnest.crews.circuit_families import build_canonical_plan
from ratsnest.crews.design_agents import CircuitArchitect
from ratsnest.design_edit.kicad_cli import parse_kicad_report
from ratsnest.evolution.registry import load_strategy
from ratsnest.freerouting import run_freerouting
from ratsnest.manufacturing import catalog_issues, write_manufacturing_outputs
from ratsnest.schemas import DesignSpec, GateStatus
from ratsnest.spice import run_spice_gate
from ratsnest.verification import _bom_gate, _emc_gate, _thermal_gate


def _strategy():
    return load_strategy(REPO_ROOT / "agent-runtime" / "strategies" / "v0")


def _buck_spec(**updates) -> DesignSpec:
    values = {
        "project_name": "stage3_buck_test",
        "input_voltage": 12.0,
        "output_voltage": 5.0,
        "output_current_a": 0.5,
        "led": None,
        "topology": "buck",
        "ambient_temperature_c": 25.0,
        "max_output_ripple_mv": 100.0,
        "requirement_text": "12V to 5V 500mA buck supply",
    }
    values.update(updates)
    return DesignSpec.model_validate(values)


def _plan(spec: DesignSpec):
    return build_canonical_plan(
        spec, solve_circuit(spec, _strategy(), Config.load()))


def test_acceptance_suite_covers_goldens_and_seeded_defects():
    suite = yaml.safe_load(
        (REPO_ROOT / "benchmarks" / "stage3" / "cases.yaml").read_text(
            encoding="utf-8"))

    assert {row["expected_topology"] for row in suite["goldens"]} == {
        LDO_TOPOLOGY, BUCK_TOPOLOGY}
    assert {row["owner"] for row in suite["seeded_defects"]} == {
        "planning", "catalog", "erc", "drc", "thermal", "emc"}
    assert all(row["required_gates"] == [
        "catalog", "bom", "erc", "drc", "spice", "thermal", "emc"]
        for row in suite["goldens"])


def test_qualified_topologies_and_safe_envelopes():
    ldo = DesignSpec(
        input_voltage=5.0, output_voltage=3.3, output_current_a=0.05,
        led=None, topology="auto")
    assert solve_circuit(ldo, _strategy()).topology == LDO_TOPOLOGY
    assert solve_circuit(_buck_spec(), _strategy()).topology == BUCK_TOPOLOGY

    with pytest.raises(UnsupportedRequirementError, match="boost converter"):
        solve_circuit(_buck_spec(
            requirement_text="12V boost converter", topology="auto"),
            _strategy())
    with pytest.raises(GenerationError, match="outside envelope"):
        solve_circuit(_buck_spec(output_current_a=3.0), _strategy())


def test_catalog_and_bom_reconciliation_detect_tampering(tmp_path):
    spec = _buck_spec()
    plan = _plan(spec)
    assert plan.catalog_version == load_catalog().version
    assert catalog_issues(plan) == []

    write_manufacturing_outputs(tmp_path, plan, spec)
    assert _bom_gate(tmp_path, plan).status == GateStatus.passed

    bad_plan = plan.model_copy(deep=True)
    bad_plan.component("U1").properties["MPN"] = ""
    assert any("no exact MPN" in issue for issue in catalog_issues(bad_plan))

    manifest_path = tmp_path / "manufacturing_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bom"][0]["mpn"] = "UNTRUSTED-PART"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _bom_gate(tmp_path, plan).status == GateStatus.failed


def test_solver_authority_rejects_wrong_feedback_divider():
    spec = _buck_spec()
    canonical = _plan(spec)
    bad = canonical.model_copy(deep=True)
    bad.component("R2").value = "99k"

    with pytest.raises(ValueError, match="component catalog"):
        CircuitArchitect.validate_candidate(bad, canonical)


def test_production_plan_exposes_feedback_and_indicator_testpoints():
    plan = _plan(_buck_spec(led="red"))
    pin_nets = {(item.ref, item.pin): item.net for item in plan.connections}
    positions = {item.ref: (item.x, item.y) for item in plan.placement_hints}

    for ref in ("R1", "R2"):
        component = plan.component(ref)
        assert component.catalog_id == "yageo.rc1206fr"
        assert component.footprint == "Resistor_SMD:R_1206_3216Metric"
        assert component.properties["MPN"].startswith("RC1206FR-07")
    assert plan.component("R3").catalog_id == "yageo.rc0805fr"
    assert plan.component("TP4").role == "feedback_testpoint"
    assert pin_nets[("TP4", "1")] == "FB"
    assert sum((a - b) ** 2 for a, b in zip(
        positions["TP4"], positions["R2"])) ** 0.5 < 10.0
    assert plan.component("TP5").role == "indicator_testpoint"
    assert pin_nets[("TP5", "1")] == "LED_A"


def test_spice_gate_uses_transient_output_and_ripple(tmp_path, monkeypatch):
    spec = _buck_spec()
    plan = _plan(spec)
    config = Config.load()
    config.ngspice_library = tmp_path / "ngspice.dll"
    config.ngspice_library.touch()

    def simulate(_library, _deck, data):
        data.write_text("\n".join(
            f"{index * 0.0002:.6f} {5.0 + (0.005 if index % 2 else -0.005):.6f}"
            for index in range(201)), encoding="utf-8")
        return ["simulated"]

    monkeypatch.setattr("ratsnest.spice._simulate", simulate)
    gate = run_spice_gate(tmp_path, plan, spec, config)
    assert gate.status == GateStatus.passed
    assert gate.metrics["peak_to_peak_ripple_mv"] == pytest.approx(10.0)

    spec = _buck_spec(max_output_ripple_mv=5.0)
    gate = run_spice_gate(tmp_path, _plan(spec), spec, config)
    assert gate.status == GateStatus.failed


def test_thermal_gate_rejects_seeded_junction_overload(tmp_path):
    spec = _buck_spec()
    plan = _plan(spec).model_copy(deep=True)
    plan.design_limits.estimated_junction_c = (
        plan.design_limits.max_junction_c + 5.0)

    gate = _thermal_gate(tmp_path, plan, spec)

    assert gate.status == GateStatus.failed
    assert "junction" in gate.summary


def test_kicad_report_parser_extracts_erc_and_drc_defects(tmp_path):
    erc = tmp_path / "erc.rpt"
    erc.write_text(
        "ERC report\n ** ERC messages: 2  Errors 1  Warnings 1\n",
        encoding="utf-8")
    drc = tmp_path / "drc.rpt"
    drc.write_text(
        "** Found 1 DRC violations **\n"
        "** Found 2 unconnected pads **\n"
        "** Found 0 Footprint errors **\n", encoding="utf-8")

    assert parse_kicad_report(erc, "erc")["errors"] == 1
    assert parse_kicad_report(drc, "drc") == {
        "violations": 1, "unconnected_pads": 2, "footprint_errors": 0}


def test_freerouting_adapter_owns_bounded_command(tmp_path, monkeypatch):
    java = tmp_path / "java.exe"
    jar = tmp_path / "freerouting.jar"
    board_path = tmp_path / "board.kicad_pcb"
    for path in (java, jar, board_path):
        path.touch()
    config = Config.load()
    config.freerouting_java = java
    config.freerouting_jar = jar
    config.freerouting_max_passes = 7
    config.freerouting_timeout_seconds = 41
    calls = {}

    class Board:
        def GetTracks(self):
            return []

        def Save(self, path):
            calls["saved"] = path

    def export(_board, path):
        Path(path).write_text("dsn", encoding="ascii")
        return True

    def imported(_board, path):
        calls["imported"] = path
        return True

    def run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        output = Path(command[command.index("-do") + 1])
        output.write_text("(session)", encoding="ascii")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setitem(sys.modules, "pcbnew", SimpleNamespace(
        ExportSpecctraDSN=export, ImportSpecctraSES=imported))
    monkeypatch.setattr("ratsnest.freerouting.subprocess.run", run)

    result = run_freerouting(Board(), board_path, config)

    command = calls["command"]
    assert command[0] == str(java.resolve())
    assert command[command.index("-mp") + 1] == "7"
    assert "--router.optimizer.enabled=false" in command
    assert "--router.max_threads=1" in command
    assert "--gui.enabled=false" in command
    assert calls["kwargs"]["timeout"] == 41
    assert "shell" not in calls["kwargs"]
    assert result["mode"] == "freerouting-cli"


def test_emc_gate_rejects_long_switch_node_and_remote_decoupling(
        tmp_path, monkeypatch):
    spec = _buck_spec()
    plan = _plan(spec)
    (tmp_path / "board.kicad_pcb").touch()

    class Position:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class Footprint:
        def __init__(self, ref, x, y):
            self.ref, self.position = ref, Position(x, y)

        def GetReference(self):
            return self.ref

        def GetPosition(self):
            return self.position

    class Track:
        def __init__(self, net, length, width):
            self.net, self.length, self.width = net, length, width

        def GetClass(self):
            return "PCB_TRACK"

        def GetNetname(self):
            return self.net

        def GetLength(self):
            return self.length

        def GetWidth(self):
            return self.width

    class Connectivity:
        def GetUnconnectedCount(self, _include_zones):
            return 0

    class Board:
        def BuildConnectivity(self):
            pass

        def GetConnectivity(self):
            return Connectivity()

        def GetFootprints(self):
            return [
                Footprint("U1", 0, 0), Footprint("C1", 30, 0),
                Footprint("C2", 10, 0), Footprint("R1", 2, 2),
                Footprint("R2", 3, 2),
            ]

        def GetTracks(self):
            return [
                Track("+12V", 20, 1.5), Track("+5V", 20, 1.5),
                Track("GND", 30, 1.5), Track("SW", 46, 1.2),
                Track("FB", 20, 0.25),
            ]

    monkeypatch.setattr("ratsnest.verification.bootstrap_kicad", lambda _p: True)
    monkeypatch.setitem(sys.modules, "pcbnew", SimpleNamespace(
        LoadBoard=lambda _path: Board(), ToMM=lambda value: value))

    gate = _emc_gate(tmp_path, plan, Config.load())

    assert gate.status == GateStatus.failed
    assert "C1" in gate.summary
    assert "SW copper" in gate.summary
