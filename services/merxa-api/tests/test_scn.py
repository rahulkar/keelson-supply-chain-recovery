"""scn/1.0 export: snapshot_open → snapshot_export → signed download → verify; changes_since."""

from __future__ import annotations

import hashlib
import io
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pyarrow.parquet as pq
import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import admin, apply_validated, auth, build_change_set, key, po_line_version, preparer, reader
from merxa_api import tables as t
from merxa_api.services.snapshots import ENTITIES, ENTITY_NAMES
from support import FakeKeelson, FakeRealm

# The scn/1.0 column contract (docs/contracts/25-scn-1.0.md), written out independently.
SCN_COLUMNS: dict[str, list[str]] = {
    "location": [
        "source_id",
        "kind",
        "code",
        "name",
        "country",
        "city",
        "lat",
        "lon",
        "parent_code",
        "grade",
        "climate",
        "unlocode",
        "src_version",
    ],
    "item": [
        "source_id",
        "kind",
        "code",
        "style_code",
        "family",
        "colour",
        "size",
        "size_seq",
        "retail_price_cents",
        "unit_cost_cents",
        "currency",
        "src_version",
    ],
    "bom_line": [
        "source_id",
        "style_code",
        "component_code",
        "supplier_code",
        "factory_code",
        "qty_per_unit",
        "src_version",
    ],
    "lane": [
        "source_id",
        "code",
        "origin_code",
        "dest_code",
        "mode",
        "carrier_code",
        "transit_days",
        "via",
        "alt_lane_code",
        "src_version",
    ],
    "lane_rate": [
        "source_id",
        "lane_code",
        "valid_from",
        "valid_to",
        "basis",
        "rate_cents",
        "weekly_capacity",
        "src_version",
    ],
    "sailing": ["source_id", "code", "lane_code", "cutoff", "etd", "eta", "capacity_teu", "status", "src_version"],
    "po_line": [
        "source_id",
        "po_code",
        "line_no",
        "supplier_code",
        "factory_code",
        "dest_dc_code",
        "sku_code",
        "qty_ordered",
        "qty_received",
        "ex_factory_date",
        "eta_dc",
        "status",
        "shipment_code",
        "sailing_code",
        "src_version",
    ],
    "production_order": [
        "source_id",
        "code",
        "po_line_ref",
        "line_code",
        "seq",
        "planned_start",
        "planned_end",
        "status",
        "hold_reason",
        "src_version",
    ],
    "inventory_position": [
        "source_id",
        "location_kind",
        "location_code",
        "sku_code",
        "on_hand",
        "allocated",
        "reserved",
        "in_transit",
        "src_version",
    ],
    "demand_forecast": [
        "source_id",
        "style_code",
        "location_kind",
        "location_code",
        "iso_week",
        "units",
        "forecast_version",
    ],
    "size_curve": ["source_id", "family", "cluster_code", "size", "share_bp"],
    "allocation": [
        "source_id",
        "code",
        "sku_code",
        "dc_code",
        "store_code",
        "ship_week",
        "qty",
        "status",
        "src_version",
    ],
    "transfer_order_line": [
        "source_id",
        "to_code",
        "from_dc",
        "to_dc",
        "ship_date",
        "eta_date",
        "sku_code",
        "qty",
        "status",
        "src_version",
    ],
    "online_order_line": [
        "source_id",
        "order_code",
        "market",
        "channel",
        "store_code",
        "sku_code",
        "qty",
        "reserved_qty",
        "promise_date",
        "status",
        "src_version",
    ],
    "promotion": [
        "source_id",
        "code",
        "markets",
        "grades",
        "style_codes",
        "period_start",
        "period_end",
        "discount_bp",
        "uplift_bp",
        "src_version",
    ],
}


def test_entity_columns_match_the_contract() -> None:
    assert set(ENTITY_NAMES) == set(SCN_COLUMNS)
    for name, (schema, _sql) in ENTITIES.items():
        assert schema.names == SCN_COLUMNS[name], name
        assert "source_id" in schema.names


async def _download(http: httpx.AsyncClient, uri: str) -> httpx.Response:
    parts = urlsplit(uri)
    assert f"{parts.scheme}://{parts.netloc}" == "http://merxa.localhost"
    return await http.get(f"{parts.path}?{parts.query}")  # no Authorization header


