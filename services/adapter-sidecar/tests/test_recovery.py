"""Crash recovery of CSV applies (``apply_journal`` roll-forward at start-up)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeKeelson, sidecar_env, start_app
from helpers import TO_ITEM, apply, call, csv_rows, prepare, stock
from sqlalchemy import text

from adapter_sidecar.auth import Principal
from adapter_sidecar.backends import csvdir
from adapter_sidecar.db import begin_write


class Crash(BaseException):
    """The process dies (not an Exception: nothing in the sidecar catches it)."""


async def _boot(tmp_path: Path, data_dir: Path, keelson: FakeKeelson):  # type: ignore[no-untyped-def]
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    settings = Settings.from_env(sidecar_env(tmp_path, data_dir))
    sidecar = Sidecar(settings, http_client=keelson.client())
    return sidecar, *(await start_app(settings, sidecar))


async def test_crash_between_file_replacements_rolls_forward(tmp_path: Path, data_dir: Path, keelson: FakeKeelson,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import Running

    sidecar, app, shutdown = await _boot(tmp_path, data_dir, keelson)
    run = Running(app, sidecar, data_dir, tmp_path)
    real_replace = csvdir.os.replace
    calls = {"n": 0}

    def replace(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash
        real_replace(src, dst)

    try:
        async with run.client() as c:
            validated = await prepare(c, [TO_ITEM])
            monkeypatch.setattr(csvdir.os, "replace", replace)
            with pytest.raises(Crash):
                await sidecar.changesets.apply(
                    Principal("key:keelson", "key:keelson", frozenset({"changes:apply:auto"})),
                    validated["change_set_id"], preview_hash=validated["preview_hash"], approval_ref="APR-1",
                    approval_receipt=keelson.receipt(change_set_ref=validated["change_set_id"],
                                                     change_set_hash=validated["preview_hash"], approval_id="APR-1"),
                    idempotency_key="crash-1")
            monkeypatch.setattr(csvdir.os, "replace", real_replace)
    finally:
        await shutdown()
    staged = list(data_dir.glob(".*.sidecar-tmp"))
    assert len(staged) == 2  # 3 files staged, one published before the crash
    # restart: the committed apply is rolled forward
    sidecar2, app2, shutdown2 = await _boot(tmp_path, data_dir, keelson)
    try:
        assert not list(data_dir.glob(".*.sidecar-tmp"))
        async with sidecar2.state.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM apply_journal"))).scalar_one() == 0
        assert any(r["created_via"].startswith("change_set:") for r in csv_rows(data_dir, "transfers.csv"))
        assert len(csv_rows(data_dir, "transfer_lines.csv")) == 4
        assert int(stock(data_dir, "FH-LDS", "FH-PARKA-NVY-M")["in_transit"]) == 60
        run2 = Running(app2, sidecar2, data_dir, tmp_path)
        async with run2.client() as c:
            cs = await call(c, "get_change_set", {"change_set_id": validated["change_set_id"]})
            assert cs["status"] == "applied"
            # the reloaded staging matches the files (no drift reported for the sidecar's own apply)
            assert (await call(c, "scn.changes_since", {"watermark": "j:0"})).get("full_resync_required") is None
            # a replay of the same request returns the stored response
            again = await apply(c, keelson, validated, idem="crash-1")
            assert again["status"] == "applied"
    finally:
        await shutdown2()


async def _journal(sidecar: object, cs_id: str, files: list[dict[str, str]]) -> None:
    async with begin_write(sidecar.state) as conn:  # type: ignore[attr-defined]
        await conn.execute(text("INSERT INTO apply_journal (change_set_id, purpose, plan, created_at) "
                                "VALUES (:c, 'apply', :p, '2026-10-08T00:00:00Z')"),
                           {"c": cs_id, "p": json.dumps({"files": files})})


async def test_recovery_outcomes(tmp_path: Path, data_dir: Path, keelson: FakeKeelson) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    sidecar = Sidecar(Settings.from_env(sidecar_env(tmp_path, data_dir)), http_client=keelson.client())
    await sidecar.start()
    try:
        backend = sidecar.backend
        assert isinstance(backend, csvdir.CsvBackend)
        path = data_dir / "suppliers.csv"
        now_sha = csvdir.sha256_file(path)
        # the staged file is gone but the target already holds the after-image: fine
        await _journal(sidecar, "cs-ok", [{"table": "suppliers", "path": str(path), "tmp": str(path) + ".gone",
                                          "before": "x", "after": now_sha}])
        # neither the staged file nor the after-image: the change set is marked failed, drift journalled
        async with begin_write(sidecar.state) as conn:
            await conn.execute(text("INSERT INTO change_set (id, code, title, status, created_at, updated_at) "
                                    "VALUES ('cs-lost', 'CS-9999', 't', 'applied', 'x', 'x')"))
        await _journal(sidecar, "cs-lost", [{"table": "suppliers", "path": str(path), "tmp": str(path) + ".gone",
                                            "before": "x", "after": "0" * 64}])
        notes = await backend.recover()
        assert notes == []
        async with sidecar.state.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM apply_journal"))).scalar_one() == 0
            status = (await conn.execute(text("SELECT status, result FROM change_set WHERE id = 'cs-lost'"))).one()
            assert status[0] == "failed" and "apply_lost" in status[1]
            kinds = [r[0] for r in (await conn.execute(text("SELECT kind FROM journal"))).all()]
            assert kinds == ["external"]
    finally:
        await sidecar.close()


async def test_second_process_is_refused(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.errors import ConfigError
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    first = Sidecar(Settings.from_env(sidecar_env(tmp_path, data_dir)))
    await first.start()
    second = Sidecar(Settings.from_env(sidecar_env(tmp_path / "other", data_dir)))
    try:
        with pytest.raises(ConfigError, match="another sidecar process holds"):
            await second.start()
    finally:
        await second.close()
        await first.close()
