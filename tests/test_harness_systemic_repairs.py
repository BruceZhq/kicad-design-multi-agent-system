from __future__ import annotations

import json
from pathlib import Path

from agents.ratsnestpro import ratsnestpro_agent, tools
from agents.ratsnestpro.ehe_memory import EheMemory
from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.orchestration import connection_synthesis as connection_module
from ratsnestpro.orchestration import pipeline as pipeline_module
from ratsnestpro.orchestration.ahe import (
    FailureOrigin,
    Recoverability,
    make_capability_gap,
    make_failure,
)
from ratsnestpro.orchestration.component_resolution import (
    ComponentResolution,
    ComponentResolutionService,
    LibraryClosureResult,
    ResolutionStatus,
)
from ratsnestpro.orchestration.connection_synthesis import (
    ConnectionSynthesisCheckpoint,
    assign_parts_to_topology_blocks,
    new_connection_checkpoint,
    plan_connection_batches,
    prepare_resumable_connection_checkpoint,
    topology_fingerprint,
)
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineContext,
    PipelineState,
    SchConnectionsStep,
    SelectionStep,
    _ConnectivityView,
    _diode_polarity_checks,
    _external_input_protection_topology_checks,
    _library_closure_check,
    _library_closure_diagnostics,
    _mcu_control_topology_checks,
    _repairable_selection_refs,
    _specific_component_identity_error,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    GroundTieContract,
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
    TopologyBlock,
    TopologyPlan,
)
from service.governance_scope import TrustedGovernanceScope


def _component_service() -> ComponentResolutionService:
    return ComponentResolutionService(
        resolve_symbol=lambda _lib_id: object(),
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "K", "type": "passive"},
            {"number": "2", "name": "A", "type": "passive"},
        ],
        symbol_properties=lambda _lib_id: {"Value": "D"},
        footprint_pads=lambda _lib_id: [
            {"number": "1"},
            {"number": "2"},
        ],
        symbol_index=lambda: ("Device:D",),
    )


def test_generic_diode_symbol_closes_capability_identity_but_not_fixed_exact() -> None:
    proposed = SelectedPart(
        ref="D1",
        symbol="Device:D",
        value="SS14",
        footprint="Diode_SMD:D_SOD-123",
        role="flyback_diode",
    )

    capability = _component_service().resolve(
        proposed.model_copy(deep=True),
        trusted_identity_mode="capability_only",
    )
    fixed = _component_service().resolve(
        proposed.model_copy(deep=True),
        fixed_identity=True,
    )

    assert capability.release_ready
    assert capability.reason_code == "generic_primitive"
    assert fixed.status == ResolutionStatus.UNRESOLVED_EVIDENCE_GAP
    assert fixed.reason_code == "device_identity_mismatch"
    assert fixed.blocks_execution


def test_generic_capability_identity_regression_is_a_harness_failure(
    monkeypatch,
) -> None:
    service = _component_service()
    proposed = SelectedPart(
        ref="D1",
        symbol="Device:D",
        value="generic Schottky diode",
        footprint="Diode_SMD:D_SOD-123",
        role="flyback_diode",
    )
    # Simulate a regression in identity closure while retaining the real
    # installed-symbol and compatible pin/pad checks in _installed_resolution.
    monkeypatch.setattr(service, "_identity_relation", lambda *_args: None)

    resolution = service.resolve(
        proposed,
        trusted_identity_mode="capability_only",
    )

    assert resolution.status == ResolutionStatus.HARNESS_FAILURE
    assert resolution.reason_code == "generic_capability_closure_contradiction"
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.category == "harness_failure"


def test_reverse_protection_keeps_generic_identity_without_relabeling(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_properties",
        lambda _lib_id: {
            "Value": "D_Schottky",
            "Description": "Schottky diode",
        },
    )
    generic = SelectedPart(
        ref="D1",
        symbol="Device:D_Schottky",
        value="D_Schottky",
        footprint="Diode_SMD:D_SMA",
        role="reverse_polarity_protection",
    )
    relabeled = generic.model_copy(update={"value": "SS14"})

    assert _specific_component_identity_error(generic, "5 V input") is None
    assert "select the real device" in (
        _specific_component_identity_error(relabeled, "5 V input") or ""
    )


