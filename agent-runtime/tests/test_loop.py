"""End-to-end loop test: defective board -> converged, trajectory captured."""

import json
import shutil

import pytest

from ratsnest.config import REPO_ROOT, Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_edit import Patcher
from ratsnest.orchestrator import RunLoop, RunStore
from ratsnest.schemas import PatchPlan, RepairOp, RepairOpType, RunConfig


@pytest.fixture()
def defective_project(tmp_path):
    golden = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"
    dst = tmp_path / "defective"
    shutil.copytree(golden, dst)
    (dst / "analysis.json").unlink(missing_ok=True)
    ops = [RepairOp(op=RepairOpType.set_value, ref="R1", params={"value": "1.5k"}, finding_id="s"),
           RepairOp(op=RepairOpType.set_value, ref="R3", params={"value": "10"}, finding_id="s")]
    ops += [RepairOp(op=RepairOpType.set_property, ref=r,
                     params={"name": "MPN", "value": ""}, finding_id="s")
            for r in ("U1", "R1", "R2", "R3", "D1")]
    assert Patcher().apply(PatchPlan(run_id="t", ops=ops), dst).applied
    return dst


def _loop(tmp_path):
    config = Config.load()
    config.runs_dir = tmp_path / "runs"
    return RunLoop(config), config


def test_loop_converges_and_captures_trajectory(defective_project, tmp_path):
    loop, config = _loop(tmp_path)
    record = loop.execute(RunConfig(project_dir=str(defective_project),
                                    max_iterations=3, run_erc=False))
    assert record.status == "converged"
    assert record.strategy_version_id.startswith("strat_")
    # score strictly improved and ended clean
    assert record.iterations[0].score_delta > 0
    assert record.iterations[-1].scorecard.score == 100.0
    assert record.iterations[-1].scorecard.severity_counts.get("error", 0) == 0

    run_dir = config.runs_dir / record.run_id
    assert (run_dir / "run.json").exists()
    events = [json.loads(l) for l in
              (run_dir / "trajectory.jsonl").read_text().splitlines()]
    nodes = [e["node"] for e in events]
    for expected in ("evaluate", "plan_repairs", "apply_patches", "verify", "finish"):
        assert expected in nodes
    finish = [e for e in events if e["node"] == "finish"][0]
    assert finish["reward"] > 0  # net score gain recorded as ATDP reward
    assert all(e["metadata"]["strategy_version_id"] == record.strategy_version_id
               for e in events)


def test_suggest_only_leaves_files_untouched(defective_project, tmp_path):
    sch = defective_project / "demo_board.kicad_sch"
    before = sch.read_text(encoding="utf-8")
    loop, _ = _loop(tmp_path)
    record = loop.execute(RunConfig(project_dir=str(defective_project),
                                    max_iterations=2, fix_policy="suggest_only",
                                    run_erc=False))
    assert record.status == "suggested"
    assert record.iterations[0].patch_plan is not None
    assert len(record.iterations[0].patch_plan.ops) > 0
    assert sch.read_text(encoding="utf-8") == before


def test_clean_board_converges_immediately(tmp_path):
    golden = REPO_ROOT / "benchmarks" / "corpus" / "demo_board"
    dst = tmp_path / "clean"
    shutil.copytree(golden, dst)
    (dst / "analysis.json").unlink(missing_ok=True)
    loop, _ = _loop(tmp_path)
    record = loop.execute(RunConfig(project_dir=str(dst), max_iterations=2,
                                    run_erc=False))
    assert record.status == "converged"
    assert record.iterations[0].scorecard.score == 100.0
    assert not record.iterations[0].patch_plan.ops


def test_generation_and_repair_share_one_trajectory(defective_project, tmp_path):
    loop, config = _loop(tmp_path)
    run_id = "run_shared_trajectory"
    recorder = Recorder(config.runs_dir / run_id, run_id)
    recorder.emit("requirement_agent", outcome={"ok": True})

    record = loop.execute(
        RunConfig(project_dir=str(defective_project), max_iterations=3,
                  run_erc=False),
        recorder=recorder, run_id=run_id)

    events = [json.loads(line) for line in
              (config.runs_dir / run_id / "trajectory.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    assert record.run_id == run_id
    assert [event["step"] for event in events] == list(
        range(1, len(events) + 1))
    assert events[0]["node"] == "requirement_agent"
    assert events[-1]["node"] == "finish"
    assert all(event["run_id"] == run_id for event in events)
