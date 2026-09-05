"""Public optimization cases disclosed to the governed patch proposer."""

from __future__ import annotations

import pytest

from agents.ratsnestpro.decision_engine import apply_resolutions, design_decisions
from agents.ratsnestpro.intent_router import classify_intent
from evolution.live_runner import LiveCase, _grade


def test_frozen_board_constraints_are_not_reopened_or_overridden() -> None:
    requirement = "请设计一块双层板，板框不超过 40 mm × 30 mm。"
    assert {item.slot for item in design_decisions(requirement)}.isdisjoint(
        {"board_outline", "layer_count"}
    )
    with pytest.raises(ValueError, match="cannot override"):
        apply_resolutions(
            requirement,
            [
                {
                    "slot": "board_outline",
                    "kind": "assumption",
                    "key": "A",
                    "label": "50 x 40 mm",
                    "value": "For board_outline, use 50 x 40 mm.",
                    "citation": "default",
                }
            ],
        )


def test_checkpoint_instruction_is_a_resume_not_an_amendment() -> None:
    decision = classify_intent(
        "余额已恢复，从已保存的 layout_partition 检查点继续，不要重跑已验证步骤。",
        prior_intent="build",
        has_active_context=True,
    )
    assert decision.primary_intent == "build"
    assert decision.context_relation == "resume"


def test_release_label_without_engineering_evidence_fails_closed() -> None:
    case = LiveCase.model_validate(
        {
            "caseId": "optimization.release-truth",
            "category": "eda_pipeline",
            "prompt": "build a board",
            "expectedIntents": ["build"],
            "expectReleaseReady": True,
        }
    )
    observed = {
        "httpStatus": 200,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "build",
        "phases": [],
        "tools": [],
        "completedSteps": 17,
        "deliveryStatus": "release_ready",
        "artifacts": [{"name": "pipeline_result.json", "valid": True}],
        "artifactsValid": True,
        "releaseEvidence": {"strictGatePassed": False},
    }
    assert _grade(case, observed, None)["releaseGate"] is False
