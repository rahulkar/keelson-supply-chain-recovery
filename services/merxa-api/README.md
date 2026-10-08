# merxa-api — Merxa Retail Suite backend

Mock third-party ERP ("Merxa Systems") for the Keelson demo. Standalone uv project; it
does **not** depend on any Keelson package. Contracts: `docs/contracts/20-merxa.md`
(domain, REST, MCP, change sets), `docs/contracts/22-merxa-m2.md` (M2: full domain,
aggregates, scn export, webhooks, API keys, `/mcp-lite`), `docs/contracts/25-scn-1.0.md`
(canonical exchange format), `docs/contracts/10-auth.md` (realm `merxa`, approval
receipts, Merxa UI BFF) and `docs/contracts/95-m5-remediation.md` (M5: Guide-mode manual
changes, `capacity_booking.cancel`, lane capacity inject, four-eyes co-approvers).

| Surface | Path |
|---|---|
| Health | `GET /healthz` (process), `GET /readyz` (DB reachable + lifespan up) |
| REST | `/v1/*` — JSON, cursor pagination, RFC 9457 problems, `If-Match`, `Idempotency-Key` |
| MCP | `/mcp` — FastMCP 4.0.11, stateless streamable HTTP, Bearer JWT from realm `merxa` |
| MCP lite (Tier A) | `/mcp-lite` — plain CRUD tools; `X-Merxa-Api-Key: mx_…` **or** `Authorization: Bearer` (API key or realm JWT) |
| scn downloads | `GET /v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=&sig=` — HMAC-signed, no auth header |
| Protected Resource Metadata | `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-protected-resource` |
| UI BFF auth | `/v1/auth/{login,callback,logout,me}`, `/v1/auth/demo/{personas,login-as}` (cookie `merxa_session`) |
| Manual changes (M5) | `POST /v1/transfer-orders`, `PATCH /v1/allocations/{code}`, `PATCH /v1/purchase-orders/{po}/lines/{n}`, `POST /v1/capacity-bookings`, `POST /v1/capacity-bookings/{code}:cancel` (+ `GET /v1/allocations/{code}`, `GET /v1/capacity-bookings/{code}`) — see "Manual changes" |
| Demo director | `POST /v1/admin/scenario:{reset,inject,advance-clock}` (DEMO_MODE=1 + role `merxa-admin`) |
| OpenAPI | `/v1/docs`, `/v1/openapi.json` |

## Layout

```
src/merxa_api/
  app.py               FastAPI factory; lifespan = wait for DB → migrate → seed-if-empty → seeded webhook
                       subscription → MCP session managers (/mcp, /mcp-lite) → webhook worker + housekeeping
  config.py            Settings from env; read_secret(name) (/run/secrets/<name> → env NAME)
  migrate.py           numbered-SQL runner (merxa.schema_migrations, advisory lock, checksums)
  migrations/0001_init.sql       M1 tables (PG18 uuidv7(), version/ETag trigger, CHECKs, append-only audit_log)
  migrations/0002_m2_domain.sql  M2 tables (ports … sales_history, api_key, webhooks, scn_snapshot), outbox seq
  migrations/0003_m5.sql         M5: change_set.co_approvers, audit_log.co_approved_by
  tables.py            SQLAlchemy Core metadata (query building only; DDL lives in SQL)
  canonical.py         canonical JSON + preview_hash (00-overview.md)
  security/            principal (scopes/roles), tokens (FastMCP JWTVerifier), receipts, oidc, sessions
  services/            catalog (M1 reads + aggregates), supply (M2 reads), changesets (engine + rollback),
                       ops / ops_m2 (op handlers + inverses), events (outbox, feed, changes_since),
                       snapshots (scn/1.0 Parquet + signed URLs), webhooks (Standard Webhooks worker),
                       apikeys, clients (OAuth last-seen), idempotency, audit, admin (demo director),
                       manual (M5 manual changes behind the Guide-mode forms)
  api/                 REST routers (read, write, auth, m2, m5) + dependencies
  mcp/                 server.py (/mcp, tools in contract order), lite.py (/mcp-lite), pydantic models
  seed/                generator (both profiles), reference / reference_full, stores_full (city table),
                       network (logistics, production, online orders, promotions, history), dataset (CSV/COPY),
                       loader (golden cache, reset, fingerprints), story projection, CLI, cities_extract
  seed/data/cities.csv city table for the `full` profile (see "City data")
tests/                 pytest (throwaway Postgres 18; mocked IdP, Keelson JWKS and webhook receiver)
```

## Configuration

