# CircuitFoundry contracts

Contracts are versioned independently from their transport adapters.

- `public/v1/problem-detail.schema.json` defines the Java control-plane error envelope.
- `public/v1/organization-project-api.schema.json` defines organization, membership, and project DTOs.
- `public/v1/user-profile-api.schema.json` defines principal-scoped profile DTOs.
- `public/v1/run-api.schema.json` defines browser-facing run, event, history, and runtime-info DTOs.
- `agent-runtime/v1/agent-runtime.schema.json` defines both the Java `AgentRuntimeGateway` records and the private Python REST/SSE wire format.
- `agent-runtime/v1/agent_runtime.proto` is the canonical v1 contract for durable run start, status, control, and event subscription over internal gRPC.

## Public control-plane API

All `/api/v1/**` requests require an OIDC access token accepted by Java. Except for organization creation and organization discovery, tenant-scoped requests also require `X-Organization-ID`; Java treats that value only as a candidate and verifies the JWT principal's database membership before activating the PostgreSQL tenant context.

Organization and project routes are:

| Method | Path | Required tenant header |
| --- | --- | --- |
| `POST` | `/api/v1/organizations` | No |
| `GET` | `/api/v1/organizations` | No |
| `GET` | `/api/v1/organizations/current` | Yes |
| `GET`, `POST` | `/api/v1/projects` | Yes |
| `GET`, `PUT` | `/api/v1/projects/{projectId}` | Yes |

The authenticated principal's profile is independent of organization membership. Its
immutable identity is the token's `(issuer, subject)` pair; neither value is accepted
from browser input. Profile routes therefore do not require `X-Organization-ID`:

| Method | Path | Result |
| --- | --- | --- |
| `GET`, `PUT` | `/api/v1/me/profile` | Read or update display name, title, biography, locale, and time zone with an optimistic `version`. |
| `GET` | `/api/v1/me/profile/avatar` | Return the current avatar bytes after object-store integrity verification. |
| `PUT` | `/api/v1/me/profile/avatar` | Accept multipart `file` plus `version`; JPEG, PNG, or WebP only, at most 2 MiB. |

Avatar bytes are content-addressed in private S3-compatible storage. PostgreSQL stores
only the object key, media type, SHA-256 digest, byte count, and profile version. The
public JSON contract is `public/v1/user-profile-api.schema.json` and does not expose the
object key or OIDC subject.

