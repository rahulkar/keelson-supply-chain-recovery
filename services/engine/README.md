# keelson-engine

Keelson's backend: **engine-api** (FastAPI BFF + REST + SSE + back-office, port 8000),
**engine-worker** (ADK runtime, E2 — `runtime/`, `worker.py`) and the one-shot **bootstrap** job.
One uv project (`pyproject.toml` + `uv.lock`, Python 3.14), one image (`Dockerfile`, build
context = repo root). Contracts: `docs/contracts/10-auth.md`, `30-engine-api-events.md`,
`50-engine-workflow.md`, `55-engine-internals.md`, `70-backoffice-m2.md`, `80-m3-deterministic-core.md` §1
(canonical store + sync), `25-scn-1.0.md`, `90-m4-investigation.md` §7 (knowledge base) + §10 (`scenario.reset`),
`95-m5-remediation.md` (M5 platform side: quorum, policies API, re-plan routing, Guide checklists, demo reset).

This README documents the **platform** part (E1): `config.py`, `core/`, `api/`, `bootstrap.py`,
`migrations/`, `tests/platform/`.

## Layout

```
src/keelson_engine/
  config.py            Settings (pydantic-settings; every *engine-env var) — get_settings()/set_settings()
  public_errors.py     user-visible failures: generic message + error_id; exception logged under the id
  bootstrap.py         python -m keelson_engine.bootstrap (idempotent; exit 1 on failure)
  core/
    secrets.py         read_secret(name): /run/secrets/<name> → env NAME
    logs.py            structlog JSON logs, trace_id contextvar
    db.py              init_db, keelson_engine, tenant_session(tenant), system_session()
    migrate.py         numbered-SQL runner (schema_migrations, advisory lock, checksums)
    crypto.py          AES-256-GCM envelope (dk-1), JWKS (ca-1, ar-1), client assertion, receipts
    valkey.py, http.py shared clients (connector_http goes through the egress proxy)
    events.py          EventBus (run_event + Valkey stream), ReasoningCoalescer
    runs.py            enqueue/claim/heartbeat/finish/reap/cancel (runs queue)
    interrupts.py      create/record_decision(quorum, supersede, resume run)/supersede_open
    tokens.py          TokenBroker (service + delegated tokens), ConnectRequired
    oidc.py            OIDC discovery/JWKS/token endpoint/JWT verification (Authlib PKCE helpers)
    connectors.py      connector registry rows, tool snapshot + review state, auth_headers() for the
                       runtime, tenant LLM keys (+ key_version)
    connector_admin.py back-office connector admin: URL policy, create, test-connection step log,
                       tool review (guarded), activate/disable/delete, webhook events
    mcp.py             FastMCP client helpers (paged tools/list, connection test), via the egress proxy
    litellm_admin.py   LiteLLM management API client (bootstrap, readiness, AI Gateway facade)
    gateway.py         AI Gateway facade: alias rules, model views, tenant team, key rotation, usage
    agent_specs.py     versioned agent specs (merge patch, optimistic concurrency, validation, catalog)
    packs.py           template packs (PACKS_DIR/<pack>/pack.yaml; M7 `mappings` files)
    tenants.py         tenant provisioning from a pack (LiteLLM team+key, connectors, spec v1, Keycloak)
    keycloak_admin.py  Keycloak admin REST (`tenant` user attribute; M7 users, roles, actions e-mail, logout)
    webhooks.py        Standard Webhooks verification + connector_event storage (+ scn stale marks)
    scn_sync.py        scn/1.0 canonical store: snapshot sync (open → export → download → COPY → flip → GC),
                       current/pin helpers, stale marks + debounce, due list, tier-C poll (M3)
    audit.py           admin_audit listing (+ M7 categories), cross-source activity timeline, evals listing
    kb.py              knowledge base (M4): embedding profiles, ingestion, search(), re-embed jobs, bootstrap seed
    kb_text.py         KB text handling (pure): type detection, md/txt/pdf extraction, heading-aware chunker
    kb_pdf.py          PDF extraction in a resource-limited child process (RLIMIT_AS/CPU, timeout, no secrets)
    policies.py        M5 approval policy: default rules, effective rule set, guards, evaluator port (CEL in runtime)
    replan.py          M5 plan dependencies (lanes/receipts/connectors/DCs) + webhook re-plan routing → signal runs
    signals.py         M5 get_signal(): what a signal run reads (case_signal row / connector event)
    checklists.py      M5 Guide-mode checklists: store, runtime port, manual ticks, MCP read-back verification, poll
    demo_reset.py      M5 full demo reset (step log; Merxa, rows, ADK, Valkey, Keycloak consents, links, spec, resync)
    signal_hub.py      M6 Signal Hub client (service JWT, alerts/events/network/ingest/demo), internal connector spec
    network.py         M6 tenant network (nodes + lane paths) from the current snapshots, pushed after each sync
    hub_ingest.py      M6 webhook -> hub forwarding (inline + retry outbox on connector_event)
    alerts.py          M6 alert<->case (one case per alert, auto-open), AlertWatcher (poll, diff, SSE stream)
    alert_map.py       M6 the alert drawer's network_map@1 (footprint + exposed sites/lanes at network positions)
    evals.py           M8 eval runs: runner heartbeat/claim/lease/events, case links, results, imports, export,
                       compare, trends, recordings coverage, runtime port (get_eval_link / add_eval_usage)
    evals_summary.py   M8 pure run summary, case columns, run comparison, trend series (integer units)
    change_requests.py M7 four-eyes change requests (GuardedChange → pending request, approve/reject, expiry, handlers)
    users.py           M7 tenant users via the Keycloak admin API (list/create/patch/disable, admin grants = four-eyes)
    connector_mappings.py M7 tier-A read/action mappings (versioned), capabilities, action dry-run
    mapping_eval.py    M7 safe interpreter of the mapping language + templates + read-back match (no eval)
    scn_crawl.py       M7 tier-A crawl sync (paged read tools → scn/1.0 snapshot, consistency 'crawled')
    signals_admin.py   M7 Signals admin: source subscriptions + cadence, watch regions, alert rules → hub
    repo.py            tenant-scoped domain helpers (cases, messages, scenarios, evidence, plans,
                       blocks, change sets, ledger, findings, calc, agent specs, admin_audit)
    context.py         use_tenant()/resolve_tenant() (ambient tenant for repo helpers)
  api/
    main.py            create_app(); app (uvicorn keelson_engine.api.main:app)
    middleware.py      trace id + Origin check on mutating /api/* (pure ASGI, SSE-safe)
    sessions.py        bff_session, oauth_flow, console tickets/sessions
    deps.py            session/CSRF/tenant/role dependencies
    routes_auth.py     /api/auth/*, /api/me, /api/demo/*
    routes_cases.py    /api/cases*, SSE /api/cases/{id}/events, /api/blocks/{id}/data
    routes_interrupts.py /api/interrupts/{id}/decision, /api/runs/{id}/cancel
    routes_connectors.py /api/connectors*, Connect-<slug> OAuth, refresh-tools
    routes_admin.py    /api/admin/* (agents, gateway, connectors, packs, tenants, audit)
    routes_admin_kb.py /api/admin/kb* (knowledge bases, documents, re-embed)
    admin_deps.py      back-office context: role, tenant (+ audited platform-admin ?tenant=)
    routes_webhooks.py POST /api/webhooks/{tenant}/{connector} (no cookie/CSRF/Origin; signed)
    routes_demo.py     POST /api/demo/reset (M5), /api/demo/packs/{pack}/{action} (M6) (DEMO_MODE, presenter)
    routes_alerts.py   M6 /api/alerts*, SSE /api/alerts/events
    routes_admin_m7.py M7 /api/admin/* (change requests, users, mappings + crawl + dry-run, signals, activity, evals)
    routes_admin_evals.py   M8 /api/admin/evals/* (status, runs, events, case results, cancel, export, imports, compare, trends)
    routes_internal_evals.py M8 /api/internal/evals/runner/* (bearer EVALS_RUNNER_TOKEN: heartbeat, claim, events, attach, results, finish)
    routes_system.py   healthz/readyz, JWKS, forward-auth, console handoff (/__keelson/*)
    shutdown.py        SIGTERM hook that ends open SSE streams at once (graceful shutdown)
migrations/0001_init.sql   all engine tables + RLS policies
migrations/0002_tenant_consistency.sql   composite (tenant, ref) foreign keys
migrations/0003_backoffice.sql   connector/connector_tool M2 columns, connector_event, agent_spec seq,
                                 tenant_llm_key.key_version, tenant.admin_overrides
migrations/0004_scn.sql   scn_snapshot, scn_current, scn_pin, scn_sync_state + one scn_<entity> table per
                          scn/1.0 entity (RLS FORCE)
migrations/0005_kb.sql    embedding_profile, kb, kb_document, kb_chunk, kb_reembed_job (RLS FORCE); the
                          per-profile kb_embedding_<id> vector tables are created by core/kb.py
migrations/0006_llm_replay.sql  llm_replay (runtime/replay.py recordings)
migrations/0007_m5_remediation.sql  approval_decision.roles/reason, plan_dependency, case_signal, checklist,
                          checklist_step (RLS FORCE, composite tenant FKs)
migrations/0008_scn_gc.sql  scn_snapshot.gc_requested_at (background snapshot GC)
migrations/0009_m6_signals.sql  auth_kind service_jwt, alert_case (RLS FORCE), connector_event hub forward outbox
migrations/0010_m7_backoffice.sql  tenant.status (suspension), admin_change_request + connector_mapping (RLS FORCE),
                          crawled snapshot columns, per-op commit_ledger columns, connector_tool.binding
migrations/0012_m8_evals.sql  eval_run, eval_case_result, eval_run_event, eval_case_link (RLS FORCE), eval_runner
tests/platform/            pytest (throwaway Postgres + Valkey; mocked Keycloak/LiteLLM via respx)
```

## Configuration

All variables of `compose.yaml` `*engine-env` (names are env var names):