| Env | Default | Notes |
|---|---|---|
| `PORT` | `8100` | |
| `MERXA_DATABASE_URL` | `postgresql+asyncpg://merxa:merxa@localhost:5432/merxa` | schema `merxa` is created by the migration |
| `OIDC_ISSUER` | `http://auth.localhost/realms/merxa` | token `iss`; JWKS discovered from `{issuer}/.well-known/openid-configuration` |
| `OIDC_JWKS_URI` | — | optional override (disables discovery) |
| `OIDC_TOKEN_ALG` | `RS256` | Keycloak's default access-token algorithm |
| `MCP_RESOURCE` | `http://merxa.localhost/mcp` | required token audience on `/mcp` **and** `/mcp-lite`; also the PRM `resource` |
| `PUBLIC_URL` | `http://merxa.localhost` | redirect URIs, deep links, CSRF origin, signed snapshot URLs |
| `TRUSTED_RECEIPT_ISSUERS` | `http://keelson.localhost` | comma separated |
| `KEELSON_JWKS_URL` | `http://keelson.localhost/api/.well-known/jwks.json` | receipt keys, cached 5 min, refetch on unknown `kid` |
| `MERXA_WEB_CLIENT_SECRET`, `MERXA_DEMO_CLIENT_SECRET`, `SESSION_SECRET`, `DEMO_USER_PASSWORD`, `DEMO_ACCESS_SECRET` | — | via `read_secret` (file in `/run/secrets/<lowercase>` or env) — never logged |
| `DEMO_MODE` | `0` | `1` enables persona switcher + demo director |
| `SEED_PROFILE` | `full` | `full` (451 stores, 180 SKUs — the demo) or `small` (62 stores, 30 SKUs — fast tests) |
| `SEED_ON_STARTUP` | `1` | seed if `merxa.dc` is empty |
| `SNAPSHOT_DIR` | `/data/snapshots` | Parquet snapshot files (compose: named volume `merxa_snapshots`) |
| `MERXA_SNAPSHOT_SECRET` | — | HMAC key for signed download URLs (`read_secret`); unset → random per process (warning) |
| `SNAPSHOT_TTL_S` | `7200` | snapshots (and their URLs) expire after 2 h; housekeeping deletes them |
| `KEELSON_WEBHOOK_URL` + `MERXA_WEBHOOK_SECRET` | — | both set → seeded subscription "Keelson (demo-retail)" for all events; the secret is `whsec_<base64>` |
| `WEBHOOK_WORKER` | `1` | run the delivery worker in-process (tests drive `run_once()` instead) |
| `WEBHOOK_POLL_S` | `1.0` | worker poll interval |
| `MERXA_DATA_KEY` | — | key material for secrets at rest (webhook secrets); falls back to `SESSION_SECRET` |
| `MCP_JSON_RESPONSE` | `1` | JSON (not SSE) responses for the stateless MCP endpoints |
| `MCP_ALLOWED_HOSTS` | `<PUBLIC_URL host>,merxa-api,localhost,127.0.0.1` | Host-header allowlist for `/mcp` and `/mcp-lite` (DNS-rebinding guard) |
| `MCP_ALLOWED_ORIGINS` | — | extra browser origins allowed to call the MCP endpoints (e.g. an MCP Inspector) |

## Running

```bash
# in the stack (see compose.yaml)
env -u LITELLM_MASTER_KEY docker compose -p keelson up -d pg-merxa merxa-api

# image only
docker build -t keelson/merxa-api services/merxa-api

# seed CLI (inside the container / any env with MERXA_DATABASE_URL)
python -m merxa_api.seed --profile full --reset --check-story
python -m merxa_api.seed --profile small --no-db --check-story     # no database: generation + story only
```

