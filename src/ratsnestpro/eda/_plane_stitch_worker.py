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
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import pcbnew

_NET_RE = re.compile(r"\[([^\]]+)\]")
_LAYER_RE = re.compile(r"\bon\s+((?:F|B|In\d+)\.Cu)\b")


def _run_drc(cli: str, pcb_path: Path, report_path: Path, *, timeout: float = 120) -> dict:
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
        timeout=max(0.1, timeout),
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
            if net_match is None:
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
                        "layer": (
                            layer_match.group(1)
                            if layer_match is not None
                            else None
                        ),
                    }
                )
            except (KeyError, TypeError, ValueError):
                valid = False
                break
        if valid and len(endpoints) == 2:
            left_layer = endpoints[0]["layer"]
            right_layer = endpoints[1]["layer"]
            if left_layer is None and right_layer is not None:
                endpoints[0]["layer"] = right_layer
            elif right_layer is None and left_layer is not None:
                endpoints[1]["layer"] = left_layer
            elif left_layer is None and right_layer is None:
                endpoints[0]["layer"] = "F.Cu"
                endpoints[1]["layer"] = "F.Cu"
            result.append({"net": net_name, "endpoints": endpoints})
    return result


def _unconnected_count(report: dict) -> int:
    """Count every authoritative unconnected error, parsed or not."""

    return sum(
        isinstance(finding, dict)
        and str(finding.get("severity", "error")) == "error"
        for finding in report.get("unconnected_items", [])
    )


def _no_new_errors(after: Counter, before: Counter) -> bool:
    return all(count <= before[key] for key, count in after.items())


def _candidate_is_monotonic_gain(
    after_errors: Counter,
    before_errors: Counter,
    *,
    added_zones: int,
    after_gap_count: int,
    before_gap_count: int,
) -> bool:
    """Keep safe copper materialization even when no ratline needed closing."""

    return (
        _no_new_errors(after_errors, before_errors)
        and after_gap_count <= before_gap_count
        and (added_zones > 0 or after_gap_count < before_gap_count)
    )


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
        # Generated planes must not introduce a starved-thermal DRC error on
        # sparse nets (for example, a bottom GND plane whose only bottom-side
        # anchor is one through-hole pad).  A solid connection is deterministic
        # and removes the spoke-count dependency; removing isolated islands
        # prevents disconnected pour fragments from surviving the fill.
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
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
    fanout_approval: dict | None = None,
    bend_offset: tuple[float, float] | None = None,
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
        approval = fanout_approval or {}
        bend_offset = bend_offset or (0.0, 0.0)
        last_length = math.dist(bend_offset, offset)
        length = math.hypot(*bend_offset) + last_length
        pad_owned = any(
            pad.GetNetname() == net_name
            and pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD
            and pad.IsOnLayer(layer_id)
            and pad.GetPosition() == start
            for footprint in board.GetFootprints() for pad in footprint.Pads()
        )
        if (approval and pad_owned and last_length > 0.25
                and 0.25 < length - 0.25 <= float(approval["max_chain_length_mm"])
                and float(approval["minimum_width_mm"]) < track_width_mm):
            # Keep a full-width landing before the via. The release auditor
            # independently verifies the thin chain reaches this real trunk.
            bend = pcbnew.VECTOR2I(start.x + pcbnew.FromMM(bend_offset[0]),
                                   start.y + pcbnew.FromMM(bend_offset[1]))
            fraction = (last_length - 0.25) / last_length
            landing = pcbnew.VECTOR2I(
                bend.x + round((end.x - bend.x) * fraction),
                bend.y + round((end.y - bend.y) * fraction),
            )
            if bend != start:
                head = pcbnew.PCB_TRACK(board)
                head.SetStart(start)
                head.SetEnd(bend)
                head.SetWidth(pcbnew.FromMM(float(approval["minimum_width_mm"])))
                head.SetLayer(layer_id)
                head.SetNet(net)
                board.Add(head)
            track.SetStart(bend)
            track.SetEnd(landing)
            track.SetWidth(pcbnew.FromMM(float(approval["minimum_width_mm"])))
            board.Add(track)
            trunk = pcbnew.PCB_TRACK(board)
            trunk.SetStart(landing)
            trunk.SetEnd(end)
            trunk.SetWidth(pcbnew.FromMM(track_width_mm))
            trunk.SetLayer(layer_id)
            trunk.SetNet(net)
            board.Add(trunk)
        else:
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
    step = max(0.5, via_diameter_mm / 2 + clearance_mm)
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
        for scale in (1.0, 1.5, 2.0, 3.0)
        for dx, dy in unit
    ]]


