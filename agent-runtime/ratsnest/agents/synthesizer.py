"""Review synthesizer: merge, suppress, dedupe, and augment analyzer findings.

Deterministic core (default). The augmentation rules are where RatsNest adds
review intelligence above the raw analyzers — e.g. cross-checking a computed
feedback-divider output against the *name* of the rail it drives, using the
strategy's curated Vref table.
"""

from __future__ import annotations

import re
from pathlib import Path

from ratsnest.kh_adapter.scorecard import compute_scorecard
from ratsnest.schemas import (
    AnalyzerOutput,
    EvaluationResult,
    Finding,
    StrategyBundle,
    VerificationGate,
)

_SEV_RANK = {"error": 0, "warning": 1, "info": 2}


def parse_rail_voltage(rail: str) -> float | None:
    """'+5V' -> 5.0, '+3V3' -> 3.3, '12V' -> 12.0, 'VBUS'/'VCC' -> None."""
    m = re.fullmatch(r"[+-]?(\d+)V(\d*)", rail.strip())
    if not m:
        return None
    whole, frac = m.group(1), m.group(2)
    return float(f"{whole}.{frac}" if frac else whole)


def _match_vref(strategy: StrategyBundle, *texts: str) -> float | None:
    table = strategy.solver_params.get("vref_table", {})
    for key, vref in table.items():
        for t in texts:
            if t and key.lower() in t.lower():
                return float(vref)
    return None


def _augment_vout_mismatch(findings: list[Finding],
                           strategy: StrategyBundle) -> list[Finding]:
    """RN-VOUT-001: divider-set Vout disagrees with the rail's name."""
    out = []
    tol = float(strategy.solver_params.get("vout_tolerance_pct", 2.0)) / 100.0
    for f in findings:
        if f.rule_id != "PR-DET":
            continue
        extra = f.model_extra or {}
        div = extra.get("feedback_divider") or {}
        rail = extra.get("output_rail") or ""
        r_top, r_bot = div.get("r_top") or {}, div.get("r_bottom") or {}
        target = parse_rail_voltage(rail)
        # match on Value only: lib symbols get reused across pin-compatible
        # parts, so lib_id is not authoritative for part identity
        vref = _match_vref(strategy, extra.get("value", ""))
        if not (target and vref and r_top.get("ohms") and r_bot.get("ohms")):
            continue
        regulator_value = str(extra.get("value", ""))
        tlv1117 = "tlv1117" in regulator_value.lower()
        if tlv1117:
            # TLV1117: R1 is VOUT-to-ADJ, R2 is ADJ-to-ground.
            expected = (vref * (1 + r_bot["ohms"] / r_top["ohms"])
                        + 80e-6 * r_bot["ohms"])
        else:
            expected = vref * (1 + r_top["ohms"] / r_bot["ohms"])
        if abs(expected - target) <= tol * target:
            continue
        reg_ref = extra.get("ref", "")
        out.append(Finding.model_validate({
            "detector": "ratsnest_synthesizer",
            "rule_id": "RN-VOUT-001",
            "severity": "error",
            "confidence": "deterministic",
            "summary": (f"Regulator {reg_ref}: feedback divider sets "
                        f"Vout={expected:.3g}V but the rail is named {rail} "
                        f"(Vref={vref}V, {r_top.get('ref')}/{r_bot.get('ref')})"),
            "components": [c for c in (r_top.get("ref"), r_bot.get("ref"), reg_ref) if c],
            "regulator_ref": reg_ref,
            "r_top": r_top,
            "r_bottom": r_bot,
            "vref": vref,
            "target_vout": target,
            "computed_vout": round(expected, 4),
            "rail": rail,
            "divider_orientation": (
                "output_adjust_adjust_ground" if tlv1117
                else "output_feedback_feedback_ground"),
        }))
    return out


def _suppressed(f: Finding, strategy: StrategyBundle) -> bool:
    comps = set(f.components_involved())
    for rule in strategy.suppressions:
        if rule.rule_id and rule.rule_id != f.rule_id:
            continue
        if rule.detector and rule.detector != f.detector:
            continue
        if rule.ref and rule.ref not in comps:
            continue
        if rule.rule_id or rule.detector or rule.ref:
            return True
    return False


def synthesize(
    outputs: dict[str, AnalyzerOutput],
    strategy: StrategyBundle,
    project_dir: Path | str = "",
    erc_passed: bool | None = None,
    gate_results: dict[str, VerificationGate] | None = None,
    additional_findings: list[Finding] | None = None,
) -> EvaluationResult:
    raw: list[Finding] = []
    for env in outputs.values():
        raw.extend(env.findings)
    raw.extend(additional_findings or [])

    # augment BEFORE suppression so suppressions can also target synthesized rules
    raw.extend(_augment_vout_mismatch(raw, strategy))

    kept: list[Finding] = []
    suppressed = 0
    seen: set[str] = set()
    for f in raw:
        if _suppressed(f, strategy):
            suppressed += 1
            continue
        fid = f.finding_id()
        if fid in seen:
            continue
        seen.add(fid)
        kept.append(f)

    kept.sort(key=lambda f: (_SEV_RANK.get(f.severity, 3), f.rule_id or "",
                             f.finding_id()))
    scorecard = compute_scorecard(
        kept, weights=strategy.scorecard_weights, erc_passed=erc_passed,
        suppressed_total=suppressed, strategy_version_id=strategy.version_id(),
        gate_results=gate_results,
    )
    return EvaluationResult(project_dir=str(project_dir), scorecard=scorecard,
                            findings=kept, analyzer_outputs=outputs)
