from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ratsnestpro.orchestration.component_resolution import (
    ComponentResolution,
    LibraryClosureResult,
    ResolutionStatus,
)
from ratsnestpro.orchestration.design_closure import (
    build_component_closure_manifest,
    design_ir_pin_net_set,
    diff_pin_net_sets,
    read_kicad_xml_pin_net_set,
    validate_component_closure_freshness,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    MappedNet,
    MappedPin,
    PinMapPlan,
    SelectedPart,
    SelectionPlan,
)


def _selection() -> SelectionPlan:
    return SelectionPlan(parts=[SelectedPart(
        ref="J1",
        symbol="Connector_Generic:Conn_01x02",
        value="5V input",
        footprint="Connector:PinHeader_1x02",
        footprint_binding_status="verified_installed",
        footprint_binding_basis="test_live_library_evidence",
        requested_identity="two-pin input connector",
        identity_mode="capability_only",
        identity_provenance="user_requirement",
    )])


def _closure(*, release_ready: bool = True) -> LibraryClosureResult:
    return LibraryClosureResult(resolutions=[ComponentResolution(
        ref="J1",
        status=ResolutionStatus.INSTALLED_EXACT,
        requested_identity="two-pin input connector",
        symbol="Connector_Generic:Conn_01x02",
        footprint="Connector:PinHeader_1x02",
        release_ready=release_ready,
        blocks_execution=not release_ready,
        reason_code="exact",
        detail="installed binding verified",
        identity_mode="capability_only",
        identity_provenance="user_requirement",
    )])


def _pin_map() -> PinMapPlan:
    return PinMapPlan(nets=[
        MappedNet(
            name="5V",
            pins=[MappedPin(ref="J1", logical="VIN", number="1")],
        ),
        MappedNet(
            name="GND",
            pins=[MappedPin(ref="J1", logical="GND", number="2")],
        ),
    ])


def test_component_manifest_binds_identity_mapping_hash_and_freshness(
    tmp_path: Path,
) -> None:
    symbol_file = tmp_path / "Connector_Generic.kicad_sym"
    footprint_file = tmp_path / "PinHeader_1x02.kicad_mod"
    symbol_file.write_text("symbol-v1", encoding="utf-8")
    footprint_file.write_text("footprint-v1", encoding="utf-8")

    manifest = build_component_closure_manifest(
        _selection(),
        _closure(),
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: [
            {"number": "1"},
            {"number": "2"},
        ],
        symbol_path=lambda _lib_id: symbol_file,
        footprint_path=lambda _lib_id: footprint_file,
    )

    assert manifest.release_ready is True
    assert manifest.blockers == []
    assert manifest.components[0].identity_provenance == "user_requirement"
    assert [
        (item.pin_number, item.pad_number)
        for item in manifest.components[0].pin_pad_bindings
    ] == [("1", "1"), ("2", "2")]
    assert all(len(item.sha256) == 64 for item in manifest.components[0].evidence)
    assert validate_component_closure_freshness(manifest).current is True

    symbol_file.write_text("symbol-v2", encoding="utf-8")
    freshness = validate_component_closure_freshness(manifest)
    assert freshness.current is False
    assert freshness.stale_evidence == [
        "J1:symbol:Connector_Generic:Conn_01x02:sha256_changed"
    ]


def test_component_manifest_blocks_incomplete_pin_pad_mapping_before_schematic(
    tmp_path: Path,
) -> None:
    symbol_file = tmp_path / "symbol.kicad_sym"
    footprint_file = tmp_path / "footprint.kicad_mod"
    symbol_file.write_text("symbol", encoding="utf-8")
    footprint_file.write_text("footprint", encoding="utf-8")

    manifest = build_component_closure_manifest(
        _selection(),
        _closure(),
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: [{"number": "1"}],
        symbol_path=lambda _lib_id: symbol_file,
        footprint_path=lambda _lib_id: footprint_file,
    )

    assert manifest.release_ready is False
    assert manifest.blockers == ["J1:pin_pad_mapping_incomplete"]


