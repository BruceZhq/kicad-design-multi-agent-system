"""AHE v1 tests: registry lifecycle, gates reject bad candidates, promote good ones."""

import json

import pytest

from ratsnest.config import Config
from ratsnest.evolution import StrategyRegistry
from ratsnest.evolution.experiment import run_experiment
from ratsnest.evolution.variants import expanded_vref, no_divider_repair
from ratsnest.orchestrator import RunStore
from ratsnest.schemas import StrategyBundle


@pytest.fixture()
def exp_config(tmp_path):
    config = Config.load()
    config.runs_dir = tmp_path / "runs"
    return config


def _incumbent() -> StrategyBundle:
    return StrategyRegistry().load_active()[1]


def test_registry_promote_and_rollback(tmp_path):
    reg = StrategyRegistry(tmp_path)
    v0 = StrategyBundle(name="v0")
    v1 = StrategyBundle(name="v1", scorecard_weights={"error": 25.0})
    reg.save_candidate(v0, "v0")
    reg.promote("v0")
    reg.save_candidate(v1, "v1")
    reg.promote("v1")
    assert reg.active_name() == "v1"
    assert reg.rollback() == "v0"
    assert reg.load_active()[1].version_id() == v0.version_id()
    with pytest.raises(RuntimeError):
        reg.rollback()  # history exhausted


def test_good_candidate_passes_gates(exp_config):
    incumbent = _incumbent()
    candidate = expanded_vref(incumbent)
    report = run_experiment(candidate, incumbent, boards=["lm1117_divider"],
                            config=exp_config,
                            store=RunStore(exp_config.runs_dir))
    row = report.per_board[0]
    # incumbent can't see the LM1117 divider defect (vref gap) -> ground-truth
    # penalty; candidate repairs it
    assert row["incumbent_unrepaired"], "expected incumbent to miss the defect"
    assert not row["candidate_unrepaired"]
    assert report.mean_candidate_score > report.mean_incumbent_score
    assert all(report.gates.values()), report.gate_reasons


def test_bad_candidate_rejected_by_gates(exp_config):
    incumbent = _incumbent()
    candidate = no_divider_repair(incumbent)
    report = run_experiment(candidate, incumbent, boards=["divider_led_mpn"],
                            config=exp_config,
                            store=RunStore(exp_config.runs_dir))
    assert not all(report.gates.values()), "gates must reject the bad candidate"
    assert not report.gates["replay_no_regression"] or \
           not report.gates["mean_improvement"]


def test_trigger_stats_from_trajectories(exp_config):
    from ratsnest.evolution.triggers import compute_stats, propose_surface
    incumbent = _incumbent()
    # produce trajectories: incumbent on the board it cannot fully fix
    run_experiment(incumbent.model_copy(deep=True), incumbent,
                   boards=["lm1117_divider"], config=exp_config,
                   store=RunStore(exp_config.runs_dir))
    stats = compute_stats(exp_config.runs_dir)
    assert stats["runs"] >= 2
    proposal = propose_surface(stats)
    assert proposal  # always produces a concrete proposal string


def test_agent_failures_select_bounded_policy_surface(exp_config):
    from ratsnest.evolution.triggers import compute_stats, propose_surface

    trajectory_dir = exp_config.runs_dir / "run_agent_failure"
    trajectory_dir.mkdir(parents=True)
    events = [
        {"node": "design.pcb_designer.plan", "outcome": {"validated": True}},
        {"node": "design.pcb_designer.tool", "outcome": {"ok": False}},
        {"node": "blackboard.message", "action": {
            "sender": "pcb_designer", "kind": "status",
            "payload": {"status": "blocked"}}},
        {"node": "finish", "outcome": {"status": "escalated"},
         "reward": -3},
    ]
    (trajectory_dir / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8")

    stats = compute_stats(exp_config.runs_dir)

    assert stats["agent_plan_calls"] == {"pcb_designer": 1}
    assert stats["agent_tool_failures"] == {"pcb_designer": 1}
    assert stats["blocked_agent_tasks"] == {"pcb_designer": 1}
    proposal = propose_surface(stats)
    assert proposal.startswith("agent-policy surface: pcb_designer")
