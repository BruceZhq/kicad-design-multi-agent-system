from types import SimpleNamespace

import pytest

import ratsnestpro.orchestration.pipeline as pipeline
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    LayoutGeneralStep,
    PcbPlacement,
    PcbPlacementPlan,
    PipelineContext,
    PipelineState,
    PipelineStep,
    Severity,
)


def test_functional_anchor_uses_role_and_connectivity_not_physical_nearness() -> None:
    anchor = pipeline._functional_anchor_ref(
        "C5",
        "ldo_output_capacitor",
        {
            "C5": "ldo_output_capacitor",
            "U1": "mcu",
            "U2": "ldo_regulator",
        },
        {
            "C5": (1.0, 1.0),
            "U1": (2.0, 1.0),
            "U2": (20.0, 10.0),
        },
        connected_refs={"C5": {"U1": 0.05, "U2": 1.0}},
        allow_connectors=True,
        eligible_anchor_refs={"U1", "U2"},
    )

    assert anchor == "U2"


def test_local_support_prefers_functional_ic_over_button_or_connector() -> None:
    anchor = pipeline._functional_anchor_ref(
        "R1",
        "reset_pullup",
        {
            "R1": "reset_pullup",
            "U1": "mcu",
            "SW1": "reset_button",
            "J1": "reset_connector",
        },
        {
            "R1": (55.5, 5.0),
            "U1": (35.0, 24.0),
            "SW1": (10.5, 4.0),
            "J1": (8.0, 4.0),
        },
        connected_refs={
            "R1": {"U1": 1.0, "SW1": 1.0, "J1": 1.0},
        },
        allow_connectors=True,
        eligible_anchor_refs={"U1", "SW1", "J1"},
    )

    assert anchor == "U1"


@pytest.mark.parametrize("role", [
    "boot0_pulldown", "mcu_vdd_decoupling", "mcu_bulk_capacitor",
    "reset_filter_capacitor",
])
def test_local_support_inherits_functional_owner_zone(monkeypatch, role) -> None:
    state = PipelineState(requirement_text="board", project_name="board")
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = pipeline.BoardPartition(
        board_width=70.0,
        board_height=45.0,
        zones=[
            pipeline.BoardZone(
                name="MCU_Core_U1",
                kind="mcu",
                target_ref="U1",
                x1=28.0,
                y1=15.0,
                x2=48.0,
                y2=30.0,
            ),
            pipeline.BoardZone(
                name="SWD_Debug_J2",
                kind="debug_interface",
                target_ref="J2",
                x1=62.0,
                y1=18.0,
                x2=69.0,
                y2=28.0,
            ),
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_roles",
        lambda _state: {
            "U1": "mcu",
            "J2": "debug_connector",
            "R2": role,
        },
    )
    monkeypatch.setattr(
        pipeline,
        "_connected_refs_by_ref",
        lambda _state: {"R2": {"U1": 1.0, "J2": 1.0}},
    )
    monkeypatch.setattr(
        pipeline,
        "_functional_anchor_refs",
        lambda _state: {"U1", "J2"},
    )

    targets, ambiguities = pipeline._resolved_zone_targets(state)

    assert targets["R2"] == (38.0, 22.5)
    assert "R2" not in ambiguities


@pytest.mark.parametrize("role", [
    "analog_filter_resistor_1", "analog_filter_capacitor_2",
    "user_button_filter_capacitor", "audio_filter_inductor", "rf_filter_choke",
])
def test_filter_passives_are_proximity_sensitive_local_support(role) -> None:
    assert pipeline._is_local_support_role(role)
    assert pipeline._is_proximity_sensitive_role(role)


@pytest.mark.parametrize("role", [
    "analog_filter_controller", "analog_filter_connector", "filterbank",
])
def test_filter_active_parts_are_not_classified_as_passive_support(role) -> None:
    assert not pipeline._is_local_support_role(role)


@pytest.mark.parametrize("explicit_filter_zone", [False, True])
def test_analog_rc_filters_use_signal_owner_unless_explicitly_zoned(
    explicit_filter_zone,
) -> None:
    state = PipelineState(requirement_text="board", project_name="board")
    parts = [
        ("U1", "mcu", "MCU:Controller"),
        ("U2", "ldo_regulator", "Regulator_Linear:LDO"),
        ("J1", "analog_input_connector", "Connector_Generic:Conn_01x03"),
        ("J2", "analog_input_connector", "Connector_Generic:Conn_01x03"),
        ("R1", "analog_filter_resistor_1", "Device:R"),
        ("R2", "analog_filter_resistor_2", "Device:R"),
        ("C1", "analog_filter_capacitor_1", "Device:C"),
        ("C2", "analog_filter_capacitor_2", "Device:C"),
    ]
    state.artifacts[PipelineStep.SELECTION] = pipeline.SelectionPlan(parts=[
        pipeline.SelectedPart(ref=ref, role=role, symbol=symbol, value=role)
        for ref, role, symbol in parts
    ])
    net_pins = [
        ("AIN1_RAW", "signal", [("J1", "3"), ("R1", "1")]),
        ("AIN2_RAW", "signal", [("J2", "3"), ("R2", "1")]),
        ("MCU_AIN1", "signal", [("R1", "2"), ("U1", "17"), ("C1", "1")]),
        ("MCU_AIN2", "signal", [("R2", "2"), ("U1", "18"), ("C2", "1")]),
        ("GND", "ground", [
            ("U1", "9"), ("U2", "2"), ("J1", "2"), ("J2", "2"),
            ("C1", "2"), ("C2", "2"),
        ]),
    ]
    state.artifacts[PipelineStep.SCH_PINMAP] = pipeline.PinMapPlan(nets=[
        pipeline.MappedNet(name=name, kind=kind, pins=[
            pipeline.MappedPin(ref=ref, logical=number, number=number)
            for ref, number in pins
        ])
        for name, kind, pins in net_pins
    ])
    zones = [
        pipeline.BoardZone(
            name="mcu", kind="processor", target_ref="U1",
            x1=16.0, y1=8.0, x2=38.0, y2=28.0,
        ),
        pipeline.BoardZone(
            name="power_regulation", kind="power", target_ref="U2",
            x1=6.0, y1=28.0, x2=20.0, y2=40.0,
        ),
        pipeline.BoardZone(
            name="analog_input_1", kind="analog_input", target_ref="J1",
            x1=0.0, y1=8.0, x2=12.0, y2=18.0,
        ),
        pipeline.BoardZone(
            name="analog_input_2", kind="analog_input", target_ref="J2",
            x1=0.0, y1=18.0, x2=12.0, y2=28.0,
        ),
    ]
    if explicit_filter_zone:
        zones.append(pipeline.BoardZone(
            name="required_filter_position", kind="analog", target_ref="R1",
            x1=4.0, y1=8.0, x2=12.0, y2=18.0,
        ))
    state.artifacts[PipelineStep.LAYOUT_PARTITION] = pipeline.BoardPartition(
        board_width=70.0, board_height=45.0, zones=zones,
    )

    targets, ambiguities = pipeline._resolved_zone_targets(state)

    assert not ambiguities
    assert targets["R1"] == ((8.0, 13.0) if explicit_filter_zone else targets["U1"])
    for ref in ("R2", "C1", "C2"):
        assert targets[ref] == targets["U1"] == (27.0, 18.0)
    assert targets["J1"] == (6.0, 13.0)
    assert targets["J2"] == (6.0, 23.0)


def test_layout_general_repairs_local_support_before_repacking(monkeypatch) -> None:
    step = LayoutGeneralStep()
    state = PipelineState(requirement_text="board", project_name="board")
    original = PcbPlacementPlan(
        board_width=70.0,
        board_height=50.0,
        placements=[
            PcbPlacement(ref="U1", x=10.0, y=25.0),
            PcbPlacement(ref="R1", x=60.0, y=25.0),
        ],
        rationale="baseline",
    )
    monkeypatch.setattr(
        pipeline,
        "_roles",
        lambda _state: {"U1": "mcu", "R1": "reset_pullup"},
    )
    monkeypatch.setattr(
        pipeline,
        "_footprints_of",
        lambda _state: {"U1": "", "R1": ""},
    )
    monkeypatch.setattr(
        pipeline,
        "_connected_refs_by_ref",
        lambda _state: {"R1": {"U1": 1.0}, "U1": {"R1": 1.0}},
    )
    monkeypatch.setattr(
        pipeline,
        "_functional_anchor_refs",
        lambda _state: {"U1"},
    )
    monkeypatch.setattr(
        step,
        "propose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local support repair must run before full repacking")
        ),
    )

    checks = step.check(state, original)
    assert any(
        check.name == "local_support_near_anchor" and not check.ok
        for check in checks
    )

    repaired, used_llm = step.repair(
        state,
        PipelineContext(),
        "",
        original,
        checks,
    )

    assert repaired.by_ref()["R1"] != original.by_ref()["R1"]
    assert not any(
        not check.ok and check.severity == Severity.ERROR
        for check in step.check(state, repaired)
    )
    assert used_llm is False


