from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.ratsnestpro import ratsnestpro_agent, tools
from agents.ratsnestpro.ehe_memory import EheMemory
from agents.ratsnestpro.temporal.client import _forward_ahe_events
from ratsnestpro.orchestration.ahe import (
    FailureAction,
    FailureOrigin,
    Recoverability,
    attribute_failure,
    make_capability_gap,
    make_failure,
)
from ratsnestpro.orchestration.pipeline import (
    CheckResult,
    PipelineContext,
    PipelineState,
    PipelineStep,
    PipelineStepBase,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectionPlan
from service.ahe_event import (
    ahe_event_record,
    append_ahe_event,
    sanitize_ahe_event,
)
from service.governance_scope import TrustedGovernanceScope

_SECRET = "RAW PROMPT: proprietary-board-secret"


def _event(*, event_name: str = "harness_defect_observed") -> dict[str, object]:
    return {
        "kind": "ahe_event",
        "event": event_name,
        "step": "schematic_connections",
        "revision": 3,
        "requirement": _SECRET,
        "prompt": _SECRET,
        "failure": {
            "failure_id": "failure-1",
            "signature": "0123456789abcdef0123",
            "step": "schematic_connections",
            "check_name": "step_execution_failed",
            "category": "unknown",
            "recoverability": "harness_observation",
            "origin": "harness",
            "reason_code": "verified_pin_alias_resolution_lost",
            "required_capability": "unclassified_hardware_repair",
            "message": _SECRET,
            "evidence": {"raw_prompt": _SECRET},
            "affected_refs": ["U1"],
        },
        "attribution": {
            "action": "observe_harness",
            "reason_code": "harness_defect_not_yet_cross_run_reproducible",
            "origin": "harness",
            "independent_run_count": 1,
            "independent_project_count": 1,
        },
        "repair": {"detail": _SECRET},
    }


def test_ahe_record_is_stable_and_drops_prompt_bearing_fields() -> None:
    first = ahe_event_record(_event(), workflow_id="workflow-1")
    second = ahe_event_record(_event(), workflow_id="workflow-1")
    serialized = json.dumps(first, ensure_ascii=False)

    assert first["record_id"] == second["record_id"]
    assert _SECRET not in serialized
    assert "requirement" not in first
    assert "prompt" not in first
    assert "message" not in first["failure"]
    assert "evidence" not in first["failure"]


def test_jsonl_bridge_replays_once_by_record_id(tmp_path: Path) -> None:
    path = tmp_path / "ahe-events.jsonl"
    record = ahe_event_record(_event(), workflow_id="workflow-1")
    append_ahe_event(path, record)
    append_ahe_event(path, record)
    forwarded: list[dict[str, object]] = []
    seen: set[str] = set()

    cursor = _forward_ahe_events(path, 0, forwarded.append, seen)
    cursor = _forward_ahe_events(path, cursor, forwarded.append, seen)

    assert cursor == path.stat().st_size
    assert len(forwarded) == 1
    assert forwarded[0]["kind"] == "ahe_event"
    assert forwarded[0]["record_id"] == record["record_id"]


def test_temporal_progress_forwards_ahe_as_custom_runtime_event(monkeypatch) -> None:
    forwarded: list[dict[str, object]] = []
    monkeypatch.setattr(
        ratsnestpro_agent,
        "get_stream_writer",
        lambda: forwarded.append,
    )
    record = ahe_event_record(_event(), workflow_id="workflow-1")

    ratsnestpro_agent._temporal_progress(record)

    assert forwarded == [record]


def test_unscoped_governed_event_stays_local_and_never_reaches_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        tools,
        "publish_ahe_event_best_effort",
        lambda _config, **kwargs: published.append(kwargs["record"]),
    )
    audit = tmp_path / "run" / "ahe-events.jsonl"

    tools._record_ahe_event(
        EheMemory(tmp_path / "ehe"),
        _event(),
        run_name="run-one",
        project_name="project-one",
        requirement=_SECRET,
        workflow_id="workflow-one",
        audit_path=audit,
    )

    assert not audit.exists()
    assert published == []
    local_events = list((tmp_path / "ehe" / "u" / "e").glob("*.json"))
    assert len(local_events) == 1
    assert _SECRET not in local_events[0].read_text(encoding="utf-8")


