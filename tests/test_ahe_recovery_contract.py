from __future__ import annotations

from ratsnestpro.orchestration.ahe import (
    FailureOrigin,
    RecoveryAction,
    RecoveryDecision,
    RecoveryTurnRecord,
    ahe_event,
)
from service.ahe_event import sanitize_ahe_event


def test_recovery_contract_defaults_are_fail_closed_and_event_serializable() -> None:
    default_decision = RecoveryDecision.model_validate({})
    assert default_decision.action == RecoveryAction.STOP
    assert default_decision.origin == FailureOrigin.UNKNOWN

    recovery = RecoveryTurnRecord.model_validate({
        "step": "erc",
        "failure_ids": ["erc:topology:mismatch"],
        "decision": {
            "failure_ids": ["erc:topology:mismatch"],
            "action": "investigate_harness",
            "hypothesis": "the verifier may be comparing equivalent net names",
            "success_checks": ["design_ir_matches_kicad_netlist"],
        },
    })
    event = ahe_event(
        "recovery_planned",
        step="erc",
        revision=1,
        recovery=recovery,
    )

    assert event["recovery"]["decision"]["action"] == "investigate_harness"
    assert event["recovery"]["status"] == "planned"
    safe = sanitize_ahe_event(event)
    assert safe["recovery"]["action"] == "investigate_harness"
    assert "decision" not in safe["recovery"]
    assert event["recovery"]["before_score"] == [0, 0, 0]
