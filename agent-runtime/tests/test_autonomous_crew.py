"""Contract and control-loop tests for the autonomous design crew."""

import pytest
from pydantic import ValidationError

from ratsnest.config import Config
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.contracts import (
    AgentMessage,
    AgentResultPayload,
    BoardComponent,
    BoardConnection,
    BoardOutline,
    BoardPlan,
    FindingPayload,
    MessageKind,
    ToolCall,
)
from ratsnest.crews.design_agents import (
    PcbDesigner,
    RepairAgent,
    SchematicDesigner,
)
from ratsnest.crews.design_tools import route_key
from ratsnest.llm import BrainRequiredError, LlmClient
from ratsnest.schemas import EvaluationResult, Finding, Scorecard, StrategyBundle


class FakeLlm:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []
        self.available = True

    def complete_json(self, agent, system, user, max_tokens=0):
        self.calls.append(agent)
        return self.responses.get(agent)


class FakeToolbox:
    """In-memory file-authority substitute; production uses real KiCad files."""

    def __init__(self, blackboard: DesignBlackboard):
        self.blackboard = blackboard

    def observe(self) -> dict:
        return self.blackboard.state.model_dump(
            mode="json", exclude={"messages", "tool_history"})

    def execute(self, agent: str, call: ToolCall) -> dict:
        state = self.blackboard.state
        args = call.arguments
        if call.tool == "create_project":
            state.project_created = True
        elif call.tool == "place_component":
            state.observed_components.append(str(args["ref"]))
        elif call.tool == "connect_pin":
            state.observed_pin_nets[
                f"{args['ref']}:{args['pin']}"] = str(args["net"])
        elif call.tool == "sync_board":
            state.board_exists = True
            state.board_synced = True
            state.observed_footprints = [
                component.ref for component in state.board_plan.components]
        elif call.tool == "set_board_outline":
            state.outline_set = True
        elif call.tool == "place_footprint":
            state.placed_footprints.append(str(args["ref"]))
        elif call.tool == "autoroute_board":
            state.autorouted = True
            state.routing_mode = "freerouting"
        self.blackboard.record_tool(agent, call, True, {"ok": True})
        return {"ok": True}


def _board_plan() -> BoardPlan:
    return BoardPlan(
        topology="adjustable_linear_regulator",
        components=[
            BoardComponent(
                ref="J1", symbol="Connector_Generic:Conn_01x02",
                value="Conn_01x02"),
            BoardComponent(
                ref="U1", symbol="Regulator_Linear:AP1117-ADJ",
                value="AP1117-ADJ"),
        ],
        connections=[
            BoardConnection(net="VIN", ref="J1", pin="1"),
            BoardConnection(net="VIN", ref="U1", pin="3"),
            BoardConnection(net="GND", ref="J1", pin="2"),
            BoardConnection(net="GND", ref="U1", pin="1"),
        ],
        outline=BoardOutline(width=50, height=35),
        rationale="test plan")


def _crew_context(tmp_path):
    blackboard = DesignBlackboard(str(tmp_path))
    blackboard.state.board_plan = _board_plan()
    strategy = StrategyBundle(name="test")
    return blackboard, strategy, FakeToolbox(blackboard)


def _action(tool: str, arguments: dict | None = None) -> dict:
    return {
        "tool": tool,
        "arguments": arguments or {},
        "reason": f"execute {tool}",
        "expected_result": f"{tool} completed",
    }


def test_board_plan_rejects_unknown_refs_and_duplicate_pins():
    with pytest.raises(ValidationError, match="unknown component"):
        BoardPlan(
            topology="test_topology",
            components=[BoardComponent(
                ref="J1", symbol="Connector_Generic:Conn_01x02", value="J")],
            connections=[BoardConnection(net="VIN", ref="U9", pin="1")])

    with pytest.raises(ValidationError, match="multiple nets"):
        BoardPlan(
            topology="test_topology",
            components=[BoardComponent(
                ref="J1", symbol="Connector_Generic:Conn_01x02", value="J")],
            connections=[
                BoardConnection(net="VIN", ref="J1", pin="1"),
                BoardConnection(net="GND", ref="J1", pin="1"),
            ])


def test_contracts_forbid_unknown_fields_and_message_type_mismatch(tmp_path):
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolCall(tool="save_project", shell_command="rm -rf")

    with pytest.raises(ValidationError):
        AgentMessage(
            sender="architect", recipient="designer", kind=MessageKind.task,
            payload=_board_plan().model_dump(mode="json"))

    finding = Finding(detector="erc", rule_id="ERC-1", severity="error")
    message = DesignBlackboard(str(tmp_path)).publish(
        "verification_crew", "repair_agent", MessageKind.finding,
        {"stage": "schematic", "finding_id": finding.finding_id(),
         "finding": finding.model_dump(mode="json")})
    assert isinstance(message.payload, FindingPayload)


