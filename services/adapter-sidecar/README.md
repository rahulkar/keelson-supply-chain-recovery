# Keelson adapter sidecar (`services/adapter-sidecar`)

A template service a customer deploys next to a system Keelson cannot reach natively — a directory of
CSV exports, a SQL database or a read-only REST API — so Keelson can plan on its data and change it
safely (contract `docs/contracts/122-m8-adapter-sidecar.md`, plan §10). One declarative YAML file
(`SIDECAR_CONFIG`) describes the system; the sidecar then offers over MCP the same tools Keelson
already uses with Merxa:

| Tier | Tools | What Keelson does with them |
|---|---|---|
| A | any tool | registers the connector, classifies the tools |
| B | `scn.snapshot_open`, `scn.snapshot_export` | syncs canonical `scn/1.0` snapshots (signed Parquet parts) |
| C | `scn.changes_since` | polls the change feed and resyncs when the source changed |
| W | `create_change_set`, `add_change_set_items`, `validate_change_set`, `apply_change_set`, `rollback_change_set` (+ `get_change_set`, `get_audit_trail`, `list_transfer_orders`, `get_operator_guide`) | applies approved plans atomically with approval receipts, reconciles them, undoes them |

CSV and SQL backends are tiers **A·B·C·W**; REST is **A·B·C** (read-only in M8). There is no model
anywhere: the sidecar is deterministic and offline by construction. It ships with the demo
instance **Fernhill ERP** (`demo/fernhill`, compose profile `adapters`, `http://fernhill.localhost`).

## Routes (one process: uvicorn, 1 worker, port 8400)

| Route | Auth | Purpose |
|---|---|---|
| `POST /mcp` | `X-Adapter-Key` (or `Authorization: Bearer`) / OAuth JWT | FastMCP 4, stateless streamable HTTP, JSON responses |
| `GET /v1/snapshots/{id}/{entity}/part-NNNN.parquet?exp=&sig=` | HMAC signature in the URL | signed part downloads (403 `invalid_signature` / `url_expired`, 404) |
| `GET /healthz` | — | process up |
| `GET /readyz` | — | 200 once the config, state migrations, backend start-up, entity-SQL column probes and `SNAPSHOT_DIR` all passed and the backend still answers |
| `GET /v1/admin/stats`, `GET /v1/admin/rows/{table}?col=value`, `POST /v1/admin/reset` | `X-Admin-Key` = `SIDECAR_ADMIN_KEY` (never given to Keelson) | counts / fingerprints / watermark, row read-back, demo reset (`DEMO_MODE`, CSV only) |
| `GET /.well-known/oauth-protected-resource[/mcp]` | — | RFC 9728 metadata (OAuth mode only) |

## Configuration (`sidecar.yaml`, `version: 1`)

Validated at start-up (fail fast) and by `adapter-sidecar check`; contract 122 §3 lists every key.

```yaml
version: 1
system: {name: Fernhill ERP, vendor: Fernhill}
backend: {kind: csv, csv: {delimiter: ",", encoding: utf-8}}      # or sql {url_env, …} / rest {base_url, …}
tables:                                                             # typed source tables
  stock:
    file: stock.csv
    columns: {site: text, site_kind: text, sku: text, on_hand: int, allocated: int, reserved: int,
              in_transit: int, rev: int}
    key: [site, sku]
    version_col: rev                                                # optimistic concurrency + scn src_version
    affects: {inventory_position: "{{site_kind}}/{{site}}/{{sku}}"} # change-feed key per scn entity
entities:                                                           # one SELECT per scn/1.0 entity
  inventory_position:
    sql: SELECT site_kind || '/' || site || '/' || sku AS source_id, site_kind AS location_kind, …
views: {transfer_orders: {header_sql: …, lines_sql: …}}             # needed by transfer_order.create
ops:                                                                # canonical ops as write primitives
  transfer_order.create:
    code: {format: "TR-{n:05d}", table: transfers, column: code}
    steps:
      - require: {table: stock, for_each: payload.lines, key: {…}, at_least: {expr: {on_hand: 1, allocated: -1,
                  reserved: -1}, value: "{{item.qty}}"}, error: insufficient_atp}
      - insert: {table: transfers, values: {code: "{{code}}", created_via: "change_set:{{change_set.code}}", …}}
      - adjust: {table: stock, for_each: payload.lines, key: {…}, add: {reserved: "{{item.qty}}"}}
invariants: {stock: [{expr: {on_hand: 1, allocated: -1, reserved: -1}, min: 0, error: negative_atp}]}
auth: {static_header: {header: X-Adapter-Key, keys: [{name: keelson, key_env: SIDECAR_API_KEY, scopes: [...]}]}}
receipts: {tenants: [northwind-test], expected_ext_sub: "connector:fernhill",
           autocommit: {ops: [transfer_order.create], max_items: 50, max_units: 5000}}
```

