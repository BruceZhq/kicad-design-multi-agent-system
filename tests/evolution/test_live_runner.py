import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from evolution.live_runner import (
    LiveCase,
    LivePlan,
    _artifact_facts,
    _capture_stream,
    _grade,
    _load_blind_reviews,
    _native_path,
    _report,
    _usage_tokens,
    _validate_frozen_execution,
)


def _case(**overrides: object) -> LiveCase:
    values: dict[str, object] = {
        "caseId": "live.intent-research",
        "category": "intent_routing",
        "prompt": "workflow_mode: research; compare two MCUs",
        "expectedIntents": ["research"],
        "requiredPhases": ["intent-router", "architect"],
        "forbiddenPhases": ["hardware-engineer"],
        "requiredTools": ["ratsnest_search_internal_knowledge"],
        "forbiddenTools": ["artifact.publish"],
    }
    values.update(overrides)
    return LiveCase.model_validate(values)


def test_live_plan_rejects_duplicate_case_ids() -> None:
    case = _case()
    with pytest.raises(ValidationError, match="case IDs must be unique"):
        LivePlan(planId="duplicate", cases=[case, case])


def test_paired_plan_requires_one_frozen_case_per_arm() -> None:
    digest = "a" * 64
    multi = _case(caseId="pair.multi", arm="multi_agent", pairId="pair.control")
    single = _case(caseId="pair.single", arm="single_agent", pairId="pair.control")
    plan = LivePlan.model_validate(
        {
            "planId": "paired",
            "frozenExecution": {
                "model": "model-a",
                "provider": "provider-a",
                "environmentDigest": digest,
                "configDigest": digest,
            },
            "cases": [multi, single],
        }
    )

    assert {case.arm for case in plan.cases} == {"multi_agent", "single_agent"}
    assert (
        _validate_frozen_execution(
            plan,
            model="model-a",
            provider="provider-a",
            environment_digest=digest,
            config_digest=digest,
        )
        == "model-a"
    )
    with pytest.raises(ValueError, match="environmentDigest"):
        _validate_frozen_execution(
            plan,
            model="model-a",
            provider="provider-a",
            environment_digest="b" * 64,
            config_digest=digest,
        )


def test_paired_plan_rejects_missing_control_arm() -> None:
    with pytest.raises(ValidationError, match="exactly one case per arm"):
        LivePlan.model_validate(
            {
                "planId": "unpaired",
                "frozenExecution": {
                    "model": "model-a",
                    "provider": "provider-a",
                    "environmentDigest": "a" * 64,
                    "configDigest": "b" * 64,
                },
                "cases": [
                    _case(caseId="pair.multi", arm="multi_agent", pairId="pair.control")
                ],
            }
        )


def test_live_grade_uses_only_structured_facts() -> None:
    case = _case()
    observed = {
        "httpStatus": 200,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "research",
        "phases": ["intent-router", "architect", "supervisor"],
        "tools": ["ratsnest_search_internal_knowledge"],
        "deliveryStatus": None,
        "artifacts": [],
        "artifactsValid": True,
    }
    assert all(value is not False for value in _grade(case, observed, None).values())


def test_release_gate_can_be_observed_without_preassigned_outcome() -> None:
    case = _case(
        caseId="eda.observe-release",
        category="eda_pipeline",
        expectedIntents=["build"],
        expectReleaseReady=None,
    )
    observed = {
        "httpStatus": 200,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "build",
        "phases": [],
        "tools": [],
        "deliveryStatus": "execution_blocked",
        "artifacts": [],
        "artifactsValid": True,
    }

    assert _grade(case, observed, None)["releaseGate"] is True
    assert _grade(case, observed, None)["edaPipeline"] is False


def test_eda_pipeline_requires_full_steps_and_core_artifacts() -> None:
    case = _case(
        caseId="eda.full-pipeline",
        category="eda_pipeline",
        expectedIntents=["build"],
        expectReleaseReady=None,
    )
    observed = {
        "httpStatus": 200,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "build",
        "phases": [],
        "tools": [],
        "completedSteps": 17,
        "deliveryStatus": "completed_with_issues",
        "artifacts": [
            {"name": "pipeline_result.json"},
            {"name": "board.kicad_sch"},
            {"name": "board.kicad_pcb"},
        ],
        "artifactsValid": True,
    }

    assert _grade(case, observed, None)["edaPipeline"] is True


def test_explicit_tool_evidence_fails_closed_when_postcondition_is_missing() -> None:
    case = _case(expectedToolCalls=["ratsnest_search_internal_knowledge"])
    observed = {
        "httpStatus": 200,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "research",
        "phases": ["intent-router", "architect"],
        "tools": ["ratsnest_search_internal_knowledge"],
        "toolCalls": [
            {
                "tool": "ratsnest_search_internal_knowledge",
                "argumentsSchemaValid": True,
                "resultStatus": "ok",
                "postconditionSatisfied": None,
            }
        ],
        "deliveryStatus": None,
        "artifacts": [],
        "artifactsValid": True,
    }

    assert _grade(case, observed, None)["toolEvidence"] is False
    observed["toolCalls"][0]["postconditionSatisfied"] = True
    assert _grade(case, observed, None)["toolEvidence"] is True


