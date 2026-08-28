from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from agents.ratsnestpro.intent_router import classify_intent
from agents.ratsnestpro.profiles.registry import REGISTRY
from agents.ratsnestpro.ratsnestpro_agent import initialize


def test_redo_numbered_step_resumes_active_build_context() -> None:
    decision = classify_intent(
        "重新做step11/17,然后完成该任务",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.primary_intent == "build"
    assert decision.context_relation == "resume"
    assert decision.in_scope is True


def test_revision_envelope_does_not_turn_checkpoint_resume_into_amendment() -> None:
    decision = classify_intent(
        "USER CHANGE REQUEST:\n"
        "余额已恢复，请严格复用已保存的检查点，从 layout_partition 第9步继续完成当前任务，"
        "不要重新执行前8步。",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.primary_intent == "build"
    assert decision.context_relation == "resume"


def test_revision_envelope_preserves_real_layout_amendment() -> None:
    decision = classify_intent(
        "USER CHANGE REQUEST:\n把板框尺寸改为100x80mm。",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.primary_intent == "build"
    assert decision.context_relation == "amend"


def test_negated_requirement_change_is_a_resume_not_an_amendment() -> None:
    for request in (
        "Resume from the saved checkpoint. This is not a design-requirement change; "
        "do not change the user requirements.",
        "从已保存检查点继续。这不是需求变更，不要修改原始需求。",
    ):
        decision = classify_intent(
            request,
            prior_intent="build",
            has_active_context=True,
        )

        assert decision.primary_intent == "build"
        assert decision.context_relation == "resume"


def test_positive_change_after_negated_change_remains_an_amendment() -> None:
    decision = classify_intent(
        "Do not change the MCU; continue and add a second LED.",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.context_relation == "amend"


def test_initialize_reuses_original_workspace_for_wrapped_resume() -> None:
    profile = REGISTRY.all()[0].model_dump(mode="json")
    original_requirement = "workflow_mode: build\nDesign a KiCad NE555 LED board."
    original_workspace = "original-workspace-run"
    state = {
        "messages": [
            HumanMessage(
                content=(
                    "USER CHANGE REQUEST:\n余额已恢复，请严格复用已保存的检查点，"
                    "从 layout_partition 第9步继续完成当前任务，不要重新执行前8步。"
                )
            )
        ],
        "requirement": original_requirement,
        "workflow_mode": "build",
        "run_name": "original-run",
        "execution_scope": "0123456789abcdef",
        "workspace_run_name": original_workspace,
        "project_name": "board",
        "capability_profile": profile,
        "architecture": {"status": "ok"},
        "parts": {"status": "ok"},
        "trace": [],
    }

    result = asyncio.run(
        initialize(
            state,
            {
                "configurable": {
                    "client_thread_id": "thread-1",
                    "user_id": "user-1",
                }
            },
        )
    )

    assert result["requirement"] == original_requirement
    assert result["run_name"] == "original-run"
    assert result["workspace_run_name"] == original_workspace
    assert result["incremental_resume"] is True


def test_negated_redo_does_not_resume_active_build_context() -> None:
    decision = classify_intent(
        "不要重新做之前的任务",
        prior_intent="build",
        has_active_context=True,
    )

    assert decision.context_relation != "resume"
