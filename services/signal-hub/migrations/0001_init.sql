-- Signal Hub schema (docs/contracts/100-m6-signals.md §1-§5, §7).
-- Runs as role `signals`, owner of database `signals`; PostGIS is created by the bootstrap
-- (pg-core superuser). Re-runnable (IF NOT EXISTS / CREATE OR REPLACE / DROP POLICY IF EXISTS).
--
-- Tenancy (RLS FORCE, so the owning role is subject to it too). The session variable
-- app.tenant_id is set per transaction by signal_hub.db.tx:
--   * '' (system context): sees and writes global rows only (tenant IS NULL);
--   * '<tenant>': sees global rows + its own rows, writes only its own rows.
-- Mixed tables (signal, event, news_item): read = global or own; write = exactly own scope.
-- Tenant-only tables (network, exposure, alert, rules, demo runs): own rows only.

CREATE OR REPLACE FUNCTION sh_tenant() RETURNS text
  LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '') $$;

-- Tenants that pushed a network (slug registry, no tenant data; not RLS-scoped).
CREATE TABLE IF NOT EXISTS tenant_registry (
  tenant             text PRIMARY KEY CHECK (tenant ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  network_version    integer NOT NULL DEFAULT 0,
  network_updated_at timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now()
);

-- Per-source polling state: ETag / Last-Modified, adapter cursor, schedule (global).
CREATE TABLE IF NOT EXISTS source_cursor (
  connector_id         text PRIMARY KEY,
  etag                 text,
  last_modified        text,
  cursor               jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_polled_at       timestamptz,
  last_status          text,
  last_error           text,
  consecutive_failures integer NOT NULL DEFAULT 0,
  next_poll_at         timestamptz,
  items_total          bigint NOT NULL DEFAULT 0
);

-- ───────────────────────────── signals ─────────────────────────────
CREATE TABLE IF NOT EXISTS signal (
  id            uuid PRIMARY KEY,
  tenant        text,
  connector_id  text NOT NULL,
  native_key    text NOT NULL,
  version       integer NOT NULL CHECK (version >= 1),
  supersedes    uuid,
  is_current    boolean NOT NULL DEFAULT true,
  content_hash  text NOT NULL,
  origin        text NOT NULL CHECK (origin IN ('feed','news','connected_system','chat','demo')),
  kind          text NOT NULL,
  subtype       text NOT NULL,
  title         text NOT NULL,
  provenance    text NOT NULL CHECK (provenance IN ('fact','inference','simulated')),
  authoritative boolean NOT NULL DEFAULT false,
  severity_bp   integer NOT NULL CHECK (severity_bp BETWEEN 0 AND 10000),
  confidence_bp integer NOT NULL CHECK (confidence_bp BETWEEN 0 AND 10000),
  authority_bp  integer NOT NULL CHECK (authority_bp BETWEEN 0 AND 10000),
  observed_at   timestamptz NOT NULL,
  starts_at     timestamptz NOT NULL,
  ends_at       timestamptz,
  expires_at    timestamptz,
  location_refs text[] NOT NULL DEFAULT '{}',
  geog          geography,
  event_id      uuid,
  pack_id       text,
  doc           jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS signal_native_version
  ON signal ((coalesce(tenant, '')), connector_id, native_key, version);
CREATE INDEX IF NOT EXISTS signal_current ON signal (connector_id, native_key) WHERE is_current;
CREATE INDEX IF NOT EXISTS signal_geog ON signal USING gist (geog);
CREATE INDEX IF NOT EXISTS signal_observed ON signal (observed_at DESC);
CREATE INDEX IF NOT EXISTS signal_event ON signal (event_id);
CREATE INDEX IF NOT EXISTS signal_refs ON signal USING gin (location_refs);
CREATE INDEX IF NOT EXISTS signal_tenant_pack ON signal (tenant, pack_id) WHERE pack_id IS NOT NULL;

-- ───────────────────────────── events (clusters) ─────────────────────────────
CREATE TABLE IF NOT EXISTS event (
  id               uuid PRIMARY KEY,
  tenant           text,
  kind             text NOT NULL,
  subtype          text NOT NULL,
  title            text NOT NULL,
  summary          text,
  anchor_signal_id uuid,
  geometry         jsonb,
  geog             geography,
  window_start     timestamptz NOT NULL,
  window_end       timestamptz,
  severity_bp      integer NOT NULL CHECK (severity_bp BETWEEN 0 AND 10000),
  confidence_bp    integer NOT NULL CHECK (confidence_bp BETWEEN 0 AND 10000),
  status           text NOT NULL CHECK (status IN ('candidate','confirmed','monitoring','resolved')),
  taxonomy_key     text NOT NULL,
  radius_km        integer NOT NULL DEFAULT 50,
  simulated        boolean NOT NULL DEFAULT false,
  pack_id          text,
  expires_at       timestamptz,
  last_observed_at timestamptz,
  merged_into      uuid,
  updated_at       timestamptz NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS event_geog ON event USING gist (geog);
CREATE INDEX IF NOT EXISTS event_open ON event (status) WHERE status <> 'resolved';
CREATE INDEX IF NOT EXISTS event_tenant_pack ON event (tenant, pack_id) WHERE pack_id IS NOT NULL;

-- ───────────────────────────── news items (quarantine queue) ─────────────────────────────
-- Untrusted text waiting for / after the guard + extractor. Never shown to a tool-using LLM.
CREATE TABLE IF NOT EXISTS news_item (
  id              uuid PRIMARY KEY,
  tenant          text,
  connector_id    text NOT NULL,
  native_key      text NOT NULL,
  origin          text NOT NULL CHECK (origin IN ('news','connected_system','demo')),
  source_name     text NOT NULL,
  license         text NOT NULL,
  authority_bp    integer NOT NULL,
  url             text,
  title           text NOT NULL,
  snippet         text,
  published_at    timestamptz,
  retrieved_at    timestamptz NOT NULL,
  status          text NOT NULL CHECK (status IN ('pending_extraction','extracted','flagged','rejected','failed')),
  attempts        integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  last_error      text,
  guard           jsonb,
  extraction      jsonb,
  extracted_by    text,
  signal_id       uuid,
  context         jsonb NOT NULL DEFAULT '{}'::jsonb,
  pack_id         text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS news_item_native ON news_item ((coalesce(tenant, '')), connector_id, native_key);
CREATE INDEX IF NOT EXISTS news_item_pending ON news_item (next_attempt_at) WHERE status = 'pending_extraction';

-- ───────────────────────────── tenant network ─────────────────────────────
CREATE TABLE IF NOT EXISTS network_node (
  tenant text NOT NULL,
  type   text NOT NULL CHECK (type IN ('supplier','factory','port','dc','store')),
  code   text NOT NULL,
  name   text NOT NULL,
  geog   geography(Point, 4326) NOT NULL,
  PRIMARY KEY (tenant, type, code)
);
CREATE INDEX IF NOT EXISTS network_node_geog ON network_node USING gist (geog);

CREATE TABLE IF NOT EXISTS network_lane (
  tenant text NOT NULL,
  code   text NOT NULL,
  mode   text NOT NULL,
  path   geography(LineString, 4326) NOT NULL,
  PRIMARY KEY (tenant, code)
);
CREATE INDEX IF NOT EXISTS network_lane_path ON network_lane USING gist (path);

-- ───────────────────────────── exposure + alerts ─────────────────────────────
CREATE TABLE IF NOT EXISTS exposure (
  tenant      text NOT NULL,
  event_id    uuid NOT NULL REFERENCES event (id) ON DELETE CASCADE,
  node_type   text NOT NULL,
  node_code   text NOT NULL,
  method      text NOT NULL CHECK (method IN ('intersects','dwithin_km','lane_crosses','entity_match')),
  distance_km integer,
  weight_bp   integer NOT NULL,
  computed_at timestamptz NOT NULL,
  PRIMARY KEY (tenant, event_id, node_type, node_code)
);

CREATE TABLE IF NOT EXISTS alert (
  id                  uuid PRIMARY KEY,
  tenant              text NOT NULL,
  event_id            uuid NOT NULL REFERENCES event (id) ON DELETE CASCADE,
  status              text NOT NULL CHECK (status IN ('new','seen','snoozed','dismissed','opened','resolved')),
  severity            text NOT NULL,
  severity_bp         integer NOT NULL,
  title               text NOT NULL,
  why                 text NOT NULL,
  exposure_summary    jsonb NOT NULL,
  first_raised_at     timestamptz NOT NULL,
  updated_at          timestamptz NOT NULL,
  case_id             text,
  simulated           boolean NOT NULL,
  rule_id             text NOT NULL,
  action              text NOT NULL CHECK (action IN ('alert','alert_and_draft_case')),
  notified_severity_bp integer NOT NULL,
  notified_at         timestamptz NOT NULL,
  UNIQUE (tenant, event_id)
);
CREATE INDEX IF NOT EXISTS alert_tenant_status ON alert (tenant, status, updated_at DESC);

-- Append-only change feed (engine: GET /v1/alerts/changes?after=<seq>).
CREATE TABLE IF NOT EXISTS alert_change (
  seq      bigserial PRIMARY KEY,
  tenant   text NOT NULL,
  alert_id uuid NOT NULL,
  change   text NOT NULL CHECK (change IN ('raised','renotified','updated','resolved','status')),
  action   text,
  at       timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_change_tenant ON alert_change (tenant, seq);

CREATE TABLE IF NOT EXISTS alert_rule_set (
  tenant     text PRIMARY KEY,
  rules      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────────────────────── demo pack runs (SimClock) ─────────────────────────────
CREATE TABLE IF NOT EXISTS demo_run (
  tenant          text NOT NULL,
  pack_id         text NOT NULL,
  sim_start       timestamptz NOT NULL,
  started_wall_at timestamptz NOT NULL,
  speed           double precision NOT NULL DEFAULT 0,
  offset_s        bigint NOT NULL DEFAULT 0,
  last_step_index integer NOT NULL DEFAULT -1,
  status          text NOT NULL CHECK (status IN ('running','finished')),
  log             jsonb NOT NULL DEFAULT '[]'::jsonb,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, pack_id)
);

-- ───────────────────────────── RLS ─────────────────────────────
ALTER TABLE signal ENABLE ROW LEVEL SECURITY;
ALTER TABLE signal FORCE ROW LEVEL SECURITY;
ALTER TABLE event ENABLE ROW LEVEL SECURITY;
ALTER TABLE event FORCE ROW LEVEL SECURITY;
ALTER TABLE news_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_item FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS signal_read ON signal;
DROP POLICY IF EXISTS signal_insert ON signal;
DROP POLICY IF EXISTS signal_update ON signal;
DROP POLICY IF EXISTS signal_delete ON signal;
CREATE POLICY signal_read ON signal FOR SELECT USING (tenant IS NULL OR tenant = sh_tenant());
CREATE POLICY signal_insert ON signal FOR INSERT WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY signal_update ON signal FOR UPDATE USING (tenant IS NOT DISTINCT FROM sh_tenant())
  WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY signal_delete ON signal FOR DELETE USING (tenant IS NOT DISTINCT FROM sh_tenant());

DROP POLICY IF EXISTS event_read ON event;
DROP POLICY IF EXISTS event_insert ON event;
DROP POLICY IF EXISTS event_update ON event;
DROP POLICY IF EXISTS event_delete ON event;
CREATE POLICY event_read ON event FOR SELECT USING (tenant IS NULL OR tenant = sh_tenant());
CREATE POLICY event_insert ON event FOR INSERT WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY event_update ON event FOR UPDATE USING (tenant IS NOT DISTINCT FROM sh_tenant())
  WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY event_delete ON event FOR DELETE USING (tenant IS NOT DISTINCT FROM sh_tenant());

DROP POLICY IF EXISTS news_item_read ON news_item;
DROP POLICY IF EXISTS news_item_insert ON news_item;
DROP POLICY IF EXISTS news_item_update ON news_item;
DROP POLICY IF EXISTS news_item_delete ON news_item;
CREATE POLICY news_item_read ON news_item FOR SELECT USING (tenant IS NULL OR tenant = sh_tenant());
CREATE POLICY news_item_insert ON news_item FOR INSERT WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY news_item_update ON news_item FOR UPDATE USING (tenant IS NOT DISTINCT FROM sh_tenant())
  WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant());
CREATE POLICY news_item_delete ON news_item FOR DELETE USING (tenant IS NOT DISTINCT FROM sh_tenant());

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['network_node','network_lane','exposure','alert','alert_change','alert_rule_set','demo_run']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant', t);
    EXECUTE format('CREATE POLICY %I ON %I USING (tenant = sh_tenant()) WITH CHECK (tenant = sh_tenant())',
                   t || '_tenant', t);
  END LOOP;
END $$;
