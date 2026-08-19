from __future__ import annotations

import httpx

from agents.ratsnestpro.knowledge_gateway import search_external_knowledge


def test_gateway_is_optional(monkeypatch) -> None:
    monkeypatch.delenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", raising=False)

    result = search_external_knowledge(query="q", role="architect", limit=3)

    assert result == {"status": "disabled", "evidence_sufficient": False, "results": []}


def test_gateway_sends_opaque_scope_and_normalises_evidence(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "sufficient",
                "evidence_sufficient": True,
                "results": [
                    {
                        "id": "chunk-1",
                        "title": "Official datasheet",
                        "source_url": "https://manufacturer.example/device.pdf",
                        "authority": "official_manufacturer",
                        "evidence_type": "datasheet",
                        "page": 42,
                        "score": 0.9,
                        "text": "Verified excerpt",
                    }
                ],
            },
        )

    monkeypatch.setenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", "http://agentic-rag:8090/v1/search")
    monkeypatch.setenv("RATSNEST_KNOWLEDGE_GATEWAY_TOKEN", "server-token")
    monkeypatch.setattr(httpx, "post", fake_post)

    result = search_external_knowledge(
        query="STM32 evidence",
        role="architect",
        limit=6,
        evidence_types=["datasheet"],
        principal_scope="rt1:principal",
        tenant_scope="rt1:tenant",
        project_scope="rt1:project",
    )

    assert result["status"] == "ok"
    assert result["evidence_sufficient"] is True
    assert result["results"][0]["provider"] == "external_agentic_rag"
    assert result["results"][0]["untrusted_content"] is True
    assert captured["headers"]["Authorization"] == "Bearer server-token"
    assert captured["json"]["scope"] == {
        "principal": "rt1:principal",
        "tenant": "rt1:tenant",
        "project": "rt1:project",
    }
    assert captured["follow_redirects"] is False


def test_gateway_never_marks_empty_results_sufficient(monkeypatch) -> None:
    def fake_post(url, **_kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"status": "ok", "evidence_sufficient": True, "results": []},
        )

    monkeypatch.setenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", "https://rag.example/v1/search")
    monkeypatch.setattr(httpx, "post", fake_post)

    result = search_external_knowledge(query="q", role="reviewer", limit=3)

    assert result["evidence_sufficient"] is False


def test_gateway_failure_is_a_soft_fallback(monkeypatch) -> None:
    def fake_post(_url, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setenv("RATSNEST_KNOWLEDGE_GATEWAY_URL", "https://rag.example/v1/search")
    monkeypatch.setattr(httpx, "post", fake_post)

    result = search_external_knowledge(query="q", role="parts-specialist", limit=3)

    assert result["status"] == "unavailable"
    assert result["evidence_sufficient"] is False
    assert result["results"] == []
