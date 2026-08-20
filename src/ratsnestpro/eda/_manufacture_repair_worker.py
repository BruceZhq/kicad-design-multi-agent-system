"""Apply DRC-monotonic, manufacturing-safe local PCB geometry repairs.

This worker runs under KiCad's system Python.  It currently normalizes only
round plated holes that the authoritative DRC identifies as smaller than the
selected fabrication process.  A candidate is committed only when it reduces
the real error count, preserves all connectivity, and introduces no new DRC
error signature.

The implementation is deliberately evidence-driven: affected pads, required
diameters, and acceptance all come from the board and its KiCad DRC report.
There are no component references or board families in the repair policy.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pcbnew

_MIN_HOLE_RE = re.compile(r"\bmin hole\s+([0-9.]+)\s*mm\b", re.IGNORECASE)


def _run_drc(cli: str, pcb_path: Path, report_path: Path) -> dict:
    report_path.unlink(missing_ok=True)
    subprocess.run(
        [
            cli,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(report_path),
            "--exit-code-violations",
            str(pcb_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    with report_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _errors(report: dict) -> list[dict]:
    return [
        finding
        for key in ("violations", "schematic_parity")
        for finding in report.get(key, [])
        if (
            isinstance(finding, dict)
            and str(finding.get("severity", "error")) == "error"
        )
    ]


def _error_counts(report: dict) -> Counter:
    return Counter(
        (
            str(finding.get("type", "unknown")),
            str(finding.get("description", "DRC error")),
        )
        for finding in _errors(report)
    )


def _unconnected_count(report: dict) -> int:
    return sum(
        isinstance(finding, dict)
        and str(finding.get("severity", "error")) == "error"
        for finding in report.get("unconnected_items", [])
    )


def _drill_targets(report: dict, process_min_mm: float) -> dict[str, float]:
    targets: dict[str, float] = {}
    for finding in _errors(report):
        if str(finding.get("type", "")) != "drill_out_of_range":
            continue
        match = _MIN_HOLE_RE.search(str(finding.get("description", "")))
        required = max(
            process_min_mm,
            float(match.group(1)) if match is not None else process_min_mm,
        )
        for item in finding.get("items", []):
            if not isinstance(item, dict):
                continue
            uuid = str(item.get("uuid", "")).strip()
            if uuid:
                targets[uuid] = max(targets.get(uuid, 0.0), required)
    return targets


def _normalize_round_plated_holes(
    board,
    targets: dict[str, float],
    min_annular_mm: float,
) -> list[dict]:
    changed: list[dict] = []
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            uuid = pad.m_Uuid.AsString()
            target_mm = targets.get(uuid)
            if target_mm is None:
                continue
            drill = pad.GetDrillSize()
            size = pad.GetSize()
            target = pcbnew.FromMM(target_mm)
            # Attribute 0 is a plated through-hole pad.  Slots and ambiguous
            # custom drills require a footprint substitution, not an automatic
            # geometry edit.
            if (
                int(pad.GetAttribute()) != 0
                or drill.x <= 0
                or drill.x != drill.y
                or drill.x >= target
                or (min(size.x, size.y) - target) / 2
                < pcbnew.FromMM(min_annular_mm)
            ):
                continue
            changed.append(
                {
                    "ref": footprint.GetReference(),
                    "pad": pad.GetNumber(),
                    "from_mm": pcbnew.ToMM(drill.x),
                    "to_mm": target_mm,
                }
            )
            pad.SetDrillSize(pcbnew.VECTOR2I(target, target))
    return changed


def main() -> None:
    pcb_path = Path(sys.argv[1]).resolve()
    cli = sys.argv[2]
    process_min_mm = float(sys.argv[3])
    min_annular_mm = float(sys.argv[4])
    report_path = Path(sys.argv[5]).resolve()
    result = {
        "ok": False,
        "changed": [],
        "before_errors": -1,
        "after_errors": -1,
        "unconnected": -1,
        "error": "",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="rnp_manufacture_repair_") as raw:
            root = Path(raw)
            candidate = root / pcb_path.name
            shutil.copy2(pcb_path, candidate)
            project = pcb_path.with_suffix(".kicad_pro")
            if project.is_file():
                shutil.copy2(project, candidate.with_suffix(".kicad_pro"))

            before_report = _run_drc(
                cli,
                candidate,
                root / "before.drc.json",
            )
            before_errors = _error_counts(before_report)
            before_unconnected = _unconnected_count(before_report)
            targets = _drill_targets(before_report, process_min_mm)
            board = pcbnew.LoadBoard(str(candidate))
            changed = _normalize_round_plated_holes(
                board,
                targets,
                min_annular_mm,
            )
            if not changed:
                raise RuntimeError(
                    "no DRC-identified round plated hole can be enlarged "
                    "within the required annular ring"
                )
            pcbnew.SaveBoard(str(candidate), board)
            after_report = _run_drc(
                cli,
                candidate,
                root / "after.drc.json",
            )
            after_errors = _error_counts(after_report)
            after_unconnected = _unconnected_count(after_report)
            no_new_errors = all(
                count <= before_errors[signature]
                for signature, count in after_errors.items()
            )
            accepted = (
                sum(after_errors.values()) < sum(before_errors.values())
                and after_unconnected <= before_unconnected
                and no_new_errors
            )
            result.update(
                {
                    "ok": accepted,
                    "changed": changed,
                    "before_errors": sum(before_errors.values()),
                    "after_errors": sum(after_errors.values()),
                    "unconnected": after_unconnected,
                }
            )
            if not accepted:
                result["error"] = (
                    "candidate did not monotonically improve authoritative DRC"
                )
            else:
                shutil.copy2(candidate, pcb_path)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                with report_path.open("w", encoding="utf-8") as handle:
                    json.dump(after_report, handle, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001 - caller treats this as rejected
        result["error"] = str(exc)
    print("RESULT " + json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
