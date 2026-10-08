-- M5 polish: background GC of canonical snapshots (core/scn_sync.py, docs/contracts/80 §1).
-- A snapshot to delete is only *marked* (gc_requested_at) by the sync's GC pass or a demo reset; the
-- worker's GC task then deletes its entity rows in small batches (short transactions) and finally
-- the registry row. A marked snapshot is never current, pinned or pinnable, and is hidden from the
-- snapshot listings. Re-runnable.

ALTER TABLE scn_snapshot ADD COLUMN IF NOT EXISTS gc_requested_at timestamptz;

CREATE INDEX IF NOT EXISTS scn_snapshot_gc_idx ON scn_snapshot (gc_requested_at, tenant, id)
  WHERE gc_requested_at IS NOT NULL;
