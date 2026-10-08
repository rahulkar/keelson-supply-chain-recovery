"""M2 change-set ops (validation, before/after, audit, outbox) and rollback via compensating sets."""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import (
    apply_validated,
    auth,
    build_change_set,
    dc_position,
    key,
    marco,
    po_line_version,
    preparer,
    reader,
    rollback,
)
from merxa_api import tables as t
from support import FakeKeelson, FakeRealm


async def _po_line(http: httpx.AsyncClient, token: str, po: str, line_no: int) -> dict[str, Any]:
    r = await http.get(f"/v1/purchase-orders/{po}", headers=auth(token))
    return next(ln for ln in r.json()["lines"] if ln["line_no"] == line_no)


async def _promo(http: httpx.AsyncClient, token: str, code: str) -> dict[str, Any]:
    items = (await http.get("/v1/promotions", headers=auth(token))).json()["items"]
    return next(p for p in items if p["code"] == code)


async def _order(http: httpx.AsyncClient, token: str, code: str) -> dict[str, Any]:
    items = (await http.get("/v1/online-orders", params={"limit": 200}, headers=auth(token))).json()["items"]
    return next(o for o in items if o["code"] == code)


async def _schedule(http: httpx.AsyncClient, token: str, line: str) -> list[dict[str, Any]]:
    r = await http.get("/v1/production-orders", params={"line_code": line, "limit": 1000}, headers=auth(token))
    return sorted(r.json()["items"], key=lambda o: o["seq"])


async def _line_version(http: httpx.AsyncClient, token: str, line: str) -> int:
    r = await http.get("/v1/factory-lines", params={"factory_code": "F-BD-01"}, headers=auth(token))
    return next(fl["version"] for fl in r.json()["items"] if fl["code"] == line)