def test_scoped_harness_observation_bridges_when_ehe_storage_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope = TrustedGovernanceScope(
        tenant_scope="1" * 16,
        project_scope="2" * 16,
        run_scope="3" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="4" * 64,
    )
    memory = EheMemory(tmp_path / "ehe", governance_scope=scope)
    audit = tmp_path / "run" / "ahe-events.jsonl"
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        memory,
        "record",
        lambda _event: (_ for _ in ()).throw(OSError("memory unavailable")),
    )
    monkeypatch.setattr(
        tools,
        "publish_ahe_event_best_effort",
        lambda _config, **kwargs: published.append(kwargs["record"]),
    )

    tools._record_ahe_event(
        memory,
        _event(),
        run_name="display-only-run",
        project_name="display-only-project",
        requirement=_SECRET,
        workflow_id="workflow-storage-failure",
        audit_path=audit,
    )

    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == ["harness_defect_observed"]
    assert published == records


def test_only_cross_project_harness_recurrence_emits_capability_gap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tools,
        "publish_ahe_event_best_effort",
        lambda _config, **_kwargs: None,
    )
    first_scope = TrustedGovernanceScope(
        tenant_scope="1" * 16,
        project_scope="2" * 16,
        run_scope="3" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="4" * 64,
    )
    second_scope = TrustedGovernanceScope(
        tenant_scope=first_scope.tenant_scope,
        project_scope="5" * 16,
        run_scope="6" * 64,
        harness_version_id=first_scope.harness_version_id,
        harness_manifest_digest=first_scope.harness_manifest_digest,
    )
    first_memory = EheMemory(tmp_path / "ehe", governance_scope=first_scope)
    second_memory = EheMemory(tmp_path / "ehe", governance_scope=second_scope)
    first_audit = tmp_path / "first.jsonl"
    second_audit = tmp_path / "second.jsonl"
    pipeline_state = PipelineState(
        requirement_text=_SECRET,
        project_name="board",
    )
    pipeline_state_path = tmp_path / "pipeline-state.json"

    tools._record_ahe_event(
        first_memory,
        _event(),
        run_name="run-one",
        project_name="project-one",
        requirement=_SECRET,
        workflow_id="workflow-one",
        audit_path=first_audit,
    )
    tools._record_ahe_event(
        second_memory,
        _event(),
        run_name="run-two",
        project_name="project-two",
        requirement=_SECRET,
        workflow_id="workflow-two",
        audit_path=second_audit,
        state=pipeline_state,
        state_path=pipeline_state_path,
    )

    first_events = [json.loads(line) for line in first_audit.read_text().splitlines()]
    second_events = [json.loads(line) for line in second_audit.read_text().splitlines()]
    assert [event["event"] for event in first_events] == ["harness_defect_observed"]
    assert [event["event"] for event in second_events] == [
        "harness_defect_observed",
        "capability_gap",
    ]
    assert second_events[-1]["attribution"]["independent_project_count"] == 2
    assert len(pipeline_state.capability_gaps) == 1
    persisted_state = json.loads(pipeline_state_path.read_text(encoding="utf-8"))
    assert persisted_state["capability_gaps"][0]["status"] == "promoted"
    assert len(first_memory.active_gaps()) == 1
    assert len(second_memory.active_gaps()) == 1
    later_first_scope = TrustedGovernanceScope(
        tenant_scope=first_scope.tenant_scope,
        project_scope=first_scope.project_scope,
        run_scope="7" * 64,
        harness_version_id=first_scope.harness_version_id,
        harness_manifest_digest=first_scope.harness_manifest_digest,
    )
    later_first_memory = EheMemory(
        tmp_path / "ehe",
        governance_scope=later_first_scope,
    )
    gap = later_first_memory.active_gaps()[0]
    resolved_audit = tmp_path / "resolved.jsonl"
    tools._record_ahe_event(
        later_first_memory,
        {
            "kind": "ahe_event",
            "event": "capability_gap_resolved",
            "step": gap.step,
            "revision": 4,
            "gap": gap.model_dump(mode="json"),
            "failure": {
                "failure_id": f"resolved:{gap.signature}",
                "signature": gap.signature,
                "step": gap.step,
                "check_name": gap.check_name,
                "category": gap.category,
                "recoverability": "capability_gap",
                "origin": "harness",
                "reason_code": "verified_harness_capability_gap_resolved",
                "required_capability": gap.required_capability,
                "affected_refs": gap.affected_refs,
            },
        },
        run_name="a-name-that-is-never-counted",
        project_name="also-never-counted",
        requirement=_SECRET,
        workflow_id="workflow-three",
        audit_path=resolved_audit,
    )
    resolved = json.loads(resolved_audit.read_text(encoding="utf-8"))
    assert resolved["attribution"] == {
        "action": "resolve_capability_gap",
        "reason_code": "verified_harness_capability_gap_resolved",
        "origin": "harness",
        "independent_run_count": 1,
        "independent_project_count": 1,
    }
    assert later_first_memory.active_gaps() == []
    assert len(second_memory.active_gaps()) == 1


