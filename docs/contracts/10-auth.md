# 10 · Authentication contract (Keycloak 26.8, two realms)

Keycloak runs `start-dev --db=postgres` with `KC_HOSTNAME=http://auth.localhost`, `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`, `KC_PROXY_HEADERS=xforwarded`, `KC_HTTP_ENABLED=true`, `KC_HEALTH_ENABLED=true`, `KC_CACHE=local`. Realms are applied (upsert, every boot) by `kc-config` (`adorsys/keycloak-config-cli`, `IMPORT_VARSUBSTITUTION_ENABLED=true`, syntax `$(env:VAR)`) from `infra/keycloak/config/*.yaml`; the demo set in `infra/keycloak/config/demo/` is applied only when `DEMO_MODE=1`. Both realms: `sslRequired: none` (dev), access token lifespan 300s.

## Realm `keelson` (our product)
- Realm roles: `planner`, `approver`, `compliance`, `tenant-admin`, `platform-admin`, `demo-presenter`.
- **Tenancy (M1):** user attribute `tenant` (multi-valued, e.g. `demo-retail`) emitted as claim `tenants` (array) by a protocol mapper on client scope `keelson-tenant` (default scope of `keelson-bff`). BFF stores `active_tenant` (first tenant unless the user picks). Second tenant `northwind-test` exists only for isolation tests (no shared users). (Keycloak Organizations are a later upgrade.)
- Roles emitted as claim `roles` (array, realm roles) via mapper.
- Clients:
  - `keelson-bff` — confidential (`KEELSON_BFF_CLIENT_SECRET`), standard flow + PKCE S256, redirect `http://keelson.localhost/api/auth/callback`, post-logout `http://keelson.localhost/`, web origins `http://keelson.localhost`.
  - `keelson-demo` (**demo set only**) — confidential (`KEELSON_DEMO_CLIENT_SECRET`), direct access grants only; adds hardcoded claim `demo: true`.
  - `litellm-console` — later milestone.
- Users (demo set; password `DEMO_USER_PASSWORD`; attribute `tenant=demo-retail`, `merxa_username` for linking hints):
  | username | name | roles | merxa_username |
  |---|---|---|---|
  | `priya` | Priya Shah | planner | `priya.shah` |
  | `marco` | Marco Rossi | planner, approver | `marco.rossi` |
  | `lena` | Lena Vogel | compliance | — |
  | `sam` | Sam Okafor | tenant-admin, planner | — |
  | `ava` | Ava Lindqvist | platform-admin | — |
  | `nora` (tenant `northwind-test`) | Nora Test | planner | — |

## Realm `merxa` (the vendor's own identity domain)
- Realm roles: `merch-planner`, `ops-director`, `integration-writer`, `merxa-admin`.
- Users (demo set): `priya.shah` (Priya Shah, merch-planner), `marco.rossi` (Marco Rossi, ops-director), `merxa.admin` (Merxa Admin, merxa-admin).
- Client scopes (each **optional** unless stated, `include.in.token.scope=true`, audience mapper adding `http://merxa.localhost/mcp`): `catalog:read`, `inventory:read`, `po:read`, `orders:read`, `snapshot:read`, `changes:preview`, `changes:apply`, `changes:apply:auto`.
- Merxa enforces role checks in addition to scopes: `changes:apply` requires the token's user to hold `ops-director`; `changes:apply:auto` requires `azp == keelson-autocommit`.
- Clients (Keelson-side ones authenticate with **`private_key_jwt`**: `clientAuthenticatorType: client-jwt`, attributes `use.jwks.url: "true"`, `jwks.url: http://keelson.localhost/api/.well-known/jwks.json`, `token.endpoint.auth.signing.alg: ES256`):
  | client | grant | default scopes |
  |---|---|---|
  | `keelson-reader` | client credentials (service account) | catalog:read inventory:read po:read orders:read snapshot:read |
  | `keelson-preparer` | client credentials | changes:preview + all read scopes |
  | `keelson-autocommit` | client credentials | changes:apply:auto |
  | `keelson-connect` | authorization code + PKCE S256, `offline_access`; redirect `http://keelson.localhost/api/connectors/merxa/callback` | optional: changes:apply, offline_access |
  | `merxa-web` | confidential (`MERXA_WEB_CLIENT_SECRET`), standard flow; redirect `http://merxa.localhost/v1/auth/callback` | — |
  | `merxa-demo` (demo set) | confidential (`MERXA_DEMO_CLIENT_SECRET`), direct access grants | — |
- Service accounts get realm role `integration-writer` (reader/preparer/autocommit).

