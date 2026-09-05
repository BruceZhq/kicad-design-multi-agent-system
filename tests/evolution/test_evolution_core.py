from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evolution.collector import (
    ObservationContext,
    aggregate_candidates,
    observation_from_ahe_event,
)
from evolution.contracts import (
    DeliveryOutcome,
    EvalCaseManifest,
    EvolutionCandidate,
    EvolutionObservation,
    HarnessIdentity,
    HarnessManifest,
    resolve_harness_identity,
)
from evolution.evaluator import (
    compare_reports,
    evaluate_suite,
    load_eval_case,
    load_run_evidence,
)
from evolution.optimizer import (
    OptimizerRequest,
    PatchChange,
    PatchPlan,
    PublicCaseResult,
    PublicEvalSummary,
    build_worktree_plan,
    load_governance_policy,
    optimizer_prompt,
    validate_patch_plan,
)
from evolution.proposal_service import _pinned_context

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64
VERSION_ID = "harness-1.0.0"
PROFILE_DIGEST = "d" * 64
FINGERPRINT_KEY = b"evolution-tests-key-material-32-bytes-minimum"


def _harness() -> HarnessIdentity:
    return HarnessIdentity(
        version_id=VERSION_ID,
        channel="stable",
        manifest_digest=DIGEST,
    )


def _manifest() -> HarnessManifest:
    return HarnessManifest(
        source_commit="a" * 40,
        source_tree_digest="b" * 64,
        dirty=False,
        bundle_digest="c" * 64,
        contract_digest="d" * 64,
        policy_digest="e" * 64,
        manifest_digest="f" * 64,
    )


def _candidate() -> EvolutionCandidate:
    return EvolutionCandidate(
        candidate_id="1" * 64,
        base_harness_version_id=VERSION_ID,
        base_manifest_digest="f" * 64,
        failure_signature="generic-signature",
        step="selection",
        check_name="structured_output",
        category="structured_output",
        required_capability="structured_output_recovery",
        profile_references=["sipi-channel-pdn-eval@1.0"],
        observation_ids=["2" * 64],
        occurrence_count=2,
        project_count=2,
        status="eligible",
    )


def _ahe_event(event_type: str = "capability_gap") -> dict[str, object]:
    detail_key = "gap" if event_type == "capability_gap_resolved" else "failure"
    event: dict[str, object] = {
        "kind": "ahe_event",
        "event": event_type,
        "step": "selection",
        "revision": 2,
        detail_key: {
            "signature": "stable-general-signature",
            "check_name": "structured_output",
            "category": "structured_output",
            "recoverability": "capability_gap",
            "required_capability": "structured_output_recovery",
            "message": "private-project-secret must never be persisted",
            "evidence": {"raw": "private-project-secret"},
        },
    }
    if event_type == "capability_gap":
        event["attribution"] = {
            "action": "capability_gap",
            "reason_code": "cross_run_reproducible_harness_defect",
            "origin": "harness",
            "independent_run_count": 2,
            "independent_project_count": 2,
        }
    return event


def _observation(project: str, seq: int, event_type: str = "capability_gap"):
    context = ObservationContext(
        tenant_id="tenant-private",
        project_id=project,
        run_id=f"run-{project}",
        source_event_seq=seq,
        harness=_harness(),
        profile_reference="sipi-channel-pdn-eval@1.0",
        profile_digest=PROFILE_DIGEST,
    )
    return observation_from_ahe_event(
        _ahe_event(event_type),
        context,
        fingerprint_key=FINGERPRINT_KEY,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq),
    )


def test_run_identity_matches_deployed_runtime() -> None:
    identity = resolve_harness_identity(
        {
            "runtime_config": {
                "harness_version": {
                    "id": "run-pinned",
                    "channel": "canary",
                    "manifest_digest": "b" * 64,
                }
            }
        },
        environ={
            "RATSNEST_HARNESS_VERSION_ID": "run-pinned",
            "RATSNEST_HARNESS_CHANNEL": "canary",
            "RATSNEST_HARNESS_MANIFEST_DIGEST": "b" * 64,
        },
        require_explicit=True,
    )
    assert identity.version_id == "run-pinned"
    assert identity.channel == "canary"
    assert identity.manifest_digest == "b" * 64


