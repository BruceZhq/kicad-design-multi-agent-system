"""Bounded, evidence-preserving search plans for KiCad review findings."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

REMEDIATION_QUERY_MAX_CHARS = 240

_CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "library",
        ("lib_", "library", "symbol", "footprint", "padstack"),
    ),
    (
        "connectivity",
        (
            "pin_",
            "label_",
            "net_",
            "unconnected",
            "dangling",
            "junction",
            "not_driven",
            "power_pin",
            "multiple_net",
        ),
    ),
    (
        "clearance",
        (
            "clearance",
            "overlap",
            "solder_mask",
            "silk_",
            "courtyard",
        ),
    ),
    (
        "routing",
        ("track", "via", "route", "diff_pair", "length", "skew", "width"),
    ),
    (
        "board_geometry",
        ("board_edge", "outline", "hole", "drill", "annular"),
    ),
)

_CATEGORY_SEARCH_TERMS = {
    "library": "symbol footprint library mismatch",
    "connectivity": "pin net label connectivity",
    "clearance": "clearance solder mask silkscreen",
    "routing": "tracks vias routing constraints",
    "board_geometry": "board outline holes geometry",
    "erc_other": "schematic ERC violation",
    "drc_other": "PCB DRC violation",
}


def _normalize_rule_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "unknown"


def _rule_category(rule_id: str, section: str) -> str:
    for category, patterns in _CATEGORY_PATTERNS:
        if any(pattern in rule_id for pattern in patterns):
            return category
    return "erc_other" if section == "erc" else "drc_other"


def _query_scope(sections: set[str]) -> str:
    if sections == {"erc"}:
        return "Schematic Editor ERC"
    if sections == {"drc"}:
        return "PCB Editor DRC"
    return "ERC DRC"


def _query_chunks(
    *,
    category: str,
    sections: set[str],
    rule_ids: list[str],
) -> list[dict[str, Any]]:
    base = (
        f"site:docs.kicad.org KiCad {_query_scope(sections)} "
        f"{_CATEGORY_SEARCH_TERMS[category]} remediation"
    )
    chunks: list[dict[str, Any]] = []
    current_query = base
    current_rules: list[str] = []

    def finish_chunk() -> None:
        nonlocal current_query, current_rules
        if current_rules:
            chunks.append(
                {
                    "category": category,
                    "source_sections": sorted(sections),
                    "normalized_rule_ids": current_rules,
                    "query": current_query,
                }
            )
        current_query = base
        current_rules = []

    for rule_id in rule_ids:
        search_phrase = rule_id.replace("_", " ")
        candidate = f"{current_query} {search_phrase}"
        if (
            current_rules
            and len(candidate) > REMEDIATION_QUERY_MAX_CHARS
        ):
            finish_chunk()
            candidate = f"{current_query} {search_phrase}"
        if len(candidate) > REMEDIATION_QUERY_MAX_CHARS:
            available = REMEDIATION_QUERY_MAX_CHARS - len(current_query) - 1
            search_phrase = search_phrase[: max(1, available)].rstrip()
            candidate = f"{current_query} {search_phrase}"
        current_query = candidate
        current_rules.append(rule_id)
    finish_chunk()
    return chunks


def build_remediation_search_plan(
    *,
    verification: Mapping[str, Any] | None,
    issue_ledger: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Plan one bounded official-doc search per stable ERC/DRC category.

    Harness findings remain authoritative run-local evidence. They are indexed
    here but never copied into a public search query.
    """

    grouped_rules: dict[str, set[str]] = defaultdict(set)
    grouped_sections: dict[str, set[str]] = defaultdict(set)
    external_evidence: list[dict[str, Any]] = []
    verification = verification or {}
    for section in ("erc", "drc"):
        section_result = verification.get(section, {})
        by_type = (
            section_result.get("by_type", {})
            if isinstance(section_result, Mapping)
            else {}
        )
        if not isinstance(by_type, Mapping):
            continue
        for original_rule_id, count in by_type.items():
            normalized_rule_id = _normalize_rule_id(str(original_rule_id))
            category = _rule_category(normalized_rule_id, section)
            grouped_rules[category].add(normalized_rule_id)
            grouped_sections[category].add(section)
            external_evidence.append(
                {
                    "section": section,
                    "rule_id": str(original_rule_id),
                    "normalized_rule_id": normalized_rule_id,
                    "count": count,
                    "category": category,
                }
            )

    queries = [
        query
        for category in sorted(grouped_rules)
        for query in _query_chunks(
            category=category,
            sections=grouped_sections[category],
            rule_ids=sorted(grouped_rules[category]),
        )
    ]
    unique_queries: list[dict[str, Any]] = []
    query_indexes: dict[str, int] = {}
    for query in queries:
        key = " ".join(str(query["query"]).casefold().split())
        if key in query_indexes:
            existing = unique_queries[query_indexes[key]]
            existing["normalized_rule_ids"] = sorted({
                *existing["normalized_rule_ids"],
                *query["normalized_rule_ids"],
            })
            continue
        query_indexes[key] = len(unique_queries)
        unique_queries.append(query)

    skipped_run_local = [
        {
            "index": index,
            "step": str(finding.get("step", "")),
            "name": str(finding.get("name", "")),
            "reason": "run_local_structure",
        }
        for index, finding in enumerate(issue_ledger or [])
        if isinstance(finding, dict)
    ]
    return {
        "schema_version": 1,
        "query_max_chars": REMEDIATION_QUERY_MAX_CHARS,
        "queries": unique_queries,
        "external_rule_evidence": external_evidence,
        "skipped_run_local_findings": skipped_run_local,
        "evidence_sources": {
            "external_rules": "review.verification.{erc,drc}.by_type",
            "run_local_findings": "hardware.issue_ledger",
        },
    }
