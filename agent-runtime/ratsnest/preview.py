"""Headless read-only previews: schematic/PCB -> SVG via kicad-cli.

Feature-gated: silently skipped when kicad-cli is unavailable (e.g. inside
the slim worker container). Output convention consumed by the control plane:
    <project>/preview/sch.svg
    <project>/preview/pcb.svg
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ratsnest.config import Config

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        return proc.returncode == 0
    except Exception:
        return False


def snapshot_schematic(project_dir: Path, tag: str,
                       config: Config | None = None) -> Path | None:
    """One timeline frame: export the schematic's CURRENT state to
    preview/steps/<tag>.svg. Best-effort and cheap to call after each agent
    action — this is what the frontend plays back step by step."""
    config = config or Config.load()
    if not config.kicad_cli or not Path(config.kicad_cli).exists():
        return None
    project_dir = Path(project_dir)
    schs = sorted(project_dir.glob("*.kicad_sch"))
    if not schs:
        return None
    steps_dir = project_dir / "preview" / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        if not _run([str(config.kicad_cli), "sch", "export", "svg",
                     "--output", td, "--no-background-color", str(schs[0])]):
            return None
        produced = sorted(Path(td).glob("*.svg"))
        if not produced:
            return None
        safe = "".join(c for c in tag if c.isalnum() or c in "_-")[:60]
        target = steps_dir / f"{safe}.svg"
        shutil.copy(produced[-1], target)
        return target


def generate_previews(project_dir: Path,
                      config: Config | None = None) -> dict[str, Path]:
    config = config or Config.load()
    if not config.kicad_cli or not Path(config.kicad_cli).exists():
        return {}
    project_dir = Path(project_dir)
    preview_dir = project_dir / "preview"
    preview_dir.mkdir(exist_ok=True)
    out: dict[str, Path] = {}

    schs = sorted(project_dir.glob("*.kicad_sch"))
    if schs and _run([str(config.kicad_cli), "sch", "export", "svg",
                      "--output", str(preview_dir),
                      "--no-background-color", str(schs[0])]):
        produced = sorted(preview_dir.glob("*.svg"),
                          key=lambda p: p.stat().st_mtime)
        if produced:
            target = preview_dir / "sch.svg"
            if produced[-1] != target:
                shutil.move(str(produced[-1]), target)
            out["sch"] = target

    pcbs = sorted(project_dir.glob("*.kicad_pcb"))
    if pcbs:
        target = preview_dir / "pcb.svg"
        if _run([str(config.kicad_cli), "pcb", "export", "svg",
                 "--output", str(target),
                 "--layers", "F.Cu,B.Cu,Edge.Cuts,F.SilkS",
                 "--page-size-mode", "2", str(pcbs[0])]) and target.exists():
            out["pcb"] = target
    return out
