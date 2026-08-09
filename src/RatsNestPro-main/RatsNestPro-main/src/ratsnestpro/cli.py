"""RatsNestPro command-line interface.

    ratsnestpro design-plan "<requirement>" --out runs/demo [--llm auto]
    ratsnestpro design      "<requirement>" --out runs/demo [--llm required] [--no-erc]

The Architect normalizes the requirement, judges the family, and selects
in-family parameters. In ``--llm offline`` (default) this is deterministic; in
``auto``/``required`` it uses EricAI (falling back / failing closed per mode).
Explicit --crystal/--ldo/... flags always override the Architect's choice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from ratsnestpro.agents import Architect, LlmError, parse_mode
from ratsnestpro.agents.heuristics import params_from_requirement  # re-exported
from ratsnestpro.families import Atmega328Params
from ratsnestpro.orchestration import generate_design
from ratsnestpro.orchestration.generate import build_design_plan

__all__ = ["main", "params_from_requirement"]


def _overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    out: dict[str, object] = {}
    if args.crystal is not None:
        out["crystal_mhz"] = args.crystal
    if args.ldo is not None:
        out["ldo_output_v"] = args.ldo
    if args.decoupling is not None:
        out["decoupling_count"] = args.decoupling
    if args.led is not None:
        out["power_led"] = args.led
    if args.rows is not None:
        out["breakout_rows"] = args.rows
    if args.pins is not None:
        out["breakout_pins_per_row"] = args.pins
    if args.holes is not None:
        out["mounting_holes"] = args.holes
    return out


def _resolve_params(args: argparse.Namespace) -> tuple[Atmega328Params | None, int]:
    """Return (params, exit_code). params is None when the CLI should stop."""
    mode = parse_mode(args.llm)
    architect = Architect()
    try:
        result = architect.plan(args.requirement, mode)
    except LlmError as exc:
        print(f"LLM required but unavailable: {exc}", file=sys.stderr)
        return None, 2

    if not result.decision.qualified:
        print("Request is not in the qualified ATmega328 family.", file=sys.stderr)
        for q in result.decision.clarifying_questions:
            print(f"  ? {q}", file=sys.stderr)
        return None, 3

    overrides = _overrides_from_args(args)
    if result.params is None and not overrides:
        # Qualified but parameters couldn't be settled (e.g. contradictory
        # request) and nothing on the command line resolves it.
        print("Could not settle parameters — please clarify:", file=sys.stderr)
        for q in result.decision.clarifying_questions:
            print(f"  ? {q}", file=sys.stderr)
        return None, 3
    base = result.params.model_dump() if result.params else {}
    merged = {**base, **overrides}
    try:
        params = Atmega328Params(**merged)  # type: ignore[arg-type]
    except ValidationError as exc:
        # Contradictory or ambiguous: surface as clarifying questions.
        print("Could not settle parameters — please clarify:", file=sys.stderr)
        for err in exc.errors():
            print(f"  - {err['msg']}", file=sys.stderr)
        for q in result.decision.clarifying_questions:
            print(f"  ? {q}", file=sys.stderr)
        return None, 2
    if args.llm != "offline" and result.source == "ericai":
        print(f"[architect via EricAI] {result.decision.rationale}")
    return params, 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("requirement", help="natural-language requirement (in quotes)")
    p.add_argument("--out", default="runs/design", help="output run directory")
    p.add_argument(
        "--llm", default="offline", help="LLM mode: offline | auto | required (default offline)"
    )
    p.add_argument("--crystal", type=int, choices=[8, 16], help="crystal frequency MHz")
    p.add_argument("--ldo", type=float, choices=[3.3, 5.0], help="LDO output voltage")
    p.add_argument("--decoupling", type=int, help="number of decoupling caps (4-8)")
    led = p.add_mutually_exclusive_group()
    led.add_argument("--led", dest="led", action="store_true", default=None, help="add power LED")
    led.add_argument("--no-led", dest="led", action="store_false", help="omit power LED")
    p.add_argument("--rows", type=int, choices=[1, 2], help="breakout header rows")
    p.add_argument("--pins", type=int, help="pins per breakout header (4-12)")
    p.add_argument("--holes", type=int, choices=[0, 4], help="mounting holes")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ratsnestpro", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("design-plan", help="build the immutable DesignPlan (plan.json)")
    _add_common(plan)
    design = sub.add_parser("design", help="generate + verify the schematic")
    _add_common(design)
    design.add_argument("--no-erc", action="store_true", help="skip kicad-cli ERC")
    design.add_argument("--repair", action="store_true", help="run the repair loop if blocked")
    design.add_argument("--max-iter", type=int, default=5, help="max repair iterations")
    design.add_argument(
        "--auto", action="store_true",
        help="auto repair without per-step confirmation (default: semi-auto)",
    )

    review = sub.add_parser("review", help="review an existing KiCad project")
    review.add_argument("project", help="path to a KiCad project dir or .kicad_sch/.kicad_pcb")
    review.add_argument("--llm", default="offline", help="LLM mode: offline | auto | required")
    review.add_argument("--out", default=None, help="write the review markdown to this file")

    parts = sub.add_parser("parts", help="grounded part search over the local JLCPCB cache")
    parts.add_argument("query", help="search text, e.g. '10k 0603'")
    parts.add_argument("--limit", type=int, default=10, help="max results")

    pcb = sub.add_parser("pcb", help="run the full knowledge-driven PCB pipeline")
    pcb.add_argument("requirement", help="natural-language requirement (in quotes)")
    pcb.add_argument("--out", default="runs/pcb", help="output run directory")
    pcb.add_argument("--llm", default="offline", help="LLM mode: offline | auto | required")
    pcb.add_argument("--project", default="board", help="project name for output files")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "review":
        from ratsnestpro.orchestration import review_project
        from ratsnestpro.orchestration.review_project import ReviewProjectError

        try:
            pr = review_project(args.project, mode=parse_mode(args.llm))
        except ReviewProjectError as exc:
            print(f"Review failed: {exc}", file=sys.stderr)
            return 2
        print(pr.markdown)
        if args.out:
            Path(args.out).write_text(pr.markdown, encoding="utf-8")
            print(f"\nWrote review: {args.out}")
        return 0

    if args.command == "parts":
        from ratsnestpro.parts import PartSelector

        selector = PartSelector()
        if not selector.available():
            print(
                "No local JLCPCB cache found. Set KICAD_MCP_HOME to a directory "
                "containing jlcpcb.sqlite, or populate the cache first.",
                file=sys.stderr,
            )
            return 2
        hits = selector.search(args.query, limit=args.limit)
        if not hits:
            print("No matching parts.")
            return 0
        for c in hits:
            tier = "Basic" if c.basic else "Extended"
            print(f"{c.lcsc:>10}  {c.mpn:<20} {c.package:<8} {tier:<8} "
                  f"stock={c.stock} ${c.price:.4f}  {c.description[:50]}")
        return 0

    if args.command == "pcb":
        from ratsnestpro.agents.llm import resolve_client
        from ratsnestpro.orchestration.pipeline import (
            Pipeline,
            PipelineContext,
            PipelineState,
        )

        mode = parse_mode(args.llm)
        try:
            client = resolve_client(mode, None)
        except LlmError as exc:
            print(f"LLM required but unavailable: {exc}", file=sys.stderr)
            return 2
        state = PipelineState(requirement_text=args.requirement, project_name=args.project)
        ctx = PipelineContext(mode=mode, client=client, out_dir=args.out, repair_attempts=2)
        try:
            Pipeline().run(state, ctx)
        except LlmError as exc:
            print(f"LLM required but unavailable: {exc}", file=sys.stderr)
            return 2
        for r in state.results:
            flag = "OK " if not r.blocked else "BLK"
            llm = " [llm]" if r.used_llm else ""
            print(f"  [{flag}] {r.step.value:<22} {r.summary}{llm}")
            for chk in r.error_checks:
                print(f"          x {chk.name}: {chk.message}")
        completed = [s.value for s in state.completed]
        print(f"\ncompleted {len(completed)}/{len(state.results)} steps; "
              f"out={args.out}")
        return 1 if state.blocked else 0

    params, code = _resolve_params(args)
    if params is None:
        return code

    if args.command == "design-plan":
        plan = build_design_plan(args.requirement, params)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        plan_path = out / "plan.json"
        plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        print(f"Wrote plan: {plan_path}")
        print(f"  family={plan.circuit.family} components={len(plan.circuit.components)} "
              f"nets={len(plan.circuit.nets)}")
        print(f"  params={params.model_dump()}")
        return 0

    result = generate_design(
        args.requirement, params=params, out_dir=args.out, run_erc=not args.no_erc
    )
    print(result.summary)
    print(f"  schematic: {result.schematic_path}")
    print(f"  plan:      {result.plan_path}")
    print(f"  report:    {result.report_path}")
    for gate in result.report.gates:
        print(f"  [{gate.status.value:>11}] {gate.gate}")

    if result.blocked and getattr(args, "repair", False):
        from ratsnestpro.families import expectations_for
        from ratsnestpro.orchestration import run_repair

        target = expectations_for(params)

        def _semi_auto(i, decision, next_params):  # pragma: no cover - interactive
            print(f"  repair step {i}: {decision.diagnosis}")
            if args.auto:
                return True
            ans = input("    apply this repair? [y/N] ").strip().lower()
            return ans in ("y", "yes")

        rep = run_repair(
            params, target, max_iter=args.max_iter,
            mode=parse_mode(args.llm), on_step=_semi_auto,
        )
        print(f"  repair: {'SUCCESS' if rep.success else 'FAILED'} "
              f"({rep.iterations} iter) — {rep.reason}")
        if rep.success:
            regen = generate_design(
                args.requirement, params=rep.params, out_dir=args.out, run_erc=not args.no_erc
            )
            print(regen.summary)
            return 0 if not regen.blocked else 1
        return 1

    return 1 if result.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
