import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from evolution.live_runner import LiveCase, LivePlan, _artifact_facts, _grade, _usage_tokens


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
    assert all(_grade(case, observed, None).values())


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


def test_usage_tokens_prefers_provider_total() -> None:
    event = {"response_metadata": {"usage_metadata": {"total_tokens": 42}}}
    assert _usage_tokens(event) == 42
