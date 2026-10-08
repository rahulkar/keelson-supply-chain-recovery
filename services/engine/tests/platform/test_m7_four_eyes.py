"""M7 four-eyes change requests, users admin and tenant suspension
(docs/contracts/110-m7-backoffice-generic.md §1, §2): a guarded change by a tenant-admin becomes a
pending request (202, nothing applied); self-approval is refused; a different tenant-admin or a
platform-admin approves and the change is re-validated against the current state (409 stale base,
the request stays pending); rejects need a reason; requests expire after 72 h; everything is
audited. Users through a mocked Keycloak admin API (tenant attribute, product roles only,
temporary password + UPDATE_PASSWORD never returned, cross-tenant and platform-admin limits,
admin grants are four-eyes); suspension blocks logins and API calls."""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import PUBLIC, TENANT_A, TENANT_B
from e1_keycloak_admin import FakeKeycloakUsers
from e1_lite_server import LiteServer, drop_lite, register_lite
from e1_litellm import FakeLiteLLM
from keelson_engine.config import Settings
from keelson_engine.core import change_requests, connectors, users
from keelson_engine.core.db import system_session
from keelson_engine.core.keycloak_admin import KeycloakAdmin
from test_api_connectors import TOOLS
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

SPEC = "/api/admin/agents/spec"
CR = "/api/admin/change-requests"
KC = "http://keycloak:8080"


@pytest_asyncio.fixture(loop_scope="session")
async def merxa_tools(platform: None) -> None:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await connectors.upsert_tool_snapshot(TENANT_A, "merxa", copy.deepcopy(TOOLS), auto_approve=True,
                                          untrusted=("list_supplier_messages",))


@pytest_asyncio.fixture(loop_scope="session")
async def admins(browser_factory: Any, session_browser: Any, platform: None) -> dict[str, Browser]:
    """sam (tenant-admin A), tia (second tenant-admin A), ava (platform-admin), priya (planner A),
    nadia + omar (tenant-admins B)."""
    await reset_specs(TENANT_A)
    out = {n: await browser_factory(n) for n in ("sam", "ava", "priya")}
    out["tia"] = await session_browser("tia", ["tenant-admin"], [TENANT_A])
    out["nadia"] = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
    out["omar"] = await session_browser("omar", ["tenant-admin"], [TENANT_B])
    async with system_session() as s:
        await s.execute(text("DELETE FROM admin_change_request"))
    return out


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def lite(db: None) -> AsyncIterator[LiteServer]:
    server = LiteServer()
    await server.start()
    yield server
    await server.stop()
    await drop_lite(TENANT_B, "erp-4eyes", "erp-egress")


@pytest.fixture
def dev_hosts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_dev_hosts", "127.0.0.1")


async def _put_spec(b: Browser, base: str, patch: dict[str, Any]) -> Any:
    return await b.put(SPEC, json={"base_version": base, "spec_patch": patch})


async def _audit(b: Browser, action: str) -> list[dict[str, Any]]:
    return (await b.get(f"/api/admin/audit?action={action}&limit=200")).json()["items"]


# ───────────────────────────── change requests ─────────────────────────────


