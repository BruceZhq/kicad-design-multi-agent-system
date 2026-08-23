"""Low-cardinality Agent telemetry with a strict privacy boundary.

The OpenTelemetry Operator supplies the SDK and exporter in deployed Pods. This
module uses only the public API, so local tests remain dependency-free and the
same code becomes active when an SDK provider is installed by auto-instrumentation.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

_INSTRUMENTATION_NAME = "kicad-design-multi-agent-system"
_TRACER = trace.get_tracer(_INSTRUMENTATION_NAME)
_METER = metrics.get_meter(_INSTRUMENTATION_NAME)

_RUNS = _METER.create_counter(
    "ratsnest.agent.runs",
    description="Agent runs by stable outcome.",
    unit="{run}",
)
_RUN_DURATION = _METER.create_histogram(
    "ratsnest.agent.run.duration",
    description="Agent run duration.",
    unit="s",
)
_INTENTS = _METER.create_counter(
    "ratsnest.agent.intent.decisions",
    description="Intent decisions by bounded intent and decision source.",
    unit="{decision}",
)
_TOOLS = _METER.create_counter(
    "ratsnest.agent.tool.calls",
    description="Tool calls by stable tool name and outcome.",
    unit="{call}",
)
_TOOL_DURATION = _METER.create_histogram(
    "ratsnest.agent.tool.duration",
    description="Tool-call duration including bounded retries.",
    unit="s",
)
_PIPELINE_STEPS = _METER.create_counter(
    "ratsnest.agent.pipeline.steps",
    description="Temporal pipeline step attempts by outcome.",
    unit="{step}",
)
_PIPELINE_DURATION = _METER.create_histogram(
    "ratsnest.agent.pipeline.step.duration",
    description="Temporal pipeline step duration.",
    unit="s",
)
_RELEASE_GATES = _METER.create_counter(
    "ratsnest.agent.release_gate.decisions",
    description="Deterministic release-gate decisions.",
    unit="{decision}",
)

_SENSITIVE_KEY_PARTS = {
    "authorization",
    "body",
    "completion",
    "cookie",
    "credential",
    "input",
    "output",
    "path",
    "principal",
    "project",
    "prompt",
    "query",
    "request_id",
    "secret",
    "sql",
    "statement",
    "tenant",
    "token",
    "user",
}
_LABEL_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _label(value: object, *, fallback: str = "unknown") -> str:
    normalized = _LABEL_RE.sub("_", str(value).strip())[:96]
    return normalized or fallback


def safe_attributes(values: Mapping[str, Any] | None) -> dict[str, str | int | float | bool]:
    """Drop high-risk keys and normalize values before they enter telemetry."""

    cleaned: dict[str, str | int | float | bool] = {}
    for raw_key, value in (values or {}).items():
        key = str(raw_key).strip()
        folded = key.casefold()
        if not key or any(part in folded for part in _SENSITIVE_KEY_PARTS):
            continue
        if isinstance(value, bool | int | float):
            cleaned[key] = value
        elif value is not None:
            cleaned[key] = _label(value)
    return cleaned


@contextmanager
def operation_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """Create a privacy-filtered span without recording exception messages."""

    with _TRACER.start_as_current_span(name, attributes=safe_attributes(attributes)) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        else:
            span.set_status(Status(StatusCode.OK))


def record_agent_run(*, agent_id: str, kind: str, outcome: str, duration_seconds: float) -> None:
    attrs = safe_attributes(
        {"agent.id": agent_id, "agent.run.kind": kind, "agent.run.outcome": outcome}
    )
    _RUNS.add(1, attrs)
    _RUN_DURATION.record(max(0.0, duration_seconds), attrs)


def record_intent_decision(*, intent: str, source: str) -> None:
    _INTENTS.add(1, safe_attributes({"agent.intent": intent, "agent.intent.source": source}))


def record_tool_call(
    *,
    phase: str,
    tool: str,
    outcome: str,
    attempts: int,
    duration_seconds: float,
) -> None:
    attrs = safe_attributes(
        {
            "agent.phase": phase,
            "agent.tool.name": tool,
            "agent.tool.outcome": outcome,
            "agent.tool.attempts": max(1, attempts),
        }
    )
    _TOOLS.add(1, attrs)
    _TOOL_DURATION.record(max(0.0, duration_seconds), attrs)


def record_pipeline_step(
    *, step: str, outcome: str, attempt: int, duration_seconds: float
) -> None:
    attrs = safe_attributes(
        {
            "workflow.step": step,
            "workflow.step.outcome": outcome,
            "workflow.step.attempt": max(1, attempt),
        }
    )
    _PIPELINE_STEPS.add(1, attrs)
    _PIPELINE_DURATION.record(max(0.0, duration_seconds), attrs)


def record_release_gate(*, decision: str, blocker_count: int) -> None:
    _RELEASE_GATES.add(
        1,
        safe_attributes(
            {
                "agent.release_gate.decision": decision,
                "agent.release_gate.blocker_count": max(0, blocker_count),
            }
        ),
    )
