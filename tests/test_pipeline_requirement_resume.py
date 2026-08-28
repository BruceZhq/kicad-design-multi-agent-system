from __future__ import annotations

from agents.ratsnestpro.tools import (
    _requirement_contract_payload,
    _requirement_invalidation_step,
)
from ratsnestpro.orchestration.pipeline import PipelineStep

_BASE_REQUIREMENT = "workflow_mode: build\nDesign a KiCad NE555 LED board."
_RESUME_REQUEST = (
    "余额已恢复，请严格复用已保存的检查点，从 layout_partition 第9步继续完成当前任务，"
    "不要重新执行前8步。"
)


def test_numbered_checkpoint_resume_does_not_invalidate_pipeline() -> None:
    resumed = f"{_BASE_REQUIREMENT}\n\nUSER CHANGE REQUEST:\n{_RESUME_REQUEST}"

    assert _requirement_contract_payload(resumed) == _requirement_contract_payload(
        _BASE_REQUIREMENT
    )
    assert _requirement_invalidation_step(_BASE_REQUIREMENT, resumed) is None


def test_real_layout_change_still_invalidates_from_layout_partition() -> None:
    amended = (
        f"{_BASE_REQUIREMENT}\n\nUSER CHANGE REQUEST:\n"
        "继续执行，但把板框尺寸改为100x80mm。"
    )

    assert (
        _requirement_invalidation_step(_BASE_REQUIREMENT, amended)
        == PipelineStep.LAYOUT_PARTITION
    )
