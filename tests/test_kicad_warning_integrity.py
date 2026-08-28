from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from agents.ratsnestpro import tools as agent_tools
from agents.ratsnestpro.tools import (
    _classify_eda_warnings,
    _verification_blockers,
)
from ratsnestpro.eda.vendor.library import read_lib_table
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import find_all, find_first, loads, tag_of
from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline_contracts import (
    SchLayoutPlan,
    SelectedPart,
    SelectionPlan,
    SheetPlacement,
)


def test_schematic_placement_is_on_connection_grid() -> None:
    snapped = pipeline._snap_sheet_placement(
        SheetPlacement(ref="U1", x=16.16, y=42.48, rotation=87.0)
    )

    assert math.isclose(round(snapped.x / 1.27), snapped.x / 1.27)
    assert math.isclose(round(snapped.y / 1.27), snapped.y / 1.27)
    assert snapped.rotation == 90.0


def test_overlap_reflow_remains_on_connection_grid(monkeypatch) -> None:
    state = pipeline.PipelineState(requirement_text="test")
    state.artifacts[pipeline.PipelineStep.SELECTION] = SelectionPlan(parts=[
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="1k",
            footprint="Resistor_SMD:R_0805_2012Metric",
        ),
        SelectedPart(
            ref="C1",
            symbol="Device:C",
            value="100nF",
            footprint="Capacitor_SMD:C_0805_2012Metric",
        ),
    ])
    proposed = SchLayoutPlan(
        placements=[
            SheetPlacement(ref="R1", x=16.16, y=42.48, rotation=87.0),
            SheetPlacement(ref="C1", x=16.16, y=42.48, rotation=2.0),
        ]
    )
    monkeypatch.setattr(
        pipeline,
        "propose_structured",
        lambda *args, **kwargs: (proposed, True),
    )
    monkeypatch.setattr(pipeline.symbols, "symbol_pins", lambda _lib_id: [])

    artifact, _used_llm = pipeline.SchLayoutStep().propose(
        state,
        pipeline.PipelineContext(),
        "",
    )

    assert isinstance(artifact, SchLayoutPlan)
    assert not pipeline._sheet_overlaps(artifact.placements, {})
    grid_check = next(
        check
        for check in pipeline.SchLayoutStep().check(state, artifact)
        if check.name == "schematic_connection_grid"
    )
    assert grid_check.ok is True


def test_project_library_tables_bind_only_resolved_libraries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    symbol_file = tmp_path / "installed-symbols" / "Timer.kicad_sym"
    footprint_file = (
        tmp_path
        / "installed-footprints"
        / "Package_SO.pretty"
        / "SOIC-8.kicad_mod"
    )
    symbol_file.parent.mkdir()
    footprint_file.parent.mkdir(parents=True)
    symbol_file.write_text("resolved", encoding="utf-8")
    footprint_file.write_text("resolved", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(symbol_file.parent))
    monkeypatch.setenv(
        "KICAD_FOOTPRINT_DIR",
        str(footprint_file.parent.parent),
    )

    monkeypatch.setattr(
        pipeline.symbols,
        "resolve_symbol",
        lambda lib_id: symbol_file if lib_id == "Timer:NE555D" else None,
    )
    monkeypatch.setattr(
        pipeline.footprints,
        "footprint_path",
        lambda lib_id: footprint_file if lib_id == "Package_SO:SOIC-8" else None,
    )

    assert pipeline._register_project_library_bindings(
        project,
        ["Timer:NE555D", "Missing:Part"],
        kind="sym",
    ) == ["Timer"]
    assert pipeline._register_project_library_bindings(
        project,
        ["Package_SO:SOIC-8", "Missing:Footprint"],
        kind="fp",
    ) == ["Package_SO"]

    assert read_lib_table("sym", str(project)) == [{
        "name": "Timer",
        "type": "KiCad",
        "uri": "${KIPRJMOD}/.ratsnest-libs/symbols/Timer.kicad_sym",
        "options": "",
        "descr": "",
    }]
    assert read_lib_table("fp", str(project)) == [{
        "name": "Package_SO",
        "type": "KiCad",
        "uri": "${KIPRJMOD}/.ratsnest-libs/footprints/Package_SO.pretty",
        "options": "",
        "descr": "",
    }]
    lock = json.loads(
        (project / "library-bindings.lock.json").read_text(encoding="utf-8")
    )
    assert [(item["kind"], item["lib_id"]) for item in lock["bindings"]] == [
        ("fp", "Package_SO:SOIC-8"),
        ("sym", "Timer:NE555D"),
    ]
    assert all(len(item["source_sha256"]) == 64 for item in lock["bindings"])
    assert all(item["vendored"] is True for item in lock["bindings"])
    assert (
        project / ".ratsnest-libs" / "symbols" / "Timer.kicad_sym"
    ).read_text(encoding="utf-8") == "resolved"
    assert (
        project
        / ".ratsnest-libs"
        / "footprints"
        / "Package_SO.pretty"
        / "SOIC-8.kicad_mod"
    ).read_text(encoding="utf-8") == "resolved"


