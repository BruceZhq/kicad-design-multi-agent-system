"""Project-local correction of electrical conflicts proven by a PDF table.

No global KiCad library is edited. Only exact, independently mapped table rows
can change a pin's name/type; all other pins retain their installed definition.
"""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from agents.ratsnestpro.pin_evidence import functions, pin_differences
from ratsnestpro.eda import symbols


def table_pin_rows(layout: str, plain: str, package: str):
    headers = list(dict.fromkeys(re.findall(r"\b(?:LQFP|TQFP|QFN|TSSOP|SOIC)\s*[-]?\s*\d+\b", plain, re.I)))
    normalized = [re.sub(r"\W", "", h).lower() for h in headers]
    if package not in normalized:
        return []
    index = normalized.index(package)
    result = []
    for line in layout.splitlines():
        cells = re.split(r"\s{2,}", line.strip())
        n = len(headers)
        if len(cells) < n + 2 or not all(re.fullmatch(r"\d+|-+", c) for c in cells[:n]):
            continue
        if not cells[index].isdigit():
            continue
        name, kind = cells[n:n + 2]
        types = {"I/O": "bidirectional", "I": "input", "O": "output", "NC": "no_connect"}
        if kind not in types or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+/-]*", name):
            continue
        result.append({"number": cells[index], "name": name, "type": types[kind]})
    return result


def repair_symbol(part, document: dict, workspace: Path):
    from pypdf import PdfReader
    from ratsnestpro.eda.local_library import generate_local_symbol_library
    table = document.get("visual_pin_table")
    rows = symbols.symbol_pins(part.symbol) or []
    differences = pin_differences(rows, table)
    if not differences or any(d["reason"] != "symbol_electrical_type_conflict" for d in differences):
        return None
    digest = document.get("source_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    path = workspace / "technical-evidence" / (digest + ".pdf")
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        return None
    match = re.search(r"(?:LQFP|TQFP|QFN|TSSOP|SOIC)[-_]?\d+", part.footprint, re.I)
    if not match:
        return None
    package = re.sub(r"\W", "", match.group()).lower()
    evidence = {}
    reader = PdfReader(path)
    for page_number, page in enumerate(reader.pages, 1):
        plain = page.extract_text() or ""
        if "Pin assignment" not in plain and "Pin description" not in plain:
            continue
        for row in table_pin_rows(page.extract_text(extraction_mode="layout") or "", plain, package):
            evidence.setdefault(row["number"], []).append({**row, "page": page_number})
    changes = {}
    for difference in differences:
        candidates = evidence.get(difference["number"], [])
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        observed = set().union(*(functions(f) for f in difference["observed_functions"]))
        if functions(candidate["name"]) != observed or candidate["type"] == "no_connect":
            return None
        changes[difference["number"]] = candidate
    identity = part.requested_identity or part.mpn or part.value
    source = document["source_url"]
    pages = sorted(set([p["page"] for p in document["matched_pages"]] + [r["page"] for r in changes.values()]))
    spec = {"device_id": identity, "manufacturer": part.manufacturer or urlparse(source).hostname,
            "official_domains": [urlparse(source).hostname], "declared_pin_count": len(rows),
            "package_name": match.group(), "footprint_lib_id": part.footprint,
            "pins": [{"number": r["number"], "pad_number": r["number"],
                      "name": changes.get(r["number"], r)["name"],
                      "electrical_type": changes.get(r["number"], r)["type"]} for r in rows],
            "evidence": [{"device_id": identity, "url": source, "page_numbers": pages,
                          "document_id": digest, "covers": ["identity", "pin_table", "package_dimensions"]}]}
    generated = generate_local_symbol_library(spec, allowed_footprint_lib_ids=[part.footprint],
                                              root=workspace / "evidence-symbols", project_dir=workspace)
    if not generated.artifacts:
        return None
    audit = {"original_symbol": part.symbol, "replacement_symbol": generated.artifacts.symbol_lib_id,
             "document_sha256": digest, "pin_changes": changes}
    (workspace / "technical-evidence" / (part.ref + "-symbol-repair.json")).write_text(json.dumps(audit), encoding="utf-8")
    return part.model_copy(update={"symbol": generated.artifacts.symbol_lib_id, "value": identity})
