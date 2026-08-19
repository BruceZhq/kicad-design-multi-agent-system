"""Authoritative circuit-family selection, value solving, and derating.

This module contains no KiCad or agent behavior. It converts a validated
DesignSpec into one deterministic SolvedCircuit that every backend, checker,
and approval-time revalidation path must use.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ratsnest.catalog import led_catalog_id, load_catalog
from ratsnest.config import Config
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import DesignSpec, StrategyBundle

LDO_TOPOLOGY = "adjustable_ldo"
BUCK_TOPOLOGY = "asynchronous_buck"
LDO_PART = "TLV1117-ADJ"
BUCK_PART = "LM2596S-ADJ"
REGULATOR_PART = LDO_PART  # compatibility name; new code uses SolvedCircuit
REQUIRED_PRODUCTION_GATES = (
    "catalog", "bom", "erc", "drc", "spice", "thermal", "emc")

LED_I_TARGET_A = 0.008
DEFAULT_LED_VF = {"red": 2.0, "green": 2.2, "blue": 3.1}

LDO_MAX_CURRENT_A = 0.5
LDO_MAX_LOSS_W = 0.5
LDO_MIN_EFFICIENCY = 0.65
DESIGN_TJ_LIMIT_C = 110.0
BUCK_MAX_CURRENT_A = 2.0
BUCK_INPUT_MAX_V = 35.0
BUCK_OUTPUT_MAX_V = 30.0


class GenerationError(ValueError):
    pass


class UnsupportedRequirementError(GenerationError):
    pass


class SolvedCircuit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topology: Literal["adjustable_ldo", "asynchronous_buck"]
    family_version: str
    catalog_version: str
    values: dict[str, str]
    mpns: dict[str, str]
    catalog_ids: dict[str, str]
    roles: dict[str, str]
    include_led: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    required_gates: tuple[str, ...] = REQUIRED_PRODUCTION_GATES


def snap_e_series(config: Config, ideal: float, series: str = "E24") -> float:
    """Snap to the nearest E-series value via kicad-happy's utility."""
    utils = load_kh_module("kicad_utils", config.kicad_scripts)
    snapped, _ = utils.snap_to_e_series(ideal, series)
    return float(snapped)


def format_ohms(value: float) -> str:
    """3000 -> '3k', 4700 -> '4.7k', 330 -> '330'."""
    for factor, suffix in ((1e6, "M"), (1e3, "k")):
        if value >= factor:
            text = f"{value / factor:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def parse_si_value(value: str) -> float:
    """Parse the compact resistor/inductor/capacitor values emitted here."""
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([pnumkM]?)\s*(?:[A-Za-zΩ]+)?(?:\s+\d+V)?\s*",
        value)
    if not match:
        raise GenerationError(f"unsupported engineering value {value!r}")
    multipliers = {
        "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
        "": 1.0, "k": 1e3, "M": 1e6,
    }
    return float(match.group(1)) * multipliers[match.group(2)]


def resistor_mpn(strategy: StrategyBundle, value_str: str,
                 catalog_id: str = "yageo.rc0805fr") -> str:
    """Resolve an approved YAGEO value-coded MPN for a catalog package."""
    patterns = {
        "yageo.rc0805fr": (
            "resistor_mpn_pattern", "RC0805FR-07{code}L"),
        "yageo.rc1206fr": (
            "resistor_1206_mpn_pattern", "RC1206FR-07{code}L"),
    }
    if catalog_id not in patterns:
        raise GenerationError(f"unsupported resistor catalog id {catalog_id!r}")
    mpn_map: dict = strategy.solver_params.get("mpn_map", {})
    if catalog_id == "yageo.rc0805fr" and value_str in mpn_map:
        return str(mpn_map[value_str])
    pattern_key, default_pattern = patterns[catalog_id]
    pattern = strategy.solver_params.get(pattern_key, default_pattern)
    if value_str.endswith(("k", "M")):
        suffix = value_str[-1].upper()
        body = value_str[:-1]
        if "." in body:
            whole, fraction = body.split(".")
            code = f"{whole}{suffix}{fraction}"
        else:
            code = f"{body}{suffix}"
    else:
        code = f"{value_str}R"
    return pattern.format(code=code)


