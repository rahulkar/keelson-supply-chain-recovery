"""Change-set engine end to end over REST: validate → apply, conflicts, replay, caps, audit."""

from __future__ import annotations

import httpx
import jwt
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import (
    auth,
    autocommit,
    build_change_set,
    dc_position,
    independent_hash,
    key,
    marco,
    po_line_version,
    preparer,
    reader,
)
from merxa_api import tables as t
from support import MARCO_SUB, FakeKeelson, FakeRealm


async def _planned_allocation(http: httpx.AsyncClient, token: str, offset: int = 0) -> dict:
    r = await http.get(
        "/v1/allocations", params={"status": "planned", "dc_code": "DE1", "limit": 20}, headers=auth(token)
    )
    return r.json()["items"][offset]


async def test_validate_apply_happy_path(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, engine: AsyncEngine, reseed: None
) -> None:
    prep = preparer(realm)
    v1 = await po_line_version(http, prep, "PO-BD-0001", 1)
    alloc = await _planned_allocation(http, prep)
    de1_before = await dc_position(http, prep, "DE1", "PUF-W-BLK-M")
    uk1_before = await dc_position(http, prep, "UK1", "PUF-W-BLK-M")
    items = [
        {
            "op": "transfer_order.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "from_dc": "DE1",
                "to_dc": "UK1",
                "ship_date": "2026-10-12",
                "lines": [{"sku": "PUF-W-BLK-M", "qty": 40}, {"sku": "PUF-W-BLK-L", "qty": 30}],
            },
        },
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0001#1",
            "expected_version": v1,
            "payload": {"eta_dc": "2026-11-05"},
        },
        {
            "op": "allocation.set_qty",
            "target_ref": alloc["code"],
            "expected_version": alloc["version"],
            "payload": {"qty": alloc["qty"] + 2},
        },
    ]
    validated = await build_change_set(http, prep, items, title="Peak cover UK1")
    assert validated["status"] == "validated", validated
    assert validated["preview_hash"] == independent_hash(items)
    costs = {
        s["code"]: s["unit_cost_cents"] for s in (await http.get("/v1/styles", headers=auth(prep))).json()["items"]
    }
    alloc_cost = costs[alloc["sku"].rsplit("-", 1)[0]]
    assert validated["totals"] == {"lines": 3, "units_moved": 72, "value_cents": 70 * 1150 + 2 * alloc_cost}
    to_item = validated["items"][0]
    assert to_item["before"]["lines"][0]["from_atp"] == de1_before["atp"]
    assert to_item["after"]["lines"][0]["from_atp"] == de1_before["atp"] - 40
    assert validated["items"][1]["after"]["eta_dc"] == "2026-11-05"
    cs_id, h = validated["change_set_id"], validated["preview_hash"]

    # Apply with Marco's delegated token + a user receipt.
    receipt = keelson.receipt(change_set_ref=cs_id, change_set_hash=h, ext_sub=MARCO_SUB, approval_id="APR-0001")
    k = key()
    body = {"preview_hash": h, "approval_ref": "APR-0001", "approval_receipt": receipt}
    r = await http.post(f"/v1/change-sets/{cs_id}:apply", json=body, headers=auth(marco(realm), k))
    assert r.status_code == 200, r.text
    applied = r.json()
    assert applied["status"] == "applied" and applied["applied"] == 3
    code = applied["change_set_code"]

    # Effects
    de1_after = await dc_position(http, prep, "DE1", "PUF-W-BLK-M")
    uk1_after = await dc_position(http, prep, "UK1", "PUF-W-BLK-M")
    assert de1_after["reserved"] == de1_before["reserved"] + 40 and de1_after["atp"] == de1_before["atp"] - 40
    assert uk1_after["in_transit"] == uk1_before["in_transit"] + 40
    assert await po_line_version(http, prep, "PO-BD-0001", 1) == v1 + 1
    tos = (await http.get("/v1/transfer-orders", params={"from_dc": "DE1", "to_dc": "UK1"}, headers=auth(prep))).json()
    created = [to for to in tos["items"] if to["created_via"] == f"change_set:{code}"]
    assert len(created) == 1 and created[0]["total_qty"] == 70 and created[0]["status"] == "planned"

    # Change set attribution
    cs = (await http.get(f"/v1/change-sets/{cs_id}", headers=auth(prep))).json()
    assert cs["status"] == "applied" and cs["approval_ref"] == "APR-0001" and cs["approved_by"] == "user:marco"
    assert cs["initiated_by"] == "Priya Shah" and cs["applied_by"]["client_id"] == "keelson-connect"
    assert cs["attribution_text"] == "Changed by Marco Rossi via Keelson (APR-0001)"
    assert all(it["status"] == "applied" for it in cs["items"])

    # Audit: one row per entity change, attributed, and the outbox event.
    rows = (await http.get("/v1/audit-log", params={"change_set_code": code, "limit": 100}, headers=auth(prep))).json()[
        "items"
    ]
    entity_rows = [r for r in rows if r["entity_type"] != "change_set"]
    # TO create + 2×(reserve, in_transit) + reschedule + allocation
    assert len(entity_rows) == 7
    for row in entity_rows:
        assert row["via"] == "Keelson" and row["approval_ref"] == "APR-0001" and row["approved_by"] == "user:marco"
        assert row["actor_sub"] == MARCO_SUB and row["actor_display"] == "Marco Rossi"
        assert row["actor_username"] == "marco.rossi" and row["client_id"] == "keelson-connect"
        assert row["initiated_by"] == "Priya Shah" and row["trace_id"]
    async with engine.connect() as conn:
        events = (
            await conn.execute(select(t.outbox.c.payload).where(t.outbox.c.event_type == "change_set.applied"))
        ).all()
    assert any(e.payload["code"] == code for e in events)

    # Idempotent replay: same key → same response, nothing applied twice — even with a re-minted receipt.
    receipt2 = keelson.receipt(change_set_ref=cs_id, change_set_hash=h, ext_sub=MARCO_SUB, approval_id="APR-0001")
    replay = await http.post(
        f"/v1/change-sets/{cs_id}:apply", json={**body, "approval_receipt": receipt2}, headers=auth(marco(realm), k)
    )
    assert replay.status_code == 200 and replay.json() == applied
    assert replay.headers.get("idempotent-replayed") == "true"
    assert (await dc_position(http, prep, "DE1", "PUF-W-BLK-M"))["reserved"] == de1_after["reserved"]
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                select(func.count())
                .select_from(t.transfer_order)
                .where(t.transfer_order.c.created_via == f"change_set:{code}")
            )
        ).scalar()
    assert n == 1

    # Same key, different request → 422; new key on an applied set → 409 already_applied.
    r = await http.post(
        f"/v1/change-sets/{cs_id}:apply", json={**body, "approval_ref": "APR-0002"}, headers=auth(marco(realm), k)
    )
    assert r.status_code == 422
    r = await http.post(f"/v1/change-sets/{cs_id}:apply", json=body, headers=auth(marco(realm), key()))
    assert r.status_code == 409 and r.json()["code"] == "already_applied"


