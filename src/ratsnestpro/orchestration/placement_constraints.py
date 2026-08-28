"""Compile and evaluate deterministic PCB placement constraints."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from ratsnestpro.orchestration.pipeline_contracts import (
    BoardPartition,
    BoardZone,
    PcbPlacement,
    PlacementConstraint,
    PlacementConstraintReview,
    PlacementConstraintSet,
)

_EDGE_EPSILON_MM = 1e-6


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(token) if token.isdigit() else token.casefold()
        for token in re.split(r"(\d+)", value)
    )


def _placement_group(value: str) -> str:
    text = value.casefold().replace("-", "_")
    if "mounting" in text and "hole" in text or "mechanical_mounting" in text:
        return "mounting_hole"
    if any(token in text for token in ("connector", "header", "socket", "receptacle")):
        return "connector"
    if "timer" in text:
        return "timer"
    if any(token in text for token in ("mcu", "controller", "digital")):
        return "digital"
    if any(token in text for token in ("analog", "adc", "timing_rc")):
        return "analog"
    if any(token in text for token in ("led", "button", "interface", "user")):
        return "interface"
    if any(token in text for token in ("power", "regulator", "buck", "ldo")):
        return "power"
    if any(token in text for token in ("sensor", "i2c")):
        return "sensor"
    if any(token in text for token in ("storage", "flash", "sdio", "microsd")):
        return "storage"
    return ""


def bind_zone_targets(
    partition: BoardPartition,
    roles: Mapping[str, str],
) -> BoardPartition:
    """Bind repeated functional zones to stable component identities.

    An LLM may describe four identical mounting zones without knowing that the
    downstream placement contract needs H1/H2/H3/H4 identities.  Equal-cardinality
    groups are paired deterministically by natural reference order and geometric
    top-to-bottom/left-to-right order.  Ambiguous unequal groups remain unbound.
    """

    zones = [zone.model_copy(deep=True) for zone in partition.zones]
    refs = set(roles)
    assigned_refs: set[str] = set()
    assigned_zone_indexes: set[int] = set()

    for index, zone in enumerate(zones):
        target = zone.target_ref.strip()
        if target in refs and target not in assigned_refs:
            assigned_refs.add(target)
            assigned_zone_indexes.add(index)
            continue
        zone.target_ref = ""

    for index, zone in enumerate(zones):
        if index in assigned_zone_indexes:
            continue
        matches = [
            ref
            for ref in refs - assigned_refs
            if zone.name.strip().casefold() == ref.casefold()
            or re.search(
                rf"(?<![A-Za-z0-9]){re.escape(ref)}(?![A-Za-z0-9])",
                zone.name,
                re.IGNORECASE,
            )
        ]
        if len(matches) == 1:
            zone.target_ref = matches[0]
            assigned_refs.add(matches[0])
            assigned_zone_indexes.add(index)

    ref_groups: dict[str, list[str]] = {}
    for ref, role in roles.items():
        if ref in assigned_refs:
            continue
        group = _placement_group(role)
        if group:
            ref_groups.setdefault(group, []).append(ref)
    zone_groups: dict[str, list[int]] = {}
    for index, zone in enumerate(zones):
        if index in assigned_zone_indexes:
            continue
        group = _placement_group(f"{zone.kind} {zone.name}")
        if group:
            zone_groups.setdefault(group, []).append(index)

    for group, grouped_refs in ref_groups.items():
        grouped_zones = zone_groups.get(group, [])
        if len(grouped_refs) < 2 or len(grouped_refs) != len(grouped_zones):
            continue
        ordered_refs = sorted(grouped_refs, key=_natural_key)
        ordered_zones = sorted(
            grouped_zones,
            key=lambda index: (
                (zones[index].y1 + zones[index].y2) / 2,
                (zones[index].x1 + zones[index].x2) / 2,
                zones[index].name.casefold(),
            ),
        )
        for ref, index in zip(ordered_refs, ordered_zones, strict=True):
            zones[index].target_ref = ref
            assigned_refs.add(ref)
            assigned_zone_indexes.add(index)

    return partition.model_copy(update={"zones": zones})


def _constraint_digest(constraints: list[PlacementConstraint]) -> str:
    payload = [item.model_dump(mode="json") for item in constraints]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _exact_zones(
    partition: BoardPartition,
    refs: set[str],
) -> dict[str, BoardZone]:
    by_target: dict[str, list[BoardZone]] = {}
    for zone in partition.zones:
        if zone.target_ref:
            by_target.setdefault(zone.target_ref.casefold(), []).append(zone)
    by_name: dict[str, list[BoardZone]] = {}
    for zone in partition.zones:
        by_name.setdefault(zone.name.strip().casefold(), []).append(zone)
    return {
        ref: matches[0]
        for ref in refs
        if len(
            matches := (
                by_target.get(ref.strip().casefold(), [])
                or by_name.get(ref.strip().casefold(), [])
            )
        ) == 1
    }


def _zone_edge(zone: BoardZone, width: float, height: float) -> str:
    edges = []
    if zone.x1 <= _EDGE_EPSILON_MM:
        edges.append("left")
    if zone.x2 >= width - _EDGE_EPSILON_MM:
        edges.append("right")
    if zone.y1 <= _EDGE_EPSILON_MM:
        edges.append("top")
    if zone.y2 >= height - _EDGE_EPSILON_MM:
        edges.append("bottom")
    return edges[0] if len(edges) == 1 else ""


def _edge_distance_limit(
    zone: BoardZone,
    edge: str,
    width: float,
    height: float,
) -> float:
    if edge == "left":
        return zone.x2
    if edge == "right":
        return width - zone.x1
    if edge == "top":
        return zone.y2
    return height - zone.y1


def _non_overlapping_order(
    zones: Mapping[str, BoardZone],
    axis: str,
) -> list[str]:
    if axis == "x":
        ordered = sorted(zones, key=lambda ref: (zones[ref].x1, zones[ref].x2, ref))
        return ordered if all(
            zones[left].x2 <= zones[right].x1
            for left, right in zip(ordered, ordered[1:], strict=False)
        ) else []
    ordered = sorted(zones, key=lambda ref: (zones[ref].y1, zones[ref].y2, ref))
    return ordered if all(
        zones[left].y2 <= zones[right].y1
        for left, right in zip(ordered, ordered[1:], strict=False)
    ) else []


def compile_placement_constraints(
    partition: BoardPartition,
    roles: Mapping[str, str],
    requirement_text: str,
) -> PlacementConstraintSet:
    """Compile exact zones into generic hard constraints.

    LLM-provided constraint objects are deliberately ignored.  The compiler
    derives them from the validated partition and selected references.
    """

    partition = bind_zone_targets(partition, roles)
    exact = _exact_zones(partition, set(roles))
    constraints: list[PlacementConstraint] = []
    edge_by_ref: dict[str, str] = {}
    for ref, zone in sorted(exact.items()):
        constraints.append(PlacementConstraint(
            constraint_id=f"zone:{ref}",
            kind="in_zone",
            refs=[ref],
            region=(zone.x1, zone.y1, zone.x2, zone.y2),
            source="partition",
            evidence=f"exact partition zone {zone.name}",
        ))
        if "connector" in roles.get(ref, "").casefold():
            edge = _zone_edge(zone, partition.board_width, partition.board_height)
            if edge:
                edge_by_ref[ref] = edge
                constraints.append(PlacementConstraint(
                    constraint_id=f"edge:{ref}:{edge}",
                    kind="edge",
                    refs=[ref],
                    edge=edge,  # type: ignore[arg-type]
                    max_distance_mm=_edge_distance_limit(
                        zone, edge, partition.board_width, partition.board_height
                    ),
                    source="derived",
                    evidence=f"connector zone {zone.name} touches {edge} edge",
                ))

    opposite = {("left", "right"), ("right", "left"), ("top", "bottom"), ("bottom", "top")}
    edge_refs = sorted(edge_by_ref)
    for index, left in enumerate(edge_refs):
        for right in edge_refs[index + 1:]:
            if (edge_by_ref[left], edge_by_ref[right]) not in opposite:
                continue
            axis = "x" if edge_by_ref[left] in {"left", "right"} else "y"
            constraints.append(PlacementConstraint(
                constraint_id=f"opposite:{left}:{right}",
                kind="opposite_edges",
                refs=[left, right],
                axis=axis,
                source="derived",
                evidence="exact connector zones touch opposite board edges",
            ))

    if len(exact) >= 3:
        x_order = _non_overlapping_order(exact, "x")
        y_order = _non_overlapping_order(exact, "y")
        ordered = x_order or y_order
        axis = "x" if x_order else "y"
        if ordered:
            constraints.append(PlacementConstraint(
                constraint_id=f"ordered:{axis}:{':'.join(ordered)}",
                kind="ordered",
                refs=ordered,
                axis=axis,
                source="derived",
                evidence="non-overlapping exact reference zones define a stable order",
            ))

    return PlacementConstraintSet(
        constraints=constraints,
        board_width=partition.board_width,
        board_height=partition.board_height,
        constraint_digest=_constraint_digest(constraints),
        source_requirement_digest=hashlib.sha256(requirement_text.encode()).hexdigest(),
    )


def allowed_origin_regions(
    constraints: PlacementConstraintSet,
) -> dict[str, tuple[float, float, float, float]]:
    return {
        item.refs[0]: item.region
        for item in constraints.constraints
        if item.hard and item.kind == "in_zone" and item.region is not None
    }


def required_edges(constraints: PlacementConstraintSet) -> dict[str, str]:
    return {
        item.refs[0]: item.edge
        for item in constraints.constraints
        if item.hard and item.kind == "edge" and item.edge
    }


def placement_constraint_violations(
    constraints: PlacementConstraintSet,
    placements: Mapping[str, PcbPlacement],
) -> list[str]:
    violations: list[str] = []
    for item in constraints.constraints:
        if not item.hard or any(ref not in placements for ref in item.refs):
            continue
        if item.kind == "in_zone" and item.region is not None:
            placement = placements[item.refs[0]]
            x1, y1, x2, y2 = item.region
            if not (x1 <= placement.x <= x2 and y1 <= placement.y <= y2):
                violations.append(f"{item.constraint_id}: {item.refs[0]} outside {item.region}")
        elif item.kind == "edge" and item.edge:
            placement = placements[item.refs[0]]
            distance = {
                "left": placement.x,
                "right": constraints.board_width - placement.x,
                "top": placement.y,
                "bottom": constraints.board_height - placement.y,
            }[item.edge]
            if distance > item.max_distance_mm:
                violations.append(
                    f"{item.constraint_id}: distance {distance:.3f} mm exceeds "
                    f"{item.max_distance_mm:.3f} mm"
                )
        elif item.kind == "opposite_edges":
            left, right = (placements[ref] for ref in item.refs)
            if item.axis == "x" and left.x == right.x:
                violations.append(f"{item.constraint_id}: equal x coordinates")
            elif item.axis == "y" and left.y == right.y:
                violations.append(f"{item.constraint_id}: equal y coordinates")
        elif item.kind == "ordered":
            values = [
                placements[ref].x if item.axis == "x" else placements[ref].y
                for ref in item.refs
            ]
            if any(left >= right for left, right in zip(values, values[1:], strict=False)):
                violations.append(f"{item.constraint_id}: order is {values}")
    return violations


def write_placement_constraint_manifest(
    path: Path,
    constraints: PlacementConstraintSet,
) -> None:
    """Atomically persist the exact constraints used by the layout solver."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(constraints.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def review_pcb_placement_constraints(
    pcb_path: Path,
    manifest_path: Path | None = None,
) -> PlacementConstraintReview:
    """Re-read final PCB coordinates and independently evaluate the manifest."""

    manifest = manifest_path or pcb_path.with_suffix(".placement_constraints.json")
    if not manifest.is_file():
        return PlacementConstraintReview(
            manifest_path=str(manifest),
            error="placement constraint manifest not found",
        )
    try:
        constraints = PlacementConstraintSet.model_validate_json(
            manifest.read_text(encoding="utf-8")
        )
    except Exception as exc:  # noqa: BLE001 - malformed evidence is reported
        return PlacementConstraintReview(
            manifest_path=str(manifest),
            manifest_found=True,
            violations=["placement constraint manifest is invalid"],
            error=f"{type(exc).__name__}: {exc}",
        )

    digest_valid = (
        bool(constraints.constraint_digest)
        and constraints.constraint_digest == _constraint_digest(constraints.constraints)
    )
    if not digest_valid:
        return PlacementConstraintReview(
            manifest_path=str(manifest),
            manifest_found=True,
            violations=["placement constraint digest mismatch"],
            error="constraint manifest integrity check failed",
        )

    try:
        from ratsnestpro.eda.vendor.pcb import PcbBoard

        board = PcbBoard.load(pcb_path)
        placements = {
            str(item["reference"]): PcbPlacement(
                ref=str(item["reference"]),
                x=float(item["at"]["x"]),
                y=float(item["at"]["y"]),
                rotation=float(item["at"]["rotation"]),
                side=(
                    "back"
                    if str(item.get("layer", "")).startswith("B.")
                    else "front"
                ),
            )
            for item in board.list_footprints()
            if item.get("reference") and item.get("at")
        }
    except Exception as exc:  # noqa: BLE001 - reviewer reports unreadable PCB
        return PlacementConstraintReview(
            manifest_path=str(manifest),
            manifest_found=True,
            digest_valid=True,
            violations=["final PCB could not be read for placement review"],
            error=f"{type(exc).__name__}: {exc}",
        )

    required_refs = {
        ref
        for item in constraints.constraints
        if item.hard
        for ref in item.refs
    }
    missing = sorted(required_refs - set(placements))
    violations = [
        *(f"missing constrained footprint: {ref}" for ref in missing),
        *placement_constraint_violations(constraints, placements),
    ]
    return PlacementConstraintReview(
        manifest_path=str(manifest),
        manifest_found=True,
        evaluated=True,
        digest_valid=True,
        placement_count=len(placements),
        missing_refs=missing,
        violations=violations,
    )
