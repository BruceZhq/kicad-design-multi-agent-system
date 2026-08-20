"""Materialize pipeline designs into real KiCad schematics.

The adaptive pipeline embeds library symbols, places labels on real pin
coordinates, emits explicit no-connect markers, and drives power nets with
``PWR_FLAG`` symbols so the resulting schematic can be checked by
``kicad-cli``.
"""

from __future__ import annotations

from typing import Any

from ratsnestpro.domain.contracts import BoardPlan, CircuitIR
from ratsnestpro.eda import SchematicDoc
from ratsnestpro.eda import symbols as _symbols

_POWER_NET_NAMES = {"GND", "GROUND", "VSS"}


def _is_power(net_name: str, supply_net: str) -> bool:
    return net_name.upper() in _POWER_NET_NAMES or net_name == supply_net


def materialize_design(
    ir: CircuitIR,
    board: BoardPlan,
    supply_net: str = "3V3",
) -> SchematicDoc:
    """Build a SchematicDoc from the IR and placement plan."""
    doc = SchematicDoc.new()
    placements = {p.ref: p for p in board.placements}

    # Place components at their planned coordinates.
    for comp in ir.components:
        pl = placements.get(comp.ref)
        x = pl.x_mm if pl else 20.0
        y = pl.y_mm if pl else 20.0
        rot = pl.rotation_deg if pl else 0.0
        doc.add_component(
            lib_id=comp.symbol,
            reference=comp.ref,
            value=comp.value,
            x=x,
            y=y,
            rotation=rot,
            footprint=comp.footprint,
            dnp=bool(getattr(comp, "dnp", False)),
        )

    # Net labels on a collision-free grid in a region to the right of the board.
    # (Positions are only for structural validity + label-netlist round-trip.)
    label_x0 = 120.0
    label_y0 = 10.0
    step = 2.54
    cols = 40
    counter = 0
    for net in ir.nets:
        for _pin in net.pins:
            col = counter % cols
            row = counter // cols
            x = label_x0 + col * step
            y = label_y0 + row * step
            doc.add_net_label(net.name, x, y)
            counter += 1

    # Power symbols for supply + ground (visual/GUI intent).
    py = label_y0
    for net in ir.nets:
        if _is_power(net.name, supply_net):
            doc.add_power_symbol(net.name, 110.0, py)
            py += step

    return doc



