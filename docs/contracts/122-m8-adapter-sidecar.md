# 122 · M8 adapter sidecar — scn/1.0 export (tier B), change feed (tier C) and change-set facade (tier W) in front of CSV / SQL / REST systems, with the "Fernhill" demo ERP

Plan §10 ("adapter sidecar template") and §19 M8. Builds on 20/22 (Merxa tool shapes, change sets, receipts,
idempotency), 25 (scn/1.0), 70 (connector registry, tiers), 95 (rollback, four-eyes receipts), 110 §4 (tier-A
generic proof) and the M8 seams in `120-m8-overview.md`. No model is ever called: the sidecar is deterministic and
offline by construction, and so is everything in this workstream's tests and e2e.

## 1. Scope

A reusable **Keelson-provided template service** (`services/adapter-sidecar`, Python 3.14, FastAPI + FastMCP 4)
that a customer deploys next to a system Keelson cannot reach natively, driven by one declarative YAML file. It
speaks exactly the tool surface the engine already uses with Merxa, so Keelson registers it like any third-party
MCP server and detects **tiers A·B·C·W** (CSV / SQL backends) or **A·B·C** (REST, read-only in M8) with no
engine special-casing:

- **Tier B** `scn.snapshot_open` / `scn.snapshot_export`: every `scn/1.0` entity as signed, zstd Parquet parts.
- **Tier C** `scn.changes_since`: a change journal of the sidecar's own applies / rollbacks plus detected source
  drift and resets (`full_resync_required`).
- **Tier W** `create/add/validate/apply/rollback_change_set` + `get_change_set`, `get_audit_trail`,
  `list_transfer_orders`, `get_operator_guide`: Merxa-compatible shapes, Keelson approval receipts bound to the
  sidecar's own audience, idempotency keys, an append-only audit log, compensating rollbacks.
- A **demo instance "Fernhill ERP"** (a directory of CSV files) in compose profile `adapters` at
  `http://fernhill.localhost`, plus the small engine / contracts changes that make a non-Merxa tier-W connector
  safe (per-connector receipt audience) and a non-point-in-time tier-B source honest (crawled manifests).

## 2. Shape

- One process per data source: uvicorn, **1 worker** (the write lock and the CSV directory lock are per process),
  port 8400. Routes: `/mcp` (stateless streamable HTTP, JSON responses), `GET /v1/snapshots/{id}/{entity}/{file}`
  (signed part downloads, no auth header), `/healthz`, `/readyz`, `/v1/admin/*` (admin key), and in OAuth mode
  `/.well-known/oauth-protected-resource[/mcp]`.
- One uniform pipeline for every backend: the backend yields a SQLAlchemy async connection inside a consistent
  read transaction — the real database (SQL), or **typed staging tables in the sidecar's state SQLite** (CSV,
  REST) — and per `scn/1.0` entity one configured `SELECT` produces the canonical columns. Writes go only through
  declarative **write primitives** with bound parameters (§5.2): never free SQL in writes, so effects are captured
  exactly and rollback is generic.
- State: a SQLite database (`STATE_DB_URL`, `sqlite+aiosqlite`) with its own migration runner
  (`schema_migrations`; `src/adapter_sidecar/migrations/0001_init.sql`, 120 §2).
- Modules: `config.py` (YAML v1 + static checks), `templates.py`, `settings.py` (env), `state.py` (migrations,
  sequences), `backends/{csvdir,sql,rest,staging}.py`, `scn_export.py` + `snapshots.py` (tier B), `journal.py`
  (tier C), `primitives.py` + `changesets.py` + `idempotency.py` + `receipts.py` (tier W), `auth.py`,
  `mcp_server.py`, `service.py` (wiring, admin/demo), `app.py`, `__main__.py` (CLI).

## 3. Configuration (`sidecar.yaml`, `version: 1`)

Pydantic models with `extra=forbid`, validated at start-up (fail fast) and by `check` (§6.3). Top-level keys:

