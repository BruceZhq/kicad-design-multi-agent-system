"""A model for KiCAD ``.kicad_pcb`` board files.

Because we only need the *result* (a valid board file and its manufacturing
outputs), this edits the ``.kicad_pcb`` S-expression directly — no running
KiCAD, no IPC. Board outline, footprint placement, traces, vias, zones, nets
and text are all created by inserting nodes into the tree and saving
atomically, exactly like the schematic model.

Limitation: footprint *pad geometry* comes from a footprint library
(``.kicad_mod``). Placing a footprint here writes a positioned footprint node;
embedding full pads from a library is handled separately (roadmap). Traces,
vias, zones and the board outline are fully realised.
"""

from __future__ import annotations

import os
import tempfile
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .sexpr import Atom, Node, dumps, find_all, find_first, loads, tag_of

DEFAULT_VERSION = "20231120"
GENERATOR = "kicad-mcp-py"

# A reasonable 2-layer stack with the technical layers kicad-cli needs.
_BLANK_LAYERS = [
    (0, "F.Cu", "signal", None),
    (31, "B.Cu", "signal", None),
    (32, "B.Adhes", "user", "B.Adhesive"),
    (33, "F.Adhes", "user", "F.Adhesive"),
    (34, "B.Paste", "user", None),
    (35, "F.Paste", "user", None),
    (36, "B.SilkS", "user", "B.Silkscreen"),
    (37, "F.SilkS", "user", "F.Silkscreen"),
    (38, "B.Mask", "user", None),
    (39, "F.Mask", "user", None),
    (40, "Dwgs.User", "user", "User.Drawings"),
    (41, "Cmts.User", "user", "User.Comments"),
    (44, "Edge.Cuts", "user", None),
    (45, "Margin", "user", None),
    (46, "B.CrtYd", "user", "B.Courtyard"),
    (47, "F.CrtYd", "user", "F.Courtyard"),
    (48, "B.Fab", "user", None),
    (49, "F.Fab", "user", None),
]


def _fmt(v: float) -> str:
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return ("%.6f" % v).rstrip("0").rstrip(".")


def _n(v: float) -> Atom:
    return Atom(_fmt(v))


def _sym(tag: str, *children: Node) -> list:
    return [Atom(tag), *children]


def new_uuid() -> str:
    return str(_uuid.uuid4())


def _refresh_embedded_uuids(nodes: list[Node]) -> None:
    """Give copied footprint children unique identities while preserving references."""
    replacements: dict[str, str] = {}

    def replace_declarations(node: Node) -> None:
        if not isinstance(node, list):
            return
        if tag_of(node) in {"uuid", "tstamp"} and len(node) > 1:
            old = str(node[1])
            replacement = replacements.setdefault(old, new_uuid())
            node[1] = Atom(replacement) if isinstance(node[1], Atom) else replacement
        for child in node:
            replace_declarations(child)

    def replace_references(node: Node) -> None:
        if not isinstance(node, list):
            return
        for index, child in enumerate(node):
            if isinstance(child, list):
                replace_references(child)
                continue
            replacement = replacements.get(str(child))
            if replacement is not None:
                node[index] = Atom(replacement) if isinstance(child, Atom) else replacement

    replace_declarations(nodes)
    replace_references(nodes)


def _apply_footprint_rotation(nodes: list[Node], rotation: float) -> None:
    """Convert library-local pad angles to board-instance pad angles."""
    if float(rotation) % 360 == 0:
        return
    for node in nodes:
        if not isinstance(node, list) or tag_of(node) != "pad":
            continue
        at = find_first(node, "at")
        if at is None:
            continue
        local_angle = float(str(at[3])) if len(at) > 3 else 0.0
        instance_angle = (local_angle + rotation) % 360
        if len(at) > 3:
            at[3] = _n(instance_angle)
        else:
            at.append(_n(instance_angle))


def _blank_layers_node() -> list:
    node = [Atom("layers")]
    for idx, name, typ, alias in _BLANK_LAYERS:
        entry = [Atom(str(idx)), name, Atom(typ)]
        if alias:
            entry.append(alias)
        node.append(entry)
    return node