async def test_conflict_rolls_back_everything(
    http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, engine: AsyncEngine, reseed: None
) -> None:
    prep = preparer(realm)
    v2 = await po_line_version(http, prep, "PO-BD-0002", 2)
    de1_before = await dc_position(http, prep, "DE1", "PUF-M-NVY-S")
    items = [
        {
            "op": "transfer_order.create",
            "payload": {
                "from_dc": "DE1",
                "to_dc": "NL1",
                "ship_date": "2026-10-13",
                "lines": [{"sku": "PUF-M-NVY-S", "qty": 5}],
            },
        },
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0002#2",
            "expected_version": v2,
            "payload": {"eta_dc": "2026-11-10"},
        },
    ]
    validated = await build_change_set(http, prep, items)
    assert validated["status"] == "validated"
    cs_id, h = validated["change_set_id"], validated["preview_hash"]

    # Someone edits the PO line in Merxa after validation (version bump).
    async with engine.begin() as conn:
        await conn.execute(
            update(t.po_line)
            .where(t.po_line.c.po_code == "PO-BD-0002", t.po_line.c.line_no == 2)
            .values(qty_received=0)
        )
    receipt = keelson.receipt(change_set_ref=cs_id, change_set_hash=h, approval_id="APR-0101")
    k = key()
    body = {"preview_hash": h, "approval_ref": "APR-0101", "approval_receipt": receipt}
    r = await http.post(f"/v1/change-sets/{cs_id}:apply", json=body, headers=auth(marco(realm), k))
    assert r.status_code == 409, r.text
    doc = r.json()
    assert doc["status"] == "conflict" and r.headers["content-type"].startswith("application/problem+json")
    assert doc["conflicts"] == [
        {
            "seq": 2,
            "target_ref": "PO-BD-0002#2",
            "expected_version": v2,
            "actual_version": v2 + 1,
            "reason": "version_mismatch",
        }
    ]
    # Nothing applied: the TO item (seq 1, valid on its own) was rolled back too.
    assert (await dc_position(http, prep, "DE1", "PUF-M-NVY-S"))["reserved"] == de1_before["reserved"]
    cs = (await http.get(f"/v1/change-sets/{cs_id}", headers=auth(prep))).json()
    assert cs["status"] == "conflict"
    assert [it["status"] for it in cs["items"]] == ["ok", "error"]
    async with engine.connect() as conn:
        tos = (
            await conn.execute(
                select(func.count())
                .select_from(t.transfer_order)
                .where(t.transfer_order.c.created_via == f"change_set:{cs['code']}")
            )
        ).scalar()
    assert tos == 0
    # Replaying the same key returns the same conflict outcome.
    again = await http.post(f"/v1/change-sets/{cs_id}:apply", json=body, headers=auth(marco(realm), k))
    assert again.status_code == 409 and again.json()["conflicts"] == doc["conflicts"]