Rules worth knowing:

* Entity SQL must return exactly the entity's `scn/1.0` columns (`keelson_contracts.scn.SCN_COLUMNS`);
  every row is validated with the contract row models and `source_id` must be unique. Entities you
  leave out are exported **empty** (Keelson's sync needs all 15 in every manifest).
* Writes happen only through the primitives `require` / `update` / `insert` / `adjust` (bound
  parameters, quoted identifiers, `for_each` over a payload list) — never free SQL. Key and version
  columns are maintained by the sidecar; every changed row is checked against the table's invariants.
* Templates `{{ref}}` use the engine's rule (one exact placeholder keeps its JSON type, mixed strings
  are text, missing paths are `null`); typed ops may only reference fields of
  `keelson_contracts.changeset` payload models. No `eval`, no attribute access.
* Secrets are only ever named (`*_env`); literal secret-like values are refused.

Examples: `demo/fernhill/sidecar.yaml` (CSV), `examples/sql-postgres.yaml` + `examples/sql-schema.sql`
(Postgres or SQLite), `examples/rest-readonly.yaml` (paged JSON API).

## Backends

* **CSV** — files are typed into staging tables of the state database (errors name file, row and
  column). An apply runs on the staging tables, writes each touched file deterministically to a staged
  temp file (fsync), refuses with conflict `source_changed` if a file changed underneath (files stay
  byte-identical), journals the staged files, commits, then `os.replace`s them. A crash between commit
  and replace is rolled forward at start-up. One sidecar per directory (`fcntl` lock on
  `DATA_DIR/.sidecar.lock`). Files edited by hand are detected (sha256) and reported as drift.
* **SQL** — `backend.sql.url_env` names an env var holding a SQLAlchemy async URL
  (`postgresql+asyncpg://…` or `sqlite+aiosqlite:///…`). Snapshots read in one `REPEATABLE READ, READ
  ONLY` transaction; an apply is one transaction plus an exactly-once marker row in
  `apply_marker_table`, which settles an apply left in doubt by a crash. The sidecar creates the
  marker table only with `manage_marker_table: true`; otherwise the DBA creates it (customer-DB DDL,
  not a Keelson migration):

  ```sql
  CREATE TABLE keelson_sidecar_apply (change_set_id TEXT PRIMARY KEY, purpose TEXT NOT NULL,
                                      preview_hash TEXT, applied_at TEXT NOT NULL);
  ```

  `fingerprint_sql` per table (e.g. `SELECT count(*), max(rev) FROM stock`) lets the change feed notice
  writes by other ERP clients. Prefer versioned tables (`version_col`): without one, concurrency checks
  fall back to full-row comparison.
* **REST** (read-only) — each table names an endpoint (`page` / `offset` / `cursor` / `link`
  pagination, JSONPath-lite `items_path` and column paths, header token or OAuth client credentials).
  No redirects, `trust_env=False` (optional `OUTBOUND_PROXY`), next links must stay on `base_url`'s
  origin, byte and page caps (a capped crawl fails rather than serving a truncated table). Snapshots
  say `consistency: crawled` with their `as_of_range`; Keelson labels plans on them advisory.

## Tier W in one paragraph