def test_ahe_observation_is_private_and_requires_cross_project_evidence() -> None:
    first = _observation("project-one", 1)
    second = _observation("project-two", 2)
    serialized = first.model_dump_json(by_alias=True)

    assert "private-project-secret" not in serialized
    assert "tenant-private" not in serialized
    assert "project-one" not in serialized
    candidates = aggregate_candidates([first, second])
    assert len(candidates) == 1
    assert candidates[0].status == "eligible"
    assert candidates[0].project_count == 2


def test_capability_gap_attribution_fails_closed() -> None:
    forged = _ahe_event()
    forged.pop("attribution")
    context = ObservationContext(
        tenant_id="tenant-private",
        project_id="project-one",
        run_id="run-one",
        source_event_seq=1,
        harness=_harness(),
        profile_reference="sipi-channel-pdn-eval@1.0",
        profile_digest=PROFILE_DIGEST,
    )

    with pytest.raises(ValueError, match="governed attribution"):
        observation_from_ahe_event(
            forged,
            context,
            fingerprint_key=FINGERPRINT_KEY,
        )


def test_resolved_project_is_removed_from_candidate_evidence() -> None:
    observations = [
        _observation("project-one", 1),
        _observation("project-two", 2),
        _observation("project-one", 3, "capability_gap_resolved"),
    ]
    candidate = aggregate_candidates(observations)[0]
    assert candidate.status == "observed"
    assert candidate.project_count == 1


def test_recorded_manifests_grade_without_llm_or_eda() -> None:
    case_dir = ROOT / "evals" / "manifests"
    bad_case = load_eval_case(case_dir / "release-truth-missing-artifact.v1.json")
    good_case = load_eval_case(case_dir / "ahe-recovery.v1.json")
    bad_evidence = load_run_evidence(ROOT / bad_case.input_ref)
    good_evidence = load_run_evidence(ROOT / good_case.input_ref)

    baseline = evaluate_suite(
        [bad_case, good_case],
        {bad_case.case_id: bad_evidence, good_case.case_id: good_evidence},
        harness=_harness(),
    )
    candidate_evidence = bad_evidence.model_copy(
        update={
            "outcome": DeliveryOutcome.EXECUTION_BLOCKED,
                "invariant_results": {
                    "release-truth": True,
                    "no-fabricated-eda-evidence": True,
                },
                "tool_calls": ["reviewer.evaluate"],
            }
        )
    candidate = evaluate_suite(
        [bad_case, good_case],
        {bad_case.case_id: candidate_evidence, good_case.case_id: good_evidence},
        harness=HarnessIdentity(
            version_id="harness-1.0.1",
            channel="evaluation",
            manifest_digest="b" * 64,
        ),
    )
    comparison = compare_reports(baseline, candidate)

    assert baseline.metrics.passed_cases == 1
    assert comparison.improved_cases == [bad_case.case_id]
    assert comparison.regressed_cases == []
    assert comparison.cost_guard_passed
    assert comparison.candidate_passed

    unchanged = compare_reports(candidate, candidate)
    assert not unchanged.candidate_passed

    expensive = candidate.model_copy(
        update={
            "metrics": candidate.metrics.model_copy(
                update={
                    "total_llm_tokens": baseline.metrics.total_llm_tokens * 2,
                    "total_wall_clock_seconds": (baseline.metrics.total_wall_clock_seconds * 2),
                }
            )
        }
    )
    cost_regression = compare_reports(baseline, expensive)
    assert not cost_regression.cost_guard_passed
    assert not cost_regression.candidate_passed


def test_sealed_holdout_and_adversarial_cases_are_deterministic() -> None:
    case_dir = ROOT / "evals" / "sealed" / "manifests"
    cases = [
        load_eval_case(case_dir / "constraint-preservation.v1.json"),
        load_eval_case(case_dir / "prompt-injection-release-truth.v1.json"),
        load_eval_case(case_dir / "systemic-grounding-and-evolution.v1.json"),
    ]
    evidence = {case.case_id: load_run_evidence(ROOT / case.input_ref) for case in cases}
    report = evaluate_suite(cases, evidence, harness=_harness())

    assert all(case.sealed for case in cases)
    assert {case.suite for case in cases} == {"holdout", "adversarial"}
    assert report.metrics.passed_cases == 3


