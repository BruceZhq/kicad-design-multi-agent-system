"""Repair planner: findings × strategy mapping table -> RepairHints -> PatchPlan.

Discipline (design doc §4.5 risks): repairs come ONLY from the rule_id→mapping
table plus deterministic solvers. Unmapped findings are escalated, never
improvised. E-series snapping reuses kicad-happy's kicad_utils (unforked).
"""

from __future__ import annotations

import re

from ratsnest.circuit_math import format_ohms, resistor_mpn, snap_e_series
from ratsnest.config import Config
from ratsnest.protocols import LlmBrain
from ratsnest.schemas import (
    EvaluationResult,
    Finding,
    PatchPlan,
    RepairHint,
    RepairMapping,
    RepairOp,
    RepairOpType,
    StrategyBundle,
)


# ---------------------------------------------------------------------------
# Solvers — each returns list[RepairOp] (possibly empty) + explanation
# ---------------------------------------------------------------------------

def _solve_feedback_divider(f: Finding, mapping: RepairMapping,
                            strategy: StrategyBundle, config: Config,
                            ctx: dict) -> tuple[list[RepairOp], str]:
    extra = f.model_extra or {}
    r_top, r_bot = extra.get("r_top") or {}, extra.get("r_bottom") or {}
    vref, target = extra.get("vref"), extra.get("target_vout")
    if not (r_top.get("ohms") and r_bot.get("ohms") and vref and target):
        return [], "missing divider payload"
    series = str(mapping.params.get("e_series", "E24"))
    if extra.get("divider_orientation") == "output_adjust_adjust_ground":
        # TLV1117: retain R1 (VOUT-to-ADJ) and solve R2 (ADJ-to-ground),
        # including the catalog-qualified 80uA adjust-pin current estimate.
        adjust_current = 80e-6
        ideal = (target - vref) / (vref / r_top["ohms"] + adjust_current)
        snapped = snap_e_series(config, ideal, series)
        achieved = vref * (1 + snapped / r_top["ohms"]) + adjust_current * snapped
        target_resistor = r_bot
    else:
        # Ground-referenced FB divider: retain bottom and solve upper.
        ideal = r_bot["ohms"] * (target / vref - 1.0)
        snapped = snap_e_series(config, ideal, series)
        achieved = vref * (1 + snapped / r_bot["ohms"])
        target_resistor = r_top
    new_value = format_ohms(snapped)
    op = RepairOp(op=RepairOpType.set_value, ref=target_resistor["ref"],
                  params={"value": new_value}, finding_id=f.finding_id())
    ctx.setdefault("planned_values", {})[target_resistor["ref"]] = new_value
    return [op], (f"set {target_resistor['ref']}={new_value} ({series} snap of "
                  f"{ideal:.1f}Ω) -> Vout {achieved:.3g}V vs target {target}V")


def _solve_led_resistor(f: Finding, mapping: RepairMapping,
                        strategy: StrategyBundle, config: Config,
                        ctx: dict) -> tuple[list[RepairOp], str]:
    fp = (f.model_extra or {}).get("fix_params") or {}
    if fp.get("type") != "resistor_value_change" or not fp.get("component"):
        return [], "no usable fix_params on finding"
    # formula like: "R = (Vrail - Vf) / Iled = (5.0 - 1.8) / 0.01"
    m = re.search(r"\(\s*([\d.]+)\s*-\s*([\d.]+)\s*\)\s*/\s*([\d.]+)",
                  fp.get("formula", ""))
    if not m:
        return [], f"unparseable fix formula: {fp.get('formula')!r}"
    vrail, vf, i_target = (float(m.group(i)) for i in (1, 2, 3))
    if i_target <= 0:
        return [], "non-positive target current"
    series = str(mapping.params.get("e_series", "E24"))
    ideal = (vrail - vf) / i_target
    snapped = snap_e_series(config, ideal, series)
    if snapped < ideal:  # never snap below: current must not exceed target
        snapped = snap_e_series(config, ideal * 1.1, series)
    ref = fp["component"]
    new_value = format_ohms(snapped)
    op = RepairOp(op=RepairOpType.set_value, ref=ref,
                  params={"value": new_value}, finding_id=f.finding_id())
    ctx.setdefault("planned_values", {})[ref] = new_value
    return [op], (f"set {ref}={new_value} for ~{(vrail - vf) / snapped * 1000:.1f}mA "
                  f"(target {i_target * 1000:.0f}mA)")


def _solve_fill_mpn(f: Finding, mapping: RepairMapping,
                    strategy: StrategyBundle, config: Config,
                    ctx: dict) -> tuple[list[RepairOp], str]:
    mpn_map: dict = strategy.solver_params.get("mpn_map", {})
    components: list[dict] = ctx.get("components", [])
    planned_values: dict = ctx.get("planned_values", {})
    already: set = ctx.setdefault("mpn_filled", set())
    ops, misses = [], []
    for comp in components:
        ref = comp.get("reference", "")
        if not ref or ref in already or comp.get("mpn"):
            continue
        # if this plan also changes the value, look up MPN for the NEW value
        value = planned_values.get(ref, comp.get("value", ""))
        mpn = mpn_map.get(value)
        if not mpn and comp.get("type") == "resistor":
            mpn = resistor_mpn(strategy, value)  # pattern fallback
        if not mpn:
            misses.append(f"{ref}({value})")
            continue
        ops.append(RepairOp(op=RepairOpType.set_property, ref=ref,
                            params={"name": "MPN", "value": str(mpn)},
                            finding_id=f.finding_id()))
        already.add(ref)
    note = f"filled {len(ops)} MPNs from curated map"
    if misses:
        note += f"; no mapping for {', '.join(misses)} (escalate)"
    return ops, note