async def test_snapshot_round_trip(
    http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any, engine: AsyncEngine, reseed: None
) -> None:
    tok = reader(realm)
    async with engine.connect() as conn:
        n_inv = (await conn.execute(select(func.count()).select_from(t.inventory_position))).scalar_one()
        n_fc = (await conn.execute(select(func.count()).select_from(t.demand_forecast))).scalar_one()
        n_loc = sum(
            [
                (await conn.execute(select(func.count()).select_from(tb))).scalar_one()
                for tb in (t.dc, t.store_cluster, t.store, t.port, t.supplier, t.factory)
            ]
        )
    async with mcp_client(tok) as c:
        opened = (await c.call_tool("scn.snapshot_open", {"entities": list(ENTITY_NAMES)})).structured_content
        assert opened["canonical_model"] == "scn/1.0"
        assert opened["watermark"].startswith("ox:")
        assert opened["entities"]["inventory_position"]["rows"] == n_inv
        assert opened["entities"]["demand_forecast"]["rows"] == n_fc
        assert opened["entities"]["location"]["rows"] == n_loc
        sid = opened["snapshot_id"]
        for entity in ENTITY_NAMES:
            res = await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": entity})
            exp = res.structured_content
            links = [b for b in res.content if getattr(b, "type", "") == "resource_link"]
            assert [str(link.uri) for link in links] == [p["uri"] for p in exp["parts"]]
            total = 0
            for part in exp["parts"]:
                r = await _download(http, part["uri"])
                assert r.status_code == 200, (entity, r.text)
                assert r.headers["content-type"] == "application/vnd.apache.parquet"
                data = r.content
                assert hashlib.sha256(data).hexdigest() == part["sha256"]
                assert len(data) == part["bytes"]
                table = pq.read_table(io.BytesIO(data))
                assert table.num_rows == part["rows"]
                assert table.schema.names == SCN_COLUMNS[entity]
                total += table.num_rows
            assert total == opened["entities"][entity]["rows"], entity
            assert len(exp["parts"]) == opened["entities"][entity]["parts"]
            assert [p["sha256"] for p in exp["parts"]] == opened["entities"][entity]["sha256"]

        # spot-check values
        res = await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "po_line"})
        rows = pq.read_table(io.BytesIO((await _download(http, res.structured_content["parts"][0]["uri"])).content))
        po = {r["source_id"]: r for r in rows.to_pylist()}
        line = po["PO-BD-0003#1"]
        assert (line["supplier_code"], line["dest_dc_code"], line["sailing_code"]) == ("CMT-BD-01", "UK1", "SAIL-0001")
        assert line["shipment_code"] == "SHP-000003" and str(line["eta_dc"]) == "2026-11-10"
        res = await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "lane"})
        lanes = pq.read_table(io.BytesIO((await _download(http, res.structured_content["parts"][0]["uri"])).content))
        sa = next(r for r in lanes.to_pylist() if r["code"] == "CGP-UK1-SA")
        assert sa["via"] == ["AEJEA"] and sa["mode"] == "sea_air"
        res = await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "location"})
        locs = pq.read_table(io.BytesIO((await _download(http, res.structured_content["parts"][0]["uri"])).content))
        store = next(r for r in locs.to_pylist() if r["kind"] == "store")
        assert store["parent_code"] and store["climate"] in ("cold", "mild", "warm") and isinstance(store["lat"], float)
        # items: components carry their material name + kind as the item family (scn/1.0 has no item
        # name column) so consumers can match material words; styles / SKUs carry the style family
        res = await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "item"})
        items = [r for part in res.structured_content["parts"]
                 for r in pq.read_table(io.BytesIO((await _download(http, part["uri"])).content)).to_pylist()]
        async with engine.connect() as conn:
            comps = {r.code: (r.name, r.kind) for r in (await conn.execute(select(t.component))).all()}
            fams = {r.code: r.family for r in (await conn.execute(select(t.style))).all()}
        exported = {r["code"]: r for r in items}
        assert comps and fams
        for code, (name, kind) in comps.items():
            words = kind.replace("_", " ").lower()
            want = name if words in name.lower() else f"{name} ({words})"
            assert exported[code]["family"] == want, code
            assert words in exported[code]["family"].lower()
        for r in items:
            if r["kind"] in ("style", "sku"):
                assert r["family"] == fams[r["style_code"]], r["code"]

        with pytest.raises(ToolError, match="unknown_entity"):
            await c.call_tool("scn.snapshot_open", {"entities": ["nope"]})
        with pytest.raises(ToolError, match="not_found"):
            await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "nope"})


