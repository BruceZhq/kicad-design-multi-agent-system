"""Reproducible KiCad ERC/DRC release gates."""

from __future__ import annotations

import subprocess
from pathlib import Path
import re

from ratsnest.config import Config
from ratsnest.kh_adapter.runner import find_root_schematic
from ratsnest.schemas import GateStatus, VerificationGate


def parse_kicad_report(report: Path, gate: str) -> dict[str, int]:
    """Extract authoritative violation counts from KiCad text reports."""
    text = Path(report).read_text(encoding="utf-8", errors="replace")
    if gate == "erc":
        match = re.search(
            r"ERC messages:\s*(\d+)\s+Errors\s+(\d+)\s+Warnings\s+(\d+)",
            text, flags=re.IGNORECASE)
        if match is None:
            raise ValueError("ERC report has no parseable summary")
        return {
            "messages": int(match.group(1)),
            "errors": int(match.group(2)),
            "warnings": int(match.group(3)),
        }
    if gate == "drc":
        patterns = {
            "violations": r"Found\s+(\d+)\s+DRC violations",
            "unconnected_pads": r"Found\s+(\d+)\s+unconnected pads",
            "footprint_errors": r"Found\s+(\d+)\s+Footprint errors",
        }
        counts: dict[str, int] = {}
        for name, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match is None:
                raise ValueError(f"DRC report has no parseable {name} summary")
            counts[name] = int(match.group(1))
        return counts
    raise ValueError(f"unsupported KiCad report type {gate!r}")


def _run_gate(name: str, project_dir: Path, config: Config,
              command: list[str], input_file: Path) -> VerificationGate:
    evidence_dir = Path(project_dir) / "verification"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report = evidence_dir / f"{name}.rpt"
    if not config.kicad_cli or not Path(config.kicad_cli).is_file():
        return VerificationGate(
            name=name, status=GateStatus.unavailable,
            summary="kicad-cli is unavailable", tool="kicad-cli")
    if not input_file.is_file():
        return VerificationGate(
            name=name, status=GateStatus.failed,
            summary=f"required {input_file.suffix} input is missing",
            tool="kicad-cli")

    args = [str(config.kicad_cli), *command,
            "--format", "report", "--output", str(report),
            "--severity-error", "--exit-code-violations", str(input_file)]
    try:
        process = subprocess.run(
            args, capture_output=True, text=True, timeout=180,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.SubprocessError, OSError) as exc:
        return VerificationGate(
            name=name, status=GateStatus.error,
            summary=f"kicad-cli execution failed: {exc}", tool="kicad-cli")

    evidence = [str(report.relative_to(project_dir))] if report.is_file() else []
    counts: dict[str, int] = {}
    parse_error = None
    if report.is_file():
        try:
            counts = parse_kicad_report(report, name)
        except ValueError as exc:
            parse_error = str(exc)
    error_count = (counts.get("errors", 0) if name == "erc" else
                   sum(counts.values()))
    if process.returncode == 0 and report.is_file() and parse_error is None \
            and error_count == 0:
        status = GateStatus.passed
        summary = f"KiCad {name.upper()} found no error-severity violations"
    elif report.is_file() and parse_error is None:
        status = GateStatus.failed
        summary = f"KiCad {name.upper()} reported error-severity violations"
    elif report.is_file():
        status = GateStatus.error
        summary = f"KiCad {name.upper()} report is invalid: {parse_error}"
    else:
        status = GateStatus.error
        detail = (process.stderr or process.stdout or "no report produced").strip()
        summary = f"KiCad {name.upper()} did not produce a report: {detail[:240]}"
    return VerificationGate(
        name=name, status=status, summary=summary, tool="kicad-cli",
        evidence=evidence,
        metrics={"exit_code": process.returncode, **counts})


def run_erc_gate(project_dir: Path,
                 config: Config | None = None) -> VerificationGate:
    config = config or Config.load()
    try:
        schematic = find_root_schematic(Path(project_dir))
    except Exception:
        schematic = Path(project_dir) / "missing.kicad_sch"
    return _run_gate(
        "erc", Path(project_dir), config, ["sch", "erc"], schematic)


def run_drc_gate(project_dir: Path,
                 config: Config | None = None) -> VerificationGate:
    config = config or Config.load()
    boards = sorted(Path(project_dir).glob("*.kicad_pcb"))
    board = boards[0] if boards else Path(project_dir) / "missing.kicad_pcb"
    return _run_gate(
        "drc", Path(project_dir), config,
        ["pcb", "drc", "--schematic-parity", "--all-track-errors"], board)


def run_erc(project_dir: Path, config: Config | None = None) -> bool | None:
    """Compatibility adapter used by legacy repair-only runs."""
    gate = run_erc_gate(project_dir, config)
    if gate.status in {GateStatus.unavailable, GateStatus.error}:
        return None
    return gate.status == GateStatus.passed
