"""Framework integration for the embedded RatsNestPro project."""

import sys
from pathlib import Path

from temporalio import workflow

# Local source checkouts need the embedded package on sys.path. The production
# image installs it explicitly, and Temporal's deterministic sandbox must not
# perform filesystem discovery while it re-imports workflow modules.
if not workflow.unsafe.in_sandbox():
    _EMBEDDED_SRC = (
        Path(__file__).resolve().parents[2]
        / "RatsNestPro-main"
        / "RatsNestPro-main"
        / "src"
    )
    if _EMBEDDED_SRC.is_dir() and str(_EMBEDDED_SRC) not in sys.path:
        sys.path.insert(0, str(_EMBEDDED_SRC))
