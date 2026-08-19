"""Trigger statistics from captured ATDP trajectories (paper [1] §5).

Statistics — not anecdotes — decide when to evolve and which surface to
mutate: escalation clusters point at missing repair mappings / solver params,
veto clusters point at bad solver output, healthy runs propose no-op.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def compute_stats(runs_dir: Path) -> dict:
    runs_dir = Path(runs_dir)
    stats = {
        "runs": 0, "converged": 0, "escalated": 0, "vetoes": 0,
        "mean_final_reward": 0.0,
        "escalated_rule_ids": {}, "planned_repair_types": {},
        "agent_plan_calls": {}, "agent_tool_failures": {},
        "blocked_agent_tasks": {},
    }
    rewards: list[float] = []
    escalated: Counter = Counter()
    planned: Counter = Counter()
    agent_plans: Counter = Counter()
    tool_failures: Counter = Counter()
    blocked_tasks: Counter = Counter()

    for traj in sorted(runs_dir.glob("*/trajectory.jsonl")):
        stats["runs"] += 1
        for line in traj.read_text(encoding="utf-8").splitlines():
            evt = json.loads(line)
            node, outcome = evt.get("node"), evt.get("outcome", {})
            if node == "plan_repairs":
                for rid in evt.get("action", {}).get("escalated_rule_ids", []):
                    if rid:
                        escalated[rid] += 1
                for hint in evt.get("agent_state", {}).get("hints", []):
                    planned[hint.get("repair_type", "?")] += 1
            elif node == "verify" and outcome.get("vetoed"):
                stats["vetoes"] += 1
            elif isinstance(node, str) and node.startswith("design.") \
                    and node.endswith(".plan"):
                agent_plans[node.split(".")[1]] += 1
            elif isinstance(node, str) and node.startswith("design.") \
                    and node.endswith(".tool") and not outcome.get("ok", False):
                tool_failures[node.split(".")[1]] += 1
            elif node == "blackboard.message":
                action = evt.get("action", {})
                payload = action.get("payload", {})
                if (action.get("kind") == "status"
                        and payload.get("status") == "blocked"):
                    blocked_tasks[action.get("sender", "unknown")] += 1
            elif node == "finish":
                if outcome.get("status") == "converged":
                    stats["converged"] += 1
                elif outcome.get("status") == "escalated":
                    stats["escalated"] += 1
                if evt.get("reward") is not None:
                    rewards.append(float(evt["reward"]))

    if rewards:
        stats["mean_final_reward"] = round(sum(rewards) / len(rewards), 2)
    stats["escalated_rule_ids"] = dict(escalated.most_common())
    stats["planned_repair_types"] = dict(planned.most_common())
    stats["agent_plan_calls"] = dict(agent_plans.most_common())
    stats["agent_tool_failures"] = dict(tool_failures.most_common())
    stats["blocked_agent_tasks"] = dict(blocked_tasks.most_common())
    return stats


def propose_surface(stats: dict) -> str:
    """Map trajectory statistics to an intervention-surface proposal."""
    esc = stats.get("escalated_rule_ids", {})
    if esc:
        rule, count = next(iter(esc.items()))
        return (f"skill-patch surface: {count} runs escalated on {rule} — "
                f"propose a repair mapping or solver_params extension for it")
    if stats.get("vetoes", 0) > 0:
        return ("skill-patch surface: patches were vetoed — review solver "
                "params (values computed by solvers made boards worse)")
    failures = stats.get("agent_tool_failures", {})
    blocked = stats.get("blocked_agent_tasks", {})
    if failures or blocked:
        agent = next(iter(failures or blocked))
        count = (failures or blocked)[agent]
        return (f"agent-policy surface: {agent} has {count} failed/blocked "
                "events - propose a bounded prompt or tool-budget update")
    if stats.get("runs", 0) and stats.get("escalated", 0) == 0:
        return "no-op: runs converge cleanly, nothing to evolve"
    return "insufficient data: capture more runs before evolving"