def test_eval_suite_index_is_content_addressed() -> None:
    for suite_name in (
        "core.v1.json",
        "optimization.v1.json",
        "holdout.v1.json",
        "adversarial.v1.json",
    ):
        index = json.loads((ROOT / "evals" / "suites" / suite_name).read_text())
        entries = []
        for item in index["cases"]:
            manifest_path = ROOT / item["ref"]
            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            assert digest == item["sha256"]
            manifest = load_eval_case(manifest_path)
            evidence_digest = hashlib.sha256((ROOT / manifest.input_ref).read_bytes()).hexdigest()
            assert evidence_digest == manifest.input_digest
            entries.append(f"{item['ref']}|{digest}")
        assert hashlib.sha256("\n".join(entries).encode()).hexdigest() == index["suiteDigest"]


def test_patch_plan_uses_the_single_governance_registry(tmp_path: Path) -> None:
    policy = load_governance_policy(ROOT / "config" / "harness" / "invariants.v1.json")
    preserved = [item.id for item in policy.invariants if not item.mutable_by_evolution]
    plan = PatchPlan(
        candidate_id=_candidate().candidate_id,
        base_commit=_manifest().source_commit,
        summary="Tighten a generic structured-output recovery path and add its regression.",
        changes=[
            PatchChange(
                operation="modify",
                path="src/agents/ratsnestpro/intent_router.py",
                rationale="Use a generic recovery contract.",
                estimated_added_lines=8,
            ),
            PatchChange(
                operation="create",
                path="docs/evolution-candidate.md",
                rationale="Record the candidate's bounded behavior change for review.",
                estimated_added_lines=20,
            ),
        ],
        preserved_invariants=preserved,
    )
    assert (
        validate_patch_plan(
            plan,
            candidate=_candidate(),
            harness_manifest=_manifest(),
            policy=policy,
        )
        == plan
    )

    denied = plan.model_copy(
        update={
            "changes": [
                PatchChange(
                    operation="modify",
                    path="backend/src/main/resources/db/migration/V99__bypass.sql",
                    rationale="This must be denied.",
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="denies patch path"):
        validate_patch_plan(
            denied,
            candidate=_candidate(),
            harness_manifest=_manifest(),
            policy=policy,
        )

    worktree = build_worktree_plan(
        plan,
        repository_root=ROOT,
        worktree_root=ROOT.parent / ".evolution-test-worktrees" / tmp_path.name,
    )
    assert worktree.create_command[0] == "git"
    assert not worktree.automatic_merge
    assert not worktree.automatic_deploy


def test_optimizer_prompt_cannot_receive_sealed_case_content() -> None:
    policy = load_governance_policy(ROOT / "config" / "harness" / "invariants.v1.json")
    request = OptimizerRequest(
        candidate=_candidate(),
        harness_manifest=_manifest(),
        public_eval_summary=PublicEvalSummary(
            case_results=[
                PublicCaseResult(
                    case_id="harness.ahe-bounded-recovery.v1",
                    passed=False,
                    failed_graders=["recovery"],
                )
            ],
            improvement_count=0,
            regression_count=0,
        ),
        repository_context={"src/agents/ratsnestpro/intent_router.py": "def route(): ..."},
    )
    prompt = optimizer_prompt(request, policy)
    assert "harness.ahe-bounded-recovery.v1" in prompt
    assert "inputRef" not in prompt
    assert "evals/sealed" not in prompt

    poisoned = request.model_copy(
        update={
            "repository_context": {
                "evals/sealed/fixtures/prompt-injection-release-truth.v1.json": "secret"
            }
        }
    )
    with pytest.raises(ValueError, match="denied path"):
        optimizer_prompt(poisoned, policy)


def test_proposal_service_rejects_sealed_context_before_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = load_governance_policy(ROOT / "config" / "harness" / "invariants.v1.json")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("sealed source was read before policy validation")

    monkeypatch.setattr("evolution.proposal_service._git_object", forbidden_read)
    with pytest.raises(ValueError, match="denied path"):
        _pinned_context(
            ROOT,
            "a" * 40,
            ["evals/sealed/regression/holdout.v1.json"],
            policy,
        )


def test_json_schema_tracks_python_contract_aliases() -> None:
    bundle = json.loads(
        (ROOT / "contracts" / "evolution" / "v1" / "evolution.schema.json").read_text(
            encoding="utf-8"
        )
    )
    definitions = bundle["$defs"]
    for model in (
        HarnessManifest,
        EvolutionObservation,
        EvalCaseManifest,
        EvolutionCandidate,
    ):
        expected = set(model.model_json_schema(by_alias=True)["properties"])
        assert set(definitions[model.__name__]["properties"]) == expected
