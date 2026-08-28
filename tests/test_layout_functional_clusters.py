from types import SimpleNamespace

import ratsnestpro.orchestration.pipeline as pipeline
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    LayoutGeneralStep,
    PcbPlacement,
    PcbPlacementPlan,
    PipelineContext,
    PipelineState,
    Severity,
)


def test_functional_anchor_uses_role_and_connectivity_not_physical_nearness() -> None:
    anchor = pipeline._functional_anchor_ref(
        "C5",
        "ldo_output_capacitor",
        {
            "C5": "ldo_output_capacitor",
            "U1": "mcu",
            "U2": "ldo_regulator",
        },
        {
            "C5": (1.0, 1.0),
            "U1": (2.0, 1.0),
            "U2": (20.0, 10.0),
        },
        connected_refs={"C5": {"U1": 0.05, "U2": 1.0}},
        allow_connectors=True,
        eligible_anchor_refs={"U1", "U2"},
    )

    assert anchor == "U2"


def test_maxrect_emits_functional_anchor_before_large_dependent(
    monkeypatch,
) -> None:
    boxes = {
        "large-dependent": (0.0, 0.0, 10.0, 10.0),
        "small-anchor": (0.0, 0.0, 2.0, 2.0),
        "other": (0.0, 0.0, 4.0, 4.0),
    }
    monkeypatch.setattr(
        pipeline,
        "_placement_bbox",
        lambda footprint: boxes[footprint],
    )
    monkeypatch.setattr(
        pipeline.config,
        "process_capability",
        lambda: SimpleNamespace(min_board_edge_clearance=0.5),
    )

    placements, unplaced = pipeline._maxrect_pack(
        ["large-dependent", "small-anchor", "other"],
        {
            "large-dependent": "large-dependent",
            "small-anchor": "small-anchor",
            "other": "other",
        },
        40.0,
        30.0,
        0.2,
        dependency={"large-dependent": "small-anchor"},
    )

    assert not unplaced
    refs = [placement.ref for placement in placements]
    assert refs.index("small-anchor") < refs.index("large-dependent")


def test_proximity_repair_repackages_cluster_when_single_body_move_stagnates(
    monkeypatch,
) -> None:
    step = LayoutGeneralStep()
    state = PipelineState(requirement_text="board", project_name="board")
    original = PcbPlacementPlan(
        board_width=40.0,
        board_height=30.0,
        placements=[PcbPlacement(ref="C1", x=5.0, y=5.0)],
        rationale="baseline",
    )
    repacked = original.model_copy(update={"rationale": "cluster repacked"})
    failure = CheckResult(
        name="decoupling_near_mcu",
        ok=False,
        severity=Severity.ERROR,
        message="dependent remains outside its real-pad distance limit",
    )
    monkeypatch.setattr(
        pipeline,
        "_repair_proximity_placements",
        lambda _state, artifact: artifact,
    )
    monkeypatch.setattr(step, "check", lambda _state, _artifact: [failure])
    monkeypatch.setattr(
        step,
        "propose",
        lambda _state, _ctx, _knowledge: (repacked, False),
    )

    repaired, used_llm = step.repair(
        state,
        PipelineContext(),
        "",
        original,
        [failure],
    )

    assert repaired is repacked
    assert used_llm is False
