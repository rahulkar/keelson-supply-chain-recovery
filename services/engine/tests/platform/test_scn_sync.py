"""Canonical store sync (``core/scn_sync.py``, contract 80 §1) against a real local FastMCP 4
snapshot source serving pyarrow Parquet parts (``fakes/e1_scn_server.py``): happy path, pointer flip,
GC with pins, failures keep the previous snapshot (sha256, row counts, foreign host, manifest,
schema, data, FK), single-flight (in-process and cross-process), orphaned attempts, RLS isolation
of the ``scn_*`` tables, stale marks + debounce + back-off, the tier-C poll, webhook → stale, the
admin endpoints (RBAC), connector deletion purge and the egress proxy."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from e1_scn_server import ForwardProxy, ScnState, ScnTestServer, bulk_forecast_parts, small_dataset
from keelson_contracts.scn import SCN_ENTITIES
from keelson_engine.config import Settings
from keelson_engine.core import repo, scn_sync
from keelson_engine.core.db import keelson_engine, system_session, tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

SLUG = "scn-erp"
TOOLS = ("scn.snapshot_open", "scn.snapshot_export", "scn.changes_since")


# ───────────────────────────── helpers ─────────────────────────────


async def drop(tenant: str, slug: str = SLUG) -> None:
    async with tenant_session(tenant) as s:
        await scn_sync.purge_connector(s, tenant, slug)
        await s.execute(text("DELETE FROM connector WHERE slug = :s"), {"s": slug})


async def add_connector(tenant: str, url: str, slug: str = SLUG, *, status: str = "active",
                        tools: tuple[str, ...] = TOOLS, tool_status: str = "approved",
                        webhook_ref: str | None = None) -> None:
    await drop(tenant, slug)
    async with tenant_session(tenant) as s:
        await s.execute(
            text("INSERT INTO connector (tenant, slug, name, mcp_url, issuer, status, auth_kind, webhook_secret_ref) "
                 "VALUES (:t, :s, 'Test ERP', :u, '', :st, 'none', :w)"),
            {"t": tenant, "s": slug, "u": url, "st": status, "w": webhook_ref},
        )
        for name in tools:
            await s.execute(
                text("INSERT INTO connector_tool (tenant, connector, name, classification, definition_hash, status, "
                     "access, content_trust, egress, idempotency) VALUES (:t, :s, :n, '{}'::jsonb, 'h', :ts, "
                     "'read', 'trusted_data', 'external', 'enforced')"),
                {"t": tenant, "s": slug, "n": name, "ts": tool_status},
            )


async def counts(tenant: str, snapshot_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    async with tenant_session(tenant) as s:
        for e in SCN_ENTITIES:
            out[e] = (await s.execute(text(f"SELECT count(*) FROM scn_{e} WHERE snapshot_id = :i"),
                                      {"i": snapshot_id})).scalar_one()
    return out


async def all_rows(tenant: str) -> int:
    async with tenant_session(tenant) as s:
        return sum([(await s.execute(text(f"SELECT count(*) FROM scn_{e}"))).scalar_one() for e in SCN_ENTITIES])


async def make_plan(tenant: str) -> int:
    case_id = await repo.create_case("scn pin", created_by="u", tenant=tenant)
    async with tenant_session(tenant) as s:
        return int((await s.execute(
            text("INSERT INTO plan (tenant, case_id, version, options) VALUES (:t, :c, 1, '[]'::jsonb) RETURNING id"),
            {"t": tenant, "c": case_id})).scalar_one())


def expected_counts(state: ScnState) -> dict[str, int]:
    return {e: len(state.dataset.get(e, [])) for e in SCN_ENTITIES}


@pytest_asyncio.fixture(loop_scope="session")
async def source(platform: None) -> AsyncIterator[ScnTestServer]:
    server = ScnTestServer(ScnState(part_rows=4))  # forecasts (9 rows) span 3 parts
    await server.start()
    await add_connector(TENANT_A, server.url)
    yield server
    await server.stop()
    await drop(TENANT_A)


@pytest.fixture
def fast(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "scn_sync_retry_seconds", 60.0)
    return settings


# ───────────────────────────── happy path ─────────────────────────────


async def test_sync_happy_path(source: ScnTestServer, fast: Settings) -> None:
    assert await scn_sync.current_snapshot(TENANT_A, SLUG) is None
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="test")
    assert snap.status == "ready" and snap.reason == "test"
    assert snap.row_counts == expected_counts(source.state)
    assert snap.source_snapshot_id in source.state.snapshots and snap.watermark == "ox:1"
    assert snap.as_of is not None and snap.ready_at is not None and snap.bytes > 0 and snap.duration_ms is not None
    assert (await scn_sync.current_snapshot(TENANT_A, SLUG)).id == snap.id  # type: ignore[union-attr]
    assert await counts(TENANT_A, snap.id) == expected_counts(source.state)
    assert len([d for d in source.state.downloads if d.startswith("demand_forecast/")]) == 3
    async with tenant_session(TENANT_A) as s:
        po = (await s.execute(text("SELECT * FROM scn_po_line WHERE snapshot_id = :i"), {"i": snap.id})).mappings().one()
        lane = (await s.execute(text("SELECT via FROM scn_lane WHERE snapshot_id = :i"), {"i": snap.id})).scalar_one()
        loc = (await s.execute(text("SELECT lat, lon FROM scn_location WHERE snapshot_id = :i AND code = 'S001'"),
                               {"i": snap.id})).one()
        promo = (await s.execute(text("SELECT markets, period_end FROM scn_promotion WHERE snapshot_id = :i"),
                                 {"i": snap.id})).one()
    assert po["tenant"] == TENANT_A and po["sku_code"] == "ST1-M" and po["eta_dc"] == dt.date(2026, 11, 2)
    assert po["qty_ordered"] == 1200 and po["sailing_code"] == "SAIL-1"
    assert lane == ["CAPE"] and tuple(loc) == (51.5072, -0.1276)
    assert promo[0] == ["GB", "IE"] and promo[1] == dt.date(2026, 11, 29)
    listed = await scn_sync.list_snapshots(TENANT_A, SLUG)
    assert [r.id for r in listed] == [snap.id] and listed[0].to_json()["rows"] == sum(snap.row_counts.values())
    # a second sync supersedes the first atomically
    second = await scn_sync.sync_connector(TENANT_A, SLUG, reason="again")
    assert (await scn_sync.get_snapshot(TENANT_A, snap.id)).status == "superseded"  # type: ignore[union-attr]
    assert (await scn_sync.current_snapshot(TENANT_A, SLUG)).id == second.id  # type: ignore[union-attr]
    assert second.watermark == "ox:2"
    assert (TENANT_A, SLUG) in await scn_sync.list_sync_connectors()


async def test_gc_keeps_last_two_and_pinned(source: ScnTestServer, fast: Settings) -> None:
    a = await scn_sync.sync_connector(TENANT_A, SLUG, reason="a")
    plan = await make_plan(TENANT_A)
    await scn_sync.pin_snapshot(TENANT_A, a.id, plan)
    await scn_sync.pin_snapshot(TENANT_A, a.id, plan)  # idempotent
    b = await scn_sync.sync_connector(TENANT_A, SLUG, reason="b")
    c = await scn_sync.sync_connector(TENANT_A, SLUG, reason="c")
    d = await scn_sync.sync_connector(TENANT_A, SLUG, reason="d")
    ids = {r.id: r for r in await scn_sync.list_snapshots(TENANT_A, SLUG, 50)}
    assert b.id not in ids  # superseded, beyond the last 2, unpinned → marked for GC (hidden at once)
    # the sync itself only marks: the rows go with the background GC (worker), in batches
    assert sum((await counts(TENANT_A, b.id)).values()) == sum(b.row_counts.values())
    progress = await scn_sync.collect_garbage(batch_rows=3, tenant=TENANT_A)
    assert progress.done and progress.snapshots >= 1 and progress.rows >= sum(b.row_counts.values())
    assert sum((await counts(TENANT_A, b.id)).values()) == 0
    async with tenant_session(TENANT_A) as s:
        assert (await s.execute(text("SELECT count(*) FROM scn_snapshot WHERE id = :i"), {"i": b.id})).scalar_one() == 0
    assert ids[a.id].status == "superseded" and ids[a.id].pins == 1  # pinned survives
    assert sum((await counts(TENANT_A, a.id)).values()) == sum(a.row_counts.values())
    assert ids[c.id].status == "superseded" and ids[d.id].status == "ready"
    # pinning needs a ready/superseded snapshot of the tenant
    with pytest.raises(scn_sync.SnapshotNotFound):
        await scn_sync.pin_snapshot(TENANT_A, b.id, plan)
    with pytest.raises(scn_sync.SnapshotNotFound):
        await scn_sync.pin_snapshot(TENANT_A, "not-a-uuid", plan)
    assert await scn_sync.unpin_plan(TENANT_A, plan) == 1
    e = await scn_sync.sync_connector(TENANT_A, SLUG, reason="e")
    ids = {r.id for r in await scn_sync.list_snapshots(TENANT_A, SLUG, 50)}
    assert ids == {d.id, e.id}
    await scn_sync.collect_garbage(tenant=TENANT_A)  # the worker's background GC
    assert sum((await counts(TENANT_A, a.id)).values()) == 0
    # deleting the plan drops its pins (FK cascade)
    plan2 = await make_plan(TENANT_A)
    await scn_sync.pin_snapshot(TENANT_A, d.id, plan2)
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("DELETE FROM plan WHERE id = :p"), {"p": plan2})
    assert (await scn_sync.get_snapshot(TENANT_A, d.id)).pins == 0  # type: ignore[union-attr]


async def test_sync_refreshes_planner_statistics_before_the_flip(source: ScnTestServer, fast: Settings,
                                                                settings: Settings) -> None:
    """Regression (M3 integration): a new snapshot_id is absent from the old planner statistics,
    so the first ProblemData build after a sync planned nested loops and took ~58 s on the full
    profile (1 s once autovacuum analysed). The sync ANALYZEs the scn tables in its own
    transaction: the new snapshot is in the statistics the moment it becomes current."""
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="stats")
    # pg_stats hides RLS-protected tables from the (non-bypassrls) app role: read it as superuser
    from conftest import ADMIN_DSN

    db_name = settings.keelson_database_url.rsplit("/", 1)[1].split("?", 1)[0]
    admin = await asyncpg.connect(ADMIN_DSN.rsplit("/", 1)[0] + "/" + db_name)
    try:
        for table in ("scn_demand_forecast", "scn_inventory_position", "scn_location"):
            mcv = await admin.fetchval(
                "SELECT most_common_vals::text FROM pg_stats WHERE tablename = $1 AND attname = 'snapshot_id'", table
            )
            assert mcv is not None and snap.id in mcv, (table, mcv)
            assert await admin.fetchval("SELECT reltuples FROM pg_class WHERE relname = $1", table) > 0
    finally:
        await admin.close()


# ───────────────────────────── failures keep the previous snapshot ─────────────────────────────


async def _good_then_fail(source: ScnTestServer, code: str) -> scn_sync.SyncFailed:
    """A clean sync, then one with the knobs the caller armed → ``code``; checks nothing changed."""
    st = source.state
    armed = (set(st.corrupt), dict(st.lie_rows), st.foreign_host, set(st.omit), st.mutate, st.tool_error)
    st.corrupt, st.lie_rows, st.foreign_host, st.omit, st.mutate, st.tool_error = set(), {}, None, set(), None, None
    good = await scn_sync.sync_connector(TENANT_A, SLUG, reason="good")
    st.corrupt, st.lie_rows, st.foreign_host, st.omit, st.mutate, st.tool_error = armed
    before = await all_rows(TENANT_A)
    with pytest.raises(scn_sync.SyncFailed) as err:
        await scn_sync.sync_connector(TENANT_A, SLUG, reason="bad")
    assert err.value.code == code, err.value.snapshot.error
    failed = err.value.snapshot
    assert failed.status == "failed" and failed.error and failed.error.startswith(f"{code}: ")
    assert "sig=" not in failed.error and "exp=" not in failed.error
    assert (await scn_sync.current_snapshot(TENANT_A, SLUG)).id == good.id  # type: ignore[union-attr]
    assert (await scn_sync.get_snapshot(TENANT_A, good.id)).status == "ready"  # type: ignore[union-attr]
    assert sum((await counts(TENANT_A, failed.id)).values()) == 0
    assert await all_rows(TENANT_A) == before
    return err.value


async def test_sha256_mismatch_fails_and_keeps_previous(source: ScnTestServer, fast: Settings) -> None:
    source.state.corrupt.add("demand_forecast")
    err = await _good_then_fail(source, "sha256_mismatch")
    assert "demand_forecast part 1" in (err.snapshot.error or "")
    state = await scn_sync.sync_state(TENANT_A, SLUG)
    assert state is not None and state["attempts"] == 1 and state["last_error"].startswith("sha256_mismatch")
    due = dt.datetime.fromisoformat(state["due_at"])
    assert due > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=50)  # back-off, not immediately due
    # a second failure doubles the back-off; recovery clears it
    with pytest.raises(scn_sync.SyncFailed):
        await scn_sync.sync_connector(TENANT_A, SLUG, reason="bad2")
    state = await scn_sync.sync_state(TENANT_A, SLUG)
    assert state["attempts"] == 2  # type: ignore[index]
    assert dt.datetime.fromisoformat(state["retry_at"]) > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=110)  # type: ignore[index]
    source.state.corrupt.clear()
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="fixed")
    state = await scn_sync.sync_state(TENANT_A, SLUG)
    assert state["attempts"] == 0 and state["due_at"] is None and state["last_error"] is None  # type: ignore[index]


async def test_row_count_mismatch(source: ScnTestServer, fast: Settings) -> None:
    source.state.lie_rows["po_line"] = 1  # manifest + export agree on a count the Parquet does not have
    err = await _good_then_fail(source, "row_count_mismatch")
    assert "po_line" in (err.snapshot.error or "")


async def test_foreign_host_refused(source: ScnTestServer, fast: Settings) -> None:
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="good")
    downloads = len(source.state.downloads)
    for foreign in ("http://evil.localhost", f"http://127.0.0.1:{source.port + 1}", f"https://127.0.0.1:{source.port}",
                    f"http://localhost:{source.port}"):
        source.state.foreign_host = foreign
        with pytest.raises(scn_sync.SyncFailed) as err:
            await scn_sync.sync_connector(TENANT_A, SLUG, reason="foreign")
        assert err.value.code == "foreign_host", foreign
    assert len(source.state.downloads) == downloads  # nothing was fetched


async def test_manifest_schema_data_and_fk_failures(source: ScnTestServer, fast: Settings) -> None:
    source.state.omit.add("promotion")
    await _good_then_fail(source, "manifest_incomplete")
    source.state.omit.clear()

    def unknown_sku(data: dict[str, list[dict[str, Any]]]) -> None:
        data["po_line"][0]["sku_code"] = "NOPE-XL"

    source.state.mutate = unknown_sku
    err = await _good_then_fail(source, "fk_violation")
    assert "NOPE-XL" in (err.snapshot.error or "")

    def unknown_store(data: dict[str, list[dict[str, Any]]]) -> None:
        data["inventory_position"][1]["location_code"] = "S999"

    source.state.mutate = unknown_store
    err = await _good_then_fail(source, "fk_violation")
    assert "store:S999" in (err.snapshot.error or "")

    def negative_qty(data: dict[str, list[dict[str, Any]]]) -> None:  # fails mid-COPY (row 7 of 9)
        data["demand_forecast"][6]["units"] = -5

    source.state.mutate = negative_qty
    err = await _good_then_fail(source, "data_invalid")
    assert "demand_forecast" in (err.snapshot.error or "")

    def duplicate(data: dict[str, list[dict[str, Any]]]) -> None:
        data["item"].append(dict(data["item"][1]))

    source.state.mutate = duplicate
    err = await _good_then_fail(source, "data_invalid")
    assert "duplicate source_id" in (err.snapshot.error or "")
    source.state.mutate = None
    source.state.tool_error = "snapshot_storage_unavailable: disk full"
    err = await _good_then_fail(source, "tool_error")
    source.state.tool_error = None


async def test_unavailable_connectors(source: ScnTestServer, fast: Settings) -> None:
    with pytest.raises(scn_sync.SyncUnavailable) as err:
        await scn_sync.sync_connector(TENANT_A, "nope", reason="x")
    assert err.value.code == "not_found"
    await add_connector(TENANT_A, source.url, "scn-off", status="disabled")
    await add_connector(TENANT_A, source.url, "scn-review", tool_status="needs_review")
    try:
        with pytest.raises(scn_sync.SyncUnavailable) as err:
            await scn_sync.sync_connector(TENANT_A, "scn-off", reason="x")
        assert err.value.code == "connector_inactive"
        with pytest.raises(scn_sync.SyncUnavailable) as err:
            await scn_sync.sync_connector(TENANT_A, "scn-review", reason="x")
        assert err.value.code == "sync_unsupported"
        assert await scn_sync.list_snapshots(TENANT_A, "scn-review") == []
    finally:
        await drop(TENANT_A, "scn-off")
        await drop(TENANT_A, "scn-review")


# ───────────────────────────── concurrency ─────────────────────────────


async def test_single_flight_in_process(source: ScnTestServer, fast: Settings) -> None:
    source.state.open_delay = 0.4
    a, b, c = await asyncio.gather(*(scn_sync.sync_connector(TENANT_A, SLUG, reason=f"r{i}") for i in range(3)))
    assert a.id == b.id == c.id
    assert source.state.open_calls == 1
    assert len(await scn_sync.list_snapshots(TENANT_A, SLUG)) == 1


async def test_single_flight_across_processes(source: ScnTestServer, fast: Settings, settings: Settings) -> None:
    """Another process holding the advisory lock → SyncInProgress, nothing recorded."""
    dsn = settings.keelson_database_url.replace("postgresql+asyncpg://", "postgresql://")
    other = await asyncpg.connect(dsn)
    key = f"keelson:scn_sync|{TENANT_A}|{SLUG}"
    try:
        assert await other.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key)
        with pytest.raises(scn_sync.SyncInProgress):
            await scn_sync.sync_connector(TENANT_A, SLUG, reason="blocked")
        assert source.state.open_calls == 0
        await other.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
        assert (await scn_sync.sync_connector(TENANT_A, SLUG, reason="free")).status == "ready"
    finally:
        await other.close()


async def test_orphaned_loading_attempt_is_failed(source: ScnTestServer, fast: Settings) -> None:
    async with tenant_session(TENANT_A) as s:
        orphan = (await s.execute(text("INSERT INTO scn_snapshot (tenant, connector, status) "
                                       "VALUES (:t, :c, 'loading') RETURNING id"),
                                  {"t": TENANT_A, "c": SLUG})).scalar_one()
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="after crash")
    row = await scn_sync.get_snapshot(TENANT_A, str(orphan))
    assert row is not None and row.status == "failed" and (row.error or "").startswith("interrupted")


async def test_cancelled_sync_is_recorded(source: ScnTestServer, fast: Settings) -> None:
    good = await scn_sync.sync_connector(TENANT_A, SLUG, reason="good")
    source.state.open_delay = 5
    task = asyncio.ensure_future(scn_sync.sync_connector(TENANT_A, SLUG, reason="slow"))
    for _ in range(100):
        if source.state.open_calls == 2:
            break
        await asyncio.sleep(0.02)
    inner = scn_sync._inflight[(TENANT_A, SLUG)]
    inner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        rows = await scn_sync.list_snapshots(TENANT_A, SLUG)
        if rows[0].status == "failed":
            break
        await asyncio.sleep(0.02)
    assert rows[0].status == "failed" and rows[0].error == "cancelled: the sync was cancelled"
    assert (await scn_sync.current_snapshot(TENANT_A, SLUG)).id == good.id  # type: ignore[union-attr]


async def test_timeout_fails(source: ScnTestServer, fast: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fast, "scn_sync_timeout_seconds", 0.3)
    source.state.open_delay = 2
    with pytest.raises(scn_sync.SyncFailed) as err:
        await scn_sync.sync_connector(TENANT_A, SLUG, reason="slow")
    assert err.value.code == "timeout"


# ───────────────────────────── RLS ─────────────────────────────


async def test_scn_tables_are_tenant_isolated(source: ScnTestServer, fast: Settings) -> None:
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="rls")
    async with tenant_session(TENANT_B) as s:
        for table in ("scn_snapshot", "scn_current", "scn_pin", "scn_sync_state", *(f"scn_{e}" for e in SCN_ENTITIES)):
            assert (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table
        res = await s.execute(text("UPDATE scn_po_line SET qty_ordered = 0 RETURNING source_id"))
        assert res.first() is None
    assert await scn_sync.current_snapshot(TENANT_B, SLUG) is None
    assert await scn_sync.get_snapshot(TENANT_B, snap.id) is None
    assert await scn_sync.list_snapshots(TENANT_B, SLUG) == []
    plan_b = await make_plan(TENANT_B)
    with pytest.raises(scn_sync.SnapshotNotFound):
        await scn_sync.pin_snapshot(TENANT_B, snap.id, plan_b)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(TENANT_B) as s:
            await s.execute(text("INSERT INTO scn_item (tenant, snapshot_id, source_id, kind, code, src_version) "
                                 "VALUES (:t, :i, 'x', 'sku', 'X', 0)"), {"t": TENANT_A, "i": snap.id})
    with pytest.raises(DBAPIError):  # FK (tenant, snapshot_id): B cannot point at A's snapshot
        async with tenant_session(TENANT_B) as s:
            await s.execute(text("INSERT INTO scn_current (tenant, connector, snapshot_id) VALUES (:t, :c, :i)"),
                            {"t": TENANT_B, "c": SLUG, "i": snap.id})
    async with keelson_engine().connect() as conn:  # no tenant context: nothing
        assert (await conn.execute(text("SELECT count(*) FROM scn_po_line"))).scalar_one() == 0
    async with system_session() as s:
        assert (await s.execute(text("SELECT count(*) FROM scn_po_line WHERE snapshot_id = :i"),
                                {"i": snap.id})).scalar_one() == 1


# ───────────────────────────── triggers ─────────────────────────────


async def _due() -> list[tuple[str, str]]:
    return [(d.tenant, d.connector) for d in await scn_sync.due_syncs(100)]


async def test_mark_stale_debounce_and_due(source: ScnTestServer, fast: Settings,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="base")
    await scn_sync.mark_stale(TENANT_A, SLUG, "webhook:po_line.eta_changed")
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert st is not None and st["reason"] == "webhook:po_line.eta_changed" and st["stale_since"]
    due_at = dt.datetime.fromisoformat(st["due_at"])
    assert dt.timedelta(seconds=25) < due_at - dt.datetime.now(dt.UTC) <= dt.timedelta(seconds=31)
    assert (TENANT_A, SLUG) not in await _due()
    # max delay: a steady event stream cannot postpone the sync beyond stale_since + max
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE scn_sync_state SET stale_since = now() - interval '299 seconds' "
                             "WHERE connector = :c"), {"c": SLUG})
    await scn_sync.mark_stale(TENANT_A, SLUG, "webhook:inventory.adjusted")
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert dt.datetime.fromisoformat(st["due_at"]) - dt.datetime.now(dt.UTC) < dt.timedelta(seconds=3)  # type: ignore[index]
    await asyncio.sleep(1.1)
    due = await scn_sync.due_syncs(100)
    assert any(d.tenant == TENANT_A and d.connector == SLUG and d.reason == "webhook:inventory.adjusted" for d in due)
    # an event that arrives while the sync runs keeps it due; otherwise the sync clears the mark
    source.state.open_delay = 0.5
    task = asyncio.ensure_future(scn_sync.sync_connector(TENANT_A, SLUG, reason="stale"))
    await asyncio.sleep(0.25)
    await scn_sync.mark_stale(TENANT_A, SLUG, "webhook:sailing.updated")
    await task
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert st["stale_since"] is not None and st["due_at"] is not None  # type: ignore[index]
    source.state.open_delay = 0
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="stale")
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert st["stale_since"] is None and st["due_at"] is None  # type: ignore[index]
    # request_sync: due now (no debounce), and it overrides a failure back-off
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE scn_sync_state SET retry_at = now() + interval '1 hour' WHERE connector = :c"),
                        {"c": SLUG})
    await scn_sync.mark_stale(TENANT_A, SLUG, "webhook:change_set.applied")
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert dt.datetime.fromisoformat(st["due_at"]) > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=50)  # type: ignore[index]
    await scn_sync.request_sync(TENANT_A, SLUG, "admin:sam")
    assert (TENANT_A, SLUG) in await _due()
    # due_syncs only lists sync-capable connectors
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'disabled' WHERE slug = :c"), {"c": SLUG})
    assert (TENANT_A, SLUG) not in await _due()
    assert (TENANT_A, SLUG) not in await scn_sync.list_sync_connectors()
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'active' WHERE slug = :c"), {"c": SLUG})


async def test_poll_changes(source: ScnTestServer, fast: Settings) -> None:
    assert await scn_sync.poll_changes(TENANT_A, SLUG) is True  # no snapshot yet → stale
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["reason"] == "poll:no_snapshot"  # type: ignore[index]
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="base")
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["stale_since"] is None  # type: ignore[index]
    assert await scn_sync.poll_changes(TENANT_A, SLUG) is False
    assert source.state.changes_calls[-1] == snap.watermark
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["last_poll_at"] is not None  # type: ignore[index]
    source.state.changes = {"changes": [{"entity": "po_line", "op": "upsert", "key": "PO-1#1", "version": 5}],
                            "next_watermark": "ox:9"}
    assert await scn_sync.poll_changes(TENANT_A, SLUG) is True
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["reason"] == "poll:1_changes"  # type: ignore[index]
    source.state.changes = {"changes": [], "next_watermark": "ox:9", "full_resync_required": True}
    assert await scn_sync.poll_changes(TENANT_A, SLUG) is True
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["reason"] == "poll:full_resync"  # type: ignore[index]
    source.state.changes = {"changes": "nope"}
    with pytest.raises(scn_sync.ScnSyncError) as err:
        await scn_sync.poll_changes(TENANT_A, SLUG)
    assert err.value.code == "invalid_changes"
    await add_connector(TENANT_A, source.url, "scn-nochanges", tools=TOOLS[:2])
    try:
        with pytest.raises(scn_sync.SyncUnavailable):
            await scn_sync.poll_changes(TENANT_A, "scn-nochanges")
    finally:
        await drop(TENANT_A, "scn-nochanges")


async def test_webhook_events_mark_stale(source: ScnTestServer, api: httpx.AsyncClient,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import json
    import uuid

    from keelson_engine.core.webhooks import sign

    secret = "whsec_" + base64.b64encode(b"scn-webhook-secret-0123456789abc").decode()
    monkeypatch.setenv("SCNERP_WEBHOOK_SECRET", secret)
    await add_connector(TENANT_A, source.url, webhook_ref="SCNERP_WEBHOOK_SECRET")

    async def deliver(event_type: str, eid: str | None = None) -> None:
        eid = eid or f"msg_{uuid.uuid4().hex}"
        stamp = str(int(time.time()))
        raw = json.dumps({"type": event_type, "id": eid, "timestamp": stamp, "data": {}}).encode()
        r = await api.post(f"/api/webhooks/{TENANT_A}/{SLUG}", content=raw, headers={
            "webhook-id": eid, "webhook-timestamp": stamp, "webhook-signature": sign(secret, eid, stamp, raw),
            "content-type": "application/json"})
        assert r.status_code == 204, r.text

    await deliver("supplier_message.received")
    assert await scn_sync.sync_state(TENANT_A, SLUG) is None  # not a data-changing event
    for event_type in ("po_line.eta_changed", "inventory.adjusted", "sailing.updated", "change_set.applied",
                       "change_set.rolled_back"):
        await deliver(event_type, f"evt-{event_type}")
        st = await scn_sync.sync_state(TENANT_A, SLUG)
        assert st is not None and st["reason"] == f"webhook:{event_type}" and st["due_at"] is not None
    # a duplicate delivery does not touch the state again
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE scn_sync_state SET reason = 'marker' WHERE connector = :c"), {"c": SLUG})
    await deliver("po_line.eta_changed", "evt-po_line.eta_changed")
    assert (await scn_sync.sync_state(TENANT_A, SLUG))["reason"] == "marker"  # type: ignore[index]
    # a failing stale mark never fails the delivery
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(scn_sync, "mark_stale", boom)
    await deliver("inventory.adjusted")


# ───────────────────────────── admin API ─────────────────────────────


async def test_admin_sync_endpoints(source: ScnTestServer, browser_factory: Any, session_browser: Any,
                                    fast: Settings) -> None:
    sam: Browser = await browser_factory("sam")
    priya: Browser = await browser_factory("priya")
    ava: Browser = await browser_factory("ava")
    sync_url, snaps_url = f"/api/admin/connectors/{SLUG}/sync", f"/api/admin/connectors/{SLUG}/snapshots"
    for who in (priya,):
        assert (await who.post(sync_url)).status_code == 403
        assert (await who.get(snaps_url)).status_code == 403
    no_csrf = await sam.client.post(sync_url, headers={"Cookie": f"keelson_session={sam.cookie}", "Origin":
                                                       "http://keelson.localhost"})
    assert no_csrf.status_code == 403
    r = await sam.get(snaps_url)
    assert r.status_code == 200, r.text
    assert r.json() == {"connector": SLUG, "current": None, "state": None, "items": []}
    r = await sam.post(sync_url)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["state"]["reason"] == "admin:sam" and body["current"] is None
    due = [d for d in await scn_sync.due_syncs(100) if (d.tenant, d.connector) == (TENANT_A, SLUG)]
    assert due and due[0].reason == "admin:sam"
    # what the worker does with it
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason=due[0].reason or "stale")
    r = await sam.get(snaps_url, params={"limit": 5})
    body = r.json()
    assert body["current"]["id"] == snap.id and body["current"]["status"] == "ready"
    assert body["items"][0]["row_counts"] == expected_counts(source.state)
    assert body["items"][0]["reason"] == "admin:sam" and body["state"]["due_at"] is None
    async with system_session() as s:
        audit = (await s.execute(text("SELECT action, target FROM admin_audit WHERE tenant = :t AND action = "
                                      "'connector.sync' ORDER BY id DESC LIMIT 1"), {"t": TENANT_A})).one()
    assert tuple(audit) == ("connector.sync", f"connector:{SLUG}")
    # 404 unknown / foreign connector, 409 not syncable
    assert (await sam.post("/api/admin/connectors/nope/sync")).status_code == 404
    assert (await sam.get("/api/admin/connectors/nope/snapshots")).status_code == 404
    other: Browser = await session_browser("olaf", ["tenant-admin"], [TENANT_B])
    assert (await other.post(sync_url)).status_code == 404
    assert (await other.get(snaps_url)).status_code == 404
    await add_connector(TENANT_A, source.url, "scn-review", tool_status="needs_review")
    await add_connector(TENANT_A, source.url, "scn-draft", status="draft")
    try:
        r = await sam.post("/api/admin/connectors/scn-review/sync")
        assert r.status_code == 409 and r.json()["code"] == "sync_unsupported"
        r = await sam.post("/api/admin/connectors/scn-draft/sync")
        assert r.status_code == 409 and r.json()["code"] == "connector_inactive"
    finally:
        await drop(TENANT_A, "scn-review")
        await drop(TENANT_A, "scn-draft")
    # a platform admin may act on another tenant explicitly
    r = await ava.get(snaps_url, params={"tenant": TENANT_A})
    assert r.status_code == 200 and r.json()["current"]["id"] == snap.id


async def test_connector_delete_purges_snapshots(source: ScnTestServer, browser_factory: Any,
                                                 fast: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_engine.core import agent_specs

    async def unbound(_tenant: str) -> set[str]:  # the bound-connector rule is tested in test_admin_connectors
        return set()

    monkeypatch.setattr(agent_specs, "bound_connectors", unbound)
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="before delete")
    await scn_sync.mark_stale(TENANT_A, SLUG, "webhook:po_line.eta_changed")
    plan = await make_plan(TENANT_A)
    await scn_sync.pin_snapshot(TENANT_A, snap.id, plan)
    sam: Browser = await browser_factory("sam")
    r = await sam.delete(f"/api/admin/connectors/{SLUG}")
    # M7 four-eyes: a tenant-admin's delete is a change request; a platform admin co-signs it
    assert r.status_code == 202 and r.json()["kind"] == "connector_delete", r.text
    ava: Browser = await browser_factory("ava")
    ok = await ava.post(f"/api/admin/change-requests/{r.json()['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    async with tenant_session(TENANT_A) as s:
        for table in ("scn_snapshot", "scn_current", "scn_pin", "scn_sync_state"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table} WHERE "
                                      + ("connector = :c" if table not in ("scn_pin",) else "snapshot_id = :i")),
                                 {"c": SLUG, "i": snap.id})).scalar_one()
            assert n == 0, table
    assert sum((await counts(TENANT_A, snap.id)).values()) == 0


# ───────────────────────────── egress proxy ─────────────────────────────


async def test_mcp_and_downloads_go_through_the_egress_proxy(source: ScnTestServer, fast: Settings,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = ForwardProxy()
    await proxy.start()
    monkeypatch.setattr(fast, "connector_proxy_url", proxy.url)
    try:
        snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="proxied")
    finally:
        await proxy.stop()
    assert snap.status == "ready"
    seen = proxy.requests
    assert any(r.startswith(f"POST {source.origin}/mcp") for r in seen), seen
    parts = [r for r in seen if r.startswith(f"GET {source.origin}/v1/snapshots/")]
    assert len(parts) == len(source.state.downloads), (seen, source.state.downloads)
    assert parts and len(parts) >= len(SCN_ENTITIES)


# ───────────────────────────── S5 preview (opt-in) ─────────────────────────────


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


@pytest.mark.skipif(not os.environ.get("SCN_PERF"), reason="S5 preview: set SCN_PERF=1 (and SCN_PERF_ROWS)")
async def test_s5_full_profile_sized_sync(platform: None, fast: Settings) -> None:
    import gc
    import io
    import resource

    import pyarrow as pa
    import pyarrow.parquet as pq

    from e1_scn_server import schema

    n = int(os.environ.get("SCN_PERF_ROWS", "200000"))
    alloc_n = int(os.environ.get("SCN_PERF_ALLOCATIONS", "92601"))
    inv_n = int(os.environ.get("SCN_PERF_INVENTORY", "81900"))
    state = ScnState(dataset=small_dataset(stores=450))
    state.bulk["demand_forecast"] = bulk_forecast_parts(n, 50_000)

    def bulk(entity: str, total: int, make: Any) -> list[tuple[bytes, int]]:
        sch = schema(entity)
        out = []
        for start in range(0, total, 50_000):
            idx = range(start, min(total, start + 50_000))
            cols = make(idx)
            buf = io.BytesIO()
            pq.write_table(pa.Table.from_arrays([pa.array(cols[c], type=f.type) for c, f in zip(sch.names, sch,
                                                                                                strict=True)],
                                                schema=sch), buf, compression="zstd")
            out.append((buf.getvalue(), len(idx)))
        return out

    state.bulk["allocation"] = bulk("allocation", alloc_n, lambda idx: {
        "source_id": [f"alloc:{i}" for i in idx], "code": [f"AL-{i}" for i in idx],
        "sku_code": ["ST1-M"] * len(idx), "dc_code": ["UK1"] * len(idx),
        "store_code": [f"S{i % 450 + 1:03d}" for i in idx], "ship_week": ["2026-W45"] * len(idx),
        "qty": [i % 50 for i in idx], "status": ["planned"] * len(idx), "src_version": [1] * len(idx)})
    # store inventory: the FK sanity check (every inventory location exists) runs over all of it
    state.bulk["inventory_position"] = bulk("inventory_position", inv_n, lambda idx: {
        "source_id": [f"inv:{i}" for i in idx], "location_kind": ["store"] * len(idx),
        "location_code": [f"S{i % 450 + 1:03d}" for i in idx], "sku_code": [f"ST1-{i}" for i in idx],
        "on_hand": [i % 40 for i in idx], "allocated": [0] * len(idx), "reserved": [0] * len(idx),
        "in_transit": [i % 3 for i in idx], "src_version": [1] * len(idx)})
    server = ScnTestServer(state)
    await server.start()
    await add_connector(TENANT_A, server.url, "scn-perf")
    try:
        gc.collect()
        base = _rss_mb()
        peak = base
        stop = asyncio.Event()

        async def sample() -> None:
            nonlocal peak
            while not stop.is_set():
                peak = max(peak, _rss_mb())
                await asyncio.sleep(0.05)

        sampler = asyncio.ensure_future(sample())
        t0 = time.perf_counter()
        snap = await scn_sync.sync_connector(TENANT_A, "scn-perf", reason="s5")
        elapsed = time.perf_counter() - t0
        stop.set()
        await sampler
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        rows = sum(snap.row_counts.values())
        print(f"\nS5 preview: {rows} rows ({n} forecasts, {alloc_n} allocations, {inv_n} inventory),{snap.bytes / 1e6:.1f} MB parquet, "
              f"sync {elapsed:.2f} s (server-side duration {snap.duration_ms} ms); RSS base {base:.0f} MB, "
              f"peak {peak:.0f} MB (+{peak - base:.0f} MB), process ru_maxrss {maxrss:.0f} MB")
        assert snap.row_counts["demand_forecast"] == n and snap.row_counts["allocation"] == alloc_n
        # second sync: GC + supersede path at volume
        t1 = time.perf_counter()
        await scn_sync.sync_connector(TENANT_A, "scn-perf", reason="s5-again")
        t2 = time.perf_counter()
        await scn_sync.sync_connector(TENANT_A, "scn-perf", reason="s5-gc")
        print(f"S5 preview: re-sync {t2 - t1:.2f} s; re-sync incl. GC of one snapshot {time.perf_counter() - t2:.2f} s")
    finally:
        await drop(TENANT_A, "scn-perf")
        await server.stop()


# ───────────────────────────── review regressions ─────────────────────────────


async def test_gc_never_drops_a_pin_committed_while_it_waits(source: ScnTestServer, fast: Settings,
                                                             settings: Settings,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin whose transaction holds the snapshot row (FK key-share lock) when GC selects its
    victims: GC waits for it and must then see the pin, not delete the snapshot (the pin would
    vanish silently through ``ON DELETE CASCADE``)."""
    real_gc = scn_sync._gc

    async def no_gc(*_a: Any) -> list[str]:
        return []

    monkeypatch.setattr(scn_sync, "_gc", no_gc)
    a = await scn_sync.sync_connector(TENANT_A, SLUG, reason="a")
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="b")
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="c")  # a is now a GC victim (beyond the last 2)
    plan = await make_plan(TENANT_A)
    dsn = settings.keelson_database_url.replace("postgresql+asyncpg://", "postgresql://")
    other = await asyncpg.connect(dsn)
    try:
        tx = other.transaction()
        await tx.start()
        await other.execute("SELECT set_config('app.tenant_id', $1, true)", TENANT_A)
        await other.execute("INSERT INTO scn_pin (tenant, snapshot_id, plan_id) VALUES ($1, $2, $3)",
                            TENANT_A, a.id, plan)
        gc_task = asyncio.ensure_future(real_gc(TENANT_A, SLUG))
        for _ in range(100):  # until GC waits on the pin's row lock
            waiting = await other.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
            if waiting:
                break
            await asyncio.sleep(0.02)
        assert waiting, "GC did not wait for the pinning transaction"
        await tx.commit()
        deleted = await asyncio.wait_for(gc_task, 10)
    finally:
        await other.close()
    assert a.id not in deleted
    row = await scn_sync.get_snapshot(TENANT_A, a.id)
    assert row is not None and row.status == "superseded" and row.pins == 1
    assert sum((await counts(TENANT_A, a.id)).values()) == sum(a.row_counts.values())


