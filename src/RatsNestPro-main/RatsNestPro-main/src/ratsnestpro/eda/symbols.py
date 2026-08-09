"""Resolve KiCAD symbol libraries to real pin geometry — new + legacy formats.

This is an adapter layer over the vendored ``sexpr`` engine. It deliberately
lives outside ``eda/vendor`` so the vendored kicad-mcp-py core stays untouched.

Two on-disk library layouts are supported transparently:

* **Legacy single-file** — ``<nick>.kicad_sym`` is one S-expression holding
  every symbol in the library. ``(extends "base")`` inheritance resolves
  within that same file.
* **New directory format** (KiCad 10, symbol-lib ``version 20251024``) —
  ``<nick>.kicad_symdir/`` is a directory with **one ``<symbol>.kicad_sym``
  file per symbol**. Here a derived part such as ``ATmega328P-A`` carries no
  pins of its own and inherits them from a base symbol
  (``ATmega48PV-10A``) that lives in a **sibling file** in the same directory.
  Cross-file ``extends`` resolution is handled here.

A schematic ``lib_id`` looks like ``Device:R`` — the part before the colon is
the library nickname, the part after is the symbol name.

Pin coordinates are returned in the symbol's own frame (Y axis up), matching
the vendored ``symbol_lib`` convention; use ``transform_pin`` to map them into
schematic space once a placement position/rotation/mirror is known.
"""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from ratsnestpro.eda.library_roots import generated_library_roots
from ratsnestpro.eda.vendor.sexpr import Atom, find_all, find_first, loads, tag_of

# Re-export the placement transform so callers get one import site for symbol
# geometry helpers. It is pure math and safe to reuse from the vendored layer.
from ratsnestpro.eda.vendor.symbol_lib import transform_pin  # noqa: F401

__all__ = [
    "symbol_roots",
    "resolve_symbol",
    "symbol_pins",
    "symbol_properties",
    "symbol_info",
    "symbol_definition",
    "transform_pin",
]

_SYMDIR_SUFFIX = ".kicad_symdir"
_SYMFILE_SUFFIX = ".kicad_sym"
_MAX_EXTENDS_DEPTH = 8
_SYMBOL_CACHE_LOCK = threading.RLock()


def _root_signature() -> tuple[str, ...]:
    """Return the ordered roots that determine symbol lookup results."""

    return tuple(str(root) for root in symbol_roots())


def invalidate_symbol_caches() -> None:
    """Clear parsed and derived symbol data after an in-place library change."""

    with _SYMBOL_CACHE_LOCK:
        _load_lib_node.cache_clear()
        _locate_symbol_for.cache_clear()
        _symbol_pins_for.cache_clear()
        _symbol_properties_for.cache_clear()


def symbol_roots() -> list[Path]:
    """Directories that may contain symbol libraries, in priority order.

    An explicit ``KICAD_SYMBOL_DIR`` (os.pathsep-separated) is authoritative.
    KiCad install locations are discovered only when it is not configured.
    Only existing directories are returned.
    """
    # Evidence-generated workspace libraries take priority over installed
    # libraries so their exact, provenance-bound device identity wins.
    roots: list[Path] = list(generated_library_roots())
    env = os.environ.get("KICAD_SYMBOL_DIR")
    if env:
        roots.extend(Path(p) for p in env.split(os.pathsep) if p)
    else:
        # Fall back to KiCad install dirs (vendored discovery).
        from ratsnestpro.eda.vendor.symbol_lib import symbol_roots as _vendor_roots

        roots.extend(_vendor_roots())
    # De-duplicate, keep order, keep only existing dirs.
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen and r.exists():
            seen.add(key)
            out.append(r)
    return out


def resolve_symbol(lib_id: str) -> Path | None:
    """Resolve ``Lib:Name`` to the ``.kicad_sym`` file that defines it.

    For the legacy layout this is ``<root>/<nick>.kicad_sym``; for the new
    directory layout it is ``<root>/<nick>.kicad_symdir/<name>.kicad_sym``.
    The symbol must actually be present in the file — a legacy library that
    exists but does not contain ``Name`` does not match. Returns ``None`` when
    the symbol cannot be located.
    """
    located = _locate_symbol(lib_id)
    return located[2] if located else None