def _vref_for(strategy: StrategyBundle, part: str, catalog_id: str) -> float:
    catalog_vref = load_catalog().entry(catalog_id).ratings.get("vref_v")
    if catalog_vref is None:
        raise GenerationError(f"catalog has no Vref for {part}")
    strategy_values = strategy.solver_params.get("vref_table", {})
    matches = [float(value) for key, value in strategy_values.items()
               if key.lower() in part.lower()]
    if matches and abs(matches[0] - catalog_vref) > 1e-9:
        raise GenerationError(
            f"strategy Vref for {part} conflicts with trusted catalog")
    return float(catalog_vref)


def pick_divider(config: Config, target: float, vref: float,
                 tolerance_pct: float = 2.0) -> tuple[float, float, float]:
    """Pick (upper, lower, achieved) for a ground-referenced FB divider."""
    best: tuple[float, float, float, float] | None = None
    for lower in (1000.0, 1200.0, 1500.0, 2000.0):
        ideal_upper = lower * (target / vref - 1.0)
        if ideal_upper <= 0:
            continue
        upper = snap_e_series(config, ideal_upper)
        achieved = vref * (1 + upper / lower)
        deviation = abs(achieved - target) / target
        if best is None or deviation < best[3]:
            best = (upper, lower, achieved, deviation)
    if best is None or best[3] > tolerance_pct / 100.0:
        raise GenerationError(
            f"no E24 divider reaches {target}V within {tolerance_pct}% "
            f"(best: {best[2] if best else None}V)")
    return best[0], best[1], best[2]


def pick_ldo_divider(config: Config, target: float, vref: float,
                     tolerance_pct: float = 2.0,
                     adjust_current_a: float = 80e-6,
                     ) -> tuple[float, float, float]:
    """Pick TLV1117 R1(output-ADJ), R2(ADJ-ground), and achieved Vout."""
    best: tuple[float, float, float, float] | None = None
    for upper in (120.0, 150.0, 180.0, 220.0):
        ideal_lower = upper * (target / vref - 1.0)
        if ideal_lower <= 0:
            continue
        lower = snap_e_series(config, ideal_lower)
        achieved = vref * (1 + lower / upper) + adjust_current_a * lower
        deviation = abs(achieved - target) / target
        if best is None or deviation < best[3]:
            best = (upper, lower, achieved, deviation)
    if best is None or best[3] > tolerance_pct / 100.0:
        raise GenerationError(
            f"no TLV1117 E24 divider reaches {target}V within "
            f"{tolerance_pct}%")
    return best[0], best[1], best[2]


_UNSUPPORTED_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bboost\b|\bstep[- ]?up\b|升压", "boost converter"),
    (r"buck[- ]?boost|升降压", "buck-boost converter"),
    (r"\bflyback\b|反激", "flyback converter"),
    (r"\bisolat(?:ed|ion)\b|隔离", "isolated power"),
    (r"\binverter\b|逆变", "inverter"),
    (r"battery\s+charg|充电器|充电板", "battery charger"),
    (r"\b(?:mcu|microcontroller|stm32|esp32|arduino|fpga)\b|单片机|微控制器",
     "programmable logic"),
    (r"\busb\b|以太网|\bethernet\b|\bcan(?:\s+bus)?\b", "digital interface"),
    (r"motor\s+(?:driver|control)|电机驱动|电机控制", "motor control"),
)


def detect_unsupported_features(text: str) -> list[str]:
    found: list[str] = []
    for pattern, label in _UNSUPPORTED_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE) and label not in found:
            found.append(label)
    return found


def validate_supported_requirement(spec: DesignSpec) -> None:
    unsupported = list(spec.unsupported_features)
    for feature in detect_unsupported_features(spec.requirement_text):
        if feature not in unsupported:
            unsupported.append(feature)
    if unsupported:
        raise UnsupportedRequirementError(
            "unsupported Stage 3 requirement features: "
            + ", ".join(unsupported))
    if spec.output_voltage >= spec.input_voltage:
        raise UnsupportedRequirementError(
            "Stage 3 supports step-down power only; output must be below input")


def _ldo_metrics(spec: DesignSpec) -> dict[str, float]:
    catalog = load_catalog().entry("ti.tlv1117-adj.dcy")
    dropout = float(catalog.ratings["dropout_max_v"])
    loss = (spec.input_voltage - spec.output_voltage) * spec.output_current_a
    efficiency = spec.output_voltage / spec.input_voltage
    junction = (spec.ambient_temperature_c
                + loss * float(catalog.ratings["theta_ja_c_per_w"]))
    return {
        "dropout_margin_v": spec.input_voltage - spec.output_voltage - dropout,
        "controller_loss_w": loss,
        "estimated_junction_c": junction,
        "estimated_efficiency_pct": efficiency * 100,
        "max_junction_c": DESIGN_TJ_LIMIT_C,
    }


