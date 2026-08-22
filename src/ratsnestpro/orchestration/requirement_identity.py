"""Deterministic component-identity constraints from a raw requirement.

The LLM is allowed to propose parts, but it is not allowed to decide which of
its own proposals came from the user.  This module extracts only identities
whose literal source span can be verified in the original request and assigns
constraint strength from nearby user language.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ratsnestpro.domain.contracts import ComponentIdentityConstraint
from ratsnestpro.eda import grounding
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart

_FIXED_MARKER = re.compile(
    r"(?:"
    r"\bmust\s+(?:be|use)\b|"
    r"\bshall\s+(?:be|use)\b|"
    r"\brequired\s+(?:device|part|component|mcu|processor)?\s*(?:is|:|=)\s*|"
    r"\buse\s+exact(?:ly)?\b|"
    r"\bdo\s+not\s+substitute\b|"
    r"必须(?:是|为|使用|采用|选用)|"
    r"只能(?:是|为|使用|采用|选用)|"
    r"固定(?:为|使用|采用)|"
    r"指定(?:为|使用|采用)"
    r")",
    re.IGNORECASE,
)
_CAPABILITY_MARKER = re.compile(
    r"(?:"
    r"\b(?:use|using|select|choose|prefer)\b|"
    r"(?:使用|采用|选用|优先使用|优先采用)"
    r")",
    re.IGNORECASE,
)
_EQUIVALENT_MARKER = re.compile(
    r"(?:"
    r"\bor\s+(?:a\s+)?(?:functional(?:ly)?\s+)?equivalent\b|"
    r"\bor\s+better\b|"
    r"\bcompatible\s+equivalent\b|"
    r"(?:或|以及)(?:功能)?等效(?:器件|型号)?|"
    r"可(?:用|由).{0,16}(?:等效|替代)"
    r")",
    re.IGNORECASE,
)
_FAMILY_MARKER = re.compile(
    r"\b(?:family|series|variant)\b|(?:系列|家族|同系列|任一型号)",
    re.IGNORECASE,
)
_NEGATIVE_ALTERNATIVE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|must\s+not|shall\s+not|not)\s+"
    r"(?:substitute|replace|use)\b|"
    r"\binstead\s+of\b|"
    r"(?:禁止|不得|不可|不能)(?:以|将|把|换成|替换|改用|使用|采用)|"
    r"(?:而非|不要改成|不得改成)"
    r")",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(r"(?:[\r\n。；;!?！？]+|\.(?=\s|$))+")
_DEVICE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9._/-]*\d)"
    r"[A-Za-z][A-Za-z0-9]*(?:[._/-][A-Za-z0-9]+)*"
    r"(?![A-Za-z0-9])"
)
_PACKAGE_TOKEN = re.compile(
    r"^(?:"
    r"(?:u?qfn|lqfp|tqfp|qfp|bga|wlcsp|dfn|soic|sop|ssop|tssop|msop|"
    r"sot|to|dip|sip|plcc|csp|lga|0402|0603|0805|1206)"
    r"[-_]?\d*[a-z0-9-]*"
    r")$",
    re.IGNORECASE,
)
_NON_DEVICE_TOKEN = re.compile(
    r"^(?:"
    r"usb\d*|i2c\d*|spi\d*|uart\d*|can\d*|rs485|sdio\d*|"
    r"cortexm?\d*|swd\d*|jtag\d*|kicad\d*|freerouting\d*|"
    r"erc\d*|drc\d*|pcb\d*|bom\d*|cpl\d*|dsn\d*|ses\d*|"
    r"vbus\d*|vdd\d*|vdda\d*|vbat\d*|vcap\d*|boot\d*|reset\d*|"
    r"gnd\d*|gpio\d*|adc\d*|pwm\d*|led\d*|"
    r"\d+(?:mhz|khz|hz|mbit|kbit|gb|mb|kb|mm|mil|ohm)"
    r")$",
    re.IGNORECASE,
)


def _identity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _device_tokens(text: str) -> list[str]:
    """Return code-like device identities, excluding packages/protocols."""

    tokens: list[str] = []
    for match in _DEVICE_TOKEN.finditer(text):
        token = match.group(0).strip(".,:()[]{}'\"")
        compact = _identity_key(token)
        if (
            len(compact) < 4
            or not any(char.isalpha() for char in compact)
            or not any(char.isdigit() for char in compact)
            or _PACKAGE_TOKEN.fullmatch(token)
            or _NON_DEVICE_TOKEN.fullmatch(token)
        ):
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


def _candidate_segment(clause: str, marker_end: int) -> str:
    """Bound a marker's positive object before a list of forbidden alternatives."""

    tail = clause[marker_end : marker_end + 320]
    negative = _NEGATIVE_ALTERNATIVE.search(tail)
    if negative:
        tail = tail[: negative.start()]
    return tail


