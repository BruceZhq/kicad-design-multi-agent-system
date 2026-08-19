"""Pre-execution design planning and approved-plan materialization.

This is the product trust boundary. Planning does not instantiate a KiCad
toolbox or create the requested project directory. Execution accepts only the
exact PlannedDesign bytes approved by the control plane.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ratsnest.circuit_math import solve_circuit
from ratsnest.config import Config
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.contracts import PlannedDesign
from ratsnest.crews.design_agents import CircuitArchitect
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen import parse_requirement
from ratsnest.protocols import LlmBrain
from ratsnest.schemas import DesignSpec, StrategyBundle


class PlanIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovedPlan:
    plan: PlannedDesign
    raw_json: str
    subject_sha256: str


def serialize_plan(plan: PlannedDesign) -> str:
    return plan.model_dump_json()


def plan_sha256(plan_json: str) -> str:
    return hashlib.sha256(plan_json.encode("utf-8")).hexdigest()


def parse_approved_plan(plan_json: str, expected_sha256: str) -> ApprovedPlan:
    actual = plan_sha256(plan_json)
    if not expected_sha256 or actual != expected_sha256.lower():
        raise PlanIntegrityError(
            f"approved plan hash mismatch: expected {expected_sha256}, got {actual}")
    try:
        plan = PlannedDesign.model_validate_json(plan_json)
    except Exception as exc:
        raise PlanIntegrityError(f"invalid PlannedDesign: {exc}") from exc
    return ApprovedPlan(plan=plan, raw_json=plan_json, subject_sha256=actual)


def plan_design(requirement: str, backend: str, strategy_name: str,
                strategy: StrategyBundle, config: Config,
                recorder: Recorder | None = None,
                llm: LlmBrain | None = None,
                run_id: str | None = None) -> PlannedDesign:
    """Produce a typed design plan without creating or mutating KiCad files."""
    if backend not in {"template", "crew", "mcp"}:
        raise ValueError("backend must be one of template, crew, mcp")
    if llm is None:
        from ratsnest.llm import LlmClient
        llm = LlmClient(config, recorder)

    spec: DesignSpec | None = None
    if llm is not None:
        from ratsnest.design_gen.requirement_agent import parse_requirement_llm
        spec = parse_requirement_llm(requirement, llm)
    brain = "llm" if spec is not None else "deterministic"
    if spec is None:
        spec = parse_requirement(requirement)
    if recorder is not None:
        recorder.emit(
            "requirement_agent", 0,
            observation={"requirement": requirement[:300]},
            agent_state={"brain": brain},
            action={"spec": spec.model_dump(mode="json")},
            outcome={"ok": True},
            metadata={"agent": "requirement_agent", "crew": "creator"})

    solved = solve_circuit(spec, strategy, config)
    blackboard = DesignBlackboard("<unmaterialized>", recorder)
    architect = CircuitArchitect(
        strategy, blackboard, llm=llm, recorder=recorder)
    board_plan = architect.create_plan(spec, solved)
    planned_run_id = run_id or (
        recorder.run_id if recorder is not None else "run_untracked")
    return PlannedDesign(
        run_id=planned_run_id,
        requirement=requirement,
        backend=backend,
        design_spec=spec,
        board_plan=board_plan,
        strategy_name=strategy_name,
        strategy_version_id=strategy.version_id(),
        trajectory_step=recorder.step if recorder is not None else 0,
    )


def validate_plan_against_strategy(plan: PlannedDesign,
                                   strategy: StrategyBundle,
                                   config: Config) -> None:
    if strategy.version_id() != plan.strategy_version_id:
        raise PlanIntegrityError("execution strategy differs from approved plan")
    solved = solve_circuit(plan.design_spec, strategy, config)
    blackboard = DesignBlackboard("<validation>")
    architect = CircuitArchitect(strategy, blackboard)
    canonical = architect.canonical_plan(plan.design_spec, solved)
    try:
        architect.validate_candidate(plan.board_plan, canonical)
    except ValueError as exc:
        raise PlanIntegrityError(
            f"approved BoardPlan no longer validates: {exc}") from exc


def execute_approved_plan(approved: ApprovedPlan, out_dir: Path,
                          strategy: StrategyBundle, config: Config,
                          recorder: Recorder | None = None,
                          llm: LlmBrain | None = None) -> DesignSpec:
    """Execute a validated plan through the selected trusted backend."""
    plan = approved.plan
    validate_plan_against_strategy(plan, strategy, config)
    out_dir = Path(out_dir).resolve()
    if llm is None:
        from ratsnest.llm import LlmClient
        llm = LlmClient(config, recorder)

    if plan.backend == "crew":
        from ratsnest.crews import CreatorCrew
        CreatorCrew(config, recorder, llm=llm).generate_approved(
            plan.design_spec, out_dir, strategy, plan.board_plan)
    else:
        from ratsnest.pipeline import BACKEND_FACTORIES
        BACKEND_FACTORIES[plan.backend](config, recorder, llm).generate(
            plan.design_spec, out_dir, strategy)
        (out_dir / "boardplan.json").write_text(
            plan.board_plan.model_dump_json(indent=2), encoding="utf-8")

    from ratsnest.manufacturing import write_manufacturing_outputs
    write_manufacturing_outputs(out_dir, plan.board_plan, plan.design_spec)

    (out_dir / "approved_plan.json").write_text(
        approved.raw_json, encoding="utf-8")
    return plan.design_spec
