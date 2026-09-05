from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import ratsnestpro.orchestration.pipeline as pipeline_module
from ratsnestpro.domain.contracts import RequirementSpec
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import Atom, find_all, find_first, tag_of
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    ErcStep,
    ManufactureStep,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    PipelineStepBase,
    _DrcSnapshot,
    _read_drc_snapshot,
    _repair_silkscreen_entities,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    ErcSummary,
    ManufactureResult,
    TopologyPlan,
)


def _write_report(path: Path, finding: dict) -> None:
    path.write_text(
        json.dumps({"violations": [finding]}),
        encoding="utf-8",
    )


def test_drc_snapshot_counts_and_routes_smd_to_pth_gap(tmp_path: Path) -> None:
    report = tmp_path / "board.drc.json"
    report.write_text(json.dumps({
        "violations": [],
        "schematic_parity": [],
        "unconnected_items": [{
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection between items",
            "items": [
                {
                    "description": "Pad 18 [I2C_SDA] of U2 on F.Cu",
                    "pos": {"x": 25.3625, "y": 16.375},
                },
                {
                    "description": "PTH pad 4 [I2C_SDA] of J3",
                    "pos": {"x": 40.0, "y": 17.12},
                },
            ],
        }],
    }), encoding="utf-8")

    snapshot = _read_drc_snapshot(report)

    assert snapshot.unconnected == 1
    assert len(snapshot.gaps) == 1
    assert snapshot.gaps[0].left.layer == "F.Cu"
    assert snapshot.gaps[0].right.layer == "F.Cu"


def test_manufacture_short_maps_to_u1_and_clean_layout_rebuild(
    tmp_path: Path,
) -> None:
    report = tmp_path / "board.drc.json"
    _write_report(report, {
        "type": "shorting_items",
        "severity": "error",
        "description": "Items shorting two nets",
        "items": [
            {
                "description": "Pad 1 [GND] of U1 on F.Cu",
                "pos": {"x": 1.0, "y": 2.0},
            },
            {
                "description": "Pad 2 [VCC] of U1 on F.Cu",
                "pos": {"x": 1.2, "y": 2.0},
            },
        ],
    })
    artifact = ManufactureResult(
        drc_report_path=str(report),
        drc_violations=["kicad_cli:shorting_items:Items shorting two nets"],
    )
    state = PipelineState(requirement_text="test")
    step = ManufactureStep()

    drc_check = next(
        check for check in step.check(state, artifact) if check.name == "drc_clean"
    )

    assert drc_check.affected_refs == ["U1"]
    assert "repair_or_substitute_verified_footprint_geometry" in drc_check.reason_code
    assert step.rollback_target(state, artifact, [drc_check]) == PipelineStep.LAYOUT_WRITE


def test_erc_pin_not_connected_rebuilds_materialization_from_design_ir(
    tmp_path: Path,
) -> None:
    report = tmp_path / "board.erc.json"
    report.write_text(json.dumps({
        "sheets": [{
            "violations": [{
                "type": "pin_not_connected",
                "severity": "error",
                "description": "Pin not connected",
                "items": [{
                    "description": "Symbol J1 Pin 1 [Pin_1, Passive, Line]",
                    "pos": {"x": 25.4, "y": 30.48},
                }],
            }],
        }],
    }), encoding="utf-8")
    artifact = ErcSummary(
        sch_path=str(tmp_path / "board.kicad_sch"),
        cli_available=True,
        cli_ran=True,
        cli_error_count=1,
        cli_report_path=str(report),
        connectivity_checked=True,
        connectivity_matches=False,
        connectivity_missing=["J1.1@VIN"],
    )
    state = PipelineState(requirement_text="test")
    step = ErcStep()
    checks = step.check(state, artifact)
    cli_check = next(check for check in checks if check.name == "kicad_cli_erc")

    assert cli_check.affected_refs == ["J1"]
    assert step.rollback_target(state, artifact, checks) == PipelineStep.SCH_MATERIALIZE


