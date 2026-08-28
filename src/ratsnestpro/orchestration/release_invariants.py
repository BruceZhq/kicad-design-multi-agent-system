"""Deterministic requirement invariants for manufacturing release.

The language model may propose topology, placement, and routing, but explicit
user constraints are compiled once and re-checked against the final KiCad
board.  This module is deliberately board-family agnostic: it recognizes only
physical contracts that can be proven from the generated project.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ratsnestpro.domain.contracts import ContractModel

_EVIDENCE_MARKERS = (
    "GROUNDED ARCHITECT EVIDENCE",
    "VALIDATED CAPABILITY PROFILE",
)
_GROUND_NAMES = {"GND", "AGND", "DGND", "PGND", "VSS"}
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_CONTINUOUS_ZONE_MIN_COVERAGE = 0.80


class RequirementInvariants(ContractModel):
    """Versioned physical constraints extracted from the user's own text."""

    schema_version: Literal["ratsnestpro.release-invariants.v1"] = (
        "ratsnestpro.release-invariants.v1"
    )
    source_digest: str = Field(min_length=64, max_length=64)
    copper_layer_count: int | None = Field(default=None, ge=1, le=16)
    max_board_width_mm: float | None = Field(default=None, gt=0)
    max_board_height_mm: float | None = Field(default=None, gt=0)
    ground_plane_required: bool = False
    ground_plane_layer: str = ""
    continuous_ground_required: bool = False
    minimum_track_width_mm: float | None = Field(default=None, gt=0)
    minimum_track_width_nets: list[str] = Field(default_factory=list)
    decoupling_max_distance_mm: float | None = Field(default=None, gt=0)
    mounting_hole_count: int | None = Field(default=None, ge=0, le=100)
    mounting_holes_non_plated: bool = False


class InvariantFinding(ContractModel):
    """One independently observed mismatch between request and final PCB."""

    invariant_id: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2_000)
    affected_refs: list[str] = Field(default_factory=list, max_length=100)


class ReleaseIdentity(ContractModel):
    """Content identity shared by manufacturing, resume, and review."""

    schema_version: Literal[1] = 1
    project_name: str = Field(min_length=1, max_length=160)
    requirement_source_digest: str = Field(pattern=_DIGEST_PATTERN)
    pcb_relpath: str = Field(min_length=1, max_length=255)
    pcb_sha256: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _safe_board_name(self) -> ReleaseIdentity:
        candidate = Path(self.pcb_relpath)
        if candidate.is_absolute() or candidate.name != self.pcb_relpath:
            raise ValueError("pcb_relpath must be one local filename")
        return self


