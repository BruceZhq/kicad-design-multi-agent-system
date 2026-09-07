"""Freeze electrical identity and rule-bearing geometry across script repair."""

import hashlib
import json

from ratsnestpro.eda.vendor.sexpr import find_all, find_first, tag_of


def _normalized(value):
    if isinstance(value, list):
        return [
            _normalized(v)
            for v in value
            if not (isinstance(v, list) and tag_of(v) in {"uuid", "tstamp"})
        ]
    text = str(value)
    try:
        return round(float(text), 8)
    except ValueError:
        return text


def immutable_signature(board) -> str:
    footprints = []
    for fp in find_all(board.root, "footprint"):
        at = find_first(fp, "at")
        rotation = float(str(at[3])) if at and len(at) > 3 else 0.0
        properties = {
            str(n[1]): str(n[2])
            for n in find_all(fp, "property")
            if len(n) > 2 and str(n[1]) in {"Reference", "Value"}
        }
        pads = []
        for pad in find_all(fp, "pad"):
            pos = find_first(pad, "at")
            angle = float(str(pos[3])) if pos and len(pos) > 3 else 0.0
            fields = [n for n in pad[4:] if tag_of(n) not in {"at", "uuid", "tstamp"}]
            pads.append(
                [
                    _normalized(pad[:4]),
                    _normalized(pos[:3]) if pos else None,
                    round((angle - rotation) % 360, 6),
                    _normalized(fields),
                ]
            )
        courtyard = [
            n
            for n in fp
            if isinstance(n, list)
            and (layer := find_first(n, "layer"))
            and str(layer[1]) in {"F.CrtYd", "B.CrtYd"}
        ]
        footprints.append(
            [
                str(fp[1]),
                properties,
                pads,
                _normalized(courtyard),
                _normalized(find_first(fp, "attr")),
            ]
        )
    outline = [
        n
        for n in board.root
        if isinstance(n, list)
        and (layer := find_first(n, "layer"))
        and str(layer[1]) == "Edge.Cuts"
    ]
    keepouts = [n for n in find_all(board.root, "zone") if find_first(n, "keepout") is not None]
    payload = {
        "footprints": sorted(footprints, key=lambda fp: fp[1].get("Reference", "")),
        "nets": sorted(board.list_nets(), key=lambda n: n["index"]),
        "layers": _normalized(find_first(board.root, "layers")),
        "setup": _normalized(find_first(board.root, "setup")),
        "outline": _normalized(outline),
        "keepouts": _normalized(keepouts),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
