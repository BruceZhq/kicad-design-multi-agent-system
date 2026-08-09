"""Discovery for evidence-generated KiCad libraries.

Generated libraries deliberately have their own root instead of being copied
into KiCad's system installation.  A root may be configured explicitly with
``RATSNESTPRO_GENERATED_LIBRARY_ROOT`` (``os.pathsep`` separated), derived from
``RATSNESTPRO_WORKSPACE_ROOT``, or registered for the lifetime of the current
process by a caller that supplied an explicit output path.

This module has no dependency on the symbol/footprint resolvers.  Keeping the
root registry at this low layer avoids import cycles while allowing both
resolvers and their indexes to discover a newly generated library.
"""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock

_ROOT_LOCK = RLock()
_RUNTIME_ROOTS: set[Path] = set()


def _normal(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _configured_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("RATSNESTPRO_GENERATED_LIBRARY_ROOT", "")
    roots.extend(_normal(item) for item in configured.split(os.pathsep) if item)
    workspace = os.environ.get("RATSNESTPRO_WORKSPACE_ROOT", "").strip()
    if workspace:
        roots.append(_normal(Path(workspace) / "generated-libraries"))
    return roots


def default_generated_library_root() -> Path:
    """Return the deterministic workspace-local output root.

    The directory is not created here.  Generation owns that state change.
    """

    configured = os.environ.get("RATSNESTPRO_GENERATED_LIBRARY_ROOT", "")
    first = next((item for item in configured.split(os.pathsep) if item), None)
    if first:
        return _normal(first)
    workspace = os.environ.get("RATSNESTPRO_WORKSPACE_ROOT", "").strip()
    if workspace:
        return _normal(Path(workspace) / "generated-libraries")
    return _normal(Path.cwd() / ".ratsnestpro" / "generated-libraries")


def register_generated_library_root(path: str | os.PathLike[str]) -> Path:
    """Make an explicit generated root discoverable in this process."""

    root = _normal(path)
    with _ROOT_LOCK:
        _RUNTIME_ROOTS.add(root)
    return root


def generated_library_roots(*, existing_only: bool = True) -> list[Path]:
    """Return generated roots in deterministic priority order."""

    with _ROOT_LOCK:
        runtime = sorted(_RUNTIME_ROOTS, key=str)
    roots = [*_configured_roots(), *runtime]
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key in seen or (existing_only and not root.is_dir()):
            continue
        seen.add(key)
        result.append(root)
    return result

