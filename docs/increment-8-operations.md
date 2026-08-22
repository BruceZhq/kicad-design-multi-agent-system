# Increment 8 operations and disaster-recovery gates

Date: 2026-08-08

## Deployment model

- The primary region runs a Cell across at least three availability zones. A Cell owns its application compute and scales independently; stateful dependencies use managed multi-AZ services where available.
- A second region is a warm standby. It receives replicated state and deployable configuration, but it does not serve production traffic until an authorized failover.
- The targets **RPO <= 1 minute** and **RTO <= 30 minutes** are acceptance objectives, not achieved or verified claims.

## Required replication

- PostgreSQL: encrypted cross-region replica, continuously monitored replication lag, and a tested promotion procedure.
- S3-compatible artifact storage: versioning and cross-region replication for immutable artifact objects and manifests.
- Kafka: cross-region topic mirroring for durable lifecycle, audit, usage, and EHE events; consumer offsets and replay boundaries must be documented.
- Temporal: a vendor-supported production disaster-recovery design with replicated persistence and namespace/workflow recovery procedures. Application-level replay alone is not sufficient evidence.

Artifact objects are retained for 90 days by default. Audit records are retained for one year. Legal hold and tenant-specific policy may only extend these periods.

## Failover order

1. Declare the incident, freeze nonessential writes, record the recovery timestamp and last confirmed event sequence.
2. Verify replication lag and object/topic completeness against the selected recovery point.
3. Promote PostgreSQL and the Temporal persistence path in the standby region.
4. Switch Kafka producers and consumers, then verify idempotency by `run_id + event_seq` before enabling workers.
5. Enable Python workers, Java control plane, and the web/OIDC edge in that order.
6. Shift traffic gradually, reconcile active runs and artifact manifests, then announce recovery.

## Failback order

1. Stabilize the recovered region and keep it authoritative while the former primary is rebuilt.
2. Re-seed and validate PostgreSQL, object storage, Kafka, and Temporal state in the former primary.
3. Run reconciliation and duplicate-execution checks; do not copy state bidirectionally without an explicit conflict policy.
4. Quiesce writes, switch traffic in stages, verify service-level indicators, then restore replication in the normal direction.

## Production acceptance gates

Production readiness requires retained evidence from a staged exercise:

- multi-AZ scheduling and single-AZ-loss survival;
- measured PostgreSQL lag and successful promotion;
- artifact and manifest checksum reconciliation after S3 replication;
- Kafka mirrored-event ordering, offset recovery, and duplicate suppression;
- Temporal recovery of running, retrying, signalled, and cancelled workflows;
- authenticated user access, SSE reconnection, and artifact authorization after traffic switch;
- measured RPO and RTO within the stated objectives; and
- a successful failback with no lost or duplicated run.

Observability and autoscaling additionally require retained evidence that:

- Tempo and Loki exporter certificates and authorization are accepted without an
  insecure fallback;
- the collector's file-backed queue survives a pod restart and produces an alert on
  retry exhaustion, queue saturation, and dropped telemetry;
- `custom.metrics.k8s.io` serves the per-pod SSE gauge for every referenced HPA
  target, and `external.metrics.k8s.io` serves the selected Temporal backlog gauge;
- both metrics cause a measured scale-up and stable scale-down in the target Cell.

Run `scripts/validate_increment8_static.ps1` for manifest checks. Its default success
means only that static contracts are present. Production gating must use
`-RequireReleaseEvidence` with metrics, telemetry, and disaster-recovery evidence
files; missing evidence intentionally returns a non-zero exit code.

## Not yet verified

The repository manifests and this runbook are deployable intent only. No real multi-AZ cluster, cross-region replication, metrics adapter, production load test, regional failover/failback exercise, or measured RPO/RTO is established by this document. These remain release gates and must not be reported as complete until the evidence above is captured and reviewed.
