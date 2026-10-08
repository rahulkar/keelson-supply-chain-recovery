# Pack `foreign-erp-lite` — the generic Tier-A proof

A tenant whose ERP is a plain MCP server with generic CRUD tools (no snapshots, no change sets),
profiled on Merxa `/mcp-lite` (`services/merxa-api/src/merxa_api/mcp/lite.py`). Contract:
`docs/contracts/110-m7-backoffice-generic.md` §4.

## Set-up (back-office, tenant-admin)

1. Connectors › Add: MCP URL `http://merxa.localhost/mcp-lite`, auth **static header**
   `X-Merxa-Api-Key` = a Merxa API key with `catalog:read inventory:read po:read orders:read`
   (+ `changes:apply` for the action mapping). Slug e.g. `merxa-lite`.
2. Test, then review the tools:
   | tool | access | content trust | idempotency |
   |---|---|---|---|
   | `list_items`, `get_item` | read | trusted_data | — |
   | `update_item` | write | trusted_data | best_effort |
   | `create_transfer_order` | write | trusted_data | best_effort |
3. Activate, then install the mappings:
   `POST /api/admin/connectors/merxa-lite/mappings/from-pack {"pack": "foreign-erp-lite"}`
   (or paste `mappings/*.yaml` into the mapping editors). Check them with *Test read mapping* and
   the action *Dry-run*, then *Crawl now*.

## What is mapped

`mappings/read.yaml` → scn/1.0 (source ids as Merxa's own tier-B export):

| entity | from | notes |
|---|---|---|
| location | `dc`, `store` | + one `cluster` row per distinct store `cluster_code` (parent = the first store's `home_dc_code`) |
| item | `style`, `sku` | SKUs take family / colour / prices from their style (lookups) |
| po_line | `po_line` | supplier, factory, destination DC, ex-factory date from the `purchase_order` header (lookups) |
| inventory_position | `inventory` | |
| demand_forecast | `forecast` | store rows when `store_code` is set, else cluster rows |
| allocation | `allocation` | |

Not mapped (no such data on `/mcp-lite`): bom_line, lane, lane_rate, sailing, production_order,
size_curve, online_order_line, promotion, and **transfer_order_line** — the lite `transfer_order`
type is a header without SKU/qty lines, so lines cannot be derived without inventing data. Add an
entity mapping once the vendor exposes transfer-order lines.

`mappings/action.yaml`: `transfer_order.create` → `create_transfer_order` and
`allocation.set_qty` → `update_item(type=allocation, key, fields_json={qty}, expected_version)`,
both `best_effort` with the per-line `request_id` and a read-back before any retry. Every other op
becomes a guided step. Limitation: the transfer-order read-back matches the header (route, ship
date, `created_via = mcp-lite`) because `/mcp-lite` exposes neither lines nor the request id — a
pre-existing transfer order with the same route and date also matches.

Plans built on the crawled snapshot are **advisory** ("Data gathered by paging, not a consistent
snapshot"). At demo scale a crawl is ~300k rows / ~1,500 `list_items` pages.
