"""Sealed non-regression cases unavailable to the patch proposer."""

from __future__ import annotations

import pytest

from agents.ratsnestpro.decision_engine import apply_resolutions, design_decisions
from agents.ratsnestpro.intent_router import classify_intent


def test_bilingual_physical_constraints_remain_authoritative() -> None:
    requirement = (
        "Create a two-layer PCB no larger than 38 mm by 28 mm; "
        "底层必须保持连续 GND。"
    )
    assert {item.slot for item in design_decisions(requirement)}.isdisjoint(
        {"board_outline", "layer_count"}
    )
    with pytest.raises(ValueError, match="cannot override"):
        apply_resolutions(
            requirement,
            [
                {
                    "slot": "layer_count",
                    "kind": "assumption",
                    "key": "B",
                    "label": "4 layers",
                    "value": "For layer_count, use 4 layers.",
                    "citation": "optimizer suggestion",
                }
            ],
        )


def test_resume_with_negated_change_preserves_checkpoint_semantics() -> None:
    decision = classify_intent(
        "Continue from the saved checkpoint. This is not a requirement change; "
        "do not rebuild the verified prefix.",
        prior_intent="build",
        has_active_context=True,
    )
    assert decision.context_relation == "resume"
