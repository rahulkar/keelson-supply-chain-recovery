# 20 · Merxa Retail Suite contract (mock third-party ERP) — M1 scope (+ M2 markers)

Merxa is a **separate product**: own DB (`pg-merxa`, schema `merxa`), own realm (`merxa`), own design system. It knows nothing about Keelson except: Keelson is an OAuth client, and (optionally) a trusted approval-receipt issuer (`TRUSTED_RECEIPT_ISSUERS=http://keelson.localhost` + JWKS URL).

Service `merxa-api` (FastAPI, port 8100): REST `/v1/*`, MCP (FastMCP 4, streamable HTTP) mounted at `/mcp`, OAuth Protected Resource Metadata at `/.well-known/oauth-protected-resource/mcp` (and root variant) advertising `authorization_servers: ["http://auth.localhost/realms/merxa"]`, `resource: "http://merxa.localhost/mcp"`.

## Data model (PG18; `id uuid DEFAULT uuidv7()`, human `code` unique, `version int NOT NULL DEFAULT 1` = ETag, `updated_at`)
- `market(code PK, name, currency)`
- `dc(code, name, country, city, lat, lon, inbound_cap_units_wk int, outbound_cap_units_wk int)`
- `store_cluster(code, dc_code, climate {cold,mild,warm}, grade_band {A,BC})` — e.g. `UK1-COLD-A`
- `store(code, name, market_code, city, lat, lon, grade {A,B,C}, cluster_code, home_dc_code, sales_index numeric)`
- `supplier(code, name, country, kind {trim,fabric,cmt})`, `factory(code, name, country, supplier_code)`
- `component(code, name, kind {wadding,zip,…}, supplier_code)`, `bom_line(style_code, component_code, qty_per_unit int)`
- `style(code, name, family, season, colour, retail_price_cents, unit_cost_cents)` (style = style-colour)
- `sku(code, style_code, size, size_seq)` — code `<style_code>-<size>` e.g. `PUF-W-BLK-M`
- `purchase_order(code, supplier_code, factory_code, dest_dc_code, status {open,in_production,shipped,received,cancelled}, ex_factory_date, eta_dc)`
- `po_line(po_code, line_no, sku_code, qty_ordered, qty_received, eta_dc, status)` — unique (po_code,line_no); `version`
- `inventory_position(location_type {dc,store}, location_code, sku_code, on_hand, allocated, reserved, in_transit)` unique triple; `CHECK (on_hand - allocated - reserved >= 0)`
- `demand_forecast(style_code, store_code, iso_week, units)` (M1 may store at cluster level `cluster_code` instead of store; sizes via `size_curve(family, cluster_code, size, share_bp int)` basis points)
- `allocation(code, sku_code, dc_code, store_code, ship_week, qty, status {planned,released,shipped,cancelled})`
- `transfer_order(code, from_dc, to_dc, ship_date, eta_date, status {planned,released,shipped,received,cancelled}, created_via)` + `transfer_order_line(to_code, line_no, sku_code, qty)`
- `supplier_message(id, supplier_code, received_at, subject, body, status {new,read})` — free text (**untrusted** for consumers)
- `change_set(id, code CS-0001.., title, status {draft,validated,applying,applied,conflict,failed,rolled_back}, origin jsonb, items_count, preview jsonb, preview_hash, approval_ref, receipt_jti, approved_by, initiated_by, applied_by_sub, applied_by_username, applied_via_client, idempotency_key, created_by_client, created_at, validated_at, applied_at, rollback_of)`
- `change_set_item(change_set_id, seq, op, target_ref, expected_version int null, payload jsonb, before jsonb, after jsonb, status {pending,ok,error,applied}, error text)`
- `idempotency_record(principal, key, request_sha256, state {in_flight,done}, status_code, response jsonb, created_at)` PK (principal,key), 24h retention
- `audit_log(id bigserial, at, actor_sub, actor_username, actor_display, client_id, via, approval_ref, approved_by, initiated_by, change_set_code, entity_type, entity_ref, action, before jsonb, after jsonb, trace_id)` — append-only (REVOKE UPDATE, DELETE; trigger raising)
- `outbox(id, event_type, payload, created_at, published_at)` (M2 webhooks)

