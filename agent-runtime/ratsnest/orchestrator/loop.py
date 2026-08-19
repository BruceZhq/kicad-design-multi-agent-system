"""The closed loop: analyze -> synthesize -> plan -> apply -> verify -> converge.

Safety invariants (design doc §4.4):
- score-monotonic acceptance: an applied patch that lowers the score or
  introduces a NEW error-severity finding is reverted (new-critical veto)
- iteration budget; on exhaustion with open errors the run escalates
- every node emits an ATDP TrajectoryEvent; every run is stamped with the
  strategy version id
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.agents import plan_repairs, synthesize
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_edit import Patcher
from ratsnest.design_edit.kicad_cli import run_erc
from ratsnest.evolution import StrategyRegistry
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.kh_adapter.runner import find_root_schematic
from ratsnest.orchestrator.run_store import RunStore
from ratsnest.schemas import (
    EvaluationResult,
    IterationRecord,
    RunConfig,
    RunRecord,
    StrategyBundle,
)


def _error_ids(ev: EvaluationResult) -> set[str]:
    return {f.finding_id() for f in ev.findings if f.severity == "error"}


def _finding_ids(ev: EvaluationResult) -> set[str]:
    return {f.finding_id() for f in ev.findings}


class RunLoop:
    def __init__(self, config: Config | None = None,
                 registry: StrategyRegistry | None = None,
                 store: RunStore | None = None):
        self.config = config or Config.load()
        self.adapter = KicadHappyAdapter(self.config)
        self.patcher = Patcher(self.config, self.adapter)
        self.registry = registry or StrategyRegistry(self.config.strategies_dir)
        self.store = store or RunStore(self.config.runs_dir)

    # ------------------------------------------------------------------
    def _evaluate(self, project_dir: Path, strategy: StrategyBundle,
                  run_erc_flag: bool, recorder=None,
                  iteration: int = 0) -> EvaluationResult:
        # kicad-happy disassembled: each analyzer runs as a checker-crew
        # agent with its own ATDP identity (in-process, code stays vendored)
        from ratsnest.crews import CheckerCrew
        crew = CheckerCrew(self.config, strategy, recorder=recorder,
                           iteration=iteration)
        outputs = crew.evaluate(project_dir)
        gates = {}
        gate_findings = []
        if run_erc_flag:
            from ratsnest.verification import verify_production
            gates, gate_findings = verify_production(project_dir, self.config)
        if gates:
            erc_gate = gates.get("erc")
            erc = erc_gate.passed if erc_gate is not None else None
        else:
            erc = run_erc(project_dir, self.config) if run_erc_flag else None
        return synthesize(
            outputs, strategy, project_dir, erc_passed=erc,
            gate_results=gates, additional_findings=gate_findings)

    def execute(self, run_config: RunConfig,
                strategy_override: StrategyBundle | None = None,
                recorder: Recorder | None = None,
                run_id: str | None = None) -> RunRecord:
        project_dir = Path(run_config.project_dir)
        if strategy_override is not None:
            strategy = strategy_override
        elif run_config.strategy_version_id:
            strategy = self.registry.load(run_config.strategy_version_id)
        else:
            _, strategy = self.registry.load_active()

        resolved_run_id = run_id or (recorder.run_id if recorder else None)
        if recorder is not None and resolved_run_id != recorder.run_id:
            raise ValueError("recorder run_id does not match the requested run_id")
        record_kwargs = {
            "config": run_config,
            "strategy_version_id": strategy.version_id(),
            "status": "running",
        }
        if resolved_run_id is not None:
            record_kwargs["run_id"] = resolved_run_id
        record = RunRecord(**record_kwargs)
        meta = {"strategy_version_id": strategy.version_id(),
                "project": str(project_dir)}
        if recorder is None:
            rec = Recorder(self.store.run_dir(record.run_id), record.run_id,
                           self.config.control_plane_url, base_metadata=meta)
        else:
            rec = recorder
            rec.base_metadata = {**rec.base_metadata, **meta}
        from ratsnest.llm import LlmClient
        llm = LlmClient(self.config, rec)
        sch_path = find_root_schematic(project_dir)

        ev = self._evaluate(project_dir, strategy, run_config.run_erc,
                            recorder=rec, iteration=0)
        initial_score = ev.scorecard.score
        rec.emit("evaluate", 0,
                 observation={"project": str(project_dir)},
                 outcome={"score": ev.scorecard.score,
                          "severity_counts": ev.scorecard.severity_counts,
                          "required_gates_passed":
                              ev.scorecard.required_gates_passed,
                          "gates": {name: gate.status.value for name, gate in
                                    ev.scorecard.gate_results.items()}},
                 metadata=meta)

        for iteration in range(1, run_config.max_iterations + 1):
            prev_score = ev.scorecard.score
            prev_errors = _error_ids(ev)
            prev_ids = _finding_ids(ev)

            llm.iteration = iteration
            plan, hints, escalations = plan_repairs(
                ev, strategy, run_id=record.run_id, iteration=iteration,
                config=self.config, llm=llm)
            plan_evt = rec.emit(
                "plan_repairs", iteration,
                observation={"actionable": len([f for f in ev.findings
                                                if f.severity in ("error", "warning")]),
                             "score": prev_score},
                agent_state={"hints": [h.model_dump(mode="json") for h in hints]},
                action={"ops": [op.model_dump(mode="json") for op in plan.ops],
                        "escalated_rule_ids": [f.rule_id for f in escalations]},
                metadata=meta)

            if not plan.ops:
                record.status = (
                    "converged"
                    if not prev_errors and (
                        not ev.scorecard.gate_results
                        or ev.scorecard.required_gates_passed)
                    else "escalated")
                if escalations:
                    record.escalation = {
                        "unresolved": [f.finding_id() for f in escalations],
                        "reason": "no repair mapping produced ops",
                    }
                record.iterations.append(IterationRecord(
                    iteration=iteration, scorecard=ev.scorecard,
                    patch_plan=plan))
                break

            if run_config.fix_policy == "suggest_only":
                record.status = "suggested"
                record.iterations.append(IterationRecord(
                    iteration=iteration, scorecard=ev.scorecard,
                    patch_plan=plan))
                break

            original_text = sch_path.read_text(encoding="utf-8")
            result = self.patcher.apply(plan, project_dir)
            rec.emit("apply_patches", iteration,
                     action={"plan_id": plan.plan_id, "ops": len(plan.ops)},
                     outcome={"applied": result.applied, "error": result.error,
                              "rolled_back": result.rolled_back},
                     metadata=meta)
            if not result.applied:
                record.status = "escalated"
                record.escalation = {"reason": f"patch failed: {result.error}"}
                record.iterations.append(IterationRecord(
                    iteration=iteration, scorecard=ev.scorecard,
                    patch_plan=plan, patch_result=result))
                break

            new_ev = self._evaluate(project_dir, strategy, run_config.run_erc,
                                    recorder=rec, iteration=iteration)
            new_errors = _error_ids(new_ev) - prev_errors
            score_delta = new_ev.scorecard.score - prev_score
            vetoed = bool(new_errors) or score_delta < 0
            if vetoed:  # score-monotonic acceptance + new-critical veto
                sch_path.write_text(original_text, encoding="utf-8")
                result.rolled_back = True
            rec.emit("verify", iteration,
                     observation={"prev_score": prev_score},
                     outcome={"new_score": new_ev.scorecard.score,
                              "score_delta": round(score_delta, 2),
                              "new_error_findings": sorted(new_errors),
                              "vetoed": vetoed},
                     reward=round(score_delta, 2),
                     metadata=meta)

            if vetoed:
                record.status = "escalated"
                record.escalation = {
                    "reason": "patch vetoed (new errors or score regression)",
                    "new_errors": sorted(new_errors),
                    "score_delta": score_delta,
                }
                record.iterations.append(IterationRecord(
                    iteration=iteration, scorecard=ev.scorecard,
                    patch_plan=plan, patch_result=result,
                    new_error_findings=sorted(new_errors),
                    score_delta=score_delta))
                break

            record.iterations.append(IterationRecord(
                iteration=iteration, scorecard=new_ev.scorecard,
                patch_plan=plan, patch_result=result,
                resolved_findings=sorted(prev_ids - _finding_ids(new_ev)),
                score_delta=round(score_delta, 2)))
            ev = new_ev

            if (not any(f.severity in ("error", "warning") for f in ev.findings)
                    and (not ev.scorecard.gate_results
                         or ev.scorecard.required_gates_passed)):
                record.status = "converged"
                break
        else:
            record.status = (
                "converged"
                if not _error_ids(ev) and (
                    not ev.scorecard.gate_results
                    or ev.scorecard.required_gates_passed)
                else "escalated")
            if record.status == "escalated":
                record.escalation = {"reason": "iteration budget exhausted",
                                     "open_errors": sorted(_error_ids(ev))}

        from ratsnest.schemas.models import _now_iso
        record.finished_at = _now_iso()
        rec.emit("finish", len(record.iterations),
                 outcome={"status": record.status,
                          "final_score": ev.scorecard.score,
                          "initial_score": initial_score},
                 reward=round(ev.scorecard.score - initial_score, 2),
                 metadata=meta)
        self.store.save(record)
        return record
