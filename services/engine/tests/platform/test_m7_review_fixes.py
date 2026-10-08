"""Regression tests for the M7 review (docs/contracts/110-m7-backoffice-generic.md §1, §2, §4–§6):

* four-eyes **stale apply**: connector / tool / knowledge-base change requests record the state
  the requester saw (``base_version`` = a state fingerprint); approving after the connector was
  re-pointed, a tool definition drifted, a binding / classification moved or documents were added
  answers ``409 stale_base`` and the request stays pending (the approver never applies a change to
  something they did not review);
* an approver without a subject can never decide (self-approval is detected by subject);
* pending change requests are capped per tenant (429);
* the per-op commit's action mapping is re-checked against the current tool catalogue
  (``get_action_mapping`` drops ops whose tools are no longer approved / read-only read-backs);
* the activity timeline pages with an exact keyset (no row lost or repeated among rows that share
  a timestamp);
* users: a platform-admin by group membership is protected from tenant-admins;
* Signals: source subscriptions per tenant are bounded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from e1_browser import Browser
from e1_env import HAVE_PACKS, REPO_PACKS, TENANT_B
from e1_keycloak_admin import FakeKeycloakUsers
from e1_lite_server import LiteServer, drop_lite, register_lite
from keelson_engine.config import Settings
from keelson_engine.core import change_requests, connector_mappings, signals_admin, users
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.keycloak_admin import KeycloakAdmin

pytestmark = pytest.mark.asyncio(loop_scope="session")

CR = "/api/admin/change-requests"
SLUG = "m7-rv-stale"
KC = "http://keycloak:8080"


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
async def people(browser_factory: Any, session_browser: Any, platform: None) -> dict[str, Browser]:
    async with system_session() as s:
        await s.execute(text("DELETE FROM admin_change_request WHERE tenant = :t"), {"t": TENANT_B})
    return {"nadia": await session_browser("nadia", ["tenant-admin"], [TENANT_B]),
            "omar": await session_browser("omar", ["tenant-admin"], [TENANT_B]),
            "ava": await browser_factory("ava")}


async def _status(b: Browser, rid: str) -> str:
    return str((await b.get(f"{CR}/{rid}")).json()["status"])


# ───────────────────────────── stale apply ─────────────────────────────


async def test_tool_drift_and_moved_binding_refuse_approval(people: dict[str, Browser], lite: LiteServer,
                                                            dev_hosts: None) -> None:
    nadia, omar, ava = people["nadia"], people["omar"], people["ava"]
    await register_lite(nadia, lite, TENANT_B, SLUG)
    tools = f"/api/admin/connectors/{SLUG}/tools"

    r = await nadia.patch(f"{tools}/update_item", json={"access": "read"})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    assert str(r.json()["base_version"]).startswith("state:")
    # the vendor changed the tool's definition after the request (rug pull / drift)
    async with tenant_session(TENANT_B) as s:
        await s.execute(text("UPDATE connector_tool SET definition_hash = 'drifted' WHERE connector = :c "
                             "AND name = 'update_item'"), {"c": SLUG})
    stale = await omar.post(f"{CR}/{rid}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base", stale.text
    assert await _status(omar, rid) == "pending"
    listed = {t["name"]: t for t in (await nadia.get(tools)).json()["items"]}
    assert listed["update_item"]["access"] == "write"  # nothing applied
    assert (await omar.post(f"{CR}/{rid}/reject", json={"reason": "tool changed"})).status_code == 200

    # a fresh request against the current definition applies
    r = await nadia.patch(f"{tools}/update_item", json={"access": "read"})
    ok = await omar.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    assert (await ava.patch(f"{tools}/update_item?tenant={TENANT_B}", json={"access": "write"})).status_code == 200

    # binding: someone else moved the binding meanwhile → stale
    r = await nadia.put(f"{tools}/create_transfer_order/binding", json={"binding": "auto"})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    moved = await nadia.put(f"{tools}/create_transfer_order/binding", json={"binding": "deny"})
    assert moved.status_code == 200, moved.text
    stale = await omar.post(f"{CR}/{rid}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base", stale.text
    listed = {t["name"]: t for t in (await nadia.get(tools)).json()["items"]}
    assert listed["create_transfer_order"]["binding"] == "deny"


async def test_repointed_connector_refuses_trust_approval(people: dict[str, Browser], lite: LiteServer,
                                                          dev_hosts: None) -> None:
    nadia, omar = people["nadia"], people["omar"]
    await register_lite(nadia, lite, TENANT_B, SLUG)
    r = await nadia.patch(f"/api/admin/connectors/{SLUG}", json={"trust_level": "first_party"})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    async with tenant_session(TENANT_B) as s:
        url = (await s.execute(text("SELECT mcp_url FROM connector WHERE slug = :c"), {"c": SLUG})).scalar_one()
        await s.execute(text("UPDATE connector SET mcp_url = 'https://attacker.example/mcp' WHERE slug = :c"),
                        {"c": SLUG})
    stale = await omar.post(f"{CR}/{rid}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base", stale.text
    assert (await nadia.get(f"/api/admin/connectors/{SLUG}")).json()["trust_level"] == "third_party"
    # back to the server the requester saw: the same request applies
    async with tenant_session(TENANT_B) as s:
        await s.execute(text("UPDATE connector SET mcp_url = :u WHERE slug = :c"), {"u": url, "c": SLUG})
    ok = await omar.post(f"{CR}/{rid}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text


async def test_kb_delete_is_stale_after_changes(people: dict[str, Browser]) -> None:
    nadia, omar = people["nadia"], people["omar"]
    kb = await nadia.post("/api/admin/kb", json={"name": "Stale SOPs", "description": "ops"})
    assert kb.status_code == 201, kb.text
    kid = kb.json()["id"]
    r = await nadia.delete(f"/api/admin/kb/{kid}")
    assert r.status_code == 202 and r.json()["kind"] == "kb_delete", r.text
    async with tenant_session(TENANT_B) as s:
        await s.execute(text("UPDATE kb SET name = 'Renamed SOPs' WHERE id = CAST(:k AS uuid)"), {"k": kid})
    stale = await omar.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base", stale.text
    assert (await nadia.get(f"/api/admin/kb/{kid}/documents")).status_code == 200  # still there


# ───────────────────────────── decisions & limits ─────────────────────────────


async def test_actor_without_subject_never_decides(people: dict[str, Browser], lite: LiteServer,
                                                   dev_hosts: None) -> None:
    nadia = people["nadia"]
    await register_lite(nadia, lite, TENANT_B, SLUG)
    r = await nadia.patch(f"/api/admin/connectors/{SLUG}/tools/update_item", json={"access": "read"})
    rid = r.json()["id"]
    for sub in (None, "", "  "):
        with pytest.raises(AdminError) as err:
            await change_requests.approve(TENANT_B, rid, actor={"sub": sub, "username": "nadia"},
                                          actor_roles=["tenant-admin"], actor_tenants=[TENANT_B])
        assert err.value.status == 403
    assert await _status(nadia, rid) == "pending"


async def test_pending_change_requests_are_capped(people: dict[str, Browser], lite: LiteServer, dev_hosts: None,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    nadia = people["nadia"]
    await register_lite(nadia, lite, TENANT_B, SLUG)
    pending = await change_requests.pending_count(TENANT_B)
    monkeypatch.setattr(change_requests, "MAX_PENDING", pending + 1)
    assert (await nadia.patch(f"/api/admin/connectors/{SLUG}/tools/update_item",
                              json={"access": "read"})).status_code == 202
    over = await nadia.put(f"/api/admin/connectors/{SLUG}/tools/create_transfer_order/binding",
                           json={"binding": "auto"})
    assert over.status_code == 429 and over.json()["code"] == "too_many_pending_changes", over.text
    assert await change_requests.pending_count(TENANT_B) == pending + 1


# ───────────────────────────── action mapping re-check ─────────────────────────────


@pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")
async def test_action_mapping_drops_ops_whose_tools_changed(people: dict[str, Browser], lite: LiteServer,
                                                            dev_hosts: None) -> None:
    nadia = people["nadia"]
    await register_lite(nadia, lite, TENANT_B, SLUG)
    r = await nadia.post(f"/api/admin/connectors/{SLUG}/mappings/from-pack", json={"pack": "foreign-erp-lite"})
    assert r.status_code == 200, r.text
    assert REPO_PACKS.is_dir()
    mapping = await connector_mappings.get_action_mapping(TENANT_B, SLUG)
    assert mapping is not None and mapping.mapped_ops == ["allocation.set_qty", "transfer_order.create"]
    # the allocation read-back tool is disabled: that op is no longer applied per op (guided)
    assert (await nadia.patch(f"/api/admin/connectors/{SLUG}/tools/get_item",
                              json={"status": "disabled"})).status_code == 200
    mapping = await connector_mappings.get_action_mapping(TENANT_B, SLUG)
    assert mapping is not None and mapping.mapped_ops == ["transfer_order.create"]
    caps = (await nadia.get(f"/api/admin/connectors/{SLUG}/capabilities")).json()
    assert "allocation.set_qty" not in str(caps.get("mapped_ops", caps))
    # the transfer-order write tool is disabled too: nothing is mapped any more
    assert (await nadia.patch(f"/api/admin/connectors/{SLUG}/tools/create_transfer_order",
                              json={"status": "disabled"})).status_code == 200
    assert await connector_mappings.get_action_mapping(TENANT_B, SLUG) is None


# ───────────────────────────── activity keyset ─────────────────────────────


async def test_activity_pages_rows_sharing_a_timestamp(people: dict[str, Browser]) -> None:
    nadia = people["nadia"]
    async with tenant_session(TENANT_B) as s:
        await s.execute(text(
            "INSERT INTO admin_audit (tenant, at, actor_sub, actor_username, action, target) "
            "SELECT :t, timestamptz '2026-10-01 12:00:00+00', 'sub-x', 'pager', 'mapping.same_instant', "
            "'row:' || g FROM generate_series(1, 37) g"), {"t": TENANT_B})
    seen: list[str] = []
    cursor = None
    for _ in range(20):
        url = "/api/admin/audit/activity?category=admin&action=mapping.same_instant&limit=6"
        r = await nadia.get(url + (f"&before={cursor}" if cursor else ""))
        assert r.status_code == 200, r.text
        seen += [x["key"] for x in r.json()["items"]]
        cursor = r.json()["next_before"]
        if not cursor:
            break
    assert len(seen) == 37 and len(set(seen)) == 37


# ───────────────────────────── users: effective roles ─────────────────────────────


@pytest.fixture
def kc(http_mock: respx.Router) -> Any:
    fake = FakeKeycloakUsers(KC, "kc-pw")
    fake.install(http_mock)
    users.set_keycloak_admin(KeycloakAdmin(KC, "kc-pw"))
    yield fake
    users.set_keycloak_admin(None)


async def test_group_platform_admin_is_protected(people: dict[str, Browser], kc: FakeKeycloakUsers) -> None:
    nadia, ava = people["nadia"], people["ava"]
    uid = kc.add_user("grouped", tenant=[TENANT_B], roles=["planner"])
    kc.group_roles[uid] = {"platform-admin"}
    for body in ({"enabled": False}, {"roles": ["approver"]}):
        r = await nadia.patch(f"/api/admin/users/{uid}", json=body)
        assert r.status_code == 403, r.text
    assert (await nadia.delete(f"/api/admin/users/{uid}")).status_code == 403
    assert kc.users[uid].get("enabled", True) is True and kc.roles[uid] >= {"planner"}
    # a platform-admin may
    r = await ava.patch(f"/api/admin/users/{uid}?tenant={TENANT_B}", json={"enabled": False})
    assert r.status_code == 200, r.text


async def test_group_admin_reenable_is_four_eyes(people: dict[str, Browser], kc: FakeKeycloakUsers) -> None:
    nadia = people["nadia"]
    uid = kc.add_user("groupadmin", tenant=[TENANT_B], roles=["planner"])
    kc.group_roles[uid] = {"tenant-admin"}
    kc.users[uid]["enabled"] = False
    r = await nadia.patch(f"/api/admin/users/{uid}", json={"enabled": True})
    assert r.status_code == 202 and r.json()["kind"] == "user_roles", r.text
    assert kc.users[uid]["enabled"] is False


# ───────────────────────────── signals: bounded subscriptions ─────────────────────────────


async def test_source_subscriptions_are_bounded(platform: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async with system_session() as s:
        await s.execute(text("UPDATE tenant SET config = config - 'signal_sources' WHERE slug = :t"), {"t": TENANT_B})
    monkeypatch.setattr(signals_admin, "MAX_SUBSCRIPTIONS", 2)
    actor = {"sub": "s", "username": "nadia"}
    setting = signals_admin.SourceSetting(enabled=False)
    for sid in ("src-a", "src-b"):
        await signals_admin.put_source(TENANT_B, sid, setting, actor)
    await signals_admin.put_source(TENANT_B, "src-a", signals_admin.SourceSetting(enabled=True), actor)  # existing
    with pytest.raises(AdminError) as err:
        await signals_admin.put_source(TENANT_B, "src-c", setting, actor)
    assert err.value.status == 422 and err.value.code == "too_many_subscriptions"
    async with system_session() as s:
        await s.execute(text("UPDATE tenant SET config = config - 'signal_sources' WHERE slug = :t"), {"t": TENANT_B})


# ───────────────────────────── per-op ledger: no regression ─────────────────────────────


async def test_settled_ledger_lines_never_move_back(platform: None) -> None:
    import datetime as dt

    from e1_env import TENANT_A
    from keelson_engine.core import repo

    case_id = await repo.create_case("ledger-regress", created_by="test", tenant=TENANT_A)
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "x"}], "opt-A", tenant=TENANT_A)
    items = [{"seq": i, "op": "allocation.set_qty", "target_ref": f"AL-{i}", "expected_version": 1,
              "payload": {"qty": 5}} for i in (1, 2)]
    cs_id = await repo.create_change_set(case_id, 1, "opt-A", "m7-rv-ledger", items, "r" * 64, tenant=TENANT_A)
    now = dt.datetime.now(dt.UTC).isoformat()
    base = {"change_set_id": cs_id, "op": "allocation.set_qty", "connector": "m7-rv-ledger", "tool": "update_item",
            "idempotency": "best_effort", "attempts": 1, "request_id": "rq-1", "updated_at": now}
    found = {"at": now, "ok": True, "found": True, "detail": "qty 5"}
    await repo.ledger_record({**base, "line_seq": 1, "state": "attempted"}, tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 1, "state": "succeeded", "ext_ref": "AL-1@v2"}, tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 1, "state": "succeeded", "ext_ref": "AL-1@v2"}, tenant=TENANT_A)
    # a stale writer (e.g. a second worker) cannot erase that the line applied → it would be sent again
    for stale in ({"state": "attempted"}, {"state": "in_doubt", "error": "lost"}, {"state": "failed", "error": "x"}):
        with pytest.raises(repo.LedgerRegression) as err:
            await repo.ledger_record({**base, "line_seq": 1, **stale}, tenant=TENANT_A)
        assert err.value.current_state == "succeeded"
    await repo.ledger_record({**base, "line_seq": 1, "state": "verified", "readback": found}, tenant=TENANT_A)
    await repo.ledger_record({**base, "line_seq": 1, "state": "verified", "readback": found}, tenant=TENANT_A)
    with pytest.raises(repo.LedgerRegression):
        await repo.ledger_record({**base, "line_seq": 1, "state": "succeeded"}, tenant=TENANT_A)
    # attempts never decrease
    missing = {"at": now, "ok": True, "found": False, "detail": "not there"}
    await repo.ledger_record({**base, "line_seq": 2, "state": "in_doubt", "attempts": 2, "error": "lost",
                              "readback": missing},
                             tenant=TENANT_A)
    with pytest.raises(repo.LedgerRegression):
        await repo.ledger_record({**base, "line_seq": 2, "state": "in_doubt", "attempts": 1, "error": "lost2"},
                                 tenant=TENANT_A)
    states = {e["line_seq"]: (e["state"], e["attempts"]) for e in await repo.ledger_entries(cs_id, tenant=TENANT_A)}
    assert states == {1: ("verified", 1), 2: ("in_doubt", 2)}
