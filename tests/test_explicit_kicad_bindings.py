import json

from agents.ratsnestpro import ratsnestpro_agent, tools


def test_inline_installed_bindings_are_paired_within_each_clause(monkeypatch) -> None:
    symbols = (
        "Connector_Generic:Conn_01x02",
        "Device:R",
        "Connector:TestPoint",
    )
    footprint_ids = {
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        "Resistor_SMD:R_0603_1608Metric",
        "TestPoint:TestPoint_Plated_Hole_D2.0mm",
    }
    monkeypatch.setattr(ratsnestpro_agent.grounding, "symbol_index", lambda: symbols)
    monkeypatch.setattr(
        ratsnestpro_agent.footprints,
        "footprint_pad_numbers",
        lambda lib_id: frozenset({"1"}) if lib_id in footprint_ids else None,
    )

    requirement = (
        "J1 uses Connector_Generic:Conn_01x02 + "
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical；"
        "R1 uses Device:R + Resistor_SMD:R_0603_1608Metric；"
        "TP1 uses Connector:TestPoint + "
        "TestPoint:TestPoint_Plated_Hole_D2.0mm"
    )

    assert ratsnestpro_agent._explicit_kicad_bindings(requirement) == [
        {
            "symbol_lib_id": "Connector_Generic:Conn_01x02",
            "footprint_lib_id": ("Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"),
        },
        {
            "symbol_lib_id": "Device:R",
            "footprint_lib_id": "Resistor_SMD:R_0603_1608Metric",
        },
        {
            "symbol_lib_id": "Connector:TestPoint",
            "footprint_lib_id": "TestPoint:TestPoint_Plated_Hole_D2.0mm",
        },
    ]


def test_inline_ids_from_different_clauses_are_not_paired(monkeypatch) -> None:
    monkeypatch.setattr(
        ratsnestpro_agent.grounding,
        "symbol_index",
        lambda: ("Device:R",),
    )
    monkeypatch.setattr(
        ratsnestpro_agent.footprints,
        "footprint_pad_numbers",
        lambda lib_id: (
            frozenset({"1", "2"}) if lib_id == "Resistor_SMD:R_0603_1608Metric" else None
        ),
    )

    requirement = "symbol Device:R；footprint Resistor_SMD:R_0603_1608Metric"

    assert ratsnestpro_agent._explicit_kicad_bindings(requirement) == []


def test_zero_pin_mechanical_symbol_accepts_zero_pad_footprint(monkeypatch) -> None:
    monkeypatch.setattr(tools.symbols, "symbol_info", lambda _lib_id: {"pins": []})
    monkeypatch.setattr(
        tools.footprints,
        "footprint_pad_numbers",
        lambda _lib_id: frozenset(),
    )

    result = json.loads(
        tools.ratsnest_validate_kicad_binding(
            "Mechanical:MountingHole",
            "MountingHole:MountingHole_2.2mm_M2",
        )
    )

    assert result["status"] == "ok"
    assert result["pin_pad_compatible"] is True


def test_zero_pin_symbol_rejects_footprint_with_electrical_pad(monkeypatch) -> None:
    monkeypatch.setattr(tools.symbols, "symbol_info", lambda _lib_id: {"pins": []})
    monkeypatch.setattr(
        tools.footprints,
        "footprint_pad_numbers",
        lambda _lib_id: frozenset({"1"}),
    )

    result = json.loads(
        tools.ratsnest_validate_kicad_binding(
            "Mechanical:MountingHole",
            "Connector:OnePad",
        )
    )

    assert result["status"] == "blocked"
    assert result["pin_pad_compatible"] is False


def test_non_mechanical_zero_pin_zero_pad_binding_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(tools.symbols, "symbol_info", lambda _lib_id: {"pins": []})
    monkeypatch.setattr(
        tools.footprints,
        "footprint_pad_numbers",
        lambda _lib_id: frozenset(),
    )

    result = json.loads(
        tools.ratsnest_validate_kicad_binding(
            "Device:UnverifiedPlaceholder",
            "Package_Custom:UnverifiedPlaceholder",
        )
    )

    assert result["status"] == "blocked"
    assert result["pin_pad_compatible"] is False
    assert "zero-pin/zero-pad" in result["blockers"][0]
