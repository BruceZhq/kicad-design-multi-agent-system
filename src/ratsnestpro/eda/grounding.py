"""Ground LLM-proposed symbol/footprint names to real KiCad library IDs.

An LLM proposes plausible-looking library IDs (``Device:Resistor``,
``Resistor_SMD:R_0603`` or an abbreviated MCU nickname) that frequently do not
match the *exact* names KiCad ships (``Device:R``, ``R_0603_1608Metric``,
an installed canonical library identifier). This module maps a proposal to a real,
existing library ID so the design can proceed — **without ever fabricating**:
if no real match is found the original string is returned unchanged, so the
bottom-line selection check still fails closed on a genuinely bad part.

Strategy (symbols): exact resolve → small EE name/lib alias (semantic maps that
lexical fuzzing cannot bridge, e.g. "Resistor"→"R") → fuzzy/substring match of
the name within the (aliased) library, then across all libraries.

Strategy (footprints): exact pad lookup → substring search (the vendored
search already substring-matches the module stem, so ``R_0603`` finds
``R_0603_1608Metric``) → closest match, preferring the proposed library.
"""

from __future__ import annotations

import difflib
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TextIO

from ratsnestpro.eda import footprints, symbols
from ratsnestpro.eda.vendor.library import footprint_roots, search_footprints
from ratsnestpro.eda.vendor.sexpr import find_all

__all__ = [
    "invalidate_library_indexes",
    "footprint_index",
    "ground_symbol",
    "ground_footprint",
    "symbol_identity_match_kind",
    "symbol_index",
]

_FOOTPRINT_INDEX_LOCK = threading.RLock()
_SYMBOL_INDEX_LOCK = threading.RLock()


def invalidate_library_indexes() -> None:
    """Drop name indexes after a workspace-local library changes."""

    global _SYMBOL_SEARCH_INDEX, _SYMBOL_SEARCH_SOURCE
    with _SYMBOL_INDEX_LOCK, _FOOTPRINT_INDEX_LOCK:
        _footprint_index_for.cache_clear()
        _symbol_index_for.cache_clear()
        _SYMBOL_SEARCH_SOURCE = None
        _SYMBOL_SEARCH_INDEX = None
        symbols.invalidate_symbol_caches()

# Semantic name aliases: descriptive LLM name (lower) -> KiCad symbol name.
# These are *normalization* aids (not electrical rules); lexical fuzzing alone
# cannot get "Resistor" -> "R".
_NAME_ALIASES: dict[str, str] = {
    "resistor": "R",
    "res": "R",
    "capacitor": "C",
    "cap": "C",
    "capacitor_polarized": "C_Polarized",
    "capacitor_electrolytic": "C_Polarized",
    "inductor": "L",
    "ferrite_bead": "FerriteBead",
    "polyfuse": "Polyfuse_Small",
    "polyfuse_smd": "Polyfuse_Small",
    "fuse": "Fuse",
    "tvs": "D_TVS",
    "tvs_diode": "D_TVS",
    "diode": "D",
    "zener": "D_Zener",
    "schottky": "D_Schottky",
    "led": "LED",
    "switch": "SW_Push",
    "button": "SW_Push",
    "pushbutton": "SW_Push",
}
# Library-nick aliases: LLM lib (lower) -> real KiCad library nick.
_LIB_ALIASES: dict[str, str] = {
    "mcu_atmega": "MCU_Microchip_ATmega",
    "mcu_microchip": "MCU_Microchip_ATmega",
    "connector_usb": "Connector",
    "connector_pinheader_2.54mm": "Connector_Generic",
    "connector_pinheader": "Connector_Generic",
    "switch": "Switch",
}


