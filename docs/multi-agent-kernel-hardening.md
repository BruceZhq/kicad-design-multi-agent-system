# RatsNestPro multi-agent kernel hardening

This note records the failure modes found during the Increment 8 audit and the
invariants implemented in the runtime. It is intentionally board-agnostic.

## Implemented invariants

| Area | Failure mode | Implemented invariant |
| --- | --- | --- |
| Intent | Weak words such as `Java`, `design`, or an explicit UI mode could start the hardware graph for an unrelated request. | Deterministic domain evidence gates every explicit and inferred mode. Ambiguous requests are clarified; high-confidence unsupported requests do not spend a routing-model call. |
| Retry | Any tool `error` could be retried, including deterministic validation failures. | Only classified transient I/O, timeout, rate-limit, and empty recoverable results are retried within a bounded count. |
| Call lifetime | A model or evidence tool could occupy a LangGraph node indefinitely. | Every non-Temporal model/tool call has a per-call deadline. Hardware execution retains its separate Temporal workflow deadline and AHE budget. |
| Redis ownership | Loss of the Redis renewal path could leave a local producer running after its lease was no longer authoritative. | Renewal errors cancel the local owner task (fail closed). A fencing token remains required for state mutation. |
| Recovery | Java reconciliation observed a non-terminal run but did not cause an expired Python lease to be taken over. | Reconciliation submits the same idempotent `StartRun`; a live lease attaches, while an expired lease is acquired without creating a second logical run. |
| Tenant isolation | Verified Java tenant/project/principal claims were lost when Python constructed its graph input. | Internal adapters bind claims to a private, non-serializable request attribute. Checkpoint keys, locks, Redis ownership, workspace scope, and audit metadata use a three-domain opaque `rt1` scope. Public JSON cannot set it. |
| Temporal reattach | `WorkflowAlreadyStarted` attached solely by workflow ID. | The workflow exposes a digest of immutable business input. Reattach succeeds only when the digest matches; missing legacy identity or mismatched input fails closed. |
| Graph state | Full Hardware results accumulated in `hardware_attempts`, increasing every checkpoint. | The current complete result stays in `hardware`; only the two most recent compact attempt summaries remain in history and attempt numbering stays monotonic. |
| EHE trust | Unreviewed or tenant-controlled observations could influence future strategy selection. | EHE persists opaque fingerprints, orders by event time, and only scores observations belonging to releases with both independent-review and release-ready attestations. EHE never edits source at runtime. |

## Concurrency model

The main LangGraph path is deliberately serial. Bounded specialist calls may run
concurrently, but their results are joined once before the next state write.
Durable Hardware Engineer work is owned by one Temporal workflow and its
activities. Redis leases and fencing protect live run production; PostgreSQL and
Kafka retain authoritative business and audit events. If future graph nodes write
the same field in parallel, they must first define an associative reducer and a
single deterministic join node.

## Deployment and migration boundary

The `rt1` identity scope intentionally does not fall back to pre-upgrade internal
checkpoint or Redis keys. Before deploying this change, either drain all active
internal runs or execute a separately reviewed one-time migration that derives the
scope from authoritative Java records. Never guess tenant scope from legacy
client-provided `user_id` values.

Use `scripts/audit_runtime_identity_migration.ps1 -RequireDrained` immediately
before rollout. It checks authoritative Java run state, Redis owners, and scoped
versus unmapped checkpoint threads without printing credentials. Unmapped legacy
checkpoints are retained and isolated; deleting them is a separate destructive
retention decision, not part of the rollout.

The development OIDC stack is suitable only for local testing. Production remains
blocked until real cluster evidence proves Metrics API/HPA behavior, authenticated
telemetry TLS plus restart recovery, and cross-region failover/failback within the
declared RPO/RTO.

## LLM output bridge

Temporal provider-visible LLM output now uses a bounded, per-workflow Redis Stream
as its primary live replay channel. A Lua append operation deduplicates stable
`record_id` values atomically; the stream and its deduplication indexes have explicit
length limits and TTLs. The cursor is checkpointed with the workflow reference, so a
restarted waiter resumes from the last Redis Stream ID instead of replaying the full
transcript.

The per-workflow JSONL transcript remains a complete audit copy and a fail-open
transport fallback: a short Redis timeout never fails the Hardware workflow. Kafka
still receives only complete messages, milestones, audit, and lifecycle events; token
chunks are not written to Kafka or Temporal Event History. A future Cell may remove
the shared JSONL dependency only after an object-store or other durable audit sink has
been proven under restart and retention tests.