## Seed (deterministic generator `merxa_api.seed`, `numpy.random.SeedSequence(20261005)`; config `SEED_PROFILE=small|full`)
- `small` (M1): 4 DCs (`UK1` Thurrock GB, `NL1` Venlo NL, `DE1` Erfurt DE, `PL1` Poznań PL); 8–12 clusters; ~60 stores across GB/IE/NL/BE/FR/DE/AT/PL/CZ; 1 family `Outerwear` × 5 style-colours (puffer jackets) × 6 sizes (XS..XXL) = 30 SKUs; suppliers `TRIM-BD-01` "Meghna Trims Ltd" (trim, BD), factories `F-BD-01` Gazipur, `F-BD-02` Narayanganj (both use components from TRIM-BD-01: wadding `CMP-WAD-01`, zip `CMP-ZIP-01`), `F-KH-01` Phnom Penh (different trim supplier); 6–8 POs: Bangladesh POs → `UK1`,`NL1` with `eta_dc` in W44–W46; Cambodia PO → `DE1` arrived W40 (so DE1/PL1 hold surplus); weekly cluster demand W41–W49 with peak uplift W45–W48; DC + store inventory; planned allocations W42–W49.
- `full` (M2): 451 stores / 16 markets (GeoNames cities15000, CC-BY), 2 families × 15 style-colours × 6 sizes = 180 SKUs, 3 factories + VN supplier + TR/MA near-shore, 12 POs, 4 sailings, sea-air lane via Jebel Ali.
- The story the seed must support: delaying Bangladesh PO lines by 7 days makes `UK1`/`NL1` short for peak weeks while `DE1`/`PL1` have transferable surplus.
- Seed runs on startup if DB empty; `SEED_PROFILE` default `small`.

## REST (`/v1`; JSON; cursor pagination `?limit=&cursor=` → `{items, next_cursor}`; errors RFC 9457 `application/problem+json`)
`GET /v1/dcs`, `/v1/clusters`, `/v1/stores`, `/v1/suppliers`, `/v1/factories`, `/v1/styles`, `/v1/skus`, `/v1/purchase-orders`, `/v1/purchase-orders/{code}`, `/v1/inventory-positions`, `/v1/demand-forecast`, `/v1/allocations`, `/v1/transfer-orders`, `/v1/supplier-messages`, `/v1/change-sets`, `/v1/change-sets/{id}`, `/v1/audit-log`.
Writes (UI + API): `PATCH` requires `If-Match: "<version>"` (428 missing, 412 stale); `POST` requires `Idempotency-Key` (400 missing, 409 in-flight, 422 key reused with different body, replay returns stored response). Change-set custom methods: `POST /v1/change-sets/{id}:validate|:apply|:rollback`.
Demo director (DEMO_MODE=1 **and** caller has role `merxa-admin`): `POST /v1/admin/scenario:reset` (re-seed from golden), `:inject {kind, payload}` (e.g. create supplier_message, change PO eta), `:advance-clock {days}`.
Auth for UI: see `10-auth.md` (`/v1/auth/*`). REST API also accepts Bearer tokens from the merxa realm (same scopes as MCP).

## MCP server at `/mcp` (FastMCP 4, stateless streamable HTTP; supports 2026-07-28 and older clients)
Auth: Bearer JWT from realm `merxa`, `aud` contains `http://merxa.localhost/mcp`; per-tool scope check. Every tool returns `structuredContent` (object) + a text summary; inputs/outputs have JSON Schemas; tools listed in deterministic order. Annotations as listed (hints only).