async def test_pin_unknown_or_foreign_plan(source: ScnTestServer, fast: Settings) -> None:
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="pin")
    with pytest.raises(scn_sync.PlanNotFound):
        await scn_sync.pin_snapshot(TENANT_A, snap.id, 987_654_321)
    plan_b = await make_plan(TENANT_B)
    with pytest.raises(scn_sync.PlanNotFound):  # another tenant's plan id
        await scn_sync.pin_snapshot(TENANT_A, snap.id, plan_b)
    assert (await scn_sync.get_snapshot(TENANT_A, snap.id)).pins == 0  # type: ignore[union-attr]


async def test_failed_unlock_never_leaks_the_sync_lock(source: ScnTestServer, fast: Settings, settings: Settings,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """If releasing the advisory lock fails, the pooled connection is dropped (the lock dies with the
    backend) and the good sync is still reported as ready."""
    monkeypatch.setattr(scn_sync, "_UNLOCK_SQL", "SELECT pg_advisory_unlock_no_such_function($1)")
    snap = await scn_sync.sync_connector(TENANT_A, SLUG, reason="unlock fails")
    assert snap.status == "ready"
    dsn = settings.keelson_database_url.replace("postgresql+asyncpg://", "postgresql://")
    other = await asyncpg.connect(dsn)
    key = f"keelson:scn_sync|{TENANT_A}|{SLUG}"
    try:
        got = False
        for _ in range(50):  # the backend exit is asynchronous
            got = await other.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key)
            if got:
                break
            await asyncio.sleep(0.05)
        assert got, "the sync lock leaked on a pooled connection"
        await other.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
    finally:
        await other.close()
    monkeypatch.undo()
    assert (await scn_sync.sync_connector(TENANT_A, SLUG, reason="next")).status == "ready"


