"""The Fernhill demo instance: reproducible golden data, demo seeding, admin reset / stats / rows."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from conftest import ADMIN_KEY, FERNHILL, GOLDEN, FakeKeelson, Running, sidecar_env, start_app
from helpers import TO_ITEM, apply, call, prepare


def test_generator_reproduces_golden_files(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("fernhill_generate", FERNHILL / "generate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["generate.py", str(tmp_path)]) == 0
    produced = {p.name: p.read_bytes() for p in tmp_path.glob("*.csv")}
    golden = {p.name: p.read_bytes() for p in GOLDEN.glob("*.csv")}
    assert produced == golden and len(golden) == 13


async def test_demo_mode_seeds_an_empty_data_dir(tmp_path: Path, keelson: FakeKeelson) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    empty = tmp_path / "fresh"
    settings = Settings.from_env(sidecar_env(tmp_path, empty))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar)
    try:
        assert sorted(p.name for p in empty.glob("*.csv")) == sorted(p.name for p in GOLDEN.glob("*.csv"))
        async with Running(app, sidecar, empty, tmp_path).http() as http:
            assert (await http.get("/readyz")).status_code == 200
    finally:
        await shutdown()


async def test_admin_reset_stats_and_rows(fernhill: Running, keelson: FakeKeelson) -> None:
    d = fernhill.data_dir
    assert d is not None
    h = {"X-Admin-Key": ADMIN_KEY}
    async with fernhill.client() as c:
        v = await prepare(c, [TO_ITEM])
        await apply(c, keelson, v)
    async with fernhill.http() as http:
        stats = (await http.get("/v1/admin/stats", headers=h)).json()
        assert stats["tables"]["transfers"] == 2 and stats["change_sets"] == 1
        assert stats["entities"]["transfer_order_line"] == 4 and stats["watermark"] != "j:0"
        assert set(stats["fingerprints"]) == {f"csv:{t}" for t in fernhill.sidecar.cfg.tables}
        rows = (await http.get("/v1/admin/rows/stock?site=FH-RTM&sku=FH-PARKA-NVY-M", headers=h)).json()
        assert len(rows["items"]) == 1 and rows["items"][0]["rev"] == 2
        assert (await http.get("/v1/admin/rows/stock?colour=red", headers=h)).status_code == 400
        assert (await http.get("/v1/admin/rows/nope", headers=h)).status_code == 404
        reset = await http.post("/v1/admin/reset", headers=h)
        assert reset.status_code == 200 and reset.json()["status"] == "reset"
    assert {p.name: p.read_bytes() for p in d.glob("*.csv")} == {p.name: p.read_bytes() for p in GOLDEN.glob("*.csv")}
    assert not list((fernhill.tmp / "snapshots").iterdir())
    async with fernhill.client() as c:
        changes = await call(c, "scn.changes_since", {"watermark": "j:1"})
        assert changes["full_resync_required"] is True
        again = await prepare(c, [TO_ITEM])
        # change-set codes keep counting (the audit log is append-only and keeps CS-0001's rows)
        assert again["code"] == "CS-0002"
        trail = await call(c, "get_audit_trail", {"change_set_code": "CS-0001"})
        assert trail["items"]
        snap = await call(c, "scn.snapshot_open", {"entities": ["transfer_order_line"]})
        assert snap["entities"]["transfer_order_line"]["rows"] == 2


async def test_reset_needs_demo_mode(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    settings = Settings.from_env(sidecar_env(tmp_path, data_dir, DEMO_MODE="0"))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar)
    try:
        async with Running(app, sidecar, data_dir, tmp_path).http() as http:
            r = await http.post("/v1/admin/reset", headers={"X-Admin-Key": ADMIN_KEY})
            assert r.status_code == 403 and r.json()["code"] == "demo_mode_off"
            assert (await http.get("/healthz")).json() == {"status": "ok"}
    finally:
        await shutdown()
    shutil.rmtree(tmp_path / "snapshots", ignore_errors=True)
