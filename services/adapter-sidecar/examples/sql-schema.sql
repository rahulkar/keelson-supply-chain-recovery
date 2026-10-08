-- Example ERP schema for examples/sql-postgres.yaml (the Fernhill tables as SQL tables).
-- Portable between Postgres and SQLite (the sidecar's SQL backend tests run it on both).
-- The apply marker table (keelson_sidecar_apply) is created by the sidecar itself because the
-- example sets manage_marker_table: true; a DBA can create it instead:
--   CREATE TABLE keelson_sidecar_apply (change_set_id TEXT PRIMARY KEY, purpose TEXT NOT NULL,
--                                       preview_hash TEXT, applied_at TEXT NOT NULL);

CREATE TABLE warehouses (code TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL, city TEXT,
                         lat DOUBLE PRECISION, lon DOUBLE PRECISION, rev INTEGER NOT NULL);
CREATE TABLE shops (code TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL, city TEXT,
                    lat DOUBLE PRECISION, lon DOUBLE PRECISION, grade TEXT, cluster TEXT NOT NULL, climate TEXT,
                    home_wh TEXT NOT NULL, rev INTEGER NOT NULL);
CREATE TABLE suppliers (code TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT, rev INTEGER NOT NULL);
CREATE TABLE products (code TEXT PRIMARY KEY, name TEXT NOT NULL, family TEXT NOT NULL, colour TEXT,
                       retail_price_cents INTEGER, currency TEXT, supplier TEXT, rev INTEGER NOT NULL);
CREATE TABLE skus (sku TEXT PRIMARY KEY, style TEXT NOT NULL, size TEXT NOT NULL, size_seq INTEGER,
                   unit_cost_cents INTEGER, rev INTEGER NOT NULL);
CREATE TABLE purchase_orders (po TEXT PRIMARY KEY, supplier TEXT NOT NULL, dest_wh TEXT NOT NULL, ex_factory DATE,
                              eta DATE, status TEXT NOT NULL, rev INTEGER NOT NULL);
CREATE TABLE purchase_lines (po TEXT NOT NULL, line_no INTEGER NOT NULL, sku TEXT NOT NULL,
                             qty_ordered INTEGER NOT NULL, qty_received INTEGER NOT NULL, eta DATE, status TEXT NOT NULL,
                             rev INTEGER NOT NULL, PRIMARY KEY (po, line_no));
CREATE TABLE stock (site TEXT NOT NULL, site_kind TEXT NOT NULL, sku TEXT NOT NULL, on_hand INTEGER NOT NULL,
                    allocated INTEGER NOT NULL, reserved INTEGER NOT NULL, in_transit INTEGER NOT NULL,
                    rev INTEGER NOT NULL, PRIMARY KEY (site, sku),
                    CHECK (on_hand - allocated - reserved >= 0));
CREATE TABLE weekly_forecast (style TEXT NOT NULL, shop TEXT NOT NULL, iso_week TEXT NOT NULL, units INTEGER NOT NULL,
                              rev INTEGER NOT NULL, PRIMARY KEY (style, shop, iso_week));
CREATE TABLE size_mix (family TEXT NOT NULL, cluster TEXT NOT NULL, size TEXT NOT NULL, share_bp INTEGER NOT NULL,
                       PRIMARY KEY (family, cluster, size));
CREATE TABLE store_allocations (code TEXT PRIMARY KEY, sku TEXT NOT NULL, from_wh TEXT NOT NULL, shop TEXT NOT NULL,
                                ship_week TEXT NOT NULL, qty INTEGER NOT NULL, status TEXT NOT NULL,
                                rev INTEGER NOT NULL);
CREATE TABLE transfers (code TEXT PRIMARY KEY, from_wh TEXT NOT NULL, to_wh TEXT NOT NULL, ship_date DATE NOT NULL,
                        eta_date DATE, status TEXT NOT NULL, created_via TEXT, rev INTEGER NOT NULL);
CREATE TABLE transfer_lines (transfer_code TEXT NOT NULL REFERENCES transfers (code), line_no INTEGER NOT NULL,
                             sku TEXT NOT NULL, qty INTEGER NOT NULL, rev INTEGER NOT NULL,
                             PRIMARY KEY (transfer_code, line_no));