def test_open_gap_unions_project_subjects_and_requires_all_to_pass(
    tmp_path: Path,
) -> None:
    scope = TrustedGovernanceScope(
        tenant_scope="1" * 16,
        project_scope="2" * 16,
        run_scope="3" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="4" * 64,
    )
    memory = EheMemory(tmp_path / "ehe", governance_scope=scope)
    observed = make_failure(
        step="selection",
        check_name="harness_consistency:generic_capability_closure",
        message="generic closure contradicted its verified inputs",
        repair_available=True,
        origin=FailureOrigin.HARNESS,
        reason_code="generic_capability_closure_contradiction",
        affected_refs=["D3"],
    ).model_copy(update={"recoverability": Recoverability.CAPABILITY_GAP})
    first_gap = make_capability_gap(observed)
    second_gap = first_gap.model_copy(update={"affected_refs": ["D4"]})

    memory.open_gap(first_gap)
    memory.open_gap(second_gap)

    active = memory.active_gaps()
    assert len(active) == 1
    assert active[0].affected_refs == ["D3", "D4"]
    assert not memory.close_gap(
        first_gap.signature,
        affected_refs=["D3"],
    )
    assert len(memory.active_gaps()) == 1
    assert memory.close_gap(
        first_gap.signature,
        affected_refs=["D3", "D4"],
    )
    assert memory.active_gaps() == []


def test_deterministic_failure_attribution_is_fail_closed() -> None:
    design = make_failure(
        step="schematic_connections",
        check_name="mcu_reset_boot_support",
        message="BOOT control is connected to the wrong pin",
        repair_available=False,
    )
    transient = make_failure(
        step="route_signals",
        check_name="router_process",
        message="connection reset while invoking router",
        repair_available=False,
    )
    harness = make_failure(
        step="selection",
        check_name="step_execution_failed",
        message="deterministic validator raised unexpectedly",
        repair_available=False,
        origin=FailureOrigin.HARNESS,
    )

    assert attribute_failure(design).action == FailureAction.REVISION
    assert design.recoverability == Recoverability.REVISION_REQUIRED
    assert attribute_failure(transient).action == FailureAction.RETRY
    assert attribute_failure(harness).action == FailureAction.OBSERVE_HARNESS
    repeated = attribute_failure(
        harness,
        independent_run_count=2,
        independent_project_count=2,
    )
    assert repeated.action == FailureAction.CAPABILITY_GAP
    with pytest.raises(ValueError, match="cross-run attributed"):
        make_capability_gap(design)


