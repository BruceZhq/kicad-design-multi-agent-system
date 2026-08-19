"""Autonomous creator crew: agents plan; KiCad tool services execute.

The crew is a typed collaboration workflow rather than a fixed sequence of
low-level KiCad calls:

  CircuitArchitect  DesignSpec -> BoardPlan
  SchematicDesigner Plan/Act/Observe until the schematic satisfies BoardPlan
  VerificationCrew  publish verified findings to the blackboard
  RepairAgent       assign findings to the owning design agent/repair loop
  PcbDesigner       Plan/Act/Observe for board placement and routing

All file writes still pass through deterministic, capability-scoped services.
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.circuit_math import GenerationError, solve_circuit
from ratsnest.config import Config
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.contracts import AgentTask, BoardPlan, TaskStatus
from ratsnest.crews.design_agents import (
    CircuitArchitect,
    PcbDesigner,
    RepairAgent,
    SchematicDesigner,
    VerificationCrew,
)
from ratsnest.crews.design_tools import KiCadDesignToolbox
from ratsnest.data_proxy import Recorder
from ratsnest.protocols import LlmBrain
from ratsnest.schemas import DesignSpec, StrategyBundle


class CreatorCrew:
    """DesignBackend implemented by collaborating autonomous agents."""

    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None, iteration: int = 0,
                 llm: LlmBrain | None = None):
        self.config = config or Config.load()
        self.recorder = recorder
        self.iteration = iteration
        self.llm = llm
        self._step_n = 0
        self.blackboard: DesignBlackboard | None = None

    def _snapshot(self, out_dir: Path, label: str) -> None:
        if self.recorder is None:
            return
        from ratsnest.preview import snapshot_schematic
        self._step_n += 1
        safe_label = "".join(
            character if character.isalnum() or character in "_-" else "_"
            for character in label)[:44]
        tag = f"step_{self._step_n:02d}_{safe_label}"
        path = snapshot_schematic(out_dir, tag, self.config)
        self.recorder.emit(
            "creator.step", 0,
            action={"step": self._step_n, "label": label.replace("_", " ")},
            outcome={"ok": True,
                     "preview": (f"preview/steps/{path.name}"
                                 if path else None)},
            metadata={"agent": "timeline", "crew": "design"})

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        solved = solve_circuit(spec, strategy, self.config)

        blackboard = DesignBlackboard(str(out_dir), self.recorder)
        self.blackboard = blackboard
        architect = CircuitArchitect(
            strategy, blackboard, llm=self.llm, recorder=self.recorder)
        board_plan = architect.create_plan(spec, solved)

        return self.generate_approved(spec, out_dir, strategy, board_plan)

    def generate_approved(self, spec: DesignSpec, out_dir: Path,
                          strategy: StrategyBundle,
                          board_plan: BoardPlan) -> Path:
        """Materialize an already-approved BoardPlan, resuming if possible."""
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = out_dir / "design_state.json"
        blackboard = DesignBlackboard.resume(
            str(out_dir), self.recorder, checkpoint)
        existing = blackboard.state.board_plan
        if existing is not None and existing.model_dump(mode="json") != (
                board_plan.model_dump(mode="json")):
            raise GenerationError(
                "checkpoint BoardPlan differs from the approved plan")
        if Path(blackboard.state.project_dir).resolve() != out_dir:
            raise GenerationError("checkpoint belongs to another project directory")
        blackboard.state.board_plan = board_plan
        blackboard.checkpoint()
        self.blackboard = blackboard

        toolbox = KiCadDesignToolbox(
            self.config, board_plan, strategy, out_dir, blackboard,
            snapshot=lambda label: self._snapshot(out_dir, label))
        schematic = SchematicDesigner(
            strategy, blackboard, toolbox, self.llm, self.recorder)
        pcb = PcbDesigner(
            strategy, blackboard, toolbox, self.llm, self.recorder)
        verifier = VerificationCrew(
            self.config, strategy, blackboard, self.recorder)
        repair = RepairAgent(
            strategy, blackboard, self.llm, self.recorder)

        schematic_task = blackboard.assign(AgentTask(
            assignee=schematic.name, goal=schematic.default_goal(),
            acceptance_criteria=schematic.acceptance_criteria()),
            sender=CircuitArchitect.name)
        if not schematic.run(schematic_task):
            toolbox.close()
            self._persist_state(out_dir, spec, board_plan, blackboard)
            raise GenerationError(
                "schematic designer exhausted its action budget before "
                "satisfying the BoardPlan")

        schematic_eval = verifier.verify(out_dir, "schematic")
        schematic_repairs = repair.assign(schematic_eval)
        self._retry_owned_tasks(schematic_repairs, schematic, pcb, blackboard)

        pcb_task = blackboard.assign(AgentTask(
            assignee=pcb.name, goal=pcb.default_goal(),
            acceptance_criteria=pcb.acceptance_criteria()),
            sender=CircuitArchitect.name)
        if not pcb.run(pcb_task):
            toolbox.close()
            self._persist_state(out_dir, spec, board_plan, blackboard)
            raise GenerationError(
                "PCB designer exhausted its action budget before satisfying "
                "the BoardPlan")

        final_eval = verifier.verify(out_dir, "pcb")
        final_repairs = repair.assign(final_eval)
        self._retry_owned_tasks(final_repairs, schematic, pcb, blackboard)

        toolbox.execute("crew", self._save_call())
        toolbox.close()
        self._snapshot(out_dir, "crew_complete")
        self._persist_state(out_dir, spec, board_plan, blackboard)
        from ratsnest.manufacturing import write_manufacturing_outputs
        write_manufacturing_outputs(out_dir, board_plan, spec)
        return out_dir

    @staticmethod
    def _save_call():
        from ratsnest.crews.contracts import ToolCall
        return ToolCall(
            tool="save_project", reason="persist final verified crew state",
            expected_result="KiCad project saved")

    @staticmethod
    def _retry_owned_tasks(tasks: list[AgentTask],
                           schematic: SchematicDesigner,
                           pcb: PcbDesigner,
                           blackboard: DesignBlackboard) -> None:
        """Re-enter a designer only when file truth shows its plan incomplete.

        General electrical repairs remain assigned to the existing repair_loop;
        unsupported changes remain explicit human tasks on the blackboard.
        """
        for task in tasks:
            if task.assignee == schematic.name and not schematic.acceptance_met():
                schematic.run(task)
            elif task.assignee == pcb.name and not pcb.acceptance_met():
                pcb.run(task)
            elif task.assignee in (schematic.name, pcb.name):
                blackboard.set_task_status(task, TaskStatus.blocked)

    @staticmethod
    def _persist_state(out_dir: Path, spec: DesignSpec, board_plan,
                       blackboard: DesignBlackboard) -> None:
        (out_dir / "designspec.json").write_text(
            spec.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / "boardplan.json").write_text(
            board_plan.model_dump_json(indent=2), encoding="utf-8")
        blackboard.checkpoint()
