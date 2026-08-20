"""A model for KiCAD ``.kicad_sch`` schematic files.

This wraps the parsed S-expression tree with a handful of high-level
operations: listing symbols, adding a symbol instance, and adding a wire.
Saves are atomic (write to a temp file, fsync, then ``os.replace``) so an
interrupted write can never truncate or corrupt an existing schematic.

The KiCAD schematic file format is publicly documented; this model is an
original implementation of read/modify/write on top of that format.
"""

from __future__ import annotations

import os
import tempfile
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import sexpr
from .sexpr import Atom, Node, dumps, find_all, find_first, loads, tag_of

# KiCAD file-format version this writer targets. The 8.x stable series uses
# this token; newer KiCAD releases read it without complaint.
DEFAULT_VERSION = "20231120"
GENERATOR = "kicad-mcp-py"


def _fmt(value: float) -> str:
    """Format a coordinate the way KiCAD does: trim trailing zeros."""
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return ("%.4f" % value).rstrip("0").rstrip(".")


def _num(value: float) -> Atom:
    return Atom(_fmt(value))


def _sym(tag: str, *children: Node) -> list:
    """Build a list node ``(tag child1 child2 ...)``."""
    return [Atom(tag), *children]


def new_uuid() -> str:
    return str(_uuid.uuid4())


def _effects_font(size: float = 1.27) -> list:
    return _sym("effects", _sym("font", _sym("size", _num(size), _num(size))))


