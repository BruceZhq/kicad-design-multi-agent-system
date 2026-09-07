import asyncio
from types import SimpleNamespace

import pytest

from agents.ratsnestpro.intent_router import classify_intent, requests_new_context
from agents.ratsnestpro import resume_context


@pytest.mark.parametrize("message", [
    "继续当前 KiCad 构建任务，不新建 Run 或工程。沿用原始需求和原检查点，不重跑前两步。",
    "Resume the PCB task. Do not start a new project.",
    "继续原任务，不要新建工程。",
])
def test_negated_new_is_resume(message):
    assert not requests_new_context(message)
    assert classify_intent(message, prior_intent="build", has_active_context=True).context_relation == "resume"


def test_real_new_is_not_suppressed():
    assert requests_new_context("不要继续旧任务。新建一个 KiCad 工程。")


def test_history_restores_exact_contract_not_messages(monkeypatch):
    monkeypatch.setattr(resume_context, "has_checkpoint", lambda v: v.get("workspace_run_name") == "original")
    original = {"workspace_run_name": "original", "requirement": "STM32G070RBT6 双层",
                "messages": ["old"], "project_name": "board"}
    async def history(*a, **kw):
        yield SimpleNamespace(values={"latest_request": "继续，不新建工程"})
        yield SimpleNamespace(values=original)
    result = asyncio.run(resume_context.recover_context(
        SimpleNamespace(aget_state_history=history), {}, {"requirement": "STM32G070"}, "继续原检查点"))
    assert result["requirement"] == original["requirement"]
    assert "messages" not in result


def test_history_never_crosses_new_task(monkeypatch):
    monkeypatch.setattr(resume_context, "has_checkpoint", lambda v: False)
    async def history(*a, **kw):
        yield SimpleNamespace(values={"latest_request": "新建 KiCad 工程"})
        raise AssertionError("must not cross task boundary")
    with pytest.raises(ValueError, match="refusing"):
        asyncio.run(resume_context.recover_context(SimpleNamespace(aget_state_history=history), {},
                                                   {"requirement": "new"}, "恢复原检查点"))


def test_checkpoint_routes_directly_to_hardware(monkeypatch):
    from agents.ratsnestpro import ratsnestpro_agent as agent
    monkeypatch.setattr(agent, "_release_repair_resume_step", lambda state: "selection")
    assert agent._after_initialize({"workflow_mode": "build", "incremental_resume": True,
                                    "architecture": {"status": "blocked"}}) == agent._HARDWARE_NODE
