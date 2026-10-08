"""CSV backend: typed loading, deterministic write-back, drift detection, touched-files-only
applies, and an external edit during an apply (conflict ``source_changed``, files untouched)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import GOLDEN, FakeKeelson, Running, sidecar_env
from helpers import TO_ITEM, alloc_item, apply, call, prepare, stock

from adapter_sidecar.backends.csvdir import CsvBackend
from adapter_sidecar.errors import SourceDataError


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_files(d: Path) -> dict[str, str]:
    return {p.name: digest(p) for p in sorted(d.glob("*.csv"))}


async def test_typed_load_errors_name_row_and_column(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    text = (data_dir / "stock.csv").read_text(encoding="utf-8").splitlines()
    cells = text[2].split(",")
    cells[3] = "12.5"  # on_hand is an int column
    text[2] = ",".join(cells)
    (data_dir / "stock.csv").write_text("\n".join(text) + "\n", encoding="utf-8")
    sidecar = Sidecar(Settings.from_env(sidecar_env(tmp_path, data_dir)))
    try:
        with pytest.raises(SourceDataError) as err:
            await sidecar.start()
        assert err.value.row == 3 and err.value.column == "on_hand" and "not an integer" in str(err.value)
    finally:
        await sidecar.close()


@pytest.mark.parametrize(("edit", "message"), [
    (lambda t: t.replace("code,name,country", "code,label,country", 1), "header"),
    (lambda t: t + "FH-LDS,Dup,GB,Leeds,1.0,2.0,1\n", "duplicate key"),
    (lambda t: t + "FH-XXX,Short,GB\n", "cells"),
])
async def test_structural_errors(tmp_path: Path, data_dir: Path, edit: object, message: str) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    path = data_dir / "warehouses.csv"
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")  # type: ignore[operator]
    sidecar = Sidecar(Settings.from_env(sidecar_env(tmp_path, data_dir)))
    try:
        with pytest.raises(SourceDataError, match=message):
            await sidecar.start()
    finally:
        await sidecar.close()


async def test_write_back_is_deterministic(fernhill: Running) -> None:
    """Every table rendered from its staging rows is byte-identical to the golden file."""
    backend = fernhill.sidecar.backend
    assert isinstance(backend, CsvBackend)
    async with backend.engine.connect() as conn:
        for name, table in backend.cfg.tables.items():
            data = await backend.render_file(conn, name, table)
            assert data == (GOLDEN / str(table.file)).read_bytes(), name


async def test_apply_rewrites_only_touched_files(fernhill: Running, keelson: FakeKeelson) -> None:
    d = fernhill.data_dir
    assert d is not None
    before = snapshot_files(d)
    stock_before = (d / "stock.csv").read_text(encoding="utf-8").splitlines()
    async with fernhill.client() as c:
        validated = await prepare(c, [TO_ITEM])
        assert (await apply(c, keelson, validated))["status"] == "applied"
    after = snapshot_files(d)
    changed = {n for n in before if before[n] != after[n]}
    assert changed == {"stock.csv", "transfers.csv", "transfer_lines.csv"}
    stock_after = (d / "stock.csv").read_text(encoding="utf-8").splitlines()
    diff = [i for i, (a, b) in enumerate(zip(stock_before, stock_after, strict=True)) if a != b]
    assert len(diff) == 4  # FH-RTM reserved (2 SKUs) + FH-LDS in_transit (2 SKUs); every other line identical
    assert stock(d, "FH-RTM", "FH-PARKA-NVY-M")["rev"] == "2"
    assert not list(d.glob(".*.sidecar-tmp"))


async def test_external_edit_is_drift(fernhill: Running) -> None:
    d = fernhill.data_dir
    assert d is not None
    async with fernhill.client() as c:
        snap = await call(c, "scn.snapshot_open", {"entities": ["inventory_position"]})
        quiet = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
        assert quiet["changes"] == [] and "full_resync_required" not in quiet
        text = (d / "stock.csv").read_text(encoding="utf-8")
        (d / "stock.csv").write_text(text.replace("FH-RTM,dc,FH-PARKA-NVY-M,", "FH-RTM,dc,FH-PARKA-NVY-M,9", 1),
                                     encoding="utf-8")
        drift = await call(c, "scn.changes_since", {"watermark": snap["watermark"]})
        assert drift["full_resync_required"] is True and drift["changes"] == []
        # the staging tables follow the file
        rows = await fernhill.sidecar.rows("stock", {"site": "FH-RTM", "sku": "FH-PARKA-NVY-M"})
        assert str(rows["items"][0]["on_hand"]).startswith("9")


async def test_external_edit_during_apply_conflicts(fernhill: Running, keelson: FakeKeelson,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    d = fernhill.data_dir
    assert d is not None
    async with fernhill.client() as c:
        validated = await prepare(c, [TO_ITEM, alloc_item(d)])
        original = CsvBackend.render_file
        edited: dict[str, bytes] = {}

        async def render_and_edit(self: CsvBackend, conn: object, name: str, table: object) -> bytes:
            data = await original(self, conn, name, table)  # type: ignore[arg-type]
            if name == "stock" and not edited:  # someone saves stock.csv while the apply runs
                path = d / "stock.csv"
                edited["stock"] = path.read_bytes() + b"FH-S01,store,FH-NEW-1,1,0,0,0,1\n"
                path.write_bytes(edited["stock"])
            return data

        monkeypatch.setattr(CsvBackend, "render_file", render_and_edit)
        before = snapshot_files(d)
        result = await apply(c, keelson, validated)
        assert result["status"] == "conflict"
        assert {x["reason"] for x in result["conflicts"]} == {"source_changed"}
        assert [x["seq"] for x in result["conflicts"]] == [1]  # only the transfer touched stock
        after = snapshot_files(d)
        assert after["stock.csv"] == hashlib.sha256(edited["stock"]).hexdigest()
        assert {n: h for n, h in after.items() if n != "stock.csv"} == {n: h for n, h in before.items()
                                                                        if n != "stock.csv"}
        assert not list(d.glob(".*.sidecar-tmp"))
        cs = await call(c, "get_change_set", {"change_set_id": validated["change_set_id"]})
        assert cs["status"] == "conflict"