class Schematic:
    """In-memory representation of a ``.kicad_sch`` file."""

    def __init__(self, root: list, path: Optional[Path] = None):
        if tag_of(root) != "kicad_sch":
            raise ValueError("root node is not a (kicad_sch ...) expression")
        self.root = root
        self.path = Path(path) if path else None

    # -- construction ------------------------------------------------------ #

    @classmethod
    def load(cls, path: os.PathLike | str) -> "Schematic":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        node = loads(text)
        if not isinstance(node, list):
            raise ValueError("file did not contain an S-expression list")
        return cls(node, p)

    @classmethod
    def blank(cls, paper: str = "A4") -> "Schematic":
        sch_uuid = new_uuid()
        root = _sym(
            "kicad_sch",
            _sym("version", Atom(DEFAULT_VERSION)),
            _sym("generator", GENERATOR),
            _sym("generator_version", "0.1"),
            _sym("uuid", sch_uuid),
            _sym("paper", paper),
            _sym("lib_symbols"),
            _sym("sheet_instances", _sym("path", "/", _sym("page", "1"))),
        )
        return cls(root)

    # -- accessors --------------------------------------------------------- #

    @property
    def uuid(self) -> str:
        node = find_first(self.root, "uuid")
        if node and len(node) > 1 and isinstance(node[1], (str, Atom)):
            return str(node[1])
        return ""

    def _symbols(self) -> List[list]:
        return find_all(self.root, "symbol")

    def _property(self, symbol: list, name: str) -> Optional[str]:
        for prop in find_all(symbol, "property"):
            if len(prop) >= 3 and str(prop[1]) == name:
                return str(prop[2])
        return None

    def list_components(self) -> List[Dict[str, Any]]:
        """Return a summary of every placed symbol instance."""
        out: List[Dict[str, Any]] = []
        for sym in self._symbols():
            lib = find_first(sym, "lib_id")
            at = find_first(sym, "at")
            uuid_node = find_first(sym, "uuid")
            dnp_node = find_first(sym, "dnp")
            properties = {
                str(prop[1]): str(prop[2])
                for prop in find_all(sym, "property")
                if len(prop) >= 3
            }
            entry = {
                "reference": self._property(sym, "Reference"),
                "value": self._property(sym, "Value"),
                "footprint": self._property(sym, "Footprint"),
                "lib_id": str(lib[1]) if lib and len(lib) > 1 else None,
                "uuid": str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None,
                "dnp": bool(
                    dnp_node
                    and len(dnp_node) > 1
                    and str(dnp_node[1]).casefold() == "yes"
                ),
                "properties": properties,
            }
            if at and len(at) >= 3:
                entry["at"] = {
                    "x": float(Atom(str(at[1])).as_float()),
                    "y": float(Atom(str(at[2])).as_float()),
                    "rotation": float(Atom(str(at[3])).as_float()) if len(at) > 3 else 0.0,
                }
            out.append(entry)
        return out

    # -- mutation ---------------------------------------------------------- #

    def add_component(
        self,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        footprint: str = "",
        dnp: bool = False,
        properties: Optional[Dict[str, str]] = None,
    ) -> str:
        """Insert a symbol instance and return its UUID.

        Note: for the symbol to render with its graphics in the KiCAD GUI,
        the matching definition must exist in the ``lib_symbols`` section.
        Embedding library graphics is a planned feature; this MVP writes a
        structurally valid instance with references, value and placement.
        """
        if self._reference_exists(reference):
            raise ValueError(f"reference {reference!r} is already used")

        comp_uuid = new_uuid()
        symbol = _sym(
            "symbol",
            _sym("lib_id", lib_id),
            _sym("at", _num(x), _num(y), _num(rotation)),
            _sym("unit", Atom("1")),
            _sym("in_bom", Atom("yes")),
            _sym("on_board", Atom("yes")),
            _sym("dnp", Atom("yes" if dnp else "no")),
            _sym("uuid", comp_uuid),
            _sym(
                "property",
                "Reference",
                reference,
                _sym("at", _num(x), _num(y - 2.54), _num(0)),
                _effects_font(),
            ),
            _sym(
                "property",
                "Value",
                value,
                _sym("at", _num(x), _num(y + 2.54), _num(0)),
                _effects_font(),
            ),
            _sym(
                "property",
                "Footprint",
                footprint,
                _sym("at", _num(x), _num(y), _num(0)),
                [Atom("effects"), _sym("font", _sym("size", _num(1.27), _num(1.27))), Atom("hide")],
            ),
            _sym(
                "instances",
                _sym(
                    "project",
                    GENERATOR,
                    _sym(
                        "path",
                        f"/{self.uuid}",
                        _sym("reference", reference),
                        _sym("unit", Atom("1")),
                    ),
                ),
            ),
        )
        reserved = {"Reference", "Value", "Footprint"}
        for name, property_value in (properties or {}).items():
            if name in reserved:
                continue
            symbol.append(
                _sym(
                    "property",
                    name,
                    str(property_value),
                    _sym("at", _num(x), _num(y), _num(0)),
                    _sym(
                        "effects",
                        _sym("font", _sym("size", _num(1.27), _num(1.27))),
                        Atom("hide"),
                    ),
                )
            )
        self._insert_before_trailer(symbol)
        return comp_uuid

    def add_wire(self, x1: float, y1: float, x2: float, y2: float) -> str:
        """Add a wire segment between two points and return its UUID."""
        wire_uuid = new_uuid()
        wire = _sym(
            "wire",
            _sym("pts", _sym("xy", _num(x1), _num(y1)), _sym("xy", _num(x2), _num(y2))),
            _sym("stroke", _sym("width", _num(0)), _sym("type", Atom("default"))),
            _sym("uuid", wire_uuid),
        )
        self._insert_before_trailer(wire)
        return wire_uuid

    # -- component read/modify/delete -------------------------------------- #

    def _find_symbol(self, reference: str) -> Optional[list]:
        for sym in self._symbols():
            if self._property(sym, "Reference") == reference:
                return sym
        return None

    def get_component(self, reference: str) -> Dict[str, Any]:
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        props = {}
        for p in find_all(sym, "property"):
            if len(p) >= 3:
                props[str(p[1])] = str(p[2])
        at = find_first(sym, "at")
        lib = find_first(sym, "lib_id")
        uuid_node = find_first(sym, "uuid")
        dnp_node = find_first(sym, "dnp")
        return {
            "reference": reference,
            "lib_id": str(lib[1]) if lib and len(lib) > 1 else None,
            "uuid": str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None,
            "properties": props,
            "dnp": bool(
                dnp_node
                and len(dnp_node) > 1
                and str(dnp_node[1]).casefold() == "yes"
            ),
            "at": {
                "x": Atom(str(at[1])).as_float() if at else None,
                "y": Atom(str(at[2])).as_float() if at and len(at) > 2 else None,
                "rotation": Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0,
            },
        }

    def delete_component(self, reference: str) -> bool:
        sym = self._find_symbol(reference)
        if sym is None:
            return False
        self.root.remove(sym)
        return True

    def _symbol_mirror(self, sym: list) -> Optional[str]:
        m = find_first(sym, "mirror")
        if m and len(m) > 1:
            return str(m[1])
        return None

    def pin_locations(self, reference: str) -> Optional[List[Dict[str, Any]]]:
        """Absolute pin coordinates of a placed symbol, or None if the symbol
        library cannot be resolved (best-effort geometry)."""
        from .symbol_lib import symbol_pins, transform_pin
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        lib = find_first(sym, "lib_id")
        lib_id = str(lib[1]) if lib and len(lib) > 1 else None
        if not lib_id:
            return None
        pins = symbol_pins(lib_id)
        if pins is None:
            return None
        at = find_first(sym, "at")
        px = Atom(str(at[1])).as_float() if at else 0.0
        py = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        rot = Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0
        mirror = self._symbol_mirror(sym)
        out = []
        for p in pins:
            ax, ay = transform_pin(px, py, rot, mirror, p["x"], p["y"])
            out.append({"number": p["number"], "name": p["name"], "type": p["type"],
                        "x": ax, "y": ay})
        return out

    def _set_property(self, symbol: list, name: str, value: str) -> None:
        for p in find_all(symbol, "property"):
            if len(p) >= 3 and str(p[1]) == name:
                p[2] = value
                return
        # Property doesn't exist yet: append one anchored at the symbol origin.
        at = find_first(symbol, "at")
        x = Atom(str(at[1])).as_float() if at else 0.0
        y = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        symbol.append(
            _sym("property", name, value, _sym("at", _num(x), _num(y), _num(0)), _effects_font())
        )

    def edit_component(
        self,
        reference: str,
        value: Optional[str] = None,
        footprint: Optional[str] = None,
        properties: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        if value is not None:
            self._set_property(sym, "Value", value)
        if footprint is not None:
            self._set_property(sym, "Footprint", footprint)
        for k, v in (properties or {}).items():
            self._set_property(sym, k, v)
        return self.get_component(reference)

    def move_component(self, reference: str, x: float, y: float) -> Dict[str, Any]:
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        at = find_first(sym, "at")
        rot = Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0
        # Replace the at node in place, preserving rotation.
        for i, child in enumerate(sym):
            if tag_of(child) == "at":
                sym[i] = _sym("at", _num(x), _num(y), _num(rot))
                break
        return self.get_component(reference)

    def rotate_component(self, reference: str, angle: float) -> Dict[str, Any]:
        if angle not in (0, 90, 180, 270):
            raise ValueError("rotation must be one of 0, 90, 180, 270")
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        at = find_first(sym, "at")
        x = Atom(str(at[1])).as_float() if at else 0.0
        y = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        for i, child in enumerate(sym):
            if tag_of(child) == "at":
                sym[i] = _sym("at", _num(x), _num(y), _num(angle))
                break
        return self.get_component(reference)

    def replace_component(self, reference: str, new_lib_id: str) -> Dict[str, Any]:
        sym = self._find_symbol(reference)
        if sym is None:
            raise ValueError(f"component {reference!r} not found")
        for i, child in enumerate(sym):
            if tag_of(child) == "lib_id":
                sym[i] = _sym("lib_id", new_lib_id)
                break
        return self.get_component(reference)

    # -- wires ------------------------------------------------------------- #

    def list_wires(self) -> List[Dict[str, Any]]:
        out = []
        for wire in find_all(self.root, "wire"):
            pts = find_first(wire, "pts")
            uuid_node = find_first(wire, "uuid")
            xys = find_all(pts, "xy") if pts else []
            entry: Dict[str, Any] = {
                "uuid": str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None,
            }
            if len(xys) >= 2:
                entry["start"] = [Atom(str(xys[0][1])).as_float(), Atom(str(xys[0][2])).as_float()]
                entry["end"] = [Atom(str(xys[1][1])).as_float(), Atom(str(xys[1][2])).as_float()]
            out.append(entry)
        return out

    def delete_wire(
        self,
        uuid: Optional[str] = None,
        start: Optional[tuple] = None,
        end: Optional[tuple] = None,
    ) -> bool:
        for wire in list(find_all(self.root, "wire")):
            if uuid is not None:
                un = find_first(wire, "uuid")
                if un and len(un) > 1 and str(un[1]) == uuid:
                    self.root.remove(wire)
                    return True
                continue
            if start is not None and end is not None:
                pts = find_first(wire, "pts")
                xys = find_all(pts, "xy") if pts else []
                if len(xys) >= 2:
                    s = (Atom(str(xys[0][1])).as_float(), Atom(str(xys[0][2])).as_float())
                    e = (Atom(str(xys[1][1])).as_float(), Atom(str(xys[1][2])).as_float())
                    if (s == tuple(start) and e == tuple(end)) or (
                        s == tuple(end) and e == tuple(start)
                    ):
                        self.root.remove(wire)
                        return True
        return False

    # -- labels / power / junctions / no-connect / text -------------------- #

    _LABEL_TAGS = {"local": "label", "global": "global_label", "hierarchical": "hierarchical_label"}

    def add_net_label(
        self, text: str, x: float, y: float, label_type: str = "local", rotation: float = 0.0
    ) -> str:
        tag = self._LABEL_TAGS.get(label_type)
        if tag is None:
            raise ValueError(f"label_type must be one of {sorted(self._LABEL_TAGS)}")
        label_uuid = new_uuid()
        node = _sym(
            tag,
            text,
            _sym("at", _num(x), _num(y), _num(rotation)),
            _sym("effects", _sym("font", _sym("size", _num(1.27), _num(1.27)))),
            _sym("uuid", label_uuid),
        )
        self._insert_before_trailer(node)
        return label_uuid

    def list_labels(self) -> List[Dict[str, Any]]:
        out = []
        for kind, tag in self._LABEL_TAGS.items():
            for node in find_all(self.root, tag):
                at = find_first(node, "at")
                out.append(
                    {
                        "type": kind,
                        "text": str(node[1]) if len(node) > 1 else None,
                        "at": [Atom(str(at[1])).as_float(), Atom(str(at[2])).as_float()]
                        if at
                        else None,
                    }
                )
        return out

    def list_nets(self) -> List[str]:
        names = set()
        for label in self.list_labels():
            if label["text"]:
                names.add(label["text"])
        # Power symbols contribute their Value (net name, e.g. GND, +3V3).
        for sym in self._symbols():
            lib = find_first(sym, "lib_id")
            if lib and len(lib) > 1 and str(lib[1]).startswith("power:"):
                val = self._property(sym, "Value")
                if val:
                    names.add(val)
        return sorted(names)

    def _next_power_ref(self) -> str:
        n = 1
        existing = {self._property(s, "Reference") for s in self._symbols()}
        while f"#PWR{n:02d}" in existing:
            n += 1
        return f"#PWR{n:02d}"

    def add_power_symbol(self, net: str, x: float, y: float, rotation: float = 0.0) -> str:
        ref = self._next_power_ref()
        comp_uuid = new_uuid()
        symbol = _sym(
            "symbol",
            _sym("lib_id", f"power:{net}"),
            _sym("at", _num(x), _num(y), _num(rotation)),
            _sym("unit", Atom("1")),
            _sym("in_bom", Atom("no")),
            _sym("on_board", Atom("yes")),
            _sym("uuid", comp_uuid),
            _sym("property", "Reference", ref, _sym("at", _num(x), _num(y), _num(0)), _effects_font()),
            _sym("property", "Value", net, _sym("at", _num(x), _num(y), _num(0)), _effects_font()),
            _sym(
                "instances",
                _sym(
                    "project",
                    GENERATOR,
                    _sym("path", f"/{self.uuid}", _sym("reference", ref), _sym("unit", Atom("1"))),
                ),
            ),
        )
        self._insert_before_trailer(symbol)
        return comp_uuid

    def add_junction(self, x: float, y: float, diameter: float = 0.0) -> str:
        j_uuid = new_uuid()
        node = _sym(
            "junction",
            _sym("at", _num(x), _num(y)),
            _sym("diameter", _num(diameter)),
            _sym("uuid", j_uuid),
        )
        self._insert_before_trailer(node)
        return j_uuid

    def add_no_connect(self, x: float, y: float) -> str:
        nc_uuid = new_uuid()
        node = _sym("no_connect", _sym("at", _num(x), _num(y)), _sym("uuid", nc_uuid))
        self._insert_before_trailer(node)
        return nc_uuid

    def add_text(self, text: str, x: float, y: float, rotation: float = 0.0) -> str:
        t_uuid = new_uuid()
        node = _sym(
            "text",
            text,
            _sym("at", _num(x), _num(y), _num(rotation)),
            _sym("effects", _sym("font", _sym("size", _num(1.27), _num(1.27)))),
            _sym("uuid", t_uuid),
        )
        self._insert_before_trailer(node)
        return t_uuid

    def _label_nodes(self) -> List[list]:
        out = []
        for tag in self._LABEL_TAGS.values():
            out.extend(find_all(self.root, tag))
        return out

    def delete_label(self, text: str, x: Optional[float] = None,
                     y: Optional[float] = None) -> int:
        removed = 0
        for node in list(self._label_nodes()):
            if len(node) < 2 or str(node[1]) != text:
                continue
            if x is not None and y is not None:
                at = find_first(node, "at")
                if not at or Atom(str(at[1])).as_float() != x or \
                        Atom(str(at[2])).as_float() != y:
                    continue
            self.root.remove(node)
            removed += 1
        return removed

    def rotate_labels(self, text: str, angle: float) -> int:
        n = 0
        for node in self._label_nodes():
            if len(node) >= 2 and str(node[1]) == text:
                at = find_first(node, "at")
                if at:
                    x = Atom(str(at[1])).as_float()
                    y = Atom(str(at[2])).as_float()
                    for i, c in enumerate(node):
                        if tag_of(c) == "at":
                            node[i] = _sym("at", _num(x), _num(y), _num(angle))
                            break
                    n += 1
        return n

    def move_labels(self, text: str, dx: float, dy: float) -> int:
        n = 0
        for node in self._label_nodes():
            if len(node) >= 2 and str(node[1]) == text:
                at = find_first(node, "at")
                if at:
                    x = Atom(str(at[1])).as_float() + dx
                    y = Atom(str(at[2])).as_float() + dy
                    rot = Atom(str(at[3])).as_float() if len(at) > 3 else 0.0
                    for i, c in enumerate(node):
                        if tag_of(c) == "at":
                            node[i] = _sym("at", _num(x), _num(y), _num(rot))
                            break
                    n += 1
        return n

    def delete_no_connect(self, x: float, y: float) -> bool:
        for nc in list(find_all(self.root, "no_connect")):
            at = find_first(nc, "at")
            if at and Atom(str(at[1])).as_float() == x and \
                    Atom(str(at[2])).as_float() == y:
                self.root.remove(nc)
                return True
        return False

    # -- hierarchical sheets ---------------------------------------------- #

    def _sheets(self) -> List[list]:
        return find_all(self.root, "sheet")

    def _sheet_prop(self, sheet: list, name: str) -> Optional[str]:
        for p in find_all(sheet, "property"):
            if len(p) >= 3 and str(p[1]) == name:
                return str(p[2])
        return None

    def _find_sheet(self, name: str) -> Optional[list]:
        for sh in self._sheets():
            if self._sheet_prop(sh, "Sheetname") == name:
                return sh
        return None

    def add_sheet(self, name: str, filename: str, x: float, y: float,
                  w: float = 30.0, h: float = 20.0) -> str:
        if self._find_sheet(name):
            raise ValueError(f"sheet {name!r} already exists")
        sheet_uuid = new_uuid()
        node = _sym(
            "sheet",
            _sym("at", _num(x), _num(y)),
            _sym("size", _num(w), _num(h)),
            _sym("stroke", _sym("width", _num(0.12)), _sym("type", Atom("solid"))),
            _sym("uuid", sheet_uuid),
            _sym("property", "Sheetname", name, _sym("at", _num(x), _num(y - 1), _num(0)),
                 _effects_font()),
            _sym("property", "Sheetfile", filename, _sym("at", _num(x), _num(y + h + 1), _num(0)),
                 _effects_font()),
        )
        self._insert_before_trailer(node)
        # Create the child file if it doesn't exist yet.
        if self.path:
            child = self.path.parent / filename
            if not child.exists():
                Schematic.blank().save(child)
        return sheet_uuid

    def edit_sheet(self, name: str, new_name: Optional[str] = None,
                   new_file: Optional[str] = None, x: Optional[float] = None,
                   y: Optional[float] = None, w: Optional[float] = None,
                   h: Optional[float] = None) -> bool:
        sh = self._find_sheet(name)
        if sh is None:
            return False
        if new_name is not None:
            for p in find_all(sh, "property"):
                if str(p[1]) == "Sheetname":
                    p[2] = new_name
        if new_file is not None:
            for p in find_all(sh, "property"):
                if str(p[1]) == "Sheetfile":
                    p[2] = new_file
        if x is not None and y is not None:
            for i, c in enumerate(sh):
                if tag_of(c) == "at":
                    sh[i] = _sym("at", _num(x), _num(y))
        if w is not None and h is not None:
            for i, c in enumerate(sh):
                if tag_of(c) == "size":
                    sh[i] = _sym("size", _num(w), _num(h))
        return True

    def move_sheet(self, name: str, x: float, y: float) -> bool:
        sh = self._find_sheet(name)
        if sh is None:
            return False
        for i, c in enumerate(sh):
            if tag_of(c) == "at":
                sh[i] = _sym("at", _num(x), _num(y))
                return True
        return False

    def delete_sheet(self, name: str) -> bool:
        sh = self._find_sheet(name)
        if sh is None:
            return False
        self.root.remove(sh)
        return True

    def duplicate_sheet(self, name: str, new_name: str, new_file: str,
                        dx: float = 10.0, dy: float = 10.0) -> Optional[str]:
        from copy import deepcopy
        src = self._find_sheet(name)
        if src is None or self._find_sheet(new_name):
            return None
        clone = deepcopy(src)
        sheet_uuid = new_uuid()
        for i, c in enumerate(clone):
            tag = tag_of(c)
            if tag == "uuid":
                clone[i] = _sym("uuid", sheet_uuid)
            elif tag == "at":
                x = Atom(str(c[1])).as_float() + dx
                y = Atom(str(c[2])).as_float() + dy
                clone[i] = _sym("at", _num(x), _num(y))
            elif tag == "property" and len(c) >= 3:
                if str(c[1]) == "Sheetname":
                    c[2] = new_name
                elif str(c[1]) == "Sheetfile":
                    c[2] = new_file
        self.root.append(clone)
        if self.path:
            src_file = self._sheet_prop(src, "Sheetfile")
            dst = self.path.parent / new_file
            src_path = self.path.parent / src_file if src_file else None
            if src_path and src_path.exists() and not dst.exists():
                child = Schematic.load(src_path)
                # Give the clone its own root uuid.
                for i, c in enumerate(child.root):
                    if tag_of(c) == "uuid":
                        child.root[i] = _sym("uuid", new_uuid())
                child.save(dst)
            elif not dst.exists():
                Schematic.blank().save(dst)
        return sheet_uuid

    def list_sheets(self) -> List[Dict[str, Any]]:
        out = []
        for sh in self._sheets():
            at = find_first(sh, "at")
            size = find_first(sh, "size")
            uuid_node = find_first(sh, "uuid")
            out.append({
                "name": self._sheet_prop(sh, "Sheetname"),
                "file": self._sheet_prop(sh, "Sheetfile"),
                "uuid": str(uuid_node[1]) if uuid_node and len(uuid_node) > 1 else None,
                "at": [Atom(str(at[1])).as_float(), Atom(str(at[2])).as_float()] if at else None,
                "size": [Atom(str(size[1])).as_float(), Atom(str(size[2])).as_float()]
                if size else None,
                "pins": self._sheet_pins(sh),
            })
        return out

    def _sheet_pins(self, sheet: list) -> List[Dict[str, Any]]:
        pins = []
        for pin in find_all(sheet, "pin"):
            at = find_first(pin, "at")
            pins.append({
                "name": str(pin[1]) if len(pin) > 1 else "",
                "type": str(pin[2]) if len(pin) > 2 and isinstance(pin[2], Atom) else "passive",
                "at": [Atom(str(at[1])).as_float(), Atom(str(at[2])).as_float()] if at else None,
                "angle": Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0,
            })
        return pins

    def add_sheet_pin(self, sheet_name: str, pin_name: str, etype: str = "passive",
                      x: Optional[float] = None, y: Optional[float] = None,
                      angle: float = 0.0) -> Optional[str]:
        sh = self._find_sheet(sheet_name)
        if sh is None:
            return None
        at = find_first(sh, "at")
        sx = Atom(str(at[1])).as_float() if at else 0.0
        sy = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        px = sx if x is None else x
        py = sy if y is None else y
        pin_uuid = new_uuid()
        sh.append(_sym("pin", pin_name, Atom(etype),
                       _sym("at", _num(px), _num(py), _num(angle)),
                       _effects_font(), _sym("uuid", pin_uuid)))
        return pin_uuid

    def edit_sheet_pin(self, sheet_name: str, pin_name: str,
                       new_name: Optional[str] = None, new_type: Optional[str] = None,
                       x: Optional[float] = None, y: Optional[float] = None) -> bool:
        sh = self._find_sheet(sheet_name)
        if sh is None:
            return False
        for pin in find_all(sh, "pin"):
            if len(pin) > 1 and str(pin[1]) == pin_name:
                if new_name is not None:
                    pin[1] = new_name
                if new_type is not None and len(pin) > 2:
                    pin[2] = Atom(new_type)
                if x is not None and y is not None:
                    for i, c in enumerate(pin):
                        if tag_of(c) == "at":
                            ang = Atom(str(c[3])).as_float() if len(c) > 3 else 0.0
                            pin[i] = _sym("at", _num(x), _num(y), _num(ang))
                return True
        return False

    def delete_sheet_pin(self, sheet_name: str, pin_name: str) -> bool:
        sh = self._find_sheet(sheet_name)
        if sh is None:
            return False
        for pin in list(find_all(sh, "pin")):
            if len(pin) > 1 and str(pin[1]) == pin_name:
                sh.remove(pin)
                return True
        return False

    def child_hier_labels(self, sheet_name: str) -> List[str]:
        sh = self._find_sheet(sheet_name)
        if sh is None or not self.path:
            return []
        fname = self._sheet_prop(sh, "Sheetfile")
        if not fname:
            return []
        child = self.path.parent / fname
        if not child.exists():
            return []
        node = loads(child.read_text(encoding="utf-8"))
        names = []
        for hl in find_all(node, "hierarchical_label"):
            if len(hl) > 1:
                names.append(str(hl[1]))
        return names

    def import_sheet_pins(self, sheet_name: str) -> Dict[str, Any]:
        sh = self._find_sheet(sheet_name)
        if sh is None:
            return {"added": [], "error": "sheet_not_found"}
        existing = {str(p[1]) for p in find_all(sh, "pin") if len(p) > 1}
        added = []
        labels = self.child_hier_labels(sheet_name)
        at = find_first(sh, "at")
        size = find_first(sh, "size")
        sx = Atom(str(at[1])).as_float() if at else 0.0
        sy = Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0
        h = Atom(str(size[2])).as_float() if size and len(size) > 2 else 20.0
        step = h / (len(labels) + 1) if labels else 0
        for i, name in enumerate(labels):
            if name in existing:
                continue
            self.add_sheet_pin(sheet_name, name, "passive", sx, sy + step * (i + 1), 0)
            added.append(name)
        return {"added": added, "child_labels": labels}

    def validate_sheet_pins(self) -> List[Dict[str, Any]]:
        issues = []
        for sh in self._sheets():
            name = self._sheet_prop(sh, "Sheetname")
            pin_names = {str(p[1]) for p in find_all(sh, "pin") if len(p) > 1}
            labels = set(self.child_hier_labels(name)) if name else set()
            for missing in labels - pin_names:
                issues.append({"sheet": name, "issue": "label_without_pin", "name": missing})
            for orphan in pin_names - labels:
                issues.append({"sheet": name, "issue": "pin_without_label", "name": orphan})
        return issues

    # -- internals --------------------------------------------------------- #

    def _reference_exists(self, reference: str) -> bool:
        return any(self._property(s, "Reference") == reference for s in self._symbols())

    def _insert_before_trailer(self, node: list) -> None:
        """Insert a node before the trailing ``sheet_instances`` block.

        KiCAD keeps ``sheet_instances`` last; placing new content ahead of it
        keeps the file tidy and predictable.
        """
        insert_at = len(self.root)
        for i, child in enumerate(self.root):
            if tag_of(child) == "sheet_instances":
                insert_at = i
                break
        self.root.insert(insert_at, node)

    # -- serialization ----------------------------------------------------- #

    def to_text(self) -> str:
        return dumps(self.root, pretty=True)

    def save(self, path: Optional[os.PathLike | str] = None) -> Path:
        """Atomically write the schematic to disk."""
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path given and schematic has no associated path")
        target.parent.mkdir(parents=True, exist_ok=True)

        text = self.to_text()
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".kicad_mcp_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.path = target
        return target