def _ldo_supported(spec: DesignSpec) -> tuple[bool, str]:
    metrics = _ldo_metrics(spec)
    reasons = []
    if not (2.7 <= spec.input_voltage <= 13.0):
        reasons.append("input is outside the 2.7-13V product envelope")
    if not (1.25 <= spec.output_voltage <= 13.7):
        reasons.append("output is outside 1.25-13.7V")
    if spec.output_current_a > LDO_MAX_CURRENT_A:
        reasons.append(f"current exceeds {LDO_MAX_CURRENT_A}A product derating")
    if metrics["dropout_margin_v"] < 0:
        reasons.append("worst-case dropout margin is negative")
    if metrics["controller_loss_w"] > LDO_MAX_LOSS_W:
        reasons.append(f"linear loss exceeds {LDO_MAX_LOSS_W}W")
    if metrics["estimated_efficiency_pct"] < LDO_MIN_EFFICIENCY * 100:
        reasons.append("linear efficiency is below 65%")
    if metrics["estimated_junction_c"] > DESIGN_TJ_LIMIT_C:
        reasons.append("estimated junction temperature exceeds 110C")
    return not reasons, "; ".join(reasons)


def _buck_metrics(spec: DesignSpec) -> dict[str, float]:
    catalog = load_catalog()
    regulator = catalog.entry("ti.lm2596s-adj.ktt")
    diode = catalog.entry("onsemi.nrvbs540t3g")
    inductor = catalog.entry("coilcraft.mss1210h-683med")
    vf = float(diode.ratings["forward_voltage_v"])
    frequency = float(regulator.ratings["switching_hz"])
    duty = (spec.output_voltage + vf) / (spec.input_voltage + vf)
    ripple_a = ((spec.input_voltage - spec.output_voltage) * duty
                / (float(inductor.ratings["inductance_uh"]) * 1e-6 * frequency))
    peak_a = spec.output_current_a + ripple_a / 2
    switch_loss = (duty * spec.output_current_a
                   * float(regulator.ratings["switch_sat_v"])
                   + spec.input_voltage * float(regulator.ratings["iq_a"]))
    diode_loss = ((1 - duty) * spec.output_current_a * vf)
    inductor_loss = (spec.output_current_a ** 2
                     * float(inductor.ratings["dcr_max_ohm"]))
    total_loss = switch_loss + diode_loss + inductor_loss
    output_power = spec.output_voltage * spec.output_current_a
    efficiency = output_power / (output_power + total_loss)
    junction = (spec.ambient_temperature_c
                + switch_loss * float(regulator.ratings["theta_ja_c_per_w"]))
    return {
        "duty_cycle": duty,
        "switching_frequency_hz": frequency,
        "inductor_ripple_a": ripple_a,
        "inductor_peak_a": peak_a,
        "controller_loss_w": switch_loss,
        "diode_loss_w": diode_loss,
        "inductor_loss_w": inductor_loss,
        "total_loss_w": total_loss,
        "estimated_junction_c": junction,
        "estimated_efficiency_pct": efficiency * 100,
        "max_junction_c": DESIGN_TJ_LIMIT_C,
    }


def _buck_supported(spec: DesignSpec) -> tuple[bool, str]:
    metrics = _buck_metrics(spec)
    catalog = load_catalog()
    inductor = catalog.entry("coilcraft.mss1210h-683med")
    reasons = []
    if not (7.0 <= spec.input_voltage <= BUCK_INPUT_MAX_V):
        reasons.append(f"input is outside 7-{BUCK_INPUT_MAX_V:g}V")
    if not (1.23 <= spec.output_voltage <= BUCK_OUTPUT_MAX_V):
        reasons.append(f"output is outside 1.23-{BUCK_OUTPUT_MAX_V:g}V")
    if spec.output_current_a > BUCK_MAX_CURRENT_A:
        reasons.append(f"current exceeds {BUCK_MAX_CURRENT_A:g}A product derating")
    if not (0.1 <= metrics["duty_cycle"] <= 0.85):
        reasons.append("duty cycle is outside the qualified 10-85% range")
    peak_limit = 0.8 * float(inductor.ratings["isat_10pct_a"])
    if metrics["inductor_peak_a"] > peak_limit:
        reasons.append("inductor peak current exceeds 80% saturation derating")
    if metrics["estimated_junction_c"] > DESIGN_TJ_LIMIT_C:
        reasons.append("estimated junction temperature exceeds 110C")
    return not reasons, "; ".join(reasons)


