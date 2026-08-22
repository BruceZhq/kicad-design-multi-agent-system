"""Materialize planned copper planes and stitch disconnected rail islands.

This module targets KiCad's system Python (which provides ``pcbnew``), not the
main application interpreter.  It is invoked as a subprocess by the adaptive
hardware-engineering loop and prints one ``RESULT <json>`` line.

Every candidate is checked by the authoritative KiCad DRC.  A patch is kept
only when it reduces the number of unconnected items without introducing or
increasing any non-connectivity error.  The search is intentionally derived
from DRC coordinates and plane assignments; it has no board-, net-, or
reference-specific cases.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pcbnew

_NET_RE = re.compile(r"\[([^\]]+)\]")
_LAYER_RE = re.compile(r"\bon\s+((?:F|B|In\d+)\.Cu)\b")


def _run_drc(cli: str, pcb_path: Path, report_path: Path) -> dict:
    report_path.unlink(missing_ok=True)
    subprocess.run(
        [
            cli,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(report_path),
            "--exit-code-violations",
            str(pcb_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    with report_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _error_counts(report: dict) -> Counter:
    return Counter(
        (
            str(finding.get("type", "unknown")),
            str(finding.get("description", "DRC error")),
        )
        for key in ("violations", "schematic_parity")
        for finding in report.get(key, [])
        if (
            isinstance(finding, dict)
            and str(finding.get("severity", "error")) == "error"
        )
    )


def _gaps(report: dict) -> list[dict]:
    result: list[dict] = []
    for finding in report.get("unconnected_items", []):
        if (
            not isinstance(finding, dict)
            or str(finding.get("severity", "error")) != "error"
        ):
            continue
        items = finding.get("items", [])
        if not isinstance(items, list) or len(items) != 2:
            continue
        endpoints: list[dict] = []
        net_name = ""
        valid = True
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("pos"), dict):
                valid = False
                break
            description = str(item.get("description", ""))
            net_match = _NET_RE.search(description)
            layer_match = _LAYER_RE.search(description)
            if net_match is None or layer_match is None:
                valid = False
                break
            if net_name and net_match.group(1) != net_name:
                valid = False
                break
            net_name = net_match.group(1)
            try:
                endpoints.append(
                    {
                        "x": float(item["pos"]["x"]),
                        "y": float(item["pos"]["y"]),
                        "layer": layer_match.group(1),
                    }
                )
            except (KeyError, TypeError, ValueError):
                valid = False
                break
        if valid and len(endpoints) == 2:
            result.append({"net": net_name, "endpoints": endpoints})
    return result


def _no_new_errors(after: Counter, before: Counter) -> bool:
    return all(count <= before[key] for key, count in after.items())


def _layer_id(board, name: str):
    for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
        try:
            if board.GetLayerName(layer_id) == name:
                return layer_id
        except Exception:
            continue
    return None


def _board_polygon(board, inset_mm: float) -> list[tuple[float, float]]:
    bounds = board.GetBoardEdgesBoundingBox()
    left = pcbnew.ToMM(bounds.GetX()) + inset_mm
    top = pcbnew.ToMM(bounds.GetY()) + inset_mm
    right = pcbnew.ToMM(bounds.GetRight()) - inset_mm
    bottom = pcbnew.ToMM(bounds.GetBottom()) - inset_mm
    if right <= left or bottom <= top:
        raise RuntimeError("board outline is too small for a copper plane")
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def _has_zone(board, net_code: int, layer_id: int) -> bool:
    return any(
        zone.GetNetCode() == net_code and zone.IsOnLayer(layer_id)
        for zone in board.Zones()
    )


def _materialize_planes(
    board,
    assignments: list[dict],
    clearance_mm: float,
) -> int:
    polygon = _board_polygon(board, max(0.5, clearance_mm))
    added = 0
    for assignment in assignments:
        net = board.FindNet(str(assignment["net"]))
        layer_id = _layer_id(board, str(assignment["layer"]))
        if net is None or layer_id is None or _has_zone(
            board,
            net.GetNetCode(),
            layer_id,
        ):
            continue
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer_id)
        zone.SetNet(net)
        zone.SetLocalClearance(pcbnew.FromMM(clearance_mm))
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in polygon:
            outline.Append(
                pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))
            )
        board.Add(zone)
        added += 1
    if added:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return added


def _add_fanout(
    board,
    *,
    net_name: str,
    endpoint: dict,
    offset: tuple[float, float],
    track_width_mm: float,
    via_diameter_mm: float,
    via_drill_mm: float,
) -> None:
    net = board.FindNet(net_name)
    layer_id = _layer_id(board, str(endpoint["layer"]))
    if net is None or layer_id is None:
        raise RuntimeError(f"cannot resolve {net_name!r} on {endpoint['layer']!r}")
    start = pcbnew.VECTOR2I(
        pcbnew.FromMM(float(endpoint["x"])),
        pcbnew.FromMM(float(endpoint["y"])),
    )
    end = pcbnew.VECTOR2I(
        pcbnew.FromMM(float(endpoint["x"]) + offset[0]),
        pcbnew.FromMM(float(endpoint["y"]) + offset[1]),
    )
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(start)
    track.SetEnd(end)
    track.SetWidth(pcbnew.FromMM(track_width_mm))
    track.SetLayer(layer_id)
    track.SetNet(net)
    if start != end:
        board.Add(track)

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(end)
    # KiCad 9 warns when SetWidth has no layer.  Front width defines a
    # through-via's diameter while retaining compatibility with KiCad 8.
    if hasattr(via, "SetFrontWidth"):
        via.SetFrontWidth(pcbnew.FromMM(via_diameter_mm))
    else:
        via.SetWidth(pcbnew.FromMM(via_diameter_mm))
    via.SetDrill(pcbnew.FromMM(via_drill_mm))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetNet(net)
    board.Add(via)


def _offsets(
    *,
    via_diameter_mm: float,
    clearance_mm: float,
) -> list[tuple[float, float]]:
    step = max(1.0, via_diameter_mm + clearance_mm)
    unit = (
        (0.0, -step),
        (step, 0.0),
        (-step, 0.0),
        (0.0, step),
        (step, -step),
        (-step, -step),
        (step, step),
        (-step, step),
    )
    return [(0.0, 0.0), *[
        (round(dx * scale, 3), round(dy * scale, 3))
        for scale in (1.0, 1.5, 2.0)
        for dx, dy in unit
    ]]


def main() -> None:
    pcb_path = Path(sys.argv[1]).resolve()
    cli = sys.argv[2]
    assignments = json.loads(sys.argv[3])
    clearance_mm = float(sys.argv[4])
    track_width_mm = float(sys.argv[5])
    via_diameter_mm = float(sys.argv[6])
    via_drill_mm = float(sys.argv[7])
    report_path = Path(sys.argv[8]).resolve()

    result = {
        "ok": False,
        "unconnected": -1,
        "closed_gaps": 0,
        "added_zones": 0,
        "added_vias": 0,
        "routed_tracks": 0,
        "error": "",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="rnp_plane_stitch_") as temp:
            temp_root = Path(temp)
            accepted_path = temp_root / pcb_path.name
            shutil.copy2(pcb_path, accepted_path)
            project_path = pcb_path.with_suffix(".kicad_pro")
            accepted_project = accepted_path.with_suffix(".kicad_pro")
            if project_path.is_file():
                shutil.copy2(project_path, accepted_project)

            baseline_report = _run_drc(
                cli,
                accepted_path,
                temp_root / "baseline.drc.json",
            )
            baseline_errors = _error_counts(baseline_report)
            baseline_gaps = _gaps(baseline_report)
            original_gap_count = len(baseline_gaps)
            board = pcbnew.LoadBoard(str(accepted_path))
            result["added_zones"] = _materialize_planes(
                board,
                assignments,
                clearance_mm,
            )
            pcbnew.SaveBoard(str(accepted_path), board)
            accepted_report = _run_drc(
                cli,
                accepted_path,
                temp_root / "accepted.drc.json",
            )
            accepted_errors = _error_counts(accepted_report)
            accepted_gaps = _gaps(accepted_report)
            if not _no_new_errors(accepted_errors, baseline_errors):
                raise RuntimeError(
                    "planned copper planes introduced new DRC errors"
                )
            plane_nets = {
                str(assignment["net"])
                for assignment in assignments
            }
            offsets = _offsets(
                via_diameter_mm=via_diameter_mm,
                clearance_mm=clearance_mm,
            )

            # Return a zone-only improvement immediately. The outer AHE loop
            # checkpoints that monotonic gain and can re-evaluate the smaller
            # residual set, instead of spending minutes searching vias after
            # a valid patch is already available.
            while (
                accepted_gaps
                and len(accepted_gaps) == original_gap_count
            ):
                gap = next(
                    (
                        candidate
                        for candidate in accepted_gaps
                        if candidate["net"] in plane_nets
                    ),
                    None,
                )
                if gap is None:
                    break
                improved = False
                # Most rail islands need only one via to reach the newly
                # materialized plane. Try those cheap monotonic candidates
                # before a bounded two-ended fallback.
                candidate_fanouts = [
                    [(endpoint_index, offset)]
                    for endpoint_index in (0, 1)
                    for offset in offsets
                ]
                candidate_fanouts.extend(
                    [
                        [(0, left_offset), (1, right_offset)]
                        for left_offset, right_offset in itertools.chain(
                            zip(offsets, offsets, strict=True),
                            itertools.islice(
                                itertools.product(offsets, offsets),
                                96,
                            ),
                        )
                    ]
                )
                for fanouts in candidate_fanouts:
                    candidate_path = temp_root / "candidate.kicad_pcb"
                    shutil.copy2(accepted_path, candidate_path)
                    if accepted_project.is_file():
                        shutil.copy2(
                            accepted_project,
                            candidate_path.with_suffix(".kicad_pro"),
                        )
                    candidate_board = pcbnew.LoadBoard(str(candidate_path))
                    for endpoint_index, offset in fanouts:
                        _add_fanout(
                            candidate_board,
                            net_name=gap["net"],
                            endpoint=gap["endpoints"][endpoint_index],
                            offset=offset,
                            track_width_mm=track_width_mm,
                            via_diameter_mm=via_diameter_mm,
                            via_drill_mm=via_drill_mm,
                        )
                    pcbnew.ZONE_FILLER(candidate_board).Fill(
                        candidate_board.Zones()
                    )
                    pcbnew.SaveBoard(str(candidate_path), candidate_board)
                    candidate_report = _run_drc(
                        cli,
                        candidate_path,
                        temp_root / "candidate.drc.json",
                    )
                    candidate_errors = _error_counts(candidate_report)
                    candidate_gaps = _gaps(candidate_report)
                    if (
                        len(candidate_gaps) < len(accepted_gaps)
                        and _no_new_errors(candidate_errors, accepted_errors)
                    ):
                        shutil.copy2(candidate_path, accepted_path)
                        accepted_errors = candidate_errors
                        accepted_gaps = candidate_gaps
                        result["added_vias"] += len(fanouts)
                        improved = True
                        break
                if not improved:
                    break

            result["unconnected"] = len(accepted_gaps)
            result["closed_gaps"] = max(
                0,
                original_gap_count - len(accepted_gaps),
            )
            if result["closed_gaps"] > 0:
                final_board = pcbnew.LoadBoard(str(accepted_path))
                try:
                    result["routed_tracks"] = int(final_board.GetTracks().size())
                except Exception:
                    result["routed_tracks"] = len(list(final_board.GetTracks()))
                shutil.copy2(accepted_path, pcb_path)
                final_report = _run_drc(cli, pcb_path, report_path)
                result["unconnected"] = len(_gaps(final_report))
                result["ok"] = True
            else:
                result["added_zones"] = 0
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("RESULT " + json.dumps(result))


main()
