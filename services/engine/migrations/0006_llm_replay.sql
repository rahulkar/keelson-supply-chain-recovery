-- Engine-level LLM record/replay (plan §16 "Demo determinism", spike S9; runtime/replay.py).
-- One row per normalised request key and tenant: the recorded LlmResponse sequence (partials +
-- final, incl. function calls, thought parts and usage) with run-volatile ids replaced by
-- placeholders. Re-runnable (IF NOT EXISTS / DROP POLICY IF EXISTS).

CREATE TABLE IF NOT EXISTS llm_replay (
  tenant        text NOT NULL REFERENCES tenant(slug),
  key           text NOT NULL,
  alias         text NOT NULL,
  agent         text NOT NULL DEFAULT '',
  spec_version  text NOT NULL DEFAULT '',
  model         text NOT NULL,
  responses     jsonb NOT NULL,
  usage         jsonb,
  bytes         integer NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant, key),
  CONSTRAINT llm_replay_key_chk CHECK (key ~ '^[0-9a-f]{64}$'),
  CONSTRAINT llm_replay_alias_chk CHECK (length(alias) BETWEEN 1 AND 120),
  CONSTRAINT llm_replay_responses_chk CHECK (jsonb_typeof(responses) = 'array'),
  -- runtime/replay.py HARD_MAX_BYTES (LLM_REPLAY_MAX_BYTES is clamped to it)
  CONSTRAINT llm_replay_bytes_chk CHECK (bytes BETWEEN 0 AND 8388608)
);
-- housekeeping: "forget recordings older than …" / per-agent inspection
CREATE INDEX IF NOT EXISTS llm_replay_created_idx ON llm_replay (tenant, created_at);

ALTER TABLE llm_replay ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_replay FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON llm_replay;
CREATE POLICY tenant_isolation ON llm_replay AS PERMISSIVE FOR ALL
  USING (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on')
  WITH CHECK (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on');