| Env | Default | Notes |
|---|---|---|
| `KEELSON_DATABASE_URL` | `postgresql+asyncpg://keelson:…@pg-core:5432/keelson` | role `keelson` (owner, no BYPASSRLS) |
| `ADK_DATABASE_URL` | | used by the runtime (ADK `DatabaseSessionService`) |
| `VALKEY_URL` | `redis://valkey:6379/0` | run wake-ups + live event tail |
| `OIDC_ISSUER` | `http://auth.localhost/realms/keelson` | BFF client `keelson-bff` (`KEELSON_BFF_CLIENT_SECRET`) |
| `KEELSON_DEMO_CLIENT_SECRET`, `DEMO_USER_PASSWORD` | | persona switcher (`keelson-demo` password grant) |
| `MERXA_ISSUER`, `MERXA_MCP_URL` | merxa realm / `http://merxa.localhost/mcp` | connector defaults (rows come from tenants.yaml) |
| `CONNECTOR_PROXY_URL` | `http://egress:3128` | all connector traffic (MCP + merxa realm) |
| `KEYCLOAK_INTERNAL_URL`, `KC_ADMIN_PASSWORD` | | `merxa_username` hint at login; tenant creation appends the `tenant` user attribute (admin-cli, master realm) |
| `LITELLM_BASE_URL`, `LITELLM_MASTER_KEY` | | bootstrap, readiness, AI Gateway facade (server-side only) |
| `LLM_REQUEST_TIMEOUT_S` | `180` | one model request through the proxy; the client retries a timed-out request (a hung provider call stalled a run 10 min at 600 s). Used by every worker LLM agent (`KeelsonLiteLlm(timeout=…)` via `runner_cache.default_bindings`) and by KB `/embeddings` calls (`core/kb.py`, ingestion and search). (updated M4, 2026-10-07) |
| `PACKS_DIR` | `/app/packs` (compose mounts `data/packs`) | template packs; falls back to the source tree |
| `EVALS_RESULTS_DIR`, `EVALS_LIST_LIMIT` | `/app/evals/results` (else the source tree), `10` | M7 `GET /api/admin/evals`: latest `evals/results/<UTC stamp>.json` (mount read-only); M8: the "results folder" the Evals page imports from |
| `EVALS_RUNNER_TOKEN` | empty (off) | M8 bearer token of the in-app eval runner (`/api/internal/evals/runner/*`; empty = 404, no in-app runs) |
| `EVALS_LIVE_ENABLED` | `0` | M8 allow live (model-calling) eval runs; replay runs need nothing |
| `EVALS_LIVE_MAX_TOKENS`, `EVALS_LIVE_MAX_COST_MICROUSD` | `3000000`, `5000000` | M8 hard caps of one live eval run's budget (tokens; integer micro-USD) |
| `EVALS_RUNNER_STALE_S`, `EVALS_LEASE_S`, `EVALS_MAX_IMPORT_BYTES` | `60`, `180`, `8388608` | M8 runner offline after; claimed-run lease (renewed by every runner call); largest imported results document |
| `SCN_CRAWL_MAX_ROWS` | `2000000` | M7 tier-A crawl: rows one crawl may load (all entities; hard cap 10M) |
| `CONNECTOR_DEV_HOSTS` | `merxa.localhost` | comma-separated hosts allowed although not public (URL policy) |
| `MERXA_WEBHOOK_SECRET` (any `*_WEBHOOK_SECRET`) | | named by a connector's `webhook_secret_ref`; `whsec_<base64>` |
| `CONNECTOR_TEST_TIMEOUT_SECONDS` | `20` | per network step of a connection test |
| `PLATFORM_MAX_LLM_CALLS`, `PLATFORM_MAX_TOTAL_TOKENS` | `120`, `2000000` | agent-spec budget ceilings |
| `PLATFORM_MAX_RPM_LIMIT`, `PLATFORM_MAX_TPM_LIMIT` | `1000`, `2000000` | tenant-admins may set RPM/TPM up to these |
| `WEBHOOK_MAX_BODY_BYTES`, `WEBHOOK_TOLERANCE_SECONDS`, `WEBHOOK_RATE_PER_MINUTE` | `262144`, `300`, `600` | webhook receiver |
| `PUBLIC_URL` | `http://keelson.localhost` | redirect URIs; the only accepted `Origin` |
| `PUBLIC_BASE_DOMAIN` | `localhost` | persona switcher gating; console hosts `gateway.<domain>`, `traces.<domain>` |
| `SESSION_SECRET` | | HMAC key for session ids (ephemeral random if unset, with a warning) |
| `DEMO_MODE`, `DEMO_ACCESS_SECRET` | `0` | see "Demo persona switcher" |
| `BRAND_ENGINE_NAME`, `BRAND_VENDOR_NAME` | `Keelson`, `Merxa` | `/api/me` brand |
| `MODELS_LOCK`, `TENANTS_FILE` | `/app/models.lock`, `/app/tenants.yaml` | bootstrap inputs |
| `SECRETS_DIR` | `/run/secrets` | `keelson_client_assertion_key`, `keelson_receipt_key`, `keelson_data_key` |
| `MIGRATIONS_DIR` | `/app/migrations` (image) or the source tree | |
| `SCN_SYNC_DEBOUNCE_SECONDS`, `SCN_SYNC_MAX_DELAY_SECONDS` | `30`, `300` | webhook stale marks: due 30 s after the last event, at most 300 s after the first |
| `SCN_SYNC_RETRY_SECONDS` | `60` | back-off after a failed sync: `retry × 2^(failures-1)`, ≤ 15 min |
| `SCN_SYNC_TIMEOUT_SECONDS`, `SCN_SYNC_DOWNLOAD_TIMEOUT_SECONDS`, `SCN_SYNC_DOWNLOAD_CONCURRENCY` | `600`, `120`, `4` | whole sync / per part / parallel part downloads |
| `SCN_SYNC_MAX_PART_BYTES`, `SCN_SYNC_MAX_TOTAL_BYTES` | 256 MiB, 2 GiB | declared sizes above these fail the sync before downloading |
| `SCN_SYNC_KEEP_SNAPSHOTS`, `SCN_SYNC_KEEP_FAILED` | `2`, `20` | GC: newest ready/superseded snapshots kept (+ pinned); failed attempts kept as history |
| `SCN_SYNC_TMP_DIR` | system temp | where Parquet parts are staged (a tmpfs `/tmp` is enough; parts are deleted after the load) |
| `KB_EMBED_ALIAS` | `embed-default` | models.lock alias the embedding profile is seeded from (`params.dimensions` = dims) |
| `KB_SEED_DIR` | `/app/kb`, else the source tree's `data/kb` | bootstrap seed documents `<dir>/<tenant slug>/*.md` |
| `KB_SEED_NAME` | `Playbooks & policies` | name of the seeded knowledge base |
| `KB_MAX_UPLOAD_BYTES`, `KB_MAX_PDF_PAGES`, `KB_MAX_CHUNKS_PER_DOCUMENT` | 5 MiB, `500`, `2000` | upload limits |
| `KB_CHUNK_TOKENS`, `KB_CHUNK_OVERLAP_TOKENS`, `KB_EMBED_BATCH` | `600`, `80`, `32` | chunker (estimated tokens) / texts per `/embeddings` request |
| `KB_PDF_TIMEOUT_S`, `KB_PDF_MEMORY_MB` | `60`, `384` | PDF extraction child process: wall-clock timeout (killed) and RLIMIT_AS; either → 422 `pdf_too_complex` |
| `KB_REEMBED_STALE_SECONDS` | `900` | a `running` re-embed job (or `processing` document) without progress this long counts as interrupted |
| `REPLAN_EVENT_TYPES` | `lane.capacity_changed` | M5: webhook types that re-plan dependent cases (also `sailing.updated`) |
| `CHECKLIST_POLL_SECONDS` | `60` | M5: read-back poll of open Guide checklists (sooner after a connector event) |
| `DEMO_TENANTS` | `demo-retail` | M5: the tenant `POST /api/demo/reset` resets (first entry) |
| `DEMO_MERXA_USERS` | `marco.rossi,priya.shah` | M5: Merxa users whose consents/offline sessions the reset always revokes (+ every linked user) |
| `MERXA_API_URL`, `MERXA_PUBLIC_URL`, `DEMO_MERXA_ADMIN` | `http://merxa-api:8100`, `http://merxa.localhost`, `merxa.admin` | M5 demo reset: Merxa REST base (engine-api reaches merxa-api on the `edge` network), the `Origin` Merxa expects, the demo director persona |
| `DEMO_RESET_MERXA_TIMEOUT_S` | `25` | M5: Merxa `scenario:reset` timeout |
| `SIGNAL_HUB_URL`, `SIGNAL_HUB_SHARED_SECRET` | compose: `http://signal-hub-api:8300` / from `.env` | M6: the Signal Hub (internal); both set = the integration is on (network push, webhook forwarding, alerts, the internal connector). Service JWT HS256 `{iss keelson-engine, aud signal-hub, sub, tenant, iat, exp, jti}` |
| `SIGNAL_HUB_CONNECTOR_SLUG`, `SIGNAL_HUB_TIMEOUT_S`, `SIGNAL_HUB_TOKEN_TTL_S` | `signal-hub`, `10`, `60` | M6: the internal connector's slug; per-call timeout; token lifetime (5-600 s) |
| `SIGNAL_HUB_FORWARD_EVENT_TYPES`, `SIGNAL_HUB_FORWARD_INLINE_TIMEOUT_S` | `supplier_message.received,lane.capacity_changed,sailing.updated,po_line.updated`, `3` | M6: webhook types forwarded to `POST /v1/ingest/connector-event`; inline forward timeout (slower = retried) |
| `ALERTS_WATCH`, `ALERTS_POLL_SECONDS`, `ALERTS_LIST_LIMIT`, `ALERTS_STREAM_MAXLEN`, `ALERTS_TOOLS_REFRESH_SECONDS` | `true`, `5`, `200`, `500`, `900` | M6 alert watcher in engine-api (see "Signal Hub") |
| `RUN_LEASE_SECONDS`, `EVENT_STREAM_MAXLEN`, `EVENT_STREAM_TTL_SECONDS`, `SESSION_IDLE_SECONDS`, `SESSION_MAX_SECONDS`, `LOG_LEVEL`, `LOG_JSON`, `PORT` | | |

### Runtime settings (engine-worker) (updated M4, 2026-10-07)

The worker reads the table above (`config.Settings`) plus these. None of them are set in `compose.yaml`, so the defaults apply.

| Env | Default | Notes |
|---|---|---|
| `OPTIMIZER_URL`, `OPTIMIZER_SHARED_SECRET` | `http://optimizer:8200`, — | `runtime/optimizer_client.py` (`/v1/impact`, `/v1/optimize`; HS256 service JWT); both are in `*engine-env` |
| `LLM_REQUEST_TIMEOUT_S` | `180` | see above. `runner_cache` falls back to 600 s only if the settings object has no such field (test doubles) |
| `RUN_LEASE_SECONDS` | `60` | worker lease per claimed run (`Worker(lease_seconds=…)`) |
| `WORKER_HEARTBEAT_INTERVAL_S`, `WORKER_CANCEL_POLL_INTERVAL_S` | `15`, `2` | lease renewal / cancel watcher |
| `WORKER_REAPER_INTERVAL_S`, `WORKER_MAX_ATTEMPTS` | `15`, `3` | expired leases re-queued as `crash_retry`; after `max_attempts` the run fails (`CRASH_LIMIT`) |
| `WORKER_IDLE_POLL_INTERVAL_S` | `5` | claim-loop fallback when no Valkey wake-up arrives |
| `WORKER_SHUTDOWN_GRACE_S` | `25` | SIGTERM: in-flight runs get this long, then they are left to the reaper |
| `WORKER_SNAPSHOT_BACKOFF_INITIAL_S`, `WORKER_SNAPSHOT_BACKOFF_MAX_S` | `2`, `60` | start-up Merxa tool-snapshot refresh retries (doubling) |
| `WORKER_CONCURRENCY` | `1` | runs executed in parallel by one worker |
| `WORKER_RUNNER_CACHE_SIZE` | `8` | LRU of `(App, Runner)` per tenant + spec version (`runtime/runner_cache.py`) |
| `WORKER_SCN_SYNC_ENABLED` | `true` | run the scn sync task beside the claim loop (`80` §1) |
| `WORKER_SCN_DUE_INTERVAL_S` | `5` | how often `due_syncs()` is asked; due syncs run one at a time |
| `WORKER_SCN_POLL_INTERVAL_S`, `WORKER_SCN_POLL_TIMEOUT_S` | `300`, `60` | tier-C `scn.changes_since` poll cadence / upper bound of one poll |
| `WORKER_SCN_NIGHTLY_HOUR_UTC` | `2` (0–23) | from this UTC hour the daily full resync is requested |
| `WORKER_SCN_SYNC_TIMEOUT_S` | `600` | upper bound of one `sync_connector` call in the worker (`asyncio.wait_for`); the platform's own `SCN_SYNC_TIMEOUT_SECONDS` also applies |
| `WORKER_SCN_GC_ENABLED`, `WORKER_SCN_GC_INTERVAL_S` | `true`, `10` | background snapshot GC task beside the claim and sync loops; idle cadence when nothing is marked |
| `WORKER_SCN_GC_BATCH_ROWS`, `WORKER_SCN_GC_BATCHES_PER_TICK`, `WORKER_SCN_GC_PAUSE_S` | `20000`, `25`, `0.05` | rows per delete statement (one short transaction), statements per pass (passes follow each other while work is left), pause between statements |
| `TAXONOMY_DIR` | `/app/taxonomy`, else the source tree's `data/taxonomy` | M4 event taxonomy (`events.yaml`) and material synonyms (`synonyms.yaml`), checked at worker start-up (`check_taxonomy`) |
| `GEO_DIR` | `/app/geo`, else the source tree's `data/geo` | map geometry, `chokepoints.json` and the region gazetteer `regions.json` (framing, `network_map`) |
| `MALLOC_ARENA_MAX` | `2` (compose, worker only) | fewer glibc malloc arenas: less retained heap during case runs and syncs |

`WORKER_*` variables are read by `runtime/settings.py` `WorkerSettings` (pydantic-settings, prefix `WORKER_`, case-insensitive), which is separate from `config.Settings`.

## Running

```bash
# whole stack (from Git Bash; strip the host LITELLM_MASTER_KEY that shadows .env)
env -u LITELLM_MASTER_KEY docker compose up -d --build bootstrap engine-api
# or: powershell -File scripts/up.ps1 -Services bootstrap,engine-api

# image only (context = repo root)
docker build -f services/engine/Dockerfile -t keelson/engine .
```

Bootstrap order (compose): `pg-core`, `litellm`, `kc-config`, `merxa-api` healthy → `bootstrap`
(migrations, tenants, LiteLLM aliases/teams/keys, connector rows, template packs, knowledge-base
profile + seed) → `engine-api` +
`engine-worker`. Packs (`pack:` per tenant in `infra/tenants.yaml`) are applied idempotently:
connectors of the pack the tenant does not have yet (tenants.yaml entries win) and agent spec `v1`
(built-in default + `spec_overrides`) when the tenant has no spec version — `demo-retail` gets
`value-fashion-eu`, `northwind-test` gets `blank`. Team limits = pack budgets < tenants.yaml <
`tenant.admin_overrides` (back-office changes survive re-runs); team/key model allow-lists =
models.lock aliases ∪ the aliases already granted (a re-run never revokes a back-office grant).
The Merxa **tool snapshot** is not taken by bootstrap: Keycloak validates Keelson's
`private_key_jwt` assertions against `http://keelson.localhost/api/.well-known/jwks.json`, which
only exists once engine-api runs. The worker calls `core.connectors.refresh_tool_snapshot` at
startup (E2); tenant/platform admins can call `POST /api/connectors/{slug}/refresh-tools`.

### End to end (real stack)

`e2e/api_thin_slice.py` drives the M1 thin slice through Caddy with the real Gemini models:
demo login → case → trim-delay message → approach confirm → review blocks → apply → human
approval with a scripted Connect-Merxa (Keycloak login form) → applied + verified, then reads
the change set, transfer orders and audit rows back from Merxa. It starts with a demo reset
(Merxa `scenario:reset` + `DELETE /api/connectors/merxa/link` for marco). See `e2e/README.md`
for the command and the timings it prints.

Runtime behaviour worth knowing when reading its timeline:
- A run shows its first stage as soon as it starts (`RUN_STARTED` + `STEP_STARTED`:
  chat/approach resume → `understanding`, apply → `preparing_changes`, approval resume →
  `awaiting_approval`); the intake LLM's first ADK event only arrives with its answer.
- The worker passes the claiming worker id (`run.lease_owner`) to `core.runs.finish_run`, so
  the lease guard is active.
- Tool results are stored in full as evidence, but the investigator's model sees a compacted
  copy (`runtime.plugins.compact_for_model`, 6,000 chars; long top-level lists cut to a prefix,
  with `_truncated` + `_note`), so JSON pointers into the evidence stay valid.
- The Strategist's brief lists each KPI placeholder with its meaning (`kpi_catalog`), including
  `exposure_*` keys for the exposure before the option.
- The Valkey client uses `socket_timeout=30 s` (redis-py 8 defaults to 5 s, which aborted every
  5 s `XREAD BLOCK` of the SSE tail and the worker wake-up).

## Design notes

### Row-level security and `system_session`
Every tenant table has `ENABLE` + `FORCE ROW LEVEL SECURITY` and one policy
`tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on'`.
`tenant_session(t)` = `BEGIN; set_config('app.tenant_id', t, true)`. `system_session()` =
`BEGIN; set_config('app.system', 'on', true)` — used only for cross-tenant infrastructure
(runs queue/leases/reaper, bootstrap, BFF/console/OAuth tables, platform audit rows).

Deviation from 55-engine-internals ("role `keelson_system` BYPASSRLS"): pg-core's init only
creates the login role `keelson` (`NOCREATEROLE`, `NOBYPASSRLS`), so the engine cannot create
a second role. Both session kinds use `keelson`; `FORCE` makes the policies apply to the owner,
and both GUCs are transaction-local (`is_local=true`), so they can never leak to the next user of
a pooled connection (tested). A connection without either setting sees no tenant rows.
Platform tables without a tenant column (`tenant`, `bff_session`, `oauth_flow`,
`console_ticket`, `console_session`, `schema_migrations`) have no RLS.

Foreign-key checks bypass RLS, so migration `0002_tenant_consistency` adds composite keys
`(tenant, case_id) → case(tenant, id)` (and likewise for interrupt, change set and plan
references): a tenant-A transaction can never attach a row to tenant B's case even if it knows
the id. They are added `NOT VALID` and then validated (a legacy inconsistent row only logs a
warning instead of failing bootstrap).

### Events (`core/events.py`)
`EventBus.append` validates the AG-UI event (+ `metadata.sc`), takes a transaction-scoped
advisory lock on the case stream (`lock_case_stream`; held until commit, so writers of one case
commit in `seq` order even when the API's cancel `RUN_ERROR` races the worker — a tailing reader
can never skip a lower `seq`), inserts `run_event` with `metadata.sc.seq = id` in one statement, then `XADD keelson:case:<id> MAXLEN ~ 1000` + `EXPIRE` (24 h).
`replay_then_tail` snapshots the stream tip, replays `seq > after` from Postgres, then `XREAD
BLOCK`s; a Valkey entry with a `seq` is only a wake-up (events are re-read from Postgres, so a
lost `XADD` or a Valkey restart never loses persisted events; idle timeouts also poll Postgres).
Live-only events (`persist=False`, reasoning deltas) are yielded with `seq = 0` and sent without
an SSE `id:` (they never move `Last-Event-ID`). `ReasoningCoalescer` (`bus.coalescer()`) sends
`REASONING_MESSAGE_CONTENT` deltas live only and persists one aggregated content event at
`REASONING_MESSAGE_END`.

