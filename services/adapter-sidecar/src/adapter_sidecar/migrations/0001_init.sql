-- adapter-sidecar state store (SQLite), docs/contracts/122-m8-adapter-sidecar.md §5.
--
-- * change_set / change_set_item / change_set_effect: the change-set facade (Merxa-compatible
--   shapes). Effects are the exact row images every applied primitive wrote: rollback replays
--   them backwards (update -> restore the before image, insert -> delete).
-- * idempotency_record: (principal, key) -> stored response (24 h); used_receipt: single-use jti.
-- * audit_log: append-only (triggers refuse UPDATE / DELETE).
-- * journal: the tier-C change feed; seq never repeats (AUTOINCREMENT), watermark "j:<seq>".
--   kind change = a row the sidecar wrote, external = source drift it detected, reset = demo reset.
-- * source_fingerprint: per source (csv file / sql table / rest endpoint) the last sha256 seen.
-- * snapshot: scn/1.0 export manifests (parts live under SNAPSHOT_DIR/<id>/).
-- * apply_journal: an apply that touched the outside world but has not been settled yet
--   (staged CSV files, or a SQL backend commit awaiting its state write); replayed at start-up.
-- * sequence: change-set codes (CS-0001...) and generated record codes.
-- CSV / REST backends also stage their tables in this database (names reserved: config.STATE_TABLES).

CREATE TABLE IF NOT EXISTS change_set (
  id               TEXT PRIMARY KEY,
  code             TEXT NOT NULL UNIQUE,
  title            TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('draft', 'validated', 'applying', 'applied', 'conflict',
                                                   'failed', 'rolled_back')),
  origin           TEXT NOT NULL DEFAULT '{}',
  items_count      INTEGER NOT NULL DEFAULT 0,
  preview          TEXT,
  preview_hash     TEXT,
  approval_ref     TEXT,
  receipt_jti      TEXT UNIQUE,
  approved_by      TEXT,
  approver_name    TEXT,
  initiated_by     TEXT,
  co_approvers     TEXT,
  via              TEXT,
  created_by       TEXT,
  applied_by       TEXT,
  rollback_of      TEXT REFERENCES change_set (id),
  rolled_back_by   TEXT,
  result           TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  validated_at     TEXT,
  applied_at       TEXT,
  version          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS change_set_item (
  change_set_id    TEXT NOT NULL REFERENCES change_set (id),
  seq              INTEGER NOT NULL,
  op               TEXT NOT NULL,
  target_ref       TEXT,
  expected_version INTEGER,
  payload          TEXT NOT NULL,
  before           TEXT,
  after            TEXT,
  status           TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'error', 'applied')),
  error            TEXT,
  result_ref       TEXT,
  PRIMARY KEY (change_set_id, seq)
);

CREATE TABLE IF NOT EXISTS change_set_effect (
  change_set_id    TEXT NOT NULL REFERENCES change_set (id),
  n                INTEGER NOT NULL,
  item_seq         INTEGER NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('update', 'insert', 'delete')),
  tbl              TEXT NOT NULL,
  key              TEXT NOT NULL,
  before           TEXT,
  after            TEXT,
  PRIMARY KEY (change_set_id, n)
);

CREATE TABLE IF NOT EXISTS idempotency_record (
  principal        TEXT NOT NULL,
  key              TEXT NOT NULL,
  request_sha256   TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('in_flight', 'done')),
  status_code      INTEGER,
  response         TEXT,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (principal, key)
);

CREATE TABLE IF NOT EXISTS used_receipt (
  jti              TEXT PRIMARY KEY,
  purpose          TEXT NOT NULL CHECK (purpose IN ('apply', 'rollback')),
  change_set_id    TEXT NOT NULL,
  used_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  at               TEXT NOT NULL,
  change_set_code  TEXT,
  entity_type      TEXT NOT NULL,
  entity_ref       TEXT NOT NULL,
  action           TEXT NOT NULL,
  before           TEXT,
  after            TEXT,
  client_id        TEXT,
  via              TEXT,
  approval_ref     TEXT,
  approved_by      TEXT,
  approver_name    TEXT,
  initiated_by     TEXT,
  co_approved_by   TEXT
);
CREATE INDEX IF NOT EXISTS audit_log_cs ON audit_log (change_set_code);
CREATE INDEX IF NOT EXISTS audit_log_entity ON audit_log (entity_type, entity_ref);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE IF NOT EXISTS journal (
  seq              INTEGER PRIMARY KEY AUTOINCREMENT,
  at               TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('change', 'external', 'reset')),
  entity           TEXT,
  op               TEXT CHECK (op IS NULL OR op IN ('upsert', 'delete')),
  key              TEXT,
  version          INTEGER,
  change_set_code  TEXT,
  detail           TEXT
);

CREATE TABLE IF NOT EXISTS source_fingerprint (
  source           TEXT PRIMARY KEY,
  sha256           TEXT NOT NULL,
  observed_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot (
  id               TEXT PRIMARY KEY,
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  as_of            TEXT NOT NULL,
  watermark        TEXT NOT NULL,
  consistency      TEXT NOT NULL CHECK (consistency IN ('consistent', 'crawled')),
  as_of_from       TEXT,
  as_of_to         TEXT,
  manifest         TEXT NOT NULL,
  created_by       TEXT
);

CREATE TABLE IF NOT EXISTS apply_journal (
  change_set_id    TEXT PRIMARY KEY,
  purpose          TEXT NOT NULL CHECK (purpose IN ('apply', 'rollback')),
  plan             TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence (
  name             TEXT PRIMARY KEY,
  value            INTEGER NOT NULL
);
