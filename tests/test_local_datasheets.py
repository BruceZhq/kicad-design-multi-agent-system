import hashlib
from types import SimpleNamespace

import pytest

from agents.ratsnestpro import local_datasheets as local


def test_local_document_roundtrip_and_tamper_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(local, "_parse_datasheet_pdf", lambda content, *a, **kw: {
        "source_sha256": hashlib.sha256(content).hexdigest(), "matched_pages": [{"page": 1}],
    })
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-test")
    with pytest.raises(ValueError):
        local.import_document(pdf, "EX1234", "https://www.st.com/a.pdf")
    digest = local.import_document(pdf, "EX1234", "https://www.st.com/a.pdf", approve_source=True)
    assert local.find_document("EX1234")["retrieval_method"] == "operator_imported_pdf"
    assert local.find_document("EX1235") is None
    (local.registry_root() / (digest + ".pdf")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        local.find_document("EX1234")


def test_pdf_selection_keeps_ordering_page(monkeypatch):
    from agents.ratsnestpro import web_tools
    texts = ["EX1234 pinout " * 20, "EX1234 package " * 20,
             "Ordering information\nEX1234 code T means LQFP"]
    pages = [SimpleNamespace(extract_text=lambda text=text, **kw: text) for text in texts]
    monkeypatch.setattr(web_tools, "PdfReader", lambda _: SimpleNamespace(pages=pages))
    result = web_tools._parse_datasheet_pdf(b"%PDF-test", "https://www.st.com/a.pdf", "EX1234", 2)
    assert 3 in [p["page"] for p in result["matched_pages"]]
