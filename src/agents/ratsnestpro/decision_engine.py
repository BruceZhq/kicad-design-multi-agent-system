"""Structured engineering decisions for the AG-UI human-input gate.

This is the small, transport-neutral core of the legacy Decision Engine.  It
turns a bounded set of missing requirement values into validated choices.  The
frontend renders the choices, but only this module decides whether an answer is
valid and how it changes the downstream requirement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DECISION_REQUEST_SCHEMA = "ratsnest.decision-request.v1"
DECISION_ANSWER_SCHEMA = "ratsnest.decision-answer.v1"
DECISION_PREFIX = "DECISION:"


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[A-Z][A-Z0-9_.+-]{0,15}$")
    label: str = Field(min_length=1, max_length=500)
    value: str = Field(default="", max_length=2_000)
    basis: str = Field(default="", max_length=500)
    free_text: bool = False


class OpenDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    question: str = Field(min_length=1, max_length=1_000)
    kind: str = Field(default="assumption", max_length=40)
    options: list[DecisionOption] = Field(min_length=2, max_length=6)
    recommended_key: str = Field(default="", max_length=16)
    citation: str = Field(default="", max_length=500)

    def option(self, key: str) -> DecisionOption | None:
        wanted = key.strip().upper()
        return next((item for item in self.options if item.key == wanted), None)


class DecisionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_.+-]{0,15}$")
    text: str = Field(default="", max_length=2_000)


class DecisionAnswerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: str
    answers: list[DecisionAnswer] = Field(min_length=1, max_length=12)


@dataclass(frozen=True)
class RequirementGap:
    slot: str
    question_en: str
    question_zh: str
    assumed: str
    alternatives: tuple[str, ...]
    basis_en: str
    basis_zh: str
    applies_when: tuple[str, ...] = ()
    stated_when: tuple[str, ...] = ()
    stated_pattern: str = ""

    def relevant(self, lowered: str) -> bool:
        return not self.applies_when or any(token in lowered for token in self.applies_when)

    def stated(self, lowered: str) -> bool:
        return any(token in lowered for token in self.stated_when) or bool(
            self.stated_pattern and re.search(self.stated_pattern, lowered)
        )


_DEFAULT = ("engineering default", "工程默认值")
_CONVENTION = ("board-level convention", "板级惯例")
_ESPRESSIF_MODULE_RE = re.compile(
    r"\besp32[\w-]*(?:wroom|wrover|mini|pico)[\w-]*\b|"
    r"\besp32[^\n。；;]{0,32}(?:module|模块|模组)",
    re.IGNORECASE,
)
_PROGRAMMING_INTERFACE_PATTERN = (
    r"(?i)(?:(?:uart|serial|usb|jtag|swd|isp|updi|icsp|串口|调试口)"
    r"[^\n。；;]{0,24}(?:download|program|flash|debug|bootloader|下载|烧写|编程|调试)|"
    r"(?:download|program|flash|debug|bootloader|下载|烧写|编程|调试)"
    r"[^\n。；;]{0,24}(?:uart|serial|usb|jtag|swd|isp|updi|icsp|串口|接口|连接器))"
)

# Ordered by layout/BOM consequence.  A request can raise at most six items.
_GAPS: tuple[RequirementGap, ...] = (
    RequirementGap(
        "board_outline",
        "Board outline is not specified. What size should it be?",
        "板子外形尚未确定。应采用什么尺寸？",
        "50 x 40 mm rectangular outline with 1 mm corner radii",
        (
            "80 x 50 mm rectangular outline",
            "Wait for an enclosure DXF or mechanical drawing",
        ),
        *_DEFAULT,
        stated_when=("board size", "outline", "尺寸", "外形", "板框", "长宽"),
        stated_pattern=(
            r"\d+(?:\.\d+)?\s*(?:mm)?\s*(?:x|×|\*|by)\s*"
            r"\d+(?:\.\d+)?\s*mm"
        ),
    ),
    RequirementGap(
        "layer_count",
        "Copper layer count is not specified. Which stack should be used?",
        "PCB 层数尚未确定。应采用几层板？",
        "2 copper layers",
        ("4 copper layers", "6 copper layers"),
        *_CONVENTION,
        stated_when=(
            "two-layer",
            "2-layer",
            "four-layer",
            "4-layer",
            "6-layer",
            "两层",
            "双层",
            "2层",
            "四层",
            "4层",
            "六层",
        ),
    ),
    RequirementGap(
        "input_power",
        "Input power source is not specified. How is the board powered?",
        "输入电源尚未确定。板子应从哪里取电？",
        "USB-C receptacle used as a 5 V sink",
        ("5.5/2.1 mm DC jack", "2-pin header fed from an external 5 V supply"),
        *_DEFAULT,
        stated_when=("usb", "battery", "barrel", "dc jack", "电池", "电源输入", "外部供电"),
        stated_pattern=r"(?:input|输入|供电)[^\n]{0,30}\b\d+(?:\.\d+)?\s*v\b|\b\d+(?:\.\d+)?\s*v[^\n]{0,30}(?:input|输入|供电)",
    ),
    RequirementGap(
        "main_rail",
        "Logic supply rail is not specified. Which rail should be used?",
        "逻辑主电源轨尚未确定。应采用哪种电压？",
        "3.3 V main rail from a linear regulator",
        ("5 V rail with no regulator", "1.8 V main rail from a linear regulator"),
        *_DEFAULT,
        stated_when=("ldo", "regulator", "稳压", "电压轨", "ap2112"),
        stated_pattern=r"\b(?:1\.8|3\.3|5)\s*v\b",
    ),
    RequirementGap(
        "clock_source",
        "MCU clock source is not specified. Which source should be used?",
        "MCU 时钟来源尚未确定。应使用哪种时钟？",
        "Internal oscillator only, with no external crystal",
        (
            "External high-speed crystal selected from the MCU datasheet",
            "External high-speed crystal plus a 32.768 kHz crystal",
        ),
        *_CONVENTION,
        applies_when=("mcu", "stm32", "esp32", "atmega", "微控制器", "主控"),
        stated_when=("crystal", "xtal", "oscillator", "晶振", "晶体", "内部时钟", "时钟源"),
        stated_pattern=r"\b\d+(?:\.\d+)?\s*(?:mhz|khz)\b",
    ),
    RequirementGap(
        "debug_port",
        "Programming and debug connection is not specified. Which interface should be used?",
        "烧写与调试接口尚未确定。应采用哪种接口？",
        "10-pin Cortex SWD connector",
        ("4-pin SWD header", "SWD test pads without a connector"),
        *_CONVENTION,
        applies_when=("mcu", "stm32", "esp32", "atmega", "微控制器", "主控"),
        stated_when=("swd", "jtag", "debug", "bootloader", "调试", "烧写", "下载口"),
        stated_pattern=_PROGRAMMING_INTERFACE_PATTERN,
    ),
    RequirementGap(
        "led_resistor",
        "Indicator LED current-limit resistor is not specified. Which value should be used?",
        "用户 LED 的限流电阻值尚未确定。应采用多大阻值？",
        "1 kOhm series resistor per indicator LED",
        ("330 Ohm series resistor", "2.2 kOhm series resistor"),
        *_DEFAULT,
        applies_when=("led", "指示灯", "状态灯"),
        stated_pattern=(
            r"(?i)(?:(?:led|指示灯|状态灯|限流)[^\n。；;]{0,30}"
            r"\b\d+(?:\.\d+)?\s*(?:k\s*)?(?:ohm|ω|欧姆|欧)\b|"
            r"\b\d+(?:\.\d+)?\s*(?:k\s*)?(?:ohm|ω|欧姆|欧)\b"
            r"[^\n。；;]{0,20}(?:current[- ]?limit|series|限流)"
            r"[^\n。；;]{0,24}(?:led|指示灯|状态灯)|"
            r"\b\d+(?:\.\d+)?\s*(?:k\s*)?(?:ohm|ω|欧姆|欧)\b"
            r"[^\n。；;]{0,12}(?:led)[^\n。；;]{0,12}(?:resistor))"
        ),
    ),
    RequirementGap(
        "button_bias",
        "User button bias is not specified. How should the inactive level be held?",
        "用户按键的偏置方式尚未确定。未按下时应如何保持稳定电平？",
        "External 10 kOhm pull-up resistor",
        ("MCU internal pull-up", "External 4.7 kOhm pull-up resistor"),
        *_DEFAULT,
        applies_when=("button", "pushbutton", "switch", "按键", "按钮"),
        stated_when=(),
        stated_pattern=(
            r"(?i)(?:(?:button|pushbutton|switch|按键|按钮|启动键|复位键)"
            r"[^\n。；;]{0,48}(?:internal|external|内部|外部)"
            r"[^\n。；;]{0,20}(?:pull[- ]?(?:up|down)|上拉|下拉)|"
            r"(?:internal|external|内部|外部)[^\n。；;]{0,20}"
            r"(?:pull[- ]?(?:up|down)|上拉|下拉)[^\n。；;]{0,48}"
            r"(?:button|pushbutton|switch|按键|按钮|启动键|复位键)|"
            r"(?:internal|external|内部|外部)[^\n。；;]{0,20}"
            r"(?:button|pushbutton|switch|按键|按钮|启动键|复位键)"
            r"[^\n。；;]{0,20}(?:pull[- ]?(?:up|down)|上拉|下拉))"
        ),
    ),
    RequirementGap(
        "mounting",
        "Mechanical mounting is not specified. How should the board be fixed?",
        "机械固定方式尚未确定。板子应如何安装？",
        "Four non-plated M2 mounting holes",
        ("Four non-plated M3 mounting holes", "No mounting holes"),
        *_CONVENTION,
        stated_when=("mounting hole", "standoff", "安装孔", "固定孔", "螺丝"),
        stated_pattern=r"\bm[23]\b",
    ),
)


def _controller_family(lowered: str) -> str:
    if "esp32" in lowered:
        return "esp32"
    if "atmega" in lowered or re.search(r"\bavr\b", lowered):
        return "avr"
    if re.search(r"\bpic(?:1[0268]|2[4-9]|3[012])?[a-z0-9-]*\b", lowered):
        return "pic"
    if re.search(r"\b(?:stm32|rp2040|nrf\d+|samd\d+)", lowered):
        return "arm_swd"
    return "unknown"


def _specialize_gap(gap: RequirementGap, lowered: str) -> RequirementGap | None:
    """Use controller-family semantics instead of applying Cortex defaults globally."""

    family = _controller_family(lowered)
    if gap.slot == "clock_source" and family == "esp32":
        if _ESPRESSIF_MODULE_RE.search(lowered):
            return None
        return replace(
            gap,
            assumed="Datasheet-matched external high-frequency crystal and load network",
            alternatives=(
                "Datasheet-matched high-frequency plus 32.768 kHz RTC crystals",
                "External clock input meeting the selected ESP32 datasheet",
            ),
        )
    if gap.slot != "debug_port":
        return gap
    if family == "esp32":
        return replace(
            gap,
            assumed="UART download header with EN and BOOT controls",
            alternatives=(
                "Native USB Serial/JTAG connector when supported by the selected ESP32",
                "JTAG test header matched to the selected Espressif pinout",
            ),
            stated_when=("bootloader", "下载口", "下载接口", "烧写接口", "编程接口"),
            stated_pattern=_PROGRAMMING_INTERFACE_PATTERN,
        )
    if family == "avr":
        return replace(
            gap,
            assumed="6-pin AVR ISP header",
            alternatives=("10-pin AVR ISP header", "AVR ISP test pads"),
            stated_when=("isp", "bootloader", "下载口", "下载接口", "烧写接口", "编程接口"),
            stated_pattern=_PROGRAMMING_INTERFACE_PATTERN,
        )
    if family == "pic":
        return replace(
            gap,
            assumed="6-pin PIC ICSP header",
            alternatives=("Tag-Connect ICSP footprint", "PIC ICSP test pads"),
            stated_when=("icsp", "bootloader", "下载口", "下载接口", "烧写接口", "编程接口"),
            stated_pattern=_PROGRAMMING_INTERFACE_PATTERN,
        )
    if family == "arm_swd":
        return gap
    return None


def _language(text: str) -> str:
    return "zh" if re.search(r"[\u3400-\u9fff]", text or "") else "en"


def design_decisions(
    requirement: str,
    *,
    settled: frozenset[str] = frozenset(),
    limit: int = 6,
) -> list[OpenDecision]:
    """Return deterministic choices for relevant values not stated by the user."""

    lowered = (requirement or "").lower()
    if not lowered.strip():
        return []
    language = _language(requirement)
    decisions: list[OpenDecision] = []
    for base_gap in _GAPS:
        if len(decisions) >= limit:
            break
        gap = _specialize_gap(base_gap, lowered)
        if gap is None:
            continue
        if gap.slot in settled or not gap.relevant(lowered) or gap.stated(lowered):
            continue
        basis = gap.basis_zh if language == "zh" else gap.basis_en
        options = [
            DecisionOption(
                key="A",
                label=(f"采用 {gap.assumed}" if language == "zh" else f"Use {gap.assumed}"),
                value=(
                    f"For {gap.slot}, the user confirmed: {gap.assumed}. "
                    "Use exactly this and do not substitute it."
                ),
                basis=basis,
            )
        ]
        for offset, alternative in enumerate(gap.alternatives[:4], start=1):
            options.append(
                DecisionOption(
                    key=chr(ord("A") + offset),
                    label=(f"改用 {alternative}" if language == "zh" else f"Use {alternative}"),
                    value=(
                        f"For {gap.slot}, the user confirmed: {alternative}. "
                        "Use exactly this and do not substitute it."
                    ),
                    basis=basis,
                )
            )
        options.append(
            DecisionOption(
                key=chr(ord("A") + len(gap.alternatives[:4]) + 1),
                label=("自定义" if language == "zh" else "Use a custom value"),
                basis="user supplied",
                free_text=True,
            )
        )
        decisions.append(
            OpenDecision(
                slot=gap.slot,
                question=gap.question_zh if language == "zh" else gap.question_en,
                options=options,
                recommended_key="A",
                citation=basis,
            )
        )
    return decisions


def _slot_accepts_resolution(requirement: str, slot: str) -> bool:
    base_gap = next((item for item in _GAPS if item.slot == slot), None)
    if base_gap is None:
        return True
    lowered = (requirement or "").lower()
    gap = _specialize_gap(base_gap, lowered)
    if gap is None:
        return False
    return gap.relevant(lowered) and not gap.stated(lowered)


def applicable_decisions(
    requirement: str,
    decisions: list[OpenDecision],
) -> list[OpenDecision]:
    """Drop stale questions whose value is already fixed by user input.

    Checkpointed HITL questions can outlive a parser deployment.  Revalidating
    them at resume time makes the original requirement authoritative and keeps
    human input as a fill-only patch, never an implicit requirement rewrite.
    """

    return [
        decision
        for decision in decisions
        if _slot_accepts_resolution(requirement, decision.slot)
    ]


def intent_decisions(intent: dict[str, Any], requirement: str) -> list[OpenDecision]:
    if not intent.get("needs_clarification"):
        return []
    language = _language(requirement)
    question = str(intent.get("clarification_question") or "").strip() or (
        "这次需要新建设计、审查已有工程，还是只做器件验证？"
        if language == "zh"
        else "Is this a new design, an existing-project review, or a parts-only task?"
    )
    labels = (
        ("新建设计，从零生成 KiCad 工程", "审查已有 KiCad 工程", "只做器件与资料验证")
        if language == "zh"
        else ("Build a new KiCad design", "Review an existing KiCad project", "Validate parts only")
    )
    return [
        OpenDecision(
            slot="task_kind",
            kind="intent",
            question=question,
            options=[
                DecisionOption(
                    key="A",
                    label=labels[0],
                    value="This is a NEW KiCad PCB build task.",
                ),
                DecisionOption(
                    key="B",
                    label=labels[1],
                    value="Review the existing KiCad project supplied by the user.",
                    free_text=True,
                ),
                DecisionOption(
                    key="C",
                    label=labels[2],
                    value="Only validate parts and evidence; do not generate a KiCad project.",
                ),
            ],
            recommended_key="A",
        )
    ]


def public_questions(decisions: list[OpenDecision]) -> list[dict[str, Any]]:
    return [
        {
            "slot": decision.slot,
            "question": decision.question,
            "kind": decision.kind,
            "recommendedKey": decision.recommended_key,
            "citation": decision.citation,
            "options": [
                {
                    "key": option.key,
                    "label": option.label,
                    "basis": option.basis,
                    "freeText": option.free_text,
                }
                for option in decision.options
            ],
        }
        for decision in decisions
    ]


def parse_resolutions(answer: str, decisions: list[OpenDecision]) -> list[dict[str, str]]:
    """Validate a structured AG-UI answer against the exact offered decisions."""

    try:
        raw = json.loads(answer)
        envelope = DecisionAnswerEnvelope.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("Decision answer is not valid structured JSON.") from exc
    if envelope.schemaVersion != DECISION_ANSWER_SCHEMA:
        raise ValueError("Decision answer schema version is unsupported.")

    offered = {decision.slot: decision for decision in decisions}
    received: dict[str, DecisionAnswer] = {}
    for item in envelope.answers:
        if item.slot in received:
            raise ValueError(f"Decision slot {item.slot!r} was answered more than once.")
        if item.slot not in offered:
            raise ValueError(f"Decision slot {item.slot!r} was not offered.")
        received[item.slot] = item
    missing = [decision.slot for decision in decisions if decision.slot not in received]
    if missing:
        raise ValueError(f"Decision answer is incomplete: {', '.join(missing)}")

    resolved: list[dict[str, str]] = []
    for decision in decisions:
        item = received[decision.slot]
        option = decision.option(item.key)
        if option is None:
            raise ValueError(f"Option {item.key!r} was not offered for {decision.slot!r}.")
        text = item.text.strip()
        if option.free_text and not text:
            raise ValueError(f"Decision slot {decision.slot!r} requires a custom value.")
        value = (
            f"For {decision.slot}, the user supplied: {text}"
            if option.free_text
            else option.value
        )
        resolved.append(
            {
                "slot": decision.slot,
                "kind": decision.kind,
                "key": option.key,
                "label": option.label,
                "value": value,
                "citation": option.basis or decision.citation,
            }
        )
    return resolved


def merge_resolutions(
    carried: list[dict[str, Any]],
    fresh: list[dict[str, str]],
) -> list[dict[str, Any]]:
    merged = {
        str(item["slot"]): dict(item)
        for item in [*carried, *fresh]
        if isinstance(item, dict) and item.get("slot")
    }
    return list(merged.values())


def reconcile_resolutions(
    requirement: str,
    carried: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Drop checkpointed answers invalidated by the authoritative requirement."""

    base_requirement = "\n".join(
        line
        for line in (requirement or "").splitlines()
        if not line.lstrip().startswith(DECISION_PREFIX)
    ).rstrip()
    valid = [
        dict(item)
        for item in carried
        if isinstance(item, dict)
        and item.get("slot")
        and _slot_accepts_resolution(base_requirement, str(item["slot"]))
    ]
    return apply_resolutions(base_requirement, valid), valid


def apply_resolutions(requirement: str, resolved: list[dict[str, str]]) -> str:
    stale_slots = {
        str(item.get("slot", ""))
        for item in resolved
        if item.get("slot")
        and not _slot_accepts_resolution(requirement, str(item["slot"]))
    }
    if stale_slots:
        raise ValueError(
            "HITL answers cannot override values already fixed by the original "
            f"requirement: {', '.join(sorted(stale_slots))}"
        )
    lines = [
        f"{DECISION_PREFIX} {item['slot']}={item['key']} — {item['value']}"
        for item in resolved
        if item.get("value")
    ]
    return requirement if not lines else f"{requirement.rstrip()}\n" + "\n".join(lines)


def from_state(payload: Any) -> list[OpenDecision]:
    if not isinstance(payload, list):
        return []
    decisions: list[OpenDecision] = []
    for item in payload:
        try:
            decisions.append(OpenDecision.model_validate(item))
        except (ValidationError, TypeError):
            continue
    return decisions


def to_state(decisions: list[OpenDecision]) -> list[dict[str, Any]]:
    return [decision.model_dump(mode="json") for decision in decisions]
