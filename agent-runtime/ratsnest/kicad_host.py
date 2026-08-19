"""Shared in-process KiCad host — the ONE place the vendored
KiCADInterface (from KiCAD-MCP-Server) is bootstrapped and held.

Consumers: the creator crew's skill agents and the Web-EDA engine. Both
execute KiCad writes through this host so there is a single trusted write
path; neither owns the infrastructure.
"""

from __future__ import annotations

import sys

from ratsnest.config import Config
from ratsnest.kicad_env import bootstrap_kicad


class KicadHostError(RuntimeError):
    """The in-process host could not be bootstrapped."""


_host = None


def get_host(config: Config):
    """Singleton in-process KiCADInterface from the vendored server."""
    global _host
    if _host is not None:
        return _host
    if not bootstrap_kicad(config.kicad_python):
        raise KicadHostError(
            "pcbnew unavailable — cannot host KiCad skills in-process")
    if not config.mcp_server_dir:
        raise KicadHostError(
            "KiCAD-MCP-Server dir not found (RATSNEST_MCP_SERVER)")
    py_dir = str(config.mcp_server_dir / "python")
    if py_dir not in sys.path:
        sys.path.insert(0, py_dir)
    import importlib
    module = importlib.import_module("kicad_interface")
    _host = module.KiCADInterface()
    from ratsnest.mcp_exec.hotfixes import apply_hotfixes
    apply_hotfixes(_host, config)
    return _host
