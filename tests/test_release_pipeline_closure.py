from __future__ import annotations

from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.orchestration.component_resolution import (
    _compatible_pin_pad_sets,
)
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineState,
    PipelineStep,
    _bounded_selection_patch,
    _explicit_requested_layer_count,
    _normalize_plane_plan,
    _requested_layer_count,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    MappedNet,
    MappedPin,
    PinMapPlan,
    PlanePlan,
    RoutePlan,
    SelectedPart,
    SelectionPatch,
    SelectionPlan,
    TopologyBlock,
    TopologyPlan,
)


def _part(ref: str, role: str) -> SelectedPart:
    mounting = "mounting" in role
    return SelectedPart(
        ref=ref,
        symbol="Mechanical:MountingHole" if mounting else "Device:C",
        value="M2 NPTH" if mounting else "100nF",
        footprint=(
            "MountingHole:MountingHole_2.2mm_M2"
            if mounting
            else "Capacitor_SMD:C_0603_1608Metric"
        ),
        role=role,
    )


def test_selection_ahe_cannot_delete_required_mounting_holes() -> None:
    state = PipelineState(
        requirement_text="四角各放置一个 M2 非金属化安装孔。",
        project_name="guarded-selection",
    )
    plan = SelectionPlan(
        parts=[_part(f"H{index}", "mechanical_mounting_hole") for index in range(1, 5)]
    )
    patch = SelectionPatch(
        remove_refs=["H1", "H2", "H3", "H4"],
        rationale="remove failures",
    )
    checks = [
        CheckResult(
            name="component_library_closure",
            ok=False,
            affected_refs=["H1", "H2", "H3", "H4"],
        )
    ]

    bounded = _bounded_selection_patch(state, plan, patch, checks)

    assert bounded.remove_refs == []


def test_component_closure_allows_zero_pin_only_for_real_mechanics() -> None:
    assert _compatible_pin_pad_sets(
        "Mechanical:MountingHole",
        "MountingHole:MountingHole_2.2mm_M2",
        set(),
        set(),
    )
    assert not _compatible_pin_pad_sets(
        "Device:UnverifiedPlaceholder",
        "Package_Custom:UnverifiedPlaceholder",
        set(),
        set(),
    )


def test_selection_ahe_cannot_delete_sole_topology_implementation() -> None:
    state = PipelineState(
        requirement_text="J1 is the two-pin 5V power input.",
        project_name="guarded-topology",
    )
    state.artifacts[PipelineStep.TOPOLOGY] = TopologyPlan(
        blocks=[
            TopologyBlock(
                name="J1_PWR_IN",
                kind="power_input",
                implementation_kind="component",
                implementation_refs=["J1"],
            )
        ]
    )
    j1 = SelectedPart(
        ref="J1",
        symbol="Connector_Generic:Conn_01x02",
        value="5V input",
        footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        role="power_input_connector",
    )
    plan = SelectionPlan(parts=[j1])
    patch = SelectionPatch(remove_refs=["J1"], rationale="remove invalid part")
    checks = [
        CheckResult(
            name="component_library_closure",
            ok=False,
            affected_refs=["J1"],
        )
    ]

    bounded = _bounded_selection_patch(state, plan, patch, checks)

    assert bounded.remove_refs == []


def _pin(ref: str, number: str) -> MappedPin:
    return MappedPin(ref=ref, logical=number, number=number)


def test_plane_plan_discards_template_nets_and_preserves_two_layer_ground() -> None:
    state = PipelineState(
        requirement_text="必须使用两层板，底层做连续 GND 铺铜。",
        project_name="grounded-plane",
    )
    state.artifacts[PipelineStep.SCH_PINMAP] = PinMapPlan(
        nets=[
            MappedNet(name="GND", kind="ground", pins=[_pin("U1", "1"), _pin("C1", "2")]),
            MappedNet(name="5V", kind="power", pins=[_pin("U1", "8"), _pin("C1", "1")]),
            MappedNet(name="OUT", kind="signal", pins=[_pin("U1", "3"), _pin("J1", "1")]),
        ]
    )
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(layers=2)
    proposal = PlanePlan(
        ground_net="GND",
        planes=["In1.Cu:GND", "In2.Cu:5V"],
        critical_nets=["CRYSTAL_XIN", "USB_DP", "5V"],
        rationale="Use a 4-layer template.",
    )

    normalized = _normalize_plane_plan(state, proposal)

    assert normalized.planes[0] == "B.Cu:GND"
    assert all(not declaration.startswith("In") for declaration in normalized.planes)
    assert normalized.critical_nets == ["5V"]
    assert "4-layer" not in normalized.rationale


def test_route_stackup_cannot_be_escalated_by_conflicting_hitl_patch() -> None:
    requirement = (
        "板子必须是双层，尺寸不超过 40 mm × 30 mm。\n"
        "DECISION: layer_count=B — For layer_count, the user confirmed: "
        "4 copper layers."
    )

    assert _explicit_requested_layer_count(requirement) == 2
    assert _requested_layer_count(requirement) == 2


def test_pcb_round_trip_exposes_real_zone_and_track_width(tmp_path) -> None:
    pcb_path = tmp_path / "physical.kicad_pcb"
    board = PcbBoard.blank()
    board.add_net("GND")
    board.add_zone("B.Cu", "GND", [(0, 0), (20, 0), (20, 10), (0, 10)])
    board.add_track(1, 1, 5, 1, width=0.3, layer="B.Cu", net="GND")
    board.save(pcb_path)

    reloaded = PcbBoard.load(pcb_path)

    assert reloaded.list_zones() == [
        {
            "net_index": 1,
            "net": "GND",
            "layer": "B.Cu",
            "points": [[0.0, 0.0], [20.0, 0.0], [20.0, 10.0], [0.0, 10.0]],
            "filled_polygons": [],
        }
    ]
    assert reloaded.list_tracks()[0]["net_name"] == "GND"
    assert reloaded.list_tracks()[0]["width"] == 0.3
