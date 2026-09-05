from __future__ import annotations

import json

import pytest

from agents.ratsnestpro.decision_engine import (
    DECISION_ANSWER_SCHEMA,
    apply_resolutions,
    design_decisions,
    parse_resolutions,
    public_questions,
    reconcile_resolutions,
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


def test_explicit_chinese_board_contract_is_not_sent_to_hitl() -> None:
    requirement = (
        "请设计一块双层控制板，板子尺寸不超过 40 mm × 30 mm，"
        "底层连续铺地。"
    )

    slots = {item.slot for item in design_decisions(requirement)}

    assert "board_outline" not in slots
    assert "layer_count" not in slots


def test_explicit_chinese_external_button_pullup_is_not_sent_to_hitl() -> None:
    requirement = "增加一个带外部 10 kΩ 上拉的低电平有效按键。"

    slots = {item.slot for item in design_decisions(requirement)}

    assert "button_bias" not in slots


def test_hitl_cannot_override_an_explicit_original_constraint() -> None:
    requirement = "请设计一块双层板，尺寸不超过 40 mm × 30 mm。"

    with pytest.raises(ValueError, match="cannot override"):
        apply_resolutions(
            requirement,
            [{
                "slot": "board_outline",
                "kind": "assumption",
                "key": "A",
                "label": "50 x 40 mm",
                "value": "For board_outline, use 50 x 40 mm.",
                "citation": "engineering default",
            }],
        )


def test_esp32_module_with_uart_download_does_not_get_clock_or_swd_questions() -> None:
    requirement = (
        "请设计 ESP32-C3 运动遥测节点，主控使用 ESP32-C3-WROOM-02U，"
        "SDA/SCL 各 4.7 kΩ 上拉，另有状态 LED 和四针 UART 下载接口。"
    )

    slots = {item.slot for item in design_decisions(requirement, limit=12)}

    assert "clock_source" not in slots
    assert "debug_port" not in slots
    assert "led_resistor" in slots


def test_unspecified_esp32_module_programming_choice_never_offers_cortex_swd() -> None:
    decisions = design_decisions("Design an ESP32-C3-WROOM-02U MCU board.")
    debug = next(item for item in decisions if item.slot == "debug_port")
    labels = " ".join(option.label for option in debug.options).lower()

    assert "clock_source" not in {item.slot for item in decisions}
    assert "uart" in debug.options[0].label.lower()
    assert "cortex" not in labels
    assert "swd" not in labels


def test_atmega_programming_choice_uses_isp_instead_of_swd() -> None:
    decisions = design_decisions("Design an ATmega328P MCU controller board.")
    debug = next(item for item in decisions if item.slot == "debug_port")
    labels = " ".join(option.label for option in debug.options).lower()

    assert "isp" in debug.options[0].label.lower()
    assert "swd" not in labels


def test_unrelated_i2c_pullup_does_not_settle_button_bias() -> None:
    requirement = (
        "ESP32-C3 board with 4.7 kOhm I2C pull-ups and a separate user button."
    )

    assert "button_bias" in {
        item.slot for item in design_decisions(requirement, limit=12)
    }


def test_resume_drops_stale_esp32_clock_and_cortex_swd_decisions() -> None:
    original = (
        "设计 ESP32-C3-WROOM-02U 模块板，提供四针 UART 下载接口。"
    )
    requirement = (
        f"{original}\n"
        "DECISION: clock_source=A — Use the internal oscillator.\n"
        "DECISION: debug_port=A — Use a 10-pin Cortex SWD connector."
    )
    carried = [
        {
            "slot": "clock_source",
            "key": "A",
            "value": "Use the internal oscillator.",
        },
        {
            "slot": "debug_port",
            "key": "A",
            "value": "Use a 10-pin Cortex SWD connector.",
        },
    ]

    reconciled_requirement, reconciled = reconcile_resolutions(requirement, carried)

    assert reconciled == []
    assert reconciled_requirement == original
    assert "Cortex SWD" not in reconciled_requirement
