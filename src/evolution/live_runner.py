"""Run fixed Agent cases against the deployed HTTP/SSE runtime.

The runner persists only bounded workflow facts. Prompts, model responses,
reasoning, user identities, credentials, and local project paths are never
written to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.ratsnestpro.profiles.registry import REGISTRY


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class LiveCase(_Model):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$")
    category: Literal[
        "intent_routing",
        "rag_grounding",
        "tool_orchestration",
        "release_gate",
        "recovery_idempotency",
        "prompt_injection",
        "eda_pipeline",
    ]
    prompt: str = Field(min_length=1, max_length=100_000)
    expected_intents: list[str] = Field(min_length=1, max_length=4)
    required_phases: list[str] = Field(default_factory=list, max_length=16)
    forbidden_phases: list[str] = Field(default_factory=list, max_length=16)
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    forbidden_tools: list[str] = Field(default_factory=list, max_length=32)
    required_artifacts: list[str] = Field(default_factory=list, max_length=32)
    expected_terminal: Literal["completed", "waiting_for_input"] = "completed"
    expect_release_ready: bool | None = False
    profile_reference: str | None = None
    replay: Literal["none", "same", "conflict"] = "none"
    timeout_seconds: float = Field(default=900, ge=1, le=36_000)
    agent_config: dict[str, Any] = Field(default_factory=dict)


class LivePlan(_Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(min_length=1, max_length=120)
    cases: list[LiveCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_cases(self) -> LivePlan:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("live evaluation case IDs must be unique")
        return self


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _git_identity(root: Path) -> tuple[str, bool]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return commit, dirty


def _profile_selection(reference: str | None) -> dict[str, str] | None:
    if reference is None:
        return None
    for snapshot in REGISTRY.all():
        if snapshot.reference == reference:
            return snapshot.selection().model_dump(mode="json")
    raise ValueError(f"unknown capability profile: {reference}")


def _usage_tokens(event: dict[str, Any]) -> int:
    metadata = event.get("response_metadata", {})
    if not isinstance(metadata, dict):
        return 0
    usage = metadata.get("usage_metadata") or metadata.get("token_usage") or metadata.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "total_token_count"):
        value = usage.get(key)
        if isinstance(value, int):
            return max(0, value)
    return sum(
        max(0, value)
        for key, value in usage.items()
        if isinstance(value, int) and key in {"input_tokens", "output_tokens"}
    )


def _artifact_facts(manifest: dict[str, Any], root: Path) -> tuple[list[dict[str, Any]], bool]:
    facts: list[dict[str, Any]] = []
    all_valid = True
    artifact_root = (root / "data" / "ratsnestpro" / "artifacts").resolve()
    for raw in manifest.get("artifacts", []):
        if not isinstance(raw, dict):
            all_valid = False
            continue
        name = str(raw.get("name", ""))[:160]
        digest = str(raw.get("sha256", ""))
        object_key = str(raw.get("object_key", ""))
        valid = False
        try:
            candidate = (artifact_root / object_key).resolve()
            candidate.relative_to(artifact_root)
            valid = (
                candidate.is_file()
                and len(digest) == 64
                and _sha256_bytes(candidate.read_bytes()) == digest
            )
        except (OSError, ValueError):
            valid = False
        facts.append({"name": name, "sha256": digest, "valid": valid})
        all_valid = all_valid and valid
    return facts, all_valid


def _capture_stream(
    client: httpx.Client,
    *,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    root: Path,
) -> dict[str, Any]:
    started = monotonic()
    events: list[dict[str, Any]] = []
    phases: list[str] = []
    tools: list[str] = []
    intent = ""
    done = False
    human_input = False
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    completed_steps = 0
    llm_tokens = 0
    with client.stream("POST", endpoint, json=payload, headers=headers) as response:
        status_code = response.status_code
        if status_code >= 400:
            response.read()
            return {
                "httpStatus": status_code,
                "durationSeconds": round(monotonic() - started, 3),
                "done": False,
                "humanInput": False,
                "errors": [f"http_{status_code}"],
                "intent": "",
                "phases": [],
                "tools": [],
                "completedSteps": 0,
                "llmTokens": 0,
                "deliveryStatus": None,
                "artifacts": [],
                "artifactsValid": True,
                "eventDigest": _canonical_digest([]),
            }
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            value = line[6:]
            if value == "[DONE]":
                done = True
                continue
            try:
                envelope = json.loads(value)
            except json.JSONDecodeError:
                errors.append("invalid_sse_json")
                continue
            if not isinstance(envelope, dict):
                continue
            envelope_type = str(envelope.get("type", ""))
            if envelope_type == "error":
                errors.append("agent_stream_error")
            if envelope_type == "ag_ui":
                human_input = True
            custom: dict[str, Any] = {}
            if envelope_type == "artifact_manifest" and isinstance(envelope.get("content"), dict):
                manifest = dict(envelope["content"])
            elif envelope_type == "message" and isinstance(envelope.get("content"), dict):
                message = envelope["content"]
                for call in message.get("tool_calls", []):
                    if isinstance(call, dict) and call.get("name"):
                        tools.append(str(call["name"])[:160])
                if isinstance(message.get("custom_data"), dict):
                    custom = message["custom_data"]
            if custom.get("kind") == "workflow_event":
                phase = str(custom.get("phase", ""))[:160]
                status = str(custom.get("status", ""))[:80]
                if phase:
                    phases.append(phase)
                event: dict[str, Any] = {"kind": "workflow_event", "phase": phase, "status": status}
                if custom.get("event_type") == "intent_decision":
                    intent = str(custom.get("intent", ""))[:40]
                    event["intent"] = intent
                if custom.get("event_type") == "tool_call" and custom.get("tool"):
                    tool = str(custom["tool"])[:160]
                    tools.append(tool)
                    event.update({"tool": tool, "outcome": str(custom.get("outcome", ""))[:80]})
                step_count = custom.get("completed_steps")
                if isinstance(step_count, int):
                    completed_steps = max(completed_steps, step_count)
                    event["completedSteps"] = step_count
                events.append(event)
            elif custom.get("kind") == "llm_output":
                llm_tokens += _usage_tokens(custom)
                events.append(
                    {
                        "kind": "llm_output",
                        "phase": str(custom.get("phase", ""))[:160],
                        "status": str(custom.get("status", ""))[:80],
                    }
                )
            elif custom.get("kind") == "ahe_event":
                events.append(
                    {
                        "kind": "ahe_event",
                        "event": str(custom.get("event", ""))[:80],
                        "step": str(custom.get("step", ""))[:120],
                    }
                )

    artifact_facts, artifacts_valid = _artifact_facts(manifest, root) if manifest else ([], True)
    delivery_status = manifest.get("delivery_status") if manifest else None
    event_facts = {
        "events": events,
        "done": done,
        "humanInput": human_input,
        "deliveryStatus": delivery_status,
        "artifacts": artifact_facts,
    }
    return {
        "httpStatus": status_code,
        "durationSeconds": round(monotonic() - started, 3),
        "done": done,
        "humanInput": human_input,
        "errors": sorted(set(errors)),
        "intent": intent,
        "phases": list(dict.fromkeys(phases)),
        "tools": sorted(set(tools)),
        "completedSteps": completed_steps,
        "llmTokens": llm_tokens,
        "deliveryStatus": delivery_status,
        "artifacts": artifact_facts,
        "artifactsValid": artifacts_valid,
        "eventDigest": _canonical_digest(event_facts),
    }


def _grade(
    case: LiveCase, observed: dict[str, Any], replay: dict[str, Any] | None
) -> dict[str, bool]:
    phases = set(observed["phases"])
    tools = set(observed["tools"])
    artifact_names = [str(item["name"]).casefold() for item in observed["artifacts"]]
    terminal_ok = (
        observed["httpStatus"] == 200
        and not observed["errors"]
        and (
            observed["humanInput"]
            if case.expected_terminal == "waiting_for_input"
            else observed["done"] and not observed["humanInput"]
        )
    )
    artifact_ok = all(
        any(name.endswith(required.casefold()) for name in artifact_names)
        for required in case.required_artifacts
    ) and (observed["artifactsValid"] if case.required_artifacts else True)
    release_ready = observed["deliveryStatus"] == "release_ready"
    release_ok = (
        True
        if case.expect_release_ready is None
        else release_ready == case.expect_release_ready
    )
    if release_ready:
        release_ok = release_ok and bool(observed["artifacts"]) and observed["artifactsValid"]
    replay_ok = True
    if case.replay == "same":
        replay_ok = bool(
            replay
            and replay["httpStatus"] == 200
            and replay["eventDigest"] == observed["eventDigest"]
        )
    elif case.replay == "conflict":
        replay_ok = bool(replay and replay["httpStatus"] == 409)
    return {
        "intent": observed["intent"] in case.expected_intents,
        "requiredPhases": set(case.required_phases) <= phases,
        "forbiddenPhases": not (set(case.forbidden_phases) & phases),
        "requiredTools": set(case.required_tools) <= tools,
        "forbiddenTools": not (set(case.forbidden_tools) & tools),
        "terminal": terminal_ok,
        "artifacts": artifact_ok,
        "releaseGate": release_ok,
        "replay": replay_ok,
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = path.with_suffix(".md")
    metrics = report["metrics"]
    lines = [
        "# Live Agent evaluation",
        "",
        "> Scope: real HTTP/SSE runs with sanitized workflow evidence. This is not a manufacturing approval.",
        "",
        f"- Plan: `{report['planId']}` (`{report['planDigest']}`)",
        f"- Source commit: `{report['sourceCommit']}`",
        f"- Cases: {metrics['passedCases']}/{metrics['caseCount']}",
        f"- Pass rate: {metrics['passRate']:.3f}",
        f"- Intent accuracy: {metrics['intentAccuracy']:.3f}",
        f"- Tool-contract accuracy: {metrics['toolContractAccuracy']:.3f}",
        f"- Gate accuracy: {metrics['gateAccuracy']:.3f}",
        f"- False releases: {metrics['falseReleaseCount']}",
        "",
        "| Case | Category | Intent | Status | Tools | Duration | Result |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for item in report["cases"]:
        lines.append(
            f"| `{item['caseId']}` | {item['category']} | {item['observed']['intent'] or '-'} | "
            f"{item['observed']['deliveryStatus'] or item['expectedTerminal']} | "
            f"{len(item['observed']['tools'])} | {item['observed']['durationSeconds']:.1f}s | "
            f"{'PASS' if item['passed'] else 'FAIL'} |"
        )
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_plan(
    *,
    root: Path,
    plan_path: Path,
    endpoint: str,
    output: Path,
    selected_cases: set[str],
    model: str | None,
    auth_token: str | None,
) -> dict[str, Any]:
    plan_bytes = plan_path.read_bytes()
    plan = LivePlan.model_validate_json(plan_bytes)
    source_commit, dirty = _git_identity(root)
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=None) as client:
        for case in plan.cases:
            if selected_cases and case.case_id not in selected_cases:
                continue
            request_id = f"p2-{case.case_id}-{uuid4().hex[:12]}"
            thread_id = f"p2-thread-{uuid4().hex}"
            agent_config = {
                "intent_llm_enabled": False,
                "project_name": f"p2-{case.case_id}",
                "run_name": f"p2-{case.case_id}-{uuid4().hex[:8]}",
                **case.agent_config,
            }
            profile = _profile_selection(case.profile_reference)
            if profile is not None:
                agent_config["capability_profile"] = profile
            payload: dict[str, Any] = {
                "message": case.prompt,
                "thread_id": thread_id,
                "user_id": "p2-live-eval",
                "request_id": request_id,
                "timeout_seconds": case.timeout_seconds,
                "agent_config": agent_config,
                "stream_tokens": False,
            }
            if model:
                payload["model"] = model
            observed = _capture_stream(
                client,
                endpoint=endpoint,
                payload=payload,
                headers=headers,
                root=root,
            )
            replay: dict[str, Any] | None = None
            if case.replay == "same":
                replay = _capture_stream(
                    client,
                    endpoint=endpoint,
                    payload=payload,
                    headers=headers,
                    root=root,
                )
            elif case.replay == "conflict":
                conflicting = {**payload, "message": case.prompt + "\nconflicting replay"}
                replay = _capture_stream(
                    client,
                    endpoint=endpoint,
                    payload=conflicting,
                    headers=headers,
                    root=root,
                )
            checks = _grade(case, observed, replay)
            result = {
                "caseId": case.case_id,
                "category": case.category,
                "expectedTerminal": case.expected_terminal,
                "requestFingerprint": _sha256_bytes(request_id.encode("utf-8")),
                "observed": observed,
                "replay": replay,
                "checks": checks,
                "passed": all(checks.values()),
            }
            results.append(result)
            report = _report(plan, plan_bytes, source_commit, dirty, results)
            _write_report(output, report)
            print(
                f"{case.case_id}: {'PASS' if result['passed'] else 'FAIL'} "
                f"intent={observed['intent'] or '-'} tools={len(observed['tools'])} "
                f"duration={observed['durationSeconds']:.1f}s",
                flush=True,
            )
    return _report(plan, plan_bytes, source_commit, dirty, results)


def _report(
    plan: LivePlan,
    plan_bytes: bytes,
    source_commit: str,
    dirty: bool,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    count = len(results)
    passed = sum(bool(item["passed"]) for item in results)
    check_rate = lambda key: (  # noqa: E731 - compact metric projection
        sum(bool(item["checks"][key]) for item in results) / count if count else 0.0
    )
    false_releases = sum(
        item["observed"]["deliveryStatus"] == "release_ready" and not item["checks"]["releaseGate"]
        for item in results
    )
    return {
        "schemaVersion": "1.0",
        "scope": "live_http_sse_agent_evaluation_not_manufacturing_approval",
        "planId": plan.plan_id,
        "planDigest": _sha256_bytes(plan_bytes),
        "sourceCommit": source_commit,
        "sourceDirty": dirty,
        "createdAt": datetime.now(UTC).isoformat(),
        "cases": results,
        "metrics": {
            "caseCount": count,
            "passedCases": passed,
            "passRate": passed / count if count else 0.0,
            "intentAccuracy": check_rate("intent"),
            "toolContractAccuracy": (
                (check_rate("requiredTools") + check_rate("forbiddenTools")) / 2
            ),
            "gateAccuracy": check_rate("releaseGate"),
            "falseReleaseCount": false_releases,
            "totalLlmTokens": sum(item["observed"]["llmTokens"] for item in results),
            "totalWallClockSeconds": sum(item["observed"]["durationSeconds"] for item in results),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, default=Path("evals/live/cases.v1.json"))
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8080/ratsnestpro-multi-agent/stream",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--auth-token-env", default="AUTH_SECRET")
    parser.add_argument("--min-pass-rate", type=float, default=0.85)
    parser.add_argument("--max-false-release-count", type=int, default=0)
    args = parser.parse_args()
    root = args.root.resolve()
    plan_path = args.plan if args.plan.is_absolute() else root / args.plan
    output = args.output if args.output.is_absolute() else root / args.output
    report = run_plan(
        root=root,
        plan_path=plan_path,
        endpoint=args.endpoint,
        output=output,
        selected_cases=set(args.case),
        model=args.model,
        auth_token=os.getenv(args.auth_token_env),
    )
    metrics = report["metrics"]
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    return int(
        metrics["passRate"] < args.min_pass_rate
        or metrics["falseReleaseCount"] > args.max_false_release_count
    )


if __name__ == "__main__":
    raise SystemExit(main())
