# CircuitFoundry control plane

The control plane is a Java 21 Spring Boot service. It owns SaaS identity and business data; it does not run LangGraph or KiCad.

## Required production configuration

- `RATSNEST_OIDC_ISSUER_URI`, `RATSNEST_OIDC_JWK_SET_URI`, `RATSNEST_OIDC_AUDIENCE`
- `RATSNEST_DB_URL`, `RATSNEST_DB_USER`, `RATSNEST_DB_PASSWORD`
- `RATSNEST_AGENT_RUNTIME_URL`, an internal HTTP(S) base URL without credentials or query parameters
- `RATSNEST_INTERNAL_SIGNING_SECRET`, the same dedicated secret configured on the Python runtime and at least 32 bytes long

HTTP remains the default compatibility transport. To opt into the versioned
gRPC run boundary, set `RATSNEST_AGENT_RUNTIME_TRANSPORT=grpc` and
`RATSNEST_AGENT_RUNTIME_GRPC_TARGET`. TLS is the default; plaintext must be
enabled explicitly with `RATSNEST_AGENT_RUNTIME_GRPC_PLAINTEXT=true` on a
trusted development network. Metadata and history continue over signed HTTP in
protocol v1.

Terminal gRPC status also carries the canonical artifact manifest and delivery
classification, so reconciliation does not depend on an active event subscriber.

The Flyway login must own the `control_plane` schema. Migrations run in a separate deployment job; the long-running application has Flyway disabled and must not receive migration credentials. The runtime login must be the pre-provisioned PostgreSQL role `ratsnest_app` with `NOSUPERUSER NOBYPASSRLS`; it must not own the schema, business tables, or migrator role. Migration V1 grants that role only the privileges required by the application.

The `dev` Spring profile points at an explicit loopback OIDC issuer. It still requires signed JWTs and does not bypass authentication.

## Control-plane boundary

The browser and Next.js BFF call Java only. Java authorizes the OIDC principal, verifies `X-Organization-ID` membership, owns project/run state and idempotency, and then invokes the fixed `ratsnestpro-multi-agent` Python runtime. It never accepts a browser `user_id` as an execution identity.

The public run flow is:

1. `POST /api/v1/projects/{projectId}/runs` with `Idempotency-Key` creates or returns a run.
2. `GET /api/v1/runs/{runId}/events` streams events; reconnects send `Last-Event-ID`.
3. `GET /api/v1/runs/{runId}` reads status, and `POST /api/v1/runs/{runId}:cancel` explicitly cancels it.
4. Thread history and model metadata are available at `/api/v1/projects/{projectId}/threads/{threadId}/messages` and `/api/v1/projects/{projectId}/runtime-info`.

Disconnecting an SSE client cancels only that subscription. Python continues the idempotent background run, so a later subscription can replay events after `Last-Event-ID`.

## Durable run events and recovery

Migration V4 adds a transactional run outbox, and V6 hardens per-run sequence
allocation and publication leasing. `RATSNEST_RUN_OUTBOX_ENABLED`
publishes lifecycle events plus complete message/error/terminal events to Kafka;
token, reasoning and heartbeat chunks are never published. Delivery is
at-least-once, keyed by `runId`, and consumers deduplicate with `eventId` or
`runId + sourceEventSeq`.

`RATSNEST_RUN_RECONCILIATION_ENABLED` enables a bounded cross-tenant worker for
`QUEUED` and `RUNNING` rows. It reuses the control-plane run UUID as the
idempotent runtime request ID, so a timeout or process restart cannot create a
second task. Both features default to `false` for a safe rolling migration; run
through V6 before enabling either one.

V4 also snapshots the opaque runtime principal for new runs so internal signing-key
rotation does not change execution ownership. Before the first post-V4 key rotation,
allow pre-V4 active runs with a null snapshot to finish or migrate them explicitly.

## Agent Runtime authentication

The Python adapter is private. Java sends snake-case DTOs only to `/internal/v1/**` and signs every request with a 90-second HS256 bearer token. The token binds its issuer, audience, opaque principal, tenant, project, run, HTTP method, exact path, and SHA-256 body digest. Python derives its execution owner from the verified subject and rejects a `user_id` field in private request bodies.

Use a dedicated internal signing secret; do not reuse an OIDC client secret, browser session key, or legacy FastAPI bearer credential. Network policy must prevent browsers and untrusted workloads from reaching the Python internal port.

The versioned JSON schemas and the complete endpoint table are in [`../contracts`](../contracts/README.md).

## Migration rollback

Production schema changes use forward-compatible expand/contract migrations and application rollback. Flyway Community does not execute undo migrations. The destructive V1 rollback fixture exists only under test resources and requires both a `_rollback` database name and an explicit confirmation variable.
