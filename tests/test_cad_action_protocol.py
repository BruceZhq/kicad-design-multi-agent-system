from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ratsnestpro.eda import routing
from ratsnestpro.orchestration.ahe import (
    FailureOrigin,
    Recoverability,
    RecoveryAction,
    RecoveryDecision,
    make_missing_mutation_failure,
)
from ratsnestpro.orchestration.entity_repairs import CadActionBatch
from ratsnestpro.orchestration.pipeline import (
    _apply_schematic_cad_action_batch,
    _artifact_sha256,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _move_batch(base_fingerprint: str) -> CadActionBatch:
    return CadActionBatch.model_validate({
        "batch_id": "batch-layout-0001",
        "idempotency_key": "layout-action-0001",
        "owner_step": "layout_general",
        "base_artifact_fingerprint": base_fingerprint,
        "actions": [{
            "action_id": "move-u1-0001",
            "operation": "move_footprint",
            "target": {"kind": "footprint", "reference": "U1"},
            "position": {"x_mm": 20.0, "y_mm": 15.0},
        }],
        "success_checks": ["kicad_drc", "requirement_invariants"],
    })


def test_recovery_decision_carries_a_strict_cad_action_batch() -> None:
    batch = _move_batch("a" * 64)
    decision = RecoveryDecision(
        action=RecoveryAction.LOCAL_REPAIR,
        target_step="layout_general",
        cad_action_batch=batch,
    )

    restored = RecoveryDecision.model_validate(decision.model_dump(mode="json"))

    assert restored.cad_action_batch is not None
    assert restored.cad_action_batch.actions[0].operation == "move_footprint"
    assert restored.cad_action_batch.actions[0].target.reference == "U1"


def test_cad_action_contract_rejects_wrong_target_and_unbounded_parameters() -> None:
    payload = _move_batch("b" * 64).model_dump(mode="json")
    payload["actions"][0]["target"] = {"kind": "net", "net": "3V3"}
    with pytest.raises(ValidationError, match="cannot target"):
        CadActionBatch.model_validate(payload)

    payload = _move_batch("b" * 64).model_dump(mode="json")
    payload["actions"][0]["width_mm"] = 0.25
    with pytest.raises(ValidationError, match="does not accept"):
        CadActionBatch.model_validate(payload)


def test_missing_cad_primitive_produces_stable_ehe_evidence() -> None:
    first = make_missing_mutation_failure(
        step="route_signals",
        requested_action="shove_track_pair",
        affected_refs=["U1"],
    )
    repeated = make_missing_mutation_failure(
        step="route_signals",
        requested_action="shove track pair",
        message="a different board exposed the same missing primitive",
        affected_refs=["J2"],
    )

    assert first.signature == repeated.signature
    assert first.reason_code == "missing_mutation_capability"
    assert first.required_capability == "eda.pcb.mutation.shove_track_pair"
    assert first.origin == FailureOrigin.HARNESS
    assert first.recoverability == Recoverability.HARNESS_OBSERVATION


def test_apply_cad_action_batch_is_atomic_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pcb = run_dir / "board.kicad_pcb"
    pcb.write_bytes(b"board-before")
    batch = _move_batch(_digest(b"board-before"))
    calls = 0

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        candidate = Path(args[3])
        candidate.write_bytes(b"board-after")
        result = {
            "ok": True,
            "error": "",
            "before_fingerprint": _digest(b"board-before"),
            "after_fingerprint": _digest(b"board-after"),
            "action_results": [{
                "action_id": "move-u1-0001",
                "operation": "move_footprint",
                "status": "applied",
                "detail": "moved U1",
            }],
        }
        return SimpleNamespace(
            stdout="RESULT " + json.dumps(result),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(routing, "kicad_python", lambda: "kicad-python")
    monkeypatch.setattr(routing.subprocess, "run", fake_run)

    first = routing.apply_cad_action_batch(pcb, batch, run_dir=run_dir)
    replay = routing.apply_cad_action_batch(pcb, batch, run_dir=run_dir)

    assert first.status == "applied"
    assert first.before_fingerprint == _digest(b"board-before")
    assert first.after_fingerprint == _digest(b"board-after")
    assert first.pending_success_checks == ["kicad_drc", "requirement_invariants"]
    assert pcb.read_bytes() == b"board-after"
    assert replay.status == "already_applied"
    assert calls == 1


def test_apply_cad_action_batch_rejects_stale_or_out_of_scope_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stale_pcb = run_dir / "stale.kicad_pcb"
    stale_pcb.write_bytes(b"newer-revision")
    batch = _move_batch(_digest(b"older-revision"))

    def must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("worker must not run for a stale artifact")

    monkeypatch.setattr(routing.subprocess, "run", must_not_run)

    stale = routing.apply_cad_action_batch(stale_pcb, batch, run_dir=run_dir)
    assert stale.status == "rejected"
    assert "fingerprint" in stale.detail

    outside = tmp_path / "outside.kicad_pcb"
    outside.write_bytes(b"older-revision")
    out_of_scope = routing.apply_cad_action_batch(outside, batch, run_dir=run_dir)
    assert out_of_scope.status == "rejected"
    assert "workspace" in out_of_scope.detail


def test_typed_schematic_actions_apply_atomically_to_source_ir() -> None:
    intent = NetlistIntent(
        nets=[
            NetIntent(
                name="3V3",
                kind="power",
                pins=[
                    LogicalPin(ref="U1", pin="VDD"),
                    LogicalPin(ref="U2", pin="OUT"),
                ],
            ),
        ],
        no_connect_pins=[LogicalPin(ref="U1", pin="PA0")],
        supply_nets=["3V3"],
    )
    batch = CadActionBatch.model_validate({
        "batch_id": "batch-schematic-0001",
        "idempotency_key": "schematic-action-0001",
        "owner_step": "schematic_connections",
        "base_artifact_fingerprint": _artifact_sha256(intent),
        "actions": [
            {
                "action_id": "connect-pa0-0001",
                "operation": "upsert_net_pin",
                "target": {
                    "kind": "pin",
                    "reference": "U1",
                    "pin": "PA0",
                    "net": "USER_LED",
                },
                "preconditions": {"expected_net": "__NO_CONNECT__"},
            },
            {
                "action_id": "remove-u2-out-0001",
                "operation": "remove_net_pin",
                "target": {"kind": "pin", "reference": "U2", "pin": "OUT"},
                "preconditions": {"expected_net": "3V3"},
            },
            {
                "action_id": "nc-u2-out-0001",
                "operation": "set_no_connect",
                "target": {"kind": "pin", "reference": "U2", "pin": "OUT"},
            },
        ],
        "success_checks": ["no_duplicate_pin_assignments", "erc"],
    })

    candidate, observation = _apply_schematic_cad_action_batch(intent, batch)

    assert observation.status == "applied"
    assert candidate.net("USER_LED").pins == [LogicalPin(ref="U1", pin="PA0")]
    assert candidate.net("3V3").pins == [LogicalPin(ref="U1", pin="VDD")]
    assert LogicalPin(ref="U2", pin="OUT") in candidate.no_connect_pins


def test_schematic_precondition_failure_rolls_back_whole_batch() -> None:
    intent = NetlistIntent(
        nets=[NetIntent(
            name="GND",
            kind="ground",
            pins=[LogicalPin(ref="U1", pin="VSS"), LogicalPin(ref="J1", pin="2")],
        )],
    )
    batch = CadActionBatch.model_validate({
        "batch_id": "batch-schematic-0002",
        "idempotency_key": "schematic-action-0002",
        "owner_step": "schematic_connections",
        "base_artifact_fingerprint": _artifact_sha256(intent),
        "actions": [{
            "action_id": "wrong-precondition-0001",
            "operation": "set_no_connect",
            "target": {"kind": "pin", "reference": "U1", "pin": "VSS"},
            "preconditions": {"expected_net": "3V3"},
        }],
        "success_checks": ["erc"],
    })

    candidate, observation = _apply_schematic_cad_action_batch(intent, batch)

    assert observation.status == "rejected"
    assert candidate == intent
    assert candidate.no_connect_pins == []


def test_missing_schematic_primitive_uses_schematic_ehe_namespace() -> None:
    failure = make_missing_mutation_failure(
        step="schematic_connections",
        requested_action="set_no_connect",
    )

    assert failure.required_capability == "eda.schematic.mutation.set_no_connect"
