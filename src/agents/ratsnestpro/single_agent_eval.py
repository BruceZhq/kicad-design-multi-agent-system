"""Evaluation-only single-agent control for RatsNestPro.

This is deliberately one LangGraph node with one continuous LLM context. It
does not invoke the production role subgraphs or manufacture role handoffs.
The control reuses the production evidence functions, durable Hardware
Temporal workflow, independent deterministic review, and artifact publisher.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from agents.ratsnestpro.artifact_publisher import (
    artifact_workspace_root,
    publish_artifact_manifest,
)
from agents.ratsnestpro.call_limits import await_with_deadline
from agents.ratsnestpro.intent_router import classify_intent
from agents.ratsnestpro.profiles import gate_build_profile, render_profile_boundary
from agents.ratsnestpro.ratsnestpro_agent import (
    _CAPABILITY_PROFILE_REFERENCE_RE,
    _component_queries,
    _explicit_kicad_bindings,
    _knowledge_scope,
    _safe_name,
    _temporal_progress,
    _validate_hardware_result,
    _workspace_run_key,
)
from agents.ratsnestpro.temporal.client import (
    await_hardware_workflow,
    dispatch_hardware_workflow,
    temporal_enabled,
)
from agents.ratsnestpro.tools import (
    ratsnest_lookup_kicad_symbol,
    ratsnest_review_kicad_project,
    ratsnest_search_internal_knowledge,
    ratsnest_search_parts,
    ratsnest_validate_kicad_binding,
)
from agents.ratsnestpro.web_tools import (
    fetch_datasheet,
    web_search,
    web_search_official_manufacturer,
)
from core import get_model, settings


class SingleAgentEvalState(MessagesState, total=False):
    artifact_manifest: dict[str, Any]
    capability_profile: dict[str, Any]
    hardware: dict[str, Any]
    review: dict[str, Any]


class _SingleAgentPlan(BaseModel):
    """One model-owned plan; deterministic tools remain authoritative."""

    model_config = ConfigDict(extra="forbid")

    execution_requirement: str = Field(min_length=1, max_length=30_000)
    answer: str = Field(min_length=1, max_length=12_000)


class _ToolFact(TypedDict):
    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]


_SYSTEM_PROMPT = """You are the single-agent control arm in a paired PCB evaluation.
You have one continuous context and no specialist roles or role handoffs. Use only the
supplied tool evidence. Never claim that a symbol, footprint, part, artifact, ERC, DRC,
routing result, review, or release gate passed unless the evidence says so. Preserve
every user constraint in execution_requirement. For build work, write a precise KiCad
pipeline requirement; deterministic pipeline and review gates make the final verdict.
"""

# The control arm keeps one model context and one graph node, but it is not a
# deliberately weakened baseline. These are the same evidence classes used by
# production Architect/Parts phases for an already-installed exact binding.
_EVIDENCE_TOOL_ALLOWLIST = frozenset(
    {
        "ratsnest_lookup_kicad_symbol",
        "ratsnest_validate_kicad_binding",
        "ratsnest_search_internal_knowledge",
        "web_search_kicad_official_docs",
        "web_search",
        "web_search_official_manufacturer",
        "fetch_datasheet",
        "ratsnest_search_parts",
    }
)


def _event(status: str, *, attributes: dict[str, Any] | None = None) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    event: dict[str, Any] = {
        "kind": "workflow_event",
        "phase": "single-agent-eval",
        "status": status,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    if attributes:
        event.update(attributes)
    writer(event)


def _manifest_event(manifest: dict[str, Any]) -> None:
    try:
        get_stream_writer()({"kind": "artifact_manifest", **manifest})
    except RuntimeError:
        pass


def _json_result(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "error": "tool returned invalid JSON"}
    return value if isinstance(value, dict) else {"status": "error"}


def _public_args(arguments: dict[str, Any]) -> dict[str, Any]:
    private = {
        "principal_scope",
        "tenant_scope",
        "project_scope",
        "run_scope",
        "harness_version_id",
        "harness_manifest_digest",
        "governance_scope_token",
    }
    return {key: value for key, value in arguments.items() if key not in private}


def _tool_messages(fact: _ToolFact) -> list[Any]:
    call_id = str(uuid4())
    raw = json.dumps(fact["result"], ensure_ascii=False, default=str)
    return [
        AIMessage(
            content=f"Executing required tool: {fact['tool']}",
            tool_calls=[
                {
                    "name": fact["tool"],
                    "args": _public_args(fact["arguments"]),
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content=raw[:24_000], tool_call_id=call_id),
    ]


def _profile_budget(profile: dict[str, Any]) -> dict[str, int]:
    raw = profile.get("manifest", {}).get("budget", {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: int(raw[key])
        for key in (
            "max_wall_clock_minutes",
            "max_llm_tokens",
            "max_ahe_repairs",
            "max_same_failure_retries",
        )
        if isinstance(raw.get(key), int)
    }


def _tool_transcript(facts: list[_ToolFact]) -> list[Any]:
    return [message for fact in facts for message in _tool_messages(fact)]


def _planning_evidence_message(facts: list[_ToolFact]) -> HumanMessage:
    evidence = [
        {
            "tool": fact["tool"],
            "arguments": _public_args(fact["arguments"]),
            "result_json": json.dumps(
                fact["result"], ensure_ascii=False, default=str
            )[:24_000],
        }
        for fact in facts
    ]
    return HumanMessage(
        content=(
            "DETERMINISTIC TOOL EVIDENCE — authoritative, read-only results. "
            "Use these results to produce the requested plan; do not request or replay "
            "tools:\n"
            + json.dumps(evidence, ensure_ascii=False, default=str)
        )
    )


def _review_passed(review: dict[str, Any]) -> bool:
    verdict = review.get("release_verdict")
    if not isinstance(verdict, dict):
        return False
    report_path = Path(str(review.get("report_path", "")))
    return (
        review.get("status") == "ok"
        and report_path.is_file()
        and verdict.get("scope") == "overall_release"
        and verdict.get("evaluated") is True
        and verdict.get("verdict") == "PASS"
        and verdict.get("blocked") is False
    )


async def _call_json_tool(
    name: str,
    function: Any,
    arguments: dict[str, Any],
) -> _ToolFact:
    raw = await asyncio.to_thread(function, **arguments)
    result = _json_result(raw)
    _event(
        "tool_completed",
        attributes={
            "event_type": "tool_call",
            "tool": name,
            "outcome": str(result.get("status", "unknown"))[:80],
            "arguments_schema_valid": True,
            # Search/lookup postcondition is a well-formed, explicit result,
            # including honest no-results/unavailable outcomes.
            "postcondition_satisfied": result.get("status") != "error",
        },
    )
    return {"tool": name, "arguments": arguments, "result": result}


async def _call_langchain_json_tool(
    name: str,
    function: Any,
    arguments: dict[str, Any],
) -> _ToolFact:
    return await _call_json_tool(
        name,
        lambda **values: function.invoke(values),
        arguments,
    )


def _first_pdf(result: dict[str, Any]) -> str | None:
    for item in result.get("results", []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("href", "")).strip()
        if url.startswith("https://") and url.lower().endswith(".pdf"):
            return url
    return None


async def _evidence_closure(
    requirement: str,
    config: RunnableConfig,
) -> list[_ToolFact]:
    """Collect the production-equivalent installed-library evidence surface."""

    scope = _knowledge_scope(config)
    facts: list[_ToolFact] = []
    architect_knowledge = await _call_json_tool(
        "ratsnest_search_internal_knowledge",
        ratsnest_search_internal_knowledge,
        {
            "query": f"board architecture power interfaces KiCad {requirement[:1_500]}",
            "role": "architect",
            "limit": 6,
            "evidence_types": [
                "datasheet",
                "application_note",
                "reference_design",
                "kicad_documentation",
                "internal_standard",
            ],
            **scope,
        },
    )
    facts.append(architect_knowledge)
    if architect_knowledge["result"].get("evidence_sufficient") is not True:
        kicad_search = await _call_langchain_json_tool(
            "web_search_kicad_official_docs",
            web_search,
            {
                "query": (
                    "site:docs.kicad.org official KiCad symbol footprint library "
                    "kicad-cli ERC DRC"
                )
            },
        )
        manufacturer_search = await _call_langchain_json_tool(
            "web_search",
            web_search,
            {
                "query": (
                    f"{requirement[:400]} official manufacturer datasheet PDF pin "
                    "assignment package land pattern hardware design reference"
                )
            },
        )
        facts.extend((kicad_search, manufacturer_search))
        if pdf_url := _first_pdf(manufacturer_search["result"]):
            facts.append(
                await _call_langchain_json_tool(
                    "fetch_datasheet",
                    fetch_datasheet,
                    {
                        "url": pdf_url,
                        "query": "pinout package land pattern application circuit",
                        "max_pages": 8,
                    },
                )
            )

    for binding in _explicit_kicad_bindings(requirement):
        facts.append(
            await _call_json_tool(
                "ratsnest_validate_kicad_binding",
                ratsnest_validate_kicad_binding,
                {
                    "symbol_lib_id": binding["symbol_lib_id"],
                    "footprint_lib_id": binding["footprint_lib_id"],
                },
            )
        )

    for query in _component_queries(requirement):
        facts.append(
            await _call_json_tool(
                "ratsnest_lookup_kicad_symbol",
                ratsnest_lookup_kicad_symbol,
                {"query": query, "limit": 3},
            )
        )
        knowledge = await _call_json_tool(
            "ratsnest_search_internal_knowledge",
            ratsnest_search_internal_knowledge,
            {
                "query": f"{query} datasheet lifecycle alternative KiCad binding",
                "role": "parts-specialist",
                "limit": 5,
                "evidence_types": [
                    "datasheet",
                    "approved_vendor_list",
                    "historical_bom",
                    "lifecycle",
                    "alternate_part",
                    "kicad_binding",
                ],
                **scope,
            },
        )
        catalog = await _call_json_tool(
            "ratsnest_search_parts",
            ratsnest_search_parts,
            {"query": query, "limit": 10},
        )
        facts.extend((knowledge, catalog))
        if (
            knowledge["result"].get("evidence_sufficient") is not True
            and not catalog["result"].get("results")
        ):
            official = await _call_langchain_json_tool(
                "web_search_official_manufacturer",
                web_search_official_manufacturer,
                {
                    "query": (
                        f"{query} official manufacturer datasheet PDF pinout package "
                        "land pattern application circuit"
                    )
                },
            )
            facts.append(official)
            if pdf_url := _first_pdf(official["result"]):
                facts.append(
                    await _call_langchain_json_tool(
                        "fetch_datasheet",
                        fetch_datasheet,
                        {
                            "url": pdf_url,
                            "query": (
                                f"{query} pinout package land pattern recommended "
                                "operating conditions application circuit"
                            ),
                            "max_pages": 5,
                        },
                    )
                )
    return facts


def _latest_requirement(state: SingleAgentEvalState) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content).strip()
    return ""


def _execution_names(requirement: str, config: RunnableConfig) -> tuple[str, str]:
    configurable = config.get("configurable", {})
    run_name = _safe_name(str(configurable.get("run_name", "single-agent-eval")), "eval")
    project_name = _safe_name(str(configurable.get("project_name", "board")), "board")
    identity = "\0".join(
        (
            str(configurable.get("user_id", "anonymous")),
            str(configurable.get("client_thread_id", configurable.get("thread_id", "eval"))),
        )
    )
    execution_scope = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return _workspace_run_key(run_name, requirement, execution_scope), project_name


async def _single_agent_node(
    state: SingleAgentEvalState,
    config: RunnableConfig,
) -> dict[str, Any]:
    if not settings.RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED:
        raise RuntimeError("single-agent evaluation arm is disabled")
    requirement = _latest_requirement(state)
    decision = classify_intent(requirement)
    if decision.needs_clarification:
        answer = interrupt(
            {
                "kind": "clarification",
                "question": decision.clarification_question,
                "agent": "ratsnestpro-single-agent-eval",
            }
        )
        requirement = f"{requirement}\n\nUSER CLARIFICATION ANSWER:\n{answer}"
        decision = classify_intent(requirement, explicit_mode="build")

    _event(
        "started",
        attributes={"event_type": "intent_decision", "intent": decision.primary_intent},
    )
    if not decision.in_scope or decision.primary_intent == "unsupported":
        _event("completed")
        return {"messages": [AIMessage(content="This request is outside the PCB evaluation scope.")]}

    capability_profile: dict[str, Any] = {}
    if decision.primary_intent == "build":
        resolved_profile, profile_error = gate_build_profile(
            config.get("configurable", {}).get("capability_profile")
        )
        if resolved_profile is None:
            _event("execution_blocked")
            return {
                "capability_profile": {},
                "messages": [
                    AIMessage(
                        content=(
                            "Single-agent evaluation blocked at the capability boundary: "
                            f"{profile_error}"
                        )
                    )
                ],
            }
        requested_profile = _CAPABILITY_PROFILE_REFERENCE_RE.search(requirement)
        if (
            requested_profile is not None
            and requested_profile.group(1).casefold()
            != str(resolved_profile.get("reference", "")).casefold()
        ):
            _event("execution_blocked")
            return {
                "capability_profile": resolved_profile,
                "messages": [
                    AIMessage(
                        content=(
                            "Single-agent evaluation blocked: the request names capability "
                            "profile "
                            f"{requested_profile.group(1)}, but the frozen run envelope "
                            f"selected {resolved_profile.get('reference', 'unknown')}."
                        )
                    )
                ],
            }
        capability_profile = resolved_profile

    facts = await _evidence_closure(requirement, config)

    context: list[Any] = [SystemMessage(content=_SYSTEM_PROMPT)]
    if capability_profile:
        context.append(
            SystemMessage(
                content=(
                    "VALIDATED CAPABILITY PROFILE — authoritative scope, budget, and "
                    f"acceptance boundary:\n{render_profile_boundary(capability_profile)}"
                )
            )
        )
    context.append(HumanMessage(content=requirement))
    tool_transcript = _tool_transcript(facts)
    context.append(_planning_evidence_message(facts))
    selected_model = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
    # Keep provider-specific tool history out of the planning request. The real
    # transcript is still returned below for UI telemetry and paired metrics.
    runnable = get_model(
        selected_model,
        reasoning_effort=(
            str(configurable["reasoning_effort"])
            if configurable.get("reasoning_effort")
            else None
        ),
    ).with_structured_output(
        _SingleAgentPlan,
        method="function_calling",
    )
    plan = await await_with_deadline(
        runnable.ainvoke(context, config),
        timeout_seconds=settings.RATSNESTPRO_AGENT_CALL_TIMEOUT_SECONDS,
        operation_name="single-agent-eval:plan",
    )
    plan = plan if isinstance(plan, _SingleAgentPlan) else _SingleAgentPlan.model_validate(plan)
    messages = tool_transcript

    if decision.primary_intent != "build":
        _event("completed")
        return {"messages": [*messages, AIMessage(content=plan.answer)]}

    workspace_run_name, project_name = _execution_names(requirement, config)
    if not temporal_enabled():
        blocked = {
            "status": "error",
            "release_ready": False,
            "completed_steps": 0,
            "total_steps": 17,
            "release_blockers": ["Temporal is required for the single-agent evaluation arm"],
            "actual_files": [],
        }
        _event("execution_blocked")
        return {
            "hardware": blocked,
            "messages": [
                *messages,
                AIMessage(content="Single-agent evaluation blocked: Temporal is not enabled."),
            ],
        }

    configurable = config.get("configurable", {})
    hardware_requirement = (
        f"{requirement}\n\nSINGLE-AGENT EXECUTION PLAN — advisory; deterministic gates remain "
        f"authoritative:\n{plan.execution_requirement}"
    )
    hardware_requirement += (
        "\n\nVALIDATED CAPABILITY PROFILE — this is a scope, evidence, budget, and "
        "acceptance boundary, not a fixed circuit answer:\n"
        f"{render_profile_boundary(capability_profile)}"
    )
    run_ref = await dispatch_hardware_workflow(
        request_id=str(configurable.get("request_id", uuid4())),
        requirement=hardware_requirement,
        run_name=str(configurable.get("run_name", "single-agent-eval")),
        workspace_run_name=workspace_run_name,
        execution_scope=workspace_run_name.split("--")[-2],
        project_name=project_name,
        llm_mode="required",
        model_name=getattr(selected_model, "value", str(selected_model)),
        model_type=type(selected_model).__name__,
        reasoning_effort=(
            str(configurable["reasoning_effort"])
            if configurable.get("reasoning_effort")
            else None
        ),
        vision_model_name=(
            str(configurable["vision_model"])
            if configurable.get("vision_model")
            else None
        ),
        vision_reasoning_effort=(
            str(configurable["vision_reasoning_effort"])
            if configurable.get("vision_reasoning_effort")
            else None
        ),
        attempt=1,
        ahe_budget=_profile_budget(capability_profile),
        tenant_scope=str(configurable.get("tenant_scope", "")),
        project_scope=str(configurable.get("project_scope", "")),
        run_scope=str(configurable.get("run_scope", "")),
        harness_version_id=str(configurable.get("harness_version_id", "")),
        harness_manifest_digest=str(configurable.get("harness_manifest_digest", "")),
        governance_scope_token=str(configurable.get("governance_scope_token", "")),
    )
    raw_hardware = await await_hardware_workflow(run_ref, on_progress=_temporal_progress)
    hardware = _validate_hardware_result(raw_hardware)
    _event(
        "tool_completed",
        attributes={
            "event_type": "tool_call",
            "tool": "ratsnest_temporal_hardware_workflow",
            "outcome": str(hardware.get("status", "unknown"))[:80],
            "arguments_schema_valid": True,
            "postcondition_satisfied": hardware.get("completed_steps") == 17,
            "completed_steps": int(hardware.get("completed_steps", 0) or 0),
        },
    )

    review_args = {
        "project_path": str(hardware.get("run_directory", "")),
        "report_name": f"{project_name}-single-agent-eval-review.md",
        "llm_mode": "offline",
        "upstream_release_ready": hardware.get("release_ready") is True,
        "upstream_release_blockers": list(hardware.get("release_blockers", [])),
        "upstream_release_identity": hardware.get("release_identity"),
    }
    review = _json_result(await asyncio.to_thread(ratsnest_review_kicad_project, **review_args))
    review_ok = _review_passed(review)
    _event(
        "tool_completed",
        attributes={
            "event_type": "tool_call",
            "tool": "ratsnest_review_kicad_project",
            "outcome": str(review.get("status", "unknown"))[:80],
            "arguments_schema_valid": True,
            "postcondition_satisfied": review_ok,
        },
    )
    release_ready = hardware.get("release_ready") is True and review_ok
    delivery_status = (
        "release_ready"
        if release_ready
        else "delivered_with_issues"
        if hardware.get("actual_files")
        else "execution_blocked"
    )
    manifest = publish_artifact_manifest(
        paths=[str(path) for path in hardware.get("actual_files", [])],
        workspace=str(artifact_workspace_root()),
        run_id=str(configurable.get("request_id", workspace_run_name)),
        delivery_status=delivery_status,
    )
    _manifest_event(manifest)
    _event("completed", attributes={"delivery_status": manifest["delivery_status"]})
    summary = (
        f"Single-agent evaluation completed {hardware.get('completed_steps', 0)}/17 steps. "
        f"Deterministic release_ready={manifest['delivery_status'] == 'release_ready'}. "
        f"Release blockers: {hardware.get('release_blockers', [])}.\n\n{plan.answer}"
    )
    return {
        "hardware": hardware,
        "review": review,
        "capability_profile": capability_profile,
        "artifact_manifest": manifest,
        "messages": [*messages, AIMessage(content=summary)],
    }


builder = StateGraph(SingleAgentEvalState)
builder.add_node("single-agent-eval", _single_agent_node)
builder.add_edge(START, "single-agent-eval")
builder.add_edge("single-agent-eval", END)
ratsnestpro_single_agent_eval = builder.compile(name="ratsnestpro-single-agent-eval")
