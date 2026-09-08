"""Draft admission and final repair over the existing, authoritative EDA steps.

Deferral changes scheduling only: errors remain errors and release checks are
always run with the original policy. No model can mark a deferred issue passed.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def deferred_checks(step, artifact, checks):
    from ratsnestpro.orchestration.ahe import FailureOrigin

    if artifact is None:
        return checks
    result = []
    for check in checks:
        defer = not check.blocks_execution
        # An installed, compatible binding may be used in a draft while its
        # independent source is being verified. Never admit a placeholder,
        # incompatible pad map or missing/tampered manifest this way.
        if step.value == "selection" and check.name == "prepared_component_manifest":
            diagnostics = check.evidence.get("component_diagnostics", [])
            defer = (
                check.reason_code == "independent_package_evidence_missing"
                and bool(diagnostics)
                and all(
                    d.get("blockers") == ["independent_package_evidence_missing"]
                    and "verified_local_kicad_binding" in d.get("available_source_kinds", [])
                    for d in diagnostics
                )
            )
        if check.name in {"power_domains_have_erc_drivers", "design_ir_matches_kicad_netlist"}:
            schematic = getattr(artifact, "sch_path", "")
            defer = bool(schematic and Path(schematic).is_file())
            if check.name == "design_ir_matches_kicad_netlist":
                defer = defer and bool(getattr(artifact, "connectivity_checked", False)) and not getattr(
                    artifact, "connectivity_error", "",
                )
        if check.origin in {FailureOrigin.INFRASTRUCTURE, FailureOrigin.HARNESS}:
            defer = False
        if not check.ok and defer:
            check = check.model_copy(update={
                "blocks_execution": False,
                "evidence": {**check.evidence, "deferred_until_final_repair": True,
                             "original_blocks_execution": check.blocks_execution},
            })
        result.append(check)
    return result


def defer_result(state, result, artifact, emit=None):
    from ratsnestpro.orchestration.pipeline import Severity, _artifact_fingerprint

    result.checks = deferred_checks(result.step, artifact, result.checks)
    result.execution_blocked = any(
        not c.ok and c.severity == Severity.ERROR and c.blocks_execution
        for c in result.checks
    )
    # Persist exact failure evidence, not only a natural-language summary.
    ledger = state.draft_execution.setdefault("issues", {})
    ledger[result.step.value] = {
        "artifact_fingerprint": _artifact_fingerprint(artifact),
        "checks": [c.model_dump(mode="json") for c in result.error_checks],
    }
    if result.error_checks and not result.execution_blocked and emit:
        emit({"event": "draft.issues_deferred", "step": result.step.value,
              "issue_count": len(result.error_checks), "release_ready": False})


def dependency_fingerprint(step, artifact):
    """Evidence/rationale changes do not invalidate an electrically identical BOM."""
    data = artifact.model_dump(mode="json", exclude={"rationale", "note"})
    if step.value == "selection":
        data = [{k: part.get(k) for k in
                 ("ref", "value", "mpn", "symbol", "footprint", "role", "asset_lock_digest")}
                for part in data.get("parts", [])]
    else:
        # Path strings alone cannot detect an in-place CAD edit.
        data = {"artifact": data, "cad_digests": {
            key: hashlib.sha256(Path(value).read_bytes()).hexdigest()
            for key, value in data.items()
            if isinstance(value, str) and value.endswith((".kicad_pcb", ".kicad_sch"))
            and Path(value).is_file()
        }}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def recover_draft_transaction(state, ctx):
    """An interrupted joint transaction rolls back to its durable full draft."""
    from ratsnestpro.orchestration import pipeline as p

    pending = state.draft_execution.get("active_candidate")
    if not pending:
        return
    snapshot = p.CandidateStateSnapshot.model_validate(pending)
    state.revision += 1
    p._restore_candidate_baseline(state, ctx, snapshot)
    state.draft_execution.pop("active_candidate", None)
    state.draft_execution["phase"] = "final_repair"
    for result in state.results:
        defer_result(state, result, state.artifact(result.step))
    if ctx.on_progress_checkpoint:
        ctx.on_progress_checkpoint(state)


def finalize_draft(state, ctx, *, max_passes=3):
    """Repair existing artifacts with the selected strong client, then rebuild.

