"""Strict domain contracts exchanged by agents, the EDA adapter, and verifiers.

Design stance (inherited from the RatsNest runtime): these models are the
boundary where silently discarded or malformed data is unsafe. The LLM may
*propose* instances of these types, but every instance is validated here
before any deterministic code acts on it. Electrical facts are decided by the
verification layer, never by model prose.

This module covers the schematic-generation slice. PCB placement fields exist
for forward compatibility but the phase-1/2 flow only exercises the schematic
and connectivity portions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


def _is_scalar_str_field(annotation: object) -> bool:
    """True when the field is a plain ``str`` (or ``str | None``) — i.e. a
    scalar text field, not a ``list[str]``/``dict``. Used to tolerate LLM
    output that returns a list of sentences where a single string is expected."""
    if annotation is str:
        return True
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args == [str]
    return False


class ContractModel(BaseModel):
    """Base for boundaries where silently discarded data is unsafe."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def _coerce_scalar_text(cls, data: object) -> object:
        """Normalize richer LLM output for scalar text fields.

        LLMs frequently return a *list* of sentences (or a nested object) where
        the contract declares a single ``str`` (e.g. ``rationale``). Rather than
        fail closed on a harmless shape mismatch, join list items into one
        string. Fields typed as ``list[...]``/``dict`` are left untouched, so
        genuinely structured data is still validated strictly."""
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            if name not in data or not _is_scalar_str_field(field.annotation):
                continue
            val = data[name]
            if isinstance(val, list):
                parts = [str(x) for x in val if not isinstance(x, dict | list)]
                data[name] = "; ".join(parts)
        return data


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Stage(StrEnum):
    INTAKE = "intake"
    ARCHITECTURE = "architecture"
    SCHEMATIC = "schematic"
    VERIFICATION = "verification"
    REPAIR = "repair"
    REVIEW = "review"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Requirement intake
# --------------------------------------------------------------------------- #


class ComponentIdentityConstraint(ContractModel):
    """One device identity constraint grounded directly in the user request.

    This contract is populated by deterministic source-span verification, not
    copied from an LLM component proposal.  It therefore preserves the
    distinction between a user-fixed part and a model-selected implementation.
    """

    requested_identity: str = Field(min_length=2, max_length=200)
    mode: Literal[
        "fixed_exact",
        "family_variant",
        "capability_only",
    ]
    provenance: Literal["user_requirement"] = "user_requirement"
    allow_equivalent: bool = False
    source_excerpt: str = Field(min_length=1, max_length=500)


class RequirementSpec(ContractModel):
    """Normalized requirement. The LLM may produce this from raw text, but the
    fields are validated and the raw text is always preserved."""

    requirement_id: str = Field(default_factory=lambda: _id("req"))
    # Architect evidence and retrieved source excerpts are intentionally carried
    # with the original request. Keep a finite checkpoint bound, but do not force
    # otherwise valid grounded requests through a 10k bottleneck.
    raw_text: str = Field(min_length=1, max_length=100_000)
    project_name: str = Field(default="generated_board", min_length=1, max_length=120)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=100)
    component_identity_constraints: list[ComponentIdentityConstraint] = Field(
        default_factory=list,
        max_length=256,
    )


# --------------------------------------------------------------------------- #
# Circuit intermediate representation (schematic intent)
# --------------------------------------------------------------------------- #


class PinRef(ContractModel):
    component_ref: str = Field(min_length=1, max_length=32)
    pin: str = Field(min_length=1, max_length=32)

    def key(self) -> str:
        return f"{self.component_ref}:{self.pin}"


class ComponentSpec(ContractModel):
    ref: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=200)
    footprint: str = Field(default="", max_length=240)
    catalog_id: str = Field(default="", max_length=160)
    role: str = Field(default="", max_length=120)
    properties: dict[str, str] = Field(default_factory=dict)
    in_bom: bool = True
    on_board: bool = True


class NetSpec(ContractModel):
    name: str = Field(min_length=1, max_length=100)
    pins: list[PinRef] = Field(default_factory=list, max_length=500)
    net_class: str | None = Field(default=None, max_length=100)
    properties: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_pins(self) -> NetSpec:
        keys = [pin.key() for pin in self.pins]
        if len(keys) != len(set(keys)):
            raise ValueError(f"net {self.name!r} contains duplicate pins")
        return self


