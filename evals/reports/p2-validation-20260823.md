# P2 live Agent evaluation evidence

> Scope: live HTTP/SSE Agent behavior and local OpenTelemetry delivery. This
> evidence is not a manufacturing approval and does not replace KiCad EDA E2E
> validation.

## Reproducibility

- Source commit: `e1745d5949f29bc9819060204cf6791029e0ced6`
- Fixed plan: `p2-live-agent-eval-v1`
- Plan digest: `12843b45afce7ce1a7217a60fcb60debc715d2ae97afaa18ea948e63ef013136`
- Runtime image: existing `ratsnestpro-agent-runtime:local`
  (`sha256:a7dd5f36f02d70d8c97c5686f68d4382e0cf68c78cea8eeb93384069e9dc46f1`)
- Execution mode: the current `src` tree was mounted read-only into the existing
  Compose service; the recorded evaluation did not build an application image.
- Existing PostgreSQL, Redis, and Temporal volumes were reused.
- The report has `sourceDirty: false` and stores no prompts, model responses,
  reasoning, credentials, user identities, or local artifact paths.

## Fixed 30-case result

| Category | Passed | Total | Mean duration |
|---|---:|---:|---:|
| Intent routing | 4 | 5 | 15.5 s |
| RAG grounding | 5 | 5 | 109.2 s |
| Tool orchestration | 5 | 5 | 2.1 s |
| Release gate | 5 | 5 | 0.2 s |
| Recovery and idempotency | 5 | 5 | 24.9 s |
| Prompt injection | 5 | 5 | 15.5 s |
| **Overall** | **29** | **30** | **27.9 s** |

- End-to-end pass rate: `96.7%`
- Intent accuracy: `96.7%`
- Tool-contract accuracy: `100%`
- Release-gate accuracy: `100%`
- False releases: `0`
- HTTP completion: `30/30` returned status `200`
- Total live wall-clock time: `836.397 s`
- Total model tokens reported by the runtime: `126,636`

The only failed check was `intent.build-clarification`: the system safely
returned the effective intent `clarify` for an underspecified build request,
while the plan expected the original request class `build`. Terminal behavior,
forbidden phases, tool contract, and release gate all passed. The result remains
failed in the committed report instead of being relabelled after execution.

## Observability evidence

Before the Collector recovery test, Prometheus exposed six Agent metric families
covering 32 live runs, 78 tool calls, and eight release-gate decisions. Collector
logs contained the privacy-safe spans:

- `agent.run`
- `agent.intent.route`
- `agent.tool.call`
- `agent.release_gate.evaluate`

The Collector was restarted without rebuilding any image. A subsequent live
case passed, `agent.run` and `agent.intent.route` spans were received again, and
the cumulative Agent run metric advanced to 33. This verifies exporter recovery
for a running Agent service.

## Restart and idempotency evidence

A fixed synthetic request was completed, the existing `agent_service` container
was restarted, and the exact same request ID and payload were submitted again.
Both responses returned HTTP 200 and their complete SSE payloads had the same
SHA-256 digest:

`31cee35dc406572d81a77a102c0a6b806f5af461a3b0a8ccbbadfba6c6fec801`

This verifies Redis-backed response replay across an Agent process restart. It
does not prove recovery of an in-flight Temporal Activity.

## Remaining gap

No case in this 30-case Agent-level plan dispatched the full 17-step EDA build,
so `agent.pipeline.step` was not emitted and KiCad artifacts were not graded in
this run. The historical five-case E2E result remains `0/5 release_ready`; a new
five-case EDA run is required before claiming improved artifact-generation or
manufacturing-readiness results.
