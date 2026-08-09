# Increment 6 acceptance report

Date: 2026-08-07

## Scope

This acceptance covered only the durable Java-to-Python runtime boundary, Kafka
run events, and restart recovery. It did not invoke an LLM, KiCad,
Freerouting, or a hardware workflow.

## Result

The Increment 6 runtime path is accepted for the local Compose environment.

- The Java 21 control-plane image built successfully: 38 tests passed and the
  opt-in cross-process test was skipped by the normal unit-test profile.
- Flyway validated and installed migrations V1 through V6; failed migrations:
  zero.
- A real Java client reached the real Python gRPC adapter and verified invalid
  signature rejection (401), `GetRun`, ordered events 1/2/3, cancellation, and
  the persisted cancelled state. The injected runtime was deterministic and
  could not invoke an agent or EDA tool.
- A real control-plane publisher sent two committed outbox records through the
  local Kafka broker. Observed state versions were 1/2, duplicate source event
  sequence was suppressed without a version gap, both rows were acknowledged
  only after broker ACK, and the topic offset remained 2.
- Redis recovery tests passed (`2 passed in 0.72s`): graceful runtime shutdown
  released the fenced lease without manufacturing a cancellation; a replacement
  runtime resumed the same request with `fencing_token + 1`; terminal replay did
  not execute the producer again.
- Java reconciliation tests passed in the Java build: an existing runtime is
  queried without a duplicate `StartRun`, while a missing runtime entry resumes
  with the persisted run ID and thread ID.

## Corrections made during acceptance

- Added V6 atomic outbox append and per-run head claiming. Duplicate source
  events are checked before allocating `state_version`; concurrent publishers
  cannot lease two unpublished events for one run.
- Publisher database ACK now follows Kafka broker ACK. Retries retain the same
  immutable event ID.
- Added Kafka protocol, SASL, JAAS, and TLS hostname-verification configuration
  with fail-fast validation.
- Added an explicit Kafka producer configuration for the optional outbox path.
  Spring Boot 4 no longer auto-configures Kafka from the low-level
  `spring-kafka` dependency alone.
- Fixed the Compose Agent Runtime hostname by adding the RFC-valid
  `agent-service` network alias; Java URI validation rejects the underscore in
  `agent_service`.
- Python graceful shutdown now releases its execution lease instead of marking
  active work cancelled.
- Python gRPC runtime loading is lazy when a deterministic or dedicated runtime
  is injected, avoiding an unnecessary LangGraph initialization side effect.

## Delivery semantics and remaining boundaries

- Kafka delivery is intentionally at-least-once. A crash after broker ACK and
  before database ACK can redeliver the same event ID; consumers must deduplicate
  on `eventId`.
- An ungraceful Python process death can require waiting for the configured
  Redis lease expiry (30 seconds by default) before takeover.
- Local Kafka acceptance used `PLAINTEXT`. SASL/TLS configuration and Kubernetes
  secret wiring passed static configuration checks, but require a secured broker
  environment for a transport-level TLS acceptance test.
- The cross-process gRPC assertions passed when the Python service and Java
  client were run as two bounded steps. The convenience PowerShell wrapper has
  internal startup/client limits, but its full exit was not observed inside the
  Codex runner's 30-second outer limit; this does not affect the verified gRPC
  transport path.
- The local control-plane image currently uses the cached Java 21 Maven image as
  its runtime base because the smaller JRE image could not be pulled. Production
  packaging should switch back to a Java 21 JRE base when registry access is
  available.

All temporary Increment 6 containers, Docker networks, Kafka topics, Redis keys,
and test tenants were removed after acceptance.
