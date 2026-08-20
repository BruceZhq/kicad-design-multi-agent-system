# ADR-0002: Keep the control plane modular; extract only independent failure domains

- Status: Accepted
- Date: 2026-08-21

## Context

The Java control plane owns OIDC identity, tenant membership, projects, immutable Run/Revision lineage, artifacts, the transactional outbox, interaction CAS, Harness rollout and Evolution approval. These capabilities frequently share one PostgreSQL transaction and one tenant RLS context. The product also has naturally independent execution domains: the Python Agent Runtime, Temporal hardware workers, the Evolution evaluator, identity proxy and object storage.

Two organization styles were considered:

1. a conventional `controller -> service interface -> service/impl -> MyBatis mapper` monolith;
2. business-capability modules with `api -> application -> domain port -> infrastructure adapter`, plus independently deployed execution services.

The first style is familiar and compact for CRUD systems, but a single implementation interface adds ceremony, mapper DTOs tend to leak persistence into services, and MyBatis does not solve transaction ownership or remote-runtime boundaries. Splitting every capability into a microservice would add distributed transactions, service discovery, retries, tracing, schema compatibility and partial-failure handling without providing an independent scaling requirement for most control-plane domains.

## Decision

Keep the Java control plane as a modular monolith. Organize it by business capability, not by a global controller/service/mapper technical layer. Within each capability:

- `api` owns Spring MVC and wire DTOs;
- `application` owns use cases, transactions and authorization coordination;
- `domain/model` owns stable business values;
- `domain/port` defines persistence and remote-service contracts;
- `infrastructure` contains JDBC, S3, Kafka, HTTP and gRPC adapters.

Use Spring `JdbcClient` adapters behind domain ports instead of introducing MyBatis solely for package symmetry. A service interface is required only when there are multiple implementations, a stable plugin boundary, or a useful test seam; otherwise the application service class is the use-case boundary. HTTP and gRPC Runtime implementations remain adapters behind the same port.

Deploy separate services only where failure, scaling, security or runtime requirements are genuinely independent:

- Python Agent Runtime;
- Temporal hardware worker;
- trusted Evolution controller and isolated evaluator;
- OAuth2 Proxy/Keycloak;
- PostgreSQL, Redis, Kafka, Temporal and S3-compatible storage.

## Extraction rule

A Java capability may become a microservice only when all of the following are demonstrated:

1. it has an independently versioned API and data owner;
2. it needs independent scaling or isolation in production measurements;
3. cross-boundary transactions can be replaced by an explicit saga/outbox protocol;
4. idempotency, retry, observability, authentication and rollback are specified;
5. the migration reduces operational risk rather than only changing package names.

Artifacts and Evolution are the most plausible future candidates. Run/Revision, tenant membership and interaction CAS should remain together until their transaction boundary can be safely redesigned.

## Consequences

This choice retains strict dependency direction and adapter replaceability without paying the operational cost of many small services. It is intentionally different from a CRUD-oriented `service/impl/mapper` template: MyBatis remains a valid adapter choice if complex SQL mapping later justifies it, but it is not the architecture itself.
