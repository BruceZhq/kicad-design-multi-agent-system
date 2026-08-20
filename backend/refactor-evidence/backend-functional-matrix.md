# Java backend layering functional matrix

| Capability | API boundary | Application use case | Domain/port | Infrastructure | Preservation evidence |
|---|---|---|---|---|---|
| Run root/revision/fork submission | `run.api.RunController` | `RunSubmissionService` | `Run`, `RunStore`, `RunOutbox` | JDBC store + outbox | Existing route/DTO digests unchanged; root/revision/fork mapping retained |
| Run query/status/history | `run.api.RunController` | `RunQueryService` | `RunStore` | JDBC + Runtime gateway | Existing status, history and conversation endpoints retained |
| HITL response/recovery | `run.api.RunController` | `RunInteractionService` | `RunInteractionStore` | JDBC + Runtime gateway | Existing idempotency/state-version checks retained |
| Lifecycle/artifact/outbox/reconciliation | `run.api.RunController` | `RunLifecycleService` | run/artifact/outbox ports | JDBC, Kafka, scheduler | Existing SQL, source-event idempotency and manifest gates retained |
| Durable Runtime event ingestion | none; control-plane owned | `RunEventIngestionService` | `RunEventIngestionStore` | V15 cursor/lease, JDBC CAS, worker | Independent of browser SSE; browser events only publish a monotonic backlog hint; WAITING backlog drains without idle polling; failure leaves cursor retryable |
| Artifact manifest/download | `artifact.api.ArtifactController` | `ArtifactService`, `ArtifactManifestParser` | `ArtifactStore`, `ArtifactStorage` | JDBC + S3 adapter | `ArtifactStore.persist` now accepts tenant/run IDs; SQL and wire API unchanged |
| Identity/admin authorization | JWT mappers in `identity.api` | `PlatformAccess` | pure `AuthenticatedActor` | Spring Security/OIDC | issuer/subject checks, roles/scopes, error codes/text unchanged |
| Tenant membership/RLS context | `tenancy.api.MembershipController` | `MembershipService`, `TenantAccess` | `MembershipStore`, `TenantContext`, pure `MembershipRole` | JDBC/Postgres | Role HTTP error mapping moved out of domain; SQL/API unchanged |
| Organization/project | module `api` controllers | module services | models and stores | JDBC adapters | `X-Organization-ID` moved to shared `ApiHeaders`; all mappings unchanged |
| User profile/avatar | `profile.api.UserProfileController` + JWT mapper | `UserProfileService` | profile/avatar models and ports | JDBC + S3 | Size/signature/key/integrity behavior and errors retained |
| Agent Runtime transport | none | consumers depend on domain port | `AgentRuntimeGateway`, `RuntimeCredentials` | HTTP, conditional gRPC, signer | Large streaming protocol implementations only moved/rebound; no protocol rewrite |
| Governed Evolution recurrence | evolution API unchanged | `EvolutionCollector` | governance observation/repository | hardened JDBC + V14 | Trusted HDO + gap count at project 2; strict resolution; record replay idempotency; candidate identity is partitioned by Harness version even when manifests match |

## Cross-cutting invariants

- 57 route annotations and 39 controller wire records match the pre-refactor digests exactly.
- Ten unchanged persistence pairs retain identical normalized SQL literals; only governed Evolution and the new ingestion cursor add intentional SQL.
- Flyway V1 through V13 are byte-identical. V14 and V15 are additive migrations.
- Outbox publication, source-event idempotency, reconciliation leases, RLS activation, profile/avatar validation and OIDC error contracts remain present.
- `domain` contains no Spring, AWS, gRPC, servlet or web imports. `application`/`domain` contain no imports from `api` or `infrastructure`.
- Component names are unique. Every new domain port has one adapter, except the pre-existing selectable HTTP/gRPC Runtime transports.

## Verification

- Java 21 source compilation: 136 main + 20 generated protobuf + 8 test sources, all successful.
- Targeted behavior execution: 12/12 passed, covering browser-independent ingestion, browser-first HITL backlog wakeup/drain, replay-gap fail-closed behavior, legal filtered sequence holes, cursor retry after persistence failure, HITL persistence-before-forward, Evolution attribution/recurrence/idempotency/privacy gates, and cross-version candidate identity partitioning.
- No Docker, Maven, npm, Kubernetes or network execution was used.
