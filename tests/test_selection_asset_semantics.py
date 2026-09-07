import hashlib
import json
from types import SimpleNamespace

import pytest

from agents.ratsnestpro import symbol_evidence_repair as repair
from ratsnestpro.orchestration import pipeline
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart


def test_testpoint_is_not_an_ordinary_connector(monkeypatch):
    part = SelectedPart(ref="TP1", role="testpoint", value="TP",
                        symbol="Connector:TestPoint",
                        footprint="TestPoint:TestPoint_Pad_D2.5mm")
    monkeypatch.setattr(pipeline.symbols, "symbol_pins", lambda _: [{"number": "1"}])
    monkeypatch.setattr(pipeline.footprints, "footprint_pads", lambda _: [{"number": "1"}])
    pipeline._bind_selected_footprints([part])
    assert part.footprint_binding_status == "verified_installed"
    assert not pipeline._footprint_matches_symbol_family("Connector:Conn_01x02", part.footprint)
    monkeypatch.setattr(pipeline.footprints, "footprint_pads", lambda _: [{"number": "2"}])
    pipeline._bind_selected_footprints([part])
    assert part.footprint_binding_status == "unresolved"


@pytest.mark.parametrize("corruption", [None, "pdf", "pin", "source", "row"])
def test_repaired_family_requires_live_source_and_document(monkeypatch, tmp_path, corruption):
    library = tmp_path / "evidence-symbols" / "RatsNestGenerated.kicad_sym"
    library.parent.mkdir()
    library.write_text("fixture")
    evidence = tmp_path / "technical-evidence"
    evidence.mkdir()
    digest = hashlib.sha256(b"pdf").hexdigest()
    (evidence / (digest + ".pdf")).write_bytes(b"wrong" if corruption == "pdf" else b"pdf")
    generated = "RatsNestGenerated:Example"
    original = "MCU_Example:Example"
    row = {"number": "1", "name": "PA9", "type": "bidirectional", "page": 1}
    receipt = {"original_symbol": original, "replacement_symbol": generated,
               "document_sha256": digest, "pin_changes": {"1": row}}
    (evidence / "U1-symbol-repair.json").write_text(json.dumps(receipt))
    page = SimpleNamespace(extract_text=lambda **kw: "1  PA9  I/O" if kw else "LQFP64")
    if corruption == "row":
        page.extract_text = lambda **kw: "1  PA8  I/O" if kw else "LQFP64"
    monkeypatch.setattr("pypdf.PdfReader", lambda _: SimpleNamespace(pages=[page]))
    monkeypatch.setattr(repair.symbols, "resolve_symbol", lambda _: library)
    monkeypatch.setattr(repair.symbols, "symbol_properties", lambda _: {"Footprint": "Package_QFP:LQFP-64"})
    def pins(symbol):
        if symbol == original:
            return [] if corruption == "source" else [{"number": "1", "name": "NC/PA9", "type": "no_connect"}]
        return [{"number": "1", "name": "PA8" if corruption == "pin" else "PA9", "type": "bidirectional"}]
    monkeypatch.setattr(repair.symbols, "symbol_pins", pins)
    assert repair.verified_repair_source(generated) == (original if corruption is None else "")


def test_generated_namespace_alone_cannot_impersonate_mcu(monkeypatch):
    part = SelectedPart(ref="U1", role="mcu", value="Example",
                        symbol="RatsNestGenerated:Example", footprint="Package_QFP:LQFP-64")
    monkeypatch.setattr(repair, "verified_repair_source", lambda _: "")
    monkeypatch.setattr(pipeline.symbols, "symbol_properties", lambda _: {})
    assert pipeline._role_symbol_family_error(part)
    monkeypatch.setattr(repair, "verified_repair_source", lambda _: "MCU_Example:Example")
    assert pipeline._role_symbol_family_error(part) is None


def test_pin_alias_lineage_still_requires_unchanged_physical_pin(monkeypatch):
    part = SelectedPart(ref="U1", role="mcu", value="Example",
                        symbol="RatsNestGenerated:Example", footprint="Package_QFP:LQFP-64")
    alias = SimpleNamespace(symbol_lib_id="MCU_Example:Example", pin_number="46",
                            symbol_pin_name="PA14", aliases=["BOOT0"])
    view = SimpleNamespace(pins={"U1": [{"number": "46", "name": "PA14"}]})
    monkeypatch.setattr(repair, "verified_repair_source", lambda _: "MCU_Example:Example")
    assert pipeline._verified_function_pin_candidates(view, part, [alias], "BOOT0") == {"46"}
    view.pins["U1"][0]["name"] = "PA13"
    assert pipeline._verified_function_pin_candidates(view, part, [alias], "BOOT0") == set()
    view.pins["U1"][0]["name"] = "PA14"
    monkeypatch.setattr(repair, "verified_repair_source", lambda _: "")
    assert pipeline._verified_function_pin_candidates(view, part, [alias], "BOOT0") == set()