def prepare_power_fanouts(pcb_path: Path, classes: list[dict], power_nets: list[str],
                         approval: dict, *, deadline: float) -> dict:
    """DRC-checked fine-pitch escapes before the router reserves other channels.

    Only pads narrower than their power trunk are candidates. No component
    names, pin numbers, or board families are special-cased. Failed candidates
    never change the input PCB; the subsequent router must connect the via.
    """
    import time
    from fanout_policy import pad_escape_axis

    receipt = {"accepted": [], "rejections": [], "attempts": 0,
               "approval_digest": approval.get("receipt_digest", "")}
    cli = shutil.which("kicad-cli")
    if not approval or not cli:
        return receipt
    rules = {n: c for c in classes for n in c["nets"] if n in power_nets}
    with tempfile.TemporaryDirectory(prefix="rnp_escape_") as temp:
        candidate = Path(temp) / pcb_path.name
        project = pcb_path.with_suffix(".kicad_pro")
        if project.is_file():
            shutil.copy2(project, candidate.with_suffix(".kicad_pro"))
        if time.monotonic() >= deadline:
            return receipt
        try:
            baseline = _error_counts(_run_drc(cli, pcb_path, Path(temp) / "baseline.json",
                                             timeout=deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            receipt["deadline_reached"] = True
            return receipt
        board = pcbnew.LoadBoard(str(pcb_path))
        targets = []
        for fp in board.GetFootprints():
            center = fp.GetPosition()
            for pad in fp.Pads():
                rule = rules.get(pad.GetNetname())
                size = pad.GetSize()
                if (not rule or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                        or pcbnew.ToMM(min(size.x, size.y)) >= rule["width"] - 1e-6
                        or not pad.IsOnLayer(pcbnew.F_Cu)):
                    continue
                pos = pad.GetPosition()
                axis = pad_escape_axis((size.x, size.y), pad.GetOrientationDegrees(),
                                       (pos.x - center.x, pos.y - center.y))
                targets.append((fp.GetReference(), pad.GetNumber(), pad.GetNetname(),
                                {"x": pcbnew.ToMM(pos.x), "y": pcbnew.ToMM(pos.y), "layer": "F.Cu"},
                                axis, rule, pcbnew.ToMM(max(size.x, size.y)) / 2
                                + rule["clearance"] + float(approval["minimum_width_mm"]) / 2))
        for ref, number, net, endpoint, axis, rule, straight_exit in targets:
            candidates = [(length, 0.0) for length in (1.2, 2.0, 1.6)]
            # Some dense power-pin rows have decouplers outside the package.
            # The opposite pad-axis escape is a legitimate alternative when
            # no physical copper/keepout blocks it; KiCad DRC must prove this.
            candidates.extend((length, 0.0) for length in (-1.2, -2.0))
            candidates.extend((straight_exit + 0.75, side * (rule["via_diameter"] / 2 + rule["clearance"] + 0.1))
                              for side in (-1, 1))
            for length, lateral in candidates:
                if time.monotonic() >= deadline:
                    receipt["deadline_reached"] = True
                    return receipt
                offset = (axis[0] * length - axis[1] * lateral,
                          axis[1] * length + axis[0] * lateral)
                bend = (axis[0] * straight_exit, axis[1] * straight_exit) if lateral else (0.0, 0.0)
                chain_length = math.hypot(*bend) + math.dist(bend, offset) - 0.25
                if chain_length > float(approval["max_chain_length_mm"]):
                    continue
                shutil.copy2(pcb_path, candidate)
                proposal = pcbnew.LoadBoard(str(candidate))
                _add_fanout(proposal, net_name=net, endpoint=endpoint,
                            offset=offset, bend_offset=bend,
                            track_width_mm=rule["width"], via_diameter_mm=rule["via_diameter"],
                            via_drill_mm=rule["via_drill"], fanout_approval=approval)
                pcbnew.SaveBoard(str(candidate), proposal)
                try:
                    errors = _error_counts(_run_drc(cli, candidate, Path(temp) / "candidate.json",
                                                   timeout=deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    receipt["deadline_reached"] = True
                    return receipt
                receipt["attempts"] += 1
                if _no_new_errors(errors, baseline):
                    shutil.copy2(candidate, pcb_path)
                    baseline = errors
                    receipt["accepted"].append({"ref": ref, "pad": number, "net": net,
                                                "thin_chain_length_mm": round(chain_length, 4),
                                                "lateral_offset_mm": lateral})
                    break
                receipt["rejections"].append({"ref": ref, "pad": number, "net": net,
                                              "offset_mm": offset,
                                              "new_errors": [[*key, count] for key, count in (errors - baseline).most_common(3)]})
                receipt["rejections"] = receipt["rejections"][-8:]
    return receipt


def main() -> None:
    pcb_path = Path(sys.argv[1]).resolve()
    cli = sys.argv[2]
    assignments = json.loads(sys.argv[3])
    clearance_mm = float(sys.argv[4])
    track_width_mm = float(sys.argv[5])
    via_diameter_mm = float(sys.argv[6])
    via_drill_mm = float(sys.argv[7])
    report_path = Path(sys.argv[8]).resolve()
    fanout_approval = json.loads(sys.argv[9]) if len(sys.argv) > 9 else {}
    repair_seconds = max(0.0, min(120.0, float(sys.argv[10]))) if len(sys.argv) > 10 else 120.0

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
            original_gap_count = _unconnected_count(baseline_report)
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
            accepted_gap_count = _unconnected_count(accepted_report)
            if (
                not _no_new_errors(accepted_errors, baseline_errors)
                or accepted_gap_count > original_gap_count
            ):
                raise RuntimeError(
                    "planned copper planes worsened DRC"
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
            exhausted_gaps = set()
            repair_deadline = time.monotonic() + repair_seconds
            while (
                accepted_gaps
                and accepted_gap_count == original_gap_count
                and time.monotonic() < repair_deadline
            ):
                gap = next(
                    (
                        candidate
                        for candidate in accepted_gaps
                        if candidate["net"] in plane_nets
                        and json.dumps(candidate, sort_keys=True) not in exhausted_gaps
                    ),
                    None,
                )
                if gap is None:
                    break
                improved = False
                # One inaccessible MCU pad must not starve every other rail
                # island. Bound that gap, then inspect the remaining gaps.
                gap_deadline = min(repair_deadline, time.monotonic() + 20)
                # Most rail islands need only one via to reach the newly
                # materialized plane. Try those cheap monotonic candidates
                # before a bounded two-ended fallback.
                candidate_fanouts = [
                    [(endpoint_index, offset)]
                    for offset in offsets
                    for endpoint_index in (0, 1)
                ]
                # Escape along a real SMD pad's major axis before turning;
                # diagonal rays alone cross adjacent fine-pitch pads.
                if fanout_approval:
                    from fanout_policy import pad_escape_axis
                    bent = []
                    for endpoint_index, endpoint in enumerate(gap["endpoints"]):
                        for footprint in board.GetFootprints():
                            for pad in footprint.Pads():
                                pos = pad.GetPosition()
                                if (pad.GetNetname() != gap["net"] or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD
                                        or math.dist((pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)),
                                                     (endpoint['x'], endpoint['y'])) > 1e-4):
                                    continue
                                center, size = footprint.GetPosition(), pad.GetSize()
                                axis = pad_escape_axis((size.x, size.y), pad.GetOrientationDegrees(),
                                                       (pos.x-center.x, pos.y-center.y))
                                escape = max(.5, pcbnew.ToMM(max(size.x, size.y))/2
                                             + via_diameter_mm/2 + clearance_mm + .025)
                                for length in (escape, escape+.25):
                                    bend = (axis[0]*length, axis[1]*length)
                                    for lateral in (.6, -.6, .8, -.8):
                                        offset = (bend[0]-axis[1]*lateral, bend[1]+axis[0]*lateral)
                                        bent.append([(endpoint_index, offset, bend)])
                    candidate_fanouts = bent + candidate_fanouts
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
                    if time.monotonic() >= gap_deadline:
                        break
                    candidate_path = temp_root / "candidate.kicad_pcb"
                    shutil.copy2(accepted_path, candidate_path)
                    if accepted_project.is_file():
                        shutil.copy2(
                            accepted_project,
                            candidate_path.with_suffix(".kicad_pro"),
                        )
                    candidate_board = pcbnew.LoadBoard(str(candidate_path))
                    for fanout in fanouts:
                        endpoint_index, offset = fanout[:2]
                        _add_fanout(
                            candidate_board,
                            net_name=gap["net"],
                            endpoint=gap["endpoints"][endpoint_index],
                            offset=offset,
                            track_width_mm=track_width_mm,
                            via_diameter_mm=via_diameter_mm,
                            via_drill_mm=via_drill_mm,
                            fanout_approval=fanout_approval,
                            bend_offset=fanout[2] if len(fanout) > 2 else None,
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
                    candidate_gap_count = _unconnected_count(candidate_report)
                    if (
                        candidate_gap_count < accepted_gap_count
                        and _no_new_errors(candidate_errors, accepted_errors)
                    ):
                        shutil.copy2(candidate_path, accepted_path)
                        accepted_errors = candidate_errors
                        accepted_gaps = candidate_gaps
                        accepted_gap_count = candidate_gap_count
                        result["added_vias"] += len(fanouts)
                        improved = True
                        break
                if not improved:
                    exhausted_gaps.add(json.dumps(gap, sort_keys=True))

            result["unconnected"] = accepted_gap_count
            result["closed_gaps"] = max(
                0,
                original_gap_count - accepted_gap_count,
            )
            if _candidate_is_monotonic_gain(
                accepted_errors,
                baseline_errors,
                added_zones=result["added_zones"],
                after_gap_count=accepted_gap_count,
                before_gap_count=original_gap_count,
            ):
                final_board = pcbnew.LoadBoard(str(accepted_path))
                try:
                    result["routed_tracks"] = int(final_board.GetTracks().size())
                except Exception:
                    result["routed_tracks"] = len(list(final_board.GetTracks()))
                shutil.copy2(accepted_path, pcb_path)
                final_report = _run_drc(cli, pcb_path, report_path)
                result["unconnected"] = _unconnected_count(final_report)
                result["ok"] = True
            else:
                result["added_zones"] = 0
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("RESULT " + json.dumps(result))


if __name__ == "__main__":
    main()