async def _snapshot(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """The state every op in the big rollback test touches."""
    sched = await _schedule(http, token, "FL-BD01-A")
    bookings = (await http.get("/v1/capacity-bookings", params={"limit": 1000}, headers=auth(token))).json()["items"]
    shipments = (await http.get("/v1/shipments", params={"limit": 1000}, headers=auth(token))).json()["items"]
    pos = (await http.get("/v1/purchase-orders", params={"limit": 1000}, headers=auth(token))).json()["items"]
    tos = (await http.get("/v1/transfer-orders", params={"limit": 1000}, headers=auth(token))).json()["items"]
    po3 = (await http.get("/v1/purchase-orders/PO-BD-0003", headers=auth(token))).json()
    return {
        "po3_lines": [(ln["line_no"], ln["qty_ordered"], ln["eta_dc"], ln["status"]) for ln in po3["lines"]],
        "po1_line1": (await _po_line(http, token, "PO-BD-0001", 1))["eta_dc"],
        "po2_line1": (await _po_line(http, token, "PO-BD-0002", 1))["qty_ordered"],
        "promo": (await _promo(http, token, "PROMO-BF26"))["period_start"],
        "order": (await _order(http, token, "OO-000001"))["promise_date"],
        "schedule": [(o["code"], o["seq"], o["planned_start"], o["planned_end"]) for o in sched],
        "active_bookings": sorted(b["code"] for b in bookings if b["status"] != "released"),
        "active_shipments": sorted(s["code"] for s in shipments if s["status"] != "cancelled"),
        "sea_shipment_qty": [
            ln["qty"]
            for s in shipments
            if s["code"] == "SHP-000003"
            for ln in s["lines"]
            if ln["po_line_ref"] == "PO-BD-0003#2"
        ],
        "open_pos": sorted(p["code"] for p in pos if p["status"] != "cancelled"),
        "planned_tos": sorted(o["code"] for o in tos if o["status"] == "planned"),
        "de1": await dc_position(http, token, "DE1", "PUF-W-BLK-M"),
        "uk1": await dc_position(http, token, "UK1", "PUF-W-BLK-M"),
    }


async def test_new_ops_validate_with_before_after(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    prep = preparer(realm)
    l3_1 = await _po_line(http, prep, "PO-BD-0003", 1)
    l3_2 = await _po_line(http, prep, "PO-BD-0003", 2)
    promo = await _promo(http, prep, "PROMO-BF26")
    order = await _order(http, prep, "OO-000001")
    sched = await _schedule(http, prep, "FL-BD01-A")
    movable = [o["code"] for o in sched if o["status"] in ("planned", "held")]
    assert len(movable) >= 2
    items = [
        {"op": "po_line.cancel", "target_ref": "PO-BD-0003#1", "expected_version": l3_1["version"], "payload": {}},
        {
            "op": "po_line.split",
            "target_ref": "PO-BD-0003#2",
            "expected_version": l3_2["version"],
            "payload": {"mode": "sea_air", "qty": 5, "lane_code": "CGP-UK1-SA"},
        },
        {
            "op": "capacity_booking.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {"lane_code": "CGP-NL1-SA", "iso_week": "2026-W43", "qty_units": 500},
        },
        {
            "op": "production_order.resequence",
            "target_ref": "FL-BD01-A",
            "expected_version": await _line_version(http, prep, "FL-BD01-A"),
            "payload": {"line_code": "FL-BD01-A", "order_codes": list(reversed(movable))},
        },
        {
            "op": "promotion.reschedule",
            "target_ref": "PROMO-BF26",
            "expected_version": promo["version"],
            "payload": {"period_start": "2026-11-30", "period_end": "2026-12-06"},
        },
        {
            "op": "online_order.repromise",
            "target_ref": "OO-000001",
            "expected_version": order["version"],
            "payload": {"promise_date": "2026-12-04"},
        },
        {
            "op": "po.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "supplier_code": "CMT-BD-01",
                "factory_code": "F-BD-01",
                "dest_dc": "UK1",
                "eta_dc": "2026-11-20",
                "lines": [{"sku": "PUF-W-BLK-M", "qty": 300}, {"sku": "PUF-W-BLK-L", "qty": 200}],
            },
        },
    ]
    v = await build_change_set(http, prep, items, title="M2 ops")
    assert v["status"] == "validated", [it["errors"] for it in v["items"]]
    by_op = {it["op"]: it for it in v["items"]}
    assert by_op["po_line.cancel"]["after"]["status"] == "cancelled"
    split = by_op["po_line.split"]["after"]["split"]
    assert split["eta_dc"] == "2026-10-22" and split["days_earlier"] == 19 and split["freight_cents"] == 5 * 145
    assert by_op["capacity_booking.create"]["after"]["remaining_capacity"] >= 0
    reseq = by_op["production_order.resequence"]["after"]["orders"]
    assert [o["code"] for o in reseq] == list(reversed(movable))
    assert by_op["promotion.reschedule"]["before"]["period_start"] == promo["period_start"]
    assert by_op["online_order.repromise"]["after"]["promise_date"] == "2026-12-04"
    po_after = by_op["po.create"]["after"]["purchase_order"]
    assert po_after["ex_factory_date"] == "2026-11-04" and po_after["transit_days"] == 16
    # units: cancel (open qty) + split 5 + po.create 500
    assert v["totals"]["units_moved"] == l3_1["qty_ordered"] - l3_1["qty_received"] + 5 + 500


async def test_new_ops_reject_bad_input(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    prep = preparer(realm)
    l1 = await _po_line(http, prep, "PO-BD-0001", 3)
    cases: list[tuple[dict[str, Any], str]] = [
        (
            {
                "op": "po_line.split",
                "target_ref": "PO-BD-0001#3",
                "expected_version": l1["version"],
                "payload": {"mode": "sea_air", "qty": l1["qty_ordered"], "lane_code": "CGP-UK1-SA"},
            },
            "smaller than the open quantity",
        ),
        (
            {
                "op": "po_line.split",
                "target_ref": "PO-BD-0001#3",
                "expected_version": l1["version"],
                "payload": {"mode": "sea_air", "qty": 1, "lane_code": "CGP-NL1-SA"},
            },
            "the PO needs BDCGP → UK1",
        ),
        (
            {
                "op": "capacity_booking.create",
                "target_ref": None,
                "expected_version": None,
                "payload": {"lane_code": "CGP-FXT-SEA", "iso_week": "2026-W43", "qty_units": 10},
            },
            "use its sailings",
        ),
        (
            {
                "op": "capacity_booking.create",
                "target_ref": None,
                "expected_version": None,
                "payload": {"lane_code": "CGP-UK1-SA", "iso_week": "2026-W43", "qty_units": 10_000_000},
            },
            "insufficient capacity",
        ),
        (
            {
                "op": "po.create",
                "target_ref": None,
                "expected_version": None,
                "payload": {
                    "supplier_code": "CMT-BD-01",
                    "factory_code": "F-BD-01",
                    "dest_dc": "UK1",
                    "eta_dc": "2026-10-15",
                    "lines": [{"sku": "PUF-W-BLK-M", "qty": 1}],
                },
            },
            "not reachable",
        ),
        (
            {
                "op": "po.create",
                "target_ref": None,
                "expected_version": None,
                "payload": {
                    "supplier_code": "CMT-KH-01",
                    "factory_code": "F-BD-01",
                    "dest_dc": "UK1",
                    "eta_dc": "2026-12-15",
                    "lines": [{"sku": "PUF-W-BLK-M", "qty": 1}],
                },
            },
            "belongs to supplier",
        ),
        (
            {
                "op": "promotion.reschedule",
                "target_ref": "PROMO-BF26",
                "expected_version": 1,
                "payload": {"period_start": "2026-12-06", "period_end": "2026-11-30"},
            },
            "must not be before",
        ),
        (
            {
                "op": "online_order.repromise",
                "target_ref": "OO-000001",
                "expected_version": 99,
                "payload": {"promise_date": "2026-12-01"},
            },
            "version_mismatch",
        ),
        (
            {
                "op": "production_order.resequence",
                "target_ref": None,
                "expected_version": None,
                "payload": {"line_code": "FL-BD01-A", "order_codes": ["MO-000001"]},
            },
            "cannot move",
        ),
        (
            {"op": "po_line.cancel", "target_ref": "PO-KH-0001#1", "expected_version": 1, "payload": {}},
            "cannot be cancelled",
        ),
        (
            {"op": "revert.transfer_order", "target_ref": "TO-000001", "expected_version": 1, "payload": {}},
            "unsupported_op",
        ),
    ]
    for item, needle in cases:
        v = await build_change_set(http, prep, [item], title="bad")
        assert v["status"] == "invalid", item
        errors = " ".join(v["items"][0]["errors"])
        assert needle in errors, (item["op"], errors)


async def test_apply_m2_ops_then_rollback_restores_state(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, engine: AsyncEngine, reseed: None
) -> None:
    prep = preparer(realm)
    before = await _snapshot(http, prep)
    l3_1 = await _po_line(http, prep, "PO-BD-0003", 1)
    l3_2 = await _po_line(http, prep, "PO-BD-0003", 2)
    sched = await _schedule(http, prep, "FL-BD01-A")
    movable = [o["code"] for o in sched if o["status"] in ("planned", "held")]
    items = [
        {"op": "po_line.cancel", "target_ref": "PO-BD-0003#1", "expected_version": l3_1["version"], "payload": {}},
        {
            "op": "po_line.split",
            "target_ref": "PO-BD-0003#2",
            "expected_version": l3_2["version"],
            "payload": {"mode": "sea_air", "qty": 7, "lane_code": "CGP-UK1-SA"},
        },
        {
            "op": "capacity_booking.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {"lane_code": "CGP-NL1-SA", "iso_week": "2026-W44", "qty_units": 400},
        },
        {
            "op": "production_order.resequence",
            "target_ref": "FL-BD01-A",
            "expected_version": await _line_version(http, prep, "FL-BD01-A"),
            "payload": {"line_code": "FL-BD01-A", "order_codes": list(reversed(movable))},
        },
        {
            "op": "promotion.reschedule",
            "target_ref": "PROMO-BF26",
            "expected_version": (await _promo(http, prep, "PROMO-BF26"))["version"],
            "payload": {"period_start": "2026-11-30", "period_end": "2026-12-06"},
        },
        {
            "op": "online_order.repromise",
            "target_ref": "OO-000001",
            "expected_version": (await _order(http, prep, "OO-000001"))["version"],
            "payload": {"promise_date": "2026-12-04"},
        },
        {
            "op": "po.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "supplier_code": "CMT-BD-01",
                "factory_code": "F-BD-02",
                "dest_dc": "NL1",
                "eta_dc": "2026-11-25",
                "lines": [{"sku": "PUF-M-BLK-M", "qty": 150}],
            },
        },
        {
            "op": "transfer_order.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "from_dc": "DE1",
                "to_dc": "UK1",
                "ship_date": "2026-10-12",
                "lines": [{"sku": "PUF-W-BLK-M", "qty": 25}],
            },
        },
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0001#1",
            "expected_version": await po_line_version(http, prep, "PO-BD-0001", 1),
            "payload": {"eta_dc": "2026-11-06"},
        },
        {
            "op": "po_line.change_qty",
            "target_ref": "PO-BD-0002#1",
            "expected_version": await po_line_version(http, prep, "PO-BD-0002", 1),
            "payload": {"qty": before["po2_line1"] + 11},
        },
    ]
    v = await build_change_set(http, prep, items, title="M2 apply + rollback")
    assert v["status"] == "validated", [it["errors"] for it in v["items"]]
    r = await apply_validated(http, realm, keelson, v, "APR-M2-1")
    assert r.status_code == 200, r.text

    after = await _snapshot(http, prep)
    assert after["po3_lines"][0][3] == "cancelled"
    assert len(after["po3_lines"]) == len(before["po3_lines"]) + 1  # split line
    new_line = after["po3_lines"][-1]
    assert new_line[1] == 7 and new_line[2] == "2026-10-22"
    assert after["po3_lines"][1][1] == before["po3_lines"][1][1] - 7
    assert after["sea_shipment_qty"] == [before["sea_shipment_qty"][0] - 7]
    assert after["promo"] == "2026-11-30" and after["order"] == "2026-12-04"
    assert [c for c, *_ in after["schedule"] if c in movable] != [c for c, *_ in before["schedule"] if c in movable]
    assert len(after["active_bookings"]) == len(before["active_bookings"]) + 2  # booking + split's booking
    assert len(after["open_pos"]) == len(before["open_pos"]) + 1
    assert len(after["planned_tos"]) == len(before["planned_tos"]) + 1
    assert after["de1"]["reserved"] == before["de1"]["reserved"] + 25

    cs = (await http.get(f"/v1/change-sets/{v['change_set_id']}", headers=auth(prep))).json()
    assert all(it["status"] == "applied" for it in cs["items"])
    split_item = next(it for it in cs["items"] if it["op"] == "po_line.split")
    assert split_item["after"]["split"]["lane_code"] == "CGP-UK1-SA"
    audit = (
        await http.get("/v1/audit-log", params={"change_set_code": cs["code"], "limit": 500}, headers=auth(prep))
    ).json()["items"]
    actions = {(a["entity_type"], a["action"]) for a in audit}
    assert {
        ("po_line", "cancel"),
        ("po_line", "split"),
        ("capacity_booking", "create"),
        ("shipment", "create"),
        ("production_order", "resequence"),
        ("promotion", "reschedule"),
        ("online_order", "repromise"),
        ("purchase_order", "create"),
    } <= actions
    assert all(
        a["approval_ref"] == "APR-M2-1" and a["via"] == "Keelson" for a in audit if a["entity_type"] != "change_set"
    )

    # A policy receipt cannot roll back; a user receipt can.
    r = await rollback(http, realm, keelson, v, "APR-RB-POL", approved_by="policy:P-1@1")
    assert r.status_code == 403, r.text
    r = await rollback(http, realm, keelson, v, "APR-RB-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rolled_back" and body["change_set_code"] == cs["code"]
    assert body["compensating_code"].startswith("CS-") and body["applied"] >= len(items)

    restored = await _snapshot(http, prep)
    for field in (
        "po1_line1",
        "po2_line1",
        "promo",
        "order",
        "schedule",
        "active_bookings",
        "active_shipments",
        "open_pos",
        "planned_tos",
        "sea_shipment_qty",
    ):
        assert restored[field] == before[field], field
    assert [ln[:4] for ln in restored["po3_lines"][: len(before["po3_lines"])]] == before["po3_lines"]
    assert restored["po3_lines"][-1][3] == "cancelled"  # the split line is cancelled, not deleted
    for dc in ("de1", "uk1"):
        for f in ("on_hand", "allocated", "reserved", "in_transit", "atp"):
            assert restored[dc][f] == before[dc][f], (dc, f)

    orig = (await http.get(f"/v1/change-sets/{v['change_set_id']}", headers=auth(prep))).json()
    comp = (await http.get(f"/v1/change-sets/{body['compensating_change_set_id']}", headers=auth(prep))).json()
    assert orig["status"] == "rolled_back"
    assert comp["status"] == "applied" and comp["rollback_of"] == v["change_set_id"]
    assert comp["approval_ref"] == "APR-RB-1" and comp["via"] == "Keelson"
    assert {it["op"] for it in comp["items"]} >= {
        "revert.transfer_order",
        "revert.po_create",
        "revert.po_line_split",
        "revert.capacity_booking",
        "revert.production_schedule",
    }
    # second rollback → 409; replay with the same idempotency key is not needed here
    r = await rollback(http, realm, keelson, v, "APR-RB-2")
    assert r.status_code == 409 and r.json()["code"] == "already_rolled_back"
    # outbox: rolled_back event with scn changes
    async with engine.connect() as conn:
        ev = (
            await conn.execute(
                select(t.outbox.c.payload, t.outbox.c.changes)
                .where(t.outbox.c.event_type == "change_set.rolled_back")
                .order_by(t.outbox.c.seq.desc())
                .limit(1)
            )
        ).one()
    assert ev.payload["code"] == cs["code"] and ev.payload["compensating_code"] == body["compensating_code"]
    assert {c["entity"] for c in ev.changes} >= {"po_line", "inventory_position", "promotion", "production_order"}


async def test_rollback_conflicts_when_records_changed(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, engine: AsyncEngine, reseed: None
) -> None:
    prep = preparer(realm)
    promo = await _promo(http, prep, "PROMO-WW26")
    items = [
        {
            "op": "promotion.reschedule",
            "target_ref": "PROMO-WW26",
            "expected_version": promo["version"],
            "payload": {"period_start": "2026-11-16", "period_end": "2026-11-29"},
        },
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0004#2",
            "expected_version": await po_line_version(http, prep, "PO-BD-0004", 2),
            "payload": {"eta_dc": "2026-11-02"},
        },
    ]
    v = await build_change_set(http, prep, items)
    assert (await apply_validated(http, realm, keelson, v)).status_code == 200
    # somebody else touches the PO line afterwards
    async with engine.begin() as conn:
        await conn.execute(
            update(t.po_line).where(t.po_line.c.po_code == "PO-BD-0004", t.po_line.c.line_no == 2).values(qty_ordered=1)
        )
    r = await rollback(http, realm, keelson, v)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == "conflict"
    assert [c["reason"] for c in body["conflicts"]] == ["version_mismatch"]
    assert body["conflicts"][0]["target_ref"] == "PO-BD-0004#2"
    # nothing was rolled back
    assert (await _promo(http, prep, "PROMO-WW26"))["period_start"] == "2026-11-16"
    cs = (await http.get(f"/v1/change-sets/{v['change_set_id']}", headers=auth(prep))).json()
    assert cs["status"] == "applied"