| Key | Content |
|---|---|
| `system` | `{name, vendor}` |
| `backend` | `kind: csv \| sql \| rest` + exactly the matching block: `csv {delimiter, encoding}`; `sql {url_env, read_only, apply_marker_table (default keelson_sidecar_apply), manage_marker_table}`; `rest {base_url, auth {kind: none \| header \| oauth_client_credentials, header, token_env, token_url, client_id_env, client_secret_env, scope}, max_pages, max_response_bytes (10 MB), timeout_s, min_crawl_interval_s}` |
| `tables` | per table: `file` (csv) / `endpoint {path, params, items_path, pagination {kind: page \| offset \| cursor \| link, param, size_param, size, start, cursor_path, next_path}}` (rest), `columns {name: text \| int \| real \| date \| bool \| json}` (rest: `{type, path}` JSONPath-lite), `key [cols]`, `version_col?` (int), `affects {scn_entity: source_id template}` (change-feed keys), `fingerprint_sql?` (sql: drift detection) |
| `entities` | per `scn/1.0` entity `{sql}`: one `SELECT … AS source_id, …` returning exactly `SCN_COLUMNS[entity]` (checked with `LIMIT 0` at start-up). An entity left out is exported **empty** |
| `views.transfer_orders` | `{header_sql (code, from_dc, to_dc, ship_date, eta_date, status, created_via), lines_sql (code, line_no, sku, qty)}` — **required** when `transfer_order.create` is configured (the engine's reconcile calls `list_transfer_orders`) |
| `ops` | per canonical op (`OP_NAME_PATTERN`): `{target? {table, key}, check_version, code? {format "TR-{n:05d}", table, column}, steps [require \| update \| insert \| adjust], units? {for_each?, qty \| delta, price? {table, key, column}}, result_ref?, guide? {title, instructions, deep_link?, expected?, list? {for_each, item, sep}}}` |
| `invariants` | per table `[{expr {int_col: coef}, min, error}]`, checked on every changed row |
| `auth` | `static_header {header (default X-Adapter-Key), keys [{name, key_env, scopes}]}` and/or `oauth {issuer, jwks_url, audience?, scopes_claim}` |
| `receipts` | `{tenants [slug], expected_ext_sub?, ext_iss?, autocommit? {ops, max_items (50), max_units (5000)}}` |
| `snapshot` / `changes` | `{part_rows (50 000), ttl_s (7 200)}` / `{retention (10 000 journal rows)}` |

Static rules: identifiers match `^[A-Za-z_][A-Za-z0-9_]{0,62}$` (tables optionally `schema.table` for SQL) and
are always quoted; secrets only as `*_env` names (secret-like literal values and URLs with userinfo are refused);
no float literals; templates `{{ref}}` only over the roots allowed in their context (`payload`, `target_ref`,
`expected_version`, `op`, `item`, `index`, `code`, `change_set`, `before`; table columns in `affects`), and a
typed op's `payload.*` / `item.*` refs must exist in `keelson_contracts.changeset.OP_PAYLOAD_MODELS`; referenced
tables / columns must exist; key and version columns are never written by `update`/`adjust`; a table changed by
an op needs `affects`; `receipts.autocommit.ops` ⊆ configured ops; with ops configured some key must hold
`changes:apply` or `changes:apply:auto`; REST backends and SQL `read_only: true` refuse `ops`; CSV/REST table
names may not shadow the state tables. Templates render like the engine's action-mapping templates: a string that
is exactly one placeholder keeps the JSON type, mixed strings become text, a missing path is `null`; no `eval`,
no attribute access (`__` segments refused).

Examples shipped: `demo/fernhill/sidecar.yaml` (CSV, §7), `examples/sql-postgres.yaml` + `examples/sql-schema.sql`
(the Fernhill tables as SQL tables; Postgres or SQLite), `examples/rest-readonly.yaml` (paged JSON API).

## 4. Tier B and tier C

### 4.1 `scn.snapshot_open(entities)` → manifest
Unknown entity → `unknown_entity`. Sources are refreshed first (drift journalled, §4.3); then — one build at a
time — the watermark `j:<journal seq>` is read and every mapped entity's SQL runs inside one consistent read
transaction (SQL: `REPEATABLE READ, READ ONLY`, watermark read just before; SQLite: one WAL snapshot; CSV/REST:
the staging tables under the write lock). Values are converted with an explicit `SCN_ARROW_TYPES` table
(`string`, `int64`, `float64` lat/lon, `date32` from ISO text, `list<string>` from JSON arrays; a test pins its
keys to `SCN_COLUMNS`), every row is validated with `keelson_contracts.scn.parse_row`, `source_id` must be
unique per entity (else `invalid_source_data`), and parts of ≤ `part_rows` rows are written to
`SNAPSHOT_DIR/<id>/<entity>/part-NNNN.parquet` (zstd, tmp + rename, `0444`, sha256). **Every** `scn/1.0`
entity is in the manifest — an unmapped one as one empty part — because the engine sync requires all 15.
Result (validates as `keelson_contracts.scn.SnapshotManifest`): `{snapshot_id (uuid7), as_of, watermark,
canonical_model: "scn/1.0", expires_at, consistency: consistent | crawled, as_of_range: {from, to} | null,
entities {e: {rows, parts, sha256 [..]}}}`.

### 4.2 `scn.snapshot_export(snapshot_id, entity)` and downloads
`{snapshot_id, entity, rows, parts [{uri, sha256, rows, bytes}]}` + one `ResourceLink` content block per part.
`uri = PUBLIC_URL/v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=<unix>&sig=<hex>`, `sig` =
HMAC-SHA256(`SIDECAR_SNAPSHOT_SECRET`, `"<id>/<entity>/<file>:<exp>"`). The download route checks the uuid,
entity and `part-NNNN.parquet` patterns, a 64-hex signature in constant time and the expiry: 403
`invalid_signature` / `url_expired`, 404 `not_found`; `application/vnd.apache.parquet`. **`PUBLIC_URL` must be
the origin of the MCP URL registered in Keelson** — the engine refuses parts on any other host (`foreign_host`,
default deny). Without `SIDECAR_SNAPSHOT_SECRET` a random per-process secret is used (logged warning; URLs die
with the process). Housekeeping every 10 min deletes expired snapshots (directory + row), purges idempotency
records older than 24 h and prunes the journal to `changes.retention`.

### 4.3 `scn.changes_since(watermark)` (tier C)
Watermark `j:<n>` (anything else → `invalid_watermark`). Journal rows: `change` (one per applied/rolled-back
row and affected entity: `{entity, op: upsert | delete, key (affects template), version, at}`), `external`
(a source fingerprint changed without a sidecar apply: CSV file sha256, SQL `fingerprint_sql`, REST re-crawl at
most every `min_crawl_interval_s`) and `reset` (demo reset). Result `{changes, next_watermark, has_more (≤ 2000
per page), full_resync_required}` (validates as `ChangesSince`); `full_resync_required` when an `external` /
`reset` row is in range, the watermark is older than the retained journal or ahead of it (state lost). This plugs straight into the
engine's `scn_sync.poll_changes` (worker, every `scn_poll_interval_s`) → `mark_stale` → resync after the debounce.
Webhooks are deferred (§13).

## 5. Tier W

### 5.1 Tools (Merxa-compatible shapes, contract 20/22/95)
Deterministic order (`tools/list`): `get_system_info`, `scn.snapshot_open`, `scn.snapshot_export`,
`scn.changes_since`, `get_change_set`, `get_audit_trail`, `get_operator_guide`, `list_transfer_orders` (only with
`views.transfer_orders`), then — only when the config is writable — `create_change_set`, `add_change_set_items`,
`validate_change_set`, `apply_change_set`, `rollback_change_set`. Annotations: reads `readOnlyHint`; create/add/
validate non-destructive + idempotent; apply/rollback destructive + idempotent; `openWorldHint: false`
everywhere (Keelson suggests internal egress, so activation is not a guarded change). Every tool has an output
schema and returns `structuredContent` plus one summary line. Descriptions avoid the words the engine's
classifier reads as untrusted free text (message, e-mail, event, news, comment, note; pinned by a test). Errors
are tool errors `"<code>: <detail> (reason: <reason>)"` (the engine's `parse_tool_error`).

| Tool | Input → output |
|---|---|
| `get_system_info` | → `{system, vendor, backend, consistency, canonical_model, entities {e: mapped \| empty}, ops, tiers_offered}` |
| `create_change_set` | `{title, origin?, idempotency_key}` → `{change_set_id (uuid7), code (CS-0001…), status: draft}` |
| `add_change_set_items` | `{change_set_id, items [1..500] {op (enum of the configured ops), target_ref?, expected_version?, payload}, idempotency_key}` → `{change_set_id, items_count}`; items validated with `CHANGE_SET_ITEM_ADAPTER` (typed ops: no floats, ISO dates, PG int range); adding resets a validated set to draft |
| `validate_change_set` | `{change_set_id, idempotency_key}` → `{change_set_id, code, status: validated \| invalid, preview_hash, items [{seq, op, target_ref, before, after, ok, errors}], totals {lines, units_moved, value_cents}, atomic: true}`; `preview_hash = keelson_contracts.changeset.change_set_hash(items)` = the engine's `H` by construction |
| `apply_change_set` | `{change_set_id, preview_hash, approval_ref, approval_receipt, idempotency_key}` → `{status: applied, applied, audit_ref, change_set_code, change_set_id}` or `{status: conflict, conflicts [{seq, target_ref, expected_version, actual_version, reason}]}` (nothing applied) |
| `rollback_change_set` | `{change_set_id, approval_ref, approval_receipt, idempotency_key}` → `{status: rolled_back, change_set_id, change_set_code, compensating_change_set_id, compensating_code, applied, audit_ref}` or conflict |
| `get_change_set` | `{change_set_id \| code}` → the set with items (status, `result_ref`, before/after), `preview_hash`, `approval_ref`, `attribution_text` (Merxa formats: "Applied via Keelson auto-policy …, initiated by …" / "Changed by … via Keelson (APR-…)" / "Rolled back by …") |
| `get_audit_trail` | `{change_set_code?, entity_type?, entity_ref?, limit ≤ 1000}` → `{items}` newest first |
| `list_transfer_orders` | `{status?, from_dc?, to_dc?}` → `{items [{code, from_dc, to_dc, ship_date, eta_date, status, created_via, lines [{line_no, sku, qty}]}]}` |
| `get_operator_guide` | `{change_set_id, locale}` → `{change_set_id, code, steps [{seq, item_seq, op, target_ref, title, instructions, deep_link, expected}]}` rendered from the op `guide` templates |

Error codes (Merxa's where they exist): `unauthorized`, `insufficient_scope`, `not_found`, `invalid_items`,
`invalid_state`, `empty_change_set`, `preview_hash_mismatch`, `already_applied`, `already_rolled_back`,
`not_reversible`, `rollback_requires_user_approval`, `invalid_receipt` (+ reason), `auto_cap_exceeded`,
`idempotency_key_required`, `invalid_idempotency_key`, `idempotency_key_reused` (422), `idempotency_in_flight`
(409, **transient**), `receipt_jwks_unavailable` (503, **transient**), `read_only`, `unknown_entity`,
`invalid_watermark`, `invalid_source_data`, `source_invalid`. Item errors in a preview / conflict reasons:
`unsupported_op`, `not_found`, `version_mismatch`, `row_changed`, `invalid_payload`, `invalid_value`,
`duplicate_key`, `source_changed` and the configured `require` / invariant codes (Fernhill: `insufficient_atp`,
`unknown_warehouse`, `allocation_locked`, `negative_atp`).

### 5.2 Write primitives
Steps per item, optionally `for_each` element of a payload list (binds `item`, `index` 1-based):
`require {table, key, in?, at_least? {expr, value}, error, missing_error}` (checked before anything changes),
`update {table, key, set, add, bump_version}` (row locked; missing → `not_found`), `insert {table, values}`,
`adjust {table, key, add, upsert?}` (add to int columns; `upsert` inserts a missing row first). `target` +
`check_version`: the row's `version_col` must equal `expected_version` (`version_mismatch`); a target table
without a version column is compared with the before-image captured at validation (`row_changed`). `code`
allocates `TR-00001…` at **apply** time (the preview shows `null`), never reusing a code present in the table or
ever issued. Items run in order in **one** backend transaction, each in a savepoint: validate rolls everything
back (later items see earlier effects; the preview is exact), apply runs the same code in two phases (all items
checked and executed, then committed only if every item succeeded). Every mutation is recorded as an **effect**
`{kind: update | insert | delete, table, key, before, after}`; the change feed, the audit log and rollback are
derived from effects.

### 5.3 Apply, rollback, idempotency
Apply order: status `validated` and `preview_hash` match → receipt verified (§5.4) → policy receipts checked
against `receipts.autocommit` (ops ⊆ allowed, items ≤ `max_items`, preview `units_moved` ≤ `max_units`;
`auto_cap_exceeded`) → under the write lock: re-check, jti unused, one backend transaction (phase 1 lock/check
all, phase 2 mutate) → change-set/item rows, effects, audit rows (via Keelson, `approval_ref`, `approved_by`,
`approver_name`, `initiated_by`, `co_approved_by`), journal rows, `used_receipt` (jti single-use across apply and
rollback) and the idempotency completion commit together (CSV: same state transaction + staged files; SQL: the
state rows are journalled in `apply_journal` before the backend commit and replayed after it). Rollback: the
receipt must be a **user** approval bound to the **original** change set (id/code, `preview_hash`) with an
`approval_ref` different from the apply's (`approval_reused`), with co-approvers when the original was
co-approved; a compensating change set (`rollback_of`) replays the effects newest first — update → before values
+ version bump, insert → delete — each requiring that the row still equals its after-image (version or full row),
else conflict and nothing is undone. Idempotency (Merxa protocol, keyed `(principal, key)`, key 1..255
printable): `done` + same request hash → stored response replayed; different hash → 422
`idempotency_key_reused`; `in_flight` → 409 `idempotency_in_flight` unless older than `IDEMPOTENCY_STALE_S`
(120 s; then taken over). The request fingerprint excludes the receipt, so a re-minted receipt replays.

### 5.4 Receipts
Same algorithm as Merxa's verifier, written fresh: header `typ approval-receipt+jwt`, `alg ES256`, `kid`; JWKS
from `KEELSON_JWKS_URL` (TTL cache `RECEIPT_JWKS_TTL_S`, refetch on an unknown kid at most every 10 s; a fetch
failure is the transient `receipt_jwks_unavailable`); `aud` = `MCP_RESOURCE`, `iss` ∈ `TRUSTED_RECEIPT_ISSUERS`,
required claims, leeway `RECEIPT_LEEWAY_S` (30 s); bindings `change_set_hash == preview_hash`, `change_set_ref ∈
{id, code}`, `approval_id == approval_ref`, `ext_iss` (when present) == `receipts.ext_iss` or the origin of
`MCP_RESOURCE`, `tenant ∈ receipts.tenants` (when set), `ext_sub == receipts.expected_ext_sub` (when set; Keelson
sends `connector:<slug>`), `co_approved_by` shape rules; `user:` approvals need scope `changes:apply`, `policy:`
approvals `changes:apply:auto` and a configured `autocommit` block. Reasons (`invalid_receipt (reason: …)`):
`missing`, `malformed`, `wrong_typ`, `wrong_alg`, `missing_kid`, `unknown_kid`, `bad_signature`, `expired`,
`wrong_audience`, `untrusted_issuer`, `missing_claim`, `hash_mismatch`, `change_set_mismatch`,
`approval_ref_mismatch`, `ext_iss_mismatch`, `wrong_tenant`, `ext_sub_mismatch`, `insufficient_scope`,
`policy_not_accepted`, `bad_approved_by`, `bad_co_approved_by`, `replayed_jti`, `approval_reused`,
`co_approval_required`.

### 5.5 Inbound auth
- **Static header keys**: each key's value comes from the env var named by `key_env` (≥ 32 characters); only its
  sha256 is kept and a presented token is compared with every key in constant time. `HeaderKeyMiddleware` maps
  the configured header (`X-Adapter-Key`) to `Authorization: Bearer` (an explicit `Authorization` wins), so
  Keelson's `static_header` auth and plain bearer clients both work. Token: `client_id key:<name>`, the key's
  scopes.
- **OAuth** (optional): FastMCP `JWTVerifier` (issuer, JWKS, `aud` = `MCP_RESOURCE` or `auth.oauth.audience`; a
  token must carry `exp` and `sub`) in a `RemoteAuthProvider` that serves RFC 9728 metadata at
  `/.well-known/oauth-protected-resource/mcp` (and the root variant) — matching the engine's
  `oauth_client_credentials` auth kind. Static keys and OAuth may be combined.
- Scopes `catalog:read`, `orders:read`, `snapshot:read`, `changes:preview`, `changes:apply`, `changes:apply:auto`,
  enforced per tool at call time; `tools/list` shows every tool. `/v1/admin/*` uses `X-Admin-Key` =
  `SIDECAR_ADMIN_KEY`, which Keelson never receives. Logs never contain header values, keys, tokens or query
  strings (download signatures).

### 5.6 State database (`0001_init.sql`, own runner)
`change_set` (uuid7 id, unique `CS-NNNN` code, status `draft | validated | applying | applied | conflict | failed |
rolled_back`, origin, preview, preview_hash, approval_ref, unique receipt_jti, approver fields, co_approvers,
rollback_of, result, timestamps, version), `change_set_item` (pk `(change_set_id, seq)`, op, target_ref,
expected_version, payload, before, after, status, error, result_ref), `change_set_effect`, `idempotency_record`
(pk `(principal, key)`), `used_receipt` (jti pk, purpose), `audit_log` (append-only: `BEFORE UPDATE/DELETE`
triggers `RAISE`), `journal`, `source_fingerprint`, `snapshot`, `apply_journal`, `sequence`. CSV and REST staging
tables live in the same database. No engine, merxa-api or signal-hub migration (120 §2). In a SQL *backend* the
sidecar may create `keelson_sidecar_apply(change_set_id, purpose, preview_hash, applied_at)` — only with
`manage_marker_table: true` (customer-DB DDL, documented in the README, not a Keelson migration).

## 6. Backends, settings and CLI

### 6.1 Backends
- **csv** (`backends/csvdir.py`): files typed into staging tables (cached by sha256; errors name file, row and
  column); reads and applies under the process write lock and an `fcntl` lock on `DATA_DIR/.sidecar.lock` (one
  sidecar per directory). Apply: primitives on the staging tables → each touched table serialised
  deterministically (header order, decimal ints, shortest round-trip reals, ISO dates, empty = NULL, `\n`) to a
  staged `.<file>.sidecar-tmp` + fsync → every touched file must still hash to what was loaded (else conflict
  `source_changed`, files byte-identical) → `apply_journal` + state rows commit → `os.replace` + directory fsync.
  Start-up rolls a crashed apply forward (staged file present → replace; target already at the after-sha →
  fine; otherwise the change set is marked `failed` and the feed asks for a resync).
- **sql** (`backends/sql.py`): any SQLAlchemy async URL (`postgresql+asyncpg`, `sqlite+aiosqlite`) from the env
  var `url_env`; snapshot reads `REPEATABLE READ, READ ONLY`; an apply is one backend transaction (rows locked
  `FOR UPDATE` on Postgres) plus the marker row; an apply in doubt after a crash is settled at start-up from the
  marker (present → state rows written; absent → never happened); optional `fingerprint_sql` drift detection.
- **rest** (`backends/rest.py`, read-only): declarative endpoints crawled into staging tables (page / offset /
  cursor / link pagination, JSONPath-lite `items_path` and column paths, header-token or client-credentials auth
  to the API); `httpx` with `follow_redirects=False`, `trust_env=False`, optional `OUTBOUND_PROXY`; next-page
  URLs must stay on the `base_url` origin (`keelson_contracts.scn.part_uri_allowed`); per-response byte cap and
  `max_pages` (a crawl hitting a cap **fails** — a truncated table is never served). Manifests say
  `consistency: crawled` with `as_of_range` (first request … last response). No write tools are registered.

### 6.2 Environment (`settings.py`; prefix `SIDECAR_*` / demo `FERNHILL_*`, 120 §2)

| Var | Default / use |
|---|---|
| `PORT` | 8400 |
| `SIDECAR_CONFIG` | path of `sidecar.yaml` (required) |
| `PUBLIC_URL` | public origin for MCP and part URLs (required; no path / userinfo) |
| `MCP_RESOURCE` | `PUBLIC_URL/mcp`; must be on the `PUBLIC_URL` origin; receipt audience |
| `MCP_ALLOWED_HOSTS` | allowed `Host` values for `/mcp` (DNS-rebinding protection); default the `PUBLIC_URL` host, must include it |
| `STATE_DB_URL` | `sqlite+aiosqlite:////data/state/sidecar.db` (a Postgres state store is refused: deferred) |
| `DATA_DIR` | CSV directory |
| `SNAPSHOT_DIR` / `SNAPSHOT_TTL_S` | `/data/snapshots` / overrides `snapshot.ttl_s` (60..86 400) |
| `SIDECAR_SNAPSHOT_SECRET` | HMAC secret of part URLs (≥ 32 chars) |
| `SIDECAR_API_KEY` (or any `key_env` of the config) | static key(s) (≥ 32 chars) |
| `SIDECAR_ADMIN_KEY` | `/v1/admin/*` (≥ 32 chars; unset = admin routes refuse everything) |
| `KEELSON_JWKS_URL` / `TRUSTED_RECEIPT_ISSUERS` | `http://keelson.localhost/api/.well-known/jwks.json` / `http://keelson.localhost` |
| `RECEIPT_LEEWAY_S` / `RECEIPT_JWKS_TTL_S` | 30 / 300 |
| `IDEMPOTENCY_STALE_S` | 120 |
| `OUTBOUND_PROXY` | REST backend only |
| `DEMO_MODE` / `DEMO_GOLDEN_DIR` | demo features (seed an empty `DATA_DIR`, `POST /v1/admin/reset`) / `/app/demo/fernhill/golden` |
| `LOG_LEVEL` | `INFO` (JSON lines) |

`/readyz` is 200 only after the config validated, migrations ran, the backend started (staging loaded, CSV lock
taken, in-doubt applies settled), every entity SQL / view returned exactly its columns (`LIMIT 0` probe) and
`SNAPSHOT_DIR` is writable — and the backend still answers. A failing check refuses to start (logged).

### 6.3 CLI (`adapter-sidecar` / `python -m adapter_sidecar`)
`serve` (default); `check [--config]` (static validation, backend connection — a CSV directory is checked on a
temporary copy — entity SQL probes, a dry snapshot of all 15 entities and the engine's two FK checks:
`po_line.sku_code` → item of kind sku, inventory locations exist; exit 0/1); `snapshot --out DIR [--config]` (the
same dry export copied out with `manifest.json`); `hash-key` (sha256 of a key from stdin, or a new 48-char key).

### 6.4 Admin / demo routes (`X-Admin-Key`)
`GET /v1/admin/stats` → `{system, backend, tables {t: rows}, entities {e: rows}, fingerprints, watermark,
change_sets}`; `GET /v1/admin/rows/{table}?<column>=<value>…` → `{table, items}` (≤ 1000, typed values; e2e
read-back); `POST /v1/admin/reset` (`DEMO_MODE`, CSV only) restores the golden files, forgets change sets,
snapshots, idempotency records and used receipts, journals a `reset` row (Keelson resyncs) and keeps the
append-only audit log and the change-set code counter (codes never repeat).

## 7. Demo instance "Fernhill ERP"

`demo/fernhill/generate.py` (stdlib `random.Random(20261008)`, byte-identical output, tested) writes
`golden/*.csv`: 2 warehouses (FH-LDS Leeds GB, FH-RTM Rotterdam NL), 12 shops in 4 clusters
(FH-LDS-A/-BC, FH-RTM-A/-BC) with grade, climate and home warehouse, 2 suppliers, 2 styles × 5 sizes (10 SKUs,
prices/costs in cents), 4 POs × 5 lines, stock per site × SKU (`on_hand/allocated/reserved/in_transit`), a
nine-week store forecast 2026-W41..W49, size mixes (`share_bp`), 84 store allocations for the W45 drop and one
transfer. **Story**: the Navy parka PO for Leeds lands only in W47, so Leeds shops run short in W45 while
Rotterdam holds transferable surplus — a transfer FH-RTM → FH-LDS plus allocation trims is the recovery.
`demo/fernhill/sidecar.yaml` maps 8 entities (location: dc + derived cluster + store + supplier; item: style +
sku; po_line; inventory_position; demand_forecast; size_curve; allocation; transfer_order_line); the other 7 are
exported empty. The data satisfies the engine FK checks. Ops: `transfer_order.create` (ATP `on_hand − allocated −
reserved ≥ qty` per line at the from-warehouse, header with `created_via change_set:{{change_set.code}}` and code
`TR-{n:05d}`, lines, `reserved += qty` at the source and `in_transit += qty` at the destination, invariant
`negative_atp`) and `allocation.set_qty` (version-checked update; status must be `planned | released`).
`views.transfer_orders`, guide templates, `receipts {tenants: [northwind-test], expected_ext_sub:
connector:fernhill, autocommit: {both ops, 50 items, 5000 units}}`, one key `keelson` (`SIDECAR_API_KEY`) with
every scope.

## 8. Engine and contracts changes (additive; Merxa behaviour byte-identical)

- `keelson_contracts.receipts.ApprovalReceiptClaims.issue(…, aud=RECEIPT_AUDIENCE, ext_iss=MERXA_ISSUER)`: the
  defaults keep platform receipts byte-identical (golden claim test).
- `keelson_contracts.scn.SnapshotManifest` gains `consistency: SnapshotConsistency = "consistent"` and
  `as_of_range: AsOfRange | None = None` (a validator requires the range exactly for `crawled`) and a `label`
  property; `extra="ignore"` and the defaults mean Merxa manifests parse unchanged. Neither model is exported to
  JSON Schema / Zod: no contracts-ts change (`m8-contracts.sh --check` clean).
- `runtime/commit.mint_receipt(…, audience=None, ext_iss=None)` and `core/crypto.quorum_receipt_claims(…, aud,
  ext_iss)` pass through (`None` = Merxa's).
- `runtime/nodes/commit_nodes.py`: `ReceiptTarget` + `_receipt_target(run, connector)` — the platform connector
  keeps Merxa's audience; any other connector gets `aud` = its registered `mcp_url` and `ext_iss` = its issuer,
  else the URL's origin; used by `_apply_one` and `_execute_rollback`. `_credentials` already gives non-platform
  connectors principal `autocommit` and `ext_sub connector:<slug>`. A sidecar receipt cannot be replayed at Merxa
  and vice versa.
- `core/scn_sync._note_source` stores the manifest's `consistency`, `as_of_from`, `as_of_to` (columns since
  0010): a crawled tier-B export is labelled advisory exactly like a tier-A crawl.
- Compose engine env: `CONNECTOR_DEV_HOSTS: merxa.localhost,fernhill.localhost`.
No engine migration; the engine's tier detection, sync, reconcile and capability table work unchanged on the
sidecar's tool names and op enum.

## 9. Infra

- `compose.yaml` (one block): service `fernhill-sidecar` (profile `adapters`, build `services/adapter-sidecar/
  Dockerfile` from the repo root, env per §6.2 with `FERNHILL_*` secrets, volume `fernhill_data:/data` holding
  `fernhill/`, `state/`, `snapshots/`, network `edge`, `mem_limit 256m`, `cpus 0.5`, healthcheck `/healthz`), volume
  `fernhill_data`, Caddy edge alias `fernhill.localhost`, engine `CONNECTOR_DEV_HOSTS`. Start:
  `docker compose --profile adapters up -d fernhill-sidecar` (with the core stack up).
- `infra/caddy/Caddyfile`: `http://fernhill.localhost { import common; reverse_proxy
  {$FERNHILL_UPSTREAM:fernhill-sidecar:8400} { import proxy_headers; flush_interval -1 } }` (502 while the profile
  is off).
- `infra/squid/squid.conf`: `dev_hosts` adds `fernhill.localhost` (port 80, no CONNECT); `internal_names` adds
  `fernhill-sidecar`.
- `scripts/init_secrets.py` / `.env.example`: `FERNHILL_API_KEY` (40), `FERNHILL_ADMIN_KEY` (40),
  `FERNHILL_SNAPSHOT_SECRET` (48). `scripts/cloudrun/deploy.sh` `ENV_SECRETS` lists them (the Cloud Run secret
  wiring test requires every `.env.example` secret); the sidecar itself is not deployed to Cloud Run, and
  `scripts/cloudrun/tests/test_cloudrun.py` treats `fernhill` as a local-only Caddy host.
- `scripts/tests/caddy/*`: routes `/mcp`, `/v1/snapshots/…`, `/v1/admin/…`, `/healthz` → 8400 and an unbuffered
  `/mcp` stream; `scripts/verify_infra.py` `check_egress`: `fernhill.localhost` via the proxy answered by Caddy,
  `:443` and `fernhill-sidecar:8400` denied.
- `services/adapter-sidecar/Dockerfile`: repo-root context (needs `packages/contracts-py`), stages
  `base → builder → test → runtime`, uv `--frozen`, non-root uid 10001, `/data/{state,snapshots,fernhill}` owned
  by the app user, image healthcheck. Runtime image ≈ 625 MB (pyarrow), ~125 MiB RSS idle with the Fernhill data.

## 10. Security

- Apply / rollback always need a Keelson ES256 receipt bound to `preview_hash`, the change-set ref, the
  `approval_ref`, the tenant, `ext_sub` and the sidecar's own audience; a leaked API key can read and draft but
  never apply or roll back. Static-header connectors give reader, preparer and autocommit **one** credential
  (engine 70 §3), so principal separation rests on the receipts; OAuth mode allows per-role keys later.
- Payloads validated by the contract models (no floats, ISO dates, int range); writes only via declared
  primitives with bound parameters and quoted, validated identifiers; snapshot SQL in read-only transactions.
  Entity SQL and views are operator-authored config reviewed like code (no runtime config API).
- Signed, expiring, path-checked downloads (constant-time compare); parts `0444`; the engine downloads only from
  the connector origin through the egress proxy.
- Egress: Keelson → sidecar always via Squid (dev-host exception port 80 only). Sidecar → outside only the JWKS
  URL (direct on the compose `edge` network, like Merxa) and, for REST, the configured base origin (no
  redirects, size/page caps, optional proxy).
- No free-text tools; descriptions free of untrusted-trigger words; guide deep links are still checked by the
  engine's `is_safe_deep_link`. Four-eyes unchanged: registering as first party, reclassifying write → read or
  untrusted → trusted and auto-binding a write tool stay guarded change requests in the engine.
- Synced rows land in the tenant's RLS-forced `scn_*` tables through the existing sync; the sidecar is
  single-tenant (`receipts.tenants`). Non-root container; secrets only from env; logs carry key names, never
  values.

## 11. e2e (`e2e/api_m8_sidecar.py`, offline, run by the integrator)

Inside the engine image with the adapters profile up (command in `e2e/README.md`; the script reads
`FERNHILL_API_KEY` / `FERNHILL_ADMIN_KEY` from its env or the mounted `.env`). Steps: Fernhill reset (anonymous
reset and `/mcp` → 401) → register `fernhill` on northwind-test as ava (`static_header X-Adapter-Key`, third
party, key never echoed) → test: tier `[A,B,C,W]`, 13 tools, suggestions match the annotations → approve as
suggested, activate (no change request), capability table `change_set` mode with both ops from the op enum →
worker sync: consistent snapshot, counts equal `/v1/admin/stats`, same watermark → `poll_changes` False → commit
harness (in-process, the commit nodes' building blocks): change set `H`, dry run as preparer (`preview_hash ==
H`, totals), a receipt for Merxa's audience refused (`invalid_receipt`, `wrong_audience`), the policy receipt via
`_credentials` / `_receipt_target` (`aud http://fernhill.localhost/mcp`) applied, reconcile `verified` with one
transfer order `created_via change_set:<code>`, CSV read-back, same-key replay identical → tier C:
`changes_since(old watermark)` lists the rows, `poll_changes` → worker resync shows the new allocation / transfer
lines / reserved units → rollback with nora's user receipt restores the business columns and removes the
transfer order → clean-up (reset, disable; `--keep` skips). Exit 0 / 1 / 2. Syntax-checked (`py_compile`, ruff);
**not run in this workstream** (needs the full stack).

## 12. Tests

- **Sidecar** (`services/adapter-sidecar/tests`, 135 tests; Postgres variants run when `SIDECAR_TEST_PG_DSN` is
  set, `SIDECAR_TEST_PG_DB` names the throw-away database): config acceptance/rejections, templates, CSV backend
  (typed load errors, deterministic write-back, drift, only touched files rewritten, `source_changed`), crash
  recovery (roll-forward, after-sha, failed), SQL backend (SQLite + PG: read-only snapshot, one-transaction
  apply, marker, in-doubt settlement, `fingerprint_sql` drift), REST backend (page/cursor/link/offset,
  `items_path`, foreign next link refused, byte and page caps, crawled label, no write tools), scn export (all 15
  entities, empty parts, sha256, column names/types, `parse_row`, duplicate `source_id`, part splitting, manifest
  and export validate with the contract models), signed URLs (round trip, bad/non-hex/expired signature, bad
  entity, traversal, TTL cleanup), change feed (own applies, drift, reset, invalid watermark, paging,
  `ChangesSince`), change sets parametrised over csv / sqlite / postgres (draft reset, preview + `H`, errors,
  apply, audit, journal, conflict leaves nothing applied, idempotency replay/422/409/stale takeover, `get_change_set`,
  attribution, audit trail, transfer orders, operator guide), rollback (restore + version bump, later edit →
  conflict, already rolled back, policy receipt refused, reused approval refused), receipts (every claim and
  binding reason, jti replay, tenant/ext_sub, caps, JWKS 500 transient, a receipt from
  `ApprovalReceiptClaims.issue(aud=…)` verifies), auth (no/wrong/short key, Bearer, per-tool scopes, OAuth good /
  wrong aud / iss / expired, PRM, separate admin key), MCP surface (order, annotations, output schemas, op enum,
  `detect_tiers` A·B·C·W / A·B·C, `idempotency_key` required, no untrusted words, `ResourceLink`s), Fernhill demo
  (generator byte-identical, `check` passes, FK checks, admin reset restores golden + reset row), state migrations.
  Run: `docker build -f services/adapter-sidecar/Dockerfile --target test -t keelson-adapter-sidecar:test .` then
  `docker run --rm --memory 1500m keelson-adapter-sidecar:test` (add `--network` + `SIDECAR_TEST_PG_DSN` for PG).
- **Contracts** (`packages/contracts-py/tests/test_m8_sidecar.py`): `issue()` defaults = the M1 claim set
  (golden), `aud`/`ext_iss` overrides, manifest consistency rules, a Merxa-shaped manifest still parses.
- **Engine**: `tests/runtime/test_m8_receipt_audience.py` (a static-header connector's apply and rollback receipts
  carry `aud` = its URL, `ext_iss` = its origin/issuer, `ext_sub connector:<slug>`; the platform connector keeps
  Merxa's audience); `tests/platform/test_m8_manifest_consistency.py` (a fake tier-B server's crawled manifest →
  `scn_snapshot.consistency crawled`, advisory label, `as_of_range`; consistent default unchanged). Full
  `tests/platform` and `tests/runtime` suites green (known 120 §5 failures excepted).
- **Infra**: `scripts/tests/caddy/run.sh` (routes + unbuffered fernhill MCP), `scripts/tests/test_init_secrets.py`,
  `scripts/cloudrun/tests` (fernhill as a local-only host; the three failures there are pre-existing on
  `m8-post-demo`: `SIGNALS_DATA_KEY`, `EVALS_RESULTS_DIR`, the evals results mount).

## 13. Deviations from 120 and from the design brief

- None from 120's allocations: service `fernhill-sidecar`, port 8400, profile `adapters`, volume `fernhill_data`,
  host `http://fernhill.localhost`, env prefixes `SIDECAR_*` / `FERNHILL_*`, own `0001_init.sql`, additive edits to
  `receipts.py` and `scn.py`, e2e `e2e/api_m8_sidecar.py`.
- Module layout: the brief's `ops.py` is `primitives.py`, `admin.py` lives in `app.py` + `service.py`,
  `backends/sqlbase.py` is `backends/__init__.py` + `backends/staging.py`; no service `.dockerignore` (the build
  context is the repo root, whose `.dockerignore` applies).
- Config: `tables.<t>.affects` is a map `entity → source_id template` (the brief's list of entities cannot produce
  change-feed keys); `require.missing_error`, `adjust.upsert` (brief: `upsert_defaults`), `op.code {format, table,
  column}` (codes scan the table and a state sequence so they never repeat), `receipts.ext_iss`, per-key `scopes`
  for static keys, `SNAPSHOT_TTL_S` / `IDEMPOTENCY_STALE_S` env overrides.
- Signed URL message is `"<id>/<entity>/<file>:<exp>"` (the brief's "path:exp" with the path relative to
  `/v1/snapshots/`).
- Admin read-back is `GET /v1/admin/rows/{table}?<column>=<value>` (brief: `?key=`), which also serves composite
  keys.
- Demo reset keeps the audit log (append-only by trigger) and the change-set code counter, so codes are not
  `CS-0001` on every run; the e2e uses the returned code.
- `scripts/up.sh` / `up.ps1` `--adapters` flag (optional in the brief) not added, to keep shared-file edits
  minimal; the profile is started with plain `docker compose --profile adapters up -d fernhill-sidecar`.
- Image ≈ 625 MB instead of the brief's 200–250 MB estimate (pyarrow, SQLAlchemy, asyncpg, FastMCP).
- Shared test touched: `scripts/cloudrun/tests/test_cloudrun.py` (`LOCAL_ONLY_HOSTS = {"fernhill"}`), and the
  FERNHILL secrets are wired in `deploy.sh` `ENV_SECRETS` (the test requires every `.env.example` secret there).
- The e2e's optional stretch (`build_problem_from_snapshot` on the synced snapshot) is not included.

## 14. Deferred

- REST **write path** (saga with compensation and per-call `Idempotency-Key`), so REST backends could offer tier W;
  write tools stay unregistered for `rest`.
- **Standard Webhooks** emitter from the sidecar to Keelson `/api/webhooks/{tenant}/{connector}` (push tier C);
  the poll via `scn.changes_since` covers tier C in M8.
- **MCP Tasks** (async) for `scn.snapshot_export` on large exports (synchronous, like Merxa).
- **LLM-proposed** sidecar entity SQL and op config (FakeLlm path), shared with the M8 mapping-compiler area; the
  integration points are `get_system_info` and the `check` CLI.
- Back-office UI: an "Adapter sidecar" preset in the ConnectorWizard and a Playwright screenshot of the A·B·C·W
  badges (the existing wizard and badges work generically).
- Postgres state store, HA / multi-replica and multi-tenant sidecars; the engine's `/api/demo/reset` calling the
  sidecar reset; Guide read-back verification for sidecar entities.
- `scripts/up.*` `--adapters` flag (see §13).

## 15. Known issues

- One sidecar process per data source (uvicorn 1 worker); a second process on the same CSV directory is refused
  by the lock, and a human editing a CSV mid-apply is detected (`source_changed`) but never merged.
- SQL backend: the sidecar's own applies refresh `fingerprint_sql` right after their commit — a foreign write in
  that instant is not reported as drift (best effort). Tables without a version column fall back to full-row
  before-image comparison and export `src_version` from the configured SQL (recommend versioned tables).
- The demo key carries every scope for the one Keelson credential (static-header connectors share it across
  reader / preparer / autocommit); separation is by receipts.
- `PUBLIC_URL` must equal the registered connector origin and `MCP_ALLOWED_HOSTS` must include its host — both
  easy demo misconfigurations (parts refused as `foreign_host`, `/mcp` answering 421/403); `/readyz` and the README
  call them out.
- The e2e was written and syntax-checked but not executed here (it needs the full stack with the adapters
  profile); the integrator runs it.
