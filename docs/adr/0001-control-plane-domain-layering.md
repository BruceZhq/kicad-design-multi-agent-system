# ADR-0001: Business-capability layering for the Java control plane

- Status: Accepted
- Date: 2026-08-20

## Context

The control plane originally grouped controllers, JDBC repositories, records, runtime clients, and orchestration services in the same package. That made Spring dependencies implicit, allowed controllers to coordinate workflows directly, and made ownership difficult for multiple teams. The public HTTP and PostgreSQL contracts are already deployed, so restructuring must not change those contracts.

## Decision

Organize Java code by business capability, with four explicit layers inside each capability:

1. `api`: Spring MVC endpoints and transport DTOs. It may depend on `application` and identity/access services.
2. `application`: use cases, transactions, authorization coordination, and cross-port orchestration. It may depend on `domain` and another capability's documented application API.
3. `domain/model` and `domain/port`: business records plus inbound/outbound abstractions. Domain code must not depend on JDBC, HTTP clients, Kafka, S3, or Spring MVC.
4. `infrastructure`: Spring/JDBC/HTTP/gRPC implementations of domain ports.

Constructor injection is mandatory. Repository and remote-runtime types consumed by application services are interfaces in `domain/port`; concrete classes use descriptive adapter names such as `JdbcEvolutionRepository` and `HttpEvolutionTrialRuntime`.

Controllers may validate DTO syntax and obtain the authenticated actor, but workflow coordination belongs to application services. For example, `EvolutionTrialLauncher` now owns prepare/bind/start orchestration, and `EvolutionResultIngestionService` owns signed result-proof decoding and verification. Endpoint paths, header names, payload fields, SQL statements, and database migrations are unchanged.

## Reference capability layout

```text
evolution/
  api/                    EvolutionController, EvolutionAdminController,
                          EvolutionResultController
  application/            EvolutionQueryService, EvolutionCandidateService,
                          EvolutionTrialService, EvolutionCollector,
                          EvolutionTrialLauncher,
                          EvolutionResultIngestionService
  domain/model/           EvolutionCandidate, EvolutionObservation,
                          EvolutionTrial
  domain/port/            EvolutionRepository, EvolutionTrialRuntime
  infrastructure/
    persistence/          JdbcEvolutionRepository
    gateway/              HttpEvolutionTrialRuntime

harness/
  api/                    HarnessVersionController,
                          HarnessReleaseAdminController
  application/            HarnessVersionService, HarnessReleaseRouter
  domain/model/           HarnessVersion, HarnessRollout
  domain/port/            HarnessVersionRepository,
                          HarnessRolloutRepository
  infrastructure/
    persistence/          JdbcHarnessVersionRepository,
                          JdbcHarnessRolloutRepository
```

## Compatibility constraints

- Do not rename routes, headers, JSON properties, error codes, database schemas, tables, columns, or Flyway migrations during package migration.
- Do not create compatibility copies of moved classes; one Spring bean must implement each responsibility.
- Do not let infrastructure types leak into controller constructor signatures.
- Move tests with the layer they exercise and mock domain ports rather than JDBC adapters.
- A capability migration is complete only when its old flat package contains no Java files and compile/reference checks pass.

## Remaining migration backlog

Migrate one capability at a time after evolution and harness:

1. `run`: split its large controller/service into submission, interaction, lifecycle, event-stream, reconciliation, and outbox use cases; expose Agent Runtime and outbox persistence as ports.
2. `identity` and `tenancy`: separate OIDC/Spring Security adapters from membership and tenant-policy domain services.
3. `artifact`: separate manifest/domain policy from JDBC and object-storage adapters.
4. `organization`, `project`, and `profile`: move MVC DTOs out of services and place repositories behind domain ports.
5. `agentgateway`: retain HTTP/gRPC implementations as infrastructure adapters behind a stable runtime port.
6. `bootstrap` and `shared/web`: keep only composition-root and cross-cutting transport concerns; do not place business logic there.

These migrations are deliberately not bundled into this ADR's first implementation so that API and persistence behavior can be verified capability by capability.

## Consequences

The package tree is larger but ownership and dependency direction are explicit. Business services can be tested against ports without JDBC or runtime clients. Adapter replacement no longer changes controller or domain code. Java package names are internal and changed; public network and database contracts did not.
