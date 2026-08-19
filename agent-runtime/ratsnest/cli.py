"""RatsNest CLI.

    python -m ratsnest evaluate <project_dir> [--json]
    python -m ratsnest fix <project_dir> [--max-iter N] [--suggest-only] [--json]
    python -m ratsnest design-plan <requirement> --backend crew --json
    python -m ratsnest design-execute --plan <file> --plan-sha256 <sha> --out <dir>
    python -m ratsnest evolve [--boards N] [--promote]
    python -m ratsnest stats
    python -m ratsnest seed-defects
    python -m ratsnest export-schemas
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from ratsnest.config import REPO_ROOT, Config
from ratsnest.evolution import StrategyRegistry
from ratsnest.schemas import RunConfig


def _print_scorecard(sc, findings) -> None:
    print(f"  score: {sc.score}/100   findings: {sc.findings_total} "
          f"(suppressed {sc.suppressed_total})   severity: {sc.severity_counts}")
    for f in findings:
        if f.severity in ("error", "warning"):
            print(f"    [{f.severity:7s}] {f.rule_id or f.detector}: "
                  f"{(f.model_extra or {}).get('summary', '')[:90]}")


def cmd_evaluate(args) -> int:
    from ratsnest.crews import CheckerCrew
    config = Config.load()
    _, strategy = StrategyRegistry(config.strategies_dir).load_active()
    outputs = CheckerCrew(config, strategy).evaluate(Path(args.project_dir))
    ev = synthesize(outputs, strategy, args.project_dir)
    if args.json:
        print(ev.model_dump_json(indent=2, exclude={"analyzer_outputs"}))
    else:
        print(f"evaluate {args.project_dir}  [strategy {strategy.version_id()}]")
        _print_scorecard(ev.scorecard, ev.findings)
    return 0


def cmd_fix(args) -> int:
    from ratsnest.orchestrator import RunLoop
    rc = RunConfig(project_dir=str(Path(args.project_dir).resolve()),
                   max_iterations=args.max_iter,
                   fix_policy="suggest_only" if args.suggest_only else "auto",
                   run_erc=not args.no_erc)
    record = RunLoop().execute(rc)
    if args.json:
        print(record.model_dump_json(indent=2))
        return 0
    print(f"run {record.run_id}  status={record.status}  "
          f"strategy={record.strategy_version_id}")
    for it in record.iterations:
        ops = len(it.patch_plan.ops) if it.patch_plan else 0
        print(f"  iter {it.iteration}: score={it.scorecard.score}  "
              f"delta={it.score_delta:+.1f}  ops={ops}  "
              f"resolved={len(it.resolved_findings)}")
        if it.patch_plan:
            for fid, why in it.patch_plan.rationale.items():
                print(f"      {fid}: {why}")
    if record.escalation:
        print(f"  escalation: {record.escalation}")
    return 0 if record.status in ("converged", "suggested") else 1


def _create_design_plan(requirement: str, backend: str,
                        run_id: str | None = None):
    """Build one no-KiCad planning result and its trajectory recorder."""
    from ratsnest.data_proxy import Recorder
    from ratsnest.design_workflow import plan_design, serialize_plan
    from ratsnest.orchestrator import RunStore

    config = Config.load()
    strategy_name, strategy = StrategyRegistry(
        config.strategies_dir).load_active()
    run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    recorder = Recorder(
        RunStore(config.runs_dir).run_dir(run_id), run_id,
        config.control_plane_url,
        base_metadata={"strategy_version_id": strategy.version_id(),
                       "backend": backend, "workflow_phase": "plan"})
    plan = plan_design(requirement, backend, strategy_name, strategy, config,
                       recorder=recorder, run_id=run_id)
    return config, plan, serialize_plan(plan)


def _emit_design_plan(plan, payload: str, json_output: bool,
                      plan_out: str | None) -> int:
    from ratsnest.design_workflow import plan_sha256

    digest = plan_sha256(payload)
    if json_output:
        sys.stdout.write(payload)
        return 0
    destination = (Path(plan_out).resolve() if plan_out else
                   REPO_ROOT / "generated" / "plans" /
                   f"{plan.board_plan.plan_id}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")
    print(f"plan: {destination}")
    print(f"plan sha256: {digest}")
    print(f"topology: {plan.board_plan.topology}  "
          f"components={len(plan.board_plan.components)}  "
          f"backend={plan.backend}")
    print("No KiCad project was created. Review the plan, then execute it with:")
    print(f'  python -m ratsnest design-execute --plan "{destination}" '
          f'--plan-sha256 {digest}')
    return 0


def cmd_design_plan(args) -> int:
    _, plan, payload = _create_design_plan(
        args.requirement, args.backend, args.run_id)
    return _emit_design_plan(plan, payload, args.json, args.plan_out)


def _execute_design_payload(plan_json: str, expected_sha256: str,
                            out_arg: str | None, max_iter: int,
                            no_fix: bool, no_erc: bool,
                            json_output: bool, open_project: bool) -> int:
    from ratsnest.agents import synthesize
    from ratsnest.data_proxy import Recorder
    from ratsnest.design_workflow import (
        execute_approved_plan,
        parse_approved_plan,
    )
    from ratsnest.orchestrator import RunLoop, RunStore
    from ratsnest.pipeline import evaluate_for_release, finalize_outputs

    config = Config.load()
    approved = parse_approved_plan(plan_json, expected_sha256)
    plan = approved.plan
    strategy = StrategyRegistry(config.strategies_dir).load_exact(
        plan.strategy_name, plan.strategy_version_id)
    quiet = json_output

    def say(msg: str) -> None:
        if not quiet:
            print(msg)

    out_dir = Path(out_arg) if out_arg else (
        REPO_ROOT / "generated" / plan.design_spec.project_name)
    gen_recorder = Recorder(
        RunStore(config.runs_dir).run_dir(plan.run_id), plan.run_id,
        config.control_plane_url,
        base_metadata={"strategy_version_id": strategy.version_id(),
                       "project": str(out_dir), "backend": plan.backend,
                       "workflow_phase": "execute"},
        initial_step=plan.trajectory_step)
    spec = execute_approved_plan(
        approved, out_dir, strategy, config, recorder=gen_recorder)
    say(f"executed approved {plan.backend} plan: {out_dir}")

    record = None
    if no_fix:
        ev = evaluate_for_release(out_dir, strategy, config)
    else:
        record = RunLoop(config).execute(RunConfig(
            project_dir=str(out_dir), max_iterations=max_iter,
            run_erc=True), recorder=gen_recorder, run_id=plan.run_id)
        ev = evaluate_for_release(out_dir, strategy, config)
        say(f"loop: {record.status} in {len(record.iterations)} iteration(s)")

    outputs = finalize_outputs(out_dir, ev, record, spec, config)

    if quiet and record is not None:
        sys.stdout.write(record.model_dump_json())
        return 0
    say(f"report: {outputs['report']}")
    say(f"kicad project: {out_dir}")
    say(f"release package: {outputs['release']}")
    for k in ("preview_sch", "preview_pcb"):
        if k in outputs:
            say(f"{k}: {outputs[k]}")
    _print_scorecard(ev.scorecard, ev.findings)
    if open_project:
        pros = sorted(Path(out_dir).glob("*.kicad_pro"))
        if pros and hasattr(os, "startfile"):
            say(f"opening in KiCad: {pros[0].name}")
            os.startfile(str(pros[0]))  # noqa: S606 - explicit user request
    return 0 if ev.scorecard.severity_counts.get("error", 0) == 0 else 1


def cmd_design_execute(args) -> int:
    plan_json = Path(args.plan).read_text(encoding="utf-8")
    return _execute_design_payload(
        plan_json, args.plan_sha256, args.out, args.max_iter,
        args.no_fix, args.no_erc, args.json, args.open)


def cmd_design(args) -> int:
    """Human-facing plan command; execution requires explicit auto-approval."""
    _, plan, payload = _create_design_plan(
        args.requirement, args.backend, args.run_id)
    if not args.auto_approve:
        return _emit_design_plan(plan, payload, args.json, args.plan_out)
    from ratsnest.design_workflow import plan_sha256
    return _execute_design_payload(
        payload, plan_sha256(payload), args.out, args.max_iter,
        args.no_fix, args.no_erc, args.json, args.open)


def cmd_evolve(args) -> int:
    from ratsnest.evolution.experiment import run_default_experiment

    candidate = args.candidate
    if candidate == "llm":
        # Evolution Agent: brain proposes a bounded diff from trajectory
        # evidence; the candidate still has to survive the benchmark gates
        from ratsnest.data_proxy import Recorder
        from ratsnest.evolution.proposer import propose_candidate
        from ratsnest.evolution.triggers import compute_stats
        from ratsnest.llm import LlmClient
        from ratsnest.orchestrator import RunStore

        config = Config.load()
        registry = StrategyRegistry(config.strategies_dir)
        _, incumbent = registry.load_active()
        recorder = Recorder(RunStore(config.runs_dir).run_dir("evolution_llm"),
                            "evolution_llm", config.control_plane_url)
        proposal = propose_candidate(
            incumbent, compute_stats(config.runs_dir),
            LlmClient(config, recorder))
        if proposal is None:
            print("evolution agent produced no valid candidate "
                  "(no API key configured, or the diff failed validation)")
            return 1
        name, bundle, rationale = proposal
        registry.save_candidate(bundle, name)
        print(f"evolution agent proposed: {name}")
        print(f"  rationale: {rationale}")
        candidate = name

    report = run_default_experiment(promote=args.promote,
                                    candidate=candidate)
    print(f"experiment {report.experiment_id}: candidate "
          f"'{report.candidate_name}' vs incumbent")
    print(f"  mean score: incumbent={report.mean_incumbent_score:.1f}  "
          f"candidate={report.mean_candidate_score:.1f}")
    for row in report.per_board:
        print(f"  {row['board']}: {row['incumbent_score']:.1f} -> "
              f"{row['candidate_score']:.1f}  new_errors={row['new_errors']}")
    print("  gates:")
    for gate, ok in report.gates.items():
        print(f"    [{'PASS' if ok else 'FAIL'}] {gate}: "
              f"{report.gate_reasons.get(gate, '')}")
    print(f"  promoted: {report.promoted}")
    return 0


def cmd_eda(args) -> int:
    """Web-EDA bridge: emit editable state; apply typed ops when given."""
    from ratsnest.eda import apply_edits, get_state
    ops = []
    if args.ops:
        raw = args.ops
        if Path(raw).exists():
            raw = Path(raw).read_text(encoding="utf-8-sig")  # BOM-tolerant
        ops = json.loads(raw)
        if not isinstance(ops, list):
            raise SystemExit("--ops must be a JSON array of edit ops")
    state = (apply_edits(Path(args.project_dir), ops)
             if ops else get_state(Path(args.project_dir)))
    print(json.dumps(state))
    return 0 if not state.get("errors") else 1


def cmd_stats(args) -> int:
    from ratsnest.evolution.triggers import compute_stats, propose_surface
    stats = compute_stats(Config.load().runs_dir)
    print(json.dumps(stats, indent=2))
    print("proposal:", propose_surface(stats))
    return 0


def cmd_seed(args) -> int:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))
    import seed_defects
    seed_defects.seed()
    return 0


def cmd_export_schemas(args) -> int:
    from ratsnest.schemas.export import export_all
    for p in export_all():
        print(p)
    return 0


def main(argv: list[str] | None = None) -> int:
    # CJK consoles (GBK) choke on characters like mm² in finding summaries
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="ratsnest")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("evaluate", help="analyze + synthesize -> scorecard")
    p.add_argument("project_dir")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("fix", help="run the closed repair loop")
    p.add_argument("project_dir")
    p.add_argument("--max-iter", type=int, default=4)
    p.add_argument("--suggest-only", action="store_true")
    p.add_argument("--no-erc", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fix)

    p = sub.add_parser("design",
                       help="generate a KiCad project from a requirement, "
                            "review it, and write a report")
    p.add_argument("requirement", help='e.g. "12V to 3.3V board with green LED"')
    p.add_argument("--backend", choices=["template", "crew", "mcp"],
                   default="template",
                   help="template: deterministic S-expression writer; "
                        "crew: autonomous LLM design agents with validated "
                        "KiCad tool services hosted in-process (no Node); "
                        "mcp: same skills via the external MCP stdio server")
    p.add_argument("--out", default=None, help="output project directory")
    p.add_argument("--no-fix", action="store_true",
                   help="generate + evaluate only, skip the repair loop")
    p.add_argument("--max-iter", type=int, default=4)
    p.add_argument("--no-erc", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="print PlannedDesign JSON, or RunRecord with --auto-approve")
    p.add_argument("--open", action="store_true",
                   help="open the finished project in KiCad (local use)")
    p.add_argument("--plan-out", default=None,
                   help="write the reviewable PlannedDesign to this file")
    p.add_argument("--run-id", default=None,
                   help="trajectory run id supplied by the control plane")
    p.add_argument("--auto-approve", action="store_true",
                   help="explicit local-only approval and immediate execution")
    p.set_defaults(func=cmd_design)

    p = sub.add_parser(
        "design-plan", help="produce a reviewable plan without touching KiCad")
    p.add_argument("requirement")
    p.add_argument("--backend", choices=["template", "crew", "mcp"],
                   default="crew")
    p.add_argument("--plan-out", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_design_plan)

    p = sub.add_parser(
        "design-execute", help="execute an approved immutable design plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--plan-sha256", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--max-iter", type=int, default=4)
    p.add_argument("--no-erc", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_design_execute)

    p = sub.add_parser("evolve", help="run an AHE experiment (offline)")
    p.add_argument("--promote", action="store_true",
                   help="promote the candidate if all gates pass")
    p.add_argument("--candidate", default=None,
                   help="name of a strategies/<name> dir to evaluate; "
                        "default: auto-generated candidate")
    p.set_defaults(func=cmd_evolve)

    p = sub.add_parser("eda", help="web-EDA bridge: state out, edit ops in")
    p.add_argument("project_dir")
    p.add_argument("--ops", default=None,
                   help="JSON array of edit ops (inline or a file path)")
    p.set_defaults(func=cmd_eda)

    p = sub.add_parser("stats", help="trigger statistics from ATDP trajectories")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("seed-defects", help="regenerate the defective benchmark board")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("export-schemas", help="export JSON Schemas for the control plane")
    p.set_defaults(func=cmd_export_schemas)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
