from ratsnestpro.orchestration.pipeline_contracts import SelectedPart
from ratsnestpro.orchestration.component_resolution import ComponentResolutionService
from ratsnestpro.orchestration import pipeline as p
from ratsnestpro.repair.draft import needs_prerequisite_repair
import pytest


def test_pinless_mechanical_candidate_can_be_recovered():
    service = ComponentResolutionService(
        resolve_symbol=lambda name: True if name == "Mechanical:MountingHole" else None,
        symbol_pins=lambda name: [] if name == "Mechanical:MountingHole" else None,
        symbol_properties=lambda name: {},
        footprint_pads=lambda name: [{"number": "", "type": "np_thru_hole"}],
        symbol_index=lambda: ["Mechanical:MountingHole"],
    )
    part = SelectedPart(ref="H1", value="MountingHole", role="mounting_hole",
                        symbol="MountingHole:MountingHole",
                        footprint="MountingHole:MountingHole_2.2mm_M2")
    result = service.resolve(part)
    assert result.release_ready
    assert part.symbol == "Mechanical:MountingHole"


@pytest.mark.parametrize("origin,expected", [
    (None, True), (p.FailureOrigin.DESIGN, True),
    (p.FailureOrigin.INFRASTRUCTURE, False),
    (p.FailureOrigin.HARNESS, False),
    (p.FailureOrigin.EXTERNAL_EVIDENCE, False),
])
def test_only_blocking_design_prerequisites_get_local_repair(origin, expected):
    check = p.CheckResult(name="footprints_bound_after_selection", ok=False,
                          blocks_execution=True, origin=origin)
    assert needs_prerequisite_repair(p.PipelineStep.SELECTION, object(), [check]) is expected
    check.blocks_execution = False
    assert not needs_prerequisite_repair(p.PipelineStep.SELECTION, object(), [check])
