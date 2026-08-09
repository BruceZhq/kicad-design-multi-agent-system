# Increment 7 acceptance — artifacts, revisions, and bounded delivery

Date: 2026-08-08

## Scope

Increment 7 adds immutable hardware delivery manifests, authorized artifact
downloads, human-feedback revisions, the three public delivery outcomes, and
bounded AHE execution. It does not run an LLM, KiCad, or Freerouting as part of
this acceptance.

## Implemented behavior

- Python publishes only verified files contained by the current run workspace.
  Every artifact has an immutable UUID, SHA-256, size, media type, kind, and a
  content-addressed `runs/{run_id}/...` object key.
- Local storage remains the safe development default. An S3-compatible backend
  performs idempotent `HEAD`/upload operations and records the SHA-256 as object
  metadata. `RATSNEST_ARTIFACT_SSE=none` explicitly disables SSE for local
  stores that do not provide a KMS.
- Docker Compose provides an opt-in `artifact-store` profile with a private
  MinIO bucket initializer. Its image is `pull_policy: never`, so enabling the
  profile cannot silently download data into Docker Desktop storage.
- The runtime emits one structured `artifact_manifest` after the independent
  review. HTTP, SSE, Redis state, and gRPC `result_json` preserve the same
  manifest and delivery status.
- Java verifies the canonical manifest digest, UUIDs, run object namespace,
  file names, hashes, sizes, and delivery state before inserting the immutable
  manifest and artifacts under tenant RLS.
- Java authorizes each artifact read and returns a short-lived S3-compatible
  pre-signed download redirect. Upload and browser-facing endpoints are
  configured separately.
- Human feedback creates a new Run Revision. It requires a terminal parent,
  locks the root revision, rejects a stale parent, and never overwrites an old
  manifest. Artifact listings identify superseded revisions.
- Delivery status is exactly one of `execution_blocked`,
  `delivered_with_issues`, or `release_ready`. A trusted non-empty manifest is
  required for `release_ready`.
- AHE is bounded per workflow by six total repairs, two attempts for the same
  failure signature, 60 minutes wall clock, and 120,000 model tokens. Budgets
  survive Temporal Activity and checkpoint boundaries.

## Verification evidence

- Java 21 Maven verification inside the final control-plane image:
  `38 tests`, `0 failures`, `0 errors`, `1 skipped` opt-in cross-process test;
  `BUILD SUCCESS` in 26.852 seconds.
- Frontend production build: Next.js compile and TypeScript validation passed in
  11.6 seconds.
- Python artifact tests and Ruff: `2 passed in 1.72s`; all selected checks
  passed.
- Delivery/Profile/Temporal budget tests: `8 passed in 1.59s`.
- Revision/BFF/frontend tests: TypeScript passed; `11 tests` passed.
- Real preloaded MinIO smoke: two deterministic KiCad fixtures were uploaded,
  replayed idempotently, verified by `HEAD` metadata and downloaded SHA-256,
  then deleted. Result marker: `INCREMENT7_ARTIFACT_SMOKE_OK`.
- Compose configuration with both `artifact-store` and `control-plane` profiles
  parses successfully.
- Final static audit found no P0 or P1 contract, concurrency, tenant-isolation,
  or artifact-safety issue.

## Deliberately deferred validation

The full browser-authenticated `S3 upload -> Java database ingestion -> HTTP
303 -> object download` scenario was not run in this bounded pass because it
requires a configured OIDC test issuer and starting the complete application
stack. The individual production paths compile and their deterministic
boundaries were verified, but this exact browser integration remains the next
staging smoke test. It must not invoke an LLM or EDA tool.

The broad Python runtime test batch produced no output within 30 seconds and
was terminated once, as required by the bounded-test policy. It left no running
process and was replaced by focused tests; it is not counted as a passing gate.

## Resource state after acceptance

The MinIO container was stopped and its initializer removed after the smoke
test. Test objects were deleted. Existing PostgreSQL, Redis, Kafka, and Temporal
services were left unchanged.
