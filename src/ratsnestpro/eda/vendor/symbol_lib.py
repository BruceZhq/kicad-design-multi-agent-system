"""Read KiCAD symbol libraries (``.kicad_sym``) for pin geometry.

Resolves a schematic ``lib_id`` (``Device:R``) to its symbol definition and
extracts pin connection points. Pin coordinates in a library are in the
symbol's own frame (Y axis up); placing the symbol in a schematic applies the
instance position, rotation and mirror, and flips Y (schematic Y is down).

The placement transform is a best-effort reproduction of KiCAD's convention.
It is exact for unrotated, unmirrored parts and correct for the common
0/90/180/270 rotations; unusual mirror+rotation combinations may differ.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .sexpr import Atom, find_all, find_first, loads, tag_of

_WIN_ROOTS = [
    r"C:\Program Files\KiCad\9.0\share\kicad\symbols",
    r"C:\Program Files\KiCad\8.0\share\kicad\symbols",
    r"C:\Program Files\KiCad\7.0\share\kicad\symbols",
]
_POSIX_ROOTS = [
    "/usr/share/kicad/symbols",
    "/usr/local/share/kicad/symbols",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
]


def symbol_roots() -> List[Path]:
    roots: List[Path] = []
    env = os.environ.get("KICAD_SYMBOL_DIR")
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p)
    from .kicad_paths import symbol_dirs
    roots.extend(symbol_dirs())
    roots.extend(Path(c) for c in (_WIN_ROOTS if os.name == "nt" else _POSIX_ROOTS))
    return [r for r in roots if r.exists()]


def resolve_symbol_library(nick: str) -> Optional[Path]:
    for root in symbol_roots():
        cand = root / f"{nick}.kicad_sym"
        if cand.exists():
            return cand
    return None


def _find_symbol_def(lib_node: list, name: str) -> Optional[list]:
    for sym in find_all(lib_node, "symbol"):
        if len(sym) > 1 and str(sym[1]) == name:
            return sym
    return None


def _pins_in(symbol: list) -> List[Dict[str, Any]]:
    """Collect pins from a symbol and its unit sub-symbols."""
    pins: List[Dict[str, Any]] = []
    for pin in find_all(symbol, "pin"):
        at = find_first(pin, "at")
        num = find_first(pin, "number")
        name = find_first(pin, "name")
        etype = str(pin[1]) if len(pin) > 1 else "passive"
        pins.append({
            "number": str(num[1]) if num and len(num) > 1 else "",
            "name": str(name[1]) if name and len(name) > 1 else "",
            "type": etype,
            "x": Atom(str(at[1])).as_float() if at else 0.0,
            "y": Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0,
            "angle": Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0,
        })
    for sub in find_all(symbol, "symbol"):  # unit sub-symbols
        pins.extend(_pins_in(sub))
    return pins


def symbol_pins(lib_id: str) -> Optional[List[Dict[str, Any]]]:
    """Return the pins (local coords) for a lib_id, or None if unresolved.

    Follows KiCAD ``(extends "base")`` inheritance: derived parts (e.g.
    derived device variants) carry no pins of their own and inherit them from a base
    symbol in the same library.
    """
    if ":" not in lib_id:
        return None
    nick, name = lib_id.split(":", 1)
    lib_path = resolve_symbol_library(nick)
    if not lib_path:
        return None
    lib_node = loads(lib_path.read_text(encoding="utf-8"))
    if tag_of(lib_node) != "kicad_symbol_lib":
        return None

    def pins_for(sym_name: str, depth: int = 0) -> List[Dict[str, Any]]:
        if depth > 6:
            return []
        sym = _find_symbol_def(lib_node, sym_name)
        if sym is None:
            return []
        pins = _pins_in(sym)
        if not pins:
            ext = find_first(sym, "extends")
            if ext and len(ext) > 1:
                return pins_for(str(ext[1]), depth + 1)
        return pins

    pins = pins_for(name)
    if not pins:
        return None
    seen = {}
    for p in pins:
        if p["number"] and p["number"] not in seen:
            seen[p["number"]] = p
    return list(seen.values())


def transform_pin(px: float, py: float, rot: float, mirror: Optional[str],
                  lx: float, ly: float) -> tuple:
    """Map a library-local pin (lx, ly) to schematic space."""
    x, y = lx, -ly  # symbol Y-up → schematic Y-down
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    r = math.radians(rot)
    rx = x * math.cos(r) - y * math.sin(r)
    ry = x * math.sin(r) + y * math.cos(r)
    return round(px + rx, 4), round(py + ry, 4)
