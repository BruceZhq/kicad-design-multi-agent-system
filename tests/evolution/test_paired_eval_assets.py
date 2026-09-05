import hashlib
import json
from pathlib import Path

import pytest

from evolution.live_runner import (
    LivePlan,
    _arm_metrics,
    _paired_comparison,
    _validate_asset_manifest,
)

ROOT = Path(__file__).parents[2]
PLAN_PATH = ROOT / "frontend" / "public" / "evals" / "paired-kicad-golden.v1.json"
ASSET_PATH = ROOT / "evals" / "paired" / "kicad-assets.v1.json"
CONFIG_PATH = ROOT / "evals" / "paired" / "frozen-config.v1.json"
BLIND_TEMPLATE_PATH = ROOT / "evals" / "paired" / "blind-review-template.v1.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan() -> LivePlan:
    return LivePlan.model_validate_json(PLAN_PATH.read_bytes())


def test_golden_plan_has_ten_counterbalanced_complete_pairs() -> None:
    plan = _plan()
    assert len(plan.cases) == 20
    pairs: dict[str, list] = {}
    for case in plan.cases:
        pairs.setdefault(str(case.pair_id), []).append(case)
    assert len(pairs) == 10
    for index, pair_cases in enumerate(pairs.values(), start=1):
        assert [case.arm for case in pair_cases] == (
            ["single_agent", "multi_agent"]
            if index % 2
            else ["multi_agent", "single_agent"]
        )
        assert pair_cases[0].prompt == pair_cases[1].prompt
        assert pair_cases[0].verified_asset_ids == pair_cases[1].verified_asset_ids
        assert pair_cases[0].agent_config == pair_cases[1].agent_config


def test_golden_prompts_are_natural_and_have_no_eval_or_orchestration_cues() -> None:
    forbidden = ("hitl", "评测", "single_agent", "multi_agent", "supervisor", "architect")
    for case in _plan().cases:
        lowered = case.prompt.casefold()
        assert not any(term in lowered for term in forbidden)
        assert "KiCad symbol:" in case.prompt
        assert case.prompt.count("KiCad symbol:") == case.prompt.count("KiCad footprint:")


def test_asset_and_frozen_config_digests_are_bound_and_ids_exist() -> None:
    plan = _plan()
    config = json.loads(CONFIG_PATH.read_bytes())
    blind_template = json.loads(BLIND_TEMPLATE_PATH.read_bytes())
    assert plan.asset_manifest_digest == _digest(ASSET_PATH)
    assert plan.frozen_execution is not None
    assert plan.frozen_execution.environment_digest == _digest(ASSET_PATH)
    assert plan.frozen_execution.config_digest == _digest(CONFIG_PATH)
    assert config["assetSnapshotDigest"] == _digest(ASSET_PATH)
    assert blind_template["planDigest"] == _digest(PLAN_PATH)
    verified = _validate_asset_manifest(ROOT, plan)
    assert verified
    assert all(set(case.verified_asset_ids) <= verified for case in plan.cases)


def test_unknown_verified_asset_fails_before_execution() -> None:
    document = json.loads(PLAN_PATH.read_bytes())
    for case in document["cases"]:
        if case["pairId"] == "golden.p01":
            case["verifiedAssetIds"].append("invented-part")
    plan = LivePlan.model_validate(document)
    with pytest.raises(ValueError, match="unknown verifiedAssetIds"):
        _validate_asset_manifest(ROOT, plan)


def _result(pair_id: str, arm: str, *, passed: bool, duration: float) -> dict:
    return {
        "caseId": f"{pair_id}.{arm}",
        "category": "eda_pipeline",
        "pairId": pair_id,
        "arm": arm,
        "passed": passed,
        "humanAcceptance": None,
        "checks": {
            "terminal": True,
            "requiredPhases": True,
            "forbiddenPhases": True,
            "requiredTools": True,
            "forbiddenTools": True,
        },
        "observed": {
            "httpStatus": 200,
            "durationSeconds": duration,
            "completedSteps": 17,
            "deliveryStatus": "release_ready" if passed else "delivered_with_issues",
            "releaseEvidence": {
                "pipelineResultValid": True,
                "releaseReady": passed,
                "strictGatePassed": passed,
                "ercErrors": 0 if passed else 1,
                "drcErrors": 0 if passed else 1,
                "unconnected": 0 if passed else 1,
                "ercClean": passed,
                "drcClean": passed,
                "zeroUnconnected": passed,
                "routingComplete": passed,
                "coreArtifactsPresent": passed,
                "artifactIdentity": {"valid": passed},
                "failureFacts": [],
            },
            "toolCalls": [],
            "handoffs": None if arm == "single_agent" else [],
            "handoffErrorCount": None if arm == "single_agent" else 0,
            "hitl": {"requestCount": 0},
        },
    }


def test_paired_delta_uses_complete_pairs_only_and_single_handoff_is_na() -> None:
    results = [
        _result("p1", "single_agent", passed=False, duration=4),
        _result("p1", "multi_agent", passed=True, duration=6),
        _result("p2", "multi_agent", passed=True, duration=100),
    ]
    comparison = _paired_comparison(results)
    assert comparison["pairCount"] == 2
    assert comparison["deltaDenominatorCompletePairs"] == 1
    assert comparison["metricDeltas"]["strictTaskSuccessRate"] == 1.0
    assert comparison["metricDeltas"]["meanDurationSeconds"] == 2.0
    assert comparison["completePairArmMetrics"]["single_agent"]["handoffEvidenceStatus"] == "not_applicable"
    assert _arm_metrics(results[1:], "multi_agent")["p95DurationSeconds"] is None
