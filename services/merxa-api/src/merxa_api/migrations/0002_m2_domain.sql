-- Merxa Retail Suite — M2 (docs/contracts/22-merxa-m2.md): logistics, production, online
-- orders, promotions, sales history, API keys, webhooks, scn snapshots. Re-runnable.

SET search_path TO merxa, public;

-- PRIMARY KEY (lane_code, valid WITHOUT OVERLAPS) needs GiST equality for text.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ───────────────────────── extended checks ─────────────────────────
ALTER TABLE merxa.component DROP CONSTRAINT IF EXISTS component_kind_check;
ALTER TABLE merxa.component ADD CONSTRAINT component_kind_check
  CHECK (kind IN ('wadding','zip','shell_fabric','lining','trim','label','button','yarn','fleece_fabric'));

-- Forecast rows are style × store × ISO week from M2 on (cluster_code is kept, denormalised
-- from the store, so cluster aggregates need no join).

-- Outbox: gap-free, commit-ordered sequence (writers serialise on an advisory xact lock, see
-- services/audit.emit) used as the scn `watermark`; `changes` = scn entity changes.
ALTER TABLE merxa.outbox ADD COLUMN IF NOT EXISTS seq bigint GENERATED ALWAYS AS IDENTITY;
ALTER TABLE merxa.outbox ADD COLUMN IF NOT EXISTS changes jsonb NULL;
CREATE UNIQUE INDEX IF NOT EXISTS outbox_seq_uq ON merxa.outbox (seq);

-- Per-item apply result (created codes, new versions) — the input for rollback.
ALTER TABLE merxa.change_set_item ADD COLUMN IF NOT EXISTS result jsonb NULL;

CREATE SEQUENCE IF NOT EXISTS merxa.po_code_seq;
CREATE SEQUENCE IF NOT EXISTS merxa.shipment_code_seq;
CREATE SEQUENCE IF NOT EXISTS merxa.capacity_booking_code_seq;

