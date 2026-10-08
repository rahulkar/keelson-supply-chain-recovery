-- M4 knowledge base (docs/contracts/90-m4-investigation.md §7): embedding profiles, knowledge bases,
-- documents, chunks and the re-embed job log. Re-runnable (IF NOT EXISTS / guarded changes).
--
-- The vectors live in one table per embedding profile, kb_embedding_<profile id>(chunk_id, tenant,
-- embedding vector(dims) | halfvec(dims) when dims > 2000) + HNSW cosine index + RLS FORCE. Those
-- tables are created by core/kb.py when a profile is created (bootstrap seeds the first one from
-- models.lock, a re-embed creates a shadow table and swaps), because their column type depends on
-- the profile's dims.
--
-- pgvector: pg-core's init script creates the extension as superuser (it is not a trusted extension,
-- so the keelson role cannot); IF NOT EXISTS makes this a no-op there and fails loudly elsewhere.
CREATE EXTENSION IF NOT EXISTS vector;

-- ───────────────────────────── embedding profiles (platform table, no tenant) ─────────────────────────────

CREATE TABLE IF NOT EXISTS embedding_profile (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alias           text NOT NULL,
  provider_model  text NOT NULL,
  dims            integer NOT NULL,
  version         integer NOT NULL,
  active          boolean NOT NULL DEFAULT false,
  status          text NOT NULL DEFAULT 'building',
  created_at      timestamptz NOT NULL DEFAULT now(),
  activated_at    timestamptz,
  retired_at      timestamptz,
  CONSTRAINT embedding_profile_dims_chk CHECK (dims BETWEEN 1 AND 4000),
  CONSTRAINT embedding_profile_status_chk CHECK (status IN ('building', 'active', 'retired', 'failed')),
  CONSTRAINT embedding_profile_active_chk CHECK (active = (status = 'active')),
  CONSTRAINT embedding_profile_alias_version_uq UNIQUE (alias, version)
);
-- exactly one active profile at a time (the swap flips both rows in one transaction)
CREATE UNIQUE INDEX IF NOT EXISTS embedding_profile_one_active_uq ON embedding_profile ((true)) WHERE active;

-- ───────────────────────────── knowledge bases ─────────────────────────────

CREATE TABLE IF NOT EXISTS kb (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant       text NOT NULL REFERENCES tenant(slug),
  name         text NOT NULL,
  description  text NOT NULL DEFAULT '',
  created_by   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kb_name_chk CHECK (length(name) BETWEEN 1 AND 120)
);
CREATE UNIQUE INDEX IF NOT EXISTS kb_tenant_id_uq ON kb (tenant, id);
CREATE UNIQUE INDEX IF NOT EXISTS kb_tenant_name_uq ON kb (tenant, lower(name));

CREATE TABLE IF NOT EXISTS kb_document (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant      text NOT NULL REFERENCES tenant(slug),
  kb_id       uuid NOT NULL,
  title       text NOT NULL,
  filename    text,
  mime        text NOT NULL,
  bytes       bigint NOT NULL,
  sha256      text NOT NULL,
  status      text NOT NULL DEFAULT 'processing',
  error       text,
  chunks      integer NOT NULL DEFAULT 0,
  tokens      integer NOT NULL DEFAULT 0,
  created_by  text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kb_document_status_chk CHECK (status IN ('processing', 'ready', 'failed')),
  CONSTRAINT kb_document_mime_chk CHECK (mime IN ('text/markdown', 'text/plain', 'application/pdf')),
  CONSTRAINT kb_document_sha_chk CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  FOREIGN KEY (tenant, kb_id) REFERENCES kb (tenant, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS kb_document_tenant_id_uq ON kb_document (tenant, id);
-- idempotent ingestion: the same bytes in the same knowledge base are one document
CREATE UNIQUE INDEX IF NOT EXISTS kb_document_sha_uq ON kb_document (tenant, kb_id, sha256);
CREATE INDEX IF NOT EXISTS kb_document_kb_idx ON kb_document (tenant, kb_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kb_chunk (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant       text NOT NULL REFERENCES tenant(slug),
  document_id  uuid NOT NULL,
  ordinal      integer NOT NULL,
  text         text NOT NULL,
  tokens       integer NOT NULL,
  heading      text,
  FOREIGN KEY (tenant, document_id) REFERENCES kb_document (tenant, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS kb_chunk_tenant_id_uq ON kb_chunk (tenant, id);
CREATE UNIQUE INDEX IF NOT EXISTS kb_chunk_ordinal_uq ON kb_chunk (tenant, document_id, ordinal);

-- re-embed jobs (POST /api/admin/kb/reembed): tenant = NULL for a platform profile change
-- (shadow table + swap over every tenant), else a tenant repair of missing vectors.
CREATE TABLE IF NOT EXISTS kb_reembed_job (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  tenant           text REFERENCES tenant(slug),
  scope            text NOT NULL,
  from_profile_id  bigint,
  to_profile_id    bigint,
  status           text NOT NULL DEFAULT 'running',
  chunks_total     integer NOT NULL DEFAULT 0,
  chunks_done      integer NOT NULL DEFAULT 0,
  error            text,
  requested_by     text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  CONSTRAINT kb_reembed_job_scope_chk CHECK (scope IN ('platform', 'tenant')),
  CONSTRAINT kb_reembed_job_scope_tenant_chk CHECK ((scope = 'platform') = (tenant IS NULL)),
  CONSTRAINT kb_reembed_job_status_chk CHECK (status IN ('running', 'succeeded', 'failed'))
);
CREATE INDEX IF NOT EXISTS kb_reembed_job_recent_idx ON kb_reembed_job (tenant, created_at DESC);
-- one running job per scope (platform, or one tenant)
CREATE UNIQUE INDEX IF NOT EXISTS kb_reembed_job_one_running_uq ON kb_reembed_job ((coalesce(tenant, '*')))
  WHERE status = 'running';

-- ───────────────────────────── RLS ─────────────────────────────

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['kb', 'kb_document', 'kb_chunk', 'kb_reembed_job']
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
