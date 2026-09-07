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
