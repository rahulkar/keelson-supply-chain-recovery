"""SQL backend (SQLite file and — with ``SIDECAR_TEST_PG_DSN`` — Postgres): read-only snapshot
transactions, the exactly-once apply marker and in-doubt recovery, fingerprint drift."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from adapter_sidecar.auth import Principal
from adapter_sidecar.backends import sql as sql_backend
from adapter_sidecar.db import begin_write
from adapter_sidecar.errors import ConfigError
from conftest import SQL_CONFIG, FakeKeelson, Running, fresh_pg_database, load_sql_fixture, sidecar_env, start_app
from helpers import TO_ITEM, apply, call, prepare


class Crash(BaseException):
    pass


@pytest.fixture(params=["sqlite", "postgres"])
async def sql_url(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[str]:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'erp.db'}"
    else:
        maybe = await fresh_pg_database()
        if maybe is None:
            pytest.skip("SIDECAR_TEST_PG_DSN is not set / reachable")
        url = maybe
    await load_sql_fixture(url)
    yield url


async def boot(tmp_path: Path, url: str, keelson: FakeKeelson, **extra: str):  # type: ignore[no-untyped-def]
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    settings = Settings.from_env(sidecar_env(tmp_path, None, SQL_CONFIG, ERP_DATABASE_URL=url, **extra))
    sidecar = Sidecar(settings, http_client=keelson.client())
    app, shutdown = await start_app(settings, sidecar)
    return Running(app, sidecar, None, tmp_path), shutdown


async def test_snapshot_reads_in_a_read_only_snapshot(sql_url: str, tmp_path: Path, keelson: FakeKeelson) -> None:
    run, shutdown = await boot(tmp_path, sql_url, keelson)
    try:
        backend = run.sidecar.backend
        async with backend.read() as conn:
            if backend.engine.dialect.name == "postgresql":
                iso = (await conn.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()
                ro = (await conn.execute(text("SELECT current_setting('transaction_read_only')"))).scalar_one()
                assert (iso, ro) == ("repeatable read", "on")
            else:
                await conn.execute(text("UPDATE stock SET in_transit = 999"))  # rolled back with the read transaction
        assert all(int(r["in_transit"]) != 999 for r in (await run.sidecar.rows("stock", {"site_kind": "dc"}))["items"])
        async with run.client() as c:
            m = await call(c, "scn.snapshot_open", {"entities": ["location", "po_line", "inventory_position"]})
        assert m["consistency"] == "consistent"
        assert m["entities"]["location"]["rows"] == 2 + 4 + 12 + 2
        assert m["entities"]["inventory_position"]["rows"] == 140
    finally:
        await shutdown()


async def test_marker_and_in_doubt_recovery(sql_url: str, tmp_path: Path, keelson: FakeKeelson,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    run, shutdown = await boot(tmp_path, sql_url, keelson)
    try:
        async with run.client() as c:
            done = await prepare(c, [TO_ITEM])
            assert (await apply(c, keelson, done))["status"] == "applied"
            pending = await prepare(c, [{**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "2026-10-27"}}])
        async with run.sidecar.backend.engine.connect() as conn:
            marker = (await conn.execute(text("SELECT change_set_id, purpose, preview_hash FROM keelson_sidecar_apply"))
                      ).all()
        assert [tuple(m) for m in marker] == [(done["change_set_id"], "apply", done["preview_hash"])]
        # the process dies right after the backend COMMIT, before the state rows are written
        monkeypatch.setattr(sql_backend, "run_statements", lambda *a, **k: (_ for _ in ()).throw(Crash()))
        receipt = keelson.receipt(change_set_ref=pending["change_set_id"], change_set_hash=pending["preview_hash"],
                                  approval_id="APR-2")
        with pytest.raises(Crash):
            await run.sidecar.changesets.apply(
                Principal("key:keelson", "key:keelson", frozenset({"changes:apply:auto"})), pending["change_set_id"],
                preview_hash=pending["preview_hash"], approval_ref="APR-2", approval_receipt=receipt,
                idempotency_key="in-doubt")
        monkeypatch.undo()
        cs = await run.sidecar.changesets.get(pending["change_set_id"])
        assert cs["status"] == "validated"  # the state store does not know yet
        # an apply journal row that never reached the backend (no marker) is discarded
        async with begin_write(run.sidecar.state) as conn:
            await conn.execute(text("INSERT INTO apply_journal (change_set_id, purpose, plan, created_at) VALUES "
                                    "('never', 'apply', :p, 'x')"),
                               {"p": '{"state": [["UPDATE change_set SET title = \'forged\'", {}]], '
                                     '"marker": "never"}'})
    finally:
        await shutdown()
    run2, shutdown2 = await boot(tmp_path, sql_url, keelson)
    try:
        cs = await run2.sidecar.changesets.get(pending["change_set_id"])
        assert cs["status"] == "applied" and cs["items"][0]["result_ref"] == "TR-00003"
        assert {c["title"] for c in [await run2.sidecar.changesets.get(done["change_set_id"]), cs]} == {"test"}
        async with run2.sidecar.state.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM apply_journal"))).scalar_one() == 0
            done_row = (await conn.execute(text("SELECT state FROM idempotency_record WHERE key = 'in-doubt'"))).one()
        assert done_row[0] == "done"
        async with run2.client() as c:  # the retry with the same key replays the settled outcome
            again = await apply(c, keelson, pending, approval_ref="APR-2", idem="in-doubt")
            assert again["status"] == "applied" and again["change_set_code"] == pending["code"]
            tos = await call(c, "list_transfer_orders", {"from_dc": "FH-RTM"})
            assert [t["code"] for t in tos["items"]] == ["TR-00001", "TR-00002", "TR-00003"]
    finally:
        await shutdown2()


async def test_fingerprint_drift(sql_url: str, tmp_path: Path, keelson: FakeKeelson) -> None:
    run, shutdown = await boot(tmp_path, sql_url, keelson)
    try:
        async with run.client() as c:
            snap = await call(c, "scn.snapshot_open", {"entities": ["inventory_position"]})
            v = await prepare(c, [TO_ITEM])
            await apply(c, keelson, v)
            own = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
            assert own["changes"] and "full_resync_required" not in own  # the sidecar's own apply is not drift
            async with run.sidecar.backend.engine.begin() as conn:  # another ERP client writes
                await conn.execute(text("UPDATE stock SET on_hand = on_hand + 5, rev = rev + 1 "
                                        "WHERE site = 'FH-LDS' AND sku = 'FH-FLEECE-GRN-M'"))
            drift = await call(c, "scn.changes_since", {"watermark": own["next_watermark"]})
            assert drift["full_resync_required"] is True
    finally:
        await shutdown()


async def test_missing_marker_table_is_not_ready(sql_url: str, tmp_path: Path) -> None:
    import yaml

    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    raw = yaml.safe_load(SQL_CONFIG.read_text(encoding="utf-8"))
    raw["backend"]["sql"]["manage_marker_table"] = False
    path = tmp_path / "nomarker.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    sidecar = Sidecar(Settings.from_env(sidecar_env(tmp_path, None, path, ERP_DATABASE_URL=sql_url)))
    try:
        with pytest.raises(ConfigError, match="backend unreachable"):
            await sidecar.start()
    finally:
        await sidecar.close()
