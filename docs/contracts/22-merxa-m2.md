# 22 · Merxa M2 contract — full domain, aggregates, scn export, webhooks, API keys, /mcp-lite

Extends `20-merxa.md` (everything there still holds). Merxa remains a separate product.

## 1. Full domain (new tables; PG18 conventions as before: uuidv7 ids, `code`, `version` ETag)
- `port(unlocode PK, name, country, lat, lon, kind {sea,air})` — seeds: BDCGP Chittagong, LKCMB Colombo, VNSGN Ho Chi Minh City, KHPNH Phnom Penh (river/road to VNSGN), NLRTM Rotterdam, GBFXT Felixstowe, DEHAM Hamburg, PLGDN Gdańsk, AEJEA Jebel Ali (sea-air hub), TRIST Istanbul, MATNG Tanger Med.
- `carrier(code, name, mode {sea,air,road})`
- `lane(code, origin, dest, mode {sea,air,sea_air,road}, carrier_code, transit_days, via text[] (chokepoints e.g. SUEZ, CAPE), alt_lane_code)` — origins/dests are UN/LOCODEs or DC codes; DC↔DC road lanes mirror `infra/tenants.yaml` transfer lanes.
- `rate_card(lane_code, valid daterange, basis {per_unit,per_kg,per_teu}, rate_cents, weekly_capacity, PRIMARY KEY (lane_code, valid WITHOUT OVERLAPS))` (btree_gist).
- `sailing(code, lane_code, vessel, cutoff date, etd date, eta date, capacity_teu, status {scheduled,departed,arrived,cancelled})`
- `capacity_booking(code, lane_code, iso_week, qty_units, basis, status {held,confirmed,released}, expires_at)`
- `shipment(code, po_code, mode, sailing_code null, booking_code null, origin, dest_dc_code, etd, eta, ata null, status)` + `shipment_line(shipment_code, line_no, po_line_ref, sku_code, qty)`
- `factory_line(code, factory_code, capabilities text[], std_minutes_day)`, `line_calendar(line_code, day, available_minutes, status {open,closed,reduced})`
- `production_order(code, po_line_ref, line_code, seq, planned_start, planned_end, status {planned,held,running,done}, hold_reason)`
- `online_order(code, market, channel {online,click_collect}, store_code null, promise_date, status)` + `online_order_line(order_code, line_no, sku_code, qty, reserved_qty)`
- `promotion(code, name, markets text[], grades text[], style_codes text[], period daterange, discount_bp, uplift_bp)`
- `sales_history(style_code, store_code, iso_week, units)` (last 8 weeks)
- `demand_forecast` stays **style × store × ISO week** (M2 `full`: 30 styles × 451 stores × 9 weeks ≈ 122k rows) with `size_curve(family, cluster_code, size, share_bp)`.
- `api_key(id, name, prefix, sha256, scopes text[], created_by, created_at, last_used_at, revoked_at)` — Merxa-issued API keys (show-once), used by `/mcp-lite` and REST.
- `webhook_subscription(id, name, url, events text[], secret_enc, active, created_at)` + `webhook_delivery(id, subscription_id, event_id, attempt, status, response_code, next_attempt_at)`.

