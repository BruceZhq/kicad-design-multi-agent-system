"""Pre-execution approval and recovery invariants."""

from pathlib import Path

import pytest

from ratsnest.cli import main
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.contracts import ToolCall
from ratsnest.design_workflow import (
    PlanIntegrityError,
    parse_approved_plan,
    plan_sha256,
)


def test_design_cli_plans_without_creating_kicad_project(
        tmp_path, monkeypatch, capsys):
    project_dir = tmp_path / "must-not-exist-before-approval"
    monkeypatch.setenv("RATSNEST_LLM", "off")
    monkeypatch.setenv("RATSNEST_RUNS_DIR", str(tmp_path / "runs"))

    result = main([
        "design", "12V to 5V board with red LED",
        "--backend", "crew", "--out", str(project_dir),
        "--run-id", "run_plan_gate", "--json",
    ])

    payload = capsys.readouterr().out
    assert result == 0
    assert not project_dir.exists()
    approved = parse_approved_plan(payload, plan_sha256(payload))
    assert approved.plan.run_id == "run_plan_gate"
    assert approved.plan.board_plan.components


def test_approved_plan_rejects_any_byte_change(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RATSNEST_LLM", "off")
    monkeypatch.setenv("RATSNEST_RUNS_DIR", str(tmp_path / "runs"))
    assert main([
        "design-plan", "12V to 5V", "--backend", "crew",
        "--run-id", "run_integrity", "--json",
    ]) == 0
    payload = capsys.readouterr().out
    digest = plan_sha256(payload)

    with pytest.raises(PlanIntegrityError, match="hash mismatch"):
        parse_approved_plan(payload + " ", digest)


def test_blackboard_checkpoint_restores_typed_tool_history(tmp_path):
    checkpoint = tmp_path / "design_state.json"
    blackboard = DesignBlackboard(
        str(tmp_path), checkpoint_path=checkpoint)
    call = ToolCall(
        tool="save_project", reason="checkpoint test",
        expected_result="saved")

    blackboard.record_tool("schematic_designer", call, True, {"ok": True})
    resumed = DesignBlackboard.resume(
        str(tmp_path), checkpoint_path=checkpoint)

    assert checkpoint.is_file()
    assert len(resumed.state.tool_history) == 1
    assert resumed.state.tool_history[0].call.call_id == call.call_id
