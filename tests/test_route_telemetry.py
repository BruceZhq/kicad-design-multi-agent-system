from __future__ import annotations

import json
import math
import subprocess

from ratsnestpro.eda import routing
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    _connection_metrics_after_copper_repair,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    NetClass,
    PcbWriteResult,
    RoutePlan,
    RouteResult,
)


def _route_result(**updates: object) -> RouteResult:
    baseline = RouteResult(
        method="freerouting",
        routed_nets=7,
        total_nets=7,
        routed_connections=17,
        total_connections=17,
        metric_basis="kicad_connectivity",
        unconnected=0,
    )
    return baseline.model_copy(update=updates)


def test_plane_only_ahe_preserves_freerouting_connection_telemetry() -> None:
    artifact = _route_result()

    metrics = _connection_metrics_after_copper_repair(
        artifact,
        remaining=0,
        connectivity_changed=False,
    )

    assert metrics == (17, 17, "kicad_connectivity")


def test_plane_materialization_keeps_router_connection_counts(
    monkeypatch,
    tmp_path,
) -> None:
    pcb_path = tmp_path / "routed.kicad_pcb"
    pcb_path.write_text("routed board", encoding="utf-8")
    state = PipelineState(requirement_text="two-layer board", project_name="route")
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = _route_result(routed_tracks=68)

    class Completed:
        returncode = 0
        stdout = (
            'RESULT {"ok": true, "unconnected": 0, "closed_gaps": 0, '
            '"added_zones": 1, "added_vias": 0, "routed_tracks": 68}'
        )

    monkeypatch.setattr(pipeline, "kicad_cli_available", lambda: "kicad-cli")
    monkeypatch.setattr(routing, "kicad_python", lambda: "python")
    monkeypatch.setattr(
        pipeline,
        "_resolved_plane_assignments",
        lambda _state, _layers: [{"layer": "B.Cu", "net": "GND"}],
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Completed())

    repaired = pipeline._repair_power_plane_gaps(
        state,
        PipelineContext(out_dir=str(tmp_path)),
        artifact,
    )

    assert repaired.routed_connections == 17
    assert repaired.total_connections == 17
    assert repaired.metric_basis == "kicad_connectivity"
    assert repaired.unconnected == 0
    assert "AHE materialized 1 planned copper plane" in repaired.note


def test_gap_closing_ahe_recomputes_against_frozen_kicad_baseline() -> None:
    artifact = _route_result(
        routed_nets=0,
        routed_connections=14,
        unconnected=3,
    )

    metrics = _connection_metrics_after_copper_repair(
        artifact,
        remaining=1,
        connectivity_changed=True,
    )

    assert metrics == (
        16,
        17,
        "kicad_connectivity_total+kicad_drc_unconnected_after_repair",
    )


def test_gap_closing_ahe_does_not_invent_a_missing_connection_baseline() -> None:
    artifact = _route_result(
        routed_connections=-1,
        total_connections=-1,
        metric_basis="unavailable",
        unconnected=2,
    )

    metrics = _connection_metrics_after_copper_repair(
        artifact,
        remaining=0,
        connectivity_changed=True,
    )

    assert metrics == (
        -1,
        -1,
        "kicad_drc_unconnected_after_repair_without_baseline",
    )