class ReleaseInvariantManifest(ContractModel):
    """Strict receipt binding one requirement audit to one physical PCB."""

    schema_version: Literal["ratsnestpro.release-invariants.v2"] = (
        "ratsnestpro.release-invariants.v2"
    )
    source: Literal["pipeline.requirement_text+final_kicad_pcb"] = (
        "pipeline.requirement_text+final_kicad_pcb"
    )
    release_identity: ReleaseIdentity
    requirement_release_ready: bool
    requirement_release_blockers: list[str] = Field(default_factory=list)
    invariants: RequirementInvariants
    findings: list[InvariantFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _internally_consistent(self) -> ReleaseInvariantManifest:
        if (
            self.release_identity.requirement_source_digest
            != self.invariants.source_digest
        ):
            raise ValueError("release identity does not match invariant source digest")
        finding_messages = {
            f"{finding.invariant_id}: {finding.message}"
            for finding in self.findings
        }
        if not finding_messages.issubset(set(self.requirement_release_blockers)):
            raise ValueError("manifest blockers do not cover every invariant finding")
        expected_ready = not self.requirement_release_blockers and not self.findings
        if self.requirement_release_ready != expected_ready:
            raise ValueError("manifest release-ready value contradicts its evidence")
        return self


def sha256_file(path: Path) -> str:
    """Hash a file without loading a potentially large PCB into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_requirement(requirement: str) -> str:
    source = requirement
    for marker in _EVIDENCE_MARKERS:
        source = source.partition(marker)[0]
    return source.strip()


def _requirement_without_decisions(source: str) -> str:
    """Return user-authored constraints without generated HITL patches."""

    return "\n".join(
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("DECISION:")
    ).strip()


def _decision_source(source: str, slot: str) -> str:
    prefix = f"DECISION: {slot}="
    return "\n".join(
        line.strip()
        for line in source.splitlines()
        if line.lstrip().startswith(prefix)
    )


def _explicit_layer_count(text: str) -> int | None:
    lower = text.lower()
    mentions: list[tuple[int, int]] = []
    for match in re.finditer(
        r"\b(1[0-6]|[2-9])\s*(?:copper\s+)?layers?\b",
        lower,
    ):
        mentions.append((match.start(), int(match.group(1))))
    for word, count in {"two": 2, "four": 4, "six": 6, "eight": 8}.items():
        for match in re.finditer(
            rf"\b{word}\s+(?:copper\s+)?layers?\b",
            lower,
        ):
            mentions.append((match.start(), count))
    for match in re.finditer(r"(?<!\d)(1[0-6]|[2-9])\s*层(?:板|铜)?", lower):
        mentions.append((match.start(), int(match.group(1))))
    for word, count in {
        "二层": 2,
        "两层": 2,
        "双层": 2,
        "四层": 4,
        "六层": 6,
        "八层": 8,
        "十层": 10,
        "十二层": 12,
        "十六层": 16,
    }.items():
        for match in re.finditer(rf"{word}(?:板|铜)?", lower):
            mentions.append((match.start(), count))
    return max(mentions)[1] if mentions else None


def _board_limits(text: str) -> tuple[float | None, float | None]:
    candidates: list[tuple[int, float, float]] = []
    dimension = re.compile(
        r"(?P<w>\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×*]\s*"
        r"(?P<h>\d+(?:\.\d+)?)\s*mm\b",
        re.IGNORECASE,
    )
    lower = text.lower()
    for match in dimension.finditer(text):
        context = lower[max(0, match.start() - 80):match.end() + 40]
        if not any(
            token in context
            for token in (
                "board",
                "outline",
                "dimension",
                "size",
                "板框",
                "板尺寸",
                "尺寸",
                "不超过",
                "不得超过",
                "至多",
                "≤",
                "maximum",
                "max ",
            )
        ):
            continue
        candidates.append(
            (match.start(), float(match.group("w")), float(match.group("h")))
        )
    if not candidates:
        return None, None
    _, width, height = max(candidates)
    return width, height


def _ground_plane_contract(text: str) -> tuple[bool, str, bool]:
    lower = text.lower()
    ground = r"(?:gnd|ground|接地|地层|地平面|铺地)"
    plane = r"(?:plane|pour|copper|zone|铺铜|覆铜|敷铜|铺设|铺地|铜皮|平面)"
    required = bool(
        re.search(rf"{ground}[^\n.;。；]{{0,40}}{plane}", lower)
        or re.search(rf"{plane}[^\n.;。；]{{0,40}}{ground}", lower)
        or "铺地" in lower
    )
    if not required:
        return False, "", False
    contexts = [
        lower[max(0, match.start() - 50):match.end() + 50]
        for match in re.finditer(ground, lower)
    ]
    joined = " ".join(contexts)
    if any(token in joined for token in ("bottom", "b.cu", "底层", "背面")):
        layer = "B.Cu"
    elif any(token in joined for token in ("top", "f.cu", "顶层", "正面")):
        layer = "F.Cu"
    else:
        layer = ""
    continuous = any(
        token in joined
        for token in (
            "continuous",
            "unbroken",
            "solid",
            "完整",
            "连续",
            "不分割",
        )
    )
    return True, layer, continuous


def _minimum_track_width(text: str) -> tuple[float | None, list[str]]:
    label = r"(?:track|trace|line|trunk|主干|走线|线宽)"
    minimum = (
        r"(?:minimum|min\.?|at\s+least|not\s+less\s+than|"
        r">=|≥|最小|不小于|不得小于|至少)"
    )
    value = r"(?P<width>\d+(?:\.\d+)?)\s*mm\b"
    patterns = (
        rf"{label}[^\n.;。；]{{0,36}}{minimum}[^\n.;。；]{{0,20}}{value}",
        rf"{minimum}[^\n.;。；]{{0,20}}{value}[^\n.;。；]{{0,36}}{label}",
        rf"{label}[^\n.;。；]{{0,36}}{value}[^\n.;。；]{{0,20}}{minimum}",
    )
    matches = [
        match
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    if not matches:
        return None, []
    strictest = max(matches, key=lambda item: float(item.group("width")))
    width = float(strictest.group("width"))
    context = text[max(0, strictest.start() - 60):strictest.end() + 20].upper()
    nets = []
    for match in re.finditer(
        r"(?<![A-Z0-9])(?:\+?\d+(?:\.\d+)?V|GND|AGND|DGND|PGND|VCC|VDD)"
        r"(?![A-Z0-9])",
        context,
    ):
        net = match.group(0).lstrip("+")
        if net not in nets:
            nets.append(net)
    return width, nets


def _decoupling_distance(text: str) -> float | None:
    distance = r"(?:distance|within|away|距离|间距|靠近|不超过)"
    decoupling = r"(?:decoupl\w*|bypass|去耦|旁路)"
    limit = (
        r"(?:not\s+more\s+than|no\s+more\s+than|within|<=|≤|"
        r"不超过|不得超过|小于等于|以内)?"
    )
    value = r"(?P<distance>\d+(?:\.\d+)?)\s*mm\b"
    patterns = (
        rf"{decoupling}[^\n.;。；]{{0,90}}{distance}[^\n.;。；]{{0,30}}{limit}"
        rf"[^\n.;。；]{{0,10}}{value}",
        rf"{decoupling}[^\n.;。；]{{0,90}}{limit}[^\n.;。；]{{0,20}}{value}",
    )
    values = [
        float(match.group("distance"))
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    return min(values) if values else None


def _mounting_holes(text: str) -> tuple[int | None, bool]:
    lower = text.lower()
    non_plated = any(
        token in lower
        for token in ("non-plated", "non plated", "npth", "非金属化", "非电镀")
    )
    if re.search(r"(?:four|4|四)\s*(?:个)?\s*角", lower) and re.search(
        r"(?:mounting\s+holes?|安装孔|固定孔)", lower
    ):
        return 4, non_plated
    english_numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    english = list(re.finditer(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|[1-9]\d*)"
        r"\b[^\n.;。；]{0,48}\bmounting\s+holes?\b",
        lower,
    ))
    if english:
        raw_count = english[-1].group(1)
        count = (
            english_numbers[raw_count]
            if raw_count in english_numbers
            else int(raw_count)
        )
        return count, non_plated
    arabic = re.search(
        r"(?<![a-z0-9])([1-9]\d*)\s+(?:non[- ]plated\s+)?"
        r"mounting\s+holes?\b",
        lower,
    )
    if arabic:
        return int(arabic.group(1)), non_plated
    chinese = re.search(
        r"([一二两三四五六七八九十])\s*个?\s*(?:m\d\s*)?(?:非金属化)?安装孔",
        lower,
    )
    if chinese:
        values = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        return values[chinese.group(1)], non_plated
    return None, non_plated


def extract_requirement_invariants(requirement: str) -> RequirementInvariants:
    """Compile only explicit, mechanically verifiable user constraints."""

    source = _source_requirement(requirement)
    original = _requirement_without_decisions(source)
    width, height = _board_limits(original)
    if width is None or height is None:
        width, height = _board_limits(_decision_source(source, "board_outline"))
    layer_count = _explicit_layer_count(original)
    if layer_count is None:
        layer_count = _explicit_layer_count(
            _decision_source(source, "layer_count")
        )
    plane_required, plane_layer, continuous = _ground_plane_contract(original)
    track_width, track_nets = _minimum_track_width(original)
    hole_count, non_plated = _mounting_holes(original)
    if hole_count is None:
        hole_count, non_plated = _mounting_holes(
            _decision_source(source, "mounting")
        )
    return RequirementInvariants(
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        copper_layer_count=layer_count,
        max_board_width_mm=width,
        max_board_height_mm=height,
        ground_plane_required=plane_required,
        ground_plane_layer=plane_layer,
        continuous_ground_required=continuous,
        minimum_track_width_mm=track_width,
        minimum_track_width_nets=track_nets,
        decoupling_max_distance_mm=_decoupling_distance(original),
        mounting_hole_count=hole_count,
        mounting_holes_non_plated=non_plated,
    )


def is_mounting_hole_part(part: Any) -> bool:
    text = " ".join(
        str(getattr(part, field, ""))
        for field in ("role", "symbol", "footprint", "value")
    ).lower()
    return "mounting" in text and "hole" in text


def _is_decoupling_part(part: Any) -> bool:
    role = str(getattr(part, "role", "")).lower()
    return "decoupl" in role or (
        "capacitor" in role and any(token in role for token in ("vcc", "vdd", "supply"))
    )


def _is_ground_net(value: object) -> bool:
    name = str(value or "").strip().upper()
    return (
        name in _GROUND_NAMES
        or name.endswith("_GND")
        or name.startswith("GND_")
    )


def _is_power_net(value: object) -> bool:
    name = str(value or "").strip().upper().lstrip("+")
    return bool(
        name in {"VCC", "VDD", "VBAT", "VIN", "VBUS"}
        or re.fullmatch(r"\d+(?:\.\d+)?V", name)
        or re.fullmatch(r"\d+V\d+", name)
    )


def _polygon_area(points: object) -> float | None:
    """Return a finite polygon area, or ``None`` when geometry is unprovable."""

    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        vertices = [
            (float(point[0]), float(point[1]))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
    except (TypeError, ValueError, OverflowError):
        return None
    if len(vertices) != len(points) or not all(
        math.isfinite(value) for point in vertices for value in point
    ):
        return None
    area = abs(sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(
            vertices,
            vertices[1:] + vertices[:1],
            strict=True,
        )
    )) / 2.0
    return area if area > 1e-9 else None


def _continuous_ground_finding(
    matching: list[dict[str, Any]],
    extents: dict[str, Any] | None,
) -> InvariantFinding | None:
    """Conservatively prove one filled, board-spanning, non-island plane.

    A zone declaration alone is not proof of continuous copper: KiCad can leave
    it unfilled or split it into islands around keepouts.  The receipt therefore
    accepts only one source zone with one filled polygon, each covering at least
    80 percent of the enclosing board/source geometry.  Anything less remains a
    manual-review blocker instead of becoming a false release.
    """

    reason = ""
    if len(matching) != 1:
        reason = f"expected one authoritative GND zone, found {len(matching)}"
    elif not isinstance(extents, dict):
        reason = "board outline extents are unavailable"
    else:
        board_width = float(extents.get("width", 0.0) or 0.0)
        board_height = float(extents.get("height", 0.0) or 0.0)
        board_area = board_width * board_height
        zone = matching[0]
        source_area = _polygon_area(zone.get("points"))
        filled = zone.get("filled_polygons")
        if board_area <= 1e-9:
            reason = "board outline has no positive area"
        elif source_area is None:
            reason = "GND zone source polygon is unreadable"
        elif source_area / board_area < _CONTINUOUS_ZONE_MIN_COVERAGE:
            reason = (
                "GND zone source polygon covers only "
                f"{source_area / board_area:.1%} of the board"
            )
        elif not isinstance(filled, list) or len(filled) != 1:
            count = len(filled) if isinstance(filled, list) else 0
            reason = (
                "filled GND copper is unavailable or split into islands "
                f"(filled polygon count={count})"
            )
        else:
            filled_area = _polygon_area(filled[0].get("points"))
            if filled_area is None:
                reason = "filled GND polygon geometry is unreadable"
            elif bool(filled[0].get("island")):
                reason = "the only filled GND polygon is marked as an island"
            elif filled_area / source_area < _CONTINUOUS_ZONE_MIN_COVERAGE:
                reason = (
                    "filled GND copper covers only "
                    f"{filled_area / source_area:.1%} of its source zone"
                )
    if not reason:
        return None
    return InvariantFinding(
        invariant_id="continuous_ground_geometry",
        message=f"continuous GND plane cannot be proven: {reason}",
    )


def audit_pcb_invariants(
    invariants: RequirementInvariants,
    board: Any,
    parts: list[Any],
) -> list[InvariantFinding]:
    """Re-read a final board and prove every compiled physical invariant."""

    findings: list[InvariantFinding] = []
    board_info = board.get_board_info()
    if (
        invariants.copper_layer_count is not None
        and board_info.get("copper_layers") != invariants.copper_layer_count
    ):
        findings.append(InvariantFinding(
            invariant_id="copper_layer_count",
            message=(
                f"final PCB has {board_info.get('copper_layers')} copper layers; "
                f"requirement is exactly {invariants.copper_layer_count}"
            ),
        ))

    extents = board.get_board_extents()
    if extents is None and (
        invariants.max_board_width_mm is not None
        or invariants.max_board_height_mm is not None
    ):
        findings.append(InvariantFinding(
            invariant_id="board_outline_available",
            message="final PCB has no readable closed board outline",
        ))
    if extents is not None:
        if (
            invariants.max_board_width_mm is not None
            and float(extents["width"]) > invariants.max_board_width_mm + 1e-6
        ):
            findings.append(InvariantFinding(
                invariant_id="board_max_width",
                message=(
                    f"final PCB width {float(extents['width']):.3f} mm exceeds "
                    f"{invariants.max_board_width_mm:.3f} mm"
                ),
            ))
        if (
            invariants.max_board_height_mm is not None
            and float(extents["height"]) > invariants.max_board_height_mm + 1e-6
        ):
            findings.append(InvariantFinding(
                invariant_id="board_max_height",
                message=(
                    f"final PCB height {float(extents['height']):.3f} mm exceeds "
                    f"{invariants.max_board_height_mm:.3f} mm"
                ),
            ))

    if invariants.ground_plane_required:
        zones = board.list_zones()
        matching = [
            zone
            for zone in zones
            if _is_ground_net(zone.get("net"))
            and (
                not invariants.ground_plane_layer
                or str(zone.get("layer", "")) == invariants.ground_plane_layer
            )
        ]
        if not matching:
            layer = invariants.ground_plane_layer or "a copper layer"
            findings.append(InvariantFinding(
                invariant_id="ground_plane_materialized",
                message=f"final PCB has no physical GND zone on {layer}",
            ))
        elif invariants.continuous_ground_required:
            continuous_finding = _continuous_ground_finding(matching, extents)
            if continuous_finding is not None:
                findings.append(continuous_finding)

    mounting_parts = [part for part in parts if is_mounting_hole_part(part)]
    if invariants.mounting_hole_count is not None:
        if len(mounting_parts) != invariants.mounting_hole_count:
            findings.append(InvariantFinding(
                invariant_id="mounting_hole_count",
                message=(
                    f"selection/final contract has {len(mounting_parts)} mounting "
                    f"holes; requirement is {invariants.mounting_hole_count}"
                ),
                affected_refs=[str(getattr(part, "ref", "")) for part in mounting_parts],
            ))
        final_refs = {
            str(item.get("reference", ""))
            for item in board.list_footprints()
        }
        missing = sorted(
            str(getattr(part, "ref", ""))
            for part in mounting_parts
            if str(getattr(part, "ref", "")) not in final_refs
        )
        if missing:
            findings.append(InvariantFinding(
                invariant_id="mounting_holes_materialized",
                message=f"mounting-hole footprints missing from final PCB: {missing}",
                affected_refs=missing,
            ))
        if invariants.mounting_holes_non_plated:
            plated: list[str] = []
            for part in mounting_parts:
                ref = str(getattr(part, "ref", ""))
                try:
                    pads = board.footprint_pads(ref)
                except Exception:
                    plated.append(f"{ref} (pad geometry unreadable)")
                    continue
                if not any(str(pad.get("type", "")) == "np_thru_hole" for pad in pads):
                    plated.append(ref)
            if plated:
                findings.append(InvariantFinding(
                    invariant_id="mounting_holes_non_plated",
                    message=f"mounting holes are not proven NPTH: {plated}",
                    affected_refs=plated,
                ))

    if invariants.minimum_track_width_mm is not None:
        wanted_nets = {
            name.upper().lstrip("+")
            for name in invariants.minimum_track_width_nets
        }
        thin: list[str] = []
        for track in board.list_tracks():
            net = str(track.get("net_name", "")).upper().lstrip("+")
            if wanted_nets and net not in wanted_nets:
                continue
            if not wanted_nets and not (_is_power_net(net) or _is_ground_net(net)):
                continue
            width = track.get("width")
            if width is not None and float(width) + 1e-9 < invariants.minimum_track_width_mm:
                thin.append(f"{net or '<unnamed>'}:{float(width):.3f}mm")
        if thin:
            findings.append(InvariantFinding(
                invariant_id="minimum_track_width",
                message=(
                    f"tracks below {invariants.minimum_track_width_mm:.3f} mm: "
                    f"{sorted(set(thin))}"
                ),
            ))

    if invariants.decoupling_max_distance_mm is not None:
        anchors = [
            part
            for part in parts
            if str(getattr(part, "ref", "")).upper().startswith("U")
            and not _is_decoupling_part(part)
        ]
        far: list[str] = []
        decoupling_parts = [part for part in parts if _is_decoupling_part(part)]
        if not decoupling_parts:
            findings.append(InvariantFinding(
                invariant_id="decoupling_parts_present",
                message=(
                    "an explicit decoupling-distance requirement exists, but "
                    "no selected part is classified as power decoupling"
                ),
            ))
        for part in decoupling_parts:
            ref = str(getattr(part, "ref", ""))
            try:
                cap_pads = board.footprint_pads(ref)
            except Exception:
                continue
            power_pads = [pad for pad in cap_pads if _is_power_net(pad.get("net"))]
            if not power_pads:
                continue
            candidates: list[tuple[float, str]] = []
            for anchor in anchors:
                anchor_ref = str(getattr(anchor, "ref", ""))
                try:
                    anchor_pads = board.footprint_pads(anchor_ref)
                except Exception:
                    continue
                for cap_pad in power_pads:
                    for anchor_pad in anchor_pads:
                        if cap_pad.get("net") != anchor_pad.get("net"):
                            continue
                        candidates.append((
                            math.hypot(
                                float(cap_pad["x"]) - float(anchor_pad["x"]),
                                float(cap_pad["y"]) - float(anchor_pad["y"]),
                            ),
                            anchor_ref,
                        ))
            if not candidates:
                far.append(f"{ref}-><no power-pin anchor>")
                continue
            distance, anchor_ref = min(candidates)
            if distance > invariants.decoupling_max_distance_mm + 1e-9:
                far.append(f"{ref}->{anchor_ref} ({distance:.2f}mm)")
        if far:
            findings.append(InvariantFinding(
                invariant_id="decoupling_distance",
                message=(
                    "power decoupling exceeds explicit limit "
                    f"{invariants.decoupling_max_distance_mm:.3f} mm: {far}"
                ),
                affected_refs=[item.partition("->")[0] for item in far],
            ))
    return findings


def build_release_invariant_manifest(
    *,
    project_name: str,
    requirement: str,
    pcb_path: Path,
    findings: list[InvariantFinding],
    blockers: list[str],
    pcb_sha256: str | None = None,
) -> ReleaseInvariantManifest:
    """Build a validated receipt for the exact bytes audited by Manufacture."""

    invariants = extract_requirement_invariants(requirement)
    return ReleaseInvariantManifest(
        release_identity=ReleaseIdentity(
            project_name=project_name,
            requirement_source_digest=invariants.source_digest,
            pcb_relpath=pcb_path.name,
            pcb_sha256=pcb_sha256 or sha256_file(pcb_path),
        ),
        requirement_release_ready=not blockers,
        requirement_release_blockers=list(dict.fromkeys(blockers)),
        invariants=invariants,
        findings=findings,
    )


def validate_release_invariant_manifest(
    manifest_path: Path,
    *,
    project_name: str,
    requirement: str,
    pcb_path: Path,
    parts: list[Any],
) -> ReleaseInvariantManifest:
    """Validate and independently re-audit one receipt against current bytes."""

    manifest = ReleaseInvariantManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    identity = manifest.release_identity
    expected_invariants = extract_requirement_invariants(requirement)
    if identity.project_name != project_name:
        raise ValueError(
            "release-invariant project mismatch: "
            f"{identity.project_name!r} != {project_name!r}"
        )
    if identity.requirement_source_digest != expected_invariants.source_digest:
        raise ValueError("release-invariant requirement source digest is stale")
    if manifest.invariants != expected_invariants:
        raise ValueError("release-invariant requirement contract is stale")
    expected_board = pcb_path.resolve()
    receipt_board = (manifest_path.parent / identity.pcb_relpath).resolve()
    if receipt_board.parent != manifest_path.parent.resolve():
        raise ValueError("release-invariant PCB path escapes its project directory")
    if receipt_board != expected_board or identity.pcb_relpath != pcb_path.name:
        raise ValueError("release-invariant receipt references a different PCB")
    current_sha256 = sha256_file(expected_board)
    if identity.pcb_sha256 != current_sha256:
        raise ValueError("release-invariant PCB SHA-256 is stale")

    from ratsnestpro.eda.vendor.pcb import PcbBoard

    current_findings = audit_pcb_invariants(
        expected_invariants,
        PcbBoard.load(expected_board),
        parts,
    )
    if current_findings != manifest.findings:
        raise ValueError(
            "release-invariant findings do not match a current PCB re-audit"
        )
    return manifest