async def test_apply_guards(http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, reseed: None) -> None:
    prep = preparer(realm)
    items = [
        {
            "op": "transfer_order.create",
            "payload": {
                "from_dc": "PL1",
                "to_dc": "NL1",
                "ship_date": "2026-10-14",
                "lines": [{"sku": "PUF-W-KHK-S", "qty": 3}],
            },
        }
    ]
    validated = await build_change_set(http, prep, items)
    cs_id, h = validated["change_set_id"], validated["preview_hash"]
    url = f"/v1/change-sets/{cs_id}:apply"

    def body(receipt: str, preview: str = h) -> dict:
        return {"preview_hash": preview, "approval_ref": "APR-0201", "approval_receipt": receipt}

    good = keelson.receipt(change_set_ref=cs_id, change_set_hash=h, approval_id="APR-0201")
    # Preparer may not apply at all.
    assert (await http.post(url, json=body(good), headers=auth(prep, key()))).status_code == 403
    # Wrong preview hash.
    r = await http.post(url, json=body(good, "0" * 64), headers=auth(marco(realm), key()))
    assert r.status_code == 409 and r.json()["code"] == "preview_hash_mismatch"
    # Receipt bound to another subject.
    other = keelson.receipt(change_set_ref=cs_id, change_set_hash=h, approval_id="APR-0201", ext_sub="not-marco")
    r = await http.post(url, json=body(other), headers=auth(marco(realm), key()))
    assert r.status_code == 403 and r.json()["reason"] == "ext_sub_mismatch"
    # A planner token with changes:apply but without ops-director role.
    planner = realm.token(sub=MARCO_SUB, azp="keelson-connect", scopes=["changes:apply"], roles=["merch-planner"])
    r = await http.post(url, json=body(good), headers=auth(planner, key()))
    assert r.status_code == 403 and r.json()["reason"] == "missing_role"
    # Failed attempts stored nothing: the set is still applicable.
    assert (await http.post(url, json=body(good), headers=auth(marco(realm), key()))).status_code == 200
    # A receipt jti cannot be reused for a different change set.
    validated2 = await build_change_set(http, prep, items)
    jti = jwt.decode(good, options={"verify_signature": False})["jti"]
    reused = keelson.receipt(
        change_set_ref=validated2["change_set_id"],
        change_set_hash=validated2["preview_hash"],
        approval_id="APR-0201",
        overrides={"jti": jti},
    )
    r = await http.post(
        f"/v1/change-sets/{validated2['change_set_id']}:apply",
        json=body(reused, validated2["preview_hash"]),
        headers=auth(marco(realm), key()),
    )
    assert r.status_code == 403 and r.json()["reason"] == "replayed_jti"


