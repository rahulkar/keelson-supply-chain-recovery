-- M8 mapping compiler + proposed mappings, engine side (docs/contracts/121-m8-mapping-compiler.md).
-- Re-runnable.
--
-- * scn_raw_set / scn_raw_page: the raw pages a tier-A crawl fetched (core/scn_staging.py), keyed by
--   the crawl attempt (scn_snapshot id) and the compiled source key; kept for the current snapshot
--   and the latest failed attempt only, so an admin previews a mapping edit against the full data
--   without calling the vendor. Deleted with their snapshot (GC, failed-history cap) or connector.
-- * connector_mapping_proposal: rule-based / LLM-proposed read and action mapping drafts
--   (core/mapping_proposals.py); never active, the last 20 per connector are kept.
-- * connector_mapping.origin / proposal_id: provenance of every mapping version.
-- * admin_change_request kind 'connector_mapping': saving an action mapping that came from a
--   proposal needs a second admin.

-- ───────────────────────────── raw staging ─────────────────────────────

CREATE TABLE IF NOT EXISTS scn_raw_set (
  tenant           text NOT NULL REFERENCES tenant(slug),
  snapshot_id      uuid NOT NULL,
  connector        text NOT NULL,
  status           text NOT NULL DEFAULT 'staging',
  mapping_version  integer,
  mapping_hash     text,
  sources          jsonb NOT NULL DEFAULT '[]'::jsonb,
  pages            integer NOT NULL DEFAULT 0,
  items            bigint NOT NULL DEFAULT 0,
  bytes            bigint NOT NULL DEFAULT 0,
  fetched_from     timestamptz,
  fetched_to       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, snapshot_id),
  CONSTRAINT scn_raw_set_status_chk CHECK (status IN ('staging', 'complete', 'failed')),
  FOREIGN KEY (tenant, snapshot_id) REFERENCES scn_snapshot (tenant, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant, connector) REFERENCES connector (tenant, slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS scn_raw_set_recent_idx ON scn_raw_set (tenant, connector, created_at DESC);

CREATE TABLE IF NOT EXISTS scn_raw_page (
  tenant       text NOT NULL,
  snapshot_id  uuid NOT NULL,
  source_key   text NOT NULL,
  page_no      integer NOT NULL,
  item_count   integer NOT NULL,
  bytes        integer NOT NULL,
  items        jsonb NOT NULL,
  fetched_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, snapshot_id, source_key, page_no),
  CONSTRAINT scn_raw_page_key_chk CHECK (source_key ~ '^[0-9a-f]{16}$'),
  CONSTRAINT scn_raw_page_no_chk CHECK (page_no >= 0),
  FOREIGN KEY (tenant, snapshot_id) REFERENCES scn_raw_set (tenant, snapshot_id) ON DELETE CASCADE
);

-- ───────────────────────────── proposals ─────────────────────────────

CREATE TABLE IF NOT EXISTS connector_mapping_proposal (
  id                       uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant                   text NOT NULL REFERENCES tenant(slug),
  connector                text NOT NULL,
  kinds                    text[] NOT NULL,
  proposer                 text NOT NULL,
  model_alias              text,
  status                   text NOT NULL DEFAULT 'ready',
  inputs_hash              text NOT NULL,
  proposal                 jsonb NOT NULL,
  base_read_version        integer,
  base_action_version      integer,
  accepted_read_version    integer,
  accepted_action_version  integer,
  created_by               text,
  created_at               timestamptz NOT NULL DEFAULT now(),
  decided_by               text,
  decided_at               timestamptz,
  CONSTRAINT connector_mapping_proposal_proposer_chk CHECK (proposer IN ('heuristic', 'llm+heuristic')),
  CONSTRAINT connector_mapping_proposal_status_chk CHECK (status IN ('ready', 'accepted', 'discarded')),
  FOREIGN KEY (tenant, connector) REFERENCES connector (tenant, slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS connector_mapping_proposal_recent_idx
  ON connector_mapping_proposal (tenant, connector, created_at DESC);

-- ───────────────────────────── mapping provenance ─────────────────────────────

ALTER TABLE connector_mapping ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'manual';
ALTER TABLE connector_mapping ADD COLUMN IF NOT EXISTS proposal_id uuid;
ALTER TABLE connector_mapping DROP CONSTRAINT IF EXISTS connector_mapping_origin_chk;
ALTER TABLE connector_mapping ADD CONSTRAINT connector_mapping_origin_chk
  CHECK (origin IN ('manual', 'pack', 'proposal', 'change_request'));

-- ───────────────────────────── four-eyes kind ─────────────────────────────

ALTER TABLE admin_change_request DROP CONSTRAINT IF EXISTS admin_change_request_kind_chk;
ALTER TABLE admin_change_request ADD CONSTRAINT admin_change_request_kind_chk
  CHECK (kind IN ('agent_spec', 'policy_set', 'connector_tool', 'tool_binding', 'connector_activate',
                  'connector_trust', 'embedding_profile', 'connector_delete', 'kb_delete', 'user_roles',
                  'connector_mapping'));

-- ───────────────────────────── row-level security ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['scn_raw_set', 'scn_raw_page', 'connector_mapping_proposal']
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
