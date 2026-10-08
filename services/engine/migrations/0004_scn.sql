-- M3 canonical store (docs/contracts/80-m3-deterministic-core.md §1, 25-scn-1.0.md):
-- snapshot registry, current pointer, plan pins, sync state and one table per scn/1.0 entity.
-- Re-runnable (IF NOT EXISTS / guarded constraint changes). Every table is a tenant table with
-- ENABLE + FORCE ROW LEVEL SECURITY and the usual tenant_isolation policy.
--
-- The entity tables carry no foreign key to scn_snapshot on purpose: an RI trigger per loaded
-- row would cost seconds per full-profile sync. core/scn_sync.py owns their life cycle (loaded
-- in the same transaction that flips the pointer, deleted by GC / connector deletion).

-- ───────────────────────────── registry ─────────────────────────────

CREATE TABLE IF NOT EXISTS scn_snapshot (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant              text NOT NULL REFERENCES tenant(slug),
  connector           text NOT NULL,
  source_snapshot_id  text,
  as_of               timestamptz,
  watermark           text,
  status              text NOT NULL DEFAULT 'loading',
  reason              text,
  row_counts          jsonb NOT NULL DEFAULT '{}'::jsonb,
  bytes               bigint NOT NULL DEFAULT 0,
  duration_ms         integer,
  error               text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  ready_at            timestamptz,
  CONSTRAINT scn_snapshot_status_chk CHECK (status IN ('loading', 'ready', 'failed', 'superseded'))
);
CREATE UNIQUE INDEX IF NOT EXISTS scn_snapshot_tenant_id_uq ON scn_snapshot (tenant, id);
CREATE INDEX IF NOT EXISTS scn_snapshot_recent_idx ON scn_snapshot (tenant, connector, created_at DESC);
-- at most one ready snapshot per connector (the pointer flip supersedes the previous one first)
CREATE UNIQUE INDEX IF NOT EXISTS scn_snapshot_one_ready_uq ON scn_snapshot (tenant, connector)
  WHERE status = 'ready';

-- pointer: the snapshot readers use (flipped atomically with the status change)
CREATE TABLE IF NOT EXISTS scn_current (
  tenant       text NOT NULL REFERENCES tenant(slug),
  connector    text NOT NULL,
  snapshot_id  uuid NOT NULL,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector),
  FOREIGN KEY (tenant, snapshot_id) REFERENCES scn_snapshot (tenant, id)
);

-- snapshots pinned by plans are never garbage-collected
CREATE TABLE IF NOT EXISTS scn_pin (
  tenant       text NOT NULL REFERENCES tenant(slug),
  snapshot_id  uuid NOT NULL,
  plan_id      bigint NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, snapshot_id, plan_id),
  FOREIGN KEY (tenant, snapshot_id) REFERENCES scn_snapshot (tenant, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant, plan_id) REFERENCES plan (tenant, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS scn_pin_plan_idx ON scn_pin (tenant, plan_id);

-- sync triggers: stale marks (webhooks, change-feed polls, admin requests) with debounce + retry
CREATE TABLE IF NOT EXISTS scn_sync_state (
  tenant           text NOT NULL REFERENCES tenant(slug),
  connector        text NOT NULL,
  stale_since      timestamptz,
  last_event_at    timestamptz,
  due_at           timestamptz,
  reason           text,
  attempts         integer NOT NULL DEFAULT 0,
  retry_at         timestamptz,
  last_attempt_at  timestamptz,
  last_error       text,
  last_poll_at     timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector)
);
CREATE INDEX IF NOT EXISTS scn_sync_state_due_idx ON scn_sync_state (due_at) WHERE due_at IS NOT NULL;

-- ───────────────────────────── scn/1.0 entities ─────────────────────────────
-- Columns in 25-scn-1.0.md order (= keelson_contracts.scn.SCN_COLUMNS) after tenant + snapshot_id.

