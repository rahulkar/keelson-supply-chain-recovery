-- M7 Signals admin (docs/contracts/110 §5) + the dedicated LiteLLM key for news extraction.
-- Re-runnable (IF NOT EXISTS / DROP POLICY IF EXISTS), runs as role `signals`.

-- ───────────── per-tenant source subscriptions (tenant-only, RLS) ─────────────
-- No row = subscribed with the default cadence. A disabled source no longer raises or keeps
-- alerts for the tenant (signal_hub.admin / Pipeline.refresh_event); cadence_s is the tenant's
-- wish for the shared poll (bounded per adapter by signal_hub.admin.CADENCE_LIMITS).
CREATE TABLE IF NOT EXISTS source_subscription (
  tenant       text NOT NULL,
  connector_id text NOT NULL CHECK (connector_id ~ '^[a-z][a-z0-9_.-]{0,63}$'),
  enabled      boolean NOT NULL DEFAULT true,
  cadence_s    integer CHECK (cadence_s IS NULL OR cadence_s BETWEEN 60 AND 86400),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  updated_by   text,
  PRIMARY KEY (tenant, connector_id)
);

-- Global poll schedule derived from every registered tenant's subscription (no tenant data:
-- counts and the fastest wanted cadence only). Written by signal_hub.admin.recompute_schedule.
CREATE TABLE IF NOT EXISTS source_schedule (
  connector_id     text PRIMARY KEY,
  cadence_s        integer,
  enabled_tenants  integer NOT NULL DEFAULT 0,
  disabled_tenants integer NOT NULL DEFAULT 0,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- ───────────── watch regions (tenant-only, RLS) ─────────────
-- polygon: a GeoJSON Polygon/MultiPolygon (geog); tenant nodes inside it are exposed when an event
--          footprint touches it. radius: radius_km around one named tenant node (or lane).
CREATE TABLE IF NOT EXISTS watch_region (
  tenant     text NOT NULL,
  id         text NOT NULL CHECK (id ~ '^[a-z][a-z0-9_-]{0,63}$'),
  name       text NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('polygon','radius')),
  geometry   jsonb,
  geog       geography,
  node_type  text CHECK (node_type IS NULL OR node_type IN ('supplier','factory','port','dc','store','lane')),
  node_code  text,
  radius_km  integer CHECK (radius_km IS NULL OR radius_km BETWEEN 1 AND 2000),
  node_types text[] NOT NULL DEFAULT '{}',
  enabled    boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text,
  PRIMARY KEY (tenant, id),
  CHECK ((kind = 'polygon' AND geog IS NOT NULL AND node_type IS NULL AND node_code IS NULL AND radius_km IS NULL)
      OR (kind = 'radius' AND geog IS NULL AND node_type IS NOT NULL AND node_code IS NOT NULL AND radius_km IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS watch_region_geog ON watch_region USING gist (geog);

-- Audit columns on the existing rule set.
ALTER TABLE alert_rule_set ADD COLUMN IF NOT EXISTS updated_by text;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['source_subscription','watch_region']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant', t);
    EXECUTE format('CREATE POLICY %I ON %I USING (tenant = sh_tenant()) WITH CHECK (tenant = sh_tenant())',
                   t || '_tenant', t);
  END LOOP;
END $$;

-- ───────────── service secrets (system context only) ─────────────
-- The dedicated LiteLLM virtual key for news extraction, AES-256-GCM encrypted with a key derived
-- from SIGNALS_DATA_KEY (else SIGNAL_HUB_SHARED_SECRET) — signal_hub.secretbox. Readable only in
-- the system context (app.tenant_id = ''), never in a tenant transaction.
CREATE TABLE IF NOT EXISTS hub_secret (
  name       text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{0,62}$'),
  ciphertext text NOT NULL,
  kek_id     text NOT NULL,
  meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE hub_secret ENABLE ROW LEVEL SECURITY;
ALTER TABLE hub_secret FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hub_secret_system ON hub_secret;
CREATE POLICY hub_secret_system ON hub_secret USING (sh_tenant() IS NULL) WITH CHECK (sh_tenant() IS NULL);
