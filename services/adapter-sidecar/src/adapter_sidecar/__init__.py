"""Keelson adapter sidecar template (docs/contracts/122-m8-adapter-sidecar.md).

A small FastAPI + FastMCP 4 service that a customer runs next to a system Keelson cannot reach
natively (a CSV directory, a SQL database or a read-only REST API). Driven by one declarative
YAML file, it offers the tier B/C/W tools Keelson already speaks to Merxa:

* ``scn.snapshot_open`` / ``scn.snapshot_export`` — signed Parquet parts of every ``scn/1.0``
  entity (tier B);
* ``scn.changes_since`` — a change journal of its own applies plus detected source drift (tier C);
* ``create/add/validate/apply/rollback_change_set`` with Keelson approval receipts, idempotency
  keys and an append-only audit log (tier W).

Deterministic and offline by construction: no model is ever called.
"""

__version__ = "0.1.0"
