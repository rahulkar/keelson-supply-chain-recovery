-- Tenant consistency of references (defence in depth for RLS).
--
-- Foreign-key checks bypass row-level security, so with only `case_id -> "case"(id)` a session
-- of tenant A could insert a child row (tenant = A, WITH CHECK passes) that points at a case
-- (or interrupt / change set / plan) of tenant B. Composite keys `(tenant, <ref>)` make the
-- database itself reject such rows. Re-runnable: indexes IF NOT EXISTS, constraints only
-- added when missing. Constraints are added NOT VALID (enforced for every new/updated row at
-- once) and then validated; a legacy inconsistent row only produces a WARNING instead of
-- failing the bootstrap.

CREATE UNIQUE INDEX IF NOT EXISTS case_tenant_id_uq ON "case" (tenant, id);
CREATE UNIQUE INDEX IF NOT EXISTS interrupt_tenant_id_uq ON interrupt (tenant, id);
CREATE UNIQUE INDEX IF NOT EXISTS change_set_tenant_id_uq ON change_set (tenant, id);
CREATE UNIQUE INDEX IF NOT EXISTS plan_tenant_id_uq ON plan (tenant, id);

DO $$
DECLARE
  spec text[];
BEGIN
  FOREACH spec SLICE 1 IN ARRAY ARRAY[
    -- table,                constraint name,                     local cols,        referenced table + cols
    ARRAY['case_message',       'case_message_tenant_case_fk',       'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['run',                'run_tenant_case_fk',                'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['run_event',          'run_event_tenant_case_fk',          'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['interrupt',          'interrupt_tenant_case_fk',          'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['scenario',           'scenario_tenant_case_fk',           'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['plan',               'plan_tenant_case_fk',               'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['investigation_task', 'investigation_task_tenant_case_fk', 'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['finding',            'finding_tenant_case_fk',            'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['evidence',           'evidence_tenant_case_fk',           'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['calc',               'calc_tenant_case_fk',               'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['block',              'block_tenant_case_fk',              'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['change_set',         'change_set_tenant_case_fk',         'tenant, case_id', '"case" (tenant, id)'],
    ARRAY['change_set',         'change_set_tenant_plan_fk',         'tenant, plan_id', 'plan (tenant, id)'],
    ARRAY['approval_decision',  'approval_decision_tenant_intr_fk',  'tenant, interrupt_id', 'interrupt (tenant, id)'],
    ARRAY['resume_request',     'resume_request_tenant_intr_fk',     'tenant, interrupt_id', 'interrupt (tenant, id)'],
    ARRAY['commit_ledger',      'commit_ledger_tenant_cs_fk',        'tenant, change_set_id', 'change_set (tenant, id)']
  ]
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = spec[2]) THEN
      EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (%s) REFERENCES %s ON DELETE CASCADE NOT VALID',
                     spec[1], spec[2], spec[3], spec[4]);
    END IF;
    BEGIN
      EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', spec[1], spec[2]);
    EXCEPTION WHEN foreign_key_violation THEN
      RAISE WARNING 'constraint % on % not validated: legacy rows reference another tenant', spec[2], spec[1];
    END;
  END LOOP;
END $$;
