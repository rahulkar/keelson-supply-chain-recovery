-- M8 more feeds (docs/contracts/124-m8-signal-feeds.md §1): organisation-name matching for global
-- signals (OFAC SDN) without tenant data in global rows, and snapshot state for list-type sources.
-- Re-runnable (IF NOT EXISTS / DROP POLICY IF EXISTS), runs as role `signals`. 0004 belongs to the
-- M8 clustering workstream (120 §2).

-- Public organisation-name keys a global signal names (signal_hub.names.match_key). The column sits
-- on `signal`, so it inherits its mixed global/own RLS policies; only OFAC fills it.
ALTER TABLE signal ADD COLUMN IF NOT EXISTS match_keys text[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS signal_match_keys
  ON signal USING gin (match_keys) WHERE cardinality(match_keys) > 0;

-- The same key of each tenant node name, computed by Pipeline.put_network. network_node is
-- tenant-only (RLS), so the name match runs inside the tenant's context. Rows pushed before this
-- migration keep NULL until the engine's next network push (it re-pushes after every scn sync).
ALTER TABLE network_node ADD COLUMN IF NOT EXISTS name_key text;
CREATE INDEX IF NOT EXISTS network_node_name_key
  ON network_node (tenant, name_key) WHERE name_key IS NOT NULL;

-- Last seen snapshot of a list-type source (OFAC SDN: one row per entity, hash of its name,
-- programs, aliases and countries) for the delta of the next publication. Global metadata without
-- tenant data, readable and writable in the system context only (like hub_secret).
CREATE TABLE IF NOT EXISTS source_snapshot (
  connector_id  text NOT NULL CHECK (connector_id ~ '^[a-z][a-z0-9_.-]{0,63}$'),
  item_key      text NOT NULL CHECK (char_length(item_key) BETWEEN 1 AND 200),
  item_hash     text NOT NULL,
  first_seen_at timestamptz NOT NULL,
  updated_at    timestamptz NOT NULL,
  PRIMARY KEY (connector_id, item_key)
);
ALTER TABLE source_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_snapshot FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS source_snapshot_system ON source_snapshot;
CREATE POLICY source_snapshot_system ON source_snapshot
  USING (sh_tenant() IS NULL) WITH CHECK (sh_tenant() IS NULL);
