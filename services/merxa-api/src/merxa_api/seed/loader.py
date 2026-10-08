"""Load a generated dataset into Postgres; reset; fingerprint key tables.

The golden dataset of a profile is generated once per process and cached as CSV bytes
per table (:func:`golden`). A first load is one ``COPY … FORMAT csv`` per table (FK
triggers off when the role may, big tables' indexes rebuilt after the COPY); a reset of a
database that already holds the dataset merges only the tables written since
(:func:`merge_golden`). The INSERT path (every constraint checked row by row) is kept for
tests that prove both fast paths store the same content.
"""

from __future__ import annotations

import functools
import hashlib
import io
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from merxa_api import tables as t
from merxa_api.canonical import canonical_json
from merxa_api.logs import get_logger
from merxa_api.seed.dataset import Dataset, DateRange, EncodedTable, encode_dataset, fingerprint
from merxa_api.seed.generator import TABLE_ORDER, generate
from merxa_api.services import events

log = get_logger(__name__)

# Tables wiped by a reset (audit_log is append-only and survives; so do UI sessions, the
# outbox — event history and scn watermarks stay monotonic — API keys, webhook
# subscriptions/deliveries and snapshot metadata).
RESET_TABLES: tuple[str, ...] = (
    "change_set_item",
    "change_set",
    "idempotency_record",
    "production_order",
    "line_calendar",
    "factory_line",
    "shipment_line",
    "shipment",
    "capacity_booking",
    "sailing",
    "rate_card",
    "lane",
    "carrier",
    "port",
    "online_order_line",
    "online_order",
    "promotion",
    "sales_history",
    "transfer_order_line",
    "transfer_order",
    "allocation",
    "demand_forecast",
    "inventory_position",
    "po_line",
    "purchase_order",
    "size_curve",
    "bom_line",
    "sku",
    "style",
    "component",
    "factory",
    "supplier_message",
    "supplier",
    "store",
    "store_cluster",
    "dc",
    "market",
    "sim_clock",
)

# Natural ordering keys for fingerprinting (ids/timestamps/versions excluded).
HASH_TABLES: dict[str, tuple[str, ...]] = {
    "market": ("code",),
    "dc": ("code",),
    "store_cluster": ("code",),
    "store": ("code",),
    "supplier": ("code",),
    "factory": ("code",),
    "component": ("code",),
    "style": ("code",),
    "sku": ("code",),
    "bom_line": ("style_code", "component_code", "factory_code"),
    "size_curve": ("family", "cluster_code", "size"),
    "purchase_order": ("code",),
    "po_line": ("po_code", "line_no"),
    "inventory_position": ("location_type", "location_code", "sku_code"),
    "demand_forecast": ("style_code", "cluster_code", "store_code", "iso_week"),
    "allocation": ("code",),
    "transfer_order": ("code",),
    "transfer_order_line": ("to_code", "line_no"),
    "supplier_message": ("received_at", "subject"),
    "port": ("unlocode",),
    "carrier": ("code",),
    "lane": ("code",),
    "rate_card": ("lane_code", "valid"),
    "sailing": ("code",),
    "capacity_booking": ("code",),
    "shipment": ("code",),
    "shipment_line": ("shipment_code", "line_no"),
    "factory_line": ("code",),
    "line_calendar": ("line_code", "day"),
    "production_order": ("code",),
    "online_order": ("code",),
    "online_order_line": ("order_code", "line_no"),
    "promotion": ("code",),
    "sales_history": ("style_code", "store_code", "iso_week"),
}
_EXCLUDED_COLS = {"id", "version", "updated_at"}
_EXCLUDED_CREATED_AT = {"change_set", "transfer_order"}  # server-stamped created_at

_CHUNK = 2000
SEED_LOCK = 4242020


@dataclass(frozen=True)
class GoldenSeed:
    profile: str
    tables: tuple[EncodedTable, ...]
    fingerprint: str
    generate_ms: int

    @property
    def counts(self) -> dict[str, int]:
        return {tb.name: tb.rows for tb in self.tables}


async def is_seeded(conn: AsyncConnection) -> bool:
    return (await conn.execute(select(t.dc.c.code).limit(1))).first() is not None


def encode(ds: Dataset) -> tuple[EncodedTable, ...]:
    return tuple(encode_dataset(ds, TABLE_ORDER))


@functools.lru_cache(maxsize=2)
def golden(profile: str) -> GoldenSeed:
    """The golden dataset of ``profile`` as CSV per table + its fingerprint, computed once per
    process (generation is deterministic; demo resets reuse it)."""
    started = time.perf_counter()
    tables = encode(generate(profile))
    ms = round((time.perf_counter() - started) * 1000)
    return GoldenSeed(profile, tables, fingerprint(tables), ms)


