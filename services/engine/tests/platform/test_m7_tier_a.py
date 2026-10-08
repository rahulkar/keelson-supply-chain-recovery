"""M7 tier A against a real FastMCP fake of Merxa ``/mcp-lite`` (``fakes/e1_lite_server.py``):
mapping storage (versions, optimistic concurrency, catalogue cross-checks), install from the
``foreign-erp-lite`` pack, the read-mapping test, the **crawl sync** (consistency ``crawled`` +
``as_of_range``, pointer flip, supersede, failures purge their rows and keep the previous snapshot,
FK sanity, reclassified tools), crawl status, the action **dry-run** (renders the call, runs only
the read-back, never calls a write tool) and the per-op commit ledger
(docs/contracts/110-m7-backoffice-generic.md §4)."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import HAVE_PACKS, REPO_PACKS, TENANT_A, TENANT_B
from e1_lite_server import LITE_DATA, LiteServer, drop_lite, register_lite
from keelson_engine.config import Settings
from keelson_engine.core import repo, scn_sync
from keelson_engine.core.db import tenant_session
from keelson_engine.core.packs import load_pack, load_pack_mappings

pytestmark = [pytest.mark.asyncio(loop_scope="session"),
              pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")]

SLUG = "m7-lite-erp"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def lite(db: None) -> AsyncIterator[LiteServer]:
    server = LiteServer()
    await server.start()
    yield server
    await server.stop()
    await drop_lite(TENANT_B, SLUG)


@pytest.fixture
def dev_hosts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_dev_hosts", "127.0.0.1")


@pytest_asyncio.fixture(loop_scope="session")
async def admin(session_browser: Any, platform: None) -> Browser:
    return await session_browser("nadia", ["tenant-admin"], [TENANT_B])


def _mappings() -> dict[str, dict[str, Any]]:
    return load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))


async def _connector(admin: Browser, lite: LiteServer, slug: str = SLUG) -> None:
    await register_lite(admin, lite, TENANT_B, slug)


async def _put(admin: Browser, kind: str, mapping: dict[str, Any], base: int | None, slug: str = SLUG) -> Any:
    return await admin.put(f"/api/admin/connectors/{slug}/mappings/{kind}",
                           json={"base_version": base, "mapping": mapping})


async def _count(entity: str, sid: str) -> int:
    async with tenant_session(TENANT_B) as s:
        return int((await s.execute(text(f"SELECT count(*) FROM scn_{entity} WHERE tenant = :t AND snapshot_id = "
                                         "CAST(:s AS uuid)"), {"t": TENANT_B, "s": sid})).scalar_one())


# ───────────────────────────── mapping storage ─────────────────────────────


async def test_mapping_versions_and_checks(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _connector(admin, lite)
    read = _mappings()["read"]
    url = f"/api/admin/connectors/{SLUG}/mappings/read"
    empty = (await admin.get(url)).json()
    assert empty["version"] is None and empty["mapping"] is None
    r = await _put(admin, "read", read, None)
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1 and r.json()["unchanged"] is False and "entity mappings" in r.json()["summary"]
    same = await _put(admin, "read", read, 1)
    assert same.status_code == 200 and same.json()["unchanged"] is True and same.json()["version"] == 1
    stale = await _put(admin, "read", read, None)
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict" and stale.json()["latest_version"] == 1
    edited = copy.deepcopy(read)
    edited["entities"] = edited["entities"][:2]
    r2 = await _put(admin, "read", edited, 1)
    assert r2.status_code == 201 and r2.json()["version"] == 2
    versions = (await admin.get(f"{url}/versions")).json()["items"]
    assert [v["version"] for v in versions] == [2, 1] and "mapping" not in versions[0]
    v1 = (await admin.get(f"{url}/versions/1")).json()
    assert len(v1["mapping"]["entities"]) == len(read["entities"])
    assert (await admin.get(f"{url}/versions/9")).status_code == 404
    got = (await admin.get(url)).json()
    assert got["version"] == 2 and got["problems"] == []

    # contract errors
    bad = copy.deepcopy(read)
    bad["entities"][0]["fields"]["code"] = "$.code.__class__"
    r = await _put(admin, "read", bad, 2)
    assert r.status_code == 422 and r.json()["code"] == "mapping_invalid" and r.json()["errors"], r.text
    bad = copy.deepcopy(read)
    del bad["entities"][0]["fields"]["src_version"]
    assert (await _put(admin, "read", bad, 2)).status_code == 422
    # catalogue cross-checks: a crawl never calls a write tool or crawls untrusted text
    for tool in ("update_item", "list_notes", "nope"):
        bad = copy.deepcopy(read)
        bad["entities"][0]["source"]["tool"] = tool
        r = await _put(admin, "read", bad, 2)
        assert r.status_code == 422 and r.json()["problems"], (tool, r.text)
    bad = copy.deepcopy(read)
    bad["lookups"]["po_supplier"]["source"]["tool"] = "create_transfer_order"
    assert (await _put(admin, "read", bad, 2)).status_code == 422
    # action mapping: the action tool must be a write tool, the read-back a read tool
    action = _mappings()["action"]
    bad = copy.deepcopy(action)
    bad["ops"][0]["tool"] = "list_items"
    assert (await _put(admin, "action", bad, None)).status_code == 422
    bad = copy.deepcopy(action)
    bad["ops"][1]["readback"]["tool"] = "update_item"
    assert (await _put(admin, "action", bad, None)).status_code == 422
    bad = copy.deepcopy(action)
    bad["ops"][1]["idempotency"] = "enforced"  # the tool is only confirmed best_effort
    assert (await _put(admin, "action", bad, None)).status_code == 422
    r = await _put(admin, "action", action, None)
    assert r.status_code == 201 and r.json()["version"] == 1, r.text
    caps = (await admin.get(f"/api/admin/connectors/{SLUG}/capabilities")).json()
    assert caps["read_mapping_version"] == 2 and caps["action_mapping_version"] == 1
    # tenant isolation: another tenant's admin sees no such connector
    other = await (await _other_admin(admin)).get(url)
    assert other.status_code == 404
    audit = (await admin.get("/api/admin/audit?action=connector.mapping")).json()["items"]
    assert {a["action"] for a in audit} == {"connector.mapping.update"} and audit[0]["category"] == "admin"


async def _other_admin(admin: Browser) -> Browser:
    from e1_browser import login_as

    return await login_as(admin.client, "sam")  # tenant-admin of demo-retail


async def test_install_from_pack_and_read_test(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _connector(admin, lite)
    r = await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    assert r.status_code == 200, r.text
    assert r.json()["mappings"]["read"]["version"] == 1 and r.json()["mappings"]["action"]["version"] == 1
    again = await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    assert again.json()["mappings"]["read"] == {**r.json()["mappings"]["read"], "unchanged": True}
    assert (await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack",
                             json={"pack": "blank"})).json()["code"] == "pack_has_no_mappings"
    assert (await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack",
                             json={"pack": "../etc"})).status_code == 422
    lite.calls.clear()
    test = await admin.post(f"/api/admin/connectors/{SLUG}/mappings/read/test", json={"max_pages": 1})
    assert test.status_code == 200, test.text
    body = test.json()
    assert body["ok"] is True and body["error"] is None
    rows = {e["where"]: e for e in body["entities"]}
    assert rows["entities[0] (location)"]["rows"] == 2
    po = next(e for e in body["entities"] if e["entity"] == "po_line")
    assert po["sample"][0]["source_id"] == "PO-1001#1" and po["sample"][0]["supplier_code"] == "SUP-VN-01"
    assert po["sample"][0]["ex_factory_date"] == "2026-10-20"
    assert not lite.writes and {t for t, _a in lite.calls} == {"list_items"}
    # a draft with a bad expression for these rows reports the entity's error, stores nothing
    draft = copy.deepcopy(_mappings()["read"])
    draft["entities"][0]["fields"]["src_version"] = "int($.name)"
    bad = (await admin.post(f"/api/admin/connectors/{SLUG}/mappings/read/test", json={"mapping": draft})).json()
    assert bad["ok"] is False and bad["entities"][0]["error"].startswith("mapping_failed")
    assert (await admin.get(f"/api/admin/connectors/{SLUG}/mappings/read")).json()["version"] == 1
    snaps = (await admin.get(f"/api/admin/connectors/{SLUG}/snapshots")).json()
    assert snaps["items"] == [] and snaps["current"] is None  # a test never loads a snapshot


# ───────────────────────────── crawl sync ─────────────────────────────


async def test_crawl_sync_loads_a_crawled_snapshot(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _connector(admin, lite)
    status = await admin.get(f"/api/admin/connectors/{SLUG}/crawl")
    assert status.status_code == 404 and status.json()["code"] == "no_read_mapping"
    assert (await admin.post(f"/api/admin/connectors/{SLUG}/crawl")).status_code == 409
    await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    assert (await admin.get(f"/api/admin/connectors/{SLUG}/crawl")).json()["status"] == "idle"
    queued = await admin.post(f"/api/admin/connectors/{SLUG}/crawl")
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    assert (await admin.get(f"/api/admin/connectors/{SLUG}/crawl")).json()["status"] == "queued"

    before = dt.datetime.now(dt.UTC)
    row = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert row.status == "ready" and row.consistency == "crawled" and row.advisory
    assert row.label["consistency"] == "crawled" and row.as_of_range is not None
    assert before <= dt.datetime.fromisoformat(row.as_of_range["from"]) <= \
        dt.datetime.fromisoformat(row.as_of_range["to"]) <= dt.datetime.now(dt.UTC)
    assert row.source_snapshot_id == f"crawl:{row.id}"
    assert {k: v for k, v in row.row_counts.items() if v} == {
        "location": 7, "item": 8, "po_line": 3, "inventory_position": 3, "demand_forecast": 2, "allocation": 2}
    assert row.crawl is not None and row.crawl["truncated"] == [] and row.crawl["lookups"]["po_supplier"] == 2
    assert not lite.writes and {t for t, _a in lite.calls} == {"list_items"}
    current = await scn_sync.current_snapshot(TENANT_B, SLUG)
    assert current is not None and current.id == row.id and current.consistency == "crawled"
    async with tenant_session(TENANT_B) as s:
        po = (await s.execute(text("SELECT supplier_code, dest_dc_code, ex_factory_date FROM scn_po_line "
                                   "WHERE snapshot_id = CAST(:s AS uuid) AND source_id = 'PO-1001#2'"),
                              {"s": row.id})).one()
        cl = (await s.execute(text("SELECT parent_code FROM scn_location WHERE snapshot_id = CAST(:s AS uuid) "
                                   "AND source_id = 'cluster:NL-URB-B'"), {"s": row.id})).scalar_one()
    assert (po[0], po[1], str(po[2])) == ("SUP-VN-01", "DE1", "2026-10-20") and cl == "NL1"
    st = (await admin.get(f"/api/admin/connectors/{SLUG}/crawl")).json()
    assert st["status"] == "succeeded" and st["consistency"] == "crawled" and st["snapshot_id"] == row.id
    assert st["as_of_range"] == row.as_of_range and st["read_mapping_version"] == 1
    assert st["entities"]["location"] == 7

    # a second crawl supersedes the first (same pointer / GC rules as tier B)
    lite.data["allocation"][0]["qty"] = 55
    second = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert second.id != row.id
    old = await scn_sync.get_snapshot(TENANT_B, row.id)
    assert old is not None and old.status == "superseded"
    async with tenant_session(TENANT_B) as s:
        qty = (await s.execute(text("SELECT qty FROM scn_allocation WHERE snapshot_id = CAST(:s AS uuid) "
                                    "AND code = 'AL-2001'"), {"s": second.id})).scalar_one()
    assert qty == 55


async def test_crawl_failures_keep_the_previous_snapshot(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _connector(admin, lite)
    await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    good = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")

    async def failing(code: str) -> str:
        with pytest.raises(scn_sync.SyncFailed) as err:
            await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
        assert err.value.code == code, err.value
        cur = await scn_sync.current_snapshot(TENANT_B, SLUG)
        assert cur is not None and cur.id == good.id
        failed = err.value.snapshot
        assert failed.status == "failed"
        for entity in ("location", "item", "po_line", "inventory_position", "allocation"):
            assert await _count(entity, failed.id) == 0, entity  # rows written so far are purged
        await _clear_backoff()
        return failed.error or ""

    # a tool error part-way (after location + item rows were streamed)
    lite.fail["list_items"] = [("inventory", 1, "upstream timeout")]
    assert "upstream timeout" in await failing("tool_error")
    # FK sanity: inventory at a DC the crawl did not load
    lite.data["dc"] = [d for d in lite.data["dc"] if d["code"] != "NL1"]
    assert "inventory_position.location" in await failing("fk_violation")
    lite.data["dc"] = copy.deepcopy(LITE_DATA["dc"])
    # a row the scn/1.0 model refuses
    lite.data["inventory"][0]["on_hand"] = -5
    assert "inventory_position" in await failing("data_invalid")
    lite.data["inventory"][0]["on_hand"] = 320
    # the read tool was reclassified as a write tool since the mapping was saved: never called
    r = await admin.patch(f"/api/admin/connectors/{SLUG}/tools/list_items", json={"access": "write"})
    assert r.status_code == 200, r.text
    lite.calls.clear()
    assert "write tool" in await failing("mapping_invalid")
    assert lite.calls == []
    got = (await admin.get(f"/api/admin/connectors/{SLUG}/mappings/read")).json()
    assert got["problems"] and "write" in got["problems"][0]
    st = (await admin.get(f"/api/admin/connectors/{SLUG}/crawl")).json()
    assert st["status"] == "failed" and st["error"] and st["snapshot_id"] == good.id


async def _clear_backoff() -> None:
    async with tenant_session(TENANT_B) as s:
        await s.execute(text("UPDATE scn_sync_state SET retry_at = NULL, attempts = 0 WHERE tenant = :t"),
                        {"t": TENANT_B})


# ───────────────────────────── action dry-run ─────────────────────────────


async def test_action_dry_run_never_executes(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _connector(admin, lite)
    url = f"/api/admin/connectors/{SLUG}/mappings/action/dry-run"
    sample = {"op": "allocation.set_qty", "target_ref": "AL-2001", "expected_version": 3, "payload": {"qty": 30}}
    none = await admin.post(url, json=sample)
    assert none.status_code == 200 and none.json()["mapped"] is False and "guided" in none.json()["guided_reason"]
    await admin.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    lite.calls.clear()
    r = await admin.post(url, json=sample)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mapped"] is True and out["executed"] is False and out["idempotency"] == "best_effort"
    args = out["call"]["args"]
    assert out["call"]["tool"] == "update_item" and args["type"] == "allocation" and args["key"] == "AL-2001"
    assert args["fields_json"] == '{"qty":30}' and args["expected_version"] == 3
    assert args["request_id"].startswith("dryrun-") and out["request_id_arg"] == "request_id"
    rb = out["readback"]
    assert rb["executed"] is True and rb["ok"] is True and rb["items_found"] == 1 and rb["matched"] is False
    assert rb["call"] == {"tool": "get_item", "args": {"type": "allocation", "key": "AL-2001"}}
    assert lite.writes == [] and [t for t, _a in lite.calls] == ["get_item"]
    # the read-back matches once the effect is visible
    r = await admin.post(url, json={**sample, "payload": {"qty": 40}})
    assert r.json()["readback"]["matched"] is True
    # transfer_order.create: canonical lines_json, a filtered list_items read-back
    to = {"op": "transfer_order.create", "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-12",
                                                      "lines": [{"sku": "PUF-W-BLK-M", "qty": 10}]}}
    r = await admin.post(url, json=to)
    assert r.status_code == 200, r.text
    call = r.json()["call"]
    assert call["tool"] == "create_transfer_order" and call["args"]["lines_json"] == '[{"qty":10,"sku":"PUF-W-BLK-M"}]'
    rb = r.json()["readback"]
    assert rb["call"]["args"]["filter_json"] == \
        '{"created_via":"mcp-lite","from_dc":"DE1","ship_date":"2026-10-12","to_dc":"NL1"}'
    assert rb["matched"] is False and rb["items_found"] == 0  # TO-0001 was created via the api, not mcp-lite
    same_rid = (await admin.post(url, json=to)).json()["call"]["args"]["request_id"]
    assert same_rid == call["args"]["request_id"]  # deterministic per sample
    # run_readback false: nothing at all is called
    lite.calls.clear()
    r = await admin.post(url, json={**sample, "run_readback": False})
    assert r.json()["readback"]["executed"] is False and lite.calls == []
    # unmapped op → guided; invalid sample payload → 422
    g = (await admin.post(url, json={"op": "po_line.reschedule", "target_ref": "PO-1001#1", "expected_version": 1,
                                     "payload": {"eta_dc": "2026-12-01"}})).json()
    assert g["mapped"] is False and "guided step" in g["guided_reason"]
    bad = await admin.post(url, json={"op": "allocation.set_qty", "target_ref": "AL-2001", "expected_version": 3,
                                      "payload": {"qty": -1}})
    assert bad.status_code == 422 and bad.json()["code"] == "payload_invalid"
    # a disabled read-back tool is never called, the write tool never either
    r = await admin.patch(f"/api/admin/connectors/{SLUG}/tools/get_item", json={"status": "disabled"})
    assert r.status_code == 200
    stale = await admin.post(url, json=sample)
    assert stale.status_code == 409 and stale.json()["code"] == "mapping_stale"
    assert lite.writes == []


# ───────────────────────────── per-op commit ledger ─────────────────────────────


async def test_commit_ledger_per_op_states(platform: None) -> None:
    case_id = await repo.create_case("ledger", created_by="test", tenant=TENANT_A)
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "x"}], "opt-A", tenant=TENANT_A)
    items = [{"seq": i, "op": "allocation.set_qty", "target_ref": f"AL-{i}", "expected_version": 1,
              "payload": {"qty": 5}} for i in (1, 2, 3)]
    cs_id = await repo.create_change_set(case_id, 1, "opt-A", SLUG, items, "h" * 64, tenant=TENANT_A)
    now = dt.datetime.now(dt.UTC)
    base = {"change_set_id": cs_id, "op": "allocation.set_qty", "connector": SLUG, "tool": "update_item",
            "idempotency": "best_effort", "attempts": 1, "request_id": "rq-1", "updated_at": now.isoformat()}
    await repo.ledger_record({**base, "line_seq": 1, "state": "attempted"}, tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 1, "state": "succeeded", "ext_ref": "AL-1@v2"}, tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 2, "state": "in_doubt", "error": "response lost"}, tenant=TENANT_A)
    readback = {"at": now.isoformat(), "ok": True, "found": False, "detail": "qty still 4"}
    await repo.ledger_record({**base, "line_seq": 2, "state": "succeeded", "attempts": 2, "readback": readback},
                             tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 3, "state": "failed", "idempotency": "none", "request_id": None,
                              "error": "version_conflict"}, tenant=TENANT_A)
    with pytest.raises(ValueError):  # none is never auto-retried
        await repo.ledger_record({**base, "line_seq": 3, "state": "attempted", "idempotency": "none",
                                  "request_id": None, "attempts": 2}, tenant=TENANT_A)
    with pytest.raises(ValueError):  # best_effort retries only after a successful read-back
        await repo.ledger_record({**base, "line_seq": 1, "state": "attempted", "attempts": 2}, tenant=TENANT_A)
    entries = {e["line_seq"]: e for e in await repo.ledger_entries(cs_id, tenant=TENANT_A)}
    assert entries[1]["state"] == "succeeded" and entries[1]["ext_ref"] == "AL-1@v2" and entries[1]["attempts"] == 1
    assert entries[2]["attempts"] == 2 and entries[2]["readback"]["found"] is False and entries[2]["readback"]["ok"]
    assert entries[3]["state"] == "failed" and entries[3]["error"] == "version_conflict"
    # change-set commits (no per-op columns) still use the plain mark
    await repo.ledger_mark(cs_id, 9, "verified", "CS-1", tenant=TENANT_A)
    assert 9 not in {e["line_seq"] for e in await repo.ledger_entries(cs_id, tenant=TENANT_A)}
    rows = await repo.list_ledger(cs_id, tenant=TENANT_A)
    assert [r["state"] for r in rows] == ["succeeded", "succeeded", "failed", "verified"]
    async with tenant_session(TENANT_B) as s:  # RLS: invisible to another tenant
        n = (await s.execute(text("SELECT count(*) FROM commit_ledger WHERE change_set_id = :c"),
                             {"c": cs_id})).scalar_one()
    assert n == 0


# ───────────────────────────── credential rotation ─────────────────────────────


async def test_static_header_secret_rotation(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    """A vendor API key is rotated in place (the connector keeps its tools, mappings and bindings);
    the secret is never echoed nor audited, and the test connection uses the new value."""
    from e1_mcp_server import API_KEY

    await _connector(admin, lite)
    url = f"/api/admin/connectors/{SLUG}/secret"
    wrong = await admin.put(url, json={"secret": "mx_wrong_key_value"})
    assert wrong.status_code == 200 and wrong.json()["rotated"] is True, wrong.text
    assert "mx_wrong_key_value" not in wrong.text
    failed = (await admin.post(f"/api/admin/connectors/{SLUG}/test")).json()
    assert failed["ok"] is False  # the server refuses the wrong key
    ok = await admin.put(url, json={"secret": API_KEY})
    assert ok.status_code == 200, ok.text
    passed = (await admin.post(f"/api/admin/connectors/{SLUG}/test")).json()
    assert passed["ok"] is True and passed["tier"] == ["A"], passed
    tools = {t["name"]: t for t in (await admin.get(f"/api/admin/connectors/{SLUG}/tools")).json()["items"]}
    assert tools["update_item"]["status"] == "approved"  # nothing but the secret changed
    audit = (await admin.get("/api/admin/audit?action=connector.secret_rotated&limit=5")).json()["items"]
    assert audit and all(API_KEY not in str(a) and "mx_wrong_key_value" not in str(a) for a in audit)
    bad = await admin.put(url, json={"secret": "bad\u0007value"})
    assert bad.status_code == 422
    assert (await admin.put("/api/admin/connectors/no-such-connector/secret", json={"secret": "x"})).status_code == 404
