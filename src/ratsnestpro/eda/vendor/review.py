"""Heuristic design-review audits and manufacturing helpers.

These are best-effort checks over the schematic/PCB data we can extract. They
flag likely issues (missing decoupling, floating pins, DFM spacing) rather than
performing a full electrical simulation. Each finding includes a severity so a
caller can triage.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional


def _is_cap(comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    return lib.endswith(":c") or "capacitor" in lib or ":c_" in lib


def _is_resistor(comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    return lib.endswith(":r") or "resistor" in lib or ":r_" in lib


def _is_ic(sch, comp: Dict[str, Any]) -> bool:
    lib = (comp.get("lib_id") or "").lower()
    family = lib.partition(":")[0]
    reference = str(comp.get("reference") or "").upper()
    if any(
        token in family
        for token in (
            "mcu",
            "regulator",
            "amplifier",
            "logic",
            "interface",
            "sensor",
            "memory",
            "driver",
            "rf_module",
        )
    ):
        return True
    if not (
        reference.startswith("U")
        or re.fullmatch(r"IC\d+[A-Z]*", reference) is not None
    ):
        return False
    try:
        pins = sch.pin_locations(comp["reference"])
    except Exception:
        pins = None
    return bool(
        pins
        and len(pins) >= 3
    )


def _dist(a: Dict[str, float], b: Dict[str, float]) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _is_ground_net(name: object) -> bool:
    text = str(name or "").strip().upper()
    return bool(
        text in {"GND", "AGND", "DGND", "PGND", "VSS"}
        or text.endswith("_GND")
        or text.startswith("GND_")
    )


def audit_decoupling(
    sch,
    radius: float = 10.0,
    board=None,
) -> List[Dict[str, Any]]:
    """Audit real PCB power-pin-to-capacitor distance.

    Schematic symbol coordinates express drawing layout, not physical
    placement, so they cannot support a millimetre proximity claim. When no
    PCB is available this audit deliberately emits no distance findings.
    """

    if board is None:
        return []
    comps = sch.list_components()
    caps = [c for c in comps if _is_cap(c)]
    cap_paths: list[tuple[str, str, Dict[str, Any]]] = []
    for cap in caps:
        try:
            pads = board.footprint_pads(cap["reference"])
        except Exception:
            continue
        nets = {pad.get("net") for pad in pads if pad.get("net")}
        grounds = {net for net in nets if _is_ground_net(net)}
        signals = nets - grounds
        if not grounds or len(signals) != 1:
            continue
        supply = next(iter(signals))
        for pad in pads:
            if pad.get("net") == supply:
                cap_paths.append((cap["reference"], supply, pad))

    findings = []
    for c in comps:
        if not _is_ic(sch, c):
            continue
        try:
            symbol_pins = sch.pin_locations(c["reference"]) or []
            board_pads = {
                str(pad.get("number", "")): pad
                for pad in board.footprint_pads(c["reference"])
            }
        except Exception:
            continue
        for pin in symbol_pins:
            pin_type = str(pin.get("type", "")).lower()
            pin_name = str(pin.get("name", "")).upper()
            if (
                pin_type not in {"power_in", "power_out"}
                or _is_ground_net(pin_name)
            ):
                continue
            power_pad = board_pads.get(str(pin.get("number", "")))
            power_net = power_pad.get("net") if power_pad else None
            if not power_pad or not power_net or _is_ground_net(power_net):
                continue
            candidates = [
                (
                    math.hypot(
                        float(power_pad["x"]) - float(cap_pad["x"]),
                        float(power_pad["y"]) - float(cap_pad["y"]),
                    ),
                    cap_ref,
                )
                for cap_ref, cap_net, cap_pad in cap_paths
                if cap_net == power_net
            ]
            if not candidates:
                findings.append({
                    "severity": "warning",
                    "reference": c["reference"],
                    "issue": (
                        f"{c['reference']} pin {pin.get('number')} "
                        f"({pin_name}) on {power_net} has no capacitor from "
                        "that rail to ground"
                    ),
                })
                continue
            distance, cap_ref = min(candidates)
            if distance > radius:
                findings.append({
                    "severity": "warning",
                    "reference": c["reference"],
                    "issue": (
                        f"{c['reference']} pin {pin.get('number')} "
                        f"({pin_name}) nearest grounded rail capacitor "
                        f"{cap_ref} is {distance:.2f}mm away "
                        f"(limit {radius:.1f}mm)"
                    ),
                })
    return findings


def audit_connections(sch) -> List[Dict[str, Any]]:
    findings = []
    nets = [n.lower() for n in sch.list_nets()]
    has_i2c = any("sda" in n or "scl" in n for n in nets)
    resistors = [c for c in sch.list_components() if _is_resistor(c)]
    if has_i2c and len(resistors) < 2:
        findings.append({"severity": "warning",
                         "issue": "I2C nets present but few pull-up resistors found"})
    # Floating pins: pins with no wire/label coincident (needs geometry).
    try:
        from .connectivity import SchematicGraph
        g = SchematicGraph(sch)
        for comp in g.components():
            if len(comp["pins"]) == 1 and not comp["nets"]:
                p = comp["pins"][0]
                findings.append({"severity": "info", "reference": p["ref"],
                                 "pin": p["pin"], "issue": "pin on an unnamed single-node net"})
    except Exception:
        pass
    return findings


def audit_power_rails(sch) -> List[Dict[str, Any]]:
    findings = []
    nets = sch.list_nets()
    power_nets = [n for n in nets if n.upper() in ("VCC", "VDD", "+5V", "+3V3", "+3.3V", "VBUS")
                  or n.startswith("+")]
    caps = [c for c in sch.list_components() if _is_cap(c)]
    if power_nets and not caps:
        findings.append({"severity": "warning",
                         "issue": "power rails present but no bulk/decoupling capacitors found"})
    return findings


def audit_manufacturing(board, min_spacing: float = 0.5) -> List[Dict[str, Any]]:
    findings = []
    fps = [f for f in board.list_footprints() if f.get("at")]
    if board.get_board_extents() is None:
        findings.append({"severity": "error", "issue": "no board outline (Edge.Cuts) found"})
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            d = _dist(fps[i]["at"], fps[j]["at"])
            if d < min_spacing:
                findings.append({"severity": "warning",
                                 "issue": "components very close (%.2fmm)" % d,
                                 "refs": [fps[i]["reference"], fps[j]["reference"]]})
    return findings


def check_bom_health(sch) -> Dict[str, Any]:
    comps = sch.list_components()
    no_value = [c["reference"] for c in comps if not c.get("value")]
    no_footprint = [c["reference"] for c in comps
                    if not (c.get("footprint"))]
    return {"total": len(comps), "missing_value": no_value,
            "missing_footprint": no_footprint,
            "healthy": not no_value and not no_footprint}
