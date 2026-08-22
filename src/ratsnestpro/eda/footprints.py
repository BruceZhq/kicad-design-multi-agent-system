"""Resolve KiCAD footprints to real pad geometry.

A thin, typed adapter over the vendored ``footprint`` reader. Footprint
libraries use the standard ``<nick>.pretty/<name>.kicad_mod`` layout, which the
vendored resolver already understands via the ``KICAD_FOOTPRINT_DIR`` env var
(set by :mod:`ratsnestpro.config`). This module normalizes the vendored output
into flat ``{number, x, y, layers}`` pad dicts and exposes a footprint bounding
box for courtyard / placement checks later in the pipeline.
"""

from __future__ import annotations

import math
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from ratsnestpro.eda.vendor.footprint import (
    load_footprint_node,
    pad_offsets,
    resolve_footprint,
)
from ratsnestpro.eda.vendor.sexpr import find_all, find_first

__all__ = [
    "resolve_footprint",
    "footprint_pads",
    "footprint_bbox",
    "footprint_courtyard_bbox",
    "footprint_courtyard_rects",
    "footprint_pad_numbers",
    "invalidate_caches",
]

_PAD_CACHE_LOCK = threading.RLock()
_PAD_HEAD_RE = re.compile(
    r'\(\s*pad\s+(?:"((?:\\.|[^"\\])*)"|([^\s()]+))(?=\s)',
)


@lru_cache(maxsize=8192)
def _footprint_pads_from_path(
    path_text: str,
    modified_ns: int,
) -> tuple[tuple[str, float, float, tuple[str, ...]], ...]:
    del modified_ns  # cache-key version for edited or generated libraries
    node = load_footprint_node(path_text)
    out: list[tuple[str, float, float, tuple[str, ...]]] = []
    for pad in pad_offsets(node):
        rel = pad.get("rel", (0.0, 0.0))
        out.append(
            (
                str(pad.get("number", "")),
                float(rel[0]),
                float(rel[1]),
                tuple(str(layer) for layer in pad.get("layers", [])),
            )
        )
    return tuple(out)


@lru_cache(maxsize=8192)
def _footprint_pad_numbers_from_path(
    path_text: str,
    modified_ns: int,
) -> frozenset[str]:
    del modified_ns  # cache-key version for edited or generated libraries
    source = Path(path_text).read_text(encoding="utf-8")
    return frozenset(
        (quoted.replace(r"\"", '"').replace(r"\\", "\\") if quoted else atom)
        for quoted, atom in _PAD_HEAD_RE.findall(source)
    )


def invalidate_caches() -> None:
    """Drop parsed footprint data after an explicit workspace-library edit."""

    with _PAD_CACHE_LOCK:
        _footprint_pad_numbers_from_path.cache_clear()
        _footprint_pads_from_path.cache_clear()


def footprint_pad_numbers(lib_id: str) -> frozenset[str] | None:
    """Return only the electrical pad-number signature without parsing geometry."""

    path = resolve_footprint(lib_id)
    if not path:
        return None
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    with _PAD_CACHE_LOCK:
        return _footprint_pad_numbers_from_path(str(path), modified_ns)


def footprint_pads(lib_id: str) -> list[dict[str, Any]] | None:
    """Return pads of ``Lib:Name`` as ``{number, x, y, layers}``, or ``None``."""

    path = resolve_footprint(lib_id)
    if not path:
        return None
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    # functools.lru_cache may evaluate duplicate concurrent misses. Keeping the
    # cached call inside the lock makes a first parse single-flight.
    with _PAD_CACHE_LOCK:
        cached = _footprint_pads_from_path(str(path), modified_ns)
    out = [
        {
            "number": number,
            "x": x,
            "y": y,
            "layers": list(layers),
        }
        for number, x, y, layers in cached
    ]
    return out or None


