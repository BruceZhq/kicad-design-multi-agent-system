"""Bounded semantic lookup over the installed KiCad footprint names."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from ratsnestpro.eda import grounding

_MAX_CANDIDATES = 128
_INDEX_LOCK = threading.RLock()


@dataclass(frozen=True)
class _CandidateIndex:
    by_id: dict[str, frozenset[str]]
    by_token: dict[str, tuple[str, ...]]


_source_ids: tuple[str, ...] | None = None
_candidate_index: _CandidateIndex | None = None


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z]+[a-z0-9]*", text.lower())
        if len(token) >= 2
    )


def _build_candidate_index(footprint_ids: tuple[str, ...]) -> _CandidateIndex:
    by_id = {lib_id: _tokens(lib_id) for lib_id in footprint_ids}
    buckets: dict[str, list[str]] = {}
    for lib_id, tokens in by_id.items():
        for token in tokens:
            buckets.setdefault(token, []).append(lib_id)
    return _CandidateIndex(
        by_id=by_id,
        by_token={token: tuple(ids) for token, ids in buckets.items()},
    )


def invalidate_candidate_index() -> None:
    """Clear the derived lookup; primarily useful after an explicit library edit."""

    global _candidate_index, _source_ids
    with _INDEX_LOCK:
        _source_ids = None
        _candidate_index = None


def _current_index() -> _CandidateIndex:
    global _candidate_index, _source_ids
    installed = grounding.footprint_index()
    with _INDEX_LOCK:
        if _candidate_index is None or installed != _source_ids:
            _candidate_index = _build_candidate_index(installed)
            _source_ids = installed
        return _candidate_index


def footprint_candidates(
    wanted_tokens: set[str],
    *,
    limit: int = _MAX_CANDIDATES,
) -> tuple[str, ...]:
    """Return a relevance-ranked, bounded subset without parsing footprint files."""

    wanted = {
        token.lower()
        for token in wanted_tokens
        if len(token) >= 2
    }
    bounded_limit = min(max(limit, 0), _MAX_CANDIDATES)
    if not wanted or bounded_limit == 0:
        return ()

    index = _current_index()
    candidate_ids = {
        lib_id
        for token in wanted
        for lib_id in index.by_token.get(token, ())
    }
    ranked = sorted(
        candidate_ids,
        key=lambda lib_id: (
            -len(wanted & index.by_id[lib_id]),
            len(lib_id),
            lib_id,
        ),
    )
    return tuple(ranked[:bounded_limit])