def test_schematic_agent_executes_llm_tool_plan(tmp_path):
    blackboard, strategy, toolbox = _crew_context(tmp_path)
    llm = FakeLlm({"schematic_designer": {
        "goal": "materialize schematic",
        "actions": [
            _action("create_project"),
            _action("place_component", {"ref": "J1", "x": 75, "y": 60}),
            _action("place_component", {"ref": "U1", "x": 100, "y": 60}),
            *[_action("connect_pin", item.model_dump(mode="json"))
              for item in blackboard.state.board_plan.connections],
            _action("save_project"),
        ],
        "expected_result": "BoardPlan is present in KiCad",
        "rationale": "smallest complete action batch",
        "done": False,
    }})

    result = SchematicDesigner(
        strategy, blackboard, toolbox, llm=llm).run()

    assert result is True
    assert llm.calls == ["schematic_designer"]
    assert [entry.call.tool for entry in blackboard.state.tool_history] == [
        "create_project", "place_component", "place_component",
        "connect_pin", "connect_pin", "connect_pin", "connect_pin",
        "save_project"]
    assert blackboard.state.messages[-1].kind == MessageKind.result
    assert isinstance(
        blackboard.state.messages[-1].payload, AgentResultPayload)


def test_schematic_agent_rejects_unauthorized_tool_and_recovers(tmp_path):
    blackboard, strategy, toolbox = _crew_context(tmp_path)
    llm = FakeLlm({"schematic_designer": {
        "goal": "route from the schematic agent",
        "actions": [_action("route_connection", {
            "net": "VIN", "from_ref": "J1", "from_pin": "1",
            "to_ref": "U1", "to_pin": "3"})],
        "expected_result": "invalid cross-capability action",
        "done": False,
    }})

    result = SchematicDesigner(
        strategy, blackboard, toolbox, llm=llm).run()

    assert result is True
    executed = {entry.call.tool for entry in blackboard.state.tool_history}
    assert "route_connection" not in executed
    assert executed <= SchematicDesigner.allowed_tools
    assert llm.calls


def test_required_brain_never_falls_back_to_deterministic_tools(tmp_path):
    blackboard, strategy, toolbox = _crew_context(tmp_path)
    config = Config.load()
    config.llm_enabled = True
    config.llm_required = True
    config.llm_provider = "deepseek"
    config.llm_api_key = None

    with pytest.raises(BrainRequiredError):
        SchematicDesigner(
            strategy, blackboard, toolbox, llm=LlmClient(config)).run()

    assert blackboard.state.tool_history == []


def test_pcb_agent_executes_llm_tool_plan(tmp_path):
    blackboard, strategy, toolbox = _crew_context(tmp_path)
    # create_project may leave an empty PCB file; existence is not sync.
    blackboard.state.board_exists = True
    llm = FakeLlm({"pcb_designer": {
        "goal": "materialize PCB",
        "actions": [
            _action("sync_board"),
            _action("set_board_outline", {"width": 50, "height": 35}),
            _action("place_footprint", {"ref": "J1", "x": 5, "y": 10}),
            _action("place_footprint", {"ref": "U1", "x": 20, "y": 10}),
            _action("autoroute_board"),
            _action("save_project"),
        ],
        "expected_result": "PCB is ready for verification",
        "done": False,
    }})

    result = PcbDesigner(strategy, blackboard, toolbox, llm=llm).run()

    assert result is True
    assert llm.calls == ["pcb_designer"]
    assert blackboard.state.autorouted is True
    assert blackboard.state.routing_mode == "freerouting"
    assert blackboard.state.outline_set is True


def test_repair_agent_uses_typed_assignments_and_falls_back(tmp_path):
    blackboard, strategy, _ = _crew_context(tmp_path)
    finding = Finding(
        detector="erc", rule_id="ERC-UNCONNECTED", severity="error",
        summary="schematic pin is unconnected", component="U1")
    evaluation = EvaluationResult(
        project_dir=str(tmp_path),
        scorecard=Scorecard(score=70, severity_counts={"error": 1}),
        findings=[finding])
    llm = FakeLlm({"repair_agent": {
        "assignments": [{
            "assignee": "pcb_designer",
            "goal": "invent an unrelated PCB repair",
            "finding_ids": ["UNKNOWN:global"],
            "acceptance_criteria": ["unknown finding disappears"],
        }]}})

    tasks = RepairAgent(strategy, blackboard, llm=llm).assign(evaluation)

    assert len(tasks) == 1
    assert tasks[0].assignee == "schematic_designer"
    assert tasks[0].context["finding_ids"] == [finding.finding_id()]
    assert blackboard.state.messages[-1].kind == MessageKind.task


def test_schematic_agent_resumes_without_replaying_completed_component(tmp_path):
    checkpoint = tmp_path / "design_state.json"
    blackboard = DesignBlackboard(
        str(tmp_path), checkpoint_path=checkpoint)
    blackboard.state.board_plan = _board_plan()
    blackboard.state.project_created = True
    blackboard.state.observed_components = ["J1"]
    blackboard.checkpoint()
    resumed = DesignBlackboard.resume(
        str(tmp_path), checkpoint_path=checkpoint)
    toolbox = FakeToolbox(resumed)

    assert SchematicDesigner(
        StrategyBundle(name="test"), resumed, toolbox).run() is True

    placed_refs = [
        entry.call.arguments["ref"]
        for entry in resumed.state.tool_history
        if entry.call.tool == "place_component"
    ]
    assert placed_refs == ["U1"]