async def test_connector_purged_during_sync(source: ScnTestServer, fast: Settings) -> None:
    """Connector deleted while its sync runs: the attempt fails cleanly (``interrupted``), nothing is
    left behind (no rows, no snapshot, no orphan trigger state)."""
    source.state.open_delay = 0.5
    task = asyncio.ensure_future(scn_sync.sync_connector(TENANT_A, SLUG, reason="doomed"))
    for _ in range(100):
        if source.state.open_calls:
            break
        await asyncio.sleep(0.02)
    await drop(TENANT_A)
    with pytest.raises(scn_sync.SyncFailed) as err:
        await task
    assert err.value.code == "interrupted" and err.value.snapshot.status == "failed"
    assert await scn_sync.sync_state(TENANT_A, SLUG) is None
    assert await scn_sync.list_snapshots(TENANT_A, SLUG) == []
    assert await all_rows(TENANT_A) == 0


async def test_admin_request_during_a_sync_is_kept(source: ScnTestServer, fast: Settings) -> None:
    """"Sync now" while a sync is already running (it read the source before the request): the
    request stays due instead of being cleared by the finishing sync."""
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="base")
    source.state.open_delay = 0.5
    task = asyncio.ensure_future(scn_sync.sync_connector(TENANT_A, SLUG, reason="running"))
    for _ in range(100):
        if source.state.open_calls == 2:
            break
        await asyncio.sleep(0.02)
    await scn_sync.request_sync(TENANT_A, SLUG, "admin:sam")
    await task
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert st is not None and st["due_at"] is not None and st["stale_since"] is not None
    assert dt.datetime.fromisoformat(st["due_at"]) <= dt.datetime.now(dt.UTC)
    assert any((d.tenant, d.connector) == (TENANT_A, SLUG) for d in await scn_sync.due_syncs(100))
    source.state.open_delay = 0
    await scn_sync.sync_connector(TENANT_A, SLUG, reason="admin:sam")
    st = await scn_sync.sync_state(TENANT_A, SLUG)
    assert st["due_at"] is None and st["stale_since"] is None  # type: ignore[index]


async def test_text_postgres_rejects_is_data_invalid(source: ScnTestServer, fast: Settings) -> None:
    """A NUL character passes the row model but Postgres refuses it inside COPY: a data error of
    the source (``data_invalid``), not an internal crash."""

    def nul(data: dict[str, list[dict[str, Any]]]) -> None:
        data["location"][0]["name"] = "Lon\x00don"

    source.state.mutate = nul
    err = await _good_then_fail(source, "data_invalid")
    assert "location" in (err.snapshot.error or "")
    source.state.mutate = None