def footprint_bbox(lib_id: str) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounding box (x1, y1, x2, y2) of a footprint's pads (mm).

    A coarse extent derived from pad centers; refined courtyard handling comes
    in the placement tasks. Returns ``None`` when the footprint is unresolved
    or has no pads.
    """
    pads = footprint_pads(lib_id)
    if not pads:
        return None
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    return (min(xs), min(ys), max(xs), max(ys))


def _xy(node: list | None) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    return float(str(node[1])), float(str(node[2]))


@lru_cache(maxsize=4096)
def _courtyard_bbox_from_path(
    path_text: str,
    modified_ns: int,
) -> tuple[float, float, float, float] | None:
    del modified_ns  # part of the cache key so an edited library is re-read
    node = load_footprint_node(path_text)
    points: list[tuple[float, float]] = []
    for tag in ("fp_line", "fp_rect", "fp_arc", "fp_poly", "fp_circle"):
        for graphic in find_all(node, tag):
            layer = find_first(graphic, "layer")
            if layer is None or len(layer) < 2:
                continue
            if str(layer[1]) not in {"F.CrtYd", "B.CrtYd"}:
                continue
            local: list[tuple[float, float]] = []
            for point_tag in ("start", "mid", "end", "center"):
                point = _xy(find_first(graphic, point_tag))
                if point is not None:
                    local.append(point)
            pts = find_first(graphic, "pts")
            if pts is not None:
                local.extend(
                    point
                    for child in find_all(pts, "xy")
                    if (point := _xy(child)) is not None
                )
            if tag == "fp_circle":
                center = _xy(find_first(graphic, "center"))
                edge = _xy(find_first(graphic, "end"))
                if center is not None and edge is not None:
                    radius = math.dist(center, edge)
                    local.extend([
                        (center[0] - radius, center[1] - radius),
                        (center[0] + radius, center[1] + radius),
                    ])
            points.extend(local)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def footprint_courtyard_bbox(
    lib_id: str,
) -> tuple[float, float, float, float] | None:
    """Return the real F/B.CrtYd extent, falling back to pad-center geometry."""
    path = resolve_footprint(lib_id)
    if path is None:
        return None
    courtyard = _courtyard_bbox_from_path(str(path), path.stat().st_mtime_ns)
    return courtyard if courtyard is not None else footprint_bbox(lib_id)


def _point_key(point: tuple[float, float]) -> tuple[int, int]:
    return round(point[0] * 1_000_000), round(point[1] * 1_000_000)


def _point_in_polygon(
    point: tuple[float, float],
    polygon: tuple[tuple[float, float], ...],
) -> bool:
    x, y = point
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing_x > x:
                inside = not inside
        previous = current
    return inside


def _line_polygons(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[tuple[float, float], ...]]:
    """Chain closed courtyard line loops without assuming source order."""

    remaining = list(segments)
    polygons: list[tuple[tuple[float, float], ...]] = []
    while remaining:
        start, end = remaining.pop(0)
        loop = [start, end]
        while _point_key(loop[-1]) != _point_key(loop[0]):
            tail = _point_key(loop[-1])
            match_index = next(
                (
                    index
                    for index, segment in enumerate(remaining)
                    if tail in {_point_key(segment[0]), _point_key(segment[1])}
                ),
                None,
            )
            if match_index is None:
                break
            left, right = remaining.pop(match_index)
            loop.append(right if _point_key(left) == tail else left)
        if len(loop) >= 4 and _point_key(loop[-1]) == _point_key(loop[0]):
            polygons.append(tuple(loop[:-1]))
    return polygons


@lru_cache(maxsize=4096)
def _courtyard_polygons_from_path(
    path_text: str,
    modified_ns: int,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    del modified_ns
    node = load_footprint_node(path_text)
    polygons: list[tuple[tuple[float, float], ...]] = []
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for graphic in find_all(node, "fp_line"):
        layer = find_first(graphic, "layer")
        if layer is None or len(layer) < 2:
            continue
        if str(layer[1]) not in {"F.CrtYd", "B.CrtYd"}:
            continue
        start = _xy(find_first(graphic, "start"))
        end = _xy(find_first(graphic, "end"))
        if start is not None and end is not None:
            segments.append((start, end))
    polygons.extend(_line_polygons(segments))
    for graphic in find_all(node, "fp_rect"):
        layer = find_first(graphic, "layer")
        if layer is None or len(layer) < 2:
            continue
        if str(layer[1]) not in {"F.CrtYd", "B.CrtYd"}:
            continue
        start = _xy(find_first(graphic, "start"))
        end = _xy(find_first(graphic, "end"))
        if start is None or end is None:
            continue
        x1, x2 = sorted((start[0], end[0]))
        y1, y2 = sorted((start[1], end[1]))
        polygons.append(((x1, y1), (x2, y1), (x2, y2), (x1, y2)))
    for graphic in find_all(node, "fp_poly"):
        layer = find_first(graphic, "layer")
        if layer is None or len(layer) < 2:
            continue
        if str(layer[1]) not in {"F.CrtYd", "B.CrtYd"}:
            continue
        pts = find_first(graphic, "pts")
        if pts is None:
            continue
        polygon = tuple(
            point
            for child in find_all(pts, "xy")
            if (point := _xy(child)) is not None
        )
        if len(polygon) >= 3:
            polygons.append(polygon)
    unique: dict[
        tuple[tuple[int, int], ...],
        tuple[tuple[float, float], ...],
    ] = {}
    for polygon in polygons:
        keys = tuple(_point_key(point) for point in polygon)
        rotations = [keys[index:] + keys[:index] for index in range(len(keys))]
        reversed_keys = tuple(reversed(keys))
        rotations.extend(
            reversed_keys[index:] + reversed_keys[:index]
            for index in range(len(reversed_keys))
        )
        unique[min(rotations)] = polygon
    return tuple(unique.values())


def _polygon_rectangles(
    polygon: tuple[tuple[float, float], ...],
) -> list[tuple[float, float, float, float]]:
    """Decompose an orthogonal polygon into non-overlapping rectangles."""

    xs = sorted({point[0] for point in polygon})
    ys = sorted({point[1] for point in polygon})
    if len(xs) < 2 or len(ys) < 2:
        return []
    rows: list[tuple[float, float, float, float]] = []
    for y1, y2 in zip(ys, ys[1:], strict=False):
        spans: list[tuple[float, float]] = []
        for x1, x2 in zip(xs, xs[1:], strict=False):
            if _point_in_polygon(((x1 + x2) / 2, (y1 + y2) / 2), polygon):
                if spans and abs(spans[-1][1] - x1) <= 1e-6:
                    spans[-1] = (spans[-1][0], x2)
                else:
                    spans.append((x1, x2))
        rows.extend((x1, y1, x2, y2) for x1, x2 in spans)
    merged: list[tuple[float, float, float, float]] = []
    for rect in rows:
        x1, y1, x2, y2 = rect
        match = next(
            (
                index
                for index, current in enumerate(merged)
                if abs(current[0] - x1) <= 1e-6
                and abs(current[2] - x2) <= 1e-6
                and abs(current[3] - y1) <= 1e-6
            ),
            None,
        )
        if match is None:
            merged.append(rect)
        else:
            current = merged[match]
            merged[match] = (current[0], current[1], current[2], y2)
    return merged


def footprint_courtyard_rects(
    lib_id: str,
) -> tuple[tuple[float, float, float, float], ...] | None:
    """Return a conservative rectangular decomposition of the real courtyard.

    Most footprints have one rectangular courtyard. RF modules and other
    edge-mounted parts can use a concave courtyard to reserve an antenna or
    connector keepout. Returning that shape as several rectangles avoids
    treating its empty concavity as occupied board area.
    """

    path = resolve_footprint(lib_id)
    if path is None:
        return None
    polygons = _courtyard_polygons_from_path(
        str(path),
        path.stat().st_mtime_ns,
    )
    rectangles: list[tuple[float, float, float, float]] = []
    for polygon in polygons:
        decomposed = _polygon_rectangles(polygon)
        if not decomposed:
            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            decomposed = [(min(xs), min(ys), max(xs), max(ys))]
        rectangles.extend(decomposed)
    if rectangles:
        return tuple(dict.fromkeys(rectangles))
    bbox = footprint_courtyard_bbox(lib_id)
    return (bbox,) if bbox is not None else None


def footprint_path(lib_id: str) -> Path | None:
    """Resolve ``Lib:Name`` to its ``.kicad_mod`` path, or ``None``."""
    return resolve_footprint(lib_id)


def _demo(argv: list[str]) -> int:  # pragma: no cover - CLI convenience
    from ratsnestpro import config

    if not argv:
        cap = config.process_capability()
        print(f"process: {cap.fab_house} / {cap.profile}")
        print(f"  min_track_width = {cap.min_track_width} mm")
        print(f"  min_clearance   = {cap.min_clearance} mm")
        print(f"  min_via_diameter= {cap.min_via_diameter} mm")
        print("usage: python -m ratsnestpro.eda.footprints <Lib:Name> [...]")
        return 0
    rc = 0
    for lib_id in argv:
        pads = footprint_pads(lib_id)
        if pads is None:
            print(f"{lib_id}: NOT FOUND")
            rc = 1
            continue
        print(f"{lib_id}  ({len(pads)} pads)  <- {footprint_path(lib_id)}")
        for p in pads[:8]:
            print(f"  pad {p['number']:>4} @ ({p['x']:.3f}, {p['y']:.3f})  {p['layers']}")
        if len(pads) > 8:
            print(f"  ... (+{len(pads) - 8} more)")
    return rc


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_demo(sys.argv[1:]))
