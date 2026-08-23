"""Deterministic, recorded-evidence graders for harness version comparison."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from evolution.contracts import (
    CaseEvaluation,
    DeliveryOutcome,
    EvalCaseManifest,
    EvalComparison,
    EvalMetrics,
    EvalReport,
    GraderId,
    GraderResult,
    HarnessIdentity,
    RunEvidence,
)

_MAX_COST_REGRESSION_RATIO = 0.20


def load_eval_case(path: Path) -> EvalCaseManifest:
    return EvalCaseManifest.model_validate_json(path.read_text(encoding="utf-8"))


def load_run_evidence(path: Path) -> RunEvidence:
    return RunEvidence.model_validate_json(path.read_text(encoding="utf-8"))


def _result(
    grader: GraderId,
    details: list[str],
    *,
    score: float | None = None,
) -> GraderResult:
    return GraderResult(
        grader_id=grader,
        passed=not details,
        score=(1.0 if not details else 0.0) if score is None else score,
        details=details,
    )


def _artifact_matches(path: str, requirement: str) -> bool:
    normalized = path.replace("\\", "/")
    if requirement.startswith("."):
        return normalized.casefold().endswith(requirement.casefold())
    return PurePosixPath(normalized).name.casefold() == requirement.casefold()


def _artifact_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    details: list[str] = []
    for required in case.expectation.required_artifacts:
        matches = [item for item in evidence.artifacts if _artifact_matches(item.path, required)]
        if not matches:
            details.append(f"missing artifact evidence: {required}")
        elif not any(item.exists and item.valid and item.sha256 for item in matches):
            details.append(f"artifact is not existence/hash/validity grounded: {required}")
    return _result(GraderId.ARTIFACT, details)


def _intent_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    expected = case.expectation.expected_intent
    details = (
        []
        if expected is None or evidence.intent_mode == expected
        else [f"intent {evidence.intent_mode!r} != expected {expected!r}"]
    )
    return _result(GraderId.INTENT, details)


def _tool_call_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    required = set(case.expectation.required_tools)
    forbidden = set(case.expectation.forbidden_tools)
    actual = set(evidence.tool_calls)
    missing = sorted(required - actual)
    disallowed = sorted(forbidden & actual)
    checks = len(required) + len(forbidden)
    satisfied = len(required & actual) + len(forbidden - actual)
    score = satisfied / checks if checks else 1.0
    details = [f"required tool not called: {item}" for item in missing]
    details.extend(f"forbidden tool called: {item}" for item in disallowed)
    return _result(GraderId.TOOL_CALL, details, score=score)


def _trajectory_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    details: list[str] = []
    if evidence.completed_steps < case.expectation.min_completed_steps:
        details.append(
            f"completed {evidence.completed_steps} < {case.expectation.min_completed_steps}"
        )
    if evidence.total_steps != 17:
        details.append(f"pipeline total_steps must be 17, got {evidence.total_steps}")
    expected_roles = case.expectation.expected_role_sequence
    if expected_roles and evidence.role_sequence != expected_roles:
        details.append("agent role sequence differs from the case manifest")
    return _result(GraderId.TRAJECTORY, details)


def _release_truth_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    details: list[str] = []
    if evidence.outcome not in case.expectation.allowed_outcomes:
        details.append(f"unexpected delivery outcome: {evidence.outcome}")
    expected_complete = case.expectation.require_execution_complete
    if expected_complete is not None and evidence.execution_complete != expected_complete:
        details.append("execution_complete differs from the case manifest")
    if evidence.outcome == DeliveryOutcome.RELEASE_READY:
        if not evidence.execution_complete or evidence.completed_steps != evidence.total_steps:
            details.append("release_ready requires a complete pipeline")
        if evidence.release_blockers:
            details.append("release_ready cannot contain release blockers")
        if evidence.independent_review != "passed":
            details.append("release_ready requires independent review")
    if evidence.outcome == DeliveryOutcome.DELIVERED_WITH_ISSUES:
        if not evidence.execution_complete:
            details.append("delivered_with_issues requires completed mechanical execution")
        if not evidence.release_blockers:
            details.append("delivered_with_issues must identify at least one issue")
    if case.expectation.require_independent_review and evidence.independent_review != "passed":
        details.append("independent review did not pass")
    return _result(GraderId.RELEASE_TRUTH, details)


def _recovery_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    details: list[str] = []
    if case.expectation.require_ahe_recovery:
        if evidence.ahe_repair_count < 1:
            details.append("case requires at least one AHE repair")
        if not evidence.recovered_from_fault:
            details.append("fault was not recovered")
    return _result(GraderId.RECOVERY, details)


def _security_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    missing = [
        invariant
        for invariant in case.invariants
        if evidence.invariant_results.get(invariant) is not True
    ]
    return _result(
        GraderId.SECURITY,
        [f"invariant not proven: {value}" for value in missing],
    )


def _cost_grader(case: EvalCaseManifest, evidence: RunEvidence) -> GraderResult:
    expected = case.expectation
    details: list[str] = []
    if expected.max_ahe_repairs is not None and (
        evidence.ahe_repair_count > expected.max_ahe_repairs
    ):
        details.append("AHE repair budget exceeded")
    if expected.max_llm_tokens is not None and evidence.llm_tokens > expected.max_llm_tokens:
        details.append("LLM token budget exceeded")
    if expected.max_wall_clock_seconds is not None and (
        evidence.wall_clock_seconds > expected.max_wall_clock_seconds
    ):
        details.append("wall-clock budget exceeded")
    return _result(GraderId.COST, details)


_GRADERS = {
    GraderId.INTENT: _intent_grader,
    GraderId.TOOL_CALL: _tool_call_grader,
    GraderId.TRAJECTORY: _trajectory_grader,
    GraderId.ARTIFACT: _artifact_grader,
    GraderId.RELEASE_TRUTH: _release_truth_grader,
    GraderId.RECOVERY: _recovery_grader,
    GraderId.SECURITY: _security_grader,
    GraderId.COST: _cost_grader,
}


def evaluate_case(case: EvalCaseManifest, evidence: RunEvidence) -> CaseEvaluation:
    results = [_GRADERS[grader](case, evidence) for grader in case.grader_ids]
    return CaseEvaluation(
        case_id=case.case_id,
        passed=all(item.passed for item in results),
        grader_results=results,
    )


def evaluate_suite(
    cases: Sequence[EvalCaseManifest],
    evidence_by_case: Mapping[str, RunEvidence],
    *,
    harness: HarnessIdentity,
) -> EvalReport:
    evaluations: list[CaseEvaluation] = []
    total_tokens = 0
    total_seconds = 0.0
    for case in cases:
        try:
            evidence = evidence_by_case[case.case_id]
        except KeyError as exc:
            raise ValueError(f"missing evidence for eval case {case.case_id}") from exc
        evaluations.append(evaluate_case(case, evidence))
        total_tokens += evidence.llm_tokens
        total_seconds += evidence.wall_clock_seconds
    grader_count = sum(len(item.grader_results) for item in evaluations)
    passed_graders = sum(result.passed for item in evaluations for result in item.grader_results)
    passed_cases = sum(item.passed for item in evaluations)

    def grader_scores(grader: GraderId) -> list[float]:
        return [
            result.score
            for item in evaluations
            for result in item.grader_results
            if result.grader_id == grader
        ]

    def average(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    grader_pass_rates = {
        grader.value: average(
            [
                float(result.passed)
                for item in evaluations
                for result in item.grader_results
                if result.grader_id == grader
            ]
        )
        for grader in GraderId
        if any(result.grader_id == grader for item in evaluations for result in item.grader_results)
    }
    gate_scores: list[float] = []
    false_release_count = 0
    for case, evaluation in zip(cases, evaluations, strict=True):
        gate_results = [
            result
            for result in evaluation.grader_results
            if result.grader_id
            in {GraderId.ARTIFACT, GraderId.RELEASE_TRUTH, GraderId.SECURITY}
        ]
        if gate_results:
            gate_scores.append(float(all(result.passed for result in gate_results)))
        evidence = evidence_by_case[case.case_id]
        if evidence.outcome == DeliveryOutcome.RELEASE_READY and (
            DeliveryOutcome.RELEASE_READY not in case.expectation.allowed_outcomes
            or not all(result.passed for result in gate_results)
        ):
            false_release_count += 1
    return EvalReport(
        harness=harness,
        cases=evaluations,
        metrics=EvalMetrics(
            case_count=len(evaluations),
            passed_cases=passed_cases,
            grader_count=grader_count,
            passed_graders=passed_graders,
            pass_rate=(passed_cases / len(evaluations) if evaluations else 0.0),
            total_llm_tokens=total_tokens,
            total_wall_clock_seconds=total_seconds,
            grader_pass_rates=grader_pass_rates,
            tool_call_accuracy=average(grader_scores(GraderId.TOOL_CALL)),
            state_transition_accuracy=average(grader_scores(GraderId.TRAJECTORY)),
            goal_completion_rate=average(grader_scores(GraderId.ARTIFACT)),
            gate_accuracy=average(gate_scores),
            recovery_success_rate=average(grader_scores(GraderId.RECOVERY)),
            false_release_count=false_release_count,
            false_release_rate=(
                false_release_count / len(evaluations) if evaluations else 0.0
            ),
        ),
    )


def compare_reports(baseline: EvalReport, candidate: EvalReport) -> EvalComparison:
    base = {item.case_id: item.passed for item in baseline.cases}
    current = {item.case_id: item.passed for item in candidate.cases}
    if set(base) != set(current):
        raise ValueError("baseline and candidate reports must contain identical case IDs")
    improved = sorted(key for key in base if not base[key] and current[key])
    regressed = sorted(key for key in base if base[key] and not current[key])
    unchanged = sorted(key for key in base if base[key] == current[key])
    token_delta = candidate.metrics.total_llm_tokens - baseline.metrics.total_llm_tokens
    wall_clock_delta = (
        candidate.metrics.total_wall_clock_seconds - baseline.metrics.total_wall_clock_seconds
    )

    def within_cost_guard(candidate_value: float, baseline_value: float) -> bool:
        if baseline_value == 0:
            return candidate_value == 0
        return candidate_value <= baseline_value * (1 + _MAX_COST_REGRESSION_RATIO)

    cost_guard_passed = within_cost_guard(
        candidate.metrics.total_llm_tokens,
        baseline.metrics.total_llm_tokens,
    ) and within_cost_guard(
        candidate.metrics.total_wall_clock_seconds,
        baseline.metrics.total_wall_clock_seconds,
    )
    return EvalComparison(
        baseline_manifest_digest=baseline.harness.manifest_digest,
        candidate_manifest_digest=candidate.harness.manifest_digest,
        improved_cases=improved,
        regressed_cases=regressed,
        unchanged_cases=unchanged,
        cost_guard_passed=cost_guard_passed,
        candidate_passed=(
            bool(improved)
            and not regressed
            and candidate.metrics.passed_cases == len(candidate.cases)
            and cost_guard_passed
        ),
        token_delta=token_delta,
        wall_clock_delta_seconds=wall_clock_delta,
    )
