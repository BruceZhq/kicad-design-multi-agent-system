"""Typed upstream mutations for the isolated PCB candidate, not production writes."""

import copy
import hashlib
import json


def fingerprint(artifacts):
    return hashlib.sha256(json.dumps(
        {key.value: value.model_dump(mode="json") for key, value in artifacts.items()},
        sort_keys=True, default=str,
    ).encode()).hexdigest()


def apply_upstream(host, proposal):
    from ratsnestpro.orchestration import pipeline as p

    state = host.view_state
    partition = state.artifact(p.PipelineStep.LAYOUT_PARTITION)
    selection = state.artifact(p.PipelineStep.SELECTION)
    refs = {part.ref for part in selection.parts}
    zones = {zone.name for zone in partition.zones}
    if not set(proposal.zone_bindings) <= refs:
        raise ValueError("joint candidate contains unknown component references")
    if not set(proposal.zone_bindings.values()) <= zones:
        raise ValueError("joint candidate must bind existing zones, not invent regions")
    if proposal.zone_bindings:
        _, ambiguous = p._resolved_zone_targets(state)
        # Resolve ambiguous derived ownership only. This channel cannot override
        # already-unambiguous/user-constrained placement ownership.
        if not set(proposal.zone_bindings) <= set(ambiguous):
            raise ValueError("only ambiguous zone ownership can be changed")
        # Existing heuristic scores are the faulty input, not an allowlist of
        # correct destinations. Permit any existing region for an ambiguous
        # ref; downstream physical/functional constraints remain authoritative.
        state.artifacts[p.PipelineStep.LAYOUT_PARTITION] = partition.model_copy(
            update={"zone_bindings": {**partition.zone_bindings, **proposal.zone_bindings}}, deep=True,
        )
    if proposal.refresh_evidence:
        ctx = copy.copy(host.ctx)
        ctx.out_dir = str(host.root)
        ctx.draft_first = False
        before = [(x.ref, x.mpn, x.symbol, x.footprint) for x in selection.parts]
        updated, _ = p._prepare_and_persist_components(
            selection.model_copy(deep=True), state, ctx, preserve_requested_identities=True,
        )
        if [(x.ref, x.mpn, x.symbol, x.footprint) for x in updated.parts] != before:
            raise ValueError("joint evidence refresh cannot change locked component identities")
        state.artifacts[p.PipelineStep.SELECTION] = updated


def synchronize_placements(host):
    from ratsnestpro.eda.vendor.pcb import PcbBoard

    p = host.p
    positions = {f["reference"]: f["at"] for f in PcbBoard.load(host.pcb).list_footprints()}
    for step in (p.PipelineStep.LAYOUT_CRITICAL, p.PipelineStep.LAYOUT_GENERAL):
        plan = host.view_state.artifact(step)
        if plan is not None:
            host.view_state.artifacts[step] = plan.model_copy(update={
                "placements": [item.model_copy(update=positions[item.ref]) for item in plan.placements],
            })


def upstream_errors(host):
    p = host.p
    checks = []
    for step in (p.PipelineStep.SELECTION, p.PipelineStep.LAYOUT_PARTITION,
                 p.PipelineStep.LAYOUT_CRITICAL, p.PipelineStep.LAYOUT_GENERAL):
        artifact = host.view_state.artifact(step)
        if artifact is not None:
            checks.extend((step.value + ":" + check.name, check.message)
                          for check in p.ALL_STEPS[p._ORDER_INDEX[step]].check(host.view_state, artifact)
                          if not check.ok and check.severity == p.Severity.ERROR)
    return checks
