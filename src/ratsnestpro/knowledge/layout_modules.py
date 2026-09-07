"""Physical templates derived from a content-bound released circuit module.

Relative placement is a seed, not permission to overwrite task-specific
placement constraints. Copper crossing the module boundary is never copied.
"""

from pathlib import Path
import hashlib

from ratsnestpro.eda.vendor.pcb import PcbBoard


def extract_layout(pcb_path, *, pcb_sha256, components, nets):
    path = Path(pcb_path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != pcb_sha256:
        raise ValueError("physical template source differs from reviewed PCB")
    board = PcbBoard.load(path)
    placed = {fp["reference"]: fp for fp in board.list_footprints()}
    refs = [part["ref"] for part in components]
    if not refs or any(ref not in placed for ref in refs):
        raise ValueError("module footprints are absent from released PCB")
    anchor = placed[refs[0]]["at"]
    placements = []
    for part in components:
        fp = placed[part["ref"]]
        if fp["lib_id"] != part["footprint_lib_id"]:
            raise ValueError("physical template footprint identity differs")
        placements.append(
            {
                "ref": part["ref"],
                "x": round(fp["at"]["x"] - anchor["x"], 4),
                "y": round(fp["at"]["y"] - anchor["y"], 4),
                "rotation": fp["at"]["rotation"],
                "layer": fp["layer"],
            }
        )
    internal = {net["name"] for net in nets if not net["boundary"]}
    tracks = [
        {
            "net": track["net_name"],
            "layer": track["layer"],
            "width": track["width"],
            "start": [
                round(track["start"][0] - anchor["x"], 4),
                round(track["start"][1] - anchor["y"], 4),
            ],
            "end": [
                round(track["end"][0] - anchor["x"], 4),
                round(track["end"][1] - anchor["y"], 4),
            ],
        }
        for track in board.list_tracks()
        if track["net_name"] in internal
    ]
    # Keep retrieval bounded. Never present truncated routing as a complete route.
    return {
        "anchor_ref": refs[0],
        "placements": placements,
        "internal_tracks": tracks[:100],
        "tracks_truncated": len(tracks) > 100,
        "usage": "candidate seed; rebind exact assets and validate placement, clearance and DRC",
    }