def dataset_fingerprint(ds: Dataset) -> str:
    """sha256 over the generated rows (independent of the database)."""
    return fingerprint(encode(ds))


async def _disable_fk_checks(conn: AsyncConnection) -> bool:
    """Skip FK triggers for this transaction when permitted (superuser / owner with the
    privilege); otherwise keep them. Uses a savepoint so a refusal does not abort the tx."""
    try:
        async with conn.begin_nested():
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
        return True
    except Exception:
        return False


BULK_ROWS = 20_000  # tables at least this big are loaded without their indexes


async def _detach_indexes(conn: AsyncConnection, table: str) -> list[str] | None:
    """Drop the PK/UNIQUE constraints and secondary indexes of ``table`` (rebuilt after the
    COPY — a sorted build is several times faster than row-by-row maintenance). Returns the
    statements that restore them, or ``None`` when the role may not do this (then nothing
    was changed). Only valid on an empty table inside the loading transaction."""
    q = text(
        """
        SELECT 'c' AS kind, c.conname AS name, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
         WHERE c.conrelid = to_regclass(:fq) AND c.contype IN ('p', 'u')
        UNION ALL
        SELECT 'i', i.relname, pg_get_indexdef(i.oid)
          FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
         WHERE x.indrelid = to_regclass(:fq)
           AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = x.indexrelid)
        """
    )
    rows = list(await conn.execute(q, {"fq": f"{t.SCHEMA}.{table}"}))
    restore: list[str] = []
    try:
        async with conn.begin_nested():
            for kind, name, definition in rows:
                if kind == "c":
                    await conn.execute(text(f'ALTER TABLE {t.SCHEMA}.{table} DROP CONSTRAINT "{name}"'))
                    restore.append(f'ALTER TABLE {t.SCHEMA}.{table} ADD CONSTRAINT "{name}" {definition}')
                else:
                    await conn.execute(text(f'DROP INDEX {t.SCHEMA}."{name}"'))
                    restore.append(definition)
    except Exception:
        log.warning("cannot detach indexes for the seed load; loading with indexes", extra={"table": table})
        return None
    return restore


async def load_golden(conn: AsyncConnection, golden_seed: GoldenSeed, *, detach_indexes: bool = True) -> dict[str, int]:
    """COPY every table of the golden seed (inside the caller's transaction, tables empty)."""
    raw = (await conn.get_raw_connection()).driver_connection
    fast = await _disable_fk_checks(conn)
    counts: dict[str, int] = {}
    for tb in golden_seed.tables:
        counts[tb.name] = tb.rows
        if not tb.rows:
            continue
        restore = await _detach_indexes(conn, tb.name) if detach_indexes and tb.rows >= BULK_ROWS else None
        await raw.copy_to_table(
            tb.name, source=io.BytesIO(tb.data), columns=list(tb.columns), schema_name=t.SCHEMA, format="csv"
        )
        for stmt in restore or ():
            await conn.execute(text(stmt))
    if fast:
        await conn.execute(text("SET LOCAL session_replication_role = origin"))
    await _bump_sequences(conn)
    return counts


def _insert_value(value: Any) -> Any:
    if isinstance(value, DateRange):
        return Range(value.lower, value.upper, bounds="[)")
    return value


async def load(conn: AsyncConnection, ds: Dataset, *, use_copy: bool = True) -> dict[str, int]:
    """Insert ``ds`` in foreign-key order: ``COPY`` (fast path) or multi-row INSERT (every
    constraint and FK checked row by row)."""
    if use_copy:
        return await load_golden(conn, GoldenSeed(ds.profile, encode(ds), "", 0))
    counts: dict[str, int] = {}
    for name in TABLE_ORDER:
        td = ds.tables.get(name)
        counts[name] = len(td.rows) if td else 0
        if not td or not td.rows:
            continue
        table = t.metadata.tables[f"{t.SCHEMA}.{name}"]
        rows = [{c: _insert_value(v) for c, v in zip(td.columns, r, strict=True)} for r in td.rows]
        for i in range(0, len(rows), _CHUNK):
            await conn.execute(insert(table), rows[i : i + _CHUNK])
    await _bump_sequences(conn)
    return counts


async def _bump_sequences(conn: AsyncConnection) -> None:
    # Codes minted later (change sets, demo injects) must never collide with seeded codes.
    for seq, floor in (
        ("transfer_order_code_seq", 1000),
        ("po_code_seq", 100),
        ("shipment_code_seq", 1000),
        ("capacity_booking_code_seq", 100),
    ):
        await conn.execute(
            text(f"SELECT setval('merxa.{seq}', GREATEST((SELECT last_value FROM merxa.{seq}), {floor}))")
        )


async def reset(conn: AsyncConnection) -> None:
    """Delete all domain data (not the append-only audit log, sessions, outbox or config)."""
    names = ", ".join(f"merxa.{n}" for n in RESET_TABLES)
    await conn.execute(text(f"TRUNCATE {names}"))