-- ───────────────────────── logistics ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.port (
  unlocode    text PRIMARY KEY CHECK (unlocode ~ '^[A-Z]{2}[A-Z0-9]{3}$'),
  id          uuid NOT NULL UNIQUE DEFAULT uuidv7(),
  name        text NOT NULL,
  country     text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
  lat         double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon         double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
  kind        text NOT NULL CHECK (kind IN ('sea','air')),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.carrier (
  code        text PRIMARY KEY,
  id          uuid NOT NULL UNIQUE DEFAULT uuidv7(),
  name        text NOT NULL,
  mode        text NOT NULL CHECK (mode IN ('sea','air','road')),
  version     int  NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- origin/dest are UN/LOCODEs or DC codes; via = chokepoints/hubs (SUEZ, CAPE, AEJEA).
CREATE TABLE IF NOT EXISTS merxa.lane (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  code           text NOT NULL UNIQUE,
  origin         text NOT NULL,
  dest           text NOT NULL,
  mode           text NOT NULL CHECK (mode IN ('sea','air','sea_air','road')),
  carrier_code   text NOT NULL REFERENCES merxa.carrier(code),
  transit_days   int  NOT NULL CHECK (transit_days > 0),
  via            text[] NOT NULL DEFAULT '{}',
  alt_lane_code  text NULL,
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (origin <> dest)
);
CREATE INDEX IF NOT EXISTS lane_od_idx ON merxa.lane (origin, dest);

CREATE TABLE IF NOT EXISTS merxa.rate_card (
  id               uuid NOT NULL UNIQUE DEFAULT uuidv7(),
  lane_code        text NOT NULL REFERENCES merxa.lane(code),
  valid            daterange NOT NULL CHECK (NOT isempty(valid)),
  basis            text NOT NULL CHECK (basis IN ('per_unit','per_kg','per_teu')),
  rate_cents       bigint NOT NULL CHECK (rate_cents >= 0),
  currency         text NOT NULL DEFAULT 'EUR' CHECK (currency ~ '^[A-Z]{3}$'),
  weekly_capacity  int NOT NULL CHECK (weekly_capacity >= 0),
  version          int NOT NULL DEFAULT 1,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (lane_code, valid WITHOUT OVERLAPS)
);

CREATE TABLE IF NOT EXISTS merxa.sailing (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  code          text NOT NULL UNIQUE,
  lane_code     text NOT NULL REFERENCES merxa.lane(code),
  vessel        text NOT NULL,
  cutoff        date NOT NULL,
  etd           date NOT NULL,
  eta           date NOT NULL,
  capacity_teu  int  NOT NULL CHECK (capacity_teu >= 0),
  status        text NOT NULL CHECK (status IN ('scheduled','departed','arrived','cancelled')),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (cutoff <= etd AND etd <= eta)
);

CREATE TABLE IF NOT EXISTS merxa.capacity_booking (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  code         text NOT NULL UNIQUE,
  lane_code    text NOT NULL REFERENCES merxa.lane(code),
  iso_week     text NOT NULL CHECK (iso_week ~ '^[0-9]{4}-W[0-9]{2}$'),
  qty_units    int  NOT NULL CHECK (qty_units > 0),
  basis        text NOT NULL CHECK (basis IN ('per_unit','per_kg','per_teu')),
  status       text NOT NULL CHECK (status IN ('held','confirmed','released')),
  expires_at   timestamptz NULL,
  created_via  text NOT NULL DEFAULT 'ui',
  version      int  NOT NULL DEFAULT 1,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS capacity_booking_lane_week_idx ON merxa.capacity_booking (lane_code, iso_week);

CREATE TABLE IF NOT EXISTS merxa.shipment (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  code          text NOT NULL UNIQUE,
  po_code       text NOT NULL REFERENCES merxa.purchase_order(code),
  mode          text NOT NULL CHECK (mode IN ('sea','air','sea_air','road')),
  lane_code     text NULL REFERENCES merxa.lane(code),
  sailing_code  text NULL REFERENCES merxa.sailing(code),
  booking_code  text NULL REFERENCES merxa.capacity_booking(code),
  origin        text NOT NULL,
  dest_dc_code  text NOT NULL REFERENCES merxa.dc(code),
  etd           date NOT NULL,
  eta           date NOT NULL,
  ata           date NULL,
  status        text NOT NULL CHECK (status IN ('planned','booked','in_transit','arrived','cancelled')),
  created_via   text NOT NULL DEFAULT 'ui',
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (etd <= eta)
);
CREATE INDEX IF NOT EXISTS shipment_po_idx ON merxa.shipment (po_code);

CREATE TABLE IF NOT EXISTS merxa.shipment_line (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  shipment_code  text NOT NULL REFERENCES merxa.shipment(code),
  line_no        int  NOT NULL CHECK (line_no > 0),
  po_line_ref    text NOT NULL,
  sku_code       text NOT NULL REFERENCES merxa.sku(code),
  qty            int  NOT NULL CHECK (qty > 0),
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shipment_code, line_no)
);
CREATE INDEX IF NOT EXISTS shipment_line_ref_idx ON merxa.shipment_line (po_line_ref);

-- ───────────────────────── production ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.factory_line (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  code             text NOT NULL UNIQUE,
  factory_code     text NOT NULL REFERENCES merxa.factory(code),
  capabilities     text[] NOT NULL DEFAULT '{}',
  std_minutes_day  int  NOT NULL CHECK (std_minutes_day > 0),
  version          int  NOT NULL DEFAULT 1,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.line_calendar (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  line_code          text NOT NULL REFERENCES merxa.factory_line(code),
  day                date NOT NULL,
  available_minutes  int  NOT NULL CHECK (available_minutes >= 0),
  status             text NOT NULL CHECK (status IN ('open','closed','reduced')),
  version            int  NOT NULL DEFAULT 1,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (line_code, day)
);

CREATE TABLE IF NOT EXISTS merxa.production_order (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  code           text NOT NULL UNIQUE,
  po_line_ref    text NOT NULL,
  line_code      text NOT NULL REFERENCES merxa.factory_line(code),
  seq            int  NOT NULL CHECK (seq > 0),
  planned_start  date NOT NULL,
  planned_end    date NOT NULL,
  status         text NOT NULL CHECK (status IN ('planned','held','running','done')),
  hold_reason    text NULL,
  version        int  NOT NULL DEFAULT 1,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (planned_end >= planned_start),
  CONSTRAINT production_order_line_seq_uq UNIQUE (line_code, seq) DEFERRABLE INITIALLY DEFERRED
);

-- ───────────────────────── online orders, promotions, history ─────────────────────────
CREATE TABLE IF NOT EXISTS merxa.online_order (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  code          text NOT NULL UNIQUE,
  market        text NOT NULL REFERENCES merxa.market(code),
  channel       text NOT NULL CHECK (channel IN ('online','click_collect')),
  store_code    text NULL REFERENCES merxa.store(code),
  promise_date  date NOT NULL,
  status        text NOT NULL CHECK (status IN ('open','picking','shipped','delivered','cancelled')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (channel <> 'click_collect' OR store_code IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS online_order_promise_idx ON merxa.online_order (promise_date);

CREATE TABLE IF NOT EXISTS merxa.online_order_line (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  order_code    text NOT NULL REFERENCES merxa.online_order(code),
  line_no       int  NOT NULL CHECK (line_no > 0),
  sku_code      text NOT NULL REFERENCES merxa.sku(code),
  qty           int  NOT NULL CHECK (qty > 0),
  reserved_qty  int  NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
  version       int  NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_code, line_no),
  CHECK (reserved_qty <= qty)
);

-- period is inclusive of both ends: daterange(period_start, period_end, '[]').
CREATE TABLE IF NOT EXISTS merxa.promotion (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  code         text NOT NULL UNIQUE,
  name         text NOT NULL,
  markets      text[] NOT NULL DEFAULT '{}',
  grades       text[] NOT NULL DEFAULT '{}',
  style_codes  text[] NOT NULL DEFAULT '{}',
  period       daterange NOT NULL CHECK (NOT isempty(period)),
  discount_bp  int NOT NULL CHECK (discount_bp BETWEEN 0 AND 10000),
  uplift_bp    int NOT NULL CHECK (uplift_bp >= 0),
  version      int NOT NULL DEFAULT 1,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Last 8 weeks of sell-through per style × store (bulk history; no ETag).
CREATE TABLE IF NOT EXISTS merxa.sales_history (
  style_code  text NOT NULL REFERENCES merxa.style(code),
  store_code  text NOT NULL REFERENCES merxa.store(code),
  iso_week    text NOT NULL CHECK (iso_week ~ '^[0-9]{4}-W[0-9]{2}$'),
  units       int  NOT NULL CHECK (units >= 0),
  PRIMARY KEY (style_code, store_code, iso_week)
);

-- ───────────────────────── integration: API keys, webhooks, snapshots ─────────────────────────
-- API keys are `mx_<prefix>_<secret>`; only sha256(key) is stored (shown once at mint).
CREATE TABLE IF NOT EXISTS merxa.api_key (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  name          text NOT NULL,
  prefix        text NOT NULL UNIQUE CHECK (prefix ~ '^[a-z0-9]{8}$'),
  sha256        text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  scopes        text[] NOT NULL,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz NULL,
  revoked_at    timestamptz NULL,
  version       int NOT NULL DEFAULT 1,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- secret_enc = AES-256-GCM(key derived from MERXA_DATA_KEY / SESSION_SECRET) of `whsec_…`.
CREATE TABLE IF NOT EXISTS merxa.webhook_subscription (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  name        text NOT NULL UNIQUE,
  url         text NOT NULL CHECK (url ~ '^https?://'),
  events      text[] NOT NULL,
  secret_enc  text NOT NULL,
  active      boolean NOT NULL DEFAULT true,
  created_by  text NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  version     int NOT NULL DEFAULT 1,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merxa.webhook_delivery (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  subscription_id  uuid NOT NULL REFERENCES merxa.webhook_subscription(id) ON DELETE CASCADE,
  event_id         uuid NOT NULL,
  event_type       text NOT NULL,
  attempt          int  NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  status           text NOT NULL CHECK (status IN ('pending','delivered','failed')),
  response_code    int  NULL,
  last_error       text NULL,
  next_attempt_at  timestamptz NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  delivered_at     timestamptz NULL,
  UNIQUE (subscription_id, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_delivery_due_idx ON merxa.webhook_delivery (next_attempt_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS merxa.scn_snapshot (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  as_of       timestamptz NOT NULL,
  watermark   text NOT NULL,
  wal_lsn     text NULL,
  manifest    jsonb NOT NULL,
  created_by  text NULL
);
CREATE INDEX IF NOT EXISTS scn_snapshot_expires_idx ON merxa.scn_snapshot (expires_at);

-- Which golden dataset the domain tables hold (resets merge against it when it matches).
CREATE TABLE IF NOT EXISTS merxa.seed_state (
  singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  profile         text NOT NULL,
  dataset_sha256  text NOT NULL,
  loaded_at       timestamptz NOT NULL DEFAULT now()
);

-- Golden tables written since the last seed load (statement-level triggers below). A demo
-- reset merges only these tables back. Insert-if-absent, no updates: writers never wait.
CREATE TABLE IF NOT EXISTS merxa.seed_dirty_log (
  id          bigserial PRIMARY KEY,
  table_name  text NOT NULL,
  at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS seed_dirty_log_table_idx ON merxa.seed_dirty_log (table_name);

CREATE OR REPLACE FUNCTION merxa.mark_seed_dirty() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO merxa.seed_dirty_log(table_name)
  SELECT TG_TABLE_NAME
   WHERE NOT EXISTS (SELECT 1 FROM merxa.seed_dirty_log WHERE table_name = TG_TABLE_NAME);
  RETURN NULL;
END;
$$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'market','dc','store_cluster','store','supplier','factory','component','style','sku','bom_line',
    'size_curve','purchase_order','po_line','inventory_position','demand_forecast','allocation',
    'transfer_order','transfer_order_line','supplier_message','sim_clock','port','carrier','lane',
    'rate_card','sailing','capacity_booking','shipment','shipment_line','factory_line','line_calendar',
    'production_order','online_order','online_order_line','promotion','sales_history'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON merxa.%I', t || '_seed_dirty', t);
    EXECUTE format(
      'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON merxa.%I '
      'FOR EACH STATEMENT EXECUTE FUNCTION merxa.mark_seed_dirty()', t || '_seed_dirty', t);
  END LOOP;
END;
$$;

-- OAuth clients seen on /mcp, /mcp-lite and REST (UI "API & MCP Clients": last seen).
CREATE TABLE IF NOT EXISTS merxa.oauth_client_seen (
  client_id      text PRIMARY KEY,
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  last_sub       text NULL,
  last_username  text NULL,
  surface        text NULL
);

-- ───────────────────────── triggers ─────────────────────────
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'port','carrier','lane','rate_card','sailing','capacity_booking','shipment','shipment_line',
    'factory_line','line_calendar','production_order','online_order','online_order_line','promotion',
    'api_key','webhook_subscription'
  ] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON merxa.%I', t || '_touch', t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON merxa.%I FOR EACH ROW EXECUTE FUNCTION merxa.touch_row()',
      t || '_touch', t);
  END LOOP;
END;
$$;
