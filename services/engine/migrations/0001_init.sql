-- Keelson engine DB (database "keelson", role "keelson"). Contract: docs/contracts/30-engine-api-events.md
-- "Engine DB" + 55-engine-internals.md. Re-runnable (IF NOT EXISTS / CREATE OR REPLACE).
--
-- Row-level security: every tenant table has ENABLE + FORCE ROW LEVEL SECURITY and the policy
--   tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on'
-- core.db.tenant_session() sets app.tenant_id (SET LOCAL); core.db.system_session() sets app.system.
-- FORCE makes the policies apply to the table owner ("keelson") as well. Tables without a tenant
-- column (tenant registry, BFF/OAuth/console session state) are platform tables without RLS.

-- ───────────────────────────── platform tables (no RLS) ─────────────────────────────

CREATE TABLE IF NOT EXISTS tenant (
  slug        text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name        text NOT NULL,
  config      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- BFF session (cookie keelson_session). id = HMAC-SHA256(SESSION_SECRET, cookie value) hex:
-- a DB leak does not reveal usable cookies. Tokens are AES-GCM encrypted (AAD "bff|<id>").
CREATE TABLE IF NOT EXISTS bff_session (
  id               text PRIMARY KEY,
  sub              text NOT NULL,
  username         text NOT NULL,
  name             text NOT NULL DEFAULT '',
  email            text,
  roles            jsonb NOT NULL DEFAULT '[]'::jsonb,
  tenants          jsonb NOT NULL DEFAULT '[]'::jsonb,
  active_tenant    text,
  merxa_username   text,
  demo             boolean NOT NULL DEFAULT false,
  csrf_token       text NOT NULL,
  tokens_enc       text,
  auth_time        timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  idle_expires_at  timestamptz NOT NULL,
  expires_at       timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS bff_session_sub_idx ON bff_session (sub);
CREATE INDEX IF NOT EXISTS bff_session_expiry_idx ON bff_session (expires_at);

-- Pending OAuth authorization requests (login to realm keelson, Connect-Merxa). Keyed by
-- sha256(state); single use; PKCE verifier encrypted (AAD "flow|<state_hash>").
CREATE TABLE IF NOT EXISTS oauth_flow (
  state_hash         text PRIMARY KEY,
  kind               text NOT NULL CHECK (kind IN ('login', 'connect')),
  session_id         text,
  keelson_sub        text,
  tenant             text,
  connector          text,
  nonce              text NOT NULL,
  code_verifier_enc  text NOT NULL,
  return_to          text NOT NULL DEFAULT '/',
  created_at         timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL
);

-- Console handoff (gateway./traces.localhost): 60 s single-use tickets and 8 h sessions,
-- both keyed by sha256 of the opaque value handed to the browser.
CREATE TABLE IF NOT EXISTS console_ticket (
  id_hash     text PRIMARY KEY,
  target      text NOT NULL CHECK (target IN ('gateway', 'traces')),
  sub         text NOT NULL,
  username    text NOT NULL,
  roles       jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);
CREATE TABLE IF NOT EXISTS console_session (
  id_hash     text PRIMARY KEY,
  target      text NOT NULL CHECK (target IN ('gateway', 'traces')),
  sub         text NOT NULL,
  username    text NOT NULL,
  roles       jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL
);

-- ───────────────────────────── tenant tables (RLS FORCE) ─────────────────────────────

CREATE TABLE IF NOT EXISTS tenant_llm_key (
  tenant             text PRIMARY KEY REFERENCES tenant(slug),
  alias_prefix       text NOT NULL DEFAULT '',
  key_enc            text NOT NULL,
  key_id             text NOT NULL,
  litellm_key_alias  text,
  team_id            text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit of admin/security actions. tenant NULL = platform-level (visible to
-- system sessions only).
CREATE TABLE IF NOT EXISTS admin_audit (
  id              bigserial PRIMARY KEY,
  tenant          text,
  at              timestamptz NOT NULL DEFAULT now(),
  actor_sub       text,
  actor_username  text,
  action          text NOT NULL,
  target          text,
  before          jsonb,
  after           jsonb,
  trace_id        text
);
CREATE INDEX IF NOT EXISTS admin_audit_tenant_at_idx ON admin_audit (tenant, at DESC);

CREATE TABLE IF NOT EXISTS "case" (
  id                    text PRIMARY KEY DEFAULT uuidv7()::text,
  tenant                text NOT NULL REFERENCES tenant(slug),
  title                 text NOT NULL,
  mode                  text NOT NULL DEFAULT 'actual' CHECK (mode IN ('actual', 'what_if')),
  status                text NOT NULL DEFAULT 'open',
  created_by            text NOT NULL,
  created_by_name       text NOT NULL DEFAULT '',
  last_message_preview  text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS case_tenant_updated_idx ON "case" (tenant, updated_at DESC);

CREATE TABLE IF NOT EXISTS case_message (
  id          text PRIMARY KEY DEFAULT uuidv7()::text,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  role        text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  text        text NOT NULL,
  run_id      text,
  author_sub  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS case_message_case_idx ON case_message (case_id, created_at, id);

-- Runs queue. kind 'crash_retry' = re-queued by the reaper (origin_kind keeps the original).
CREATE TABLE IF NOT EXISTS run (
  id                  text PRIMARY KEY,
  tenant              text NOT NULL REFERENCES tenant(slug),
  case_id             text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  kind                text NOT NULL CHECK (kind IN ('chat', 'action', 'signal', 'resume', 'crash_retry')),
  origin_kind         text NOT NULL CHECK (origin_kind IN ('chat', 'action', 'signal', 'resume')),
  status              text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'paused', 'cancelled')),
  payload             jsonb NOT NULL,
  actor               jsonb NOT NULL,
  idempotency_key     text,
  request_hash        text,
  attempt             integer NOT NULL DEFAULT 0,
  invocation_id       text,
  session_id          text NOT NULL,
  agent_spec_version  text NOT NULL DEFAULT 'default',
  created_by          text NOT NULL,
  lease_owner         text,
  lease_expires_at    timestamptz,
  heartbeat_at        timestamptz,
  cancel_requested    boolean NOT NULL DEFAULT false,
  error               text,
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  started_at          timestamptz,
  finished_at         timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS run_idempotency_uq ON run (tenant, case_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
-- Per-case lease: at most one running run per case (the claim relies on this).
CREATE UNIQUE INDEX IF NOT EXISTS run_one_running_per_case_uq ON run (case_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS run_queued_idx ON run (created_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS run_lease_idx ON run (lease_expires_at) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS run_case_idx ON run (case_id, created_at);

CREATE TABLE IF NOT EXISTS run_event (
  id          bigserial PRIMARY KEY,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id      text,
  type        text NOT NULL,
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_event_case_idx ON run_event (case_id, id);

CREATE TABLE IF NOT EXISTS interrupt (
  id                 text PRIMARY KEY,
  tenant             text NOT NULL REFERENCES tenant(slug),
  case_id            text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id             text NOT NULL,
  kind               text NOT NULL CHECK (kind IN ('approach', 'approval')),
  status             text NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING', 'RESOLVED', 'SUPERSEDED', 'EXPIRED')),
  quorum_spec        jsonb NOT NULL DEFAULT '{}'::jsonb,
  plan_version       integer,
  change_set_hash    text,
  payload            jsonb NOT NULL DEFAULT '{}'::jsonb,
  message            text NOT NULL DEFAULT '',
  response_schema    jsonb NOT NULL DEFAULT '{}'::jsonb,
  invocation_id      text NOT NULL,
  session_id         text NOT NULL,
  initiator_sub      text,
  superseded_reason  text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  resolved_at        timestamptz
);
CREATE INDEX IF NOT EXISTS interrupt_case_status_idx ON interrupt (case_id, status);

CREATE TABLE IF NOT EXISTS approval_decision (
  id                 bigserial PRIMARY KEY,
  tenant             text NOT NULL REFERENCES tenant(slug),
  interrupt_id       text NOT NULL REFERENCES interrupt(id) ON DELETE CASCADE,
  approver_sub       text NOT NULL,
  approver_username  text NOT NULL,
  approver_name      text NOT NULL DEFAULT '',
  role               text,
  decision           text NOT NULL,
  change_set_hash    text,
  plan_version       integer,
  edits              text,
  auth_time          timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (interrupt_id, approver_sub)
);

CREATE TABLE IF NOT EXISTS resume_request (
  id            bigserial PRIMARY KEY,
  tenant        text NOT NULL REFERENCES tenant(slug),
  interrupt_id  text NOT NULL UNIQUE REFERENCES interrupt(id) ON DELETE CASCADE,
  run_id        text NOT NULL,
  decision      text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scenario (
  id           bigserial PRIMARY KEY,
  tenant       text NOT NULL REFERENCES tenant(slug),
  case_id      text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  version      integer NOT NULL,
  scenario     jsonb NOT NULL,
  fingerprint  text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, version)
);

CREATE TABLE IF NOT EXISTS plan (
  id                     bigserial PRIMARY KEY,
  tenant                 text NOT NULL REFERENCES tenant(slug),
  case_id                text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  version                integer NOT NULL,
  options                jsonb NOT NULL,
  recommended_option_id  text,
  kpis                   jsonb NOT NULL DEFAULT '{}'::jsonb,
  status                 text NOT NULL DEFAULT 'proposed',
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (case_id, version)
);

CREATE TABLE IF NOT EXISTS investigation_task (
  id          text PRIMARY KEY,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id      text,
  specialist  text NOT NULL,
  status      text NOT NULL DEFAULT 'pending',
  input       jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS finding (
  id          bigserial PRIMARY KEY,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  task_id     text NOT NULL,
  run_id      text,
  findings    jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS finding_case_idx ON finding (case_id, task_id);

CREATE TABLE IF NOT EXISTS evidence (
  id          text PRIMARY KEY CHECK (id ~ '^ev_[0-9A-Za-z_-]{1,64}$'),
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id      text,
  connector   text,
  tool        text NOT NULL,
  args        jsonb NOT NULL DEFAULT '{}'::jsonb,
  result      jsonb NOT NULL,
  prov        text NOT NULL DEFAULT 'fact',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_case_idx ON evidence (case_id, created_at);

CREATE TABLE IF NOT EXISTS calc (
  id          text PRIMARY KEY,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id      text,
  op          text NOT NULL,
  inputs      jsonb NOT NULL DEFAULT '{}'::jsonb,
  result      jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS block (
  id          text PRIMARY KEY CHECK (id ~ '^blk_[0-9A-Za-z_-]{1,64}$'),
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  run_id      text,
  kind        text NOT NULL,
  v           integer NOT NULL DEFAULT 1,
  envelope    jsonb NOT NULL,
  data        jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS block_case_idx ON block (case_id, created_at);

CREATE TABLE IF NOT EXISTS change_set (
  id                  text PRIMARY KEY DEFAULT uuidv7()::text,
  tenant              text NOT NULL REFERENCES tenant(slug),
  case_id             text NOT NULL REFERENCES "case"(id) ON DELETE CASCADE,
  plan_id             bigint REFERENCES plan(id),
  plan_version        integer,
  option_id           text,
  connector           text NOT NULL,
  revision            integer NOT NULL DEFAULT 1,
  items               jsonb NOT NULL,
  hash                text NOT NULL,
  status              text NOT NULL DEFAULT 'draft',
  ext_change_set_id   text,
  ext_code            text,
  preview             jsonb,
  approval_id         text,
  receipt_jti         text,
  result              jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_set_case_idx ON change_set (case_id, created_at);

CREATE TABLE IF NOT EXISTS commit_ledger (
  id             bigserial PRIMARY KEY,
  tenant         text NOT NULL REFERENCES tenant(slug),
  change_set_id  text NOT NULL REFERENCES change_set(id) ON DELETE CASCADE,
  line_seq       integer NOT NULL,
  state          text NOT NULL CHECK (state IN ('attempted', 'succeeded', 'in_doubt', 'failed', 'verified')),
  ext_ref        text,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (change_set_id, line_seq)
);

CREATE TABLE IF NOT EXISTS connector (
  tenant       text NOT NULL REFERENCES tenant(slug),
  slug         text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  name         text NOT NULL,
  mcp_url      text NOT NULL,
  issuer       text NOT NULL,
  trust_level  text NOT NULL DEFAULT 'third_party',
  auth         jsonb NOT NULL DEFAULT '{}'::jsonb,
  lanes        jsonb NOT NULL DEFAULT '[]'::jsonb,
  config       jsonb NOT NULL DEFAULT '{}'::jsonb,
  status       text NOT NULL DEFAULT 'pending_snapshot',
  tiers        jsonb NOT NULL DEFAULT '[]'::jsonb,
  snapshot_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, slug)
);

CREATE TABLE IF NOT EXISTS connector_tool (
  tenant           text NOT NULL REFERENCES tenant(slug),
  connector        text NOT NULL,
  name             text NOT NULL,
  title            text,
  description      text NOT NULL DEFAULT '',
  input_schema     jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_schema    jsonb,
  annotations      jsonb NOT NULL DEFAULT '{}'::jsonb,
  classification   jsonb NOT NULL,
  definition_hash  text NOT NULL,
  status           text NOT NULL CHECK (status IN ('approved', 'approval_gate', 'needs_review', 'removed', 'disabled')),
  position         integer NOT NULL DEFAULT 0,
  first_seen_at    timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector, name),
  FOREIGN KEY (tenant, connector) REFERENCES connector(tenant, slug) ON DELETE CASCADE
);

-- Encrypted per principal: AAD "cred|<tenant>|<connector>|<issuer>|<principal_type>|<principal_sub>|<field>".
CREATE TABLE IF NOT EXISTS connector_credential (
  tenant              text NOT NULL REFERENCES tenant(slug),
  connector           text NOT NULL,
  issuer              text NOT NULL,
  principal_type      text NOT NULL CHECK (principal_type IN ('service', 'user')),
  principal_sub       text NOT NULL,
  refresh_token_enc   text,
  access_token_enc    text,
  access_expires_at   timestamptz,
  refresh_expires_at  timestamptz,
  key_id              text NOT NULL,
  scopes              jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector, issuer, principal_type, principal_sub)
);

CREATE TABLE IF NOT EXISTS connector_identity_link (
  tenant        text NOT NULL REFERENCES tenant(slug),
  connector     text NOT NULL,
  keelson_sub   text NOT NULL,
  ext_iss       text NOT NULL,
  ext_sub       text NOT NULL,
  ext_username  text,
  roles         jsonb NOT NULL DEFAULT '[]'::jsonb,
  scopes        jsonb NOT NULL DEFAULT '[]'::jsonb,
  status        text NOT NULL CHECK (status IN ('active', 'expired', 'revoked')),
  linked_at     timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, connector, keelson_sub)
);

CREATE TABLE IF NOT EXISTS agent_spec (
  tenant      text NOT NULL REFERENCES tenant(slug),
  version     text NOT NULL,
  spec        jsonb NOT NULL,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, version)
);

-- ───────────────────────────── RLS policies ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'tenant_llm_key', 'admin_audit', 'case', 'case_message', 'run', 'run_event', 'interrupt',
    'approval_decision', 'resume_request', 'scenario', 'plan', 'investigation_task', 'finding',
    'evidence', 'calc', 'block', 'change_set', 'commit_ledger', 'connector', 'connector_tool',
    'connector_credential', 'connector_identity_link', 'agent_spec'
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

-- admin_audit is append-only (the owner bypasses GRANTs, so use triggers).
CREATE OR REPLACE FUNCTION admin_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'admin_audit is append-only';
END $$;
DROP TRIGGER IF EXISTS admin_audit_no_update ON admin_audit;
CREATE TRIGGER admin_audit_no_update BEFORE UPDATE OR DELETE ON admin_audit
  FOR EACH ROW EXECUTE FUNCTION admin_audit_append_only();
DROP TRIGGER IF EXISTS admin_audit_no_truncate ON admin_audit;
CREATE TRIGGER admin_audit_no_truncate BEFORE TRUNCATE ON admin_audit
  FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_append_only();
