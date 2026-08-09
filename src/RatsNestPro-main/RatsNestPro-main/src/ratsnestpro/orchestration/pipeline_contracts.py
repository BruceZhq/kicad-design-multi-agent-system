"""Structured I/O contracts for the fixed PCB pipeline steps.

Each pipeline step consumes the previous artifacts and produces one of these
validated models. The LLM may *propose* an instance, but construction here
enforces structure (``extra="forbid"``), and a cheap bottom-line check in the
step verifies it against real libraries / fab values before the flow advances.

This module grows one contract per step as the pipeline is built out. Task 4
seeds the front-end (topology); later tasks add selection, connectivity,
placement, routing, and manufacturing contracts.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ratsnestpro.domain.contracts import ContractModel

_COMPONENT_REFERENCE_PATTERN = r"^[A-Za-z#][A-Za-z0-9_]*$"
_COMPONENT_REFERENCE_RE = re.compile(_COMPONENT_REFERENCE_PATTERN)
_COMPONENT_REFERENCE_TOKEN_RE = re.compile(r"#?[A-Za-z]+[0-9]+[A-Za-z]?")
_COMPONENT_REFERENCE_RANGE_RE = re.compile(
    r"^\s*(#?[A-Za-z]+)([0-9]+)\s*[-–—]\s*(#?[A-Za-z]+)([0-9]+)\s*$"
)
_COMPONENT_REFERENCE_SEPARATORS_RE = re.compile(r"[\s,，、;/&+]+")
_MAX_REFERENCE_RANGE_EXPANSION = 64


def _expanded_component_references(ref: str) -> list[str] | None:
    """Expand an unambiguous grouped reference emitted by an LLM.

    KiCad requires one reference per physical part. Models occasionally compact
    identical parts as ``"C1, C2"`` or ``"R3-R5"``. Expanding those forms is a
    lossless normalization; prose or ambiguous strings remain invalid and are
    rejected by :class:`SelectedPart`.
    """

    ref = ref.strip()
    if _COMPONENT_REFERENCE_RE.fullmatch(ref):
        return [ref]

    range_match = _COMPONENT_REFERENCE_RANGE_RE.fullmatch(ref)
    if range_match:
        start_prefix, start_text, end_prefix, end_text = range_match.groups()
        start = int(start_text)
        end = int(end_text)
        if (
            start_prefix.upper() == end_prefix.upper()
            and start <= end
            and end - start < _MAX_REFERENCE_RANGE_EXPANSION
        ):
            return [f"{start_prefix}{number}" for number in range(start, end + 1)]
        return None

    references = _COMPONENT_REFERENCE_TOKEN_RE.findall(ref)
    residue = _COMPONENT_REFERENCE_TOKEN_RE.sub("", ref)
    if (
        len(references) > 1
        and not _COMPONENT_REFERENCE_SEPARATORS_RE.sub("", residue)
        and all(_COMPONENT_REFERENCE_RE.fullmatch(item) for item in references)
    ):
        return references
    return None


def _expand_grouped_parts(value: object, field_name: str) -> object:
    if not isinstance(value, dict):
        return value
    raw_parts = value.get(field_name)
    if not isinstance(raw_parts, list):
        return value

    expanded: list[object] = []
    changed = False
    for raw_part in raw_parts:
        if not isinstance(raw_part, dict):
            expanded.append(raw_part)
            continue
        raw_ref = raw_part.get("ref")
        references = (
            _expanded_component_references(raw_ref)
            if isinstance(raw_ref, str)
            else None
        )
        if not references or len(references) == 1:
            expanded.append(raw_part)
            continue
        changed = True
        for reference in references:
            expanded.append({**raw_part, "ref": reference})
    return {**value, field_name: expanded} if changed else value


class ProposalModel(ContractModel):
    """Base for LLM-proposed pipeline artifacts.

    Unlike the strict domain contracts, these tolerate *extra* fields: an LLM
    often decorates its JSON with helpful-but-unmodelled keys (a per-part
    ``rationale``, a ``voltage`` on a rail, ...). Dropping those is safe — the
    electrical truth is decided by each step's bottom-line check against real
    libraries, not by field-name strictness. Scalar-text coercion and
    validate_assignment are inherited from :class:`ContractModel`.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class TopologyBlock(ProposalModel):
    """One functional block of the design (a node in the block diagram)."""

    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=2_000)


class TopologyPlan(ProposalModel):
    """The block-level architecture: functional blocks + supply rails.

    This is the output of the topology step — an LLM proposal, validated for
    structure and checked (bottom-line) for a power rail + ground presence.
    """

    blocks: list[TopologyBlock] = Field(default_factory=list, max_length=200)
    rails: list[str] = Field(default_factory=list, max_length=50)
    ground_net: str = Field(default="GND", min_length=1, max_length=100)
    rationale: str = Field(default="", max_length=10_000)

    @field_validator("rails", mode="before")
    @classmethod
    def _coerce_rails(cls, v: object) -> object:
        """Tolerate richer LLM output: a rail may arrive as a plain name or as
        an object like {"name": "3V3", "voltage": 3.3, ...}. Normalize to the
        rail name string; drop anything without an identifiable name."""
        if not isinstance(v, list):
            return v
        out: list[str] = []
        for item in v:
            if isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("rail")
                    or item.get("net")
                    or item.get("voltage")
                )
                if name is not None:
                    out.append(str(name))
            elif item is not None:
                out.append(str(item))
        return out

    @model_validator(mode="after")
    def _unique(self) -> TopologyPlan:
        names = [b.name for b in self.blocks]
        if len(names) != len(set(names)):
            raise ValueError("topology block names must be unique")
        if len(self.rails) != len(set(self.rails)):
            raise ValueError("supply rails must be unique")
        return self

    def block_kinds(self) -> set[str]:
        return {b.kind for b in self.blocks}


class SelectedPart(ProposalModel):
    """One chosen component, grounded in a real symbol/footprint.

    ``mpn``/``lcsc`` are filled only from a real catalog; they are never
    fabricated by the LLM (left empty when the cache is unavailable).
    """

    ref: str = Field(
        min_length=1,
        max_length=32,
        pattern=_COMPONENT_REFERENCE_PATTERN,
    )
    symbol: str = Field(min_length=1, max_length=200)  # lib_id, e.g. Device:R
    value: str = Field(min_length=1, max_length=200)
    footprint: str = Field(default="", max_length=240)
    role: str = Field(default="", max_length=120)
    mpn: str = Field(default="", max_length=160)
    lcsc: str = Field(default="", max_length=40)
    # Deterministic library-closure metadata. Proposal fields are never trusted:
    # ComponentResolutionService overwrites them before downstream use.
    requested_identity: str = Field(default="", max_length=200)
    identity_mode: str = Field(
        default="",
        max_length=32,
        pattern=r"^(?:|fixed_exact|family_variant|capability_only)$",
    )
    identity_provenance: str = Field(default="", max_length=240)
    resolution_status: str = Field(default="", max_length=64)
    resolution_detail: str = Field(default="", max_length=1_000)
    release_ready: bool = False
    dnp: bool = False
    unresolved: bool = False


class SelectionPlan(ProposalModel):
    """The chosen bill of components for the design."""

    parts: list[SelectedPart] = Field(default_factory=list, max_length=1_000)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="before")
    @classmethod
    def _expand_grouped_references(cls, value: object) -> object:
        return _expand_grouped_parts(value, "parts")

    @model_validator(mode="after")
    def _unique_refs(self) -> SelectionPlan:
        refs = [p.ref for p in self.parts]
        if len(refs) != len(set(refs)):
            raise ValueError("selected part references must be unique")
        return self


