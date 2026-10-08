-- Merxa Retail Suite — initial schema (PostgreSQL 18).
-- Conventions: id uuid DEFAULT uuidv7(), human `code` unique, `version` = ETag
-- (bumped by trigger on every UPDATE), `updated_at` maintained by the same trigger.

CREATE SCHEMA IF NOT EXISTS merxa;
SET search_path TO merxa, public;

-- ───────────────────────── helpers ─────────────────────────
CREATE OR REPLACE FUNCTION merxa.touch_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.version := OLD.version + 1;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION merxa.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'merxa.%: table is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$;

-- ───────────────────────── master data ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.market (
  code        text PRIMARY KEY,
  id          uuid NOT NULL UNIQUE DEFAULT uuidv7(),
  name        text NOT NULL,
  currency    text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.dc (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  code                   text NOT NULL UNIQUE,
  name                   text NOT NULL,
  country                text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
  city                   text NOT NULL,
  lat                    double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon                    double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
  inbound_cap_units_wk   int NOT NULL CHECK (inbound_cap_units_wk >= 0),
  outbound_cap_units_wk  int NOT NULL CHECK (outbound_cap_units_wk >= 0),
  version                int NOT NULL DEFAULT 1,
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.store_cluster (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  code        text NOT NULL UNIQUE,
  dc_code     text NOT NULL REFERENCES merxa.dc(code),
  climate     text NOT NULL CHECK (climate IN ('cold','mild','warm')),
  grade_band  text NOT NULL CHECK (grade_band IN ('A','BC')),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.store (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  code          text NOT NULL UNIQUE,
  name          text NOT NULL,
  market_code   text NOT NULL REFERENCES merxa.market(code),
  city          text NOT NULL,
  lat           double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon           double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
  grade         text NOT NULL CHECK (grade IN ('A','B','C')),
  cluster_code  text NOT NULL REFERENCES merxa.store_cluster(code),
  home_dc_code  text NOT NULL REFERENCES merxa.dc(code),
  sales_index   numeric(6,3) NOT NULL CHECK (sales_index > 0),
  version       int NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS store_cluster_idx ON merxa.store (cluster_code);
CREATE INDEX IF NOT EXISTS store_home_dc_idx ON merxa.store (home_dc_code);

CREATE TABLE IF NOT EXISTS merxa.supplier (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  code        text NOT NULL UNIQUE,
  name        text NOT NULL,
  country     text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
  kind        text NOT NULL CHECK (kind IN ('trim','fabric','cmt')),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.factory (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  code           text NOT NULL UNIQUE,
  name           text NOT NULL,
  country        text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
  supplier_code  text NOT NULL REFERENCES merxa.supplier(code),
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.component (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  code           text NOT NULL UNIQUE,
  name           text NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('wadding','zip','shell_fabric','lining','trim','label','button')),
  supplier_code  text NOT NULL REFERENCES merxa.supplier(code),
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.style (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  code                text NOT NULL UNIQUE,
  name                text NOT NULL,
  family              text NOT NULL,
  season              text NOT NULL,
  colour              text NOT NULL,
  retail_price_cents  bigint NOT NULL CHECK (retail_price_cents >= 0),
  unit_cost_cents     bigint NOT NULL CHECK (unit_cost_cents >= 0),
  version             int NOT NULL DEFAULT 1,
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- bom_line.factory_code (extension): NULL = used by every factory making the style;
-- otherwise the component is sourced only by that factory (e.g. Bangladesh trims).
CREATE TABLE IF NOT EXISTS merxa.bom_line (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  style_code      text NOT NULL REFERENCES merxa.style(code),
  component_code  text NOT NULL REFERENCES merxa.component(code),
  factory_code    text NULL REFERENCES merxa.factory(code),
  qty_per_unit    int  NOT NULL CHECK (qty_per_unit > 0),
  version         int  NOT NULL DEFAULT 1,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS bom_line_uq
  ON merxa.bom_line (style_code, component_code, coalesce(factory_code, ''));

CREATE TABLE IF NOT EXISTS merxa.sku (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  code        text NOT NULL UNIQUE,
  style_code  text NOT NULL REFERENCES merxa.style(code),
  size        text NOT NULL,
  size_seq    int  NOT NULL CHECK (size_seq >= 0),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (style_code, size)
);

CREATE TABLE IF NOT EXISTS merxa.size_curve (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  family        text NOT NULL,
  cluster_code  text NOT NULL REFERENCES merxa.store_cluster(code),
  size          text NOT NULL,
  share_bp      int  NOT NULL CHECK (share_bp BETWEEN 0 AND 10000),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (family, cluster_code, size)
);

-- ───────────────────────── supply ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.purchase_order (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  code             text NOT NULL UNIQUE,
  supplier_code    text NOT NULL REFERENCES merxa.supplier(code),
  factory_code     text NOT NULL REFERENCES merxa.factory(code),
  dest_dc_code     text NOT NULL REFERENCES merxa.dc(code),
  status           text NOT NULL CHECK (status IN ('open','in_production','shipped','received','cancelled')),
  ex_factory_date  date NOT NULL,
  eta_dc           date NOT NULL,
  version          int  NOT NULL DEFAULT 1,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (eta_dc >= ex_factory_date)
);

CREATE TABLE IF NOT EXISTS merxa.po_line (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  po_code       text NOT NULL REFERENCES merxa.purchase_order(code),
  line_no       int  NOT NULL CHECK (line_no > 0),
  sku_code      text NOT NULL REFERENCES merxa.sku(code),
  qty_ordered   int  NOT NULL CHECK (qty_ordered >= 0),
  qty_received  int  NOT NULL DEFAULT 0 CHECK (qty_received >= 0),
  eta_dc        date NOT NULL,
  status        text NOT NULL CHECK (status IN ('open','in_production','shipped','received','cancelled')),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (po_code, line_no),
  CHECK (qty_received <= qty_ordered)
);
CREATE INDEX IF NOT EXISTS po_line_sku_idx ON merxa.po_line (sku_code);

-- ───────────────────────── demand & stock ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.inventory_position (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  location_type  text NOT NULL CHECK (location_type IN ('dc','store')),
  location_code  text NOT NULL,
  sku_code       text NOT NULL REFERENCES merxa.sku(code),
  on_hand        int  NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
  allocated      int  NOT NULL DEFAULT 0 CHECK (allocated >= 0),
  reserved       int  NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  in_transit     int  NOT NULL DEFAULT 0 CHECK (in_transit >= 0),
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (location_type, location_code, sku_code),
  CONSTRAINT inventory_position_atp_check CHECK (on_hand - allocated - reserved >= 0)
);

-- M1 stores the forecast at cluster level (store_code NULL); sizes come from size_curve.
CREATE TABLE IF NOT EXISTS merxa.demand_forecast (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  style_code    text NOT NULL REFERENCES merxa.style(code),
  cluster_code  text NOT NULL REFERENCES merxa.store_cluster(code),
  store_code    text NULL REFERENCES merxa.store(code),
  iso_week      text NOT NULL CHECK (iso_week ~ '^[0-9]{4}-W[0-9]{2}$'),
  units         int  NOT NULL CHECK (units >= 0),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS demand_forecast_uq
  ON merxa.demand_forecast (style_code, cluster_code, coalesce(store_code, ''), iso_week);

CREATE TABLE IF NOT EXISTS merxa.allocation (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  code        text NOT NULL UNIQUE,
  sku_code    text NOT NULL REFERENCES merxa.sku(code),
  dc_code     text NOT NULL REFERENCES merxa.dc(code),
  store_code  text NOT NULL REFERENCES merxa.store(code),
  ship_week   text NOT NULL CHECK (ship_week ~ '^[0-9]{4}-W[0-9]{2}$'),
  qty         int  NOT NULL CHECK (qty >= 0),
  status      text NOT NULL CHECK (status IN ('planned','released','shipped','cancelled')),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS allocation_store_idx ON merxa.allocation (store_code);
CREATE INDEX IF NOT EXISTS allocation_dc_idx ON merxa.allocation (dc_code, ship_week);

CREATE SEQUENCE IF NOT EXISTS merxa.transfer_order_code_seq;
CREATE TABLE IF NOT EXISTS merxa.transfer_order (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  code         text NOT NULL UNIQUE,
  from_dc      text NOT NULL REFERENCES merxa.dc(code),
  to_dc        text NOT NULL REFERENCES merxa.dc(code),
  ship_date    date NOT NULL,
  eta_date     date NOT NULL,
  status       text NOT NULL CHECK (status IN ('planned','released','shipped','received','cancelled')),
  created_via  text NOT NULL DEFAULT 'ui',
  created_at   timestamptz NOT NULL DEFAULT now(),
  version      int  NOT NULL DEFAULT 1,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (from_dc <> to_dc),
  CHECK (eta_date >= ship_date)
);

CREATE TABLE IF NOT EXISTS merxa.transfer_order_line (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  to_code     text NOT NULL REFERENCES merxa.transfer_order(code),
  line_no     int  NOT NULL CHECK (line_no > 0),
  sku_code    text NOT NULL REFERENCES merxa.sku(code),
  qty         int  NOT NULL CHECK (qty > 0),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (to_code, line_no)
);

CREATE TABLE IF NOT EXISTS merxa.supplier_message (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  supplier_code  text NOT NULL REFERENCES merxa.supplier(code),
  received_at    timestamptz NOT NULL,
  subject        text NOT NULL,
  body           text NOT NULL,
  status         text NOT NULL DEFAULT 'new' CHECK (status IN ('new','read')),
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS supplier_message_received_idx ON merxa.supplier_message (received_at, id);

-- ───────────────────────── control ─────────────────────────
CREATE SEQUENCE IF NOT EXISTS merxa.change_set_code_seq;
CREATE TABLE IF NOT EXISTS merxa.change_set (
  id                   uuid PRIMARY KEY DEFAULT uuidv7(),
  code                 text NOT NULL UNIQUE,
  title                text NOT NULL,
  status               text NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','validated','applying','applied','conflict','failed','rolled_back')),
  origin               jsonb NOT NULL DEFAULT '{}'::jsonb,
  items_count          int  NOT NULL DEFAULT 0 CHECK (items_count >= 0),
  preview              jsonb NULL,
  preview_hash         text NULL CHECK (preview_hash IS NULL OR preview_hash ~ '^[0-9a-f]{64}$'),
  result               jsonb NULL,
  approval_ref         text NULL,
  receipt_jti          text NULL UNIQUE,
  approved_by          text NULL,
  initiated_by         text NULL,
  applied_by_sub       text NULL,
  applied_by_username  text NULL,
  applied_by_display   text NULL,
  applied_via_client   text NULL,
  via                  text NULL,
  idempotency_key      text NULL,
  created_by_client    text NULL,
  created_by_sub       text NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  validated_at         timestamptz NULL,
  applied_at           timestamptz NULL,
  rollback_of          uuid NULL REFERENCES merxa.change_set(id),
  version              int  NOT NULL DEFAULT 1,
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_set_applied_idx ON merxa.change_set (applied_at);

CREATE TABLE IF NOT EXISTS merxa.change_set_item (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  change_set_id    uuid NOT NULL REFERENCES merxa.change_set(id) ON DELETE CASCADE,
  seq              int  NOT NULL CHECK (seq > 0),
  op               text NOT NULL,
  target_ref       text NULL,
  expected_version int  NULL CHECK (expected_version IS NULL OR expected_version > 0),
  payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
  before           jsonb NULL,
  after            jsonb NULL,
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ok','error','applied')),
  error            text NULL,
  version          int  NOT NULL DEFAULT 1,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (change_set_id, seq)
);

CREATE TABLE IF NOT EXISTS merxa.idempotency_record (
  principal       text NOT NULL,
  key             text NOT NULL,
  request_sha256  text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  state           text NOT NULL CHECK (state IN ('in_flight','done')),
  status_code     int  NULL,
  response        jsonb NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal, key)
);
CREATE INDEX IF NOT EXISTS idempotency_record_created_idx ON merxa.idempotency_record (created_at);

CREATE TABLE IF NOT EXISTS merxa.audit_log (
  id               bigserial PRIMARY KEY,
  at               timestamptz NOT NULL DEFAULT now(),
  actor_sub        text NULL,
  actor_username   text NULL,
  actor_display    text NULL,
  client_id        text NULL,
  via              text NULL,
  approval_ref     text NULL,
  approved_by      text NULL,
  initiated_by     text NULL,
  change_set_code  text NULL,
  entity_type      text NOT NULL,
  entity_ref       text NOT NULL,
  action           text NOT NULL,
  before           jsonb NULL,
  after            jsonb NULL,
  trace_id         text NULL
);
CREATE INDEX IF NOT EXISTS audit_log_cs_idx ON merxa.audit_log (change_set_code, id);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON merxa.audit_log (entity_type, entity_ref, id);

CREATE TABLE IF NOT EXISTS merxa.outbox (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  event_type    text NOT NULL,
  payload       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz NULL
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON merxa.outbox (created_at) WHERE published_at IS NULL;

-- Demo/simulation clock (single row). advance-clock moves it; seeds assume 2026-10-05.
CREATE TABLE IF NOT EXISTS merxa.sim_clock (
  singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  today       date NOT NULL,
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Merxa UI (BFF) sessions. `id` is HMAC-SHA256(SESSION_SECRET, cookie value) — the raw
-- cookie value is never stored.
CREATE TABLE IF NOT EXISTS merxa.ui_session (
  id            text PRIMARY KEY,
  sub           text NOT NULL,
  username      text NOT NULL,
  name          text NULL,
  email         text NULL,
  roles         text[] NOT NULL DEFAULT '{}',
  id_token      text NULL,
  demo          boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ui_session_expires_idx ON merxa.ui_session (expires_at);

-- ───────────────────────── triggers ─────────────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'market','dc','store_cluster','store','supplier','factory','component','style','bom_line',
    'sku','size_curve','purchase_order','po_line','inventory_position','demand_forecast',
    'allocation','transfer_order','transfer_order_line','supplier_message','change_set',
    'change_set_item','sim_clock'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON merxa.%I', t || '_touch', t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON merxa.%I FOR EACH ROW EXECUTE FUNCTION merxa.touch_row()',
      t || '_touch', t);
  END LOOP;
END;
$$;

-- audit_log is append-only: privileges revoked AND a trigger that raises (superusers
-- bypass privilege checks, never triggers).
REVOKE UPDATE, DELETE, TRUNCATE ON merxa.audit_log FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON merxa.audit_log FROM CURRENT_USER;
DROP TRIGGER IF EXISTS audit_log_no_update ON merxa.audit_log;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON merxa.audit_log
  FOR EACH ROW EXECUTE FUNCTION merxa.forbid_mutation();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON merxa.audit_log;
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON merxa.audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION merxa.forbid_mutation();
