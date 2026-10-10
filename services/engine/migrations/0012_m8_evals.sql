-- M8 Evals UI, engine side (docs/contracts/128-m8-evals-ui.md §3). Re-runnable.
--
-- * eval_runner: the in-app runner's heartbeat and case catalogue (platform table: no tenant, no RLS).
-- * eval_run: one eval run, started in the back-office (source in_app) or imported from a CLI results
--   file (source import). tenant = the tenant its cases ran in (the demo tenant) or the importer's.
--   At most one queued/running run platform-wide (the runner resets Merxa's demo data per case).
--   Counters are the engine's own accounting of every run of the linked cases (the live budget's base).
-- * eval_case_result: one case of a run: summary columns for listings + the full EvalCaseResult.
--   keelson_case_id deliberately has no foreign key: a demo reset deletes Keelson cases, the history stays.
-- * eval_run_event: progress events of an in-app run (the UI polls them by seq).
-- * eval_case_link: Keelson case -> eval run. The runtime applies the run's LLM mode (replay = recorded
--   answers only) and live budget to every run of a linked case, and meters its usage here and on the
--   run. Cascades with the case (a demo reset deletes it) while the eval history stays.

-- ───────────────────────────── runner ─────────────────────────────

CREATE TABLE IF NOT EXISTS eval_runner (
  id            text PRIMARY KEY,
  version       text NOT NULL,
  suite_hash    text NOT NULL,
  suite         jsonb NOT NULL,
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT eval_runner_id_chk CHECK (length(id) BETWEEN 1 AND 80),
  CONSTRAINT eval_runner_version_chk CHECK (length(version) BETWEEN 1 AND 40),
  CONSTRAINT eval_runner_hash_chk CHECK (suite_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT eval_runner_suite_chk CHECK (octet_length(suite::text) <= 262144)
);

-- ───────────────────────────── runs ─────────────────────────────

CREATE TABLE IF NOT EXISTS eval_run (
  id                        uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant                    text NOT NULL REFERENCES tenant(slug),
  source                    text NOT NULL,
  mode                      text NOT NULL,
  status                    text NOT NULL,
  case_ids                  text[] NOT NULL DEFAULT '{}',
  requested_by              jsonb,
  note                      text,
  budget_max_tokens         bigint,
  budget_max_cost_microusd  bigint,
  tokens_live               bigint NOT NULL DEFAULT 0,
  tokens_replayed           bigint NOT NULL DEFAULT 0,
  llm_calls_live            integer NOT NULL DEFAULT 0,
  llm_calls_replayed        integer NOT NULL DEFAULT 0,
  replay_misses             integer NOT NULL DEFAULT 0,
  cost_microusd             bigint NOT NULL DEFAULT 0,
  runner_id                 text,
  lease_expires_at          timestamptz,
  cancel_requested          boolean NOT NULL DEFAULT false,
  suite_hash                text,
  thresholds                jsonb NOT NULL DEFAULT '[]'::jsonb,
  summary                   jsonb,
  threshold_results         jsonb NOT NULL DEFAULT '[]'::jsonb,
  breaches                  integer NOT NULL DEFAULT 0,
  doc_schema                text,
  doc_sha256                text,
  file_name                 text,
  target                    text,
  started_at                timestamptz,
  finished_at               timestamptz,
  error                     text,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT eval_run_source_chk CHECK (source IN ('in_app', 'import')),
  CONSTRAINT eval_run_mode_chk CHECK (mode IN ('replay', 'record_or_replay', 'record', 'unknown')),
  CONSTRAINT eval_run_status_chk CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  CONSTRAINT eval_run_in_app_mode_chk CHECK (source = 'import' OR mode <> 'unknown'),
  CONSTRAINT eval_run_budget_chk CHECK ((budget_max_tokens IS NULL) = (budget_max_cost_microusd IS NULL)
                                        AND coalesce(budget_max_tokens, 1) > 0
                                        AND coalesce(budget_max_cost_microusd, 1) > 0),
  CONSTRAINT eval_run_counters_chk CHECK (tokens_live >= 0 AND tokens_replayed >= 0 AND llm_calls_live >= 0
                                          AND llm_calls_replayed >= 0 AND replay_misses >= 0 AND cost_microusd >= 0),
  CONSTRAINT eval_run_cases_chk CHECK (cardinality(case_ids) <= 100),
  CONSTRAINT eval_run_note_chk CHECK (note IS NULL OR length(note) <= 200),
  CONSTRAINT eval_run_error_chk CHECK (error IS NULL OR length(error) <= 2000),
  CONSTRAINT eval_run_doc_chk CHECK (doc_sha256 IS NULL OR doc_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT eval_run_hash_chk CHECK (suite_hash IS NULL OR suite_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT eval_run_file_chk CHECK (file_name IS NULL OR length(file_name) <= 120),
  CONSTRAINT eval_run_target_chk CHECK (target IS NULL OR length(target) <= 200),
  CONSTRAINT eval_run_final_chk CHECK ((status IN ('queued', 'running')) = (finished_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS eval_run_tenant_id_uq ON eval_run (tenant, id);
-- one active run platform-wide (the index ignores RLS: it sees every tenant's rows)
CREATE UNIQUE INDEX IF NOT EXISTS eval_run_one_active_uq ON eval_run ((status IN ('queued', 'running')))
  WHERE status IN ('queued', 'running');
-- a results document is imported once (and the export of a run re-imports as that run)
CREATE UNIQUE INDEX IF NOT EXISTS eval_run_doc_uq ON eval_run (doc_sha256) WHERE doc_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS eval_run_recent_idx ON eval_run (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS eval_run_lease_idx ON eval_run (lease_expires_at) WHERE status = 'running';

-- ───────────────────────────── case results ─────────────────────────────

CREATE TABLE IF NOT EXISTS eval_case_result (
  tenant                text NOT NULL REFERENCES tenant(slug),
  run_id                uuid NOT NULL,
  case_id               text NOT NULL,
  ordinal               integer NOT NULL,
  title                 text NOT NULL,
  tags                  text[] NOT NULL DEFAULT '{}',
  status                text NOT NULL,
  error_code            text,
  error                 text,
  keelson_case_id       text,
  duration_ms           bigint NOT NULL DEFAULT 0,
  checks_total          integer NOT NULL DEFAULT 0,
  checks_failed         integer NOT NULL DEFAULT 0,
  soft_failed           integer NOT NULL DEFAULT 0,
  entity_recall_bp      integer,
  numeric_grounding_bp  integer,
  trajectory_recall_bp  integer,
  first_stage_ms        bigint,
  options_ms            bigint,
  tokens                bigint,
  cost_microusd         bigint,
  failed_checks         text[] NOT NULL DEFAULT '{}',
  result                jsonb NOT NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, case_id),
  FOREIGN KEY (tenant, run_id) REFERENCES eval_run (tenant, id) ON DELETE CASCADE,
  CONSTRAINT eval_case_result_status_chk CHECK (status IN ('pass', 'fail', 'error', 'skipped', 'cancelled')),
  CONSTRAINT eval_case_result_bp_chk CHECK (coalesce(entity_recall_bp, 0) BETWEEN 0 AND 10000
                                            AND coalesce(numeric_grounding_bp, 0) BETWEEN 0 AND 10000
                                            AND coalesce(trajectory_recall_bp, 0) BETWEEN 0 AND 10000),
  -- keelson_contracts.evals.MAX_CASE_RESULT_BYTES caps the canonical JSON at 1 MiB; jsonb's text form
  -- adds a space after every ':' and ',', so the table allows twice that
  CONSTRAINT eval_case_result_size_chk CHECK (octet_length(result::text) <= 2097152)
);
CREATE INDEX IF NOT EXISTS eval_case_result_case_idx ON eval_case_result (case_id, created_at DESC);

-- ───────────────────────────── progress events ─────────────────────────────

CREATE TABLE IF NOT EXISTS eval_run_event (
  id       bigserial PRIMARY KEY,
  tenant   text NOT NULL REFERENCES tenant(slug),
  run_id   uuid NOT NULL,
  seq      integer NOT NULL,
  type     text NOT NULL,
  case_id  text,
  turn_id  text,
  data     jsonb NOT NULL DEFAULT '{}'::jsonb,
  at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, seq),
  FOREIGN KEY (tenant, run_id) REFERENCES eval_run (tenant, id) ON DELETE CASCADE,
  CONSTRAINT eval_run_event_type_chk CHECK (type IN ('run_started', 'case_started', 'turn_started', 'turn_finished',
                                                      'case_finished', 'note', 'run_finished')),
  CONSTRAINT eval_run_event_data_chk CHECK (octet_length(data::text) <= 16384)
);

-- ───────────────────────────── case links ─────────────────────────────

CREATE TABLE IF NOT EXISTS eval_case_link (
  tenant              text NOT NULL REFERENCES tenant(slug),
  case_id             text PRIMARY KEY,
  run_id              uuid NOT NULL,
  eval_case_id        text NOT NULL,
  llm_mode            text NOT NULL,
  llm_calls_live      integer NOT NULL DEFAULT 0,
  llm_calls_replayed  integer NOT NULL DEFAULT 0,
  tokens_live         bigint NOT NULL DEFAULT 0,
  replay_misses       integer NOT NULL DEFAULT 0,
  cost_microusd       bigint NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant, case_id) REFERENCES "case" (tenant, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant, run_id) REFERENCES eval_run (tenant, id) ON DELETE CASCADE,
  CONSTRAINT eval_case_link_mode_chk CHECK (llm_mode IN ('replay', 'record_or_replay', 'record'))
);
CREATE INDEX IF NOT EXISTS eval_case_link_run_idx ON eval_case_link (run_id);

-- ───────────────────────────── row-level security ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['eval_run', 'eval_case_result', 'eval_run_event', 'eval_case_link']
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
