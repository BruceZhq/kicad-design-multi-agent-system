# Run module behavior-preservation matrix

| Capability | Before | After | Preserved evidence |
|---|---|---|---|
| Root submission | `RunService.start` | `RunSubmissionService.start` | Same profile lookup, harness routing, request fingerprint, idempotency conflict and Runtime dispatch branches |
| Revision | `RunService.revise` | `RunSubmissionService.revise` | Same terminal/latest-parent gates, revision numbering, inherited runtime snapshot and lifecycle event |
| Fork/replay | `RunService.fork` | `RunSubmissionService.fork` | Same revision-chain validation, source digest, replay mode, message-size gate and new root/thread semantics |
| Run query | `RunService.get/authorizeRead` | `RunQueryService.get/authorizeRead` | Same tenant/project authorization and terminal manifest synchronization |
| Runtime status | `RunService.runtimeStatus` | `RunQueryService.runtimeStatus` | Same lease/recoverability projection and durable `ui_snapshot` parsing |
| History/conversations | `RunService.history/conversations/removeConversation/info` | `RunQueryService` | Same Runtime identity, per-principal soft deletion, active-conversation gate and limit |
| HITL response | `RunService.respond` | `RunInteractionService.respond` | Same option validation, state-version fencing, idempotent `RESPONDING` replay and durable `RESPONDED` transition |
| Recovery | `RunService.recover` | `RunInteractionService.recover` | Same terminal/waiting/active/recoverable gates and stable Java run ID takeover |
| Cancel | `RunService.cancel` | `RunLifecycleService.cancel` | Same queued reconciliation pre-start and Runtime cancel command |
| Event stream | `RunService.events` | `RunLifecycleService.events` | Same browser cursor and operational persistence-before-forwarding; each observed event writes only a monotonic backlog hint while the governance cursor remains independent |
| Artifact delivery | private lifecycle methods | `RunLifecycleService` | Same manifest parse/persist, immutable delivery status and delivery outbox event |
| Governed evolution telemetry | browser-triggered private persistence | `RunEventIngestionService` + V15 worker/cursor | Hardened: collection no longer depends on a browser; failures leave the durable cursor retryable |
| Runtime event ingestion | none | `RunEventIngestionService`, `RunEventIngestionStore`, `RunEventIngestionWorker` | New control-plane-owned high-water drain, lease and CAS; browser-first HITL wakes a WAITING backlog without idle polling; explicit replay gaps fail closed |
| Transactional outbox | concrete repository in flat package | `RunOutbox` port + `JdbcRunOutbox` + messaging adapter | SQL digest unchanged; `MANDATORY` transaction propagation retained |
| Reconciliation | worker calling monolith | scheduling adapter calling `RunLifecycleService.reconcile` | Claim/release SQL and bounded retry/time-budget behavior retained |

Verification summary:

- All 13 HTTP mappings have the same normalized SHA-256 digest before and after.
- All SQL-bearing lines in the run, interaction and outbox stores have identical normalized digests.
- 136 production sources plus 20 generated protobuf sources compiled with Java 21.
- All 8 test sources compiled; 12 targeted behavior tests executed and passed.
- Flyway V1-V13, endpoint, header, DTO and existing persistence SQL contracts are unchanged. Additive V14/V15 migrations harden Evolution attribution and create the independent ingestion cursor.
