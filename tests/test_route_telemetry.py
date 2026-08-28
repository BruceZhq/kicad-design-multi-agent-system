from __future__ import annotations

import subprocess

from ratsnestpro.eda import routing
from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    PipelineStep,
    _connection_metrics_after_copper_repair,
)
from ratsnestpro.orchestration.pipeline_contracts import PcbWriteResult, RouteResult


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
