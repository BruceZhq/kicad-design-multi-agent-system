import shutil
from pathlib import Path

import pytest

from ratsnest.config import REPO_ROOT

GOLDEN_BOARD = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"


@pytest.fixture()
def golden_project(tmp_path: Path) -> Path:
    """A disposable copy of the golden demo board."""
    dst = tmp_path / "demo_board"
    shutil.copytree(GOLDEN_BOARD, dst)
    # analysis.json snapshot isn't part of the project
    (dst / "analysis.json").unlink(missing_ok=True)
    return dst
