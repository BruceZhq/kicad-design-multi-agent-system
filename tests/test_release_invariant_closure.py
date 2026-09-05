from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.orchestration.pipeline import (
    ManufactureStep,
    PipelineState,
    PipelineStep,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    BoardPartition,
    BoardZone,
    ManufactureResult,
    PcbWriteResult,
    SelectionPlan,
)
from ratsnestpro.orchestration.placement_constraints import bind_zone_targets
from ratsnestpro.orchestration.release_invariants import (
    audit_pcb_invariants,
    build_release_invariant_manifest,
    extract_requirement_invariants,
)


@pytest.mark.parametrize(
    "requirement",
    [
        (
            "板框不超过 40mm x 30mm，只允许两层铜。"
            "底层保持连续 GND 铺铜。"
            "5V 和 GND 主干线宽不得小于 0.30mm。"
            "C3 去耦电容距离 U1 电源引脚不超过 3mm。"
            "四角各放置一个 M2 非金属化安装孔。"
        ),
        (
            "The board outline must not exceed 40 x 30 mm and must use "
            "exactly two layers. Maintain a continuous GND copper pour on B.Cu. "
            "The 5V and GND trunk trace width must be at least 0.30 mm. "
            "Keep each decoupling capacitor within 3 mm of its IC power pin. "
            "Use 4 non-plated mounting holes sized for M2 screws, one at each "
            "corner."
        ),
        (
            "Create a two-layer PCB no larger than 40 mm by 30 mm. Maintain "
            "a continuous GND copper pour on B.Cu. The 5V and GND trunk trace "
            "width must be at least 0.30 mm. Keep each decoupling capacitor "
            "within 3 mm of its IC power pin. Use four non-plated M2 mounting "
            "holes, one at each corner."
        ),
        (
            "设计为 2 层板，尺寸不超过 40mm × 30mm，底层尽量铺设完整 GND。"
            "5V 和 GND 主干线宽不得小于 0.30mm，去耦距离不超过 3mm。"
            "DECISION: mounting=A — Four non-plated M2 mounting holes."
        ),
    ],
    ids=("chinese", "english", "english-by-limit", "runtime-envelope"),
)
def test_extracts_explicit_release_invariants_in_both_languages(
    requirement: str,
) -> None:
    invariants = extract_requirement_invariants(requirement)

    assert invariants.copper_layer_count == 2
    assert invariants.max_board_width_mm == pytest.approx(40.0)
    assert invariants.max_board_height_mm == pytest.approx(30.0)
    assert invariants.ground_plane_required
    assert invariants.ground_plane_layer == "B.Cu"
    assert invariants.continuous_ground_required
    assert invariants.minimum_track_width_mm == pytest.approx(0.30)
    assert set(invariants.minimum_track_width_nets) == {"5V", "GND"}
    assert invariants.decoupling_max_distance_mm == pytest.approx(3.0)
    assert invariants.mounting_hole_count == 4
    assert invariants.mounting_holes_non_plated


def test_original_constraints_win_over_conflicting_hitl_patch() -> None:
    requirement = (
        "请设计一块双层板，尺寸不超过 40 mm × 30 mm，底层连续铺地。\n"
        "DECISION: board_outline=A — For board_outline, the user confirmed: "
        "50 x 40 mm rectangular outline.\n"
        "DECISION: layer_count=B — For layer_count, the user confirmed: "
        "4 copper layers."
    )

    invariants = extract_requirement_invariants(requirement)

    assert invariants.copper_layer_count == 2
    assert invariants.max_board_width_mm == pytest.approx(40.0)
    assert invariants.max_board_height_mm == pytest.approx(30.0)
    assert invariants.ground_plane_required
    assert invariants.ground_plane_layer == "B.Cu"
    assert invariants.continuous_ground_required

    bad_board = _FakeBoard(
        copper_layers=4,
        width=50.0,
        height=40.0,
        zones=[],
        footprints=[],
        pads={},
        tracks=[],
    )
    finding_ids = {
        finding.invariant_id
        for finding in audit_pcb_invariants(invariants, bad_board, [])
    }
    assert {
        "copper_layer_count",
        "board_max_width",
        "board_max_height",
        "ground_plane_materialized",
    } <= finding_ids


def _mounting_zones() -> list[BoardZone]:
    return [
        BoardZone(
            name="mounting hole bottom right",
            kind="mechanical_mounting",
            x1=35.0,
            y1=25.0,
            x2=40.0,
            y2=30.0,
        ),
        BoardZone(
            name="mounting hole top left",
            kind="mechanical_mounting",
            x1=0.0,
            y1=0.0,
            x2=5.0,
            y2=5.0,
        ),
        BoardZone(
            name="mounting hole bottom left",
            kind="mechanical_mounting",
            x1=0.0,
            y1=25.0,
            x2=5.0,
            y2=30.0,
        ),
        BoardZone(
            name="mounting hole top right",
            kind="mechanical_mounting",
            x1=35.0,
            y1=0.0,
            x2=40.0,
            y2=5.0,
        ),
    ]


