"""Candidate strategy generators — the mutation operators of AHE v1.

Deterministic, reviewable mutations (a candidate is always a small YAML diff
against the incumbent). LLM-driven prompt mutation is a later asset class.
"""

from __future__ import annotations

from ratsnest.schemas import StrategyBundle


def expanded_vref(incumbent: StrategyBundle) -> StrategyBundle:
    """Add LM1117 to the curated Vref table (fixes the lm1117 benchmark gap)."""
    c = incumbent.model_copy(deep=True)
    c.name = "candidate-expanded-vref"
    table = dict(c.solver_params.get("vref_table", {}))
    table["LM1117"] = 1.25
    c.solver_params["vref_table"] = table
    return c


def no_divider_repair(incumbent: StrategyBundle) -> StrategyBundle:
    """Deliberately bad candidate: drops the feedback-divider repair mapping.

    Used to prove the promotion gates reject harmful strategies.
    """
    c = incumbent.model_copy(deep=True)
    c.name = "candidate-no-divider-repair"
    c.repair_mappings = [m for m in c.repair_mappings
                         if m.repair_type != "feedback_divider"]
    return c


def enable_emc(incumbent: StrategyBundle) -> StrategyBundle:
    """Turn on the EMC analyst agent (checker crew roster is strategy-owned)."""
    c = incumbent.model_copy(deep=True)
    c.name = "candidate-enable-emc"
    analysts = dict(c.solver_params.get("analysts", {}))
    analysts["emc"] = True
    c.solver_params["analysts"] = analysts
    return c


GENERATORS = {
    "expanded-vref": expanded_vref,
    "no-divider-repair": no_divider_repair,
    "enable-emc": enable_emc,
}
