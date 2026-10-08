"""Back-office RBAC + multi-tenant isolation (contract 70 §7, API level): tenant-admin vs
platform-admin vs planner/compliance (403), other tenants' objects 404 (nora / a northwind tenant
admin vs demo-retail and vice versa), explicit + audited platform-admin cross-tenant access, CSRF and
Origin on mutations, audit listing."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import PUBLIC, TENANT_A, TENANT_B
from keelson_contracts.blocks import make_block
from keelson_contracts.common import new_block_id
from keelson_engine.core import connectors, interrupts, repo
from keelson_engine.core.db import tenant_session
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

ADMIN_GETS = [
    "/api/admin/agents/spec", "/api/admin/agents/spec/versions", "/api/admin/agents/catalog",
    "/api/admin/gateway/models", "/api/admin/gateway/tenant", "/api/admin/gateway/usage",
    "/api/admin/connectors", "/api/admin/audit",
]


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, session_browser: Any) -> dict[str, Browser]:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await reset_specs()
    out = {n: await browser_factory(n) for n in ("sam", "ava", "priya", "lena", "marco", "nora")}
    out["nina"] = await session_browser("nina", ["tenant-admin"], [TENANT_B])
    return out


async def test_roles(people: dict[str, Browser], api: httpx.AsyncClient) -> None:
    api.cookies.clear()
    assert (await api.get("/api/admin/agents/spec")).status_code == 401
    for who in ("priya", "lena", "marco", "nora"):
        for url in ADMIN_GETS:
            r = await people[who].get(url)
            assert r.status_code == 403, (who, url, r.text)
            assert r.json()["code"] == "forbidden"
    r = await people["priya"].put("/api/admin/agents/spec", json={"base_version": "default",
                                                                 "spec_patch": {"history_messages": 2}})
    assert r.status_code == 403
    assert (await people["sam"].get("/api/admin/connectors")).status_code == 200
    assert (await people["ava"].get("/api/admin/connectors")).status_code == 200


async def test_csrf_and_origin(people: dict[str, Browser], api: httpx.AsyncClient) -> None:
    sam = people["sam"]
    body = {"base_version": "default", "spec_patch": {"history_messages": 2}}
    no_csrf = await api.put("/api/admin/agents/spec", json=body,
                            headers={"Cookie": f"keelson_session={sam.cookie}", "Origin": PUBLIC})
    assert no_csrf.status_code == 403 and no_csrf.json()["code"] == "csrf_failed"
    evil = await api.put("/api/admin/agents/spec", json=body, headers={
        "Cookie": f"keelson_session={sam.cookie}", "Origin": "http://evil.test", "X-CSRF-Token": sam.csrf})
    assert evil.status_code == 403 and evil.json()["code"] == "bad_origin"
    no_csrf = await api.post("/api/admin/connectors/merxa/disable",
                             headers={"Cookie": f"keelson_session={sam.cookie}", "Origin": PUBLIC})
    assert no_csrf.status_code == 403
    assert (await connectors.get_connector(TENANT_A, "merxa")).status == "active"


async def test_tenant_admin_isolation(people: dict[str, Browser]) -> None:
    nina, sam = people["nina"], people["sam"]
    items = (await nina.get("/api/admin/connectors")).json()["items"]
    assert "merxa" not in {c["slug"] for c in items}
    for method, url in (("get", "/api/admin/connectors/merxa"), ("get", "/api/admin/connectors/merxa/tools"),
                        ("get", "/api/admin/connectors/merxa/events"), ("post", "/api/admin/connectors/merxa/test"),
                        ("post", "/api/admin/connectors/merxa/disable"), ("delete", "/api/admin/connectors/merxa")):
        r = await getattr(nina, method)(url)
        assert r.status_code == 404, (method, url, r.text)
    r = await nina.patch("/api/admin/connectors/merxa/tools/get_inventory", json={"status": "disabled"})
    assert r.status_code == 404
    # ?tenant= is only honoured for platform admins; anyone else gets 404
    assert (await nina.get("/api/admin/connectors", params={"tenant": TENANT_A})).status_code == 404
    assert (await sam.get("/api/admin/agents/spec", params={"tenant": TENANT_B})).status_code == 404
    r = await sam.put("/api/admin/agents/spec", params={"tenant": TENANT_B},
                      json={"base_version": "default", "spec_patch": {"history_messages": 2}})
    assert r.status_code == 404
    assert (await sam.get("/api/admin/connectors", params={"tenant": TENANT_A})).status_code == 200  # own

    # specs are per tenant
    r = await nina.put("/api/admin/agents/spec", json={"base_version": "default",
                                                      "spec_patch": {"history_messages": 3}})
    assert r.status_code == 201, r.text
    assert (await sam.get("/api/admin/agents/spec")).json()["version"] == "default"
    assert (await sam.get("/api/admin/agents/spec/versions")).json() == []
    assert (await sam.get(f"/api/admin/agents/spec/versions/{r.json()['version']}")).status_code == 404
    audit = (await nina.get("/api/admin/audit")).json()["items"]
    assert audit and all(a["tenant"] == TENANT_B for a in audit)
    assert not [a for a in (await sam.get("/api/admin/audit", params={"limit": 200})).json()["items"]
                if a["tenant"] != TENANT_A]
    await reset_specs()


async def test_platform_admin_cross_tenant_is_explicit_and_audited(people: dict[str, Browser]) -> None:
    ava = people["ava"]
    assert (await ava.get("/api/admin/connectors", params={"tenant": "no-such-tenant"})).status_code == 404
    assert (await ava.get("/api/admin/connectors", params={"tenant": "Bad Slug"})).status_code == 404
    r = await ava.get("/api/admin/connectors", params={"tenant": TENANT_B})
    assert r.status_code == 200 and "merxa" not in {c["slug"] for c in r.json()["items"]}
    put = await ava.put("/api/admin/agents/spec", params={"tenant": TENANT_B},
                        json={"base_version": "default", "spec_patch": {"history_messages": 4}})
    assert put.status_code == 201, put.text
    async with tenant_session(TENANT_B) as s:
        rows = (await s.execute(text(
            "SELECT actor_username, target, after FROM admin_audit WHERE action = 'admin.cross_tenant' "
            "ORDER BY id DESC LIMIT 2"))).all()
        spec_audit = (await s.execute(text(
            "SELECT actor_username FROM admin_audit WHERE action = 'agent_spec.update' ORDER BY id DESC LIMIT 1"
        ))).scalar_one()
    assert [r[0] for r in rows] == ["ava", "ava"] and rows[0][1] == "PUT /api/admin/agents/spec"
    assert rows[0][2] == {"home_tenant": TENANT_A} and spec_audit == "ava"
    # own tenant: no cross-tenant audit row
    assert (await ava.get("/api/admin/connectors", params={"tenant": TENANT_A})).status_code == 200
    async with tenant_session(TENANT_A) as s:
        n = (await s.execute(text("SELECT count(*) FROM admin_audit WHERE action = 'admin.cross_tenant'"))).scalar()
    assert n == 0
    await reset_specs()


async def test_m1_objects_are_404_across_tenants(people: dict[str, Browser]) -> None:
    """nora (northwind) vs demo-retail cases/events/blocks/interrupts/connectors, and vice versa."""
    priya, nora = people["priya"], people["nora"]
    case_a = (await priya.post("/api/cases", json={"title": "A only"})).json()["id"]
    block_id = new_block_id()
    env = make_block("narrative", {"markdown": "x"}, block_id=block_id, title="t",
                     provenance={"kind": "inference", "engine": "keelson-compose@0.1", "as_of": "2026-10-05"})
    await repo.save_block(case_a, None, {**env.model_dump(mode="json", by_alias=True),
                                         "data_ref": f"/api/blocks/{block_id}/data"}, data={"rows": [1]},
                          tenant=TENANT_A)
    msg = await priya.post(f"/api/cases/{case_a}/messages", json={"text": "hi"}, headers={"Idempotency-Key": "k-a"})
    await interrupts.create_interrupt(
        tenant=TENANT_A, case_id=case_a, run_id=msg.json()["run_id"], kind="approach",
        interrupt_id=f"approach-{case_a}-1", payload={}, quorum_spec={}, plan_version=None, change_set_hash=None,
        invocation_id="inv-x")
    assert (await priya.get(f"/api/blocks/{block_id}/data")).status_code == 200
    for url in (f"/api/cases/{case_a}", f"/api/cases/{case_a}/events", f"/api/blocks/{block_id}/data",
                "/api/connectors/merxa/link"):
        assert (await nora.get(url)).status_code == 404, url
    r = await nora.post(f"/api/interrupts/approach-{case_a}-1/decision", json={"decision": "confirm"})
    assert r.status_code == 404
    r = await nora.post(f"/api/cases/{case_a}/messages", json={"text": "x"}, headers={"Idempotency-Key": "k-n"})
    assert r.status_code == 404
    assert "merxa" not in {c["slug"] for c in (await nora.get("/api/connectors")).json()["items"]}

    case_b = (await nora.post("/api/cases", json={"title": "B only"})).json()["id"]
    assert (await priya.get(f"/api/cases/{case_b}")).status_code == 404
    assert (await priya.get(f"/api/cases/{case_b}/events")).status_code == 404
    assert case_b not in {c["id"] for c in (await priya.get("/api/cases")).json()["items"]}


async def test_audit_listing(people: dict[str, Browser]) -> None:
    sam, ava = people["sam"], people["ava"]
    assert (await sam.post("/api/admin/connectors/merxa/disable")).status_code == 200
    assert (await sam.post("/api/admin/connectors/merxa/activate")).json()["status"] == "active"
    page = (await sam.get("/api/admin/audit", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["next_before"] == page["items"][0]["id"]
    nxt = (await sam.get("/api/admin/audit", params={"limit": 1, "before": page["next_before"]})).json()
    assert nxt["items"][0]["id"] < page["items"][0]["id"]
    filtered = (await sam.get("/api/admin/audit", params={"action": "connector.", "limit": 200})).json()["items"]
    assert filtered and all(i["action"].startswith("connector.") for i in filtered)
    by_actor = (await sam.get("/api/admin/audit", params={"actor": "sam", "limit": 200})).json()["items"]
    assert all(i["actor_username"] == "sam" for i in by_actor)
    assert (await sam.get("/api/admin/audit", params={"scope": "platform"})).status_code == 403
    platform_rows = (await ava.get("/api/admin/audit", params={"scope": "platform"})).json()["items"]
    assert all(i["tenant"] is None for i in platform_rows)
