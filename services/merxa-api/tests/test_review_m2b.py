"""Regression tests for defects found in the second pass of the independent M2 review."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import admin, auth, key, reader
from merxa_api import tables as t
from merxa_api.services import snapshots as snapshots_mod
from merxa_api.services.webhooks import Delivery, WebhookService
from support import READ, FakeRealm


async def test_port_closure_shifts_each_po_line_and_header_once(
    http: httpx.AsyncClient, realm: FakeRealm, engine: AsyncEngine, reseed: None
) -> None:
    """A PO (line) carried by two shipments on the closed sailing was delayed twice; the PO
    header change was neither audited nor reported to ``scn.changes_since``."""
    tok, adm = reader(realm), admin(realm)
    sh, sl = t.shipment, t.shipment_line
    async with engine.begin() as conn:
        ship = (await conn.execute(select(sh).where(sh.c.po_code == "PO-BD-0003"))).one()
        assert ship.sailing_code == "SAIL-0001"
        line = (await conn.execute(select(sl).where(sl.c.shipment_code == ship.code).order_by(sl.c.line_no))).first()
        values = {c: getattr(ship, c) for c in sh.c.keys() if c not in ("id", "code", "version", "updated_at")}
        await conn.execute(sh.insert().values(**values, code="SHP-REVIEW-2"))
        await conn.execute(
            sl.insert().values(
                shipment_code="SHP-REVIEW-2", line_no=1, po_line_ref=line.po_line_ref, sku_code=line.sku_code, qty=1
            )
        )
    mark = (await http.get("/v1/changes", headers=auth(tok))).json()["next_watermark"]

    async def header_delays() -> list[dict[str, Any]]:
        audit = (
            await http.get(
                "/v1/audit-log",
                params={"entity_type": "purchase_order", "entity_ref": "PO-BD-0003", "limit": 1000},
                headers=auth(tok),
            )
        ).json()["items"]
        return [a for a in audit if a["action"] == "delay"]

    seen = {a["id"] for a in await header_delays()}  # the audit log is append-only across tests
    r = await http.post(
        "/v1/admin/scenario:inject",
        json={"kind": "port_closure", "payload": {"unlocode": "BDCGP", "days": 7}},
        headers=auth(adm, key()),
    )
    assert r.status_code == 200, r.text
    assert r.json()["po_lines"] == 60  # each line once
    po = (await http.get("/v1/purchase-orders/PO-BD-0003", headers=auth(tok))).json()
    assert po["eta_dc"] == "2026-11-17"
    assert {ln["eta_dc"] for ln in po["lines"]} == {"2026-11-17"}
    shifts = [a for a in await header_delays() if a["id"] not in seen]
    assert len(shifts) == 1 and shifts[0]["after"]["eta_dc"] == "2026-11-17"
    changes = (await http.get("/v1/changes", params={"watermark": mark}, headers=auth(tok))).json()["changes"]
    refs = [c["key"] for c in changes if c["entity"] == "po_line"]
    assert line.po_line_ref in refs


@pytest.mark.parametrize(
    "extra",
    [
        {"days": 3, "line_nos": "1"},
        {"days": 3, "line_nos": ["one"]},
        {"days": 3, "line_nos": [True]},
        {"days": 10**6},
        {"days": -(10**6)},
    ],
)
async def test_po_eta_change_rejects_malformed_payloads(
    http: httpx.AsyncClient, realm: FakeRealm, extra: dict[str, Any]
) -> None:
    r = await http.post(
        "/v1/admin/scenario:inject",
        json={"kind": "po_eta_change", "payload": {"po_code": "PO-BD-0003", **extra}},
        headers=auth(admin(realm), key()),
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "invalid_parameter"


async def test_lite_update_rejects_non_string_dates(
    http: httpx.AsyncClient, realm: FakeRealm, lite_client: Any, reseed: None
) -> None:
    r = await http.post(
        "/v1/api-keys",
        json={"name": "lite-dates", "scopes": [*READ, "changes:apply"]},
        headers=auth(admin(realm), key()),
    )
    assert r.status_code == 201, r.text
    async with lite_client(api_key=r.json()["key"]) as c:
        item = (await c.call_tool("get_item", {"type": "purchase_order", "key": "PO-BD-0003"})).structured_content
        for bad in (20261201, True, ["2026-12-01"]):
            with pytest.raises(ToolError, match="eta_dc must be YYYY-MM-DD"):
                await c.call_tool(
                    "update_item",
                    {
                        "type": "purchase_order",
                        "key": "PO-BD-0003",
                        "fields_json": json.dumps({"eta_dc": bad}),
                        "expected_version": item["item"]["version"],
                    },
                )


class _EndlessBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.chunks += 1
            if self.chunks > 200:  # stands in for "never ends"
                raise httpx.ReadError("receiver kept streaming")
            yield b"x" * 65536

    async def aclose(self) -> None:
        return None


async def test_webhook_delivery_does_not_read_the_receiver_body(
    state: Any, settings: Any, engine: AsyncEngine, reseed: None
) -> None:
    """The response body was read in full: a receiver streaming without end grew merxa-api's
    memory (384 MiB limit) until the attempt timed out or the process was OOM-killed."""
    body = _EndlessBody()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body)

    svc = WebhookService(engine, settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with engine.begin() as conn:
        sub = (await conn.execute(select(t.webhook_subscription.c.id).limit(1))).scalar_one()
        await conn.execute(t.outbox.insert().values(event_type="inventory.adjusted", payload={"probe": True}))
        ev = (await conn.execute(select(t.outbox.c.id).order_by(t.outbox.c.seq.desc()).limit(1))).scalar_one()
        did = (
            await conn.execute(
                t.webhook_delivery.insert()
                .values(subscription_id=sub, event_id=ev, event_type="inventory.adjusted", status="pending")
                .returning(t.webhook_delivery.c.id)
            )
        ).scalar_one()
    try:
        assert await svc._deliver(Delivery(did, sub, ev, 0)) == "delivered"
        assert body.chunks <= 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(t.webhook_delivery.delete().where(t.webhook_delivery.c.id == did))
            await conn.execute(t.outbox.update().where(t.outbox.c.id == ev).values(published_at=date.today()))


async def test_snapshot_builds_run_one_at_a_time(
    http: httpx.AsyncClient, realm: FakeRealm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each build holds up to 50k fetched rows plus their Arrow table; parallel builds of the
    ``full`` profile push merxa-api past its 384 MiB memory limit."""
    active = peak = 0
    lock = threading.Lock()
    original = snapshots_mod._write_part

    def tracking(*args: Any) -> Any:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            import time

            time.sleep(0.05)
            return original(*args)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(snapshots_mod, "_write_part", tracking)
    tok = reader(realm)
    rs = await asyncio.gather(
        *(
            http.post("/v1/snapshots", json={"entities": ["sailing", "lane", "promotion"]}, headers=auth(tok))
            for _ in range(4)
        )
    )
    assert all(r.status_code == 201 for r in rs), [r.text for r in rs]
    assert peak == 1