def _stronger(
    left: ComponentIdentityConstraint,
    right: ComponentIdentityConstraint,
) -> ComponentIdentityConstraint:
    rank = {"capability_only": 0, "family_variant": 1, "fixed_exact": 2}
    return right if rank[right.mode] > rank[left.mode] else left


def extract_component_identity_constraints(
    requirement: str,
) -> list[ComponentIdentityConstraint]:
    """Extract source-verifiable identity constraints without trusting an LLM.

    A bare part-number mention is not fixed.  Exact identity is granted only by
    an explicit user marker such as ``must use``/``必须使用`` and is downgraded
    when the same positive span permits an equivalent or a family variant.
    """

    by_identity: dict[str, ComponentIdentityConstraint] = {}
    for raw_clause in _CLAUSE_SPLIT.split(requirement):
        clause = raw_clause.strip()
        if not clause:
            continue
        markers = list(_FIXED_MARKER.finditer(clause))
        if not markers:
            markers = list(_CAPABILITY_MARKER.finditer(clause))
        for marker in markers:
            segment = _candidate_segment(clause, marker.end())
            identities = _device_tokens(segment)
            if not identities:
                continue
            permits_equivalent = bool(_EQUIVALENT_MARKER.search(segment))
            family = bool(_FAMILY_MARKER.search(segment))
            explicit_fixed = bool(_FIXED_MARKER.fullmatch(marker.group(0)))
            mode = (
                "capability_only"
                if permits_equivalent or not explicit_fixed
                else "family_variant"
                if family
                else "fixed_exact"
            )
            excerpt = clause[:500]
            for identity in identities:
                constraint = ComponentIdentityConstraint(
                    requested_identity=identity,
                    mode=mode,
                    allow_equivalent=permits_equivalent,
                    source_excerpt=excerpt,
                )
                key = _identity_key(identity)
                previous = by_identity.get(key)
                by_identity[key] = (
                    constraint
                    if previous is None
                    else _stronger(previous, constraint)
                )
    return sorted(
        by_identity.values(),
        key=lambda item: (_identity_key(item.requested_identity), item.mode),
    )


def _part_identity_values(part: SelectedPart) -> Iterable[str]:
    yield part.value
    if part.requested_identity:
        yield part.requested_identity
    _, _, symbol_name = part.symbol.partition(":")
    if symbol_name:
        yield symbol_name
    try:
        properties = grounding.symbol_properties(part.symbol)
    except Exception:  # noqa: BLE001 - installed-library lookup is best effort
        return
    for key in ("Value", "MPN", "ki_keywords"):
        value = str(properties.get(key, "")).strip()
        if value:
            yield value


def identity_constraint_for_part(
    part: SelectedPart,
    constraints: Sequence[ComponentIdentityConstraint],
) -> ComponentIdentityConstraint | None:
    """Return the strongest source constraint matching a selected real part."""

    matches: list[ComponentIdentityConstraint] = []
    for constraint in constraints:
        for actual in _part_identity_values(part):
            relation = grounding.symbol_identity_match_kind(
                constraint.requested_identity,
                actual,
            )
            if relation in {"exact", "kicad_wildcard", "qualified_base"}:
                matches.append(constraint)
                break
    if not matches:
        return None
    rank = {"capability_only": 0, "family_variant": 1, "fixed_exact": 2}
    return max(
        matches,
        key=lambda item: (
            rank[item.mode],
            len(_identity_key(item.requested_identity)),
        ),
    )


def missing_fixed_identities(
    parts: Sequence[SelectedPart],
    constraints: Sequence[ComponentIdentityConstraint],
) -> list[str]:
    """Return user-fixed identities absent from the selected physical BOM."""

    return [
        constraint.requested_identity
        for constraint in constraints
        if constraint.mode == "fixed_exact"
        and not any(
            identity_constraint_for_part(part, [constraint]) is not None
            for part in parts
        )
    ]


__all__ = [
    "extract_component_identity_constraints",
    "identity_constraint_for_part",
    "missing_fixed_identities",
]