The caller supplies the final model in ctx.client. Existing step repairs own
schematic/netlist edits; the sandbox owns physical PCB edits. The canonical
runner handles upstream transactions and deterministic verification.
"""
    from ratsnestpro.orchestration import pipeline as p

    meta = state.draft_execution
    if len(state.results) != len(p.CANONICAL_ORDER):
        return
    meta.update({"policy": "draft-then-repair.v1", "phase": "final_repair",
                 "draft_completed_steps": len(state.results)})
    final = copy.copy(ctx)
    final.draft_first = False
    final.artifact_first = True
    final.repair_release_issues = True
    final.design_repair_attempts = max(1, final.design_repair_attempts)
    final.repair_attempts = max(1, final.repair_attempts)
    if final.strong_repair is not None:
        final.strong_repair = copy.copy(final.strong_repair)
        final.strong_repair.limits = final.strong_repair.limits.model_copy(
            update={"stagnation_threshold": 1},
        )
    # Every decision sees the complete initial defect ledger, even when its
    # owning step is earlier than the PCB stage.
    final.repair_feedback = ""
    final.final_review_context = "Final engineering repair of an existing draft. " + json.dumps(
        meta.get("issues", {}), ensure_ascii=False, default=str,
    )[:24000]

    def save():
        if final.on_progress_checkpoint:
            final.on_progress_checkpoint(state)

    def emit(event, **details):
        if final.on_ahe_event:
            final.on_ahe_event({"event": event, **details})

    emit("draft.final_repair_started", draft_completed_steps=17)
    save()
    while True:
        # Revalidate ALL retained artifacts. A deferred flag is never a pass.
        first = None
        for index, implementation in enumerate(p.ALL_STEPS):
            artifact = state.artifact(implementation.step)
            checks = implementation.check(state, artifact)
            errors = [c for c in checks if not c.ok and c.severity == p.Severity.ERROR]
            result = state.results[index]
            result.checks = checks
            result.blocked = bool(errors)
            result.execution_blocked = any(c.blocks_execution for c in errors)
            if errors and first is None:
                first = index
        if first is None:
            meta["phase"] = "verified"
            save()
            emit("draft.final_repair_verified")
            return
        if int(meta.get("repair_passes", 0)) >= max_passes:
            meta["phase"] = "needs_attention"
            save()
            return
        meta["repair_passes"] = int(meta.get("repair_passes", 0)) + 1
        meta["repair_owner"] = p.CANONICAL_ORDER[first].value
        save()  # Charge the pass before any paid call or destructive candidate.
        originals = dict(state.artifacts)
        original_fingerprints = {s: dependency_fingerprint(s, a) for s, a in originals.items()}
        previous_results = {r.step: r for r in state.results}
        baseline = p._capture_candidate_baseline(state, final, "final-draft-" + str(meta["repair_passes"]))
        meta["active_candidate"] = baseline.model_dump(mode="json")
        state.revision += 1
        targets = p.CANONICAL_ORDER[first:]
        for target in targets:
            state.resume_candidates[target] = (originals[target], previous_results[target].used_llm)
        # BOM/plots/drills and their release identity must be generated from
        # the repaired design, never accepted as cached draft exports.
        state.resume_candidates.pop(p.PipelineStep.MANUFACTURE, None)
        state.results = state.results[:first]
        state.artifacts = {k: v for k, v in originals.items() if k not in targets}
        save()
        original_callback = final.on_step_completed

        def completed(current, result):
            before = originals.get(result.step)
            after = current.artifact(result.step)
            changed = before is None or original_fingerprints[result.step] != dependency_fingerprint(result.step, after)
            index = p._ORDER_INDEX[result.step]
            if changed:
                # Keep valid ancestors; regenerate dependent artifacts from the
                # changed engineering contract. Existing files remain candidate
                # inputs and are protected by the outer snapshot.
                for dependent in p.CANONICAL_ORDER[index + 1:]:
                    current.resume_candidates.pop(dependent, None)
                emit("draft.dependencies_invalidated", owner=result.step.value,
                     steps=[s.value for s in p.CANONICAL_ORDER[index + 1:]])
            if original_callback:
                original_callback(current, result)

        final.on_step_completed = completed
        try:
            # Refresh missing independent evidence on the existing selection;
            # no need to reselect parts merely to parse a PDF again.
            if p.CANONICAL_ORDER[first] == p.PipelineStep.SELECTION:
                selected, used = state.resume_candidates[p.PipelineStep.SELECTION]
                selected, _ = p._prepare_and_persist_components(
                    selected.model_copy(deep=True), state, final,
                    preserve_requested_identities=True,
                )
                state.resume_candidates[p.PipelineStep.SELECTION] = (selected, used)
            p.Pipeline().run(state, final)
        except BaseException:
            # Preserve the complete draft across provider/tool failure. Never
            # rollback the finalization admission ledger or paid-call records.
            durable_meta = copy.deepcopy(meta)
            p._restore_candidate_baseline(state, final, baseline)
            durable_meta.pop("active_candidate", None)
            state.draft_execution = durable_meta
            save()
            raise
        finally:
            final.on_step_completed = original_callback
        if len(state.results) != len(p.CANONICAL_ORDER) or state.execution_blocked:
            failure = [{"step": r.step.value, "checks": [c.model_dump(mode="json") for c in r.error_checks]}
                       for r in state.results if r.error_checks]
            durable_meta = copy.deepcopy(meta)
            p._restore_candidate_baseline(state, final, baseline)
            durable_meta.pop("active_candidate", None)
            state.draft_execution = meta = durable_meta
            meta.update({"phase": "needs_attention", "final_failure": failure})
            save()
            emit("draft.final_repair_needs_attention", failures=failure)
            return
        meta.pop("active_candidate", None)
        save()
        p._commit_candidate_baseline(final, baseline)
