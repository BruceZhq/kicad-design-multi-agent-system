"""Scorecard: one machine-readable number + per-category deductions.

Formula (design doc §4.3): score = 100 − Σ weight(sev)·count(sev) − erc penalty.
Weights come from the active StrategyBundle — they are an evolvable asset.
"""

from __future__ import annotations

from ratsnest.schemas import Finding, GateStatus, Scorecard, VerificationGate

DEFAULT_WEIGHTS = {"error": 30.0, "warning": 3.0, "info": 0.0, "erc_fail": 15.0}


def compute_scorecard(
    findings: list[Finding],
    weights: dict[str, float] | None = None,
    erc_passed: bool | None = None,
    suppressed_total: int = 0,
    strategy_version_id: str = "",
    gate_results: dict[str, VerificationGate] | None = None,
) -> Scorecard:
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    counts: dict[str, int] = {}
    for f in findings:
        sev = f.severity or "warning"
        counts[sev] = counts.get(sev, 0) + 1

    deductions: dict[str, float] = {}
    for sev, n in counts.items():
        d = w.get(sev, 0.0) * n
        if d:
            deductions[sev] = d
    if erc_passed is False:
        deductions["erc_fail"] = w.get("erc_fail", 15.0)

    score = max(0.0, 100.0 - sum(deductions.values()))
    gates = gate_results or {}
    required = [gate for gate in gates.values() if gate.required]
    return Scorecard(
        score=round(score, 2),
        severity_counts=counts,
        deductions=deductions,
        erc_passed=erc_passed,
        findings_total=len(findings),
        suppressed_total=suppressed_total,
        gate_results=gates,
        required_gates_passed=(
            bool(required)
            and all(gate.status == GateStatus.passed for gate in required)),
        strategy_version_id=strategy_version_id,
    )