`create` → `add` (items validated by `keelson_contracts.changeset`; `op` is an enum of the configured
ops, which is how Keelson learns what the sidecar supports) → `validate` (the whole set runs in one
rolled-back transaction: before/after per item, totals, `preview_hash = change_set_hash(items)`, equal to
Keelson's `H`) → `apply` with Keelson's ES256 approval receipt (typ, alg, JWKS, `aud` = `MCP_RESOURCE`,
issuer allowlist, hash / change-set / approval / tenant / `ext_sub` bindings, single-use `jti`, policy
receipts within `receipts.autocommit` caps) — atomic, all items or none, with effects, audit rows and
change-feed rows in the same commit → `rollback` with a **user** receipt for the original set: a
compensating change set restores the before-images (conflict if a row moved since). Every write tool
requires an `idempotency_key` (Merxa protocol: replay, 422 reuse, 409 in flight, stale takeover).

## Environment

| Var | Default / use |
|---|---|
| `SIDECAR_CONFIG` | the YAML file (required) |
| `PUBLIC_URL` | public origin, e.g. `http://fernhill.localhost` — **must be the origin of the MCP URL registered in Keelson** (part URLs live there; Keelson refuses parts on any other host) |
| `MCP_RESOURCE` | `PUBLIC_URL/mcp` (receipt audience) |
| `MCP_ALLOWED_HOSTS` | `Host` values `/mcp` accepts (default: the `PUBLIC_URL` host; must include it) |
| `PORT` | 8400 |
| `STATE_DB_URL` | `sqlite+aiosqlite:////data/state/sidecar.db` |
| `DATA_DIR` | CSV directory |
| `SNAPSHOT_DIR`, `SNAPSHOT_TTL_S` | `/data/snapshots`; TTL override (default `snapshot.ttl_s`, 7200) |
| `SIDECAR_SNAPSHOT_SECRET` | HMAC secret for part URLs (≥ 32 chars; unset = random per process) |
| `SIDECAR_API_KEY` (any `key_env`) | static keys (≥ 32 chars) |
| `SIDECAR_ADMIN_KEY` | admin routes (≥ 32 chars) |
| `KEELSON_JWKS_URL`, `TRUSTED_RECEIPT_ISSUERS` | `http://keelson.localhost/api/.well-known/jwks.json`, `http://keelson.localhost` |
| `RECEIPT_LEEWAY_S`, `RECEIPT_JWKS_TTL_S`, `IDEMPOTENCY_STALE_S` | 30, 300, 120 |
| `OUTBOUND_PROXY` | REST backend only |
| `DEMO_MODE`, `DEMO_GOLDEN_DIR` | seed an empty `DATA_DIR` from the golden files; enable `POST /v1/admin/reset` |
| `LOG_LEVEL` | `INFO` (JSON lines; never keys, tokens or query strings) |

## CLI

```bash
adapter-sidecar serve                     # default (uvicorn, 1 worker)
adapter-sidecar check [--config PATH]     # validate, probe entity SQL, dry snapshot of all 15 entities, Keelson's FK checks
adapter-sidecar snapshot --out DIR        # the same dry export written to DIR (+ manifest.json) for debugging
adapter-sidecar hash-key < key.txt        # sha256 of a key (or a new 48-char key when stdin is empty)
```

`check` and `snapshot` use the deployment environment (`DATA_DIR`, `*_env` secrets) with a throw-away
state store; a CSV directory is checked on a temporary copy, so a running sidecar is never disturbed.

## Connecting it to Keelson

1. Run the sidecar where Keelson's egress proxy can reach it, with `PUBLIC_URL` = its public origin.
2. Back office → Connectors → New: MCP URL `<PUBLIC_URL>/mcp`, auth **static header** `X-Adapter-Key`
   with the key from `SIDECAR_API_KEY` (or OAuth client credentials in OAuth mode), trust third party.
3. Test → tiers A·B·C·W. Review the tools (the suggestions follow the annotations: reads trusted,
   change-set tools write with idempotency enforced, apply/rollback destructive), activate, sync.
4. Keelson mints receipts for this connector with `aud = <PUBLIC_URL>/mcp`, `ext_iss = <PUBLIC_URL>`
   and `ext_sub = connector:<slug>`; put that slug in `receipts.expected_ext_sub` and the tenant in
   `receipts.tenants`.

## Demo: Fernhill ERP

```bash
docker compose --profile adapters up -d fernhill-sidecar          # with the core stack up
curl -s -H "X-Admin-Key: $FERNHILL_ADMIN_KEY" http://fernhill.localhost/v1/admin/stats
```

See `demo/fernhill/README.md` for the data and the story, and `e2e/api_m8_sidecar.py` for the full
offline proof (register → tiers → sync → change set → apply → reconcile → change feed → rollback).

## Docker and tests

```bash
# runtime image (build context = repo root: it needs packages/contracts-py)
docker build -f services/adapter-sidecar/Dockerfile -t keelson-adapter-sidecar .
# test image + run (135 tests; the Postgres variants are skipped without SIDECAR_TEST_PG_DSN)
docker build -f services/adapter-sidecar/Dockerfile --target test -t keelson-adapter-sidecar:test .
docker run --rm --memory 1500m keelson-adapter-sidecar:test
docker run --rm --memory 1500m --network <net> -e SIDECAR_TEST_PG_DSN=postgresql://postgres:<pw>@<host>:5432/postgres \
  -e SIDECAR_TEST_PG_DB=sidecar_test keelson-adapter-sidecar:test
# re-lock (pins match the other services' lockfiles)
docker run --rm -v "$PWD:/src" -w /src/services/adapter-sidecar ghcr.io/astral-sh/uv:python3.14-trixie-slim uv lock
```

The suite covers the config rules, templates, every backend (CSV, SQLite, Postgres, REST via
`respx`), crash recovery, scn export and signed URLs, the change feed, change sets,
rollback, receipts (a fake Keelson ES256 JWKS), inbound auth (static keys, OAuth), the MCP surface
(order, annotations, op enum, `detect_tiers`, no untrusted-trigger words) and the Fernhill demo
(generator byte-identical, `check`, FK checks, reset). No network is used.

## Limits

One process per data source (the write lock and the CSV lock are per process); the state store is
SQLite; REST writes, webhooks, async export tasks and a Postgres state store are deferred (contract
122 §14).
