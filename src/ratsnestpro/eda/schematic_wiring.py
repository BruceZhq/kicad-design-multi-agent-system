"""Conservative orthogonal sheet wiring over real symbol pin coordinates.

Cross-net intersections (including T junctions) are forbidden. If there is no
safe local path, ordinary KiCad labels retain connectivity and the receipt
explicitly records the fallback. This is a drawing policy, not an ERC waiver.
"""

from __future__ import annotations

Point = tuple[float, float]
Segment = tuple[Point, Point]


def on_segment(point: Point, segment: Segment) -> bool:
    a, b = segment
    return (min(a[0], b[0]) - 1e-6 <= point[0] <= max(a[0], b[0]) + 1e-6
            and min(a[1], b[1]) - 1e-6 <= point[1] <= max(a[1], b[1]) + 1e-6)


def intersects(first: Segment, second: Segment) -> bool:
    a, b = first
    c, d = second
    return (max(min(a[0], b[0]), min(c[0], d[0]))
            <= min(max(a[0], b[0]), max(c[0], d[0])) + 1e-6
            and max(min(a[1], b[1]), min(c[1], d[1]))
            <= min(max(a[1], b[1]), max(c[1], d[1])) + 1e-6)


def local_path(start: Point, end: Point, *, forbidden_pins: list[Point],
               forbidden_wires: list[Segment], bodies: list[tuple[float, float, float, float]]) -> list[Segment] | None:
    candidates = [[start, (end[0], start[1]), end], [start, (start[0], end[1]), end]]
    for margin in (2.54, 5.08, 10.16):
        for y in (min(start[1], end[1]) - margin, max(start[1], end[1]) + margin):
            candidates.append([start, (start[0], y), (end[0], y), end])
        for x in (min(start[0], end[0]) - margin, max(start[0], end[0]) + margin):
            candidates.append([start, (x, start[1]), (x, end[1]), end])
    candidates.sort(key=lambda points: sum(abs(a[0]-b[0])+abs(a[1]-b[1])
                                           for a, b in zip(points, points[1:])))
    for points in candidates:
        segments = [(a, b) for a, b in zip(points, points[1:]) if a != b]
        if any(on_segment(pin, seg) for seg in segments for pin in forbidden_pins):
            continue
        if any(intersects(seg, wire) for seg in segments for wire in forbidden_wires):
            continue
        if any(max(min(a[0], b[0]), x1) < min(max(a[0], b[0]), x2) + 1e-6
               and max(min(a[1], b[1]), y1) < min(max(a[1], b[1]), y2) + 1e-6
               for a, b in segments for x1, y1, x2, y2 in bodies):
            continue
        return segments
    return None


def draw_local_nets(doc, coordinates: dict[str, list[Point]], *, label_nets: set[str],
                    all_pins: list[Point], bodies: list[tuple[float, float, float, float]]) -> dict:
    wires: list[tuple[str, Segment]] = []
    receipt: dict = {"wired_nets": [], "label_nets": sorted(label_nets), "label_fallbacks": []}
    # Pin labels remain as explicit net names; real wires are added only where safe.
    for name, pins in coordinates.items():
        if name in label_nets or len(pins) < 2:
            continue
        pending = list(dict.fromkeys(pins))
        tree = [pending.pop(0)]
        complete = True
        while pending:
            _, start, end = min((abs(a[0]-b[0])+abs(a[1]-b[1]), a, b)
                                for a in tree for b in pending)
            path = local_path(start, end,
                              forbidden_pins=[p for p in all_pins if p not in pins],
                              forbidden_wires=[s for n, s in wires if n != name], bodies=bodies)
            if path is None:
                complete = False
            else:
                for a, b in path:
                    doc.add_wire(*a, *b)
                    wires.append((name, (a, b)))
                for point in (start, end):
                    doc.add_junction(*point)
            tree.append(end)
            pending.remove(end)
        receipt["wired_nets" if complete else "label_fallbacks"].append(name)
    receipt["wire_count"] = len(wires)
    return receipt
