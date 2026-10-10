# 00 · System overview & conventions (M0/M1 contract)

This file is the **source of truth for cross-service seams**. If you change a name, URL, env var or schema here, every consumer must change. Full product plan: `C:\Users\yxrkt\.claude\plans\pasted-content-id-b4b1-need-to-generic-turing.md`.

## Products
- **Keelson** — our engine (Python ADK backend + Next.js UI). Brand strings come from env `BRAND_ENGINE_NAME=Keelson`.
- **Merxa Retail Suite** (by Merxa Systems) — fictional mock third-party ERP. `BRAND_VENDOR_NAME=Merxa`. Merxa must **never import Keelson code** (separate product).

## Hostnames (HTTP only in dev; Caddy listens on 80)
| Public URL | Routed to |
|---|---|
| `http://keelson.localhost` | `keelson-web:3000`; `/api/*` → `engine-api:8000` (no buffering, SSE); `/basemap/*` → Caddy `file_server` over the `basemap` volume (M8 self-hosted Protomaps base map, `127-m8-basemap.md`) |
| `http://merxa.localhost` | `merxa-web:3001`; `/v1/*`, `/mcp`, `/mcp/*`, `/mcp-lite*`, `/.well-known/*` → `merxa-api:8100` |
| `http://auth.localhost` | `keycloak:8080` (`/admin*` and `/realms/master*` blocked unless `DEV_ALLOW_KC_ADMIN=1`) |
| `http://gateway.localhost` | `litellm:4000` behind `forward_auth` → `engine-api /api/internal/forward-auth?role=platform-admin` |
| `http://traces.localhost` | `phoenix:6006` (profile `obs`), same forward_auth |
| `http://fernhill.localhost` | `fernhill-sidecar:8400` (profile `adapters`, M8): the adapter sidecar demo "Fernhill ERP" — `/mcp`, signed `/v1/snapshots/*`, `/v1/admin/*` (`122-m8-adapter-sidecar.md`) |

Inside containers the **same hostnames** resolve to Caddy via network aliases, so OIDC issuers are identical everywhere:
- issuer keelson realm: `http://auth.localhost/realms/keelson`
- issuer merxa realm: `http://auth.localhost/realms/merxa`
- Merxa MCP resource (RFC 8707 / token audience): `http://merxa.localhost/mcp`
- Keelson JWKS (client assertions + approval receipts): `http://keelson.localhost/api/.well-known/jwks.json`
- Keelson issuer string for receipts: `http://keelson.localhost`

## Compose services (M1) and networks
`caddy, keycloak, kc-config (one-shot), pg-core, pg-litellm, pg-merxa, valkey, litellm, egress (squid), bootstrap (one-shot), engine-api, engine-worker, optimizer, merxa-api, merxa-web, keelson-web` (+ `phoenix` in profile `obs`).
Later milestones add `signal-hub-bootstrap` (one-shot), `signal-hub-api`, `signal-hub-poller` (M6) and, in opt-in profiles, `fernhill-sidecar` (`adapters`, M8 122), `evals-runner` (`evals`, M8 128) and `basemap-fetch` (`basemap`, one-shot, M8 127); see `120-m8-overview.md` §2.

Networks: `edge` (caddy + anything that must resolve public hostnames), `app` (engine, optimizer, pg-core, valkey; caddy joins with aliases `auth.localhost`,`keelson.localhost` only), `ai` (engine, bootstrap, litellm, pg-litellm), `vendor` (merxa-api, pg-merxa). **Engine connector traffic to third parties must go through `http://egress:3128`** (Squid) — engine services are not on a network where `merxa.localhost` resolves directly, except `engine-api` (edge) which must still use the proxy for connector calls.