def test_unknown_harness_reason_cannot_pollute_cross_project_recurrence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        tools,
        "publish_ahe_event_best_effort",
        lambda _config, **_kwargs: None,
    )
    first_scope = TrustedGovernanceScope(
        tenant_scope="a" * 16,
        project_scope="b" * 16,
        run_scope="c" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="d" * 64,
    )
    second_scope = TrustedGovernanceScope(
        tenant_scope=first_scope.tenant_scope,
        project_scope="e" * 16,
        run_scope="f" * 64,
        harness_version_id=first_scope.harness_version_id,
        harness_manifest_digest=first_scope.harness_manifest_digest,
    )
    invalid = _event()
    invalid["failure"]["reason_code"] = "model_reported_harness_bug"
    tools._record_ahe_event(
        EheMemory(tmp_path / "ehe", governance_scope=first_scope),
        invalid,
        run_name="spoofed-run",
        project_name="spoofed-project",
        requirement=_SECRET,
        workflow_id="workflow-invalid",
        audit_path=tmp_path / "invalid.jsonl",
    )
    valid_memory = EheMemory(tmp_path / "ehe", governance_scope=second_scope)
    valid_audit = tmp_path / "valid.jsonl"
    tools._record_ahe_event(
        valid_memory,
        _event(),
        run_name="same-visible-name",
        project_name="same-visible-project",
        requirement=_SECRET,
        workflow_id="workflow-valid",
        audit_path=valid_audit,
    )

    assert not (tmp_path / "invalid.jsonl").exists()
    assert [
        json.loads(line)["event"]
        for line in valid_audit.read_text(encoding="utf-8").splitlines()
    ] == ["harness_defect_observed"]
    assert valid_memory.harness_recurrence("0123456789abcdef0123") == (1, 1)
    isolated_scope = TrustedGovernanceScope(
        tenant_scope="9" * 16,
        project_scope=second_scope.project_scope,
        run_scope="8" * 64,
        harness_version_id=second_scope.harness_version_id,
        harness_manifest_digest=second_scope.harness_manifest_digest,
    )
    assert EheMemory(
        tmp_path / "ehe",
        governance_scope=isolated_scope,
    ).harness_recurrence("0123456789abcdef0123") == (0, 0)


def test_pipeline_emits_harness_observation_from_explicit_check_provenance() -> None:
    class HarnessContradictionStep(PipelineStepBase):
        step = PipelineStep.SELECTION

        def propose(self, _state, _ctx, _knowledge):
            return SelectionPlan(parts=[]), False

        def check(self, _state, _artifact):
            return [CheckResult(
                name="harness_consistency:verified_pin_alias_resolution:boot",
                ok=False,
                message="verified alias closure contradicted its unique physical net",
                blocks_execution=True,
                origin=FailureOrigin.HARNESS,
                reason_code="verified_pin_alias_resolution_lost",
                affected_refs=["U1"],
            )]

    events: list[dict[str, object]] = []
    result = HarnessContradictionStep().run(
        PipelineState(requirement_text="bounded test", project_name="board-a"),
        PipelineContext(kb=object(), on_ahe_event=events.append),  # type: ignore[arg-type]
    )

    assert result.failures[0].origin == FailureOrigin.HARNESS
    observation = next(
        event for event in events if event["event"] == "harness_defect_observed"
    )
    assert observation["failure"]["origin"] == FailureOrigin.HARNESS
    assert observation["attribution"]["action"] == FailureAction.OBSERVE_HARNESS


def test_sanitizer_rejects_foreign_or_unbounded_event_types() -> None:
    with pytest.raises(ValueError, match="only ahe_event"):
        sanitize_ahe_event({"kind": "llm_output", "event": "failure", "step": "x"})
    with pytest.raises(ValueError, match="bounded identifiers"):
        sanitize_ahe_event({"kind": "ahe_event", "event": "BAD EVENT", "step": "x"})
