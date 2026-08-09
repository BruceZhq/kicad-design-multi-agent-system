"""Deterministic IR-level rules. Each returns a list of Findings.

Rules check the Circuit IR against the derived Expectations. They never call
an LLM and never mutate the IR. Rules are grouped into gates by
:func:`ratsnestpro.verification.verify.verify_design`.
"""

from __future__ import annotations

from ratsnestpro.domain.contracts import CircuitIR, Finding, Severity, Stage
from ratsnestpro.verification.expectations import Expectations


def _pins_on_net(ir: CircuitIR, net_name: str) -> set[str]:
    net = ir.net(net_name)
    if net is None:
        return set()
    return {p.key() for p in net.pins}


def _finding(
    rule_id: str,
    severity: Severity,
    summary: str,
    details: str = "",
    component_refs: list[str] | None = None,
    net_names: list[str] | None = None,
    repairable: bool = True,
) -> Finding:
    return Finding(
        stage=Stage.VERIFICATION,
        severity=severity,
        rule_id=rule_id,
        summary=summary,
        details=details,
        component_refs=component_refs or [],
        net_names=net_names or [],
        repairable=repairable,
    )


def check_catalog(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    for c in ir.components:
        if not c.catalog_id:
            out.append(
                _finding(
                    "CAT-001",
                    Severity.ERROR,
                    f"component {c.ref} has no catalog_id",
                    component_refs=[c.ref],
                )
            )
    return out


def check_reference_connectivity(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    netted: set[str] = set()
    for net in ir.nets:
        for p in net.pins:
            netted.add(p.component_ref)
    for c in ir.components:
        if c.role == "mounting_hole":
            continue
        if c.ref not in netted:
            out.append(
                _finding(
                    "REF-001",
                    Severity.ERROR,
                    f"component {c.ref} has no connected pins",
                    component_refs=[c.ref],
                )
            )
    # Single-pin signal nets (net with exactly one pin) are suspicious.
    for net in ir.nets:
        if len(net.pins) == 1 and net.name not in (exp.gnd_net, exp.supply_net):
            out.append(
                _finding(
                    "REF-002",
                    Severity.WARNING,
                    f"net {net.name} has only one pin",
                    net_names=[net.name],
                )
            )
    return out


def check_voltage(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    supply = ir.net(exp.supply_net)
    if supply is None:
        out.append(
            _finding("VLT-001", Severity.ERROR, f"missing supply net {exp.supply_net}")
        )
        return out
    declared = supply.properties.get("voltage_v")
    if declared is None or abs(float(declared) - exp.supply_voltage_v) > 1e-6:
        out.append(
            _finding(
                "VLT-002",
                Severity.ERROR,
                f"supply net {exp.supply_net} voltage {declared!r} != expected "
                f"{exp.supply_voltage_v}",
                net_names=[exp.supply_net],
            )
        )
    gnd = ir.net(exp.gnd_net)
    if gnd is None:
        out.append(_finding("VLT-003", Severity.ERROR, f"missing ground net {exp.gnd_net}"))
    return out


def check_decoupling(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    caps = ir.components_with_role("decoupling")
    if len(caps) != exp.decoupling_count:
        out.append(
            _finding(
                "DEC-001",
                Severity.ERROR,
                f"expected {exp.decoupling_count} decoupling caps, found {len(caps)}",
                component_refs=[c.ref for c in caps],
            )
        )
    supply_pins = _pins_on_net(ir, exp.supply_net)
    gnd_pins = _pins_on_net(ir, exp.gnd_net)
    for c in caps:
        if c.value != exp.decoupling_value:
            out.append(
                _finding(
                    "DEC-002",
                    Severity.ERROR,
                    f"decoupling cap {c.ref} value {c.value} != {exp.decoupling_value}",
                    component_refs=[c.ref],
                )
            )
        p1, p2 = f"{c.ref}:1", f"{c.ref}:2"
        if not (p1 in supply_pins and p2 in gnd_pins):
            out.append(
                _finding(
                    "DEC-003",
                    Severity.ERROR,
                    f"decoupling cap {c.ref} not connected between "
                    f"{exp.supply_net} and {exp.gnd_net}",
                    component_refs=[c.ref],
                )
            )
    return out


def check_crystal(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    loads = ir.components_with_role("crystal_load")
    if len(loads) != 2:
        out.append(
            _finding(
                "XTL-001",
                Severity.ERROR,
                f"expected 2 crystal load caps, found {len(loads)}",
                component_refs=[c.ref for c in loads],
            )
        )
    for c in loads:
        if c.value != exp.crystal_load_cap:
            out.append(
                _finding(
                    "XTL-002",
                    Severity.ERROR,
                    f"crystal load cap {c.ref} value {c.value} != expected "
                    f"{exp.crystal_load_cap} for {exp.crystal_freq_mhz} MHz",
                    component_refs=[c.ref],
                )
            )
    gnd_pins = _pins_on_net(ir, exp.gnd_net)
    for c in loads:
        if f"{c.ref}:2" not in gnd_pins:
            out.append(
                _finding(
                    "XTL-003",
                    Severity.ERROR,
                    f"crystal load cap {c.ref} return pin not on {exp.gnd_net}",
                    component_refs=[c.ref],
                )
            )
    return out


def check_ldo_caps(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    ins = ir.components_with_role("ldo_input")
    outs = ir.components_with_role("ldo_output")
    if not ins:
        out.append(_finding("LDO-001", Severity.ERROR, "missing LDO input capacitor"))
    if not outs:
        out.append(_finding("LDO-002", Severity.ERROR, "missing LDO output capacitor"))
    for c in ins:
        if c.value != exp.ldo_input_cap:
            out.append(
                _finding(
                    "LDO-003",
                    Severity.WARNING,
                    f"LDO input cap {c.ref} value {c.value} != {exp.ldo_input_cap}",
                    component_refs=[c.ref],
                )
            )
    supply_pins = _pins_on_net(ir, exp.supply_net)
    for c in outs:
        if f"{c.ref}:1" not in supply_pins:
            out.append(
                _finding(
                    "LDO-004",
                    Severity.ERROR,
                    f"LDO output cap {c.ref} not on regulated rail {exp.supply_net}",
                    component_refs=[c.ref],
                )
            )
    return out


def check_gpio_mapping(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    gpio_nets = [n for n in ir.nets if n.name.startswith("GPIO_")]
    if len(gpio_nets) != exp.header_signal_pins:
        out.append(
            _finding(
                "GPIO-001",
                Severity.ERROR,
                f"expected {exp.header_signal_pins} breakout signal nets, "
                f"found {len(gpio_nets)}",
            )
        )
    # Each GPIO net must connect one MCU pin to one header pin.
    for net in gpio_nets:
        refs = {p.component_ref for p in net.pins}
        has_mcu = any(r == "U2" for r in refs)
        has_header = any(r in ("J2", "J3") for r in refs)
        if not (has_mcu and has_header):
            out.append(
                _finding(
                    "GPIO-002",
                    Severity.ERROR,
                    f"GPIO net {net.name} must connect the MCU to a header pin",
                    net_names=[net.name],
                )
            )
    return out


def check_headers(ir: CircuitIR, exp: Expectations) -> list[Finding]:
    out: list[Finding] = []
    supply_pins = _pins_on_net(ir, exp.supply_net)
    gnd_pins = _pins_on_net(ir, exp.gnd_net)
    for c in ir.components_with_role("breakout_header"):
        if f"{c.ref}:1" not in supply_pins:
            out.append(
                _finding(
                    "HDR-001",
                    Severity.WARNING,
                    f"header {c.ref} pin 1 is not on the supply rail",
                    component_refs=[c.ref],
                )
            )
    for c in ir.components_with_role("breakout_header"):
        header_gnd = any(k.startswith(f"{c.ref}:") for k in gnd_pins)
        if not header_gnd:
            out.append(
                _finding(
                    "HDR-002",
                    Severity.WARNING,
                    f"header {c.ref} has no ground pin",
                    component_refs=[c.ref],
                )
            )
    return out
