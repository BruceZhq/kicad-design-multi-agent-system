from __future__ import annotations

import importlib.util
import sys
import types
from collections import Counter
from pathlib import Path


def _load_worker(monkeypatch):
    monkeypatch.setitem(sys.modules, "pcbnew", types.ModuleType("pcbnew"))
    path = (
        Path(__file__).parents[1]
        / "src"
        / "ratsnestpro"
        / "eda"
        / "_plane_stitch_worker.py"
    )
    spec = importlib.util.spec_from_file_location("test_plane_stitch_worker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_zone_is_kept_without_closing_a_ratline(monkeypatch) -> None:
    worker = _load_worker(monkeypatch)

    assert worker._candidate_is_monotonic_gain(
        Counter(),
        Counter(),
        added_zones=1,
        before_gap_count=0,
        after_gap_count=0,
    )
    assert not worker._candidate_is_monotonic_gain(
        Counter(),
        Counter(),
        added_zones=0,
        before_gap_count=0,
        after_gap_count=0,
    )
    assert not worker._candidate_is_monotonic_gain(
        Counter({("clearance", "new violation"): 1}),
        Counter(),
        added_zones=1,
        before_gap_count=0,
        after_gap_count=0,
    )


def test_materialized_plane_uses_solid_pads_and_removes_islands(monkeypatch) -> None:
    worker = _load_worker(monkeypatch)

    class Net:
        def GetNetCode(self):
            return 1

    class Outline:
        def NewOutline(self):
            return 0

        def Append(self, _point):
            return 0

    class Zone:
        def __init__(self, _board):
            self.pad_connection = None
            self.island_removal = None
            self.outline = Outline()

        def SetLayer(self, _layer):
            return None

        def SetNet(self, _net):
            return None

        def SetLocalClearance(self, _clearance):
            return None

        def SetPadConnection(self, value):
            self.pad_connection = value

        def SetIslandRemovalMode(self, value):
            self.island_removal = value

        def Outline(self):
            return self.outline

    class Board:
        def __init__(self):
            self.zones = []

        def FindNet(self, _name):
            return Net()

        def Add(self, zone):
            self.zones.append(zone)

        def Zones(self):
            return self.zones

    class Filler:
        def __init__(self, _board):
            pass

        def Fill(self, _zones):
            return True

    worker.pcbnew.ZONE_CONNECTION_FULL = "solid"
    worker.pcbnew.ISLAND_REMOVAL_MODE_ALWAYS = "always"
    worker.pcbnew.ZONE = Zone
    worker.pcbnew.ZONE_FILLER = Filler
    worker.pcbnew.FromMM = lambda value: value
    worker.pcbnew.VECTOR2I = lambda x, y: (x, y)
    monkeypatch.setattr(worker, "_board_polygon", lambda _board, _inset: [(0, 0)] * 4)
    monkeypatch.setattr(worker, "_layer_id", lambda _board, _name: 31)
    monkeypatch.setattr(worker, "_has_zone", lambda *_args: False)
    board = Board()

    assert worker._materialize_planes(
        board,
        [{"layer": "B.Cu", "net": "GND"}],
        0.2,
    ) == 1
    assert board.zones[0].pad_connection == "solid"
    assert board.zones[0].island_removal == "always"