def test_reverse_protection_role_is_part_of_external_input_series_chain(
    monkeypatch,
) -> None:
    pin_map = {
        "Connector_Generic:Conn_01x02": [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        "Device:Polyfuse": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D_Schottky": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
        "Regulator_Linear:AP1117-33": [
            {"number": "1", "name": "GND"},
            {"number": "2", "name": "VO"},
            {"number": "3", "name": "VI"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="J1", symbol="Connector_Generic:Conn_01x02",
            value="Conn_01x02", footprint="Connector:Header",
            role="power_input_connector",
        ),
        SelectedPart(
            ref="F1", symbol="Device:Polyfuse", value="Polyfuse",
            footprint="Fuse:Fuse_1206_3216Metric", role="input_ptc_fuse",
        ),
        SelectedPart(
            ref="D1", symbol="Device:D_Schottky", value="D_Schottky",
            footprint="Diode_SMD:D_SMA", role="reverse_protection_diode",
        ),
        SelectedPart(
            ref="U2", symbol="Regulator_Linear:AP1117-33",
            value="AP1117-33", footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            role="ldo_regulator",
        ),
    ])
    intent = NetlistIntent(
        supply_nets=["VIN_5V", "5V_FUSED", "5V_REG", "3V3"],
        ground_net="GND",
        nets=[
            NetIntent(name="VIN_5V", kind="power", pins=[
                LogicalPin(ref="J1", pin="1"), LogicalPin(ref="F1", pin="1"),
            ]),
            NetIntent(name="5V_FUSED", kind="power", pins=[
                LogicalPin(ref="F1", pin="2"), LogicalPin(ref="D1", pin="2"),
            ]),
            NetIntent(name="5V_REG", kind="power", pins=[
                LogicalPin(ref="D1", pin="1"), LogicalPin(ref="U2", pin="3"),
            ]),
            NetIntent(name="3V3", kind="power", pins=[
                LogicalPin(ref="U2", pin="2"),
            ]),
            NetIntent(name="GND", kind="ground", pins=[
                LogicalPin(ref="J1", pin="2"), LogicalPin(ref="U2", pin="1"),
            ]),
        ],
    )

    checks = _external_input_protection_topology_checks(
        _ConnectivityView.build(selection, intent)
    )

    assert len(checks) == 1
    assert checks[0].ok, checks[0].message


def test_mixed_library_closure_does_not_relabel_design_failure_as_harness() -> None:
    closure = LibraryClosureResult(resolutions=[
        ComponentResolution(
            ref="D1",
            status=ResolutionStatus.HARNESS_FAILURE,
            requested_identity="generic diode",
            symbol="Device:D",
            footprint="Diode_SMD:D_SOD-123",
            release_ready=False,
            blocks_execution=True,
            reason_code="generic_capability_closure_contradiction",
            detail="validated generic closure was contradicted",
        ),
        ComponentResolution(
            ref="U1",
            status=ResolutionStatus.UNRESOLVED_EVIDENCE_GAP,
            requested_identity="fixed exact device",
            symbol="MCU_Test:Unknown",
            footprint="Package_QFP:QFP",
            release_ready=False,
            blocks_execution=True,
            reason_code="symbol_not_installed",
            detail="fixed device has no installed symbol evidence",
        ),
    ])

    aggregate = _library_closure_check(closure)
    diagnostics = _library_closure_diagnostics(closure)

    assert aggregate.origin is None
    assert aggregate.reason_code == "component_resolution_incomplete"
    harness_checks = [
        check
        for check in diagnostics
        if check.origin == FailureOrigin.HARNESS
    ]
    assert len(harness_checks) == 1
    assert (
        harness_checks[0].name
        == "harness_consistency:generic_capability_closure"
    )
    assert harness_checks[0].reason_code == "generic_capability_closure_contradiction"
    assert all(
        check.origin is None
        for check in diagnostics
        if check.name == "symbol:U1"
    )


def test_placeholder_components_reenter_bounded_selection_repair() -> None:
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="D1",
            symbol="RatsNestPlaceholder:UNRESOLVED_PART",
            value="unresolved diode",
            footprint="Diode_SMD:D_SOD-123",
            role="flyback_diode",
            resolution_status=(
                ResolutionStatus.PLACEHOLDER_UNVERIFIED_NONRELEASE.value
            ),
            unresolved=True,
            dnp=True,
        )
    ])

    refs = _repairable_selection_refs(
        selection,
        [
            CheckResult(
                name="component_library_closure",
                ok=False,
                message="selection contains a nonrelease placeholder",
            )
        ],
    )

    assert refs == {"D1"}


def _control_selection() -> SelectionPlan:
    return SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="MCU_Test:Controller",
            value="controller",
            footprint="Package_QFP:QFP",
            role="mcu",
        ),
        SelectedPart(
            ref="R1",
            symbol="Device:R",
            value="10k",
            footprint="Resistor_SMD:R_0603_1608Metric",
            role="boot0_pulldown",
        ),
        SelectedPart(
            ref="D1",
            symbol="Device:D",
            value="generic schottky",
            footprint="Diode_SMD:D_SOD-123",
            role="inductive_load_flyback_diode",
        ),
    ])


def _control_intent(
    *,
    reverse_diode: bool = False,
    boot_pin: str = "PA14",
    pulldown_to_supply: bool = False,
) -> NetlistIntent:
    diode_supply_pin = "A" if reverse_diode else "K"
    diode_switch_pin = "K" if reverse_diode else "A"
    return NetlistIntent(
        supply_nets=["3V3", "5V"],
        ground_net="GND",
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[LogicalPin(ref="U1", pin="VDD")],
            ),
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="VSS"),
                    *(
                        []
                        if pulldown_to_supply
                        else [LogicalPin(ref="R1", pin="2")]
                    ),
                ],
            ),
            NetIntent(
                name="SWCLK_BOOT0",
                pins=[
                    LogicalPin(ref="U1", pin=boot_pin),
                    LogicalPin(ref="R1", pin="1"),
                ],
            ),
            NetIntent(
                name="5V",
                kind="power",
                pins=[
                    LogicalPin(ref="D1", pin=diode_supply_pin),
                    *(
                        [LogicalPin(ref="R1", pin="2")]
                        if pulldown_to_supply
                        else []
                    ),
                ],
            ),
            NetIntent(
                name="LOAD_SW",
                pins=[LogicalPin(ref="D1", pin=diode_switch_pin)],
            ),
        ],
    )


def _architect_requirement() -> str:
    evidence = {
        "evidence_contract": {
            "schema_version": 1,
            "producer": "architect_phase",
        },
        "verified_pin_aliases": [
            {
                "symbol_lib_id": "MCU_Test:Controller",
                "pin_number": "5",
                "symbol_pin_name": "PA14",
                "aliases": ["BOOT0"],
                "evidence_ids": ["https://manufacturer.example/device.pdf#page=42"],
            }
        ],
    }
    return (
        "build a controller\n\nGROUNDED ARCHITECT EVIDENCE — contract:\n"
        + json.dumps(evidence)
    )


def test_topology_ownership_respects_typed_kind_and_migrates_legacy_star() -> None:
    component = TopologyBlock(
        name="R5_ground_star_tie",
        kind="ground_star",
        description="R5 sits between the GND and PGND copper pours",
        implementation_kind="component",
        implementation_refs=["R5"],
    )
    inferred_zone = TopologyBlock(
        name="logic_ground_pour",
        kind="ground_plane",
        description="GND copper pour and zone",
        implementation_kind="auto",
    )
    legacy_star = component.model_copy(
        update={"implementation_kind": "copper_zone"}
    )

    assert pipeline_module._topology_implementation_kind(component) == "component"
    assert pipeline_module._topology_implementation_kind(inferred_zone) == "copper_zone"
    migrated = pipeline_module._normalize_topology_plan(
        TopologyPlan(
            blocks=[legacy_star],
            rails=["3V3"],
            ground_net="GND",
        ),
        recover_legacy_ground_star=True,
    )
    assert migrated.blocks[0].implementation_kind == "component"
    assert migrated.ground_domains == ["GND", "PGND"]
    assert migrated.ground_ties == [
        GroundTieContract(component_ref="R5", domains=["GND", "PGND"])
    ]
    assert topology_fingerprint(migrated) != topology_fingerprint(
        TopologyPlan(
            blocks=[legacy_star],
            rails=["3V3"],
            ground_net="GND",
        )
    )
    assert pipeline_module.TopologyStep().resumed_artifact_migration_is_safe(
        TopologyPlan(
            blocks=[legacy_star],
            rails=["3V3"],
            ground_net="GND",
        ),
        migrated,
    )
    assert not pipeline_module._looks_like_ground_net_name("GND_SENSE")


