"""KiCad-python routing worker (runs under KiCad's bundled interpreter, which
provides ``pcbnew``). Invoked as a subprocess by ``ratsnestpro.eda.routing``;
never imported into the main venv. Prints one ``RESULT <json>`` line.

Steps: load board -> assign nets to pads from a pinmap -> build connectivity ->
export Specctra DSN -> run Freerouting -> import the SES back -> save. Reports
pads assigned, tracks created, and remaining unconnected items (ratsnest).

Excluded from ruff/mypy in pyproject: it targets a foreign interpreter.
"""
import json
import os
import subprocess
import sys

import pcbnew


def _router_timeout(layer_count):
    default = 3600 if layer_count >= 4 else 1800
    raw = os.environ.get("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", "")
    try:
        requested = int(raw) if raw else default
    except ValueError:
        requested = default
    return max(300, min(requested, 7200))


def _apply_default_netclass(
    board,
    clearance_mm,
    track_width_mm,
    via_diameter_mm,
    via_drill_mm,
):
    netclass = board.GetAllNetClasses()["Default"]
    netclass.SetClearance(pcbnew.FromMM(clearance_mm))
    netclass.SetTrackWidth(pcbnew.FromMM(track_width_mm))
    netclass.SetViaDiameter(pcbnew.FromMM(via_diameter_mm))
    netclass.SetViaDrill(pcbnew.FromMM(via_drill_mm))
    board.GetNetClasses()["Default"] = netclass
    # Keep KiCad's authoritative DRC boundary rule aligned with the verified
    # route rule used by the DSN/Freerouting execution.
    board.GetDesignSettings().m_CopperEdgeClearance = pcbnew.FromMM(clearance_mm)
    board.SynchronizeNetsAndNetClasses(False)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _assign_nets(board, netmap):
    """Assign nets and return physical-pad count plus matched logical pin keys."""
    name_to_net = {}
    for name in netmap:
        net = board.FindNet(name)
        if net is None:
            net = pcbnew.NETINFO_ITEM(board, name)
            board.Add(net)
        name_to_net[name] = net
    pad_net = {}
    for name, pins in netmap.items():
        for ref, pad in pins:
            pad_net[(str(ref), str(pad))] = name
    assigned = 0
    matched = set()
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            key = (ref, pad.GetNumber())
            if key in pad_net:
                pad.SetNet(name_to_net[pad_net[key]])
                assigned += 1
                matched.add(key)
    board.BuildConnectivity()
    return assigned, len(matched)


def _import_ses(board, ses):
    try:
        return pcbnew.ImportSpecctraSES(board, ses)
    except TypeError:
        return pcbnew.ImportSpecctraSES(ses)


def _track_count(board):
    try:
        return int(board.GetTracks().size())
    except Exception:
        return len(list(board.GetTracks()))


def _unconnected(board):
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    for args in ((True,), ()):
        try:
            return int(conn.GetUnconnectedCount(*args))
        except Exception:
            continue
    return -1


def main():
    pcb, netmap_json, fr_exe, workdir = sys.argv[1:5]
    max_passes = sys.argv[5] if len(sys.argv) > 5 else "20"
    layer_count = int(sys.argv[6]) if len(sys.argv) > 6 else 2
    clearance_mm = float(sys.argv[7]) if len(sys.argv) > 7 else 0.2
    track_width_mm = float(sys.argv[8]) if len(sys.argv) > 8 else 0.2
    via_diameter_mm = float(sys.argv[9]) if len(sys.argv) > 9 else 0.6
    via_drill_mm = float(sys.argv[10]) if len(sys.argv) > 10 else 0.3
    random_seed = sys.argv[11] if len(sys.argv) > 11 else ""

    stem = os.path.splitext(os.path.basename(pcb))[0]
    dsn = os.path.join(workdir, stem + ".dsn")
    ses = os.path.join(workdir, stem + ".ses")
    result = {
        "assigned": 0,
        "matched_logical_pins": 0,
        "expected_pads": 0,
        "routed_tracks": 0,
        "unconnected": -1,
        "total_connections": -1,
        "routed_connections": -1,
        "metric_basis": "unavailable",
        "fr_ok": False,
        "layers": layer_count,
        "error": "",
        "fr_tail": "",
        "dsn_path": dsn,
        "ses_path": ses,
    }
    try:
        netmap = _load(netmap_json)
        result["nets"] = len(netmap)
        logical_keys = {}
        conflicting_keys = []
        for net_name, pins in netmap.items():
            for ref, pad in pins:
                key = (str(ref), str(pad))
                previous = logical_keys.get(key)
                if previous is not None and previous != net_name:
                    conflicting_keys.append(
                        f"{key[0]}:{key[1]} in {previous} & {net_name}"
                    )
                logical_keys[key] = net_name
        if conflicting_keys:
            raise RuntimeError(
                f"logical pins assigned to multiple nets: {conflicting_keys}"
            )
        result["expected_pads"] = len(logical_keys)
        board = pcbnew.LoadBoard(pcb)
        if layer_count >= 2:
            board.SetCopperLayerCount(layer_count)
        result["assigned"], result["matched_logical_pins"] = _assign_nets(
            board, netmap
        )
        result["total_connections"] = _unconnected(board)
        result["metric_basis"] = "kicad_connectivity"
        _apply_default_netclass(
            board,
            clearance_mm,
            track_width_mm,
            via_diameter_mm,
            via_drill_mm,
        )
        if result["matched_logical_pins"] != result["expected_pads"]:
            raise RuntimeError(
                "pin-map/footprint mismatch: matched "
                f"{result['matched_logical_pins']}/{result['expected_pads']} "
                f"logical pins ({result['assigned']} physical pads assigned)"
            )
        pcbnew.SaveBoard(pcb, board)  # persist connectivity

        pcbnew.ExportSpecctraDSN(board, dsn)

        router_args = [fr_exe, "-de", dsn, "-do", ses, "-mp", str(max_passes)]
        if random_seed:
            router_args.extend(["-random_seed", random_seed])
        proc = subprocess.run(
            router_args,
            capture_output=True,
            text=True,
            timeout=_router_timeout(layer_count),
        )
        combined = "\n".join((proc.stdout + "\n" + proc.stderr).splitlines()[-6:])
        result["fr_tail"] = combined
        if proc.returncode == 0 and os.path.exists(ses) and os.path.getsize(ses) > 0:
            board2 = pcbnew.LoadBoard(pcb)  # reload (carries nets)
            _import_ses(board2, ses)
            result["unconnected"] = _unconnected(board2)
            if (
                result["total_connections"] >= 0
                and 0 <= result["unconnected"] <= result["total_connections"]
            ):
                result["routed_connections"] = (
                    result["total_connections"] - result["unconnected"]
                )
            pcbnew.SaveBoard(pcb, board2)
            result["routed_tracks"] = _track_count(board2)
            result["fr_ok"] = True
        else:
            result["error"] = (
                f"Freerouting failed (exit={proc.returncode}); tail={combined!r}"
            )
    except Exception as exc:  # noqa: BLE001 - report back to the caller
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("RESULT " + json.dumps(result))


main()