def materialize_pinmapped(
    components: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    no_connect_pins: list[dict[str, Any]] | None = None,
    supply_nets: list[str] | None = None,
    ground_net: str = "GND",
) -> SchematicDoc:
    """Build a SchematicDoc from pipeline artifacts, embedding real pin geometry.

    ``components``: {ref, symbol, value, footprint, x, y, rotation}.
    ``nets``: {name, pins:[{ref, number}]}.

    Each net pin's label is placed at the *actual* pin coordinate — the
    component placement transformed by the symbol's real pin geometry
    (``symbols.symbol_pins`` + ``transform_pin``) — rather than an arbitrary
    grid. This makes the sheet geometrically faithful and lets the label
    netlist round-trip to the intended connectivity. When symbol geometry is
    unavailable, labels fall back to a per-net grid so name-based connectivity
    still round-trips.
    """
    supply_nets = supply_nets or []
    no_connect_pins = no_connect_pins or []
    doc = SchematicDoc.new()

    placements: dict[str, tuple[float, float, float]] = {}
    pins_cache: dict[str, list[dict[str, Any]] | None] = {}
    for c in components:
        ref = str(c["ref"])
        symbol = str(c["symbol"])
        x = float(c.get("x", 20.0))
        y = float(c.get("y", 20.0))
        rot = float(c.get("rotation", 0.0))
        placements[ref] = (x, y, rot)
        if symbol not in pins_cache:
            pins_cache[symbol] = _symbols.symbol_pins(symbol)
        dnp = bool(c.get("dnp", False))
        unresolved = bool(c.get("unresolved", False))
        release_ready = c.get("release_ready")
        resolution_status = str(c.get("resolution_status", "")).strip()
        resolution_detail = str(c.get("resolution_detail", "")).strip()
        requested_identity = str(c.get("requested_identity", "")).strip()
        identity_mode = str(c.get("identity_mode", "")).strip()
        identity_provenance = str(c.get("identity_provenance", "")).strip()
        release_proven = (
            release_ready is True
            and resolution_status in {
                "installed_exact",
                "installed_qualified_validated",
                "replaceable_grounded",
            }
            and not dnp
            and not unresolved
            and not symbol.startswith("RatsNestPlaceholder:")
        )
        nonrelease = not release_proven
        audit_properties: dict[str, str] = {}
        if nonrelease or resolution_status or requested_identity:
            audit_properties = {
                "DNP": "yes" if dnp else "no",
                "RatsNestUnresolved": "yes" if unresolved else "no",
                "RatsNestReleaseReady": "no" if nonrelease else "yes",
            }
            if resolution_status:
                audit_properties["RatsNestStatus"] = resolution_status
            if resolution_detail:
                audit_properties["RatsNestResolutionDetail"] = resolution_detail
            if requested_identity:
                audit_properties["RatsNestRequestedIdentity"] = requested_identity
            if identity_mode:
                audit_properties["RatsNestIdentityMode"] = identity_mode
            if identity_provenance:
                audit_properties[
                    "RatsNestIdentityProvenance"
                ] = identity_provenance
        doc.add_component(
            lib_id=symbol, reference=ref, value=str(c.get("value", "")),
            x=x, y=y, rotation=rot, footprint=str(c.get("footprint", "")),
            dnp=dnp,
            properties=audit_properties,
        )

    ref_symbol = {str(c["ref"]): str(c["symbol"]) for c in components}
    fallback_x, fallback_y, step = 200.0, 10.0, 2.54
    counter = 0
    connected_pins: set[str] = set()
    first_net_coord: dict[str, tuple[float, float]] = {}
    power_output_nets: set[str] = set()
    for net in nets:
        name = str(net["name"])
        for pin in net.get("pins") or []:
            ref = str(pin["ref"])
            number = str(pin["number"])
            connected_pins.add(f"{ref}:{number}")
            coord = _pin_coord(ref, number, placements, ref_symbol, pins_cache)
            if coord is None:
                coord = (fallback_x + step * (counter % 40), fallback_y + step * (counter // 40))
                counter += 1
            doc.add_net_label(name, coord[0], coord[1])
            first_net_coord.setdefault(name, coord)
            symbol = ref_symbol.get(ref)
            if symbol is not None and any(
                str(candidate.get("number", "")) == number
                and str(candidate.get("type", "")).lower() == "power_out"
                for candidate in (pins_cache.get(symbol) or [])
            ):
                power_output_nets.add(name)

    for pin in no_connect_pins:
        ref = str(pin["ref"])
        number = str(pin["number"])
        if f"{ref}:{number}" in connected_pins:
            continue
        coord = _pin_coord(ref, number, placements, ref_symbol, pins_cache)
        if coord is not None:
            doc.add_no_connect(coord[0], coord[1])

    seen_power: set[str] = set()
    for name in [ground_net, *supply_nets]:
        if name and name not in seen_power and name not in power_output_nets:
            coord = first_net_coord.get(name)
            if coord is not None:
                # A PWR_FLAG placed on the already-labelled net gives KiCad ERC
                # a real power-output driver. The old isolated rail symbols
                # created dangling pins and false power-not-driven errors.
                doc.add_power_symbol("PWR_FLAG", coord[0], coord[1])
            seen_power.add(name)
    doc.embed_lib_symbols()
    return doc


def _pin_coord(
    ref: str,
    number: str,
    placements: dict[str, tuple[float, float, float]],
    ref_symbol: dict[str, str],
    pins_cache: dict[str, list[dict[str, Any]] | None],
) -> tuple[float, float] | None:
    place = placements.get(ref)
    symbol = ref_symbol.get(ref)
    if place is None or symbol is None:
        return None
    pins = pins_cache.get(symbol)
    if not pins:
        return None
    for p in pins:
        if str(p["number"]) == number:
            px, py, rot = place
            return _symbols.transform_pin(px, py, rot, None, float(p["x"]), float(p["y"]))
    return None
