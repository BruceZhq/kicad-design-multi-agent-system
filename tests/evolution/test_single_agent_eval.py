import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.agents import DEFAULT_AGENT, get_agent, get_all_agent_info
from agents.ratsnestpro.profiles.registry import REGISTRY
from agents.ratsnestpro.single_agent_eval import (
    _EVIDENCE_TOOL_ALLOWLIST,
    _evidence_closure,
    _profile_budget,
    _review_passed,
    _tool_transcript,
)
from core import settings


def test_single_agent_eval_is_one_node_and_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED", False)

    assert DEFAULT_AGENT == "ratsnestpro-multi-agent"
    assert "ratsnestpro-single-agent-eval" not in {
        item.key for item in get_all_agent_info()
    }
    with pytest.raises(KeyError):
        get_agent("ratsnestpro-single-agent-eval")


def test_single_agent_eval_requires_explicit_enablement(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED", True)

    graph = get_agent("ratsnestpro-single-agent-eval").get_graph()
    executable_nodes = set(graph.nodes) - {"__start__", "__end__"}
    assert executable_nodes == {"single-agent-eval"}


def test_single_agent_eval_uses_profile_budget_without_override() -> None:
    profile = REGISTRY.all()[0].model_dump(mode="json")

    assert _profile_budget(profile) == profile["manifest"]["budget"]


def test_single_agent_tool_transcript_is_materialized_once() -> None:
    transcript = _tool_transcript(
        [
            {"tool": "tool-a", "arguments": {"query": "a"}, "result": {"status": "ok"}},
            {"tool": "tool-b", "arguments": {"query": "b"}, "result": {"status": "ok"}},
        ]
    )

    assert [type(message) for message in transcript] == [
        AIMessage,
        ToolMessage,
        AIMessage,
        ToolMessage,
    ]
    assert transcript[0].tool_calls[0]["id"] == transcript[1].tool_call_id
    assert transcript[2].tool_calls[0]["id"] == transcript[3].tool_call_id


def test_single_agent_tool_transcript_uses_non_thinking_model(monkeypatch) -> None:
    from agents.ratsnestpro import single_agent_eval as module

    monkeypatch.setattr(settings, "RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED", True)
    monkeypatch.setattr(
        module,
        "classify_intent",
        lambda requirement: type(
            "Decision",
            (),
            {"needs_clarification": False, "in_scope": True, "primary_intent": "explain"},
        )(),
    )

    async def fake_evidence(_requirement, _config):
        return [{"tool": "lookup", "arguments": {}, "result": {"status": "ok"}}]

    calls: list[object] = []
    structured_methods: list[str | None] = []

    class FakeModel:
        def with_structured_output(self, _schema, *, method=None):
            structured_methods.append(method)
            return self

        async def ainvoke(self, messages, _config):
            assert not any(isinstance(message, ToolMessage) for message in messages)
            assert not any(
                isinstance(message, AIMessage) and message.tool_calls
                for message in messages
            )
            assert any(
                isinstance(message, HumanMessage)
                and "DETERMINISTIC TOOL EVIDENCE" in str(message.content)
                and "lookup" in str(message.content)
                for message in messages
            )
            return module._SingleAgentPlan(
                execution_requirement="grounded requirement",
                answer="grounded answer",
            )

    monkeypatch.setattr(module, "_evidence_closure", fake_evidence)
    monkeypatch.setattr(module, "get_model", lambda selected: calls.append(selected) or FakeModel())

    result = asyncio.run(
        module._single_agent_node(
            {"messages": [HumanMessage(content="explain this board")]},
            {"configurable": {"model": settings.DEFAULT_MODEL}},
        )
    )

    assert calls == [settings.DEFAULT_MODEL]
    assert structured_methods == ["function_calling"]
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert result["messages"][-1].content == "grounded answer"


def test_single_agent_release_requires_explicit_overall_pass(tmp_path: Path) -> None:
    report = tmp_path / "review.md"
    report.write_text("review", encoding="utf-8")
    review = {
        "status": "ok",
        "report_path": str(report),
        "release_verdict": {
            "scope": "overall_release",
            "evaluated": True,
            "verdict": "PASS",
            "blocked": False,
        },
    }

    assert _review_passed(review) is True
    assert _review_passed({**review, "release_verdict": {"verdict": "PASS"}}) is False


def test_default_langgraph_config_does_not_expose_eval_graph() -> None:
    root = Path(__file__).parents[2]
    default = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))
    evaluation = json.loads((root / "langgraph.eval.json").read_text(encoding="utf-8"))

    assert set(default["graphs"]) == {"ratsnestpro-multi-agent"}
    assert "ratsnestpro-single-agent-eval" in evaluation["graphs"]


def test_single_agent_has_production_equivalent_evidence_classes(monkeypatch) -> None:
    from agents.ratsnestpro import single_agent_eval as module

    assert _EVIDENCE_TOOL_ALLOWLIST == {
        "ratsnest_lookup_kicad_symbol",
        "ratsnest_validate_kicad_binding",
        "ratsnest_search_internal_knowledge",
        "web_search_kicad_official_docs",
        "web_search",
        "web_search_official_manufacturer",
        "fetch_datasheet",
        "ratsnest_search_parts",
    }
    calls: list[str] = []

    async def fake_call(name, function, arguments):
        del function, arguments
        calls.append(name)
        if name in {"web_search", "web_search_official_manufacturer"}:
            result = {"status": "ok", "results": [{"href": "https://example.test/a.pdf"}]}
        elif name == "ratsnest_search_parts":
            result = {"status": "ok", "results": []}
        else:
            result = {"status": "ok", "evidence_sufficient": False}
        return {"tool": name, "arguments": {}, "result": result}

    monkeypatch.setattr(module, "_call_json_tool", fake_call)
    monkeypatch.setattr(module, "_call_langchain_json_tool", fake_call)
    monkeypatch.setattr(module, "_knowledge_scope", lambda config: {})
    monkeypatch.setattr(
        module,
        "_explicit_kicad_bindings",
        lambda requirement: [
            {"symbol_lib_id": "Timer:NE555D", "footprint_lib_id": "Package_SO:SOIC-8"}
        ],
    )
    monkeypatch.setattr(module, "_component_queries", lambda requirement: ["NE555D"])

    facts = asyncio.run(_evidence_closure("build a timer", {}))

    assert _EVIDENCE_TOOL_ALLOWLIST <= set(calls)
    assert [fact["tool"] for fact in facts] == calls


def test_single_agent_graph_has_no_role_handoff_nodes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED", True)
    graph = get_agent("ratsnestpro-single-agent-eval").get_graph()
    executable_nodes = set(graph.nodes) - {"__start__", "__end__"}

    assert executable_nodes == {"single-agent-eval"}
    assert all("handoff" not in name for name in executable_nodes)