_SOLVERS = {
    "feedback_divider": _solve_feedback_divider,
    "led_resistor": _solve_led_resistor,
    "fill_mpn": _solve_fill_mpn,
}


def _mapping_for(f: Finding, strategy: StrategyBundle) -> RepairMapping | None:
    for m in strategy.repair_mappings:
        if not m.enabled:
            continue
        if m.match_rule_id and m.match_rule_id == f.rule_id:
            return m
        if m.match_detector and m.match_detector == f.detector:
            return m
    return None


_REASONER_PROMPT = """You are the repair reasoning agent of a KiCad design \
loop. Deterministic solvers have already computed candidate repairs for \
analyzer findings. Your job: review each candidate, approve the ones that \
are electrically sound, reject any that could make the design worse, and \
explain WHY in one sentence each. You cannot change values or invent new \
repairs — approve/reject/explain only.

Return ONLY JSON:
{"approve": [finding_id...],
 "reject": [{"finding_id": str, "reason": str}...],
 "notes": {finding_id: one-sentence rationale}}"""


def _reason_about_repairs(hints: list[RepairHint], findings: list[Finding],
                          llm) -> dict | None:
    """Brain path. Returns validated decision or None (keep all hints)."""
    if llm is None or not hints:
        return None
    import json as _json
    payload = {
        "findings": [{"finding_id": f.finding_id(), "rule_id": f.rule_id,
                      "severity": f.severity,
                      "summary": str((f.model_extra or {}).get("summary", ""))[:200]}
                     for f in findings if f.severity in ("error", "warning")],
        "candidate_repairs": [{"finding_id": h.finding_id,
                               "repair_type": h.repair_type,
                               "ops": [{"op": o.op.value, "ref": o.ref,
                                        "params": o.params}
                                       for o in h.suggested_ops],
                               "solver_explanation": h.explanation}
                              for h in hints],
    }
    raw = llm.complete_json("repair_reasoner", _REASONER_PROMPT,
                            _json.dumps(payload), max_tokens=1200)
    if not raw:
        return None
    valid_ids = {h.finding_id for h in hints}
    approve = [i for i in raw.get("approve", []) if i in valid_ids]
    rejects = {r.get("finding_id"): str(r.get("reason", ""))[:200]
               for r in raw.get("reject", [])
               if isinstance(r, dict) and r.get("finding_id") in valid_ids}
    if not approve and not rejects:
        return None  # nothing actionable in the decision -> fail open
    notes = {k: str(v)[:200] for k, v in (raw.get("notes") or {}).items()
             if k in valid_ids}
    return {"approve": set(approve) or (valid_ids - set(rejects)),
            "rejects": rejects, "notes": notes}


def plan_repairs(
    evaluation: EvaluationResult,
    strategy: StrategyBundle,
    run_id: str = "",
    iteration: int = 0,
    config: Config | None = None,
    llm: LlmBrain | None = None,
) -> tuple[PatchPlan, list[RepairHint], list[Finding]]:
    """Returns (plan, hints, escalations). Escalations = actionable findings
    (error/warning) with no mapping or no solvable ops."""
    config = config or Config.load()
    sch = evaluation.analyzer_outputs.get("schematic")
    ctx: dict = {
        "components": ((sch.model_extra or {}).get("components", []) if sch else []),
    }

    # value-changing solvers must run before fill_mpn (MPN follows new value)
    actionable = [f for f in evaluation.findings if f.severity in ("error", "warning")]
    order = {"feedback_divider": 0, "led_resistor": 0, "fill_mpn": 1}
    matched: list[tuple[Finding, RepairMapping]] = []
    escalations: list[Finding] = []
    for f in actionable:
        m = _mapping_for(f, strategy)
        (matched.append((f, m)) if m else escalations.append(f))
    matched.sort(key=lambda fm: order.get(fm[1].repair_type, 2))

    hints: list[RepairHint] = []
    all_ops: list[RepairOp] = []
    rationale: dict[str, str] = {}
    for f, m in matched:
        solver = _SOLVERS.get(m.repair_type)
        if solver is None:
            escalations.append(f)
            continue
        ops, explanation = solver(f, m, strategy, config, ctx)
        if not ops:
            escalations.append(f)
            continue
        hints.append(RepairHint(
            finding_id=f.finding_id(), rule_id=f.rule_id, severity=f.severity,
            repair_type=m.repair_type, targets=[op.ref for op in ops],
            suggested_ops=ops, confidence=f.confidence or "heuristic",
            explanation=explanation,
        ))
        all_ops.extend(ops)
        rationale[f.finding_id()] = explanation

    # brain review: approve/reject/explain — solver output stays authoritative
    # for values; rejected hints escalate instead of applying silently
    decision = _reason_about_repairs(hints, evaluation.findings, llm)
    if decision is not None:
        approved: set = decision["approve"]
        kept_hints = [h for h in hints if h.finding_id in approved]
        all_ops = [op for h in kept_hints for op in h.suggested_ops]
        finding_by_id = {f.finding_id(): f for f in evaluation.findings}
        for fid, reason in decision["rejects"].items():
            rationale[fid] = f"REJECTED by repair reasoner: {reason}"
            if fid in finding_by_id:
                escalations.append(finding_by_id[fid])
        for fid, note in decision["notes"].items():
            if fid in approved:
                rationale[fid] = f"{rationale.get(fid, '')} | reasoner: {note}"
        hints = kept_hints

    plan = PatchPlan(run_id=run_id, iteration=iteration, ops=all_ops,
                     strategy_version_id=strategy.version_id(),
                     rationale=rationale)
    return plan, hints, escalations
