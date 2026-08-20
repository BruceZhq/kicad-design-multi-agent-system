"""Schematic connectivity via union-find over wire/label/junction topology.

Builds connected components from wire endpoints (each wire unions its two
ends), attaches net names from labels and power symbols by coincident
position, and — when symbol geometry can be resolved — attaches component pins
too. This yields net membership, shorted-net detection and point tracing
without needing a running KiCAD.

Coordinates are snapped to a small grid so near-coincident endpoints connect.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

Point = Tuple[float, float]


def _key(x: float, y: float, tol: float = 0.01) -> Point:
    q = 1.0 / tol
    return (round(x * q) / q, round(y * q) / q)


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[Any, Any] = {}

    def find(self, a: Any) -> Any:
        self.parent.setdefault(a, a)
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:
            self.parent[a], a = root, self.parent[a]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


class SchematicGraph:
    def __init__(self, sch) -> None:
        self.sch = sch
        self.uf = UnionFind()
        self.point_labels: Dict[Point, set] = {}
        self.point_pins: Dict[Point, List[Dict[str, Any]]] = {}
        self._build()

    def _build(self) -> None:
        # Wires union their endpoints.
        for w in self.sch.list_wires():
            if w.get("start") and w.get("end"):
                a = _key(*w["start"])
                b = _key(*w["end"])
                self.uf.union(a, b)
        # Labels attach net names to their points.
        for lbl in self.sch.list_labels():
            if lbl.get("at") and lbl.get("text"):
                p = _key(*lbl["at"])
                self.uf.find(p)
                self.point_labels.setdefault(p, set()).add(lbl["text"])
        # Component pins (best-effort — needs symbol geometry).
        for comp in self.sch.list_components():
            ref = comp.get("reference")
            if not ref:
                continue
            try:
                pins = self.sch.pin_locations(ref)
            except Exception:
                pins = None
            if not pins:
                continue
            for pin in pins:
                p = _key(pin["x"], pin["y"])
                self.uf.find(p)
                self.point_pins.setdefault(p, []).append(
                    {"ref": ref, "pin": pin["number"], "name": pin["name"]})

    # -- queries ----------------------------------------------------------- #

    def _component_points(self, root: Any) -> List[Point]:
        return [p for p in self.uf.parent if self.uf.find(p) == root]

    def components(self) -> List[Dict[str, Any]]:
        roots: Dict[Any, Dict[str, Any]] = {}
        for p in list(self.uf.parent):
            r = self.uf.find(p)
            entry = roots.setdefault(r, {"points": [], "nets": set(), "pins": []})
            entry["points"].append(list(p))
            entry["nets"].update(self.point_labels.get(p, set()))
            entry["pins"].extend(self.point_pins.get(p, []))
        out = []
        for entry in roots.values():
            out.append({"points": entry["points"], "nets": sorted(entry["nets"]),
                        "pins": entry["pins"]})
        return out

    def net_of_point(self, x: float, y: float) -> Dict[str, Any]:
        p = _key(x, y)
        if p not in self.uf.parent:
            return {"found": False}
        root = self.uf.find(p)
        for comp in self.components():
            if [x, y] in comp["points"] or list(p) in comp["points"]:
                names = comp["nets"]
                return {"found": True, "nets": names, "pins": comp["pins"],
                        "point_count": len(comp["points"])}
        return {"found": True, "nets": [], "pins": []}

    def net_members(self, net: str) -> Optional[Dict[str, Any]]:
        for comp in self.components():
            if net in comp["nets"]:
                return {"net": net, "pins": comp["pins"], "labels": comp["nets"],
                        "point_count": len(comp["points"])}
        return None

    def shorted_nets(self) -> List[List[str]]:
        shorts = []
        for comp in self.components():
            if len(comp["nets"]) > 1:
                shorts.append(comp["nets"])
        return shorts
