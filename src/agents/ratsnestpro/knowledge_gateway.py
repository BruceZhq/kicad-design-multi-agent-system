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
        "provider": "external_agentic_rag",
        "untrusted_content": True,
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
        sufficient = bool(raw.get("evidence_sufficient")) and bool(results)
        return {
            "status": "ok" if status == "sufficient" else status,
            "evidence_sufficient": sufficient,
            "results": results,
        }
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return {
            "status": "unavailable",
            "evidence_sufficient": False,
            "results": [],
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
