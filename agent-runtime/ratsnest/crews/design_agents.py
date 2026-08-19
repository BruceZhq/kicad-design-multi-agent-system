"""Autonomous, contract-bounded agents for KiCad design creation.

The agents own goals and choose high-level ToolCalls.  Tool services remain
deterministic and file-authoritative; every plan is validated before acting.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ratsnest.config import Config
from ratsnest.circuit_math import SolvedCircuit
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.circuit_families import build_canonical_plan
from ratsnest.crews.contracts import (
    AgentPlan,
    AgentTask,
    BoardConnection,
    BoardOutline,
    BoardPlan,
    MessageKind,
    TaskStatus,
    ToolCall,
)
from ratsnest.crews.design_tools import (
    KiCadDesignToolbox,
    ToolServiceError,
    route_key,
)
from ratsnest.data_proxy import Recorder
from ratsnest.protocols import LlmBrain
from ratsnest.schemas import DesignSpec, EvaluationResult, StrategyBundle

class AgentContractError(RuntimeError):
    pass


def _brain_required(llm: LlmBrain | None) -> bool:
    return bool(getattr(llm, "required", False))


def _successful_calls(blackboard: DesignBlackboard, tool: str) -> list[ToolCall]:
    return [entry.call for entry in blackboard.state.tool_history
            if entry.success and entry.call.tool == tool]


def _saved_after_changes(blackboard: DesignBlackboard,
                         mutating_tools: set[str]) -> bool:
    last_save = -1
    last_change = -1
    for index, entry in enumerate(blackboard.state.tool_history):
        if not entry.success:
            continue
        if entry.call.tool == "save_project":
            last_save = index
        if entry.call.tool in mutating_tools:
            last_change = index
    return last_save > last_change


class CircuitArchitect:
    """DesignSpec + solved parts -> validated BoardPlan."""

    name = "circuit_architect"

    _PROMPT = """You are the circuit architect for a constrained KiCad design
