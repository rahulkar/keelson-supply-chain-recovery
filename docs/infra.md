# Local platform infrastructure

The services behind Keelson (engine) and Merxa (mock ERP) on a developer machine. The
cross-service seams are defined in `docs/contracts/00-overview.md` (hostnames, env vars,
secrets) and `docs/contracts/10-auth.md` (Keycloak realms). This page covers how to run,
verify, reset and troubleshoot the infrastructure part.

| Service | Image | Role | Memory cap / typical |
|---|---|---|---|
| `caddy` | `caddy:2-alpine` | Edge proxy on host port 80, `*.localhost` routing, forward_auth | 96 MiB / ~12 MiB |
| `egress` | `ubuntu/squid` (Squid 6.13) | Only path for connector traffic (`http://egress:3128`) | 128 MiB / ~13 MiB |
| `keycloak` | `quay.io/keycloak/keycloak:26.8.0` | Realms `keelson`, `merxa` | 900 MiB / ~485 MiB |
| `kc-config` | `adorsys/keycloak-config-cli:6.5.1-26` (pinned) | One-shot realm upsert on every boot | 512 MiB / exits |
| `pg-core` | built from `infra/postgres/core` (PG 18.6 + PostGIS 3.6.4 + pgvector 0.8.7) | DBs `keelson`, `adk`, `keycloak` | 384 MiB / ~120 MiB |
| `pg-litellm` | `postgres:18-trixie` | LiteLLM's own DB (isolated) | 256 MiB / ~66 MiB |
| `pg-merxa` | `postgres:18-trixie` | Merxa's DB | 320 MiB |
| `valkey` | `valkey/valkey:8-alpine` | Run wake-ups, event hot tail | 128 MiB / ~4 MiB |
| `litellm` | `ghcr.io/berriai/litellm-non_root:1.104.0` | AI gateway (Gemini) | 1.2 GiB / ~570 MiB |
| `phoenix` | `arizephoenix/phoenix` (profile `obs`) | Traces | 768 MiB |

All infra together (without pg-merxa/phoenix) measured **~1.27 GiB** (`docker stats`, idle).

## 1. Initialise (once, idempotent)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\init.ps1      # or: scripts/init.sh (Git Bash)
powershell -ExecutionPolicy Bypass -File scripts\init.ps1 -Check   # validate only
```

`scripts/init_secrets.py` runs inside `python:3.14-slim-trixie` (`cryptography==50.0.2`)
and creates:

* `.env` with every variable from `.env.example` (random alphanumeric secrets, URL/shell
  safe; `LITELLM_MASTER_KEY` starts with `sk-`). Existing values are **never** overwritten;
  missing or empty ones are appended. Delete a line and re-run to regenerate one value.
* `secrets/keelson_client_assertion_key.pem`, `secrets/keelson_receipt_key.pem` (EC P-256,
  PKCS#8 PEM) and `secrets/keelson_data_key` (base64 of 32 random bytes). Existing files
  are validated and kept; invalid ones are reported (exit 1), never replaced.
* Writes are atomic (temp file + rename), so an interrupted run never truncates `.env`.
  On Linux/macOS hosts the files are handed to the owner of the repo directory (the init
  container runs as root); `.env` is `0600`, the key files stay readable for the
  non-root service users that read them through compose `secrets:`.

`GEMINI_API_KEY` is never written to disk: set it in your **user environment**
(`setx GEMINI_API_KEY <key>`, new terminal). Only `litellm` receives it.

Unit tests (`scripts/tests`):

```bash
MSYS_NO_PATHCONV=1 docker run --rm --memory 384m -v D:/primark:/work -v keelson_pipcache:/root/.cache/pip \
  -w /work python:3.14-slim-trixie sh -c "pip install -q cryptography==50.0.2 pytest && python -m pytest -q -p no:cacheprovider scripts/tests"
```

Component tests that need no running stack (each cleans up after itself; exit status =
failed checks):

```bash
scripts/tests/caddy/run.sh    # Caddyfile: routes, X-Forwarded-*, admin block (+ DEV_ALLOW_KC_ADMIN=1),
                              # forward_auth incl. header spoofing, streaming, access-log redaction
scripts/tests/pgcore/run.sh   # pg-core image: health gating during first init, roles/DB owners,
                              # CONNECT revoked from PUBLIC, extensions
scripts/tests/keycloak/run.sh # realm files imported from scratch into a throwaway Keycloak:
                              # issuer, demo grants + claims, private_key_jwt clients, built-ins,
                              # idempotent re-import, DEMO_ENABLED=false disables demo grants
