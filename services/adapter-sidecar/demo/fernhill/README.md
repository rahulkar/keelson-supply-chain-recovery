# Fernhill ERP — the adapter sidecar demo (`demo/fernhill`)

Fernhill is a small fictional outdoor-apparel retailer whose "ERP" is a directory of CSV exports. The
adapter sidecar (`sidecar.yaml`) puts Keelson's tier B/C/W tools in front of it, so Keelson registers
`http://fernhill.localhost/mcp` like any third-party MCP server and detects tiers **A·B·C·W**
(contract `docs/contracts/122-m8-adapter-sidecar.md` §7).

## Data (`golden/*.csv`, written by `generate.py`)

| File | Rows | Content |
|---|---|---|
| `warehouses.csv` | 2 | FH-LDS Leeds (GB), FH-RTM Rotterdam (NL) |
| `shops.csv` | 12 | grade A/B/C, cluster `FH-LDS-A`, `FH-LDS-BC`, `FH-RTM-A`, `FH-RTM-BC`, climate, home warehouse |
| `suppliers.csv` | 2 | SUP-TR-01 (Izmir), SUP-PT-01 (Porto) |
| `products.csv` / `skus.csv` | 2 / 10 | Harbour Parka Navy, Fell Fleece Green × XS–XL; prices and costs in cents |
| `purchase_orders.csv` / `purchase_lines.csv` | 4 / 20 | one PO per style and warehouse |
| `stock.csv` | 140 | per warehouse and shop × SKU: `on_hand`, `allocated`, `reserved`, `in_transit` |
| `weekly_forecast.csv` | 216 | style × shop × 2026-W41..W49 |
| `size_mix.csv` | 40 | size shares in basis points per family × cluster |
| `store_allocations.csv` | 84 | the W45 drop (`planned`) |
| `transfers.csv` / `transfer_lines.csv` | 1 / 2 | one shipped transfer TR-00001 |

**Story.** The Navy parka purchase order for **Leeds** (PO-FH-0001) lands only in W47, so the Leeds
shops run short for the W45 drop, while **Rotterdam** holds transferable parka surplus. A transfer
FH-RTM → FH-LDS plus a few allocation trims is the obvious recovery — exactly the two ops the sidecar
offers: `transfer_order.create` (ATP check at the source, header + lines, `reserved += qty` at the
source and `in_transit += qty` at the destination) and `allocation.set_qty` (version-checked).

Eight `scn/1.0` entities are mapped (location: DCs, derived store clusters, stores, suppliers; item:
styles and SKUs; po_line; inventory_position; demand_forecast; size_curve; allocation;
transfer_order_line); the other seven are exported empty. The data passes Keelson's FK checks
(`adapter-sidecar check`).

## Regenerate

```bash
docker run --rm -v "$PWD:/src" -w /src/services/adapter-sidecar/demo/fernhill python:3.14-slim-trixie \
  python generate.py            # rewrites golden/ — byte-identical (random.Random(20261008)), tested
```

## Run it

```bash
docker compose --profile adapters up -d fernhill-sidecar   # the core stack must be up (JWKS, Caddy, egress)
```

The compose service seeds `DATA_DIR` (`/data/fernhill` in the `fernhill_data` volume) from the golden
files on first start (`DEMO_MODE`). Keys come from `.env` (`scripts/init`): `FERNHILL_API_KEY` is the
`X-Adapter-Key` you register in Keelson, `FERNHILL_ADMIN_KEY` opens the admin routes:

```bash
curl -s -H "X-Admin-Key: $FERNHILL_ADMIN_KEY" http://fernhill.localhost/v1/admin/stats
curl -s -H "X-Admin-Key: $FERNHILL_ADMIN_KEY" "http://fernhill.localhost/v1/admin/rows/stock?site=FH-RTM"
curl -s -X POST -H "X-Admin-Key: $FERNHILL_ADMIN_KEY" http://fernhill.localhost/v1/admin/reset
```

A reset restores the golden files, forgets change sets, snapshots and idempotency records, and adds a
`reset` row to the change feed (Keelson resyncs). The append-only audit log and the change-set code
counter survive a reset. Receipts are accepted for tenant `northwind-test` with `ext_sub
connector:fernhill` (register the connector with slug `fernhill`); policy receipts are capped at both
ops, 50 items and 5,000 units.
