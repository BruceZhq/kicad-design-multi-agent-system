"""Thin wrapper around the ``kicad-cli`` command-line tool.

``kicad-cli`` ships with KiCAD and drives Gerber/PDF export, ERC and DRC.
We shell out to it rather than linking any KiCAD library, so this module has
no KiCAD Python dependency and works against whatever KiCAD is installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Common install locations checked when ``kicad-cli`` is not on PATH.
_WINDOWS_CANDIDATES = [
    r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
    r"C:\Program Files\KiCad\7.0\bin\kicad-cli.exe",
]
_POSIX_CANDIDATES = [
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
]


class KicadCliNotFound(RuntimeError):
    pass


@dataclass
class CliResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    outputs: List[str]


def find_kicad_cli(explicit: Optional[str] = None) -> str:
    """Locate the ``kicad-cli`` executable or raise :class:`KicadCliNotFound`."""
    if explicit:
        if Path(explicit).exists():
            return explicit
        raise KicadCliNotFound(f"kicad-cli not found at {explicit!r}")
    found = shutil.which("kicad-cli")
    if found:
        return found
    from .kicad_paths import cli_candidates
    candidates = [str(p) for p in cli_candidates()]
    candidates += _WINDOWS_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES
    for cand in candidates:
        if Path(cand).exists():
            return cand
    raise KicadCliNotFound(
        "kicad-cli not found on PATH or common install locations; "
        "pass its path explicitly"
    )


class KicadCli:
    def __init__(self, cli_path: Optional[str] = None):
        self.cli_path = find_kicad_cli(cli_path)

    def _run(self, args: List[str], produced: Optional[List[str]] = None) -> CliResult:
        proc = subprocess.run(
            [self.cli_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        outputs = [p for p in (produced or []) if Path(p).exists()]
        return CliResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            outputs=outputs,
        )

    # -- schematic --------------------------------------------------------- #

    def export_schematic_pdf(self, sch_path: str, out_pdf: str) -> CliResult:
        return self._run(
            ["sch", "export", "pdf", "--output", out_pdf, sch_path],
            produced=[out_pdf],
        )

    def run_erc(self, sch_path: str, out_report: str) -> CliResult:
        return self._run(
            ["sch", "erc", "--output", out_report, "--exit-code-violations", sch_path],
            produced=[out_report],
        )

    # -- pcb --------------------------------------------------------------- #

    def export_gerbers(self, pcb_path: str, out_dir: str) -> CliResult:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        return self._run(
            ["pcb", "export", "gerbers", "--output", out_dir, pcb_path],
            produced=[out_dir],
        )

    def export_pcb_pdf(self, pcb_path: str, out_pdf: str) -> CliResult:
        return self._run(
            ["pcb", "export", "pdf", "--output", out_pdf, pcb_path],
            produced=[out_pdf],
        )

    def run_drc(self, pcb_path: str, out_report: str) -> CliResult:
        return self._run(
            ["pcb", "drc", "--output", out_report, "--exit-code-violations", pcb_path],
            produced=[out_report],
        )

    def version(self) -> CliResult:
        return self._run(["version"])