system. Return ONLY a BoardPlan JSON object. The supported topology and the
electrically solved component catalog/connections in the user payload are
authoritative: include every supplied component and connection exactly once,
without changing refs, symbols, values, footprints, properties, or nets.
The family, catalog, limits, outline, placement hints, net classes, gates, and
constraints are also immutable. You may order the graph and explain the design
rationale. Never emit tool calls or KiCad file text."""

    def __init__(self, strategy: StrategyBundle,
                 blackboard: DesignBlackboard,
                 llm: LlmBrain | None = None,
                 recorder: Recorder | None = None):
        self.strategy = strategy
        self.blackboard = blackboard
        self.llm = llm
        self.recorder = recorder

    def create_plan(self, spec: DesignSpec,
                    solved: SolvedCircuit) -> BoardPlan:
        canonical = self.canonical_plan(spec, solved)
        selected = canonical
        brain = "deterministic"
        error: str | None = None
        if self.llm is not None:
            attempted = self.llm.available
            system = self._PROMPT
            custom = self.strategy.prompts.get(self.name)
            if custom:
                system += f"\nAdditional strategy policy:\n{custom}"
            raw = self.llm.complete_json(
                self.name, system,
                json.dumps({
                    "requirement": spec.model_dump(mode="json"),
                    "supported_topology": solved.topology,
                    "authoritative_plan": canonical.model_dump(mode="json"),
                    "outline_bounds_mm": {"width": [20, 200],
                                          "height": [20, 150]},
                }), max_tokens=2500)
            if raw:
                try:
                    candidate = BoardPlan.model_validate(raw)
                    self.validate_candidate(candidate, canonical)
                    selected = candidate
                    brain = "llm"
                except (ValidationError, ValueError) as exc:
                    error = str(exc)[:300]
                    brain = "deterministic_fallback"
                    if _brain_required(self.llm):
                        raise AgentContractError(
                            f"circuit architect proposal rejected: {error}") from exc
            elif attempted:
                brain = "deterministic_fallback"
                error = "brain returned no valid BoardPlan"

        self.blackboard.state.board_plan = selected
        self.blackboard.state.stage = "architecture"
        self.blackboard.publish(
            self.name, "schematic_designer", MessageKind.board_plan,
            selected.model_dump(mode="json"), selected.plan_id)
        if self.recorder is not None:
            self.recorder.emit(
                "design.circuit_architect.plan", 0,
                observation={"requirement": spec.requirement_text[:300]},
                agent_state={"brain": brain},
                action={"board_plan": selected.model_dump(mode="json")},
                outcome={"accepted": True, "fallback_error": error},
                metadata={"agent": self.name, "crew": "design"})
        return selected

    def canonical_plan(self, spec: DesignSpec,
                       solved: SolvedCircuit) -> BoardPlan:
        return build_canonical_plan(spec, solved)

    @staticmethod
    def validate_candidate(candidate: BoardPlan, canonical: BoardPlan) -> None:
        if candidate.topology != canonical.topology:
            raise ValueError("unsupported topology")
        for field in (
                "family_version", "catalog_version", "design_limits",
                "outline", "placement_hints", "net_classes",
                "required_gates", "constraints"):
            if getattr(candidate, field) != getattr(canonical, field):
                raise ValueError(f"BoardPlan {field} is solver-authoritative")
        canonical_components = {
            item.ref: item.model_dump(mode="json")
            for item in canonical.components}
        candidate_components = {
            item.ref: item.model_dump(mode="json")
            for item in candidate.components}
        if candidate_components != canonical_components:
            raise ValueError("component catalog is not authoritative")
        expected_connections = {item.key() for item in canonical.connections}
        proposed_connections = {item.key() for item in candidate.connections}
        if proposed_connections != expected_connections:
            raise ValueError("connection graph differs from the solved topology")

    # Compatibility for callers from the first autonomous-Crew iteration.
    def _canonical_plan(self, spec: DesignSpec,
                        solved: SolvedCircuit) -> BoardPlan:
        return self.canonical_plan(spec, solved)

    @staticmethod
    def _validate_candidate(candidate: BoardPlan, canonical: BoardPlan) -> None:
        CircuitArchitect.validate_candidate(candidate, canonical)


class AutonomousDesignAgent:
    """Bounded Plan -> Validate -> Act -> Observe loop."""

    name = "design_agent"
    allowed_tools: frozenset[str] = frozenset()
    _PROMPT = ""

    def __init__(self, strategy: StrategyBundle,
                 blackboard: DesignBlackboard,
                 toolbox: KiCadDesignToolbox,
                 llm: LlmBrain | None = None,
                 recorder: Recorder | None = None):
        self.strategy = strategy
        self.blackboard = blackboard
        self.toolbox = toolbox
        self.llm = llm
        self.recorder = recorder
        policies = strategy.solver_params.get("tool_policies", {})
        policy = policies.get(self.name, {}) if isinstance(policies, dict) else {}
        self.max_steps = min(max(int(policy.get("max_steps", 8)), 1), 12)
        self.max_actions = min(
            max(int(policy.get("max_actions_per_step", 12)), 1), 20)

    def run(self, task: AgentTask | None = None) -> bool:
        task = task or AgentTask(
            assignee=self.name, goal=self.default_goal(),
            acceptance_criteria=self.acceptance_criteria())
        managed = task in self.blackboard.state.tasks
        if managed:
            self.blackboard.set_task_status(task, TaskStatus.running)
        self.blackboard.state.stage = self.name

        for step in range(1, self.max_steps + 1):
            observation = self.observe()
            if self.acceptance_met():
                if managed:
                    self.blackboard.set_task_status(task, TaskStatus.completed)
                self._publish_result(task, True)
                return True

            plan = self._propose(task, observation)
            brain = "llm" if plan is not None else (
                "deterministic_fallback"
                if self.llm is not None and self.llm.available
                else "deterministic")
            if plan is None:
                plan = self.deterministic_plan(task, observation)
            try:
                self.validate_plan(plan)
            except (AgentContractError, ValidationError, ValueError) as exc:
                if _brain_required(self.llm):
                    if managed:
                        self.blackboard.set_task_status(task, TaskStatus.blocked)
                    raise AgentContractError(
                        f"{self.name} plan rejected: {exc}") from exc
                plan = self.deterministic_plan(task, observation)
                self.validate_plan(plan)
                brain = "deterministic_fallback"

            if plan.done and not self.acceptance_met():
                if _brain_required(self.llm):
                    raise AgentContractError(
                        f"{self.name} declared done before acceptance criteria")
                plan = self.deterministic_plan(task, observation)
                self.validate_plan(plan)
                brain = "deterministic_fallback"
            if not plan.actions and not self.acceptance_met():
                if _brain_required(self.llm):
                    raise AgentContractError(
                        f"{self.name} returned no actions before acceptance")
                plan = self.deterministic_plan(task, observation)
                self.validate_plan(plan)
                brain = "deterministic_fallback"
            self._emit_plan(step, task, observation, plan, brain)
            if not plan.actions:
                break
            for call in plan.actions:
                try:
                    self.toolbox.execute(self.name, call)
                except ToolServiceError:
                    # The next observation exposes the failure to the planner.
                    continue

        success = self.acceptance_met()
        if managed:
            self.blackboard.set_task_status(
                task, TaskStatus.completed if success else TaskStatus.blocked)
        self._publish_result(task, success)
        return success

    def _propose(self, task: AgentTask, observation: dict) -> AgentPlan | None:
        if self.llm is None:
            return None
        system = self._PROMPT
        custom = self.strategy.prompts.get(self.name)
        if custom:
            system += f"\nAdditional strategy policy:\n{custom}"
        raw = self.llm.complete_json(
            self.name, system,
            json.dumps({
                "task": task.model_dump(mode="json"),
                "board_plan": self.blackboard.state.board_plan.model_dump(
                    mode="json") if self.blackboard.state.board_plan else None,
                "observation": observation,
                "available_tools": sorted(self.allowed_tools),
                "max_actions": self.max_actions,
            }), max_tokens=2200)
        if not raw:
            return None
        try:
            return AgentPlan.model_validate(raw)
        except ValidationError as exc:
            if _brain_required(self.llm):
                raise AgentContractError(str(exc)) from exc
            return None

    def validate_plan(self, plan: AgentPlan) -> None:
        if len(plan.actions) > self.max_actions:
            raise AgentContractError(
                f"plan has {len(plan.actions)} actions; limit is {self.max_actions}")
        signatures: set[str] = set()
        for call in plan.actions:
            if call.tool not in self.allowed_tools:
                raise AgentContractError(
                    f"{self.name} cannot call {call.tool!r}")
            signature = f"{call.tool}:{json.dumps(call.arguments, sort_keys=True)}"
            if signature in signatures:
                raise AgentContractError("duplicate tool call in one plan")
            signatures.add(signature)
            self.validate_call(call)

    def observe(self) -> dict:
        return self.toolbox.observe()

    def _emit_plan(self, step: int, task: AgentTask,
                   observation: dict, plan: AgentPlan, brain: str) -> None:
        if self.recorder is not None:
            self.recorder.emit(
                f"design.{self.name}.plan", step,
                observation=observation,
                agent_state={"task": task.model_dump(mode="json"),
                             "brain": brain},
                action=plan.model_dump(mode="json"),
                outcome={"validated": True},
                metadata={"agent": self.name, "crew": "design"})

    def _publish_result(self, task: AgentTask, success: bool) -> None:
        self.blackboard.publish(
            self.name, "verification_crew", MessageKind.result,
            {"task_id": task.task_id, "success": success,
             "acceptance_criteria": task.acceptance_criteria}, task.task_id)

    def default_goal(self) -> str:
        raise NotImplementedError

    def acceptance_criteria(self) -> list[str]:
        raise NotImplementedError

    def acceptance_met(self) -> bool:
        raise NotImplementedError

    def deterministic_plan(self, task: AgentTask,
                           observation: dict) -> AgentPlan:
        raise NotImplementedError

    def validate_call(self, call: ToolCall) -> None:
        raise NotImplementedError


class SchematicDesigner(AutonomousDesignAgent):
    name = "schematic_designer"
    allowed_tools = frozenset({
        "create_project", "place_component", "connect_pin", "save_project"})
    _PROMPT = """You are an autonomous KiCad schematic designer. Use the
