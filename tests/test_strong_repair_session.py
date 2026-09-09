import base64
import json

import pytest

from ratsnestpro.repair.contracts import RepairLimits, SandboxFile, SandboxRequest
from ratsnestpro.repair.session import CandidateAssessment, run_session
from repair_executor.docker_runner import container_config


class Host:
    def __init__(self):
        self.current = self.best = CandidateAssessment("original", 0, 3, 0)
        self.committed = False

    def observe(self):
        return {"actual_pcb": self.current.fingerprint}

    def query(self, value):
        return {"pads": [1, 2]}

    def images(self):
        return []

    def assess(self):
        return self.current

    def checkpoint_candidate(self):
        self.best = self.current

    def rollback_candidate(self):
        self.current = self.best

    def commit(self, fingerprint):
        assert fingerprint == "original"
        self.committed = True

    def execute(self, script, timeout):
        self.current = {
            "improve": CandidateAssessment("better", 0, 1, 0),
            "regress": CandidateAssessment("worse", 1, 0, 0),
            "cheat": CandidateAssessment("fake", 0, 0, 0, ("changed pin identity",)),
            "finish": CandidateAssessment("finished", 0, 0, 0),
        }[script]
        return {"status": "completed"}


def test_failed_program_survives_observation_history_window():
    host, prompts = Host(), []
    original_execute = host.execute
    host.execute = lambda script, timeout: (
        {"status": "failed", "output": "AttributeError: missing API"}
        if script == "broken" else original_execute(script, timeout))
    replies = iter([
        {"action": "execute_python", "rationale": "attempt", "script": "broken"},
        *[{"engineering_queries": []} for _ in range(7)],
        {"action": "execute_python", "rationale": "correct API", "script": "finish"},
    ])
    def complete(_system, user, *_):
        prompts.append(json.loads(user))
        return json.dumps(next(replies))
    assert run_session(host, complete=complete, limits=RepairLimits(max_turns=9), record=lambda _: None)
    assert prompts[-1]["last_execution_failure"]["script"] == "broken"
    assert "missing API" in prompts[-1]["last_execution_failure"]["output"]
    assert prompts[-1]["remaining_turns"] == 1


def test_geometry_defect_can_be_corrected_in_next_action():
    host, calls = Host(), []
    def execute(*_):
        calls.append(host.current.fingerprint)
        host.current = (CandidateAssessment('needs-sizing', 0, 1, 0,
                        ('via mismatch',), ('via mismatch',)) if len(calls) == 1
                        else CandidateAssessment('corrected', 0, 0, 0))
        return {'status': 'completed'}
    host.execute = execute
    assert run_session(host, complete=lambda *_: json.dumps({'action': 'execute_python',
        'rationale': 'correct via', 'script': 'repair'}),
        limits=RepairLimits(max_turns=2), record=lambda _: None)
    assert calls == ['original', 'needs-sizing']
    assert host.committed and host.current.fingerprint == 'corrected'


def test_unfixed_geometry_never_commits():
    host = Host()
    def execute(*_):
        host.current = CandidateAssessment('invalid', 0, 0, 0, ('via mismatch',), ('via mismatch',))
        return {'status': 'completed'}
    host.execute = execute
    assert not run_session(host, complete=lambda *_: json.dumps({'action': 'execute_python',
        'rationale': 'attempt', 'script': 'repair'}), limits=RepairLimits(max_turns=1), record=lambda _: None)
    assert not host.committed and host.current.fingerprint == 'original'


@pytest.mark.parametrize("last", ["regress", "cheat"])
def test_best_candidate_survives_failed_joint_edits(last):
    host = Host()
    replies = iter(["improve", last])
    assert run_session(
        host,
        complete=lambda *_: json.dumps(
            {"action": "execute_python", "rationale": "measured copper", "script": next(replies)}
        ),
        limits=RepairLimits(max_turns=2),
        record=lambda _: None,
    )
    assert host.committed and host.current.fingerprint == "better"


