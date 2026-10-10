"""Rollback: a compensating change set restores the business columns (versions keep counting up);
anything changed since makes it a conflict; the receipt rules of a rollback."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from conftest import FakeKeelson, Running
from helpers import TO_ITEM, apply, call, prepare, rollback
from test_changesets import alloc, external_update, row


async def test_rollback_restores_business_columns(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    a = await alloc(run)
    rtm0 = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    al0 = await row(run, "store_allocations", code="AL-0003")
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM, a])
        await apply(c, keelson, v)
        out = await rollback(c, keelson, v, co_approved_by=())
        assert out["status"] == "rolled_back" and out["change_set_code"] == "CS-0001"
        assert out["compensating_code"] == "CS-0002" and out["applied"] == 8  # 2 inserts+2 lines+... effects
        orig = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
        comp = await call(c, "get_change_set", {"change_set_id": "CS-0002"})
        assert orig["status"] == "rolled_back" and orig["rolled_back_by"] == "CS-0002"
        assert comp["status"] == "applied" and comp["rollback_of"] == "CS-0001"
        assert comp["attribution_text"] == "Rolled back by Marco Rossi via Keelson (APR-RB-1)"
        assert {it["op"] for it in comp["items"]} == {"revert.insert", "revert.update"}
        tos = await call(c, "list_transfer_orders", {})
        assert [t["code"] for t in tos["items"]] == ["TR-00001"]
        with pytest.raises(ToolError, match="already_rolled_back"):
            await rollback(c, keelson, v, approval_ref="APR-RB-2")
    rtm = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    al = await row(run, "store_allocations", code="AL-0003")
    assert {k: rtm[k] for k in ("on_hand", "allocated", "reserved", "in_transit")} == \
           {k: rtm0[k] for k in ("on_hand", "allocated", "reserved", "in_transit")}
    assert int(rtm["rev"]) == 3 and al["qty"] == al0["qty"] and int(al["rev"]) == 3
    # the FH-LDS row the transfer created by upsert? (it existed: only restored) — no stray rows
    assert len((await run.sidecar.rows("transfer_lines", {}))["items"]) == 2


async def test_rollback_of_a_row_changed_twice(any_backend: Running, keelson: FakeKeelson) -> None:
    """Two items of one change set adjust the same versioned stock rows (two transfers of one SKU):
    the rollback undoes both effects newest first — its own first undo is not a conflict."""
    run = any_backend
    business = ("on_hand", "allocated", "reserved", "in_transit")
    rtm0 = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    lds0 = await row(run, "stock", site="FH-LDS", sku="FH-PARKA-NVY-M")
    first = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "lines": [{"sku": "FH-PARKA-NVY-M", "qty": 3}]}}
    second = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "2026-10-27",
                                     "lines": [{"sku": "FH-PARKA-NVY-M", "qty": 2}]}}
    async with run.client() as c:
        v = await prepare(c, [first, second])
        assert v["status"] == "validated"
        assert (await apply(c, keelson, v))["status"] == "applied"
        rtm1 = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
        assert int(rtm1["reserved"]) == int(rtm0["reserved"]) + 5 and int(rtm1["rev"]) == int(rtm0["rev"]) + 2
        out = await rollback(c, keelson, v)
        assert out["status"] == "rolled_back", out
        assert out["applied"] == 8  # 2 × (header, line, source stock, destination stock)
        assert [t["code"] for t in (await call(c, "list_transfer_orders", {}))["items"]] == ["TR-00001"]
    rtm = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    lds = await row(run, "stock", site="FH-LDS", sku="FH-PARKA-NVY-M")
    assert {k: rtm[k] for k in business} == {k: rtm0[k] for k in business}
    assert {k: lds[k] for k in business} == {k: lds0[k] for k in business}
    assert int(rtm["rev"]) == int(rtm0["rev"]) + 4 and int(lds["rev"]) == int(lds0["rev"]) + 4


async def test_rollback_conflicts_when_changed_since(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM, await alloc(run)])
        await apply(c, keelson, v)
        await external_update(run, "UPDATE store_allocations SET qty = :qty, rev = rev + 1 WHERE code = :code",
                              code="AL-0003", qty=1)
        out = await rollback(c, keelson, v)
        assert out["status"] == "conflict"
        assert out["conflicts"] == [{"seq": 2, "target_ref": "store_allocations:AL-0003", "expected_version": 2,
                                     "actual_version": 3, "reason": "row_changed"}]
        cs = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
        assert cs["status"] == "applied"  # nothing was rolled back
        assert len((await call(c, "list_transfer_orders", {}))["items"]) == 2


async def test_rollback_receipt_rules(fernhill: Running, keelson: FakeKeelson) -> None:
    async with fernhill.client() as c:
        v = await prepare(c, [TO_ITEM])
        await apply(c, keelson, v, approval_ref="APR-1", co_approved_by=())
        with pytest.raises(ToolError, match="rollback_requires_user_approval"):
            await rollback(c, keelson, v, policy=True)
        with pytest.raises(ToolError, match=r"invalid_receipt: .*\(reason: approval_reused\)"):
            await rollback(c, keelson, v, approval_ref="APR-1")
        with pytest.raises(ToolError, match=r"\(reason: hash_mismatch\)"):
            await call(c, "rollback_change_set", {
                "change_set_id": v["change_set_id"], "approval_ref": "APR-RB-9", "idempotency_key": "rb-x",
                "approval_receipt": keelson.receipt(change_set_ref=v["change_set_id"], change_set_hash="0" * 64,
                                                    approval_id="APR-RB-9", policy=False)})
        v2 = await prepare(c, [{**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "2026-10-28"}}])
        await apply(c, keelson, v2, approval_ref="APR-4E", policy=False, co_approved_by=("user:lena",))
        with pytest.raises(ToolError, match=r"\(reason: co_approval_required\)"):
            await rollback(c, keelson, v2, approval_ref="APR-RB-4E")
        assert (await rollback(c, keelson, v2, approval_ref="APR-RB-4E2",
                               co_approved_by=("user:lena",)))["status"] == "rolled_back"
        draft = await prepare(c, [TO_ITEM])
        with pytest.raises(ToolError, match="invalid_state"):
            await rollback(c, keelson, draft)