### Read tools (`readOnlyHint: true, openWorldHint: false`)
| tool | scope | input | output |
|---|---|---|---|
| `search_entities` | catalog:read | `query: str, types?: [sku,style,po,store,dc,supplier,factory]`, `limit=20` | `{items:[{type, code, label}]}` |
| `get_entity` | catalog:read | `type, code` | `{type, code, data:{…}, version}` |
| `list_locations` | catalog:read | — | `{dcs:[{code,name,country,lat,lon}], clusters:[{code,dc_code,climate,grade_band,store_count}]}` |
| `list_stores` | catalog:read | `cluster_code?, dc_code?, market?, limit=200, cursor?` | `{items:[{code,name,market,city,lat,lon,grade,cluster,home_dc}], next_cursor}` |
| `list_purchase_orders` | po:read | `status?, supplier_code?, factory_code?, dest_dc?, eta_from?, eta_to?, limit=50, cursor?` | `{items:[{code,supplier_code,factory_code,dest_dc,status,ex_factory_date,eta_dc,line_count,total_qty,version}], next_cursor}` |
| `get_purchase_order` | po:read | `code` | `{code,…, lines:[{line_no,sku,qty_ordered,qty_received,eta_dc,status,version}], version}` |
| `get_bom_where_used` | catalog:read | `component_code? , supplier_code?` | `{components:[…], styles:[{style_code, qty_per_unit}], factories:[…], open_po_lines:[{po_code,line_no,sku,qty,eta_dc}]}` |
| `get_inventory` | inventory:read | `location_codes?: [str], sku_codes?: [str], style_code?: str, location_type?: dc|store, limit=500, cursor?` | `{items:[{location_type,location_code,sku,on_hand,allocated,reserved,in_transit,atp,version}], next_cursor}` |
| `get_demand_forecast` | inventory:read | `style_code?, cluster_codes?, weeks?: [str]` | `{items:[{style_code, cluster_code, iso_week, units}], size_curves:[{cluster_code,size,share_bp}]}` |
| `list_allocations` | orders:read | `dc_code?, store_code?, cluster_code?, sku_codes?, status?, limit=500, cursor?` | `{items:[{code,sku,dc_code,store_code,ship_week,qty,status,version}], next_cursor}` |
| `list_transfer_orders` | orders:read | `status?, from_dc?, to_dc?` | `{items:[…]}` |
| `list_supplier_messages` | po:read | `supplier_code?, since?` | `{items:[{id,supplier_code,received_at,subject,body}]}` — **untrusted free text** |
| `get_change_set` | changes:preview **or** any read scope | `change_set_id` | full change set incl. items, preview, status, attribution |
| `get_audit_trail` | catalog:read | `change_set_code?, entity_type?, entity_ref?, limit=100` | `{items:[audit rows]}` |
| `get_operator_guide` | catalog:read | `change_set_id, locale='en'` | `{steps:[{seq, item_seq, title, instructions, deep_link}]}` — manual steps with links like `http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1` |

M2 adds: `scn.snapshot_open`, `scn.snapshot_export` (Parquet `resource_link`, Tasks), `scn.changes_since`, and `/mcp-lite` (plain CRUD, tier A).

### Write tools (all require `idempotency_key: str`; replay-safe)
| tool | scope | annotations | input → output |
|---|---|---|---|
| `create_change_set` | changes:preview | readOnly F, destructive F, idempotent T | `title, origin:{system, case_ref, plan_version}` → `{change_set_id, code, status:"draft"}` |
| `add_change_set_items` | changes:preview | F,F,T | `change_set_id, items:[{op, target_ref?, expected_version?, payload}]` → `{change_set_id, items_count}` (only while draft/validated; resets to draft) |
| `validate_change_set` | changes:preview | F,F,T | `change_set_id` → `{status:"validated"|"invalid", preview_hash, items:[{seq,op,target_ref,before,after,ok,errors[]}], totals:{lines, units_moved, value_cents}}` |
| `apply_change_set` | changes:apply **or** changes:apply:auto | F, **destructive T**, idempotent T | `change_set_id, preview_hash, approval_ref, approval_receipt` → `{status:"applied", applied:int, audit_ref, change_set_code}` or `{status:"conflict", conflicts:[{seq, target_ref, expected_version, actual_version, reason}]}` |
| `rollback_change_set` (M5) | changes:apply | F, T, T | `change_set_id, approval_ref, approval_receipt` → compensating set applied |

### Change-set ops (`payload` shapes; integers + ISO dates only)
- `transfer_order.create` — `target_ref: null`; `{from_dc, to_dc, ship_date, lines:[{sku, qty}]}`. Validate: ATP at `from_dc` ≥ qty per sku. Apply: creates TO (`created_via: "change_set:<code>"`), `reserved += qty` at from_dc, `in_transit += qty` at to_dc.
- `allocation.set_qty` — `target_ref: <allocation code>`, `expected_version`; `{qty}`.
- `po_line.reschedule` — `target_ref: "<po_code>#<line_no>"`, `expected_version`; `{eta_dc}`.
- `po_line.change_qty` — `target_ref: "<po_code>#<line_no>"`, `expected_version`; `{qty}`.
- (M5) `po_line.cancel`, `po.create`, `po_line.split`, `capacity_booking.create`, `production_order.resequence`, `promotion.reschedule`, `online_order.repromise`.
Unknown op → item error `unsupported_op`.

