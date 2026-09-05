"""Execute content-addressed governed regression suites in a candidate checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from time import monotonic
from typing import Any

DEFAULT_SUITES = (
    "evals/regression/optimization.v1.json",
    "evals/sealed/regression/holdout.v1.json",
    "evals/sealed/regression/adversarial.v1.json",
)
_KINDS = {"optimization", "holdout", "adversarial"}
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,119}$")
_MAX_OUTPUT_CHARS = 6_000


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _repo_file(root: Path, reference: str) -> Path:
    candidate = (root / reference).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(f"regression reference escapes repository root: {reference}")
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"regression reference is not a regular file: {reference}")
    return candidate


def _portable_reference(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _digest_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": document.get("schemaVersion"),
        "suiteId": document.get("suiteId"),
        "suiteKind": document.get("suiteKind"),
        "sealed": document.get("sealed"),
        "cases": document.get("cases"),
    }


def calculate_suite_digest(document: dict[str, Any]) -> str:
    """Calculate the digest while excluding the self-referential digest field."""

    return _canonical_digest(_digest_payload(document))


def load_suite(root: Path, reference: str) -> dict[str, Any]:
    manifest_path = _repo_file(root, reference)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schemaVersion") != "1.0":
        raise ValueError(f"invalid regression suite schema: {reference}")
    kind = document.get("suiteKind")
    sealed = document.get("sealed")
    if kind not in _KINDS or not isinstance(sealed, bool):
        raise ValueError(f"invalid regression suite identity: {reference}")
    manifest_is_sealed = "evals/sealed" in manifest_path.as_posix().casefold()
    if sealed != manifest_is_sealed:
        raise ValueError(f"sealed suite location disagrees with manifest: {reference}")
    if kind in {"holdout", "adversarial"} and not sealed:
        raise ValueError(f"{kind} suite must be sealed")
    if kind == "optimization" and sealed:
        raise ValueError("optimization suite must remain public")
    expected_digest = str(document.get("suiteDigest") or "")
    if calculate_suite_digest(document) != expected_digest:
        raise ValueError(f"regression suite digest mismatch: {reference}")

    cases = document.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 64:
        raise ValueError(f"regression suite cases are invalid: {reference}")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "caseId",
            "stage",
            "source",
            "sourceSha256",
            "nodeId",
            "timeoutSeconds",
        }:
            raise ValueError(f"regression case contract is invalid: {reference}")
        case_id = str(case["caseId"])
        if not _CASE_ID.fullmatch(case_id) or case_id in seen:
            raise ValueError(f"duplicate or invalid regression case ID: {case_id}")
        seen.add(case_id)
        source_ref = str(case["source"])
        source_path = _repo_file(root, source_ref)
        if source_path.suffix != ".py":
            raise ValueError(f"regression source must be Python: {source_ref}")
        source_is_sealed = "evals/sealed" in source_path.as_posix().casefold()
        if sealed != source_is_sealed:
            raise ValueError(f"case source sealing disagrees with suite: {case_id}")
        source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_digest != case["sourceSha256"]:
            raise ValueError(f"regression source digest mismatch: {source_ref}")
        node_id = str(case["nodeId"])
        if not node_id.startswith(f"{source_ref}::test_") or any(
            value in node_id for value in ("\n", "\r", "\0")
        ):
            raise ValueError(f"regression nodeId is not bound to its source: {case_id}")
        timeout = case["timeoutSeconds"]
        if not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise ValueError(f"regression timeout is invalid: {case_id}")
        if not str(case["stage"]).strip() or len(str(case["stage"])) > 80:
            raise ValueError(f"regression stage is invalid: {case_id}")
    return document


def _run_case(
    candidate_root: Path,
    suite_root: Path,
    suite: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    started = monotonic()
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    with tempfile.TemporaryDirectory(prefix="ratsnest-regression-") as temporary:
        junit_path = Path(temporary) / "result.xml"
        source_path = _repo_file(suite_root, str(case["source"]))
        test_name = str(case["nodeId"]).rsplit("::", 1)[-1]
        command = (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            temporary,
            "--junitxml",
            str(junit_path),
            f"{source_path}::{test_name}",
        )
        try:
            completed = subprocess.run(
                command,
                cwd=candidate_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=int(case["timeoutSeconds"]),
                check=False,
            )
            output = (completed.stdout + completed.stderr)[-_MAX_OUTPUT_CHARS:]
            exit_code: int | None = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            fragments = [
                value.decode(errors="replace")
                if isinstance(value, bytes)
                else str(value or "")
                for value in (exc.stdout, exc.stderr)
            ]
            output = "".join(fragments)[-_MAX_OUTPUT_CHARS:]
            exit_code = None
            timed_out = True
        exact_execution = _exact_junit_execution(junit_path, str(case["nodeId"]))
        source_unchanged = (
            hashlib.sha256(source_path.read_bytes()).hexdigest()
            == case["sourceSha256"]
        )
    passed = exit_code == 0 and not timed_out and exact_execution and source_unchanged
    identity = (
        f"{suite['suiteKind']}\0{case['caseId']}\0{case['stage']}\0{case['nodeId']}"
    )
    return {
        "caseId": case["caseId"],
        "suiteKind": suite["suiteKind"],
        "stage": case["stage"],
        "passed": passed,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "exactNodeExecuted": exact_execution,
        "sourceUnchanged": source_unchanged,
        "durationMs": max(0, int((monotonic() - started) * 1_000)),
        "failureSignature": None if passed else hashlib.sha256(identity.encode()).hexdigest(),
        "output": output,
    }


def _exact_junit_execution(path: Path, node_id: str) -> bool:
    """Require machine evidence that pytest collected and ran exactly one requested node."""

    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
        return False
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return False
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if len(suites) != 1:
        return False
    suite = suites[0]
    integer = lambda name: int(suite.attrib.get(name, "-1"))  # noqa: E731
    try:
        if (
            integer("tests") != 1
            or integer("failures") != 0
            or integer("errors") != 0
            or integer("skipped") != 0
        ):
            return False
    except ValueError:
        return False
    cases = list(suite.findall("testcase"))
    expected_name = node_id.rsplit("::", 1)[-1]
    return len(cases) == 1 and cases[0].attrib.get("name") == expected_name


def run_suites(
    root: Path,
    references: Sequence[str],
    *,
    suite_root: Path | None = None,
) -> dict[str, Any]:
    trusted_root = (suite_root or root).resolve()
    suites = [(reference, load_suite(trusted_root, reference)) for reference in references]
    kinds = [suite["suiteKind"] for _, suite in suites]
    if len(kinds) != len(set(kinds)):
        raise ValueError("only one governed regression suite per kind is allowed")
    results = [
        _run_case(root, trusted_root, suite, case)
        for _, suite in suites
        for case in suite["cases"]
    ]
    failures_by_stage: dict[str, int] = {}
    for result in results:
        if not result["passed"]:
            stage = str(result["stage"])
            failures_by_stage[stage] = failures_by_stage.get(stage, 0) + 1
    passed = sum(result["passed"] for result in results)
    return {
        "schemaVersion": "1.0",
        "suiteManifests": [
            {
                "reference": reference,
                "suiteId": suite["suiteId"],
                "suiteKind": suite["suiteKind"],
                "sealed": suite["sealed"],
                "suiteDigest": suite["suiteDigest"],
            }
            for reference, suite in suites
        ],
        "caseCount": len(results),
        "passedCases": passed,
        "passRate": passed / len(results) if results else 0.0,
        "passed": passed == len(results),
        "failuresByStage": dict(sorted(failures_by_stage.items())),
        "cases": results,
    }


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_manifests = baseline.get("suiteManifests")
    candidate_manifests = candidate.get("suiteManifests")
    if base_manifests != candidate_manifests:
        raise ValueError("baseline and candidate use different suite manifests")
    base = {str(item["caseId"]): bool(item["passed"]) for item in baseline.get("cases", [])}
    current = {
        str(item["caseId"]): bool(item["passed"]) for item in candidate.get("cases", [])
    }
    if not base or set(base) != set(current):
        raise ValueError("baseline and candidate must contain identical regression cases")
    improved = sorted(key for key in base if not base[key] and current[key])
    regressed = sorted(key for key in base if base[key] and not current[key])
    unchanged = sorted(key for key in base if base[key] == current[key])
    return {
        "improvedCases": improved,
        "regressedCases": regressed,
        "unchangedCases": unchanged,
        "qualityImproved": bool(improved),
        "candidatePassed": not regressed and all(current.values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--suite-root", type=Path)
    parser.add_argument("--suite", action="append", dest="suites")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--expected-suite-digest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    references = tuple(args.suites or DEFAULT_SUITES)
    if args.expected_suite_digest is not None:
        if len(references) != 1:
            raise ValueError("expected-suite-digest requires exactly one suite")
        if not re.fullmatch(r"[0-9a-f]{64}", args.expected_suite_digest):
            raise ValueError("expected-suite-digest must be lowercase SHA-256")
        configured = load_suite(root, references[0])
        if configured["suiteDigest"] != args.expected_suite_digest:
            raise ValueError("configured suite digest does not match executed suite")
    suite_root = args.suite_root.resolve() if args.suite_root is not None else root
    report = run_suites(root, references, suite_root=suite_root)
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        report["comparison"] = compare_reports(baseline, report)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        output = args.output if args.output.is_absolute() else root / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
