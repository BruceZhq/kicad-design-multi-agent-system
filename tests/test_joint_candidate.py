import copy
from types import SimpleNamespace

import pytest

from ratsnestpro.orchestration import pipeline as p
from ratsnestpro.repair.contracts import RepairProposal
from ratsnestpro.repair.joint_candidate import apply_upstream
from ratsnestpro.repair.pipeline_adapter import _BoardHost


def host_fixture(tmp_path):
    state = p.PipelineState("keep board dimensions and component identity")
    state.artifacts[p.PipelineStep.SELECTION] = p.SelectionPlan(parts=[
        p.SelectedPart(ref="U2", symbol="Regulator:Part", value="Part", footprint="Package:Part", role="ldo_regulator"),
    ])
    state.artifacts[p.PipelineStep.LAYOUT_PARTITION] = p.BoardPartition(
        board_width=40, board_height=30, zones=[
            p.BoardZone(name=name, kind="power", x1=x, y1=0, x2=x+10, y2=10)
            for name, x in [("input", 0), ("output", 20)]
        ],
    )
    host = _BoardHost.__new__(_BoardHost)
    host.p, host.state, host.joint = p, state, True
    host.view_state = copy.copy(state)
    host.view_state.artifacts = copy.deepcopy(state.artifacts)
    host.ctx = p.PipelineContext()
    host.root = tmp_path
    host.pcb = tmp_path / "candidate.kicad_pcb"
    host.pcb.write_bytes(b"original PCB")
    host.last = None
    return host


def test_topology_ownership_preserves_membership_and_live_state(tmp_path):
    from ratsnestpro.orchestration.pipeline_contracts import TopologyBlock
    host = host_fixture(tmp_path)
    topology = p.TopologyPlan(blocks=[
        TopologyBlock(name=name, kind='power', implementation_refs=['U2'])
        for name in ('supply', 'regulator')
    ], rails=['3V3'], ground_domains=['GND'])
    host.state.artifacts[p.PipelineStep.TOPOLOGY] = topology
    host.view_state.artifacts[p.PipelineStep.TOPOLOGY] = topology.model_copy(deep=True)
    apply_upstream(host, RepairProposal(action='joint_candidate', rationale='U2 is the regulator',
                                       topology_owners={'U2': 'regulator'}))
    candidate = host.view_state.artifact(p.PipelineStep.TOPOLOGY)
    assert candidate.owner_bindings == {'u2': 'regulator'}
    assert [b.implementation_refs for b in candidate.blocks] == [['U2'], ['U2']]
    assert not topology.owner_bindings
    check = next(c for c in p.TopologyStep().check(host.view_state, candidate)
                 if c.name == 'implementation_ref_has_unique_owner')
    assert check.ok
    for bindings in ({'U2': 'supply'}, {'U9': 'regulator'}, {'U2': 'invented'}):
        with pytest.raises(ValueError):
            apply_upstream(host, RepairProposal(action='joint_candidate', rationale='invalid', topology_owners=bindings))


def test_explicit_binding_resolves_only_candidate_state(tmp_path):
    host = host_fixture(tmp_path)
    assert "U2" in p._resolved_zone_targets(host.state)[1]
    apply_upstream(host, RepairProposal(action="joint_candidate", rationale="owner", zone_bindings={"U2": "input"}))
    assert p._resolved_zone_targets(host.view_state)[0]["U2"] == (5, 5)
    assert not p._resolved_zone_targets(host.view_state)[1]
    assert "U2" in p._resolved_zone_targets(host.state)[1]
    assert host.view_state.artifact(p.PipelineStep.LAYOUT_PARTITION).board_width == 40


@pytest.mark.parametrize("bindings", [{"U9": "input"}, {"U2": "invented"}])
def test_unknown_refs_or_zones_rejected(tmp_path, bindings):
    with pytest.raises(ValueError):
        apply_upstream(host_fixture(tmp_path), RepairProposal(action="joint_candidate", rationale="owner", zone_bindings=bindings))