def _tokens(name: str) -> set[str]:
    """Tokenize a library id/name for overlap scoring.

    Splits on non-alphanumeric AND on alpha/digit boundaries so ``C0603`` →
    ``{c, 0603}`` and ``R_0603_1608Metric`` → ``{r, 0603, 1608, metric}``.
    Connector counts are zero-pad normalized so ``2x03`` matches ``02x03``."""
    toks: set[str] = set()
    for raw in re.split(r"[^a-z0-9]+", name.lower()):
        if not raw:
            continue
        toks.add(raw)
        for piece in re.findall(r"[a-z]+|\d+", raw):  # split c0603 -> c, 0603
            toks.add(piece)
    for t in list(toks):  # NxM connector-count normalization
        m = re.fullmatch(r"(\d+)x(\d+)", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            toks.add(f"{a}x{b}")
            toks.add(f"{a:02d}x{b:02d}")
    return toks


@dataclass(frozen=True)
class _SymbolEntry:
    full_id: str
    library: str
    name: str
    normalized_length: int
    tokens: frozenset[str]


@dataclass(frozen=True)
class _SymbolSearchIndex:
    ids: frozenset[str]
    by_full_casefold: dict[str, tuple[str, ...]]
    by_name_casefold: dict[str, tuple[_SymbolEntry, ...]]
    by_library: dict[str, tuple[_SymbolEntry, ...]]
    by_normalized_length: dict[int, tuple[_SymbolEntry, ...]]
    by_token: dict[str, tuple[_SymbolEntry, ...]]
    by_trigram: dict[str, tuple[_SymbolEntry, ...]]


_SYMBOL_SEARCH_SOURCE: tuple[str, ...] | None = None
_SYMBOL_SEARCH_INDEX: _SymbolSearchIndex | None = None


def _name_trigrams(name: str) -> set[str]:
    normalized = "".join(char.lower() for char in name if char.isalnum())
    if len(normalized) < 3:
        return set()
    return {
        normalized[index:index + 3]
        for index in range(len(normalized) - 2)
    }


def _build_symbol_search_index(ids: tuple[str, ...]) -> _SymbolSearchIndex:
    """Build reusable exact, library, token, and fuzzy candidate indexes."""

    by_full_casefold: dict[str, list[str]] = {}
    by_name_casefold: dict[str, list[_SymbolEntry]] = {}
    by_library: dict[str, list[_SymbolEntry]] = {}
    by_normalized_length: dict[int, list[_SymbolEntry]] = {}
    by_token: dict[str, list[_SymbolEntry]] = {}
    by_trigram: dict[str, list[_SymbolEntry]] = {}
    for full_id in ids:
        library, separator, name = full_id.partition(":")
        if not separator or not library or not name:
            continue
        normalized_length = len(
            "".join(char for char in name if char.isalnum())
        )
        entry = _SymbolEntry(
            full_id=full_id,
            library=library.casefold(),
            name=name,
            normalized_length=normalized_length,
            tokens=frozenset(_tokens(name)),
        )
        by_full_casefold.setdefault(full_id.casefold(), []).append(full_id)
        by_name_casefold.setdefault(name.casefold(), []).append(entry)
        by_library.setdefault(entry.library, []).append(entry)
        by_normalized_length.setdefault(normalized_length, []).append(entry)
        for token in entry.tokens:
            by_token.setdefault(token, []).append(entry)
        for trigram in _name_trigrams(name):
            by_trigram.setdefault(trigram, []).append(entry)

    def frozen(
        values: dict[str, list[_SymbolEntry]],
    ) -> dict[str, tuple[_SymbolEntry, ...]]:
        return {
            key: tuple(sorted(entries, key=lambda entry: entry.full_id))
            for key, entries in values.items()
        }

    return _SymbolSearchIndex(
        ids=frozenset(ids),
        by_full_casefold={
            key: tuple(sorted(full_ids))
            for key, full_ids in by_full_casefold.items()
        },
        by_name_casefold=frozen(by_name_casefold),
        by_library=frozen(by_library),
        by_normalized_length={
            key: tuple(sorted(entries, key=lambda entry: entry.full_id))
            for key, entries in by_normalized_length.items()
        },
        by_token=frozen(by_token),
        by_trigram=frozen(by_trigram),
    )


def _symbol_search_index() -> _SymbolSearchIndex:
    """Return one search index for the current installed-symbol tuple."""

    global _SYMBOL_SEARCH_INDEX, _SYMBOL_SEARCH_SOURCE
    source = symbol_index()
    if not isinstance(source, tuple):
        source = tuple(source)
    with _SYMBOL_INDEX_LOCK:
        if source is not _SYMBOL_SEARCH_SOURCE:
            if _SYMBOL_SEARCH_SOURCE is None or source != _SYMBOL_SEARCH_SOURCE:
                _SYMBOL_SEARCH_INDEX = _build_symbol_search_index(source)
            _SYMBOL_SEARCH_SOURCE = source
        if _SYMBOL_SEARCH_INDEX is None:
            _SYMBOL_SEARCH_INDEX = _build_symbol_search_index(source)
        return _SYMBOL_SEARCH_INDEX


def _unique_full_id(
    entries: tuple[_SymbolEntry, ...],
    requested_name: str,
) -> str | None:
    """Return one exact/wildcard-compatible canonical symbol identity."""

    exact = [
        entry.full_id
        for entry in entries
        if symbol_identity_match_kind(requested_name, entry.name) == "exact"
    ]
    if len(exact) == 1:
        return exact[0]
    wildcard = [
        entry.full_id
        for entry in entries
        if symbol_identity_match_kind(requested_name, entry.name)
        == "kicad_wildcard"
    ]
    return wildcard[0] if len(wildcard) == 1 else None


def _token_candidates(
    index: _SymbolSearchIndex,
    requested: str,
    *,
    library: str | None = None,
) -> tuple[_SymbolEntry, ...]:
    candidates: set[_SymbolEntry] = set()
    for token in _tokens(requested):
        candidates.update(index.by_token.get(token, ()))
    if library is not None:
        candidates = {
            entry
            for entry in candidates
            if entry.library == library.casefold()
        }
    return tuple(sorted(candidates, key=lambda entry: entry.full_id))


def _fuzzy_candidates(
    index: _SymbolSearchIndex,
    requested: str,
    *,
    library: str | None = None,
) -> tuple[_SymbolEntry, ...]:
    candidates: set[_SymbolEntry] = set()
    for trigram in _name_trigrams(requested):
        candidates.update(index.by_trigram.get(trigram, ()))
    if library is not None:
        candidates = {
            entry
            for entry in candidates
            if entry.library == library.casefold()
        }
    return tuple(sorted(candidates, key=lambda entry: entry.full_id))


def _allows_fuzzy_symbol_grounding(library: str) -> bool:
    """Limit lexical substitution to KiCad's generic primitive families."""

    normalized = library.casefold()
    generic_families = {
        "connector",
        "device",
        "graphic",
        "jumper",
        "mechanical",
        "power",
        "simulation_spice",
        "switch",
    }
    return normalized in generic_families or any(
        normalized.startswith(f"{family}_")
        for family in generic_families
    )


def _best_by_tokens(requested: str, index: list[tuple[str, set[str]]]) -> str | None:
    """Pick the library id whose tokens best overlap ``requested``.

    Requires a *distinctive* overlap — either a token containing a digit
    (package size / pin count like ``0603``/``02x03``) or at least two shared
    tokens — so unrelated parts are not matched. Ties break toward the shortest
    id (the most generic variant)."""
    want = _tokens(requested)
    if not want:
        return None
    best_id: str | None = None
    best_score = 0.0
    for lib_id, toks in index:
        inter = want & toks
        if not inter:
            continue
        distinctive = any(any(ch.isdigit() for ch in t) for t in inter) or len(inter) >= 2
        if not distinctive:
            continue
        # Bonus when a requested token matches a WHOLE underscore-delimited
        # segment of the candidate — disambiguates e.g. imperial "0603"
        # (segment in ``C_0603_1608Metric``) from the metric 0603 that only
        # appears fused inside ``C_0201_0603Metric``.
        name = lib_id.partition(":")[2].lower()
        segs = {s for s in re.split(r"[^a-z0-9]+", name) if s}
        seg_bonus = len(want & segs)
        score = len(inter) + 2.0 * seg_bonus - 0.001 * len(lib_id)
        if score > best_score:
            best_score, best_id = score, lib_id
    return best_id


def _build_footprint_index(
    roots: tuple[str, ...],
) -> tuple[tuple[str, frozenset[str]], ...]:
    """All ``Lib:Footprint`` ids with pre-computed tokens for the given roots."""
    out: list[tuple[str, frozenset[str]]] = []
    for root_str in roots:
        for mod in Path(root_str).glob("*.pretty/*.kicad_mod"):
            lib_id = f"{mod.parent.stem}:{mod.stem}"
            out.append((lib_id, frozenset(_tokens(lib_id))))
    return tuple(out)


@lru_cache(maxsize=4)
def _footprint_index_for(
    roots: tuple[str, ...],
) -> tuple[tuple[str, frozenset[str]], ...]:
    return _build_footprint_index(roots)


def _footprint_index() -> tuple[tuple[str, frozenset[str]], ...]:
    """All ``Lib:Footprint`` ids with tokens (cached per root set)."""
    # An lru_cache can execute its wrapped function more than once for
    # concurrent misses. Lock outside the cached call so waiters re-check the
    # populated cache after the first build completes.
    with _FOOTPRINT_INDEX_LOCK:
        roots = tuple(str(r) for r in footprint_roots())
        return _footprint_index_for(roots)


def footprint_index() -> tuple[str, ...]:
    """Return all installed footprint IDs, deduplicated across library roots."""

    return tuple(sorted({lib_id for lib_id, _tokens in _footprint_index()}))


def _iter_sexpr_tokens(stream: TextIO) -> Iterator[tuple[str, str]]:
    """Yield the small token stream needed to index a symbol library."""

    state = "between"
    buf: list[str] = []
    escaped = False
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    for chunk in iter(lambda: stream.read(64 * 1024), ""):
        for char in chunk:
            if state == "between":
                if char.isspace() or char == "\ufeff":
                    continue
                if char == "(":
                    yield "lparen", char
                elif char == ")":
                    yield "rparen", char
                elif char == '"':
                    state = "string"
                    buf = []
                else:
                    state = "atom"
                    buf = [char]
                continue

            if state == "atom":
                if char.isspace():
                    yield "atom", "".join(buf)
                    state = "between"
                elif char in "()":
                    yield "atom", "".join(buf)
                    yield ("lparen" if char == "(" else "rparen"), char
                    state = "between"
                elif char == '"':
                    yield "atom", "".join(buf)
                    state = "string"
                    buf = []
                else:
                    buf.append(char)
                continue

            if escaped:
                buf.append(escapes.get(char, char))
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                yield "string", "".join(buf)
                state = "between"
            else:
                buf.append(char)

    if state == "atom":
        yield "atom", "".join(buf)
    elif state == "string":
        raise ValueError("unterminated quoted string")


def _stream_legacy_symbol_names(path: Path) -> tuple[str, ...] | None:
    """Read root-level symbol names without building a full parse tree.

    ``None`` asks the caller to use the compatibility parser.
    """

    canonical = _stream_canonical_symbol_names(path)
    if canonical is not None:
        return canonical

    depth = 0
    root_tag: str | None = None
    root_closed = False
    child_tag: str | None = None
    child_name_seen = False
    names: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for kind, value in _iter_sexpr_tokens(stream):
                if kind == "lparen":
                    if root_closed:
                        return None
                    if depth == 1:
                        child_tag = None
                        child_name_seen = False
                    depth += 1
                    continue
                if kind == "rparen":
                    if depth <= 0:
                        return None
                    if depth == 1:
                        root_closed = True
                    depth -= 1
                    continue
                if depth == 0:
                    return None
                if depth == 1 and root_tag is None:
                    if kind != "atom" or value != "kicad_symbol_lib":
                        return None
                    root_tag = value
                    continue
                if depth != 2:
                    continue
                if child_tag is None:
                    if kind != "atom":
                        return None
                    child_tag = value
                elif child_tag == "symbol" and not child_name_seen:
                    if kind != "string":
                        return None
                    names.append(value)
                    child_name_seen = True
    except (OSError, UnicodeError, ValueError):
        return None
    if depth != 0 or not root_closed or root_tag != "kicad_symbol_lib":
        return None
    return tuple(names)


def _decode_quoted_prefix(text: str) -> str | None:
    """Decode one KiCad quoted token at the start of ``text``."""

    if not text.startswith('"'):
        return None
    escaped = False
    decoded: list[str] = []
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    for char in text[1:]:
        if escaped:
            decoded.append(escapes.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return "".join(decoded)
        else:
            decoded.append(char)
    return None


def _stream_canonical_symbol_names(path: Path) -> tuple[str, ...] | None:
    """Fast-path canonical KiCad formatting using root-level line indentation."""

    names: list[str] = []
    indent: str | None = None
    root_closed = False
    try:
        with path.open("r", encoding="utf-8") as stream:
            first = stream.readline()
            if first.lstrip("\ufeff").strip() != "(kicad_symbol_lib":
                return None
            for line in stream:
                if line.startswith(")"):
                    root_closed = True
                    continue
                if indent is None:
                    match = re.match(r"^([ \t]+)\(", line)
                    if match is not None:
                        indent = match.group(1)
                if indent is None:
                    continue
                prefix = f"{indent}(symbol "
                symbol_form = f"{indent}(symbol"
                if not line.startswith(prefix):
                    if line.startswith(symbol_form):
                        # Generated/compatible KiCad may put the quoted name
                        # on the next line.  The token fallback handles it.
                        return None
                    continue
                name = _decode_quoted_prefix(line[len(prefix):])
                if name is None:
                    return None
                names.append(name)
    except (OSError, UnicodeError):
        return None
    if not root_closed or indent is None:
        return None
    return tuple(names)


def _build_symbol_index(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Build all available ``Lib:Name`` symbol IDs for the given roots."""

    ids: set[str] = set()
    for root_str in roots:
        root = Path(root_str)
        # New directory layout — nick from the dir, name from the file stem.
        for f in root.glob("*.kicad_symdir/*.kicad_sym"):
            nick = f.parent.name[: -len(".kicad_symdir")]
            ids.add(f"{nick}:{f.stem}")
        # Legacy single-file layout — stream names, with a compatibility
        # fallback for syntax the lightweight reader does not support.
        for f in root.glob("*.kicad_sym"):
            nick = f.stem
            names = _stream_legacy_symbol_names(f)
            if names is None:
                node = symbols._read_lib_node(str(f))
                if node is None:
                    continue
                names = tuple(
                    str(sym[1])
                    for sym in find_all(node, "symbol")
                    if len(sym) > 1 and str(sym[1])
                )
            ids.update(f"{nick}:{name}" for name in names if name)
    return tuple(sorted(ids))


@lru_cache(maxsize=4)
def _symbol_index_for(roots: tuple[str, ...]) -> tuple[str, ...]:
    """Cache one symbol index per root set."""

    return _build_symbol_index(roots)


def symbol_index() -> tuple[str, ...]:
    """All available ``Lib:Name`` symbol IDs across the configured roots.

    Handles both the legacy single-file layout and the new per-symbol directory
    layout. Keyed on the current roots for correct test isolation."""
    with _SYMBOL_INDEX_LOCK:
        roots = tuple(str(r) for r in symbols.symbol_roots())
        return _symbol_index_for(roots)


def _looks_like_order_code(name: str) -> bool:
    """Return whether ``name`` is a concrete manufacturer-style part code."""

    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    return (
        "_" not in name
        and len(compact) >= 5
        and any(char.isalpha() for char in compact)
        and any(char.isdigit() for char in compact)
    )


def symbol_identity_match_kind(requested: str, candidate: str) -> str | None:
    """Return a deterministic identity relation for an installed symbol name.

    KiCad uses a literal lowercase ``x`` in some library symbol names as a
    one-character ordering-code wildcard.  The candidate's original spelling
    is therefore significant: an uppercase ``X`` in a manufacturer part number
    remains a literal character.
    """

    wanted = "".join(
        char.lower()
        for char in requested
        if char.isalnum()
    )
    raw_candidate = "".join(char for char in candidate if char.isalnum())
    available = raw_candidate.lower()
    if wanted == available:
        return "exact"

    if (
        "x" in raw_candidate
        and len(raw_candidate) == len(wanted)
        and all(
            candidate_char == "x"
            or candidate_char.lower() == requested_char
            for candidate_char, requested_char in zip(
                raw_candidate,
                wanted,
                strict=True,
            )
        )
    ):
        return "kicad_wildcard"

    raw_requested = requested.strip()
    candidate_base = candidate.strip()
    if (
        len(raw_requested) > len(candidate_base)
        and raw_requested[:len(candidate_base)].casefold()
        == candidate_base.casefold()
        and raw_requested[len(candidate_base)] in "-_/ "
    ):
        return "qualified_base"
    return None


def ground_symbol(proposed: str) -> str | None:
    """Map ``proposed`` to a real symbol ID, or return it unchanged if already
    valid. Returns ``None`` only when no library is configured to check against."""
    if not proposed or ":" not in proposed:
        return proposed
    index = _symbol_search_index()
    if not index.ids:
        return proposed
    exact = index.by_full_casefold.get(proposed.casefold(), ())
    if len(exact) == 1:
        return exact[0]

    nick, _, name = proposed.partition(":")
    a_nick = _LIB_ALIASES.get(nick.lower(), nick)
    a_name = _NAME_ALIASES.get(name.lower(), name)

    # Exact hit after aliasing.
    cand = f"{a_nick}:{a_name}"
    canonical = index.by_full_casefold.get(cand.casefold(), ())
    if len(canonical) == 1:
        return canonical[0]
    if a_name != name:
        # Primitive aliases may arrive under a plausible but wrong library
        # nickname. They are safe only when the normalized generic identity is
        # exact and unique.
        device = index.by_full_casefold.get(
            f"Device:{a_name}".casefold(),
            (),
        )
        if len(device) == 1:
            return device[0]
        named = tuple(
            entry
            for entry in index.by_name_casefold.get(a_name.casefold(), ())
            if _allows_fuzzy_symbol_grounding(entry.library)
        )
        if len(named) == 1:
            return named[0].full_id

    # A near-looking order code is still a different electrical device.
    # Preserve an unresolved concrete code so selection can synthesize/repair
    # it instead of silently grounding DRVxxxx to another DRVxxxx, etc. KiCad
    # package wildcards such as STM32...x remain valid deterministic matches.
    scoped_entries = index.by_library.get(a_nick.casefold(), ())
    if _looks_like_order_code(a_name):
        compatible = _unique_full_id(scoped_entries, a_name)
        if compatible is None:
            normalized_length = len(
                "".join(char for char in a_name if char.isalnum())
            )
            compatible = _unique_full_id(
                index.by_normalized_length.get(normalized_length, ()),
                a_name,
            )
        if compatible is not None:
            return compatible
        return proposed

    # Similar active devices are discovery candidates, not interchangeable
    # electrical identities. Direct lexical grounding is reserved for KiCad's
    # generic primitive families.
    if not _allows_fuzzy_symbol_grounding(a_nick):
        return proposed

    # Prefer token-overlap scoring (precise for terse EE names): scope to the
    # aliased library first, then fall back to every library.
    scoped = _token_candidates(index, a_name, library=a_nick)
    if scoped:
        best = _best_by_tokens(
            a_name,
            [
                (entry.full_id, set(entry.tokens))
                for entry in scoped
            ],
        )
        if best is not None:
            return best
    global_candidates = tuple(
        entry
        for entry in _token_candidates(index, f"{a_nick} {a_name}")
        if _allows_fuzzy_symbol_grounding(entry.library)
    )
    best = _best_by_tokens(
        f"{a_nick} {a_name}",
        [
            (entry.full_id, set(entry.tokens))
            for entry in global_candidates
        ],
    )
    if best is not None:
        return best
    # Fuzzy / substring as a last resort within the aliased library.
    fuzzy_entries = _fuzzy_candidates(index, a_name, library=a_nick)
    if not fuzzy_entries:
        fuzzy_entries = tuple(
            entry
            for entry in _fuzzy_candidates(index, a_name)
            if _allows_fuzzy_symbol_grounding(entry.library)
        )
    names = [entry.name for entry in fuzzy_entries]
    close = difflib.get_close_matches(a_name, names, n=1, cutoff=0.7)
    if close:
        return next(
            entry.full_id
            for entry in fuzzy_entries
            if entry.name == close[0]
        )
    low = a_name.lower()
    subs = [
        (entry.name, entry.full_id)
        for entry in fuzzy_entries
        # Require a meaningful (>=3 char) overlap so a bogus name like
        # "DoesNotExist" cannot match a 1-char symbol ("D"/"R"/"C") just
        # because that letter happens to appear in it.
        if len(entry.name) >= 3
        and (low in entry.name.lower() or entry.name.lower() in low)
    ]
    if subs:  # shortest name containing the token is the most generic match
        subs.sort(key=lambda t: len(t[0]))
        return subs[0][1]
    return proposed  # leave as-is; the bottom-line check will block it


def ground_footprint(proposed: str) -> str | None:
    """Map ``proposed`` to a real footprint ID, or return it unchanged if valid.
    Empty input is passed through (footprint is optional at selection time)."""
    if not proposed:
        return proposed
    if footprints.footprint_pads(proposed) is not None:
        return proposed

    nick, _, name = proposed.partition(":") if ":" in proposed else ("", "", proposed)
    index = _footprint_index()
    if not index:
        return proposed
    query = f"{name}" if name else proposed
    # Prefer candidates in the proposed library, then search all libraries.
    same = [(i, set(t)) for i, t in index if i.partition(":")[0].lower() == nick.lower()]
    best = _best_by_tokens(query, same) if same else None
    if best is None:
        best = _best_by_tokens(query, [(i, set(t)) for i, t in index])
    if best is not None:
        return best
    # Last resort: the vendored substring search (handles odd stems).
    hits = search_footprints(name, limit=20) or search_footprints(
        name.split("_")[0], limit=20
    )
    return hits[0]["lib_id"] if hits else proposed