async def test_signed_urls_reject_tamper_and_expiry(
    http: httpx.AsyncClient, realm: FakeRealm, state: Any, reseed: None
) -> None:
    tok = reader(realm)
    r = await http.post("/v1/snapshots", json={"entities": ["sailing", "promotion"]}, headers=auth(tok))
    assert r.status_code == 201, r.text
    sid = r.json()["snapshot_id"]
    exp = (await http.get(f"/v1/snapshots/{sid}/sailing", headers=auth(tok))).json()
    uri = exp["parts"][0]["uri"]
    assert (await _download(http, uri)).status_code == 200
    parts = urlsplit(uri)
    q = parse_qs(parts.query)
    sig, expiry = q["sig"][0], int(q["exp"][0])
    bad_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert (await http.get(f"{parts.path}?exp={expiry}&sig={bad_sig}")).status_code == 403
    assert (await http.get(f"{parts.path}?exp={expiry + 60}&sig={sig}")).status_code == 403  # exp is signed
    other = parts.path.replace("/sailing/", "/promotion/")
    assert (await http.get(f"{other}?exp={expiry}&sig={sig}")).status_code == 403  # path is signed
    assert (await http.get(parts.path)).status_code == 403
    # a correctly signed but expired URL
    past = int(time.time()) - 10
    path = parts.path.removeprefix("/v1/snapshots/")
    r = await http.get(f"{parts.path}?exp={past}&sig={state.snapshots.sign(path, past)}")
    assert r.status_code == 403 and r.json()["code"] == "url_expired"
    # traversal / unknown files
    assert (await http.get(f"/v1/snapshots/{sid}/sailing/..%2F..%2Fx?exp={expiry}&sig={sig}")).status_code == 404
    # expired snapshots disappear (export 404, files removed by housekeeping)
    async with state.engine.begin() as conn:
        await conn.execute(update(t.scn_snapshot).where(t.scn_snapshot.c.id == sid).values(expires_at=func.now()))
    assert (await http.get(f"/v1/snapshots/{sid}/sailing", headers=auth(tok))).status_code == 404
    assert await state.snapshots.cleanup() >= 1
    assert not (state.snapshots.root / sid).exists()
    assert (await _download(http, uri)).status_code == 404


async def test_changes_since_watermark(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, mcp_client: Any, reseed: None
) -> None:
    tok, prep = reader(realm), preparer(realm)
    async with mcp_client(tok) as c:
        opened = (
            await c.call_tool("scn.snapshot_open", {"entities": ["po_line", "inventory_position"]})
        ).structured_content
        wm = opened["watermark"]
        empty = (await c.call_tool("scn.changes_since", {"watermark": wm})).structured_content
        assert empty == {"changes": [], "next_watermark": wm, "has_more": False}

    items = [
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0002#3",
            "expected_version": await po_line_version(http, prep, "PO-BD-0002", 3),
            "payload": {"eta_dc": "2026-11-06"},
        },
        {
            "op": "transfer_order.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "from_dc": "PL1",
                "to_dc": "NL1",
                "ship_date": "2026-10-13",
                "lines": [{"sku": "PUF-M-NVY-L", "qty": 9}],
            },
        },
    ]
    v = await build_change_set(http, prep, items)
    r = await apply_validated(http, realm, keelson, v)
    assert r.status_code == 200, r.text
    code = r.json()["change_set_code"]
    async with mcp_client(tok) as c:
        ch = (await c.call_tool("scn.changes_since", {"watermark": wm})).structured_content
    keys = {(x["entity"], x["key"]) for x in ch["changes"]}
    assert ("po_line", "PO-BD-0002#3") in keys
    assert ("inventory_position", "dc/PL1/PUF-M-NVY-L") in keys
    assert ("inventory_position", "dc/NL1/PUF-M-NVY-L") in keys
    assert any(e == "transfer_order_line" for e, _ in keys)
    assert all(x["op"] == "upsert" and x["at"] and isinstance(x["version"], int) for x in ch["changes"])
    po = (await http.get("/v1/purchase-orders/PO-BD-0002", headers=auth(tok))).json()
    v3 = next(ln["version"] for ln in po["lines"] if ln["line_no"] == 3)
    assert next(x for x in ch["changes"] if x["key"] == "PO-BD-0002#3")["version"] == v3
    assert int(ch["next_watermark"][3:]) > int(wm[3:])
    # nothing new after next_watermark
    again = (await http.get("/v1/changes", params={"watermark": ch["next_watermark"]}, headers=auth(tok))).json()
    assert again["changes"] == []
    # a demo reset tells consumers to resync
    r = await http.post("/v1/admin/scenario:reset", headers=auth(admin(realm), key()))
    assert r.status_code == 200, r.text
    after_reset = (await http.get("/v1/changes", params={"watermark": ch["next_watermark"]}, headers=auth(tok))).json()
    assert after_reset.get("full_resync_required") is True
    assert (await http.get("/v1/changes", params={"watermark": "x1"}, headers=auth(tok))).status_code == 400
    del code


async def test_unwritable_snapshot_storage_is_a_503(
    http: httpx.AsyncClient, realm: FakeRealm, state: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(state.snapshots, "root", blocker)
    r = await http.post("/v1/snapshots", json={"entities": ["sailing"]}, headers=auth(reader(realm)))
    assert r.status_code == 503 and r.json()["code"] == "snapshot_storage_unavailable"