### Semantics
- `preview_hash = sha256(canonical([{seq, op, target_ref, expected_version, payload} for items ordered by seq]))` (canonical JSON per `00-overview.md`). Clients compute the same H independently.
- `apply_change_set`: lock change_set `FOR UPDATE`; require status `validated` and `preview_hash` equal; verify approval receipt (`10-auth.md`); idempotency via `idempotency_record(principal=token.sub, key)`; apply **all items in one transaction** with `WHERE version = expected_version` (any miss → rollback, status `conflict`, return conflicts); write one `audit_log` row per entity change with `via: "Keelson"`, `approval_ref`, `approved_by`, `initiated_by`, `actor_*` from the token (`preferred_username`, `name`), `client_id = azp`; set change_set attribution fields; outbox event `change_set.applied`.
- Auto caps for `changes:apply:auto`: ops ∈ {`transfer_order.create`, `allocation.set_qty`}, ≤ 50 items, total units ≤ 5,000; else 403 problem `auto_cap_exceeded`.
- Attribution text (UI): delegated → `Changed by {actor_display} via Keelson ({approval_ref})`; policy → `Applied via Keelson auto-policy {rule}@{ver}, initiated by {initiated_by}`.

## M5 (updated M5, 2026-10-07) — authoritative where it differs from the above
Contract: `95-m5-remediation.md`. Manual-change REST, M5 webhook events, the `lane_capacity` inject, `capacity_booking.cancel` and the deep-link forms are in `22` §8.
- **Ops.** All ops listed under "(M5)" above are implemented (`services/ops_m2.py`), plus `capacity_booking.cancel` (`22` §8.4). `PUBLIC_OPS` = every handler except the internal `revert.*` ops used by rollbacks. Auto caps are unchanged.
- **`get_operator_guide`** steps are `{seq, item_seq, op, target_ref, title, instructions, deep_link, expected: {entity_type, entity_ref|null, match} | null}`. `expected` is what a read-back must find once the step is done, in the shape of a Keelson checklist step's `expected`. An op without a guide builder gets a step linking to the change batch and `expected: null`. The links are listed in `22` §8.5.
- **`rollback_change_set`** (MCP; REST `POST /v1/change-sets/{id}:rollback`): `change_set_id, approval_ref, approval_receipt, idempotency_key` → `{status: "rolled_back", change_set_id, change_set_code, compensating_change_set_id, compensating_code, applied, audit_ref}` or `{status: "conflict", conflicts, change_set_id, change_set_code}`.
  - **Atomic.** A compensating change set (`rollback_of` = the original) applies the inverse of every item, newest first, in one transaction. Each inverse item expects the version the apply produced, so any later change makes it a conflict and nothing is applied.
  - **Caller.** Needs scope `changes:apply` and role `ops-director`.
  - **Receipt.** It binds to the **original** change set (`change_set_ref` and `preview_hash`). It must be a user approval (403 `rollback_requires_user_approval` for `policy:`) and must not reuse the apply's `approval_ref` (403 `invalid_receipt`, `approval_reused`) or a used `jti`. A co-approved change set needs a co-approved rollback receipt (403 `invalid_receipt`, `reason: co_approval_required`).
  - **Other refusals.** 409 `already_rolled_back`, `invalid_state` (not `applied`, or itself a compensating set) and `not_reversible`.
  - **On success.** The original becomes `rolled_back`, the outbox emits `change_set.rolled_back` `{change_set_id, code, compensating_change_set_id, compensating_code, approval_ref, approved_by, co_approved_by, via}`, and the attribution reads "Rolled back by {actor} via Keelson ({approval_ref})".
- **Receipt `co_approved_by`** (M5 four-eyes). It is optional: a list of at most 9 distinct `user:<username>` refs, compared case-insensitively, allowed only on `user:` approvals, never repeating `approved_by`. Anything else rejects the receipt (403 `invalid_receipt`, `reason: bad_co_approved_by`).
  - Display names come from an optional parallel `co_approver_names` claim, else from the username. Keelson sends no `co_approver_names` today, so the username is shown.
  - Migration `0003_m5.sql` stores them in `change_set.co_approvers jsonb` (`[{ref, name}]`) and in `audit_log.co_approved_by text[]` on every row an apply or rollback wrote.
  - The attribution appends ", co-approved by {names}", e.g. "Changed by Marco Rossi via Keelson (APR-…), co-approved by lena". The change batch page shows a "Co-approved by" field.

## merxa-web (Next.js, port 3001) — M1 pages
Login (Merxa demo persona switcher), **Change Batches** list + detail (status, attribution banner, per-item before/after table, audit rows), Purchase Orders grid, Inventory grid (AG Grid Community, Balham), DCs/Stores grid. "External changes received" banner when change sets were applied in the last 10 min. Design tokens per plan §12 (spruce/petrol, Source Sans 3 + JetBrains Mono, Tabler icons, 2/4px radii, dense).