### User-visible errors (`public_errors.py`)
Nothing a browser can see carries exception text (it can contain SQL, internal URLs, tokens in
query strings or other tenants' ids). Failures show a fixed message + an `error_id` (UUID4):
- `RUN_ERROR {message, code}`: `message` = generic text ending in "… error ID <id>", the id also
  in `metadata.errorId`; `code` is an enum-like constant only (`INTERNAL_ERROR`,
  `RUNTIME_UNAVAILABLE`, `INVALID_RUN`, `SESSION_NOT_FOUND`, `CONNECT_REQUIRED`,
  `BUDGET_EXCEEDED`, `CRASH_LIMIT`, `CANCELLED`; never an exception class name). Cancellations
  carry no id.
- `SUBAGENT_ERROR`: generic message + `metadata.errorId`; `code` = the ADK error code when it is
  an enum-like constant, else `SUBAGENT_FAILED`.
- API: unhandled exceptions → `500 {code: "internal_error", message, error_id}`; connector
  refresh failures → `502 {code, message, error_id}`.
- The exception (type, message, traceback) is logged at `error` level with `error_id=<id>`
  (`grep error_id=<id>` / JSON field `error_id`); `run.error` (server-side only, never returned by
  the API) is `"[<error_id>] <detail>: <ExceptionType>: <message>"`.

### Run attribution (`metadata.sc.runId`)
Every event the engine emits carries `metadata.sc.runId` (translator, `fail_run`, the API's
cancel `RUN_ERROR`, the reaper's `CRASH_LIMIT`/`CANCELLED`). AG-UI 1.0 `RUN_ERROR` has no
`runId`, so this is how a client tells which run failed and skips replayed events of earlier
runs. The field is optional in `ScMetadata` (old events have none).

### Case snapshot (`GET /api/cases/{id}`)
`repo.case_snapshot` reads `last_event_seq` **first**, then open interrupts, messages and blocks,
all in one `REPEATABLE READ READ ONLY` transaction (`tenant_session(..., snapshot=True)`). Anything
committed later has `seq > last_event_seq`, so a client that renders the snapshot and then follows
`/events?after=last_event_seq` cannot miss an interrupt (before: separate transactions, cursor read
last, an approval card committed in between was in neither).

### Graceful shutdown (`api/shutdown.py`)
uvicorn waits for open connections *before* the lifespan shutdown, so SSE streams must end on the
signal itself: the lifespan startup chains a `SIGTERM`/`SIGINT` handler in front of uvicorn's that
sets a shutdown event; `until_shutdown()` wraps the SSE tail and returns at once (the browser
reconnects with `Last-Event-ID`). The lifespan shutdown sets it too (fallback). Compose runs
engine-api with `--timeout-graceful-shutdown 5` (also the image `CMD`), which only bounds a stuck
response so `docker compose stop` (10 s) never needs `SIGKILL`.

### Runs (`core/runs.py`)
`run.id` = UUIDv7 = ADK session id for new runs; resume runs reuse the paused run's
`session_id` + the interrupt's `invocation_id`. `claim_next_run` picks the oldest queued run of a
case with no running run (`FOR UPDATE SKIP LOCKED`); the partial unique index
`run_one_running_per_case_uq` makes the per-case lease race-free. The reaper re-queues expired
leases as `kind='crash_retry'` (`origin_kind`, `invocation_id`, `session_id` kept; `RunRow.request()`
rebuilds the original `RunRequest`), or marks them `failed` after `max_attempts` and emits
`RUN_ERROR {code: CRASH_LIMIT}`. Extra helpers: `set_run_invocation(run_id, invocation_id)` (record
the ADK invocation as soon as it is known), `request_cancel`, `get_run`, `emit_run_error`.
`finish_run(..., worker_id=None) -> bool` never overwrites a final status (e.g. a run the reaper
already failed/cancelled) and, given `worker_id`, never touches a run whose lease another worker
now holds (a zombie worker cannot clear a live lease); it returns False when it changed nothing.

### Interrupts (`core/interrupts.py`)
`record_decision` (contract signature) / `record_decision_ex` (returns the resume run id).
Approve requires `change_set_hash` + `plan_version` equal to the interrupt's **and** to the case's
current state — the latest `plan.version` and, when the payload names a `change_set_id`, that row's
`hash` — else `StaleDecision` (409);
eligibility from `quorum_spec.roles` (+ `exclude_initiator`); approvals accumulate per person until
`count`; any other decision by an eligible person resolves at once. Resolution flips the interrupt
to `RESOLVED` and inserts `resume_request` + the `kind='resume'` run (`ResumePayload` with
`decided_by`) in the same transaction. Default quorum: approach `planner|approver|tenant-admin`,
approval `approver` × 1.
An approach **Edit** carries M1 free text or (M4) typed edits `[{key, value_int | value_ids}]`
(`ApproachEdit`) and/or `removed_checks`; `POST /api/interrupts/{id}/decision` validates them against
`ResumePayload` (typed edits / removed checks only with `edit`, unique keys and check ids → else
422 `invalid_decision`) and `record_decision_ex` passes them into the resume payload unchanged
(stored in `approval_decision.edits` as JSON). Whether an edit fits the card (editable key, kind,
bounds, one check left) is decided by `approach_router` against the stored card.
M5 quorum: see "Remediation depth (M5)" below.

### Tokens (`core/tokens.py`)
Service principals (reader/preparer/autocommit): client credentials + `private_key_jwt` +
`resource`, cached per `(tenant, connector, issuer, 'service', client_id)` until 30 s before expiry
(single request under concurrency). Delegated: the refresh token lives encrypted in
`connector_credential` (AAD `cred|tenant|connector|issuer|type|sub|field`); refresh takes the row
lock (`FOR UPDATE`), re-checks for an access token another process just stored, refreshes, stores the
rotated refresh token atomically. `invalid_grant` → link `expired` → `ConnectRequired(connect_url)`.
`user_token` re-reads the identity link on every call: the in-process cache is only used while the
link is `active` and still the same external identity (`ext_sub` + `linked_at`), so a link expired
or re-connected by another process is never served from a stale cache.

### Connector tool snapshot
See the module docstring of `core/connectors.py`. Every tool row carries the admin classification
(`access`, `content_trust`, `egress`, `idempotency` columns, mirrored into `classification`), the
**suggested** classification (contract 70 §3 rules), `status ∈ {needs_review, approved, disabled,
removed}` and `drift`. First-party connectors (Merxa) auto-approve suggestions (+ the explicit
`untrusted_tools`, default `list_supplier_messages`) and audit drift; everything else lands in
`needs_review` (changed tools keep the admin's classification, `drift=true`). M1's
`approval_gate` status became `approved` (migration 0003). `list_approved_tools` returns `approved`
rows of an **active** connector only; agents additionally need read + trusted_data.

## Back-office (M2, `docs/contracts/70-backoffice-m2.md`)

All `/api/admin/*` routes: session + CSRF + Origin (mutations), role `tenant-admin` or
`platform-admin`, tenant = `active_tenant`. A platform admin may pass `?tenant=<slug>` to act on
another tenant; each such request writes `admin.cross_tenant` to **that** tenant's `admin_audit`.
For anyone else a foreign `?tenant=` (and any foreign object) is 404. Errors are `{code, message,
...}`; request-body validation errors are `422 {code: "invalid_request", message, detail}`.

| Area | Routes (under `/api/admin`) | Notes |
|---|---|---|
| Agents | `GET/PUT /agents/spec`, `GET /agents/spec/versions[/{v}]`, `POST /agents/spec/restore`, `GET /agents/catalog` | `PUT {base_version, spec_patch}` = RFC 7386 merge patch → `201 v<n>` (`200 unchanged:true` when nothing changed); `409 version_conflict {latest_version}`; validates what differs from the latest version: chat aliases of the tenant team (tool-using roles need tool-loop signatures), added tools approved + read + trusted_data on an **active** bound connector, budgets ≤ platform max; raising `policy.auto_max_lines/auto_max_units` → `403 guarded_change` unless platform-admin (also changing `policy.approver_role`, lowering `policy.approval_quorum` and changing the planning `connector`, which must then exist and be active). Stored specs are full specs (runtime `AgentSpec` minus tenant/version) |
| AI Gateway | `GET /gateway/models`, `PUT/DELETE /gateway/models/{alias}` (platform), `GET/PATCH /gateway/tenant`, `POST /gateway/tenant/rotate-key`, `GET /gateway/usage?days=` | `core/gateway.py`; `PATCH` rpm/tpm ≤ platform max for tenant-admins, budget only platform-admin; rotate-key bumps `tenant_llm_key.key_version` and sets Valkey `keelson:tenant:<t>:llm_key_version`; rotations of one tenant are serialised (advisory lock) and each new key gets a unique alias `keelson-<t>-<stamp>-<rand>`, so no rotated key is ever left live but unstored |
| Connectors | `GET/POST /connectors`, `GET/DELETE /connectors/{slug}`, `POST …/test`, `GET …/tools`, `PATCH …/tools/{name}`, `POST …/activate` / `…/disable`, `GET …/events?limit` | URL policy + egress; secrets AES-GCM (`secret_enc`, AAD bound to tenant + connector), never returned; a `static_header` value must be visible ASCII (no CR/LF/control characters); a client-credentials token response with a malformed `expires_in` is cached 60 s (clamped to ≤ 1 day); `first_party`, `webhook_secret_ref` and `private_key_jwt` client auth (signed with Keelson's own `ca-1` key) only by platform admins; a `private_key_jwt` assertion audience must be on the `token_url` origin (checked at create and again before signing); a first-party drift never loosens a human-reviewed classification (per field the stricter wins); test steps `url_policy → reach → discover → auth → tools_list → tier_detect → classify`; activate needs a passing test; delete refused while the latest spec binds the connector |
| Platform | `GET /packs`, `GET/POST /tenants` (platform-admin) | `POST {slug, name, pack, admin_usernames}` → 201 (200 when re-run with the same pack, 409 another pack) |
| Audit | `GET /audit?limit&before&action&actor&target&scope=tenant\|platform` | keyset pagination (`next_before`) |

| Canonical store (M3) | `POST /connectors/{slug}/sync`, `GET /connectors/{slug}/snapshots?limit=` | `POST` → `202 {status: "queued", connector, state, current}`: makes the sync due **now** (no debounce, clears a failure back-off; audited `connector.sync`); the **worker** runs it (`scn_sync.due_syncs`). 404 unknown connector, 409 `connector_inactive` / `sync_unsupported` (no approved `scn.snapshot_open` + `scn.snapshot_export`). `GET` → `{connector, current, state, items}`: `items` newest first = `{id, status, source_snapshot_id, as_of, watermark, reason, row_counts, rows, bytes, duration_ms, error, created_at, ready_at, pins}`, `state` = `{stale_since, last_event_at, due_at, reason, attempts, retry_at, last_attempt_at, last_error, last_poll_at}` or null |

Webhooks: `POST /api/webhooks/{tenant}/{connector}` → 204 (duplicates too); 400 missing/invalid
headers or body, 401 signature/timestamp, 404 unknown/disabled/unconfigured, 413, 429. A newly stored
event whose type starts with `change_set.`, `po_line.`, `inventory.` or `sailing.`, **or a source reset**
(`scenario.reset`, any `merxa.*` event ending in `.reset` / `_reset`; contract 90 §10,
`scn_sync.is_reset_event`) marks the connector's canonical snapshot stale (`scn_sync.mark_stale`, reason
`webhook:<type>`, debounced; a failing mark never fails the delivery).

### Runtime helpers (E2 uses these)

```python
# keelson_engine.core.connectors
async def auth_headers(tenant: str, connector_slug: str, principal: str | None = "reader") -> dict[str, str]
    # principal: "reader" | "preparer" | "autocommit" (platform clients) or "user:<keelson_sub>" (delegated)
    # none -> {} ; static_header -> {<header_name>: <secret>} ; oauth_client_credentials ->
    # {"Authorization": "Bearer ..."} (client_secret or private_key_jwt, cached until exp-30 s) ;
    # oauth_platform_merxa -> Bearer from the TokenBroker. Raises ConnectorNotFound, ConnectorDisabled
    # (status != active), TokenError, ConnectRequired. Never log the result.
async def get_connector(tenant, slug) -> ConnectorRow         # any status; .usable .url .auth_kind .is_platform
async def get_active_connector(tenant, slug) -> ConnectorRow  # raises ConnectorDisabled unless active
async def list_approved_tools(tenant, connector) -> list[ToolRow]   # [] unless the connector is active
async def get_tenant_llm_key(tenant) -> str                   # 10 s in-process cache
async def get_tenant_llm_key_version(tenant) -> int           # bumps on every key change (rotate/regenerate);
                                                              # a changed version drops the stale cached key
def forget_tenant_llm_key(tenant) -> None                     # drop one tenant's cached key (runner rebuild)
def llm_key_version_key(tenant) -> str                        # Valkey key holding the same version
```
All connector traffic (MCP + token endpoints) goes through the egress proxy (`CONNECTOR_PROXY_URL`).

### Canonical store sync (M3, `core/scn_sync.py`) — for the runtime / worker

Contract `80-m3-deterministic-core.md` §1. Tables (migration `0004_scn`, all tenant tables with RLS FORCE):
`scn_snapshot` (one row per attempt: `id uuid`, `connector`, `source_snapshot_id`, `as_of`, `watermark`,
`status ∈ {loading, ready, failed, superseded}`, `reason`, `row_counts jsonb`, `bytes`, `duration_ms`,
`error`, `created_at`, `ready_at`; at most one `ready` per connector), `scn_current (tenant, connector) →
snapshot_id`, `scn_pin (tenant, snapshot_id, plan_id → plan)`, `scn_sync_state` (stale/due/back-off) and
`scn_<entity>` for the 15 entities of `25-scn-1.0.md` = `tenant, snapshot_id` + the `SCN_COLUMNS` in
contract order (text / bigint / double precision / date / text[]), PK `(tenant, snapshot_id, source_id)`,
indexes `(tenant, snapshot_id, <natural key>)` (e.g. `scn_po_line (…, po_code, line_no)` and `(…, sku_code)`,
`scn_demand_forecast (…, style_code, location_code, iso_week)`, `scn_inventory_position (…, location_kind,
location_code, sku_code)`, `scn_location (…, code, kind)`, `scn_item (…, code, kind)` / `(…, style_code)`).

```python
from keelson_engine.core import scn_sync

# reading (problem_scn): always scope to ONE snapshot id
snap = await scn_sync.current_snapshot(tenant, connector)   # SnapshotRow | None (None → M2 MCP fallback)
#   SELECT … FROM scn_demand_forecast WHERE tenant = :t AND snapshot_id = :id   (inside tenant_session(t))
#   snap.id, snap.watermark, snap.as_of, snap.row_counts, snap.status ('ready'), snap.to_json()
await scn_sync.get_snapshot(tenant, snapshot_id)             # any status, None if unknown / other tenant
await scn_sync.pin_snapshot(tenant, snap.id, plan_id)        # plan.id (bigint); idempotent; GC never deletes
                                                             # a pinned snapshot; SnapshotNotFound unless the
                                                             # snapshot is ready/superseded (e.g. GC'd, also
                                                             # when GC deletes it concurrently); PlanNotFound
                                                             # (LookupError) for an unknown / other tenant's plan
await scn_sync.unpin_plan(tenant, plan_id)                   # when the plan is closed (deleting the plan row
                                                             # also drops its pins)

# syncing (worker only — the case path never waits for a sync)
row = await scn_sync.sync_connector(tenant, connector, reason=reason)  # -> SnapshotRow (status 'ready')
#   raises SyncUnavailable (code not_found | connector_inactive | sync_unsupported; nothing recorded),
#          SyncInProgress (another process holds the lock), SyncFailed (.snapshot = failed row, .code);
#   all subclass ScnSyncError(.code, .message). On failure the previous snapshot stays current and the
#   connector becomes due again after the back-off.
await scn_sync.list_sync_connectors()     # [(tenant, connector)] active + approved snapshot tools
await scn_sync.request_sync(t, c, reason) # due now (worker start-up after the tool snapshot, admin)
await scn_sync.mark_stale(t, c, reason)   # debounced (the webhook receiver already calls it)
await scn_sync.due_syncs(limit=20)        # [DueSync(tenant, connector, reason, stale_since)] due now
await scn_sync.poll_changes(t, c)         # tier C: scn.changes_since(current watermark); True = marked stale
                                          # (any change or full_resync_required, or no snapshot yet);
                                          # SyncUnavailable without an approved scn.changes_since
```

Worker loop (E2 wires it in `worker.py`): at start-up, after `refresh_tool_snapshot`, call
`request_sync(t, c, "startup")` for every `list_sync_connectors()` entry; then every ~5 s run
`sync_connector(d.tenant, d.connector, reason=d.reason or "stale")` for each `due_syncs()` entry
(catch `ScnSyncError`; one at a time is fine), and every 5 min `poll_changes(t, c)` for connectors with tier
`C` (`ConnectorRow.tiers`). Run syncs in the worker (memory budget: contract S5 is engine-worker RSS).

Pipeline: `scn.snapshot_open(all 15 entities)` as the connector's **reader** principal
(`connectors.headers_for(conn, "reader")`; FastMCP client through `CONNECTOR_PROXY_URL`) → manifest checks
(`canonical_model`, every entity present) → `scn.snapshot_export` per entity (echoed snapshot/entity, part
count, rows and per-part sha256 must match the manifest; **every part URL must be on the connector's own
origin** — scheme, host and port of `mcp_url`, `keelson_contracts.scn.part_uri_allowed`) → parts streamed
through the egress proxy (no redirects, declared size enforced, sha256 + byte count verified) into a temp dir →
one transaction: per entity the Parquet columns must equal `SCN_COLUMNS[entity]`, each row is validated with
the `scn/1.0` row model (in a worker thread, 10k rows per batch) and binary-`COPY`'d into an `ON COMMIT DROP`
temp table, then `INSERT … SELECT` into `scn_<entity>`; loaded counts = manifest; FK sanity (every
`po_line.sku_code` is a `sku` item, every inventory `location_kind:location_code` is a location of that kind);
previous `ready` → `superseded`, new → `ready`, `scn_current` upsert → commit. GC (own transaction): **mark**
(`scn_snapshot.gc_requested_at`, migration `0008_scn_gc`) `superseded` snapshots beyond the newest
`SCN_SYNC_KEEP_SNAPSHOTS` ready/superseded ones unless pinned or current; keep the newest
`SCN_SYNC_KEEP_FAILED` failed rows. The marked snapshots' rows are deleted by the worker's background GC
task (`collect_garbage`, below), so a sync never waits for ~300k-row deletes. Failure codes in `scn_snapshot.error`
(`<code>: <detail>`, never a signed URL or a token): `auth_failed`, `connector_error`, `tool_error`,
`manifest_invalid`, `manifest_incomplete`, `export_invalid`, `export_mismatch`, `manifest_mismatch`,
`foreign_host`, `part_too_large`, `snapshot_too_large`, `download_failed`, `size_mismatch`,
`sha256_mismatch`, `parquet_invalid`, `schema_mismatch`, `row_count_mismatch`, `data_invalid`,
`fk_violation`, `timeout`, `cancelled`, `interrupted`, `internal_error`.

Design notes:
- **COPY + RLS.** Postgres refuses `COPY FROM` into a table with row-level security ("COPY FROM not
  supported with row-level security", also for the owner under `FORCE`), so rows are `COPY`'d into a session
  temp table and moved with one `INSERT … SELECT` that the RLS `WITH CHECK` policy validates.
- **No partial state.** Rows, counts, FK checks and the pointer flip are one transaction; the `loading` row is
  committed first (visible in the back-office) and marked `failed` (or `interrupted` by the next sync if the
  process died). The entity tables have no FK to `scn_snapshot` (an RI trigger per row would cost seconds);
  `scn_sync` deletes their rows (GC, connector deletion via `purge_connector`).
- **Single-flight.** In-process callers of the same `(tenant, connector)` share one run; across processes a
  session-level advisory lock (`hashtextextended('keelson:scn_sync|<t>|<c>')`) on a dedicated pooled
  connection → `SyncInProgress`. If the unlock does not complete (error, cancellation) the physical connection
  is terminated and dropped from the pool, so the session lock can never stay held by an idle pooled connection.
- **GC vs. pins.** Marking (`_gc` after a sync, `mark_noncurrent` in a demo reset) locks its candidate rows
  (`FOR UPDATE`), then marks only those still unpinned and not current in a new statement: a pin committed
  while it waited is seen; `pin_snapshot` reads the row `FOR KEY SHARE` and requires no mark, so a pin
  arriving after the lock waits and gets `SnapshotNotFound`. A marked snapshot is hidden from
  `get_snapshot` / `list_snapshots` at once. A pin is never dropped silently by the `scn_pin` cascade.
- **Background GC.** `scn_sync.collect_garbage(batch_rows=20000, max_batches=None, pause_s=0, tenant=None)
  -> GcProgress{snapshots, rows, batches, pending, skipped, done}` deletes marked snapshots oldest mark
  first: it probes which entity tables still hold rows of the snapshot, deletes them `batch_rows` at a
  time (`DELETE … WHERE ctid = ANY(ARRAY(SELECT ctid … LIMIT n))`, one short transaction per statement,
  `asyncio.sleep(pause_s)` between statements), then the registry row. Every statement runs under a
  per-snapshot advisory xact lock and re-checks that the snapshot is still marked and neither current
  nor pinned (else it is skipped with an error log, never touched). `max_batches` bounds one pass
  (`pending > 0`: call again); stopping half-way is safe (the mark stays until the registry row goes).
  The worker calls it through `ScnPort.collect_garbage` (optional) — see `WORKER_SCN_GC_*`: the next pass
  follows after `pause_s` only while work is left **and** the pass deleted something (a pass that found
  every marked snapshot locked by another worker waits `interval_s`). Marking locks candidate rows in id
  order (a sync GC and a demo reset never deadlock). Full-profile
  timing (`SCN_PERF=1 pytest tests/platform/test_m5_scn_gc.py -k full_profile`): 3 × 300k backlog rows,
  demo-reset row step 0.14 s, background GC 5 s in 2 passes.
- **Requests during a sync.** A `request_sync` (admin "Sync now", start-up) made while a sync runs stays due
  now after that sync finishes (the running sync read the source before the request); an event during the sync
  makes it due `debounce` after that event.
- **Stale marks.** `due_at = max(min(now + debounce, stale_since + max_delay), retry_at)`; a successful sync
  clears the mark unless an event arrived after the sync started (then it stays due). Failures set `retry_at`.
- **S5 preview**: see "Tests" below.

## Knowledge base (M4, `docs/contracts/90-m4-investigation.md` §7)

### Search — for the runtime (quarantine wrapper)

```python
from keelson_engine.core import kb

hits = await kb.search(tenant, query, kb_ids=None, top_k=6)
# -> [{"chunk_id": str, "document_title": str, "heading": str | None, "text": str, "score": float}, ...]
```

- Best first; `score` = cosine similarity (1 − pgvector `<=>` distance) rounded to 4 decimals. Only
  chunks of the tenant's documents with status `ready`; `kb_ids` (list of KB ids) narrows to those
  knowledge bases (unknown/invalid ids match nothing → `[]`); `top_k` is clamped to 1..50.
- The query (whitespace-normalised, ≤ 2000 chars) is embedded exactly like the chunks: LiteLLM
  `POST /embeddings` with the **tenant's** virtual key, `model` = the active profile's alias,
  `dimensions` = its dims. No ready document → `[]` without an embedding call.
- **The texts are untrusted (`content_trust=untrusted_text`).** Never give them to a model that has
  tools; only through the quarantine wrapper (screen → extractor → structured `UntrustedExtraction`).
  `data/kb/adversarial/` holds an injection fixture for evals (upload it explicitly).
- Raises `kb.KbError(status, code, message, **extra)`: `invalid_query` (422), `no_embedding_profile`
  (503), `llm_key_missing` (503), `embedding_failed` / `embedding_invalid` / `budget_exhausted` /
  `embedding_dims_mismatch` (502, `extra = {expected_dims, returned_dims}`), `profile_changing` (503,
  only if the profile was swapped three times during one search).
- The vector query runs under `pg_advisory_xact_lock_shared(KB_PROFILE_LOCK)` and re-checks the active
  profile, so a concurrent platform swap can never drop the table being read; when the profile changed
  while the query was being embedded, the query is embedded again for the new profile.
- `heading` is the Markdown heading path (`"12. Force majeure > 12.2 Notice"`; the document's own H1 is
  left out unless the chunk sits directly under it), `"Page N"` for PDFs, `None` for plain text.

### Tables and storage

`embedding_profile(id, alias, provider_model, dims, version, active, status building|active|retired|failed)`
(platform table, exactly one active), `kb(id, tenant, name, description)`, `kb_document(id, tenant, kb_id,
title, filename, mime, bytes, sha256, status processing|ready|failed, error, chunks, tokens, created_by,
created_at)`, `kb_chunk(id, tenant, document_id, ordinal, text, tokens, heading)`,
`kb_reembed_job(id, tenant|NULL, scope tenant|platform, from/to_profile_id, status, chunks_total/done, error)`
and per profile `kb_embedding_<id>(chunk_id → kb_chunk ON DELETE CASCADE, tenant, embedding)` with
`vector(dims)` for dims ≤ 2000 or `halfvec(dims)` up to 4000, an HNSW `vector_cosine_ops` /
`halfvec_cosine_ops` index and RLS FORCE. Search sets `hnsw.iterative_scan = relaxed_order` and
`hnsw.ef_search = 100` (transaction-local) so the tenant / KB filters do not starve the index scan.
pgvector is created by pg-core's init script (superuser; not a trusted extension); migration 0005 only
runs `CREATE EXTENSION IF NOT EXISTS vector`.

### Ingestion (`kb.ingest_document(tenant, kb_id, filename, data, title=None, created_by=None)`)

1. Type from the **extension** (`.md`/`.markdown`, `.txt`, `.pdf`), checked against the content (`%PDF-`
   header, no NUL bytes, UTF-8); ≤ `KB_MAX_UPLOAD_BYTES` (5 MB). PDFs via **pypdf** (one section per page;
   encrypted → `encrypted_pdf`, unreadable → `invalid_pdf`, > `KB_MAX_PDF_PAGES` → `too_many_pages`;
   no text, e.g. scanned → `no_text`). Refused uploads store nothing.
   **PDFs are never parsed inside engine-api** (`core/kb_pdf.py`): a 200 KB PDF of flate-compressed page
   operators cost pypdf 425 s of CPU and 937 MB in-process. The child `python -I -m
   keelson_engine.core.kb_pdf` gets the bytes on stdin, a minimal environment (no secrets), `RLIMIT_AS` =
   `KB_PDF_MEMORY_MB`, `RLIMIT_CPU` and a wall-clock kill at `KB_PDF_TIMEOUT_S`; one child at a time per
   process. pypdf's per-stream decode limits are cut to 4 MB (`kb_text.PDF_LIMITS`) and the text stops at
   `KB_MAX_CHUNKS_PER_DOCUMENT × KB_CHUNK_TOKENS × 4` characters (`too_many_chunks`). A killed / crashed /
   out-of-memory child → 422 `pdf_too_complex`.
2. Chunking (`core/kb_text.py`): sections at Markdown headings (not inside code fences); per section,
   paragraphs packed up to 600 estimated tokens (`ceil(chars/4)`), long paragraphs split into sentences →
   word windows → character windows; each further chunk of a section starts with the previous chunk's last
   ≤ 80 tokens of words. A chunk never spans sections. Embedded text = `title + heading path + text`.
   Extraction **and** chunking run in a worker thread (never on the event loop) and are linear in the
   upload size: headings are parsed without a backtracking regex, the packers keep running lengths, and
   the chunker stops as soon as the document exceeds `KB_MAX_CHUNKS_PER_DOCUMENT` (a 5 MB worst case
   takes about 3 s of CPU).
3. Idempotent by `sha256` per KB (advisory lock + unique index): the same bytes return the existing ready
   (or in-flight) document (`created=False`, API 200 `duplicate: true`); a `failed` (or abandoned
   `processing`) one is re-claimed with the same id and its chunks replaced.
4. Vectors for the active profile (required) and for the building profile of a **live** platform re-embed
   (running, progress within `KB_REEMBED_STALE_SECONDS`; best effort: a failure there is logged and the
   job's fill pass embeds it before the swap), written with the document's `ready` flip in one
   transaction under `pg_advisory_xact_lock_shared(KB_PROFILE_LOCK)`. A building profile left behind by a
   crashed job is ignored and discarded by the next platform job. Every returned vector must have
   exactly `dims` finite numbers. On an embedding failure for the active profile the document stays
   `failed` (`error` = code) with its chunks; the API answers 502/503 with `document_id`.

### Re-embed (`POST /api/admin/kb/reembed`)

- `scope: "tenant"` (tenant-admin): embeds the tenant's chunks that lack a vector in the active profile and
  flips repaired `failed` documents to `ready`.
- `scope: "platform"` (platform-admin; `alias?`, `dims?` default to the active profile's): new profile
  version (`building`) + shadow table → every tenant's chunks embedded with **that tenant's** key → swap
  under the exclusive advisory lock when no chunk of a ready/failed document is missing (else fill again,
  ≤ 5 rounds): old profile `retired` and its table dropped, new one `active`. Any failure marks the job
  `failed`, the building profile `failed` (table dropped) and keeps the old profile active.
- Bootstrap calls `kb.ensure_profile(alias, provider_model, dims)` from models.lock: the first run creates
  the active profile; a changed model/dims runs the platform re-embed synchronously (on failure the old
  profile stays and the error is logged). models.lock is the source of truth: a platform re-embed to
  another alias/dims through the API is re-embedded back to the models.lock spec by the next bootstrap.
- A platform job first discards any `building` profile (and its shadow table) left by a job that died;
  only one platform job runs at a time, so such a profile can have no live owner.
- Jobs run as background tasks of the API process (`kb.start_reembed`), are listed by
  `GET /api/admin/kb/reembed`, one running job per scope (409 `reembed_running`); a job without progress
  for `KB_REEMBED_STALE_SECONDS` is marked `failed: interrupted` by the next request; shutdown cancels
  them (`failed: cancelled`).

### Admin API (`api/routes_admin_kb.py`; the `/api/admin` rules above: role, tenant, CSRF, 404, audit)

| Route | Result |
|---|---|
| `GET /api/admin/kb` | `{items: [{id, name, description, created_by, created_at, documents, ready_documents, chunks}], profile}` |
| `POST /api/admin/kb` `{name, description?}` | 201 kb; 409 `name_taken`; audit `kb.create` |
| `GET /api/admin/kb/{id}/documents` | `{items: [{id, kb_id, title, filename, mime, bytes, sha256, status, error, chunks, tokens, created_by, created_at, updated_at}]}` |
| `POST /api/admin/kb/{id}/documents` (multipart `file`, optional `title`) | 201 document / 200 `duplicate: true`; 413 `file_too_large` (Content-Length and body capped **before** multipart parsing); 415 `unsupported_media_type`; 422 `invalid_pdf`/`encrypted_pdf`/`pdf_too_complex`/`invalid_text`/`no_text`/`too_many_*`/`file_required`; 502/503 embedding failure; audit `kb.document.upload` |
| `DELETE /api/admin/kb/{id}/documents/{doc}` | 204 (chunks + vectors cascade); audit `kb.document.delete` |
| `POST /api/admin/kb/reembed` `{scope?, alias?, dims?}` | 202 job; 403 `platform_admin_required` for `platform` by a tenant-admin; audit `kb.reembed` |
| `GET /api/admin/kb/reembed` | `{items: [job], profiles: [profile]}` |

### Seed (bootstrap)

`KB_SEED_DIR/<tenant slug>/*.md` is ingested into the tenant's `KB_SEED_NAME` knowledge base **only when
the tenant has no ready / in-flight document** (a deleted seed document is not re-added while others
remain). `data/kb/demo-retail/` holds five synthetic, clearly fictional documents (peak trading continuity
playbook; Meghna Trims supply agreement extract with notice / force-majeure clauses; freight & expedite
policy with sea-air premium caps; responsible sourcing code + approved near-shore suppliers F-TR-01 /
F-MA-01; DC-to-DC transfer SOP). `data/kb/adversarial/` (injection fixture) is never seeded (no such
tenant). Seed failures (no key, embedding outage) are logged, never fail the bootstrap; failed documents
are retried by the next run. Compose must mount `./data/kb:/app/kb:ro` on `bootstrap` (or set
`KB_SEED_DIR`).

### BFF (`api/`)
Session cookie `keelson_session` (HttpOnly, SameSite=Lax, Path=/); row id = HMAC(SESSION_SECRET,
cookie); tokens AES-GCM with AAD `bff|<id>`; idle (sliding) + absolute expiry. Mutating `/api/*`:
`Origin` must equal `PUBLIC_URL`'s origin (middleware) **and** `X-CSRF-Token` must match the session
(dependency). Login: code + PKCE S256, state/nonce server-side, flow bound to the browser with the
short-lived `keelson_login` cookie (prevents login CSRF), ID token verified (iss, aud, nonce,
signature), safe `next` paths only. Every tenant route resolves objects through RLS **and**
re-checks the tenant; other tenants' objects are 404. An open SSE stream re-checks its session at
most every 30 s (on the next event) and ends after logout / revocation.

Roles: read cases/blocks/SSE = planner, approver, compliance, tenant-admin; create/message/action/
cancel = planner, approver, tenant-admin; decide = any case role **holding a quorum role** of the
interrupt (M5: compliance co-approves four-eyes change sets);
refresh-tools = tenant-admin, platform-admin; console handoff = platform-admin.

### Demo persona switcher
`DEMO_MODE=1` only (else 404). With `PUBLIC_BASE_DOMAIN=localhost` it is open; otherwise
`POST /api/demo/login-as` needs `X-Demo-Access: <DEMO_ACCESS_SECRET>` (or a `demo-presenter`
session) and `ava` (platform-admin) is not offered. The password-grant token must carry `demo: true`
and the requested username. Never returns tokens; every switch is in `admin_audit`.

### Connect-Merxa
`GET /api/connectors/{slug}/connect?return_to=` → merxa realm authorize (`keelson-connect`,
`scope=openid offline_access changes:apply`, PKCE S256, `prompt=login`, `login_hint` =
`merxa_username` user attribute when known, `resource`, state + nonce bound to the BFF session and
Keelson sub). Callback exchanges the code with `private_key_jwt`, verifies the ID token
(iss/aud/nonce/signature) and the access token, stores the refresh token encrypted and upserts
`connector_identity_link`, then redirects to `return_to?connected=<slug>` (errors:
`connect_error=<code>`). Approve returns `412 {code: connect_required, connect_url, link_status}`
when the approver has no active link with `changes:apply`.

**Whose link counts — for the web.** The `approval_card` block's `connect` prop
(`{status, connect_url}`) is computed for the **initiator** when the card is created, and is
the same for every viewer; it must not decide whether the current viewer sees "Connect Merxa".
The source of truth is the decision API's pre-flight for the **person deciding**. Clients ask
for the same rule up front with:

```
GET /api/connectors/merxa/link   (cookie session; any tenant role)
200 {"status": "active"|"missing"|"expired", "ext_username": str|null,
     "roles": [str], "scopes": [str], "apply_ready": bool}
```

for the current user (never per case). `apply_ready` = `status == "active"` and `changes:apply`
in `scopes`, exactly the check behind `412 connect_required`
(`core.tokens.apply_ready`). Show Approve when `apply_ready`, else "Connect Merxa" →
`/api/connectors/merxa/connect?return_to=/cases/<id>`; on `412` follow `connect_url` (the race
where the link expired after the check). Re-fetch after returning with `?connected=merxa`.

**Disconnect** (demo resets, settings):

```
DELETE /api/connectors/merxa/link   (cookie session + X-CSRF-Token + Origin)
200 {"status": "missing", "revoked": bool, "idp_revocation": "revoked"|"failed"|"not_needed"}
```

Under the row locks: the refresh token is revoked at the merxa realm (RFC 7009
`revocation_endpoint`, `token_type_hint=refresh_token`, `private_key_jwt` as `keelson-connect`;
Keycloak also ends the offline session), the stored tokens are wiped, the link is marked
`revoked` (reads as `missing`) and `connector.unlink` is audited. Local revocation happens even if
the IdP call fails (`idp_revocation: "failed"`, logged). Idempotent.

### Console handoff (gateway./traces.localhost)
`GET /api/console/handoff?target=gateway|traces` (platform-admin) → 60 s single-use ticket →
`http://<target>.<domain>/__keelson/handoff?ticket=…` (Caddy routes `/__keelson/*` there to
engine-api without forward_auth) → host-only `keelson_console` cookie (8 h, server-side record) →
`/`. `/api/internal/forward-auth?role=` accepts `keelson_console` for the request Host or a
`keelson_session`; `GET /__keelson/logout` ends the console session.

## Framing, refine and follow-ups (M4, `runtime/nodes/`)

`docs/contracts/90-m4-investigation.md` §1, §5, §6, §8 and §10. `runtime/nodes/M4_NODES.md`
documents each node's input, output and routes for the workflow wiring.

The framing data is platform data:

* `data/taxonomy/events.yaml` maps each event type to perturbation templates, p10/p50/p90 priors,
  a duration prior and a TTR prior.
* `data/geo/chokepoints.json` lists chokepoint codes, aliases and an int-e6 polygon each.
* `data/geo/regions.json` (gazetteer) lists countries (ISO-2 / ISO-3, names, aliases) and multi-country
  regions; region mentions expand to the suppliers, factories, ports and DCs located there.
* `data/taxonomy/synonyms.yaml` maps material terms to aliases and item-code segments, so
  "wadding and zips" resolves to component items.

All four are loaded fail-fast. Call `taxonomy.check_taxonomy()` at worker start-up. Look-up order:
`$TAXONOMY_DIR`, `/app/taxonomy`, then the source tree; the chokepoints are read next to
`sea_routes.json`, and the gazetteer from `$GEO_DIR`, `/app/geo`, then the source tree's `data/geo`.

Nodes by module:

* `framing.frame_case` compiles any event type from its template, with no scenario-specific code.
* `framing.promote_case` turns a what-if into an actual case and re-simulates it on the current
  snapshot.
* `framing.confirm_assumptions` confirms unconfirmed assumptions, then re-runs impact and simulate.
* `refine.refine_scenario` applies fact-backed scenario updates and turns inference-backed ones into
  unconfirmed assumptions.
* `followup.make_followup_delta` classifies a follow-up (FakeLlm key `followup_classifier`) and
  computes a deterministic dirty set. `followup.explain`, `prepare_resimulate` and
  `prepare_reinvestigate` act on the result.
* `routing.route_intake`: the intake is told whether the case already has a plan and for which
  event (`case_brief`, set by `pre_route`). An intake `NEW_DISRUPTION` that names no entity, on a
  case with a plan, of the case's event family (or `OTHER`) only modifies the current disruption
  ("what if the delay doubles?") and goes to the follow-up classifier instead of re-framing the case
  (`modifies_current_case`).

Tests: `tests/runtime/test_m4_framing_*.py`, `test_m4_refine.py`, `test_m4_followup.py`. They are
offline and use FakeLlm and a full-shaped Act 2 ProblemData fixture (`test_m4_framing_support.py`).

The planner, the parallel specialists, quarantine and verify / critic are described in
`runtime/nodes/M4_INVESTIGATION.md`.

### M4 integration notes

* **Images.** The Dockerfile copies `data/taxonomy` to `/app/taxonomy` (the worker checks it at
  start-up) and the tenant KB seed `data/kb/demo-retail` to `/app/kb/demo-retail` (bootstrap ingests
  `/app/kb/<tenant>/*.md` into an empty tenant KB; the adversarial eval fixture never ships). The
  test stage copies both under `/src/data`.
* **Spec upgrade.** Bootstrap calls `agent_specs.upgrade_builtins` after the pack: built-in
  specialists that the tenant's spec **history** never had (M4: sourcing, logistics, playbook) are
  added as one new version, and budgets still at the M3 default (40 calls / 600k tokens) move to the
  current default. A specialist an admin removed later is never re-added.
* **Actions.** `POST /api/cases/{id}/actions` accepts `confirm_assumptions` with `assumption_keys`
  (required for it, refused for other types → 422 `invalid_action`). `compile_changeset` refuses an
  apply while the scenario has unconfirmed values ("Confirm before applying: …"), like a what-if.
* **What changed.** Follow-ups and case actions (promote, confirm) end with a `narrative@1` whose
  `what_changed` is one deterministic line: the stated changes, then units at risk and the
  recommendation against the superseded plan version (numbers from the plans, never from an LLM).
* **Agent rows.** The planner has its own row ("Planner") when its LLM runs (not for a single
  specialist or an Edit that reuses the plan). The recommender brief names levers in plain words.
* **verify_numbers.** A claim is a figure when it carries a value (placeholder, `calc_id`, free number,
  or a pointer plus a number in its text); a pointer on a qualitative claim is a citation: kept when it
  resolves (into untrusted text only at an item or a number), else dropped and counted as
  `dropped_citations`, outside the REVISE ratio. `/_truncated/<list>/total` (the size a model saw in a
  cut tool result) resolves to the stored list's length.

## Remediation depth (M5, `docs/contracts/95-m5-remediation.md`) — platform

### Quorum decisions (`core/interrupts.py`, `POST /api/interrupts/{id}/decision`)
* `interrupt.quorum_spec` is a `keelson_contracts.policy.Requirement` `{roles, count, exclude_initiator}`
  (the matched policy rule's requirement; unknown keys ignored, an unusable spec falls back to the
  default quorum). `approval_decision` stores the decider's **roles** (all of them) and the counted
  `role`; approvals are counted with `quorum_assign` (maximum matching): distinct people, one of each
  role when `count >= len(roles)` (four-eyes = an approver **and** compliance, never two approvers),
  the excluded initiator never counts, a second identical approve by the same person is acknowledged
  and not counted again, an approval that cannot advance the quorum is refused (`403` with
  `missing_roles`, nothing stored). Any `reject`/`revise` by a person holding a required role
  resolves at once; `reason` (1–500 chars, only with reject/revise) reaches `ResumePayload.reason`
  and `approval_decision.reason`. An excluded initiator may not decide at all.
* `DecisionOutcome.quorum` / the API body `quorum` = `QuorumView` (`approvals[{sub,name,role,at}]`,
  `remaining`, `missing_roles`); `GET /api/cases/{id}` adds `quorum` to open approval interrupts.
  The case's `approval_card@1` block for the interrupt is updated in place (`props.quorum`, or
  `props.rejection {sub, name, role, at, reason}` on reject/revise) + `ACTIVITY_SNAPSHOT`
  (label "1 of 2 approvals"); best effort.
* **Executor** = the approval counted under `approver` (`quorum_executor`), whatever the order; it is
  first in `ResumePayload.decided_by`, the co-approvers follow in approval order. The approve
  pre-flight checks the Merxa link (`apply_ready`) **only for the executor**: `412 connect_required`
  when that is the decider, `412 executor_connect_required {executor}` when a co-approver completes the
  quorum but the executor's link is gone (nothing recorded; retry after they reconnect). Compliance
  never needs a link.
* Receipts: `core.crypto.quorum_receipt_claims` / `sign_quorum_receipt(decided_by=…, ext_sub=<executor's
  Merxa sub>, …)` → executor = `approved_by`/`approver_name`/`keelson_sub`, others →
  `co_approved_by: ["user:<username>"]`; a single approver yields the exact M1 claim set
  (`split_quorum` for the executor/co-approver split).

### Policies (`core/policies.py`, `/api/admin/policies`)
* Rules live in the versioned agent spec: `policy.version` + `policy.rules` (runtime
  `PolicyConfig.rules`; `None` = the defaults built from the caps). `effective_policy(section)` is the
  rule set the worker applies (`runtime.policy.policy_set`, else `DEFAULT_RULES`). The pack
  `value-fashion-eu` stores the doc §1 set explicitly as `p-2` (refuse-unconfirmed 10, four-eyes 20,
  human-untrusted 30, auto-small-transfer 40, default 50 — four-eyes before human-untrusted so a webhook
  re-plan creating POs still needs two people).
* `GET /api/admin/policies` → `{spec_version, policy {version, rules}, source: spec|default, editable,
  evaluator_available, fallback, defaults}` (tenant-admin). `PUT {base_version, rules, version?}` → new
  spec version (`201`; `200 unchanged`), the policy version bumps when the rules change (`p-2` → `p-3`;
  an explicit unchanged version with changed rules → `422 policy_version_unchanged`), `409
  version_conflict`, `422 policy_invalid` (shape or the evaluator's CEL check per rule, `errors[].loc =
  rules.<id>.when`). **Guards** (`403 guarded_change {fields, reasons}` unless platform-admin, also
  enforced by `PUT /agents/spec`, including changes of the default caps): new or wider auto rules
  (raised caps), auto rules moved ahead of others, refuse rules removed / narrowed / turned into
  approvals, approval rules removed / narrowed / weakened (fewer people, other roles, initiator no
  longer excluded), new, moved or widened approval rules that would take precedence over a refuse rule
  or a stricter approval rule. Numeric-only edits are classified by comparison direction (`condition_change`);
  anything else counts as a change.
* `POST /api/admin/policies/test {context: PolicyContext, rules?, version?}` → `{policy_version,
  decision, approved_by, matched_rule, floors {must_refuse, auto_allowed}}`; `503
  policy_evaluator_unavailable` without an evaluator, `422 context_invalid`.
* **Evaluator port**: `core.policies.register_policy_evaluator(evaluate_policy, check=check_rule)` with
  `evaluate_policy(rules: PolicySet, context: PolicyContext) -> PolicyDecision` (sync or async; must
  raise on CEL errors) and `check_rule(when) -> [problems]`. engine-api registers
  `keelson_engine.runtime.policy.evaluate_policy` / `check_rule` at start-up
  (`install_runtime_evaluator`, also tried lazily); the worker's runtime registers itself the same way.
  `core.policies.evaluate` re-applies the hard floors to whatever the evaluator answers (a what-if /
  unconfirmed context is refused; an auto decision for a non-chat / tainted context raises
  `PolicyEvaluationError`, fail closed).

### Re-plan routing (`core/replan.py`, `core/signals.py`)
* `plan_dependency(plan_id, kind lane|receipt|connector|dc, ref, source plan|change_set|runtime)`:
  `repo.save_plan` extracts lanes (`lane_code`, `alt_lane_code`, `allowed_lanes`, DC pairs `FROM>TO`),
  receipts (`po_line_ref`, `replaces_po_line_ref`, `receipt_ref`, `PO#line` `target_ref`s) and connectors
  from the options (re-saving replaces them); `repo.create_change_set` adds what the items touch plus
  the connector; `repo.save_plan_dependencies(case_id, plan_version, {lanes, dcs, po_lines, connectors,
  committed_lanes})` indexes the runtime fingerprint. `repo.list_plan_versions` → `[{version, status}]`.
* A newly stored webhook of a type in `REPLAN_EVENT_TYPES` (`lane.capacity_changed`: structured
  `data.lane_code` + integer `data.factor_bp` (+ `weeks`, typed `origin`/`dest`), never free text) finds
  the cases whose **latest** plan is open/committed (not rolled_back/superseded/…, case not closed) and
  depends on the lane — by its code (`matched_by` `lane`), by the transfer pseudo-lane `ORIGIN>DEST` of
  the lane's (origin, dest) pair (`lane_pair`: plans record DC-to-DC transfers as `FROM>TO`, Merxa's lane
  events carry codes like `DE1-NL1-RD`; the pair is the lane row's `origin_code`/`dest_code` in the
  connector's current scn snapshot when it has the lane — authoritative: an event pair contradicting it
  is ignored and dropped from the signal — else the event's typed `origin`/`dest` or
  `origin_code`/`dest_code`; `replan.lane_pairs`), or through a
  receipt whose sailing runs on the lane in that snapshot (`receipt`); plans pinned to another connector
  are skipped — and enqueues one `signal` run per case:
  `payload {signal_id}` → `case_signal` row; actor `webhook:<connector>` with **no roles** (the policy
  floor never auto-approves non-chat runs anyway). Idempotent per (case, event), so duplicate deliveries
  are routed again: a routing error answers `503 replan_routing_failed` (the event is stored) and the
  sender's retry with the same `webhook-id` routes it (at-least-once; a transient failure never loses
  the re-plan). `signals.get_signal(tenant=, signal_id=)` → `{id, case_id, connector, event_id, type,
  kind, data: {event_id, kind: "replan", lane_code, factor_bp, weeks, origin?, dest?, connector,
  event_type, plan_version, matched_by, plan_lane_code? (the pseudo-lane, whenever the pair matched)},
  received_at, run_id}` (falls back to a stored connector event by id /
  webhook-id). `GET /api/cases` items carry `last_signal_at` ("Updated" badge).

### Guide mode (`core/checklists.py`)
* Tables `checklist` (+ `meta`, `block_id`, `status open|completed|cancelled`, poll bookkeeping) and
  `checklist_step` (`remediation_checklist@1` step fields + `matched_ref`, `updated_by`).
* Runtime port (`runtime.deps.ChecklistsPort`): `create_checklist(tenant=, case_id=, checklist_id=,
  connector=, plan_version=, option_id=, steps=, meta=)` (idempotent on the id), `get_checklist` (steps as
  exact `ChecklistStep` JSON), `list_open_checklists(tenant=None)`, `set_checklist_step(tenant=,
  checklist_id=, seq=, status=, verified_at=, evidence_id=, actor=)` (verified is final; completes the
  checklist when every step is verified/skipped), `set_checklist_status`. `checklist_props` /
  `attach_block` for the `remediation_checklist@1` block.
* `GET /api/cases/{id}/checklists` (case readers) and `POST /api/cases/{id}/checklists/{cid}/steps/{seq}
  {status: done|skipped}` (planner/approver/tenant-admin; `admin_audit checklist.step_<status>`; 409
  `already_verified`; block refreshed + `ACTIVITY_SNAPSHOT`).
* `verify_checklists(tenant, connector, event=None)`: reads the expected entities back with the
  connector's approved read tools **as the reader principal** (`READBACK_HINTS` e.g. `po_line` →
  `get_purchase_order` lines; `get_entity` for a known ref **only when its `type` enum lists the entity
  type**; else `list_<entity_type>s` with the scalar `match` fields its schema accepts, keeping only the
  named row for a known ref; a listed item without some `match` field is read in full through
  `get_<entity_type> {code}`), JSON-subset match (`qty` may be `qty_ordered`/`qty_units`; an entity with
  `lines` also matches through its own fields + one line, e.g. a shipment of a PO line). **Create** steps
  (`entity_ref` null): one entity per step, never one in the runtime's `meta.baseline`, created before the
  checklist or created by an applied change set (`created_via: change_set:…`). **Edit** steps (named
  entity) verify however old the entity is. A match → `verified` + evidence row (`prov fact`) + block
  refresh. A completed checklist reopens when a step goes back to pending/done (a skip undone).
  `poll_checklists(interval_s=None)` (**the worker should call it every few seconds**): verifies each
  `(tenant, connector)` with an open checklist that was not checked for `CHECKLIST_POLL_SECONDS` or got a
  connector event (webhook) since. Event content is never trusted (it only triggers the read-back).
* `POST /api/cases/{id}/actions {type: "rollback", change_set_id}` (M5 Undo): the change set must
  belong to the case and be `applied`/`verified` (`409 not_undoable`); one Undo per change set at a time
  (`409 rollback_pending {run_id}` while a rollback run is queued/running or waits on an open approval;
  the same `Idempotency-Key` still answers the first run); enqueues the action run.

### Demo reset (`core/demo_reset.py`, `POST /api/demo/reset`, `scripts/demo-reset.(ps1|sh)`)
DEMO_MODE only (404), platform-admin or a demo-presenter **who is a member of the demo tenant** (403;
realm roles are global), CSRF + Origin; body `{keep_consents?}`;
`200 DemoResetResult` step log (also with failed steps, `ok: false`), `409 reset_in_progress` (Valkey
lock). Steps in `DEMO_RESET_STEPS` order, each timed, a failure never stops the rest:
`merxa_scenario_reset` (Merxa REST `demo/login-as` as `merxa.admin` → `scenario:reset` with
`Idempotency-Key` → logout), `keelson_rows` (the demo tenant's cases and everything cascading from them,
connector events older than the reset; non-current scn snapshots are only **marked** for the worker's
background GC — count `scn_snapshot_gc` — so the step stays well under a second with any backlog; kept:
connectors, tools, KBs, `llm_replay`, audit), `adk_sessions` (`events`/`sessions`/`app_states`/`user_states` of `t_<tenant>` in
`ADK_DATABASE_URL`), `valkey_keys` (deleted cases' streams, webhook rate-limit keys), `merxa_consents`
(Keycloak admin API in each platform connector's realm: consents + offline tokens of the linked users and
`DEMO_MERXA_USERS` for the connect clients), `token_broker` (delegated credentials + identity links),
`agent_spec` (`agent_specs.reset_to_default`: the pack default; an equal stored version becomes latest
again, so version names — part of LLM replay keys — stay stable), `sim_clock` (restored by Merxa's
golden seed), `scn_resync` (`request_sync` for every sync-capable connector). `keep_consents` skips the two
consent steps (rehearsal). `scripts/demo-reset.ps1 [-KeepConsents]` / `scripts/demo-reset.sh
[--keep-consents]` sign in as `ava` through the persona switcher and print the log.

### M5 integration notes (for the runtime / worker)
* Worker: call `core.checklists.poll_checklists()` every few seconds (cheap when nothing is open) — or
  its own `runtime.guide.verify_open_checklists` — and register the CEL evaluator at start-up.
* `commit_nodes` must take the executor from `ResumePayload.decided_by` (first entry = `approver`
  member) and accept co-approvers with other roles; mint receipts with `crypto.sign_quorum_receipt`.
* Signal runs: `RunRequest {kind: "signal", payload: {signal_id}}`, details via `signals.get_signal`.
* (integrator) `lane.capacity_changed` signals carry `{lane_code, factor_bp, weeks, from_week?, to_week?,
  weeks_detail?: [{iso_week, weekly_capacity_before?, weekly_capacity?, booked_units?, overbooked_units?}]}`
  — typed values only (free text such as `carrier_note` never reaches `case_signal`). A DC-to-DC
  transfer is fingerprinted as `FROM>TO`, not as Merxa's road-lane code: routing also matches it by the
  lane's (origin, dest) pair (`matched_by` `lane_pair`, signal `plan_lane_code`, M5 polish — see the
  re-plan routing section above), and the run re-plans the `FROM>TO` lane. All lanes of one DC pair
  share that one transfer lane, so a cut on any of them scales the pair's transfer capacity.

## Signal Hub (M6, `docs/contracts/100-m6-signals.md` §6) — platform

The hub owns signals, events, exposure and alerts; the engine talks to it only through
`core/signal_hub.py` (internal network, no egress proxy) with a **service JWT** per call
(`mint_hub_token`: HS256 `SIGNAL_HUB_SHARED_SECRET`, `iss keelson-engine`, `aud signal-hub`, `tenant`,
60 s). The tenant claim is always the tenant of what is being served: the session's active tenant, the
webhook **path** tenant, the synced tenant, the demo tenant. Ids that go into hub paths are validated
with `fullmatch` (canonical lowercase UUIDs, pack ids) — a malformed id is a 404 without a hub call.
Hub endpoints used: `GET /v1/alerts?status=&limit=` (a list or `{items}`), `GET /v1/alerts/{id}`
(`AlertDetail`), `POST /v1/alerts/{id}/status` (`AlertStatusUpdate`), `GET /v1/events/{id}`,
`PUT /v1/tenants/{tenant}/network` (`TenantNetwork`), `PUT /v1/tenants/{tenant}/alert-rules` (`{rules}`),
`POST /v1/ingest/connector-event` (the hub's `ConnectorEvent` = the webhook envelope
`{connector_id, type, id, timestamp, data}` + `event_id`/`received_at` for tracing),
`GET /v1/demo/packs` (+ `GET /v1/demo/packs/{pack}` state), `POST /v1/demo/packs/{pack}/{start|advance|reset}`,
`POST /v1/demo/reset` (the tenant's packs and the rest of its hub state; 404 -> per-pack resets). Responses are contract-validated; rows of another tenant are dropped; a 3xx is never
followed nor taken as an answer (503).

* **Tenant network** (`core/network.py`): after every successful scn sync (`scn_sync._sync_once` ->
  `network.on_sync_ready`, best effort, 60 s cap) the tenant's current snapshots become
  `TenantNetwork` nodes (supplier, factory, port (+ airport as port), dc, stores aggregated to clusters
  as type `store`) and lanes (stops drawn with `data/geo/sea_routes.json` via `GeoCatalog.stops_path`,
  float `[lon, lat]`). Positions are the snapshot's own; suppliers/factories exported without
  coordinates (Merxa) sit at their factory / the tenant's port in the same country (the one most lanes
  start from) / the country centroid; ports without coordinates use the catalogue place. Codes longer
  than 120 chars are skipped (never truncated). The same hook pushes the tenant's own `alert_rules`
  (`[]` = the hub's defaults) so the hub's alerts and the engine's auto-open use one rule set.
* **Webhook forwarding** (`core/hub_ingest.py`): after a verified delivery is stored, the configured
  types are posted to the hub inline (3 s); a failed forward stays pending
  (`connector_event.hub_forwarded_at IS NULL`) and is retried by the alert watcher (24 h, 10 tries); a hub
  refusal (4xx) is not retried. The delivery answers as before (204 / 503 for re-plan routing).
* **Internal connector**: bootstrap registers `signal-hub` for every tenant (`auth_kind service_jwt`,
  `first_party`, `mcp_url <SIGNAL_HUB_URL>/mcp`, active). `connectors.headers_for` mints the service JWT
  **only** when the row's URL is on the `SIGNAL_HUB_URL` origin; the back-office cannot register this
  kind (422), its slug is reserved for `POST /api/admin/connectors` (422 `reserved_slug`), bootstrap never
  adopts a tenant connector that already holds the slug (logged), and only a platform admin may delete
  it. Tool snapshots (taken by the watcher every 15 min,
  directly, not via egress): read-only tools are auto-approved, any other tool needs review;
  `search_signals`, `get_event`, `hazards_near_node` are `untrusted_text` (`config.untrusted_tools`),
  `assess_exposure` (structured exposure) `trusted_data`.
* **Alerts API** (`api/routes_alerts.py`): `GET /api/alerts?status=&severity=&limit=` ->
  `{items: [Alert], new_count}`; `GET /api/alerts/count` -> `{new}`; `GET /api/alerts/{id}` ->
  `AlertDetail` + `map` (the drawer's `network_map@1` `{props, data}` from `core/alert_map.py`: the event's
  polygon footprint, every exposed site at the tenant network's own position — `disrupted` when inside
  the footprint / named by a connector event, `at_risk` when only within the kind radius — and every
  exposed lane along its catalogued path with its endpoints; framed on the footprint + exposed sites;
  codes, enums, coordinates and the tenant's own site names only; network cached 60 s per tenant;
  `null` on any problem, then the web draws the footprint alone); `POST /api/alerts/{id}/open-case` -> `{alert_id, event_id, case_id, run_id, created, auto}`;
  `POST /api/alerts/{id}/dismiss|snooze|seen` -> `Alert`. Read roles for reads and `seen`; planner,
  approver, tenant-admin for open-case/dismiss/snooze; another tenant's alert is 404; hub down -> 503
  `signal_hub_unavailable`. A case opened here wins over a hub status not yet updated
  (`status opened`, `case_id`).
* **One case per alert** (`core/alerts.open_case`, table `alert_case` PK `(tenant, alert_id)`, advisory
  lock): case + queued `signal` run `{event_id, alert_id}` (idempotency key `alert:<id>`) + mapping in one
  transaction, then `opened {case_id}` to the hub (retried by the watcher while `hub_synced = false`). The
  case title is deterministic from the event subtype + exposure counts ("Tropical cyclone alert — 2 sites,
  2 lanes exposed (simulated)") — never the (possibly news-derived) alert title. A hub refusal of the
  `opened` status (e.g. 409, another case) is final (logged, not retried every poll). `GET /api/cases/{id}`
  lists the case's queued runs that no user message names as `pending_runs: [{id, kind}]`, so the web
  shows the signal run of a case opened from an alert as a waiting turn ("Getting started…", Stop) until
  it starts.
* **Live channel — the engine polls the hub** (the contract defines no hub -> engine callback, and the
  hub then needs no engine address or reverse credential). `AlertWatcher` (engine-api lifespan, one task
  per process; a Valkey lock per tenant so one replica polls per interval; paused while a demo reset runs)
  polls each tenant with an active `signal-hub` connector every `ALERTS_POLL_SECONDS`, diffs against a
  Valkey state hash and appends AG-UI `CUSTOM` events to `keelson:alerts:<tenant>` (MAXLEN ~500, TTL 24 h):
  `sc.alert.raised {alert, renotify}` (new; or `renotify: true` when the severity band rose),
  `sc.alert.updated {alert}`, `sc.alert.removed {alert_id}`, `sc.alert.count {new}`,
  `sc.alert.case_opened {alert_id, case_id, run_id, auto}`. The first poll on an empty state only primes
  it (one count event). `GET /api/alerts/events` (read roles) sends `retry: 3000`, one fresh
  `sc.alert.count`, then tails the stream (`id` = stream entry id; `Last-Event-ID` resumes), keep-alives
  every 15 s, ends on logout (30 s re-check) and at shutdown. The events carry no `metadata.sc` (they
  belong to no case).
* **Auto-open** (`alert_and_draft_case`): `Alert` carries no action, so the watcher evaluates the
  tenant's rules itself (`decide_alert_action` on the event's severity/confidence/kind + the alert's
  exposure counts; rules = `tenant.config.alert_rules` (tenants.yaml), invalid -> `DEFAULT_ALERT_RULES`),
  once per alert version, only for `new` alerts without a case, and only when the event is backed by a
  structured signal (a member with provenance `fact`/`simulated`, or an anchor): a news-only event
  (every member an LLM `inference`) raises its alert but a person opens the case. The case/run belong to
  `system:signal-hub` (no roles: never auto-approved; the run stops at the approach card).
* **Demo** (`api/routes_demo.py`): `GET /api/demo/packs` -> `{packs: [{id, title, description, status,
  steps_applied, steps_total}]}` (the hub's pack list + each pack's replay state for the demo tenant;
  same role guards; the persona menu's Demo scenarios dialog) and `POST /api/demo/packs/{pack}/start|advance|reset` (DEMO_MODE,
  platform-admin or demo-presenter of the demo tenant, CSRF; optional JSON object body <= 4 KiB passed
  through; NaN/Infinity/overflowing numbers -> 422; hub 404 -> 404, refusal -> 409, down -> 503). The demo reset runs a `signal_hub` step
  (`POST /v1/demo/reset`, or every pack's reset on a hub without it) right after `merxa_scenario_reset` (before the cases are deleted); `keelson_rows`
  deletes `alert_case` with the cases and `valkey_keys` the tenant's alert keys (count `alert_keys`).
* **For the runtime** (`signal` runs `{event_id, alert_id}`): `core.signals.get_hub_event(tenant=,
  event_id=, alert_id=)` -> `{event, alert, exposures, signals}` (contract JSON; `None` = unknown for this
  tenant). Every title/summary/why/raw_text in it is untrusted text.

## Back-office M7 (`docs/contracts/110-m7-backoffice-generic.md` §1, §2, §4–§6) — platform

### Four-eyes change requests (`core/change_requests.py`)

Every guarded change of plan §17 raises `GuardedChange` in its core function instead of 403 (agent
spec + policy caps/quorum/approver, refuse/never-auto rules, write→read / untrusted→trusted tool
reclassification, `auto` binding on a write tool, activating a connector with external-egress tools,
first-party trust, connector / KB delete, the platform embedding profile, and — beyond §1 — a
tenant-admin granting `tenant-admin`, kind `user_roles`). The API answers **202** with the
`AdminChangeRequest` (header `X-Keelson-Change-Request`); nothing is applied.

| Route | |
|---|---|
| `GET /api/admin/change-requests?status=&kind=&limit=` | queue + `pending` count (pending requests past 72 h flip to `expired`, audited) |
| `GET /api/admin/change-requests/{id}` | one request (other tenants: 404) |
| `POST …/{id}/approve {reason?}` | a **different** tenant-admin of the tenant, or a platform-admin (`?tenant=`); re-runs the core function `cosigned` against the current state → `applied` (`approved` for the async re-embed); `409 stale_base` when the target moved (request stays pending): the agent spec / policy set version, or — for connector, tool and KB kinds — the `state:<hash>` fingerprint of what the requester saw (server URL / issuer / auth settings; the tool's definition hash, classification, status and binding; the approved external-egress tools of an activation; a KB's name + document count); any other refusal is returned as-is (stays pending). An approver session without a subject never decides |
| `POST …/{id}/reject {reason}` | reason required; the requester may withdraw (reject) but never approve |

Payloads/diffs never carry credential-like keys. `embedding_profile` needs a platform-admin. At most
100 pending requests per tenant (`429 too_many_pending_changes`).
Audit: `change_request.created|approved|applied|rejected|expired` + the applied change's own row.

### Users and tenants (`core/users.py`, `core/keycloak_admin.py`, `core/tenants.py`)

`GET|POST /api/admin/users`, `PATCH|DELETE /api/admin/users/{id}` (Keycloak realm `keelson`, users
holding the tenant in the multi-valued `tenant` attribute; only `PRODUCT_ROLES` are read/written).
Create = tenant attribute + random temporary password + required action `UPDATE_PASSWORD`; Keycloak
e-mails the set-password link when it can (`execute-actions-email`), the password is never returned,
logged or stored. A tenant-admin never grants/revokes `platform-admin`, never touches a
platform-admin's account (checked on the **effective** realm roles, so a platform-admin by group
membership is covered too) or a user who also belongs to another tenant (403); granting
`tenant-admin` is a `user_roles` change request; platform-admins apply directly. Role changes and
disabling end the user's BFF + Keycloak sessions. `DELETE` disables. `POST /api/admin/tenants/{slug}/suspend`
(platform-admin) blocks logins (the tenant never becomes a session's tenant), ends its sessions and
answers `403 tenant_suspended` on every tenant-scoped call; `…/reactivate` (alias `…/resume`).

### Tier A: mappings, crawl, dry-run, per-op ledger

| Route | |
|---|---|
| `GET\|PUT /api/admin/connectors/{slug}/mappings/{read\|action}` | `PUT {base_version, mapping}` → new version (201) / unchanged (200) / `409 version_conflict` / `422 mapping_invalid` (`errors` = contract, `problems` = catalogue: crawl sources must be approved trusted **read** tools; action tools approved write tools with at least the claimed idempotency; read-backs read tools) |
| `GET …/mappings/{kind}/versions[/{v}]` | history |
| `POST …/mappings/from-pack {pack, kinds?}` | install a pack's mapping files (`pack.yaml` `mappings`) as new versions |
| `POST …/mappings/read/test {mapping?, max_pages≤3}` | crawl ≤ N pages per source, map, sample 5 rows per entity mapping; stores nothing |
| `POST …/mappings/action/dry-run {op, target_ref?, expected_version?, payload, run_readback}` | renders the exact tool call (never executed) + runs the read-back (read tool, reader principal) |
| `GET\|POST /api/admin/connectors/{slug}/crawl` | crawl status (`idle\|queued\|running\|succeeded\|partial\|failed`, label, as-of range) / queue a crawl sync |
| `GET /api/admin/connectors/{slug}/capabilities` | tiers + mapping versions → sync / commit mode / advisory |

`scn_sync` crawls a connector that has an active read mapping and no tier-B snapshot tools
(`core/scn_crawl.py`): one MCP session with the reader credentials via the egress proxy; `source`
lookups first (one paging run per distinct source), then each entity mapping (page numbers or
cursor, ≤ `max_pages`, truncation recorded); rows validated with the scn/1.0 models and loaded in
5,000-row batches; FK sanity, `ANALYZE`, supersede + pointer flip in one transaction with
`consistency = 'crawled'`, `as_of_from/as_of_to` (first/last page) and `crawl = {pages, rows_seen,
truncated, lookups}`; a failure purges the rows written so far and keeps the previous snapshot
(failure codes in the module docstring). The mapping is re-checked against the catalogue before
every crawl. `SnapshotRow.label` / `.advisory` mark plans on crawled data as advisory. Expressions
are evaluated by `core/mapping_eval.py` (hand-written interpreter over the contracts' AST: no
`eval`, values are plain JSON, paths index dict keys / list positions only). Merxa `/mcp-lite`
at demo scale is ~300k rows / ~1,500 calls (well inside `SCN_SYNC_TIMEOUT_SECONDS`).

Per-op commit ledger (runtime writes, platform stores): `repo.ledger_record(CommitLedgerEntry)` /
`repo.ledger_entries(change_set_id)` — states `attempted|succeeded|in_doubt|failed` (+ `verified`),
`attempts`, `request_id`, read-back evidence, error; the contract model enforces the retry rules.
A settled line never moves back (`succeeded` → only `succeeded`/`verified`; `verified` stays) and
`attempts` never decrease: such a write raises `repo.LedgerRegression` (a stale writer cannot erase
the evidence that a line applied). `connector_mappings.get_action_mapping` (what the per-op commit
reads) re-checks every op against the current approved catalogue and leaves out ops whose action or
read-back tool is no longer approved / of the right access (they become guided steps); the admin
dry-run reports such an op as `409 mapping_stale`.

Pack `data/packs/foreign-erp-lite`: read mapping (location incl. DCs, stores and derived clusters,
item styles + SKUs, po_line, inventory_position, demand_forecast, allocation — **no
transfer_order_line**: `/mcp-lite` transfer orders have no lines) and action mapping
(`transfer_order.create` → `create_transfer_order`, `allocation.set_qty` → `update_item`, both
`best_effort` with `request_id` + read-back).

### Signals admin (`core/signals_admin.py`) and audit/evals (`core/audit.py`)

`GET /api/admin/signals/sources` (hub catalogue + poll state + this tenant's subscription),
`PUT|PATCH /api/admin/signals/sources/{id} {enabled?, cadence_s?}`, `GET|PUT /api/admin/signals/watch-regions`
(`{id, name, kind: polygon, geometry | polygon (ring or GeoJSON)}` / `{kind: radius, node_type?, node_code,
radius_km}` — node_type inferred from the network when unique), `GET /api/admin/signals/network`
(node codes), `GET|PUT /api/admin/signals/alert-rules`. The engine stores the settings in
`tenant.config` and pushes them to the hub (service JWT); `hub_synced=false` when the hub is down
(pushed again after every scn sync). `GET /api/admin/audit?category=` filters/tags `admin_audit` rows;
`GET /api/admin/audit/activity?category=&actor=&case_id=&action=&since=&until=&before=` is the
timeline across runs, approvals, change sets, admin changes and signals (opaque URL-safe cursor).
`GET /api/admin/evals` (platform-admin) lists the latest eval result files with per-case scores.
Compose mounts `./evals/results` read-only into engine-api at `/app/evals/results`
(`EVALS_RESULTS_DIR`); the files come from the CLI runner (`evals/run_live.py`).

### Agents playground, custom specialist checks, credential rotation (M7 integration)

| Route | |
|---|---|
| `GET /api/admin/agents/playground` | `{available, mode, reason, message}` — `available: false` with `reason: replay_only` when `LLM_REPLAY_MODE=replay` (fail-closed demos/evals cannot answer a new question) or `disabled` when `PLAYGROUND_ENABLED=0`; the web disables *Run in playground* with that reason |
| `POST /api/admin/agents/playground {specialist, question}` | `text/event-stream`: the turn's AG-UI activity, then `CUSTOM {name: "sc.playground.result", value: PlaygroundResult}` (`runtime.playground.run_playground`: scratch case, never commits, budget-capped, read tools only). Checked before the stream: 422 request shape, 409 `playground_unavailable`, 404 unknown saved slug, 422 `custom_specialist_invalid` (a draft reusing a built-in slug, or tenant-state problems below), 429 `playground_busy` (one turn per tenant, two per process). Closing the stream aborts the turn; audited as `agents.playground` |
| `PUT /api/admin/connectors/{slug}/secret {secret}` | rotate a `static_header` value (vendor API key) or a `client_secret` in place — tools, mappings, bindings and status are kept; never echoed or audited (`connector.secret_rotated`); run *Test* afterwards. A connector the agent spec plans on cannot be deleted (`409 connector_bound`), so this is how its key is renewed |

`core/agent_specs.custom_specialist_problems` (used by the spec PUT/restore for every **added or
edited** `custom_specialists` entry, and by the playground for drafts and saved slugs) applies
`keelson_contracts.agents.custom_specialist_violations` to the tenant's current state: approved
**read** tools of **active** connectors, a chat alias of the tenant's LiteLLM team that is not a Pro
model and can run a tool loop, existing knowledge bases → `422 custom_specialist_invalid`
(`problems[]`). Unchanged entries are not re-checked (like tool bindings).

## Evals UI (M8, `docs/contracts/128-m8-evals-ui.md`) — platform

Platform admins start eval runs in `/admin/evals` (or import CLI result files), follow them, drill
into cases, compare runs and track trends. The **in-app runner** is a separate container
(`evals/runner_service.py`, compose service `evals-runner`, profile `evals`) that drives the CLI's own
`run_live.run_case` black-box over HTTP; the engine only stores and serves.

* **Runner protocol** (`routes_internal_evals.py`, bearer `EVALS_RUNNER_TOKEN`, `hmac.compare_digest`;
  unset = 404): heartbeat with the case catalogue → claim (`FOR UPDATE SKIP LOCKED`, lease
  `EVALS_LEASE_S`; one queued/running run platform-wide) → events (seq assigned here; the answer carries
  `cancel_requested` / `budget_exhausted`) → attach each new Keelson case (same tenant, created after the
  run started, never re-linked) → PUT the case's `keelson-evals/2` result → finish. Every call renews the
  lease; an expired one is reaped lazily as `failed` (`runner_lost`), its results kept.
* **Runtime** (`runtime/execute.py`, port `RuntimeDeps.evals`): a run of an eval-linked case runs in the
  eval run's LLM mode (`runtime/replay.effective_mode`: a `replay` process always stays `replay`; a
  replay eval never calls the provider whatever the process mode), does not start once the eval run is
  cancelled (`EVAL_CANCELLED`) or its live budget is used up (`EVAL_BUDGET_EXHAUSTED`), and its
  `BudgetPlugin` stops at what is left of the budget (`BUDGET_EXCEEDED`). Each run's metered usage is
  added to the link and the eval run once (`add_eval_usage`). A replay miss ends the run with
  `LLM_REPLAY_MISS` (was `INTERNAL_ERROR`). A failing link lookup fails the run closed. Query
  embeddings are not part of LLM replay, so in a replay eval run the KB port (`deps._KbAdapter`)
  reports the knowledge base unavailable instead of embedding the query (no provider call; the
  playbook analyst reports a gap, like an embedding outage).
* **Authoritative numbers**: the summary and threshold outcomes are computed from the stored results
  when a run finishes (`core/evals_summary.py`); a case result is merged with the engine's accounting of
  its linked case (a reported `fail` with engine-seen replay misses becomes `error` / `replay_miss`).
* **Untrusted input**: runner reports and imports go through `keelson_contracts.evals.case_result_from_doc`
  (caps, control/bidi characters stripped, ≤ 1 MiB per case); a Keelson case id is kept only when linked;
  `keelson_url` is a label; nothing from a result is fetched or sent to a model.
* **Imports** are idempotent on the normalised document (`results_doc_sha256`): a file, its
  re-serialisation and the export of its run dedupe; an in-app run's export re-imports as that run.
* **History survives demo resets**: `eval_case_link` cascades with the case; runs and results stay.

## Tests

Platform tests need a throwaway Postgres 18 (superuser DSN; each session creates a non-superuser
role + a fresh database and drops it) and a Valkey:

```bash
export MSYS_NO_PATHCONV=1
docker network create e1-test-net
docker run -d --rm --name e1-test-pg --network e1-test-net --memory 384m -e POSTGRES_PASSWORD=test \
  -e KEELSON_DB_PASSWORD=x -e ADK_DB_PASSWORD=x -e KEYCLOAK_DB_PASSWORD=x keelson-pg-core:latest -c fsync=off
docker run -d --rm --name e1-test-valkey --network e1-test-net --memory 64m valkey/valkey:8-alpine \
  valkey-server --requirepass test
# test image …
docker build -f services/engine/Dockerfile --target test -t keelson/engine:test .
docker run --rm --memory 1500m --network e1-test-net keelson/engine:test
# … or the uv toolchain image with the venv in a named volume
docker run --rm --memory 1500m --network e1-test-net -v D:/primark:/work \
  -v keelson_uvcache:/root/.cache/uv -v keelson_engine_e1_venv:/opt/venv \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy -w /work/services/engine \
  ghcr.io/astral-sh/uv:python3.14-trixie-slim sh -c "uv sync --frozen && uv run pytest -q tests/platform"
docker rm -fv e1-test-pg e1-test-valkey && docker network rm e1-test-net
```

(`E1_TEST_ADMIN_DSN`, default `postgresql://postgres:test@e1-test-pg:5432/postgres`, and
`E1_TEST_VALKEY_URL`, default `redis://:test@e1-test-valkey:6379/0`, override the endpoints.)
Keycloak (both realms + the admin API) and LiteLLM are in-memory fakes served through respx
(`tests/platform/fakes/`); connector tests run a real FastMCP 4 server on 127.0.0.1
(`fakes/e1_mcp_server.py`). Pack tests need `data/packs` (present when the repo is mounted; the
`--target test` image copies it to `/src/data/packs`, with `PACKS_DIR` pointing there).

M7 (`test_m7_*.py`): mapping language semantics, refusals and a 6,000-case fuzz (no eval, no attribute
access), templates and read-back matching, crawl paging (page numbers, cursor, `max_pages`
truncation, repeated cursor, item errors), the `foreign-erp-lite` pack against real-shape `/mcp-lite`
rows; against a FastMCP `/mcp-lite` fake (`fakes/e1_lite_server.py`): mapping versions / 409 / 422
catalogue checks, install from pack, read test, crawl sync (crawled label, as-of range, supersede,
tool error / FK / data errors purge and keep the previous snapshot, reclassified tool never called),
crawl status, action dry-run (write tools never called), per-op ledger; four-eyes lifecycle
(self-approval, other tenant, stale base 409, reject reason, withdraw, platform co-sign, expiry, every
guarded kind), users through a Keycloak admin fake (`FakeKeycloakUsers`), tenant suspension, Signals
admin against the hub fake, audit categories + activity timeline, evals listing.

Coverage: migrations idempotent (+ re-runnable SQL, checksum guard); RLS forced on every tenant
table, cross-tenant reads/writes impossible, no-context sees nothing, system flag does not leak;
crypto round-trips/AAD binding/tamper detection, JWKS, client assertion + receipt claims verified
with PyJWT; EventBus seq/replay ordering/live tail/lost-publish fallback/live-only/trim+TTL/reasoning
coalescing; runs idempotency (incl. concurrent same key), FIFO + one running run per case under
concurrent claims, heartbeat ownership, reaper crash-retry (invocation kept) and give-up, cancel;
interrupts quorum (1 and 2), duplicate voter, reject, stale hash/plan, initiator exclusion,
supersede, approach edit, cross-tenant; TokenBroker private_key_jwt + resource + cache,
cross-process single-flight refresh with rotation, invalid_grant → ConnectRequired; API auth
required, CSRF token + Origin, demo login-as gating (DEMO_MODE, hosted domain secret, platform-admin
hidden), code + PKCE login with browser binding and open-redirect protection, logout, role and
tenant checks on cases/messages/actions/decisions/cancel/blocks, 412 connect_required, 409
stale/superseded; SSE over a real uvicorn server (Last-Event-ID / `?after` replay, live tail,
keep-alive, 401/404/400, disconnect never cancels); Connect-Merxa flow (session binding, PKCE,
prompt/login_hint/resource); tool snapshot classification, tiers, drift; forward-auth and the
console handoff; ops hardening (`test_ops_hardening.py`: runId on API/reaper `RUN_ERROR`, generic
500/502 with `error_id`, case snapshot vs. a concurrently committed interrupt, Disconnect-Merxa with
RFC 7009 revocation + IdP failure, viewer-based approval pre-flight/`apply_ready`), SSE streams end
promptly on shutdown + signal-hook chaining; bootstrap against a fake LiteLLM (aliases with params/mode, teams, one key per
tenant reused/regenerated, alias PATCH, idempotent re-run, reused key re-synced to new aliases,
blocked key replaced, raw key never in a URL). Back-office (M2): `test_admin_access.py` (roles,
CSRF/Origin, tenant isolation for admin + M1 objects, audited platform-admin cross-tenant access,
audit listing), `test_admin_agents.py` (versions, 409, concurrent PUTs, alias/tool/budget
validation, guarded thresholds, prompt addenda, restore, catalog, runs pin the latest version),
`test_admin_gateway.py` (views, health, alias rules, upsert/grant/delete-in-use, tenant patch vs
budget, overrides survive bootstrap, rotate-key signal, usage), `test_admin_connectors.py` (URL
policy, create, static header / client_secret / private_key_jwt / none against FastMCP, paginated
tools/list, tiers A then A,B,C,W, suggestions, needs_review + drift, guarded reclassification,
lifecycle, `auth_headers`, delete while bound), `test_webhooks.py`, `test_admin_tenants.py`
(packs, tenant creation + Keycloak + idempotency, bootstrap packs), `test_migrations_rls.py`
(0003 converts M1 rows under FORCE RLS). Review regressions: per-case append serialisation +
concurrent writers vs. a tailing reader, `finish_run` vs. reaped/foreign leases, delegated-token
cache vs. expired/re-linked links, approve vs. newer plan / revised change set, SSE end after logout.
Knowledge base (M4): `test_kb.py` against a respx fake of LiteLLM `/embeddings`
(`fakes/e1_embeddings.py`: deterministic hashed bag-of-words vectors, so cosine ranking is predictable):
heading paths + code fences, chunk size / overlap / hard splits, type detection + text rules, PDF pages /
metadata title / encrypted / too many pages / blank, ingestion request (tenant key, alias, `dimensions`,
inputs, key never in a URL), batching, chunk limit, idempotency (same bytes, other KB, 3 concurrent
uploads → one document), dims mismatch + HTTP failure → `failed` document → retried with the same id,
tenant repair re-embed, ranking with exact cosine scores, `top_k` / KB filter / unknown ids, the other
tenant sees nothing (and makes no embedding call), delete cascade, RLS (forced on the vector table,
cross-tenant reads/writes/FKs refused), platform re-embed to 2048 dims (`halfvec`, HNSW halfvec index, both
tenants' keys, old table dropped, statuses, 409 while running), a failed re-embed keeps the active profile
and drops the shadow table, ingestion racing a re-embed lands in the shadow table, `ensure_profile` on a
models.lock change, admin API (401/403/CSRF, create/list/409, md + pdf upload, duplicate, 413 both ways,
415, 422, 502 embedding failure, cross-tenant 404, delete, re-embed RBAC + jobs, audit rows), bootstrap
seed (profile, five docs, adversarial never seeded, second run makes no calls, not re-seeded when not
empty, outage → failed → retried), the repo seed docs (synthetic markers, titles, chunk sizes), and
`scenario.reset` / `merxa.*reset` webhooks → stale (`scenario.injected` not).
M5 (`test_m5_*.py`): quorum permutations (compliance first / approver first, two approvers never
complete four-eyes, same user twice, one person holding both roles, maximum matching, initiator
exclusion, required reject with reason, executor-only check incl. rollback on refusal, legacy rows), the
decision API (403 for non-quorum roles, compliance without a link, 412 for the executor, 412
`executor_connect_required`, quorum in the response and the case snapshot, live approval card +
`ACTIVITY_SNAPSHOT`, rejection on the card); quorum receipts (executor whatever the order,
`co_approved_by`, M1 claim set for one approver, PyJWT verification); policies (condition directions,
requirement strength, the guard matrix, defaults = pack = runtime defaults, evaluator port, GET/PUT with
versions / 409 / guards for tenant-admins incl. through `PUT /agents/spec` / platform-admin allowed /
CEL check 422, the test bench incl. hard floors and 503, the real cel-expr-python evaluator when
importable); re-plan (dependency extraction, save/replace/change-set/runtime fingerprint, dependent plan
lookup by lane and by receipt via the scn snapshot, closed/rolled-back/newer/other-connector/other-tenant
excluded, signed webhook → signal run + `get_signal`, duplicates, malformed data, opt-in
`sailing.updated`, routing failure → 503 and the retried delivery routes once, fallback to stored
events); Guide checklists (port shape,
idempotency, statuses, manual ticks + audit + live block, 403/404/409/422, read-back with a fake MCP:
pre-existing entities, one entity per step, `get_entity`, PO lines, completion, tool errors, unreadable
steps, Merxa-shaped read-back (allocations via `list_allocations`, old entities for edit steps, two
steps on one PO line, baseline / change-set-created entities ignored, shipment lines, PO full read),
reopening after an undone skip, the poll on interval / new event, and a real FastMCP 4 server through the reader credentials);
demo reset (full step log with mocked Merxa + Keycloak admin API + an ADK database, other tenant
untouched, stable spec version, `keep_consents`, failing Merxa / Keycloak reported per step, roles /
presenter tenant membership / CSRF / 422 / 409 lock / DEMO_MODE).
 The session database gets
`CREATE EXTENSION vector` as superuser (like pg-core's init); `KB_SEED_DIR` points at an empty dir except
in the seed test.
Canonical store (M3): `test_scn_sync.py` against a real FastMCP 4 snapshot source with pyarrow Parquet parts
(`fakes/e1_scn_server.py`): happy path (types, multi-part entities, pointer flip), GC keeping the last 2 +
pinned (pin/unpin, plan deletion drops pins), failures keep the previous snapshot and leave no rows (sha256
mismatch + back-off/recovery, row-count lie, foreign host / port / scheme refused before any download,
manifest incomplete, FK violations, invalid data mid-COPY, text Postgres rejects (NUL) → `data_invalid`,
duplicate source_id, tool error, timeout, cancellation, connector deleted mid-sync → `interrupted` without
orphan state), unavailable connectors, single-flight in-process + cross-process lock, a failed unlock never
leaks the lock, a pin committed while GC waits survives, pins of unknown / foreign plans, orphaned `loading` rows,
RLS isolation of every `scn_*` table, stale marks (debounce, max delay, event during a sync, back-off,
`request_sync`, a request during a running sync stays due, `due_syncs` filters), tier-C poll (`full_resync_required`), webhook → stale (types, duplicates,
mark failure tolerated), admin endpoints (roles, CSRF, 404/409, cross-tenant, audit), connector deletion
purge, MCP + downloads through a recording forward proxy, and the opt-in S5 measurement:
`SCN_PERF=1 pytest -s tests/platform/test_scn_sync.py -k s5` (`SCN_PERF_ROWS`, `SCN_PERF_ALLOCATIONS`,
`SCN_PERF_INVENTORY`). Each sync logs `scn sync phases` (`source_ms`, `download_ms`, `load_ms`, `check_ms`,
`analyze_ms`, `commit_ms`, `entity_ms`). The load transaction `ANALYZE`s every `scn_*` entity table before the
pointer flip (`test_sync_refreshes_planner_statistics_before_the_flip`): without it the new `snapshot_id` is
missing from the planner statistics and the first ProblemData build after a sync took ~58 s on the full
profile (nested loops on a 1-row estimate) instead of ~1 s.

S5 preview (2026-10-06, throwaway PG 18 with `fsync=off`, Docker Desktop host shared with other builds, so
timings vary ±50 %; one Python process, contract target < 60 s and ≤ 600 MB worker RSS):

| Source | Rows | Sync | Process RSS |
|---|---|---|---|
| fake source, 200,000 forecasts + 92,601 allocations + 81,900 inventory (375k rows) | 374,971 | 9–18 s (re-sync 8–27 s) | +26–61 MB over the test process |
| **running Merxa, `SEED_PROFILE=full`** (keelson-reader via Keycloak + egress proxy) | 301,049 (121,770 forecasts, 92,601 allocations, 81,900 inventory) | **15–23 s** (Merxa `snapshot_open` ≈ 3 s, download 0.1–0.3 s, load 10–17 s, checks 0.3 s, commit 1–2 s) | +100–120 MB working set (+24 MB for importing pyarrow), peak 262–302 MB in a fresh engine process |

The running engine-worker idles at ≈ 300 MB, so a sync peaks around 450 MB. A smaller `BATCH_ROWS` saves
little (2,000 rows: +102 MB; 50,000: +159 MB) because pyarrow decodes a whole row group (Merxa writes one per
50k-row part). The first version of the FK check (an anti join per inventory row, run before planner
statistics exist) took 11–15 s on the full profile; it now checks distinct keys (≈ 0.3 s).
