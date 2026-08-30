from __future__ import annotations

from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineContext,
    PipelineState,
    PipelineStep,
    RouteSignalsStep,
    StepResult,
)
from ratsnestpro.orchestration.pipeline_contracts import RouteResult


def _route_result(*, unconnected: int = 1, note: str = "") -> RouteResult:
    return RouteResult(
        method="freerouting",
        routed_nets=0,
        total_nets=8,
        routed_connections=15,
        total_connections=16,
        metric_basis="kicad_connectivity",
        unconnected=unconnected,
        note=note,
    )


def test_local_route_failure_binds_connectivity_repair_tool() -> None:
    result = StepResult(
        step=PipelineStep.ROUTE_SIGNALS,
        checks=[CheckResult(
            name="signals_routed",
            ok=False,
            reason_code="kicad_drc_unconnected",
            message="one physical connection remains open",
        )],
        blocked=True,
    )

    assert pipeline._bind_local_repair_tool(
        result,
        "reroute the DRC-reported SWCLK gap",
    ) == "repair_route_connectivity"


def test_local_width_failure_binds_physical_width_tool() -> None:
    result = StepResult(
        step=PipelineStep.ROUTE_SIGNALS,
        checks=[CheckResult(
            name="routing_physical_invariants",
            ok=False,
            message="GND tracks below minimum 0.400 mm",
        ), CheckResult(
            name="signals_routed",
            ok=False,
            reason_code="kicad_drc_unconnected",
            message="one physical connection remains open",
        )],
        blocked=True,
    )

    assert pipeline._bind_local_repair_tool(
        result,
        "widen the undersized GND segments and verify DRC",
    ) == "repair_physical_track_width"


def test_route_repair_dispatches_only_connectivity_tool(monkeypatch) -> None:
    artifact = _route_result()
    repaired = _route_result(unconnected=0, note="gap closed")
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_repair_drc_connectivity_gaps",
        lambda *_args: calls.append("connectivity") or repaired,
    )
    monkeypatch.setattr(
        pipeline,
        "_repair_undersized_physical_tracks",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("width repair is not owned by this action")
        ),
    )

    result, used_llm = RouteSignalsStep().repair(
        PipelineState(requirement_text="board"),
        PipelineContext(active_recovery_tool="repair_route_connectivity"),
        "",
        artifact,
        [CheckResult(name="signals_routed", ok=False)],
    )

    assert result == repaired
    assert used_llm is False
    assert calls == ["connectivity"]


def test_route_repair_dispatches_only_width_tool(monkeypatch) -> None:
    artifact = _route_result(unconnected=0)
    repaired = artifact.model_copy(update={"note": "width repaired"})
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_repair_undersized_physical_tracks",
        lambda *_args: calls.append("width") or repaired,
    )
    monkeypatch.setattr(
        pipeline,
        "_repair_drc_connectivity_gaps",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("connectivity repair is not owned by this action")
        ),
    )

    result, used_llm = RouteSignalsStep().repair(
        PipelineState(requirement_text="board"),
        PipelineContext(active_recovery_tool="repair_physical_track_width"),
        "",
        artifact,
        [CheckResult(name="routing_physical_invariants", ok=False)],
    )

    assert result == repaired
    assert used_llm is False
    assert calls == ["width"]


def test_unsupported_local_route_tool_returns_to_reflection(monkeypatch) -> None:
    artifact = _route_result()
    monkeypatch.setattr(
        RouteSignalsStep,
        "propose",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("local repair must not invoke global Freerouting")
        ),
    )

    result, used_llm = RouteSignalsStep().repair(
        PipelineState(requirement_text="board"),
        PipelineContext(active_recovery_tool="repair_current_step"),
        "",
        artifact,
        [CheckResult(name="explicit_layer_count_preserved", ok=False)],
    )

    assert result == artifact
    assert used_llm is False