def test_blind_review_manifest_is_external_and_plan_bound(tmp_path: Path) -> None:
    path = tmp_path / "blind-review.json"
    path.write_text(
        """{
  "schemaVersion": "1.0",
  "planDigest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "labels": [{
    "caseId": "live.intent-research",
    "accepted": true,
    "rubricVersion": "pcb-rubric-v1",
    "reviewerIdHash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }]
}""",
        encoding="utf-8",
    )

    labels = _load_blind_reviews(path, plan_digest="a" * 64)
    assert labels["live.intent-research"].accepted is True
    with pytest.raises(ValueError, match="not bound"):
        _load_blind_reviews(path, plan_digest="c" * 64)


def test_report_keeps_missing_human_and_handoff_evidence_as_na() -> None:
    plan = LivePlan(planId="unpaired", cases=[_case()])
    observed = {
        "httpStatus": 200,
        "durationSeconds": 1.0,
        "done": True,
        "humanInput": False,
        "errors": [],
        "intent": "research",
        "phases": ["intent-router", "architect"],
        "tools": ["ratsnest_search_internal_knowledge"],
        "toolCalls": [],
        "handoffs": None,
        "handoffErrorCount": None,
        "hitl": {"requestCount": 0, "responseCount": None},
        "completedSteps": 0,
        "llmTokens": 0,
        "deliveryStatus": None,
        "artifacts": [],
        "artifactsValid": True,
    }
    checks = _grade(plan.cases[0], observed, None)
    report = _report(
        plan,
        b"frozen-plan",
        "a" * 40,
        False,
        [
            {
                "caseId": plan.cases[0].case_id,
                "category": plan.cases[0].category,
                "arm": None,
                "pairId": None,
                "observed": observed,
                "checks": checks,
                "humanAcceptance": None,
                "passed": True,
            }
        ],
    )

    assert report["metrics"]["humanAcceptanceRate"] is None
    assert report["metrics"]["handoffEvidenceAccuracy"] is None
    assert report["metrics"]["hitlResponseCount"] is None


def test_stream_deduplicates_handoffs_without_counting_them_as_phases(
    tmp_path: Path,
) -> None:
    custom = {
        "kind": "workflow_event",
        "phase": "supervisor->architect",
        "status": "handoff",
        "event_type": "handoff",
        "handoff_id": "supervisor->architect",
        "producer": "supervisor",
        "consumer": "architect",
        "handoff_status": "accepted",
        "payload_digest": "a" * 64,
    }
    envelope = json.dumps(
        {"type": "message", "content": {"custom_data": custom}}
    )
    body = f"data: {envelope}\n\ndata: {envelope}\n\ndata: [DONE]\n\n"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body, request=request)
        )
    )

    observed = _capture_stream(
        client,
        endpoint="http://test/stream",
        payload={},
        headers={},
        root=tmp_path,
        arm="multi_agent",
    )

    assert observed["phases"] == []
    assert observed["handoffs"] == [
        {
            "handoffId": "supervisor->architect",
            "producer": "supervisor",
            "consumer": "architect",
            "status": "accepted",
            "payloadDigest": "a" * 64,
        }
    ]
    assert observed["handoffErrorCount"] == 0


def test_single_agent_handoff_metric_stays_na_and_flags_boundary_violation(
    tmp_path: Path,
) -> None:
    custom = {
        "kind": "workflow_event",
        "phase": "supervisor->architect",
        "status": "handoff",
        "event_type": "handoff",
        "handoff_id": "supervisor->architect",
        "producer": "supervisor",
        "consumer": "architect",
        "handoff_status": "accepted",
        "payload_digest": "a" * 64,
    }
    envelope = json.dumps(
        {"type": "message", "content": {"custom_data": custom}}
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=f"data: {envelope}\n\ndata: [DONE]\n\n",
                request=request,
            )
        )
    )

    observed = _capture_stream(
        client,
        endpoint="http://test/stream",
        payload={},
        headers={},
        root=tmp_path,
        arm="single_agent",
    )

    assert observed["handoffs"] is None
    assert observed["handoffErrorCount"] is None
    assert observed["errors"] == ["unexpected_single_agent_handoff"]


def test_local_artifact_digest_is_recomputed(tmp_path: Path) -> None:
    artifact = tmp_path / "data" / "ratsnestpro" / "artifacts" / "runs" / "x.kicad_sch"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"verified")
    digest = hashlib.sha256(b"verified").hexdigest()
    manifest = {
        "artifacts": [{"name": "x.kicad_sch", "sha256": digest, "object_key": "runs/x.kicad_sch"}]
    }
    facts, valid = _artifact_facts(manifest, tmp_path)
    assert valid is True
    assert facts == [{"name": "x.kicad_sch", "sha256": digest, "valid": True}]


def test_local_artifact_validation_supports_content_addressed_long_paths(
    tmp_path: Path,
) -> None:
    content = b"verified long path"
    digest = hashlib.sha256(content).hexdigest()
    filename = f"{'long-artifact-' * 8}.kicad_pcb"
    object_key = f"runs/{'a' * 36}/{digest}/{filename}"
    artifact = (tmp_path / "data" / "ratsnestpro" / "artifacts" / object_key).resolve()
    native_artifact = _native_path(artifact)
    native_artifact.parent.mkdir(parents=True, exist_ok=True)
    native_artifact.write_bytes(content)
    manifest = {
        "artifacts": [
            {
                "name": filename,
                "sha256": digest,
                "object_key": object_key,
            }
        ]
    }

    facts, valid = _artifact_facts(manifest, tmp_path)

    assert len(str(artifact)) >= 260
    assert valid is True
    assert facts == [{"name": filename, "sha256": digest, "valid": True}]


def test_usage_tokens_prefers_provider_total() -> None:
    event = {"response_metadata": {"usage_metadata": {"total_tokens": 42}}}
    assert _usage_tokens(event) == 42
