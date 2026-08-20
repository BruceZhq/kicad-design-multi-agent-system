"""EDA adapter layer: typed, in-process primitives over the vendored
kicad-mcp-py core (no MCP round-trip)."""

from ratsnestpro.eda.adapter import (
    ErcResult,
    SchematicDoc,
    kicad_cli_available,
    run_erc,
)

__all__ = [
    "ErcResult",
    "SchematicDoc",
    "kicad_cli_available",
    "run_erc",
]
