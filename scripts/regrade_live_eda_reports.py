"""Regrade raw live reports with the current strict EDA checks."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evolution.live_runner import LivePlan, _grade


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def regrade(plan_path: Path, report_paths: list[Path]) -> dict[str, Any]:
    plan = LivePlan.model_validate_json(plan_path.read_bytes())
    cases_by_id = {case.case_id: case for case in plan.cases}
    results: list[dict[str, Any]] = []
    for report_path in report_paths:
        raw = _load(report_path)
        for item in raw.get("cases", []):
            case_id = str(item.get("caseId", ""))
            case = cases_by_id.get(case_id)
            if case is None:
                raise ValueError(f"report contains unknown case: {case_id}")
            observed = item.get("observed")
            if not isinstance(observed, dict):
                raise ValueError(f"report case has no observed result: {case_id}")
            replay = item.get("replay")
            checks = _grade(case, observed, replay if isinstance(replay, dict) else None)
            results.append(
                {
                    "caseId": case_id,
                    "sourceReport": report_path.as_posix(),
                    "rawPassed": bool(item.get("passed")),
                    "strictPassed": all(checks.values()),
                    "checks": checks,
                    "observed": {
                        "completedSteps": observed.get("completedSteps", 0),
                        "deliveryStatus": observed.get("deliveryStatus"),
                        "durationSeconds": observed.get("durationSeconds", 0),
                        "artifactNames": [
                            str(artifact.get("name", ""))
                            for artifact in observed.get("artifacts", [])
                            if isinstance(artifact, dict)
                        ],
                        "artifactsValid": bool(observed.get("artifactsValid", False)),
                    },
                }
            )
    count = len(results)
    passed = sum(item["strictPassed"] for item in results)
    false_passes = sum(item["rawPassed"] and not item["strictPassed"] for item in results)
    return {
        "schemaVersion": "1.0",
        "scope": "strict_17_step_eda_regrade",
        "createdAt": datetime.now(UTC).isoformat(),
        "planId": plan.plan_id,
        "criteria": {
            "completedSteps": 17,
            "acceptedDeliveryStatuses": [
                "completed_with_issues",
                "delivered_with_issues",
                "release_ready",
            ],
            "requiredArtifacts": [
                "pipeline_result.json",
                ".kicad_sch",
                ".kicad_pcb",
            ],
        },
        "metrics": {
            "caseCount": count,
            "strictPassedCases": passed,
            "strictPassRate": passed / count if count else 0.0,
            "rawFalsePassCount": false_passes,
        },
        "cases": results,
    }


def _write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Strict live EDA regrade",
        "",
        "> A case passes only when the 17-step pipeline reaches an accepted terminal status and publishes valid core EDA artifacts.",
        "",
        f"- Plan: `{report['planId']}`",
        f"- Strict cases: {report['metrics']['strictPassedCases']}/{report['metrics']['caseCount']}",
        f"- Raw false passes: {report['metrics']['rawFalsePassCount']}",
        "",
        "| Case | Steps | Delivery | Raw | Strict | Missing check |",
        "|---|---:|---|---|---|---|",
    ]
    for item in report["cases"]:
        failed = ", ".join(name for name, passed in item["checks"].items() if not passed)
        observed = item["observed"]
        lines.append(
            f"| `{item['caseId']}` | {observed['completedSteps']} | "
            f"{observed['deliveryStatus'] or '-'} | "
            f"{'PASS' if item['rawPassed'] else 'FAIL'} | "
            f"{'PASS' if item['strictPassed'] else 'FAIL'} | {failed or '-'} |"
        )
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = regrade(args.plan, args.report)
    _write(args.output, report)
    print(json.dumps(report["metrics"], ensure_ascii=False))
    return int(report["metrics"]["strictPassedCases"] != report["metrics"]["caseCount"])


if __name__ == "__main__":
    raise SystemExit(main())
