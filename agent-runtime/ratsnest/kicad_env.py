"""KiCad in-process environment bootstrap.

Our venv is built from KiCad 10's bundled interpreter, so pcbnew (a compiled
SWIG module) is importable once KiCad's DLL dir and site-packages are on the
path. This is what lets both vendored projects run IN-PROCESS as agent skills
instead of behind subprocess/MCP walls.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_bootstrapped = False


def bootstrap_kicad(kicad_python: Path | None = None) -> bool:
    """Make pcbnew + KiCad site-packages importable. Returns availability."""
    global _bootstrapped
    if _bootstrapped:
        return True
    if kicad_python is None:
        from ratsnest.config import Config
        kicad_python = Config.load().kicad_python
    if not kicad_python:
        return False
    bin_dir = Path(kicad_python).parent
    site = bin_dir / "Lib" / "site-packages"
    if not site.exists():
        return False
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(bin_dir))
    if str(site) not in sys.path:
        sys.path.insert(0, str(site))
    # symbol library anchor for non-standard install locations (E:\KiCad):
    # the vendored dynamic symbol loader consults these env vars first
    symbols = bin_dir.parent / "share" / "kicad" / "symbols"
    if symbols.exists():
        for var in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD_SYMBOL_DIR"):
            os.environ.setdefault(var, str(symbols))
    footprints = bin_dir.parent / "share" / "kicad" / "footprints"
    if footprints.exists():
        for var in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR",
                    "KICAD_FOOTPRINT_DIR"):
            os.environ.setdefault(var, str(footprints))
    try:
        import pcbnew  # noqa: F401
    except Exception:
        return False
    _bootstrapped = True
    return True
