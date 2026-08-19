# RatsNest Enterprise Product Roadmap

Date: 2026-07-15

## Product objective

RatsNest will be an enterprise AI engineering product for hardware teams. Its
operating invariant is:

> LLMs propose, typed contracts validate, deterministic tools execute, KiCad
> files provide ground truth, checkers verify, humans govern releases, and AHE
> evolves only through measured promotion gates.

The product is complete when an engineer can submit a supported requirement,
review and approve an immutable design plan, observe bounded autonomous agents
materialize and verify it, and download a traceable KiCad deliverable from a
secure, resilient, multi-tenant platform.

## Delivery stages

### Stage 1 - Enterprise Alpha foundation (complete)

- Organization, workspace, project, membership, and tenant-scoped runs.
- Durable artifacts, checksums, transactional dispatch outbox, and worker ACKs.
- Post-generation engineering release approval.
- Cookie JWT, service identity, same-origin mutation protection, and previews.

### Stage 2 - Controlled autonomous agents (complete)

- Split design into `Plan -> Approve -> Execute -> Verify -> Repair -> Release`.
- Persist an immutable, versioned `PlannedDesign` and bind approval to its hash.
- Prevent every KiCad-mutating tool until BoardPlan approval is committed.
- Run RequirementAgent, CircuitArchitect, SchematicDesigner, PcbDesigner,
  VerificationCrew, and RepairAgent through typed Blackboard contracts.
- Checkpoint DesignState after actions so bounded execution can resume safely.
- Add provider routing, timeout, retry, call/token budgets, and DeepSeek support.

### Stage 3 - Production EDA capability (complete for frozen support matrix)

- Expand supported circuit families and trusted component/footprint catalogs.
- Strengthen schematic, placement, routing, BOM, ERC, DRC, SPICE, thermal, and
  basic EMC verification.
- Build golden designs, seeded defects, and board-family acceptance benchmarks.
- Frozen implementation and acceptance contract:
  [`stage-3-production-eda.md`](stage-3-production-eda.md).

### Stage 4 - Distributed execution platform

- S3/MinIO ArtifactStore, migration-owned PostgreSQL schema, and database-level
  tenant isolation.
- Worker leases, heartbeats, checkpoints, cancellation, retry/DLQ, priorities,
  Kubernetes scheduling, and autoscaling.

### Stage 5 - Enterprise identity, security, and governance

- OIDC/SAML SSO, SCIM, organization RBAC/ABAC, and configurable approval policy.
- Vault/KMS secrets, encryption, service identity, immutable audit exports,
  retention, deletion, quotas, SBOM, signing, and provenance.

### Stage 6 - Hardware engineer product experience

- Project/version history, plan and KiCad diffs, live agent timeline, comments,
  approvals, previews, controlled edits, BOM/Gerber/report downloads, and
  Git/PLM/ERP integrations.

### Stage 7 - Governed AHE evolution

- Curated trajectory datasets, offline and adversarial evaluation suites.
- Versioned prompt/tool/repair-policy candidates, shadow and canary evaluation,
  human promotion, and one-step rollback.
- No autonomous production code changes or gate bypasses.

### Stage 8 - Reliability and launch validation

- OpenTelemetry, SLOs, cost controls, capacity/load/soak/chaos testing,
  backup/restore and disaster-recovery exercises, penetration testing, and
  operational runbooks.

### Stage 9 - Enterprise pilot

- Design-partner pilots on explicitly supported board families.
- Compare cycle time, acceptance rate, human rework, and safety findings against
  the existing engineering process.
- Validate cloud and private deployment, support, licensing, and SLA boundaries.

### Stage 10 - General availability

- Natural-language requirement to approved, verified, downloadable KiCad project.
- Complete traceability for decisions, tool calls, checks, repairs, and releases.
- Secure multi-tenant operation, fault recovery, bounded cost, and governed AHE.

## Dependency order

Stages 2 and 3 now provide the controlled execution contracts and bounded EDA
acceptance baseline. Stage 4 is the next critical path. Stages 4, 5, and 6 may
proceed in parallel; Stage 7 must wait for sufficient high-quality production
trajectories in addition to the stable Stage 3 benchmarks. Stages 8 and 9 are
mandatory gates before Stage 10.