def test_v1_connection_checkpoint_upgrades_only_when_partition_is_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        connection_module.symbols,
        "symbol_pins",
        lambda _lib_id: [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
    )
    topology = pipeline_module._normalize_topology_plan(TopologyPlan(
        blocks=[TopologyBlock(name="passives", kind="passive")],
        rails=["3V3"],
        ground_net="GND",
    ))
    selection = SelectionPlan(parts=[
        SelectedPart(ref="R1", symbol="Device:R", value="1k", role="resistor"),
        SelectedPart(ref="R2", symbol="Device:R", value="2k", role="resistor"),
    ])
    current_plan = plan_connection_batches(
        topology,
        selection,
        target_pin_count=2,
        max_batches=4,
    )
    current = new_connection_checkpoint(topology, selection, current_plan)
    legacy_hash = connection_module._legacy_topology_fingerprint(topology)
    legacy_plan = current.plan.model_dump(mode="json")
    legacy_plan["topology_fingerprint"] = legacy_hash
    legacy_plan["plan_fingerprint"] = connection_module._canonical_hash(
        connection_module._plan_fingerprint_payload(
            topology_hash=legacy_hash,
            selection_hash=legacy_plan["selection_fingerprint"],
            target_pin_count=legacy_plan["target_pin_count"],
            effective_target_pin_count=legacy_plan["effective_target_pin_count"],
            max_batches=legacy_plan["max_batches"],
            shared_refs=legacy_plan["shared_refs"],
            oversized_atomic_refs=legacy_plan["oversized_atomic_refs"],
            batching_supported=legacy_plan["batching_supported"],
            batches=current.plan.batches,
        )
    )
    legacy_payload = current.model_dump(mode="json")
    legacy_payload.update({
        "schema_version": 1,
        "topology_fingerprint": legacy_hash,
        "plan": legacy_plan,
    })
    legacy = ConnectionSynthesisCheckpoint.model_validate(legacy_payload)

    upgraded = prepare_resumable_connection_checkpoint(
        legacy,
        topology,
        selection,
    )

    assert upgraded is not None
    assert upgraded.schema_version == 2
    assert upgraded.batches == legacy.batches
    assert upgraded.aggregate == legacy.aggregate

    changed_topology = topology.model_copy(update={
        "ground_domains": ["GND", "AGND"],
        "ground_ties": [GroundTieContract(
            component_ref="R1",
            domains=["GND", "AGND"],
        )],
    })
    assert prepare_resumable_connection_checkpoint(
        legacy,
        changed_topology,
        selection,
    ) is None


def test_materializer_drives_each_isolated_ground_domain() -> None:
    document = materialize_pinmapped(
        components=[{
            "ref": "J1",
            "symbol": "Connector_Generic:Conn_01x02",
            "value": "ground-domain fixture",
            "footprint": "",
            "x": 20,
            "y": 20,
            "rotation": 0,
            "release_ready": True,
            "resolution_status": "installed_exact",
        }],
        nets=[
            {"name": "GND", "pins": [{"ref": "J1", "number": "1"}]},
            {"name": "PGND", "pins": [{"ref": "J1", "number": "2"}]},
        ],
        ground_nets=["GND", "PGND"],
    )

    assert sum(
        component.get("value") == "PWR_FLAG"
        for component in document.components()
    ) == 2


def test_flattened_pinout_aliases_collapse_to_unique_composite_pin() -> None:
    aliases = [
        {
            "symbol_lib_id": "MCU_Test:Controller",
            "pin_number": number,
            "symbol_pin_name": name,
            "aliases": ["BOOT0", "NRST"],
            "evidence_ids": ["https://manufacturer.example/device.pdf#page=30"],
        }
        for number, name in (("5", "PA14"), ("6", "PB2"), ("7", "PF0"))
    ]
    evidence = {
        "evidence_contract": {"schema_version": 1, "producer": "architect_phase"},
        "verified_pin_aliases": aliases,
        "datasheet": {
            "matched_pages": [{
                "page": 30,
                "text": "PB2 PF0 PA15 PA14-BOOT0 PA13 NRST LQFP64",
            }],
        },
    }
    requirement = (
        "build a controller\n\nGROUNDED ARCHITECT EVIDENCE — contract:\n"
        + json.dumps(evidence)
    )

    verified = pipeline_module._verified_pin_aliases(requirement)

    assert [
        (item.pin_number, item.symbol_pin_name, item.aliases)
        for item in verified
    ] == [("5", "PA14", ["BOOT0"])]


