"""M5 Guide mode (``95`` §2): checklist tables, the runtime port (create/get/list/set step/status),
manual ticks through ``POST /api/cases/{id}/checklists/{cid}/steps/{seq}`` (audited), read-back
verification through the connector's read tools (a fake MCP tool caller, and a real FastMCP 4
server through the reader credentials), completion, the live block and the 60 s poll."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastmcp import FastMCP
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from e1_mcp_server import McpTestServer
from keelson_contracts.blocks import ChecklistExpected, ChecklistStep, RemediationChecklistProps, parse_block
from keelson_engine.config import Settings
from keelson_engine.core import checklists, connectors, repo
from keelson_engine.core.db import tenant_session
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
NOW = dt.datetime.now(dt.UTC)

STEPS = [
    {"title": "Create transfer order DE1 → NL1", "instructions_md": "Open **Transfer orders** › New.",
     "deep_link": "http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1",
     "expected": {"entity_type": "transfer_order", "entity_ref": None,
                  "match": {"from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]}}},
    {"title": "Set allocation ALLOC-7 to 40", "instructions_md": "Edit the allocation quantity.",
     "deep_link": None, "expected": {"entity_type": "allocation", "entity_ref": "ALLOC-7", "match": {"qty": 40}}},
    {"title": "Reschedule PO-9 line 2", "instructions_md": "Change the ETA.", "deep_link": "/purchase-orders/PO-9",
     "expected": {"entity_type": "po_line", "entity_ref": "PO-9#2", "match": {"eta_dc": "2026-11-02"}}},
]
TOOLS = {"list_transfer_orders": {"properties": {"status": {}, "from_dc": {}, "to_dc": {}}},
         "get_entity": {"properties": {"type": {}, "code": {}}},
         "get_purchase_order": {"properties": {"code": {}}}}


class FakeMcp:
    """Scripted read tools (what Merxa would answer) + a call log."""

    def __init__(self) -> None:
        self.tos: list[dict[str, Any]] = []
        self.entities: dict[tuple[str, str], dict[str, Any]] = {}
        self.pos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tenant: str, connector: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "list_transfer_orders":
            items = [t for t in self.tos if all(t.get(k) == v for k, v in args.items() if k != "cursor")]
            return {"items": items}
        if tool == "get_entity":
            data = self.entities.get((args["type"], args["code"]))
            if data is None:
                raise RuntimeError("not found")
            return {"type": args["type"], "code": args["code"], "data": data, "version": 3}
        if tool == "get_purchase_order":
            return self.pos.get(args["code"], {"code": args["code"], "lines": []})
        raise RuntimeError(f"unknown tool {tool}")


@pytest_asyncio.fixture(loop_scope="session")
async def fake_mcp(platform: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeMcp]:
    fake = FakeMcp()
    checklists.set_tool_caller(fake)

    async def read_tools(tenant: str, connector: str) -> dict[str, dict[str, Any]]:
        return dict(TOOLS)

    monkeypatch.setattr(checklists, "_read_tools", read_tools)
    yield fake
    checklists.set_tool_caller(None)


async def _case() -> str:
    return await repo.create_case("guide", created_by=PRIYA, tenant=TENANT_A)


async def _checklist(case_id: str, *, block: bool = False, steps: list[dict[str, Any]] | None = None) -> str:
    cid = await checklists.create_checklist(TENANT_A, case_id, "merxa", steps or STEPS, plan_version=1,
                                            option_id="opt-A", created_by="sub-priya", meta={"vendor": "Merxa"})
    if block:
        props = await checklists.checklist_props(TENANT_A, cid)
        envelope = {"id": f"blk_guide_{cid[-12:]}", "kind": "remediation_checklist", "v": 1, "title": "Guide",
                    "placement": "inline", "data_ref": None, "props": props.model_dump(mode="json"),
                    "provenance": {"kind": "inference", "engine": "keelson-guide@0.1", "inputs_hash": None,
                                   "as_of": "2026-10-07"}}
        parse_block(envelope)
        await repo.save_block(case_id, None, envelope, tenant=TENANT_A)
        await checklists.attach_block(TENANT_A, cid, envelope["id"])
    return cid


# ───────────────────────────── pure helpers ─────────────────────────────


async def test_matches_and_plan_readback() -> None:
    m = checklists.matches
    assert m({"from_dc": "DE1", "lines": [{"sku": "S1", "qty": 120, "x": 1}, {"sku": "S2", "qty": 5}]},
             {"from_dc": "DE1", "lines": [{"sku": "S2", "qty": 5}, {"sku": "S1", "qty": 120}]})
    assert not m({"lines": [{"sku": "S1", "qty": 120}]}, {"lines": [{"sku": "S1", "qty": 120}, {"sku": "S1",
                                                                                               "qty": 120}]})
    assert m({"qty": "40"}, {"qty": 40}) and m({"qty": 40}, {"qty": "40"})
    assert not m({"qty": 41}, {"qty": 40}) and not m({}, {"qty": 40}) and not m({"ok": 1}, {"ok": True})
    exp = ChecklistExpected.model_validate(STEPS[0]["expected"])
    rb = checklists.plan_readback(exp, TOOLS)
    assert rb is not None and rb.tool == "list_transfer_orders" and rb.args == {"from_dc": "DE1", "to_dc": "NL1"}
    rb = checklists.plan_readback(ChecklistExpected.model_validate(STEPS[1]["expected"]), TOOLS)
    assert rb is not None and rb.tool == "get_entity" and rb.args == {"type": "allocation", "code": "ALLOC-7"}
    rb = checklists.plan_readback(ChecklistExpected.model_validate(STEPS[2]["expected"]), TOOLS)
    assert rb is not None and rb.tool == "get_purchase_order" and rb.ref_value == "2"
    assert checklists.plan_readback(ChecklistExpected(entity_type="booking", entity_ref=None, match={}), TOOLS) is None


# ───────────────────────────── store + port ─────────────────────────────


async def test_create_get_and_port_shape(platform: None) -> None:
    case_id = await _case()
    cid = await _checklist(case_id)
    again = await checklists.create_checklist(TENANT_A, case_id, "merxa", STEPS, checklist_id=cid)
    assert again == cid  # idempotent on the id
    cl = await checklists.get_checklist(TENANT_A, cid)
    assert cl is not None and cl["status"] == "open" and cl["meta"] == {"vendor": "Merxa"}
    steps = [ChecklistStep.model_validate(s) for s in cl["steps"]]  # exact remediation_checklist@1 steps
    assert [s.seq for s in steps] == [1, 2, 3] and all(s.status == "pending" for s in steps)
    props = await checklists.checklist_props(TENANT_A, cid)
    assert isinstance(props, RemediationChecklistProps) and props.progress.total == 3 and props.progress.done == 0
    assert await checklists.get_checklist(TENANT_B, cid) is None  # RLS
    assert cid in [c["id"] for c in await checklists.list_open_checklists(tenant=TENANT_A)]
    assert cid in [c["id"] for c in await checklists.list_open_checklists()]
    with pytest.raises(checklists.ChecklistError):
        await checklists.create_checklist(TENANT_A, case_id, "merxa", [])
    with pytest.raises(checklists.ChecklistError):  # unsafe deep link
        await checklists.create_checklist(TENANT_A, case_id, "merxa", [{**STEPS[0], "deep_link": "javascript:x"}])

    # runtime port: verified carries verified_at + evidence; never changes back
    out = await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=1, status="verified",
                                              verified_at=NOW.isoformat(), evidence_id="ev_port1", actor="read-back")
    assert out is not None and out["steps"][0]["status"] == "verified" and out["steps"][0]["evidence_id"] == "ev_port1"
    out = await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=1, status="done")
    assert out is not None and out["steps"][0]["status"] == "verified"
    await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=2, status="skipped", actor="priya")
    out = await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=3, status="skipped")
    assert out is not None and out["status"] == "completed" and out["completed_at"] is not None
    assert await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=9, status="done") is None
    assert await checklists.set_checklist_step(tenant=TENANT_A, checklist_id="nope", seq=1, status="done") is None
    await checklists.set_checklist_status(tenant=TENANT_A, checklist_id=cid, status="cancelled")
    assert (await checklists.get_checklist(TENANT_A, cid) or {})["status"] == "cancelled"


# ───────────────────────────── API ─────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    return {n: await browser_factory(n) for n in ("priya", "lena", "nora")}


async def test_api_manual_ticks(people: dict[str, Browser]) -> None:
    priya, lena, nora = people["priya"], people["lena"], people["nora"]
    case_id = await _case()
    cid = await _checklist(case_id, block=True)
    base = f"/api/cases/{case_id}/checklists"
    listed = (await lena.get(base)).json()["items"]  # compliance may read
    assert [c["id"] for c in listed] == [cid] and listed[0]["props"]["progress"] == {"done": 0, "total": 3}
    assert (await lena.post(f"{base}/{cid}/steps/1", json={"status": "done"})).status_code == 403
    assert (await nora.post(f"{base}/{cid}/steps/1", json={"status": "done"})).status_code == 404
    assert (await priya.post(f"{base}/{cid}/steps/9", json={"status": "done"})).status_code == 404
    assert (await priya.post(f"{base}/nope/steps/1", json={"status": "done"})).status_code == 404
    assert (await priya.post(f"{base}/{cid}/steps/1", json={"status": "verified"})).status_code == 422
    r = await priya.post(f"{base}/{cid}/steps/1", json={"status": "done"})
    assert r.status_code == 200, r.text
    assert r.json()["step"] == {"seq": 1, "status": "done", "updated_by": "priya"} and r.json()["changed"] is True
    assert r.json()["checklist"]["props"]["progress"]["done"] == 1
    # a checklist of another case is 404 through this case
    other_case = await _case()
    assert (await priya.post(f"/api/cases/{other_case}/checklists/{cid}/steps/1",
                             json={"status": "done"})).status_code == 404
    # audited, block refreshed + streamed
    async with tenant_session(TENANT_A) as s:
        audit = (await s.execute(text("SELECT action, actor_username, before, after FROM admin_audit "
                                      "WHERE target = :t ORDER BY id"), {"t": f"checklist:{cid}#1"})).mappings().all()
        ev = (await s.execute(text("SELECT payload FROM run_event WHERE case_id = :c AND type = 'ACTIVITY_SNAPSHOT' "
                                   "ORDER BY id DESC LIMIT 1"), {"c": case_id})).scalar_one()
    assert [(a["action"], a["actor_username"], a["before"], a["after"]["status"]) for a in audit] == [
        ("checklist.step_done", "priya", {"status": "pending"}, "done")]
    assert ev["content"]["props"]["steps"][0]["status"] == "done"
    # skip the rest → completed
    for seq in (2, 3, 1):
        assert (await priya.post(f"{base}/{cid}/steps/{seq}", json={"status": "skipped"})).status_code == 200
    final = (await priya.get(base)).json()["items"][0]
    assert final["status"] == "completed" and final["completed"] is True
    # verified steps are final
    case2 = await _case()
    cid2 = await _checklist(case2)
    await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid2, seq=1, status="verified")
    r = await priya.post(f"/api/cases/{case2}/checklists/{cid2}/steps/1", json={"status": "skipped"})
    assert r.status_code == 409 and r.json()["code"] == "already_verified"


# ───────────────────────────── read-back verification ─────────────────────────────


async def _only_new_checklists() -> None:
    async with tenant_session(TENANT_A) as s:  # earlier tests' open checklists would be verified too
        await s.execute(text("UPDATE checklist SET status = 'cancelled' WHERE status = 'open'"))


async def test_verify_marks_matching_steps(fake_mcp: FakeMcp) -> None:
    await _only_new_checklists()
    case_id = await _case()
    cid = await _checklist(case_id, block=True)
    # nothing in Merxa yet
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert res.verified == [] and res.checked_steps >= 3
    # a transfer order that existed before the checklist never counts; nor does a different qty
    old = (NOW - dt.timedelta(days=1)).isoformat()
    fake_mcp.tos = [
        {"code": "TO-OLD", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}], "created_at": old},
        {"code": "TO-2", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 99}]},
    ]
    assert (await checklists.verify_checklists(TENANT_A, "merxa")).verified == []
    # the planner creates it in Merxa's UI → verified with evidence
    fake_mcp.tos.append({"code": "TO-3", "from_dc": "DE1", "to_dc": "NL1", "status": "open",
                         "lines": [{"sku": "S1", "qty": 120}], "created_at": dt.datetime.now(dt.UTC).isoformat()})
    fake_mcp.entities[("allocation", "ALLOC-7")] = {"qty": 40, "sku": "S1"}
    fake_mcp.calls.clear()
    res = await checklists.verify_checklists(TENANT_A, "merxa", {"id": "evt-1", "type": "transfer_order.created"})
    assert (cid, 1) in res.verified and (cid, 2) in res.verified and (cid, 3) not in res.verified
    assert ("list_transfer_orders", {"from_dc": "DE1", "to_dc": "NL1"}) in fake_mcp.calls
    cl = await checklists.get_checklist(TENANT_A, cid)
    assert cl is not None
    s1 = cl["steps"][0]
    assert s1["status"] == "verified" and s1["verified_at"] and s1["evidence_id"].startswith("ev_")
    [ev] = await repo.get_evidence([s1["evidence_id"]], tenant=TENANT_A)
    assert ev["prov"] == "fact" and ev["tool"] == "list_transfer_orders" and ev["result"]["entity_ref"] == "TO-3"
    assert ev["result"]["event_id"] == "evt-1"
    # the block shows it
    block = await repo.get_block(cl["block_id"], tenant=TENANT_A)
    assert block is not None and block["envelope"]["props"]["progress"] == {"done": 2, "total": 3}
    # PO line read-back through get_purchase_order (the line of the ref) → completed
    fake_mcp.pos["PO-9"] = {"code": "PO-9", "lines": [{"line_no": 1, "eta_dc": "2026-11-02"},
                                                      {"line_no": 2, "eta_dc": "2026-10-20"}]}
    assert (await checklists.verify_checklists(TENANT_A, "merxa")).verified == []  # line 1 matches, not line 2
    fake_mcp.pos["PO-9"]["lines"][1]["eta_dc"] = "2026-11-02"
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert res.verified == [(cid, 3)] and res.completed == [cid]
    cl = await checklists.get_checklist(TENANT_A, cid)
    assert cl is not None and cl["status"] == "completed"
    # completed checklists are not read back any more
    fake_mcp.calls.clear()
    await checklists.verify_checklists(TENANT_A, "merxa")
    assert not any(a.get("code") == "PO-9" for _t, a in fake_mcp.calls)


async def test_one_entity_verifies_one_step(fake_mcp: FakeMcp) -> None:
    await _only_new_checklists()
    case_id = await _case()
    twin = [STEPS[0], STEPS[0]]
    cid = await _checklist(case_id, steps=twin)
    fake_mcp.tos = [{"code": "TO-TWIN-1", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]}]
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert [v for v in res.verified if v[0] == cid] == [(cid, 1)]
    fake_mcp.tos.append({"code": "TO-TWIN-2", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]})
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert [v for v in res.verified if v[0] == cid] == [(cid, 2)]


async def test_tool_errors_and_unreadable_steps(fake_mcp: FakeMcp) -> None:
    case_id = await _case()
    cid = await _checklist(case_id, steps=[
        {**STEPS[1], "expected": {"entity_type": "allocation", "entity_ref": "ALLOC-MISSING", "match": {"qty": 1}}},
        {**STEPS[0], "expected": {"entity_type": "capacity_booking", "entity_ref": None, "match": {"qty": 1}}},
    ])
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert (cid, 2) in res.unreadable and any(e.startswith("get_entity") for e in res.errors)
    cl = await checklists.get_checklist(TENANT_A, cid)
    assert cl is not None and [s["status"] for s in cl["steps"]] == ["pending", "pending"]


async def test_poll_due_on_interval_or_new_event(fake_mcp: FakeMcp, settings: Settings) -> None:
    await _only_new_checklists()
    case_id = await _case()
    cid = await _checklist(case_id)
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    first = await checklists.poll_checklists(interval_s=60)
    assert [(r.tenant, r.connector) for r in first] == [(TENANT_A, "merxa")]
    assert await checklists.poll_checklists(interval_s=60) == []  # checked just now
    async with tenant_session(TENANT_A) as s:  # a webhook arrives → due at once
        await s.execute(text("INSERT INTO connector_event (tenant, connector, event_id, type, payload) "
                             "VALUES (:t, 'merxa', :e, 'transfer_order.created', '{}'::jsonb)"),
                        {"t": TENANT_A, "e": f"evt-{cid}"})
    fake_mcp.tos = [{"code": "TO-POLL", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]}]
    [res] = await checklists.poll_checklists(interval_s=60)
    assert (cid, 1) in res.verified
    assert await checklists.poll_checklists(interval_s=60) == []
    assert len(await checklists.poll_checklists(interval_s=0)) == 1  # the interval elapsed


# ───────────────────────────── through a real MCP server (reader credentials) ─────────────────────────────


def _erp() -> FastMCP:
    mcp = FastMCP("guide-erp")

    @mcp.tool(name="list_transfer_orders", description="List transfer orders.", annotations={"readOnlyHint": True})
    def list_transfer_orders(from_dc: str | None = None, to_dc: str | None = None) -> dict[str, Any]:
        items = [{"code": "TO-REAL-1", "from_dc": "DE1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]},
                 {"code": "TO-REAL-2", "from_dc": "PL1", "to_dc": "NL1", "lines": [{"sku": "S1", "qty": 120}]}]
        return {"items": [i for i in items if (from_dc is None or i["from_dc"] == from_dc)
                          and (to_dc is None or i["to_dc"] == to_dc)]}

    return mcp


async def test_verify_through_fastmcp_with_reader_credentials(platform: None, settings: Settings,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_engine.core import connector_admin

    server = McpTestServer()
    server.mcp = _erp()
    await server.start()
    try:
        monkeypatch.setattr(settings, "connector_dev_hosts", "127.0.0.1,merxa.localhost")
        slug = "guide-erp"
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("DELETE FROM connector WHERE slug = :s"), {"s": slug})
        await connector_admin.create_connector(
            TENANT_A, {"slug": slug, "name": "Guide ERP", "mcp_url": server.url,
                       "auth": {"kind": "static_header", "header_name": "X-Api-Key", "secret": server.gate.api_key}},
            {"sub": "sub-sam", "username": "sam", "roles": ["tenant-admin"]}, platform_admin=False,
        )
        tools = [{"name": "list_transfer_orders", "description": "List transfer orders.",
                  "inputSchema": {"type": "object", "properties": {"from_dc": {"type": "string"},
                                                                   "to_dc": {"type": "string"}}},
                  "annotations": {"readOnlyHint": True}}]
        await connectors.upsert_tool_snapshot(TENANT_A, slug, tools, auto_approve=True)
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector SET status = 'active' WHERE slug = :s"), {"s": slug})
            await s.execute(text("UPDATE connector_tool SET status = 'approved' WHERE connector = :s"), {"s": slug})
        case_id = await _case()
        cid = await checklists.create_checklist(TENANT_A, case_id, slug, [STEPS[0]])
        res = await checklists.verify_checklists(TENANT_A, slug)
        assert res.errors == [] and res.verified == [(cid, 1)]
        cl = await checklists.get_checklist(TENANT_A, cid)
        assert cl is not None and cl["status"] == "completed"
        # the server saw the reader credential and only the filtered call
        assert any(h.get("x-api-key") == server.gate.api_key for h in server.gate.seen_headers)
    finally:
        await server.stop()
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("DELETE FROM connector WHERE slug = 'guide-erp'"))


# ───────────────────────────── Merxa-shaped read-back (regressions) ─────────────────────────────

MERXA_TOOLS: dict[str, dict[str, Any]] = {
    # get_entity only knows catalog types (the Merxa EntityType enum), never allocations
    "get_entity": {"properties": {"type": {"$ref": "#/$defs/EntityType"}, "code": {"type": "string"}},
                   "$defs": {"EntityType": {"enum": ["sku", "style", "po", "store", "dc", "supplier", "factory"],
                                            "type": "string"}}},
    "list_allocations": {"properties": {"dc_code": {}, "store_code": {}, "status": {}, "cursor": {}}},
    "list_transfer_orders": {"properties": {"status": {}, "from_dc": {}, "to_dc": {}}},
    "list_shipments": {"properties": {"po_code": {}, "status": {}, "dest_dc": {}, "cursor": {}}},
    "list_purchase_orders": {"properties": {"supplier_code": {}, "factory_code": {}, "dest_dc": {}, "cursor": {}}},
    "get_purchase_order": {"properties": {"code": {}}},
}


class MerxaShaped:
    """Read tools answering like Merxa (list rows without lines for POs, nested shipment lines)."""

    def __init__(self) -> None:
        self.allocations: list[dict[str, Any]] = []
        self.tos: list[dict[str, Any]] = []
        self.shipments: list[dict[str, Any]] = []
        self.pos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tenant: str, connector: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "get_entity":
            raise RuntimeError("Input validation error: allocation is not one of the entity types")
        if tool == "list_allocations":
            return {"items": list(self.allocations), "next_cursor": None}
        if tool == "list_transfer_orders":
            return {"items": [t for t in self.tos if all(t.get(k) == v for k, v in args.items())]}
        if tool == "list_shipments":
            return {"items": list(self.shipments), "next_cursor": None}
        if tool == "list_purchase_orders":
            return {"items": [{k: v for k, v in po.items() if k != "lines"} for po in self.pos.values()
                              if all(po.get(k) == v for k, v in args.items())], "next_cursor": None}
        if tool == "get_purchase_order":
            return dict(self.pos[args["code"]])
        raise RuntimeError(f"unknown tool {tool}")


@pytest_asyncio.fixture(loop_scope="session")
async def merxa_shaped(fake_mcp: FakeMcp, monkeypatch: pytest.MonkeyPatch) -> MerxaShaped:
    fake = MerxaShaped()
    checklists.set_tool_caller(fake)  # the fake_mcp fixture restores the default at teardown

    async def read_tools(tenant: str, connector: str) -> dict[str, dict[str, Any]]:
        return dict(MERXA_TOOLS)

    monkeypatch.setattr(checklists, "_read_tools", read_tools)
    return fake


async def test_edit_steps_on_existing_entities_verify(merxa_shaped: MerxaShaped) -> None:
    """Regressions: (1) an allocation step went to ``get_entity``, which Merxa refuses for
    allocations, so it never verified; it now reads ``list_allocations`` and keeps the named row;
    (2) an edit step on an entity created long before the checklist never counted; (3) two steps on
    the same PO line could not both verify (one entity per checklist applies to creations only)."""
    await _only_new_checklists()
    exp = ChecklistExpected(entity_type="allocation", entity_ref="AL-7", match={"qty": 40})
    rb = checklists.plan_readback(exp, MERXA_TOOLS)
    assert rb is not None and rb.tool == "list_allocations" and rb.match_ref == "AL-7"
    rb = checklists.plan_readback(ChecklistExpected(entity_type="po", entity_ref="PO-1", match={}), MERXA_TOOLS)
    assert rb is not None and rb.tool == "get_entity"
    case_id = await _case()
    old = (NOW - dt.timedelta(days=30)).isoformat()
    cid = await _checklist(case_id, steps=[
        {**STEPS[1], "expected": {"entity_type": "allocation", "entity_ref": "AL-7", "match": {"qty": 40}}},
        {**STEPS[2], "expected": {"entity_type": "po_line", "entity_ref": "PO-9#2", "match": {"eta_dc": "2026-11-02"}}},
        {**STEPS[2], "title": "Change qty", "expected": {"entity_type": "po_line", "entity_ref": "PO-9#2",
                                                          "match": {"qty_ordered": 300}}},
    ])
    merxa_shaped.allocations = [{"code": "AL-6", "qty": 40, "created_at": old},
                                {"code": "AL-7", "qty": 55, "created_at": old}]
    merxa_shaped.pos["PO-9"] = {"code": "PO-9", "created_at": old, "lines": [
        {"line_no": 2, "eta_dc": "2026-10-20", "qty_ordered": 500, "created_at": old}]}
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert [v for v in res.verified if v[0] == cid] == []  # AL-6 has the qty but is not the named row
    assert not any(t == "get_entity" for t, _a in merxa_shaped.calls)
    merxa_shaped.allocations[1]["qty"] = 40
    merxa_shaped.pos["PO-9"]["lines"][0].update(eta_dc="2026-11-02", qty_ordered=300)
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert sorted(v for v in res.verified if v[0] == cid) == [(cid, 1), (cid, 2), (cid, 3)]
    assert res.completed == [cid]


async def test_create_steps_ignore_existing_and_applied_entities(merxa_shaped: MerxaShaped) -> None:
    """Regressions: the runtime ``meta.baseline`` (entities that existed when the guide was issued)
    and entities created by an applied change set (``created_via: change_set:...``) never verify a
    create step; a split shipment is matched through its lines and a new PO through a full read
    (list rows have no lines; Merxa PO lines carry ``qty_ordered``)."""
    await _only_new_checklists()
    case_id = await _case()
    steps = [
        {**STEPS[0]},
        {**STEPS[0], "title": "Split the PO line", "expected": {
            "entity_type": "shipment", "entity_ref": None,
            "match": {"po_line_ref": "PO-5#1", "mode": "air", "qty": 400}}},
        {**STEPS[0], "title": "Create the PO", "expected": {
            "entity_type": "purchase_order", "entity_ref": None,
            "match": {"supplier_code": "SUP-1", "factory_code": "F-1", "dest_dc": "NL1",
                      "lines": [{"sku": "S1", "qty": 900}]}}},
    ]
    cid = await checklists.create_checklist(
        TENANT_A, case_id, "merxa", steps, created_by="sub-priya",
        meta={"baseline": {"1": ["TO-BEFORE"], "2": [], "3": ["PO-OLD"]}})
    line = {"sku": "S1", "qty": 120}
    merxa_shaped.tos = [
        {"code": "TO-BEFORE", "from_dc": "DE1", "to_dc": "NL1", "lines": [line]},  # baseline, no created_at
        {"code": "TO-APPLIED", "from_dc": "DE1", "to_dc": "NL1", "lines": [line], "created_via": "change_set:CS-1"},
    ]
    merxa_shaped.pos["PO-OLD"] = {"code": "PO-OLD", "supplier_code": "SUP-1", "factory_code": "F-1",
                                  "dest_dc": "NL1", "lines": [{"line_no": 1, "sku": "S1", "qty_ordered": 900}]}
    assert [v for v in (await checklists.verify_checklists(TENANT_A, "merxa")).verified if v[0] == cid] == []
    merxa_shaped.tos.append({"code": "TO-HUMAN", "from_dc": "DE1", "to_dc": "NL1", "lines": [line],
                             "created_via": "ui"})
    merxa_shaped.shipments = [{"code": "SH-1", "mode": "air", "status": "planned", "total_qty": 400,
                               "lines": [{"line_no": 1, "po_line_ref": "PO-5#1", "sku": "S1", "qty": 400}]}]
    merxa_shaped.pos["PO-NEW"] = {"code": "PO-NEW", "supplier_code": "SUP-1", "factory_code": "F-1",
                                  "dest_dc": "NL1", "lines": [{"line_no": 1, "sku": "S1", "qty_ordered": 900}]}
    merxa_shaped.calls.clear()
    res = await checklists.verify_checklists(TENANT_A, "merxa")
    assert sorted(v for v in res.verified if v[0] == cid) == [(cid, 1), (cid, 2), (cid, 3)]
    cl = await checklists.get_checklist(TENANT_A, cid)
    assert cl is not None and cl["status"] == "completed"
    async with tenant_session(TENANT_A) as s:
        refs = (await s.execute(text("SELECT seq, matched_ref FROM checklist_step WHERE checklist_id = :c "
                                     "ORDER BY seq"), {"c": cid})).all()
    assert [r[1] for r in refs] == ["TO-HUMAN", "SH-1", "PO-NEW"]
    assert ("get_purchase_order", {"code": "PO-OLD"}) not in merxa_shaped.calls  # baseline: never read in full


async def test_undoing_a_skip_reopens_a_completed_checklist(people: dict[str, Browser]) -> None:
    """Regression: a completed checklist stayed ``completed`` ("Guide completed") after a skipped
    step was ticked ``done`` again (both through the API and the runtime port)."""
    priya = people["priya"]
    case_id = await _case()
    cid = await _checklist(case_id)
    base = f"/api/cases/{case_id}/checklists/{cid}/steps"
    for seq in (1, 2, 3):
        assert (await priya.post(f"{base}/{seq}", json={"status": "skipped"})).status_code == 200
    assert (await checklists.get_checklist(TENANT_A, cid) or {})["status"] == "completed"
    r = await priya.post(f"{base}/2", json={"status": "done"})
    assert r.status_code == 200 and r.json()["checklist"]["status"] == "open", r.text
    assert r.json()["checklist"]["completed"] is False and r.json()["checklist"]["completed_at"] is None
    assert (await priya.post(f"{base}/2", json={"status": "skipped"})).json()["checklist"]["status"] == "completed"
    out = await checklists.set_checklist_step(tenant=TENANT_A, checklist_id=cid, seq=3, status="pending")
    assert out is not None and out["status"] == "open"
