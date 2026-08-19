"""Checker crew — kicad-happy disassembled into analyst agents.

Each agent owns ONE analyzer module, mounted in-process via khlib (the code
stays vendored and pullable; the architecture stops treating it as a single
black box). Every agent emits its own ATDP events, and per-agent knobs live
in the strategy's `analysts` slice — making individual checkers evolvable
(enable/disable, stage, future thresholds) instead of all-or-nothing.
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.agents.base import Agent
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.kh_adapter.runner import find_root_schematic
from ratsnest.khlib import load_kh_module
from ratsnest.schemas import AnalyzerOutput, StrategyBundle


class SchematicAnalyst(Agent):
    crew = "checker"
    name = "schematic_analyst"

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def analyze(self, project_dir: Path) -> AnalyzerOutput:
        sch = find_root_schematic(project_dir)
        module = load_kh_module("analyze_schematic", self.config.kicad_scripts)
        raw = self.act(
            "analyze_schematic",
            lambda: module.analyze_schematic(str(sch)),
            observation={"file": sch.name},
            action_detail={"module": "kicad-happy/analyze_schematic"},
        )
        return AnalyzerOutput.model_validate(raw)


class PcbAnalyst(Agent):
    crew = "checker"
    name = "pcb_analyst"

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def analyze(self, project_dir: Path) -> AnalyzerOutput | None:
        pcbs = sorted(Path(project_dir).glob("*.kicad_pcb"))
        if not pcbs:
            return None
        module = load_kh_module("analyze_pcb", self.config.kicad_scripts)
        try:
            raw = self.act(
                "analyze_pcb",
                lambda: module.analyze_pcb(str(pcbs[0])),
                observation={"file": pcbs[0].name},
                action_detail={"module": "kicad-happy/analyze_pcb"},
            )
        except Exception:
            return None  # boards with no layout content yet
        return AnalyzerOutput.model_validate(raw)


class DependentAnalyst(Agent):
    """Second-tier analysts (cross/thermal): consume the schematic + PCB
    analysis envelopes through the analyzers' designed CLI contract
    (--schematic/--pcb/--output JSON files)."""

    crew = "checker"
    script = ""       # e.g. "cross_analysis.py"
    skill = "kicad"   # kicad-happy skill dir the script lives in

    def __init__(self, config: Config, **kw):
        super().__init__(**kw)
        self.config = config

    def analyze_from(self, schematic: AnalyzerOutput,
                     pcb: AnalyzerOutput) -> AnalyzerOutput | None:
        import json
        import subprocess
        import sys
        import tempfile
        script = (self.config.kicad_happy_root / "skills" / self.skill
                  / "scripts" / self.script)

        def run() -> dict:
            with tempfile.TemporaryDirectory() as td:
                td_path = Path(td)
                sch_json = td_path / "sch.json"
                pcb_json = td_path / "pcb.json"
                out_json = td_path / "out.json"
                sch_json.write_text(schematic.model_dump_json(), encoding="utf-8")
                pcb_json.write_text(pcb.model_dump_json(), encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, str(script), "--schematic", str(sch_json),
                     "--pcb", str(pcb_json), "--output", str(out_json)],
                    capture_output=True, text=True, timeout=180,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0 or not out_json.exists():
                    raise RuntimeError(proc.stderr.strip()[:200])
                return json.loads(out_json.read_text(encoding="utf-8"))

        try:
            raw = self.act(self.script.replace(".py", ""), run,
                           action_detail={"module": f"kicad-happy/{self.script}"})
        except Exception:
            return None  # dependent analysis is best-effort
        return AnalyzerOutput.model_validate(raw)


class CrossAnalyst(DependentAnalyst):
    name = "cross_analyst"
    script = "cross_analysis.py"


class ThermalAnalyst(DependentAnalyst):
    name = "thermal_analyst"
    script = "analyze_thermal.py"


class EmcAnalyst(DependentAnalyst):
    name = "emc_analyst"
    script = "analyze_emc.py"
    skill = "emc"


class CheckerCrew:
    """Fan-out over analyst agents; roster is strategy-governed."""

    def __init__(self, config: Config | None = None,
                 strategy: StrategyBundle | None = None,
                 recorder: Recorder | None = None, iteration: int = 0):
        self.config = config or Config.load()
        self.cfg = (strategy.solver_params.get("analysts", {})
                    if strategy else {})
        common = dict(recorder=recorder, iteration=iteration,
                      strategy_slice=self.cfg)
        self.agents: list = []
        if self.cfg.get("schematic", True):
            self.agents.append(SchematicAnalyst(self.config, **common))
        if self.cfg.get("pcb", True):
            self.agents.append(PcbAnalyst(self.config, **common))
        self.dependents: list[DependentAnalyst] = []
        if self.cfg.get("cross", True):
            self.dependents.append(CrossAnalyst(self.config, **common))
        if self.cfg.get("thermal", True):
            self.dependents.append(ThermalAnalyst(self.config, **common))
        # EMC is opt-in until generated boards carry real placement: its
        # geometry rules assume laid-out boards (enable via analysts.emc)
        if self.cfg.get("emc", False):
            self.dependents.append(EmcAnalyst(self.config, **common))

    def evaluate(self, project_dir: Path) -> dict[str, AnalyzerOutput]:
        outputs: dict[str, AnalyzerOutput] = {}
        for agent in self.agents:
            result = agent.analyze(Path(project_dir))
            if result is not None:
                key = result.analyzer_type or agent.name
                outputs[key] = result
        sch, pcb = outputs.get("schematic"), outputs.get("pcb")
        if sch is not None and pcb is not None and pcb.findings is not None:
            for agent in self.dependents:
                result = agent.analyze_from(sch, pcb)
                if result is not None:
                    outputs[result.analyzer_type or agent.name] = result
        return outputs
