"""Bounded joint escape assignment; geometry callbacks keep tool dependencies out.

Plans reserve traces AND via disks together. They are candidate programs, never
a substitute for KiCad DRC. The executor helper edits only its supplied copy.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path


def solve(
    pads,
    *,
    clear_segment,
    clear_via,
    segment_distance,
    width=0.2,
    via_diameter=0.7,
    via_drill=0.3,
    clearance=0.2,
    max_nodes=2000,
):
    if not 1 <= len(pads) <= 8:
        return {"status": "unresolved", "reason": "select one coupled group of 1–8 pads"}
    options = []
    for pad in pads:
        choices = []
        for exit in pad["escape_candidates"]:
            start, end = tuple(pad["position"]), tuple(exit["end"])
            if clear_segment(pad["net"], start, end, exit["layer"], width) and clear_via(
                pad["net"], end, via_diameter
            ):
                choices.append(
                    {
                        "ref": pad["ref"],
                        "pad": pad["pad"],
                        "net": pad["net"],
                        "start": start,
                        "end": end,
                        "layer": exit["layer"],
                        "width": width,
                        "via_diameter": via_diameter,
                        "via_drill": via_drill,
                    }
                )
        options.append(sorted(choices, key=lambda x: math.dist(x["start"], x["end"])))
    options.sort(key=len)  # Most constrained pin first, but reserve the group atomically.
    visited = 0

    def compatible(a, b):
        if a["net"] == b["net"]:
            return True
        if math.dist(a["end"], b["end"]) < via_diameter + clearance:
            return False
        if (
            a["layer"] == b["layer"]
            and segment_distance(a["start"], a["end"], b["start"], b["end"]) < width + clearance
        ):
            return False
        return (
            segment_distance(a["end"], a["end"], b["start"], b["end"])
            >= via_diameter / 2 + width / 2 + clearance
            and segment_distance(b["end"], b["end"], a["start"], a["end"])
            >= via_diameter / 2 + width / 2 + clearance
        )

    def search(chosen):
        nonlocal visited
        if len(chosen) == len(options):
            return chosen
        for candidate in options[len(chosen)]:
            visited += 1
            if visited > max_nodes:
                return None
            if all(compatible(candidate, other) for other in chosen):
                solution = search([*chosen, candidate])
                if solution is not None:
                    return solution
        return None

    result = search([])
    return {
        "status": "candidate" if result else "unresolved",
        "actions": result or [],
        "searched_nodes": visited,
        "requires_exact_drc": True,
    }


def apply(pcb_path, plan, *, expected_sha256):
    """Callable from isolated Python; no implicit rip-up, net reassignment or release."""
    from ratsnestpro.eda.vendor.pcb import PcbBoard

    path = Path(pcb_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("joint escape plan is stale")
    if plan.get("status") != "candidate" or not 1 <= len(plan.get("actions", [])) <= 8:
        raise ValueError("a bounded solved candidate is required")
    board = PcbBoard.load(path)
    for item in plan["actions"]:
        pad = board.pad_position(item["ref"], item["pad"])
        if (
            not pad
            or pad["net"] != item["net"]
            or math.dist((pad["x"], pad["y"]), item["start"]) > 0.001
        ):
            raise ValueError("pad identity or position differs from the plan")
        board.add_track(
            *item["start"], *item["end"], width=item["width"], layer=item["layer"], net=item["net"]
        )
        board.add_via(
            *item["end"], size=item["via_diameter"], drill=item["via_drill"], net=item["net"]
        )
    board.save(path)