class SelectionPatch(ProposalModel):
    """A bounded repair delta for an existing component selection."""

    upsert_parts: list[SelectedPart] = Field(default_factory=list, max_length=32)
    remove_refs: list[str] = Field(default_factory=list, max_length=32)
    rationale: str = Field(default="", max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def _expand_grouped_references(cls, value: object) -> object:
        return _expand_grouped_parts(value, "upsert_parts")

    @model_validator(mode="after")
    def _unique_refs(self) -> SelectionPatch:
        upserts = [part.ref.upper() for part in self.upsert_parts]
        removals = [ref.upper() for ref in self.remove_refs]
        if len(upserts) != len(set(upserts)):
            raise ValueError("selection patch upsert references must be unique")
        if len(removals) != len(set(removals)):
            raise ValueError("selection patch removal references must be unique")
        if set(upserts) & set(removals):
            raise ValueError("a selection patch cannot upsert and remove the same ref")
        return self


class LogicalPin(ProposalModel):
    """A pin referenced by logical name/role, not yet a real pin number.

    ``pin`` may be a functional name (``VCC``, ``GND``, ``IN``, ``XTAL1``) or a
    passive terminal (``1``/``2``); the pin-mapping step resolves it to the
    real device pin number using the symbol library.
    """

    ref: str = Field(min_length=1, max_length=32)
    pin: str = Field(min_length=1, max_length=48)

    def key(self) -> str:
        return f"{self.ref}:{self.pin}"


class NetIntent(ProposalModel):
    """One electrical net expressed as logical pins (pre pin-number mapping)."""

    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="signal", max_length=32)  # power/ground/signal/clock
    pins: list[LogicalPin] = Field(default_factory=list, max_length=500)
    purpose: str = Field(default="", max_length=2_000)

    @field_validator("pins", mode="before")
    @classmethod
    def _deduplicate_identical_pins(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        seen: set[tuple[str, str]] = set()
        unique = []
        for item in value:
            if isinstance(item, dict):
                key = (str(item.get("ref", "")), str(item.get("pin", "")))
            else:
                key = (
                    str(getattr(item, "ref", "")),
                    str(getattr(item, "pin", "")),
                )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @model_validator(mode="after")
    def _unique_pins(self) -> NetIntent:
        keys = [p.key() for p in self.pins]
        if len(keys) != len(set(keys)):
            raise ValueError(f"net {self.name!r} contains duplicate pins")
        return self


class NetlistIntent(ProposalModel):
    """The electrical connection intent for the whole design.

    Single-pin / empty nets are *allowed to construct* so the bottom-line check
    can catch and report them (fail closed) rather than raising during parsing.
    """

    additional_parts: list[SelectedPart] = Field(default_factory=list, max_length=64)
    nets: list[NetIntent] = Field(default_factory=list, max_length=5_000)
    no_connect_pins: list[LogicalPin] = Field(default_factory=list, max_length=2_000)
    supply_nets: list[str] = Field(default_factory=list, max_length=50)
    ground_net: str = Field(default="GND", min_length=1, max_length=100)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def _unique_net_names(self) -> NetlistIntent:
        names = [n.name for n in self.nets]
        if len(names) != len(set(names)):
            raise ValueError("net names must be unique")
        refs = [part.ref for part in self.additional_parts]
        if len(refs) != len(set(refs)):
            raise ValueError("additional part references must be unique")
        no_connect_keys = [pin.key() for pin in self.no_connect_pins]
        if len(no_connect_keys) != len(set(no_connect_keys)):
            raise ValueError("no-connect pins must be unique")
        return self

    def net(self, name: str) -> NetIntent | None:
        for n in self.nets:
            if n.name == name:
                return n
        return None


class NetlistPatch(ProposalModel):
    """A bounded repair delta for an existing :class:`NetlistIntent`.

    ``additional_parts`` is the complete replacement list. Pins in
    ``upsert_nets`` are moved from any old net to the named net, which lets a
    repair correct a short without regenerating the whole netlist.
    """

    additional_parts: list[SelectedPart] = Field(default_factory=list, max_length=8)
    remove_nets: list[str] = Field(default_factory=list, max_length=500)
    remove_pins: list[LogicalPin] = Field(default_factory=list, max_length=2_000)
    upsert_nets: list[NetIntent] = Field(default_factory=list, max_length=2_000)
    add_no_connect_pins: list[LogicalPin] = Field(default_factory=list, max_length=2_000)
    remove_no_connect_pins: list[LogicalPin] = Field(default_factory=list, max_length=2_000)


class MappedPin(ProposalModel):
    """A logical pin resolved to a real device pin number from the symbol lib."""

    ref: str = Field(min_length=1, max_length=32)
    logical: str = Field(min_length=1, max_length=48)
    number: str = Field(min_length=1, max_length=16)


class MappedNet(ProposalModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="signal", max_length=32)
    pins: list[MappedPin] = Field(default_factory=list, max_length=500)


class PinMapPlan(ProposalModel):
    """Netlist with real pin numbers, plus any logical pins that failed to map."""

    nets: list[MappedNet] = Field(default_factory=list, max_length=5_000)
    unresolved: list[str] = Field(default_factory=list, max_length=1_000)
    rationale: str = Field(default="", max_length=10_000)


class SheetPlacement(ProposalModel):
    """A symbol's position on the schematic sheet (mm). No electrical meaning."""

    ref: str = Field(min_length=1, max_length=32)
    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)
    rotation: float = Field(default=0, ge=0, lt=360)


