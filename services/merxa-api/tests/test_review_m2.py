"""Regression tests for defects found in the independent M2 review of merxa-api."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import admin, apply_validated, auth, build_change_set, key, preparer, reader, rollback
from merxa_api import tables as t
from merxa_api.seed import loader
from merxa_api.seed.loader import seed
from support import READ, FakeKeelson, FakeRealm


async def _mint(http: httpx.AsyncClient, realm: FakeRealm, scopes: list[str], name: str) -> str:
    r = await http.post("/v1/api-keys", json={"name": name, "scopes": scopes}, headers=auth(admin(realm), key()))
    assert r.status_code == 201, r.text
    return r.json()["key"]


async def test_malformed_download_signature_is_403_not_500(
    http: httpx.AsyncClient, realm: FakeRealm, reseed: None
) -> None:
    """A non-ASCII ``sig`` reached ``hmac.compare_digest`` (TypeError → 500)."""
    tok = reader(realm)
    r = await http.post("/v1/snapshots", json={"entities": ["sailing"]}, headers=auth(tok))
    assert r.status_code == 201, r.text
    uri = (await http.get(f"/v1/snapshots/{r.json()['snapshot_id']}/sailing", headers=auth(tok))).json()["parts"][0][
        "uri"
    ]
    parts = urlsplit(uri)
    q = parse_qs(parts.query)
    sig, exp = q["sig"][0], q["exp"][0]
    assert (await http.get(f"{parts.path}?exp={exp}&sig={sig}")).status_code == 200
    for bad_sig in ("é" * 64, sig.upper(), sig[:-2], sig + "00", "zz" * 32):
        resp = await http.get(f"{parts.path}?exp={exp}&sig={quote(bad_sig)}")
        assert resp.status_code == 403, (bad_sig, resp.status_code)
        assert resp.json()["code"] == "invalid_signature"
    # non-ASCII digits in exp (str.isdigit() accepts them)
    resp = await http.get(f"{parts.path}?exp={quote('١٢٣')}&sig={sig}")
    assert resp.status_code == 403


async def test_merge_reset_keeps_the_mark_of_a_table_committed_during_the_merge(
    engine: AsyncEngine, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write whose dirty-log mark was still uncommitted when the reset read the log (and
    whose table was therefore not merged) must stay marked; the reset used to delete every
    mark with ``id <= max(id seen)``, so that table drifted from golden for good."""
    await seed(engine, settings.seed_profile, reset_first=True)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM merxa.seed_dirty_log"))).scalar_one() == 0
        code, name = (await conn.execute(select(t.style.c.code, t.style.c.name).order_by(t.style.c.code))).first()
    writer = await engine.connect()
    tx = await writer.begin()
    try:
        # W: marks `style` (lower id), not committed yet
        await writer.execute(update(t.style).where(t.style.c.code == code).values(name="drifted"))
        # Y: marks `dc` (higher id), committed
        async with engine.begin() as conn:
            await conn.execute(update(t.dc).where(t.dc.c.code == "UK1").values(inbound_cap_units_wk=1))
        original = loader.merge_golden

        async def merge_while_w_commits(conn: Any, golden_seed: Any, only: Any = None) -> Any:
            assert only == {"dc"}  # W's mark was not visible when the log was read
            await tx.commit()
            return await original(conn, golden_seed, only)

        monkeypatch.setattr(loader, "merge_golden", merge_while_w_commits)
        await seed(engine, settings.seed_profile, reset_first=True)
    finally:
        if tx.is_active:
            await tx.rollback()
        await writer.close()
    monkeypatch.setattr(loader, "merge_golden", original)
    async with engine.connect() as conn:
        marks = {r[0] for r in await conn.execute(text("SELECT table_name FROM merxa.seed_dirty_log"))}
        assert marks == {"style"}
        assert (await conn.execute(select(t.dc.c.inbound_cap_units_wk).where(t.dc.c.code == "UK1"))).scalar_one() != 1
    await seed(engine, settings.seed_profile, reset_first=True)  # the next reset repairs `style`
    async with engine.connect() as conn:
        assert (await conn.execute(select(t.style.c.name).where(t.style.c.code == code))).scalar_one() == name