def test_schematic_project_context_is_created_without_overwriting(
    tmp_path: Path,
) -> None:
    schematic = tmp_path / "board.kicad_sch"

    project = pipeline._ensure_kicad_project_context(schematic)

    assert project == tmp_path / "board.kicad_pro"
    assert json.loads(project.read_text(encoding="utf-8")) == {}

    project.write_text('{"existing": true}\n', encoding="utf-8")
    pipeline._ensure_kicad_project_context(schematic)
    assert json.loads(project.read_text(encoding="utf-8")) == {"existing": True}


def test_schematic_materialize_registers_full_local_library_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    components = [{
        "symbol": "Timer:NE555D",
        "footprint": "Package_SO:SOIC-8",
    }]
    registrations: list[tuple[str, tuple[str, ...]]] = []

    class FakeDoc:
        def save(self, path: Path) -> None:
            path.write_text("schematic", encoding="utf-8")

    step = pipeline.SchMaterializeStep()
    monkeypatch.setattr(step, "_components", lambda _state: components)
    monkeypatch.setattr(step, "_nets", lambda _state: [])
    monkeypatch.setattr(step, "_no_connect_pins", lambda _state: [])
    monkeypatch.setattr(
        pipeline,
        "materialize_pinmapped",
        lambda *args, **kwargs: FakeDoc(),
    )
    monkeypatch.setattr(
        pipeline,
        "_register_project_library_bindings",
        lambda _project, lib_ids, *, kind: registrations.append(
            (kind, tuple(lib_ids))
        ),
    )

    artifact, used_llm = step.propose(
        pipeline.PipelineState(requirement_text="test", project_name="board"),
        pipeline.PipelineContext(out_dir=str(tmp_path)),
        "",
    )

    assert registrations == [
        ("sym", ("Timer:NE555D", "power:PWR_FLAG")),
        ("fp", ("Package_SO:SOIC-8",)),
    ]
    assert Path(artifact.sch_path).is_file()
    assert (tmp_path / "board.kicad_pro").is_file()
    assert used_llm is False