## Keelson BFF (engine-api, Authlib)
- `GET /api/auth/login?next=/path` → OIDC code+PKCE to keelson realm; `GET /api/auth/callback`; `POST /api/auth/logout`.
- Session row in Postgres `bff_session` (tokens AES-GCM encrypted, AAD = `bff|<session_id>`), cookie `keelson_session` (HttpOnly, SameSite=Lax, Path=/). CSRF: `GET /api/me` returns `csrf_token`; all mutating `/api/*` require header `X-CSRF-Token` matching + `Origin` = `http://keelson.localhost`.
- `GET /api/me` → `{user:{sub, username, name, email, roles[], tenants[], active_tenant}, csrf_token, demo_mode, brand:{engine, vendor}}` (401 if no session).
- **Persona switcher** (DEMO_MODE only): `GET /api/demo/personas` → `[{username, name, role_label, initials}]`; `POST /api/demo/login-as {username}` performs a password grant on `keelson-demo` server-side and creates a BFF session (Set-Cookie). Never returns tokens. Allowed only if `DEMO_MODE=1` **and** public base domain is `localhost` (else requires header `X-Demo-Access: <DEMO_ACCESS_SECRET>`). Excludes `ava` (platform-admin) when not localhost. Every switch is written to `admin_audit`.
- Every route: dependency loads the object and checks `tenant_id == session.active_tenant` and role.

## Token Broker (engine) → Merxa
- **Client assertion** (RFC 7523 for `private_key_jwt`): header `{alg: ES256, kid: ca-1, typ: JWT}`, claims `{iss: <client_id>, sub: <client_id>, aud: "http://auth.localhost/realms/merxa", jti: uuid4, iat, exp: iat+60}`; POST `http://auth.localhost/realms/merxa/protocol/openid-connect/token` with `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`. For client-credentials also send `resource=http://merxa.localhost/mcp`.
- Service tokens cached per `(tenant, connector, issuer, principal_type='service', principal_sub=<client_id>)` until 30 s before expiry.
- **Connect Merxa** (delegated, per approver): `GET /api/connectors/merxa/connect?return_to=` → redirect to merxa realm authorize with `client_id=keelson-connect`, `scope=openid offline_access changes:apply`, PKCE S256, `state` + `nonce` bound to BFF session + keelson `sub`, `prompt=login`, `login_hint=<merxa_username attribute>` (demo), `resource=http://merxa.localhost/mcp`. Callback exchanges code (private_key_jwt), validates `iss` + nonce, stores refresh token encrypted in `connector_credential` keyed `(tenant, connector='merxa', issuer=merxa realm, principal_type='user', principal_sub=<keelson sub>)`, and upserts `connector_identity_link(tenant, connector, keelson_sub, ext_iss, ext_sub, ext_username, roles, scopes, status='active')`. `GET /api/connectors/merxa/link` → `{status, ext_username, scopes, roles}`.
- Refresh is single-flight per credential row (`SELECT … FOR UPDATE`), rotated refresh token stored atomically.
- Tokens never go into ADK session state, logs, or the browser.

## Approval receipt (Keelson → Merxa, verified by Merxa)
JWT ES256, header `{alg: ES256, kid: ar-1, typ: "approval-receipt+jwt"}`, claims:
```json
{"iss":"http://keelson.localhost","aud":"http://merxa.localhost/mcp","iat":0,"exp":0,"jti":"uuid",
 "approval_id":"APR-0001","change_set_ref":"<merxa change_set_id>","change_set_hash":"<H hex>",
 "tenant":"demo-retail","keelson_sub":"<sub>","ext_iss":"http://auth.localhost/realms/merxa","ext_sub":"<merxa sub>",
 "approved_by":"user:<keelson username>" | "policy:<rule_id>@<policy_version>",
 "approver_name":"Marco Rossi","initiated_by":"Priya Shah","policy_version":"p-1"}
```
Merxa `apply_change_set` verifies: signature via Keelson JWKS (kid `ar-1`), `typ`, `aud`, `exp`, `change_set_hash == preview_hash`, `change_set_ref` matches; if `approved_by` starts with `user:` then `ext_sub == access_token.sub` and token has scope `changes:apply` + role `ops-director`; if `policy:` then token `azp == keelson-autocommit` + scope `changes:apply:auto` + Merxa auto caps.

## Merxa UI auth (merxa-api BFF)
`GET /v1/auth/login`, `GET /v1/auth/callback`, `POST /v1/auth/logout`, `GET /v1/auth/me`; demo `GET /v1/auth/demo/personas`, `POST /v1/auth/demo/login-as {username}` (password grant on `merxa-demo`, DEMO_MODE only). Cookie `merxa_session` on `merxa.localhost`.