async def test_lite_updates_of_styles_stores_and_dcs_reach_changes_since(
    http: httpx.AsyncClient, realm: FakeRealm, lite_client: Any, engine: AsyncEngine, reseed: None
) -> None:
    """scn ``item``/``location`` changes made through /mcp-lite were missing from
    ``scn.changes_since`` (keys must equal the snapshot ``source_id``s)."""
    tok = reader(realm)
    mark = (await http.get("/v1/changes", headers=auth(tok))).json()["next_watermark"]
    write_key = await _mint(http, realm, [*READ, "changes:apply"], "lite-scn-writer")
    async with lite_client(api_key=write_key) as c:
        for type_, key_, fields in (
            ("style", "PUF-W-BLK", {"retail_price_cents": 4500}),
            ("store", "ST-GB-01", {"name": "Renamed store"}),
            ("dc", "UK1", {"inbound_cap_units_wk": 90000}),
        ):
            item = (await c.call_tool("get_item", {"type": type_, "key": key_})).structured_content["item"]
            await c.call_tool(
                "update_item",
                {"type": type_, "key": key_, "fields_json": json.dumps(fields), "expected_version": item["version"]},
            )
    changes = (await http.get("/v1/changes", params={"watermark": mark}, headers=auth(tok))).json()["changes"]
    got = {(c["entity"], c["key"]): c["version"] for c in changes}
    async with engine.connect() as conn:
        style_v = (await conn.execute(select(t.style.c.version).where(t.style.c.code == "PUF-W-BLK"))).scalar_one()
        skus = {r.code: r.version for r in await conn.execute(select(t.sku).where(t.sku.c.style_code == "PUF-W-BLK"))}
        store_v = (await conn.execute(select(t.store.c.version).where(t.store.c.code == "ST-GB-01"))).scalar_one()
        dc_v = (await conn.execute(select(t.dc.c.version).where(t.dc.c.code == "UK1"))).scalar_one()
    assert got[("item", "style:PUF-W-BLK")] == style_v == 2
    assert all(got[("item", f"sku:{code}")] == v for code, v in skus.items()) and len(skus) == 6
    assert got[("location", "store:ST-GB-01")] == store_v == 2
    assert got[("location", "dc:UK1")] == dc_v == 2
    # the keys are the snapshot's source_ids
    snap = (await http.post("/v1/snapshots", json={"entities": ["location", "item"]}, headers=auth(tok))).json()
    assert snap["entities"]["location"]["rows"] > 0


async def test_lite_filter_null_and_scalar_values(
    http: httpx.AsyncClient, realm: FakeRealm, lite_client: Any, engine: AsyncEngine, reseed: None
) -> None:
    """``filter_json`` ``null`` compared the column with the text 'None' (never matched)."""
    read_key = await _mint(http, realm, READ, "lite-filter")
    async with engine.begin() as conn:  # one cluster-level forecast row (store_code NULL)
        await conn.execute(
            t.demand_forecast.insert().values(
                style_code="PUF-W-BLK", cluster_code="UK1-COLD-A", store_code=None, iso_week="2026-W45", units=5
            )
        )
    try:
        async with lite_client(api_key=read_key) as c:
            null_store = (
                await c.call_tool("list_items", {"type": "forecast", "filter_json": '{"store_code": null}'})
            ).structured_content
            assert null_store["total"] == 1 and null_store["items"][0]["cluster_code"] == "UK1-COLD-A"
            received = (
                await c.call_tool("list_items", {"type": "po_line", "filter_json": '{"qty_received": 0}'})
            ).structured_content
            assert received["total"] > 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(t.demand_forecast.delete().where(t.demand_forecast.c.store_code.is_(None)))


