"""scn/1.0 export: every entity in the manifest, Parquet shape and types, row validation, part
splitting, duplicate source ids, and the contract models Keelson parses the results with."""

from __future__ import annotations

import datetime as dt
import hashlib
import types
import typing
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from adapter_sidecar.scn_export import SCN_ARROW_TYPES, RowError, convert_row, fk_problems
from conftest import FERNHILL, Running, sidecar_env, start_app
from helpers import call
from keelson_contracts.scn import (
    SCN_COLUMNS,
    SCN_ENTITIES,
    SCN_ROW_MODELS,
    ChangesSince,
    SnapshotExport,
    SnapshotManifest,
    parse_row,
)


def _expected_arrow(annotation: object) -> pa.DataType:
    """Arrow type a row-model field annotation calls for (independent of SCN_ARROW_TYPES)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _expected_arrow(typing.get_args(annotation)[0])
    if origin in (types.UnionType, typing.Union):
        kinds = [_expected_arrow(a) for a in typing.get_args(annotation) if a is not type(None)]
        return pa.float64() if pa.float64() in kinds else kinds[0]
    if origin is list:
        return pa.list_(pa.string())
    if origin is typing.Literal or annotation is str:
        return pa.string()
    if annotation is int:
        return pa.int64()
    if annotation is float:
        return pa.float64()
    if annotation is dt.date:
        return pa.date32()
    raise AssertionError(f"unexpected annotation {annotation!r}")


def test_arrow_types_match_the_contract() -> None:
    assert list(SCN_ARROW_TYPES) == list(SCN_ENTITIES)
    for entity, model in SCN_ROW_MODELS.items():
        assert list(SCN_ARROW_TYPES[entity]) == list(SCN_COLUMNS[entity])
        for col, field in model.model_fields.items():
            want = _expected_arrow(field.annotation)
            assert SCN_ARROW_TYPES[entity][col] == want, (entity, col, field.annotation)


def test_convert_row_rules() -> None:
    cols = list(SCN_COLUMNS["allocation"])
    ok = convert_row("allocation", cols, ["AL-1", "AL-1", "S", "DC", None, "2026-W45", 3, "planned", 1])
    assert ok["qty"] == 3 and ok["store_code"] is None
    for bad, msg in ((["AL-1", "AL-1", "S", "DC", None, "2026-W99", 3, "planned", 1], "ship_week"),
                     (["AL-1", "AL-1", "S", "DC", None, "2026-W45", 2.5, "planned", 1], "qty"),
                     (["AL-1", "AL-1", "S", "DC", None, "2026-W45", -1, "planned", 1], "qty"),
                     (["AL-1", "AL-1", 7, "DC", None, "2026-W45", 1, "planned", 1], "sku_code")):
        with pytest.raises(RowError, match=msg):
            convert_row("allocation", cols, bad)
    tcols = list(SCN_COLUMNS["transfer_order_line"])
    row = convert_row("transfer_order_line", tcols, ["T#1", "T", "A", "B", "2026-10-20", None, "S", 5, "planned", 1])
    assert row["ship_date"] == dt.date(2026, 10, 20)
    with pytest.raises(RowError, match="YYYY-MM-DD"):
        convert_row("transfer_order_line", tcols, ["T#1", "T", "A", "B", "20/10/2026", None, "S", 5, "planned", 1])
    lane = convert_row("lane", list(SCN_COLUMNS["lane"]), ["L", "L", "A", "B", "sea", None, 30, '["CAPE"]', None, 1])
    assert lane["via"] == ["CAPE"]


async def test_manifest_parts_and_rows(fernhill: Running) -> None:
    async with fernhill.client() as c:
        manifest = await call(c, "scn.snapshot_open", {"entities": list(SCN_ENTITIES)})
        parsed = SnapshotManifest.model_validate(manifest)
        assert set(parsed.entities) == set(SCN_ENTITIES) and parsed.consistency == "consistent"
        assert parsed.as_of_range is None and parsed.watermark == "j:0"
        cfg = fernhill.sidecar.cfg
        rows: dict[str, list[dict[str, object]]] = {}
        for entity in SCN_ENTITIES:
            export = await call(c, "scn.snapshot_export", {"snapshot_id": manifest["snapshot_id"], "entity": entity})
            exp = SnapshotExport.model_validate(export)
            assert exp.entity == entity and len(exp.parts) == manifest["entities"][entity]["parts"]
            if entity not in cfg.entities:
                assert len(exp.parts) == 1 and exp.parts[0].rows == 0
            folder = fernhill.tmp / "snapshots" / manifest["snapshot_id"] / entity
            for i, part in enumerate(exp.parts, start=1):
                path = folder / f"part-{i:04d}.parquet"
                data = path.read_bytes()
                assert hashlib.sha256(data).hexdigest() == part.sha256 == manifest["entities"][entity]["sha256"][i - 1]
                assert len(data) == part.bytes and path.stat().st_mode & 0o222 == 0
                table = pq.read_table(path)
                assert table.schema.names == list(SCN_COLUMNS[entity])
                assert [f.type for f in table.schema] == list(SCN_ARROW_TYPES[entity].values())
                assert pq.ParquetFile(path).metadata.row_group(0).column(0).compression == "ZSTD"
                for r in table.to_pylist():
                    parse_row(entity, r)
                    rows.setdefault(entity, []).append(r)
        assert fk_problems(rows) == []
        stats = await fernhill.sidecar.stats()
        assert {e: len(rows.get(e, [])) for e in cfg.entities} == stats["entities"]
        kinds = {r["kind"] for r in rows["location"]}
        assert kinds == {"dc", "store", "cluster", "supplier"}


async def test_unknown_entity_is_refused(fernhill: Running) -> None:
    from fastmcp.exceptions import ToolError

    async with fernhill.client() as c:
        with pytest.raises(ToolError, match="unknown_entity"):
            await c.call_tool("scn.snapshot_open", {"entities": ["shipment"]})


async def _variant(tmp_path: Path, data_dir: Path, edit: typing.Callable[[dict[str, typing.Any]], None]
                   ) -> tuple[Running, typing.Callable[[], typing.Any]]:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    raw = yaml.safe_load((FERNHILL / "sidecar.yaml").read_text(encoding="utf-8"))
    edit(raw)
    path = tmp_path / "variant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings = Settings.from_env(sidecar_env(tmp_path, data_dir, path))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar)
    return Running(app, sidecar, data_dir, tmp_path), shutdown


async def test_parts_split_at_part_rows(tmp_path: Path, data_dir: Path) -> None:
    def edit(raw: dict[str, typing.Any]) -> None:
        raw["snapshot"]["part_rows"] = 50

    run, shutdown = await _variant(tmp_path, data_dir, edit)
    try:
        async with run.client() as c:
            m = await call(c, "scn.snapshot_open", {"entities": ["demand_forecast", "item"]})
        assert m["entities"]["demand_forecast"] == {**m["entities"]["demand_forecast"], "rows": 216, "parts": 5}
        assert m["entities"]["item"]["parts"] == 1
    finally:
        await shutdown()


async def test_duplicate_source_id_fails(tmp_path: Path, data_dir: Path) -> None:
    from fastmcp.exceptions import ToolError

    def edit(raw: dict[str, typing.Any]) -> None:
        raw["entities"]["allocation"]["sql"] = raw["entities"]["allocation"]["sql"].replace(
            "SELECT code AS source_id", "SELECT 'same' AS source_id")

    run, shutdown = await _variant(tmp_path, data_dir, edit)
    try:
        async with run.client() as c:
            with pytest.raises(ToolError, match="invalid_source_data: allocation 'same': duplicate source_id"):
                await c.call_tool("scn.snapshot_open", {"entities": ["allocation"]})
        assert not list((tmp_path / "snapshots").iterdir())  # nothing half-written is kept
    finally:
        await shutdown()


def test_contract_models_accept_sidecar_shapes() -> None:
    ChangesSince.model_validate({"changes": [{"entity": "allocation", "op": "upsert", "key": "AL-1", "version": 2,
                                              "at": "2026-10-08T10:00:00Z"}], "next_watermark": "j:3",
                                 "has_more": False})