def test_optional_footprint_placeholder_becomes_repairable_missing_binding() -> None:
    part = SelectedPart(
        ref="J1",
        symbol="Connector_Generic:Conn_01x02",
        value="input",
        footprint="~",
    )
    assert part.footprint == ""

    manifest = build_component_closure_manifest(
        SelectionPlan(parts=[part]),
        _closure(),
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: None,
        symbol_path=lambda _lib_id: None,
        footprint_path=lambda _lib_id: None,
    )

    assert manifest.release_ready is False
    assert manifest.components[0].footprint_lib_id == "missing:missing"
    assert "J1:footprint_missing" in manifest.blockers


def test_kicad_xml_diff_exposes_physically_unconnected_j1_pin(
    tmp_path: Path,
) -> None:
    # KiCad exported only J1.2; J1.1 is visually labelled in the bad schematic
    # but absent from the real electrical netlist.
    netlist = tmp_path / "board.net.xml"
    netlist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<export>
  <nets>
    <net code="1" name="GND">
      <node ref="J1" pin="2" pinfunction="Pin_2" pintype="passive"/>
    </net>
  </nets>
</export>
""",
        encoding="utf-8",
    )

    expected = design_ir_pin_net_set(_pin_map())
    actual = read_kicad_xml_pin_net_set(netlist)
    result = diff_pin_net_sets(expected, actual)

    assert result.matches is False
    assert [item.model_dump() for item in result.missing] == [
        {"ref": "J1", "pin": "1", "net": "5V"}
    ]
    assert result.extra == []


def test_kicad_xml_diff_reports_wrong_net_as_missing_and_extra(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "board.net.xml"
    netlist.write_text(
        """<export><nets>
<net code="1" name="GND"><node ref="J1" pin="2"/></net>
<net code="2" name="VBUS"><node ref="J1" pin="1"/></net>
</nets></export>""",
        encoding="utf-8",
    )

    result = diff_pin_net_sets(
        design_ir_pin_net_set(_pin_map()),
        read_kicad_xml_pin_net_set(netlist),
    )

    assert [item.key() for item in result.missing] == [("J1", "1", "5V")]
    assert [item.key() for item in result.extra] == [("J1", "1", "VBUS")]


def test_kicad_xml_root_sheet_prefix_is_not_part_of_net_name(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "board.net.xml"
    netlist.write_text(
        """<export><nets>
<net code="1" name="/5V"><node ref="J1" pin="1"/></net>
<net code="2" name="/GND"><node ref="J1" pin="2"/></net>
</nets></export>""",
        encoding="utf-8",
    )

    result = diff_pin_net_sets(
        design_ir_pin_net_set(_pin_map()),
        read_kicad_xml_pin_net_set(netlist),
    )

    assert result.matches is True


def test_kicad_xml_explicit_no_connect_synthetic_net_is_not_extra(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "board.net.xml"
    netlist.write_text(
        """<export><nets>
<net code="1" name="5V"><node ref="J1" pin="1"/></net>
<net code="2" name="GND"><node ref="J1" pin="2"/></net>
<net code="3" name="unconnected-(U1-CV-Pad5)">
  <node ref="U1" pin="5" pinfunction="CV" pintype="input+no_connect"/>
</net>
</nets></export>""",
        encoding="utf-8",
    )

    result = diff_pin_net_sets(
        design_ir_pin_net_set(_pin_map()),
        read_kicad_xml_pin_net_set(netlist),
    )

    assert result.matches is True
    assert result.extra == []


def test_kicad_xml_dangling_synthetic_net_without_marker_remains_extra(
    tmp_path: Path,
) -> None:
    netlist = tmp_path / "board.net.xml"
    netlist.write_text(
        """<export><nets>
<net code="1" name="5V"><node ref="J1" pin="1"/></net>
<net code="2" name="GND"><node ref="J1" pin="2"/></net>
<net code="3" name="unconnected-(U1-CV-Pad5)">
  <node ref="U1" pin="5" pinfunction="CV" pintype="input"/>
</net>
</nets></export>""",
        encoding="utf-8",
    )

    result = diff_pin_net_sets(
        design_ir_pin_net_set(_pin_map()),
        read_kicad_xml_pin_net_set(netlist),
    )

    assert result.matches is False
    assert [item.key() for item in result.extra] == [
        ("U1", "5", "unconnected-(U1-CV-Pad5)")
    ]