async def test_rollback_requires_apply_scope_and_role(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, reseed: None
) -> None:
    prep = preparer(realm)
    promo = await _promo(http, prep, "PROMO-BF26")
    v = await build_change_set(
        http,
        prep,
        [
            {
                "op": "promotion.reschedule",
                "target_ref": "PROMO-BF26",
                "expected_version": promo["version"],
                "payload": {"period_start": "2026-11-24", "period_end": "2026-11-30"},
            }
        ],
    )
    # not applied yet → invalid state
    r = await rollback(http, realm, keelson, v)
    assert r.status_code == 409 and r.json()["code"] == "invalid_state"
    assert (await apply_validated(http, realm, keelson, v)).status_code == 200
    receipt = keelson.receipt(change_set_ref=v["change_set_id"], change_set_hash=v["preview_hash"], approval_id="APR-X")
    body = {"approval_ref": "APR-X", "approval_receipt": receipt}
    r = await http.post(f"/v1/change-sets/{v['change_set_id']}:rollback", json=body, headers=auth(reader(realm), key()))
    assert r.status_code == 403
    # receipt hash must be the original preview_hash
    bad = keelson.receipt(change_set_ref=v["change_set_id"], change_set_hash="0" * 64, approval_id="APR-Y")
    r = await http.post(
        f"/v1/change-sets/{v['change_set_id']}:rollback",
        json={"approval_ref": "APR-Y", "approval_receipt": bad},
        headers=auth(marco(realm), key()),
    )
    assert r.status_code == 403 and r.json()["code"] == "invalid_receipt"
    # idempotent replay
    k = key()
    good = keelson.receipt(change_set_ref=v["change_set_id"], change_set_hash=v["preview_hash"], approval_id="APR-Z")
    first = await http.post(
        f"/v1/change-sets/{v['change_set_id']}:rollback",
        json={"approval_ref": "APR-Z", "approval_receipt": good},
        headers=auth(marco(realm), k),
    )
    again = await http.post(
        f"/v1/change-sets/{v['change_set_id']}:rollback",
        json={"approval_ref": "APR-Z", "approval_receipt": good},
        headers=auth(marco(realm), k),
    )
    assert first.status_code == again.status_code == 200
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == first.json()


