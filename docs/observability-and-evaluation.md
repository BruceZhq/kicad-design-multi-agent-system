# Agent observability and deterministic evaluation

This project separates online observability from offline quality evaluation. A
trace explains what one run did; a content-addressed evaluation report decides
whether recorded evidence satisfies a fixed contract. Neither is treated as a
manufacturing approval.

## Online observability

The opt-in Kubernetes overlay injects OpenTelemetry into Next.js, Java, and
Python services. Application-level spans add the Agent semantics that generic
HTTP/gRPC auto-instrumentation cannot infer:

```text
HTTP or gRPC server span
└── agent.run
    ├── agent.intent.route
    ├── agent.tool.call
    ├── agent.pipeline.step
    └── agent.release_gate.evaluate
```

The Temporal client and workers share the SDK `TracingInterceptor`, which carries
W3C trace context through workflow and Activity headers. Pipeline-step spans can
therefore remain children of the originating Agent trace across process restarts;
no prompt, project path, or governance token is placed in those headers.

The Python runtime emits these low-cardinality metrics:

| OpenTelemetry metric | Purpose |
|---|---|
| `ratsnest.agent.runs` | Run count by invoke/stream kind and bounded outcome |
| `ratsnest.agent.run.duration` | End-to-end Agent duration |
| `ratsnest.agent.intent.decisions` | Intent distribution |
| `ratsnest.agent.tool.calls` | Tool calls by stable tool name and outcome |
| `ratsnest.agent.tool.duration` | Logical tool latency including bounded retries |
| `ratsnest.agent.pipeline.steps` | Temporal step attempts and outcomes |
| `ratsnest.agent.pipeline.step.duration` | Per-step execution duration |
| `ratsnest.agent.release_gate.decisions` | Passed, blocked, or errored review gates |

`src/observability/agent.py` drops keys associated with prompts, request bodies,
users, tenants, projects, credentials, paths, SQL, and tokens. Metrics never
carry run IDs or other high-cardinality identities. The Collector performs a
second redaction pass before export.

Deploy the existing observability composition, then open the sidecar-provisioned
Grafana dashboard `KiCad Design Multi-Agent System - Agent Overview`:

```bash
kubectl kustomize deploy/k8s/cells/primary-region-observability
```

The dashboard JSON is a checked-in query contract, not proof that a cluster has
emitted data. Release evidence still requires a real trace, metric discovery,
collector restart recovery, and backend reachability in the target environment.

For local verification, reuse the existing Compose project and add the opt-in
Collector profile:

```bash
RATSNEST_OBSERVABILITY_ENABLED=true docker compose \
  --profile control-plane --profile identity --profile artifact-store \
  --profile observability up -d --build
```

The Collector exposes Prometheus metrics only on `127.0.0.1:9464`; traces remain
in the Collector debug exporter so a local run does not require a second backend
or a new persistent volume.

## Live HTTP/SSE evaluation

The versioned plan in `evals/live/cases.v1.json` contains 30 synthetic cases in
six categories: intent routing, RAG grounding, tool orchestration, release gates,
recovery/idempotency, and prompt injection. It calls the deployed Agent rather
than replaying fixtures:

```bash
PYTHONPATH=src uv run --frozen python -m evolution.live_runner \
  --output evals/reports/live-agent-eval.json \
  --min-pass-rate 0.85 \
  --max-false-release-count 0
```

Run one case during diagnosis with `--case intent.unsupported`. The JSON and
Markdown reports contain only bounded facts such as intent, phase and tool names,
terminal state, duration, token count, artifact name/hash/validity, and a digest
of sanitized events. They do not retain prompts, model responses, reasoning,
credentials, user identities, or local paths. Each report binds the plan digest
and Git commit; `sourceDirty: true` means it is diagnostic rather than release
evidence.

## Offline deterministic evaluation

Each evaluation suite hashes its case manifests. Each manifest now also hashes
its sanitized run-evidence file, so changing an observed outcome invalidates the
evaluation identity. The runner validates all three levels before grading:

```text
suite digest -> manifest digest -> evidence digest
```

Run the public recorded gate:

```bash
PYTHONPATH=src uv run --frozen python -m evolution.runner \
  --suite evals/suites/holdout.v1.json \
  --suite evals/suites/adversarial.v1.json \
  --json evals/reports/public-recorded-eval.json \
  --markdown evals/reports/public-recorded-eval.md \
  --min-pass-rate 1.0 \
  --max-false-release-count 0
```

The report exposes:

- case and per-grader pass rates;
- Tool Call Accuracy based on required-tool presence and forbidden-tool absence;
- State Transition Accuracy from the deterministic trajectory grader;
- artifact completion backed by existence, validity, and SHA-256 evidence;
- release-gate and recovery accuracy;
- False Release count, which is a hard zero-tolerance CI gate.

GitHub Actions regenerates the JSON and Markdown reports and uploads both as the
`agent-evaluation-report` artifact. A prompt-injection or release-truth regression
therefore fails the Python job rather than becoming a README-only claim.

## Evidence scope

The committed public recorded report replays three sanitized holdout/adversarial cases.
It proves the evaluator, content-addressing, tool contract, and fail-closed gate
against those fixtures. It does not prove live LLM quality, latency, Kubernetes
availability, KiCad correctness, or manufacturability. The separate five-case E2E
audit remains the source for actual KiCad artifact-generation results and retains
its `0/5 release_ready` conclusion.