def _read_lib_node(path_str: str) -> list | None:
    """Parse one ``.kicad_sym`` file without retaining the full tree."""
    path = Path(path_str)
    try:
        node = loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(node, list) or tag_of(node) != "kicad_symbol_lib":
        return None
    return node


@lru_cache(maxsize=256)
def _load_lib_node(path_str: str) -> list | None:
    """Parse a symbol library and cache files used for actual part lookup."""
    return _read_lib_node(path_str)


def _find_symbol_def(lib_node: list, name: str) -> list | None:
    """Return the top-level ``(symbol "name" ...)`` node, or ``None``."""
    for sym in find_all(lib_node, "symbol"):
        if len(sym) > 1 and str(sym[1]) == name:
            return sym
    return None


def _first_symbol_def(lib_node: list) -> list | None:
    """Return the first top-level symbol in a library node (new-format file)."""
    syms = find_all(lib_node, "symbol")
    return syms[0] if syms else None


def _pins_in(symbol: list) -> list[dict[str, Any]]:
    """Collect pins from a symbol and its nested unit sub-symbols."""
    pins: list[dict[str, Any]] = []
    for pin in find_all(symbol, "pin"):
        at = find_first(pin, "at")
        num = find_first(pin, "number")
        name = find_first(pin, "name")
        etype = str(pin[1]) if len(pin) > 1 else "passive"
        pins.append(
            {
                "number": str(num[1]) if num and len(num) > 1 else "",
                "name": str(name[1]) if name and len(name) > 1 else "",
                "type": etype,
                "x": Atom(str(at[1])).as_float() if at else 0.0,
                "y": Atom(str(at[2])).as_float() if at and len(at) > 2 else 0.0,
                "angle": Atom(str(at[3])).as_float() if at and len(at) > 3 else 0.0,
            }
        )
    for sub in find_all(symbol, "symbol"):  # unit sub-symbols (e.g. R_1_1)
        pins.extend(_pins_in(sub))
    return pins


@lru_cache(maxsize=8192)
def _locate_symbol_for(
    roots: tuple[str, ...],
    lib_id: str,
) -> tuple[list, str, Path] | None:
    """Locate a symbol node by ``Lib:Name`` across both layouts.

    Returns ``(symbol_node, layout, path)`` where ``layout`` is ``"file"``
    (legacy) or ``"dir"`` (new directory format) and ``path`` is the
    ``.kicad_sym`` file it was found in. Returns ``None`` when not found. The
    symbol must genuinely exist in the file — a legacy library present on disk
    but lacking ``Name`` is skipped.
    """
    if ":" not in lib_id:
        return None
    nick, name = lib_id.split(":", 1)
    for root_str in roots:
        root = Path(root_str)
        legacy = root / f"{nick}{_SYMFILE_SUFFIX}"
        if legacy.is_file():
            lib_node = _load_lib_node(str(legacy))
            if lib_node is not None:
                sym = _find_symbol_def(lib_node, name)
                if sym is not None:
                    return sym, "file", legacy
        symdir = root / f"{nick}{_SYMDIR_SUFFIX}"
        if symdir.is_dir():
            per_symbol = symdir / f"{name}{_SYMFILE_SUFFIX}"
            if per_symbol.is_file():
                lib_node = _load_lib_node(str(per_symbol))
                if lib_node is not None:
                    # New-format files hold one symbol; match by name, else take first.
                    sym = _find_symbol_def(lib_node, name) or _first_symbol_def(lib_node)
                    if sym is not None:
                        return sym, "dir", per_symbol
    return None


def _locate_symbol(lib_id: str) -> tuple[list, str, Path] | None:
    """Single-flight cached symbol lookup for the current root signature."""

    with _SYMBOL_CACHE_LOCK:
        return _locate_symbol_for(_root_signature(), lib_id)


def _extends_base(symbol: list) -> str | None:
    ext = find_first(symbol, "extends")
    if ext and len(ext) > 1:
        return str(ext[1])
    return None


