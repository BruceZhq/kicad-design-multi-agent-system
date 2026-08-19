"""Evolution Agent: LLM proposes candidate strategies from failure evidence.

Hard boundary (paper pillar 3 / design doc §4.5): the brain can only emit a
BOUNDED DIFF over the allowed asset classes, and the candidate must pass the
same benchmark gates as any other — LLM proposals are never auto-promoted.
"""

from __future__ import annotations

import json
from typing import Any

from ratsnest.protocols import LlmBrain
from ratsnest.schemas import RepairMapping, StrategyBundle, SuppressionRule

ALLOWED_REPAIR_TYPES = ("feedback_divider", "led_resistor", "fill_mpn")
ALLOWED_AGENT_POLICIES = (
    "circuit_architect", "schematic_designer", "pcb_designer", "repair_agent")

_PROPOSER_PROMPT = """You are the evolution agent of a self-evolving KiCad \
design system. You receive trigger statistics and escalation evidence from \
captured run trajectories, plus the incumbent strategy's evolvable assets. \
Propose ONE small candidate mutation that would plausibly fix the observed \
failures.

You may ONLY emit a diff over these asset classes (all optional):
  vref_table_add       {part_substring: vref_volts in [0.4, 5.0]}
  mpn_map_add          {value_string: mpn_string}
  suppressions_add     [{"rule_id": str, "reason": str}]
  repair_mappings_add  [{"match_rule_id": str, "repair_type": one of %s,
                         "params": {}}]
  weight_updates       {"error"|"warning": number in [1, 50]}
  prompt_updates       {agent_name: full policy text, 20..2000 chars}
  tool_policy_updates  {agent_name: {"max_steps": 1..12,
                                     "max_actions_per_step": 1..20}}
  name_suffix          short slug describing the change
  rationale            one paragraph: which evidence motivates this diff

Return ONLY a JSON object with those keys. Propose the SMALLEST diff the
evidence supports — candidates are judged by benchmark gates, and unfocused
diffs fail review.""" % (ALLOWED_REPAIR_TYPES,)


def propose_candidate(incumbent: StrategyBundle, stats: dict[str, Any],
                      llm: LlmBrain | None) -> tuple[str, StrategyBundle, str] | None:
    """Returns (name, candidate_bundle, rationale) or None."""
    if llm is None:
        return None
    payload = {
        "trigger_statistics": stats,
        "incumbent_assets": {
            "vref_table": incumbent.solver_params.get("vref_table", {}),
            "mpn_map_keys": sorted(incumbent.solver_params.get("mpn_map", {})),
            "repair_mappings": [m.model_dump(mode="json")
                                for m in incumbent.repair_mappings],
            "suppression_rule_ids": [s.rule_id for s in incumbent.suppressions],
            "scorecard_weights": incumbent.scorecard_weights,
            "agent_prompts": {name: incumbent.prompts.get(name, "")
                              for name in ALLOWED_AGENT_POLICIES},
            "tool_policies": incumbent.solver_params.get("tool_policies", {}),
        },
    }
    raw = llm.complete_json("evolution_agent", _PROPOSER_PROMPT,
                            json.dumps(payload), max_tokens=1500)
    if not raw:
        return None

    candidate = incumbent.model_copy(deep=True)
    changed = False

    for part, vref in (raw.get("vref_table_add") or {}).items():
        try:
            v = float(vref)
        except (TypeError, ValueError):
            continue
        if 0.4 <= v <= 5.0 and isinstance(part, str) and 2 <= len(part) <= 20:
            table = dict(candidate.solver_params.get("vref_table", {}))
            table[part] = v
            candidate.solver_params["vref_table"] = table
            changed = True

    for value, mpn in (raw.get("mpn_map_add") or {}).items():
        if isinstance(value, str) and isinstance(mpn, str) \
                and 0 < len(mpn) <= 40:
            mpn_map = dict(candidate.solver_params.get("mpn_map", {}))
            mpn_map[value] = mpn
            candidate.solver_params["mpn_map"] = mpn_map
            changed = True

    for s in raw.get("suppressions_add") or []:
        if isinstance(s, dict) and s.get("rule_id") and s.get("reason"):
            candidate.suppressions.append(SuppressionRule(
                rule_id=str(s["rule_id"])[:20],
                reason=str(s["reason"])[:300]))
            changed = True

    for m in raw.get("repair_mappings_add") or []:
        if (isinstance(m, dict) and m.get("match_rule_id")
                and m.get("repair_type") in ALLOWED_REPAIR_TYPES):
            candidate.repair_mappings.append(RepairMapping(
                match_rule_id=str(m["match_rule_id"])[:20],
                repair_type=m["repair_type"],
                params=m.get("params") if isinstance(m.get("params"), dict)
                else {}))
            changed = True

    for sev, weight in (raw.get("weight_updates") or {}).items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if sev in ("error", "warning") and 1.0 <= w <= 50.0:
            candidate.scorecard_weights[sev] = w
            changed = True

    for agent, prompt in (raw.get("prompt_updates") or {}).items():
        if (agent in ALLOWED_AGENT_POLICIES and isinstance(prompt, str)
                and 20 <= len(prompt.strip()) <= 2000):
            candidate.prompts[agent] = prompt.strip()
            changed = True

    policy_updates = raw.get("tool_policy_updates") or {}
    if isinstance(policy_updates, dict):
        policies = dict(candidate.solver_params.get("tool_policies", {}))
        for agent, update in policy_updates.items():
            if agent not in ALLOWED_AGENT_POLICIES or not isinstance(update, dict):
                continue
            current = dict(policies.get(agent, {}))
            accepted = False
            for key, lower, upper in (
                    ("max_steps", 1, 12),
                    ("max_actions_per_step", 1, 20)):
                try:
                    value = int(update[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if lower <= value <= upper:
                    current[key] = value
                    accepted = True
            if accepted:
                policies[agent] = current
                changed = True
        candidate.solver_params["tool_policies"] = policies

    if not changed:
        return None
    suffix = "".join(c for c in str(raw.get("name_suffix", "llm"))[:24]
                     if c.isalnum() or c in "-_") or "llm"
    candidate.name = f"candidate-llm-{suffix}"
    rationale = str(raw.get("rationale", ""))[:1000]
    return candidate.name, candidate, rationale