## Env vars (generated into `.env` by `scripts/init.*`; never committed)
| Var | Used by |
|---|---|
| `PUBLIC_BASE_DOMAIN=localhost`, `HTTP_PORT=80` | all |
| `DEMO_MODE=1` | engine-api, merxa-api, web apps |
| `GEMINI_API_KEY` | litellm only (taken from host env, never written to files) |
| `PG_CORE_SUPERPASSWORD`, `KEELSON_DB_PASSWORD`, `ADK_DB_PASSWORD`, `KEYCLOAK_DB_PASSWORD` | pg-core + consumers |
| `LITELLM_DB_PASSWORD`, `LITELLM_MASTER_KEY` (`sk-…`), `LITELLM_SALT_KEY` | litellm, bootstrap, engine (master key only in bootstrap + engine-api gateway facade) |
| `MERXA_DB_PASSWORD` | pg-merxa, merxa-api |
| `VALKEY_PASSWORD` | valkey + engine |
| `KC_ADMIN_PASSWORD` | keycloak, kc-config, bootstrap, engine-api (demo reset) |
| `KEELSON_BFF_CLIENT_SECRET`, `KEELSON_DEMO_CLIENT_SECRET`, `MERXA_WEB_CLIENT_SECRET`, `MERXA_DEMO_CLIENT_SECRET` | keycloak config + owners |
| `DEMO_USER_PASSWORD` | seeded persona passwords (both realms) |
| `SESSION_SECRET`, `DEMO_ACCESS_SECRET` | engine-api, merxa-api |
| `FERNHILL_API_KEY`, `FERNHILL_ADMIN_KEY`, `FERNHILL_SNAPSHOT_SECRET` | fernhill-sidecar (profile `adapters`, M8 `122`): the static `X-Adapter-Key` Keelson stores when registering it, the sidecar's `/v1/admin/*` key (never given to Keelson), the HMAC secret of its signed Parquet URLs |

Files in `secrets/` (gitignored, mounted as compose `secrets:` → `/run/secrets/<name>`):
- `keelson_client_assertion_key.pem` (EC P-256, kid `ca-1`) — `private_key_jwt` to the merxa realm
- `keelson_receipt_key.pem` (EC P-256, kid `ar-1`) — approval receipts (`typ: approval-receipt+jwt`)
- `keelson_data_key` (32 random bytes, base64) — AES-256-GCM envelope key for tokens/keys at rest (key_id `dk-1`)

Python helper (both engine & merxa implement their own): `read_secret(name) -> str` reads `/run/secrets/<name>` then env `<NAME>` upper-case.

## Python service conventions
- Python **3.14** (`python:3.14.8-slim-trixie@sha256:…`), `uv` inside the image (`COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:… /uv /bin/uv`), `uv sync --frozen` from the service's own `uv.lock`. Every image reference (`FROM`, `ARG` defaults, `COPY --from`, `# syntax=`, compose `image:`, scripts) is pinned as `name:<exact tag>@sha256:<digest>` and every first-party Dockerfile is registered in `infra/supply-chain/images.json` (M8, `docs/supply-chain.md`; enforced by `scripts/supply-chain/check_pins.py`). Separate uv projects: `services/engine` (workspace incl. `packages/contracts-py`), `services/optimizer` (+ contracts-py path dep), `services/merxa-api` (standalone; no Keelson deps).
- Layout: `services/<svc>/src/<pkg>/...`, tests in `services/<svc>/tests`, `pyproject.toml`, `Dockerfile`, `README.md`.
- FastAPI ≥0.142, Pydantic 2.13, SQLAlchemy 2.1 async + asyncpg, httpx; structured JSON logs (`structlog` or stdlib JSON formatter) with `trace_id`.
- Every HTTP service exposes `GET /healthz` (process up) and `GET /readyz` (deps ready). engine-api exposes them under `/api/healthz`, `/api/readyz`.
- Non-root user, `PORT` env respected, no secrets in images.
- DB migrations: numbered SQL files `migrations/NNNN_name.sql` applied by a tiny runner (table `schema_migrations`). Idempotent.
- Tests: `pytest` + `pytest-asyncio`; must run inside the service image: `docker compose run --rm <svc> pytest -q` (tests needing Postgres use the compose DB or `testcontainers`-free fixtures with a throwaway schema).

## Node/Next conventions
- Node 24 (`node:24-slim`), pnpm 10+ via `corepack` is NOT available on host; builds happen in Docker. Root `pnpm-workspace.yaml` + Turborepo. Next.js 16.3, React 19.3, TypeScript, Tailwind 4.3, `output: 'standalone'`.
- `apps/keelson-web` and `apps/merxa-web` are separate apps; `merxa-web` must not import `@keelson/*` (ESLint `no-restricted-imports`).

## Money/units/time
- Money in integer **minor units** (`*_cents`, EUR) — never floats in contracts or change-set payloads.
- Quantities integer eaches. Dates ISO-8601 (`YYYY-MM-DD`), timestamps RFC 3339 UTC, ISO weeks `YYYY-Www`.
- Canonical JSON for hashing = `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)` (equivalent to RFC 8785 JCS because payloads contain no floats). `H = sha256(canonical(items))` hex.

## Demo calendar
Today = **2026-10-05 (ISO W41)**. Peak W45–W48 (2026-11-02 … 2026-11-29). Black Friday 2026-11-27. Seeds and SimClock use this.
