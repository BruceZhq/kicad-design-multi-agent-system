"""Evidence-bound, bounded Reviewer -> Hardware checkpoint handoffs.

Only the trusted reviewer writes these receipts. LLM workspace tools cannot
write them. A receipt invalidates a suffix, never grants release or changes
the original requirement. Changed evidence invalidates unused receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

RECEIPT = "review_repair.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_review_repair(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / RECEIPT).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def valid_review_resume(root: Path, step: str) -> bool:
    receipt = load_review_repair(root)
    if receipt.get("status") != "requested" or receipt.get("resume_from_step") != step:
        return False
    try:
        hashes = receipt["file_sha256"]
        if "pipeline_state.json" not in hashes or not hashes:
            return False
        return all((root / name).resolve().is_relative_to(root.resolve())
                   and _sha(root / name) == digest for name, digest in hashes.items())
    except (OSError, KeyError, TypeError):
        return False


def prepare_review_repair(root: Path, review: dict, *, max_attempts: int = 2) -> dict:
    from ratsnestpro.orchestration.pipeline import PipelineStep

    root = root.resolve()
    if review.get("status") != "blocked" or not root.is_dir():
        return {}
    checkpoint = root / "pipeline_state.json"
    if not checkpoint.is_file():
        return {"status": "unavailable", "reason": "no original checkpoint"}
    verification = review.get("verification", {})
    targets = []
    evidence: dict[str, Any] = {}
    paths = [checkpoint]
    for kind, default in (("erc", "schematic_connections"), ("drc", "route_signals")):
        result = verification.get(kind, {})
        # Tool outages have infrastructure ownership; do not redesign a board.
        if result.get("ran") is not True:
            continue
        warning_blocks = any(
            isinstance(value, dict) and value.get("blocks_release") is True
            for value in result.get("warning_classifications", {}).values()
        )
        if not (result.get("errors", 0) or result.get("unconnected", 0) or warning_blocks):
            continue
        report = Path(str(result.get("report_path", ""))).resolve()
        if not report.is_relative_to(root.resolve()) or not report.is_file():
            continue
        detail = json.loads(report.read_text(encoding="utf-8"))
        evidence[kind] = detail  # Keep UUIDs, pins and positions, not just the summary count.
        paths.append(report)
        types = set(result.get("by_type", {}))
        if kind == "erc" and types and types <= {"endpoint_off_grid", "label_dangling", "pin_not_connected"}:
            default = "schematic_layout"
        if kind == "drc" and types & {"solder_mask_bridge", "courtyards_overlap", "footprint_overlap"}:
            default = "layout_general"
        targets.append(default)
    placement = review.get("placement_constraints") or {}
    if placement.get("violations"):
        targets.append("layout_general")
        evidence["placement"] = placement
    components = review.get("component_release", {})
    if components.get("blockers"):
        targets.append("selection")
        evidence["components"] = components
    if not targets:
        return {"status": "not_actionable", "reason": "no deterministic design-owned findings"}
    for key in ("schematic_path", "pcb_path"):
        path = Path(str(review.get(key, ""))).resolve()
        if path.is_relative_to(root.resolve()) and path.is_file():
            paths.append(path)
    previous = load_review_repair(root)
    signature = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    attempts = int(previous.get("attempt", 0))
    if attempts >= max_attempts or signature == previous.get("finding_sha256"):
        return {"status": "exhausted", "attempt": attempts, "reason": "review repair budget or no-progress limit"}
    order = [s.value for s in PipelineStep]
    # Never leap past a pre-existing earlier failure.
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    for item in state.get("steps", []):
        if item.get("name") in order and (
            item.get("failed_checks") or item.get("blocked") is True or item.get("execution_blocked") is True
            or any(c.get("ok") is False for c in item.get("checks", []))
        ):
            targets.append(item["name"])
    owner = min(targets, key=order.index)
    evidence["previous_owner_artifact"] = state.get("intermediate_artifacts", {}).get(owner, {})
    cad_files = {"schematic" if p.suffix == ".kicad_sch" else "pcb": str(p.relative_to(root))
                 for p in paths if p.suffix in {".kicad_sch", ".kicad_pcb"}}
    receipt = {"schema_version": 1, "status": "requested", "attempt": attempts + 1,
               "resume_from_step": owner, "finding_sha256": signature, "cad_files": cad_files,
               "requirement_sha256": hashlib.sha256(str(state.get("requirement", "")).encode()).hexdigest(),
               "file_sha256": {str(path.relative_to(root.resolve())): _sha(path) for path in paths},
               "evidence": evidence}
    # Bounded payloads still retain complete reports on disk for paginated inspection.
    if len(json.dumps(evidence)) > 48_000:
        receipt["evidence"] = {"reports": [str(p.relative_to(root.resolve())) for p in paths[1:]],
                               "summary": verification}
    fd, temporary = tempfile.mkstemp(prefix=".review-repair-", dir=root)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False)
    os.replace(temporary, root / RECEIPT)
    return receipt
