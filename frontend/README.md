# CircuitFoundry Web

Next.js 16 / React 19 frontend for the Java CircuitFoundry control plane. The browser talks only to same-origin Next.js route handlers; those handlers forward the authenticated OIDC access token to Java. They never create a `user_id` and never call the Python Agent Runtime directly.

## Data path

```text
Browser
  REST + fetch() -> response.body.getReader() -> incremental SSE parser
       |
       v
Next.js route handlers
  forward OIDC bearer token and selected organization
       |
       v
Java /api/v1 -> internal Python ratsnestpro-multi-agent
```

The chat route starts an idempotent Java run and then subscribes to its event stream. A transient disconnect reuses the same `Idempotency-Key` and sends the latest cursor as `Last-Event-ID`; disconnecting the browser stream does not cancel the run. Only `completed`, `failed`, `cancelled`, or `timed_out` events end a client run.

## Authentication and workspace setup

Token precedence is deliberately narrow:

1. Incoming `Authorization: Bearer ...`.
2. `X-Auth-Request-Access-Token` or `X-Forwarded-Access-Token` only when `WEB_TRUST_OIDC_PROXY_ACCESS_TOKEN=true`.
3. Optional server-only `CONTROL_PLANE_ACCESS_TOKEN` for local development.

Missing credentials return HTTP 401. The BFF does not fall back to an anonymous identity.

`GET /api/session` discovers organizations and projects authorized by Java. It returns HTTP 409 when setup is required. A workspace is created only after the user explicitly triggers `POST /api/session` with `workspace_name`; no fixed default organization or project is created implicitly.

## Configuration

```dotenv
CONTROL_PLANE_URL=http://control-plane:8080
WEB_TRUST_OIDC_PROXY_ACCESS_TOKEN=false
CONTROL_PLANE_ACCESS_TOKEN=
```

All three values are server-only. Never expose an access token through a `NEXT_PUBLIC_*` variable.

## Features

- Fixed `ratsnestpro-multi-agent` execution through the Java control plane
- OIDC organization/project context with explicit first-time workspace creation
- KiCad team setup with five core roles and bounded optional specialists
- Persistent browser thread, selected organization/project, team, and model preferences
- Java-backed conversation history and runtime model metadata
- `fetch + ReadableStream` incremental SSE with event replay
- Explicit model-provider reasoning only; hidden reasoning is never fabricated
- Role, tool, workflow, AHE, and Reviewer evidence rendering
- Explicit cancellation through the Java run API
- Immutable run revision and delivery-status rendering from the Java run record
- Authorized artifact manifests and short-lived download redirects; readiness is never inferred from narrative text

Human feedback is proxied to Java as a new run revision. It never overwrites the
source run, and the browser never receives an object-store key or permanent URL.

## Commands

```bash
npm ci
npm run typecheck
npm run build
npm run dev
```

## Container image

Use the repository root as build context:

```bash
docker build -f docker/Dockerfile.frontend -t ratsnest-web .
```

The image runs the Next.js standalone server as a non-root user. Do not invent a
`CONTROL_PLANE_ACCESS_TOKEN`: Java accepts only a real OIDC/JWT credential. Use the
root Compose OIDC profile for the runnable local product, or supply a token issued by
the configured test issuer for an isolated BFF integration test.

## Visual system

The white workbench adapts MIT-licensed interaction motifs from [uiverse-io/galaxy](https://github.com/uiverse-io/galaxy): layered controls, hover overlays, radial patterns, compact status loaders, and CSS-only tooltips.
