"""Bounded Freerouting CLI adapter used by the production PCB tool.

The LLM-facing tool has no access to these arguments. RatsNest owns the
executable paths, resource bounds, and safety settings, then imports the SES
result back into the already-open KiCad board.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from ratsnest.config import Config


class FreeroutingError(RuntimeError):
    """Freerouting could not produce and import a bounded routing result."""


def run_freerouting(board: Any, board_path: Path, config: Config) -> dict[str, Any]:
    """Route ``board`` through a bounded, non-interactive Freerouting process."""
    board_path = Path(board_path).resolve()
    jar = Path(config.freerouting_jar).resolve() if config.freerouting_jar else None
    if jar is None or not jar.is_file():
        raise FreeroutingError(
            "Freerouting JAR is unavailable; set RATSNEST_FREEROUTING_JAR")

    configured_java = config.freerouting_java
    java = (str(Path(configured_java).resolve()) if configured_java
            else shutil.which("java"))
    if not java or not Path(java).is_file():
        raise FreeroutingError(
            "Freerouting Java runtime is unavailable; set RATSNEST_FREEROUTING_JAVA")

    dsn_path = board_path.with_suffix(".dsn")
    ses_path = board_path.with_suffix(".ses")
    ses_path.unlink(missing_ok=True)

    import pcbnew

    exported = pcbnew.ExportSpecctraDSN(board, str(dsn_path))
    if exported is not True and exported != 0:
        raise FreeroutingError(
            f"KiCad DSN export failed with result {exported!r}")
    if not dsn_path.is_file():
        raise FreeroutingError("KiCad DSN export produced no file")

    command = [
        java,
        "-jar",
        str(jar),
        "-de",
        str(dsn_path),
        "-do",
        str(ses_path),
        "-mp",
        str(config.freerouting_max_passes),
        "--router.optimizer.enabled=false",
        "--router.automatic_neckdown=true",
        "--router.max_threads=1",
        "--gui.enabled=false",
        "-da",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.freerouting_timeout_seconds,
            cwd=str(board_path.parent),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FreeroutingError(
            "Freerouting timed out after "
            f"{config.freerouting_timeout_seconds}s") from exc
    elapsed = round(time.monotonic() - started, 3)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
        raise FreeroutingError(
            f"Freerouting exited with code {completed.returncode}: {detail}")
    if not ses_path.is_file() or ses_path.stat().st_size == 0:
        raise FreeroutingError("Freerouting produced no inspectable SES result")

    imported = pcbnew.ImportSpecctraSES(board, str(ses_path))
    if imported is not True and imported != 0:
        raise FreeroutingError(
            f"KiCad SES import failed with result {imported!r}")
    board.Save(str(board_path))

    tracks = list(board.GetTracks())
    return {
        "success": True,
        "mode": "freerouting-cli",
        "dsn_path": str(dsn_path),
        "ses_path": str(ses_path),
        "elapsed_seconds": elapsed,
        "best_attempt": 1,
        "board_stats": {
            "tracks": sum(track.GetClass() != "PCB_VIA" for track in tracks),
            "vias": sum(track.GetClass() == "PCB_VIA" for track in tracks),
        },
        "freerouting_stdout": completed.stdout[-2000:],
    }