```

`caddy/run.sh` runs the real Caddyfile against stub upstreams on a throwaway network
(`CADDYFILE=<path>` tests another file); `pgcore/run.sh` builds the image and boots a
throwaway container without a volume; `keycloak/run.sh` never touches the shared Keycloak
(own network, in-memory DB, dummy secrets) but needs ~1.3 GB while it runs.

## 2. Start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\up.ps1           # whole stack
powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Infra    # infra services only
powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Services caddy,keycloak -NoBuild   # comma or space separated
powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Obs      # + Phoenix
```

`up.ps1` checks Docker / `GEMINI_API_KEY` / port 80, runs init when needed, strips host
environment variables that would shadow `.env` (see Troubleshooting), runs
`docker compose up -d --build`, waits for health (one-shots like `kc-config` must exit 0)
and prints a status table.

Manual equivalent: `docker compose up -d pg-core pg-litellm valkey keycloak kc-config litellm egress caddy`.
Keycloak needs ~60-70 s to become healthy; `kc-config` then takes ~15 s on the first
import and ~2 s afterwards (unchanged config is skipped by checksum).

## 3. Verify

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify-infra.ps1            # includes 1 tiny Gemini call
powershell -ExecutionPolicy Bypass -File scripts\verify-infra.ps1 -SkipLlm
```
(`scripts/verify-infra.sh [--skip-llm]` from Git Bash.) It runs `scripts/verify_infra.py`
in a throwaway container attached to `keelson_app` + `keelson_ai` (with `.env` as
environment, values never printed) and prints one JSON line per check:

| Check | Expectation |
|---|---|
| OIDC discovery `http://auth.localhost/realms/{keelson,merxa}` | issuer is exactly `http://auth.localhost/realms/<realm>` |
| password grant `keelson-demo` / `priya` | access token `roles == ["planner"]`, `tenants == ["demo-retail"]`, `demo == true`, `sub` present, 300 s lifetime |
| password grant `merxa-demo` / `marco.rossi` | `ops-director` in `roles` and `realm_access.roles` |
| admin API (internal `http://keycloak:8080`) | `keelson-reader/-preparer/-autocommit/-connect` use `client-jwt`, `use.jwks.url=true`, `jwks.url=http://keelson.localhost/api/.well-known/jwks.json`, ES256; default scopes per contract; service accounts hold `integration-writer`; example token `aud` contains `http://merxa.localhost/mcp`; connect has PKCE S256 + optional `changes:apply`, `offline_access` |
| built-ins preserved | `account`, `admin-cli`, `broker`, `realm-management`, ... and built-in client scopes still exist |
| public admin blocked | `auth.localhost/admin/*` and `/realms/master/*` → 403 |
| admin consoles | `gateway.localhost`, `traces.localhost` → 401/403 (or 502 while engine-api is down) |
| LiteLLM | `/health/readiness` 200; temporary model `infra-smoke` (`gemini/gemini-3.8-flash`) created via `/model/new`, one chat call, deleted via `/model/delete` |
| egress | `merxa.localhost` and `fernhill.localhost` (port 80 only; M8 adapter sidecar demo, 502 from Caddy while profile `adapters` is off) answered by Caddy, `merxa.localhost:443` / `fernhill.localhost:443` / `fernhill-sidecar:8400` denied; `auth.localhost/realms/merxa` allowed; `auth.localhost/realms/keelson`, `pg-core:5432`, `keycloak:8080`, `169.254.169.254`, `127.0.0.1` → 403 `ERR_ACCESS_DENIED`; `https://example.com` via CONNECT 200 |
| DNS per network | on `app` and `edge`, `auth.localhost` and `keelson.localhost` resolve to Caddy |

Last run (2026-10-06, independent review): all checks green; Gemini smoke call ~2.1 s through the proxy.

## 4. Hostnames and networks

| URL | Upstream | Notes |
|---|---|---|
| `http://keelson.localhost` | `keelson-web:3000`; `/api`, `/api/*` → `engine-api:8000` | API unbuffered (`flush_interval -1`, SSE) |
| `http://merxa.localhost` | `merxa-web:3001`; `/v1/*`, `/mcp`, `/mcp/*`, `/mcp-lite*`, `/.well-known/*` → `merxa-api:8100` | MCP + API unbuffered |
| `http://auth.localhost` | `keycloak:8080` | `/admin*`, `/realms/master*` → 403 unless `DEV_ALLOW_KC_ADMIN=1` |
| `http://gateway.localhost` | `litellm:4000` | `forward_auth` → `engine-api:8000/api/internal/forward-auth?role=platform-admin` |
| `http://traces.localhost` | `phoenix:6006` | same forward_auth |
| `http://fernhill.localhost` | `fernhill-sidecar:8400` (profile `adapters`) | M8 adapter sidecar demo (`docs/contracts/122`); unbuffered |
| anything else on :80 | — | 404 |

