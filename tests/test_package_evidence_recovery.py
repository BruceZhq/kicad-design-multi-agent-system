import json
from types import SimpleNamespace

from agents.ratsnestpro import package_evidence as adapter
from agents.ratsnestpro.ratsnestpro_agent import _component_queries
from ratsnestpro.orchestration.pipeline import _datasheet_pin_functions
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart


def test_decimal_model_is_not_truncated():
    assert "AP2112K-3.3" in _component_queries("使用 AP2112K-3.3 产生 3.3 V")


def test_package_column_cannot_be_replaced_by_other_package_numbers():
    pins = [{"number": "1", "name": "VIN"}, {"number": "2", "name": "VOUT"}]
    pages = [{"text": "LQFP64  LQFP48  Name\n1       7       VIN\n2       8       VOUT"}]
    assert len(_datasheet_pin_functions(pins, pages, "Package_QFP:LQFP-64")) == 2
    assert _datasheet_pin_functions(pins, pages, "Package_QFP:LQFP-48") == []


def test_failed_fetch_is_cached_across_recovery_instances(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter.symbols, "symbol_properties", lambda _: {})
    monkeypatch.setattr(adapter, "web_search_official_manufacturer", SimpleNamespace(invoke=lambda _: '{"results": []}'))
    part = SelectedPart(ref="U1", symbol="Regulator:Example", value="EX1234", footprint="Package:TwoPin")
    assert adapter.PackageEvidenceFetcher(tmp_path)(part) == []
    def unexpected(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unchanged failure must use cached receipt")
    monkeypatch.setattr(adapter.web_search_official_manufacturer, "invoke", unexpected)
    assert adapter.PackageEvidenceFetcher(tmp_path)(part) == []
    assert calls == []
    record = json.loads(next((tmp_path / "technical-evidence").glob("*.json")).read_text())
    assert record["identity"] == "EX1234"


def test_visual_table_is_bound_to_document_and_checks_actual_pin_functions(monkeypatch):
    from ratsnestpro.orchestration.pipeline import _datasheet_package_evidence
    part = SelectedPart(ref="U1", symbol="Regulator:EX1234", value="EX1234",
                        footprint="Package:SOIC-2", mpn="EX1234")
    monkeypatch.setattr("ratsnestpro.orchestration.pipeline.symbols.symbol_pins",
                        lambda _: [{"number": "1", "name": "VIN"}, {"number": "2", "name": "GND"}])
    document = {"status": "ok", "authority": "official_manufacturer_datasheet",
                "evidence_sufficient": True, "source_url": "https://example.com/a.pdf",
                "source_sha256": "a" * 64,
                "matched_pages": [{"page": 1, "text": "EX1234 SOIC-2"}]}
    visual = {"source_sha256": "a" * 64, "pages": [1], "pins": [
        {"number": "1", "functions": ["VIN"], "page": 1},
        {"number": "2", "functions": ["GND"], "page": 1},
    ]}
    def verify():
        return _datasheet_package_evidence(part, source_identity="EX1234", datasheet=document,
                                           visual_pin_table=visual)
    assert verify() is not None
    visual["pins"][0]["functions"] = ["VIN — Input Voltage"]
    assert verify() is not None
    visual["pins"][1]["functions"] = ["VOUT"]
    assert verify() is None
    visual["pins"][1]["functions"] = ["GND"]
    visual["source_sha256"] = "b" * 64
    assert verify() is None
    visual["source_sha256"] = "a" * 64
    visual["pins"][1]["page"] = 99
    assert verify() is None
    visual["pins"][1]["page"] = 1
    visual["pins"][1]["functions"] = [None]
    assert verify() is None
    visual["pins"][1]["functions"] = ["GND"]
    visual["source_sha256"] = document["source_sha256"] = ""
    assert verify() is None