def test_maxrect_emits_functional_anchor_before_large_dependent(
    monkeypatch,
) -> None:
    boxes = {
        "large-dependent": (0.0, 0.0, 10.0, 10.0),
        "small-anchor": (0.0, 0.0, 2.0, 2.0),
        "other": (0.0, 0.0, 4.0, 4.0),
    }
    monkeypatch.setattr(
        pipeline,
        "_placement_bbox",
        lambda footprint: boxes[footprint],
    )
    monkeypatch.setattr(
        pipeline.config,
        "process_capability",
        lambda: SimpleNamespace(min_board_edge_clearance=0.5),
    )

    placements, unplaced = pipeline._maxrect_pack(
        ["large-dependent", "small-anchor", "other"],
        {
            "large-dependent": "large-dependent",
            "small-anchor": "small-anchor",
            "other": "other",
        },
        40.0,
        30.0,
        0.2,
        dependency={"large-dependent": "small-anchor"},
    )

    assert not unplaced
    refs = [placement.ref for placement in placements]
    assert refs.index("small-anchor") < refs.index("large-dependent")


def test_proximity_repair_repackages_cluster_when_single_body_move_stagnates(
    monkeypatch,
) -> None:
    step = LayoutGeneralStep()
    state = PipelineState(requirement_text="board", project_name="board")
    original = PcbPlacementPlan(
        board_width=40.0,
        board_height=30.0,
        placements=[PcbPlacement(ref="C1", x=5.0, y=5.0)],
        rationale="baseline",
    )
    repacked = original.model_copy(update={"rationale": "cluster repacked"})
    failure = CheckResult(
        name="decoupling_near_mcu",
        ok=False,
        severity=Severity.ERROR,
        message="dependent remains outside its real-pad distance limit",
    )
    monkeypatch.setattr(
        pipeline,
        "_repair_proximity_placements",
        lambda _state, artifact: artifact,
    )
    monkeypatch.setattr(step, "check", lambda _state, _artifact: [failure])
    monkeypatch.setattr(
        step,
        "propose",
        lambda _state, _ctx, _knowledge: (repacked, False),
    )

    repaired, used_llm = step.repair(
        state,
        PipelineContext(),
        "",
        original,
        [failure],
    )

    assert repaired is repacked
    assert used_llm is False