async def test_change_request_lifecycle(admins: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                        merxa_tools: None) -> None:
    sam, tia, ava, priya = admins["sam"], admins["tia"], admins["ava"], admins["priya"]
    base = (await sam.get(SPEC)).json()
    lines = base["spec"]["policy"]["auto_max_lines"]
    r = await _put_spec(sam, base["version"], {"policy": {"auto_max_lines": lines + 5}})
    assert r.status_code == 202 and r.headers["X-Keelson-Change-Request"] == r.json()["id"], r.text
    cr = r.json()
    assert cr["status"] == "pending" and cr["requested_by"]["username"] == "sam" and cr["approver"] is None
    assert cr["diff"] and cr["base_version"] == base["version"]
    expires = dt.datetime.fromisoformat(cr["expires_at"]) - dt.datetime.fromisoformat(cr["requested_at"])
    assert expires == dt.timedelta(hours=72)
    assert (await sam.get(SPEC)).json()["version"] == base["version"]  # nothing applied

    queue = (await tia.get(f"{CR}?status=pending")).json()
    assert queue["pending"] >= 1 and cr["id"] in [x["id"] for x in queue["items"]]
    assert (await tia.get(f"{CR}/{cr['id']}")).json()["kind"] == "agent_spec"
    # four-eyes: the requester never approves; a planner never decides
    own = await sam.post(f"{CR}/{cr['id']}/approve", json={})
    assert own.status_code == 403 and own.json()["code"] == "self_approval"
    assert (await priya.post(f"{CR}/{cr['id']}/approve", json={})).status_code == 403
    # another tenant's admin does not even see it
    nadia = admins["nadia"]
    assert (await nadia.get(f"{CR}/{cr['id']}")).status_code == 404
    assert (await nadia.post(f"{CR}/{cr['id']}/approve", json={})).status_code == 404
    assert (await tia.get(f"{CR}/not-a-uuid")).status_code == 404
    # a second tenant-admin approves: applied atomically, re-validated
    ok = await tia.post(f"{CR}/{cr['id']}/approve", json={"reason": "fine"})
    assert ok.status_code == 200, ok.text
    done = ok.json()
    assert done["status"] == "applied" and done["approver"]["username"] == "tia"
    assert done["approver"]["role"] == "tenant-admin" and done["decided_at"]
    spec = (await sam.get(SPEC)).json()
    assert spec["spec"]["policy"]["auto_max_lines"] == lines + 5 and spec["version"] != base["version"]
    again = await tia.post(f"{CR}/{cr['id']}/approve", json={})
    assert again.status_code == 409 and again.json()["code"] == "not_pending"
    created = await _audit(tia, "change_request.created")
    applied = await _audit(tia, "change_request.applied")
    assert cr["id"] in {a["target"].split(":", 1)[1] for a in created}
    assert any(a["target"] == f"change_request:{cr['id']}" and a["actor_username"] == "tia" for a in applied)
    assert all(a["category"] == "approvals" for a in applied)
    cat = (await tia.get("/api/admin/audit?category=approvals&limit=200")).json()["items"]
    assert cat and any(a["action"].startswith("change_request.") for a in cat)
    assert all(a["action"].startswith(("approval.", "interrupt.", "decision.", "change_request.")) for a in cat)

    # stale base: the spec moved after the request → 409, the request stays pending
    cur = (await sam.get(SPEC)).json()
    r = await _put_spec(sam, cur["version"], {"policy": {"auto_max_units": cur["spec"]["policy"]["auto_max_units"] + 9}})
    assert r.status_code == 202
    stale_id = r.json()["id"]
    moved = await _put_spec(sam, cur["version"], {"models": {"intake": "extract-fast"}})
    assert moved.status_code == 201, moved.text
    stale = await tia.post(f"{CR}/{stale_id}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base", stale.text
    assert stale.json()["base_version"] == cur["version"]
    assert (await tia.get(f"{CR}/{stale_id}")).json()["status"] == "pending"
    # reject needs a reason; the requester may withdraw (reject) their own request
    assert (await tia.post(f"{CR}/{stale_id}/reject", json={})).json()["code"] == "reason_required"
    withdrawn = await sam.post(f"{CR}/{stale_id}/reject", json={"reason": "superseded"})
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "rejected"
    assert withdrawn.json()["decision_reason"] == "superseded"

    # platform-admins co-sign any tenant
    cur = (await sam.get(SPEC)).json()
    r = await _put_spec(sam, cur["version"], {"policy": {"approval_quorum": 0}})
    if r.status_code == 422:  # quorum floor: use the approver role instead
        r = await _put_spec(sam, cur["version"], {"policy": {"approver_role": "planner"}})
    assert r.status_code == 202, r.text
    signed = await ava.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert signed.status_code == 200 and signed.json()["approver"]["role"] == "platform-admin", signed.text


async def test_change_request_expiry(admins: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                     merxa_tools: None) -> None:
    sam, tia = admins["sam"], admins["tia"]
    cur = (await sam.get(SPEC)).json()
    r = await _put_spec(sam, cur["version"], {"policy": {"auto_max_lines": cur["spec"]["policy"]["auto_max_lines"] + 1}})
    assert r.status_code == 202
    rid = r.json()["id"]
    async with system_session() as s:
        await s.execute(text("UPDATE admin_change_request SET requested_at = now() - interval '73 hours', "
                             "expires_at = now() - interval '1 hour' WHERE id = CAST(:i AS uuid)"), {"i": rid})
    assert (await tia.get(f"{CR}/{rid}")).json()["status"] == "expired"
    late = await tia.post(f"{CR}/{rid}/approve", json={})
    assert late.status_code == 409 and late.json()["code"] in ("not_pending", "expired")
    assert any(a["target"] == f"change_request:{rid}" for a in await _audit(tia, "change_request.expired"))
    assert (await tia.get(f"{CR}?status=expired")).json()["items"][0]["id"] == rid
    assert await change_requests.expire_overdue() == 0  # idempotent


async def test_connector_guards_become_change_requests(admins: dict[str, Browser], lite: LiteServer,
                                                       dev_hosts: None) -> None:
    nadia, omar, ava = admins["nadia"], admins["omar"], admins["ava"]
    slug = "erp-4eyes"
    await register_lite(nadia, lite, TENANT_B, slug)
    tools = f"/api/admin/connectors/{slug}/tools"
    # reclassifying a write tool as read
    r = await nadia.patch(f"{tools}/update_item", json={"access": "read"})
    assert r.status_code == 202 and r.json()["kind"] == "connector_tool", r.text
    tool_cr = r.json()["id"]
    # binding auto on a write tool
    r = await nadia.put(f"{tools}/create_transfer_order/binding", json={"binding": "auto"})
    assert r.status_code == 202 and r.json()["kind"] == "tool_binding", r.text
    bind_cr = r.json()["id"]
    assert (await nadia.put(f"{tools}/get_item/binding", json={"binding": "auto"})).status_code == 200  # a read tool
    # first-party trust
    r = await nadia.patch(f"/api/admin/connectors/{slug}", json={"trust_level": "first_party"})
    assert r.status_code == 202 and r.json()["kind"] == "connector_trust", r.text
    trust_cr = r.json()["id"]
    assert "secret" not in str(r.json()).lower()
    # deleting the connector
    r = await nadia.delete(f"/api/admin/connectors/{slug}")
    assert r.status_code == 202 and r.json()["kind"] == "connector_delete", r.text
    delete_cr = r.json()["id"]
    listed = {t["name"]: t for t in (await nadia.get(tools)).json()["items"]}
    assert listed["update_item"]["access"] == "write"  # nothing applied yet

    for rid in (tool_cr, bind_cr):
        ok = await omar.post(f"{CR}/{rid}/approve", json={})
        assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    listed = {t["name"]: t for t in (await nadia.get(tools)).json()["items"]}
    assert listed["update_item"]["access"] == "read"
    # the platform admin co-signs from another tenant with ?tenant=
    ok = await ava.post(f"{CR}/{trust_cr}/approve?tenant={TENANT_B}", json={})
    assert ok.status_code == 200 and ok.json()["approver"]["role"] == "platform-admin", ok.text
    assert (await nadia.get(f"/api/admin/connectors/{slug}")).json()["trust_level"] == "first_party"
    ok = await omar.post(f"{CR}/{delete_cr}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    assert (await nadia.get(f"/api/admin/connectors/{slug}")).status_code == 404
    # re-validation on approve: a request against a connector that no longer exists stays pending
    await register_lite(nadia, lite, TENANT_B, slug)
    r = await nadia.patch(f"/api/admin/connectors/{slug}", json={"trust_level": "first_party"})
    rid = r.json()["id"]
    r = await ava.delete(f"/api/admin/connectors/{slug}?tenant={TENANT_B}")
    assert r.status_code == 204, r.text  # platform-admins delete directly
    gone = await omar.post(f"{CR}/{rid}/approve", json={})
    assert gone.status_code == 404, gone.text
    assert (await omar.get(f"{CR}/{rid}")).json()["status"] == "pending"


async def test_external_egress_activation_and_kb_delete(admins: dict[str, Browser], lite: LiteServer,
                                                        dev_hosts: None) -> None:
    nadia, omar = admins["nadia"], admins["omar"]
    slug = "erp-egress"
    await register_lite(nadia, lite, TENANT_B, slug)
    assert (await nadia.post(f"/api/admin/connectors/{slug}/disable")).status_code == 200
    r = await nadia.patch(f"/api/admin/connectors/{slug}/tools/list_items", json={"egress": "external"})
    assert r.status_code == 200, r.text
    r = await nadia.post(f"/api/admin/connectors/{slug}/activate")
    assert r.status_code == 202 and r.json()["kind"] == "connector_activate", r.text
    assert (await nadia.get(f"/api/admin/connectors/{slug}")).json()["status"] != "active"
    ok = await omar.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    assert (await nadia.get(f"/api/admin/connectors/{slug}")).json()["status"] == "active"

    kb = await nadia.post("/api/admin/kb", json={"name": "SOPs", "description": "ops"})
    assert kb.status_code == 201, kb.text
    kid = kb.json()["id"]
    r = await nadia.delete(f"/api/admin/kb/{kid}")
    assert r.status_code == 202 and r.json()["kind"] == "kb_delete", r.text
    assert (await nadia.get(f"/api/admin/kb/{kid}/documents")).status_code == 200  # still there
    ok = await omar.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    assert (await nadia.get(f"/api/admin/kb/{kid}/documents")).status_code == 404


# ───────────────────────────── users (Keycloak admin API) ─────────────────────────────


@pytest.fixture
def kc(http_mock: respx.Router, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = FakeKeycloakUsers(KC, "kc-pw")
    fake.install(http_mock)
    ids = {
        "nadia": fake.add_user("nadia", tenant=[TENANT_B], roles=["tenant-admin"]),
        "nora": fake.add_user("nora", tenant=[TENANT_B], roles=["planner"]),
        "multi": fake.add_user("multi", tenant=[TENANT_B, TENANT_A], roles=["planner"]),
        "plat": fake.add_user("plat", tenant=[TENANT_B], roles=["platform-admin"]),
        "outsider": fake.add_user("outsider", tenant=[TENANT_A], roles=["planner"]),
    }
    fake.ids = ids  # type: ignore[attr-defined]
    users.set_keycloak_admin(KeycloakAdmin(KC, "kc-pw"))
    yield fake
    users.set_keycloak_admin(None)


async def test_users_list_create_patch_disable(admins: dict[str, Browser], kc: FakeKeycloakUsers) -> None:
    nadia, omar, ava = admins["nadia"], admins["omar"], admins["ava"]
    ids: dict[str, str] = kc.ids  # type: ignore[attr-defined]
    listed = (await nadia.get("/api/admin/users")).json()["items"]
    by = {u["username"]: u for u in listed}
    assert set(by) == {"nadia", "nora", "multi", "plat"}
    assert by["nora"]["roles"] == ["planner"] and "offline_access" not in str(listed)
    assert by["multi"]["tenants"] == [TENANT_B, TENANT_A]

    # create: tenant attribute, temporary password + UPDATE_PASSWORD, never returned
    r = await nadia.post("/api/admin/users", json={"username": "newbie", "email": "newbie@example.test",
                                                   "name": "New Bie", "roles": ["planner"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["username"] == "newbie" and body["user"]["roles"] == ["planner"]
    assert body["user"]["tenants"] == [TENANT_B] and body["setup"]["link"] is None
    assert "password" in body["setup"]["instructions"]
    created = kc.created[-1]
    assert created["attributes"] == {"tenant": [TENANT_B]} and created["requiredActions"] == ["UPDATE_PASSWORD"]
    secret = kc.credentials[created["id"]][0]
    assert secret["temporary"] is True and len(secret["value"]) >= 24
    assert secret["value"] not in r.text
    assert kc.roles[created["id"]] >= {"planner"}
    log = await _audit(nadia, "user.create")
    assert log and secret["value"] not in str(log) and log[0]["category"] == "admin"
    # with a mail server Keycloak e-mails the one-time set-password link
    kc.smtp = True
    r = await nadia.post("/api/admin/users", json={"username": "mailed", "email": "mailed@example.test",
                                                   "name": "Mai Led", "roles": ["approver"]})
    assert r.status_code == 201 and "e-mailed" in r.json()["setup"]["instructions"]
    assert kc.emails[-1][1] == ["UPDATE_PASSWORD"] and kc.emails[-1][2]["client_id"] == "keelson-bff"
    dup = await nadia.post("/api/admin/users", json={"username": "newbie", "email": "x@example.test", "name": "Xa Yb",
                                                     "roles": ["planner"]})
    assert dup.status_code == 409 and dup.json()["code"] == "user_exists"
    # product roles only; a tenant-admin never grants platform-admin
    for roles in (["platform-admin"], ["offline_access"], ["admin"]):
        bad = await nadia.post("/api/admin/users", json={"username": "evil", "email": "e@example.test",
                                                         "name": "E", "roles": roles})
        assert bad.status_code in (403, 422), (roles, bad.text)
    assert all(u["username"] != "evil" for u in kc.users.values())

    # patch roles: sessions of the user end; non-product roles stay untouched
    r = await nadia.patch(f"/api/admin/users/{ids['nora']}", json={"roles": ["planner", "approver"]})
    assert r.status_code == 200 and r.json()["roles"] == ["planner", "approver"], r.text
    assert kc.roles[ids["nora"]] >= {"planner", "approver", "offline_access"} and ids["nora"] in kc.logouts
    # cross-tenant user / platform-admin account / unknown / other tenant's user
    r = await nadia.patch(f"/api/admin/users/{ids['multi']}", json={"roles": ["approver"]})
    assert r.status_code == 403, r.text
    assert (await nadia.patch(f"/api/admin/users/{ids['plat']}", json={"enabled": False})).status_code == 403
    assert (await nadia.patch(f"/api/admin/users/{ids['outsider']}", json={"roles": []})).status_code == 404
    assert (await nadia.patch("/api/admin/users/not-an-id", json={"roles": []})).status_code == 404
    assert (await nadia.patch(f"/api/admin/users/{ids['nora']}", json={"roles": ["root"]})).status_code == 422

    # granting tenant-admin is four-eyes (a second admin account must not approve its creator's changes)
    r = await nadia.post("/api/admin/users", json={"username": "boss", "email": "boss@example.test", "name": "Bo Ss",
                                                   "roles": ["tenant-admin"]})
    assert r.status_code == 202 and r.json()["kind"] == "user_roles", r.text
    assert r.json()["reasons"][0]["code"] == "admin_role_granted"
    assert all(u["username"] != "boss" for u in kc.users.values())
    assert (await nadia.post(f"{CR}/{r.json()['id']}/approve", json={})).json()["code"] == "self_approval"
    ok = await omar.post(f"{CR}/{r.json()['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    boss = next(uid for uid, u in kc.users.items() if u["username"] == "boss")
    assert "tenant-admin" in kc.roles[boss]
    r = await nadia.patch(f"/api/admin/users/{ids['nora']}", json={"roles": ["planner", "tenant-admin"]})
    assert r.status_code == 202 and r.json()["kind"] == "user_roles"
    assert "tenant-admin" not in kc.roles[ids["nora"]]
    # a platform-admin applies directly (e.g. a new tenant's first admin)
    r = await ava.post(f"/api/admin/users?tenant={TENANT_B}", json={"username": "first", "email": "f@example.test",
                                                                   "name": "Fi Rst", "roles": ["tenant-admin"]})
    assert r.status_code == 201, r.text

    # delete = disable (never deleted)
    r = await nadia.delete(f"/api/admin/users/{ids['nora']}")
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert ids["nora"] in kc.users and kc.users[ids["nora"]]["enabled"] is False
    assert await _audit(nadia, "user.disable")
    # the identity provider is down → 502, never a stack trace
    kc.down = True
    down = await nadia.get("/api/admin/users")
    assert down.status_code == 502 and down.json()["code"] == "identity_provider_unavailable"


# ───────────────────────────── tenant suspension ─────────────────────────────


async def test_suspended_tenant_blocks_logins_and_api(admins: dict[str, Browser], api: Any,
                                                      session_browser: Any) -> None:
    ava, sam = admins["ava"], admins["sam"]
    nadia = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
    assert (await nadia.get("/api/admin/change-requests")).status_code == 200
    assert (await sam.post(f"/api/admin/tenants/{TENANT_B}/suspend", json={})).status_code == 403  # platform only
    r = await ava.post(f"/api/admin/tenants/{TENANT_B}/suspend", json={"reason": "unpaid"})
    assert r.status_code == 200 and r.json()["status"] == "suspended", r.text
    try:
        listed = {t["slug"]: t for t in (await ava.get("/api/admin/tenants")).json()["items"]}
        assert listed[TENANT_B]["status"] == "suspended" and listed[TENANT_B]["suspended_by"] == "ava"
        # sessions of the tenant ended; a fresh session gets 403 on every tenant-scoped call
        assert (await nadia.get("/api/admin/change-requests")).status_code == 401
        fresh = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
        blocked = await fresh.get("/api/admin/change-requests")
        assert blocked.status_code == 403 and blocked.json()["code"] == "tenant_suspended"
        assert (await fresh.get("/api/cases")).json()["code"] == "tenant_suspended"
        assert (await fresh.get("/api/me")).status_code == 200  # the web can still say why
        # logins: the suspended tenant never becomes a session's tenant
        nora = await api.post("/api/demo/login-as", json={"username": "nora"}, headers={"Origin": PUBLIC})
        if nora.status_code == 200:
            me = await api.get("/api/me", headers={"Cookie": f"keelson_session={nora.cookies.get('keelson_session')}"})
            assert TENANT_B not in (me.json().get("tenants") or []) and me.json().get("active_tenant") != TENANT_B
        else:
            assert nora.status_code in (401, 403), nora.text
        # the platform admin keeps access (to reactivate it)
        assert (await ava.get(f"/api/admin/change-requests?tenant={TENANT_B}")).status_code == 200
        assert any(a["action"] == "tenant.suspend" for a in
                   (await ava.get(f"/api/admin/audit?tenant={TENANT_B}&action=tenant.")).json()["items"])
    finally:
        r = await ava.post(f"/api/admin/tenants/{TENANT_B}/resume", json={})
        assert r.status_code == 200 and r.json()["status"] == "active", r.text
    again = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
    assert (await again.get("/api/admin/change-requests")).status_code == 200


# ───────────────────────────── pure helpers ─────────────────────────────


async def test_strip_secrets_and_json_diff() -> None:
    payload = {"auth": {"kind": "static_header", "header_name": "X-Api-Key", "secret": "s3cr3t",
                        "client_secret": "x", "token_url": "http://idp/token"},
               "list": [{"password": "p", "ok": 1}], "My-Api-Key": "k", "db_password": "p"}
    clean = change_requests.strip_secrets(payload)
    assert clean == {"auth": {"kind": "static_header", "header_name": "X-Api-Key", "token_url": "http://idp/token"},
                     "list": [{"ok": 1}]}
    diff = change_requests.json_diff({"a": 1, "b": {"c/d": 2, "secret": "old"}, "gone": True},
                                     {"a": 2, "b": {"c/d": 3, "secret": "new"}, "new": [1]})
    assert {(d["op"], d["path"]) for d in diff} == {("replace", "/a"), ("replace", "/b/c~1d"), ("remove", "/gone"),
                                                    ("add", "/new")}
    assert all("secret" not in d["path"] for d in diff) and "'old'" not in str(diff)
    big = change_requests.json_diff({f"k{i}": i for i in range(500)}, {f"k{i}": i + 1 for i in range(500)})
    assert len(big) == change_requests.MAX_DIFF
