from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from ratsnestpro.agents.llm import LlmError, LlmMode
from ratsnestpro.orchestration.ahe import RecoveryAction, RecoveryDecision
from ratsnestpro.orchestration.engineering_workspace import (
    EngineeringQuery,
    EngineeringWorkspace,
    complete_with_observations,
)
from ratsnestpro.orchestration.entity_repairs import CadActionBatch
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    LayoutGeneralStep,
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    PipelineStepBase,
    _apply_placement_cad_action_batch,
    _artifact_sha256,
    _engineering_failure_score,
    _validate_netlist_patch_scope,
    propose_structured,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    NetlistPatch,
    PcbPlacement,
    PcbPlacementPlan,
)


class _Result(BaseModel):
    value: str


class _Client:
    def __init__(self, replies: list[str]):
        self.replies = iter(replies)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        return next(self.replies)


def test_model_can_inspect_real_artifact_then_finish_structured_proposal() -> None:
    workspace = EngineeringWorkspace(out_dir=None, artifacts=lambda: {
        "selection": {"parts": [{"ref": "C5", "role": "sensor_decoupling"}]},
    })
    client = _Client([
        '{"engineering_queries":[{"tool":"artifact","step":"selection","pointer":"/parts"}]}',
        '{"value":"capacitor, not sensor"}',
    ])
    calls: list[int] = []
    result, used = propose_structured(
        PipelineContext(mode=LlmMode.REQUIRED, client=client, engineering_workspace=workspace),
        model=_Result, system="Return _Result", user="identify C5",
        fallback=lambda: _Result(value="fallback"), before_attempt=lambda: calls.append(1),
    )
    assert used and result.value == "capacitor, not sensor"
    assert len(calls) == 2  # inspection round is charged to the real invocation budget
    assert '"role": "sensor_decoupling"' in client.prompts[1]
    assert "UNTRUSTED ENGINEERING TOOL OBSERVATIONS" in client.prompts[1]


