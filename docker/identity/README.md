# Local OIDC identity

`ratsnest-dev-realm.json` is a deterministic, development-only Keycloak realm import for local browser and API smoke tests.

It creates:

- realm: `ratsnest-dev`
- API audience: `ratsnest-api`
- oauth2-proxy client: `ratsnest-web`
- public smoke-test client: `ratsnest-smoke`
- test user: `ratsnest-dev-engineer`

Local-only credentials:

- test user password: `ratsnest-dev-only`
- `ratsnest-web` client secret: `ratsnest-web-dev-only-not-a-secret`

These values are intentionally obvious non-secrets. Never reuse this realm, its password, the confidential-client value, or the `ratsnest-smoke` password grant in production. Production credentials must come from a secret manager and production realms must disable Direct Access Grants.

Mount the JSON read-only at `/opt/keycloak/data/import/ratsnest-dev-realm.json` and start Keycloak with `start-dev --import-realm`. Keycloak imports a realm only when it does not already exist. After changing this file, delete only the dedicated development Keycloak data volume before re-importing; do not do that to a production realm.

Start the local identity path together with the Java control plane and frontend:

```powershell
docker compose --profile control-plane --profile identity up -d --no-build `
  keycloak agent_service control_plane frontend oauth2_proxy
```

Open `http://localhost:8088`. The browser is redirected to Keycloak and returns
through `http://localhost:8088/oauth2/callback`; port `3000` is a direct
development endpoint and is not the authenticated product entry point.

Expected local Discovery and JWKS endpoints (Keycloak is published on port
`8180` and deliberately uses a `.localhost` issuer so containers and browsers
validate the same `iss` value):

- `http://auth.localhost:8180/realms/ratsnest-dev/.well-known/openid-configuration`
- `http://auth.localhost:8180/realms/ratsnest-dev/protocol/openid-connect/certs`

Both `ratsnest-web` and `ratsnest-smoke` add `ratsnest-api` to the access-token `aud` claim. The web client accepts only `http://localhost:8088/oauth2/callback`. The smoke client exists solely for bounded automated checks; browser users must use the Authorization Code flow through `ratsnest-web`.

The Compose proxy enables PKCE S256. The test login is
`ratsnest-dev-engineer` / `ratsnest-dev-only`. After the first login, the UI
asks the user to create an explicit organization/project workspace; it never
trusts a browser-supplied tenant or user ID.
