"""Apply a closed batch of PCB actions under KiCad's bundled Python.

This module is launched by :mod:`ratsnestpro.eda.routing`; it is not imported
by the service runtime.  It accepts only validated JSON actions, verifies the
source artifact fingerprint again, writes a separate candidate board, and
prints one structured ``RESULT`` line.  It intentionally exposes neither a
shell command nor raw KiCad S-expression editing.
"""

import hashlib
import json
import math
import os
import sys

import pcbnew  # type: ignore[import-not-found]

_OPERATIONS = {
    "move_footprint",
    "rotate_footprint",
    "swap_footprint_positions",
    "ripup_net",
    "add_track",
    "add_via",
    "resize_track",
    "refill_zones",
    "move_silkscreen",
}


def _fingerprint(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _load(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _point(raw):
    return pcbnew.VECTOR2I(
        pcbnew.FromMM(float(raw["x_mm"])),
        pcbnew.FromMM(float(raw["y_mm"])),
    )


def _uuid(item):
    for getter_name in ("GetUuid", "GetUUID"):
        getter = getattr(item, getter_name, None)
        if getter is None:
            continue
        try:
            value = getter()
            for string_name in ("AsString", "Format"):
                string_getter = getattr(value, string_name, None)
                if string_getter is not None:
                    return str(string_getter())
            return str(value)
        except Exception:
            continue
    value = getattr(item, "m_Uuid", "")
    return str(value)


def _is_locked(item):
    getter = getattr(item, "IsLocked", None)
    if getter is None:
        return False
    return bool(getter())


def _find_footprint(board, reference):
    for footprint in board.GetFootprints():
        if footprint.GetReference() == reference:
            return footprint
    raise ValueError(f"footprint {reference} was not found")


def _net_name(item):
    getter = getattr(item, "GetNetname", None)
    if getter is not None:
        return str(getter())
    net = item.GetNet()
    return str(net.GetNetname()) if net is not None else ""


def _matching_tracks(board, target, include_vias=True):
    item_uuid = target.get("item_uuid")
    net_name = target.get("net")
    matches = []
    for item in board.GetTracks():
        is_via = isinstance(item, pcbnew.PCB_VIA)
        if not include_vias and is_via:
            continue
        if item_uuid and _uuid(item) != item_uuid:
            continue
        if net_name and _net_name(item) != net_name:
            continue
        matches.append(item)
    return matches


def _layer_id(board, name):
    for layer_id in range(pcbnew.PCB_LAYER_ID_COUNT):
        try:
            if board.GetLayerName(layer_id) == name:
                return layer_id
        except Exception:
            continue
    raise ValueError(f"board does not contain layer {name}")


def _orientation_degrees(footprint):
    orientation = footprint.GetOrientation()
    getter = getattr(orientation, "AsDegrees", None)
    if getter is not None:
        return float(getter())
    return float(orientation) / 10.0


def _set_orientation(footprint, degrees):
    setter = getattr(footprint, "SetOrientationDegrees", None)
    if setter is not None:
        setter(float(degrees))
        return
    footprint.SetOrientation(pcbnew.EDA_ANGLE(float(degrees), pcbnew.DEGREES_T))


def _position_mm(item):
    position = item.GetPosition()
    return pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)


def _close(actual, expected, tolerance=0.001):
    return math.isclose(float(actual), float(expected), abs_tol=tolerance)


def _check_common_preconditions(board, action, entities):
    preconditions = action.get("preconditions") or {}
    if preconditions.get("require_unlocked", True):
        locked = [entity for entity in entities if _is_locked(entity)]
        if locked:
            raise ValueError("target entity is locked")
    expected_count = preconditions.get("expected_item_count")
    if expected_count is not None and len(entities) != int(expected_count):
        raise ValueError(
            f"expected {expected_count} matching items, observed {len(entities)}"
        )
    expected_net = preconditions.get("expected_net")
    if expected_net is not None:
        if any(_net_name(entity) != expected_net for entity in entities):
            raise ValueError("target net precondition changed")
    expected_layer = preconditions.get("expected_layer")
    if expected_layer is not None:
        layer_id = _layer_id(board, expected_layer)
        if any(int(entity.GetLayer()) != layer_id for entity in entities):
            raise ValueError("target layer precondition changed")


def _check_footprint_preconditions(footprint, action):
    preconditions = action.get("preconditions") or {}
    expected_position = preconditions.get("expected_position")
    if expected_position is not None:
        actual_x, actual_y = _position_mm(footprint)
        if not (
            _close(actual_x, expected_position["x_mm"])
            and _close(actual_y, expected_position["y_mm"])
        ):
            raise ValueError("footprint position precondition changed")
    expected_rotation = preconditions.get("expected_rotation_degrees")
    if expected_rotation is not None and not _close(
        _orientation_degrees(footprint), expected_rotation, tolerance=0.01
    ):
        raise ValueError("footprint rotation precondition changed")


def _require_net(board, name):
    net = board.FindNet(name)
    if net is None:
        raise ValueError(f"net {name} was not found")
    return net


def _move_footprint(board, action):
    footprint = _find_footprint(board, action["target"]["reference"])
    _check_common_preconditions(board, action, [footprint])
    _check_footprint_preconditions(footprint, action)
    footprint.SetPosition(_point(action["position"]))
    return f"moved {footprint.GetReference()}"


def _rotate_footprint(board, action):
    footprint = _find_footprint(board, action["target"]["reference"])
    _check_common_preconditions(board, action, [footprint])
    _check_footprint_preconditions(footprint, action)
    _set_orientation(footprint, action["rotation_degrees"])
    return f"rotated {footprint.GetReference()}"


def _swap_footprints(board, action):
    first = _find_footprint(board, action["target"]["reference"])
    second = _find_footprint(board, action["other_reference"])
    _check_common_preconditions(board, action, [first, second])
    _check_footprint_preconditions(first, action)
    first_position = first.GetPosition()
    second_position = second.GetPosition()
    first.SetPosition(second_position)
    second.SetPosition(first_position)
    return f"swapped {first.GetReference()} and {second.GetReference()}"


def _ripup_net(board, action):
    target = action["target"]
    _require_net(board, target["net"])
    tracks = _matching_tracks(board, target)
    _check_common_preconditions(board, action, tracks)
    for item in tracks:
        board.Remove(item)
    return f"removed {len(tracks)} tracks/vias from {target['net']}"


def _add_track(board, action):
    net = _require_net(board, action["target"]["net"])
    _check_common_preconditions(board, action, [])
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(_point(action["start"]))
    track.SetEnd(_point(action["end"]))
    track.SetLayer(_layer_id(board, action["layer"]))
    track.SetWidth(pcbnew.FromMM(float(action["width_mm"])))
    track.SetNet(net)
    board.Add(track)
    return f"added track on {action['target']['net']}"


def _add_via(board, action):
    net = _require_net(board, action["target"]["net"])
    _check_common_preconditions(board, action, [])
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(_point(action["position"]))
    diameter = pcbnew.FromMM(float(action["diameter_mm"]))
    if hasattr(via, "SetFrontWidth"):
        via.SetFrontWidth(diameter)
    else:
        via.SetWidth(diameter)
    via.SetDrill(pcbnew.FromMM(float(action["drill_mm"])))
    start_layer, end_layer = action["layer_pair"]
    via.SetLayerPair(_layer_id(board, start_layer), _layer_id(board, end_layer))
    via.SetNet(net)
    board.Add(via)
    return f"added via on {action['target']['net']}"


def _resize_track(board, action):
    tracks = _matching_tracks(board, action["target"], include_vias=False)
    if not tracks:
        raise ValueError("no track matched the resize target")
    _check_common_preconditions(board, action, tracks)
    width = pcbnew.FromMM(float(action["width_mm"]))
    for track in tracks:
        track.SetWidth(width)
    return f"resized {len(tracks)} tracks"


def _refill_zones(board, action):
    target_net = action["target"].get("net")
    zones = list(board.Zones())
    if target_net:
        zones = [zone for zone in zones if _net_name(zone) == target_net]
    _check_common_preconditions(board, action, zones)
    if not zones:
        raise ValueError("no zones matched the refill target")
    # KiCad refills all board zones as one connectivity operation. Filtering is
    # used only as a target precondition, never to claim a partial fill.
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    return f"refilled {len(list(board.Zones()))} board zones"


def _move_silkscreen(board, action):
    footprint = _find_footprint(board, action["target"]["reference"])
    field_name = action["target"]["field"]
    text_item = footprint.Reference() if field_name == "reference" else footprint.Value()
    _check_common_preconditions(board, action, [text_item])
    expected_position = (action.get("preconditions") or {}).get("expected_position")
    if expected_position is not None:
        actual_x, actual_y = _position_mm(text_item)
        if not (
            _close(actual_x, expected_position["x_mm"])
            and _close(actual_y, expected_position["y_mm"])
        ):
            raise ValueError("silkscreen position precondition changed")
    text_item.SetPosition(_point(action["position"]))
    text_item.SetLayer(_layer_id(board, action["layer"]))
    return f"moved {footprint.GetReference()} {field_name} text"


_HANDLERS = {
    "move_footprint": _move_footprint,
    "rotate_footprint": _rotate_footprint,
    "swap_footprint_positions": _swap_footprints,
    "ripup_net": _ripup_net,
    "add_track": _add_track,
    "add_via": _add_via,
    "resize_track": _resize_track,
    "refill_zones": _refill_zones,
    "move_silkscreen": _move_silkscreen,
}


def main():
    source_path, output_path, batch_path, run_root = sys.argv[1:5]
    result = {
        "ok": False,
        "error": "",
        "before_fingerprint": "",
        "after_fingerprint": "",
        "action_results": [],
    }
    try:
        if not _inside(source_path, run_root) or not _inside(output_path, run_root):
            raise ValueError("CAD worker paths must stay inside the run workspace")
        if not source_path.endswith(".kicad_pcb") or not output_path.endswith(
            ".kicad_pcb"
        ):
            raise ValueError("CAD worker accepts only .kicad_pcb artifacts")
        batch = _load(batch_path)
        actions = batch.get("actions")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 32:
            raise ValueError("CAD action count is outside the bounded range")
        if any(action.get("operation") not in _OPERATIONS for action in actions):
            raise ValueError("CAD batch contains an unsupported operation")
        before = _fingerprint(source_path)
        result["before_fingerprint"] = before
        if before != batch.get("base_artifact_fingerprint"):
            raise ValueError("artifact fingerprint changed before worker execution")

        board = pcbnew.LoadBoard(source_path)
        for action in actions:
            operation = action["operation"]
            try:
                detail = _HANDLERS[operation](board, action)
            except Exception as exc:
                result["action_results"].append({
                    "action_id": action["action_id"],
                    "operation": operation,
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                })
                raise RuntimeError(
                    f"CAD action {action['action_id']} failed: {exc}"
                ) from exc
            else:
                result["action_results"].append({
                    "action_id": action["action_id"],
                    "operation": operation,
                    "status": "applied",
                    "detail": detail,
                })
        board.BuildConnectivity()
        pcbnew.SaveBoard(output_path, board)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("KiCad did not write a candidate board")
        # Loading the saved candidate catches serialization failures before the
        # service atomically replaces the source artifact.
        pcbnew.LoadBoard(output_path)
        result["after_fingerprint"] = _fingerprint(output_path)
        result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print("RESULT " + json.dumps(result))


main()
