from __future__ import annotations

import json

import pytest

from agents.ratsnestpro.decision_engine import (
    DECISION_ANSWER_SCHEMA,
    apply_resolutions,
    design_decisions,
    parse_resolutions,
    public_questions,
)

REQUIREMENT = """
设计一块 STM32G070RBT6 最小控制板。
使用两针连接器输入 5 V，使用 AP2112K-3.3 转换为 3.3 V。
提供标准 10-pin Cortex SWD 接口。
增加一个用户 LED，但暂不指定限流电阻值。
增加一个低电平有效的用户按键，输入不得悬空。
"""


def test_g070_request_becomes_multiple_grounded_choices() -> None:
    decisions = design_decisions(REQUIREMENT)

    assert [item.slot for item in decisions] == [
        "board_outline",
        "layer_count",
        "clock_source",
        "led_resistor",
        "button_bias",
        "mounting",
    ]
    assert "input_power" not in {item.slot for item in decisions}
    assert "main_rail" not in {item.slot for item in decisions}
    assert "debug_port" not in {item.slot for item in decisions}
    assert all(item.recommended_key == "A" for item in decisions)
    assert len(public_questions(decisions)) == 6


def test_structured_answer_is_validated_and_applied() -> None:
    decisions = design_decisions(REQUIREMENT)
    answer = json.dumps(
        {
            "schemaVersion": DECISION_ANSWER_SCHEMA,
            "answers": [
                {"slot": item.slot, "key": item.recommended_key, "text": ""}
                for item in decisions
            ],
        }
    )

    resolved = parse_resolutions(answer, decisions)
    updated = apply_resolutions(REQUIREMENT, resolved)

    assert len(resolved) == len(decisions)
    assert "DECISION: led_resistor=A" in updated
    assert "DECISION: button_bias=A" in updated


def test_incomplete_or_unoffered_answer_is_rejected() -> None:
    decisions = design_decisions(REQUIREMENT)
    incomplete = json.dumps(
        {
            "schemaVersion": DECISION_ANSWER_SCHEMA,
            "answers": [{"slot": decisions[0].slot, "key": "A", "text": ""}],
        }
    )
    unoffered = json.dumps(
        {
            "schemaVersion": DECISION_ANSWER_SCHEMA,
            "answers": [
                {
                    "slot": item.slot,
                    "key": "Z" if index == 0 else "A",
                    "text": "",
                }
                for index, item in enumerate(decisions)
            ],
        }
    )

    with pytest.raises(ValueError, match="incomplete"):
        parse_resolutions(incomplete, decisions)
    with pytest.raises(ValueError, match="not offered"):
        parse_resolutions(unoffered, decisions)
