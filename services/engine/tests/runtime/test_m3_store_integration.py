"""The runtime's snapshot store port against the real platform side (``80-m3-deterministic-core.md`` §1).

A throwaway database migrated with the engine's own migrations (``0004_scn``: RLS FORCE on every
``scn_*`` table), connected as a non-superuser role without BYPASSRLS, exactly like production.
:class:`CoreScnAdapter` must find the current snapshot through ``core.scn_sync``, read it through
``core.db.tenant_session`` (RLS: another tenant sees nothing), build ProblemData from it, expand
an allocation group, pin the snapshot for a plan and mark it stale.

Skipped without a throwaway Postgres (``SCN_TEST_ADMIN_DSN`` / ``E1_TEST_ADMIN_DSN``) or when the
platform's ``core.scn_sync`` / ``0004_scn.sql`` are not present yet.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fakes.core import LANES
from fakes.scn_fixture import ADMIN_DSN, full_snapshot
from keelson_contracts.scn import SCN_COLUMNS
from sqlalchemy import text

from keelson_engine.runtime.problem_scn import (
    GROUP_ALLOC_PREFIX,
    build_problem_from_snapshot,
    expand_allocation_group,
)
from keelson_engine.runtime.scn_store import CoreScnAdapter

pytestmark = pytest.mark.asyncio(loop_scope="module")

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ROLE = "keelson_er_m3_test"
TENANT, OTHER = "demo-retail", "northwind-test"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def store() -> AsyncIterator[dict[str, str]]:
    import asyncpg

    if not (MIGRATIONS / "0004_scn.sql").is_file():
        pytest.skip("platform migration 0004_scn.sql not present")
    try:
        from keelson_engine.core import scn_sync  # noqa: F401
    except ImportError:
        pytest.skip("platform core.scn_sync not present")
    try:
        admin = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no throwaway Postgres ({type(exc).__name__})")
    name = f"er_store_{uuid.uuid4().hex[:8]}"
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await admin.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{name}" OWNER {ROLE}')
    finally:
        await admin.close()
    base = ADMIN_DSN.split("@", 1)[1].rsplit("/", 1)[0]
    # migration 0005_kb needs pgvector; like pg-core's init script the superuser creates it
    ext = await asyncpg.connect(ADMIN_DSN.rsplit("/", 1)[0] + "/" + name, timeout=5)
    try:
        await ext.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except asyncpg.PostgresError as exc:
        pytest.skip(f"pgvector not available ({type(exc).__name__})")
    finally:
        await ext.close()

    from keelson_engine.config import Settings, set_settings
    from keelson_engine.core.db import close_db, init_db, keelson_engine, system_session
    from keelson_engine.core.migrate import migrate

    set_settings(Settings(keelson_database_url=f"postgresql+asyncpg://{ROLE}:x@{base}/{name}",
                          migrations_dir=MIGRATIONS, log_json=False))
    await init_db()
    snapshot_id = str(uuid.uuid4())
    try:
        await migrate(keelson_engine())
        async with system_session() as s:
            for slug in (TENANT, OTHER):
                await s.execute(text("INSERT INTO tenant (slug, name) VALUES (:s, :s) ON CONFLICT DO NOTHING"),
                                {"s": slug})
            await s.execute(
                text("INSERT INTO scn_snapshot (id, tenant, connector, status, as_of, watermark, ready_at) "
                     "VALUES (:i, :t, 'merxa', 'ready', '2026-10-05T06:00:00Z', '0/1', now())"),
                {"i": uuid.UUID(snapshot_id), "t": TENANT},
            )
            await s.execute(text("INSERT INTO scn_current (tenant, connector, snapshot_id) VALUES (:t, 'merxa', :i)"),
                            {"t": TENANT, "i": uuid.UUID(snapshot_id)})
            case = await s.execute(
                text("INSERT INTO \"case\" (tenant, title, mode, status, created_by) VALUES (:t, 'x', 'actual', 'open', 'u') "
                     "RETURNING id"), {"t": TENANT})
            case_id = case.scalar_one()
            plan = await s.execute(
                text("INSERT INTO plan (tenant, case_id, version, options, recommended_option_id, kpis) "
                     "VALUES (:t, :c, 1, '[]', 'opt-A', '{}') RETURNING id"), {"t": TENANT, "c": case_id})
            plan_id = plan.scalar_one()
            # bulk load as the platform does: rows of one snapshot into the RLS-protected tables
            rows = full_snapshot()
            conn = await (await s.connection()).get_raw_connection()
            pg = conn.driver_connection
            for entity, cols in SCN_COLUMNS.items():
                records = [(TENANT, uuid.UUID(snapshot_id), *r) for r in rows.get(entity, [])]
                if not records:
                    continue
                await pg.execute(f"CREATE TEMP TABLE t_{entity} (LIKE scn_{entity}) ON COMMIT DROP")
                await pg.copy_records_to_table(f"t_{entity}", records=records, columns=["tenant", "snapshot_id", *cols])
                await pg.execute(f"INSERT INTO scn_{entity} SELECT * FROM t_{entity}")
        async with system_session() as s:
            await s.execute(text("ANALYZE"))
        yield {"snapshot_id": snapshot_id, "plan_id": str(plan_id)}
    finally:
        await close_db()
        set_settings(None)
        admin = await asyncpg.connect(ADMIN_DSN, timeout=5)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


async def test_current_snapshot_and_rls_scoped_reads(store: dict[str, str]) -> None:
    adapter = CoreScnAdapter()
    snap = await adapter.current_snapshot(tenant=TENANT, connector="merxa")
    assert snap is not None and snap.id == store["snapshot_id"] and snap.as_of == dt.date(2026, 10, 5)
    assert snap.watermark == "0/1"
    assert await adapter.current_snapshot(tenant=OTHER, connector="merxa") is None
    async with adapter.reader(TENANT) as reader:
        data = (await build_problem_from_snapshot(TENANT, reader, snapshot=snap, lanes=LANES,
                                                  as_of=dt.date(2026, 10, 5), horizon_weeks=9)).data
    assert len(data.dcs) == 4 and len(data.skus) == 180 and data.po_lines and data.allocations
    # the other tenant's RLS-scoped session sees none of these rows, even with the snapshot id
    async with adapter.reader(OTHER) as reader:
        theirs = (await build_problem_from_snapshot(OTHER, reader, snapshot=snap, lanes=LANES,
                                                    as_of=dt.date(2026, 10, 5), horizon_weeks=9)).data
    assert theirs.dcs == [] and theirs.po_lines == [] and theirs.allocations == []
    group = next(a for a in data.allocations if a.code.startswith(GROUP_ALLOC_PREFIX))
    async with adapter.reader(TENANT) as reader:
        members = await expand_allocation_group(reader, tenant=TENANT, snapshot_id=snap.id, group_code=group.code)
    assert sum(m.qty for m in members) == group.qty


async def test_pin_and_mark_stale(store: dict[str, str]) -> None:
    from keelson_engine.core import scn_sync
    from keelson_engine.core.db import tenant_session

    adapter = CoreScnAdapter()
    await adapter.pin_snapshot(tenant=TENANT, snapshot_id=store["snapshot_id"], plan_id=int(store["plan_id"]))
    await adapter.pin_snapshot(tenant=TENANT, snapshot_id=store["snapshot_id"], plan_id=int(store["plan_id"]))
    async with tenant_session(TENANT) as s:
        pins = (await s.execute(text("SELECT count(*) FROM scn_pin WHERE plan_id = :p"),
                                {"p": int(store["plan_id"])})).scalar_one()
    assert pins == 1  # idempotent
    await adapter.unpin_plan(tenant=TENANT, plan_id=int(store["plan_id"]))  # superseded plan: pin released
    async with tenant_session(TENANT) as s:
        pins = (await s.execute(text("SELECT count(*) FROM scn_pin WHERE plan_id = :p"),
                                {"p": int(store["plan_id"])})).scalar_one()
    assert pins == 0
    await adapter.pin_snapshot(tenant=TENANT, snapshot_id=store["snapshot_id"], plan_id=int(store["plan_id"]))
    with pytest.raises(LookupError):  # unknown snapshot: the planner logs it and carries on
        await adapter.pin_snapshot(tenant=TENANT, snapshot_id=str(uuid.uuid4()), plan_id=int(store["plan_id"]))
    await adapter.mark_stale(tenant=TENANT, connector="merxa", reason="change_set.applied")
    state = await scn_sync.sync_state(TENANT, "merxa")
    assert state is not None and state["reason"] == "change_set.applied" and state["due_at"]
    assert await adapter.due_syncs() == []  # the debounce has not passed (and no capable connector row)