Caddy keeps the original `Host` header and sets `X-Forwarded-For/-Proto/-Host` (plus
`X-Forwarded-Port: 80`, `X-Real-IP`); client-supplied `X-Forwarded-*` are overwritten.
Access logs are JSON on stdout; `Cookie`/`Authorization` (Caddy default) and our own
credential headers `X-Demo-Access`, `X-CSRF-Token`, plus `code`/`session_state` query
values are redacted. Admin
API on `127.0.0.1:2019` inside the container (healthcheck only). Upstreams can be
overridden with env vars (`ENGINE_API_UPSTREAM`, `KEELSON_WEB_UPSTREAM`,
`MERXA_API_UPSTREAM`, `MERXA_WEB_UPSTREAM`, `KEYCLOAK_UPSTREAM`, `LITELLM_UPSTREAM`,
`PHOENIX_UPSTREAM`), e.g. for a host-dev overlay pointing at `host.docker.internal`.

forward_auth: Caddy sends `GET /api/internal/forward-auth?role=platform-admin` to
engine-api with the browser's headers (incl. `Cookie`), `Host: gateway.localhost` and
`X-Forwarded-Method/-Uri`. 2xx → request proxied (headers `X-Auth-User`, `X-Auth-Roles` are
copied upstream if set); anything else is returned to the browser as-is.

Inside containers the same hostnames resolve to Caddy via network aliases:
`edge` → all five names; `app` → `auth.localhost`, `keelson.localhost` only; `ai` → none.

## 5. What each config does

**pg-core** (`infra/postgres/core`): image adds `postgresql-18-pgvector` from PGDG. On the
first boot (empty volume) `init/10-keelson-init.sh` creates login roles + databases
`keelson`, `adk`, `keycloak` (each DB owned by its role, `REVOKE ALL ... FROM PUBLIC`,
`CONNECT` only for the owner; also revoked on `postgres`/`template1`/`template_postgis`) and enables
`postgis`, `vector`, `btree_gist`, `pgcrypto` in `keelson` (`CREATE` on `public` revoked from
PUBLIC). Passwords are passed as psql variables, never echoed. Init scripts run **once**:
changing a password in `.env` later requires `ALTER ROLE` or a volume reset. The healthcheck
probes over TCP (`pg_isready -h 127.0.0.1`): during first init the entrypoint's temporary
server listens on the unix socket only, so a socket probe would let Keycloak start before
the `keycloak` database exists.

**Keycloak realms** (`infra/keycloak/config/{keelson,merxa}.yaml`, keycloak-config-cli
format): see `10-auth.md`. Implementation notes:
* `roles` claim = realm roles via client scope `keelson-roles` / `merxa-roles`. Keelson
  clients use `fullScopeAllowed: false` + scope mappings so tokens contain only the six
  product roles (no `default-roles-keelson`, `offline_access`, ...). Merxa clients keep full
  scope (`roles` also includes `default-roles-merxa`, `offline_access`, `uma_authorization`).
* `tenants` = multi-valued user attribute `tenant` declared in the realm user profile as
  admin-only (users cannot edit their own tenants); `merxa_username` likewise.
  keycloak-config-cli only applies `userProfile` when realm attribute
  `userProfileEnabled: "true"` is present.
* Demo clients (`keelson-demo`, `merxa-demo`) and personas use `enabled: <DEMO_ENABLED>`.
  They live in the main files; `DEMO_ENABLED=false` disables them without deleting.
* kc-config behaviour (pinned in compose): remote state on (only deletes objects it
  created itself), `client-scope=no-delete`, default role kept on users. Built-in clients
  and scopes are untouched (verified).
* Variable substitution (`IMPORT_VARSUBSTITUTION_ENABLED=true`) uses the dollar-paren
  `env:NAME` syntax and also runs inside **YAML comments** - never write that syntax in a
  comment (an undefined variable aborts the import).

**Squid** (`infra/squid/squid.conf`): see the header comment. Order: client must be on a
private network → ports 80/443 only, CONNECT only to 443 → allow `merxa.localhost`, `fernhill.localhost`
(M8 adapter sidecar demo) and `auth.localhost/realms/merxa/*` (plain HTTP, port 80 only) → deny single-label names, compose service
names, `.localhost/.local/.internal/...` → deny private/loopback/link-local/CGNAT/ULA
destinations (on resolved IPs) → allow the rest. No caching; `max_filedescriptors 4096`.
Logs: `docker compose logs egress`.