## 2. Seed profile `full` (default from M2 on: `SEED_PROFILE=full`; `small` kept for fast tests)
451 stores in 16 markets (GB 160, ES 70, DE 45, IE 30, FR 25, NL 20, IT 20, PL 20, PT 15, BE 10, CZ 8, RO 8, AT 6, HU 6, SK 4, SI 4) from GeoNames `cities15000` (bundle a small CSV extract in the package with CC-BY attribution in README; no network at seed time), population-weighted with ~2 km jitter; DC mapping UK1→GB/IE, NL1→NL/BE/FR/ES/PT, DE1→DE/AT/IT/SI, PL1→PL/CZ/SK/RO/HU; clusters = DC × climate {cold,mild,warm} × grade band {A,BC} (≤24, ~18 non-empty). 2 families (Outerwear puffer/parka + Knit fleece) × 15 style-colours × 6 sizes = **180 SKUs**. Suppliers: `TRIM-BD-01` Meghna Trims (BD, wadding+zips) used by factories `F-BD-01` Gazipur, `F-BD-02` Narayanganj; `F-KH-01` Phnom Penh (own trims); `SUP-VN-01` Saigon Knit (VN, for the typhoon pack); near-shore alternates `F-TR-01` Istanbul, `F-MA-01` Tanger. 12 POs (BD→UK1/NL1 W44–W46 via Chittagong→Felixstowe/Rotterdam sailings around the Cape; KH→DE1 received; VN→PL1/DE1 W46–W47 via Ho Chi Minh City), 4 sailings, a sea-air lane `AEJEA` (Jebel Ali, weekly capacity, premium rate card), online orders (~600 with promise dates in W45–W48), 3 promotions (incl. Black Friday W48), sales history W33–W40. The **same story invariants** as `small` must hold (baseline 0 at risk; +7 days on PO-BD lines → material shortage at UK1/NL1 in W45–W46; DE1/PL1 transferable surplus; rebalance recovers most but not all), plus: a typhoon/port closure at VNSGN for 10 days creates shortage at PL1/DE1 for knit styles. Deterministic; golden sha256 pinned; `--check-story` reports both profiles.

## 3. New/extended MCP read tools (on `/mcp`; reads `readOnlyHint true`)
| tool | scope | input → output |
|---|---|---|
| `get_inventory` (extended) | inventory:read | adds `aggregate?: "none"|"cluster"|"dc"` (default none). `cluster` → rows `{location_type:"cluster", location_code, sku, on_hand, allocated, reserved, in_transit, atp, store_count}` (store rows summed; DC rows unchanged) |
| `get_demand_forecast` (extended) | inventory:read | adds `aggregate?: "store"|"cluster"` (default cluster); cluster → `{style_code, cluster_code, iso_week, units}` summed over stores |
| `list_sailings` | po:read | `lane_code?, status?, etd_from?, etd_to?` → `{items:[{code,lane_code,vessel,cutoff,etd,eta,capacity_teu,status,version}]}` |
| `list_shipments` | po:read | `po_code?, status?, dest_dc?` → items incl. lines |
| `get_lane_options` | po:read | `origin, dest, week?` → `{options:[{lane_code, mode, transit_days, via, rate_cents, basis, remaining_capacity_week, alt_lane_code}]}` |
| `get_factory_schedule` | po:read | `factory_code, from?, to?` → `{lines:[{code, calendar:[{day, available_minutes, status}], orders:[production_order…]}]}` |
| `list_online_orders_at_risk` | orders:read | `market?, promise_before?` → orders whose reserved_qty < qty |
| `list_promotions` | catalog:read | `active_on?` → items |
| `list_events` | po:read | `after_cursor?` → recent domain events (outbox) — **untrusted_text** for free-text fields |
Add matching REST endpoints (`/v1/ports`, `/v1/lanes`, `/v1/rate-cards`, `/v1/sailings`, `/v1/capacity-bookings`, `/v1/shipments`, `/v1/factory-lines`, `/v1/production-orders`, `/v1/online-orders`, `/v1/promotions`, `/v1/events`, `/v1/api-keys`, `/v1/webhook-subscriptions`).
New change-set ops: `po_line.cancel {}`, `po.create {supplier_code, factory_code, dest_dc, eta_dc, lines:[{sku, qty}]}` (alt-supplier), `po_line.split {mode:"sea_air", qty, lane_code}` (creates a shipment on the sea-air lane), `capacity_booking.create {lane_code, iso_week, qty_units}`, `production_order.resequence {line_code, order_codes:[…]}`, `promotion.reschedule {period_start, period_end}`, `online_order.repromise {promise_date}` — all with validation, before/after, audit, rollback support. Auto caps unchanged.