`--check-story` prints the projection (`seed/story.py`, which mirrors the Keelson
optimizer's impact model: DC supply + **store stock as cluster supply**, weekly fair share,
lost sales) of the loaded database **and**, computed offline, of both profiles: baseline,
"Bangladesh PO lines +7 days" and (`full`) "Ho Chi Minh City port closed 10 days" and the Act 2
what-if "Suez closed 21 days from the sim clock" (`suez_closure_21_days`, docs/contracts/90 §8).
The Suez delays are **resolved from the lane data** (`story.closure_shifts`, mirroring the
optimizer): an open PO line whose sailing departs inside the window on a lane `via` SUEZ
switches to the lane's `alt_lane_code` (Cape, +12 days) — or waits for the reopening when there
is no slower-or-equal alternate avoiding the chokepoint — whichever is earlier. The Suez section's `recoverable_by_dc_transfer`
(`story.transfer_recoverable`) only counts surplus at DCs with a **directional** DC-to-DC lane
into the short DC (the optimizer's `reallocate_inventory` uses lanes as given); the BD
`recoverable_by_rebalance` is the older lane-free estimate.

| Projection | `small` | `full` |
|---|---|---|
| baseline units at risk | 0 | 0 |
| BD +7 days: units at risk (UK1 / NL1), all in W45–W46 | 2,624 (1,478 / 1,146) | 16,904 (10,605 / 6,299) |
| BD +7 days: recoverable by a DE1/PL1 rebalance (rough) | 2,220 (85 %) | 13,080 (77 %) |
| VNSGN closed 10 days: knit short at DE1 / PL1 | — | 17,960 / 4,897 (from W46; DE1 W46–W48, PL1 W46–W47) |
| SUEZ closed 21 days (360 VN lines +12 days via the Cape): knit short at DE1 / PL1 | — | 17,960 / 11,582 (W46, W47 **and** W48 at both) |
| SUEZ closed 21 days: recoverable by a DC-to-DC transfer along the existing lanes | — | 0 (the knit surplus sits at UK1/NL1; no transfer lane runs from there to DE1/PL1 — the remedies are expedite / re-routing) |

The real optimizer on the `small` golden seed (M1 measurement): 0 → 2,626 units at risk
(UK1 1,478, NL1 1,148); 14 days → 8,157; the rebalance recovers 2,389.

## Tests

Tests need a throwaway Postgres 18 (each session creates/drops its own database) and do not
use the compose stack or `.env`:

```bash
MSYS_NO_PATHCONV=1 docker network create merxa-test-net
MSYS_NO_PATHCONV=1 docker run -d --rm --name merxa-test-pg --network merxa-test-net --memory 512m \
  -e POSTGRES_PASSWORD=test postgres:18-trixie -c fsync=off
# either the test image …
docker build --target test -t keelson/merxa-api:test services/merxa-api
docker run --rm --memory 1500m --network merxa-test-net \
  -e MERXA_TEST_ADMIN_DSN=postgresql://postgres:test@merxa-test-pg:5432/postgres keelson/merxa-api:test
# … or the uv toolchain image with the venv in a named volume
MSYS_NO_PATHCONV=1 docker run --rm --memory 1500m --network merxa-test-net \
  -v D:/primark:/work -v keelson_uvcache:/root/.cache/uv -v keelson_merxa_venv:/opt/venv \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy \
  -e MERXA_TEST_ADMIN_DSN=postgresql://postgres:test@merxa-test-pg:5432/postgres \
  -w /work/services/merxa-api ghcr.io/astral-sh/uv:python3.14-trixie-slim \
  sh -c "uv sync --frozen && uv run pytest -q && uv run ruff check src tests"
docker rm -f merxa-test-pg && docker network rm merxa-test-net
```

The session runs on the `small` profile; one test loads `full` into its own database
(story from the stored data, the typhoon inject, reset timing). ~2 min.

Coverage: migrations idempotent (incl. the PG18 temporal rate-card key); both seed profiles
deterministic (golden sha256 pinned per profile, per-table DB fingerprints, COPY path ≡
INSERT path) and their stories; REST pagination / ETag / If-Match (428/412) /
Idempotency-Key (400/409/422/replay); change-set happy path, conflict, replay, auto caps;
every M2 op (validation errors, before/after, audit, outbox) and **rollback** (state restored,
conflict when records changed since, user receipt required, replay, MCP); receipt rules;
MCP over the in-process FastMCP `Client` (listing order, annotations, schemas, scope
denial, write flow); M5 (`tests/test_m5.py`): `capacity_booking.cancel` (freed capacity
for later items, validation errors, apply, audit, rollback + conflict), `co_approved_by`
(attribution, audit column, events, rollback wording, malformed claims and policy receipts
rejected), manual changes (TO / allocation / PO line / booking create + cancel: Idempotency-Key
replay and reuse, If-Match 428/412, 422/404, Keelson clients and read-only apps refused, API
keys, UI session + CSRF, one event each, signed webhook delivery), the `lane_capacity` inject
(rate-card split, capacity seen by validation, event + untrusted note, compounding, reset,
validation), operator-guide links/`expected` (safe-link pattern, encoded codes) and the
seeded-subscription upgrade to the M5 types; aggregates equal the sums of store rows (inventory cluster/dc,
forecast cluster); logistics / production / online-order / promotion / event reads; scn
round trip (open → export → signed download → sha256 / rows / columns per scn/1.0),
tampered / expired URLs, `changes_since` + reset resync; Suez what-if (lane-resolved
closure shifts, W46–W48 knit shortage at DE1/PL1); webhooks (Standard Webhooks
signature verified independently, retries with backoff, same `webhook-id` on retries,
failure after 6 attempts, fan-out filter + dedupe, subscription CRUD, reset delivered as
`merxa.scenario.reset`, seeded subscription upgrade); adversarial supplier-message inject
(verbatim through REST, MCP, feed and webhook; limits); API keys (hash
only, show once, revoke, scopes, last used); `/mcp-lite` auth both ways, CRUD, write-scope
denial, idempotent `request_id`; append-only audit; BFF demo login, CSRF, demo director.

## Behaviour notes

### Auth
* **Bearer** (REST and MCP): FastMCP `JWTVerifier` — signature via realm JWKS, `iss ==
  OIDC_ISSUER`, `aud` contains `MCP_RESOURCE`, `exp`. On top of FastMCP: `exp` and `sub` are
  **required** (the base class accepts a token without `exp`), a `typ` claim other than
  `Bearer` (ID/refresh tokens) is refused, and unknown-`kid` JWKS refetches are throttled to
  one per 10 s. Roles = `realm_access.roles` ∪ `roles`. `client_id = azp`. `via = "Keelson"`
  when `azp` starts with `keelson-`.
* **API keys** (`mx_<8 char prefix>_<32 char secret>`): minted by `merxa-admin` (`POST
  /v1/api-keys {name, scopes}`), shown **once** (a replay of the mint returns `key: null`),
  stored as sha256 only, revocable (`DELETE /v1/api-keys/{id}` or `:revoke`), `last_used_at`
  updated at most once a minute. Scopes ⊆ read scopes + `changes:preview` + `changes:apply`.
  Accepted on REST (`X-Merxa-Api-Key` or `Authorization: Bearer mx_…`) and `/mcp-lite`. The
  key's scopes are its grant (no roles); attribution `via: "API key"` (REST) / `MCP-lite`.
* **MCP per-tool scopes**: every authenticated token sees the **same complete tool list** in
  contract order (the Keelson bootstrap snapshots `tools/list` with its reader principal and
  classifies writes from annotations — 55-engine-internals). Scopes are checked when a tool is
  called: missing scope → tool error `insufficient_scope: this tool requires scope …`. Each
  tool description ends with `Requires scope: …`.
* **UI sessions**: `merxa_session` (HttpOnly, SameSite=Lax) → `ui_session` row keyed by
  HMAC-SHA256(`SESSION_SECRET`, cookie). Session scopes derive from roles (all reads;
  `changes:preview` for planner/director/admin; `changes:apply` for `ops-director`).
  Cookie-authenticated writes must not carry a foreign `Origin`/`Referer`.
* **Connected clients**: OAuth clients (`azp`) seen on `/mcp`, `/mcp-lite` and REST are
  recorded (once a minute) — `GET /v1/oauth-clients` (merxa-admin) for the "API & MCP
  Clients" page.

### MCP tools (`/mcp`)
Reads (`readOnlyHint`), in order: `search_entities`, `get_entity`, `list_locations`,
`list_stores`, `list_purchase_orders`, `get_purchase_order`, `get_bom_where_used`,
`get_inventory`, `get_demand_forecast`, `list_allocations`, `list_transfer_orders`,
`list_supplier_messages`, `get_change_set`, `get_audit_trail`, `get_operator_guide`,
`list_sailings`, `list_shipments`, `get_lane_options`, `get_factory_schedule`,
`list_online_orders_at_risk`, `list_promotions`, `list_events`, `scn.snapshot_open`,
`scn.snapshot_export`, `scn.changes_since`; writes: `create_change_set`,
`add_change_set_items`, `validate_change_set`, `apply_change_set`, `rollback_change_set`.

* `get_inventory(aggregate)`: `none` (default) raw DC + store rows; `cluster` = DC rows
  unchanged + store rows summed per store cluster (`location_type: "cluster"`,
  `store_count`, no `version`); `dc` = DC rows + store rows summed per home DC
  (`location_type: "dc_stores"`). With `location_type: "store"` only the store-level rows.
  The `full` profile has 81,900 store positions — consumers should read stores with
  `aggregate: "cluster"` (≈ 3,000 rows).
* `get_demand_forecast(aggregate)`: forecasts are stored per **style × store × ISO week**;
  `cluster` (default) sums them per cluster (same rows as M1), `store` returns store rows
  (`store_code`); `limit`/`cursor` page both.
* `get_lane_options(origin, dest, week?)`: direct lanes plus two-leg routes (main leg to a
  port/DC + road leg), sorted by transit; rate and remaining weekly capacity from the rate
  card valid on the week's Monday minus held/confirmed bookings.
* `list_events(after_cursor?)`: outbox events in commit order (`seq`); free text (supplier
  message subject/body, the demo `inventory_adjust` reason) is flagged
  `content_trust: "untrusted_text"` + `untrusted_fields` (only the fields present).
* `list_supplier_messages(supplier_code?, since?)` has no cursor: it returns the **newest**
  200 matching messages, oldest first (REST `/v1/supplier-messages` pages everything).
* `scn.snapshot_export` returns the signed URLs in `structuredContent` **and** as MCP
  `resource_link` content blocks (synchronous; MCP Tasks are not used).

### `/mcp-lite` (Tier A, deliberately un-curated)
`list_items(type, filter_json?, page?, page_size≤200)`, `get_item(type, key)`,
`update_item(type, key, fields_json, expected_version, request_id?)`,
`create_transfer_order(from_dc, to_dc, ship_date, lines_json, request_id?)`. Types: `sku,
style, store, dc, purchase_order, po_line, inventory, forecast, allocation, transfer_order`;
keys `code`, `<po>#<line>`, `<dc|store>/<location>/<sku>`, `<style>|<store>|<week>`.
Annotations: `readOnlyHint` on the two reads only; the writes carry none. Reads need the
type's read scope; writes need `changes:apply` (+ role `ops-director` for bearer tokens).
`update_item` is a plain field write (whitelisted fields, version-checked, constraint
violations → `invalid_value`), audited `via: MCP-lite`, with an outbox event
(`inventory.adjusted`, `po_line.eta_changed` or internal `entity.updated`). `request_id`
makes a call replay-safe (idempotency record `lite:<request_id>`).

### Change sets
* `preview_hash = sha256(canonical([{seq, op, target_ref, expected_version, payload}] by seq))`.
* Items are checked structurally on add: integers must fit a Postgres `int`, no floats, no NUL
  characters (`400 invalid_items`); dates in payloads must lie within 730 days of the sim
  clock (validate error).
* `validate` checks every item against current data with a running simulation (ATP drawn by
  earlier items and lane capacity drawn per lane × week count), stores before/after per
  item; result `validated` or `invalid`. Unknown / internal (`revert.*`) op → `unsupported_op`.
* Ops (payloads: integers + ISO dates only):
  | op | target / payload | validation highlights | effect |
  |---|---|---|---|
  | `transfer_order.create` | — / `{from_dc, to_dc, ship_date, lines}` | ATP at from_dc | TO `TO-00xxxx`, reserved / in_transit |
  | `allocation.set_qty` | allocation code / `{qty}` | version, status, ATP for released | qty (+ DC allocated) |
  | `po_line.reschedule` | `<po>#<line>` / `{eta_dc}` | version, status, horizon | eta_dc |
  | `po_line.change_qty` | `<po>#<line>` / `{qty}` | ≥ received | qty_ordered |
  | `po_line.cancel` | `<po>#<line>` / `{}` | status open/in_production | status cancelled |
  | `po.create` | — / `{supplier_code, factory_code, dest_dc, eta_dc, lines}` | factory belongs to supplier, a lane exists, `eta_dc` reachable (fastest transit) | PO `PO-<CC>-0101…` (ex-factory = ETA − transit) |
  | `po_line.split` | `<po>#<line>` / `{mode:"sea_air", qty, lane_code}` | lane is sea-air from the PO's origin port to its DC, qty < open qty, weekly capacity | new PO line (ETA = departure + transit), confirmed booking, sea-air shipment; original line + shipment line reduced |
  | `capacity_booking.create` | — / `{lane_code, iso_week, qty_units}` | per-unit rate card in that week, remaining capacity | booking `CB-0101…` (confirmed) |
  | `capacity_booking.cancel` (M5) | booking code / `{}` | version, status held/confirmed, week not past; the freed per-unit capacity counts for **later** items of the set (cancel before re-booking) | status `released`; the preview lists `linked_shipments` (left unchanged) |
  | `production_order.resequence` | line code (opt.) / `{line_code, order_codes}` | the codes are exactly the line's planned/held orders | seqs + dates recomputed back to back from the first start (durations kept); `late_vs_ex_factory` in the preview |
  | `promotion.reschedule` | promotion code / `{period_start, period_end}` | end ≥ start, end not past | period (inclusive) |
  | `online_order.repromise` | order code / `{promise_date}` | status open/picking, not past | promise_date |
* `totals.units_moved` = Σ moved/changed units (transfers, |Δqty|, cancelled open qty, split
  qty, new PO qty); `totals.value_cents` = those units × style `unit_cost_cents`.
* `apply`: change set must be `validated` with equal `preview_hash`; receipt verified (see
  10-auth.md; `approval_id == approval_ref`, `ext_iss` = realm if present, jti single-use);
  policy receipts → auto caps (ops ⊆ {transfer_order.create, allocation.set_qty}, ≤ 50
  items, ≤ 5,000 units) else `403 auto_cap_exceeded`. One transaction: phase 1 locks every
  target and collects **all** conflicts; any conflict → rollback, status `conflict`; else
  phase 2 mutates, stores each item's `result` (created codes, produced versions), writes
  audit rows, the attribution fields, the outbox events (`change_set.applied` with scn
  changes, plus `po_line.eta_changed` for reschedules) and the idempotency record.
* **Rollback** (`POST /v1/change-sets/{id}:rollback {approval_ref, approval_receipt}`,
  MCP `rollback_change_set`): requires `changes:apply` + role `ops-director` and a **user**
  receipt bound to the **original** change set (`change_set_ref` = its id,
  `change_set_hash` = its `preview_hash`, `approval_id == approval_ref`, fresh jti) whose
  `approval_ref` differs from the one that applied the set (`403 invalid_receipt`, reason
  `approval_reused`: an apply approval, even re-minted, never authorises the undo). Merxa
  builds a compensating change set (`rollback_of`) from the items' stored results — inverse
  ops in reverse order, internal `revert.*` ops for creates/splits/cancels/resequences
  (`revert.capacity_booking_status` restores a cancelled booking's status) —
  each expecting the version the original apply produced, and applies it atomically: any
  record changed since → `409` conflict, nothing applied. Success → original
  `rolled_back`, compensating set `applied` (attribution "Rolled back by … via Keelson
  (APR-…)"), outbox `change_set.rolled_back`. Created records are cancelled/released, never
  deleted.
* **Four-eyes co-approvers** (M5): a user receipt may carry `co_approved_by:
  ["user:<username>", …]` (≤ 9, distinct and never `approved_by` — both compared
  case-insensitively, so one person is never counted twice — user approvals only; an empty
  list = none; anything else → `403 invalid_receipt`, reason `bad_co_approved_by`). A change
  set applied **with** co-approvers can only be rolled back by a receipt that carries
  co-approvers too (contract 95 §3 "four-eyes if the original needed it"; else `403
  invalid_receipt`, reason `co_approval_required`, nothing changed). Display
  names come from the optional parallel claim `co_approver_names` (else the username). Apply
  and rollback store them on the change set (`co_approvers: [{ref, name}]`), on **every**
  audit row they write (`audit_log.co_approved_by`, migration 0003) and in the
  `change_set.applied` / `change_set.rolled_back` events (`co_approved_by`); the attribution
  text gains ", co-approved by Lena Vogel".

### Events, scn export and webhooks
* **Outbox** rows get a gap-free `seq` in commit order (writers take a transaction advisory
  lock before inserting), so `ox:<seq>` is a safe watermark. Events: `change_set.applied`,
  `change_set.rolled_back`, `po_line.eta_changed`, `supplier_message.received`,
  `inventory.adjusted`, `sailing.updated`, M5: `transfer_order.created`, `allocation.updated`,
  `po_line.updated`, `capacity_booking.created`, `capacity_booking.cancelled` (manual
  changes) and `lane.capacity_changed` (+ internal `entity.updated`, `scenario.reset`).
  Each carries scn/1.0 change records `{entity, op, key, version}`. The outbox survives demo
  resets. Every reload of already-loaded data (demo reset **and** CLI `--reset`) writes
  `scenario.reset {profile, dataset_sha256, full_resync_required: true}` **in the reload's
  transaction** (never a reset without the event, or the reverse); the feed and `changes_since` keep that name, **webhooks
  deliver it as `merxa.scenario.reset`** (docs/contracts/90 §10: the consumer marks its
  snapshot stale).
* **`scn.snapshot_open(entities)`**: one `REPEATABLE READ, READ ONLY` transaction records
  the watermark (+ WAL LSN) and writes Parquet (pyarrow, zstd, ≤ 50,000 rows/part) under
  `SNAPSHOT_DIR/<id>/<entity>/part-NNNN.parquet` with the exact scn/1.0 columns; manifest
  (rows, per-part sha256) in `merxa.scn_snapshot`. Dates are `date32` (`valid_to`,
  `period_end` inclusive), lists `list<string>`, quantities/money `int64`.
  `source_id`s: `<kind>:<code>` (location, item), `<po>#<line>`, `<dc|store>/<loc>/<sku>`,
  `<style>|<store>|<week>`, `<family>|<cluster>|<size>`, `<lane>|<valid_from>`,
  `<style>|<component>|<factory or *>`, `<to>#<line>`, `<order>#<line>`, codes otherwise.
  `src_version` of line entities = line version + header version − 1.
  `item.family`: the style family for styles and SKUs; for components (scn/1.0 has no item name
  column) the material name plus its kind when the name does not already say it
  ("Polyester wadding 120 gsm", "Polyester teddy fleece (fleece fabric)"), so consumers can match
  material words against live data.
* **`scn.snapshot_export(id, entity)`**: `{parts:[{uri, sha256, rows, bytes}]}`; `uri` =
  `PUBLIC_URL/v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=<unix>&sig=<hex
  HMAC-SHA256(MERXA_SNAPSHOT_SECRET, "<id>/<entity>/<file>:<exp>")>`; `exp` = snapshot
  expiry. Bad signature / tampered path → 403, expired → 403 `url_expired`.
* **`scn.changes_since(watermark)`**: `{changes:[{entity, op, key, version, at}],
  next_watermark, has_more}`; after a demo reset `full_resync_required: true`.
* **Webhooks** (Standard Webhooks): the worker fans unpublished outbox rows out to active
  subscriptions (`webhook_delivery`, unique per subscription × event), then POSTs
  `{type, id, timestamp, tenant_hint: null, data}` with `webhook-id` (= event id, identical
  on retries), `webhook-timestamp`, `webhook-signature: v1,<base64 HMAC-SHA256(key,
  id.timestamp.body)>`, key = base64-decoded part of `whsec_…`. Non-2xx / network / client
  errors (any exception while sending) are retried after 1 s, 5 s, 30 s, 2 min, 10 min; the 6th failure marks the delivery `failed`.
  Subscriptions (URL must be http(s), ≤ 2,048 chars and usable by the HTTP client — host,
  port, IDNA; any host is allowed, merxa-admin only): `GET/POST /v1/webhook-subscriptions` (secret shown once, stored
  AES-256-GCM-encrypted), `PATCH` (If-Match), `DELETE`, deliveries
  `GET /v1/webhook-subscriptions/{id}/deliveries`, `GET /v1/webhook-deliveries` (merxa-admin).
  Subscribable types: the six M2 events, the six M5 events + `merxa.scenario.reset` (`*` = all of them; the
  internal `entity.updated` only when named). `scenario.reset` given in a subscription is
  stored as `merxa.scenario.reset`. The seeded subscription ("all events") gains event types
  added later on start-up unless an admin narrowed its list (a row still listing every M2
  type counts as "all events").

### Manual changes (M5 Guide mode, docs/contracts/95 §2)
People carry out Guide-mode steps in the Merxa UI: operator-guide deep links open a form
pre-filled with the change-set item's values; saving calls REST. Each save runs **the op
handler a change set would use** (same validation, version checks, locks, audit rows) in one
transaction, without a change set, and emits one outbox event (webhook) with the entity's
state after the change (`source: "manual"`, `via`, `changed_by`) — Keelson's checklist
auto-tick reads it back.

| Endpoint | Op | Event |
|---|---|---|
| `POST /v1/transfer-orders {from_dc, to_dc, ship_date, lines:[{sku, qty}]}` → 201 | `transfer_order.create` | `transfer_order.created` |
| `PATCH /v1/allocations/{code} {qty}` + `If-Match` | `allocation.set_qty` | `allocation.updated` |
| `PATCH /v1/purchase-orders/{po}/lines/{n} {eta_dc}` **or** `{qty_ordered}` + `If-Match` | `po_line.reschedule` / `po_line.change_qty` | `po_line.updated` (not `po_line.eta_changed`, which stays the supplier/ETA signal) |
| `POST /v1/capacity-bookings {lane_code, iso_week, qty_units}` → 201 | `capacity_booking.create` | `capacity_booking.created` |
| `POST /v1/capacity-bookings/{code}:cancel` + `If-Match` | `capacity_booking.cancel` | `capacity_booking.cancelled` |

* `Idempotency-Key` required (replay → same body, `Idempotent-Replayed: true`); missing
  `If-Match` → 428; validation errors → 422 `validation_failed` (`errors`), stale version →
  412, unknown record → 404, lock-time conflicts → 409. Responses carry an `ETag`.
* Who: a person with role `merch-planner` or `ops-director` (UI session — CSRF origin check —
  or a realm token that also holds `changes:preview`/`changes:apply`), or an API key with
  `changes:apply`. **Keelson's clients (`keelson-*`, also delegated user tokens) are refused**
  (`403 manual_edit_forbidden`): Keelson changes data through approved change sets only.
* Audit rows carry the caller's channel (`Merxa UI`, `API`, `API key`) and no change-set code;
  created records get `created_via` `ui` (session) or `api`.
* `get_operator_guide` / `GET /v1/change-sets/{id}/operator-guide` steps:
  `{seq, item_seq, op, target_ref, title, instructions, deep_link, expected}`; links point at
  these forms (`/transfer-orders/new?from=&to=&ship_date=&lines=SKU:qty,…`,
  `/allocations/{code}?action=set-qty&qty=&version=`, `/purchase-orders/{po}?line=&action=
  reschedule&eta=&version=` / `action=change-qty&qty=`, `/capacity-bookings/new?lane=&week=&qty=`,
  `/capacity-bookings/{code}?action=cancel&version=`); codes are percent-encoded as one path
  segment. `expected = {entity_type, entity_ref|null, match}` says what a read-back finds once
  the step is done (e.g. `{"entity_type": "allocation", "entity_ref": "AL-…", "match": {"qty": 9}}`).

### Idempotency
`(principal = token sub | apikey:<id>, key)`; request fingerprint = sha256(canonical(op +
body)). Only successful outcomes and change-set conflicts are stored for replay
(`Idempotent-Replayed: true`); failed requests are discarded so the same key can be
retried. For `apply` / `rollback` the receipt JWT is **not** part of the fingerprint. In-flight
rows older than 120 s may be taken over. Records older than 24 h are purged hourly.

### Seed
`SeedSequence(20261005)`; two profiles share one generator core (`seed/generator.py`):

* **`small`** (fast tests): 4 DCs, 12 clusters, 62 stores (GB/IE/NL/BE/FR/DE/AT/PL/CZ),
  5 puffer style-colours × 6 sizes = 30 SKUs, 7 POs (6 Bangladesh POs to UK1/NL1 with ETA
  W44/W45/W46, still in production; 1 Cambodia PO received at DE1 in W40), allocations
  W41–W49.
* **`full`** (demo default): 451 stores in 16 markets (GB 160, ES 70, DE 45, IE 30, FR 25,
  NL 20, IT 20, PL 20, PT 15, BE 10, CZ 8, RO 8, AT 6, HU 6, SK 4, SI 4) placed on the city
  table weighted by `population^0.6` with ~2 km jitter; DC mapping UK1→GB/IE,
  NL1→NL/BE/FR/ES/PT, DE1→DE/AT/IT/SI, PL1→PL/CZ/SK/RO/HU; 18 clusters (DC × climate ×
  grade band); Outerwear (15) + Knit (15) style-colours × 6 sizes = 180 SKUs; suppliers
  incl. `TRIM-BD-01`, `SUP-VN-01` Saigon Knit and near-shore `F-TR-01` / `F-MA-01`; 12 POs
  (6 BD, KH received at DE1, 4 VN → DE1/PL1 W46/W47, TR received at NL1); allocations
  W41–W42 only (≈ 93k rows).
* Both: ports (BDCGP, LKCMB, VNSGN, KHPNH, NLRTM, GBFXT, DEHAM, PLGDN, AEJEA, TRIST, MATNG),
  carriers, 26 lanes (Bangladesh sea around the Cape; Vietnam sea **via Suez**
  `SGN-HAM-SEA` / `SGN-GDN-SEA` whose `alt_lane_code` is the Cape route `SGN-HAM-CAPE` /
  `SGN-GDN-CAPE` (+12 days, own per-TEU rate cards, no sailings); sea-air via Jebel Ali, road
  legs, near-shore, DC↔DC mirroring `infra/tenants.yaml`), rate cards (sea-air: standard
  until 31 Oct, peak from 1 Nov), 4 sailings (`SAIL-0001/2` Chittagong → Felixstowe/Rotterdam,
  cutoff 6 Oct; `SAIL-0003/4` Ho Chi Minh City → Hamburg/Gdańsk via Suez, ETD 6 / 13 Oct),
  BD waves A/B on sea-air (confirmed
  bookings), wave C gated in for the Cape sailings, factory lines with calendars (Bangladesh:
  Friday weekend, Durga Puja 20 Oct), one production order per open PO line, online orders
  (80 / 600, promise W45–W48, ~12 % at risk), 3 promotions (Black Friday W48), sales
  history W33–W40, forecasts per store.
* Story knobs: `seed/reference.py` (`STORY_*`, `W45_COVERED_STYLES`, `TIGHT_SIZES`) and
  `seed/reference_full.py` (`W45_COVERED_STYLES`, `KNIT_SURPLUS_RATIO`, `VN_*`). Stock is
  sized as **network** stock per DC × SKU (DC on-hand + the DC's store on-hand). Knit at
  UK1/NL1 is fully stocked; knit at DE1/PL1 covers W41–W45 and depends on the VN POs (wave 0
  covers W46 + `VN_W47_CARRY` = 20 % of W47, wave 1 arrives in W47) — so a 21-day Suez closure
  (both waves +12 days via the Cape) leaves DE1/PL1 short of knit in W46, W47 and W48.
* Golden fingerprints: sha256 over the per-table COPY CSV (`seed/dataset.py`), pinned per
  profile in `tests/test_migrations_seed.py` (`GOLDEN_DATASET_SHA256`) — bump deliberately.
* `POST /v1/admin/scenario:reset` restores the golden seed in one transaction under the seed
  advisory lock (`synchronous_commit = off`). **Merge reset** (the database already holds
  this golden dataset, `merxa.seed_state`): statement-level triggers record which golden
  tables were written since the last load (`merxa.seed_dirty_log`); only those tables are
  locked (writers wait, readers don't), their golden rows COPYed into temp staging tables
  (no WAL) and just the differing rows deleted / updated / inserted (`version` back to 1),
  so a typical post-demo reset writes a few hundred rows. **Full load** (empty database, other
  profile or dataset): TRUNCATE, then one `COPY … FORMAT csv` per table from the per-process
  golden cache (FK triggers off when the role may; PK/unique/secondary indexes of tables
  ≥ 20k rows dropped and rebuilt after the COPY). Measured for `full` (≈ 410k rows): merge
  reset after a drift in inventory/allocations/PO lines **0.5–1.5 s** (test pg) and
  **2–5 s** on a pg configured like `pg-merxa` (fsync on, 48 MB shared_buffers); full COPY
  load 3–4 s (test pg) / 7–16 s (pg-merxa-like) — first start only. `small`: < 1 s. The
  audit log, UI sessions, outbox, API keys, webhook subscriptions and snapshots survive; code
  sequences keep counting. The response carries `counts`, `dataset_sha256`, `duration_ms`.
* Injects (`POST /v1/admin/scenario:inject {kind, payload}`): `trim_delay`,
  `supplier_message {supplier_code, subject, body}` (caller text stored **verbatim** — evals
  inject adversarial text this way; subject ≤ 500 and body ≤ 20,000 characters, no NUL /
  unencodable characters, else 400; unknown supplier 404; it reaches `list_supplier_messages`
  and the `supplier_message.received` event/webhook, flagged `untrusted_text`),
  `po_eta_change {po_code, days|eta_dc}`,
  `inventory_adjust {location_type?, location_code, sku, delta_on_hand, reason?}` (`reason`: string
  ≤ 500 characters, flagged untrusted in the feed),
  `port_closure {unlocode=VNSGN, days=10, from?}` (sailings departing the port in the window
  leave later; their shipments, PO lines and PO headers arrive later; `sailing.updated` +
  `po_line.eta_changed` events), `typhoon` (= VNSGN closure + the Saigon Knit message) and
  (M5) `lane_capacity {lane_code, factor_bp, weeks, from_week?, carrier_note?}`: the lane's
  weekly capacity × `factor_bp`/10,000 (of the capacity in force, rounded down; 0..100,000,
  so a second cut compounds) for `weeks` (1..52) ISO weeks from `from_week` (default the sim
  week). The rate cards overlapping the window are split at its edges (temporal key) — the
  inner part scaled, the rest unchanged; bookings are kept (a week may be over-booked).
  Outbox/webhook `lane.capacity_changed {lane_code, factor_bp, weeks,
  effective_capacity_units (= the lowest weekly capacity in the window), from_week, to_week,
  origin, dest, mode, carrier_code, basis, weeks_detail:[{iso_week, weekly_capacity_before,
  weekly_capacity, booked_units, overbooked_units}], carrier_note?, untrusted_fields?}` with
  scn `lane_rate` changes. The structured fields are facts; `carrier_note` is the caller's
  free text (≤ 2,000 characters), flagged `untrusted_fields: ["carrier_note"]` in the data and
  `untrusted_text` in the feed. A demo reset restores the golden rate cards.

### City data
`seed/data/cities.csv` (name, country, lat, lon, population; 328 places with ≥ 15,000
inhabitants in the 16 markets) is a **hand-compiled** table in the GeoNames `cities15000`
shape (coordinates/populations rounded). To use a genuine GeoNames extract, download
`cities15000.zip` from https://download.geonames.org/export/dump/ and run
`python -m merxa_api.seed.cities_extract cities15000.txt > src/merxa_api/seed/data/cities.csv`
(then re-pin the `full` golden fingerprint). GeoNames data is licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — attribution: "GeoNames
(www.geonames.org)". No network access happens at seed time.

### Audit
`audit_log` is append-only: `REVOKE UPDATE, DELETE, TRUNCATE` plus row/statement triggers
that raise (superusers bypass grants, not triggers). Demo resets keep the audit history and
append a `scenario/reset` row.
