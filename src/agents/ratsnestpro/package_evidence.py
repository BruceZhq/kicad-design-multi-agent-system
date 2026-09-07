"""Bounded official document recovery for an already selected component."""

import hashlib
import json
import time
from pathlib import Path

from agents.ratsnestpro.web_tools import (
    _official_manufacturer_domain,
    _read_datasheet,
    official_datasheet_evidence_sufficient,
    web_search_official_manufacturer,
)
from ratsnestpro.eda import symbols


class PackageEvidenceFetcher:
    """Cache observations, not release decisions; validators run on every use."""

    def __init__(self, workspace: Path, visual_client=None):
        self.root = workspace / "technical-evidence"
        self.attempted: set[str] = set()
        self.visual_client = visual_client

    def _visual(self, documents, part):
        if self.visual_client is None:
            return documents
        from agents.ratsnestpro.document_pin_table import extract_visual_pin_table
        from ratsnestpro.orchestration.pipeline import _datasheet_package_evidence
        for document in documents:
            if document.get("authority") != "official_manufacturer_datasheet":
                continue
            if _datasheet_package_evidence(
                part, source_identity=part.requested_identity or part.mpn or part.value,
                datasheet=document,
            ) is not None:
                continue
            if document.get("visual_extractor_version") != 9:
                document.pop("visual_extraction_error", None)
                try:
                    document["visual_pin_table"] = extract_visual_pin_table(
                        document, self.root, part, self.visual_client,
                    )
                    from agents.ratsnestpro.pin_evidence import pin_differences
                    rows = symbols.symbol_pins(part.symbol) or []
                    table = document["visual_pin_table"]
                    differences = pin_differences(rows, table)
                    targets = [d["number"] for d in differences
                               if d["reason"] == "pin_function_mismatch"]
                    if table and targets:
                        correction = extract_visual_pin_table(
                            document, self.root, part, self.visual_client, target_numbers=targets,
                        )
                        if correction:
                            document["visual_initial_pin_table"] = table
                            corrected = {p["number"]: p for p in correction["pins"]}
                            table = {**table, "pins": [corrected.get(p["number"], p) for p in table["pins"]]}
                            document["visual_pin_table"] = table
                    document["pin_differences"] = pin_differences(rows, table)
                except Exception as exc:
                    document["visual_pin_table"] = None
                    document["visual_extraction_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                document["visual_extractor_version"] = 9
        return documents

    def __call__(self, part):
        identity = part.requested_identity or part.mpn or part.value
        from agents.ratsnestpro.local_datasheets import find_document
        local = find_document(identity, document_store=self.root)
        if local is not None:
            local_key = hashlib.sha256(json.dumps([local["source_sha256"], identity,
                                                   part.symbol, part.footprint]).encode()).hexdigest()
            receipt = self.root / ("local-" + local_key + ".json")
            if receipt.exists():
                cached_local = json.loads(receipt.read_text(encoding="utf-8"))
                if cached_local.get("source_sha256") == local["source_sha256"]:
                    for field in ("visual_pin_table", "visual_extractor_version", "visual_extraction_error", "pin_differences", "visual_initial_pin_table"):
                        if field in cached_local:
                            local[field] = cached_local[field]
            documents = self._visual([local], part)
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps(documents[0]), encoding="utf-8")
            temporary.replace(receipt)
            return documents
        key = hashlib.sha256(json.dumps([
            identity, part.symbol, part.footprint,
        ]).encode()).hexdigest()
        path = self.root / (key + ".json")
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached["observed_at"] < 3600:
                documents = self._visual(cached["documents"], part)
                cached["documents"] = documents
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
                return documents
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if key in self.attempted:
            return []
        self.attempted.add(key)
        urls = []
        properties = symbols.symbol_properties(part.symbol)
        declared = properties.get("Datasheet", "")
        if declared.startswith("https://") and _official_manufacturer_domain(declared):
            urls.append(declared)
        errors = []
        documents = []
        try:
            search = json.loads(web_search_official_manufacturer.invoke({
                "query": f'"{identity}" official datasheet pin assignment package',
            }))
            for result in search.get("results", []):
                url = result.get("href") or result.get("url") or result.get("source_url", "")
                if url.startswith("https://") and _official_manufacturer_domain(url):
                    urls.append(url)
        except Exception as exc:
            errors.append(type(exc).__name__)
        for url in list(dict.fromkeys(urls))[:2]:
            try:
                document = _read_datasheet(
                    url, f"{identity} pin assignment pin description {part.footprint}",
                    8, document_store=self.root,
                )
                trusted = (
                    document.get("retrieval_method") != "official_document_text_proxy"
                    and official_datasheet_evidence_sufficient(identity, document)
                )
                document["authority"] = "official_manufacturer_datasheet" if trusted else "unverified"
                document["evidence_sufficient"] = trusted
                documents.append(document)
            except Exception as exc:
                errors.append(type(exc).__name__)
        self.root.mkdir(parents=True, exist_ok=True)
        documents = self._visual(documents, part)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "identity": identity, "observed_at": time.time(),
            "documents": documents, "errors": errors,
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return documents