def test_erc_pin_type_conflict_carries_pin_net_evidence_to_source_owner(
    tmp_path: Path,
) -> None:
    report = tmp_path / "board.erc.json"
    report.write_text(json.dumps({
        "sheets": [{
            "violations": [{
                "type": "pin_to_pin",
                "severity": "error",
                "description": "Pins of type Output and Power output are connected",
                "items": [
                    {"description": "Symbol U3 Pin 7 [SDO, Output, Line]"},
                    {
                        "description": (
                            "Symbol #PWR01 Pin 1 [Power output, Line]"
                        )
                    },
                ],
            }],
        }],
    }), encoding="utf-8")
    netlist = tmp_path / "board.netlist.xml"
    netlist.write_text(
        '<export><nets><net code="1" name="/GND">'
        '<node ref="U3" pin="7" pinfunction="SDO" pintype="output"/>'
        '<node ref="#PWR01" pin="1" pintype="power_out"/>'
        "</net></nets></export>",
        encoding="utf-8",
    )
    artifact = ErcSummary(
        sch_path=str(tmp_path / "board.kicad_sch"),
        cli_available=True,
        cli_ran=True,
        cli_error_count=1,
        cli_report_path=str(report),
        connectivity_checked=True,
        connectivity_matches=True,
        connectivity_netlist_path=str(netlist),
    )
    state = PipelineState(requirement_text="test")
    step = ErcStep()
    checks = step.check(state, artifact)
    cli_check = next(check for check in checks if check.name == "kicad_cli_erc")
    plan = cli_check.evidence["entity_repair_plans"][0]

    assert cli_check.affected_refs == ["#PWR01", "U3"]
    assert plan["affected_nets"] == ["GND"]
    assert plan["pin_net_facts"] == [{
        "ref": "U3",
        "pin": "7",
        "net": "GND",
        "source": "kicad_xml_netlist",
    }]
    assert plan["observed_items"][0]["description"].startswith("Symbol U3")
    assert step.rollback_target(state, artifact, checks) == PipelineStep.SCH_CONNECTIONS


class _BlockedRequirements(PipelineStepBase):
    step = PipelineStep.REQUIREMENTS

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        return RequirementSpec(raw_text=state.requirement_text), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        return [CheckResult(name="release_contract", ok=False, message="blocked")]


class _ObservedTopology(PipelineStepBase):
    step = PipelineStep.TOPOLOGY

    def __init__(self) -> None:
        self.called = False

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        self.called = True
        return TopologyPlan(), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        return []


def test_release_repair_mode_does_not_skip_a_blocked_stage() -> None:
    topology = _ObservedTopology()
    pipeline = Pipeline([_BlockedRequirements(), topology])

    pipeline.run(
        PipelineState(requirement_text="test"),
        PipelineContext(
            artifact_first=True,
            repair_release_issues=True,
            ahe_enabled=False,
        ),
    )

    assert topology.called is False


def test_silkscreen_repair_edits_real_entity_and_keeps_drc_monotonic(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pcb_path = tmp_path / "board.kicad_pcb"
    report_path = tmp_path / "board.drc.json"
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 20, 20)
    board.add_footprint(
        lib_id="Resistor_SMD:R_0805_2012Metric",
        reference="R1",
        value="1k",
        x=10,
        y=10,
    )
    board.save(pcb_path)
    footprint = find_all(board.root, "footprint")[0]
    reference = next(
        child
        for child in footprint
        if isinstance(child, list)
        and tag_of(child) == "property"
        and len(child) > 1
        and str(child[1]) == "Reference"
    )
    reference_uuid = str(find_first(reference, "uuid")[1])
    report_path.write_text(json.dumps({
        "violations": [{
            "type": "silk_edge_clearance",
            "severity": "warning",
            "description": "Silkscreen clipped by board edge",
            "items": [{
                "description": "Reference field of R1",
                "uuid": reference_uuid,
                "pos": {"x": 10, "y": 10},
            }],
        }],
        "unconnected_items": [],
        "schematic_parity": [],
    }), encoding="utf-8")

    def clean_candidate(_cli: str, _pcb: Path, candidate: Path) -> _DrcSnapshot:
        candidate.write_text(
            json.dumps({
                "violations": [],
                "unconnected_items": [],
                "schematic_parity": [],
            }),
            encoding="utf-8",
        )
        return _DrcSnapshot(findings=(), non_connectivity_errors=(), gaps=())

    monkeypatch.setattr(
        pipeline_module,
        "_run_kicad_drc_snapshot",
        clean_candidate,
    )

    assert _repair_silkscreen_entities("kicad-cli", pcb_path, report_path)
    repaired = PcbBoard.load(pcb_path)
    repaired_footprint = find_all(repaired.root, "footprint")[0]
    repaired_reference = next(
        child
        for child in repaired_footprint
        if isinstance(child, list)
        and tag_of(child) == "property"
        and len(child) > 1
        and str(child[1]) == "Reference"
    )

    assert str(find_first(repaired_reference, "layer")[1]) == "F.Fab"