def test_control_normalizer_rewires_boot_pulldown_to_verified_pin(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "6", "name": "PB2"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(pipeline_module.symbols, "symbol_pins", lambda lib_id: pin_map[lib_id])
    intent = _control_intent(boot_pin="PB2")
    intent.nets.append(NetIntent(
        name="SWCLK",
        pins=[LogicalPin(ref="U1", pin="PA14")],
    ))

    repaired = pipeline_module._normalize_control_support(
        _architect_requirement(),
        _control_selection(),
        intent,
    )
    view = _ConnectivityView.build(_control_selection(), repaired)

    assert view.part_nets(_control_selection().parts[1]) == {"GND", "SWCLK"}


def test_control_normalizer_binds_unconnected_verified_boot_pin(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "6", "name": "PB2"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(
        pipeline_module.symbols,
        "symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    intent = _control_intent(boot_pin="PB2")
    intent.net("SWCLK_BOOT0").name = "BOOT0"  # type: ignore[union-attr]

    repaired = pipeline_module._normalize_control_support(
        _architect_requirement(),
        _control_selection(),
        intent,
    )
    view = _ConnectivityView.build(_control_selection(), repaired)

    assert view.pin_nets[("U1", "5")] == "BOOT0"
    assert view.pin_nets.get(("U1", "6")) != "BOOT0"
    assert view.part_nets(_control_selection().parts[1]) == {"BOOT0", "GND"}


def test_split_ground_star_is_normalized_and_gated(monkeypatch) -> None:
    pin_map = {
        "Test:Logic": [{"number": "1", "name": "VSS"}],
        "Test:Motor": [{"number": "1", "name": "PGND"}],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
    }
    monkeypatch.setattr(
        pipeline_module.symbols,
        "symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U1",
            symbol="Test:Logic",
            value="logic",
            footprint="Test:Logic",
            role="mcu",
        ),
        SelectedPart(
            ref="U2",
            symbol="Test:Motor",
            value="motor",
            footprint="Test:Motor",
            role="motor_driver",
        ),
        SelectedPart(
            ref="R5",
            symbol="Device:R",
            value="0R",
            footprint="Resistor_SMD:R_0805_2012Metric",
            role="ground_star_net_tie",
        ),
    ])
    topology = TopologyPlan(
        blocks=[TopologyBlock(
            name="R5_ground_star_tie",
            kind="ground_star",
            description="R5 pin 1 is GND and pin 2 is PGND",
            implementation_kind="component",
            implementation_refs=["R5"],
        )],
        rails=["3V3", "VMOT"],
        ground_net="GND",
        ground_domains=["GND", "PGND"],
        ground_ties=[GroundTieContract(
            component_ref="R5",
            domains=["GND", "PGND"],
        )],
    )
    intent = NetlistIntent(
        ground_net="GND",
        nets=[
            NetIntent(
                name="GND",
                kind="ground",
                pins=[LogicalPin(ref="U1", pin="VSS")],
            ),
            NetIntent(
                name="PGND",
                kind="signal",
                pins=[LogicalPin(ref="U2", pin="PGND")],
            ),
        ],
    )

    repaired = pipeline_module._normalize_ground_star_ties(
        selection,
        intent,
        topology,
    )
    view = _ConnectivityView.build(selection, repaired)
    checks = pipeline_module._ground_star_topology_checks(
        selection,
        repaired,
        topology,
    )

    assert view.part_nets(selection.parts[2]) == {"GND", "PGND"}
    assert repaired.net("PGND").kind == "ground"  # type: ignore[union-attr]
    assert checks[0].ok
    assert "ground-tie contracts" in pipeline_module._ground_connection_guidance(
        topology
    )


def test_multiple_typed_ground_ties_and_mixed_signal_ic_are_supported(
    monkeypatch,
) -> None:
    pin_map = {
        "Test:Load": [{"number": "1", "name": "GND"}],
        "Test:Mixed": [
            {"number": "1", "name": "AGND"},
            {"number": "2", "name": "DGND"},
            {"number": "3", "name": "OUT"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:C": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
    }
    monkeypatch.setattr(
        pipeline_module.symbols,
        "symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        *(
            SelectedPart(
                ref=ref,
                symbol="Test:Load",
                value="load",
                role="domain_load",
            )
            for ref in ("U1", "U2", "U3")
        ),
        SelectedPart(
            ref="U4",
            symbol="Test:Mixed",
            value="mixed-signal IC",
            role="mixed_signal_converter",
        ),
        *(
            SelectedPart(
                ref=ref,
                symbol="Device:R",
                value="0R",
                role="ground_star_net_tie",
            )
            for ref in ("R1", "R2")
        ),
        SelectedPart(
            ref="C1",
            symbol="Device:C",
            value="1nF",
            role="chassis_ground_coupling_capacitor",
        ),
    ])
    topology = TopologyPlan(
        blocks=[
            TopologyBlock(
                name="logic_analog_boundary",
                kind="net_tie",
                implementation_kind="component",
                implementation_refs=["R1"],
            ),
            TopologyBlock(
                name="analog_power_ground_tie",
                kind="ground_star",
                implementation_kind="component",
                implementation_refs=["R2"],
            ),
        ],
        rails=["3V3"],
        ground_net="GND",
        ground_domains=["GND", "AGND", "PGND"],
        ground_ties=[
            GroundTieContract(component_ref="R1", domains=["GND", "AGND"]),
            GroundTieContract(component_ref="R2", domains=["AGND", "PGND"]),
        ],
    )
    intent = NetlistIntent(
        ground_net="GND",
        nets=[
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U1", pin="GND"),
                    LogicalPin(ref="U4", pin="DGND"),
                    LogicalPin(ref="C1", pin="1"),
                ],
            ),
            NetIntent(
                name="AGND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U2", pin="GND"),
                    LogicalPin(ref="U4", pin="AGND"),
                    LogicalPin(ref="C1", pin="2"),
                ],
            ),
            NetIntent(
                name="PGND",
                kind="ground",
                pins=[LogicalPin(ref="U3", pin="GND")],
            ),
        ],
    )

    repaired = pipeline_module._normalize_ground_star_ties(
        selection,
        intent,
        topology,
    )
    checks = pipeline_module._ground_star_topology_checks(
        selection,
        repaired,
        topology,
    )
    topology_checks = pipeline_module.TopologyStep().check(
        PipelineState(requirement_text="board"),
        topology,
    )

    assert _ConnectivityView.build(selection, repaired).part_nets(
        selection.parts[4]
    ) == {"GND", "AGND"}
    assert _ConnectivityView.build(selection, repaired).part_nets(
        selection.parts[5]
    ) == {"AGND", "PGND"}
    assert all(check.ok for check in checks)
    assert next(
        check for check in topology_checks
        if check.name == "typed_ground_tie_contracts"
    ).ok


