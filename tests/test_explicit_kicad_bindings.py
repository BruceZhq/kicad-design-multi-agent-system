from agents.ratsnestpro import ratsnestpro_agent


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
