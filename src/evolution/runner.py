"""CLI for content-addressed, deterministic Agent evaluation reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from evolution.contracts import EvalCaseManifest, EvalReport, HarnessIdentity, RunEvidence
from evolution.evaluator import evaluate_suite, load_eval_case, load_run_evidence

_DEFAULT_SUITES = (
    "evals/suites/holdout.v1.json",
    "evals/suites/adversarial.v1.json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_file(root: Path, reference: str) -> Path:
    candidate = (root / reference).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(f"evaluation reference escapes repository root: {reference}")
    if not candidate.is_file():
        raise ValueError(f"evaluation reference is not a file: {reference}")
    return candidate


def load_content_addressed_suites(
    root: Path,
    suite_refs: Sequence[str],
) -> tuple[list[EvalCaseManifest], dict[str, RunEvidence], list[str]]:
    """Load suites only after checking suite, manifest, and evidence digests."""

    cases: list[EvalCaseManifest] = []
    evidence_by_case: dict[str, RunEvidence] = {}
    suite_digests: list[str] = []
    seen: set[str] = set()
    for suite_ref in suite_refs:
        suite_path = _repo_file(root, suite_ref)
        raw = json.loads(suite_path.read_text(encoding="utf-8"))
        if raw.get("schemaVersion") != "1.0" or not isinstance(raw.get("cases"), list):
            raise ValueError(f"invalid evaluation suite schema: {suite_ref}")
        entries: list[str] = []
        for item in raw["cases"]:
            manifest_ref = str(item["ref"])
            manifest_path = _repo_file(root, manifest_ref)
            manifest_digest = _sha256(manifest_path)
            if manifest_digest != item.get("sha256"):
                raise ValueError(f"manifest digest mismatch: {manifest_ref}")
            entries.append(f"{manifest_ref}|{manifest_digest}")
            case = load_eval_case(manifest_path)
            evidence_path = _repo_file(root, case.input_ref)
            if _sha256(evidence_path) != case.input_digest:
                raise ValueError(f"evidence digest mismatch: {case.input_ref}")
            if case.case_id in seen:
                continue
            seen.add(case.case_id)
            cases.append(case)
            evidence_by_case[case.case_id] = load_run_evidence(evidence_path)
        calculated = hashlib.sha256("\n".join(entries).encode()).hexdigest()
        if calculated != raw.get("suiteDigest"):
            raise ValueError(f"suite digest mismatch: {suite_ref}")
        suite_digests.append(calculated)
    return cases, evidence_by_case, suite_digests


def run_recorded_evaluation(root: Path, suite_refs: Sequence[str]) -> EvalReport:
    cases, evidence, suite_digests = load_content_addressed_suites(root, suite_refs)
    identity_digest = hashlib.sha256("\n".join(sorted(suite_digests)).encode()).hexdigest()
    return evaluate_suite(
        cases,
        evidence,
        harness=HarnessIdentity(
            version_id=f"recorded-public-{identity_digest[:12]}",
            channel="evaluation",
            manifest_digest=identity_digest,
        ),
    )


def render_markdown(report: EvalReport, suite_refs: Sequence[str]) -> str:
    """Render a recruiter-readable report without overstating live coverage."""

    metrics = report.metrics
    lines = [
        "# Recorded Agent evaluation report",
        "",
        "> Scope: deterministic replay of content-addressed, sanitized evidence. "
        "This report is not a live LLM, KiCad, Kubernetes, latency, or manufacturing benchmark.",
        "",
        f"- Harness: `{report.harness.version_id}`",
        f"- Suites: {', '.join(f'`{item}`' for item in suite_refs)}",
        f"- Case pass rate: {metrics.passed_cases}/{metrics.case_count} "
        f"({metrics.pass_rate:.1%})",
        f"- Tool Call Accuracy: {metrics.tool_call_accuracy:.1%}",
        f"- State Transition Accuracy: {metrics.state_transition_accuracy:.1%}",
        f"- Goal/Artifact Completion: {metrics.goal_completion_rate:.1%}",
        f"- Release Gate Accuracy: {metrics.gate_accuracy:.1%}",
        f"- Recovery Success Rate: {metrics.recovery_success_rate:.1%}",
        f"- False Release: {metrics.false_release_count}/{metrics.case_count}",
        "",
        "| Case | Result | Failed graders |",
        "|---|---:|---|",
    ]
    for case in report.cases:
        failed = [result.grader_id.value for result in case.grader_results if not result.passed]
        lines.append(
            f"| `{case.case_id}` | {'PASS' if case.passed else 'FAIL'} | "
            f"{', '.join(failed) or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Metric definitions",
            "",
            "- Tool Call Accuracy checks required-tool presence and forbidden-tool absence.",
            "- State Transition Accuracy is the deterministic trajectory-grader score.",
            "- Goal/Artifact Completion requires existence, validity, and SHA-256 evidence.",
            "- Release Gate Accuracy combines artifact, release-truth, and security graders.",
            "- False Release counts any `release_ready` outcome that violates its case contract.",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--suite", action="append", dest="suites")
    parser.add_argument("--json", type=Path, required=True, dest="json_output")
    parser.add_argument("--markdown", type=Path, required=True, dest="markdown_output")
    parser.add_argument("--min-pass-rate", type=float, default=1.0)
    parser.add_argument("--max-false-release-count", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    suite_refs = tuple(args.suites or _DEFAULT_SUITES)
    report = run_recorded_evaluation(args.root.resolve(), suite_refs)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        report.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    args.markdown_output.write_text(render_markdown(report, suite_refs), encoding="utf-8")
    print(
        f"cases={report.metrics.passed_cases}/{report.metrics.case_count} "
        f"tool_call_accuracy={report.metrics.tool_call_accuracy:.3f} "
        f"false_release={report.metrics.false_release_count}"
    )
    return int(
        report.metrics.pass_rate < args.min_pass_rate
        or report.metrics.false_release_count > args.max_false_release_count
    )


if __name__ == "__main__":
    raise SystemExit(main())