def test_route_check_owns_drc_gap_by_real_refs_and_coordinates(
    monkeypatch,
    tmp_path,
) -> None:
    pcb_path = tmp_path / "board.kicad_pcb"
    pcb_path.write_text("board", encoding="utf-8")
    report_path = pcb_path.with_suffix(".route-final.drc.json")
    report_path.write_text(
        json.dumps({
            "violations": [],
            "schematic_parity": [],
            "unconnected_items": [{
                "type": "unconnected_items",
                "severity": "error",
                "description": "Missing connection between items",
                "items": [
                    {
                        "description": "Pad 45 [SWDIO] of U1 on F.Cu",
                        "pos": {"x": 44.675, "y": 21.75},
                    },
                    {
                        "description": "PTH pad 2 [SWDIO] of J2",
                        "pos": {"x": 66.27, "y": 18.0},
                    },
                ],
            }],
        }),
        encoding="utf-8",
    )
    state = PipelineState(requirement_text="two-layer board", project_name="route")
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = _route_result(
        routed_nets=0,
        routed_connections=16,
        unconnected=1,
        note="freerouting thread D55415/AFDA6A stopped",
    )
    monkeypatch.setattr(
        pipeline,
        "_routing_physical_invariant_blockers",
        lambda _state: [],
    )

    check = next(
        item
        for item in pipeline.RouteSignalsStep().check(state, artifact)
        if item.name == "signals_routed"
    )

    assert check.affected_refs == ["J2", "U1"]
    assert check.reason_code == "kicad_drc_unconnected"
    assert check.evidence["route_gaps"] == [{
        "net": "SWDIO",
        "endpoints": [
            {
                "ref": "U1",
                "pad": "45",
                "x_mm": 44.675,
                "y_mm": 21.75,
                "layer": "F.Cu",
            },
            {
                "ref": "J2",
                "pad": "2",
                "x_mm": 66.27,
                "y_mm": 18.0,
                "layer": "F.Cu",
            },
        ],
    }]
    failure = pipeline.make_failure(
        step=PipelineStep.ROUTE_SIGNALS.value,
        check_name=check.name,
        message=check.message,
        repair_available=True,
        reason_code=check.reason_code,
        affected_refs=check.affected_refs,
        evidence=check.evidence,
    )
    assert failure.affected_refs == ["J2", "U1"]


def test_route_repair_closes_drc_gap_before_replanning_invariant(
    monkeypatch,
    tmp_path,
) -> None:
    state = PipelineState(requirement_text="board", project_name="route")
    artifact = _route_result(
        routed_nets=0,
        routed_connections=16,
        unconnected=1,
    )
    closed = artifact.model_copy(update={
        "routed_nets": artifact.total_nets,
        "routed_connections": artifact.total_connections,
        "unconnected": 0,
    })
    monkeypatch.setattr(
        pipeline,
        "_repair_drc_connectivity_gaps",
        lambda *_args: closed,
    )
    monkeypatch.setattr(
        pipeline,
        "_repair_undersized_physical_tracks",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("width repair must wait for gap acceptance")
        ),
    )

    repaired, used_llm = pipeline.RouteSignalsStep().repair(
        state,
        PipelineContext(out_dir=str(tmp_path)),
        "",
        artifact,
        [
            pipeline.CheckResult(name="signals_routed", ok=False),
            pipeline.CheckResult(name="routing_physical_invariants", ok=False),
        ],
    )

    assert repaired == closed
    assert used_llm is False


def test_gap_repair_refills_zones_before_accepting_authoritative_gaps(
    monkeypatch,
    tmp_path,
) -> None:
    pcb_path = tmp_path / "routed.kicad_pcb"
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 20, 20)
    board.add_zone(
        "B.Cu",
        "GND",
        [(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)],
    )
    board.save(pcb_path)
    state = PipelineState(requirement_text="two-layer board", project_name="route")
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = _route_result(
        routed_nets=0,
        routed_connections=14,
        total_connections=17,
        unconnected=3,
    )
    stale_report = pcb_path.with_suffix(".ahe-route.drc.json")
    stale_report.write_text(json.dumps({
        "violations": [],
        "unconnected_items": [
            {"severity": "error", "type": "unconnected_items"},
            {"severity": "error", "type": "unconnected_items"},
            {"severity": "error", "type": "unconnected_items"},
        ],
    }), encoding="utf-8")
    normalized_gap = pipeline._DrcGap(
        left=pipeline._DrcEndpoint(3.0, 4.0, "NORMALIZED", "F.Cu"),
        right=pipeline._DrcEndpoint(8.0, 4.0, "NORMALIZED", "F.Cu"),
    )
    normalized = pipeline._DrcSnapshot(
        findings=("kicad_cli:unconnected_items:normalized",),
        non_connectivity_errors=(),
        gaps=(normalized_gap,),
        reported_unconnected=1,
    )
    monkeypatch.setattr(pipeline, "kicad_cli_available", lambda: "kicad-cli")

    calls: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_refill_copper_zones",
        lambda _path: calls.append("refill") or False,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_kicad_drc_snapshot",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale gaps must not be read after refill failure")
        ),
    )

    assert pipeline._repair_drc_connectivity_gaps(
        state,
        PipelineContext(out_dir=str(tmp_path)),
        artifact,
    ) == artifact
    assert calls == ["refill"]

    calls.clear()

    def authoritative_drc(*_args) -> pipeline._DrcSnapshot:
        assert calls == ["refill"]
        calls.append("drc")
        return normalized

    observed_gaps: list[pipeline._DrcGap] = []
    monkeypatch.setattr(
        pipeline,
        "_refill_copper_zones",
        lambda _path: calls.append("refill") or True,
    )
    monkeypatch.setattr(
        pipeline,
        "_run_kicad_drc_snapshot",
        authoritative_drc,
    )
    monkeypatch.setattr(
        pipeline,
        "_obstacle_aware_copper_paths",
        lambda _board, gap, **_kwargs: observed_gaps.append(gap) or [],
    )
    monkeypatch.setattr(
        pipeline,
        "_micro_jump_copper_patches",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        pipeline,
        "_routing_physical_invariant_blockers",
        lambda _state: [],
    )

    assert pipeline._repair_drc_connectivity_gaps(
        state,
        PipelineContext(out_dir=str(tmp_path)),
        artifact,
    ) == artifact
    assert calls == ["refill", "drc"]
    assert observed_gaps == [normalized_gap]