class SchLayoutPlan(ProposalModel):
    """Sheet placement + which nets are drawn as labels vs local wires."""

    placements: list[SheetPlacement] = Field(default_factory=list, max_length=2_000)
    label_nets: list[str] = Field(default_factory=list, max_length=5_000)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def _unique_placements(self) -> SchLayoutPlan:
        refs = [p.ref for p in self.placements]
        if len(refs) != len(set(refs)):
            raise ValueError("sheet placement references must be unique")
        return self


class MaterializeResult(ProposalModel):
    """Outcome of writing the .kicad_sch (paths + counts for the round-trip check)."""

    sch_path: str = Field(min_length=1)
    component_count: int = Field(default=0, ge=0)
    net_count: int = Field(default=0, ge=0)
    label_count: int = Field(default=0, ge=0)


class ErcSummary(ProposalModel):
    """Result of the schematic ERC bottom-line (deterministic + optional cli ERC)."""

    sch_path: str = Field(min_length=1)
    shorted_nets: list[list[str]] = Field(default_factory=list)
    single_pin_nets: list[str] = Field(default_factory=list)
    cli_available: bool = False
    cli_ran: bool = False
    cli_error_count: int = Field(default=0, ge=0)
    cli_warning_count: int = Field(default=0, ge=0)
    cli_error_details: list[str] = Field(default_factory=list)
    cli_report_path: str = ""


class BoardZone(ProposalModel):
    """A functional placement zone on the board (mm rectangle)."""

    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=40)  # power/analog/digital/rf/connector/mixed
    x1: float = Field(ge=-10_000, le=10_000)
    y1: float = Field(ge=-10_000, le=10_000)
    x2: float = Field(ge=-10_000, le=10_000)
    y2: float = Field(ge=-10_000, le=10_000)

    @model_validator(mode="after")
    def _ordered(self) -> BoardZone:
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError(f"zone {self.name!r} must have x2>x1 and y2>y1")
        return self


class PlacementConstraint(ProposalModel):
    """One deterministic spatial rule compiled from a board partition."""

    constraint_id: str = Field(min_length=1, max_length=160)
    kind: Literal["in_zone", "edge", "opposite_edges", "ordered"]
    refs: list[str] = Field(min_length=1, max_length=100)
    region: tuple[float, float, float, float] | None = None
    edge: Literal["left", "right", "top", "bottom", ""] = ""
    max_distance_mm: float = Field(default=0.0, ge=0, le=10_000)
    axis: Literal["x", "y", ""] = ""
    hard: bool = True
    source: Literal["partition", "profile", "requirement", "derived"] = "derived"
    evidence: str = Field(default="", max_length=1_000)


class PlacementConstraintSet(ProposalModel):
    """Versioned constraints that survive checkpoints and bounded repairs."""

    schema_version: Literal["ratsnestpro.placement-constraints.v1"] = (
        "ratsnestpro.placement-constraints.v1"
    )
    constraints: list[PlacementConstraint] = Field(default_factory=list, max_length=500)
    board_width: float = Field(default=0.0, ge=0, le=10_000)
    board_height: float = Field(default=0.0, ge=0, le=10_000)
    constraint_digest: str = Field(default="", max_length=64)
    source_requirement_digest: str = Field(default="", max_length=64)


class BoardPartition(ProposalModel):
    """Board outline + functional zones (the placement plan skeleton)."""

    board_width: float = Field(gt=0, le=10_000)
    board_height: float = Field(gt=0, le=10_000)
    zones: list[BoardZone] = Field(default_factory=list, max_length=100)
    placement_constraints: PlacementConstraintSet = Field(
        default_factory=PlacementConstraintSet
    )
    rationale: str = Field(default="", max_length=10_000)


class PcbPlacement(ProposalModel):
    """A component's placement on the PCB (mm)."""

    ref: str = Field(min_length=1, max_length=32)
    x: float = Field(ge=-10_000, le=10_000)
    y: float = Field(ge=-10_000, le=10_000)
    rotation: float = Field(default=0.0, ge=0, lt=360)
    side: str = Field(default="front", max_length=8)


class PcbPlacementPlan(ProposalModel):
    """Accumulated PCB placements + board outline (grows critical -> general)."""

    board_width: float = Field(gt=0, le=10_000)
    board_height: float = Field(gt=0, le=10_000)
    placements: list[PcbPlacement] = Field(default_factory=list, max_length=2_000)
    rationale: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def _unique(self) -> PcbPlacementPlan:
        refs = [p.ref for p in self.placements]
        if len(refs) != len(set(refs)):
            raise ValueError("PCB placement references must be unique")
        return self

    def by_ref(self) -> dict[str, PcbPlacement]:
        return {p.ref: p for p in self.placements}


