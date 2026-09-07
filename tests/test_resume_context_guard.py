import asyncio
from types import SimpleNamespace

import pytest

from agents.ratsnestpro.intent_router import classify_intent, requests_new_context
from agents.ratsnestpro import resume_context


@pytest.mark.parametrize("message", [
    "继续当前 KiCad 构建任务，不新建 Run 或工程。沿用原始需求和原检查点，不重跑前两步。",
    "Resume the PCB task. Do not start a new project.",
    "继续原任务，不要新建工程。",
    "继续当前工程，这是原任务断点续跑，不是新建板卡。",
    "恢复原检查点，并非新建工程。",
    "恢复当前任务，这不属于新建项目。",
    "Resume the PCB task. This is not a new project.",
])
def test_negated_new_is_resume(message):
    assert not requests_new_context(message)
    assert classify_intent(message, prior_intent="build", has_active_context=True).context_relation == "resume"


def test_real_new_is_not_suppressed():
    assert requests_new_context("不要继续旧任务。新建一个 KiCad 工程。")


@pytest.mark.parametrize("message", [
    "刚才不是新建工程。现在新建一个 KiCad 工程。",
    "That was not a new project. Now start a new project.",
])
def test_later_positive_new_project_is_not_suppressed(message):
    assert requests_new_context(message)


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


def test_conditional_repair_policy_is_not_a_requirement_amendment():
    from agents.ratsnestpro.intent_router import classify_intent
    policy = "继续原任务，若真实证据要求变更布线方案，仅对故障所有者做必要局部变更，不要整板重跑。"
    assert classify_intent(policy, prior_intent="build", has_active_context=True).context_relation == "resume"
    actual_change = "继续原任务，如果需要，把电压改为12V，仅对故障所有者做必要局部变更。"
    assert classify_intent(actual_change, prior_intent="build", has_active_context=True).context_relation == "amend"


def test_false_amendment_recovers_contract_even_when_workspace_exists(monkeypatch):
    policy = "继续原任务，若真实证据要求变更布线方案，仅对故障所有者做必要局部变更。"
    original = {"workspace_run_name": "owned", "requirement": "original board",
                "workflow_mode": "build", "architecture": {"status": "ok"}}
    damaged = {**original, "requirement": "original board plus recovery policy",
               "latest_request": policy, "intent": {"context_relation": "amend"},
               "architecture": {}}
    monkeypatch.setattr(resume_context, "has_checkpoint", lambda v: v.get("workspace_run_name") == "owned")
    async def history(*args, **kwargs):
        yield SimpleNamespace(values=damaged)
        yield SimpleNamespace(values=original)
    result = asyncio.run(resume_context.recover_context(
        SimpleNamespace(aget_state_history=history), {}, damaged, "继续原任务，从原检查点恢复",
    ))
    assert result["requirement"] == "original board"
    assert result["architecture"] == {"status": "ok"}


def test_checkpoint_routes_directly_to_hardware(monkeypatch):
    from agents.ratsnestpro import ratsnestpro_agent as agent
    monkeypatch.setattr(agent, "_release_repair_resume_step", lambda state: "selection")
    assert agent._after_initialize({"workflow_mode": "build", "incremental_resume": True,
                                    "architecture": {"status": "blocked"}}) == agent._HARDWARE_NODE


def test_wrapped_resume_restores_original_contract_after_false_new_head(monkeypatch):
    from langchain_core.messages import HumanMessage

    from agents.ratsnestpro import ratsnestpro_agent as agent
    from agents.ratsnestpro.profiles.registry import REGISTRY

    feedback = (
        "继续当前 STM32G070RBT6 工程，沿用原始需求、已确认工程参数、原工作区和最新有效检查点。"
        "这是原任务断点续跑，不是新建板卡。请从当前检查点的 layout_general 第11步继续，"
        "保留前10步，不重新选型或重做原理图。"
    )
    requirement = (
        "设计 STM32G070RBT6 双层控制板，板框尺寸 70x45mm。"
        "使用两针连接器输入 5 V，由 AP2112K-3.3 稳压。"
        "使用内部时钟、标准 10-pin Cortex SWD 接口和四个 M2 安装孔。"
    )
    original = {
        "requirement": requirement,
        "workflow_mode": "build",
        "run_name": "original-run",
        "workspace_run_name": "original-workspace",
        "project_name": "board",
        "capability_profile": REGISTRY.all()[0].model_dump(mode="json"),
        "architecture": {"status": "ok"},
        "parts": {"status": "ok"},
        "open_decisions": [],
        "trace": [],
    }
    damaged = {
        **original,
        "requirement": feedback,
        "latest_request": feedback,
        "workspace_run_name": "wrong-new-workspace",
        "intent": {"context_relation": "new"},
    }
    monkeypatch.setattr(
        resume_context, "has_checkpoint",
        lambda values: values.get("workspace_run_name") == "original-workspace",
    )

    async def history(*args, **kwargs):
        yield SimpleNamespace(values=damaged)
        yield SimpleNamespace(values=original)

    async def replay():
        recovered = await resume_context.recover_context(
            SimpleNamespace(aget_state_history=history), {}, damaged,
            "USER CHANGE REQUEST:\n" + feedback,
        )
        return await agent.initialize(
            {**damaged, **recovered,
             "messages": [HumanMessage(content="USER CHANGE REQUEST:\n" + feedback)]},
            {"configurable": {"client_thread_id": "thread-1", "user_id": "user-1"}},
        )

    result = asyncio.run(replay())

    assert result["requirement"] == requirement
    assert result["workspace_run_name"] == "original-workspace"
    assert result["incremental_resume"] is True
    assert not result["open_decisions"]