**LiteLLM** (`infra/litellm/config.yaml`): master key and DB URL from env,
`store_model_in_db: true`, no `model_list` (aliases are seeded by `bootstrap` from
`models.lock`), no background health checks, `drop_params`/`modify_params`, 2 retries,
600 s timeout. Never enable semantic caching or JWT auth. Console: `http://gateway.localhost`
(UI user `admin`, password `LITELLM_UI_PASSWORD`).

## 6. Stop / reset

```powershell
docker compose stop                       # keep data
docker compose down                       # remove containers + networks, keep volumes
```

Reset a single store (destroys its data; the next `up` re-initialises it):

| Reset | Command (after `docker compose rm -sf <svc>`) |
|---|---|
| pg-core (keelson, adk, **keycloak**) | `docker volume rm keelson_pg_core` |
| LiteLLM DB (models, keys, spend) | `docker volume rm keelson_pg_litellm` |
| Merxa DB | `docker volume rm keelson_pg_merxa` |
| Valkey | `docker volume rm keelson_valkey_data` |

Re-apply realms without reset: `docker compose up kc-config` (unchanged files are skipped).
Rotating generated secrets: delete the line(s) from `.env`, run init, then reset the
affected store (DB passwords are baked in at first init). **Never rotate
`LITELLM_SALT_KEY`** without resetting `pg-litellm` (stored provider keys become unreadable).

## 7. Troubleshooting

**Port 80 busy.** `Get-NetTCPConnection -LocalPort 80 -State Listen` then
`Get-Process -Id <OwningProcess>`. Usual suspects: IIS / `W3SVC` (`Stop-Service W3SVC`),
`http.sys` consumers (`netsh http show servicestate`), Skype, other dev proxies or another
compose project publishing :80. The contract assumes port 80 (OIDC issuer has no port).

**`*.localhost` on the Windows host.** Browsers and `curl.exe` resolve any `*.localhost` to
loopback internally. The Windows resolver does not: Node (`dns.lookup` → `ENOTFOUND`) and
Python (`getaddrinfo` → errno 11001) running **on the host** fail (verified with Node 25.8
and Python 3.14). Run `scripts\hosts.ps1` (prints entries; `-Apply` in an elevated shell adds
a marked block to the hosts file, `-Remove` removes it).

**`*.localhost` inside containers with curl.** curl >= 7.85 hard-wires `*.localhost` to
127.0.0.1/::1 (RFC 6761) and never asks DNS, so `curl http://auth.localhost` inside a
container fails with "connection refused" although DNS is fine. Use
`--resolve auth.localhost:80:$(getent hosts auth.localhost | cut -d' ' -f1)` or a proxy
(`-x http://egress:3128`). Python/httpx, Node and Java use the system resolver and work.

**Host environment shadows `.env`.** docker compose gives shell variables precedence over
`.env`. A user-level `LITELLM_MASTER_KEY` (seen on this machine, from another project)
silently replaced the generated key, so the master key in `.env` was rejected (401
`token_not_found_in_db`). `up.ps1` removes such variables for its own process and warns. When
calling compose by hand: `env -u LITELLM_MASTER_KEY docker compose ...` (Git Bash) or
`Remove-Item Env:LITELLM_MASTER_KEY` (PowerShell, current session only). Every compose
command also requires `GEMINI_API_KEY` to be set (litellm uses `${GEMINI_API_KEY:?}`).

**Memory (8 GB shared).** Keycloak and LiteLLM are the big ones (~0.5 GiB each). Start only
what you need (`up.ps1 -Services ...`), stop when done (`docker compose stop`). Keycloak heap:
`JAVA_OPTS_KC_HEAP` (MaxRAMPercentage=60 of the 900 MiB cap; `MinHeapFreeRatio` must be <=
`MaxHeapFreeRatio`, otherwise the JVM refuses to start). Squid without
`max_filedescriptors` uses ~130 MiB because Docker's nofile limit is 1M.

**kc-config fails** with `Cannot resolve variable 'env:...'`: a variable is missing in the
kc-config environment, or the substitution syntax appears in a comment. Tokens without
`tenants`: the user profile was not applied (check `attributes.userProfileEnabled`).

**Keycloak admin console.** Not exposed publicly. Either set `DEV_ALLOW_KC_ADMIN=1` in `.env`
and `docker compose up -d caddy` (then `http://auth.localhost/admin/`, user `admin`,
password `KC_ADMIN_PASSWORD`), or use the admin REST API from inside the network at
`http://keycloak:8080`.

**LiteLLM `/health/readiness` OK but calls fail.** Check `GEMINI_API_KEY` is set in the shell
that ran compose (`docker compose exec litellm sh -c 'test -n "$GEMINI_API_KEY" && echo set'`).