def test_silkscreen_repair_prefers_reference_over_package_outline(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pcb_path = tmp_path / "board.kicad_pcb"
    report_path = tmp_path / "board.drc.json"
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 20, 20)
    board.add_footprint(
        lib_id="Resistor_SMD:R_0805_2012Metric",
        reference="R1",
        value="1k",
        x=10,
        y=10,
    )
    footprint = find_all(board.root, "footprint")[0]
    outline_uuid = "f6faf536-5a84-4d14-a6ae-a26a18a41936"
    footprint.append([
        Atom("fp_line"),
        [Atom("start"), Atom("-1"), Atom("0")],
        [Atom("end"), Atom("1"), Atom("0")],
        [
            Atom("stroke"),
            [Atom("width"), Atom("0.15")],
            [Atom("type"), Atom("default")],
        ],
        [Atom("layer"), "F.SilkS"],
        [Atom("uuid"), outline_uuid],
    ])
    board.save(pcb_path)
    reference = next(
        child
        for child in footprint
        if isinstance(child, list)
        and tag_of(child) == "property"
        and len(child) > 1
        and str(child[1]) == "Reference"
    )
    reference_uuid = str(find_first(reference, "uuid")[1])
    report_path.write_text(json.dumps({
        "violations": [{
            "type": "silk_overlap",
            "severity": "warning",
            "description": "Silkscreen overlap",
            "items": [
                {
                    "description": "Reference field of R1",
                    "uuid": reference_uuid,
                    "pos": {"x": 10, "y": 10},
                },
                {
                    "description": "Segment of R1 on F.Silkscreen",
                    "uuid": outline_uuid,
                    "pos": {"x": 10, "y": 10},
                },
            ],
        }],
        "unconnected_items": [],
        "schematic_parity": [],
    }), encoding="utf-8")

    def clean_candidate(_cli: str, _pcb: Path, candidate: Path) -> _DrcSnapshot:
        candidate.write_text(json.dumps({
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        }), encoding="utf-8")
        return _DrcSnapshot(findings=(), non_connectivity_errors=(), gaps=())

    monkeypatch.setattr(
        pipeline_module,
        "_run_kicad_drc_snapshot",
        clean_candidate,
    )

    assert _repair_silkscreen_entities("kicad-cli", pcb_path, report_path)
    repaired = PcbBoard.load(pcb_path)
    repaired_footprint = find_all(repaired.root, "footprint")[0]
    repaired_reference = next(
        child
        for child in repaired_footprint
        if isinstance(child, list)
        and tag_of(child) == "property"
        and len(child) > 1
        and str(child[1]) == "Reference"
    )
    repaired_outline = next(
        child
        for child in find_all(repaired_footprint, "fp_line")
        if str(find_first(child, "uuid")[1]) == outline_uuid
    )

    assert str(find_first(repaired_reference, "layer")[1]) == "F.Fab"
    assert str(find_first(repaired_outline, "layer")[1]) == "F.SilkS"


def test_silkscreen_repair_rolls_back_when_connectivity_regresses(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pcb_path = tmp_path / "board.kicad_pcb"
    report_path = tmp_path / "board.drc.json"
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 20, 20)
    board.add_footprint(
        lib_id="Resistor_SMD:R_0805_2012Metric",
        reference="R1",
        value="1k",
        x=10,
        y=10,
    )
    board.save(pcb_path)
    footprint = find_all(board.root, "footprint")[0]
    reference = next(
        child
        for child in footprint
        if isinstance(child, list)
        and tag_of(child) == "property"
        and len(child) > 1
        and str(child[1]) == "Reference"
    )
    reference_uuid = str(find_first(reference, "uuid")[1])
    report_path.write_text(json.dumps({
        "violations": [{
            "type": "silk_over_copper",
            "severity": "warning",
            "description": "Silkscreen clipped by solder mask",
            "items": [{
                "description": "Reference field of R1",
                "uuid": reference_uuid,
                "pos": {"x": 10, "y": 10},
            }],
        }],
        "unconnected_items": [],
        "schematic_parity": [],
    }), encoding="utf-8")
    original_pcb = pcb_path.read_bytes()
    original_report = report_path.read_bytes()

    def regressed_candidate(
        _cli: str,
        _pcb: Path,
        candidate: Path,
    ) -> _DrcSnapshot:
        candidate.write_text(json.dumps({
            "violations": [],
            "unconnected_items": [{
                "type": "unconnected_items",
                "severity": "error",
                "description": "Missing connection between items",
            }],
            "schematic_parity": [],
        }), encoding="utf-8")
        return _DrcSnapshot(
            findings=("kicad_cli:unconnected_items:Missing connection",),
            non_connectivity_errors=(),
            gaps=(),
            reported_unconnected=1,
        )

    monkeypatch.setattr(
        pipeline_module,
        "_run_kicad_drc_snapshot",
        regressed_candidate,
    )

    assert not _repair_silkscreen_entities(
        "kicad-cli",
        pcb_path,
        report_path,
    )
    assert pcb_path.read_bytes() == original_pcb
    assert report_path.read_bytes() == original_report