BoardPlan as immutable electrical intent. Observe the current file-derived
state and return ONLY AgentPlan JSON. Choose the smallest next batch of
high-level ToolCalls. Tool arguments:
create_project {}; place_component {ref,x,y}; connect_pin {ref,pin,net};
save_project {}. You may only place planned refs and connect planned pins.
Keep schematic positions inside x=[20,277], y=[20,190]. Never emit paths,
symbols, values, footprints, raw S-expressions, or unplanned connections."""

    def default_goal(self) -> str:
        return "Materialize the BoardPlan as a complete KiCad schematic"

    def acceptance_criteria(self) -> list[str]:
        return ["all BoardPlan components exist",
                "every planned component pin is connected to its planned net",
                "project is saved"]

    def _placed(self) -> set[str]:
        placed = set(self.blackboard.state.observed_components)
        placed.update(str(call.arguments.get("ref"))
                      for call in _successful_calls(
                          self.blackboard, "place_component"))
        return placed

    def _connected(self) -> set[str]:
        connected = {
            f"{ref_pin}:{net}" for ref_pin, net
            in self.blackboard.state.observed_pin_nets.items()}
        connected.update(
            f"{call.arguments.get('ref')}:{call.arguments.get('pin')}:"
            f"{call.arguments.get('net')}"
            for call in _successful_calls(self.blackboard, "connect_pin"))
        return connected

    def acceptance_met(self) -> bool:
        plan = self.blackboard.state.board_plan
        if plan is None or not self.blackboard.state.project_created:
            return False
        refs = {component.ref for component in plan.components}
        connections = {connection.key() for connection in plan.connections}
        saved = _saved_after_changes(
            self.blackboard, {"create_project", "place_component", "connect_pin"})
        return refs <= self._placed() and connections <= self._connected() and saved

    def deterministic_plan(self, task: AgentTask,
                           observation: dict) -> AgentPlan:
        plan = self.blackboard.state.board_plan
        assert plan is not None
        actions: list[ToolCall] = []
        if not self.blackboard.state.project_created:
            actions.append(ToolCall(
                tool="create_project", reason="project does not exist",
                expected_result="root KiCad project and schematic exist"))

        positions = self._default_positions(plan)
        placed = self._placed()
        for component in plan.components:
            if component.ref not in placed and len(actions) < self.max_actions:
                x, y = positions[component.ref]
                actions.append(ToolCall(
                    tool="place_component",
                    arguments={"ref": component.ref, "x": x, "y": y},
                    reason="component is missing from the observed schematic",
                    expected_result=f"{component.ref} exists"))

        connected = self._connected()
        for connection in plan.connections:
            if connection.key() not in connected and len(actions) < self.max_actions:
                actions.append(ToolCall(
                    tool="connect_pin",
                    arguments=connection.model_dump(mode="json"),
                    reason="planned pin-to-net binding is missing",
                    expected_result=connection.key()))

        if not actions and not _saved_after_changes(
                self.blackboard,
                {"create_project", "place_component", "connect_pin"}):
            actions.append(ToolCall(
                tool="save_project", reason="persist completed schematic",
                expected_result="project saved"))
        return AgentPlan(
            goal=task.goal, actions=actions,
            expected_result="schematic satisfies BoardPlan",
            rationale="deterministic recovery policy",
            done=not actions and self.acceptance_met())

    def validate_plan(self, plan: AgentPlan) -> None:
        super().validate_plan(plan)
        tools = [call.tool for call in plan.actions]
        if not self.blackboard.state.project_created and tools:
            if tools[0] != "create_project":
                raise AgentContractError(
                    "create_project must be the first action for a new project")
        elif self.blackboard.state.project_created and "create_project" in tools:
            raise AgentContractError("project already exists")
        if tools.count("create_project") > 1:
            raise AgentContractError("project can only be created once")
        if "save_project" in tools and tools[-1] != "save_project":
            raise AgentContractError("save_project must be the final action")

        placed_refs = [str(call.arguments.get("ref")) for call in plan.actions
                       if call.tool == "place_component"]
        if len(placed_refs) != len(set(placed_refs)):
            raise AgentContractError("component placed twice in one plan")

        available_refs = self._placed()
        for call in plan.actions:
            if call.tool == "place_component":
                available_refs.add(str(call.arguments["ref"]))
            elif (call.tool == "connect_pin"
                  and str(call.arguments["ref"]) not in available_refs):
                raise AgentContractError(
                    "connect_pin requires the component to exist first")

    def validate_call(self, call: ToolCall) -> None:
        plan = self.blackboard.state.board_plan
        assert plan is not None
        args = call.arguments
        if call.tool in ("create_project", "save_project"):
            if args:
                raise AgentContractError(f"{call.tool} takes no arguments")
            return
        if call.tool == "place_component":
            if set(args) != {"ref", "x", "y"}:
                raise AgentContractError("place_component requires ref,x,y")
            try:
                plan.component(str(args["ref"]))
            except KeyError as exc:
                raise AgentContractError(
                    f"unknown component {args['ref']!r}") from exc
            x, y = float(args["x"]), float(args["y"])
            if not (20 <= x <= 277 and 20 <= y <= 190):
                raise AgentContractError("schematic position outside sheet bounds")
            return
        if call.tool == "connect_pin":
            if set(args) != {"ref", "pin", "net"}:
                raise AgentContractError("connect_pin requires ref,pin,net")
            key = f"{args['ref']}:{args['pin']}:{args['net']}"
            if key not in {connection.key() for connection in plan.connections}:
                raise AgentContractError(f"unplanned connection {key}")

    @staticmethod
    def _default_positions(plan: BoardPlan) -> dict[str, tuple[float, float]]:
        # Every coordinate is on KiCad's 2.54 mm connection grid so ERC does
        # not inherit endpoint_off_grid warnings from generated placement.
        if plan.topology == "asynchronous_buck":
            preferred = {
                "J1": (40.64, 50.8), "C1": (66.04, 50.8),
                "U1": (91.44, 50.8), "L1": (116.84, 50.8),
                "C2": (142.24, 50.8), "J2": (167.64, 50.8),
                "D1": (116.84, 76.2), "R1": (91.44, 101.6),
                "R2": (116.84, 101.6), "R3": (142.24, 101.6),
                "D2": (167.64, 101.6), "TP1": (40.64, 127.0),
                "TP2": (66.04, 127.0), "TP3": (91.44, 127.0),
                "#FLG01": (116.84, 127.0), "#FLG02": (142.24, 127.0),
            }
        else:
            preferred = {
                "J1": (50.8, 50.8), "C1": (76.2, 50.8),
                "U1": (101.6, 50.8), "C2": (127.0, 50.8),
                "J2": (152.4, 50.8), "R1": (101.6, 76.2),
                "R2": (127.0, 76.2), "R3": (152.4, 76.2),
                "D1": (177.8, 76.2), "TP1": (50.8, 101.6),
                "TP2": (76.2, 101.6), "TP3": (101.6, 101.6),
                "#FLG01": (127.0, 101.6), "#FLG02": (152.4, 101.6),
            }
        return {
            component.ref: preferred.get(
                component.ref,
                (50.8 + (index % 6) * 25.4,
                 152.4 + (index // 6) * 12.7))
            for index, component in enumerate(plan.components)
        }


class PcbDesigner(AutonomousDesignAgent):
    name = "pcb_designer"
    allowed_tools = frozenset({
        "sync_board", "set_board_outline", "place_footprint",
        "autoroute_board", "save_project"})
    _PROMPT = """You are an autonomous KiCad PCB designer. Return ONLY an
