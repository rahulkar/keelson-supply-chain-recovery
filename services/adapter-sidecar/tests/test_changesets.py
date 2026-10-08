"""Change-set facade over every writable backend (CSV, SQL on SQLite, SQL on Postgres when
``SIDECAR_TEST_PG_DSN`` is set): shapes, preview hash, validation errors, apply, conflicts,
idempotency, read-backs Keelson's reconcile uses, and the operator guide."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from conftest import FakeKeelson, Running, key
from fastmcp.exceptions import ToolError
from helpers import TO_ITEM, apply, call, prepare
from keelson_contracts.changeset import change_set_hash
from sqlalchemy import text

from adapter_sidecar import idempotency
from adapter_sidecar.db import begin_write, iso


async def row(run: Running, table: str, **filters: Any) -> dict[str, Any]:
    items = (await run.sidecar.rows(table, {k: str(v) for k, v in filters.items()}))["items"]
    assert len(items) == 1, (table, filters, items)
    return dict(items[0])


async def alloc(run: Running, code: str = "AL-0003", delta: int = -2) -> dict[str, Any]:
    r = await row(run, "store_allocations", code=code)
    return {"op": "allocation.set_qty", "target_ref": code, "expected_version": int(r["rev"]),
            "payload": {"qty": int(r["qty"]) + delta}}


async def external_update(run: Running, sql: str, **params: Any) -> None:
    """Another client of the system changes it (CSV: the file is edited; SQL: a direct UPDATE)."""
    if run.data_dir is not None:
        import csv
        import io

        path = run.data_dir / "store_allocations.csv"
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
        for r in rows:
            if r["code"] == params["code"]:
                r["qty"], r["rev"] = str(params["qty"]), str(int(r["rev"]) + 1)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        path.write_text(buf.getvalue(), encoding="utf-8")
    else:
        async with run.sidecar.backend.engine.begin() as conn:
            await conn.execute(text(sql), params)


async def test_create_add_validate(any_backend: Running) -> None:
    run = any_backend
    async with run.client() as c:
        created = await call(c, "create_change_set", {"title": "Rebalance parkas", "idempotency_key": key(),
                                                      "origin": {"system": "Keelson", "case_ref": "C-1"}})
        assert created["status"] == "draft" and created["code"] == "CS-0001"
        cs_id = created["change_set_id"]
        added = await call(c, "add_change_set_items", {"change_set_id": cs_id, "items": [TO_ITEM],
                                                       "idempotency_key": key()})
        assert added == {"change_set_id": cs_id, "items_count": 1}
        v = await call(c, "validate_change_set", {"change_set_id": cs_id, "idempotency_key": key()})
        assert v["status"] == "validated" and v["atomic"] is True
        assert v["preview_hash"] == change_set_hash([{**TO_ITEM, "seq": 1}])
        assert v["totals"] == {"lines": 1, "units_moved": 100, "value_cents": 100 * 4100}
        item = v["items"][0]
        assert item["ok"] and item["errors"] == [] and item["before"] is None
        assert item["after"]["transfers"]["code"] is None  # generated at apply
        assert item["after"]["transfers"]["created_via"] == "change_set:CS-0001"
        assert [ln["qty"] for ln in item["after"]["transfer_lines"]] == [60, 40]
        # validation changed nothing
        assert int((await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M"))["rev"]) == 1
        # adding an item resets the set to draft
        await call(c, "add_change_set_items", {"change_set_id": cs_id, "items": [await alloc(run)],
                                               "idempotency_key": key()})
        cs = await call(c, "get_change_set", {"change_set_id": cs_id})
        assert cs["status"] == "draft" and cs["items_count"] == 2 and cs["preview_hash"] is None
        assert {it["status"] for it in cs["items"]} == {"pending"}
        v2 = await call(c, "validate_change_set", {"change_set_id": "CS-0001", "idempotency_key": key()})
        assert v2["status"] == "validated" and len(v2["items"]) == 2
        assert v2["items"][1]["before"]["qty"] - v2["items"][1]["after"]["qty"] == 2
        assert v2["items"][1]["after"]["rev"] == v2["items"][1]["before"]["rev"] + 1
        assert v2["totals"]["units_moved"] == 102


async def test_validation_errors(any_backend: Running) -> None:
    run = any_backend
    a = await alloc(run)
    stale = {**a, "expected_version": 9}
    short = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "from_dc": "FH-LDS", "to_dc": "FH-RTM",
                                    "lines": [{"sku": "FH-PARKA-NVY-M", "qty": 999}]}}
    ghost = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "to_dc": "FH-XXX"}}
    m_stock = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    atp = int(m_stock["on_hand"]) - int(m_stock["allocated"]) - int(m_stock["reserved"])
    first = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "lines": [{"sku": "FH-PARKA-NVY-M", "qty": atp - 5}]}}
    second = {**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "2026-10-27",
                                     "lines": [{"sku": "FH-PARKA-NVY-M", "qty": 10}]}}
    async with run.client() as c:
        v = await prepare(c, [stale, short, ghost, first, second])
        assert v["status"] == "invalid"
        errors = [it["errors"] for it in v["items"]]
        assert errors[0] == ["version_mismatch: expected 9, current 1"]
        assert errors[1][0].startswith("insufficient_atp: stock FH-LDS|FH-PARKA-NVY-M: need 999, available ")
        assert errors[2][0].startswith("unknown_warehouse: warehouses FH-XXX does not exist")
        assert errors[3] == []
        # the second transfer sees the first one's reservation (items run in order)
        assert errors[4] == [f"insufficient_atp: stock FH-RTM|FH-PARKA-NVY-M: need 10, available 5"]
        cs = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
        assert cs["status"] == "draft" and cs["preview_hash"] is None
        assert [it["status"] for it in cs["items"]] == ["error", "error", "error", "ok", "error"]
        with pytest.raises(ToolError, match="invalid_state"):
            await apply(c, FakeKeelson(), v)
        created = await call(c, "create_change_set", {"title": "bad", "idempotency_key": key()})
        for bad, msg in (
            ({**TO_ITEM, "payload": {**TO_ITEM["payload"], "lines": [{"sku": "X", "qty": 1.5}]}}, "invalid_items"),
            ({**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "20/10/2026"}}, "invalid_items"),
            ({**a, "expected_version": None}, "invalid_items"),
            ({**a, "op": "po_line.cancel"}, "Input should be"),  # not in the published op enum
        ):
            with pytest.raises(ToolError, match=msg):
                await c.call_tool("add_change_set_items", {"change_set_id": created["change_set_id"], "items": [bad],
                                                           "idempotency_key": key()})
    # an op the system does not support (bypassing the MCP enum) is an item error, like Merxa
    from adapter_sidecar.auth import Principal

    svc = run.sidecar.changesets
    p = Principal("key:keelson", "key:keelson", frozenset({"changes:preview"}))
    cs2 = await svc.create(p, title="x", origin=None, idempotency_key=key())
    await svc.add_items(p, cs2["change_set_id"], items=[{"op": "po_line.cancel", "target_ref": "PO-FH-0001#1",
                                                         "expected_version": 1, "payload": {}}], idempotency_key=key())
    out = await svc.validate(p, cs2["change_set_id"], idempotency_key=key())
    assert out["items"][0]["errors"][0].startswith("unsupported_op:")


async def test_apply_and_reconcile_read_backs(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    a = await alloc(run)
    rtm_m = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    lds_m = await row(run, "stock", site="FH-LDS", sku="FH-PARKA-NVY-M")
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM, a])
        out = await apply(c, keelson, v)
        assert out == {"status": "applied", "applied": 2, "audit_ref": "audit:CS-0001", "change_set_code": "CS-0001",
                       "change_set_id": v["change_set_id"]}
        cs = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
        assert cs["status"] == "applied" and [it["status"] for it in cs["items"]] == ["applied", "applied"]
        assert [it["result_ref"] for it in cs["items"]] == ["TR-00002", "AL-0003"]
        assert cs["attribution_text"] == "Applied via Keelson auto-policy auto-small-transfer@p-1, initiated by Nora Test"
        assert cs["approval_ref"] == "APR-1" and cs["via"] == "Keelson"
        tos = await call(c, "list_transfer_orders", {"from_dc": "FH-RTM", "to_dc": "FH-LDS"})
        mine = [t for t in tos["items"] if t["created_via"] == "change_set:CS-0001"]
        assert len(mine) == 1 and mine[0]["code"] == "TR-00002" and mine[0]["status"] == "planned"
        assert mine[0]["ship_date"] == "2026-10-20"
        assert [(ln["line_no"], ln["sku"], ln["qty"]) for ln in mine[0]["lines"]] == [
            (1, "FH-PARKA-NVY-M", 60), (2, "FH-PARKA-NVY-L", 40)]
        assert (await call(c, "list_transfer_orders", {"status": "shipped"}))["items"][0]["code"] == "TR-00001"
        trail = await call(c, "get_audit_trail", {"change_set_code": "CS-0001", "limit": 500})
        actions = [(r["entity_type"], r["action"]) for r in trail["items"]]
        assert ("change_set", "apply") in actions and ("transfers", "insert") in actions
        assert ("store_allocations", "update") in actions and ("change_set", "create") in actions
        applied_row = next(r for r in trail["items"] if r["action"] == "apply")
        assert applied_row["approved_by"] == "policy:auto-small-transfer@p-1" and applied_row["via"] == "Keelson"
        assert applied_row["approval_ref"] == "APR-1" and applied_row["client_id"] == "key:keelson"
    rtm_after = await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M")
    lds_after = await row(run, "stock", site="FH-LDS", sku="FH-PARKA-NVY-M")
    assert int(rtm_after["reserved"]) == int(rtm_m["reserved"]) + 60 and int(rtm_after["rev"]) == 2
    assert int(lds_after["in_transit"]) == int(lds_m["in_transit"]) + 60
    al = await row(run, "store_allocations", code="AL-0003")
    assert int(al["qty"]) == a["payload"]["qty"] and int(al["rev"]) == 2


async def test_user_approval_attribution(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    async with run.client() as c:
        v = await prepare(c, [await alloc(run)])
        await apply(c, keelson, v, policy=False, approval_ref="APR-77", co_approved_by=("user:lena",))
        cs = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
    assert cs["attribution_text"] == "Changed by Marco Rossi via Keelson (APR-77), co-approved by lena"


async def test_conflict_when_a_version_moves(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    a = await alloc(run)
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM, a])
        await external_update(run, "UPDATE store_allocations SET qty = :qty, rev = rev + 1 WHERE code = :code",
                              code="AL-0003", qty=1)
        k = key()
        out = await apply(c, keelson, v, idem=k)
        assert out["status"] == "conflict"
        assert out["conflicts"] == [{"seq": 2, "target_ref": "AL-0003", "expected_version": 1, "actual_version": 2,
                                     "reason": "version_mismatch"}]
        # nothing was applied (the transfer neither)
        assert int((await row(run, "stock", site="FH-RTM", sku="FH-PARKA-NVY-M"))["rev"]) == 1
        tos = await call(c, "list_transfer_orders", {})
        assert [t["code"] for t in tos["items"]] == ["TR-00001"]
        cs = await call(c, "get_change_set", {"change_set_id": v["change_set_id"]})
        assert cs["status"] == "conflict" and cs["items"][1]["status"] == "error"
        # a conflict is a business outcome: the same request replays it
        assert await apply(c, keelson, v, idem=k) == out


async def test_idempotency_protocol(any_backend: Running, keelson: FakeKeelson) -> None:
    run = any_backend
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM])
        k = key()
        first = await apply(c, keelson, v, idem=k)
        # a retry with a freshly minted receipt (new jti) replays the stored response
        assert await apply(c, keelson, v, idem=k) == first
        assert len([t for t in (await call(c, "list_transfer_orders", {}))["items"]
                    if t["created_via"] == "change_set:CS-0001"]) == 1
        with pytest.raises(ToolError, match="idempotency_key_reused"):
            await apply(c, keelson, v, idem=k, approval_ref="APR-2")
        # in flight: a request with the same key is running
        body = {"title": "inflight", "origin": {}}
        claim_key = key()
        async with begin_write(run.sidecar.state) as conn:
            await conn.execute(text("INSERT INTO idempotency_record (principal, key, request_sha256, state, created_at) "
                                    "VALUES ('key:keelson', :k, :h, 'in_flight', :t)"),
                               {"k": claim_key, "h": idempotency.request_fingerprint("change_set.create", body),
                                "t": iso(dt.datetime.now(dt.UTC))})
        with pytest.raises(ToolError, match="idempotency_in_flight"):
            await c.call_tool("create_change_set", {"title": "inflight", "idempotency_key": claim_key})
        # ... until it is stale: then the claim is taken over
        async with begin_write(run.sidecar.state) as conn:
            await conn.execute(text("UPDATE idempotency_record SET created_at = '2000-01-01T00:00:00Z' WHERE key = :k"),
                               {"k": claim_key})
        taken = await call(c, "create_change_set", {"title": "inflight", "idempotency_key": claim_key})
        assert taken["status"] == "draft"


async def test_operator_guide(any_backend: Running) -> None:
    run = any_backend
    a = await alloc(run)
    async with run.client() as c:
        v = await prepare(c, [TO_ITEM, a])
        guide = await call(c, "get_operator_guide", {"change_set_id": v["change_set_id"]})
    s1, s2 = guide["steps"]
    assert s1["title"] == "Create a transfer FH-RTM → FH-LDS"
    assert "Add the lines FH-PARKA-NVY-M × 60, FH-PARKA-NVY-L × 40 and save" in s1["instructions"]
    assert s1["expected"] == {"entity_type": "transfer_order", "entity_ref": None,
                              "match": {"from_dc": "FH-RTM", "to_dc": "FH-LDS", "ship_date": "2026-10-20"}}
    assert s1["deep_link"] is None
    assert s2["title"] == f"Set allocation AL-0003 to {a['payload']['qty']} units"
    assert s2["expected"]["match"] == {"qty": a["payload"]["qty"]} and "expected record version 1" in s2["instructions"]
