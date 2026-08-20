"""Read KiCAD footprint files (``.kicad_mod``) and resolve library ids.

A ``.kicad_mod`` file is a single ``(footprint ...)`` S-expression holding pad
and graphic definitions with positions *relative* to the footprint origin.
Placing a footprint on a board means embedding those pads/graphics into the
board's footprint node; KiCAD then renders and nets them relative to the
footprint's placement and orientation.

Library ids look like ``Resistor_SMD:R_0603_1608Metric`` — the part before the
colon is a library nickname (a ``<nick>.pretty`` directory) and the part after
is the footprint (a ``<name>.kicad_mod`` file inside it).

This is an original implementation over the public KiCAD footprint format.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ratsnestpro.eda.library_roots import generated_library_roots

from .sexpr import Atom, Node, find_all, find_first, loads, tag_of

# Where to look for ``*.pretty`` footprint libraries, in priority order.
_WIN_ROOTS = [
    r"C:\Program Files\KiCad\9.0\share\kicad\footprints",
    r"C:\Program Files\KiCad\8.0\share\kicad\footprints",
    r"C:\Program Files\KiCad\7.0\share\kicad\footprints",
]
_POSIX_ROOTS = [
    "/usr/share/kicad/footprints",
    "/usr/local/share/kicad/footprints",
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
]


def footprint_roots() -> List[Path]:
    """Directories that contain ``*.pretty`` libraries."""
    roots: List[Path] = list(generated_library_roots())
    env = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p)
    from .kicad_paths import footprint_dirs
    roots.extend(footprint_dirs())
    candidates = _WIN_ROOTS if os.name == "nt" else _POSIX_ROOTS
    roots.extend(Path(c) for c in candidates)
    return [r for r in roots if r.exists()]


def resolve_footprint(lib_id: str) -> Optional[Path]:
    """Resolve ``Lib:Name`` to a ``.kicad_mod`` path, if it can be found."""
    if ":" not in lib_id:
        return None
    nick, name = lib_id.split(":", 1)
    for root in footprint_roots():
        candidate = root / f"{nick}.pretty" / f"{name}.kicad_mod"
        if candidate.exists():
            return candidate
    # Fall back to a name-only search across all libraries.
    for root in footprint_roots():
        for hit in root.glob(f"*.pretty/{name}.kicad_mod"):
            return hit
    return None


def load_footprint_node(path: os.PathLike | str) -> list:
    node = loads(Path(path).read_text(encoding="utf-8"))
    if tag_of(node) != "footprint":
        raise ValueError(f"{path} is not a (footprint ...) file")
    return node


# Child tags we copy from a .kicad_mod into the board footprint (everything that
# defines the physical part). We drop the library's own placement/metadata.
_EMBED_TAGS = {
    "pad", "fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_text",
    "fp_curve", "model", "zone", "group",
}


def embeddable_children(fp_node: list) -> List[Node]:
    """Return the pad/graphic children to copy into a board footprint."""
    out: List[Node] = []
    for child in fp_node[1:]:
        if isinstance(child, list) and tag_of(child) in _EMBED_TAGS:
            out.append(child)
    return out


def pad_offsets(fp_node: list) -> List[Dict[str, Any]]:
    """Extract each pad's number, relative position and layers."""
    pads = []
    for pad in find_all(fp_node, "pad"):
        if len(pad) < 2:
            continue
        number = str(pad[1])
        at = find_first(pad, "at")
        layers = find_first(pad, "layers")
        pads.append({
            "number": number,
            "rel": (Atom(str(at[1])).as_float() if at else 0.0,
                    Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0),
            "layers": [str(x) for x in layers[1:]] if layers else [],
        })
    return pads


def rotate_offset(px: float, py: float, angle_deg: float) -> Tuple[float, float]:
    """Rotate a pad offset by the footprint orientation (KiCAD convention).

    KiCAD's footprint rotation is applied with the board's Y axis pointing
    down; a positive angle rotates the offset clockwise on screen.
    """
    rot = math.radians(angle_deg)
    cos_r, sin_r = math.cos(rot), math.sin(rot)
    ax = px * cos_r - py * sin_r
    ay = px * sin_r + py * cos_r
    return ax, ay
