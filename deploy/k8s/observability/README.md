# Observability overlay

This opt-in overlay closes the application telemetry path without bundling stateful
monitoring backends into each RatsNest Cell:

`Next.js / Java / Python -> OTLP collector -> Tempo + Loki`, while Prometheus
scrapes the collector. Grafana data sources stay under the monitoring platform's
Secret management because datasource credentials must not be placed in a ConfigMap.

Prerequisites are a pinned OpenTelemetry Operator, Prometheus Operator CRDs, a
Grafana datasource sidecar, and production Tempo/Loki services in the
`observability` namespace. The checked-in exporter endpoints are HTTPS and can be
overridden in `ratsnest-observability-exporters`. Create the Secret
`ratsnest-observability-exporter-auth` with `TEMPO_AUTHORIZATION`,
`LOKI_AUTHORIZATION`, and `LOKI_TENANT_ID`; no credentials are stored here. Backend
certificates must chain to the collector image's trust store. Use a cluster-managed
trust-bundle injection when a private CA is required. The application-to-collector
hop still requires service-mesh mTLS (or an equivalent authenticated transport)
before production. Label the Prometheus namespace `monitoring`; the checked-in
NetworkPolicy only admits scrapes from that namespace.

The collector is a three-replica StatefulSet. Each replica has its own 2 GiB PVC,
and both remote exporters use bounded, file-backed queues. This survives an ordinary
pod restart, but it is not a telemetry archive: a lost PVC or full queue can still
lose data. Remote retries do not expire, so alert on collector queue/retry/drop
metrics and validate a backend-outage/restart exercise before release.

The operator injects server-side Java, Python and Node.js instrumentation; browser
RUM is deliberately not enabled because a public ingestion endpoint needs a separate
abuse and privacy design. W3C `traceparent`/`baggage` propagation and OTLP log export
provide trace/log correlation. Console logs also include `trace_id` and `span_id` for
Java and Python. The collector drops common credential, body, SQL and prompt fields.

LangSmith is disabled by default in production and its inputs, outputs and metadata
remain hidden if tracing is explicitly enabled later. Keep `LANGSMITH_API_KEY` only in
the cluster secret manager; never move it to a ConfigMap. This is a hard privacy
default, not a claim that arbitrary application log messages are automatically safe.

Render locally after installing a `kubectl`/Kustomize version that supports the base:

```bash
kubectl kustomize deploy/k8s/overlays/observability
kubectl kustomize deploy/k8s/cells/primary-region-observability
pwsh -File scripts/validate_increment8_static.ps1
```

The first command checks the observability overlay in isolation. The second is the
production composition: primary Cell capacity/network policy plus telemetry. Deploy
the composition, not both standalone overlays, to avoid duplicate base resources.

The two HPA names are also explicit contracts, not proof of metric availability.
The adapter derives `ratsnest_sse_active_connections` from the OpenTelemetry HTTP
active-request series for the Java SSE and Next.js chat-stream routes, preserving
pod and Cell labels. OAuth2 Proxy does not emit that route-scoped gauge and therefore
uses CPU scaling only. `ratsnest_temporal_hardware_task_queue_backlog` must retain its
`cell` and `task_queue` selectors. A custom/external metrics adapter is deliberately
not bundled. Prove discovery and real scale-up/scale-down behavior, then retain the
evidence files and run the script with `-RequireReleaseEvidence` and all three
evidence arguments.

Evidence files are JSON, not marker files. Each needs `schemaVersion: 1`, the
matching `kind` (`metrics`, `telemetry`, or `disaster-recovery`), `status: passed`,
a non-empty `environment`, an RFC 3339 `observedAt` no older than 90 days, and a
`checks` object. The validator requires metric discovery plus HPA scale-up/down;
TLS rejection/acceptance plus queue recovery after restart; and failover/failback
with measured RPO <= 60 seconds and RTO <= 1800 seconds respectively. An empty or
arbitrary file cannot satisfy the release gate.

Rendering does not validate installed CRD versions, collector component support,
backend reachability, TLS/authentication, trace continuity, Grafana sidecar labels,
metric emission, or adapter behavior; verify those in the target cluster before
release.
