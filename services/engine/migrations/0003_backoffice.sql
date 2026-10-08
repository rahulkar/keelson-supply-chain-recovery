-- M2 back-office (docs/contracts/70-backoffice-m2.md §1–5): connector registry extensions,
-- webhook events, versioned agent specs, LLM key versions, admin overrides of tenant limits.
-- Re-runnable (IF NOT EXISTS / guarded constraint changes).

-- The data conversions below touch tenant tables with FORCE ROW LEVEL SECURITY: without this
-- (transaction-local) switch they would silently match no rows. The runner applies every
-- migration in its own transaction, so the setting ends with it.
SELECT set_config('app.system', 'on', true);

-- ───────────────────────────── tenant ─────────────────────────────

-- Gateway limits changed in the back-office (PATCH /api/admin/gateway/tenant). Bootstrap merges
-- them over tenants.yaml so a re-run never resets an admin's change.
ALTER TABLE tenant ADD COLUMN IF NOT EXISTS admin_overrides jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Every change of the stored virtual key bumps key_version (rotate-key, bootstrap regeneration);
-- the runtime watches it to rebuild cached runners (runner-cache invalidation signal).
ALTER TABLE tenant_llm_key ADD COLUMN IF NOT EXISTS key_version integer NOT NULL DEFAULT 1;
ALTER TABLE tenant_llm_key ADD COLUMN IF NOT EXISTS rotated_at timestamptz;

-- ───────────────────────────── connector ─────────────────────────────

ALTER TABLE connector ADD COLUMN IF NOT EXISTS auth_kind text NOT NULL DEFAULT 'none';
ALTER TABLE connector ADD COLUMN IF NOT EXISTS auth_config jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE connector ADD COLUMN IF NOT EXISTS secret_enc text;
ALTER TABLE connector ADD COLUMN IF NOT EXISTS last_test jsonb;
ALTER TABLE connector ADD COLUMN IF NOT EXISTS last_test_at timestamptz;
ALTER TABLE connector ADD COLUMN IF NOT EXISTS webhook_secret_ref text;
ALTER TABLE connector ADD COLUMN IF NOT EXISTS created_by text;

-- M1 rows (Merxa from tenants.yaml) are platform connectors with Keelson's own OAuth clients.
UPDATE connector SET auth_kind = 'oauth_platform_merxa' WHERE auth_kind = 'none' AND auth ? 'reader_client';
UPDATE connector SET webhook_secret_ref = config->>'webhook_secret_env'
  WHERE webhook_secret_ref IS NULL AND config ? 'webhook_secret_env';