async def test_validation_reports_item_errors(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    prep = preparer(realm)
    v = await po_line_version(http, prep, "PO-BD-0003", 1)
    items = [
        {
            "op": "transfer_order.create",
            "payload": {
                "from_dc": "UK1",
                "to_dc": "NL1",
                "ship_date": "2026-10-12",
                "lines": [{"sku": "PUF-W-BLK-M", "qty": 999_999}],
            },
        },
        {"op": "po_line.teleport", "target_ref": "PO-BD-0003#1", "expected_version": v, "payload": {}},
        {"op": "po_line.change_qty", "target_ref": "PO-BD-0003#1", "expected_version": v + 5, "payload": {"qty": 10}},
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-KH-0001#1",
            "expected_version": 1,
            "payload": {"eta_dc": "2026-12-01"},
        },
    ]
    validated = await build_change_set(http, prep, items)
    assert validated["status"] == "invalid"
    errs = {it["seq"]: it["errors"] for it in validated["items"]}
    assert any("insufficient ATP" in e for e in errs[1])
    assert errs[2] == ["unsupported_op"]
    assert any(e.startswith("version_mismatch") for e in errs[3])
    assert any("does not allow" in e for e in errs[4])
    cs = (await http.get(f"/v1/change-sets/{validated['change_set_id']}", headers=auth(prep))).json()
    assert cs["status"] == "draft" and cs["preview_hash"] is None