class CircuitIR(ContractModel):
    ir_version: Literal["1.0"] = "1.0"
    family: str = Field(default="", max_length=120)
    components: list[ComponentSpec] = Field(default_factory=list, max_length=1_000)
    nets: list[NetSpec] = Field(default_factory=list, max_length=5_000)
    constraints: list[str] = Field(default_factory=list, max_length=200)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def validate_graph(self) -> CircuitIR:
        refs = [component.ref for component in self.components]
        if len(refs) != len(set(refs)):
            raise ValueError("component references must be unique")
        names = [net.name for net in self.nets]
        if len(names) != len(set(names)):
            raise ValueError("net names must be unique")

        known_refs = set(refs)
        assigned_pins: dict[str, str] = {}
        for net in self.nets:
            for pin in net.pins:
                if pin.component_ref not in known_refs:
                    raise ValueError(
                        f"net {net.name!r} references unknown component {pin.component_ref!r}"
                    )
                key = pin.key()
                if key in assigned_pins:
                    raise ValueError(
                        f"component pin {key!r} belongs to both "
                        f"{assigned_pins[key]!r} and {net.name!r}"
                    )
                assigned_pins[key] = net.name
        return self

    def component(self, ref: str) -> ComponentSpec | None:
        for component in self.components:
            if component.ref == ref:
                return component
        return None

    def components_with_role(self, role: str) -> list[ComponentSpec]:
        return [c for c in self.components if c.role == role]

    def net(self, name: str) -> NetSpec | None:
        for net in self.nets:
            if net.name == name:
                return net
        return None


# --------------------------------------------------------------------------- #
# Board plan (placement) — schematic slice keeps this light
# --------------------------------------------------------------------------- #


class BoardOutline(ContractModel):
    width_mm: float = Field(default=50.0, gt=0, le=1_000)
    height_mm: float = Field(default=35.0, gt=0, le=1_000)


class PlacementSpec(ContractModel):
    ref: str = Field(min_length=1, max_length=32)
    x_mm: float = Field(ge=-10_000, le=10_000)
    y_mm: float = Field(ge=-10_000, le=10_000)
    rotation_deg: float = Field(default=0, ge=0, lt=360)
    side: Literal["front", "back"] = "front"


class BoardPlan(ContractModel):
    plan_id: str = Field(default_factory=lambda: _id("board"))
    outline: BoardOutline = Field(default_factory=BoardOutline)
    placements: list[PlacementSpec] = Field(default_factory=list, max_length=1_000)
    reference_plane_net: str = Field(default="GND", min_length=1, max_length=100)
    copper_layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"], min_length=1)
    constraints: list[str] = Field(default_factory=list, max_length=200)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def unique_placements(self) -> BoardPlan:
        refs = [placement.ref for placement in self.placements]
        if len(refs) != len(set(refs)):
            raise ValueError("placement references must be unique")
        if len(self.copper_layers) != len(set(self.copper_layers)):
            raise ValueError("copper layers must be unique")
        return self


class DesignPlan(ContractModel):
    """The immutable, approvable design plan: requirement + IR + board plan."""

    plan_version: Literal["ratsnestpro.plan.v1"] = "ratsnestpro.plan.v1"
    requirement: RequirementSpec
    circuit: CircuitIR
    board: BoardPlan
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Findings and gates (verification results)
# --------------------------------------------------------------------------- #


class Finding(ContractModel):
    finding_id: str = Field(default_factory=lambda: _id("finding"))
    stage: Stage = Stage.VERIFICATION
    severity: Severity
    rule_id: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    details: str = Field(default="", max_length=10_000)
    component_refs: list[str] = Field(default_factory=list, max_length=500)
    net_names: list[str] = Field(default_factory=list, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=500)
    repairable: bool = True


class GateResult(ContractModel):
    gate: str = Field(min_length=1, max_length=200)
    status: GateStatus
    required: bool = True
    summary: str = Field(default="", max_length=2_000)
    findings: list[Finding] = Field(default_factory=list)
    metrics: dict[str, float | int | bool | str | None] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == GateStatus.PASSED


class VerificationReport(ContractModel):
    gates: list[GateResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    @property
    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        for gate in self.gates:
            out.extend(gate.findings)
        return out

    @property
    def blocked(self) -> bool:
        """Blocked when any required gate has not passed, or any error finding exists."""
        for gate in self.gates:
            if gate.required and gate.status in (GateStatus.FAILED, GateStatus.ERROR):
                return True
        return any(f.severity == Severity.ERROR for f in self.findings)

    def gate(self, name: str) -> GateResult | None:
        for gate in self.gates:
            if gate.gate == name:
                return gate
        return None


# --------------------------------------------------------------------------- #
# Agent decisions (structured LLM outputs; validated, never trusted as fact)
# --------------------------------------------------------------------------- #


class FamilyDecision(ContractModel):
    """Architect's gatekeeping decision: does the request belong to a known,
    qualified circuit family, with all mandatory features preserved?"""

    qualified: bool
    family: str = Field(default="", max_length=120)
    mandatory_features_present: bool = True
    missing_features: list[str] = Field(default_factory=list, max_length=100)
    clarifying_questions: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=4_000)


class RepairAction(ContractModel):
    """A single repair step the Coder proposes, restricted to a whitelist op."""

    operation: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=2_000)


class AgentDecision(ContractModel):
    """Generic structured decision envelope with a root-cause diagnosis and a
    bounded list of proposed repair actions."""

    decision_id: str = Field(default_factory=lambda: _id("decision"))
    diagnosis: str = Field(default="", max_length=4_000)
    actions: list[RepairAction] = Field(default_factory=list, max_length=50)
    give_up: bool = False
    rationale: str = Field(default="", max_length=4_000)