def select_topology(spec: DesignSpec) -> Literal[
        "adjustable_ldo", "asynchronous_buck"]:
    validate_supported_requirement(spec)
    ldo_ok, ldo_reason = _ldo_supported(spec)
    buck_ok, buck_reason = _buck_supported(spec)
    if spec.topology == "ldo":
        if not ldo_ok:
            raise GenerationError(f"requested LDO is outside envelope: {ldo_reason}")
        return LDO_TOPOLOGY
    if spec.topology == "buck":
        if not buck_ok:
            raise GenerationError(f"requested Buck is outside envelope: {buck_reason}")
        return BUCK_TOPOLOGY
    if ldo_ok:
        return LDO_TOPOLOGY
    if buck_ok:
        return BUCK_TOPOLOGY
    raise GenerationError(
        "no qualified Stage 3 family: "
        f"LDO [{ldo_reason}]; Buck [{buck_reason}]")


def _common_maps(strategy: StrategyBundle, spec: DesignSpec,
                 led_ref: str) -> tuple[dict[str, str], dict[str, str],
                                        dict[str, str], dict[str, str]]:
    catalog = load_catalog()
    ids = {
        "J1": "jst.b2b-xh-a", "J2": "jst.b2b-xh-a",
        "TP1": "board.testpad-2p0", "TP2": "board.testpad-2p0",
        "TP3": "board.testpad-2p0", "TP4": "board.testpad-2p0",
        "#FLG01": "kicad.power-flag", "#FLG02": "kicad.power-flag",
    }
    roles = {
        "J1": "input_connector", "J2": "output_connector",
        "TP1": "input_testpoint", "TP2": "output_testpoint",
        "TP3": "ground_testpoint", "TP4": "feedback_testpoint",
        "#FLG01": "input_power_flag",
        "#FLG02": "ground_power_flag",
    }
    values = {ref: catalog.entry(catalog_id).value
              for ref, catalog_id in ids.items()}
    mpns = {ref: catalog.entry(catalog_id).mpn
            for ref, catalog_id in ids.items()}
    if spec.led is not None:
        ids["TP5"] = "board.testpad-2p0"
        roles["TP5"] = "indicator_testpoint"
        values["TP5"] = catalog.entry(ids["TP5"]).value
        mpns["TP5"] = catalog.entry(ids["TP5"]).mpn
        led_id = led_catalog_id(spec.led)
        ids[led_ref] = led_id
        roles[led_ref] = "power_indicator"
        values[led_ref] = catalog.entry(led_id).value
        mpns[led_ref] = catalog.entry(led_id).mpn
        vf = float(catalog.entry(led_id).ratings["forward_voltage_v"])
        headroom = spec.output_voltage - vf
        if headroom <= 0.3:
            raise GenerationError(
                f"{spec.led} LED has insufficient output-voltage headroom")
        resistance = snap_e_series(Config.load(), headroom / LED_I_TARGET_A)
        if resistance < headroom / LED_I_TARGET_A:
            resistance = snap_e_series(Config.load(), resistance * 1.1)
        resistor_ref = "R3"
        ids[resistor_ref] = "yageo.rc0805fr"
        roles[resistor_ref] = "indicator_current_limit"
        values[resistor_ref] = format_ohms(resistance)
        mpns[resistor_ref] = resistor_mpn(strategy, values[resistor_ref])
    return values, mpns, ids, roles


