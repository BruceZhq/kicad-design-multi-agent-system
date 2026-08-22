"""Grounded part selection (phase-3 slice).

Component selection must be *grounded* in a real catalog, never invented by the
LLM from memory. This module queries the local JLCPCB SQLite cache (the same
cache the vendored kicad-mcp-py uses) to propose candidate parts for the values
in a Circuit IR. It degrades gracefully: if no local database is present the
selector reports ``available == False`` and returns no candidates, so the agent
still runs.

The database location follows the vendored default (``KICAD_MCP_HOME`` /
``jlcpcb.sqlite``); set ``KICAD_MCP_HOME`` to point at your own cache.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ratsnestpro.domain.contracts import CircuitIR
from ratsnestpro.eda.vendor import jlcpcb

_PACKAGE = re.compile(r"_(\d{4})_")  # imperial code inside a footprint name


@dataclass
class PartCandidate:
    lcsc: str
    mpn: str
    description: str
    package: str
    basic: bool
    stock: int
    price: float


def _package_from_footprint(footprint: str) -> str | None:
    m = _PACKAGE.search(footprint)
    return m.group(1) if m else None


def _to_candidate(row: dict) -> PartCandidate:
    return PartCandidate(
        lcsc=str(row.get("lcsc", "")),
        mpn=str(row.get("mpn", "")),
        description=str(row.get("description", "")),
        package=str(row.get("package", "")),
        basic=bool(row.get("basic", 0)),
        stock=int(row.get("stock", 0) or 0),
        price=float(row.get("price", 0.0) or 0.0),
    )


class PartSelector:
    """Read-only, grounded queries over the local JLCPCB cache."""

    def available(self) -> bool:
        s = jlcpcb.stats()
        return bool(s.get("exists")) and int(s.get("part_count", 0) or 0) > 0

    def stats(self) -> dict:
        return jlcpcb.stats()

    def search(self, query: str, limit: int = 10) -> list[PartCandidate]:
        return [_to_candidate(r) for r in jlcpcb.search(query, limit=limit)]

    def suggest(self, value: str, footprint: str = "", limit: int = 5) -> list[PartCandidate]:
        package = _package_from_footprint(footprint) if footprint else None
        rows = jlcpcb.suggest_alternatives(value, package=package, limit=limit)
        if not rows and package:
            rows = jlcpcb.suggest_alternatives(value, package=None, limit=limit)
        return [_to_candidate(r) for r in rows]

    def ground_ir(self, ir: CircuitIR, limit: int = 3) -> dict[str, list[PartCandidate]]:
        """Propose grounded candidates per component value. Empty when the cache
        is unavailable — the agent must not fabricate MPNs."""
        out: dict[str, list[PartCandidate]] = {}
        if not self.available():
            return out
        for comp in ir.components:
            if comp.role == "mounting_hole" or not comp.value:
                continue
            cands = self.suggest(comp.value, comp.footprint, limit=limit)
            if cands:
                out[comp.ref] = cands
        return out
