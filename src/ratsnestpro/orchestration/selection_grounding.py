"""Identity-safe installed KiCad candidates for component selection.

This module knows only two kinds of direct repair:

* a generic electrical primitive requested by role/category; or
* an exact/KiCad-wildcard manufacturer identity.

Lexically similar active devices are useful discovery evidence, but are never
presented as interchangeable parts.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ratsnestpro.eda import footprints, grounding, symbols

CompatibleFootprintLookup = Callable[[str, str], list[str]]

_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+_.-]{2,}\b")
_GENERIC_NAMED_TOKENS = {
    "ADC",
    "CAN",
    "DC",
    "ESD",
    "GPIO",
    "I2C",
    "LED",
    "MCU",
    "PCB",
    "SDIO",
    "SPI",
    "SWD",
    "TVS",
    "UART",
    "USB",
}


@dataclass(frozen=True)
class PrimitiveRule:
    """A declarative semantic category backed by generic KiCad primitives."""

    label: str
    required_groups: tuple[tuple[str, ...], ...]
    symbols: tuple[str, ...]
    excluded_terms: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return (
            all(any(_contains_term(text, term) for term in group)
                for group in self.required_groups)
            and not any(_contains_term(text, term) for term in self.excluded_terms)
        )


@dataclass(frozen=True)
class _InstalledSymbolLookup:
    exact_names: dict[str, tuple[str, ...]]
    wildcard_names_by_length: dict[int, tuple[tuple[str, str], ...]]
    by_library: dict[str, tuple[tuple[str, str], ...]]
    all_names: tuple[tuple[str, str], ...]


# Order is ranking: the most electrically specific generic primitive comes
# before broader categories. Every ID is filtered through the live installed
# symbol index before it reaches an LLM.
PRIMITIVE_RULES: tuple[PrimitiveRule, ...] = (
    PrimitiveRule(
        "generic TVS/ESD protection",
        (("tvs", "esd", "surge protection", "静电", "浪涌"),),
        ("Device:D_TVS",),
    ),
    PrimitiveRule(
        "generic Schottky diode",
        (("schottky", "肖特基"),),
        ("Device:D_Schottky",),
    ),
    PrimitiveRule(
        "generic Zener diode",
        (("zener", "齐纳"),),
        ("Device:D_Zener",),
    ),
    PrimitiveRule(
        "generic diode",
        (("diode", "rectifier", "二极管", "整流"),),
        ("Device:D",),
        ("tvs", "esd", "schottky", "zener", "静电", "肖特基", "齐纳"),
    ),
    PrimitiveRule(
        "generic N-channel MOSFET",
        (("nmos", "n channel mosfet", "n mosfet", "n沟道"),),
        (
            "Transistor_FET:Q_NMOS_GSD",
            "Transistor_FET:Q_NMOS_DGS",
            "Transistor_FET:Q_NMOS_GDS",
            "Transistor_FET:Q_NMOS_SDG",
            "Transistor_FET:Q_NMOS_SGD",
            "Transistor_FET:Q_NMOS_DSG",
        ),
    ),
    PrimitiveRule(
        "generic P-channel MOSFET",
        (("pmos", "p channel mosfet", "p mosfet", "p沟道"),),
        (
            "Transistor_FET:Q_PMOS_GSD",
            "Transistor_FET:Q_PMOS_DGS",
            "Transistor_FET:Q_PMOS_GDS",
            "Transistor_FET:Q_PMOS_SDG",
            "Transistor_FET:Q_PMOS_SGD",
            "Transistor_FET:Q_PMOS_DSG",
        ),
    ),
    PrimitiveRule(
        "generic NPN BJT",
        (("npn", "npn bjt", "npn transistor", "npn三极管"),),
        (
            "Transistor_BJT:Q_NPN_BEC",
            "Transistor_BJT:Q_NPN_BCE",
            "Transistor_BJT:Q_NPN_CBE",
            "Transistor_BJT:Q_NPN_CEB",
            "Transistor_BJT:Q_NPN_EBC",
            "Transistor_BJT:Q_NPN_ECB",
        ),
    ),
    PrimitiveRule(
        "generic PNP BJT",
        (("pnp", "pnp bjt", "pnp transistor", "pnp三极管"),),
        (
            "Transistor_BJT:Q_PNP_BEC",
            "Transistor_BJT:Q_PNP_BCE",
            "Transistor_BJT:Q_PNP_CBE",
            "Transistor_BJT:Q_PNP_CEB",
            "Transistor_BJT:Q_PNP_EBC",
            "Transistor_BJT:Q_PNP_ECB",
        ),
    ),
    PrimitiveRule(
        "coaxial/RF connector",
        (("u.fl", "u fl", "ufl", "coax", "coaxial", "同轴"),),
        ("Connector:Conn_Coaxial",),
    ),
    PrimitiveRule(
        "antenna",
        (("antenna", "天线"),),
        ("Device:Antenna",),
        ("connector", "socket", "接口", "连接器"),
    ),
    PrimitiveRule(
        "battery connector",
        (("battery", "电池"), ("connector", "socket", "接口", "连接器")),
        ("Connector_Generic:Conn_01x02",),
    ),
    PrimitiveRule(
        "DC motor",
        (("dc motor", "brushed motor", "直流电机"),),
        ("Motor:Motor_DC",),
    ),
    PrimitiveRule(
        "indicator LED",
        (("led", "indicator light", "指示灯", "发光二极管"),),
        ("Device:LED",),
        ("driver", "驱动"),
    ),
    PrimitiveRule(
        "microSD socket",
        (("microsd", "micro sd"),),
        ("Connector:Micro_SD_Card",),
    ),
    PrimitiveRule(
        "CAN common-mode choke",
        (("can",), ("common mode", "common-mode", "共模"),),
        ("Device:L_Coupled",),
    ),
    PrimitiveRule(
        "common-mode choke",
        (("common mode choke", "common-mode choke", "共模电感"),),
        ("Device:L_Coupled",),
    ),
    PrimitiveRule(
        "CANH/CANL/GND connector",
        (("can",), ("gnd", "ground", "接地")),
        ("Connector_Generic:Conn_01x03",),
    ),
    PrimitiveRule(
        "10-pin Cortex SWD connector",
        (("swd",), ("10 pin", "10pin", "十针")),
        ("Connector_Generic:Conn_02x05_Odd_Even",),
    ),
    PrimitiveRule(
        "rotary encoder",
        (("encoder", "编码器"),),
        ("Device:RotaryEncoder",),
    ),
    PrimitiveRule(
        "two-terminal jumper/link",
        (("jumper", "solder bridge", "solder link", "跳线", "焊桥"),),
        ("Jumper:Jumper_2_Open",),
    ),
)


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def _contains_term(text: str, term: str) -> bool:
    if any(ord(char) > 127 for char in term):
        return term.lower() in text.lower()
    normalized_text = f" {_normalized(text)} "
    normalized_term = _normalized(term)
    return bool(normalized_term) and f" {normalized_term} " in normalized_text


def _looks_like_specific_code(value: str) -> bool:
    raw = value.strip()
    if not raw or any(char.isspace() for char in raw) or "_" in raw:
        return False
    compact = "".join(char for char in raw if char.isalnum())
    letters = sum(char.isalpha() for char in compact)
    digits = sum(char.isdigit() for char in compact)
    return (
        letters > 0
        and digits > 0
        and (len(compact) >= 5 or (letters >= 2 and digits >= 2))
    )


def _specific_codes(value: str, proposed_symbol: str) -> list[str]:
    value = value.strip()
    if _looks_like_specific_code(value):
        # The selected value is the component identity contract.  A failed LLM
        # symbol may itself name a different real device and must not become a
        # second trusted identity merely because it resembles an order code.
        return [value]
    name = proposed_symbol.partition(":")[2]
    return [name.strip()] if _looks_like_specific_code(name) else []


def _identity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


@lru_cache(maxsize=4)
def _installed_symbol_lookup(
    installed_ids: tuple[str, ...],
) -> _InstalledSymbolLookup:
    exact: dict[str, list[str]] = {}
    wildcards: dict[int, list[tuple[str, str]]] = {}
    libraries: dict[str, list[tuple[str, str]]] = {}
    all_names: list[tuple[str, str]] = []
    for lib_id in installed_ids:
        library, _, name = lib_id.partition(":")
        exact.setdefault(_identity_key(name), []).append(lib_id)
        raw_name = "".join(char for char in name if char.isalnum())
        if "x" in raw_name:
            wildcards.setdefault(len(raw_name), []).append((lib_id, name))
        record = (lib_id, name)
        libraries.setdefault(library.lower(), []).append(record)
        all_names.append(record)
    return _InstalledSymbolLookup(
        exact_names={key: tuple(ids) for key, ids in exact.items()},
        wildcard_names_by_length={
            length: tuple(records)
            for length, records in wildcards.items()
        },
        by_library={
            library: tuple(records)
            for library, records in libraries.items()
        },
        all_names=tuple(all_names),
    )


def _pin_numbers(lib_id: str) -> set[str]:
    return {
        str(pin["number"])
        for pin in (symbols.symbol_pins(lib_id) or [])
        if pin.get("number")
    }


def _selected_footprint_compatible(
    lib_id: str,
    footprint: str,
) -> bool | None:
    if not footprint:
        return None
    pads = footprints.footprint_pads(footprint)
    if pads is None:
        return None
    symbol_pins = _pin_numbers(lib_id)
    pad_numbers = {
        str(pad["number"]) for pad in pads if pad.get("number")
    }
    if not symbol_pins or not pad_numbers:
        return None
    connector = lib_id.startswith(("Connector:", "Connector_Generic:"))
    return symbol_pins == pad_numbers or (
        connector and symbol_pins.issubset(pad_numbers)
    )


def _candidate_record(
    lib_id: str,
    *,
    identity_relation: str,
    direct_repair_allowed: bool,
    role: str = "",
    value: str = "",
    footprint: str = "",
    compatible_footprints: CompatibleFootprintLookup | None = None,
) -> dict[str, Any]:
    properties = symbols.symbol_properties(lib_id)
    default_footprint = properties.get("Footprint", "")
    compatible = (
        compatible_footprints(
            lib_id,
            f"{role} {value} {footprint}",
        )
        if compatible_footprints is not None
        else []
    )
    if (
        default_footprint
        and footprints.footprint_pads(default_footprint) is not None
        and default_footprint not in compatible
    ):
        compatible.insert(0, default_footprint)
    return {
        "symbol": lib_id,
        "pins": sorted(_pin_numbers(lib_id)),
        "value": properties.get("Value", ""),
        "description": properties.get("Description", ""),
        "datasheet": properties.get("Datasheet", ""),
        "default_footprint": default_footprint,
        "compatible_footprints": compatible[:12],
        "selected_footprint_compatible": _selected_footprint_compatible(
            lib_id,
            footprint,
        ),
        "identity_relation": identity_relation,
        "direct_repair_allowed": direct_repair_allowed,
    }


def _primitive_candidates(
    text: str,
    *,
    installed: set[str],
    role: str = "",
    value: str = "",
    footprint: str = "",
    compatible_footprints: CompatibleFootprintLookup | None = None,
    limit: int = 12,
) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for rule in PRIMITIVE_RULES:
        if not rule.matches(text):
            continue
        for lib_id in rule.symbols:
            if lib_id not in installed or lib_id in seen:
                continue
            seen.add(lib_id)
            found.append((
                rule.label,
                _candidate_record(
                    lib_id,
                    identity_relation="generic_primitive",
                    direct_repair_allowed=True,
                    role=role,
                    value=value,
                    footprint=footprint,
                    compatible_footprints=compatible_footprints,
                ),
            ))
            if len(found) >= limit:
                return found
    return found


def _identity_relation(
    requested_codes: Iterable[str],
    candidate_name: str,
) -> str | None:
    for requested in requested_codes:
        relation = grounding.symbol_identity_match_kind(
            requested,
            candidate_name,
        )
        if relation in {"exact", "kicad_wildcard"}:
            return relation
    return None


def _discovery_score(
    requested_codes: Iterable[str],
    candidate_name: str,
) -> float:
    normalized_candidate = re.sub(r"[^a-z0-9]", "", candidate_name.lower())
    return max(
        (
            difflib.SequenceMatcher(
                None,
                re.sub(r"[^a-z0-9]", "", requested.lower()),
                normalized_candidate,
            ).ratio()
            for requested in requested_codes
        ),
        default=0.0,
    )


def failed_symbol_candidates(
    *,
    role: str,
    value: str,
    proposed_symbol: str,
    footprint: str,
    compatible_footprints: CompatibleFootprintLookup | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return bounded installed candidates for one failed ``symbol:REF`` check."""

    installed_ids = tuple(grounding.symbol_index())
    lookup = _installed_symbol_lookup(installed_ids)
    installed = set(installed_ids)
    specific_codes = _specific_codes(value, proposed_symbol)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for requested in specific_codes:
        exact_ids = lookup.exact_names.get(_identity_key(requested), ())
        for lib_id in exact_ids:
            if lib_id in seen:
                continue
            seen.add(lib_id)
            candidates.append(_candidate_record(
                lib_id,
                identity_relation="exact",
                direct_repair_allowed=True,
                role=role,
                value=value,
                footprint=footprint,
                compatible_footprints=compatible_footprints,
            ))
            if len(candidates) >= limit:
                return candidates
        # Exact installed identities are stronger than wildcard package-family
        # alternatives and are sufficient for a bounded repair prompt.
        if exact_ids:
            continue
        requested_length = len(_identity_key(requested))
        for lib_id, name in lookup.wildcard_names_by_length.get(
            requested_length,
            (),
        ):
            if lib_id in seen:
                continue
            relation = grounding.symbol_identity_match_kind(requested, name)
            if relation != "kicad_wildcard":
                continue
            seen.add(lib_id)
            candidates.append(_candidate_record(
                lib_id,
                identity_relation=relation,
                direct_repair_allowed=True,
                role=role,
                value=value,
                footprint=footprint,
                compatible_footprints=compatible_footprints,
            ))
            if len(candidates) >= limit:
                return candidates

    # A concrete order code must retain its device identity. Generic primitives
    # become candidates only when the user/role actually asks for a primitive.
    if not specific_codes:
        text = f"{role} {value} {proposed_symbol}"
        for _label, candidate in _primitive_candidates(
            text,
            installed=installed,
            role=role,
            value=value,
            footprint=footprint,
            compatible_footprints=compatible_footprints,
            limit=limit - len(candidates),
        ):
            if candidate["symbol"] in seen:
                continue
            seen.add(candidate["symbol"])
            candidates.append(candidate)
            if len(candidates) >= limit:
                return candidates
        if candidates:
            return candidates

    requested = specific_codes or [
        proposed_symbol.partition(":")[2],
        value,
        role,
    ]
    proposed_library = proposed_symbol.partition(":")[0].lower()
    discovery_pool = (
        lookup.by_library.get(proposed_library)
        or lookup.all_names
    )
    discovery: list[tuple[tuple[int, float], str]] = []
    for lib_id, name in discovery_pool:
        if lib_id in seen:
            continue
        score = _discovery_score(requested, name)
        same_library = int(
            bool(proposed_library)
            and lib_id.partition(":")[0].lower() == proposed_library
        )
        if score < (0.55 if same_library else 0.72):
            continue
        discovery.append(((same_library, score), lib_id))
    discovery.sort(key=lambda item: item[0], reverse=True)
    for _score, lib_id in discovery[: max(0, limit - len(candidates))]:
        candidates.append(_candidate_record(
            lib_id,
            identity_relation="discovery_only",
            direct_repair_allowed=False,
            role=role,
            value=value,
            footprint=footprint,
            compatible_footprints=compatible_footprints,
        ))
    return candidates


