from pathlib import Path

from agents.ratsnestpro.hardware_state import actual_artifacts


def test_actual_artifacts_includes_pipeline_result_evidence(tmp_path: Path) -> None:
    schematic = tmp_path / "board.kicad_sch"
    pipeline_result = tmp_path / "pipeline_result.json"
    schematic.write_text("schematic", encoding="utf-8")
    pipeline_result.write_text("{}", encoding="utf-8")

    paths = actual_artifacts(
        {
            "hardware": {
                "actual_files": [str(schematic)],
                "pipeline_result_path": str(pipeline_result),
            }
        }
    )

    assert paths == [str(schematic), str(pipeline_result)]
