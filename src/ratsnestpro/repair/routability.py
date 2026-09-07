"""Geometry-driven congestion groups and escape observations, not release gates."""

from __future__ import annotations

import math
from typing import Any, Callable


def probe(
    board: Any,
    *,
    clear_segment: Callable[[str, tuple, tuple, str, float], bool],
    width: float = 0.2,
    max_groups: int = 32,
) -> dict[str, Any]:
    groups = []
    enclosed = []
    for fp in board.list_footprints():
        pads = [p for p in board.footprint_pads(fp["reference"]) if p["type"] == "smd" and p["net"]]
        center = (fp["at"]["x"], fp["at"]["y"])
        observations = []
        for pad in pads:
            origin = (pad["x"], pad["y"])
            layer = next((l for l in pad["layers"] if l in {"F.Cu", "B.Cu"}), None)
            if not layer:
                continue
            dx, dy = origin[0] - center[0], origin[1] - center[1]
            axis = (1 if dx >= 0 else -1, 0) if abs(dx) > abs(dy) else (0, 1 if dy >= 0 else -1)
            exits = []
            for direction in (axis, (-axis[0], -axis[1])):
                for distance in (0.8, 1.2, 1.8, 2.4):
                    end = (origin[0] + direction[0] * distance, origin[1] + direction[1] * distance)
                    if clear_segment(pad["net"], origin, end, layer, width):
                        exits.append({"end": end, "layer": layer})
            observation = {
                "ref": fp["reference"],
                "pad": pad["number"],
                "net": pad["net"],
                "position": origin,
                "escape_candidates": exits,
            }
            observations.append(observation)
            if not exits:
                enclosed.append(observation)
        # Connected components of neighboring pads, rather than independent
        # pair fixes that reserve the same via location more than once.
        pending = set(range(len(observations)))
        while pending:
            component = {pending.pop()}
            while True:
                neighbors = {
                    j
                    for j in pending
                    if any(
                        math.dist(observations[i]["position"], observations[j]["position"]) <= 1.05
                        for i in component
                    )
                }
                if not neighbors:
                    break
                component.update(neighbors)
                pending.difference_update(neighbors)
            if len(component) > 1:
                groups.append(
                    {
                        "reference": fp["reference"],
                        "pads": [observations[i] for i in sorted(component)],
                        "strategy": "reserve joint escapes before trunks; stagger vias",
                    }
                )
    return {
        "schema_version": 1,
        "joint_groups": groups[:max_groups],
        "enclosed_pads": enclosed[:64],
        "advisory": True,
        "limitation": "geometric candidates require exact pad/via DRC and functional placement validation",
    }


def write_preflight(pcb_path, *, clearance, width):
    """Persist congestion evidence before the first full-board router invocation."""
    import hashlib
    import json
    from pathlib import Path
    from ratsnestpro.eda.vendor.pcb import PcbBoard
    from ratsnestpro.orchestration.pipeline import _copper_obstacles, _segment_distance

    path = Path(pcb_path)
    board = PcbBoard.load(path)
    cached = {}

    def clear(net, a, b, layer, line_width):
        if (net, layer) not in cached:
            cached[(net, layer)] = _copper_obstacles(board, net_name=net, layer=layer)
        return all(
            _segment_distance(a, b, item.start, item.end)
            >= item.radius + line_width / 2 + clearance
            for item in cached[(net, layer)]
        )

    evidence = {
        "pcb_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        **probe(board, clear_segment=clear, width=width),
    }
    output = path.with_suffix(".routability.json")
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence
