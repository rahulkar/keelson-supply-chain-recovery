"""Background snapshot GC (M5 polish, ``core/scn_sync.py``): syncs and demo resets only *mark*
snapshots (``gc_requested_at``) — fast at any volume — and the worker's GC task deletes the rows in
batches (short transactions, yielding), never touching the current or a pinned snapshot. The backlog
here is a scaled-down full profile (~300k rows per snapshot in production)."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
import uuid
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from e1_env import TENANT_A, TENANT_B
from keelson_engine.config import Settings
from keelson_engine.core import demo_reset, repo, scn_sync
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.runtime.scn_store import CoreScnAdapter
from keelson_engine.runtime.settings import WorkerSettings
from keelson_engine.worker import Worker

pytestmark = pytest.mark.asyncio(loop_scope="session")

CONN = "gc-erp"
ROWS = 12_000
"""Rows per bulk table and snapshot (forecasts + allocations → 24k rows per snapshot)."""


async def _snapshot(tenant: str, status: str, rows: int = ROWS, *, current: bool = False) -> str:
    async with tenant_session(tenant) as s:
        sid = (await s.execute(text(
            "INSERT INTO scn_snapshot (id, tenant, connector, status) VALUES (:i, :t, :c, :st) RETURNING id"),
            {"i": uuid.uuid7(), "t": tenant, "c": CONN, "st": status})).scalar_one()
        await s.execute(text(
            "INSERT INTO scn_demand_forecast (tenant, snapshot_id, source_id, style_code, location_kind, location_code, "
            "iso_week, units, forecast_version) SELECT :t, :s, 'fc:' || g, 'ST1', 'store', 'S' || (g % 450), "
            "'2026-W45', g % 50, 1 FROM generate_series(1, :n) g"), {"t": tenant, "s": sid, "n": rows})
        await s.execute(text(
            "INSERT INTO scn_allocation (tenant, snapshot_id, source_id, code, sku_code, dc_code, store_code, ship_week, "
            "qty, status, src_version) SELECT :t, :s, 'al:' || g, 'AL-' || g, 'ST1-M', 'UK1', 'S001', '2026-W45', 1, "
            "'planned', 1 FROM generate_series(1, :n) g"), {"t": tenant, "s": sid, "n": rows})
        if current:
            await s.execute(text(
                "INSERT INTO scn_current (tenant, connector, snapshot_id) VALUES (:t, :c, :s) "
                "ON CONFLICT (tenant, connector) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id"),
                {"t": tenant, "c": CONN, "s": sid})
    return str(sid)


async def _rows(tenant: str, sid: str) -> int:
    async with tenant_session(tenant) as s:
        return int((await s.execute(text(
            "SELECT (SELECT count(*) FROM scn_demand_forecast WHERE snapshot_id = :i) + "
            "(SELECT count(*) FROM scn_allocation WHERE snapshot_id = :i)"), {"i": sid})).scalar_one())


async def _registry(tenant: str) -> dict[str, Any]:
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT id::text, gc_requested_at FROM scn_snapshot WHERE connector = :c"),
                              {"c": CONN})
        return {r[0]: r[1] for r in res.all()}


async def _plan(tenant: str) -> int:
    case_id = await repo.create_case("gc pin", created_by="u", tenant=tenant)
    async with tenant_session(tenant) as s:
        return int((await s.execute(
            text("INSERT INTO plan (tenant, case_id, version, options) VALUES (:t, :c, 1, '[]'::jsonb) RETURNING id"),
            {"t": tenant, "c": case_id})).scalar_one())


async def _purge(tenant: str) -> None:
    async with tenant_session(tenant) as s:
        await scn_sync.purge_connector(s, tenant, CONN)


async def _backlog(tenant: str = TENANT_A) -> dict[str, str]:
    """Three old snapshots (one pinned), the current one, and a loading attempt."""
    await _purge(tenant)
    out = {f"old{i}": await _snapshot(tenant, "superseded") for i in range(3)}
    out["pinned"] = await _snapshot(tenant, "superseded")
    await scn_sync.pin_snapshot(tenant, out["pinned"], await _plan(tenant))
    out["current"] = await _snapshot(tenant, "ready", current=True)
    out["loading"] = await _snapshot(tenant, "loading", rows=10)
    return out


async def test_demo_reset_marks_fast_and_gc_completes_in_batches(platform: None) -> None:
    snaps = await _backlog()
    try:
        # the reset's row step: cases (and with them plans + pins) go, the old snapshots are only marked
        await repo.create_case("to be reset", created_by="u", tenant=TENANT_A)
        t0 = time.perf_counter()
        counts, _ = await demo_reset._keelson_rows(TENANT_A, dt.datetime.now(dt.UTC), [])
        elapsed = time.perf_counter() - t0
        assert elapsed < 5, f"reset row step took {elapsed:.1f} s"
        # every non-current, non-loading snapshot (the pin went with its plan's case) — no rows deleted yet
        assert counts["scn_snapshot_gc"] >= 4
        reg = await _registry(TENANT_A)
        assert all(reg[snaps[k]] is not None for k in ("old0", "old1", "old2", "pinned"))
        assert reg[snaps["current"]] is None and reg[snaps["loading"]] is None
        assert await _rows(TENANT_A, snaps["old0"]) == 2 * ROWS
        # marked snapshots are gone for readers and cannot be pinned any more
        assert await scn_sync.get_snapshot(TENANT_A, snaps["old0"]) is None
        listed = {r.id for r in await scn_sync.list_snapshots(TENANT_A, CONN, 50)}
        assert listed == {snaps["current"], snaps["loading"]}
        with pytest.raises(scn_sync.SnapshotNotFound):
            await scn_sync.pin_snapshot(TENANT_A, snaps["old1"], await _plan(TENANT_A))
        assert await scn_sync.mark_noncurrent(TENANT_A) == 0  # idempotent

        # the background GC: budgeted passes, batches of 5k rows (one short transaction each)
        first = await scn_sync.collect_garbage(batch_rows=5_000, max_batches=3, tenant=TENANT_A)
        # the budget is used up after 3 statements: the first snapshot is half done, the others wait
        assert first.batches == 3 and first.pending >= 4 and not first.done
        deleted = first.rows
        passes = 1
        while True:
            progress = await scn_sync.collect_garbage(batch_rows=5_000, max_batches=3, tenant=TENANT_A)
            passes += 1
            deleted += progress.rows
            assert progress.batches <= 3
            if progress.done:
                break
            assert passes < 200
        assert passes > 5 and deleted >= 4 * 2 * ROWS
        for key in ("old0", "old1", "old2", "pinned"):
            assert await _rows(TENANT_A, snaps[key]) == 0
        reg = await _registry(TENANT_A)
        assert set(reg) == {snaps["current"], snaps["loading"]}  # registry rows deleted last
        assert await _rows(TENANT_A, snaps["current"]) == 2 * ROWS  # never touched
        assert (await scn_sync.current_snapshot(TENANT_A, CONN)).id == snaps["current"]  # type: ignore[union-attr]
        assert (await scn_sync.collect_garbage(tenant=TENANT_A)).batches == 0  # nothing left
    finally:
        await _purge(TENANT_A)


async def test_sync_gc_marks_only_unpinned_old_snapshots(platform: None, settings: Settings) -> None:
    await _purge(TENANT_A)
    try:
        old = [await _snapshot(TENANT_A, "superseded", rows=50) for _ in range(3)]
        pinned = await _snapshot(TENANT_A, "superseded", rows=50)
        await scn_sync.pin_snapshot(TENANT_A, pinned, await _plan(TENANT_A))
        keep = await _snapshot(TENANT_A, "superseded", rows=50)
        current = await _snapshot(TENANT_A, "ready", rows=50, current=True)
        t0 = time.perf_counter()
        marked = await scn_sync._gc(TENANT_A, CONN)
        assert time.perf_counter() - t0 < 5
        # newest 2 (current + keep) stay, the pinned one stays, the three old ones are marked
        assert sorted(marked) == sorted(old)
        assert await scn_sync._gc(TENANT_A, CONN) == []  # already marked: nothing new
        await scn_sync.collect_garbage(tenant=TENANT_A)
        assert set(await _registry(TENANT_A)) == {pinned, keep, current}
        assert await _rows(TENANT_A, pinned) == 100 and await _rows(TENANT_A, current) == 100
    finally:
        await _purge(TENANT_A)


async def test_gc_never_touches_a_snapshot_that_is_current_or_pinned(platform: None) -> None:
    """Defence in depth: should a marked snapshot be current or pinned after all (never by the
    marking code), the collector refuses it instead of deleting its rows."""
    await _purge(TENANT_A)
    try:
        current = await _snapshot(TENANT_A, "ready", rows=30, current=True)
        pinned = await _snapshot(TENANT_A, "superseded", rows=30)
        await scn_sync.pin_snapshot(TENANT_A, pinned, await _plan(TENANT_A))
        async with tenant_session(TENANT_A) as s:  # forged marks
            await s.execute(text("UPDATE scn_snapshot SET gc_requested_at = now() WHERE id = ANY(:ids)"),
                            {"ids": [current, pinned]})
        progress = await scn_sync.collect_garbage(tenant=TENANT_A)
        assert progress.skipped == 2 and progress.rows == 0 and progress.snapshots == 0
        assert await _rows(TENANT_A, current) == 60 and await _rows(TENANT_A, pinned) == 60
    finally:
        await _purge(TENANT_A)


async def test_pin_waits_for_a_marking_transaction_and_then_refuses(platform: None, settings: Settings) -> None:
    """A GC pass holds the snapshot row (``FOR UPDATE``) while it marks it: a concurrent pin waits
    and then sees the committed mark (``SnapshotNotFound``) — never a pin on a snapshot whose rows
    are about to be deleted."""
    await _purge(TENANT_A)
    try:
        victim = await _snapshot(TENANT_A, "superseded", rows=5)
        plan = await _plan(TENANT_A)
        dsn = settings.keelson_database_url.replace("postgresql+asyncpg://", "postgresql://")
        other = await asyncpg.connect(dsn)
        try:
            tx = other.transaction()
            await tx.start()
            await other.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_A)
            await other.execute("SELECT id FROM scn_snapshot WHERE id = $1 FOR UPDATE", uuid.UUID(victim))
            await other.execute("UPDATE scn_snapshot SET gc_requested_at = now() WHERE id = $1", uuid.UUID(victim))
            pin = asyncio.ensure_future(scn_sync.pin_snapshot(TENANT_A, victim, plan))
            waiting = 0
            for _ in range(100):
                waiting = await other.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
                if waiting:
                    break
                await asyncio.sleep(0.02)
            assert waiting, "the pin did not wait for the marking transaction"
            await tx.commit()
            with pytest.raises(scn_sync.SnapshotNotFound):
                await asyncio.wait_for(pin, 10)
        finally:
            await other.close()
        async with tenant_session(TENANT_A) as s:
            pins = (await s.execute(text("SELECT count(*) FROM scn_pin WHERE snapshot_id = :i"),
                                    {"i": victim})).scalar_one()
        assert pins == 0
    finally:
        await _purge(TENANT_A)


async def test_worker_gc_task_drains_the_backlog(platform: None) -> None:
    """The worker's background task: passes back to back while work is left, then idle; it runs
    beside (never inside) the sync loop and the run slots."""
    snaps = await _backlog()
    other = await _snapshot(TENANT_B, "superseded", rows=40)
    try:
        assert await scn_sync.mark_noncurrent(TENANT_A) >= 3  # the pin is still there: 3 old ones
        assert await scn_sync.mark_noncurrent(TENANT_B) >= 1
        worker = Worker(
            deps=SimpleNamespace(scn=CoreScnAdapter()),  # type: ignore[arg-type]
            settings=WorkerSettings(scn_gc_interval_s=0.05, scn_gc_batch_rows=4_000, scn_gc_batches_per_tick=2,
                                    scn_gc_pause_s=0.0),
            refresh_snapshot=False, sync_snapshots=False, verify_guides=False,
        )
        task = asyncio.create_task(worker._gc_loop())
        try:
            async def drained() -> None:
                while True:
                    async with system_session() as s:
                        left = (await s.execute(text(
                            "SELECT count(*) FROM scn_snapshot WHERE gc_requested_at IS NOT NULL"))).scalar_one()
                    if not left:
                        return
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(drained(), 120)
        finally:
            worker.request_stop()
            await asyncio.wait_for(task, 10)
        busy = [p for p in worker.gc_passes if p is not None and p.pending]
        assert len(busy) >= 5 and all(p.batches <= 2 for p in worker.gc_passes if p is not None)
        assert await _rows(TENANT_A, snaps["old0"]) == 0 and await _rows(TENANT_B, other) == 0
        assert await _rows(TENANT_A, snaps["pinned"]) == 2 * ROWS
        assert await _rows(TENANT_A, snaps["current"]) == 2 * ROWS
    finally:
        await _purge(TENANT_A)
        await _purge(TENANT_B)


async def test_worker_gc_backs_off_while_another_worker_holds_the_snapshot(platform: None,
                                                                          settings: Settings) -> None:
    """Review fix: a pass that only found snapshots another worker is collecting (advisory lock
    taken) reports ``pending`` without deleting anything; the loop used to treat that as "busy" and
    poll the database every ``scn_gc_pause_s``. It now waits ``scn_gc_interval_s``."""
    await _purge(TENANT_A)
    try:
        victim = await _snapshot(TENANT_A, "superseded", rows=10)
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE scn_snapshot SET gc_requested_at = now() WHERE id = :i"), {"i": victim})
        dsn = settings.keelson_database_url.replace("postgresql+asyncpg://", "postgresql://")
        other = await asyncpg.connect(dsn)
        try:
            tx = other.transaction()
            await tx.start()
            assert await other.fetchval("SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
                                        f"keelson:scn_gc|{victim}")
            worker = Worker(
                deps=SimpleNamespace(scn=CoreScnAdapter()),  # type: ignore[arg-type]
                settings=WorkerSettings(scn_gc_interval_s=0.5, scn_gc_batch_rows=100, scn_gc_batches_per_tick=2,
                                        scn_gc_pause_s=0.0),
                refresh_snapshot=False, sync_snapshots=False, verify_guides=False,
            )
            task = asyncio.create_task(worker._gc_loop())
            await asyncio.sleep(0.8)
            idle = [p for p in worker.gc_passes if p is not None and p.batches == 0 and p.pending]
            assert idle, "the locked snapshot was not reported pending"
            assert len(idle) <= 3, f"{len(idle)} passes in 0.8 s: the loop polls without progress"
            await tx.rollback()  # the other worker gave up: the next pass collects the snapshot
            for _ in range(100):
                if await _rows(TENANT_A, victim) == 0 and victim not in await _registry(TENANT_A):
                    break
                await asyncio.sleep(0.05)
            assert await _rows(TENANT_A, victim) == 0 and victim not in await _registry(TENANT_A)
            worker.request_stop()
            await asyncio.wait_for(task, 10)
        finally:
            await other.close()
    finally:
        await _purge(TENANT_A)


async def test_worker_without_a_gc_port_skips_the_task() -> None:
    worker = Worker(deps=SimpleNamespace(scn=object()), refresh_snapshot=False,  # type: ignore[arg-type]
                    sync_snapshots=False, verify_guides=False)
    await asyncio.wait_for(worker._gc_loop(), 1)  # returns at once
    assert list(worker.gc_passes) == []


@pytest.mark.skipif(not os.environ.get("SCN_PERF"), reason="full-profile timing: set SCN_PERF=1 (SCN_PERF_ROWS)")
async def test_full_profile_backlog_reset_and_gc_timing(platform: None) -> None:
    """~300k rows per snapshot (``SCN_PERF_ROWS`` per bulk table, default 150k): the reset's row
    step stays far below the 30 s target with a 3-snapshot backlog; the background GC drains it."""
    n = int(os.environ.get("SCN_PERF_ROWS", "150000"))
    await _purge(TENANT_A)
    try:
        old = [await _snapshot(TENANT_A, "superseded", rows=n) for _ in range(3)]
        current = await _snapshot(TENANT_A, "ready", rows=n, current=True)
        t0 = time.perf_counter()
        counts, _ = await demo_reset._keelson_rows(TENANT_A, dt.datetime.now(dt.UTC), [])
        reset_s = time.perf_counter() - t0
        assert counts["scn_snapshot_gc"] >= 3 and reset_s < 30
        t1 = time.perf_counter()
        passes = 0
        longest = 0.0
        while True:
            p0 = time.perf_counter()
            progress = await scn_sync.collect_garbage(max_batches=25, pause_s=0.05, tenant=TENANT_A)
            longest = max(longest, time.perf_counter() - p0)
            passes += 1
            if progress.done:
                break
        gc_s = time.perf_counter() - t1
        print(f"\nGC timing: {3 * 2 * n} backlog rows; reset row step {reset_s:.2f} s; background GC {gc_s:.1f} s "
              f"in {passes} passes (longest pass {longest:.1f} s)")
        assert all([await _rows(TENANT_A, sid) == 0 for sid in old])
        assert await _rows(TENANT_A, current) == 2 * n
    finally:
        await _purge(TENANT_A)