def test_post_route_width_repair_is_scoped_and_transactional(
    monkeypatch,
    tmp_path,
) -> None:
    pcb_path = tmp_path / "routed.kicad_pcb"
    board = PcbBoard.blank()
    board.add_track(1, 1, 2, 1, width=0.3004, net="GND")
    board.add_track(1, 2, 2, 2, width=0.20, net="SIGNAL")
    board.add_track(1, 3, 2, 3, width=0.10, net="UART_TX")
    board.save(pcb_path)
    state = PipelineState(
        requirement_text="GND trunk track width must be at least 0.40 mm.",
        project_name="route-width",
    )
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = _route_result()
    clean = pipeline._DrcSnapshot(
        findings=(),
        non_connectivity_errors=(),
        gaps=(),
    )
    monkeypatch.setattr(pipeline, "kicad_cli_available", lambda: "kicad-cli")
    monkeypatch.setattr(
        pipeline,
        "_run_kicad_drc_snapshot",
        lambda *_args, **_kwargs: clean,
    )

    repaired = pipeline._repair_undersized_physical_tracks(state, artifact)

    widths = {
        track["net_name"]: track["width"]
        for track in PcbBoard.load(pcb_path).list_tracks()
    }
    assert widths == {"GND": 0.4, "SIGNAL": 0.2, "UART_TX": 0.15}
    assert repaired != artifact
    assert "AHE widened 2 physical track segment(s)" in repaired.note

    board = PcbBoard.load(pcb_path)
    board.add_track(3, 1, 4, 1, width=0.30, net="GND")
    board.save(pcb_path)
    original_bytes = pcb_path.read_bytes()
    regressed = pipeline._DrcSnapshot(
        findings=("kicad_cli:clearance:new error",),
        non_connectivity_errors=("kicad_cli:clearance:new error",),
        gaps=(),
    )
    snapshots = iter((clean, regressed))
    monkeypatch.setattr(
        pipeline,
        "_run_kicad_drc_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )

    rejected = pipeline._repair_undersized_physical_tracks(state, repaired)

    assert rejected == repaired
    assert pcb_path.read_bytes() == original_bytes


def test_post_route_width_repair_recenters_unique_smd_escape(
    monkeypatch,
    tmp_path,
) -> None:
    from ratsnestpro.eda.vendor.sexpr import loads

    pcb_path = tmp_path / "fine-pitch.kicad_pcb"
    board = PcbBoard.blank()
    gnd = board.add_net("GND")
    power = board.add_net("3V3")
    footprint = loads(
        f"""
        (footprint "Test:LQFP"
          (layer "F.Cu")
          (at 10 10)
          (property "Reference" "U1")
          (pad "8" smd roundrect
            (at -1 -0.25) (size 1.5 0.3)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net {power} "3V3"))
          (pad "9" smd roundrect
            (at -1 0.25) (size 1.5 0.3)
            (layers "F.Cu" "F.Mask" "F.Paste")
            (net {gnd} "GND")))
        """
    )
    assert isinstance(footprint, list)
    board.root.append(footprint)
    upstream_uuid = board.add_track(
        7.8, 10.2322, 8, 10.2322, width=0.4, net="GND"
    )
    first_escape = board.add_track(
        8, 10.2322, 8.9822, 10.2322, width=0.3004, net="GND"
    )
    second_escape = board.add_track(
        8.9822, 10.2322, 9, 10.25, width=0.3004, net="GND"
    )
    board.add_track(8.7, 9.75, 9, 9.75, width=0.2, net="3V3")
    board.save(pcb_path)

    state = PipelineState(
        requirement_text="GND trunk track width must be at least 0.40 mm.",
        project_name="fine-pitch-width",
    )
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = _route_result()
    clean = pipeline._DrcSnapshot(
        findings=(),
        non_connectivity_errors=(),
        gaps=(),
    )
    regressed = pipeline._DrcSnapshot(
        findings=("kicad_cli:clearance:new error",),
        non_connectivity_errors=("kicad_cli:clearance:new error",),
        gaps=(),
    )
    snapshots = iter((clean, regressed, clean))
    monkeypatch.setattr(pipeline, "kicad_cli_available", lambda: "kicad-cli")
    monkeypatch.setattr(
        pipeline,
        "_run_kicad_drc_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        pipeline,
        "_routing_physical_invariant_blockers",
        lambda _state: [],
    )

    repaired = pipeline._repair_undersized_physical_tracks(state, artifact)

    tracks = {
        track["uuid"]: track
        for track in PcbBoard.load(pcb_path).list_tracks()
    }
    assert first_escape not in tracks
    assert second_escape not in tracks
    assert tracks[upstream_uuid]["end"] == [8.0, 10.25]
    centered = [
        track
        for track in tracks.values()
        if track["net_name"] == "GND" and track["uuid"] != upstream_uuid
    ]
    assert len(centered) == 1
    assert centered[0]["start"] == [9.0, 10.25]
    assert centered[0]["end"] == [8.0, 10.25]
    assert centered[0]["width"] == 0.4
    assert "centerline-rerouted 2 SMD escape segment(s)" in repaired.note


def test_gap_route_search_avoids_inflated_other_net_copper() -> None:
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 10, 10)
    board.add_track(5, 2, 5, 8, width=0.5, net="BLOCKED")
    gap = pipeline._DrcGap(
        left=pipeline._DrcEndpoint(1, 5, "SIGNAL", "F.Cu"),
        right=pipeline._DrcEndpoint(9, 5, "SIGNAL", "F.Cu"),
    )

    paths = pipeline._obstacle_aware_copper_paths(
        board,
        gap,
        width=0.2,
        clearance=0.15,
        budget=2,
    )

    assert paths
    for start, end in paths[0]:
        assert pipeline._segment_distance(
            start,
            end,
            (5, 2),
            (5, 8),
        ) >= 0.5 / 2 + 0.2 / 2 + 0.15 - 1e-9


