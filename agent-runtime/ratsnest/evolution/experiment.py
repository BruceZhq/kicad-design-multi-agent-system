"""AHE experiment runner: candidate vs incumbent on the seeded benchmark.

Fair comparison rule: both arms run their own strategy inside the loop, but
the FINAL board state of each arm is evaluated under the *incumbent's*
weights and suppressions (the reference metric), plus a ground-truth penalty
of 10 points per unrepaired seeded defect. A candidate cannot game the
benchmark by inflating its own scorecard weights.

Promotion gates (design doc §4.5 — all required):
  replay_no_regression   candidate >= incumbent on every board
  mean_improvement       candidate mean > incumbent mean
  no_new_criticals       candidate never ends with more error findings
Human approval stays explicit: promotion only happens with promote=True.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ratsnest.agents import synthesize
from ratsnest.config import Config
from ratsnest.evolution import StrategyRegistry
from ratsnest.evolution.benchmark import VARIANTS, materialize, unrepaired_defects
from ratsnest.evolution.variants import GENERATORS
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.orchestrator import RunLoop, RunStore
from ratsnest.schemas import ExperimentReport, RunConfig, StrategyBundle

GROUND_TRUTH_PENALTY = 10.0


def _run_arm(strategy: StrategyBundle, reference: StrategyBundle,
             variant: str, workdir: Path, config: Config,
             store: RunStore) -> dict:
    project = materialize(variant, workdir)
    loop = RunLoop(config, store=store)
    record = loop.execute(
        RunConfig(project_dir=str(project), max_iterations=3, run_erc=False),
        strategy_override=strategy)
    adapter = KicadHappyAdapter(config)
    final_eval = synthesize(adapter.analyze_all(project), reference, project)
    missed = unrepaired_defects(variant, project, config)
    return {
        "run_id": record.run_id,
        "status": record.status,
        "ref_score": final_eval.scorecard.score,
        "errors": final_eval.scorecard.severity_counts.get("error", 0),
        "unrepaired": missed,
        "benchmark_score": final_eval.scorecard.score
                           - GROUND_TRUTH_PENALTY * len(missed),
    }


def run_experiment(candidate: StrategyBundle,
                   incumbent: StrategyBundle,
                   boards: list[str] | None = None,
                   config: Config | None = None,
                   store: RunStore | None = None) -> ExperimentReport:
    config = config or Config.load()
    store = store or RunStore(config.runs_dir)
    boards = boards or list(VARIANTS)

    report = ExperimentReport(
        candidate_version_id=candidate.version_id(),
        incumbent_version_id=incumbent.version_id(),
        candidate_name=candidate.name,
    )
    with tempfile.TemporaryDirectory(prefix="ratsnest_exp_") as td:
        td_path = Path(td)
        for variant in boards:
            inc = _run_arm(incumbent, incumbent, variant,
                           td_path / "inc", config, store)
            cand = _run_arm(candidate, incumbent, variant,
                            td_path / "cand", config, store)
            report.per_board.append({
                "board": variant,
                "incumbent_score": inc["benchmark_score"],
                "candidate_score": cand["benchmark_score"],
                "incumbent_unrepaired": inc["unrepaired"],
                "candidate_unrepaired": cand["unrepaired"],
                "new_errors": max(0, cand["errors"] - inc["errors"]),
                "candidate_status": cand["status"],
            })

    n = len(report.per_board)
    report.mean_incumbent_score = sum(r["incumbent_score"] for r in report.per_board) / n
    report.mean_candidate_score = sum(r["candidate_score"] for r in report.per_board) / n

    regressions = [r["board"] for r in report.per_board
                   if r["candidate_score"] < r["incumbent_score"]]
    new_crit = [r["board"] for r in report.per_board if r["new_errors"] > 0]

    report.gates = {
        "replay_no_regression": not regressions,
        "mean_improvement": report.mean_candidate_score > report.mean_incumbent_score,
        "no_new_criticals": not new_crit,
    }
    report.gate_reasons = {
        "replay_no_regression": (f"regressed on {', '.join(regressions)}"
                                 if regressions else "no per-board regression"),
        "mean_improvement": (f"{report.mean_incumbent_score:.1f} -> "
                             f"{report.mean_candidate_score:.1f}"),
        "no_new_criticals": (f"new errors on {', '.join(new_crit)}"
                             if new_crit else "no new error findings"),
    }
    return report


def run_default_experiment(promote: bool = False,
                           candidate: str | None = None,
                           config: Config | None = None) -> ExperimentReport:
    """CLI entry: evaluate a candidate (named registry dir, generator name,
    or the default generated one) against the active strategy."""
    config = config or Config.load()
    registry = StrategyRegistry(config.strategies_dir)
    _, incumbent = registry.load_active()

    if candidate and candidate in GENERATORS:
        bundle = GENERATORS[candidate](incumbent)
    elif candidate:
        bundle = registry.load(candidate)
    else:
        bundle = GENERATORS["expanded-vref"](incumbent)

    report = run_experiment(bundle, incumbent, config=config)
    report.promoted = False
    if promote and all(report.gates.values()):
        registry.save_candidate(bundle, bundle.name)
        registry.promote(bundle.name)
        report.promoted = True

    # persist the experiment report next to run records
    store = RunStore(config.runs_dir)
    exp_dir = store.runs_dir / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / f"{report.experiment_id}.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8")
    return report