def _solve_ldo(spec: DesignSpec, strategy: StrategyBundle,
               config: Config) -> SolvedCircuit:
    catalog = load_catalog()
    vref = _vref_for(strategy, LDO_PART, "ti.tlv1117-adj.dcy")
    tolerance = float(strategy.solver_params.get("vout_tolerance_pct", 2.0))
    r1, r2, achieved = pick_ldo_divider(
        config, spec.output_voltage, vref, tolerance)
    values, mpns, ids, roles = _common_maps(strategy, spec, "D1")
    ids.update({
        "U1": "ti.tlv1117-adj.dcy", "C1": "kemet.t491b106m025at",
        "C2": "kemet.t491b106m025at", "R1": "yageo.rc0805fr",
        "R2": "yageo.rc0805fr",
    })
    roles.update({
        "U1": "linear_regulator", "C1": "input_stability",
        "C2": "output_stability", "R1": "output_to_adjust",
        "R2": "adjust_to_ground",
    })
    values.update({
        "U1": LDO_PART, "C1": catalog.entry(ids["C1"]).value,
        "C2": catalog.entry(ids["C2"]).value,
        "R1": format_ohms(r1), "R2": format_ohms(r2),
    })
    mpns.update({
        "U1": catalog.entry(ids["U1"]).mpn,
        "C1": catalog.entry(ids["C1"]).mpn,
        "C2": catalog.entry(ids["C2"]).mpn,
        "R1": resistor_mpn(strategy, values["R1"]),
        "R2": resistor_mpn(strategy, values["R2"]),
    })
    metrics = _ldo_metrics(spec)
    metrics.update({"vref_v": vref, "achieved_output_v": achieved})
    return SolvedCircuit(
        topology=LDO_TOPOLOGY, family_version="ldo.v1",
        catalog_version=catalog.version, values=values, mpns=mpns,
        catalog_ids=ids, roles=roles, include_led=spec.led is not None,
        metrics=metrics)


def _solve_buck(spec: DesignSpec, strategy: StrategyBundle,
                config: Config) -> SolvedCircuit:
    catalog = load_catalog()
    vref = _vref_for(strategy, BUCK_PART, "ti.lm2596s-adj.ktt")
    tolerance = float(strategy.solver_params.get("vout_tolerance_pct", 2.0))
    upper, lower, achieved = pick_divider(
        config, spec.output_voltage, vref, tolerance)
    values, mpns, ids, roles = _common_maps(strategy, spec, "D2")
    ids.update({
        "U1": "ti.lm2596s-adj.ktt", "C1": "panasonic.eeufr1h471",
        "C2": "panasonic.eeufr1h221", "D1": "onsemi.nrvbs540t3g",
        "L1": "coilcraft.mss1210h-683med", "R1": "yageo.rc1206fr",
        "R2": "yageo.rc1206fr",
    })
    roles.update({
        "U1": "buck_regulator", "C1": "input_bulk",
        "C2": "output_bulk", "D1": "catch_diode", "L1": "power_inductor",
        "R1": "feedback_to_ground", "R2": "output_to_feedback",
    })
    values.update({
        "U1": BUCK_PART, "C1": catalog.entry(ids["C1"]).value,
        "C2": catalog.entry(ids["C2"]).value,
        "D1": catalog.entry(ids["D1"]).value,
        "L1": catalog.entry(ids["L1"]).value,
        "R1": format_ohms(lower), "R2": format_ohms(upper),
    })
    mpns.update({
        ref: (resistor_mpn(strategy, values[ref], ids[ref])
              if ref in {"R1", "R2"} else catalog.entry(ids[ref]).mpn)
        for ref in ("U1", "C1", "C2", "D1", "L1", "R1", "R2")
    })
    metrics = _buck_metrics(spec)
    metrics.update({"vref_v": vref, "achieved_output_v": achieved})
    return SolvedCircuit(
        topology=BUCK_TOPOLOGY, family_version="buck.v1",
        catalog_version=catalog.version, values=values, mpns=mpns,
        catalog_ids=ids, roles=roles, include_led=spec.led is not None,
        metrics=metrics)


def solve_circuit(spec: DesignSpec, strategy: StrategyBundle,
                  config: Config | None = None) -> SolvedCircuit:
    config = config or Config.load()
    topology = select_topology(spec)
    if topology == LDO_TOPOLOGY:
        return _solve_ldo(spec, strategy, config)
    return _solve_buck(spec, strategy, config)


def solve_board_values(spec: DesignSpec, strategy: StrategyBundle,
                       config: Config | None = None,
                       regulator_part: str | None = None,
                       ) -> tuple[dict[str, str], dict[str, str], bool]:
    """Legacy tuple adapter. New code should retain the full SolvedCircuit."""
    solved = solve_circuit(spec, strategy, config)
    if regulator_part and regulator_part not in {solved.values.get("U1"), LDO_PART}:
        raise GenerationError("legacy regulator override conflicts with family selection")
    return dict(solved.values), dict(solved.mpns), solved.include_led
