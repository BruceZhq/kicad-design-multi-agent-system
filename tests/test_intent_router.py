from __future__ import annotations

from agents.ratsnestpro.intent_router import classify_intent


def test_redo_numbered_step_resumes_active_build_context() -> None:
    decision = classify_intent(
        "重新做step11/17,然后完成该任务",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.primary_intent == "build"
    assert decision.context_relation == "resume"
    assert decision.in_scope is True


def test_negated_redo_does_not_resume_active_build_context() -> None:
    decision = classify_intent(
        "不要重新做之前的任务",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.context_relation != "resume"