def requirement_symbol_hints(
    requirement: str,
    *,
    compatible_footprints: CompatibleFootprintLookup | None = None,
    limit_per_label: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """Return semantic and named-device hints from the live installed library."""

    installed_ids = tuple(grounding.symbol_index())
    lookup = _installed_symbol_lookup(installed_ids)
    installed = set(installed_ids)
    hints: dict[str, list[dict[str, Any]]] = {}

    for label, candidate in _primitive_candidates(
        requirement,
        installed=installed,
        compatible_footprints=compatible_footprints,
        limit=len(PRIMITIVE_RULES) * limit_per_label,
    ):
        bucket = hints.setdefault(label, [])
        if len(bucket) < limit_per_label:
            bucket.append(candidate)

    tokens = {
        token
        for token in _TOKEN_RE.findall(requirement)
        if token.upper() not in _GENERIC_NAMED_TOKENS
        and _looks_like_specific_code(token)
        and len(token) <= 64
    }
    for token in sorted(tokens):
        normalized_token = _identity_key(token)
        matched: list[tuple[str, str]] = [
            ("exact", lib_id)
            for lib_id in lookup.exact_names.get(normalized_token, ())
        ]
        if not matched:
            matched.extend(
                (relation, lib_id)
                for lib_id, name in lookup.wildcard_names_by_length.get(
                    len(normalized_token),
                    (),
                )
                if (
                    relation := _identity_relation(
                        (token,),
                        name,
                    )
                )
                == "kicad_wildcard"
            )
        named = [
            _candidate_record(
                lib_id,
                identity_relation=relation,
                direct_repair_allowed=True,
                compatible_footprints=compatible_footprints,
            )
            for relation, lib_id in matched[:limit_per_label]
        ]
        if named:
            hints[token] = named
    return hints


__all__ = [
    "PRIMITIVE_RULES",
    "PrimitiveRule",
    "failed_symbol_candidates",
    "requirement_symbol_hints",
]