def test_gap_route_width_freezes_explicit_net_minimum() -> None:
    state = PipelineState(
        requirement_text="GND trunk track width must be at least 0.40 mm.",
        project_name="route-width",
    )
    state.artifacts[PipelineStep.ROUTE_PLAN] = RoutePlan(
        layers=2,
        net_classes=[NetClass(
            name="signals",
            width=0.2,
            clearance=0.15,
        )],
    )

    assert pipeline._frozen_gap_route_width(state, "GND") == 0.4
    assert pipeline._frozen_gap_route_width(state, "SWCLK") == 0.2


def test_gap_route_micro_jump_uses_via_pair_after_front_layer_dead_end() -> None:
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 10, 10)
    board.add_track(5, 0.4, 5, 9.6, width=0.5, net="BLOCKED")
    gap = pipeline._DrcGap(
        left=pipeline._DrcEndpoint(1, 5, "SIGNAL", "F.Cu"),
        right=pipeline._DrcEndpoint(9, 5, "SIGNAL", "F.Cu"),
    )

    patches = pipeline._micro_jump_copper_patches(
        board,
        gap,
        width=0.2,
        clearance=0.15,
        via_diameter=0.6,
        budget=2,
    )

    assert patches
    assert len(patches[0].vias) == 2
    assert any(layer == "B.Cu" for _, _, layer in patches[0].tracks)
    assert all(
        min(
            math.dist(via, (gap.left.x, gap.left.y)),
            math.dist(via, (gap.right.x, gap.right.y)),
        ) >= 1.0
        for via in patches[0].vias
    )
