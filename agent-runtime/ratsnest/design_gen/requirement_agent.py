"""Requirement Understanding Agent: natural language -> DesignSpec."""

from __future__ import annotations

import re

from ratsnest.circuit_math import detect_unsupported_features
from ratsnest.protocols import LlmBrain
from ratsnest.schemas import DesignSpec

_SPEC_PROMPT = """You convert an electronics requirement into a DesignSpec
JSON for a deliberately constrained power-board product. Exactly two circuit
families are supported: an adjustable LDO and an asynchronous Buck converter.

Return ONLY a JSON object with exactly these keys:
  project_name       short snake_case slug derived from the requirement
  input_voltage      number (volts, must be greater than output_voltage)
  output_voltage     number (volts)
  output_current_a   number (amps, default 0.5 if unstated)
  led                one of "red","green","blue" or null
  topology           one of "auto","ldo","buck"; preserve explicit requests
  ambient_temperature_c number (default 25)
  max_output_ripple_mv number (default 100)
  unsupported_features array of concise unsupported feature names

Both supported families step down, so if roles are ambiguous the larger
voltage is the input. Requirements may be in any language. Buck is supported.
Boost, buck-boost, isolation, chargers, MCU/FPGA, USB/Ethernet/CAN, motor
control, and all other functions must be listed in unsupported_features;
never silently map them to a supported family."""

_LED_COLORS = ("red", "green", "blue")


def parse_requirement_llm(text: str, llm: LlmBrain) -> DesignSpec | None:
    """Return a validated proposal, or None so the caller can fall back."""
    if llm is None:
        return None
    raw = llm.complete_json(
        "requirement_agent", _SPEC_PROMPT,
        f"Requirement: {text}", max_tokens=700)
    if not raw:
        return None
    try:
        raw.setdefault("requirement_text", text)
        raw.setdefault("topology", "auto")
        raw.setdefault("ambient_temperature_c", 25.0)
        raw.setdefault("max_output_ripple_mv", 100.0)
        raw.setdefault("unsupported_features", [])
        topology = str(raw.get("topology", "auto")).lower()
        raw["topology"] = {
            "adjustable_ldo": "ldo",
            "linear": "ldo",
            "asynchronous_buck": "buck",
            "switching": "buck",
        }.get(topology, topology)
        if raw.get("project_name"):
            raw["project_name"] = re.sub(
                r"[^a-z0-9]+", "_",
                str(raw["project_name"]).lower()).strip("_")[:40]
        spec = DesignSpec.model_validate(raw)
    except Exception:
        return None
    if not (0 < spec.output_voltage < spec.input_voltage <= 60):
        return None
    if spec.led is not None and spec.led.lower() not in _LED_COLORS:
        return None
    for feature in detect_unsupported_features(text):
        if feature not in spec.unsupported_features:
            spec.unsupported_features.append(feature)
    if not spec.project_name:
        spec.project_name = "generated_board"
    return spec


# Role markers are position-aware: prepositions come before the number while
# labels come after it ("from 12V", "5V output").
_IN_BEFORE = re.compile(r"\b(from|input|vin|supply)\s*$")
_OUT_BEFORE = re.compile(r"\b(to|into|output|vout)\s*$")
_IN_AFTER = re.compile(r"^\s*(input|in\b|supply)")
_OUT_AFTER = re.compile(r"^\s*(output|out\b|rail)")


def _classify_voltages(text: str) -> tuple[list[float], list[float], list[float]]:
    lower = text.lower()
    inputs: list[float] = []
    outputs: list[float] = []
    unknown: list[float] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*v(?:olts?)?\b", lower):
        voltage = float(match.group(1))
        before = lower[max(0, match.start() - 16):match.start()]
        after = lower[match.end():match.end() + 12]
        if _IN_BEFORE.search(before) or _IN_AFTER.search(after):
            inputs.append(voltage)
        elif _OUT_BEFORE.search(before) or _OUT_AFTER.search(after):
            outputs.append(voltage)
        else:
            unknown.append(voltage)
    return inputs, outputs, unknown


def parse_requirement(text: str) -> DesignSpec:
    spec = DesignSpec(requirement_text=text)
    lower = text.lower()

    inputs, outputs, unknown = _classify_voltages(text)
    if inputs:
        spec.input_voltage = inputs[0]
    if outputs:
        spec.output_voltage = outputs[0]
    if unknown:
        if not inputs and not outputs and len(unknown) >= 2:
            spec.input_voltage, spec.output_voltage = max(unknown), min(unknown)
        elif not outputs and len(unknown) == 1:
            spec.output_voltage = unknown[0]
        elif not inputs and len(unknown) == 1:
            spec.input_voltage = unknown[0]

    current = re.search(r"(\d+(?:\.\d+)?)\s*(m?)a\b", lower)
    if current:
        value = float(current.group(1))
        spec.output_current_a = value / 1000 if current.group(2) else value

    if re.search(r"\b(?:ldo|linear regulator)\b|线性稳压", lower):
        spec.topology = "ldo"
    elif re.search(
            r"\b(?:buck|switching regulator)\b|开关降压|降压转换器", lower):
        spec.topology = "buck"

    ambient = re.search(
        r"(?:ambient|环境温度)\s*(?:of|=|:)?\s*(-?\d+(?:\.\d+)?)\s*°?c",
        lower)
    if ambient:
        spec.ambient_temperature_c = float(ambient.group(1))
    ripple = re.search(r"(\d+(?:\.\d+)?)\s*mv\s*(?:ripple|纹波)", lower)
    if ripple:
        spec.max_output_ripple_mv = float(ripple.group(1))

    if re.search(
            r"\bno\s+led\b|\bwithout\s+(an?\s+)?led\b|不要.*(?:led|指示灯)",
            lower):
        spec.led = None
    else:
        color_words = {
            "red": ("red", "红色", "红灯"),
            "green": ("green", "绿色", "绿灯"),
            "blue": ("blue", "蓝色", "蓝灯"),
        }
        for color, words in color_words.items():
            if any(word in lower for word in words):
                spec.led = color
                break

    spec.unsupported_features = detect_unsupported_features(text)
    slug = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")[:40]
    if slug:
        spec.project_name = slug
    return spec
