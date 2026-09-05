from __future__ import annotations

from ratsnestpro.orchestration.entity_repairs import (
    EntityRepairCategory,
    RepairExecutionPolicy,
    classify_kicad_finding,
    classify_kicad_report,
)


def _item(description: str, x: float, y: float) -> dict:
    return {
        "description": description,
        "pos": {"x": x, "y": y},
    }


def test_pin_not_connected_targets_schematic_connectivity() -> None:
    plan = classify_kicad_finding({
        "type": "pin_not_connected",
        "description": "Pin 2 of J1 is not connected",
        "items": [_item("Pin 2 of J1", 25.4, 30.48)],
    })

    assert plan.category == EntityRepairCategory.SCHEMATIC_CONNECTIVITY
    assert plan.rollback_step == "schematic_materialize"
    assert plan.affected_refs == ["J1"]
    assert [(pin.ref, pin.number) for pin in plan.affected_pins] == [("J1", "2")]
    assert plan.execution_policy == RepairExecutionPolicy.BOUNDED_CANDIDATE


def test_pin_type_conflict_preserves_items_and_targets_design_ir_owner() -> None:
    plan = classify_kicad_finding({
        "type": "pin_to_pin",
        "description": "Pins of type Output and Power output are connected",
        "items": [
            _item("Symbol U3 Pin 7 [SDO, Output, Line]", 0.3175, 0.8128),
            _item(
                "Symbol #PWR01 Pin 1 [Power output, Line]",
                0.1143,
                0.3556,
            ),
        ],
    })

    assert plan.category == EntityRepairCategory.SCHEMATIC_CONNECTIVITY
    assert plan.rollback_step == "schematic_connections"
    assert plan.strategy == "repair_pin_electrical_conflict_in_design_ir"
    assert plan.affected_refs == ["U3", "#PWR01"]
    assert [(pin.ref, pin.number) for pin in plan.affected_pins] == [
        ("U3", "7"),
        ("#PWR01", "1"),
    ]
    assert [item.description for item in plan.observed_items] == [
        "Symbol U3 Pin 7 [SDO, Output, Line]",
        "Symbol #PWR01 Pin 1 [Power output, Line]",
    ]
    assert plan.execution_policy == RepairExecutionPolicy.BOUNDED_CANDIDATE


def test_same_footprint_short_targets_footprint_geometry() -> None:
    plan = classify_kicad_finding({
        "type": "shorting_items",
        "description": "Items shorting two nets",
        "items": [
            _item("Pad 1 [GND] of U1 on F.Cu", 10.0, 20.0),
            _item("Pad 2 [VCC] of U1 on F.Cu", 10.4, 20.0),
        ],
    })

    assert plan.category == EntityRepairCategory.FOOTPRINT_GEOMETRY
    assert plan.rollback_step == "layout_write"
    assert plan.affected_refs == ["U1"]
    assert [(pad.ref, pad.number) for pad in plan.affected_pads] == [
        ("U1", "1"),
        ("U1", "2"),
    ]
    assert [position.layer for position in plan.positions] == ["F.Cu", "F.Cu"]
    assert plan.execution_policy == RepairExecutionPolicy.BOUNDED_CANDIDATE


def test_same_footprint_mask_bridge_uses_the_same_geometry_loop() -> None:
    plan = classify_kicad_finding({
        "type": "solder_mask_bridge",
        "items": [
            _item("Pad 3 of U2 on F.Mask", 1.0, 2.0),
            _item("Pad 4 of U2 on F.Mask", 1.2, 2.0),
        ],
    })

    assert plan.category == EntityRepairCategory.FOOTPRINT_GEOMETRY
    assert plan.strategy == "repair_or_substitute_verified_footprint_geometry"


def test_cross_footprint_clearance_targets_layout() -> None:
    plan = classify_kicad_finding({
        "type": "clearance",
        "items": [
            _item("Pad 1 [3V3] of U1 on F.Cu", 4.0, 5.0),
            _item("Pad 1 [GND] of C1 on F.Cu", 4.1, 5.0),
        ],
    })

    assert plan.category == EntityRepairCategory.LAYOUT
    assert plan.rollback_step == "layout_general"
    assert plan.affected_refs == ["U1", "C1"]


def test_tracks_crossing_targets_clean_route_snapshot() -> None:
    plan = classify_kicad_finding({
        "type": "tracks_crossing",
        "items": [
            _item("Track [SCL] on F.Cu", 6.0, 7.0),
            _item("Track [SDA] on F.Cu", 6.0, 7.0),
        ],
    })

    assert plan.category == EntityRepairCategory.ROUTING
    assert plan.rollback_step == "route_signals"
    assert plan.execution_policy == RepairExecutionPolicy.BOUNDED_CANDIDATE


def test_silkscreen_finding_targets_real_silk_entities() -> None:
    plan = classify_kicad_finding({
        "type": "silk_overlap",
        "items": [_item("Reference text of R12 on F.Silkscreen", 8.0, 9.0)],
    })

    assert plan.category == EntityRepairCategory.SILKSCREEN
    assert plan.rollback_step == "layout_write"
    assert plan.affected_refs == ["R12"]


def test_unconnected_report_section_targets_zone_and_routing() -> None:
    plans = classify_kicad_report({
        "unconnected_items": [{
            "type": "unconnected_items",
            "items": [
                _item("Pad 1 [GND] of J1 on F.Cu", 1.0, 1.0),
                _item("Pad 2 [GND] of J2 on F.Cu", 2.0, 2.0),
            ],
        }]
    })

    assert len(plans) == 1
    assert plans[0].category == EntityRepairCategory.ZONE_ROUTING
    assert plans[0].rollback_step == "route_signals"
    assert plans[0].source_section == "unconnected_items"


def test_unknown_or_under_specified_findings_fail_closed() -> None:
    unknown = classify_kicad_finding({"type": "new_future_rule"})
    under_specified = classify_kicad_finding({"type": "shorting_items"})

    assert unknown.category == EntityRepairCategory.UNCLASSIFIED
    assert unknown.strategy == "no_automatic_repair"
    assert unknown.execution_policy == RepairExecutionPolicy.MANUAL_REVIEW
    assert under_specified.execution_policy == RepairExecutionPolicy.MANUAL_REVIEW
