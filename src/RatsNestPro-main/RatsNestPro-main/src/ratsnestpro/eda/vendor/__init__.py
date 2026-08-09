"""Vendored core of ``kicad-mcp-py`` (MIT).

These modules edit ``.kicad_sch`` / ``.kicad_pcb`` S-expression files directly
and drive ``kicad-cli``; they carry no MCP/JSON-RPC layer. RatsNestPro imports
them **in-process** as a typed library through :mod:`ratsnestpro.eda.adapter`,
which is more professional and efficient than round-tripping through MCP.

Upstream: kicad-mcp-py (github, MIT). Only the core modules are vendored:
sexpr, schematic, pcb, connectivity, footprint, symbol_lib, library,
kicad_cli, kicad_paths, review, jlcpcb, fsutil. The MCP server/router/toolset
layer is intentionally not vendored.

Do not edit these files except to track upstream fixes.
"""
