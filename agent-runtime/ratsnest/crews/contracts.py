"""Typed collaboration contracts for the autonomous design crew.

Agents exchange these models through a blackboard.  Natural-language chat is
never an execution interface: an LLM may propose a BoardPlan or AgentPlan,
but only validated ToolCalls can reach the KiCad tool services.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ratsnest.schemas import DesignSpec, Finding


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoardComponent(ContractModel):
    ref: str = Field(
        min_length=1, max_length=16,
        pattern=r"^(?:[A-Z]+[0-9]+|#[A-Z]+[0-9]+)$")
    symbol: str = Field(min_length=3, max_length=120)
    value: str = Field(min_length=1, max_length=120)
    footprint: str = Field(default="", max_length=180)
    catalog_id: str = Field(default="", max_length=120)
    role: str = Field(default="", max_length=80)
    in_bom: bool = True
    on_board: bool = True
    properties: dict[str, str] = Field(default_factory=dict)


class BoardConnection(ContractModel):
    net: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=16)
    pin: str = Field(min_length=1, max_length=16)

    def key(self) -> str:
        return f"{self.ref}:{self.pin}:{self.net}"


class BoardOutline(ContractModel):
    width: float = Field(default=50.0, ge=20.0, le=200.0)
    height: float = Field(default=35.0, ge=20.0, le=150.0)


class DesignLimits(ContractModel):
    input_voltage_v: float = Field(gt=0, le=60)
    output_voltage_v: float = Field(gt=0, le=60)
    output_current_a: float = Field(gt=0, le=10)
    ambient_temperature_c: float = Field(ge=-40, le=85)
    controller_loss_w: float = Field(ge=0, le=100)
    estimated_junction_c: float = Field(ge=-40, le=250)
    max_junction_c: float = Field(gt=0, le=250)
    estimated_efficiency_pct: float = Field(gt=0, le=100)
    dropout_margin_v: float | None = Field(default=None, ge=0, le=60)
    duty_cycle: float | None = Field(default=None, gt=0, lt=1)
    switching_frequency_hz: float | None = Field(default=None, gt=0)
    max_output_ripple_mv: float = Field(gt=0, le=1000)


class PlacementHint(ContractModel):
    ref: str
    x: float = Field(ge=1, le=199)
    y: float = Field(ge=1, le=149)
    rotation: float = Field(default=0, ge=0, lt=360)


class BoardNetClass(ContractModel):
    track_width_mm: float = Field(default=0.25, ge=0.15, le=5)
    clearance_mm: float = Field(default=0.2, ge=0.1, le=3)


class BoardPlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: _id("board"))
    topology: str = Field(min_length=3, max_length=80)
    components: list[BoardComponent] = Field(min_length=1, max_length=40)
    connections: list[BoardConnection] = Field(default_factory=list, max_length=200)
    outline: BoardOutline = Field(default_factory=BoardOutline)
    family_version: str = Field(default="legacy", min_length=2, max_length=40)
    catalog_version: str = Field(default="legacy", min_length=2, max_length=40)
    design_limits: DesignLimits | None = None
    placement_hints: list[PlacementHint] = Field(default_factory=list, max_length=40)
    net_classes: dict[str, BoardNetClass] = Field(default_factory=dict)
    required_gates: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    rationale: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def validate_graph(self) -> "BoardPlan":
        refs = [component.ref for component in self.components]
        if len(refs) != len(set(refs)):
            raise ValueError("component references must be unique")
        known = set(refs)
        pin_keys: set[tuple[str, str]] = set()
        for connection in self.connections:
            if connection.ref not in known:
                raise ValueError(
                    f"connection references unknown component {connection.ref!r}")
            pin_key = (connection.ref, connection.pin)
            if pin_key in pin_keys:
                raise ValueError(
                    f"component pin {connection.ref}:{connection.pin} has multiple nets")
            pin_keys.add(pin_key)
        hinted = [hint.ref for hint in self.placement_hints]
        if len(hinted) != len(set(hinted)):
            raise ValueError("placement hints must have unique references")
        if not set(hinted) <= known:
            raise ValueError("placement hint references an unknown component")
        return self

    def component(self, ref: str) -> BoardComponent:
        for component in self.components:
            if component.ref == ref:
                return component
        raise KeyError(ref)


class PlannedDesign(ContractModel):
    """Immutable hand-off between planning, approval, and execution."""

    contract_version: Literal[
        "ratsnest.design-plan.v1", "ratsnest.design-plan.v2"
    ] = "ratsnest.design-plan.v2"
    run_id: str = Field(min_length=4, max_length=100)
    requirement: str = Field(min_length=1, max_length=500)
    backend: Literal["template", "crew", "mcp"]
    design_spec: DesignSpec
    board_plan: BoardPlan
    strategy_name: str = Field(min_length=1, max_length=80)
    strategy_version_id: str = Field(
        min_length=8, max_length=80, pattern=r"^strat_[a-f0-9]+$")
    trajectory_step: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_requirement_binding(self) -> "PlannedDesign":
        recorded = self.design_spec.requirement_text
        if recorded and recorded != self.requirement:
            raise ValueError("DesignSpec requirement differs from plan requirement")
        if self.contract_version == "ratsnest.design-plan.v2":
            plan = self.board_plan
            if plan.catalog_version == "legacy" or plan.design_limits is None:
                raise ValueError("v2 BoardPlan requires catalog and design limits")
            if not plan.required_gates:
                raise ValueError("v2 BoardPlan requires production gates")
            for component in plan.components:
                if not component.catalog_id:
                    raise ValueError(
                        f"v2 component {component.ref} has no catalog binding")
                if component.on_board and not component.footprint:
                    raise ValueError(
                        f"physical component {component.ref} has no footprint")
        return self


class ToolCall(ContractModel):
    call_id: str = Field(default_factory=lambda: _id("call"))
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=500)
    expected_result: str = Field(default="", max_length=500)


class AgentPlan(ContractModel):
    goal: str = Field(min_length=1, max_length=500)
    actions: list[ToolCall] = Field(default_factory=list, max_length=20)
    expected_result: str = Field(default="", max_length=500)
    rationale: str = Field(default="", max_length=1000)
    done: bool = False


class MessageKind(str, Enum):
    board_plan = "board_plan"
    task = "task"
    finding = "finding"
    result = "result"
    status = "status"


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    blocked = "blocked"


class AgentTask(ContractModel):
    task_id: str = Field(default_factory=lambda: _id("task"))
    assignee: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=500)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    context: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.pending
    attempts: int = 0


class FindingPayload(ContractModel):
    stage: str = Field(min_length=1, max_length=40)
    finding_id: str = Field(min_length=1, max_length=200)
    finding: Finding


class AgentResultPayload(ContractModel):
    task_id: str
    success: bool
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)


class AgentStatusPayload(ContractModel):
    task_id: str
    status: TaskStatus


MessagePayload = (
    BoardPlan | AgentTask | FindingPayload | AgentResultPayload
    | AgentStatusPayload
)


class AgentMessage(ContractModel):
    message_id: str = Field(default_factory=lambda: _id("msg"))
    sender: str = Field(min_length=1, max_length=80)
    recipient: str = Field(min_length=1, max_length=80)
    kind: MessageKind
    payload: MessagePayload
    correlation_id: str | None = None
    ts: str = Field(default_factory=_now)

    @model_validator(mode="before")
    @classmethod
    def validate_payload_for_kind(cls, data):
        if not isinstance(data, dict):
            return data
        kind = MessageKind(data.get("kind"))
        payload_models = {
            MessageKind.board_plan: BoardPlan,
            MessageKind.task: AgentTask,
            MessageKind.finding: FindingPayload,
            MessageKind.result: AgentResultPayload,
            MessageKind.status: AgentStatusPayload,
        }
        normalized = dict(data)
        normalized["payload"] = payload_models[kind].model_validate(
            data.get("payload", {}))
        return normalized


class ToolExecution(ContractModel):
    agent: str
    call: ToolCall
    success: bool
    outcome: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    ts: str = Field(default_factory=_now)


class DesignState(ContractModel):
    project_dir: str
    stage: str = "created"
    board_plan: BoardPlan | None = None
    project_created: bool = False
    observed_components: list[str] = Field(default_factory=list)
    observed_pin_nets: dict[str, str] = Field(default_factory=dict)
    board_exists: bool = False
    board_synced: bool = False
    observed_footprints: list[str] = Field(default_factory=list)
    outline_set: bool = False
    placed_footprints: list[str] = Field(default_factory=list)
    attempted_routes: list[str] = Field(default_factory=list)
    routed_connections: list[str] = Field(default_factory=list)
    autorouted: bool = False
    routing_mode: str | None = None
    routing_metrics: dict[str, Any] = Field(default_factory=dict)
    messages: list[AgentMessage] = Field(default_factory=list)
    tasks: list[AgentTask] = Field(default_factory=list)
    tool_history: list[ToolExecution] = Field(default_factory=list)
    revision: int = 0
