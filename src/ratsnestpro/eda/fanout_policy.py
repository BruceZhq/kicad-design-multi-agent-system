"""Operator-approved, physically bounded pad neckdowns; never a net-wide waiver.

Approvals live outside the CAD workspace so a model's candidate file edits cannot
grant themselves a width exception. The original requirement digest must match.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path


def pad_escape_axis(size: tuple[float, float], angle_degrees: float,
                    outward: tuple[float, float]) -> tuple[float, float]:
    """Major pad axis in KiCad board coordinates, oriented away from the body."""
    angle = math.radians(angle_degrees)
    axis = ((math.cos(angle), -math.sin(angle)) if size[0] >= size[1]
            else (math.sin(angle), math.cos(angle)))
    sign = -1 if sum(a * b for a, b in zip(axis, outward)) < 0 else 1
    return axis[0] * sign, axis[1] * sign


def load_fanout_approval(pcb_path: Path, source_digest: str) -> dict:
    workspace = Path(pcb_path).resolve().parent
    if workspace.parent.name != "runs":
        return {}
    path = workspace.parent.parent / "engineering-approvals" / f"{workspace.name}.fanout.json"
    try:
        raw = path.read_bytes()
        approval = json.loads(raw)
        if (approval.get("schema") != "ratsnest.fanout-approval.v1"
                or approval.get("workspace") != workspace.name
                or approval.get("requirement_digest") != source_digest
                or not approval.get("authorized_by")
                or not approval.get("authorization_text")
                or approval.get("scope") != "power"
                or approval.get("length_basis", "component_total") not in {"component_total", "pad_to_trunk_path"}
                or not 0.20 <= float(approval["minimum_width_mm"]) <= 0.40
                or not 0 < float(approval["max_chain_length_mm"]) <= 2.0):
            return {}
        return {**approval, "receipt_digest": hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def approved_fanout_tracks(board, approval: dict, required_width) -> set[str]:
    """Prove bounded pad-owned copper trees connected to a wide trunk.

    Legacy approvals cap the total component length. Explicit shared-escape
    approvals require every thin edge to belong to a bounded pad-to-trunk path.
Via/plane termination is deliberately not exempted: a same-layer wide landing
is required. ERC, DRC and full-board connectivity remain separate hard gates.
    """
    if not approval:
        return set()
    minimum = float(approval["minimum_width_mm"])
    maximum_length = float(approval["max_chain_length_mm"])
    tracks = board.list_tracks()

    def key(point):
        return tuple(round(float(v), 4) for v in point)

    thin = {t["uuid"]: t for t in tracks
            if t.get("uuid") and t.get("width") is not None
            and t["width"] + 1e-6 < required_width(t["net_name"])}
    # Same-net copper intersections are legal to KiCad DRC, but invalidate
    # our simple chain proof. Reject unmodelled contacts rather than counting
    # two crossing short chains as independent exemptions.
    def point_distance(p, a, b):
        delta = [b[i] - a[i] for i in (0, 1)]
        length2 = sum(v * v for v in delta)
        t = max(0, min(1, sum((p[i] - a[i]) * delta[i] for i in (0, 1)) / length2)) if length2 else 0
        return math.dist(p, [a[i] + t * delta[i] for i in (0, 1)])

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    tainted = set()
    items = list(thin.items())
    for index, (left_id, left) in enumerate(items):
        for right_id, right in items[index + 1:]:
            if (left["net"], left["layer"]) != (right["net"], right["layer"]):
                continue
            a, b, c, d = left["start"], left["end"], right["start"], right["end"]
            if {key(a), key(b)} & {key(c), key(d)}:
                continue  # Accounted for by the endpoint graph below.
            distance = min(point_distance(a, c, d), point_distance(b, c, d),
                           point_distance(c, a, b), point_distance(d, a, b))
            crossing = cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0
            if crossing or distance < (left["width"] + right["width"]) / 2 + 1e-6:
                tainted.update((left_id, right_id))
    adjacency = {}
    for uid, track in thin.items():
        for point in (track["start"], track["end"]):
            adjacency.setdefault((track["net"], track["layer"], key(point)), set()).add(uid)
    pads = {(p["net_index"], layer, key((p["x"], p["y"])))
            for fp in board.list_footprints()
            for p in board.footprint_pads(fp["reference"])
            if p["type"] == "smd"
            for layer in p["layers"] if layer.endswith(".Cu")}
    wide_ends = {(t["net"], t["layer"], key(p))
                 for t in tracks if t.get("width") is not None
                 and t["width"] + 1e-6 >= required_width(t["net_name"])
                 for p in (t["start"], t["end"])}
    accepted, visited = set(), set()
    for seed in thin:
        if seed in visited:
            continue
        pending, component, degree = [seed], set(), {}
        while pending:
            uid = pending.pop()
            if uid in component:
                continue
            component.add(uid)
            track = thin[uid]
            for point in (track["start"], track["end"]):
                endpoint = (track["net"], track["layer"], key(point))
                degree[endpoint] = degree.get(endpoint, 0) + 1
                pending.extend(adjacency[endpoint] - component)
        visited.update(component)
        ends = [p for p, count in degree.items() if count == 1]
        length = sum(math.dist(thin[u]["start"], thin[u]["end"]) for u in component)
        if (len(component) != len(degree) - 1
                or component & tainted
                or any(thin[u]["width"] + 1e-6 < minimum for u in component)):
            continue
        if approval.get("length_basis") == "pad_to_trunk_path":
            graph = {}
            for uid in component:
                track = thin[uid]
                a, b = [(track["net"], track["layer"], key(point))
                        for point in (track["start"], track["end"])]
                distance = math.dist(track["start"], track["end"])
                graph.setdefault(a, []).append((b, uid, distance))
                graph.setdefault(b, []).append((a, uid, distance))
            covered = set()
            for pad in set(degree) & pads:
                pending_paths = [(pad, None, 0.0, ())]
                while pending_paths:
                    node, previous, distance, path = pending_paths.pop()
                    if node in wide_ends:
                        covered.update(path)
                        continue
                    for other, uid, edge_length in graph[node]:
                        if other != previous and distance + edge_length <= maximum_length + 1e-6:
                            pending_paths.append((other, node, distance + edge_length, (*path, uid)))
            if covered == component:
                accepted.update(component)
            continue
        if length > maximum_length + 1e-6:
            continue
        # Adjacent power pads may share a short escape. Joining two validated
        # escapes must not invalidate them solely because their pads become
        # interior nodes. Keep the same TOTAL length cap (not per branch),
        # require a real pad and wide-trunk contact, and forbid floating leaves
        # or cycles. Unknown interior intersections remain rejected above.
        if (set(degree) & pads and set(degree) & wide_ends
                and all(endpoint in pads or endpoint in wide_ends for endpoint in ends)):
            accepted.update(component)
    return accepted
