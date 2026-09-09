import json
from types import SimpleNamespace

import pytest

from ratsnestpro.repair.pipeline_adapter import _record_strong_event
from service.ahe_event import ahe_event_record


@pytest.mark.parametrize("exhaust_budget", [False, True])
def test_strong_session_reaches_execution_through_real_bridge(tmp_path, monkeypatch, exhaust_budget):
    from ratsnestpro.orchestration import pipeline as p
    from ratsnestpro.repair import pipeline_adapter as adapter
    from ratsnestpro.repair.contracts import RepairLimits
    from ratsnestpro.agents.llm import LlmBudgetExceeded

    entered = []
    records = []
    state = SimpleNamespace(revision=5, artifact=lambda _: SimpleNamespace(pcb_path=str(tmp_path / "board.kicad_pcb")))
    runtime = adapter.StrongRepairRuntime("test", "high", lambda *_: "", RepairLimits(stagnation_threshold=1), "approved")
    root = tmp_path / ".strong-repair"
    root.mkdir()
    (root / "ledger.json").write_text(json.dumps({"sessions": 2, "attempts": 4}))
    ctx = SimpleNamespace(strong_repair=runtime, on_ahe_event=lambda event: records.append(ahe_event_record(event, workflow_id="test")))
    monkeypatch.setattr(p, "kicad_cli_available", lambda: "kicad-cli")
    monkeypatch.setattr(adapter, "_BoardHost", lambda *_: object())
    def session(_host, **kwargs):
        entered.append(True)
        kwargs["record"]({"event": "strong_repair.observed", "turn": 1})
        if exhaust_budget:
            raise LlmBudgetExceeded("allowance exhausted")
        return False  # no improvement: must not invent a successful route
    monkeypatch.setattr(adapter, "run_session", session)
    assert adapter.try_strong_repair(state, ctx, p.RouteResult(method="freerouting")) is None
    assert entered == [True]
    assert [item["event"] for item in records][:2] == ["strong_repair_started", "strong_repair_observed"]
    ledger = json.loads((root / "ledger.json").read_text())
    assert ledger["sessions"] == 3
    assert ledger["allowance_start_sessions"] == 2
    assert ledger.get("infrastructure_failures", 0) == 0
    if exhaust_budget:
        assert ledger["budget_exhausted"] is True
        assert adapter.try_strong_repair(state, ctx, p.RouteResult(method="freerouting")) is None
        assert entered == [True]  # Same allowance must not invoke the model again.


def test_repair_observation_preserves_constraints_without_repeating_evidence():
    from ratsnestpro.domain.contracts import RequirementSpec
    from ratsnestpro.repair.pipeline_adapter import _repair_requirement_observation
    marker = "\n\nVALIDATED CAPABILITY PROFILE — this is a scope, evidence, budget, and acceptance boundary, not a fixed circuit answer:\n"
    raw = "Two layers; trunk >=0.40 mm; approved short fanout >=0.20 mm."
    full = raw + marker + "datasheet evidence " * 6000
    spec = RequirementSpec(raw_text=full, constraints=["Keep every pad-to-trunk path <=2 mm"])
    state = SimpleNamespace(requirement_text=full, artifact=lambda _: spec)
    view = _repair_requirement_observation(state)
    assert view["immutable_requirement"] == raw
    assert view["requirement_constraints"]["constraints"] == spec.constraints
    assert len(json.dumps(view)) < 1500
    assert spec.complete_text == full  # Full evidence remains queryable, unchanged.


@pytest.mark.parametrize("name", [
    "started", "observed", "rendered", "candidate", "committed", "stopped",
    "infrastructure_failure", "unavailable",
])
def test_all_strong_events_cross_real_bridge_without_private_data(tmp_path, name):
    records = []
    event = {"event": f"strong_repair.{name}", "turn": 1, "improved": True,
             "execution": {"stdout": "private"}, "rationale": "private"}
    def bridge(payload):
        records.append(ahe_event_record(payload, workflow_id="workflow"))
    for session in (1, 2):
        _record_strong_event(tmp_path, event, revision=5, model="test", session=session, callback=bridge)
    assert records[0]["event"] == f"strong_repair_{name}"
    assert records[0]["revision"] == 5
    assert records[0]["strong_repair"] == {"session": 1, "turn": 1, "improved": True}
    assert records[0]["record_id"] != records[1]["record_id"]
    assert "private" not in json.dumps(records)
    assert "private" in (tmp_path / "events.jsonl").read_text()


def test_rejected_progress_does_not_abort_repair(tmp_path):
    def reject(_payload):
        raise ValueError("private diagnostic")
    _record_strong_event(tmp_path, {"event": "strong_repair.started"},
                         revision=5, model="test", session=1, callback=reject)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "strong_repair.telemetry_rejected"
    assert "private diagnostic" not in json.dumps(events)


def test_checkpoint_failure_is_not_swallowed(tmp_path):
    def fail(_payload):
        raise OSError("checkpoint storage unavailable")
    with pytest.raises(OSError):
        _record_strong_event(tmp_path, {"event": "strong_repair.started"},
                             revision=5, model="test", session=1, callback=fail)


def test_routability_preflight_crosses_real_bridge():
    from ratsnestpro.repair.routability import emit_preflight

    records = []
    emit_preflight(lambda event: records.append(ahe_event_record(event, workflow_id="test")),
                   {"enclosed_pads": ["U1.1"], "joint_groups": ["U1"]}, revision=5)
    assert records[0]["event"] == "routability_preflight"
    assert records[0]["routability"] == {"enclosed_pad_count": 1, "joint_group_count": 1}
