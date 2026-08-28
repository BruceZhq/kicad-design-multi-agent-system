from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from agents.ratsnestpro import ratsnestpro_agent, web_tools
from ratsnestpro.orchestration.pipeline import CANONICAL_ORDER, PipelineStep


def test_official_search_filters_distributors_and_never_grants_procurement(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        web_tools,
        "_search_web",
        lambda _query: json.dumps(
            {
                "status": "ok",
                "results": [
                    {
                        "title": "TPS62160 datasheet",
                        "href": "https://www.ti.com/lit/ds/symlink/tps62160.pdf",
                        "body": "Official datasheet",
                    },
                    {
                        "title": "TPS62160 product page",
                        "href": "https://www.ti.com/product/TPS62160",
                        "body": "Official product page",
                    },
                    {
                        "title": "TPS62160 in stock",
                        "href": "https://www.digikey.com/example",
                        "body": "$1.23, 1000 in stock",
                    },
                    {
                        "title": "TPS62160 review",
                        "href": "https://example.net/tps62160",
                        "body": "Blog post",
                    },
                ],
            }
        ),
    )

    result = json.loads(web_tools._official_manufacturer_sources("TPS62160"))

    assert [item["manufacturer_domain"] for item in result["results"]] == [
        "ti.com",
        "ti.com",
    ]
    assert result["results"][0]["evidence_class"] == (
        "official_manufacturer_datasheet"
    )
    assert result["evidence_sufficient"] is False
    assert result["policy"]["stock_price_lead_time_claims_allowed"] is False
    assert all(
        item["procurement_claims_allowed"] is False for item in result["results"]
    )


def test_datasheet_evidence_requires_official_host_and_exact_component_text() -> None:
    evidence = {
        "status": "ok",
        "source_url": "https://www.ti.com/lit/ds/symlink/tps62160.pdf",
        "matched_pages": [{"page": 1, "text": "TPS62160 3-V to 17-V converter"}],
    }

    assert web_tools.official_datasheet_evidence_sufficient("TPS62160", evidence)
    assert not web_tools.official_datasheet_evidence_sufficient(
        "TPS62170",
        evidence,
    )
    assert not web_tools.official_datasheet_evidence_sufficient(
        "TPS62160",
        {**evidence, "source_url": "https://parts-blog.example/tps62160.pdf"},
    )


def test_parts_phase_uses_official_fallback_as_technical_only(monkeypatch) -> None:
    monkeypatch.setattr(
        ratsnestpro_agent,
        "ratsnest_search_internal_knowledge",
        lambda **_kwargs: json.dumps(
            {"status": "ok", "evidence_sufficient": False, "results": []}
        ),
    )
    monkeypatch.setattr(
        ratsnestpro_agent,
        "ratsnest_search_parts",
        lambda **_kwargs: json.dumps(
            {"status": "unavailable", "error": "catalog not mounted"}
        ),
    )
    monkeypatch.setattr(
        ratsnestpro_agent,
        "web_search_official_manufacturer",
        SimpleNamespace(
            invoke=lambda _args: json.dumps(
                {
                    "status": "ok",
                    "results": [
                        {
                            "title": "TPS62160 datasheet",
                            "href": "https://www.ti.com/lit/ds/symlink/tps62160.pdf",
                            "authority": "official_manufacturer",
                            "manufacturer_domain": "ti.com",
                            "evidence_class": "official_manufacturer_datasheet",
                            "procurement_claims_allowed": False,
                        }
                    ],
                }
            )
        ),
    )
    monkeypatch.setattr(
        ratsnestpro_agent,
        "fetch_datasheet",
        SimpleNamespace(
            invoke=lambda _args: json.dumps(
                {
                    "status": "ok",
                    "source_url": "https://www.ti.com/lit/ds/symlink/tps62160.pdf",
                    "matched_pages": [
                        {"page": 1, "text": "TPS62160 pinout and package information"}
                    ],
                }
            )
        ),
    )

    update = asyncio.run(
        ratsnestpro_agent.parts_phase(
            {"requirement": "Use TPS62160 for the 3.3 V rail.", "trace": []},
            {"configurable": {}},
        )
    )
    parts = update["parts"]

    assert parts["technical_status"] == "ok"
    assert parts["procurement_status"] == "unavailable"
    assert parts["queries"][0]["catalog"]["status"] == "unavailable"
    fallback = parts["queries"][0]["official_web_fallback"]
    assert fallback["evidence_sufficient"] is True
    assert fallback["procurement_claims_allowed"] is False
    assert parts["component_closure"] == {
        "authority": "hardware_pipeline.selection",
        "before_step": "schematic_connections",
        "fail_closed": True,
        "web_evidence_can_bypass": False,
    }


def test_parts_phase_skips_web_when_governed_evidence_is_sufficient(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ratsnestpro_agent,
        "ratsnest_search_internal_knowledge",
        lambda **_kwargs: json.dumps(
            {
                "status": "ok",
                "evidence_sufficient": True,
                "results": [{"id": "rag-1", "text": "grounded"}],
            }
        ),
    )
    monkeypatch.setattr(
        ratsnestpro_agent,
        "ratsnest_search_parts",
        lambda **_kwargs: json.dumps({"status": "unavailable", "results": []}),
    )

    def unexpected_web(_args):
        raise AssertionError("official web fallback must not run")

    monkeypatch.setattr(
        ratsnestpro_agent,
        "web_search_official_manufacturer",
        SimpleNamespace(invoke=unexpected_web),
    )

    update = asyncio.run(
        ratsnestpro_agent.parts_phase(
            {"requirement": "Use TPS62160 for the 3.3 V rail.", "trace": []},
            {"configurable": {}},
        )
    )

    fallback = update["parts"]["queries"][0]["official_web_fallback"]
    assert fallback["status"] == "not_needed"
    assert fallback["triggered"] is False


def test_parts_evidence_is_bounded_and_selection_closure_stays_first() -> None:
    parts = {
        "queries": [
            {
                "query": "TPS62160",
                "technical_evidence": {"results": []},
                "catalog": {"results": []},
                "official_web_fallback": {
                    "evidence_sufficient": True,
                    "search": {
                        "results": [
                            {
                                "title": "Official datasheet",
                                "href": "https://www.ti.com/tps62160.pdf",
                                "authority": "official_manufacturer",
                                "manufacturer_domain": "ti.com",
                                "evidence_class": "official_manufacturer_datasheet",
                            }
                        ]
                    },
                    "datasheet": {
                        "status": "ok",
                        "source_url": "https://www.ti.com/tps62160.pdf",
                        "matched_pages": [
                            {"page": index, "text": "x" * 2_000}
                            for index in range(5)
                        ],
                    },
                },
            }
        ]
    }

    evidence = ratsnestpro_agent._parts_selection_evidence(parts)

    web = evidence["queries"][0]["official_web"]
    assert len(web["datasheet"]["matched_pages"]) == 2
    assert all(len(page["text"]) == 1_000 for page in web["datasheet"]["matched_pages"])
    assert web["procurement_claims_allowed"] is False
    assert evidence["evidence_contract"][
        "web_evidence_can_bypass_symbol_footprint_pin_pad_closure"
    ] is False
    assert CANONICAL_ORDER.index(PipelineStep.SELECTION) < CANONICAL_ORDER.index(
        PipelineStep.SCH_CONNECTIONS
    )