async def test_cumulative_atp_across_items(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    prep = preparer(realm)
    atp = (await dc_position(http, prep, "PL1", "PUF-W-CRM-XS"))["atp"]
    half = atp // 2 + 1
    line = {"from_dc": "PL1", "ship_date": "2026-10-12", "lines": [{"sku": "PUF-W-CRM-XS", "qty": half}]}
    items = [
        {"op": "transfer_order.create", "payload": {**line, "to_dc": "UK1"}},
        {"op": "transfer_order.create", "payload": {**line, "to_dc": "NL1"}},
    ]
    validated = await build_change_set(http, prep, items)
    assert validated["status"] == "invalid"
    assert validated["items"][0]["ok"] and not validated["items"][1]["ok"]


async def test_autocommit_caps(http: httpx.AsyncClient, realm: FakeRealm, keelson: FakeKeelson, reseed: None) -> None:
    prep = preparer(realm)
    auto = autocommit(realm)

    def policy_receipt(cs_id: str, h: str, ref: str) -> str:
        return keelson.receipt(
            change_set_ref=cs_id,
            change_set_hash=h,
            approval_id=ref,
            approved_by="policy:P-12@3",
            ext_sub="svc-autocommit",
        )

    small = [
        {
            "op": "transfer_order.create",
            "payload": {
                "from_dc": "DE1",
                "to_dc": "UK1",
                "ship_date": "2026-10-15",
                "lines": [{"sku": "PUF-M-BLK-L", "qty": 25}],
            },
        }
    ]
    v = await build_change_set(http, prep, small)
    r = await http.post(
        f"/v1/change-sets/{v['change_set_id']}:apply",
        json={
            "preview_hash": v["preview_hash"],
            "approval_ref": "APR-0301",
            "approval_receipt": policy_receipt(v["change_set_id"], v["preview_hash"], "APR-0301"),
        },
        headers=auth(auto, key()),
    )
    assert r.status_code == 200, r.text
    cs = (await http.get(f"/v1/change-sets/{v['change_set_id']}", headers=auth(prep))).json()
    assert cs["attribution_text"] == "Applied via Keelson auto-policy P-12@3, initiated by Priya Shah"

    # A policy receipt with the delegated (non-autocommit) client is rejected.
    v2 = await build_change_set(http, prep, small)
    r = await http.post(
        f"/v1/change-sets/{v2['change_set_id']}:apply",
        json={
            "preview_hash": v2["preview_hash"],
            "approval_ref": "APR-0302",
            "approval_receipt": policy_receipt(v2["change_set_id"], v2["preview_hash"], "APR-0302"),
        },
        headers=auth(marco(realm), key()),
    )
    assert r.status_code == 403 and r.json()["reason"] == "wrong_client"

    # Op outside the allowlist → auto_cap_exceeded.
    ver = await po_line_version(http, prep, "PO-BD-0006", 3)
    resched = [
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0006#3",
            "expected_version": ver,
            "payload": {"eta_dc": "2026-11-17"},
        }
    ]
    v3 = await build_change_set(http, prep, resched)
    r = await http.post(
        f"/v1/change-sets/{v3['change_set_id']}:apply",
        json={
            "preview_hash": v3["preview_hash"],
            "approval_ref": "APR-0303",
            "approval_receipt": policy_receipt(v3["change_set_id"], v3["preview_hash"], "APR-0303"),
        },
        headers=auth(auto, key()),
    )
    assert r.status_code == 403 and r.json()["code"] == "auto_cap_exceeded"

    # Too many units → auto_cap_exceeded.
    de1 = (
        await http.get(
            "/v1/inventory-positions",
            params={"location_type": "dc", "location_code": "DE1", "limit": 30},
            headers=auth(reader(realm)),
        )
    ).json()["items"]
    big_lines = [{"sku": p["sku"], "qty": min(p["atp"], 400)} for p in de1 if p["atp"] > 0]
    assert sum(ln["qty"] for ln in big_lines) > 5000
    v4 = await build_change_set(
        http,
        prep,
        [
            {
                "op": "transfer_order.create",
                "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-15", "lines": big_lines},
            }
        ],
    )
    assert v4["status"] == "validated", v4
    r = await http.post(
        f"/v1/change-sets/{v4['change_set_id']}:apply",
        json={
            "preview_hash": v4["preview_hash"],
            "approval_ref": "APR-0304",
            "approval_receipt": policy_receipt(v4["change_set_id"], v4["preview_hash"], "APR-0304"),
        },
        headers=auth(auto, key()),
    )
    assert r.status_code == 403 and r.json()["code"] == "auto_cap_exceeded"


async def test_add_items_resets_validation_and_guide(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    prep = preparer(realm)
    items = [
        {
            "op": "transfer_order.create",
            "payload": {
                "from_dc": "DE1",
                "to_dc": "NL1",
                "ship_date": "2026-10-12",
                "lines": [{"sku": "PUF-W-BLK-S", "qty": 7}],
            },
        }
    ]
    v = await build_change_set(http, prep, items)
    cs_id = v["change_set_id"]
    alloc = await _planned_allocation(http, prep, 3)
    more = [
        {
            "op": "allocation.set_qty",
            "target_ref": alloc["code"],
            "expected_version": alloc["version"],
            "payload": {"qty": 0},
        }
    ]
    r = await http.post(f"/v1/change-sets/{cs_id}/items", json={"items": more}, headers=auth(prep, key()))
    assert r.json()["items_count"] == 2
    cs = (await http.get(f"/v1/change-sets/{cs_id}", headers=auth(prep))).json()
    assert cs["status"] == "draft" and cs["preview_hash"] is None

    guide = (await http.get(f"/v1/change-sets/{cs_id}/operator-guide", headers=auth(prep))).json()
    assert [s["item_seq"] for s in guide["steps"]] == [1, 2]
    assert guide["steps"][0]["deep_link"].startswith("http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1")
    assert "PUF-W-BLK-S%3A7" in guide["steps"][0]["deep_link"]