# Natural keys used to merge a database back onto the golden rows.
MERGE_KEYS: dict[str, tuple[str, ...]] = {**HASH_TABLES, "sim_clock": ("singleton",)}
# Reset tables that are not part of the golden dataset (emptied by a merge reset).
NON_GOLDEN_RESET: tuple[str, ...] = ("change_set_item", "change_set", "idempotency_record")


async def loaded_fingerprint(conn: AsyncConnection) -> str | None:
    row = (await conn.execute(text("SELECT dataset_sha256 FROM merxa.seed_state"))).first()
    return row.dataset_sha256 if row else None


async def _record_loaded(conn: AsyncConnection, golden_seed: GoldenSeed) -> None:
    await conn.execute(
        text(
            "INSERT INTO merxa.seed_state(singleton, profile, dataset_sha256, loaded_at) VALUES (true, :p, :f, now()) "
            "ON CONFLICT (singleton) DO UPDATE SET profile = :p, dataset_sha256 = :f, loaded_at = now()"
        ),
        {"p": golden_seed.profile, "f": golden_seed.fingerprint},
    )


def _merge_sql(name: str, columns: tuple[str, ...], stage: str) -> tuple[str, str | None, str]:
    """(DELETE, UPDATE or None, INSERT) statements merging ``stage`` into ``merxa.<name>``."""
    table = t.metadata.tables[f"{t.SCHEMA}.{name}"]
    keys = MERGE_KEYS[name]

    def key(alias: str, col: str) -> str:
        return f"coalesce({alias}.{col}, '')" if table.c[col].nullable else f"{alias}.{col}"

    join = " AND ".join(f"{key('t', k)} = {key('s', k)}" for k in keys)
    data = [c for c in columns if c not in keys]
    versioned = "version" in table.c
    delete = f"DELETE FROM {t.SCHEMA}.{name} t WHERE NOT EXISTS (SELECT 1 FROM {stage} s WHERE {join})"
    update_sql = None
    if data or versioned:
        sets = [f"{c} = s.{c}" for c in data]
        changed = []
        if data:
            changed.append(
                f"({', '.join('t.' + c for c in data)}) IS DISTINCT FROM ({', '.join('s.' + c for c in data)})"
            )
        if versioned:
            sets += ["version = 1", "updated_at = now()"]
            changed.append("t.version <> 1")
        update_sql = (
            f"UPDATE {t.SCHEMA}.{name} t SET {', '.join(sets)} FROM {stage} s WHERE {join} AND ({' OR '.join(changed)})"
        )
    cols = ", ".join(columns)
    insert_sql = (
        f"INSERT INTO {t.SCHEMA}.{name} ({cols}) SELECT {', '.join('s.' + c for c in columns)} FROM {stage} s "
        f"WHERE NOT EXISTS (SELECT 1 FROM {t.SCHEMA}.{name} t WHERE {join})"
    )
    return delete, update_sql, insert_sql


async def dirty_tables(conn: AsyncConnection) -> tuple[set[str], int]:
    """Golden tables written since the last load (``seed_dirty_log``) and the log's max id."""
    rows = list(await conn.execute(text("SELECT id, table_name FROM merxa.seed_dirty_log")))
    return {r.table_name for r in rows}, max((r.id for r in rows), default=0)


async def merge_golden(conn: AsyncConnection, golden_seed: GoldenSeed, only: set[str] | None = None) -> dict[str, int]:
    """Bring a database that holds this golden dataset (plus demo changes) back to it.

    Only the golden tables in ``only`` (default: all) are merged: their golden rows are
    COPYed into temporary staging tables (no WAL), then just the rows that differ are
    deleted / updated / inserted (``version`` back to 1). Writers to those tables wait for
    the reset (SHARE ROW EXCLUSIVE). Content is identical to a fresh load; untouched rows
    keep their ``id``.
    """
    raw = (await conn.get_raw_connection()).driver_connection
    tables = [tb for tb in golden_seed.tables if only is None or tb.name in only]
    for tb in tables:
        await conn.execute(text(f"LOCK TABLE {t.SCHEMA}.{tb.name} IN SHARE ROW EXCLUSIVE MODE"))
    await _disable_fk_checks(conn)
    # in-memory hash joins over the ~120k-row tables (the default 4 MB spills to disk)
    await conn.execute(text("SET LOCAL work_mem = '32MB'"))
    await conn.execute(text(f"TRUNCATE {', '.join(f'{t.SCHEMA}.{n}' for n in NON_GOLDEN_RESET)}"))
    stages: dict[str, tuple[str, str | None, str]] = {}
    for tb in tables:
        stage = f"seed_stage_{tb.name}"
        cols = ", ".join(tb.columns)
        await conn.execute(
            text(f"CREATE TEMP TABLE {stage} ON COMMIT DROP AS SELECT {cols} FROM {t.SCHEMA}.{tb.name} WITH NO DATA")
        )
        if tb.rows:
            await raw.copy_to_table(stage, source=io.BytesIO(tb.data), columns=list(tb.columns), format="csv")
        stages[tb.name] = _merge_sql(tb.name, tb.columns, stage)
    changed: dict[str, int] = {}
    for tb in reversed(tables):
        res = await conn.execute(text(stages[tb.name][0]))
        changed[tb.name] = int(res.rowcount or 0)
    for tb in tables:
        _, upd, ins = stages[tb.name]
        n = 0
        if upd is not None:
            n += int((await conn.execute(text(upd))).rowcount or 0)
        n += int((await conn.execute(text(ins))).rowcount or 0)
        changed[tb.name] += n
    await conn.execute(text("SET LOCAL session_replication_role = origin"))
    await _bump_sequences(conn)
    touched = {k: v for k, v in changed.items() if v}
    log.info(
        "seed merged",
        extra={"profile": golden_seed.profile, "tables": [tb.name for tb in tables], "rows_changed": touched},
    )
    return golden_seed.counts


