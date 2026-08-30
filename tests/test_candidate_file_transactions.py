from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline import PipelineContext


def _candidate(ctx: PipelineContext, out: Path) -> str:
    (out / ".ratsnest-libs" / "symbols").mkdir(parents=True)
    (out / ".ratsnest-libs" / "symbols" / "device.kicad_sym").write_text(
        "baseline-library",
        encoding="utf-8",
    )
    (out / "board.kicad_pcb").write_text("baseline-board", encoding="utf-8")
    return pipeline._snapshot_candidate_files(ctx, "candidate")


def test_legacy_container_snapshot_path_maps_to_exact_windows_run_scope(
    tmp_path: Path,
) -> None:
    out = tmp_path / "runs" / "run-a"
    out.mkdir(parents=True)
    ctx = PipelineContext(out_dir=str(out))
    token = _candidate(ctx, out)
    legacy = (
        "/data/ratsnestpro/runs/.ratsnest-candidate-transactions/"
        f"{out.name}/{token}"
    )
    (out / "board.kicad_pcb").write_text("candidate-board", encoding="utf-8")
    (out / ".ratsnest-libs" / "symbols" / "device.kicad_sym").unlink()
    (out / "candidate-only.txt").write_text("extra", encoding="utf-8")

    pipeline._restore_candidate_files(ctx, legacy)

    assert (out / "board.kicad_pcb").read_text(encoding="utf-8") == "baseline-board"
    assert (
        out / ".ratsnest-libs" / "symbols" / "device.kicad_sym"
    ).read_text(encoding="utf-8") == "baseline-library"
    assert not (out / "candidate-only.txt").exists()


def test_staging_failure_leaves_live_candidate_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "runs" / "run-a"
    out.mkdir(parents=True)
    ctx = PipelineContext(out_dir=str(out))
    token = _candidate(ctx, out)
    (out / "board.kicad_pcb").write_text("candidate-board", encoding="utf-8")
    library = out / ".ratsnest-libs" / "symbols" / "device.kicad_sym"
    library.write_text("candidate-library", encoding="utf-8")
    original_copy = shutil.copy2

    def fail_library_copy(source: str | os.PathLike[str], target: str | os.PathLike[str]):
        if Path(source).name == "device.kicad_sym":
            raise PermissionError("simulated Windows library lock")
        return original_copy(source, target)

    monkeypatch.setattr(pipeline.shutil, "copy2", fail_library_copy)

    with pytest.raises(PermissionError, match="library lock"):
        pipeline._restore_candidate_files(ctx, token)

    assert (out / "board.kicad_pcb").read_text(encoding="utf-8") == "candidate-board"
    assert library.read_text(encoding="utf-8") == "candidate-library"


def test_snapshot_cleanup_failure_is_best_effort_after_restore_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "runs" / "run-a"
    out.mkdir(parents=True)
    ctx = PipelineContext(out_dir=str(out))
    token = _candidate(ctx, out)
    (out / "board.kicad_pcb").write_text("candidate-board", encoding="utf-8")
    original_rmtree = shutil.rmtree

    def fail_snapshot_cleanup(path: str | os.PathLike[str], *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path).name == token:
            raise PermissionError("simulated delayed Windows handle release")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline.shutil, "rmtree", fail_snapshot_cleanup)

    pipeline._restore_candidate_files(ctx, token)

    assert (out / "board.kicad_pcb").read_text(encoding="utf-8") == "baseline-board"
    assert (
        out / ".ratsnest-libs" / "symbols" / "device.kicad_sym"
    ).read_text(encoding="utf-8") == "baseline-library"


def test_partial_atomic_install_failure_rolls_back_installed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "runs" / "run-a"
    out.mkdir(parents=True)
    ctx = PipelineContext(out_dir=str(out))
    token = _candidate(ctx, out)
    board = out / "board.kicad_pcb"
    library = out / ".ratsnest-libs" / "symbols" / "device.kicad_sym"
    board.write_text("candidate-board", encoding="utf-8")
    library.write_text("candidate-library", encoding="utf-8")
    original_replace = os.replace

    def fail_second_install(source: str | os.PathLike[str], target: str | os.PathLike[str]):
        source_path = Path(source)
        if "staged" in source_path.parts and source_path.name == board.name:
            raise PermissionError("simulated atomic replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(pipeline.os, "replace", fail_second_install)

    with pytest.raises(PermissionError, match="atomic replace failure"):
        pipeline._restore_candidate_files(ctx, token)

    assert board.read_text(encoding="utf-8") == "candidate-board"
    assert library.read_text(encoding="utf-8") == "candidate-library"
