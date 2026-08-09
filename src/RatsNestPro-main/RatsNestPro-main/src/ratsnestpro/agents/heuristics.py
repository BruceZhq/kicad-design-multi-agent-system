"""Deterministic heuristics used as the offline path and the auto-mode fallback.

These contain no LLM calls. They provide a keyword-based requirement→params
extraction and a conservative family judgment so the agent can run fully
offline and so auto-mode has something safe to fall back to.
"""

from __future__ import annotations

import re

from ratsnestpro.domain.contracts import FamilyDecision
from ratsnestpro.families import FAMILY_ID

# Mandatory features that define the ATmega328 dev-board family.
MANDATORY_FEATURES = [
    "usb-c power",
    "ldo regulator",
    "atmega328 mcu",
    "crystal",
    "decoupling",
    "reset",
    "breakout header",
]

_FAMILY_KEYWORDS = ("atmega328", "atmega 328", "atmega", "328p", "328")


def params_from_requirement(text: str) -> dict[str, object]:
    """Extract in-family parameters from free text. Only sets a parameter when
    the text clearly implies it; otherwise Atmega328Params defaults apply."""
    t = text.lower()
    out: dict[str, object] = {}
    m = re.search(r"(\d+)\s*mhz", t)
    if m and int(m.group(1)) in (8, 16):
        out["crystal_mhz"] = int(m.group(1))
    if "3.3v" in t or "3v3" in t:
        out["ldo_output_v"] = 3.3
    elif "5v" in t or "5.0v" in t:
        out["ldo_output_v"] = 5.0
    if "no led" in t or "without led" in t or "no power led" in t:
        out["power_led"] = False
    elif "led" in t:
        out["power_led"] = True
    m = re.search(r"(\d+)\s*decoupl", t)
    if m:
        out["decoupling_count"] = int(m.group(1))
    if "no mounting" in t or "no holes" in t:
        out["mounting_holes"] = 0
    return out


def judge_family(text: str) -> FamilyDecision:
    """Conservative deterministic family judgment: qualified if the text names
    the ATmega328 family."""
    t = text.lower()
    qualified = any(k in t for k in _FAMILY_KEYWORDS)
    if qualified:
        return FamilyDecision(
            qualified=True,
            family=FAMILY_ID,
            mandatory_features_present=True,
            rationale="deterministic keyword match on the ATmega328 family",
        )
    return FamilyDecision(
        qualified=False,
        family="",
        mandatory_features_present=False,
        clarifying_questions=[
            "Which MCU family is this board based on? "
            "Only the ATmega328 development-board family is currently supported."
        ],
        rationale="no ATmega328 family keyword found",
    )