Run routes are:

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/v1/projects/{projectId}/runs` | Requires `Idempotency-Key`; returns `202` and a `Location` header. |
| `GET` | `/api/v1/runs/{runId}` | Returns the Java-owned run state. |
| `GET` | `/api/v1/runs/{runId}/events` | Returns browser-facing SSE. |
| `POST` | `/api/v1/runs/{runId}:cancel` | The only browser operation that cancels execution. |
| `POST` | `/api/v1/runs/{runId}/revisions` | Requires `Idempotency-Key`; creates a new run revision from human feedback without mutating the source run. |
| `GET` | `/api/v1/runs/{runId}/artifacts` | Returns the authorized artifact manifest for the selected revision. |
| `GET` | `/api/v1/artifacts/{artifactId}:download` | Redirects to a short-lived authorized object-store download URL. |
| `GET` | `/api/v1/projects/{projectId}/threads/{threadId}/messages` | Returns LangGraph thread history through Java. |
| `GET` | `/api/v1/projects/{projectId}/runtime-info` | Returns the available agent and model metadata through Java. |

Public JSON uses camel case. A start request contains `message` and `capabilityProfile`, and may contain `model`, `threadId`, and up to eight `teamMembers`. The server creates `threadId` when it is omitted and returns it in `RunResponse`. Browser input never contains `tenantId`, `userId`, or a Python Agent Runtime credential.

Every `RunResponse` identifies its immutable revision with `rootRunId`, nullable
`parentRunId`, and one-based `revisionNumber`. `deliveryStatus` is null until the
runtime has enough evidence to classify the delivery, then is exactly one of
`execution_blocked`, `delivered_with_issues`, or `release_ready`. Execution state
and delivery status are independent: a completed execution can still deliver with
issues, while an execution-level failure is blocked.

Artifact manifests expose only opaque artifact IDs, file metadata, size, and a
SHA-256 digest. They never expose the object-store key, credentials, or a permanent
URL. The download operation authorizes the current tenant and run before issuing a
short-lived redirect. A missing or invalid manifest is not interpreted as a ready
deliverable.

Every new run also contains a required `capabilityProfile` selector with only `id` and `version`. The browser never supplies a digest. Java resolves the selector against Python's signed Runtime metadata, persists the returned SHA-256 digest with the Run, and forwards the resulting snapshot as `config.capability_profile`. Historical runs created before v3 may expose a null snapshot; they are never relabeled as one of the five production profiles.

`Idempotency-Key` is scoped to an organization and project. Reusing it with the same normalized request returns the existing run; reusing it for different input returns `409`.

## SSE replay

The browser sends the last successfully processed sequence in the standard `Last-Event-ID` request header. A missing header means `0`. Java passes that cursor to Python as `last_event_id`; Python replays buffered events whose IDs are greater than the cursor. Every data event, including the terminal `[DONE]` sentinel on the private stream, has an SSE `id`. Heartbeats are comments and do not consume an event ID.

Java maps the private stream to public `RunEvent` values named `message`, `token`, `reasoning`, `error`, `completed`, `failed`, `cancelled`, or `timed_out`. `data.message` contains the complete message DTO; token and provider-supplied reasoning events use `data.content`; failures use `data.error` and may include `code` and `retryable`. Java does not fabricate hidden reasoning.

Closing a browser or Java SSE subscription closes only that subscription. The Python run registry owns the background producer, so execution continues until it reaches a terminal state, times out, or receives the explicit cancel operation.

## Private Agent Runtime boundary

`AgentRuntimeGateway` uses a `RuntimeIdentity(principalId, tenantId, projectId)`. `EventSubscription` carries the complete `StartRunCommand` plus `lastEventId`, allowing a reconnect to submit the identical idempotent command with only its replay cursor changed.
The control plane snapshots the opaque `principalId` when a run is created and reuses
it for status, event, and control calls. This keeps ownership stable when the internal
JWT signing secret rotates. Runs created before the snapshot column fall back to a
deterministic value derived from their existing creator fields. Another authorized
project member may use the Java API, but Java does not replace the runtime owner with
that viewer's identity. Project-scoped info and history calls continue to use the
current authenticated actor.

The fixed private endpoints are:

| Method | Path | Body/result |
| --- | --- | --- |
| `GET` | `/internal/v1/info` | Python `ServiceMetadata` in snake case. |
| `POST` | `/internal/v1/runs/ratsnestpro-multi-agent/stream` | `internalStreamRequest`; starts or resumes and subscribes to SSE. |
| `GET` | `/internal/v1/runs/{request_id}` | Python `RunStatus` in snake case. |
| `DELETE` | `/internal/v1/runs/{request_id}` | Explicitly cancels that run. |
| `POST` | `/internal/v1/history` | `internalHistoryRequest`; returns snake-case messages. |

The gRPC migration keeps the same `RuntimeIdentity` and request binding. Its v1
service exposes `StartRun`, `GetRun`, `ControlRun`, and `SubscribeRunEvents`.
Runtime metadata and thread history remain on the signed compatibility HTTP API
until those contracts are promoted separately. HTTP remains the default transport;
gRPC must be enabled explicitly so deployment can be rolled back without changing
browser or public API contracts.

`request_id` is also the durable execution key. Python derives the Temporal Hardware
Engineer workflow ID from it, so a Java retry after an uncertain response attaches to
the existing workflow instead of creating a second design job. `event_seq` is strictly
monotonic per run and is the replay cursor shared by gRPC and browser SSE.

The gRPC `Run.result_json` field carries canonical `runtimeResult` JSON once
available. Polling `GetRun` therefore preserves `artifact_manifest` and
`delivery_status` even when no event subscriber was connected at completion.

Private request JSON uses snake case. `user_id` is deliberately absent and rejected by Python's `extra="forbid"` models. After authentication, Python injects the signed token subject as its internal `user_id`; browser-provided identity therefore cannot reach LangGraph or the run registry.

Every private request carries `Authorization: Bearer <token>`. Java signs a 90-second HS256 JWT with a separate secret of at least 32 bytes. Its claims bind the credential to `iss`, `aud`, opaque `sub`, `tenantId`, `projectId`, `runId`, uppercase HTTP `method`, exact request `path`, SHA-256 of the exact body bytes, `iat`, and `exp`; Java also emits version and `jti` claims. Python verifies the signature with constant-time comparison, enforces issuer/audience, request binding, clock skew, and maximum lifetime, and checks the run claim wherever the endpoint identifies a run. The private service must remain reachable only on the internal network.