-- M1 statuses (pending_snapshot / needs_review / active) → the M2 lifecycle; tool review state
-- now lives on connector_tool.
UPDATE connector SET status = 'active' WHERE status NOT IN ('draft', 'tested', 'active', 'disabled');
ALTER TABLE connector ALTER COLUMN status SET DEFAULT 'draft';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_auth_kind_chk') THEN
    ALTER TABLE connector ADD CONSTRAINT connector_auth_kind_chk
      CHECK (auth_kind IN ('none', 'static_header', 'oauth_client_credentials', 'oauth_platform_merxa'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_status_chk') THEN
    ALTER TABLE connector ADD CONSTRAINT connector_status_chk
      CHECK (status IN ('draft', 'tested', 'active', 'disabled'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_webhook_ref_chk') THEN
    ALTER TABLE connector ADD CONSTRAINT connector_webhook_ref_chk
      CHECK (webhook_secret_ref IS NULL OR webhook_secret_ref ~ '^[A-Z][A-Z0-9_]{0,62}$');
  END IF;
END $$;

-- ───────────────────────────── connector_tool ─────────────────────────────

ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS access text;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS content_trust text;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS egress text;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS idempotency text;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS suggested jsonb;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS drift boolean NOT NULL DEFAULT false;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS reviewed_by text;
ALTER TABLE connector_tool ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

UPDATE connector_tool SET
  access        = COALESCE(access, classification->>'access', 'write'),
  content_trust = COALESCE(content_trust, classification->>'content_trust', 'untrusted_text'),
  egress        = COALESCE(egress, classification->>'egress', 'external'),
  idempotency   = COALESCE(idempotency, classification->>'idempotency', 'none'),
  suggested     = COALESCE(suggested, classification)
WHERE access IS NULL OR content_trust IS NULL OR egress IS NULL OR idempotency IS NULL OR suggested IS NULL;

-- M1 'approval_gate' (write tools of first-party connectors) = approved classification; writes are
-- never served to LLM agents anyway (only approved + read + trusted_data).
DO $$
BEGIN
  ALTER TABLE connector_tool DROP CONSTRAINT IF EXISTS connector_tool_status_check;
  UPDATE connector_tool SET status = 'approved' WHERE status = 'approval_gate';
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_tool_status_chk') THEN
    ALTER TABLE connector_tool ADD CONSTRAINT connector_tool_status_chk
      CHECK (status IN ('needs_review', 'approved', 'disabled', 'removed'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connector_tool_class_chk') THEN
    ALTER TABLE connector_tool ADD CONSTRAINT connector_tool_class_chk CHECK (
      access IN ('read', 'write', 'destructive')
      AND content_trust IN ('trusted_data', 'untrusted_text')
      AND egress IN ('internal', 'external')
      AND idempotency IN ('enforced', 'best_effort', 'none'));
  END IF;
END $$;
ALTER TABLE connector_tool ALTER COLUMN access SET DEFAULT 'write';
ALTER TABLE connector_tool ALTER COLUMN content_trust SET DEFAULT 'untrusted_text';
ALTER TABLE connector_tool ALTER COLUMN egress SET DEFAULT 'external';
ALTER TABLE connector_tool ALTER COLUMN idempotency SET DEFAULT 'none';
ALTER TABLE connector_tool ALTER COLUMN access SET NOT NULL;
ALTER TABLE connector_tool ALTER COLUMN content_trust SET NOT NULL;
ALTER TABLE connector_tool ALTER COLUMN egress SET NOT NULL;
ALTER TABLE connector_tool ALTER COLUMN idempotency SET NOT NULL;

-- ───────────────────────────── webhook events ─────────────────────────────

-- Standard Webhooks receiver (POST /api/webhooks/{tenant}/{connector}); dedupe on webhook-id.
CREATE TABLE IF NOT EXISTS connector_event (
  id            bigserial PRIMARY KEY,
  tenant        text NOT NULL REFERENCES tenant(slug),
  connector     text NOT NULL,
  event_id      text NOT NULL,
  type          text NOT NULL,
  payload       jsonb NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  UNIQUE (tenant, connector, event_id),
  FOREIGN KEY (tenant, connector) REFERENCES connector(tenant, slug) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS connector_event_recent_idx ON connector_event (tenant, connector, received_at DESC, id DESC);

-- ───────────────────────────── agent specs ─────────────────────────────

ALTER TABLE agent_spec ADD COLUMN IF NOT EXISTS seq integer;
ALTER TABLE agent_spec ADD COLUMN IF NOT EXISTS summary text;
ALTER TABLE agent_spec ADD COLUMN IF NOT EXISTS base_version text;
UPDATE agent_spec a SET seq = n.rn
FROM (SELECT tenant, version, row_number() OVER (PARTITION BY tenant ORDER BY created_at, version) AS rn
      FROM agent_spec) n
WHERE a.seq IS NULL AND a.tenant = n.tenant AND a.version = n.version;
CREATE UNIQUE INDEX IF NOT EXISTS agent_spec_tenant_seq_uq ON agent_spec (tenant, seq);

CREATE INDEX IF NOT EXISTS admin_audit_tenant_id_idx ON admin_audit (tenant, id DESC);

-- ───────────────────────────── RLS (new tenant tables) ─────────────────────────────

ALTER TABLE connector_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON connector_event;
CREATE POLICY tenant_isolation ON connector_event AS PERMISSIVE FOR ALL
  USING (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on')
  WITH CHECK (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on');
