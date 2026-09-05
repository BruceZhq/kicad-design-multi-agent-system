"""Execute exact net membership in KiCad project settings and Specctra DSN.

Pure stdlib: also imported by KiCad's separate interpreter. No gate settings
are changed. Existing project constraints and unrelated settings are retained.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

try:
    from .vendor.sexpr import Atom, dumps, find_first, loads, tag_of
except ImportError:  # KiCad worker invoked as a standalone script
    from vendor.sexpr import Atom, dumps, find_first, loads, tag_of


def bind_net_classes(classes: list[dict], nets: list[str], power_nets: list[str]) -> list[dict]:
    known, power = set(nets), set(power_nets)
    if len({c["name"].casefold() for c in classes}) != len(classes):
        raise ValueError("routing class names must be unique")
    explicit: dict[str, str] = {}
    for item in classes:
        for name in item.get("nets", []):
            if name not in known or name in explicit:
                raise ValueError(f"unknown or multiply assigned routing net: {name}")
            explicit[name] = item["name"]
    bound = []
    remaining = known - explicit.keys()
    for item in classes:
        members = list(item.get("nets", []))
        if not members and item["name"].casefold() in {"power", "signal"}:
            selected = remaining & power if item["name"].casefold() == "power" else remaining - power
            members = sorted(selected)
            remaining -= selected
        bound.append({**item, "nets": members})
    default = next((c for c in bound if c["name"].casefold() == "default"), None)
    if remaining:
        if default is None:
            raise ValueError(f"routing plan has no class for nets: {sorted(remaining)}")
        default["nets"] = sorted(set(default["nets"]) | remaining)
    return [item for item in bound if item["nets"]]


def persist_project_classes(pcb: Path, classes: list[dict]) -> None:
    path = pcb.with_suffix(".kicad_pro")
    project = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    settings = project.setdefault("net_settings", {})
    names = {"RN:" + c["name"] for c in classes}
    rows = [row for row in settings.get("classes", []) if row.get("name") not in names]
    for item in classes:
        rows.append({"name": "RN:" + item["name"], "track_width": item["width"],
                     "clearance": item["clearance"], "via_diameter": item["via_diameter"],
                     "via_drill": item["via_drill"]})
    assignments = settings.setdefault("netclass_assignments", {})
    for item in classes:
        for net in item["nets"]:
            assignments[net] = ["RN:" + item["name"]]
    settings["classes"] = rows
    path.write_text(json.dumps(project, indent=2), encoding="utf-8")


def apply_dsn_classes(path: Path, classes: list[dict], *, only_nets: set[str] | None = None) -> None:
    root = loads(path.read_text(encoding="utf-8"))
    network, library = find_first(root, "network"), find_first(root, "library")
    resolution = find_first(root, "resolution")
    if network is None or library is None or resolution is None:
        raise ValueError("DSN lacks network, library or resolution")
    units = {"mm": 1.0, "um": 1000.0, "mil": 1000.0 / 25.4, "inch": 1.0 / 25.4}
    scale = units[str(resolution[1])] * float(str(resolution[2]))
    available = {str(node[1]) for node in network[1:] if tag_of(node) == "net"}
    required = {name for c in classes for name in c["nets"]}
    if required - available:
        raise ValueError(f"planned nets missing from actual DSN: {sorted(required - available)}")
    structure = find_first(root, "structure")
    via = find_first(structure, "via") if structure else None
    template = next((node for node in library[1:] if tag_of(node) == "padstack"
                     and via and str(node[1]) == str(via[1])), None)
    if not template:
        raise ValueError("DSN has no real via padstack template")
    network[:] = [network[0], *[node for node in network[1:]
                    if tag_of(node) != "class" and (only_nets is None or tag_of(node) != "net"
                                                    or str(node[1]) in only_nets)]]
    for item in classes:
        members = [n for n in item["nets"] if only_nets is None or n in only_nets]
        if not members:
            continue
        via_name = f"RN-via-{item['via_diameter']:g}-{item['via_drill']:g}"
        stack = copy.deepcopy(template)
        stack[1] = via_name
        for shape in stack[2:]:
            circle = find_first(shape, "circle") if tag_of(shape) == "shape" else None
            if circle is not None:
                circle[2] = Atom(str(item["via_diameter"] * scale))
        if not any(tag_of(node) == "padstack" and str(node[1]) == via_name for node in library[1:]):
            library.append(stack)
        if via_name not in [str(v) for v in via[1:]]:
            via.append(via_name)
        network.append([Atom("class"), "RN:" + item["name"], *members,
                        [Atom("circuit"), [Atom("use_via"), via_name]],
                        [Atom("rule"), [Atom("width"), Atom(str(item["width"] * scale))],
                         [Atom("clearance"), Atom(str(item["clearance"] * scale))]]])
    path.write_text(dumps(root), encoding="utf-8")