def _corner_bindings(partition: BoardPartition) -> dict[str, str]:
    return {
        zone.name.removeprefix("mounting hole "): zone.target_ref
        for zone in partition.zones
    }


def test_mounting_holes_bind_to_corners_stably_one_to_one() -> None:
    partition = BoardPartition(
        board_width=40.0,
        board_height=30.0,
        zones=_mounting_zones(),
    )
    roles = {
        "H4": "mechanical_mounting_hole",
        "H2": "mechanical_mounting_hole",
        "H1": "mechanical_mounting_hole",
        "H3": "mechanical_mounting_hole",
    }

    first = bind_zone_targets(partition, roles)
    second = bind_zone_targets(first, roles)

    expected = {
        "top left": "H1",
        "top right": "H2",
        "bottom left": "H3",
        "bottom right": "H4",
    }
    assert _corner_bindings(first) == expected
    assert _corner_bindings(second) == expected
    assert {zone.target_ref for zone in first.zones} == set(roles)
    assert all(not zone.target_ref for zone in partition.zones)


class _FakeBoard:
    def __init__(
        self,
        *,
        copper_layers: int,
        width: float,
        height: float,
        zones: list[dict[str, Any]],
        footprints: list[str],
        pads: dict[str, list[dict[str, Any]]],
        tracks: list[dict[str, Any]],
    ) -> None:
        self._copper_layers = copper_layers
        self._width = width
        self._height = height
        self._zones = zones
        self._footprints = footprints
        self._pads = pads
        self._tracks = tracks

    def get_board_info(self) -> dict[str, Any]:
        return {"copper_layers": self._copper_layers}

    def get_board_extents(self) -> dict[str, float]:
        return {"width": self._width, "height": self._height}

    def list_zones(self) -> list[dict[str, Any]]:
        return self._zones

    def list_footprints(self) -> list[dict[str, str]]:
        return [{"reference": ref} for ref in self._footprints]

    def footprint_pads(self, ref: str) -> list[dict[str, Any]]:
        return self._pads[ref]

    def list_tracks(self) -> list[dict[str, Any]]:
        return self._tracks


def _parts() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(ref="U1", role="controller", symbol="", footprint="", value=""),
        SimpleNamespace(
            ref="C3",
            role="supply_decoupling_capacitor",
            symbol="",
            footprint="",
            value="100nF",
        ),
        *[
            SimpleNamespace(
                ref=f"H{index}",
                role="mechanical_mounting_hole",
                symbol="Mechanical:MountingHole",
                footprint="MountingHole:MountingHole_2.2mm_M2",
                value="M2 NPTH",
            )
            for index in range(1, 5)
        ],
    ]


def _pads(*, plated_holes: bool, decoupling_x: float) -> dict[str, list[dict[str, Any]]]:
    hole_type = "thru_hole" if plated_holes else "np_thru_hole"
    return {
        "U1": [
            {"type": "smd", "net": "5V", "x": 10.0, "y": 10.0},
            {"type": "smd", "net": "GND", "x": 10.0, "y": 11.0},
        ],
        "C3": [
            {"type": "smd", "net": "5V", "x": decoupling_x, "y": 10.0},
            {"type": "smd", "net": "GND", "x": decoupling_x, "y": 11.0},
        ],
        **{
            f"H{index}": [
                {"type": hole_type, "net": "", "x": 0.0, "y": 0.0}
            ]
            for index in range(1, 5)
        },
    }


def _invariants():
    return extract_requirement_invariants(
        "The board outline must not exceed 40 x 30 mm and must use exactly "
        "two layers. Maintain a continuous GND copper pour on B.Cu. The 5V "
        "and GND trunk trace width must be at least 0.30 mm. Keep each "
        "decoupling capacitor within 3 mm of its IC power pin. Use 4 "
        "non-plated mounting holes sized for M2 screws, one at each corner."
    )


def _ground_zone(*, split: bool = False, coverage: float = 1.0) -> dict[str, Any]:
    right = 40.0 * coverage
    source = [[0.0, 0.0], [right, 0.0], [right, 30.0], [0.0, 30.0]]
    filled = [{"layer": "B.Cu", "points": source, "island": False}]
    if split:
        filled.append({"layer": "B.Cu", "points": source, "island": True})
    return {
        "net": "GND",
        "layer": "B.Cu",
        "points": source,
        "filled_polygons": filled,
    }


def test_final_pcb_invariant_audit_accepts_materialized_contract() -> None:
    board = _FakeBoard(
        copper_layers=2,
        width=40.0,
        height=30.0,
        zones=[_ground_zone()],
        footprints=["U1", "C3", "H1", "H2", "H3", "H4"],
        pads=_pads(plated_holes=False, decoupling_x=12.5),
        tracks=[
            {"net_name": "5V", "width": 0.30},
            {"net_name": "GND", "width": 0.35},
        ],
    )

    assert audit_pcb_invariants(_invariants(), board, _parts()) == []


