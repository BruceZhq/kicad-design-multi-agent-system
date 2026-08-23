"""Opt-in OpenTelemetry SDK bootstrap for the local Docker runtime."""

from __future__ import annotations

import os


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _signal_endpoint(base: str, signal: str) -> str:
    value = base.rstrip("/")
    suffix = f"/v1/{signal}"
    return value if value.endswith(suffix) else value + suffix


def configure_local_sdk() -> bool:
    """Install an OTLP trace/metric provider only when explicitly enabled.

    Kubernetes continues to rely on Operator auto-instrumentation. The local
    Compose profile sets the explicit flag so it can export the same semantic
    spans without installing another observability environment.
    """

    if not _enabled(os.getenv("RATSNEST_OBSERVABILITY_ENABLED")):
        return False

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    base = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "ratsnest-agent-service"),
            "service.namespace": "ratsnest",
            "deployment.environment": os.getenv("MODE", "development"),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    trace_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        _signal_endpoint(base, "traces"),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_endpoint))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        _signal_endpoint(base, "metrics"),
    )
    interval_ms = max(1_000, int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000")))
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=metric_endpoint),
        export_interval_millis=interval_ms,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
    return True
