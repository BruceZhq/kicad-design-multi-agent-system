"""Governed dispositions for remaining KiCad warnings.

The contract is intentionally fail-closed.  A warning is cleared only when
the current artifact/report pair has deterministic project-local binding
evidence, is covered by an explicit waiver, or has identical normalized
manufacturing geometry. Narrative review text is never evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ratsnestpro.eda.vendor.footprint import load_footprint_node, resolve_footprint
from ratsnestpro.eda.vendor.sexpr import Atom, dumps, find_all, find_first, loads, tag_of

WAIVER_SCHEMA_VERSION = "ratsnestpro.warning-waiver.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MISSING_SYMBOL_LIBRARY_RE = re.compile(
    r"does not include the symbol library ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

WARNING_POLICIES: dict[str, tuple[str, str, str]] = {
    "lib_symbol_issues": (
        "library_provenance",
        "repair_required",
        "KiCad could not verify the placed symbol against its configured library.",
    ),
    "lib_symbol_mismatch": (
        "library_provenance",
        "normalized_structure_evidence_required",
        "The embedded schematic symbol differs from KiCad's library cache comparison.",
    ),
    "endpoint_off_grid": (
        "connectivity_integrity",
        "repair_required",
        "A pin or connection endpoint is outside KiCad's electrical grid.",
    ),
    "lib_footprint_mismatch": (
        "library_provenance",
        "normalized_structure_evidence_required",
        "The board instance differs from the selected installed footprint definition.",
    ),
    "silk_over_copper": (
        "assembly_readability",
        "repair_or_explicit_waiver_required",
        "Silkscreen is clipped by solder mask and may impair assembly identification.",
    ),
    "silk_edge_clearance": (
        "assembly_readability",
        "repair_or_explicit_waiver_required",
        "Silkscreen is clipped by the board edge and may be absent after routing.",
    ),
    "silk_overlap": (
        "assembly_readability",
        "repair_or_explicit_waiver_required",
        "Silkscreen objects overlap and require a visible review disposition.",
    ),
}

_WAIVERABLE_CATEGORIES = frozenset({"assembly_readability"})
_NON_WAIVERABLE_CATEGORIES = frozenset({
    "connectivity_integrity",
    "electrical_integrity",
    "structural_integrity",
    "library_provenance",
})
_PAD_FIELDS = frozenset({
    "at",
    "size",
    "drill",
    "layers",
    "rect_delta",
    "roundrect_rratio",
    "chamfer_ratio",
    "chamfer",
    "options",
    "primitives",
    "thermal_bridge_angle",
    "thermal_bridge_width",
    "thermal_gap",
    "clearance",
    "solder_mask_margin",
    "solder_paste_margin",
    "solder_paste_margin_ratio",
    "zone_connect",
})
_FOOTPRINT_FIELDS = frozenset({
    "attr",
    "clearance",
    "solder_mask_margin",
    "solder_paste_margin",
    "solder_paste_ratio",
    "zone_connect",
})
_FUNCTIONAL_GRAPHICS = frozenset({
    "fp_line",
    "fp_rect",
    "fp_circle",
    "fp_arc",
    "fp_poly",
    "fp_curve",
    "zone",
})
_FUNCTIONAL_LAYERS = frozenset({
    "F.Cu",
    "B.Cu",
    "F.Mask",
    "B.Mask",
    "F.Paste",
    "B.Paste",
    "Edge.Cuts",
    "F.CrtYd",
    "B.CrtYd",
})
_DYNAMIC_TAGS = frozenset({"uuid", "tstamp", "net", "pinfunction", "pintype"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_warnings(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify every warning while retaining an unresolved default."""

    counts: dict[str, int] = {}
    for finding in findings:
        if str(finding.get("severity", "")).casefold() != "warning":
            continue
        rule_id = str(finding.get("type", "unknown"))
        counts[rule_id] = counts.get(rule_id, 0) + 1
    classified: dict[str, Any] = {}
    for rule_id, count in counts.items():
        category, disposition, basis = WARNING_POLICIES.get(
            rule_id,
            (
                "unclassified",
                "explicit_review_required",
                "No governed automatic disposition exists for this KiCad warning type.",
            ),
        )
        classified[rule_id] = {
            "count": count,
            "category": category,
            "disposition": disposition,
            "basis": basis,
            "suppressed": False,
            "resolution": {
                "status": "blocked",
                "reason": "no governed disposition has been verified",
            },
        }
    return classified