def test_no_improvement_never_commits():
    host = Host()
    assert not run_session(
        host,
        complete=lambda *_: json.dumps(
            {"action": "execute_python", "rationale": "attempt", "script": "regress"}
        ),
        limits=RepairLimits(max_turns=1),
        record=lambda _: None,
    )
    assert not host.committed and host.current.fingerprint == "original"


def test_premature_completion_returns_findings_and_continues_repair():
    host, events, prompts = Host(), [], []
    responses = iter([
        {"action": "report_complete", "rationale": "I believe it is done"},
        {"action": "execute_python", "rationale": "fix remaining actual gaps", "script": "finish"},
    ])
    revalidated = []
    host.revalidate = lambda: (revalidated.append(True) or host.assess())
    def complete(_system, user, *_):
        prompts.append(json.loads(user))
        return json.dumps(next(responses))
    assert run_session(host, complete=complete, limits=RepairLimits(max_turns=2), record=events.append)
    assert revalidated == [True]
    assert prompts[1]['history'][-1]['candidate_checks_passed'] is False
    assert host.current.fingerprint == 'finished' and host.committed
    assert all(e.get('release_ready') is not True for e in events)


def test_completion_claim_alone_never_promotes_or_commits():
    host, events = Host(), []
    assert not run_session(host, complete=lambda *_: json.dumps({
        "action": "report_complete", "rationale": "looks good to me"}),
        limits=RepairLimits(max_turns=1), record=events.append)
    outcome = next(e for e in events if e['event'] == 'strong_repair.session_finished')
    assert outcome['agent_reported_complete'] and not outcome['candidate_checks_passed']
    assert not outcome['release_ready'] and not host.committed


def test_failed_executor_cannot_commit_even_if_it_mutated_candidate():
    host, events = Host(), []
    def fail(*_):
        host.current = CandidateAssessment('partial', 0, 0, 0)
        return {'status': 'failed', 'output': 'program exception'}
    host.execute = fail
    assert not run_session(host, complete=lambda *_: json.dumps({
        'action': 'execute_python', 'rationale': 'repair', 'script': 'fail'}),
        limits=RepairLimits(max_turns=1), record=events.append)
    assert host.current.fingerprint == 'original' and not host.committed
    assert any(e['event'] == 'strong_repair.execution_failed' for e in events)


def test_budget_stop_preserves_verified_improvement():
    from ratsnestpro.agents.llm import LlmBudgetExceeded

    host = Host()
    calls = []
    def complete(*_):
        if calls:
            raise LlmBudgetExceeded("allowance exhausted")
        calls.append(True)
        return json.dumps({"action": "execute_python", "rationale": "repair", "script": "improve"})
    assert run_session(host, complete=complete, limits=RepairLimits(max_turns=2), record=lambda _: None)
    assert host.committed and host.current.fingerprint == "better"


def test_probe_without_net_bindings_is_not_routability_success():
    from types import SimpleNamespace
    from ratsnestpro.repair.routability import probe

    board = SimpleNamespace(list_footprints=lambda: [{"reference": "U1", "at": {"x": 0, "y": 0}}],
                            footprint_pads=lambda _: [{"type": "smd", "net": None}])
    value = probe(board, clear_segment=lambda *_: True)
    assert value["status"] == "insufficient_net_geometry"
    assert value["coverage"]["smd_pads"] == 1
    assert value["connectivity_verified"] is False


def test_pcbnew_adapter_retains_detached_proxy_and_is_idempotent(monkeypatch):
    import gc
    import sys
    import weakref
    from types import SimpleNamespace
    from repair_executor.pcbnew_compat import install

    class Board:
        def Remove(self, item):
            return "removed"
    class Item:
        pass
    class Via:
        def SetWidth(self, *args):
            return args
    monkeypatch.setitem(sys.modules, "pcbnew", SimpleNamespace(BOARD=Board, PCB_VIA=Via))
    install()
    wrapped = Board.Remove
    install()
    assert Board.Remove is wrapped
    board, item = Board(), Item()
    weak = weakref.ref(item)
    assert board.Remove(item) == "removed"
    del item
    gc.collect()
    assert weak() is not None
    with pytest.raises(ValueError, match="requires"):
        Via().SetWidth(600000)
    assert Via().SetWidth(0, 600000) == (0, 600000)


