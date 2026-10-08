-- M5 remediation depth (docs/contracts/95-m5-remediation.md): quorum decisions (§1), plan
-- dependencies + webhook re-plan signals (§4), Guide-mode checklists (§2). Re-runnable
-- (IF NOT EXISTS / guarded constraints / DROP POLICY IF EXISTS). Every new table is a tenant table
-- with ENABLE + FORCE ROW LEVEL SECURITY, the usual tenant_isolation policy and composite
-- (tenant, ref) foreign keys (defence in depth for RLS, see 0002).

-- ───────────────────────────── quorum decisions (§1) ─────────────────────────────

-- The roles each person held when deciding (a four-eyes quorum is counted by maximum matching
-- over these, keelson_contracts.policy.quorum_assign) and the note of a reject/revise.
ALTER TABLE approval_decision ADD COLUMN IF NOT EXISTS roles jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE approval_decision ADD COLUMN IF NOT EXISTS reason text;

-- ───────────────────────────── plan dependencies (§4) ─────────────────────────────

-- What a saved plan version depends on (its assumptions fingerprint): lanes it uses, inbound
-- receipts (PO lines) it relies on, DCs, connectors it writes to. Written by repo.save_plan (from
-- the options), repo.create_change_set (from the items) and repo.save_plan_dependencies (the
-- runtime's fingerprint); read by the webhook re-plan router (core/replan.py).
CREATE TABLE IF NOT EXISTS plan_dependency (
  tenant        text NOT NULL REFERENCES tenant(slug),
  plan_id       bigint NOT NULL,
  case_id       text NOT NULL,
  plan_version  integer NOT NULL,
  kind          text NOT NULL,
  ref           text NOT NULL,
  source        text NOT NULL DEFAULT 'plan',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, plan_id, kind, ref, source),
  CONSTRAINT plan_dependency_kind_chk CHECK (kind IN ('lane', 'receipt', 'connector', 'dc')),
  CONSTRAINT plan_dependency_source_chk CHECK (source IN ('plan', 'change_set', 'runtime')),
  CONSTRAINT plan_dependency_ref_chk CHECK (length(ref) BETWEEN 1 AND 200),
  FOREIGN KEY (tenant, plan_id) REFERENCES plan (tenant, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant, case_id) REFERENCES "case" (tenant, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS plan_dependency_ref_idx ON plan_dependency (tenant, kind, ref);
CREATE INDEX IF NOT EXISTS plan_dependency_case_idx ON plan_dependency (tenant, case_id);

-- A structured connector event routed to a case as a `signal` run (RunRequest payload
-- {signal_id} = case_signal.id; the worker reads the details here). One per (case, event, kind).
CREATE TABLE IF NOT EXISTS case_signal (
  id          text PRIMARY KEY DEFAULT uuidv7()::text,
  tenant      text NOT NULL REFERENCES tenant(slug),
  case_id     text NOT NULL,
  connector   text NOT NULL,
  event_id    text NOT NULL,
  event_type  text NOT NULL,
  kind        text NOT NULL,
  payload     jsonb NOT NULL,
  run_id      text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT case_signal_kind_chk CHECK (kind IN ('replan')),
  UNIQUE (tenant, case_id, connector, event_id, kind),
  FOREIGN KEY (tenant, case_id) REFERENCES "case" (tenant, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS case_signal_case_idx ON case_signal (tenant, case_id, created_at);

-- ───────────────────────────── Guide mode (§2) ─────────────────────────────

CREATE TABLE IF NOT EXISTS checklist (
  id               text PRIMARY KEY DEFAULT uuidv7()::text,
  tenant           text NOT NULL REFERENCES tenant(slug),
  case_id          text NOT NULL,
  connector        text NOT NULL,
  change_set_id    text,
  plan_version     integer,
  option_id        text,
  block_id         text,
  meta             jsonb NOT NULL DEFAULT '{}'::jsonb,
  status           text NOT NULL DEFAULT 'open',
  created_by       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  completed_at     timestamptz,
  last_checked_at  timestamptz,
  last_event_id    bigint NOT NULL DEFAULT 0,
  CONSTRAINT checklist_status_chk CHECK (status IN ('open', 'completed', 'cancelled')),
  CONSTRAINT checklist_id_chk CHECK (length(id) BETWEEN 1 AND 200),
  FOREIGN KEY (tenant, case_id) REFERENCES "case" (tenant, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS checklist_tenant_id_uq ON checklist (tenant, id);
CREATE INDEX IF NOT EXISTS checklist_case_idx ON checklist (tenant, case_id, created_at);
CREATE INDEX IF NOT EXISTS checklist_open_idx ON checklist (tenant, connector) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS checklist_step (
  tenant           text NOT NULL REFERENCES tenant(slug),
  checklist_id     text NOT NULL,
  seq              integer NOT NULL,
  title            text NOT NULL,
  instructions_md  text NOT NULL,
  deep_link        text,
  expected         jsonb NOT NULL,
  status           text NOT NULL DEFAULT 'pending',
  verified_at      timestamptz,
  evidence_id      text,
  matched_ref      text,
  updated_by       text,
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (checklist_id, seq),
  CONSTRAINT checklist_step_seq_chk CHECK (seq >= 1),
  CONSTRAINT checklist_step_status_chk CHECK (status IN ('pending', 'done', 'verified', 'skipped')),
  CONSTRAINT checklist_step_verified_chk CHECK ((status = 'verified') = (verified_at IS NOT NULL)),
  FOREIGN KEY (tenant, checklist_id) REFERENCES checklist (tenant, id) ON DELETE CASCADE
);

-- ───────────────────────────── RLS (new tenant tables) ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['plan_dependency', 'case_signal', 'checklist', 'checklist_step']
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
