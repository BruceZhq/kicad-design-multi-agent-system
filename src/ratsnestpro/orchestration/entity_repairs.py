"""Classify KiCad findings into evidence-bounded entity repair plans.

The classifier does not mutate a schematic or PCB.  It only identifies the
owning pipeline stage and extracts the concrete references, pins, pads, and
positions that a bounded repair loop must verify before editing a candidate.
Unknown or under-specified findings fail closed to manual review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from ratsnestpro.domain.contracts import ContractModel


class EntityRepairCategory(StrEnum):
    SCHEMATIC_CONNECTIVITY = "schematic_connectivity"
    FOOTPRINT_GEOMETRY = "footprint_geometry"
    LAYOUT = "layout"
    ROUTING = "routing"
    SILKSCREEN = "silkscreen"
    ZONE_ROUTING = "zone_routing"
    UNCLASSIFIED = "unclassified"


class RepairExecutionPolicy(StrEnum):
    """Whether enough evidence exists to enter a bounded candidate loop."""

    BOUNDED_CANDIDATE = "bounded_candidate"
    MANUAL_REVIEW = "manual_review"


class FindingPosition(ContractModel):
    x: float
    y: float
    layer: str | None = None
    item_uuid: str | None = None


class AffectedTerminal(ContractModel):
    ref: str
    number: str
    kind: Literal["pin", "pad"]


class EntityRepairPlan(ContractModel):
    finding_type: str
    source_section: str = ""
    category: EntityRepairCategory
    rollback_step: str | None = None
    strategy: str
    execution_policy: RepairExecutionPolicy
    affected_refs: list[str] = Field(default_factory=list)
    affected_pins: list[AffectedTerminal] = Field(default_factory=list)
    affected_pads: list[AffectedTerminal] = Field(default_factory=list)
    positions: list[FindingPosition] = Field(default_factory=list)
    reason: str


_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z]{1,4}\d+[A-Z]?)(?![A-Za-z0-9_])"
)
_TERMINAL_OF_REF_RE = re.compile(
    r"\b(?P<kind>Pad|PTH\s+pad|Pin)\s+(?P<number>[^\s,;()\[\]]+)"
    r".*?\b(?:of|on)\s+(?P<ref>[A-Z]{1,4}\d+[A-Z]?)\b",
    re.IGNORECASE,
)
_REF_TERMINAL_RE = re.compile(
    r"\b(?P<ref>[A-Z]{1,4}\d+[A-Z]?)\s*(?:[:/.,-]\s*)?"
    r"(?P<kind>pad|pin)\s+(?P<number>[^\s,;()\[\]]+)",
    re.IGNORECASE,
)
_LAYER_RE = re.compile(r"\bon\s+((?:F|B|In\d+)\.[A-Za-z]+)\b")
_PIN_NOT_CONNECTED_RE = re.compile(
    r"\b(?:pin[^\n]{0,80}(?:not[ _-]?connected|unconnected)|"
    r"(?:not[ _-]?connected|unconnected)[^\n]{0,80}pin)\b",
    re.IGNORECASE,
)
_SILK_RE = re.compile(r"silk(?:screen)?|f\.silks|b\.silks", re.IGNORECASE)
_TRACK_RE = re.compile(r"\btrack|trace|via|segment\b", re.IGNORECASE)
_ZONE_RE = re.compile(r"\bzone|copper pour|filled area\b", re.IGNORECASE)

_PIN_NOT_CONNECTED_TYPES = {
    "pin_not_connected",
    "unconnected_pin",
    "input_pin_not_driven",
}
_FOOTPRINT_GEOMETRY_TYPES = {"shorting_items", "solder_mask_bridge"}
_LAYOUT_TYPES = {"clearance", "courtyards_overlap", "footprint_overlap"}
_ROUTING_TYPES = {"tracks_crossing", "track_clearance", "via_clearance"}
_UNCONNECTED_TYPES = {
    "unconnected",
    "unconnected_items",
    "unconnected_track",
    "unconnected_pad",
}


def _normalized_type(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "unknown").lower()).strip("_")


def _finding_items(finding: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = finding.get("items", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _description(record: Mapping[str, Any]) -> str:
    for key in ("description", "message", "text"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _explicit_ref(record: Mapping[str, Any]) -> str | None:
    for key in ("ref", "reference", "reference_designator"):
        value = record.get(key)
        if isinstance(value, str) and _REF_RE.fullmatch(value.strip()):
            return value.strip().upper()
    return None


def _terminals(text: str) -> list[AffectedTerminal]:
    found: list[AffectedTerminal] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern in (_TERMINAL_OF_REF_RE, _REF_TERMINAL_RE):
        for match in pattern.finditer(text):
            ref = match.group("ref").upper()
            number = match.group("number").rstrip(".")
            kind = "pin" if "pin" in match.group("kind").lower() else "pad"
            identity = (ref, number, kind)
            if identity not in seen:
                seen.add(identity)
                found.append(AffectedTerminal(ref=ref, number=number, kind=kind))
    return found


def _position(record: Mapping[str, Any], description: str) -> FindingPosition | None:
    raw = record.get("pos", record.get("position"))
    if not isinstance(raw, Mapping):
        return None
    try:
        x = float(raw["x"])
        y = float(raw["y"])
    except (KeyError, TypeError, ValueError):
        return None
    layer = record.get("layer")
    if not isinstance(layer, str) or not layer:
        match = _LAYER_RE.search(description)
        layer = match.group(1) if match is not None else None
    item_uuid = record.get("uuid")
    return FindingPosition(
        x=x,
        y=y,
        layer=layer,
        item_uuid=str(item_uuid) if item_uuid else None,
    )


def _evidence(finding: Mapping[str, Any]) -> tuple[
    str,
    list[str],
    list[AffectedTerminal],
    list[AffectedTerminal],
    list[FindingPosition],
]:
    records = [finding, *_finding_items(finding)]
    descriptions = [_description(record) for record in records]
    text = "\n".join(part for part in descriptions if part)
    terminals: list[AffectedTerminal] = []
    refs: list[str] = []
    positions: list[FindingPosition] = []
    for record, description in zip(records, descriptions, strict=True):
        explicit_ref = _explicit_ref(record)
        if explicit_ref is not None:
            refs.append(explicit_ref)
        terminals.extend(_terminals(description))
        refs.extend(_REF_RE.findall(description))
        position = _position(record, description)
        if position is not None:
            positions.append(position)
    refs.extend(terminal.ref for terminal in terminals)
    unique_refs = list(dict.fromkeys(ref.upper() for ref in refs))
    unique_terminals = list({
        (terminal.ref, terminal.number, terminal.kind): terminal
        for terminal in terminals
    }.values())
    unique_positions = list({
        (position.x, position.y, position.layer, position.item_uuid): position
        for position in positions
    }.values())
    pins = [terminal for terminal in unique_terminals if terminal.kind == "pin"]
    pads = [terminal for terminal in unique_terminals if terminal.kind == "pad"]
    return text, unique_refs, pins, pads, unique_positions


def _plan_attributes(
    finding_type: str,
    source_section: str,
    text: str,
    refs: list[str],
    pins: list[AffectedTerminal],
    pads: list[AffectedTerminal],
    positions: list[FindingPosition],
) -> tuple[EntityRepairCategory, str | None, str, bool, str]:
    source = _normalized_type(source_section)
    is_unconnected = (
        source == "unconnected_items"
        or finding_type in _UNCONNECTED_TYPES
        or finding_type.startswith("unconnected_")
    )
    if finding_type in _PIN_NOT_CONNECTED_TYPES or _PIN_NOT_CONNECTED_RE.search(text):
        return (
            EntityRepairCategory.SCHEMATIC_CONNECTIVITY,
            "schematic_materialize",
            "reconnect_exact_pin_endpoint_from_design_ir",
            bool(refs and pins),
            "ERC reports an electrically unconnected schematic pin",
        )
    if is_unconnected:
        enough = len(positions) >= 2 or len(pads) >= 2
        return (
            EntityRepairCategory.ZONE_ROUTING,
            "route_planes" if _ZONE_RE.search(text) else "route_signals",
            "rebuild_zones_and_route_from_clean_snapshot",
            enough,
            "KiCad reports a physical connectivity gap",
        )
    if _SILK_RE.search(f"{finding_type} {text}"):
        return (
            EntityRepairCategory.SILKSCREEN,
            "layout_write",
            "move_or_clip_real_silkscreen_entities",
            bool(refs or positions),
            "the finding concerns real silkscreen geometry",
        )
    terminal_refs = {terminal.ref for terminal in pads}
    same_footprint = len(terminal_refs) == 1 or (
        not terminal_refs and len(refs) == 1
    )
    if finding_type in _FOOTPRINT_GEOMETRY_TYPES and same_footprint:
        return (
            EntityRepairCategory.FOOTPRINT_GEOMETRY,
            "layout_write",
            "repair_or_substitute_verified_footprint_geometry",
            len(pads) >= 2,
            "multiple offending pads belong to the same footprint",
        )
    if finding_type in _ROUTING_TYPES or _TRACK_RE.search(text):
        return (
            EntityRepairCategory.ROUTING,
            "route_signals",
            "reroute_affected_nets_from_clean_route_snapshot",
            bool(positions or pads),
            "the finding involves routed copper entities",
        )
    if finding_type in _LAYOUT_TYPES or finding_type in _FOOTPRINT_GEOMETRY_TYPES:
        return (
            EntityRepairCategory.LAYOUT,
            "layout_general",
            "replace_affected_footprints_from_clean_layout_snapshot",
            bool(refs or positions),
            "the finding is a placement or cross-footprint geometry conflict",
        )
    return (
        EntityRepairCategory.UNCLASSIFIED,
        None,
        "no_automatic_repair",
        False,
        "no deterministic entity repair mapping exists for this finding",
    )


def classify_kicad_finding(
    finding: Mapping[str, Any],
    *,
    source_section: str = "",
) -> EntityRepairPlan:
    """Return a non-mutating, deterministic repair plan for one KiCad finding."""

    if not isinstance(finding, Mapping):
        raise TypeError("finding must be a mapping")
    finding_type = _normalized_type(
        finding.get("type", finding.get("rule", finding.get("rule_id", "unknown")))
    )
    text, refs, pins, pads, positions = _evidence(finding)
    category, rollback_step, strategy, evidence_complete, reason = _plan_attributes(
        finding_type,
        source_section,
        text,
        refs,
        pins,
        pads,
        positions,
    )
    return EntityRepairPlan(
        finding_type=finding_type,
        source_section=source_section,
        category=category,
        rollback_step=rollback_step,
        strategy=strategy,
        execution_policy=(
            RepairExecutionPolicy.BOUNDED_CANDIDATE
            if evidence_complete
            else RepairExecutionPolicy.MANUAL_REVIEW
        ),
        affected_refs=refs,
        affected_pins=pins,
        affected_pads=pads,
        positions=positions,
        reason=(
            reason
            if evidence_complete or category == EntityRepairCategory.UNCLASSIFIED
            else f"{reason}; concrete target evidence is incomplete"
        ),
    )


def classify_kicad_report(report: Mapping[str, Any]) -> list[EntityRepairPlan]:
    """Classify all structured ERC/DRC findings while preserving their section."""

    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    plans: list[EntityRepairPlan] = []
    for section in ("violations", "schematic_parity", "unconnected_items"):
        findings = report.get(section, [])
        if not isinstance(findings, list):
            continue
        plans.extend(
            classify_kicad_finding(finding, source_section=section)
            for finding in findings
            if isinstance(finding, Mapping)
        )
    return plans