def test_typed_ownership_precedes_semantics_and_signal_name_is_not_ground() -> None:
    selection = SelectionPlan(parts=[SelectedPart(
        ref="FB1",
        symbol="Device:Ferrite_Bead",
        value="ferrite",
        role="generic_filter",
    )])
    topology = TopologyPlan(
        blocks=[
            TopologyBlock(
                name="digital_filter",
                kind="filter",
                implementation_kind="component",
                implementation_refs=["FB1"],
            ),
            TopologyBlock(name="integration", kind="integration"),
            TopologyBlock(
                name="optional_mounting_hole",
                kind="mechanical",
                implementation_kind="mechanical_feature",
                implementation_refs=["H1"],
            ),
        ],
        rails=["3V3"],
        ground_net="GND",
    )
    intent = NetlistIntent(
        ground_net="GND",
        nets=[
            NetIntent(name="GND", kind="ground"),
            NetIntent(name="GND_SENSE", kind="signal"),
        ],
    )

    assert assign_parts_to_topology_blocks(topology, selection) == {
        "FB1": "digital_filter"
    }
    assert pipeline_module.SchMaterializeStep._ground_domains(intent) == ["GND"]
    assert pipeline_module._ground_domain_contract_checks(
        intent,
        pipeline_module._normalize_topology_plan(topology),
    )[0].ok
    misclassified = intent.model_copy(deep=True)
    misclassified.net("GND_SENSE").kind = "ground"  # type: ignore[union-attr]
    assert not pipeline_module._ground_domain_contract_checks(
        misclassified,
        pipeline_module._normalize_topology_plan(topology),
    )[0].ok


def test_fresh_ground_star_cannot_claim_copper_zone_ownership() -> None:
    topology = pipeline_module._normalize_topology_plan(TopologyPlan(
        blocks=[TopologyBlock(
            name="isolation_boundary",
            kind="net_tie",
            implementation_kind="copper_zone",
            implementation_refs=["NT1"],
        )],
        rails=["3V3"],
        ground_net="GND",
        ground_domains=["GND", "AGND"],
        ground_ties=[GroundTieContract(
            component_ref="NT1",
            domains=["GND", "AGND"],
        )],
    ))

    checks = pipeline_module.TopologyStep().check(
        PipelineState(requirement_text="board"),
        topology,
    )

    contract = next(
        check for check in checks if check.name == "typed_ground_tie_contracts"
    )
    assert not contract.ok
    assert "must be a component-backed block" in contract.message


