"""Dynamic loader for kicad-happy modules — library-style reuse, unforked."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_cache: dict[str, object] = {}


def load_kh_module(name: str, scripts_dir: Path):
    """Load a kicad-happy script module by file path (cached)."""
    key = f"{scripts_dir}::{name}"
    if key in _cache:
        return _cache[key]
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))  # for intra-module imports
    spec = importlib.util.spec_from_file_location(
        f"kh_{name}", scripts_dir / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _cache[key] = module
    return module
