-- M7 back-office + generic tier-A proof, engine side (docs/contracts/110-m7-backoffice-generic.md
-- §1, §2, §4). Re-runnable.
--
-- * tenant.status / suspended_at / suspended_by: a suspended tenant blocks logins (the BFF drops it
--   from new sessions) and every tenant-scoped API call (403 tenant_suspended); platform table, no RLS.
-- * admin_change_request: four-eyes change requests of guarded admin changes (core/change_requests.py).
-- * connector_mapping: tier-A read / action mappings, versioned per (connector, kind).
-- * scn_snapshot.consistency / as_of_from / as_of_to / crawl: crawled snapshots (core/scn_crawl.py)
--   carry consistency 'crawled' + the as-of range of the pages they were read from.
-- * commit_ledger: per-op commit columns (op, connector, tool, idempotency, attempts, request_id,
--   readback, error); the state check gains the states the runtime already writes for change-set
--   commits ('conflict', 'rolled_back').
-- * connector_tool.binding: agent tool binding (auto | approval_gate | deny); 'auto' on a write tool
--   is a guarded change.

-- ───────────────────────────── tenants ─────────────────────────────

ALTER TABLE tenant ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS suspended_at timestamptz;
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS suspended_by text;
ALTER TABLE tenant DROP CONSTRAINT IF EXISTS tenant_status_chk;
ALTER TABLE tenant ADD CONSTRAINT tenant_status_chk CHECK (status IN ('active', 'suspended'));

-- ───────────────────────────── four-eyes change requests ─────────────────────────────

CREATE TABLE IF NOT EXISTS admin_change_request (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant           text NOT NULL REFERENCES tenant(slug),
  kind             text NOT NULL,
  target           text NOT NULL,
  payload          jsonb NOT NULL,
  base_version     text,
  diff             jsonb NOT NULL,
  reasons          jsonb NOT NULL,
  requested_by     jsonb NOT NULL,
  requested_sub    text NOT NULL,
  requester_auth   jsonb NOT NULL DEFAULT '{}'::jsonb,
  requested_at     timestamptz NOT NULL,
  status           text NOT NULL DEFAULT 'pending',
  approver         jsonb,
  decided_at       timestamptz,
  decision_reason  text,
  expires_at       timestamptz NOT NULL,
  result           jsonb,
  CONSTRAINT admin_change_request_status_chk
    CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
  CONSTRAINT admin_change_request_kind_chk
    CHECK (kind IN ('agent_spec', 'policy_set', 'connector_tool', 'tool_binding', 'connector_activate',
                    'connector_trust', 'embedding_profile', 'connector_delete', 'kb_delete', 'user_roles')),
  CONSTRAINT admin_change_request_decided_chk
    CHECK ((status IN ('pending', 'expired')) = (decided_at IS NULL))
);
CREATE INDEX IF NOT EXISTS admin_change_request_tenant_idx
  ON admin_change_request (tenant, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS admin_change_request_pending_idx
  ON admin_change_request (expires_at) WHERE status = 'pending';

-- ───────────────────────────── connector mappings ─────────────────────────────

CREATE TABLE IF NOT EXISTS connector_mapping (
  tenant      text NOT NULL REFERENCES tenant(slug),
  connector   text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('read', 'action')),
  version     integer NOT NULL CHECK (version >= 1),
  mapping     jsonb NOT NULL,
  summary     text,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector, kind, version),
  FOREIGN KEY (tenant, connector) REFERENCES connector (tenant, slug) ON DELETE CASCADE
);

-- ───────────────────────────── crawled snapshots ─────────────────────────────

ALTER TABLE scn_snapshot ADD COLUMN IF NOT EXISTS consistency text NOT NULL DEFAULT 'consistent';
ALTER TABLE scn_snapshot ADD COLUMN IF NOT EXISTS as_of_from timestamptz;
ALTER TABLE scn_snapshot ADD COLUMN IF NOT EXISTS as_of_to timestamptz;
ALTER TABLE scn_snapshot ADD COLUMN IF NOT EXISTS crawl jsonb;
ALTER TABLE scn_snapshot DROP CONSTRAINT IF EXISTS scn_snapshot_consistency_chk;
ALTER TABLE scn_snapshot ADD CONSTRAINT scn_snapshot_consistency_chk
  CHECK (consistency IN ('consistent', 'crawled'));

-- ───────────────────────────── per-op commit ledger ─────────────────────────────

ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS op text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS connector text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS tool text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS idempotency text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 1;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS request_id text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS readback jsonb;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS error text;
ALTER TABLE commit_ledger ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE commit_ledger DROP CONSTRAINT IF EXISTS commit_ledger_state_check;
ALTER TABLE commit_ledger DROP CONSTRAINT IF EXISTS commit_ledger_state_chk;
ALTER TABLE commit_ledger ADD CONSTRAINT commit_ledger_state_chk
  CHECK (state IN ('attempted', 'succeeded', 'in_doubt', 'failed', 'verified', 'conflict', 'rolled_back'));
ALTER TABLE commit_ledger DROP CONSTRAINT IF EXISTS commit_ledger_idempotency_chk;
ALTER TABLE commit_ledger ADD CONSTRAINT commit_ledger_idempotency_chk
  CHECK (idempotency IS NULL OR idempotency IN ('enforced', 'best_effort', 'none'));

-- ───────────────────────────── tool bindings ─────────────────────────────

ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS binding text NOT NULL DEFAULT 'approval_gate';
ALTER TABLE connector_tool DROP CONSTRAINT IF EXISTS connector_tool_binding_chk;
ALTER TABLE connector_tool ADD CONSTRAINT connector_tool_binding_chk
  CHECK (binding IN ('auto', 'approval_gate', 'deny'));

-- ───────────────────────────── row-level security ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['admin_change_request', 'connector_mapping']
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