class PcbBoard:
    def __init__(self, root: list, path: Optional[Path] = None):
        if tag_of(root) != "kicad_pcb":
            raise ValueError("root node is not a (kicad_pcb ...) expression")
        self.root = root
        self.path = Path(path) if path else None

    # -- construction ------------------------------------------------------ #

    @classmethod
    def load(cls, path: os.PathLike | str) -> "PcbBoard":
        p = Path(path)
        node = loads(p.read_text(encoding="utf-8"))
        if not isinstance(node, list):
            raise ValueError("file did not contain an S-expression list")
        return cls(node, p)

    @classmethod
    def blank(cls, thickness: float = 1.6, paper: str = "A4") -> "PcbBoard":
        root = _sym(
            "kicad_pcb",
            _sym("version", Atom(DEFAULT_VERSION)),
            _sym("generator", GENERATOR),
            _sym("general", _sym("thickness", _n(thickness))),
            _sym("paper", paper),
            _blank_layers_node(),
            _sym("setup"),
            _sym("net", Atom("0"), ""),
        )
        return cls(root)

    # -- nets -------------------------------------------------------------- #

    def _net_nodes(self) -> List[list]:
        return find_all(self.root, "net")

    def list_nets(self) -> List[Dict[str, Any]]:
        out = []
        for n in self._net_nodes():
            if len(n) >= 3:
                out.append({"index": Atom(str(n[1])).as_int(), "name": str(n[2])})
        return out

    def add_net(self, name: str) -> int:
        for n in self._net_nodes():
            if len(n) >= 3 and str(n[2]) == name:
                return Atom(str(n[1])).as_int()
        idx = max((Atom(str(n[1])).as_int() for n in self._net_nodes()), default=-1) + 1
        # Net declarations live near the top, after setup.
        insert_at = self._after_tag("setup", default_before=("footprint", "segment", "via", "zone", "gr_line", "gr_rect"))
        self.root.insert(insert_at, _sym("net", Atom(str(idx)), name))
        return idx

    def _net_index(self, name_or_index: Any) -> int:
        if isinstance(name_or_index, int):
            return name_or_index
        for n in self._net_nodes():
            if len(n) >= 3 and str(n[2]) == str(name_or_index):
                return Atom(str(n[1])).as_int()
        return self.add_net(str(name_or_index))

    # -- board outline ----------------------------------------------------- #

    def set_board_outline(self, x0: float, y0: float, x1: float, y1: float,
                          width: float = 0.1) -> List[str]:
        """Draw a rectangular Edge.Cuts outline as four gr_line segments."""
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        uuids = []
        for (sx, sy), (ex, ey) in zip(corners, corners[1:]):
            u = new_uuid()
            self.root.append(
                _sym(
                    "gr_line",
                    _sym("start", _n(sx), _n(sy)),
                    _sym("end", _n(ex), _n(ey)),
                    _sym("stroke", _sym("width", _n(width)), _sym("type", Atom("default"))),
                    _sym("layer", "Edge.Cuts"),
                    _sym("uuid", u),
                )
            )
            uuids.append(u)
        return uuids

    # -- footprints -------------------------------------------------------- #

    def _footprints(self) -> List[list]:
        return find_all(self.root, "footprint")

    def _fp_property(self, fp: list, name: str) -> Optional[str]:
        for p in find_all(fp, "property"):
            if len(p) >= 3 and str(p[1]) == name:
                return str(p[2])
        return None

    def _find_footprint(self, reference: str) -> Optional[list]:
        for fp in self._footprints():
            if self._fp_property(fp, "Reference") == reference:
                return fp
        return None

    def add_footprint(self, lib_id: str, reference: str, value: str,
                      x: float, y: float, rotation: float = 0.0,
                      layer: str = "F.Cu", embed_node: Optional[list] = None) -> str:
        if self._find_footprint(reference):
            raise ValueError(f"reference {reference!r} already placed")
        fp_uuid = new_uuid()
        if embed_node is None:
            fp = _sym(
                "footprint",
                lib_id,
                _sym("layer", layer),
                _sym("uuid", fp_uuid),
                _sym("at", _n(x), _n(y), _n(rotation)),
                _sym("property", "Reference", reference,
                     _sym("at", _n(0), _n(-1), _n(0)), _sym("layer", "F.SilkS"),
                     _sym("uuid", new_uuid())),
                _sym("property", "Value", value,
                     _sym("at", _n(0), _n(1), _n(0)), _sym("layer", "F.Fab"),
                     _sym("uuid", new_uuid())),
            )
        else:
            from copy import deepcopy

            # Preserve the full real library footprint.  KiCad rotates child
            # positions through the footprint instance, but pad shape angles
            # in a board instance are absolute and must include that rotation.
            instance_owned = {
                "version", "generator", "generator_version", "layer",
                "uuid", "tstamp", "at",
            }
            children = [
                deepcopy(child)
                for child in embed_node[2:]
                if not (
                    isinstance(child, list)
                    and tag_of(child) in instance_owned
                )
            ]
            _refresh_embedded_uuids(children)
            _apply_footprint_rotation(children, rotation)
            fp = _sym(
                "footprint",
                lib_id,
                _sym("layer", layer),
                _sym("uuid", fp_uuid),
                _sym("at", _n(x), _n(y), _n(rotation)),
                *children,
            )

            properties = {
                str(child[1]): child
                for child in children
                if isinstance(child, list)
                and tag_of(child) == "property"
                and len(child) >= 3
            }
            if "Reference" in properties:
                properties["Reference"][2] = reference
            else:
                fp.insert(
                    5,
                    _sym("property", "Reference", reference,
                         _sym("at", _n(0), _n(-1), _n(0)),
                         _sym("layer", "F.SilkS"), _sym("uuid", new_uuid()))
                )
            if "Value" in properties:
                properties["Value"][2] = value
            else:
                fp.insert(
                    6 if "Reference" not in properties else 5,
                    _sym("property", "Value", value,
                         _sym("at", _n(0), _n(1), _n(0)),
                         _sym("layer", "F.Fab"), _sym("uuid", new_uuid()))
                )
        self.root.append(fp)
        return fp_uuid

    def _footprint_pads(self, fp: list) -> List[dict]:
        pads = []
        for pad in find_all(fp, "pad"):
            if len(pad) < 2:
                continue
            at = find_first(pad, "at")
            layers = find_first(pad, "layers")
            net = find_first(pad, "net")
            pads.append({
                "number": str(pad[1]),
                "type": str(pad[2]) if len(pad) > 2 else "",
                "rel": (Atom(str(at[1])).as_float() if at else 0.0,
                        Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0),
                "layers": [str(x) for x in layers[1:]] if layers else [],
                "net_index": Atom(str(net[1])).as_int()
                if net and len(net) > 1 else None,
                "net": str(net[2]) if net and len(net) > 2 else None,
            })
        return pads

    def footprint_pads(self, reference: str) -> List[Dict[str, Any]]:
        """Absolute pad positions of a placed footprint (accounts for rotation)."""
        from .footprint import rotate_offset
        fp = self._find_footprint(reference)
        if fp is None:
            raise ValueError(f"footprint {reference!r} not found")
        at = find_first(fp, "at")
        fx = Atom(str(at[1])).as_float() if at else 0.0
        fy = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        frot = Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0
        out = []
        for pad in self._footprint_pads(fp):
            dx, dy = rotate_offset(pad["rel"][0], pad["rel"][1], frot)
            out.append({"number": pad["number"], "x": round(fx + dx, 4),
                        "y": round(fy + dy, 4), "layers": pad["layers"],
                        "type": pad["type"],
                        "net_index": pad["net_index"], "net": pad["net"]})
        return out

    def pad_position(self, reference: str, pad_number: str) -> Optional[Dict[str, Any]]:
        for pad in self.footprint_pads(reference):
            if pad["number"] == str(pad_number):
                return pad
        return None

    def move_footprint(self, reference: str, x: float, y: float) -> bool:
        fp = self._find_footprint(reference)
        if fp is None:
            return False
        at = find_first(fp, "at")
        rot = Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0
        for i, c in enumerate(fp):
            if tag_of(c) == "at":
                fp[i] = _sym("at", _n(x), _n(y), _n(rot))
                break
        return True

    def rotate_footprint(self, reference: str, angle: float) -> bool:
        fp = self._find_footprint(reference)
        if fp is None:
            return False
        at = find_first(fp, "at")
        x = Atom(str(at[1])).as_float() if at else 0.0
        y = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        old_angle = Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0
        _apply_footprint_rotation(fp, angle - old_angle)
        for i, c in enumerate(fp):
            if tag_of(c) == "at":
                fp[i] = _sym("at", _n(x), _n(y), _n(angle))
                break
        return True

    def delete_footprint(self, reference: str) -> bool:
        fp = self._find_footprint(reference)
        if fp is None:
            return False
        self.root.remove(fp)
        return True

    def duplicate_footprint(self, reference: str, new_reference: str,
                            x: float, y: float) -> Optional[str]:
        """Deep-copy a placed footprint (pads included) to a new position."""
        from copy import deepcopy
        src = self._find_footprint(reference)
        if src is None or self._find_footprint(new_reference):
            return None
        clone = deepcopy(src)
        fp_uuid = new_uuid()
        rot = 0.0
        for i, c in enumerate(clone):
            tag = tag_of(c)
            if tag == "uuid":
                clone[i] = _sym("uuid", fp_uuid)
            elif tag == "at":
                rot = Atom(str(c[3])).as_float() if len(c) > 3 else 0.0
                clone[i] = _sym("at", _n(x), _n(y), _n(rot))
            elif tag == "property" and len(c) >= 3 and str(c[1]) == "Reference":
                c[2] = new_reference
        self.root.append(clone)
        return fp_uuid

    def list_footprints(self) -> List[Dict[str, Any]]:
        out = []
        for fp in self._footprints():
            at = find_first(fp, "at")
            layer = find_first(fp, "layer")
            lib = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else None
            out.append({
                "reference": self._fp_property(fp, "Reference"),
                "value": self._fp_property(fp, "Value"),
                "lib_id": lib,
                "layer": str(layer[1]) if layer and len(layer) > 1 else None,
                "at": {"x": Atom(str(at[1])).as_float(), "y": Atom(str(at[2])).as_float(),
                       "rotation": Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0}
                if at else None,
            })
        return out

    # -- traces / vias ----------------------------------------------------- #

    def add_track(self, x1: float, y1: float, x2: float, y2: float,
                  width: float = 0.25, layer: str = "F.Cu", net: Any = 0) -> str:
        net_idx = self._net_index(net) if not isinstance(net, int) or net != 0 else 0
        u = new_uuid()
        self.root.append(
            _sym(
                "segment",
                _sym("start", _n(x1), _n(y1)),
                _sym("end", _n(x2), _n(y2)),
                _sym("width", _n(width)),
                _sym("layer", layer),
                _sym("net", Atom(str(net_idx))),
                _sym("uuid", u),
            )
        )
        return u

    def add_via(self, x: float, y: float, size: float = 0.8, drill: float = 0.4,
                layers: Tuple[str, str] = ("F.Cu", "B.Cu"), net: Any = 0) -> str:
        net_idx = self._net_index(net) if not isinstance(net, int) or net != 0 else 0
        u = new_uuid()
        self.root.append(
            _sym(
                "via",
                _sym("at", _n(x), _n(y)),
                _sym("size", _n(size)),
                _sym("drill", _n(drill)),
                _sym("layers", layers[0], layers[1]),
                _sym("net", Atom(str(net_idx))),
                _sym("uuid", u),
            )
        )
        return u

    def delete_track(self, uuid: str) -> bool:
        for seg in list(find_all(self.root, "segment")):
            un = find_first(seg, "uuid")
            if un and len(un) > 1 and str(un[1]) == uuid:
                self.root.remove(seg)
                return True
        return False

    def list_tracks(self, net: Optional[Any] = None,
                    layer: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        net_idx = self._net_index(net) if net is not None else None
        net_names = {
            item["index"]: item["name"]
            for item in self.list_nets()
        }
        for seg in find_all(self.root, "segment"):
            s = find_first(seg, "start")
            e = find_first(seg, "end")
            ly = find_first(seg, "layer")
            nn = find_first(seg, "net")
            width = find_first(seg, "width")
            un = find_first(seg, "uuid")
            seg_layer = str(ly[1]) if ly and len(ly) > 1 else None
            seg_net = Atom(str(nn[1])).as_int() if nn and len(nn) > 1 else None
            if layer is not None and seg_layer != layer:
                continue
            if net_idx is not None and seg_net != net_idx:
                continue
            out.append({
                "uuid": str(un[1]) if un and len(un) > 1 else None,
                "start": [Atom(str(s[1])).as_float(), Atom(str(s[2])).as_float()] if s else None,
                "end": [Atom(str(e[1])).as_float(), Atom(str(e[2])).as_float()] if e else None,
                "layer": seg_layer,
                "net": seg_net,
                "net_name": net_names.get(seg_net, ""),
                "width": (
                    Atom(str(width[1])).as_float()
                    if width and len(width) > 1
                    else None
                ),
            })
        return out

    # -- zones / text / holes ---------------------------------------------- #

    def add_zone(self, layer: str, net: Any, points: List[Tuple[float, float]]) -> str:
        net_idx = self._net_index(net)
        net_name = next((n["name"] for n in self.list_nets() if n["index"] == net_idx), "")
        u = new_uuid()
        pts = [Atom("pts")] + [_sym("xy", _n(px), _n(py)) for px, py in points]
        self.root.append(
            _sym(
                "zone",
                _sym("net", Atom(str(net_idx))),
                _sym("net_name", net_name),
                _sym("layer", layer),
                _sym("uuid", u),
                _sym("polygon", pts),
            )
        )
        return u

    def list_zones(self) -> List[Dict[str, Any]]:
        """Return source and filled copper geometry for deterministic audits."""

        net_names = {
            item["index"]: item["name"]
            for item in self.list_nets()
        }
        zones: List[Dict[str, Any]] = []
        for zone in find_all(self.root, "zone"):
            net_node = find_first(zone, "net")
            net_name_node = find_first(zone, "net_name")
            layer_node = find_first(zone, "layer")
            polygon = find_first(zone, "polygon")
            net_index = (
                Atom(str(net_node[1])).as_int()
                if net_node and len(net_node) > 1
                else None
            )
            points: List[List[float]] = []
            if polygon is not None:
                pts = find_first(polygon, "pts")
                if pts is not None:
                    for point in find_all(pts, "xy"):
                        if len(point) >= 3:
                            points.append([
                                Atom(str(point[1])).as_float(),
                                Atom(str(point[2])).as_float(),
                            ])
            filled_polygons: List[Dict[str, Any]] = []
            for filled in find_all(zone, "filled_polygon"):
                filled_layer = find_first(filled, "layer")
                filled_points: List[List[float]] = []
                filled_pts = find_first(filled, "pts")
                if filled_pts is not None:
                    for point in find_all(filled_pts, "xy"):
                        if len(point) >= 3:
                            filled_points.append([
                                Atom(str(point[1])).as_float(),
                                Atom(str(point[2])).as_float(),
                            ])
                filled_polygons.append({
                    "layer": (
                        str(filled_layer[1])
                        if filled_layer and len(filled_layer) > 1
                        else ""
                    ),
                    "points": filled_points,
                    "island": find_first(filled, "island") is not None,
                })
            zones.append({
                "net_index": net_index,
                "net": (
                    str(net_name_node[1])
                    if net_name_node and len(net_name_node) > 1
                    else net_names.get(net_index, "")
                ),
                "layer": (
                    str(layer_node[1])
                    if layer_node and len(layer_node) > 1
                    else ""
                ),
                "points": points,
                "filled_polygons": filled_polygons,
            })
        return zones

    def add_text(self, text: str, x: float, y: float, layer: str = "F.SilkS",
                 rotation: float = 0.0) -> str:
        u = new_uuid()
        self.root.append(
            _sym(
                "gr_text",
                text,
                _sym("at", _n(x), _n(y), _n(rotation)),
                _sym("layer", layer),
                _sym("uuid", u),
                _sym("effects", _sym("font", _sym("size", _n(1), _n(1)),
                                     _sym("thickness", _n(0.15)))),
            )
        )
        return u

    def get_board_info(self) -> Dict[str, Any]:
        layers = find_first(self.root, "layers")
        n_layers = len(find_all(layers, "")) if layers else 0
        # Count copper layers by name suffix .Cu
        copper = 0
        if layers:
            for entry in layers[1:]:
                if isinstance(entry, list) and len(entry) > 1 and str(entry[1]).endswith(".Cu"):
                    copper += 1
        paper = find_first(self.root, "paper")
        return {
            "copper_layers": copper,
            "footprints": len(self._footprints()),
            "nets": len(self._net_nodes()),
            "paper": str(paper[1]) if paper and len(paper) > 1 else None,
        }

    def get_layer_list(self) -> List[Dict[str, Any]]:
        layers = find_first(self.root, "layers")
        out = []
        if layers:
            for entry in layers[1:]:
                if isinstance(entry, list) and len(entry) >= 3:
                    out.append({"index": Atom(str(entry[0])).as_int(),
                                "name": str(entry[1]), "type": str(entry[2])})
        return out

    def add_mounting_hole(self, x: float, y: float, diameter: float = 3.2) -> str:
        """Add a plain circular cut on Edge.Cuts (an NPTH mounting hole)."""
        u = new_uuid()
        r = diameter / 2.0
        self.root.append(
            _sym(
                "gr_circle",
                _sym("center", _n(x), _n(y)),
                _sym("end", _n(x + r), _n(y)),
                _sym("stroke", _sym("width", _n(0.1)), _sym("type", Atom("default"))),
                _sym("fill", Atom("none")),
                _sym("layer", "Edge.Cuts"),
                _sym("uuid", u),
            )
        )
        return u

    def get_board_extents(self) -> Optional[Dict[str, float]]:
        """Bounding box from Edge.Cuts lines and footprint positions."""
        xs: List[float] = []
        ys: List[float] = []
        for line in find_all(self.root, "gr_line"):
            for tag in ("start", "end"):
                pt = find_first(line, tag)
                if pt and len(pt) >= 3:
                    xs.append(Atom(str(pt[1])).as_float())
                    ys.append(Atom(str(pt[2])).as_float())
        for fp in self._footprints():
            at = find_first(fp, "at")
            if at and len(at) >= 3:
                xs.append(Atom(str(at[1])).as_float())
                ys.append(Atom(str(at[2])).as_float())
        if not xs:
            return None
        return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
                "width": max(xs) - min(xs), "height": max(ys) - min(ys)}

    def edit_footprint_value(self, reference: str, value: str) -> bool:
        fp = self._find_footprint(reference)
        if fp is None:
            return False
        for p in find_all(fp, "property"):
            if len(p) >= 3 and str(p[1]) == "Value":
                p[2] = value
                return True
        return False

    # -- layers / netclasses / design rules -------------------------------- #

    def add_layer(self, name: str, layer_type: str = "signal",
                  index: Optional[int] = None) -> bool:
        layers = find_first(self.root, "layers")
        if layers is None:
            return False
        used = [Atom(str(e[0])).as_int() for e in layers[1:]
                if isinstance(e, list) and len(e) > 0]
        idx = index if index is not None else (max(used) + 1 if used else 0)
        if any(isinstance(e, list) and len(e) > 1 and str(e[1]) == name for e in layers[1:]):
            return False
        layers.append([Atom(str(idx)), name, Atom(layer_type)])
        return True

    def set_active_layer(self, name: str) -> bool:
        setup = find_first(self.root, "setup")
        if setup is None:
            setup = _sym("setup")
            self.root.append(setup)
        for i, c in enumerate(setup):
            if tag_of(c) == "active_layer":
                setup[i] = _sym("active_layer", name)
                return True
        setup.append(_sym("active_layer", name))
        return True

    def add_netclass(self, name: str, clearance: float = 0.2,
                     track_width: float = 0.25, via_dia: float = 0.8,
                     via_drill: float = 0.4) -> str:
        nc = _sym("net_class", name, "",
                  _sym("clearance", _n(clearance)),
                  _sym("trace_width", _n(track_width)),
                  _sym("via_dia", _n(via_dia)),
                  _sym("via_drill", _n(via_drill)))
        self.root.append(nc)
        return name

    def assign_net_to_class(self, net: str, netclass: str) -> bool:
        for nc in find_all(self.root, "net_class"):
            if len(nc) > 1 and str(nc[1]) == netclass:
                nc.append(_sym("add_net", net))
                return True
        return False

    def set_design_rules(self, clearance: Optional[float] = None,
                         track_width: Optional[float] = None,
                         via_dia: Optional[float] = None,
                         via_drill: Optional[float] = None) -> Dict[str, Any]:
        setup = find_first(self.root, "setup")
        if setup is None:
            setup = _sym("setup")
            self.root.append(setup)

        def _set(tag, val):
            if val is None:
                return
            for i, c in enumerate(setup):
                if tag_of(c) == tag:
                    setup[i] = _sym(tag, _n(val))
                    return
            setup.append(_sym(tag, _n(val)))

        _set("min_clearance", clearance)
        _set("min_track_width", track_width)
        _set("min_via_diameter", via_dia)
        _set("min_via_drill", via_drill)
        return self.get_design_rules()

    def get_design_rules(self) -> Dict[str, Any]:
        setup = find_first(self.root, "setup")
        out: Dict[str, Any] = {}
        if setup:
            for tag in ("min_clearance", "min_track_width", "min_via_diameter", "min_via_drill"):
                node = find_first(setup, tag)
                if node and len(node) > 1:
                    out[tag] = Atom(str(node[1])).as_float()
        return out

    def footprint_distance(self, ref1: str, ref2: str) -> Optional[float]:
        a = next((f for f in self.list_footprints() if f["reference"] == ref1), None)
        b = next((f for f in self.list_footprints() if f["reference"] == ref2), None)
        if not a or not b or not a["at"] or not b["at"]:
            return None
        dx = a["at"]["x"] - b["at"]["x"]
        dy = a["at"]["y"] - b["at"]["y"]
        return round((dx * dx + dy * dy) ** 0.5, 4)

    def copy_routing_pattern(self, x0: float, y0: float, x1: float, y1: float,
                             dx: float, dy: float) -> Dict[str, int]:
        from copy import deepcopy
        copied = {"segments": 0, "vias": 0}
        lo_x, hi_x = min(x0, x1), max(x0, x1)
        lo_y, hi_y = min(y0, y1), max(y0, y1)

        def in_box(px, py):
            return lo_x <= px <= hi_x and lo_y <= py <= hi_y

        for seg in list(find_all(self.root, "segment")):
            s = find_first(seg, "start")
            e = find_first(seg, "end")
            if not s or not e:
                continue
            sx, sy = Atom(str(s[1])).as_float(), Atom(str(s[2])).as_float()
            ex, ey = Atom(str(e[1])).as_float(), Atom(str(e[2])).as_float()
            if in_box(sx, sy) and in_box(ex, ey):
                clone = deepcopy(seg)
                for i, c in enumerate(clone):
                    if tag_of(c) == "start":
                        clone[i] = _sym("start", _n(sx + dx), _n(sy + dy))
                    elif tag_of(c) == "end":
                        clone[i] = _sym("end", _n(ex + dx), _n(ey + dy))
                    elif tag_of(c) == "uuid":
                        clone[i] = _sym("uuid", new_uuid())
                self.root.append(clone)
                copied["segments"] += 1
        for via in list(find_all(self.root, "via")):
            at = find_first(via, "at")
            if not at:
                continue
            vx, vy = Atom(str(at[1])).as_float(), Atom(str(at[2])).as_float()
            if in_box(vx, vy):
                clone = deepcopy(via)
                for i, c in enumerate(clone):
                    if tag_of(c) == "at":
                        clone[i] = _sym("at", _n(vx + dx), _n(vy + dy))
                    elif tag_of(c) == "uuid":
                        clone[i] = _sym("uuid", new_uuid())
                self.root.append(clone)
                copied["vias"] += 1
        return copied

    def add_polygon(self, points: List[Tuple[float, float]], layer: str,
                    width: float = 0.1) -> str:
        u = new_uuid()
        pts = [Atom("pts")] + [_sym("xy", _n(px), _n(py)) for px, py in points]
        self.root.append(_sym("gr_poly", pts,
                              _sym("stroke", _sym("width", _n(width)), _sym("type", Atom("solid"))),
                              _sym("fill", Atom("solid")), _sym("layer", layer),
                              _sym("uuid", u)))
        return u

    # -- internals --------------------------------------------------------- #

    def _after_tag(self, tag: str, default_before: Tuple[str, ...] = ()) -> int:
        for i, child in enumerate(self.root):
            if tag_of(child) == tag:
                return i + 1
        for i, child in enumerate(self.root):
            if tag_of(child) in default_before:
                return i
        return len(self.root)

    # -- serialization ----------------------------------------------------- #

    def to_text(self) -> str:
        return dumps(self.root, pretty=True)

    def save(self, path: Optional[os.PathLike | str] = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path given and board has no associated path")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = self.to_text()
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".kicad_mcp_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.path = target
        return target
