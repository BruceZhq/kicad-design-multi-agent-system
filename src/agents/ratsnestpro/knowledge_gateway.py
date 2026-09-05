"""Bounded adapter for an externally managed Agentic RAG service."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import httpx

_MAX_RESPONSE_BYTES = 1_000_000
_MAX_TEXT_CHARS = 4_000
_ALLOWED_STATUS = {"ok", "sufficient", "no_results", "unavailable"}


def _bounded_timeout() -> float:
    try:
        value = float(os.getenv("RATSNEST_KNOWLEDGE_GATEWAY_TIMEOUT_SECONDS", "8"))
    except ValueError:
        return 8.0
    return min(max(value, 1.0), 30.0)


def _safe_reference_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return url[:2_048]


def _normalise_result(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    text = str(value.get("text", "")).strip()
    if not text:
        return None
    try:
        score = float(value.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    page = value.get("page")
    if not isinstance(page, int) or page < 1:
        page = None
    return {
        "id": str(value.get("id", "external-evidence"))[:255],
        "role": str(value.get("role", "general"))[:80],
        "source": str(value.get("source", "external_agentic_rag"))[:500],
        "source_url": _safe_reference_url(value.get("source_url")),
        "title": str(value.get("title", "Knowledge evidence"))[:500],
        "authority": str(value.get("authority", "internal_unverified"))[:80],
        "evidence_type": str(value.get("evidence_type", "internal_document"))[:80],
        "page": page,
        "score": score,
        "text": text[:_MAX_TEXT_CHARS],
        "content_hash": str(value.get("content_hash", ""))[:128],
        "updated_at": str(value.get("updated_at", ""))[:80],
        "manufacturer": str(value.get("manufacturer", ""))[:160],
        "mpn": str(value.get("mpn", ""))[:160],
        "package": str(value.get("package", ""))[:160],
        "symbol_lib_id": str(value.get("symbol_lib_id", ""))[:200],
        "footprint_lib_id": str(value.get("footprint_lib_id", ""))[:240],
        "pin_pad_digest": str(value.get("pin_pad_digest", ""))[:128],
        "provider": "external_agentic_rag",
        "untrusted_content": True,
    }


def _parts_claim_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Require exact package evidence and a matching KiCad binding claim."""

    datasheets = [
        item
        for item in results
        if item.get("evidence_type") == "datasheet"
        and item.get("authority") == "official_manufacturer"
        and item.get("mpn")
        and item.get("package")
        and item.get("content_hash")
    ]
    bindings = [
        item
        for item in results
        if item.get("evidence_type") == "kicad_binding"
        and item.get("mpn")
        and item.get("package")
        and item.get("symbol_lib_id")
        and item.get("footprint_lib_id")
        and item.get("pin_pad_digest")
        and item.get("content_hash")
    ]
    matched = any(
        str(datasheet["mpn"]).casefold() == str(binding["mpn"]).casefold()
        and str(datasheet["package"]).casefold()
        == str(binding["package"]).casefold()
        for datasheet in datasheets
        for binding in bindings
    )
    return {
        "official_exact_package": bool(datasheets),
        "verified_kicad_binding": bool(bindings),
        "same_mpn_and_package": matched,
        "sufficient": bool(datasheets and bindings and matched),
    }


def search_external_knowledge(
    *,
    query: str,
    role: str,
    limit: int,
    evidence_types: list[str] | None = None,
    principal_scope: str = "",
    tenant_scope: str = "",
    project_scope: str = "",
) -> dict[str, Any]:
    """Query a fixed trusted gateway; failures degrade to local/web retrieval."""

    endpoint = os.getenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", "").strip()
    if not endpoint:
        return {"status": "disabled", "evidence_sufficient": False, "results": []}
    parsed_endpoint = urlparse(endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
        return {
            "status": "unavailable",
            "evidence_sufficient": False,
            "results": [],
            "error": "knowledge gateway URL is invalid",
        }

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = os.getenv("RATSNEST_KNOWLEDGE_GATEWAY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "schema_version": "1.0",
        "query": query[:8_000],
        "role": role[:80],
        "limit": max(1, min(limit, 8)),
        "evidence_types": [str(item)[:80] for item in (evidence_types or [])[:12]],
        "scope": {
            "principal": principal_scope[:128],
            "tenant": tenant_scope[:128],
            "project": project_scope[:128],
        },
    }
    try:
        response = httpx.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=_bounded_timeout(),
            follow_redirects=False,
        )
        response.raise_for_status()
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ValueError("knowledge gateway response exceeds 1 MB")
        raw = response.json()
        if not isinstance(raw, dict):
            raise ValueError("knowledge gateway response must be a JSON object")
        results = [
            result
            for item in raw.get("results", [])[: payload["limit"]]
            if (result := _normalise_result(item)) is not None
        ] if isinstance(raw.get("results"), list) else []
        status = str(raw.get("status", "ok"))
        if status not in _ALLOWED_STATUS:
            status = "unavailable"
        claim_coverage: dict[str, Any] | None = None
        sufficient = bool(raw.get("evidence_sufficient")) and bool(results)
        if role == "parts-specialist":
            claim_coverage = _parts_claim_coverage(results)
            sufficient = sufficient and claim_coverage["sufficient"] is True
        return {
            "status": "ok" if status == "sufficient" else status,
            "evidence_sufficient": sufficient,
            **({"claim_coverage": claim_coverage} if claim_coverage is not None else {}),
            "results": results,
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {
            "status": "unavailable",
            "evidence_sufficient": False,
            "results": [],
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
