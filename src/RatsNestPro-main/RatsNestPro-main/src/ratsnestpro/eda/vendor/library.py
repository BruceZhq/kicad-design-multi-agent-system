"""Create, edit, list and register KiCAD symbol/footprint libraries.

Footprints are ``.kicad_mod`` files (one ``(footprint ...)`` each), grouped in
``<name>.pretty`` directories. Symbols live together in a ``<name>.kicad_sym``
file. Libraries are made visible to KiCAD by an entry in a ``fp-lib-table`` /
``sym-lib-table`` (global, in the KiCAD config dir, or per-project).

All writes are atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .footprint import footprint_roots, load_footprint_node, pad_offsets, resolve_footprint
from .sexpr import Atom, dumps, find_all, find_first, loads, tag_of
from .symbol_lib import resolve_symbol_library, symbol_pins, symbol_roots


def _n(v: float) -> Atom:
    from .pcb import _fmt
    return Atom(_fmt(v))


def _sym(tag: str, *children):
    return [Atom(tag), *children]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".kicad_mcp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Library tables
# --------------------------------------------------------------------------- #

def _kicad_config_dir() -> Optional[Path]:
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) / "kicad" if base else None
    else:
        root = Path.home() / ".config" / "kicad"
    if not root or not root.exists():
        return None
    # Pick the highest versioned sub-dir if present.
    versions = sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)
    return versions[0] if versions else root


def table_path(kind: str, project_dir: Optional[str] = None) -> Path:
    """``kind`` is 'fp' or 'sym'. Returns the table file path."""
    fname = "fp-lib-table" if kind == "fp" else "sym-lib-table"
    if project_dir:
        return Path(project_dir) / fname
    cfg = _kicad_config_dir()
    return (cfg / fname) if cfg else Path.home() / f".kicad_mcp_{fname}"


def read_lib_table(kind: str, project_dir: Optional[str] = None) -> List[Dict[str, str]]:
    p = table_path(kind, project_dir)
    if not p.exists():
        return []
    node = loads(p.read_text(encoding="utf-8"))
    out = []
    for lib in find_all(node, "lib"):
        entry = {}
        for field in ("name", "type", "uri", "options", "descr"):
            f = find_first(lib, field)
            if f and len(f) > 1:
                entry[field] = str(f[1])
        out.append(entry)
    return out


def register_library(kind: str, name: str, uri: str, lib_type: str = "KiCad",
                     project_dir: Optional[str] = None) -> Dict[str, Any]:
    p = table_path(kind, project_dir)
    root_tag = "fp_lib_table" if kind == "fp" else "sym_lib_table"
    if p.exists():
        node = loads(p.read_text(encoding="utf-8"))
    else:
        node = _sym(root_tag, _sym("version", Atom("7")))
    # Replace an existing entry of the same name.
    for lib in list(find_all(node, "lib")):
        nm = find_first(lib, "name")
        if nm and len(nm) > 1 and str(nm[1]) == name:
            node.remove(lib)
    node.append(_sym("lib", _sym("name", name), _sym("type", lib_type),
                     _sym("uri", uri), _sym("options", ""), _sym("descr", "")))
    _atomic_write(p, dumps(node, pretty=True))
    return {"table": str(p), "name": name, "uri": uri}


# --------------------------------------------------------------------------- #
# Footprints
# --------------------------------------------------------------------------- #

def create_footprint(pretty_dir: str, name: str, pads: List[Dict[str, Any]],
                     descr: str = "", *, body_width_mm: Optional[float] = None,
                     body_height_mm: Optional[float] = None,
                     courtyard_clearance_mm: float = 0.25,
                     mount_type: str = "smd") -> str:
    fp = _sym("footprint", name, _sym("layer", "F.Cu"))
    if descr:
        fp.append(_sym("descr", descr))
    fp.append(_sym("attr", Atom("through_hole" if mount_type == "tht" else "smd")))
    if body_width_mm is not None and body_height_mm is not None:
        half_w = body_width_mm / 2
        half_h = body_height_mm / 2
        fp.append(
            _sym(
                "fp_rect",
                _sym("start", _n(-half_w), _n(-half_h)),
                _sym("end", _n(half_w), _n(half_h)),
                _sym("stroke", _sym("width", _n(0.1)), _sym("type", Atom("default"))),
                _sym("fill", Atom("none")),
                _sym("layer", "F.Fab"),
            )
        )
        court_w = half_w + courtyard_clearance_mm
        court_h = half_h + courtyard_clearance_mm
        fp.append(
            _sym(
                "fp_rect",
                _sym("start", _n(-court_w), _n(-court_h)),
                _sym("end", _n(court_w), _n(court_h)),
                _sym("stroke", _sym("width", _n(0.05)), _sym("type", Atom("default"))),
                _sym("fill", Atom("none")),
                _sym("layer", "F.CrtYd"),
            )
        )
    for pad in pads:
        shape = pad.get("shape", "rect")
        ptype = pad.get("type", "smd")
        layers = pad.get("layers", ["F.Cu", "F.Paste", "F.Mask"])
        children = [
            _sym("at", _n(pad["x"]), _n(pad["y"])),
            _sym("size", _n(pad.get("size_x", 1.0)), _n(pad.get("size_y", 1.0))),
        ]
        if pad.get("drill") is not None:
            children.append(_sym("drill", _n(pad["drill"])))
        children.append(_sym("layers", *layers))
        fp.append(
            _sym(
                "pad",
                str(pad["number"]),
                Atom(ptype),
                Atom(shape),
                *children,
            )
        )
    path = Path(pretty_dir)
    path.mkdir(parents=True, exist_ok=True)
    out = path / f"{name}.kicad_mod"
    _atomic_write(out, dumps(fp, pretty=True))
    return str(out)


def edit_footprint_pad(mod_path: str, pad_number: str, **changes) -> bool:
    node = load_footprint_node(mod_path)
    changed = False
    for pad in find_all(node, "pad"):
        if len(pad) > 1 and str(pad[1]) == str(pad_number):
            if "x" in changes and "y" in changes:
                for i, c in enumerate(pad):
                    if tag_of(c) == "at":
                        pad[i] = _sym("at", _n(changes["x"]), _n(changes["y"]))
            if "size_x" in changes and "size_y" in changes:
                for i, c in enumerate(pad):
                    if tag_of(c) == "size":
                        pad[i] = _sym("size", _n(changes["size_x"]), _n(changes["size_y"]))
            if "shape" in changes and len(pad) > 3:
                pad[3] = Atom(changes["shape"])
            changed = True
    if changed:
        _atomic_write(Path(mod_path), dumps(node, pretty=True))
    return changed


def footprint_info(mod_path: str) -> Dict[str, Any]:
    node = load_footprint_node(mod_path)
    descr = find_first(node, "descr")
    return {
        "name": str(node[1]) if len(node) > 1 else None,
        "descr": str(descr[1]) if descr and len(descr) > 1 else None,
        "pads": pad_offsets(node),
        "pad_count": len(find_all(node, "pad")),
    }


def list_pretty(pretty_dir: str) -> List[str]:
    p = Path(pretty_dir)
    if not p.exists():
        return []
    return sorted(f.stem for f in p.glob("*.kicad_mod"))


def search_footprints(query: str, limit: int = 50) -> List[Dict[str, str]]:
    q = query.lower()
    hits = []
    for root in footprint_roots():
        for mod in root.glob("*.pretty/*.kicad_mod"):
            if q in mod.stem.lower():
                lib = mod.parent.stem
                hits.append({"lib_id": f"{lib}:{mod.stem}", "path": str(mod)})
                if len(hits) >= limit:
                    return hits
    return hits


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #

def _blank_symbol_lib() -> list:
    return _sym("kicad_symbol_lib", _sym("version", Atom("20231120")),
                _sym("generator", "kicad-mcp-py"))


def create_symbol(lib_path: str, name: str, pins: List[Dict[str, Any]],
                  properties: Optional[Dict[str, str]] = None, *,
                  body_width: Optional[float] = None,
                  body_height: Optional[float] = None) -> str:
    p = Path(lib_path)
    if p.exists():
        node = loads(p.read_text(encoding="utf-8"))
    else:
        node = _blank_symbol_lib()
    # Remove an existing symbol of the same name.
    for sym in list(find_all(node, "symbol")):
        if len(sym) > 1 and str(sym[1]) == name:
            node.remove(sym)
    sym = _sym("symbol", name,
               _sym("in_bom", Atom("yes")), _sym("on_board", Atom("yes")))
    for k, v in (properties or {}).items():
        sym.append(_sym("property", k, v, _sym("at", _n(0), _n(0), _n(0))))
    unit = _sym("symbol", f"{name}_1_1")
    if body_width is not None and body_height is not None:
        unit.append(
            _sym(
                "rectangle",
                _sym("start", _n(-body_width / 2), _n(body_height / 2)),
                _sym("end", _n(body_width / 2), _n(-body_height / 2)),
                _sym("stroke", _sym("width", _n(0)), _sym("type", Atom("default"))),
                _sym("fill", _sym("type", Atom("background"))),
            )
        )
    for pin in pins:
        unit.append(_sym("pin", Atom(pin.get("type", "passive")), Atom(pin.get("style", "line")),
                         _sym("at", _n(pin.get("x", 0)), _n(pin.get("y", 0)),
                              _n(pin.get("angle", 0))),
                         _sym("length", _n(pin.get("length", 2.54))),
                         _sym("name", pin.get("name", "~")),
                         _sym("number", str(pin["number"]))))
    sym.append(unit)
    node.append(sym)
    _atomic_write(p, dumps(node, pretty=True))
    return str(p)


def delete_symbol(lib_path: str, name: str) -> bool:
    p = Path(lib_path)
    if not p.exists():
        return False
    node = loads(p.read_text(encoding="utf-8"))
    removed = False
    for sym in list(find_all(node, "symbol")):
        if len(sym) > 1 and str(sym[1]) == name:
            node.remove(sym)
            removed = True
    if removed:
        _atomic_write(p, dumps(node, pretty=True))
    return removed


def list_symbols(lib_path: str) -> List[str]:
    p = Path(lib_path)
    if not p.exists():
        return []
    node = loads(p.read_text(encoding="utf-8"))
    # Top-level symbols only (skip unit sub-symbols which contain '_').
    return [str(s[1]) for s in find_all(node, "symbol") if len(s) > 1]


def symbol_info(lib_id: str) -> Optional[Dict[str, Any]]:
    pins = symbol_pins(lib_id)
    if pins is None:
        return None
    return {"lib_id": lib_id, "pins": pins, "pin_count": len(pins)}


def search_symbols(query: str, limit: int = 50) -> List[Dict[str, str]]:
    q = query.lower()
    hits = []
    for root in symbol_roots():
        for lib in root.glob("*.kicad_sym"):
            try:
                node = loads(lib.read_text(encoding="utf-8"))
            except Exception:
                continue
            for s in find_all(node, "symbol"):
                if len(s) > 1 and q in str(s[1]).lower():
                    hits.append({"lib_id": f"{lib.stem}:{s[1]}", "path": str(lib)})
                    if len(hits) >= limit:
                        return hits
    return hits
