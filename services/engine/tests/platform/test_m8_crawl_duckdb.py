"""M8 crawl through the DuckDB mapping compiler against the ``/mcp-lite`` FastMCP fake and Postgres
(``docs/contracts/121-m8-mapping-compiler.md`` §2): both engines load identical rows, the compiler
pages each distinct source once, raw pages are staged (current snapshot + latest failed attempt
only), failures write nothing and keep the previous snapshot, the byte cap, the explain and the
live / staged preview API (the staged one works with the connector disabled and calls nobody)."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import HAVE_PACKS, REPO_PACKS, TENANT_A, TENANT_B
from e1_lite_server import LITE_DATA, LiteServer, drop_lite, register_lite
from keelson_contracts import scn
from keelson_engine.config import Settings
from keelson_engine.core import scn_sync
from keelson_engine.core.db import tenant_session
from keelson_engine.core.packs import load_pack, load_pack_mappings

pytestmark = [pytest.mark.asyncio(loop_scope="session"),
              pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")]

SLUG = "m8-lite-erp"
BASE = f"/api/admin/connectors/{SLUG}"


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


def _read() -> dict[str, Any]:
    return load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))["read"]


async def _setup(admin: Browser, lite: LiteServer) -> None:
    await register_lite(admin, lite, TENANT_B, SLUG)
    r = await admin.post(f"{BASE}/mappings/from-pack", json={"pack": "foreign-erp-lite", "kinds": ["read"]})
    assert r.status_code == 200, r.text


async def _rows(sid: str) -> dict[str, set[tuple[Any, ...]]]:
    out: dict[str, set[tuple[Any, ...]]] = {}
    async with tenant_session(TENANT_B) as s:
        for entity in scn.SCN_ENTITIES:
            cols = ", ".join(scn.SCN_COLUMNS[entity])
            rows = (await s.execute(text(f"SELECT {cols} FROM scn_{entity} WHERE snapshot_id = CAST(:s AS uuid)"),
                                    {"s": sid})).all()
            if rows:
                out[entity] = {tuple(r) for r in rows}
    return out


async def _raw_sets() -> list[dict[str, Any]]:
    async with tenant_session(TENANT_B) as s:
        rows = (await s.execute(text(
            "SELECT r.snapshot_id::text AS sid, r.status, r.pages, r.items, r.bytes, sn.status AS snap, "
            "(SELECT count(*) FROM scn_raw_page p WHERE p.tenant = r.tenant AND p.snapshot_id = r.snapshot_id) AS n "
            "FROM scn_raw_set r JOIN scn_snapshot sn ON sn.tenant = r.tenant AND sn.id = r.snapshot_id "
            "WHERE r.tenant = :t AND r.connector = :c ORDER BY r.created_at"), {"t": TENANT_B, "c": SLUG})).mappings().all()
    return [dict(r) for r in rows]


async def _clear_backoff() -> None:
    async with tenant_session(TENANT_B) as s:
        await s.execute(text("UPDATE scn_sync_state SET retry_at = NULL, attempts = 0 WHERE tenant = :t"),
                        {"t": TENANT_B})


async def _fails(code: str) -> Any:
    with pytest.raises(scn_sync.SyncFailed) as err:
        await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert err.value.code == code, err.value
    await _clear_backoff()
    return err.value.snapshot


async def test_both_engines_load_identical_rows(admin: Browser, lite: LiteServer, dev_hosts: None,
                                                settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(admin, lite)
    monkeypatch.setattr(settings, "scn_mapping_engine", "interpreter")
    lite.calls.clear()
    m7 = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    m7_calls = len(lite.calls)
    assert m7.crawl is not None and m7.crawl["engine"] == "interpreter" and "sources" not in m7.crawl
    assert await _raw_sets() == []  # the interpreter keeps no raw pages
    monkeypatch.setattr(settings, "scn_mapping_engine", "duckdb")
    lite.calls.clear()
    m8 = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert len(lite.calls) < m7_calls and not lite.writes and {t for t, _a in lite.calls} == {"list_items"}
    assert m8.row_counts == m7.row_counts
    assert await _rows(m8.id) == await _rows(m7.id)
    crawl = m8.crawl or {}
    assert crawl["engine"] == "duckdb" and crawl["mapping_version"] == 1 and len(crawl["mapping_hash"]) == 64
    assert crawl["truncated"] == [] and crawl["lookups"]["po_supplier"] == 2
    sources = {s["tool"] + ":" + str(s["key"]) for s in crawl["sources"]}
    assert len(sources) == len(crawl["sources"]) == 9  # styles and stores are paged once
    assert crawl["pages"] == len(lite.calls) and m8.consistency == "crawled"
    assert all(c["severity"] == "warning" for c in crawl["checks"])
    sets = await _raw_sets()
    assert [(s["sid"], s["status"]) for s in sets] == [(m8.id, "complete")]
    assert sets[0]["n"] == sets[0]["pages"] == 9 and sets[0]["items"] == sum(len(v) for k, v in LITE_DATA.items()
                                                                               if k != "transfer_order")
    # the kill switch: a crawl by the interpreter supersedes the compiled one, whose raw set must not
    # live on as "the last crawl" of a staged preview
    monkeypatch.setattr(settings, "scn_mapping_engine", "interpreter")
    back = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert back.crawl is not None and back.crawl["engine"] == "interpreter"
    assert await _raw_sets() == []
    r = await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged"})
    assert r.status_code == 404 and r.json()["code"] == "no_staged_data"


async def test_raw_set_retention_and_failures_write_nothing(admin: Browser, lite: LiteServer,
                                                            dev_hosts: None) -> None:
    await _setup(admin, lite)
    await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    second = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert [s["sid"] for s in await _raw_sets()] == [second.id]  # a success keeps only its own set

    # mapping_failed: the interpreter's item index and message; no row written, the set is kept
    bad = copy.deepcopy(_read())
    bad["entities"][1]["fields"]["src_version"] = "int($.name)"
    cur = (await admin.get(f"{BASE}/mappings/read")).json()["version"]
    assert (await admin.put(f"{BASE}/mappings/read", json={"base_version": cur, "mapping": bad})).status_code == 201
    failed = await _fails("mapping_failed")
    assert "entities[1] (location) item 0: src_version: int()" in (failed.error or "")
    assert await _rows(failed.id) == {}
    assert (await scn_sync.current_snapshot(TENANT_B, SLUG)).id == second.id  # type: ignore[union-attr]
    sets = {s["sid"]: s for s in await _raw_sets()}
    assert set(sets) == {second.id, failed.id} and sets[failed.id]["status"] == "complete"
    staged = (await admin.get(f"{BASE}/staged")).json()
    assert staged["snapshot_id"] == failed.id and staged["status"] == "failed" and staged["pages"] == 9

    # duplicate source_id (two store rows → one id) and an FK error: nothing written either
    dup = copy.deepcopy(_read())
    dup["entities"][1]["fields"]["source_id"] = "'store:' + $.market_code"
    cur += 1
    assert (await admin.put(f"{BASE}/mappings/read", json={"base_version": cur, "mapping": dup})).status_code == 201
    failed = await _fails("data_invalid")
    assert "duplicate source_id store:DE" in (failed.error or "") and await _rows(failed.id) == {}
    cur += 1
    assert (await admin.put(f"{BASE}/mappings/read", json={"base_version": cur, "mapping": _read()})).status_code == 201
    lite.data["dc"] = [d for d in lite.data["dc"] if d["code"] != "NL1"]
    failed = await _fails("fk_violation")
    assert "inventory_position.location" in (failed.error or "") and await _rows(failed.id) == {}
    lite.data["dc"] = copy.deepcopy(LITE_DATA["dc"])
    assert {s["sid"] for s in await _raw_sets()} == {second.id, failed.id}  # the latest failed attempt only
    third = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert [s["sid"] for s in await _raw_sets()] == [third.id]


async def test_truncation_and_the_raw_byte_cap(admin: Browser, lite: LiteServer, dev_hosts: None,
                                               settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(admin, lite)
    m = copy.deepcopy(_read())
    m["entities"][0]["source"].update(page_size=1, max_pages=1)
    assert (await admin.put(f"{BASE}/mappings/read", json={"base_version": 1, "mapping": m})).status_code == 201
    lite.data["inventory"] = [i for i in lite.data["inventory"] if i["location_code"] != "NL1"]
    row = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert row.crawl is not None and row.crawl["truncated"] == ["entities[0] (location)"]
    lite.data["inventory"] = copy.deepcopy(LITE_DATA["inventory"])
    monkeypatch.setattr(settings, "scn_raw_max_bytes", 400)
    failed = await _fails("too_many_rows")
    assert "SCN_RAW_MAX_BYTES" in (failed.error or "") and await _rows(failed.id) == {}


async def test_explain_and_previews(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _setup(admin, lite)
    assert (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged"})).json()["code"] == \
        "no_staged_data"
    assert (await admin.get(f"{BASE}/staged")).status_code == 404
    plan = (await admin.post(f"{BASE}/mappings/read/compile")).json()
    assert plan["engine"] == "duckdb" and len(plan["sources"]) == 9
    assert sum(len(s["used_by"]) for s in plan["sources"]) > len(plan["sources"])  # shared reads, fetched once
    assert {c["label"] for c in plan["checks"] if c["severity"] == "error" and c["code"] == "fk"} == {
        "po_line.sku_code → item(sku)", "inventory_position.location → location"}
    assert all(c["sql"] for e in plan["entities"] for c in e["columns"]) and plan["warnings"] == []
    draft = copy.deepcopy(_read())
    draft["entities"][0]["source"]["tool"] = "update_item"
    warned = (await admin.post(f"{BASE}/mappings/read/compile", json={"mapping": draft})).json()
    assert warned["warnings"][0].startswith("catalogue:")

    live = (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "live", "sample": 2})).json()
    assert live["ok"] is True and live["engine"] == "duckdb" and live["source"] == "live" and live["staged"] is None
    po = next(e for e in live["entities"] if e["entity"] == "po_line")
    assert po["rows"] == 3 and len(po["sample"]) == 2 and {c["column"] for c in po["columns"]} >= {"supplier_code"}
    assert next(c for c in po["columns"] if c["column"] == "factory_code")["null_bp"] == 3333

    await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert (await admin.post(f"{BASE}/disable")).status_code == 200
    lite.calls.clear()
    edited = copy.deepcopy(_read())
    edited["entities"][0]["fields"]["src_version"] = "int($.name)"
    staged = (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged", "mapping": edited})).json()
    assert lite.calls == []  # no vendor call, with the connector disabled
    assert staged["source"] == "staged" and staged["staged"]["status"] == "complete" and staged["capped"] is False
    dc = staged["entities"][0]
    assert staged["ok"] is False and dc["error"].startswith("mapping_failed: entities[0] (location) item 0")
    sv = next(c for c in dc["columns"] if c["column"] == "src_version")
    assert sv["failures"] == 2 and sv["first_failure"].startswith("item 0: int()")
    assert sum(e["rows"] for e in staged["entities"] if e["entity"] == "item") == 8
    capped = (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged",
                                                                      "max_items_per_source": 1})).json()
    assert capped["capped"] is True and capped["entities"][0]["rows"] == 1
    # FK check examples on the full staged data: a draft without the DC mapping
    nodc = copy.deepcopy(_read())
    del nodc["entities"][0]
    fk = (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged", "mapping": nodc})).json()
    check = next(c for c in fk["checks"] if c["label"] == "inventory_position.location → location")
    assert fk["ok"] is False and check["severity"] == "error" and check["examples"] == ["dc:DE1", "dc:NL1"]
    # a read the last crawl never made cannot be previewed from the staged pages
    other = copy.deepcopy(_read())
    other["entities"][0]["source"]["args"] = {"type": "transfer_order"}
    ns = (await admin.post(f"{BASE}/mappings/read/preview", json={"source": "staged", "mapping": other})).json()
    assert ns["ok"] is False and ns["error"].startswith("not_staged")
    r = await admin.post(f"{BASE}/mappings/read/preview", json={"source": "live"})
    assert r.status_code == 409 and r.json()["code"] == "connector_inactive"
    audit = (await admin.get("/api/admin/audit?action=connector.mapping.preview&limit=5")).json()["items"]
    assert audit and audit[0]["after"]["source"] == "staged"


async def test_tenant_isolation_of_raw_pages(admin: Browser, lite: LiteServer, dev_hosts: None) -> None:
    await _setup(admin, lite)
    await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    async with tenant_session(TENANT_A) as s:
        for table in ("scn_raw_set", "scn_raw_page"):
            n = (await s.execute(text(f"SELECT count(*) FROM {table} WHERE tenant = :t"), {"t": TENANT_B})).scalar_one()
            assert n == 0, table
        with pytest.raises(Exception):  # noqa: B017 - RLS WITH CHECK refuses a foreign tenant's row
            await s.execute(text("INSERT INTO scn_raw_page (tenant, snapshot_id, source_key, page_no, item_count, "
                                 "bytes, items) VALUES (:t, gen_random_uuid(), '0123456789abcdef', 0, 0, 2, '[]')"),
                            {"t": TENANT_B})