AgentPlan JSON using the immutable BoardPlan and current observation.
Tool arguments: sync_board {}; set_board_outline {width,height};
place_footprint {ref,x,y}; autoroute_board {}; save_project {}. Place every
footprint inside the BoardPlan outline. Call autoroute_board only after all
physical footprints are placed; routing rules and target nets come from the
approved BoardPlan. Never emit raw KiCad content."""

    def default_goal(self) -> str:
        return "Create, place, and route a PCB that implements the BoardPlan"

    def acceptance_criteria(self) -> list[str]:
        return ["PCB is synchronized from the schematic",
                "board outline is set",
                "every planned footprint has a placement",
                "approved target nets were routed by Freerouting",
                "project is saved"]

    def _route_specs(self) -> dict[str, dict[str, str]]:
        plan = self.blackboard.state.board_plan
        assert plan is not None
        physical = {component.ref for component in plan.components
                    if component.on_board}
        by_net: dict[str, list[BoardConnection]] = defaultdict(list)
        for connection in plan.connections:
            if connection.ref in physical:
                by_net[connection.net].append(connection)
        routes: dict[str, dict[str, str]] = {}
        for net, pins in by_net.items():
            for left, right in zip(pins, pins[1:]):
                key = route_key(net, left.ref, left.pin, right.ref, right.pin)
                routes[key] = {
                    "net": net, "from_ref": left.ref, "from_pin": left.pin,
                    "to_ref": right.ref, "to_pin": right.pin}
        return routes

    def acceptance_met(self) -> bool:
        plan = self.blackboard.state.board_plan
        if plan is None:
            return False
        refs = {component.ref for component in plan.components
                if component.on_board}
        saved = _saved_after_changes(
            self.blackboard, {"sync_board", "set_board_outline",
                              "place_footprint", "autoroute_board"})
        return (self.blackboard.state.board_exists
                and self.blackboard.state.board_synced
                and self.blackboard.state.outline_set
                and refs <= set(self.blackboard.state.placed_footprints)
                and self.blackboard.state.autorouted
                and saved)

    def deterministic_plan(self, task: AgentTask,
                           observation: dict) -> AgentPlan:
        plan = self.blackboard.state.board_plan
        assert plan is not None
        actions: list[ToolCall] = []
        if not self.blackboard.state.board_synced:
            actions.append(ToolCall(
                tool="sync_board",
                reason="PCB has not been synchronized from the schematic",
                expected_result="schematic footprints synchronized to PCB"))
        if not self.blackboard.state.outline_set:
            actions.append(ToolCall(
                tool="set_board_outline",
                arguments=plan.outline.model_dump(mode="json"),
                reason="board outline is missing",
                expected_result="Edge.Cuts outline exists"))

        positions = self._default_positions(plan)
        placed = set(self.blackboard.state.placed_footprints)
        for component in (item for item in plan.components if item.on_board):
            if component.ref not in placed and len(actions) < self.max_actions:
                x, y = positions[component.ref]
                actions.append(ToolCall(
                    tool="place_footprint",
                    arguments={"ref": component.ref, "x": x, "y": y},
                    reason="footprint has no recorded placement",
                    expected_result=f"{component.ref} placed inside outline"))

        planned_refs = {component.ref for component in plan.components
                        if component.on_board}
        autoroute_attempts = sum(
            1 for execution in self.blackboard.state.tool_history
            if execution.call.tool == "autoroute_board")
        placed_after_plan = placed | {
            str(call.arguments["ref"]) for call in actions
            if call.tool == "place_footprint"}
        if (planned_refs <= placed_after_plan
                and not self.blackboard.state.autorouted
                and autoroute_attempts < 3
                and len(actions) < self.max_actions):
            actions.append(ToolCall(
                tool="autoroute_board",
                reason="all physical footprints are placed and routing is pending",
                expected_result="all approved target nets routed by Freerouting"))

        if not actions and not _saved_after_changes(
                self.blackboard,
                {"sync_board", "set_board_outline", "place_footprint",
                 "autoroute_board"}):
            actions.append(ToolCall(
                tool="save_project", reason="persist completed PCB",
                expected_result="project saved"))
        return AgentPlan(
            goal=task.goal, actions=actions,
            expected_result="PCB implementation is ready for verification",
            rationale="deterministic recovery policy",
            done=not actions and self.acceptance_met())

    def validate_plan(self, plan: AgentPlan) -> None:
        super().validate_plan(plan)
        tools = [call.tool for call in plan.actions]
        if not self.blackboard.state.board_synced and tools:
            if tools[0] != "sync_board":
                raise AgentContractError(
                    "sync_board must be the first action for a new PCB")
        elif self.blackboard.state.board_synced and "sync_board" in tools:
            raise AgentContractError("PCB is already synchronized")
        if tools.count("sync_board") > 1:
            raise AgentContractError("PCB can only be synchronized once")
        if "save_project" in tools and tools[-1] != "save_project":
            raise AgentContractError("save_project must be the final action")

        refs = [str(call.arguments.get("ref")) for call in plan.actions
                if call.tool == "place_footprint"]
        if len(refs) != len(set(refs)):
            raise AgentContractError("footprint placed twice in one plan")

        outline_ready = self.blackboard.state.outline_set
        placed = set(self.blackboard.state.placed_footprints)
        for call in plan.actions:
            if call.tool == "set_board_outline":
                outline_ready = True
            elif call.tool == "place_footprint":
                if not outline_ready:
                    raise AgentContractError(
                        "set_board_outline must precede footprint placement")
                placed.add(str(call.arguments["ref"]))
            elif call.tool == "autoroute_board":
                required_refs = {component.ref for component in
                                 self.blackboard.state.board_plan.components
                                 if component.on_board}
                if not required_refs <= placed:
                    raise AgentContractError(
                        "autoroute_board requires every footprint to be placed")

    def validate_call(self, call: ToolCall) -> None:
        plan = self.blackboard.state.board_plan
        assert plan is not None
        args = call.arguments
        if call.tool in ("sync_board", "autoroute_board", "save_project"):
            if args:
                raise AgentContractError(f"{call.tool} takes no arguments")
            return
        if call.tool == "set_board_outline":
            proposed = BoardOutline.model_validate(args)
            if proposed != plan.outline:
                raise AgentContractError("outline differs from BoardPlan")
            return
        if call.tool == "place_footprint":
            if set(args) != {"ref", "x", "y"}:
                raise AgentContractError("place_footprint requires ref,x,y")
            try:
                plan.component(str(args["ref"]))
            except KeyError as exc:
                raise AgentContractError(
                    f"unknown component {args['ref']!r}") from exc
            x, y = float(args["x"]), float(args["y"])
            if not (1 <= x <= plan.outline.width - 1
                    and 1 <= y <= plan.outline.height - 1):
                raise AgentContractError("footprint position outside board outline")
            return
    @staticmethod
    def _default_positions(plan: BoardPlan) -> dict[str, tuple[float, float]]:
        positions = {hint.ref: (hint.x, hint.y)
                     for hint in plan.placement_hints}
        physical = [component for component in plan.components
                    if component.on_board]
        columns = max(1, min(4, len(physical)))
        x_step = max(6.0, (plan.outline.width - 10.0) / columns)
        for index, component in enumerate(physical):
            if component.ref in positions:
                continue
            column = index % columns
            row = index // columns
            positions[component.ref] = (
                round(5.0 + column * x_step, 2),
                round(min(plan.outline.height - 5.0, 10.0 + row * 12.0), 2))
        return positions


class VerificationCrew:
    """Run the checker crew and publish typed Finding messages."""

    name = "verification_crew"

    def __init__(self, config: Config, strategy: StrategyBundle,
                 blackboard: DesignBlackboard,
                 recorder: Recorder | None = None):
        self.config = config
        self.strategy = strategy
        self.blackboard = blackboard
        self.recorder = recorder

    def verify(self, project_dir: Path, stage: str) -> EvaluationResult:
        from ratsnest.agents import synthesize
        from ratsnest.crews.checker import CheckerCrew
        outputs = CheckerCrew(
            self.config, self.strategy, recorder=self.recorder).evaluate(project_dir)
        evaluation = synthesize(outputs, self.strategy, project_dir)
        for finding in evaluation.findings:
            if finding.severity not in ("error", "warning"):
                continue
            self.blackboard.publish(
                self.name, "repair_agent", MessageKind.finding,
                {"stage": stage,
                 "finding_id": finding.finding_id(),
                 "finding": finding.model_dump(mode="json")},
                finding.finding_id())
        if self.recorder is not None:
            self.recorder.emit(
                "design.verification", 0,
                observation={"stage": stage},
                outcome={"score": evaluation.scorecard.score,
                         "severity_counts": evaluation.scorecard.severity_counts},
                metadata={"agent": self.name, "crew": "checker"})
        return evaluation


class RepairAgent:
    """Route checker findings to the agent that owns the failing artifact."""

    name = "repair_agent"
    ASSIGNEES = {"schematic_designer", "pcb_designer", "repair_loop", "human"}
    _PROMPT = """You are the repair coordinator for a KiCad multi-agent crew.