CREATE TABLE IF NOT EXISTS scn_location (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, kind text NOT NULL, code text NOT NULL, name text, country text, city text,
  lat double precision, lon double precision, parent_code text, grade text, climate text, unlocode text,
  src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_location_code_idx ON scn_location (tenant, snapshot_id, code, kind);
CREATE INDEX IF NOT EXISTS scn_location_parent_idx ON scn_location (tenant, snapshot_id, parent_code);

CREATE TABLE IF NOT EXISTS scn_item (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, kind text NOT NULL, code text NOT NULL, style_code text, family text, colour text,
  size text, size_seq bigint, retail_price_cents bigint, unit_cost_cents bigint, currency text,
  src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_item_code_idx ON scn_item (tenant, snapshot_id, code, kind);
CREATE INDEX IF NOT EXISTS scn_item_style_idx ON scn_item (tenant, snapshot_id, style_code);

CREATE TABLE IF NOT EXISTS scn_bom_line (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, style_code text NOT NULL, component_code text NOT NULL, supplier_code text NOT NULL,
  factory_code text, qty_per_unit bigint NOT NULL, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_bom_line_style_idx ON scn_bom_line (tenant, snapshot_id, style_code);
CREATE INDEX IF NOT EXISTS scn_bom_line_supplier_idx ON scn_bom_line (tenant, snapshot_id, supplier_code);

CREATE TABLE IF NOT EXISTS scn_lane (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, code text NOT NULL, origin_code text NOT NULL, dest_code text NOT NULL,
  mode text NOT NULL, carrier_code text, transit_days bigint NOT NULL, via text[] NOT NULL DEFAULT '{}',
  alt_lane_code text, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_lane_code_idx ON scn_lane (tenant, snapshot_id, code);

CREATE TABLE IF NOT EXISTS scn_lane_rate (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, lane_code text NOT NULL, valid_from date NOT NULL, valid_to date, basis text NOT NULL,
  rate_cents bigint NOT NULL, weekly_capacity bigint, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_lane_rate_lane_idx ON scn_lane_rate (tenant, snapshot_id, lane_code, valid_from);

CREATE TABLE IF NOT EXISTS scn_sailing (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, code text NOT NULL, lane_code text NOT NULL, cutoff date, etd date, eta date,
  capacity_teu bigint, status text NOT NULL, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_sailing_code_idx ON scn_sailing (tenant, snapshot_id, code);
CREATE INDEX IF NOT EXISTS scn_sailing_lane_idx ON scn_sailing (tenant, snapshot_id, lane_code);

CREATE TABLE IF NOT EXISTS scn_po_line (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, po_code text NOT NULL, line_no bigint NOT NULL, supplier_code text NOT NULL,
  factory_code text, dest_dc_code text NOT NULL, sku_code text NOT NULL, qty_ordered bigint NOT NULL,
  qty_received bigint NOT NULL, ex_factory_date date, eta_dc date, status text NOT NULL, shipment_code text,
  sailing_code text, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_po_line_po_idx ON scn_po_line (tenant, snapshot_id, po_code, line_no);
CREATE INDEX IF NOT EXISTS scn_po_line_sku_idx ON scn_po_line (tenant, snapshot_id, sku_code);

CREATE TABLE IF NOT EXISTS scn_production_order (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, code text NOT NULL, po_line_ref text, line_code text NOT NULL, seq bigint NOT NULL,
  planned_start date NOT NULL, planned_end date NOT NULL, status text NOT NULL, hold_reason text,
  src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_production_order_line_idx ON scn_production_order (tenant, snapshot_id, line_code, seq);
CREATE INDEX IF NOT EXISTS scn_production_order_po_line_idx ON scn_production_order (tenant, snapshot_id, po_line_ref);

CREATE TABLE IF NOT EXISTS scn_inventory_position (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, location_kind text NOT NULL, location_code text NOT NULL, sku_code text NOT NULL,
  on_hand bigint NOT NULL, allocated bigint NOT NULL, reserved bigint NOT NULL, in_transit bigint NOT NULL,
  src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_inventory_position_loc_idx
  ON scn_inventory_position (tenant, snapshot_id, location_kind, location_code, sku_code);
CREATE INDEX IF NOT EXISTS scn_inventory_position_sku_idx ON scn_inventory_position (tenant, snapshot_id, sku_code);

CREATE TABLE IF NOT EXISTS scn_demand_forecast (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, style_code text NOT NULL, location_kind text NOT NULL, location_code text NOT NULL,
  iso_week text NOT NULL, units bigint NOT NULL, forecast_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_demand_forecast_key_idx
  ON scn_demand_forecast (tenant, snapshot_id, style_code, location_code, iso_week);

CREATE TABLE IF NOT EXISTS scn_size_curve (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, family text NOT NULL, cluster_code text NOT NULL, size text NOT NULL,
  share_bp bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_size_curve_key_idx ON scn_size_curve (tenant, snapshot_id, family, cluster_code);

CREATE TABLE IF NOT EXISTS scn_allocation (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, code text NOT NULL, sku_code text NOT NULL, dc_code text NOT NULL, store_code text,
  ship_week text NOT NULL, qty bigint NOT NULL, status text NOT NULL, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_allocation_key_idx ON scn_allocation (tenant, snapshot_id, sku_code, dc_code, ship_week);

CREATE TABLE IF NOT EXISTS scn_transfer_order_line (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, to_code text NOT NULL, from_dc text NOT NULL, to_dc text NOT NULL, ship_date date NOT NULL,
  eta_date date, sku_code text NOT NULL, qty bigint NOT NULL, status text NOT NULL, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_transfer_order_line_key_idx ON scn_transfer_order_line (tenant, snapshot_id, to_code);

CREATE TABLE IF NOT EXISTS scn_online_order_line (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, order_code text NOT NULL, market text NOT NULL, channel text NOT NULL, store_code text,
  sku_code text NOT NULL, qty bigint NOT NULL, reserved_qty bigint NOT NULL, promise_date date, status text NOT NULL,
  src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_online_order_line_key_idx ON scn_online_order_line (tenant, snapshot_id, order_code);

CREATE TABLE IF NOT EXISTS scn_promotion (
  tenant text NOT NULL, snapshot_id uuid NOT NULL,
  source_id text NOT NULL, code text NOT NULL, markets text[] NOT NULL DEFAULT '{}', grades text[] NOT NULL DEFAULT '{}',
  style_codes text[] NOT NULL DEFAULT '{}', period_start date NOT NULL, period_end date NOT NULL,
  discount_bp bigint NOT NULL, uplift_bp bigint NOT NULL, src_version bigint NOT NULL,
  PRIMARY KEY (tenant, snapshot_id, source_id)
);
CREATE INDEX IF NOT EXISTS scn_promotion_code_idx ON scn_promotion (tenant, snapshot_id, code);

-- ───────────────────────────── RLS ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'scn_snapshot', 'scn_current', 'scn_pin', 'scn_sync_state',
    'scn_location', 'scn_item', 'scn_bom_line', 'scn_lane', 'scn_lane_rate', 'scn_sailing', 'scn_po_line',
    'scn_production_order', 'scn_inventory_position', 'scn_demand_forecast', 'scn_size_curve',
    'scn_allocation', 'scn_transfer_order_line', 'scn_online_order_line', 'scn_promotion'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I AS PERMISSIVE FOR ALL '
      'USING (tenant = current_setting(''app.tenant_id'', true) OR current_setting(''app.system'', true) = ''on'') '
      'WITH CHECK (tenant = current_setting(''app.tenant_id'', true) OR current_setting(''app.system'', true) = ''on'')',
      t);
  END LOOP;
END $$;
