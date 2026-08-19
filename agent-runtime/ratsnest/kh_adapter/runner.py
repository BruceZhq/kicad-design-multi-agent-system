"""Subprocess adapter around the vendored kicad-happy analyzers.

kicad-happy stays unforked: we drive its CLI/JSON contract only. Every output
envelope is validated for schema compatibility before use.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from ratsnest.config import Config
from ratsnest.schemas import AnalyzerOutput

SUPPORTED_SCHEMA_PREFIX = "1."  # kicad-happy harmonized envelope v1.x

# never flash console windows on the user's desktop (Windows only)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AdapterError(RuntimeError):
    pass


def find_root_schematic(project_dir: Path) -> Path:
    """Locate the root .kicad_sch: prefer the one matching the .kicad_pro name."""
    project_dir = Path(project_dir)
    schs = sorted(project_dir.glob("*.kicad_sch"))
    if not schs:
        raise AdapterError(f"no .kicad_sch in {project_dir}")
    pros = list(project_dir.glob("*.kicad_pro"))
    if pros:
        wanted = pros[0].with_suffix(".kicad_sch").name
        for s in schs:
            if s.name == wanted:
                return s
    return schs[0]


class KicadHappyAdapter:
    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()
        script = self.config.kicad_scripts / "analyze_schematic.py"
        if not script.exists():
            raise AdapterError(
                f"kicad-happy not found at {self.config.kicad_happy_root} "
                f"(set RATSNEST_KICAD_HAPPY_ROOT)"
            )

    def _run_analyzer(self, script_name: str, target: Path) -> AnalyzerOutput:
        script = self.config.kicad_scripts / script_name
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "out.json"
            proc = subprocess.run(
                [sys.executable, str(script), str(target), "--output", str(out_path)],
                capture_output=True, text=True, timeout=300,
                stdin=subprocess.DEVNULL,  # pytest capture leaves stdin invalid on Windows
                creationflags=NO_WINDOW,
            )
            if proc.returncode != 0 or not out_path.exists():
                raise AdapterError(
                    f"{script_name} failed (rc={proc.returncode}): "
                    f"{proc.stderr.strip()[:500]}"
                )
            raw = json.loads(out_path.read_text(encoding="utf-8"))
        envelope = AnalyzerOutput.model_validate(raw)
        if not envelope.schema_version.startswith(SUPPORTED_SCHEMA_PREFIX):
            raise AdapterError(
                f"unsupported kicad-happy schema_version "
                f"{envelope.schema_version!r} from {script_name}"
            )
        return envelope

    def analyze_schematic(self, project_dir: Path) -> AnalyzerOutput:
        return self._run_analyzer("analyze_schematic.py",
                                  find_root_schematic(project_dir))

    def analyze_pcb(self, project_dir: Path) -> AnalyzerOutput | None:
        pcbs = sorted(Path(project_dir).glob("*.kicad_pcb"))
        if not pcbs:
            return None
        return self._run_analyzer("analyze_pcb.py", pcbs[0])

    def analyze_all(self, project_dir: Path) -> dict[str, AnalyzerOutput]:
        """Run all applicable analyzers for the project. v1: schematic (+ PCB)."""
        outputs = {"schematic": self.analyze_schematic(project_dir)}
        pcb = self.analyze_pcb(project_dir)
        if pcb is not None:
            outputs["pcb"] = pcb
        return outputs