RESET_EVENT = "scenario.reset"


def reset_event_payload(golden_seed: GoldenSeed) -> dict[str, Any]:
    """Payload of the ``scenario.reset`` outbox event (webhook ``merxa.scenario.reset``)."""
    return {"profile": golden_seed.profile, "dataset_sha256": golden_seed.fingerprint, "full_resync_required": True}


async def seed(engine: AsyncEngine, profile: str = "small", *, reset_first: bool = False) -> dict[str, int]:
    """Load the golden ``profile`` in one transaction. No-op if already seeded and not resetting.

    A reset of a database that already holds this golden dataset merges the tables written
    since (see :func:`merge_golden`); otherwise (first load, other profile / dataset) it
    truncates and COPYs everything.

    Replacing data that was already loaded (demo director reset **and** the CLI ``--reset``)
    records the outbox event ``scenario.reset`` **in the same transaction**: consumers'
    snapshots and watermarks are stale the moment the new data commits (``changes_since`` →
    ``full_resync_required``; webhook ``merxa.scenario.reset``, docs/contracts/90 §10). A
    crash can never leave the data reset without the event (or the event without the reset)."""
    golden_seed = golden(profile)
    async with engine.begin() as conn:
        await conn.execute(text(f"SELECT pg_advisory_xact_lock({SEED_LOCK})"))
        # A demo reload need not wait for the WAL flush at commit: a crash right after it
        # leaves the previous (consistent) state, never a partial one.
        await conn.execute(text("SET LOCAL synchronous_commit = off"))
        seeded = await is_seeded(conn)
        if seeded and not reset_first:
            return {}
        if seeded and await loaded_fingerprint(conn) == golden_seed.fingerprint:
            dirty, upto = await dirty_tables(conn)
            counts = await merge_golden(conn, golden_seed, dirty)
            # Only the marks of the tables just merged: a writer whose mark was not yet
            # committed when the log was read (lower id than ``upto``) may commit while the
            # merge runs; its table was not merged, so its mark must survive for the next reset.
            await conn.execute(
                text("DELETE FROM merxa.seed_dirty_log WHERE id <= :upto AND table_name = ANY(:names)"),
                {"upto": upto, "names": sorted(dirty)},
            )
        else:
            if seeded:
                await reset(conn)
            counts = await load_golden(conn, golden_seed)
            await conn.execute(text("DELETE FROM merxa.seed_dirty_log"))
        await _record_loaded(conn, golden_seed)
        if seeded:
            await events.emit(conn, RESET_EVENT, reset_event_payload(golden_seed))
    log.info("seed loaded", extra={"profile": profile, "rows": sum(counts.values())})
    return counts


def _norm(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return format(Decimal(repr(value)).normalize(), "f")
    if isinstance(value, Range):
        return f"[{_norm(value.lower)},{_norm(value.upper)})"
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


async def db_fingerprint(conn: AsyncConnection, tables: tuple[str, ...] | None = None) -> dict[str, str]:
    """Per-table sha256 of key tables as stored (ids/timestamps/versions excluded)."""
    out: dict[str, str] = {}
    for name, order in HASH_TABLES.items():
        if tables is not None and name not in tables:
            continue
        table = t.metadata.tables[f"{t.SCHEMA}.{name}"]
        excluded = _EXCLUDED_COLS | ({"created_at"} if name in _EXCLUDED_CREATED_AT else set())
        cols = [c for c in table.columns if c.name not in excluded]
        res = await conn.execute(select(*cols).order_by(*(table.c[o] for o in order)))
        rows = [{c.name: _norm(v) for c, v in zip(cols, row, strict=True)} for row in res]
        out[name] = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
    return out
