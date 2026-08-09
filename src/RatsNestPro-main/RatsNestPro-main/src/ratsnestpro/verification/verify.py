"""Assemble deterministic rules and kicad-cli ERC into gated results.

The required gate list for the ATmega328 family mirrors the RatsNest
qualification profile (schematic slice): plan_contract, catalog,
reference_connectivity, voltage, six_decoupling, crystal_load, ldo_caps,
gpio_mapping, headers, kicad_erc. A required gate that is FAILED or ERROR
blocks; an UNAVAILABLE gate (e.g. ERC with no kicad-cli) is never silently
treated as a pass.
"""

from __future__ import annotations

from pathlib import Path

from ratsnestpro.domain.contracts import (
    CircuitIR,
    Finding,
    GateResult,
    GateStatus,
    Severity,
    VerificationReport,
)
from ratsnestpro.eda import run_erc
from ratsnestpro.verification import rules
from ratsnestpro.verification.expectations import Expectations

# gate name -> (rule callables, required)
_RULE_GATES: list[tuple[str, list, bool]] = [
    ("catalog", [rules.check_catalog], True),
    ("reference_connectivity", [rules.check_reference_connectivity], True),
    ("voltage", [rules.check_voltage], True),
    ("six_decoupling", [rules.check_decoupling], True),
    ("crystal_load", [rules.check_crystal], True),
    ("ldo_caps", [rules.check_ldo_caps], True),
    ("gpio_mapping", [rules.check_gpio_mapping], True),
    ("headers", [rules.check_headers], True),
]


def _status_for(findings: list[Finding]) -> GateStatus:
    if any(f.severity == Severity.ERROR for f in findings):
        return GateStatus.FAILED
    return GateStatus.PASSED


def verify_design(
    ir: CircuitIR,
    expectations: Expectations,
    sch_path: str | Path | None = None,
    explicit_cli: str | None = None,
) -> VerificationReport:
    """Run every deterministic gate, plus kicad-cli ERC when a schematic path
    is supplied and kicad-cli is available."""
    gates: list[GateResult] = []

    # plan_contract: the IR validated on construction, so reaching here means
    # the structural contract holds.
    gates.append(
        GateResult(
            gate="plan_contract",
            status=GateStatus.PASSED,
            required=True,
            summary="Circuit IR passed structural validation",
            metrics={"components": len(ir.components), "nets": len(ir.nets)},
        )
    )

    for name, rule_fns, required in _RULE_GATES:
        findings: list[Finding] = []
        for fn in rule_fns:
            findings.extend(fn(ir, expectations))
        gates.append(
            GateResult(
                gate=name,
                status=_status_for(findings),
                required=required,
                summary=f"{len(findings)} finding(s)",
                findings=findings,
            )
        )

    # kicad_erc gate
    if sch_path is not None:
        erc = run_erc(sch_path, explicit_cli=explicit_cli)
        if not erc.available:
            gates.append(
                GateResult(
                    gate="kicad_erc",
                    status=GateStatus.UNAVAILABLE,
                    required=True,
                    summary="kicad-cli not available; ERC not run",
                )
            )
        else:
            erc_findings = [
                Finding(
                    severity=Severity.ERROR if v.severity == "error" else Severity.WARNING,
                    rule_id=f"ERC-{v.rule_id}",
                    summary=v.message or v.rule_id,
                )
                for v in erc.violations
            ]
            gates.append(
                GateResult(
                    gate="kicad_erc",
                    status=GateStatus.PASSED if erc.ok else GateStatus.FAILED,
                    required=True,
                    summary=erc.summary,
                    findings=erc_findings,
                    metrics={"errors": erc.error_count, "warnings": erc.warning_count},
                )
            )
    else:
        gates.append(
            GateResult(
                gate="kicad_erc",
                status=GateStatus.UNAVAILABLE,
                required=True,
                summary="no schematic path supplied; ERC not run",
            )
        )

    return VerificationReport(gates=gates)