def _pins_for(
    roots: tuple[str, ...],
    nick: str,
    name: str,
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Resolve pins for a symbol, following ``extends`` across files if needed."""
    if depth > _MAX_EXTENDS_DEPTH:
        return []
    found = _locate_symbol_for(roots, f"{nick}:{name}")
    if found is None:
        return []
    symbol, _layout, _path = found
    pins = _pins_in(symbol)
    if pins:
        return pins
    # Derived part with no pins of its own → inherit from the base symbol.
    # In both layouts the base lives under the same library nickname (legacy:
    # same file; new: a sibling <base>.kicad_sym), so recursion handles both.
    base = _extends_base(symbol)
    if base:
        return _pins_for(roots, nick, base, depth + 1)
    return []


@lru_cache(maxsize=8192)
def _symbol_pins_for(
    roots: tuple[str, ...],
    lib_id: str,
) -> tuple[tuple[str, str, str, float, float, float], ...] | None:
    """Immutable cached pin metadata for one root signature and symbol."""

    if ":" not in lib_id:
        return None
    # A real mechanical symbol may intentionally have no electrical pins.
    # Keep that distinct from an unresolved library ID so downstream planning
    # does not treat installed mounting holes and graphics as unknown devices.
    if _locate_symbol_for(roots, lib_id) is None:
        return None
    nick, name = lib_id.split(":", 1)
    pins = _pins_for(roots, nick, name)
    seen: dict[str, dict[str, Any]] = {}
    for p in pins:
        num = p["number"]
        if num and num not in seen:
            seen[num] = p
    return tuple(
        (
            str(pin["number"]),
            str(pin["name"]),
            str(pin["type"]),
            float(pin["x"]),
            float(pin["y"]),
            float(pin["angle"]),
        )
        for pin in seen.values()
    )


def symbol_pins(lib_id: str) -> list[dict[str, Any]] | None:
    """Return the pins for ``lib_id`` (symbol-local coords), or ``None``.

    Follows ``(extends "base")`` inheritance across files. Pins are
    de-duplicated by pin number, keeping the first occurrence. A fresh public
    value is returned so callers cannot mutate the metadata cache. An installed
    pinless symbol returns ``[]``; ``None`` is reserved for an unresolved
    library ID.
    """

    with _SYMBOL_CACHE_LOCK:
        pins = _symbol_pins_for(_root_signature(), lib_id)
    if pins is None:
        return None
    return [
        {
            "number": number,
            "name": name,
            "type": pin_type,
            "x": x,
            "y": y,
            "angle": angle,
        }
        for number, name, pin_type, x, y, angle in pins
    ]


def _properties_for(
    roots: tuple[str, ...],
    nick: str,
    name: str,
    depth: int = 0,
) -> dict[str, str]:
    """Resolve symbol properties, following ``extends`` inheritance."""
    if depth > _MAX_EXTENDS_DEPTH:
        return {}
    found = _locate_symbol_for(roots, f"{nick}:{name}")
    if found is None:
        return {}
    symbol, _layout, _path = found
    inherited: dict[str, str] = {}
    base = _extends_base(symbol)
    if base:
        inherited = _properties_for(roots, nick, base, depth + 1)
    for prop in find_all(symbol, "property"):
        if len(prop) > 2:
            inherited[str(prop[1])] = str(prop[2])
    return inherited


@lru_cache(maxsize=8192)
def _symbol_properties_for(
    roots: tuple[str, ...],
    lib_id: str,
) -> tuple[tuple[str, str], ...]:
    """Immutable cached properties for one root signature and symbol."""

    if ":" not in lib_id:
        return ()
    nick, name = lib_id.split(":", 1)
    return tuple(_properties_for(roots, nick, name).items())


def symbol_properties(lib_id: str) -> dict[str, str]:
    """Return library-defined symbol properties such as ``Footprint``."""

    with _SYMBOL_CACHE_LOCK:
        properties = _symbol_properties_for(_root_signature(), lib_id)
    return dict(properties)


def symbol_info(lib_id: str) -> dict[str, Any] | None:
    """Summary dict for a symbol: path, pins, properties, and pin count."""
    pins = symbol_pins(lib_id)
    if pins is None:
        return None
    path = resolve_symbol(lib_id)
    return {
        "lib_id": lib_id,
        "path": str(path) if path else None,
        "pins": pins,
        "pin_count": len(pins),
        "properties": symbol_properties(lib_id),
    }


def _has_graphic_subsymbols(symbol: list) -> bool:
    """True if the symbol carries its own body/unit graphics (renderable)."""
    return any(isinstance(c, list) and tag_of(c) == "symbol" for c in symbol[2:])


def _rename_subsymbols(children: list, old_name: str, new_name: str) -> list:
    """Copy graphic unit sub-symbols, re-prefixing their name to ``new_name``.

    Library unit sub-symbols are named ``<SymbolName>_<unit>_<style>`` (e.g.
    ``ATmega48PV-10A_0_1``). When embedding a derived symbol we re-prefix them
    to the derived name so KiCad associates the graphics with the right symbol.
    """
    out: list = []
    for ch in children:
        if isinstance(ch, list) and tag_of(ch) == "symbol" and len(ch) > 1:
            sub = list(ch)
            subname = str(ch[1])
            if subname.startswith(old_name + "_"):
                sub[1] = new_name + subname[len(old_name):]
            out.append(sub)
    return out


def _resolve_flat(nick: str, name: str, depth: int = 0) -> list | None:
    """Return a self-contained symbol node, flattening ``extends`` graphics.

    A derived part (``(extends "base")`` with no graphics of its own) inherits
    the base symbol's body/pin graphics. For embedding into a schematic's
    ``lib_symbols`` cache we must produce a renderable, self-contained node, so
    the base graphics are cloned in under the derived name.
    """
    if depth > _MAX_EXTENDS_DEPTH:
        return None
    found = _locate_symbol(f"{nick}:{name}")
    if found is None:
        return None
    symbol, _layout, _path = found
    if _has_graphic_subsymbols(symbol) or _extends_base(symbol) is None:
        return symbol  # already renderable / leaf symbol
    base = _extends_base(symbol)
    base_flat = _resolve_flat(nick, base, depth + 1) if base else None
    if base_flat is None:
        return symbol  # cannot flatten — embed as-is (better than nothing)
    # Keep the derived symbol's own metadata (properties, pin_names, ...) but
    # drop its (extends ...) marker, then graft in the base's renamed graphics.
    head = [
        c
        for c in symbol[2:]
        if not (isinstance(c, list) and tag_of(c) in ("symbol", "extends"))
    ]
    graphics = _rename_subsymbols(base_flat[2:], str(base_flat[1]), name)
    # Reuse the derived node's own tag token (symbol[0]) — a bare-symbol Atom —
    # rather than a plain str, so the serializer writes `(symbol ...)` as a bare
    # token and tag_of recognises it after a round-trip.
    return [symbol[0], name, *head, *graphics]


def symbol_definition(lib_id: str) -> list | None:
    """Return a self-contained ``(symbol "Lib:Name" ...)`` node for embedding.

    The returned node is renderable on its own (``extends`` inheritance is
    flattened) and its top-level name is set to the full ``lib_id`` exactly as
    KiCad stores entries in a schematic's ``lib_symbols`` cache. Returns
    ``None`` when the symbol cannot be located.
    """
    if ":" not in lib_id:
        return None
    nick, name = lib_id.split(":", 1)
    flat = _resolve_flat(nick, name)
    if flat is None:
        return None
    node = list(flat)
    node[1] = lib_id  # cache entries are keyed by the full lib_id
    return node


def _demo(argv: list[str]) -> int:  # pragma: no cover - CLI convenience
    if not argv:
        print("usage: python -m ratsnestpro.eda.symbols <Lib:Name> [...]")
        return 2
    rc = 0
    for lib_id in argv:
        info = symbol_info(lib_id)
        if info is None:
            print(f"{lib_id}: NOT FOUND")
            rc = 1
            continue
        print(f"{lib_id}  ({info['pin_count']} pins)  <- {info['path']}")
        for p in info["pins"]:
            print(
                f"  pin {p['number']:>4}  {p['name']:<12} {p['type']:<12} "
                f"@ ({p['x']:.3f}, {p['y']:.3f}) angle={p['angle']:.0f}"
            )
    return rc


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_demo(sys.argv[1:]))