## 4. Canonical snapshot export (Tier B/C) — see `25-scn-1.0.md` for columns
- `scn.snapshot_open(entities: [str])` (snapshot:read; readOnly) → `{snapshot_id, as_of, watermark, canonical_model:"scn/1.0", expires_at, entities:{<name>:{rows, parts}}}`. Implementation: one REPEATABLE READ transaction writes immutable Parquet files (pyarrow) to a snapshot directory (`/data/snapshots/<id>/<entity>/part-NNNN.parquet`, ≤50k rows/part) and records the WAL LSN / outbox id as `watermark`. Snapshots expire after 2 h (cleanup task).
- `scn.snapshot_export(snapshot_id, entity)` (snapshot:read) → `{parts:[{uri, sha256, rows, bytes}]}` where `uri` = `http://merxa.localhost/v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=<unix>&sig=<hmac>` (HMAC-SHA256 with a server secret; GET returns the file; 403 if expired/bad sig; no auth header needed). May be implemented as an MCP Task (`task=True`) if FastMCP Tasks works without extra infra; otherwise synchronous (≤ a few seconds for `full`).
- `scn.changes_since(watermark)` (snapshot:read) → `{changes:[{entity, op {upsert,delete}, key, version, at}], next_watermark}` from the outbox.

## 5. Webhooks (Standard Webhooks)
Outbox events: `change_set.applied`, `change_set.rolled_back`, `po_line.eta_changed`, `supplier_message.received`, `inventory.adjusted`, `sailing.updated`. A delivery worker POSTs JSON `{type, id, timestamp, tenant_hint:null, data:{…}}` to active subscriptions with headers `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64(HMAC_SHA256(secret, id + "." + timestamp + "." + body))>`; retries with backoff (1s, 5s, 30s, 2m, 10m; max 6), marks failed after. Subscription secret format `whsec_<base64>`. Seeded subscription (if env `KEELSON_WEBHOOK_URL` and `MERXA_WEBHOOK_SECRET` are set): url `http://keelson.localhost/api/webhooks/demo-retail/merxa`, all events. Free-text in `supplier_message.received` is untrusted for consumers.
(updated M5, 2026-10-07: M5 adds six webhook types and the lane capacity inject; the full event list and the payloads are in §8.)

## 6. API keys and `/mcp-lite` (the "foreign ERP" Tier-A profile)
- Merxa admins mint API keys in the UI ("API & MCP Clients"): `mx_<prefix>_<secret>` shown once; stored as sha256; scopes subset of the read/write scopes; revocable; `last_used_at` tracked.
- `/mcp-lite` (FastMCP 4, stateless): auth via header `X-Merxa-Api-Key` **or** a Bearer token from realm merxa whose `aud` contains `http://merxa.localhost/mcp` (same audience as `/mcp`). Tools are **plain CRUD-style**, deliberately un-curated, no change-sets, no snapshots, no guide: `list_items(type, filter_json?, page?, page_size<=200)` for types {sku, style, store, dc, purchase_order, po_line, inventory, forecast, allocation, transfer_order}, `get_item(type, key)`, `update_item(type, key, fields_json, expected_version)` (write; requires write scope; idempotency via optional `request_id`), `create_transfer_order(from_dc, to_dc, ship_date, lines_json)` (write). Annotations deliberately sparse (readOnlyHint only on reads) to exercise admin classification in Keelson.

## 7. merxa-web additions
Pages: Sailings & Lanes (with rate cards and capacity), Shipments, Production schedule (per factory line, calendar strip), Online orders (at-risk filter), Promotions, Supplier Portal (messages + mark read), **API & MCP Clients** (mint/revoke API keys, show connected OAuth clients like keelson-reader with last-seen), **Webhooks** (subscriptions, recent deliveries + status). Same design system.

## 8. M5 additions (updated M5, 2026-10-07)
Contract: `95-m5-remediation.md` §2 (Guide-mode manual changes), §3 (rollback) and §4 (lane capacity inject). Code: `api/routes_m5.py`, `services/manual.py`, `services/admin.py` (`_lane_capacity`), `services/ops_m2.py` (`CapacityBookingCancel`), `services/events.py`. Migration `0003_m5.sql` is in `20` "M5".

