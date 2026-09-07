import json
from types import SimpleNamespace

from ratsnestpro.eda.fanout_policy import approved_fanout_tracks, load_fanout_approval
from ratsnestpro.eda.fanout_policy import pad_escape_axis
import pytest


def board_for(segments, *, pad_net=1):
    tracks = [{"uuid": str(i), "start": [a, 0], "end": [b, 0],
               "net": 1, "net_name": "3V3", "layer": "F.Cu", "width": width}
              for i, (a, b, width) in enumerate(segments)]
    return SimpleNamespace(
        list_tracks=lambda: tracks,
        list_footprints=lambda: [{"reference": "U1"}],
        footprint_pads=lambda _: [{"type": "smd", "net_index": pad_net,
                                   "layers": ["F.Cu"], "x": 0, "y": 0}],
    )


POLICY = {"minimum_width_mm": 0.2, "max_chain_length_mm": 2.0}


def test_escape_follows_actual_rotated_pad_axis():
    assert pad_escape_axis((1.5, 0.3), 0, (-5, 1)) == (-1, 0)
    assert pad_escape_axis((1.5, 0.3), 90, (1, 5)) == pytest.approx((0, 1))
    assert pad_escape_axis((1.5, 0.3), 45, (4, -4)) == pytest.approx((2 ** -0.5, -2 ** -0.5))


def test_only_short_pad_owned_chain_is_exempted():
    board = board_for([(0, 0.5, 0.2), (0.5, 1, 0.2), (1, 5, 0.4)])
    assert approved_fanout_tracks(board, POLICY, lambda _: 0.4) == {"0", "1"}
    assert not approved_fanout_tracks(board, {}, lambda _: 0.4)
    assert not approved_fanout_tracks(board, POLICY, lambda _: 0.0)


def test_long_chain_cannot_hide_as_many_short_segments():
    board = board_for([(0, 1, 0.2), (1, 2, 0.2), (2, 3, 0.2), (3, 5, 0.4)])
    assert not approved_fanout_tracks(board, POLICY, lambda _: 0.4)


def test_short_shared_escape_keeps_total_length_limit():
    board = board_for([(-0.5, 0, .2), (0, .5, .2), (.5, 1, .2),
                       (-2, -.5, .4), (1, 3, .4)])
    board.footprint_pads = lambda _: [
        {"type": "smd", "net_index": 1, "layers": ["F.Cu"], "x": x, "y": 0}
        for x in (0, .5)
    ]
    assert approved_fanout_tracks(board, POLICY, lambda _: .4) == {"0", "1", "2"}
    # It is not enough for each individual segment to be short.
    assert not approved_fanout_tracks(board, {**POLICY, "max_chain_length_mm": 1}, lambda _: .4)


def test_shared_escape_requires_explicit_path_approval_and_covers_every_edge():
    board = board_for([(-.95, 0, .2), (0, .5, .2), (.5, 2.25, .2),
                       (-2, -.95, .4), (2.25, 4, .4)])
    board.footprint_pads = lambda _: [
        {"type": "smd", "net_index": 1, "layers": ["F.Cu"], "x": x, "y": 0}
        for x in (0, .5)
    ]
    assert not approved_fanout_tracks(board, POLICY, lambda _: .4)
    policy = {**POLICY, "length_basis": "pad_to_trunk_path"}
    assert approved_fanout_tracks(board, policy, lambda _: .4) == {"0", "1", "2"}
    board.list_tracks()[2]["end"] = [3, 0]  # uncovered, overlong branch
    assert not approved_fanout_tracks(board, policy, lambda _: .4)


def test_same_net_interior_crossing_is_not_two_short_chains():
    board = board_for([(-0.75, 0.75, 0.2), (0.75, 3, 0.4)])
    tracks = board.list_tracks()
    tracks.extend([
        {**tracks[0], "uuid": "2", "start": [0, -0.75], "end": [0, 0.75]},
        {**tracks[1], "uuid": "3", "start": [0, 0.75], "end": [0, 3]},
    ])
    board.footprint_pads = lambda _: [
        {"type": "smd", "net_index": 1, "layers": ["F.Cu"], "x": -0.75, "y": 0},
        {"type": "smd", "net_index": 1, "layers": ["F.Cu"], "x": 0, "y": -0.75},
    ]
    assert not approved_fanout_tracks(board, POLICY, lambda _: 0.4)


def test_no_trunk_wrong_pad_and_subminimum_are_rejected():
    for board in (board_for([(0, 1, 0.2)]),
                  board_for([(0, 1, 0.2), (1, 5, 0.4)], pad_net=2),
                  board_for([(0, 1, 0.19), (1, 5, 0.4)])):
        assert not approved_fanout_tracks(board, POLICY, lambda _: 0.4)


def test_approval_must_be_external_and_bound_to_requirement(tmp_path):
    workspace = tmp_path / "runs" / "board"
    workspace.mkdir(parents=True)
    pcb = workspace / "board.kicad_pcb"
    receipt = {**POLICY, "schema": "ratsnest.fanout-approval.v1", "workspace": "board",
               "requirement_digest": "a" * 64, "scope": "power",
               "authorized_by": "operator", "authorization_text": "approved short fanout"}
    (workspace / "board.fanout.json").write_text(json.dumps(receipt))
    assert not load_fanout_approval(pcb, "a" * 64)
    directory = tmp_path / "engineering-approvals"
    directory.mkdir()
    (directory / "board.fanout.json").write_text(json.dumps(receipt))
    assert load_fanout_approval(pcb, "a" * 64)["receipt_digest"]
    assert not load_fanout_approval(pcb, "b" * 64)