def test_unchanged_images_are_not_billed_each_query_and_render_can_request_them():
    host = Host()
    host.images = lambda: ["data:image/png;base64,unchanged"]
    received = []
    responses = iter([
        {"engineering_queries": [{"tool": "pcb", "section": "pads"}]},
        {"engineering_queries": [{"tool": "render"}]},
        {"action": "stop", "rationale": "done"},
    ])
    def complete(_system, _user, images, _remaining):
        received.append(len(images))
        return json.dumps(next(responses))
    assert not run_session(host, complete=complete, limits=RepairLimits(max_turns=3), record=lambda _: None)
    assert received == [1, 0, 1]


@pytest.mark.parametrize(
    "path", ["../secret.json", "/board.kicad_pcb", "a\\b.json", ".env", "repair.py"]
)
def test_request_cannot_name_host_or_executable_files(path):
    with pytest.raises(ValueError):
        SandboxFile(path=path, data="")


def test_sandbox_configuration_has_no_secrets_mounts_or_network():
    config = container_config("approved-image@sha256:" + "a" * 64)
    host = config["HostConfig"]
    assert config["User"] == "10001:10001"
    assert config["NetworkDisabled"] and host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] and host["Binds"] == []
    assert "size=256m" in host["Tmpfs"]["/work"]
    assert host["CapDrop"] == ["ALL"]
    assert not any("KEY=" in item or "TOKEN=" in item for item in config["Env"])


def test_request_requires_the_actual_pcb():
    with pytest.raises(ValueError):
        SandboxRequest(
            files=[SandboxFile(path="other.json", data=base64.b64encode(b"{}").decode())],
            pcb_name="board.kicad_pcb",
            script="pass",
        )


def test_joint_solver_reserves_neighbor_vias_together():
    from ratsnestpro.repair.joint_escape import solve
    from ratsnestpro.orchestration.pipeline import _segment_distance

    pads = [
        {
            "ref": "U1",
            "pad": "1",
            "net": "A",
            "position": (0, 0),
            "escape_candidates": [{"end": (1, 0), "layer": "F.Cu"}],
        },
        {
            "ref": "U1",
            "pad": "2",
            "net": "B",
            "position": (0, 0.5),
            "escape_candidates": [
                {"end": (1, 0), "layer": "F.Cu"},
                {"end": (1, 1.8), "layer": "F.Cu"},
            ],
        },
    ]
    result = solve(
        pads,
        clear_segment=lambda *_: True,
        clear_via=lambda *_: True,
        segment_distance=_segment_distance,
    )
    assert result["status"] == "candidate"
    assert len({tuple(a["end"]) for a in result["actions"]}) == 2


def test_legacy_workflow_identity_does_not_change_when_escalation_disabled():
    from agents.ratsnestpro.temporal.contracts import hardware_workflow_identity

    legacy = {"run_id": "fixture", "model_name": "base"}
    assert hardware_workflow_identity(legacy) == hardware_workflow_identity(
        {**legacy, "strong_model_name": None, "strong_reasoning_effort": None}
    )
    assert hardware_workflow_identity(legacy) != hardware_workflow_identity(
        {**legacy, "strong_model_name": "gpt-5.5", "strong_reasoning_effort": "high"}
    )


def test_both_pipeline_entrypoints_accept_escalation_options():
    import inspect
    from agents.ratsnestpro.tools import ratsnest_run_pcb_pipeline, ratsnest_run_pcb_pipeline_until

    for entry in (ratsnest_run_pcb_pipeline, ratsnest_run_pcb_pipeline_until):
        assert {"strong_model_name", "strong_reasoning_effort"} <= set(
            inspect.signature(entry).parameters
        )


def test_ordinary_candidate_rollback_cannot_reset_escalation_budget():
    from pathlib import Path
    from ratsnestpro.orchestration.pipeline import _candidate_managed_file

    assert not _candidate_managed_file(Path(".strong-repair/ledger.json"))
    assert not _candidate_managed_file(Path(".strong-repair/pending-commit.json"))
    assert _candidate_managed_file(Path("board.kicad_pcb"))
