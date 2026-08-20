from __future__ import annotations

import json
from pathlib import Path

from agents.ratsnestpro import ratsnestpro_agent, tools
from agents.ratsnestpro.ehe_memory import EheMemory
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
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineContext,
    PipelineState,
    SchConnectionsStep,
    SelectionStep,
    _ConnectivityView,
    _diode_polarity_checks,
    _library_closure_check,
    _library_closure_diagnostics,
    _mcu_control_topology_checks,
    _repairable_selection_refs,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
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
