"""Central configuration: KiCad library paths + fab process capability.

Two responsibilities:

1. **Library paths.** RatsNestPro reads real KiCad symbol and footprint
   libraries to ground pin/pad geometry (never LLM memory). The locations come
   from ``KICAD_SYMBOL_DIR`` / ``KICAD_FOOTPRINT_DIR`` environment variables.
   A project-local ``.env`` file (KEY=VALUE lines) is loaded once on import so
   users can point at their libraries without exporting shell variables. The
   vendored resolvers and :mod:`ratsnestpro.eda.symbols` read these env vars,
   so setting them here is enough to wire everything up.

2. **Process capability.** The minimum manufacturable track width / clearance /
   via / drill / annular-ring values used by the "anti-board-burn" bottom-line
   checks. These are *fab facts*, not business rules — a JSON table, editable
   or overridable via ``RATSNESTPRO_PROCESS_CAPABILITY``. Defaults ship in
   ``data/process_capability.json`` (conservative JLCPCB standard 2-layer).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_ENV_LOADED = False
_DATA_DIR = Path(__file__).parent / "data"
_DEFAULT_CAPABILITY = _DATA_DIR / "process_capability.json"


def project_root() -> Path:
    """Walk up from this file to the directory containing ``pyproject.toml``."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parent


def _parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a ``.env`` file into ``os.environ``.

    Existing environment variables win unless ``override`` is set. Returns the
    parsed mapping (whether or not it was applied).
    """
    env_path = path or (project_root() / ".env")
    if not env_path.is_file():
        return {}
    parsed = _parse_dotenv(env_path.read_text(encoding="utf-8"))
    for key, value in parsed.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return parsed


def init_env() -> None:
    """Load the project ``.env`` once (idempotent). Safe to call repeatedly."""
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv()
        _ENV_LOADED = True


def symbol_dir() -> Path | None:
    """First existing directory from ``KICAD_SYMBOL_DIR`` (os.pathsep list)."""
    init_env()
    return _first_existing(os.environ.get("KICAD_SYMBOL_DIR"))


def footprint_dir() -> Path | None:
    """First existing directory from ``KICAD_FOOTPRINT_DIR`` (os.pathsep list)."""
    init_env()
    return _first_existing(os.environ.get("KICAD_FOOTPRINT_DIR"))


def _first_existing(value: str | None) -> Path | None:
    if not value:
        return None
    for part in value.split(os.pathsep):
        if part:
            p = Path(part)
            if p.exists():
                return p
    return None


class ProcessCapability(BaseModel):
    """Fab manufacturing minimums (mm). Authoritative for bottom-line checks."""

    model_config = ConfigDict(extra="ignore")

    fab_house: str = "generic"
    profile: str = "default"
    units: str = "mm"
    min_track_width: float = Field(gt=0)
    min_clearance: float = Field(gt=0)
    min_via_diameter: float = Field(gt=0)
    min_via_drill: float = Field(gt=0)
    min_annular_ring: float = Field(ge=0)
    min_hole_diameter: float = Field(gt=0)
    min_board_edge_clearance: float = Field(ge=0)
    min_silk_width: float = Field(default=0.15, ge=0)
    layer_options: list[int] = Field(default_factory=lambda: [2, 4])


@lru_cache(maxsize=8)
def _load_capability(path_str: str) -> ProcessCapability:
    data = json.loads(Path(path_str).read_text(encoding="utf-8"))
    return ProcessCapability.model_validate(data)


def process_capability(path: Path | None = None) -> ProcessCapability:
    """Load the fab process capability table.

    Resolution order: explicit ``path`` → ``RATSNESTPRO_PROCESS_CAPABILITY``
    env → bundled default ``data/process_capability.json``.
    """
    init_env()
    chosen = path or os.environ.get("RATSNESTPRO_PROCESS_CAPABILITY") or _DEFAULT_CAPABILITY
    return _load_capability(str(chosen))