def test_verified_alias_and_pull_role_drive_boot_gate(monkeypatch) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "6", "name": "PB2"},
            {"number": "7", "name": "PF0"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    view = _ConnectivityView.build(_control_selection(), _control_intent())

    checks = _mcu_control_topology_checks(view, _architect_requirement())

    alias_check = next(
        check
        for check in checks
        if check.name
        == "harness_consistency:verified_pin_alias_resolution:boot"
    )
    assert alias_check.ok
    assert alias_check.reason_code == "verified_pin_alias_resolution_verified"


def test_verified_alias_resolver_contradiction_has_explicit_harness_origin(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(
        pipeline_module.symbols,
        "symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    view = _ConnectivityView.build(_control_selection(), _control_intent())
    # The independent candidate calculation still proves exactly one physical
    # BOOT0 net; only the validator result is made inconsistent.
    monkeypatch.setattr(pipeline_module, "_verified_function_net", lambda *_a: None)

    checks = _mcu_control_topology_checks(view, _architect_requirement())

    assert len(checks) == 1
    assert not checks[0].ok
    assert (
        checks[0].name
        == "harness_consistency:verified_pin_alias_resolution:boot"
    )
    assert checks[0].origin == FailureOrigin.HARNESS
    assert checks[0].blocks_execution
    assert checks[0].reason_code == "verified_pin_alias_resolution_lost"


def test_reset_gap_is_not_closed_when_only_boot_alias_is_exercised(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "8", "name": "NRST"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(pipeline_module.symbols, "symbol_pins", lambda lib_id: pin_map[lib_id])
    evidence = {
        "evidence_contract": {"schema_version": 1, "producer": "architect_phase"},
        "verified_pin_aliases": [
            {
                "symbol_lib_id": "MCU_Test:Controller",
                "pin_number": "5",
                "symbol_pin_name": "PA14",
                "aliases": ["BOOT0"],
                "evidence_ids": ["https://manufacturer.example/device.pdf#page=42"],
            },
            {
                "symbol_lib_id": "MCU_Test:Controller",
                "pin_number": "8",
                "symbol_pin_name": "NRST",
                "aliases": ["NRST"],
                "evidence_ids": ["https://manufacturer.example/device.pdf#page=41"],
            },
        ],
    }
    both_aliases_requirement = (
        "build a controller\n\nGROUNDED ARCHITECT EVIDENCE — contract:\n"
        + json.dumps(evidence)
    )
    intent = _control_intent()
    intent.nets.append(NetIntent(
        name="MCU_NRST",
        pins=[LogicalPin(ref="U1", pin="NRST")],
    ))
    view = _ConnectivityView.build(_control_selection(), intent)
    original_resolver = pipeline_module._verified_function_net

    def reset_only_contradiction(view, mcu, aliases, *functions):
        if "NRST" in functions:
            return None
        return original_resolver(view, mcu, aliases, *functions)

    monkeypatch.setattr(
        pipeline_module,
        "_verified_function_net",
        reset_only_contradiction,
    )
    failed_checks = _mcu_control_topology_checks(view, both_aliases_requirement)
    reset_failure = next(
        check
        for check in failed_checks
        if check.name == "harness_consistency:verified_pin_alias_resolution:reset"
    )
    assert not reset_failure.ok
    assert any(
        check.ok
        and check.name
        == "harness_consistency:verified_pin_alias_resolution:boot"
        for check in failed_checks
    )
    failure = make_failure(
        step="schematic_connections",
        check_name=reset_failure.name,
        message=reset_failure.message,
        repair_available=True,
        origin=reset_failure.origin,
        reason_code=reset_failure.reason_code,
        affected_refs=reset_failure.affected_refs,
    ).model_copy(update={"recoverability": Recoverability.CAPABILITY_GAP})
    gap = make_capability_gap(failure)

    class BootOnlyAliasStep(SchConnectionsStep):
        def knowledge_query(self, _state):
            return None

        def propose(self, _state, _ctx, _knowledge):
            return _control_intent(), False

        def check(self, _state, artifact):
            boot_view = _ConnectivityView.build(_control_selection(), artifact)
            return _mcu_control_topology_checks(
                boot_view,
                _architect_requirement(),
            )

    monkeypatch.setattr(
        pipeline_module,
        "_verified_function_net",
        original_resolver,
    )
    state = PipelineState(
        requirement_text=_architect_requirement(),
        project_name="board",
        capability_gaps=[gap],
    )
    events: list[dict[str, object]] = []
    BootOnlyAliasStep().run(
        state,
        PipelineContext(
            kb=object(),  # type: ignore[arg-type]
            repair_attempts=0,
            max_total_repair_attempts=0,
            on_ahe_event=events.append,
        ),
    )

    assert not any(
        event.get("event") == "capability_gap_resolved"
        for event in events
    )
    assert state.capability_gaps == [gap]
    other_ref_failure = make_failure(
        step="schematic_connections",
        check_name=reset_failure.name,
        message=reset_failure.message.replace("U1", "U3"),
        repair_available=True,
        origin=reset_failure.origin,
        reason_code=reset_failure.reason_code,
        affected_refs=["U3"],
    )
    assert other_ref_failure.signature == failure.signature


def test_real_repair_capable_steps_keep_deterministic_harness_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SelectionHarnessFailure(SelectionStep):
        def knowledge_query(self, _state):
            return None

        def propose(self, _state, _ctx, _knowledge):
            return SelectionPlan(parts=[]), False

        def check(self, _state, _artifact):
            return [CheckResult(
                name="harness_consistency:generic_capability_closure",
                ok=False,
                blocks_execution=True,
                origin=FailureOrigin.HARNESS,
                reason_code="generic_capability_closure_contradiction",
                affected_refs=["D1"],
            )]

    class ConnectionsHarnessFailure(SchConnectionsStep):
        def knowledge_query(self, _state):
            return None

        def propose(self, _state, _ctx, _knowledge):
            return NetlistIntent(nets=[]), False

        def check(self, _state, _artifact):
            return [CheckResult(
                name="harness_consistency:verified_pin_alias_resolution:boot",
                ok=False,
                blocks_execution=True,
                origin=FailureOrigin.HARNESS,
                reason_code="verified_pin_alias_resolution_lost",
                affected_refs=["U1"],
            )]

    monkeypatch.setattr(tools, "publish_ahe_event_best_effort", lambda *_a, **_kw: None)
    for index, step in enumerate(
        (SelectionHarnessFailure(), ConnectionsHarnessFailure()),
        start=1,
    ):
        events: list[dict[str, object]] = []
        result = step.run(
            PipelineState(requirement_text="bounded test", project_name="board"),
            PipelineContext(
                kb=object(),  # type: ignore[arg-type]
                repair_attempts=0,
                max_total_repair_attempts=0,
                on_ahe_event=events.append,
            ),
        )

        assert result.failures[0].origin == FailureOrigin.HARNESS
        assert (
            result.failures[0].recoverability
            == Recoverability.HARNESS_OBSERVATION
        )
        observed = [
            event
            for event in events
            if event.get("event") == "harness_defect_observed"
        ]
        assert len(observed) == 1
        assert observed[0]["failure"]["reason_code"] in {
            "generic_capability_closure_contradiction",
            "verified_pin_alias_resolution_lost",
        }
        scope = TrustedGovernanceScope(
            tenant_scope="a" * 16,
            project_scope=f"{index:x}" * 16,
            run_scope=f"{index:x}" * 64,
            harness_version_id="harness-v1",
            harness_manifest_digest="b" * 64,
        )
        memory = EheMemory(tmp_path / f"ehe-{index}", governance_scope=scope)
        audit_path = tmp_path / f"audit-{index}.jsonl"
        tools._record_ahe_event(
            memory,
            observed[0],
            run_name="untrusted-display-name",
            project_name="untrusted-display-project",
            requirement="must never enter governed telemetry",
            workflow_id=f"workflow-{index}",
            audit_path=audit_path,
        )
        signature = str(observed[0]["failure"]["signature"])
        assert audit_path.exists()
        assert memory.harness_recurrence(signature) == (1, 1)


def test_real_generic_invariant_opens_then_closes_only_passing_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    part = SelectedPart(
        ref="D1",
        symbol="Device:D",
        value="generic Schottky diode",
        footprint="Diode_SMD:D_SOD-123",
        role="flyback_diode",
    )
    failure_closure = LibraryClosureResult(resolutions=[ComponentResolution(
        ref="D1",
        status=ResolutionStatus.HARNESS_FAILURE,
        requested_identity=part.value,
        symbol=part.symbol,
        footprint=part.footprint,
        release_ready=False,
        blocks_execution=True,
        reason_code="generic_capability_closure_contradiction",
        detail="verified generic capability closure was contradicted",
    )])
    success_closure = LibraryClosureResult(resolutions=[ComponentResolution(
        ref="D1",
        status=ResolutionStatus.INSTALLED_EXACT,
        requested_identity=part.value,
        symbol=part.symbol,
        footprint=part.footprint,
        release_ready=True,
        blocks_execution=False,
        reason_code="generic_primitive",
        detail="installed generic primitive passed pin and pad closure",
    )])
    closure = {"value": failure_closure}
    monkeypatch.setattr(pipeline_module.config, "symbol_dir", lambda: tmp_path)
    monkeypatch.setattr(pipeline_module.config, "footprint_dir", lambda: tmp_path)
    monkeypatch.setattr(
        pipeline_module,
        "_close_component_libraries",
        lambda *_args, **_kwargs: closure["value"],
    )
    monkeypatch.setattr(tools, "publish_ahe_event_best_effort", lambda *_a, **_kw: None)

    class RealGenericClosureStep(SelectionStep):
        artifact = SelectionPlan(parts=[part])

        def knowledge_query(self, _state):
            return None

        def propose(self, _state, _ctx, _knowledge):
            return self.artifact.model_copy(deep=True), False

        def check(self, state, artifact):
            return [
                check
                for check in super().check(state, artifact)
                if check.name == "harness_consistency:generic_capability_closure"
            ]

    step = RealGenericClosureStep()
    first_events: list[dict[str, object]] = []
    first_state = PipelineState(
        requirement_text="build a board with a generic flyback diode",
        project_name="board",
    )
    step.run(
        first_state,
        PipelineContext(
            kb=object(),  # type: ignore[arg-type]
            repair_attempts=0,
            max_total_repair_attempts=0,
            on_ahe_event=first_events.append,
        ),
    )
    observation = next(
        event
        for event in first_events
        if event.get("event") == "harness_defect_observed"
    )

    first_scope = TrustedGovernanceScope(
        tenant_scope="a" * 16,
        project_scope="b" * 16,
        run_scope="c" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="d" * 64,
    )
    second_scope = TrustedGovernanceScope(
        tenant_scope=first_scope.tenant_scope,
        project_scope="e" * 16,
        run_scope="f" * 64,
        harness_version_id=first_scope.harness_version_id,
        harness_manifest_digest=first_scope.harness_manifest_digest,
    )
    first_memory = EheMemory(tmp_path / "ehe", governance_scope=first_scope)
    second_memory = EheMemory(tmp_path / "ehe", governance_scope=second_scope)
    gap_state = PipelineState(
        requirement_text=first_state.requirement_text,
        project_name="board",
    )
    tools._record_ahe_event(
        first_memory,
        observation,
        run_name="display-one",
        project_name="display-one",
        requirement=gap_state.requirement_text,
        workflow_id="workflow-one",
        audit_path=tmp_path / "first.jsonl",
    )
    tools._record_ahe_event(
        second_memory,
        observation,
        run_name="display-two",
        project_name="display-two",
        requirement=gap_state.requirement_text,
        workflow_id="workflow-two",
        audit_path=tmp_path / "second.jsonl",
        state=gap_state,
    )
    assert len(gap_state.capability_gaps) == 1
    assert len(first_memory.active_gaps()) == 1
    assert len(second_memory.active_gaps()) == 1

    # Removing the checked component does not execute the invariant and cannot
    # close the existing project-scoped gap.
    closure["value"] = success_closure
    step.artifact = SelectionPlan(parts=[])
    removed_events: list[dict[str, object]] = []
    step.run(
        gap_state,
        PipelineContext(
            kb=object(),  # type: ignore[arg-type]
            repair_attempts=0,
            max_total_repair_attempts=0,
            on_ahe_event=removed_events.append,
        ),
    )
    assert not any(
        event.get("event") == "capability_gap_resolved"
        for event in removed_events
    )
    assert len(gap_state.capability_gaps) == 1

    # Re-executing the exact per-ref invariant successfully emits a strictly
    # bound resolution and closes only the current trusted project ledger.
    step.artifact = SelectionPlan(parts=[part])
    passing_events: list[dict[str, object]] = []
    step.run(
        gap_state,
        PipelineContext(
            kb=object(),  # type: ignore[arg-type]
            repair_attempts=0,
            max_total_repair_attempts=0,
            on_ahe_event=passing_events.append,
        ),
    )
    resolved = next(
        event
        for event in passing_events
        if event.get("event") == "capability_gap_resolved"
    )
    gap = resolved["gap"]
    failure = resolved["failure"]
    assert gap["signature"] == failure["signature"]
    assert gap["step"] == failure["step"] == "selection"

    later_first_scope = TrustedGovernanceScope(
        tenant_scope=first_scope.tenant_scope,
        project_scope=first_scope.project_scope,
        run_scope="1" * 64,
        harness_version_id=first_scope.harness_version_id,
        harness_manifest_digest=first_scope.harness_manifest_digest,
    )
    later_first_memory = EheMemory(
        tmp_path / "ehe",
        governance_scope=later_first_scope,
    )
    tools._record_ahe_event(
        later_first_memory,
        resolved,
        run_name="display-three",
        project_name="display-three",
        requirement=gap_state.requirement_text,
        workflow_id="workflow-three",
        audit_path=tmp_path / "resolved.jsonl",
    )
    assert later_first_memory.active_gaps() == []
    assert len(second_memory.active_gaps()) == 1


def test_boot_gate_rejects_missing_or_wrong_pin_alias_and_wrong_pull_direction(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "6", "name": "PB2"},
            {"number": "7", "name": "PF0"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )

    no_alias = _ConnectivityView.build(
        _control_selection(),
        _control_intent(),
    )
    wrong_pb2 = _ConnectivityView.build(
        _control_selection(),
        _control_intent(boot_pin="PB2"),
    )
    wrong_pf0 = _ConnectivityView.build(
        _control_selection(),
        _control_intent(boot_pin="PF0"),
    )
    wrong_pull = _ConnectivityView.build(
        _control_selection(),
        _control_intent(pulldown_to_supply=True),
    )

    failed = [
        next(
            check
            for check in _mcu_control_topology_checks(view, requirement)
            if check.name.startswith("mcu_reset_boot_support:")
        )
        for view, requirement in (
            (no_alias, "no evidence"),
            (wrong_pb2, _architect_requirement()),
            (wrong_pf0, _architect_requirement()),
            (wrong_pull, _architect_requirement()),
        )
    ]
    assert all(not check.ok for check in failed)
    assert all(check.origin is None for check in failed)


def test_signal_output_power_rail_gate_requires_a_real_pull_resistor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pin_map = {
        "Sensor_Test:Addressable": [
            {"number": "5", "name": "GND", "type": "power_in"},
            {"number": "7", "name": "SDO", "type": "output"},
        ],
        "Device:R": [
            {"number": "1", "name": "~", "type": "passive"},
            {"number": "2", "name": "~", "type": "passive"},
        ],
    }
    monkeypatch.setattr(pipeline_module.config, "symbol_dir", lambda: tmp_path)
    monkeypatch.setattr(
        pipeline_module.symbols,
        "symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = SelectionPlan(parts=[
        SelectedPart(
            ref="U3",
            symbol="Sensor_Test:Addressable",
            value="sensor",
            role="i2c_sensor",
        ),
        SelectedPart(
            ref="R7",
            symbol="Device:R",
            value="10k",
            role="address_select_pulldown",
        ),
    ])
    state = PipelineState(requirement_text="build an I2C sensor board")
    state.artifacts[pipeline_module.PipelineStep.SELECTION] = selection
    bad = NetlistIntent(
        nets=[NetIntent(
            name="GND",
            kind="ground",
            pins=[
                LogicalPin(ref="U3", pin="GND"),
                LogicalPin(ref="U3", pin="SDO"),
            ],
        )],
        ground_net="GND",
    )
    good = NetlistIntent(
        nets=[
            NetIntent(
                name="GND",
                kind="ground",
                pins=[
                    LogicalPin(ref="U3", pin="GND"),
                    LogicalPin(ref="R7", pin="2"),
                ],
            ),
            NetIntent(
                name="ADDR_STRAP",
                kind="signal",
                pins=[
                    LogicalPin(ref="U3", pin="SDO"),
                    LogicalPin(ref="R7", pin="1"),
                ],
            ),
        ],
        ground_net="GND",
    )
    step = SchConnectionsStep()
    bad_gate = next(
        check
        for check in step.check(state, bad)
        if check.name == "signal_output_not_directly_on_power_rail"
    )
    good_gate = next(
        check
        for check in step.check(state, good)
        if check.name == "signal_output_not_directly_on_power_rail"
    )

    assert not bad_gate.ok
    assert bad_gate.affected_refs == ["U3"]
    assert bad_gate.evidence["pin_net_conflicts"] == [{
        "ref": "U3",
        "pin": "7",
        "pin_name": "SDO",
        "pin_type": "output",
        "net": "GND",
    }]
    assert good_gate.ok


def test_resettable_input_fuse_is_not_classified_as_mcu_reset_support(
    monkeypatch,
) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
        "Device:Polyfuse": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    selection = _control_selection()
    selection.parts.append(SelectedPart(
        ref="F1",
        symbol="Device:Polyfuse",
        value="Polyfuse",
        footprint="Fuse:Fuse_1206_3216Metric",
        role="input_resettable_fuse",
    ))
    intent = _control_intent()
    intent.nets.append(NetIntent(
        name="5V_IN",
        kind="power",
        pins=[LogicalPin(ref="F1", pin="1")],
    ))
    next(net for net in intent.nets if net.name == "5V").pins.append(
        LogicalPin(ref="F1", pin="2")
    )

    checks = _mcu_control_topology_checks(
        _ConnectivityView.build(selection, intent),
        _architect_requirement(),
    )
    reset_boot = next(
        check for check in checks
        if check.name.startswith("mcu_reset_boot_support:")
    )

    assert "F1 (input_resettable_fuse)" not in reset_boot.message


def test_flyback_polarity_gate_uses_pin_functions_not_model_or_ref(monkeypatch) -> None:
    pin_map = {
        "MCU_Test:Controller": [
            {"number": "1", "name": "VSS"},
            {"number": "2", "name": "VDD"},
            {"number": "5", "name": "PA14"},
            {"number": "6", "name": "PB2"},
            {"number": "7", "name": "PF0"},
        ],
        "Device:R": [
            {"number": "1", "name": "~"},
            {"number": "2", "name": "~"},
        ],
        "Device:D": [
            {"number": "1", "name": "K"},
            {"number": "2", "name": "A"},
        ],
    }
    monkeypatch.setattr(
        "ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
        lambda lib_id: pin_map[lib_id],
    )
    correct = _ConnectivityView.build(_control_selection(), _control_intent())
    reversed_view = _ConnectivityView.build(
        _control_selection(),
        _control_intent(reverse_diode=True),
    )

    assert _diode_polarity_checks(correct)[0].ok
    assert not _diode_polarity_checks(reversed_view)[0].ok


def test_architect_alias_extraction_requires_datasheet_colocation() -> None:
    candidates = [{
        "lib_id": "MCU_Test:Controller",
        "pins": [{"number": "5", "name": "PA14", "type": "bidirectional"}],
    }]
    datasheet = {
        "source_url": "https://manufacturer.example/device.pdf",
        "matched_pages": [
            {
                "page": 42,
                "text": "Pin 46 PA14 provides JTCK/SWCLK and BOOT0 functions.",
            }
        ],
    }

    aliases = ratsnestpro_agent._verified_pin_aliases_from_evidence(
        candidates,
        datasheet,
    )

    assert aliases == [
        {
            "symbol_lib_id": "MCU_Test:Controller",
            "pin_number": "5",
            "symbol_pin_name": "PA14",
            "aliases": ["BOOT0", "JTCK", "SWCLK"],
            "evidence_ids": ["https://manufacturer.example/device.pdf#page=42"],
        }
    ]


def test_architect_alias_extraction_uses_nearest_pin_in_flattened_pinout() -> None:
    candidates = [{
        "lib_id": "MCU_Test:Controller",
        "pins": [
            {"number": "12", "name": "NRST", "type": "input"},
            {"number": "13", "name": "PF0", "type": "bidirectional"},
            {"number": "29", "name": "PB2", "type": "bidirectional"},
            {"number": "45", "name": "PA13", "type": "bidirectional"},
            {"number": "46", "name": "PA14", "type": "bidirectional"},
            {"number": "47", "name": "PA15", "type": "bidirectional"},
        ],
    }]
    datasheet = {
        "source_url": "https://manufacturer.example/device.pdf",
        "matched_pages": [{
            "page": 30,
            "text": (
                "LQFP64 pinout PB2 PC11 NRST PF0-OSC_IN "
                "PA15 PA14-BOOT0 PA13 PA12"
            ),
        }],
    }

    aliases = ratsnestpro_agent._verified_pin_aliases_from_evidence(
        candidates,
        datasheet,
    )

    assert aliases == [
        {
            "symbol_lib_id": "MCU_Test:Controller",
            "pin_number": "46",
            "symbol_pin_name": "PA14",
            "aliases": ["BOOT0"],
            "evidence_ids": ["https://manufacturer.example/device.pdf#page=30"],
        }
    ]


def test_architect_alias_extraction_rejects_ambiguous_singleton_controls() -> None:
    candidates = [{
        "lib_id": "MCU_Test:Controller",
        "pins": [
            {"number": "5", "name": "PA14", "type": "bidirectional"},
            {"number": "6", "name": "PB2", "type": "bidirectional"},
        ],
    }]
    datasheet = {
        "source_url": "https://manufacturer.example/device.pdf",
        "matched_pages": [{
            "page": 42,
            "text": (
                "PA14 provides BOOT0 and NRST functions.\n"
                "PB2 provides BOOT0 and NRST functions."
            ),
        }],
    }

    aliases = ratsnestpro_agent._verified_pin_aliases_from_evidence(
        candidates,
        datasheet,
    )

    assert aliases == []