### 8.1 Webhook events (updated M5, 2026-10-07)
- **Types.** The webhook types are the six M2 types plus `transfer_order.created`, `allocation.updated`, `po_line.updated`, `capacity_booking.created`, `capacity_booking.cancelled` and `lane.capacity_changed`.
  - The outbox type `scenario.reset` is delivered as `merxa.scenario.reset` (`WEBHOOK_TYPE_ALIASES`). `entity.updated` and `scenario.reset` themselves are internal (feed and `changes_since` only).
  - "All events" (`SUBSCRIBABLE_EVENTS`) is all of these. A seeded subscription that still lists every M2 type gains the newer types at start-up; one an admin narrowed down is left alone.
- **Manual changes** (§8.2). Each save emits one event whose `data` is the entity's state after the change, plus `{entity_type, entity_ref, version, source: "manual", via: "Merxa UI"|"API key"|"API", changed_by: {username, display}}`.

| type | `data` (besides the common fields) |
|---|---|
| `transfer_order.created` | `code, from_dc, to_dc, ship_date, eta_date, status, created_via, lines: [{line_no, sku, qty}], units` |
| `allocation.updated` | `code, qty, qty_before, status, sku, dc_code, store_code, ship_week` |
| `po_line.updated` | `ref` (`PO#line`), `po_code, line_no, action: "reschedule"\|"change_qty", sku, eta_dc, qty_ordered, qty_received, status, changed: {eta_dc\|qty_ordered: {before, after}}` |
| `capacity_booking.created` | `code, lane_code, iso_week, qty_units, basis, status, created_via` |
| `capacity_booking.cancelled` | the same, plus `status_before` and `linked_shipments` (shipments that reference the booking; left unchanged) |

- **`lane.capacity_changed`** (demo inject, §8.3) `data`:
  - `lane_code, factor_bp, weeks, from_week, to_week` (ISO weeks; the window);
  - `effective_capacity_units` (the smallest weekly capacity in the window);
  - `origin, dest, mode, carrier_code, basis`;
  - `weeks_detail: [{iso_week, weekly_capacity_before, weekly_capacity, booked_units, overbooked_units}]`. `booked_units`/`overbooked_units` are null unless the rate basis is `per_unit`, and `overbooked_units = max(0, booked − capacity)`.
  - The structured fields are facts. The optional `carrier_note` is the caller's free text: `data.untrusted_fields: ["carrier_note"]`, and `list_events` marks the event `untrusted_text`.

### 8.2 Manual-change REST (Guide mode; updated M5, 2026-10-07)
Each save runs the same op handler a change set uses (validation, version check, locks, audit rows) in one transaction, without a change set, and emits one webhook event (§8.1). A retried save with the same key replays (`Idempotent-Replayed: true`). The response is the event `data` plus `event` (its type), with an `ETag`.

| method & path | body | op | status |
|---|---|---|---|
| `POST /v1/transfer-orders` | `{from_dc, to_dc, ship_date, lines: [{sku, qty}] (1–500)}` | `transfer_order.create` | 201 |
| `GET /v1/allocations/{code}` | — | (read, `orders:read`) | 200 + `ETag` |
| `PATCH /v1/allocations/{code}` | `{qty}` + `If-Match` | `allocation.set_qty` | 200 |
| `PATCH /v1/purchase-orders/{po}/lines/{line_no}` | exactly one of `{eta_dc}` / `{qty_ordered}` + `If-Match` | `po_line.reschedule` / `po_line.change_qty` | 200 |
| `POST /v1/capacity-bookings` | `{lane_code, iso_week, qty_units}` | `capacity_booking.create` | 201 |
| `GET /v1/capacity-bookings/{code}` | — | (read, `po:read`): the booking, the `shipments` that reference it, `lane_week: {remaining_capacity, weekly_capacity, basis}`, `today` | 200 + `ETag` |
| `POST /v1/capacity-bookings/{code}:cancel` | — + `If-Match` | `capacity_booking.cancel` | 200 |

- **Who may write.**
  - People holding `merch-planner` or `ops-director` (UI session, or a realm token that also carries `changes:preview` or `changes:apply`).
  - API keys with `changes:apply`.
  - Never Keelson's clients: `keelson-*` client ids, including delegated user tokens, get 403 `manual_edit_forbidden`. Keelson changes data only through approved change sets.