def test_final_pcb_invariant_audit_reports_each_materialized_violation() -> None:
    board = _FakeBoard(
        copper_layers=4,
        width=41.0,
        height=31.0,
        zones=[],
        footprints=["U1", "C3", "H1", "H2", "H3"],
        pads=_pads(plated_holes=True, decoupling_x=14.0),
        tracks=[
            {"net_name": "5V", "width": 0.20},
            {"net_name": "GND", "width": 0.29},
        ],
    )

    finding_ids = {
        finding.invariant_id
        for finding in audit_pcb_invariants(_invariants(), board, _parts())
    }

    assert {
        "copper_layer_count",
        "board_max_width",
        "board_max_height",
        "ground_plane_materialized",
        "mounting_holes_materialized",
        "mounting_holes_non_plated",
        "minimum_track_width",
        "decoupling_distance",
    } <= finding_ids


def test_final_pcb_invariant_audit_reports_selection_hole_count() -> None:
    board = _FakeBoard(
        copper_layers=2,
        width=40.0,
        height=30.0,
        zones=[_ground_zone()],
        footprints=["U1", "C3", "H1", "H2", "H3"],
        pads=_pads(plated_holes=False, decoupling_x=12.5),
        tracks=[
            {"net_name": "5V", "width": 0.30},
            {"net_name": "GND", "width": 0.30},
        ],
    )

    findings = audit_pcb_invariants(_invariants(), board, _parts()[:-1])

    assert {finding.invariant_id for finding in findings} == {
        "mounting_hole_count"
    }


@pytest.mark.parametrize(
    "zone",
    [
        {"net": "GND", "layer": "B.Cu", "points": [], "filled_polygons": []},
        _ground_zone(split=True),
        _ground_zone(coverage=0.5),
    ],
    ids=("unfilled", "island", "insufficient-coverage"),
)
def test_continuous_ground_fails_closed_without_geometric_proof(
    zone: dict[str, Any],
) -> None:
    board = _FakeBoard(
        copper_layers=2,
        width=40.0,
        height=30.0,
        zones=[zone],
        footprints=["U1", "C3", "H1", "H2", "H3", "H4"],
        pads=_pads(plated_holes=False, decoupling_x=12.5),
        tracks=[
            {"net_name": "5V", "width": 0.30},
            {"net_name": "GND", "width": 0.30},
        ],
    )

    assert "continuous_ground_geometry" in {
        finding.invariant_id
        for finding in audit_pcb_invariants(_invariants(), board, _parts())
    }


def test_npth_read_failure_is_release_blocking() -> None:
    board = _FakeBoard(
        copper_layers=2,
        width=40.0,
        height=30.0,
        zones=[_ground_zone()],
        footprints=["U1", "C3", "H1", "H2", "H3", "H4"],
        pads=_pads(plated_holes=False, decoupling_x=12.5),
        tracks=[
            {"net_name": "5V", "width": 0.30},
            {"net_name": "GND", "width": 0.30},
        ],
    )
    original = board.footprint_pads

    def unreadable(ref: str):
        if ref == "H4":
            raise ValueError("corrupt pad geometry")
        return original(ref)

    board.footprint_pads = unreadable  # type: ignore[method-assign]

    findings = audit_pcb_invariants(_invariants(), board, _parts())

    npth = next(
        finding
        for finding in findings
        if finding.invariant_id == "mounting_holes_non_plated"
    )
    assert "H4 (pad geometry unreadable)" in npth.message


def test_manufacture_checkpoint_rejects_stale_pcb_receipt(tmp_path) -> None:
    requirement = "Create a PCB."
    pcb_path = tmp_path / "board.kicad_pcb"
    PcbBoard.blank().save(pcb_path)
    manifest = build_release_invariant_manifest(
        project_name="board",
        requirement=requirement,
        pcb_path=pcb_path,
        findings=[],
        blockers=[],
    )
    manifest_path = tmp_path / "board.release_invariants.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    state = PipelineState(requirement_text=requirement, project_name="board")
    state.artifacts[PipelineStep.SELECTION] = SelectionPlan(parts=[])
    state.artifacts[PipelineStep.LAYOUT_WRITE] = PcbWriteResult(
        pcb_path=str(pcb_path)
    )
    artifact = ManufactureResult(
        requirement_invariants_path=str(manifest_path),
        release_identity=manifest.release_identity,
        requirement_release_ready=True,
        requirement_release_blockers=[],
    )
    step = ManufactureStep()

    assert step.resume_artifact_is_current(state, artifact)

    pcb_path.write_text(
        pcb_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    assert not step.resume_artifact_is_current(state, artifact)