def test_workspace_rejects_parent_escape_and_private_files(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (tmp_path / "outside.json").write_text('{"secret":1}', encoding="utf-8")
    (run / "credentials.json").write_text('{"secret":2}', encoding="utf-8")
    workspace = EngineeringWorkspace(out_dir=str(run), artifacts=dict)
    for path in ("../outside.json", str(tmp_path / "outside.json"), "credentials.json"):
        receipt = workspace.observe(EngineeringQuery(tool="read_file", path=path))
        assert not receipt["ok"]
        assert '"secret"' not in json.dumps(receipt)


def test_artifact_pagination_reaches_entities_outside_old_snapshot_prefix() -> None:
    workspace = EngineeringWorkspace(out_dir=None, artifacts=lambda: {
        "selection": {"parts": [{"ref": f"R{i}"} for i in range(180)]},
    })
    receipt = workspace.observe(EngineeringQuery(
        tool="artifact", step="selection", pointer="/parts", offset=150, limit=2,
    ))
    assert receipt["result"]["data"] == [{"ref": "R150"}, {"ref": "R151"}]
    assert receipt["result"]["next_offset"] == 152


def test_repeated_inspection_is_bounded() -> None:
    query = '{"engineering_queries":[{"tool":"artifact"}]}'
    client = _Client([query] * 3)
    with pytest.raises(LlmError, match="inspection budget exhausted"):
        complete_with_observations(
            client, "system", "task", workspace=EngineeringWorkspace(out_dir=None, artifacts=dict),
            extract_json=lambda value: value, max_queries=2,
        )
    assert len(client.prompts) == 3
    assert "duplicate query" in client.prompts[-1]


def _placement() -> PcbPlacementPlan:
    return PcbPlacementPlan(board_width=40, board_height=30, placements=[
        PcbPlacement(ref="U1", x=10, y=10), PcbPlacement(ref="C5", x=5, y=5),
    ])


def _batch(plan: PcbPlacementPlan) -> CadActionBatch:
    return CadActionBatch.model_validate({
        "owner_step": "layout_general", "base_artifact_fingerprint": _artifact_sha256(plan),
        "actions": [{"operation": "move_footprint", "target": {
            "kind": "footprint", "reference": "C5"},
            "position": {"x_mm": 12, "y_mm": 10},
            "preconditions": {"expected_position": {"x_mm": 5, "y_mm": 5}}}],
        "success_checks": ["placement_constraints_satisfied"],
    })


def test_pre_pcb_placement_batch_modifies_only_the_candidate() -> None:
    plan = _placement()
    candidate, observation = _apply_placement_cad_action_batch(plan, _batch(plan))
    assert observation.status == "applied"
    assert candidate.by_ref()["C5"].x == 12
    assert plan.by_ref()["C5"].x == 5
    assert candidate.board_width == plan.board_width


def test_placement_batch_rolls_back_all_actions_when_one_precondition_fails() -> None:
    plan = _placement()
    batch = _batch(plan)
    second = batch.actions[0].model_copy(deep=True)
    second.action_id += "-second"
    second.target.reference = "U1"  # wrong expected position after the first edit
    batch.actions.append(second)
    candidate, observation = _apply_placement_cad_action_batch(plan, batch)
    assert observation.status == "rejected"
    assert candidate == plan
    assert candidate.by_ref()["C5"].x == 5


def test_layout_replan_consumes_model_delta_instead_of_repeating_packer() -> None:
    plan = _placement()
    client = _Client(['{"placements":[{"ref":"C5","x":12,"y":10}]}'])
    candidate, used = LayoutGeneralStep().replan(
        PipelineState(requirement_text="two-layer 40x30 board"),
        PipelineContext(mode=LlmMode.REQUIRED, client=client, agentic_recovery_enabled=True),
        "", plan, "move the capacitor next to U1 without changing the board",
    )
    assert used and candidate.by_ref()["C5"].x == 12
    assert candidate.by_ref()["U1"] == plan.by_ref()["U1"]
    assert candidate.board_width == 40


def test_score_measures_findings_not_diagnostic_wording() -> None:
    first = CheckResult(name="erc", ok=False, message="x", evidence={"error_count": 6})
    second = first.model_copy(update={"message": "longer, more useful evidence" * 80,
                                      "evidence": {"error_count": 2}})
    assert _engineering_failure_score([second]) < _engineering_failure_score([first])
    assert _engineering_failure_score([first]) == _engineering_failure_score([
        first.model_copy(update={"message": "completely different wording"}),
    ])


def test_out_of_scope_netlist_edit_is_rejected_not_silently_applied() -> None:
    plan = NetlistIntent(nets=[NetIntent(name="OTHER", pins=[LogicalPin(ref="U1", pin="1")])])
    patch = NetlistPatch(upsert_nets=[NetIntent(
        name="OTHER", pins=[LogicalPin(ref="U2", pin="2")],
    )])
    with pytest.raises(ValueError, match="nothing was applied"):
        _validate_netlist_patch_scope(patch, plan, {"U2"}, {"LOCAL"})


def test_pcb_query_returns_real_absolute_pad_coordinates(tmp_path: Path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text('''(kicad_pcb (version 20241229) (generator test)
      (net 0 "") (net 1 "VCC")
      (footprint "Test:C" (layer "F.Cu") (at 10 20 0)
        (property "Reference" "C5")
        (pad "1" smd rect (at 1 2) (size 1 1) (layers "F.Cu") (net 1 "VCC"))))''',
                   encoding="utf-8")
    workspace = EngineeringWorkspace(out_dir=str(tmp_path), artifacts=lambda: {
        "layout_write": {"pcb_path": str(pcb)},
    })
    receipt = workspace.observe(EngineeringQuery(tool="pcb", section="pads", reference="C5"))
    assert receipt["ok"]
    pad = receipt["result"]["observation"]["data"][0]
    assert (pad["x"], pad["y"], pad["net"]) == (11, 22, "VCC")
    assert len(receipt["result"]["pcb_sha256"]) == 64


def test_upstream_candidate_can_repair_an_intermediate_failure_before_commit() -> None:
    class Requirements(PipelineStepBase):
        step = PipelineStep.REQUIREMENTS

        def propose(self, state, ctx, knowledge):
            return _Result(value="frozen"), False

        def check(self, state, artifact):
            return [CheckResult(name="requirements", ok=True)]

    class Topology(Requirements):
        step = PipelineStep.TOPOLOGY

        def replan(self, state, ctx, knowledge, artifact, feedback):
            return _Result(value="revised"), True

    class Selection(Requirements):
        step = PipelineStep.SELECTION

        def propose(self, state, ctx, knowledge):
            return _Result(value="needs-local-fix"), False

        def check(self, state, artifact):
            return [CheckResult(name="selection", ok=artifact.value == "good",
                                message="candidate still needs a local adjustment")]

        def rollback_target(self, state, artifact, checks):
            return PipelineStep.TOPOLOGY

        def replan(self, state, ctx, knowledge, artifact, feedback):
            assert state.artifact(PipelineStep.TOPOLOGY).value == "revised"
            return _Result(value="good"), True

    client = _Client([
        RecoveryDecision(action=RecoveryAction.REPLAN_UPSTREAM, target_step="topology",
                         strategy="update topology", hypothesis="source topology needs adjustment",
                         expected_observation="selection passes").model_dump_json(),
        RecoveryDecision(action=RecoveryAction.LOCAL_REPAIR, target_step="selection",
                         strategy="finish the candidate locally", hypothesis="one local issue remains",
                         tool_args={"repair_instructions": "retain revised topology and fix selection"},
                         expected_observation="selection passes").model_dump_json(),
    ])
    state = PipelineState(requirement_text="frozen user requirement")
    Pipeline([Requirements(), Topology(), Selection()]).run(
        state, PipelineContext(mode=LlmMode.REQUIRED, client=client, agentic_recovery_enabled=True),
    )
    assert not state.blocked
    assert state.artifact(PipelineStep.TOPOLOGY).value == "revised"
    assert state.artifact(PipelineStep.SELECTION).value == "good"
    assert state.replan_history[0].status == "recovered"
    assert state.replan_history[0].intermediate_repair_attempts == 1
