# CircuitFoundry Kubernetes cell

This deployment keeps `ratsnestpro-multi-agent` as the only execution runtime.
Java is the external SaaS control plane; Python remains private:

```text
Ingress -> oauth2-proxy -> Next.js -> Java control plane
                                  -> signed gRPC/HTTP -> Python ratsnestpro-multi-agent
                                                       -> LangGraph -> Temporal Hardware Worker
```

PostgreSQL, Redis, Kafka, Temporal, DNS/TLS, and object or RWX storage are
production dependencies and are intentionally not installed by this base. Use
managed regional services or operator-managed clusters. The checked-in PVC is a
portable integration point; production cells should bind it to an encrypted RWX
class or replace the workspace publisher with object storage.

Run the versioned Flyway migrations with the schema-owner login before deploying
Java. The Java runtime login must be `ratsnest_app`, with `NOSUPERUSER`,
`NOBYPASSRLS`, and no ownership of the `control_plane` schema or its tables.

The base keeps gRPC, the PostgreSQL run outbox, and reconciliation disabled for a
safe rolling upgrade. After V6 is applied and the Kafka run topic exists, enable
`RATSNEST_INTERNAL_GRPC_ENABLED`, switch
`RATSNEST_AGENT_RUNTIME_TRANSPORT` to `grpc`, then enable
`RATSNEST_RUN_OUTBOX_ENABLED` and `RATSNEST_RUN_RECONCILIATION_ENABLED` in that
order. The checked-in plaintext gRPC setting assumes namespace NetworkPolicy plus
transport encryption from the cluster service mesh; without mesh mTLS, replace it
with server/client TLS before production enablement.

Before rendering, provide these two secrets through the cluster secret manager:

- `ratsnest-runtime-secrets`: `AUTH_SECRET`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `RATSNEST_DB_USER`, `RATSNEST_DB_PASSWORD`, `RATSNEST_INTERNAL_SIGNING_SECRET`,
  `REDIS_URL`, `RATSNEST_KAFKA_SASL_JAAS_CONFIG`, Kafka relay credentials, and
  model-provider keys. Supply the JAAS login-module stanza through the secret
  manager, never the ConfigMap. The internal signing secret is shared only by Java
  and Python and must contain at least 32 random bytes.
- `ratsnest-flyway-secrets`: schema-owner-only `RATSNEST_FLYWAY_USER` and
  `RATSNEST_FLYWAY_PASSWORD`. Do not merge these keys into the runtime Secret;
  Python Agent and Java application Pods must never receive schema-owner credentials.
- `ratsnest-oidc-secrets`: oauth2-proxy variables including
  `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`,
  `OAUTH2_PROXY_COOKIE_SECRET` and `OAUTH2_PROXY_REDIRECT_URL`.

Replace both image tags, host name, service endpoints, Cell ID, region, storage
class, and resource budgets. Then render without contacting a cluster:

```bash
kubectl kustomize deploy/k8s/cells/primary-region
```

Harness releases use an isolated stable/canary overlay rather than mixing two
versions behind one Service. The complete Flyway, promotion, drain, and explicit
Deployment rollback procedure is documented in
[`docs/HARNESS_CANARY_RUNBOOK.md`](../../docs/HARNESS_CANARY_RUNBOOK.md). Render
the release topology locally with:

```bash
kubectl kustomize deploy/k8s/overlays/harness-canary
```

Before attempting any release exercise, run the fail-closed cluster preflight:

```powershell
.\scripts\check_increment8_cluster_prerequisites.ps1 `
  -PrimaryContext <primary-context> `
  -SecondaryContext <warm-region-context> `
  -EvidencePath deploy\evidence\increment8-cluster-preflight.json
```

This preflight checks real API discovery, HPA conditions, collector readiness,
persistent queues, exporter Secret presence, and two distinct reachable contexts.
Its output is diagnostic only and intentionally cannot satisfy the release-evidence
validator. A workstation with no Kubernetes context must remain blocked.

The production cell requires HTTPS for both the configured issuer and JWKS
endpoint. Java rejects missing, non-HTTPS, or cross-origin endpoints outside the
isolated `dev` profile. OAuth2 Proxy uses PKCE S256, secure cookies, and explicit
Secret key references; no development realm, user, password, or client secret is
part of the Kubernetes manifests. `identity.example.com` and every `replace-me`
value are deployment placeholders, not a bundled production identity provider.
Replace them through the release overlay before admission. The checked-in cell
also supplies three replicas, an OIDC Proxy PDB, health probes, and ingress/egress
NetworkPolicies; these are deployment prerequisites, not proof of multi-AZ
availability until exercised in the target cluster.

The BFF accepts the proxy access-token header only because
`WEB_TRUST_OIDC_PROXY_ACCESS_TOKEN=true` in this topology and the frontend
Service is reachable only from oauth2-proxy. NetworkPolicy prevents the web pod
from calling Python directly. Do not enable this trust flag when clients can
reach Next.js without the proxy overwriting those headers.