class PcbWriteResult(ProposalModel):
    """Outcome of writing the .kicad_pcb (path + counts for the bottom-line check)."""

    pcb_path: str = Field(min_length=1)
    component_count: int = Field(default=0, ge=0)
    overlaps: list[str] = Field(default_factory=list)
    out_of_bounds: list[str] = Field(default_factory=list)
    has_board_outline: bool = False
    placement_constraints_path: str = ""


class PlacementConstraintReview(ProposalModel):
    """Independent review of persisted constraints against a final PCB."""

    manifest_path: str = ""
    manifest_found: bool = False
    evaluated: bool = False
    digest_valid: bool = False
    placement_count: int = Field(default=0, ge=0)
    missing_refs: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    error: str = ""


class NetClass(ProposalModel):
    """A routing net class: geometry rules for a group of nets (mm)."""

    name: str = Field(min_length=1, max_length=60)
    width: float = Field(gt=0)
    clearance: float = Field(gt=0)
    via_diameter: float = Field(default=0.6, gt=0)
    via_drill: float = Field(default=0.3, gt=0)
    layer: str = Field(default="F.Cu", max_length=40)

    @field_validator("layer", mode="before")
    @classmethod
    def _normalize_layer_number(cls, value: object) -> object:
        """Accept the common 1-based copper-layer notation from LLMs."""
        if isinstance(value, int) and not isinstance(value, bool):
            if value == 1:
                return "F.Cu"
            if value == 2:
                return "B.Cu"
            if value > 2:
                return f"In{value - 1}.Cu"
        if isinstance(value, str) and value.strip().isdigit():
            return cls._normalize_layer_number(int(value.strip()))
        return value


class RoutePlan(ProposalModel):
    """Stackup + net-class routing rules."""

    layers: int = Field(default=2, ge=1, le=16)
    net_classes: list[NetClass] = Field(default_factory=list, max_length=100)
    rationale: str = Field(default="", max_length=10_000)

    def net_class(self, name: str) -> NetClass | None:
        for nc in self.net_classes:
            if nc.name == name:
                return nc
        return None


class PlanePlan(ProposalModel):
    """Copper planes + the critical nets to route first."""

    ground_net: str = Field(default="GND", min_length=1, max_length=100)
    planes: list[str] = Field(default_factory=list, max_length=32)  # "B.Cu:GND" etc.
    critical_nets: list[str] = Field(default_factory=list, max_length=500)
    rationale: str = Field(default="", max_length=10_000)


class RouteResult(ProposalModel):
    """Outcome of signal routing, including auditable DSN/SES artifacts."""

    method: str = Field(default="deferred", max_length=40)  # freerouting/deferred/manual
    required: bool = False
    layers: int = Field(default=2, ge=1, le=16)
    # Legacy logical-net fields.  A partial physical connection count cannot
    # establish how many whole logical nets are complete, so routed_nets is
    # conservative (all nets only when no physical connections remain).
    routed_nets: int = Field(default=0, ge=0)
    total_nets: int = Field(default=0, ge=0)
    routed_connections: int = Field(default=-1, ge=-1)
    total_connections: int = Field(default=-1, ge=-1)
    metric_basis: str = Field(default="unavailable", max_length=80)
    assigned_pads: int = Field(default=0, ge=0)
    routed_tracks: int = Field(default=0, ge=0)
    unconnected: int = Field(default=-1, ge=-1)
    dsn_path: str = Field(default="", max_length=1_000)
    ses_path: str = Field(default="", max_length=1_000)
    note: str = Field(default="", max_length=2_000)


class FabAudit(ProposalModel):
    """Result of the routing fabrication bottom-line audit."""

    violations: list[str] = Field(default_factory=list)


class ManufactureResult(ProposalModel):
    """Manufacturing outputs + the DRC bottom-line summary."""

    bom_path: str = Field(default="", max_length=1_000)
    cpl_path: str = Field(default="", max_length=1_000)
    unresolved_manifest_path: str = Field(default="", max_length=1_000)
    component_release_ready: bool = False
    component_release_blockers: list[str] = Field(default_factory=list)
    gerber_dir: str = Field(default="", max_length=1_000)
    manufacturing_export_applicable: bool = False
    gerber_exported: bool = False
    drill_paths: list[str] = Field(default_factory=list)
    drill_exported: bool = False
    drc_violations: list[str] = Field(default_factory=list)