- **Errors.**
  - 400 `idempotency_key_required` / `invalid_idempotency_key`.
  - 428 `precondition_required` (no `If-Match`); 400 `invalid_if_match`.
  - 412 `precondition_failed` (stale version).
  - 404 `not_found`.
  - 422 `validation_failed {errors}`.
  - 409 `conflict` (another reason found at write time).
  - 403 `missing_role` / `insufficient_scope` / `manual_edit_forbidden`.
  - 400 `invalid_parameter` (a PO-line patch with both fields or neither).

### 8.3 Demo inject `lane_capacity` (updated M5, 2026-10-07)
- **Call.** `POST /v1/admin/scenario:inject {kind: "lane_capacity", payload: {lane_code, factor_bp, weeks, from_week?, carrier_note?}}` (`DEMO_MODE` + `merxa-admin`, as in `20`). The inject kinds are `supplier_message`, `trim_delay`, `po_eta_change`, `inventory_adjust`, `port_closure`, `typhoon` and `lane_capacity`. The Demo Director page offers it.
- **Payload.**
  - `factor_bp` is an integer 0–100,000 in basis points of the capacity in force: 3000 = cut to 30 %, 0 = closed, 15000 = +50 %.
  - `weeks` is 1–52.
  - `from_week` (ISO week) defaults to the simulation's current week. It may not be in the past or more than 730 days ahead.
  - `carrier_note` is 1–2000 characters.
  - Unknown fields → 400 `invalid_parameter`. An unknown lane → 404. No rate card in the window → 409 `no_rate_card`.
- **Effect.** The rate cards overlapping the window are split at its edges. The part inside gets `weekly_capacity × factor_bp / 10,000` (rounded down) and the parts outside keep their capacity. Confirmed bookings are left alone, so a week can end up over-booked.
- **In one transaction:** audit rows (`rate_card`, action `capacity_change` / `create`), scn `lane_rate` changes, and the `lane.capacity_changed` event.
- **Response.** `{kind: "lane_capacity", …event data}`. A demo reset restores the golden cards.

### 8.4 Change-set op `capacity_booking.cancel` (updated M5, 2026-10-07)
- **Item.** `target_ref` is the booking code, `expected_version` is required, and `payload` is `{}`.
- **Validation.** The booking must exist, have status `held` or `confirmed`, and its week must not be past. An earlier item in the same set may not touch it.
- **Apply.** It sets `status: "released"`. The freed `per_unit` capacity is credited to the running simulation, so a later `capacity_booking.create` or `po_line.split` in the same change set can use it. The preview's `after` lists `linked_shipments`, which are left unchanged.
- **Rollback** restores the previous status (`revert.capacity_booking_status`). The op is not in the auto-commit set (`transfer_order.create`, `allocation.set_qty`).
- Keelson's re-plan uses it to give back over-booked capacity on a cut lane (`50` "M5 re-plan").

### 8.5 merxa-web deep-link forms (updated M5, 2026-10-07)
`get_operator_guide` steps link to pre-filled forms. Each form saves through §8.2 with an `Idempotency-Key`; record edits send `If-Match` with the planned `version` from the link.
- `/transfer-orders/new?from=&to=&ship_date=&lines=SKU:qty,…` checks the source DC's ATP per line before saving.
- `/allocations/{code}?action=set-qty&qty=&version=`
- `/purchase-orders/{po}?line=&action=reschedule&eta=&version=` or `action=change-qty&qty=&version=` opens the line's edit form. `action=cancel` / `split` only show "Review line N" (no form, no manual REST).
- `/capacity-bookings/new?lane=&week=&qty=`
- `/capacity-bookings/{code}?action=cancel&version=`
- **Gaps.**
  - The `po.create` guide link points to `/purchase-orders/new`, which merxa-web does not have: it resolves as a PO code.
  - Resequence, promotion and re-promise links open the list pages (`/production-schedule?line=`, `/promotions?code=`, `/online-orders?code=`; only online orders preselect the row).
