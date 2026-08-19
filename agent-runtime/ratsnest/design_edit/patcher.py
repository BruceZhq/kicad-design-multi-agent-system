"""PatchPlan applier: ops -> file edits, with hashes, validation, rollback.

Safety invariants (design doc §4.4 roster / risks):
- ops-only editing; the LLM never writes S-expressions
- SHA-256 before/after recorded for every touched file
- post-edit validation re-parses the project with the real analyzer;
  on failure the original bytes are restored (rolled_back=True)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ratsnest.config import Config
from ratsnest.design_edit.sexp_edit import apply_property_updates
from ratsnest.kh_adapter.runner import AdapterError, KicadHappyAdapter, find_root_schematic
from ratsnest.schemas import PatchPlan, PatchResult, RepairOpType


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Patcher:
    def __init__(self, config: Config | None = None,
                 adapter: KicadHappyAdapter | None = None):
        self.config = config or Config.load()
        self.adapter = adapter or KicadHappyAdapter(self.config)

    def apply(self, plan: PatchPlan, project_dir: Path) -> PatchResult:
        project_dir = Path(project_dir)
        sch_path = find_root_schematic(project_dir)

        # 1. Translate ops -> property updates (editor v1: value/property only)
        updates: dict[str, dict[str, str]] = {}
        for op in plan.ops:
            if op.op == RepairOpType.set_value:
                updates.setdefault(op.ref, {})["Value"] = op.params["value"]
            elif op.op == RepairOpType.set_property:
                updates.setdefault(op.ref, {})[op.params["name"]] = op.params["value"]
            else:
                return PatchResult(
                    plan_id=plan.plan_id, applied=False,
                    error=f"op '{op.op.value}' not supported by editor v1",
                )
        if not updates:
            return PatchResult(plan_id=plan.plan_id, applied=False,
                               error="empty patch plan")

        original = sch_path.read_text(encoding="utf-8")
        before = _sha256(original)

        # 2. Apply in-memory; any unresolved reference fails the whole plan
        new_text, change_log = apply_property_updates(original, updates, self.config)
        errors = [e for e in change_log if e.get("action") == "error"]
        if errors:
            return PatchResult(
                plan_id=plan.plan_id, applied=False, change_log=change_log,
                error="; ".join(e.get("message", "unknown") for e in errors),
            )

        # 3. Write, then validate by re-parsing with the real analyzer
        sch_path.write_text(new_text, encoding="utf-8")
        try:
            self.adapter.analyze_schematic(project_dir)
        except AdapterError as exc:
            sch_path.write_text(original, encoding="utf-8")  # rollback
            return PatchResult(
                plan_id=plan.plan_id, applied=False, change_log=change_log,
                error=f"post-edit validation failed: {exc}", rolled_back=True,
            )

        return PatchResult(
            plan_id=plan.plan_id, applied=True,
            changed_files={str(sch_path.relative_to(project_dir)): {
                "before": before, "after": _sha256(new_text)}},
            change_log=change_log,
        )