async def test_port_closure_rejects_a_malformed_from_date(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    for bad in ("not-a-date", 20261010):
        r = await http.post(
            "/v1/admin/scenario:inject",
            json={"kind": "port_closure", "payload": {"unlocode": "BDCGP", "days": 3, "from": bad}},
            headers=auth(admin(realm), key()),
        )
        assert r.status_code == 400, r.text
        assert r.json()["code"] == "invalid_parameter"


async def test_rollback_needs_its_own_approval(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, reseed: None
) -> None:
    """A (re-minted) receipt of the approval that applied a change set must not undo it."""
    prep = preparer(realm)
    r = await http.get(
        "/v1/allocations", params={"status": "planned", "dc_code": "DE1", "limit": 5}, headers=auth(prep)
    )
    alloc = r.json()["items"][0]
    items = [
        {
            "op": "allocation.set_qty",
            "target_ref": alloc["code"],
            "expected_version": alloc["version"],
            "payload": {"qty": alloc["qty"] + 1},
        }
    ]
    v = await build_change_set(http, prep, items, title="review rollback approval")
    assert v["status"] == "validated", v
    r = await apply_validated(http, realm, keelson, v, "APR-REV-APPLY")
    assert r.status_code == 200, r.text
    r = await rollback(http, realm, keelson, v, "APR-REV-APPLY")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "invalid_receipt" and r.json()["reason"] == "approval_reused"
    r = await rollback(http, realm, keelson, v, "APR-REV-UNDO")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rolled_back"


async def test_api_keys_do_not_authenticate_the_main_mcp(
    http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any
) -> None:
    """API keys are for /mcp-lite and REST only; /mcp needs a merxa-realm token."""
    k = await _mint(http, realm, READ, "not-for-mcp")
    with pytest.raises(Exception):  # noqa: B017 - transport-level 401
        async with mcp_client(k) as c:
            await c.list_tools()
    r = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-Merxa-Api-Key": k, "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401


async def test_webhook_url_validation_and_unusable_url_delivery(
    http: httpx.AsyncClient, realm: FakeRealm, state: Any, receiver: Any, engine: AsyncEngine
) -> None:
    """A URL the HTTP client cannot use made ``_deliver`` raise out of ``run_once`` (not an
    ``httpx.HTTPError``): the delivery was never counted or failed and blocked its batch."""
    adm = admin(realm)
    for bad in ("http://xn--/", "http://[::1/x", "http://h:99999/", "http://" + "a" * 2050 + ".test/"):
        r = await http.post(
            "/v1/webhook-subscriptions",
            json={"name": f"bad {bad[:20]}", "url": bad, "events": ["supplier_message.received"]},
            headers=auth(adm, key()),
        )
        assert r.status_code == 400, (bad, r.status_code, r.text)
    for _ in range(20):  # drain anything pending from earlier tests
        if not any((await state.webhooks.run_once()).values()):
            break
    receiver.reset()
    r = await http.post(
        "/v1/webhook-subscriptions",
        json={"name": "Broken host", "url": "http://ok.example.test/h", "events": ["supplier_message.received"]},
        headers=auth(adm, key()),
    )
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    async with engine.begin() as conn:  # e.g. stored before validation existed
        await conn.execute(
            update(t.webhook_subscription).where(t.webhook_subscription.c.id == sub_id).values(url="http://xn--/")
        )
    try:
        r = await http.post(
            "/v1/admin/scenario:inject",
            json={
                "kind": "supplier_message",
                "payload": {"supplier_code": "TRIM-BD-01", "subject": "probe", "body": "probe"},
            },
            headers=auth(adm, key()),
        )
        assert r.status_code == 200, r.text
        out = await state.webhooks.run_once()  # must not raise
        assert out["delivered"] >= 1 and out["retrying"] >= 1  # the seeded subscription still got it
        async with engine.connect() as conn:
            d = (
                await conn.execute(select(t.webhook_delivery).where(t.webhook_delivery.c.subscription_id == sub_id))
            ).one()
        assert d.status == "pending" and d.attempt == 1 and "IDNAError" in d.last_error
    finally:
        assert (await http.delete(f"/v1/webhook-subscriptions/{sub_id}", headers=auth(adm))).status_code == 204
        receiver.reset()


BAD_PARAM_PROBES: list[tuple[str, dict[str, Any]]] = [
    ("/v1/lane-options", {"origin": "BDCGP", "dest": "UK1", "week": "2026-W99"}),
    ("/v1/lane-options", {"origin": "BDCGP", "dest": "UK1", "week": "bad"}),
    ("/v1/rate-cards", {"valid_on": "bad"}),
    ("/v1/rate-cards", {"cursor": "garbage"}),
    ("/v1/sailings", {"etd_from": "bad"}),
    ("/v1/sailings", {"etd_to": "2026-02-30"}),
    ("/v1/online-orders", {"promise_before": "bad"}),
    ("/v1/online-orders", {"cursor": "%%%"}),
    ("/v1/factories/F-BD-01/schedule", {"from": "bad"}),
    ("/v1/factories/NOPE/schedule", {}),
    ("/v1/promotions", {"active_on": "bad"}),
    ("/v1/events", {"after_cursor": "bad"}),
    ("/v1/events", {"after_cursor": "ox:99999999999999999999"}),
    ("/v1/changes", {"watermark": "bad"}),
    ("/v1/capacity-bookings", {"iso_week": "bad"}),
    ("/v1/shipments", {"cursor": "eyJ4Ijox"}),
    ("/v1/production-orders", {"cursor": "bad"}),
    ("/v1/inventory-positions", {"aggregate": "bad"}),
    ("/v1/inventory-positions", {"aggregate": "cluster", "cursor": "bad"}),
    ("/v1/demand-forecast", {"aggregate": "bad"}),
    ("/v1/demand-forecast", {"week": "bad"}),
    ("/v1/sales-history", {"cursor": "bad"}),
    ("/v1/snapshots/not-a-uuid/sailing", {}),
    ("/v1/snapshots/00000000-0000-0000-0000-000000000000/nope", {}),
    ("/v1/webhook-subscriptions/not-a-uuid/deliveries", {}),
    ("/v1/webhook-deliveries", {"status": "weird"}),
]


@pytest.mark.parametrize(("path", "params"), BAD_PARAM_PROBES)
async def test_m2_endpoints_answer_malformed_parameters_without_5xx(
    http: httpx.AsyncClient, realm: FakeRealm, path: str, params: dict[str, Any]
) -> None:
    r = await http.get(path, params=params, headers=auth(admin(realm)))
    assert r.status_code < 500, (path, params, r.status_code, r.text[:300])