def apply_warning_contract(
    classifications: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    pcb_path: Path | None = None,
    sch_path: Path | None = None,
    report_path: Path,
    waiver_path: Path | None = None,
) -> dict[str, Any]:
    """Attach verified, digest-bound resolutions to warning classifications."""

    resolved = {rule: dict(value) for rule, value in classifications.items()}
    if not report_path.is_file():
        return resolved
    pcb_sha256 = sha256_file(pcb_path) if pcb_path and pcb_path.is_file() else ""
    report_sha256 = sha256_file(report_path)
    selected_waiver_path = (
        waiver_path
        or (pcb_path.with_suffix(".warning-waivers.json") if pcb_path else None)
    )
    waivers, waiver_error = (
        _read_waivers(selected_waiver_path)
        if selected_waiver_path is not None
        else ([], None)
    )

    for rule_id, classification in resolved.items():
        classification = dict(classification)
        resolved[rule_id] = classification
        category = str(classification.get("category", "unclassified"))
        count = int(classification.get("count", 0))
        if rule_id == "lib_symbol_issues":
            evidence = _project_symbol_binding_evidence(
                sch_path,
                [
                    finding
                    for finding in findings
                    if str(finding.get("severity", "")).casefold() == "warning"
                    and str(finding.get("type", "")) == rule_id
                ],
                expected_count=count,
            )
            if evidence.get("equivalent") is True:
                classification["resolution"] = {
                    "status": "auto_equivalent",
                    "reason": (
                        "every reported symbol library is bound to a current "
                        "project-local source and embedded in the schematic"
                    ),
                    "schematic_sha256": sha256_file(sch_path),
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            else:
                classification["resolution"] = {
                    "status": "blocked",
                    "reason": "project-local symbol binding equivalence was not proven",
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            continue
        if rule_id == "lib_symbol_mismatch":
            evidence = _symbol_equivalence_evidence(
                sch_path,
                report_path,
                expected_count=count,
            )
            if evidence.get("equivalent") is True:
                classification["resolution"] = {
                    "status": "auto_equivalent",
                    "reason": (
                        "every reported embedded symbol is structurally identical "
                        "to its currently installed KiCad library definition"
                    ),
                    "schematic_sha256": sha256_file(sch_path),
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            else:
                classification["resolution"] = {
                    "status": "blocked",
                    "reason": "normalized symbol equivalence was not proven",
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            continue
        if rule_id == "lib_footprint_mismatch":
            if pcb_path is None or not pcb_path.is_file():
                classification["resolution"] = {
                    "status": "blocked",
                    "reason": "PCB evidence is unavailable",
                }
                continue
            evidence = _footprint_equivalence_evidence(
                pcb_path,
                [
                    finding
                    for finding in findings
                    if str(finding.get("severity", "")).casefold() == "warning"
                    and str(finding.get("type", "")) == rule_id
                ],
                expected_count=count,
            )
            if evidence.get("equivalent") is True:
                classification["resolution"] = {
                    "status": "auto_equivalent",
                    "reason": "normalized manufacturing structure matches the bound library",
                    "pcb_sha256": pcb_sha256,
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            else:
                classification["resolution"] = {
                    "status": "blocked",
                    "reason": "normalized footprint equivalence was not proven",
                    "pcb_sha256": pcb_sha256,
                    "report_sha256": report_sha256,
                    "evidence": evidence,
                }
            continue

        if category in _NON_WAIVERABLE_CATEGORIES:
            classification["resolution"] = {
                "status": "blocked",
                "reason": f"{category} warnings are non-waiverable",
            }
            continue
        if category not in _WAIVERABLE_CATEGORIES:
            classification["resolution"] = {
                "status": "blocked",
                "reason": "the warning category has no governed waiver policy",
            }
            continue

        matches = [
            waiver
            for waiver in waivers
            if _waiver_matches(
                waiver,
                rule_id=rule_id,
                count=count,
                pcb_sha256=pcb_sha256,
                report_sha256=report_sha256,
            )
        ]
        if len(matches) == 1:
            waiver = matches[0]
            classification["resolution"] = {
                "status": "waived",
                "reason": str(waiver["rationale"]),
                "approved_by": str(waiver["approved_by"]),
                "pcb_sha256": pcb_sha256,
                "report_sha256": report_sha256,
                "waiver_path": str(selected_waiver_path),
            }
        else:
            reason = waiver_error or (
                "no current digest/rule/count-bound waiver exists"
                if not matches
                else "multiple matching waivers make the disposition ambiguous"
            )
            classification["resolution"] = {
                "status": "blocked",
                "reason": reason,
                "pcb_sha256": pcb_sha256,
                "report_sha256": report_sha256,
                "waiver_path": str(selected_waiver_path),
            }
    return resolved


def _project_symbol_binding_evidence(
    sch_path: Path | None,
    findings: list[dict[str, Any]],
    *,
    expected_count: int,
) -> dict[str, Any]:
    """Prove that a headless KiCad lookup warning has project-local closure."""

    if sch_path is None or not sch_path.is_file():
        return {"equivalent": False, "reason": "schematic is unavailable"}
    table_path = sch_path.parent / "sym-lib-table"
    if not table_path.is_file():
        return {"equivalent": False, "reason": "sym-lib-table is unavailable"}
    if len(findings) != expected_count or not findings:
        return {
            "equivalent": False,
            "reason": "finding count does not match the warning classification",
        }
    try:
        schematic = sch_path.read_text(encoding="utf-8")
        table = table_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"equivalent": False, "reason": f"symbol evidence is unreadable: {exc}"}

    nicknames: set[str] = set()
    for finding in findings:
        match = _MISSING_SYMBOL_LIBRARY_RE.search(
            str(finding.get("description", ""))
        )
        if match is None:
            return {
                "equivalent": False,
                "reason": "warning does not identify one missing library nickname",
            }
        nicknames.add(match.group(1))

    libraries: list[dict[str, str]] = []
    for nickname in sorted(nicknames):
        library_path = (
            sch_path.parent
            / ".ratsnest-libs"
            / "symbols"
            / f"{nickname}.kicad_sym"
        )
        if (
            f'(name "{nickname}")' not in table
            or re.search(
                rf'\(lib_id\s+"{re.escape(nickname)}:',
                schematic,
            ) is None
            or re.search(
                rf'\(symbol\s+"{re.escape(nickname)}:',
                schematic,
            ) is None
            or not library_path.is_file()
        ):
            return {
                "equivalent": False,
                "reason": f"project-local symbol binding is incomplete: {nickname}",
            }
        libraries.append({
            "nickname": nickname,
            "source_path": str(library_path),
            "source_sha256": sha256_file(library_path),
        })
    return {
        "equivalent": True,
        "normalization_version": "ratsnestpro.project-symbol-binding.v1",
        "sym_lib_table_sha256": sha256_file(table_path),
        "libraries": libraries,
    }


_SYMBOL_ITEM_RE = re.compile(r"\bSymbol\s+([A-Za-z]+\d+)\s+\[[^\]]+\]")


def _symbol_equivalence_evidence(
    sch_path: Path | None,
    report_path: Path,
    *,
    expected_count: int,
) -> dict[str, Any]:
    """Prove exact embedded/current-library equality for mismatch warnings."""

    if sch_path is None or not sch_path.is_file():
        return {"equivalent": False, "reason": "schematic is unavailable"}
    try:
        schematic_root = loads(sch_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"equivalent": False, "reason": f"symbol evidence is unreadable: {exc}"}
    if tag_of(schematic_root) != "kicad_sch" or not isinstance(report, dict):
        return {"equivalent": False, "reason": "symbol evidence has an invalid root"}

    findings = [
        finding
        for sheet in report.get("sheets", [])
        if isinstance(sheet, dict)
        for finding in sheet.get("violations", [])
        if isinstance(finding, dict)
        and str(finding.get("severity", "")).casefold() == "warning"
        and str(finding.get("type", "")) == "lib_symbol_mismatch"
    ]
    if len(findings) != expected_count or not findings:
        return {
            "equivalent": False,
            "reason": "finding count does not match the warning classification",
        }

    lib_symbols = find_first(schematic_root, "lib_symbols")
    if not isinstance(lib_symbols, list):
        return {"equivalent": False, "reason": "embedded lib_symbols are unavailable"}
    instances = [
        child
        for child in schematic_root
        if isinstance(child, list) and tag_of(child) == "symbol"
    ]
    embedded = {
        str(child[1]): child
        for child in lib_symbols
        if isinstance(child, list) and tag_of(child) == "symbol" and len(child) > 1
    }

    from ratsnestpro.eda.symbols import symbol_definition

    comparisons: list[dict[str, str]] = []
    for finding in findings:
        references = {
            match.group(1)
            for item in finding.get("items", [])
            if isinstance(item, dict)
            if (
                match := _SYMBOL_ITEM_RE.search(str(item.get("description", "")))
            ) is not None
        }
        if len(references) != 1:
            return {
                "equivalent": False,
                "reason": "each mismatch finding must identify exactly one symbol instance",
            }
        reference = next(iter(references))
        instance = next(
            (
                node
                for node in instances
                if _symbol_property(node, "Reference") == reference
            ),
            None,
        )
        lib_id_node = find_first(instance, "lib_id") if instance is not None else None
        lib_id = (
            str(lib_id_node[1])
            if lib_id_node is not None and len(lib_id_node) > 1
            else ""
        )
        embedded_node = embedded.get(lib_id)
        library_node = symbol_definition(lib_id) if lib_id else None
        if embedded_node is None or library_node is None:
            return {
                "equivalent": False,
                "reason": f"embedded or installed symbol definition is unavailable: {lib_id}",
            }
        embedded_digest = hashlib.sha256(dumps(embedded_node).encode("utf-8")).hexdigest()
        library_digest = hashlib.sha256(dumps(library_node).encode("utf-8")).hexdigest()
        comparisons.append(
            {
                "ref": reference,
                "lib_id": lib_id,
                "embedded_structure_sha256": embedded_digest,
                "library_structure_sha256": library_digest,
            }
        )
        if embedded_digest != library_digest:
            return {
                "equivalent": False,
                "reason": f"embedded symbol structure differs for {reference}",
                "symbols": comparisons,
            }
    return {
        "equivalent": True,
        "normalization_version": "ratsnestpro.symbol-structure.v1",
        "symbols": comparisons,
    }


def _symbol_property(node: list[Any], name: str) -> str:
    for child in node:
        if (
            isinstance(child, list)
            and tag_of(child) == "property"
            and len(child) > 2
            and str(child[1]) == name
        ):
            return str(child[2])
    return ""


def _read_waivers(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.is_file():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"warning-waiver contract is unreadable: {exc}"
    if not isinstance(payload, dict) or payload.get("schema_version") != WAIVER_SCHEMA_VERSION:
        return [], "warning-waiver contract has an unsupported schema"
    waivers = payload.get("waivers")
    if not isinstance(waivers, list) or not all(isinstance(item, dict) for item in waivers):
        return [], "warning-waiver contract waivers must be an object list"
    return waivers, None


def _waiver_matches(
    waiver: dict[str, Any],
    *,
    rule_id: str,
    count: int,
    pcb_sha256: str,
    report_sha256: str,
) -> bool:
    waiver_pcb = str(waiver.get("pcb_sha256", "")).casefold()
    waiver_report = str(waiver.get("report_sha256", "")).casefold()
    return (
        waiver.get("approved") is True
        and str(waiver.get("approved_by", "")).strip() != ""
        and str(waiver.get("rationale", "")).strip() != ""
        and str(waiver.get("rule_id", "")) == rule_id
        and type(waiver.get("count")) is int
        and waiver.get("count") == count
        and _SHA256_RE.fullmatch(waiver_pcb) is not None
        and _SHA256_RE.fullmatch(waiver_report) is not None
        and waiver_pcb == pcb_sha256
        and waiver_report == report_sha256
    )


def _footprint_equivalence_evidence(
    pcb_path: Path,
    findings: list[dict[str, Any]],
    *,
    expected_count: int,
) -> dict[str, Any]:
    try:
        root = loads(pcb_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"equivalent": False, "reason": f"PCB could not be parsed: {exc}"}
    if tag_of(root) != "kicad_pcb":
        return {"equivalent": False, "reason": "PCB root is not kicad_pcb"}

    footprints = find_all(root, "footprint")
    identities = [_footprint_identity(node) for node in footprints]
    targets: list[int] = []
    for finding in findings:
        matches = _finding_target_indexes(finding, identities)
        if len(matches) != 1:
            return {
                "equivalent": False,
                "reason": "each lib_footprint_mismatch finding must identify one footprint",
            }
        targets.append(matches[0])
    if len(targets) != expected_count or not targets:
        return {
            "equivalent": False,
            "reason": "finding count does not match the warning classification",
        }

    compared: list[dict[str, Any]] = []
    for index in sorted(set(targets)):
        board_node = footprints[index]
        identity = identities[index]
        lib_id = identity["lib_id"]
        library_path = _resolve_project_footprint(pcb_path.parent, lib_id)
        if library_path is None:
            return {
                "equivalent": False,
                "reason": f"bound footprint library cannot be resolved: {lib_id}",
            }
        try:
            library_node = load_footprint_node(library_path)
            board_signature, nets_consistent = _footprint_signature(
                board_node,
                board_instance=True,
            )
            library_signature, _ = _footprint_signature(
                library_node,
                board_instance=False,
            )
        except (OSError, ValueError) as exc:
            return {
                "equivalent": False,
                "reason": f"footprint evidence could not be normalized: {exc}",
            }
        board_digest = _json_digest(board_signature)
        library_digest = _json_digest(library_signature)
        comparison = {
            "ref": identity["ref"],
            "lib_id": lib_id,
            "board_structure_sha256": board_digest,
            "library_structure_sha256": library_digest,
            "net_assignment_consistent": nets_consistent,
            "compared_fields": [
                "pad_number_type_shape",
                "padstack_geometry",
                "copper_mask_paste_geometry",
                "courtyard_and_edge_geometry",
                "functional_footprint_attributes",
                "per_pad_net_consistency",
            ],
        }
        compared.append(comparison)
        if board_digest != library_digest or not nets_consistent:
            return {
                "equivalent": False,
                "reason": f"normalized manufacturing structure differs for {identity['ref']}",
                "footprints": compared,
            }
    return {
        "equivalent": True,
        "normalization_version": "ratsnestpro.footprint-structure.v1",
        "footprints": compared,
    }


def _footprint_identity(node: list[Any]) -> dict[str, str]:
    properties = {
        str(child[1]): str(child[2])
        for child in node
        if isinstance(child, list) and tag_of(child) == "property" and len(child) > 2
    }
    uuid_node = find_first(node, "uuid") or find_first(node, "tstamp")
    return {
        "lib_id": str(node[1]) if len(node) > 1 else "",
        "ref": properties.get("Reference", ""),
        "uuid": str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else "",
    }


def _finding_target_indexes(
    finding: dict[str, Any],
    identities: list[dict[str, str]],
) -> list[int]:
    uuids: set[str] = set()
    texts: list[str] = [str(finding.get("description", ""))]
    items = finding.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("uuid"):
                uuids.add(str(item["uuid"]))
            texts.append(str(item.get("description", "")))
    uuid_matches = [
        index
        for index, identity in enumerate(identities)
        if identity["uuid"] and identity["uuid"] in uuids
    ]
    if uuid_matches:
        return sorted(set(uuid_matches))
    joined = " ".join(texts)
    ref_matches = [
        index
        for index, identity in enumerate(identities)
        if identity["ref"]
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(identity['ref'])}(?![A-Za-z0-9_])", joined)
    ]
    return sorted(set(ref_matches))


def _resolve_project_footprint(project_dir: Path, lib_id: str) -> Path | None:
    if ":" not in lib_id:
        return None
    nickname, name = lib_id.split(":", 1)
    vendored = (
        project_dir
        / ".ratsnest-libs"
        / "footprints"
        / f"{nickname}.pretty"
        / f"{name}.kicad_mod"
    )
    if vendored.is_file():
        return vendored
    return resolve_footprint(lib_id)


def _footprint_signature(
    node: list[Any],
    *,
    board_instance: bool,
) -> tuple[dict[str, Any], bool]:
    parent_at = find_first(node, "at")
    parent_rotation = (
        _decimal(parent_at[3])
        if board_instance and parent_at is not None and len(parent_at) > 3
        else Decimal(0)
    )
    pads: list[Any] = []
    nets_by_pad: dict[str, set[tuple[str, ...]]] = {}
    for pad in find_all(node, "pad"):
        pads.append(_pad_signature(pad, parent_rotation=parent_rotation))
        if board_instance:
            number = str(pad[1]) if len(pad) > 1 else ""
            net = find_first(pad, "net")
            net_value = tuple(str(value) for value in net[1:]) if net else ()
            nets_by_pad.setdefault(number, set()).add(net_value)
    functional_graphics = [
        _canonical(child)
        for child in node[1:]
        if isinstance(child, list)
        and tag_of(child) in _FUNCTIONAL_GRAPHICS
        and _node_uses_functional_layer(child)
    ]
    attributes = [
        _canonical(child)
        for child in node[1:]
        if isinstance(child, list) and tag_of(child) in _FOOTPRINT_FIELDS
    ]
    signature = {
        "pads": sorted(pads, key=_stable_key),
        "functional_graphics": sorted(functional_graphics, key=_stable_key),
        "attributes": sorted(attributes, key=_stable_key),
    }
    nets_consistent = all(len(assignments) <= 1 for assignments in nets_by_pad.values())
    return signature, nets_consistent


def _pad_signature(pad: list[Any], *, parent_rotation: Decimal) -> Any:
    head = [str(value) for value in pad[1:4]]
    children: list[Any] = []
    for child in pad[4:]:
        if not isinstance(child, list) or tag_of(child) not in _PAD_FIELDS:
            continue
        if tag_of(child) == "at":
            values = [_canonical(value) for value in child[1:3]]
            angle = (
                _decimal(child[3]) if len(child) > 3 else Decimal(0)
            ) - parent_rotation
            values.append(_normalize_angle(angle))
            children.append(["at", *values])
        else:
            children.append(_canonical(child))
    return {"head": head, "fields": sorted(children, key=_stable_key)}


def _node_uses_functional_layer(node: list[Any]) -> bool:
    layer = find_first(node, "layer")
    if layer is not None and len(layer) > 1:
        return str(layer[1]) in _FUNCTIONAL_LAYERS
    layers = find_first(node, "layers")
    return bool(
        layers is not None
        and any(str(value) in _FUNCTIONAL_LAYERS for value in layers[1:])
    )


def _canonical(value: Any) -> Any:
    if isinstance(value, list):
        if tag_of(value) in _DYNAMIC_TAGS:
            return None
        return [item for child in value if (item := _canonical(child)) is not None]
    if isinstance(value, Atom):
        try:
            return _normalize_decimal(Decimal(str(value)))
        except InvalidOperation:
            return str(value)
    return value


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric footprint field: {value}") from exc


def _normalize_decimal(value: Decimal) -> str:
    if value == Decimal("-0"):
        value = Decimal(0)
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _normalize_angle(value: Decimal) -> str:
    return _normalize_decimal(value % Decimal(360))


def _stable_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_stable_key(value).encode("utf-8")).hexdigest()