def test_evidence_is_prepared_by_host_not_model(tmp_path, monkeypatch):
    host = host_fixture(tmp_path)
    calls = []
    def prepare(selection, state, ctx, **kwargs):
        assert ctx.out_dir == str(tmp_path) and not ctx.draft_first
        assert kwargs["preserve_requested_identities"]
        calls.append(True)
        return selection.model_copy(update={"prepared_manifest_path": str(tmp_path / "verified.json")}), None
    monkeypatch.setattr(p, "_prepare_and_persist_components", prepare)
    def persist(selection, closure, ctx):
        assert selection.prepared_manifest_path == str(tmp_path / 'verified.json')
        assert ctx.out_dir == str(tmp_path)
        calls.append('closure_persisted')
        return selection.model_copy(update={'component_closure_path': str(tmp_path / 'component-closure.json')})
    monkeypatch.setattr(p, '_persist_component_closure', persist)
    apply_upstream(host, RepairProposal(action="joint_candidate", rationale="verify", refresh_evidence=True))
    assert calls == [True, 'closure_persisted']
    assert host.view_state.artifact(p.PipelineStep.SELECTION).component_closure_path == str(tmp_path / 'component-closure.json')
    assert not host.state.artifact(p.PipelineStep.SELECTION).prepared_manifest_path
    with pytest.raises(ValueError):
        RepairProposal.model_validate({"action": "joint_candidate", "rationale": "fake", "release_ready": True})


def test_failed_pcb_script_rolls_back_upstream_and_pcb(tmp_path):
    host = host_fixture(tmp_path)
    def fail(script, timeout):
        host.pcb.write_bytes(b"partial edit")
        return {"status": "failed"}
    host.execute = fail
    result = host.execute_joint(RepairProposal(action="joint_candidate", rationale="together", zone_bindings={"U2": "input"}, script="edit"), 10)
    assert result["status"] == "failed"
    assert not host.view_state.artifact(p.PipelineStep.LAYOUT_PARTITION).zone_bindings
    assert host.pcb.read_bytes() == b"original PCB"


def test_candidate_rollback_restores_both_state_and_file(tmp_path):
    host = host_fixture(tmp_path)
    host.best = tmp_path / "best"
    host.workspace = SimpleNamespace(images={})
    host.checkpoint_candidate()
    apply_upstream(host, RepairProposal(action="joint_candidate", rationale="owner", zone_bindings={"U2": "input"}))
    host.pcb.write_bytes(b"new PCB")
    host.rollback_candidate()
    assert not host.view_state.artifact(p.PipelineStep.LAYOUT_PARTITION).zone_bindings
    assert host.pcb.read_bytes() == b"original PCB"


def test_commit_rejects_stale_upstream_state(tmp_path):
    from ratsnestpro.repair.joint_candidate import fingerprint

    host = host_fixture(tmp_path)
    host.source_state_digest = fingerprint(host.state.artifacts)
    host.state.artifact(p.PipelineStep.LAYOUT_PARTITION).board_width = 50
    with pytest.raises(RuntimeError, match="upstream State changed"):
        host.commit("unused")


@pytest.mark.parametrize('owner', list(p.CANONICAL_ORDER))
def test_full_draft_enters_joint_channel_before_prefix_truncation(tmp_path, monkeypatch, owner):
    from ratsnestpro.repair import draft, pipeline_adapter
    from ratsnestpro.repair.contracts import RepairLimits

    state = host_fixture(tmp_path).state
    for step in p.CANONICAL_ORDER:
        state.artifacts.setdefault(step, p.RouteResult())
        state.results.append(p.StepResult(step=step))
    state.artifacts[p.PipelineStep.ROUTE_SIGNALS] = p.RouteResult(method="freerouting")
    checks = [SimpleNamespace(step=step, check=lambda *_: []) for step in p.CANONICAL_ORDER]
    checks[p._ORDER_INDEX[owner]].check = lambda *_: [
        p.CheckResult(name="unambiguous_zone_binding", ok=False)]
    monkeypatch.setattr(p, "ALL_STEPS", checks)
    monkeypatch.setenv('RATSNEST_A2A_REPAIR_URL', 'http://external/a2a')
    entered = []
    def repair(current, ctx, artifact, *, joint):
        assert joint and len(current.artifacts) == 17 and len(current.results) == 17
        entered.append(True)
        return None  # Failed candidates must leave the intact draft unpublishable.
    monkeypatch.setattr(pipeline_adapter, "try_strong_repair", repair)
    ctx = p.PipelineContext(out_dir=str(tmp_path), strong_repair=SimpleNamespace(limits=RepairLimits()))
    draft.finalize_draft(state, ctx)
    assert entered == [True]
    assert len(state.results) == 17
    assert state.draft_execution["phase"] == "needs_attention"