async def test_rollback_over_mcp(
    mcp_client: Any, http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, reseed: None
) -> None:
    prep = preparer(realm)
    order = await _order(http, prep, "OO-000002")
    v = await build_change_set(
        http,
        prep,
        [
            {
                "op": "online_order.repromise",
                "target_ref": "OO-000002",
                "expected_version": order["version"],
                "payload": {"promise_date": "2026-12-02"},
            }
        ],
    )
    assert (await apply_validated(http, realm, keelson, v)).status_code == 200
    receipt = keelson.receipt(
        change_set_ref=v["change_set_id"], change_set_hash=v["preview_hash"], approval_id="APR-MCP-RB"
    )
    args = {
        "change_set_id": v["code"],
        "approval_ref": "APR-MCP-RB",
        "approval_receipt": receipt,
        "idempotency_key": key(),
    }
    async with mcp_client(prep) as c:
        try:
            await c.call_tool("rollback_change_set", args)
            raise AssertionError("preparer must not roll back")
        except ToolError as exc:
            assert "insufficient_scope" in str(exc)
    async with mcp_client(marco(realm)) as c:
        res = await c.call_tool("rollback_change_set", args)
    data = res.structured_content
    assert data["status"] == "rolled_back" and data["change_set_code"] == v["code"]
    assert (await _order(http, prep, "OO-000002"))["promise_date"] == order["promise_date"]