Given verified findings, return ONLY JSON {"assignments":[...]}. Each item:
{assignee, goal, finding_ids, acceptance_criteria}. assignee must be one of
schematic_designer, pcb_designer, repair_loop, human. Route missing symbols,
pins, nets, and schematic connectivity to schematic_designer; placement,
clearance, outline, unrouted-net and PCB geometry issues to pcb_designer;
supported value/MPN repairs to repair_loop; ambiguous or unsupported design
changes to human. Do not propose tool calls or file edits."""

    def __init__(self, strategy: StrategyBundle,
                 blackboard: DesignBlackboard,
                 llm: LlmBrain | None = None,
                 recorder: Recorder | None = None):
        self.strategy = strategy
        self.blackboard = blackboard
        self.llm = llm
        self.recorder = recorder

    def assign(self, evaluation: EvaluationResult) -> list[AgentTask]:
        findings = [finding for finding in evaluation.findings
                    if finding.severity in ("error", "warning")]
        if not findings:
            return []
        tasks: list[AgentTask] | None = None
        brain = "deterministic"
        fallback_error: str | None = None
        if self.llm is not None:
            attempted = self.llm.available
            system = self._PROMPT
            custom = self.strategy.prompts.get(self.name)
            if custom:
                system += f"\nAdditional strategy policy:\n{custom}"
            raw = self.llm.complete_json(
                self.name, system,
                json.dumps({"findings": [
                    {"finding_id": finding.finding_id(),
                     **finding.model_dump(mode="json")}
                    for finding in findings]}), max_tokens=1600)
            if raw:
                try:
                    tasks = self._validate_assignments(raw, findings)
                    brain = "llm"
                except (ValidationError, ValueError) as exc:
                    fallback_error = str(exc)[:300]
                    if _brain_required(self.llm):
                        raise AgentContractError(
                            f"repair assignments rejected: {exc}") from exc
            elif attempted:
                brain = "deterministic_fallback"
                fallback_error = "brain returned no valid assignments"
        if tasks is None:
            tasks = self._deterministic_assignments(findings)
            if fallback_error is not None:
                brain = "deterministic_fallback"
        if self.recorder is not None:
            self.recorder.emit(
                "design.repair_agent.plan", 0,
                observation={"finding_ids": [finding.finding_id()
                                              for finding in findings]},
                agent_state={"brain": brain},
                action={"assignments": [task.model_dump(mode="json")
                                         for task in tasks]},
                outcome={"validated": True,
                         "fallback_error": fallback_error},
                metadata={"agent": self.name, "crew": "design"})
        for task in tasks:
            self.blackboard.assign(task, self.name)
        return tasks

    def _validate_assignments(self, raw: dict,
                              findings: list) -> list[AgentTask]:
        known = {finding.finding_id() for finding in findings}
        assigned: set[str] = set()
        tasks: list[AgentTask] = []
        items = raw.get("assignments")
        if not isinstance(items, list):
            raise ValueError("assignments must be a list")
        for item in items:
            if not isinstance(item, dict) or item.get("assignee") not in self.ASSIGNEES:
                raise ValueError("invalid repair assignee")
            ids = [str(fid) for fid in item.get("finding_ids", [])]
            if not ids or any(fid not in known for fid in ids):
                raise ValueError("assignment contains unknown finding ids")
            if any(fid in assigned for fid in ids):
                raise ValueError("a finding may only be assigned once")
            assigned.update(ids)
            tasks.append(AgentTask(
                assignee=item["assignee"], goal=str(item.get("goal", "")),
                acceptance_criteria=[str(value) for value in
                                     item.get("acceptance_criteria", [])],
                context={"finding_ids": ids}))
        if assigned != known:
            raise ValueError("every actionable finding must be assigned")
        return tasks

    def _deterministic_assignments(self, findings: list) -> list[AgentTask]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for finding in findings:
            extra = finding.model_extra or {}
            text = " ".join((finding.detector or "", finding.rule_id or "",
                             str(extra.get("summary", "")))).lower()
            if any(word in text for word in
                   ("pcb", "clearance", "outline", "unrouted", "placement")):
                assignee = "pcb_designer"
            elif any(word in text for word in
                     ("unconnected", "pin", "net", "schematic")):
                assignee = "schematic_designer"
            elif finding.rule_id in {"RN-VOUT-001", "LR-001", "SS-001", "DS-001"}:
                assignee = "repair_loop"
            else:
                assignee = "human"
            grouped[assignee].append(finding.finding_id())
        return [AgentTask(
            assignee=assignee,
            goal=f"Resolve {len(ids)} verified finding(s)",
            acceptance_criteria=["assigned findings no longer appear after verification"],
            context={"finding_ids": ids})
            for assignee, ids in grouped.items()]
