"""Operator-curated technical documents, shared by Parts and CAD preparation.

Not a chat upload API: only an operator can approve source provenance. Content
hashes protect stored receipts; electrical validation remains downstream.
"""
import hashlib
import json
import os
from pathlib import Path

from agents.ratsnestpro.web_tools import _official_manufacturer_domain, _parse_datasheet_pdf


def registry_root() -> Path:
    return Path(os.environ.get("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro")) / "reference-datasheets"


def _key(identity: str) -> str:
    return hashlib.sha256(identity.strip().casefold().encode()).hexdigest()


def import_document(path: Path, identity: str, source_url: str, *, approve_source: bool = False) -> str:
    if not approve_source or not source_url.startswith("https://") or not _official_manufacturer_domain(source_url):
        raise ValueError("Operator approval and an official manufacturer source URL are required")
    content = path.read_bytes()
    document = _parse_datasheet_pdf(content, source_url, identity + " pinout pin description package", 8)
    digest = document["source_sha256"]
    root = registry_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / (digest + ".pdf")).write_bytes(content)
    record = {"identity": identity, "source_url": source_url, "sha256": digest,
              "provenance": "operator_attested_manufacturer_pdf"}
    target = root / (_key(identity) + ".json")
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    temporary.replace(target)
    return digest


def find_document(identity: str, *, document_store: Path | None = None) -> dict | None:
    root = registry_root()
    record_path = root / (_key(identity) + ".json")
    if not record_path.exists():
        return None
    record = json.loads(record_path.read_text(encoding="utf-8"))
    digest = record["sha256"]
    if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or record.get("identity", "").casefold() != identity.strip().casefold()
            or record.get("provenance") != "operator_attested_manufacturer_pdf"
            or not _official_manufacturer_domain(record["source_url"])):
        raise ValueError("Invalid local datasheet receipt")
    content = (root / (digest + ".pdf")).read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Local datasheet content hash mismatch")
    document = _parse_datasheet_pdf(content, record["source_url"],
                                   identity + " pinout pin description package", 8,
                                   document_store=document_store)
    document.update(authority="official_manufacturer_datasheet", evidence_sufficient=True,
                    retrieval_method="operator_imported_pdf", provenance=record["provenance"])
    return document


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--approve-source", action="store_true")
    args = parser.parse_args()
    print(import_document(args.path, args.identity, args.source_url, approve_source=args.approve_source))
