"""Find a pin-compatible, intrinsically DRC-clean installed footprint.

The worker is invoked only after the final KiCad DRC proves that a footprint's
own mechanical geometry violates the selected fabrication rules.  Candidates
come from the same KiCad library and semantic footprint family, must expose the
same numbered pad set, and are ranked by authoritative isolated-footprint DRC.
It never changes an MPN or silently weakens a project rule.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pcbnew


def _footprint_root() -> Path:
    configured = os.environ.get("KICAD_FOOTPRINT_DIR", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path("/usr/share/kicad/footprints"),
        Path("/usr/local/share/kicad/footprints"),
    ]
    return next(
        path
        for path in candidates
        if path is not None and path.is_dir()
    )


def _load(lib_id: str):
    library, name = lib_id.split(":", 1)
    return pcbnew.FootprintLoad(
        str(_footprint_root() / f"{library}.pretty"),
        name,
    )


def _numbered_pads(footprint) -> set[str]:
    return {
        str(pad.GetNumber())
        for pad in footprint.Pads()
        if str(pad.GetNumber())
    }


def _family_prefix(name: str) -> str:
    tokens = name.split("_")
    if len(tokens) >= 3:
        return "_".join(tokens[:3]) + "_"
    if len(tokens) >= 2:
        return "_".join(tokens[:2]) + "_"
    return name


def _outline(board, left: float, top: float, right: float, bottom: float) -> None:
    points = (
        (left, top, right, top),
        (right, top, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, top),
    )
    for x1, y1, x2, y2 in points:
        segment = pcbnew.PCB_SHAPE(board)
        segment.SetShape(pcbnew.SHAPE_T_SEGMENT)
        segment.SetLayer(pcbnew.Edge_Cuts)
        segment.SetStart(
            pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1))
        )
        segment.SetEnd(
            pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2))
        )
        segment.SetWidth(pcbnew.FromMM(0.05))
        board.Add(segment)


def _error_summary(
    cli: str,
    project: Path,
    lib_id: str,
    root: Path,
    serial: int,
) -> tuple[int, dict[str, int]]:
    stem = f"candidate-{serial}"
    pcb_path = root / f"{stem}.kicad_pcb"
    report_path = root / f"{stem}.drc.json"
    if project.is_file():
        shutil.copy2(project, root / f"{stem}.kicad_pro")
    board = pcbnew.BOARD()
    _outline(board, 5.0, 5.0, 95.0, 95.0)
    footprint = _load(lib_id)
    footprint.SetPosition(
        pcbnew.VECTOR2I(pcbnew.FromMM(50.0), pcbnew.FromMM(50.0))
    )
    board.Add(footprint)
    pcbnew.SaveBoard(str(pcb_path), board)
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
            str(pcb_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    with report_path.open(encoding="utf-8") as handle:
        report = json.load(handle)
    counts: dict[str, int] = {}
    for key in ("violations", "schematic_parity"):
        for finding in report.get(key, []):
            if (
                not isinstance(finding, dict)
                or str(finding.get("severity", "error")) != "error"
            ):
                continue
            kind = str(finding.get("type", "unknown"))
            counts[kind] = counts.get(kind, 0) + 1
    return sum(counts.values()), counts


def main() -> None:
    pcb_path = Path(sys.argv[1]).resolve()
    cli = sys.argv[2]
    ref = sys.argv[3]
    selected_lib_id = sys.argv[4]
    result = {
        "ok": False,
        "pending": False,
        "footprint": "",
        "baseline_errors": -1,
        "candidate_errors": -1,
        "candidate_error_types": {},
        "error": "",
    }
    try:
        board = pcbnew.LoadBoard(str(pcb_path))
        actual = next(
            (
                footprint
                for footprint in board.GetFootprints()
                if footprint.GetReference() == ref
            ),
            None,
        )
        if actual is None:
            raise RuntimeError(f"board has no footprint for {ref}")
        actual_lib_id = actual.GetFPID().GetUniStringLibId()
        if actual_lib_id != selected_lib_id:
            # A previous call already updated the SelectionPlan.  The pipeline
            # must rematerialize the schematic and PCB before another decision.
            result.update(
                {
                    "ok": True,
                    "pending": True,
                    "footprint": selected_lib_id,
                }
            )
            print("RESULT " + json.dumps(result, ensure_ascii=False))
            return

        library, name = selected_lib_id.split(":", 1)
        library_path = _footprint_root() / f"{library}.pretty"
        current = _load(selected_lib_id)
        required_pads = _numbered_pads(current)
        if not required_pads:
            raise RuntimeError("current footprint has no numbered pads")
        prefix = _family_prefix(name)
        project = pcb_path.with_suffix(".kicad_pro")
        with tempfile.TemporaryDirectory(prefix="rnp_footprint_repair_") as raw:
            root = Path(raw)
            baseline_errors, _ = _error_summary(
                cli,
                project,
                selected_lib_id,
                root,
                0,
            )
            ranked: list[tuple[int, str, dict[str, int]]] = []
            serial = 0
            for path in sorted(library_path.glob(f"{prefix}*.kicad_mod")):
                candidate_lib_id = f"{library}:{path.stem}"
                if candidate_lib_id == selected_lib_id:
                    continue
                candidate = _load(candidate_lib_id)
                if _numbered_pads(candidate) != required_pads:
                    continue
                serial += 1
                error_count, error_types = _error_summary(
                    cli,
                    project,
                    candidate_lib_id,
                    root,
                    serial,
                )
                ranked.append((error_count, candidate_lib_id, error_types))
            if not ranked:
                raise RuntimeError(
                    "no installed same-family footprint has the same pad set"
                )
            candidate_errors, candidate_lib_id, candidate_types = min(ranked)
            if candidate_errors >= baseline_errors:
                raise RuntimeError(
                    "no compatible installed footprint improves intrinsic DRC"
                )
            result.update(
                {
                    "ok": True,
                    "footprint": candidate_lib_id,
                    "baseline_errors": baseline_errors,
                    "candidate_errors": candidate_errors,
                    "candidate_error_types": candidate_types,
                }
            )
    except Exception as exc:  # noqa: BLE001 - caller records a capability gap
        result["error"] = str(exc)
    print("RESULT " + json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
