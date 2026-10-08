-- M8 clustering v2 (docs/contracts/123-m8-signal-clustering.md): SimHash near-duplicates, cluster
-- terms, membership explanations, platform clustering settings, opt-in remote embedding profiles.
-- Re-runnable (IF NOT EXISTS / DROP ... IF EXISTS), runs as role `signals`. Creates no pgvector
-- objects: profile tables (signal_vec_<id>) are created at runtime by signal_hub.vectors, and only
-- when the extension exists.

-- ───────────── news items: SimHash fingerprint + near-duplicate link ─────────────
-- simhash: signed 64-bit fingerprint of headline + snippet (signal_hub.simhash); simhash_bands: its
-- eight 8-bit bands for the GIN index (any pair within 7 bits shares a band). near_duplicate_of: the
-- original item (always a root: never itself a copy). Status 'duplicate' = an identical re-post by the
-- same source (never extracted, never a signal).
ALTER TABLE news_item ADD COLUMN IF NOT EXISTS simhash bigint;
ALTER TABLE news_item ADD COLUMN IF NOT EXISTS simhash_bands integer[];
ALTER TABLE news_item ADD COLUMN IF NOT EXISTS near_duplicate_of uuid;
ALTER TABLE news_item ADD COLUMN IF NOT EXISTS near_duplicate_bits smallint;
ALTER TABLE news_item DROP CONSTRAINT IF EXISTS news_item_near_duplicate_bits_check;
ALTER TABLE news_item ADD CONSTRAINT news_item_near_duplicate_bits_check
  CHECK (near_duplicate_bits IS NULL OR near_duplicate_bits BETWEEN 0 AND 64);
ALTER TABLE news_item DROP CONSTRAINT IF EXISTS news_item_status_check;
ALTER TABLE news_item ADD CONSTRAINT news_item_status_check
  CHECK (status IN ('pending_extraction','extracted','flagged','rejected','failed','duplicate'));
CREATE INDEX IF NOT EXISTS news_item_simhash_bands ON news_item USING gin (simhash_bands) WHERE simhash IS NOT NULL;
CREATE INDEX IF NOT EXISTS news_item_retrieved ON news_item (retrieved_at DESC);

-- ───────────── signals: cluster terms + near-duplicate original ─────────────
-- cluster_terms: k:/s:/t:/p:/o: terms (signal_hub.embeddings), NOT part of content_hash.
-- near_duplicate_of: the original signal this one is a syndicated copy of (it adds no source to
-- its event's corroboration while the original is a member too).
ALTER TABLE signal ADD COLUMN IF NOT EXISTS cluster_terms text[] NOT NULL DEFAULT '{}';
ALTER TABLE signal ADD COLUMN IF NOT EXISTS near_duplicate_of uuid;

-- ───────────── why each signal is in its event (mixed scope, like `signal`) ─────────────
-- One row per signal (every version); the event is the signal's own event_id. Removed with the
-- signal (demo reset). The explanation is server-built from codes and curated names only.
CREATE TABLE IF NOT EXISTS signal_membership (
  signal_id      uuid PRIMARY KEY REFERENCES signal (id) ON DELETE CASCADE,
  tenant         text,
  method         text NOT NULL CHECK (method IN ('anchor','same_hazard','founded','attach_geo','attach_ref',
                                                 'attach_vector','near_duplicate','absorbed','merged_vector')),
  prior_method   text CHECK (prior_method IS NULL OR prior_method IN ('anchor','same_hazard','founded','attach_geo',
                                                                      'attach_ref','attach_vector','near_duplicate')),
  from_event_id  uuid,
  distance_km    integer CHECK (distance_km IS NULL OR distance_km >= 0),
  shared_refs    text[] NOT NULL DEFAULT '{}',
  similarity_bp  integer CHECK (similarity_bp IS NULL OR similarity_bp BETWEEN 0 AND 10000),
  similarity_profile text,
  hamming_bits   smallint CHECK (hamming_bits IS NULL OR hamming_bits BETWEEN 0 AND 64),
  duplicate_of   uuid,
  config_version integer NOT NULL CHECK (config_version >= 1),
  explanation    text NOT NULL CHECK (length(explanation) BETWEEN 1 AND 400),
  decided_at     timestamptz NOT NULL,  -- the decision time (the sim clock in a demo replay)
  recorded_at    timestamptz NOT NULL DEFAULT now()  -- wall clock (status counts)
);
CREATE INDEX IF NOT EXISTS signal_membership_recorded ON signal_membership (recorded_at DESC);

-- ───────────── platform clustering settings (one versioned row; system writes only) ─────────────
-- No row = DEFAULT_CLUSTERING_CONFIG (keelson_contracts.signals). Every tenant reads it (clustering
-- is shared by every tenant); only the system context writes it (PUT /v1/clustering/config with a
-- platform-scoped engine token).
CREATE TABLE IF NOT EXISTS cluster_config (
  id         smallint PRIMARY KEY CHECK (id = 1),
  version    integer NOT NULL CHECK (version >= 1),
  config     jsonb NOT NULL,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────── remote embedding profiles (opt-in; system writes only) ─────────────
-- At most one active profile; its vectors live in signal_vec_<id> (vector(d) up to 2000 dims,
-- halfvec(d) up to 4000), created/dropped by signal_hub.vectors.
CREATE TABLE IF NOT EXISTS embedding_profile (
  id         serial PRIMARY KEY,
  alias      text NOT NULL CHECK (alias ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
  dims       integer NOT NULL CHECK (dims BETWEEN 1 AND 4000),
  status     text NOT NULL CHECK (status IN ('active','retired')),
  created_at timestamptz NOT NULL DEFAULT now(),
  retired_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS embedding_profile_one_active ON embedding_profile ((true)) WHERE status = 'active';

-- Embedding work per (signal, profile) (mixed scope, like `signal`): created by the maintenance pass
-- for current signals of the last 14 days; only the API (which holds the LLM key) embeds.
CREATE TABLE IF NOT EXISTS embedding_job (
  signal_id       uuid NOT NULL REFERENCES signal (id) ON DELETE CASCADE,
  profile_id      integer NOT NULL REFERENCES embedding_profile (id) ON DELETE CASCADE,
  tenant          text,
  status          text NOT NULL CHECK (status IN ('pending','done','failed')),
  attempts        integer NOT NULL DEFAULT 0,
  bad_outputs     integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz,
  last_error      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (signal_id, profile_id)
);
CREATE INDEX IF NOT EXISTS embedding_job_due ON embedding_job (next_attempt_at) WHERE status = 'pending';

-- ───────────── RLS ─────────────
DO $$
DECLARE t text;
BEGIN
  -- mixed tables: read global + own, write exactly own scope (as `signal`)
  FOREACH t IN ARRAY ARRAY['signal_membership','embedding_job']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_read', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_insert', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_update', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_delete', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (tenant IS NULL OR tenant = sh_tenant())', t || '_read', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR INSERT WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant())',
                   t || '_insert', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR UPDATE USING (tenant IS NOT DISTINCT FROM sh_tenant()) '
                   'WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant())', t || '_update', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR DELETE USING (tenant IS NOT DISTINCT FROM sh_tenant())',
                   t || '_delete', t);
  END LOOP;
  -- platform tables: everyone reads, only the system context writes
  FOREACH t IN ARRAY ARRAY['cluster_config','embedding_profile']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_read', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_write', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING (true)', t || '_read', t);
    EXECUTE format('CREATE POLICY %I ON %I FOR ALL USING (sh_tenant() IS NULL) WITH CHECK (sh_tenant() IS NULL)',
                   t || '_write', t);
  END LOOP;
END $$;