def test_project_library_evidence_is_in_current_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    schematic = tmp_path / "board.kicad_sch"
    schematic.write_text("schematic", encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text("table", encoding="utf-8")
    (tmp_path / "library-bindings.lock.json").write_text("{}", encoding="utf-8")
    vendored = tmp_path / ".ratsnest-libs" / "symbols" / "Timer.kicad_sym"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("resolved", encoding="utf-8")
    state = pipeline.PipelineState(requirement_text="test")
    state.artifacts[pipeline.PipelineStep.SCH_MATERIALIZE] = (
        pipeline.MaterializeResult(
            sch_path=str(schematic),
            component_count=1,
            net_count=1,
            label_count=1,
        )
    )
    monkeypatch.setattr(agent_tools, "_workspace_root", lambda: tmp_path)

    delivered = agent_tools._current_pipeline_files(state)

    assert "sym-lib-table" in delivered
    assert "library-bindings.lock.json" in delivered
    assert ".ratsnest-libs\\symbols\\Timer.kicad_sym" in delivered or (
        ".ratsnest-libs/symbols/Timer.kicad_sym" in delivered
    )


def test_symbol_directory_binding_vendors_and_hashes_inheritance_closure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "installed" / "Generated.kicad_symdir"
    source_dir.mkdir(parents=True)
    derived = source_dir / "Derived.kicad_sym"
    base = source_dir / "Base.kicad_sym"
    derived.write_text('(extends "Base")', encoding="utf-8")
    base.write_text("base pins", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(pipeline.symbols, "resolve_symbol", lambda _lib_id: derived)

    pipeline._register_project_library_bindings(
        project,
        ["Generated:Derived"],
        kind="sym",
    )

    vendored = project / ".ratsnest-libs" / "symbols" / source_dir.name
    assert (vendored / derived.name).is_file()
    assert (vendored / base.name).is_file()
    lock = json.loads(
        (project / "library-bindings.lock.json").read_text(encoding="utf-8")
    )
    assert {Path(item["uri"]).name for item in lock["bindings"][0]["library_files"]} == {
        "Base.kicad_sym",
        "Derived.kicad_sym",
    }


def test_same_nickname_from_multiple_roots_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "root-a" / "Shared.pretty" / "A.kicad_mod"
    second = tmp_path / "root-b" / "Shared.pretty" / "B.kicad_mod"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        pipeline.footprints,
        "footprint_path",
        lambda lib_id: first if lib_id.endswith(":A") else second,
    )

    with pytest.raises(ValueError, match="resolved from multiple roots"):
        pipeline._register_project_library_bindings(
            project,
            ["Shared:A", "Shared:B"],
            kind="fp",
        )


def test_footprint_instance_preserves_library_geometry_and_reference_position() -> None:
    source = loads(
        """
        (footprint "C_0805"
          (version 20240108)
          (generator pcbnew)
          (layer "F.Cu")
          (descr "real library geometry")
          (tags "capacitor smd")
          (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
            (uuid "11111111-1111-1111-1111-111111111111"))
          (property "Value" "C_0805" (at 0 2 0) (layer "F.Fab")
            (uuid "22222222-2222-2222-2222-222222222222"))
          (attr smd)
          (pad "1" smd roundrect (at -0.95 0 30) (size 1 1.45)
            (layers "F.Cu" "F.Paste" "F.Mask")
            (uuid "33333333-3333-3333-3333-333333333333")))
        """
    )
    assert isinstance(source, list)
    board = PcbBoard.blank()

    board.add_footprint(
        "Capacitor_SMD:C_0805",
        "C7",
        "100nF",
        10.0,
        20.0,
        rotation=90.0,
        embed_node=source,
    )

    footprint = find_all(board.root, "footprint")[0]
    assert str(footprint[1]) == "Capacitor_SMD:C_0805"
    assert find_first(footprint, "descr")[1] == "real library geometry"
    assert str(find_first(footprint, "attr")[1]) == "smd"
    assert [str(value) for value in find_first(footprint, "at")[1:]] == [
        "10",
        "20",
        "90",
    ]

    properties = {
        str(child[1]): child
        for child in footprint
        if isinstance(child, list) and tag_of(child) == "property"
    }
    assert properties["Reference"][2] == "C7"
    assert properties["Value"][2] == "100nF"
    assert [str(value) for value in find_first(properties["Reference"], "at")[1:]] == [
        "0",
        "-2",
        "0",
    ]

    pad = find_first(footprint, "pad")
    assert pad is not None
    assert [str(value) for value in find_first(pad, "at")[1:]] == [
        "-0.95",
        "0",
        "120",
    ]
    assert str(find_first(pad, "uuid")[1]) != (
        "33333333-3333-3333-3333-333333333333"
    )

    assert board.rotate_footprint("C7", 180.0)
    assert [str(value) for value in find_first(pad, "at")[1:]] == [
        "-0.95",
        "0",
        "210",
    ]


def test_remaining_warnings_are_classified_and_never_suppressed() -> None:
    classifications = _classify_eda_warnings([
        {"severity": "warning", "type": "silk_overlap"},
        {"severity": "warning", "type": "future_rule"},
        {"severity": "error", "type": "shorting_items"},
    ])

    assert classifications["silk_overlap"]["disposition"] == (
        "repair_or_explicit_waiver_required"
    )
    assert classifications["future_rule"]["disposition"] == (
        "explicit_review_required"
    )
    assert all(
        classification["suppressed"] is False
        for classification in classifications.values()
    )


def test_unresolved_warning_dispositions_block_independent_review() -> None:
    verification = {
        "erc": {
            "applicable": True,
            "available": True,
            "ran": True,
            "errors": 0,
            "warning_classifications": {
                "endpoint_off_grid": {
                    "count": 2,
                    "disposition": "repair_required",
                },
                "future_rule": {
                    "count": 1,
                    "disposition": "explicit_review_required",
                },
            },
        },
        "drc": {
            "applicable": True,
            "available": True,
            "ran": True,
            "errors": 0,
            "unconnected": 0,
            "warning_classifications": {},
        },
    }

    blockers = _verification_blockers(verification)

    assert any("endpoint_off_grid (2 finding(s))" in item for item in blockers)
    assert any("future_rule (1 finding(s))" in item for item in blockers)


def test_unverified_warning_waiver_cannot_bypass_review_blocker() -> None:
    verification = {
        "erc": {
            "applicable": True,
            "available": True,
            "ran": True,
            "errors": 0,
            "warning_classifications": {},
        },
        "drc": {
            "applicable": True,
            "available": True,
            "ran": True,
            "errors": 0,
            "unconnected": 0,
            "warning_classifications": {
                "silk_overlap": {
                    "count": 3,
                    "disposition": "repair_or_explicit_waiver_required",
                }
            },
            # A caller-supplied assertion is not a digest-bound waiver contract.
            "warning_waivers": [{"rule_id": "silk_overlap", "approved": True}],
        },
    }

    blockers = _verification_blockers(verification)

    assert any(
        "no verified warning-waiver contract is implemented" in item
        for item in blockers
    )
