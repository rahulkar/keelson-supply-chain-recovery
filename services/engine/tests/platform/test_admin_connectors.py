"""Back-office connector registry against a real local FastMCP 4 server: URL policy, create,
test-connection step log (discover/initialize, auth kinds static_header + oauth_client_credentials
with client_secret and private_key_jwt, paginated tools/list, tier detection), classification
suggestions, needs_review on new/changed tools, guarded reclassification, lifecycle, the runtime
helpers (``auth_headers``, ``list_approved_tools``) and delete-while-bound."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from e1_admin import drop_connector, reset_specs
from e1_browser import Browser
from e1_env import TENANT_A
from e1_idp import FakeRealm
from e1_mcp_server import API_KEY, McpTestServer, add_tier_tools, change_description
from keelson_engine.config import Settings
from keelson_engine.core import connector_admin, connectors, crypto
from keelson_engine.core.connector_admin import AdminError, check_url, set_resolver
from keelson_engine.core.db import tenant_session
from keelson_engine.core.tokens import ConnectRequired

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXT_ISS = "http://idp.test/realms/ext"
TOKEN_URL = f"{EXT_ISS}/protocol/openid-connect/token"
STEPS = ["url_policy", "reach", "discover", "auth", "tools_list", "tier_detect", "classify"]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def ext_realm(http_mock: respx.Router) -> FakeRealm:
    realm = FakeRealm(EXT_ISS, jwks_provider=crypto.jwks)
    realm.add_client("ext-secret", secret="ext-pw", default_scope="erp:read")
    realm.add_client("ext-jwt", private_key_jwt=True, default_scope="erp:read")
    realm.install(http_mock)
    return realm


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def mcp_server(ext_realm: FakeRealm) -> AsyncIterator[McpTestServer]:
    server = McpTestServer()
    server.gate.realms.append(ext_realm)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def dev_hosts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_dev_hosts", "127.0.0.1,merxa.localhost,idp.test")


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any) -> dict[str, Browser]:
    return {n: await browser_factory(n) for n in ("sam", "ava", "priya")}


def _static(slug: str, url: str, secret: str = API_KEY) -> dict[str, Any]:
    return {"slug": slug, "name": "Foreign ERP", "mcp_url": url,
            "auth": {"kind": "static_header", "header_name": "X-Api-Key", "secret": secret}}


async def _fresh(*slugs: str) -> None:
    for slug in slugs:
        await drop_connector(TENANT_A, slug)
        connectors.clear_token_cache(TENANT_A, slug)


# ───────────────────────────── URL policy ─────────────────────────────


async def test_url_policy(platform: None, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_dev_hosts", "merxa.localhost")
    answers: dict[str, list[str]] = {"erp.example.com": ["93.184.216.34"],
                                     "mixed.example.com": ["93.184.216.34", "10.1.2.3"],
                                     "internal.example.com": ["172.18.0.5"]}

    async def resolver(host: str, port: int) -> list[str]:
        if host not in answers:
            raise OSError("nxdomain")
        return answers[host]

    set_resolver(resolver)
    try:
        assert "development host" in await check_url("http://merxa.localhost/mcp-lite")
        assert "public" in await check_url("https://erp.example.com/mcp")
        for bad in ("ftp://erp.example.com/mcp", "http://user:pw@erp.example.com/mcp", "http://10.0.0.5/mcp",
                    "http://[::1]/mcp", "http://169.254.169.254/latest", "http://127.0.0.1:8100/mcp",
                    "https://mixed.example.com/mcp", "https://internal.example.com/mcp",
                    "https://nowhere.example.com/mcp", "http:///mcp", "http://erp.example.com:99999/x", "",
                    "http://erp.example.com/a b"):
            with pytest.raises(AdminError) as err:
                await check_url(bad)
            assert err.value.status == 422 and err.value.code == "url_policy", bad
    finally:
        set_resolver(None)


# ───────────────────────────── create ─────────────────────────────


async def test_create_validation_and_secrets(people: dict[str, Browser], dev_hosts: None,
                                             mcp_server: McpTestServer) -> None:
    sam, ava, priya = people["sam"], people["ava"], people["priya"]
    await _fresh("erp-lite")
    url = mcp_server.url
    assert (await priya.post("/api/admin/connectors", json=_static("erp-lite", url))).status_code == 403
    r = await sam.post("/api/admin/connectors", json=_static("erp-lite", url))
    assert r.status_code == 201, r.text
    assert r.json() == {"slug": "erp-lite", "status": "draft", "trust_level": "third_party"}
    dup = await sam.post("/api/admin/connectors", json=_static("erp-lite", url))
    assert dup.status_code == 409 and dup.json()["code"] == "connector_exists"

    async def bad(body: dict[str, Any], status: int, code: str, who: Browser = sam) -> None:
        r = await who.post("/api/admin/connectors", json=body)
        assert r.status_code == status, r.text
        assert r.json()["code"] == code, r.text

    await bad({**_static("x1", url), "auth": {"kind": "static_header", "header_name": "Cookie", "secret": "s"}},
              422, "invalid_auth")
    await bad({**_static("x1", url), "auth": {"kind": "static_header", "header_name": "X-Key"}}, 422, "invalid_auth")
    await bad({**_static("x1", url), "auth": {"kind": "oauth_platform_merxa"}}, 422, "invalid_auth")
    await bad({**_static("x1", url), "auth": {"kind": "oauth_client_credentials", "client_id": "c"}}, 422,
              "invalid_auth")
    await bad({**_static("x1", url), "auth": {"kind": "oauth_client_credentials", "client_id": "c",
                                              "token_url": TOKEN_URL, "client_auth": "client_secret"}},
              422, "invalid_auth")
    # M7: first-party trust requested by a tenant-admin → created third-party + a pending change request
    fp = await sam.post("/api/admin/connectors", json={**_static("x-fp", url), "trust_level": "first_party"})
    assert fp.status_code == 201 and fp.json()["trust_level"] == "third_party", fp.text
    assert fp.json()["change_request"]["kind"] == "connector_trust"
    assert fp.json()["change_request"]["payload"] == {"connector": "x-fp", "trust_level": "first_party"}
    assert "secret" not in str(fp.json()["change_request"]).lower()
    assert (await ava.delete("/api/admin/connectors/x-fp")).status_code == 204
    await bad({**_static("x1", url), "webhook_secret_ref": "MERXA_WEBHOOK_SECRET"}, 403, "guarded_change")
    await bad({**_static("x1", url), "webhook_secret_ref": "LITELLM_MASTER_KEY"}, 422, "invalid_webhook_secret_ref",
              ava)
    await bad(_static("x1", "http://10.0.0.7/mcp"), 422, "url_policy")
    await bad(_static("Bad_Slug", url), 422, "invalid_slug")
    unknown = await sam.post("/api/admin/connectors", json={**_static("x1", url), "extra": 1})
    assert unknown.status_code == 422 and unknown.json()["code"] == "invalid_request"

    # the secret is stored encrypted and never shown
    detail = (await sam.get("/api/admin/connectors/erp-lite")).json()
    assert detail["auth"] == {"kind": "static_header", "header_name": "X-Api-Key", "has_secret": True}
    assert API_KEY not in str(detail)
    async with tenant_session(TENANT_A) as s:
        enc = (await s.execute(text("SELECT secret_enc FROM connector WHERE slug = 'erp-lite'"))).scalar_one()
        audit = (await s.execute(text(
            "SELECT after::text FROM admin_audit WHERE action = 'connector.create' ORDER BY id DESC LIMIT 1"
        ))).scalar_one()
    assert enc.startswith("dk-1:") and API_KEY not in enc and API_KEY not in audit
    listed = {c["slug"]: c for c in (await sam.get("/api/admin/connectors")).json()["items"]}
    assert listed["erp-lite"]["status"] == "draft" and listed["erp-lite"]["auth_kind"] == "static_header"
    assert listed["erp-lite"]["tools"] == {"approved": 0, "needs_review": 0, "disabled": 0}


# ───────────────────────────── static header end to end ─────────────────────────────


async def test_static_header_test_review_activate(people: dict[str, Browser], dev_hosts: None,
                                                  mcp_server: McpTestServer) -> None:
    sam, ava = people["sam"], people["ava"]
    await _fresh("erp-lite")
    assert (await sam.post("/api/admin/connectors", json=_static("erp-lite", mcp_server.url))).status_code == 201
    early = await sam.post("/api/admin/connectors/erp-lite/activate")
    assert early.status_code == 409 and early.json()["code"] == "test_required"

    mcp_server.gate.seen_headers.clear()
    r = await sam.post("/api/admin/connectors/erp-lite/test")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True, res["steps"]
    assert [s["step"] for s in res["steps"]] == STEPS and all(s["ok"] for s in res["steps"])
    steps = {s["step"]: s for s in res["steps"]}
    assert steps["discover"]["detail"] == "server requires authentication (HTTP 401)"
    assert steps["auth"]["detail"] == "header X-Api-Key accepted"
    assert steps["tools_list"]["detail"] == "6 tool(s) in 3 page(s)"  # list_page_size=2 → paginated
    assert res["tier"] == ["A"] and steps["tier_detect"]["detail"] == "A"
    assert API_KEY not in str(res)
    assert any(h.get("x-api-key") == API_KEY for h in mcp_server.gate.seen_headers)

    tools = {t["name"]: t for t in res["tools"]}
    assert set(tools) == {"list_items", "get_item", "update_item", "create_transfer_order",
                          "list_supplier_messages", "search_web"}
    assert all(t["status"] == "needs_review" for t in tools.values())
    assert tools["list_items"]["suggested"] == {"access": "read", "content_trust": "trusted_data",
                                                "egress": "internal", "idempotency": "none"}
    assert tools["update_item"]["suggested"]["access"] == "write"
    assert tools["update_item"]["suggested"]["idempotency"] == "best_effort"  # optional request_id
    assert tools["create_transfer_order"]["suggested"]["access"] == "destructive"
    assert tools["create_transfer_order"]["suggested"]["idempotency"] == "enforced"  # required idempotency_key
    assert tools["list_supplier_messages"]["suggested"]["content_trust"] == "untrusted_text"
    assert tools["search_web"]["suggested"]["egress"] == "external"
    assert len(tools["get_item"]["definition_hash"]) == 64

    detail = (await sam.get("/api/admin/connectors/erp-lite")).json()
    assert detail["status"] == "tested" and detail["last_test"]["ok"] is True and detail["tier"] == ["A"]
    assert detail["tools"] == {"approved": 0, "needs_review": 6, "disabled": 0}
    # draft/tested connectors serve nothing to the runtime
    assert await connectors.list_approved_tools(TENANT_A, "erp-lite") == []
    with pytest.raises(connectors.ConnectorDisabled):
        await connectors.auth_headers(TENANT_A, "erp-lite")

    # review: approve reads; guarded reclassifications
    base = "/api/admin/connectors/erp-lite/tools"
    for name in ("list_items", "get_item"):
        r = await sam.patch(f"{base}/{name}", json={"status": "approved"})
        assert r.status_code == 200, r.text
        assert r.json()["bindable"] is True and r.json()["reviewed_by"] == "sam"
    r = await sam.patch(f"{base}/update_item", json={"access": "read"})
    assert r.status_code == 202 and r.json()["kind"] == "connector_tool", r.text  # M7: change request
    assert r.json()["reasons"][0]["code"] == "tool_reclassified_read"
    r = await sam.patch(f"{base}/list_supplier_messages", json={"content_trust": "trusted_data"})
    assert r.status_code == 202 and r.json()["reasons"][0]["code"] == "tool_reclassified_trusted"
    assert (await sam.get(base)).json()["items"]  # nothing applied:
    still = {t["name"]: t for t in (await sam.get(base)).json()["items"]}
    assert still["update_item"]["access"] == "write"
    assert still["list_supplier_messages"]["content_trust"] == "untrusted_text"
    r = await sam.patch(f"{base}/list_supplier_messages", json={"status": "approved"})
    assert r.status_code == 200 and r.json()["bindable"] is False  # approved, still untrusted
    r = await sam.patch(f"{base}/update_item", json={"status": "approved"})
    assert r.status_code == 200 and r.json()["access"] == "write" and r.json()["bindable"] is False
    r = await ava.patch(f"{base}/create_transfer_order", json={"access": "read"})
    assert r.status_code == 200 and r.json()["access"] == "read"  # platform-admin may
    r = await sam.patch(f"{base}/search_web", json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert (await sam.patch(f"{base}/nope", json={"status": "approved"})).status_code == 404
    assert (await sam.patch(f"{base}/get_item", json={})).status_code == 422
    assert (await sam.patch(f"{base}/get_item", json={"status": "removed"})).status_code == 422

    act = await sam.post("/api/admin/connectors/erp-lite/activate")
    assert act.status_code == 200 and act.json()["status"] == "active"
    approved = [t.name for t in await connectors.list_approved_tools(TENANT_A, "erp-lite")]
    assert set(approved) == {"list_items", "get_item", "list_supplier_messages", "update_item"}
    bindable = [t.name for t in await connectors.list_approved_tools(TENANT_A, "erp-lite")
                if t.classification.investigator_bindable]
    assert sorted(bindable) == ["get_item", "list_items"]
    assert await connectors.auth_headers(TENANT_A, "erp-lite") == {"X-Api-Key": API_KEY}
    listed = (await sam.get(base)).json()["items"]
    assert {t["name"]: t["status"] for t in listed}["search_web"] == "disabled"

    # drift: a changed tool and new capability tools → needs_review; tiers B, C, W
    change_description(mcp_server.mcp, "get_item", "Get one row by key (v2).")
    add_tier_tools(mcp_server.mcp)
    r = (await sam.post("/api/admin/connectors/erp-lite/test")).json()
    assert r["ok"] is True and r["tier"] == ["A", "B", "C", "W"]
    tools = {t["name"]: t for t in (await sam.get(base)).json()["items"]}
    assert tools["get_item"]["status"] == "needs_review" and tools["get_item"]["drift"] is True
    assert tools["get_item"]["access"] == "read"  # the admin's classification is kept for the review
    assert tools["list_items"]["status"] == "approved" and tools["list_items"]["drift"] is False
    assert tools["scn.snapshot_open"]["status"] == "needs_review" and tools["scn.snapshot_open"]["drift"] is False
    assert tools["search_web"]["status"] == "disabled"
    assert "get_item" not in [t.name for t in await connectors.list_approved_tools(TENANT_A, "erp-lite")]
    classify = next(s for s in r["steps"] if s["step"] == "classify")["detail"]
    assert classify.startswith("7 new · 1 changed · 0 removed")

    # removed tools
    mcp_server.mcp.local_provider.remove_tool("search_web")
    r = (await sam.post("/api/admin/connectors/erp-lite/test")).json()
    tools = {t["name"]: t for t in (await sam.get(base)).json()["items"]}
    assert tools["search_web"]["status"] == "removed"
    gone = await sam.patch(f"{base}/search_web", json={"status": "approved"})
    assert gone.status_code == 409 and gone.json()["code"] == "tool_removed"

    # disable → never served; re-activate works with the last passing test
    assert (await sam.post("/api/admin/connectors/erp-lite/disable")).json()["status"] == "disabled"
    assert await connectors.list_approved_tools(TENANT_A, "erp-lite") == []
    with pytest.raises(connectors.ConnectorDisabled):
        await connectors.auth_headers(TENANT_A, "erp-lite")
    assert (await sam.post("/api/admin/connectors/erp-lite/activate")).json()["status"] == "active"
    assert (await sam.get("/api/admin/connectors/erp-lite/events")).json() == {"items": []}

    async with tenant_session(TENANT_A) as s:
        actions = [r[0] for r in (await s.execute(text(
            "SELECT action FROM admin_audit WHERE target LIKE 'connector:erp-lite%' ORDER BY id")))]
    for expected in ("connector.create", "connector.test", "connector.tool.update", "connector.activate",
                     "connector.disable", "connector.tools.drift"):
        assert expected in actions


async def test_wrong_credentials_and_unreachable(people: dict[str, Browser], dev_hosts: None,
                                                 mcp_server: McpTestServer) -> None:
    sam = people["sam"]
    await _fresh("erp-bad", "erp-down", "erp-open")
    assert (await sam.post("/api/admin/connectors",
                           json=_static("erp-bad", mcp_server.url, secret="wrong"))).status_code == 201
    r = (await sam.post("/api/admin/connectors/erp-bad/test")).json()
    assert r["ok"] is False and [s["step"] for s in r["steps"]] == STEPS[:4]
    assert r["steps"][-1]["detail"] == "header X-Api-Key rejected (HTTP 401)"
    detail = (await sam.get("/api/admin/connectors/erp-bad")).json()
    assert detail["status"] == "draft" and detail["last_test"]["ok"] is False
    assert (await sam.post("/api/admin/connectors/erp-bad/activate")).status_code == 409

    assert (await sam.post("/api/admin/connectors",
                           json=_static("erp-down", "http://127.0.0.1:9/mcp"))).status_code == 201
    r = (await sam.post("/api/admin/connectors/erp-down/test")).json()
    assert r["ok"] is False and r["steps"][-1]["step"] == "reach" and "not reachable" in r["steps"][-1]["detail"]

    # auth kind none: fails at discover against a protected server, passes against an open one
    body = {"slug": "erp-open", "mcp_url": mcp_server.url, "auth": {"kind": "none"}}
    assert (await sam.post("/api/admin/connectors", json=body)).status_code == 201
    r = (await sam.post("/api/admin/connectors/erp-open/test")).json()
    assert r["ok"] is False and r["steps"][-1]["step"] == "discover"
    assert "HTTP 401" in r["steps"][-1]["detail"]
    mcp_server.gate.require_auth = False
    try:
        r = (await sam.post("/api/admin/connectors/erp-open/test")).json()
    finally:
        mcp_server.gate.require_auth = True
    assert r["ok"] is True, r["steps"]
    discover = next(s for s in r["steps"] if s["step"] == "discover")["detail"]
    assert discover.split(" · ")[0] in ("server/discover", "initialize") and "foreign-erp-test" in discover
    assert next(s for s in r["steps"] if s["step"] == "auth")["detail"] == "no credentials accepted"


async def test_client_credentials(people: dict[str, Browser], dev_hosts: None, mcp_server: McpTestServer,
                                  ext_realm: FakeRealm) -> None:
    sam = people["sam"]
    await _fresh("erp-cc", "erp-jwt", "erp-ccbad")
    body = {"slug": "erp-cc", "mcp_url": mcp_server.url,
            "auth": {"kind": "oauth_client_credentials", "token_url": TOKEN_URL, "client_id": "ext-secret",
                     "client_secret": "ext-pw", "scopes": ["erp:read", "erp:write"],
                     "resource": mcp_server.url}}
    assert (await sam.post("/api/admin/connectors", json=body)).status_code == 201
    mark = len(ext_realm.grants("client_credentials"))
    r = (await sam.post("/api/admin/connectors/erp-cc/test")).json()
    assert r["ok"] is True, r["steps"]
    assert next(s for s in r["steps"] if s["step"] == "auth")["detail"] == \
        "client-credentials token (client_secret) accepted"
    form = ext_realm.grants("client_credentials")[mark]["form"]
    assert form["client_id"] == "ext-secret" and form["client_secret"] == "ext-pw"
    assert form["scope"] == "erp:read erp:write" and form["resource"] == mcp_server.url
    assert "ext-pw" not in str(r)
    detail = (await sam.get("/api/admin/connectors/erp-cc")).json()
    assert detail["auth"]["client_auth"] == "client_secret" and "client_secret" not in detail["auth"]
    # the runtime helper: bearer from the (cached) client-credentials token
    assert (await sam.post("/api/admin/connectors/erp-cc/activate")).status_code == 200
    mark = len(ext_realm.grants("client_credentials"))
    h1 = await connectors.auth_headers(TENANT_A, "erp-cc", "reader")
    h2 = await connectors.auth_headers(TENANT_A, "erp-cc", "preparer")
    assert h1 == h2 and h1["Authorization"].startswith("Bearer ")
    assert len(ext_realm.grants("client_credentials")) == mark + 1  # cached, one token request

    jwt_body = {"slug": "erp-jwt", "mcp_url": mcp_server.url,
                "auth": {"kind": "oauth_client_credentials", "token_url": TOKEN_URL, "client_id": "ext-jwt",
                         "client_auth": "private_key_jwt", "audience": EXT_ISS}}
    # private_key_jwt signs with Keelson's platform key (ca-1): platform admins only
    denied = await sam.post("/api/admin/connectors", json=jwt_body)
    assert denied.status_code == 403 and denied.json()["code"] == "guarded_change"
    # never an assertion audience on another origin than token_url (confused deputy)
    foreign = {**jwt_body, "auth": {**jwt_body["auth"], "audience": "http://auth.localhost/realms/merxa"}}
    r = await people["ava"].post("/api/admin/connectors", json=foreign)
    assert r.status_code == 422 and r.json()["code"] == "invalid_auth"
    assert (await people["ava"].post("/api/admin/connectors", json=jwt_body)).status_code == 201
    r = (await sam.post("/api/admin/connectors/erp-jwt/test")).json()
    assert r["ok"] is True, r["steps"]
    form = ext_realm.grants("client_credentials")[-1]["form"]
    assert form["client_id"] == "ext-jwt" and "client_secret" not in form
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

    bad = {**body, "slug": "erp-ccbad", "auth": {**body["auth"], "client_secret": "nope"}}
    assert (await sam.post("/api/admin/connectors", json=bad)).status_code == 201
    r = (await sam.post("/api/admin/connectors/erp-ccbad/test")).json()
    assert r["ok"] is False and r["steps"][-1]["step"] == "auth"
    assert r["steps"][-1]["detail"].startswith("credential refused") and "nope" not in str(r)


async def test_platform_connector_helpers(people: dict[str, Browser]) -> None:
    from test_tokens import MERXA_SPEC

    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    conn = await connectors.get_connector(TENANT_A, "merxa")
    assert conn.auth_kind == "oauth_platform_merxa" and conn.status == "active" and conn.is_platform
    headers = await connectors.auth_headers(TENANT_A, "merxa", "reader")
    assert headers["Authorization"].startswith("Bearer ")
    with pytest.raises(ConnectRequired):
        await connectors.auth_headers(TENANT_A, "merxa", "user:sub-nobody")
    with pytest.raises(ValueError):
        await connectors.auth_headers(TENANT_A, "merxa", "admin")
    with pytest.raises(connectors.ConnectorNotFound):
        await connectors.auth_headers(TENANT_A, "no-such-connector")
    # tenants.yaml re-runs never reset an admin's disable
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'disabled' WHERE slug = 'merxa'"))
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    assert (await connectors.get_connector(TENANT_A, "merxa")).status == "disabled"
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'active' WHERE slug = 'merxa'"))
    # platform connectors: only a platform admin may delete
    r = await people["sam"].delete("/api/admin/connectors/merxa")
    assert r.status_code in (403, 409)


async def test_delete_refused_while_bound(people: dict[str, Browser], dev_hosts: None,
                                          mcp_server: McpTestServer) -> None:
    sam = people["sam"]
    await reset_specs(TENANT_A)
    await _fresh("erp-del")
    assert (await sam.post("/api/admin/connectors", json=_static("erp-del", mcp_server.url))).status_code == 201
    assert (await sam.post("/api/admin/connectors/erp-del/test")).json()["ok"] is True
    assert (await sam.patch("/api/admin/connectors/erp-del/tools/list_items",
                            json={"status": "approved"})).status_code == 200
    assert (await sam.post("/api/admin/connectors/erp-del/activate")).status_code == 200
    put = await sam.put("/api/admin/agents/spec", json={
        "base_version": "default",
        "spec_patch": {"answer_connector": "erp-del", "answer_tools": ["list_items"]}})
    assert put.status_code == 201, put.text
    r = await sam.delete("/api/admin/connectors/erp-del")
    assert r.status_code == 409 and r.json()["code"] == "connector_bound"
    unbind = await sam.put("/api/admin/agents/spec", json={
        "base_version": put.json()["version"], "spec_patch": {"answer_connector": "merxa", "answer_tools": []}})
    assert unbind.status_code == 201, unbind.text
    cr = await sam.delete("/api/admin/connectors/erp-del")  # M7: four-eyes
    assert cr.status_code == 202 and cr.json()["kind"] == "connector_delete", cr.text
    approved = await people["ava"].post(f"/api/admin/change-requests/{cr.json()['id']}/approve", json={})
    assert approved.status_code == 200 and approved.json()["status"] == "applied", approved.text
    assert (await sam.get("/api/admin/connectors/erp-del")).status_code == 404
    assert (await sam.delete("/api/admin/connectors/erp-del")).status_code == 404
    await reset_specs(TENANT_A)


async def test_suggestion_rules_unit() -> None:
    s = connectors.suggest_classification
    assert s({"name": "get_order_notes", "annotations": {"readOnlyHint": True}}).content_trust.value == \
        "untrusted_text"
    assert s({"name": "list_items", "description": "Lists news-free rows"}).content_trust.value == "untrusted_text"
    assert s({"name": "list_emails"}).content_trust.value == "untrusted_text"
    assert s({"name": "get_entity", "description": "Get one entity"}).content_trust.value == "trusted_data"
    assert s({"name": "denote_x", "description": "annotated values"}).content_trust.value == "trusted_data"
    req = {"name": "w", "inputSchema": {"properties": {"request_id": {}}, "required": ["request_id"]}}
    assert s(req).idempotency.value == "enforced"
    assert s({"name": "w", "inputSchema": {"properties": {"idempotency_key": {}}}}).idempotency.value == "best_effort"
    assert s({"name": "r", "annotations": {"readOnlyHint": True, "destructiveHint": True}}).access.value == "read"
    assert s({"name": "d", "annotations": {"destructiveHint": True}}).access.value == "destructive"
    assert connector_admin._http_status(RuntimeError("Client error '403 Forbidden' for url")) == 403


async def test_private_key_jwt_audience_pinned_to_token_origin(platform: None) -> None:
    """Review fix: even a stored row (or a pack) with a foreign assertion audience never gets
    Keelson to sign an assertion valid at another IdP."""
    from keelson_engine.core.tokens import TokenError

    conn = connectors.ConnectorRow(
        tenant=TENANT_A, slug="erp-x", name="x", url="https://erp.example.com/mcp", issuer="", clients={},
        lanes=[], config={}, tier=None, tiers=[], trust="third_party", status="active",
        auth_kind="oauth_client_credentials",
        auth_config={"token_url": "https://evil.example.com/token", "client_id": "keelson-reader",
                     "client_auth": "private_key_jwt", "audience": "http://auth.localhost/realms/merxa"})
    with pytest.raises(TokenError, match="audience"):
        await connectors.client_credentials_token(conn, None)
    assert connectors.same_origin("https://a.example/x", "https://a.example:443/token")
    assert not connectors.same_origin("http://a.example/x", "https://a.example/x")
    assert not connectors.same_origin("https://a.example/x", "https://b.example/x")
    assert not connectors.same_origin("nonsense", "nonsense")


async def test_first_party_drift_never_loosens_a_reviewed_classification(platform: None) -> None:
    """Review fix: a vendor update of a first-party tool (auto-approved) used to reset the admin's
    stricter classification to the suggestion; now per field the stricter one wins."""
    import copy

    from test_api_connectors import TOOLS
    from test_tokens import MERXA_SPEC

    sam = {"sub": "sub-sam", "username": "sam"}

    async def reset() -> None:
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("DELETE FROM connector_tool WHERE connector = 'merxa'"))
        await connectors.upsert_tool_snapshot(TENANT_A, "merxa", copy.deepcopy(TOOLS), auto_approve=True,
                                              untrusted=("list_supplier_messages",))

    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await reset()
    try:
        r = await connector_admin.update_tool(TENANT_A, "merxa", "get_inventory",
                                              {"content_trust": "untrusted_text", "status": "approved"}, sam,
                                              platform_admin=False)
        assert r["content_trust"] == "untrusted_text" and r["bindable"] is False
        changed = copy.deepcopy(TOOLS)
        for t in changed:
            if t["name"] in ("get_inventory", "create_change_set"):
                t["description"] += " (v2)"
        summary = await connectors.upsert_tool_snapshot(TENANT_A, "merxa", changed, auto_approve=True,
                                                        untrusted=("list_supplier_messages",))
        assert sorted(summary["changed"]) == ["create_change_set", "get_inventory"]
        inv = await connectors.get_tool(TENANT_A, "merxa", "get_inventory")
        assert inv is not None and inv.status == "approved"
        assert inv.content_trust == "untrusted_text" and not inv.classification.investigator_bindable
        assert inv.suggested is not None and inv.suggested.content_trust.value == "trusted_data"
        assert inv.reviewed_by == "sam"  # still a human decision → kept on the next drift too
        # tools nobody reviewed keep following the suggestion
        cs = await connectors.get_tool(TENANT_A, "merxa", "create_change_set")
        assert cs is not None and cs.reviewed_by == "auto:first_party" and cs.access == "write"
    finally:
        await reset()


# ───────────────────────────── reviewer hardening (EA review) ─────────────────────────────


async def test_static_header_value_must_be_visible_ascii() -> None:
    """Review fix: only LF was refused — CR / NUL / other control characters (header injection, or a
    connector whose every request fails inside the HTTP client) and non-ASCII values (not sendable as
    a header) were stored."""
    url = "http://merxa.localhost/mcp-lite"

    def auth(secret: str) -> dict[str, Any]:
        return {"kind": "static_header", "header_name": "X-Api-Key", "secret": secret}

    for bad in ("abc\r\nX-Evil: 1", "abc\rdef", "ab\x00c", "ab\tc", "naïve-key", "key\x7f"):
        with pytest.raises(AdminError) as err:
            connector_admin._clean_auth(auth(bad), mcp_url=url)
        assert err.value.status == 422 and err.value.code == "invalid_auth", repr(bad)
    kind, cfg, secret = connector_admin._clean_auth(auth("  mx_ab12_s3cr3t value  "), mcp_url=url)
    assert (kind, cfg, secret) == ("static_header", {"header_name": "X-Api-Key"}, "mx_ab12_s3cr3t value")


async def test_client_credentials_token_tolerates_odd_idp_responses(platform: None, http_mock: respx.Router) -> None:
    """Review fix: a non-numeric ``expires_in`` raised ValueError and a JSON array error body raised
    AttributeError out of the runtime helper (callers only expect TokenError)."""
    import httpx

    from keelson_engine.core.tokens import TokenError

    url = "http://idp-odd.test/realms/x/protocol/openid-connect/token"
    conn = connectors.ConnectorRow(
        tenant=TENANT_A, slug="odd-idp", name="odd", url="http://erp-odd.test/mcp", issuer="", clients={},
        lanes=[], config={}, tier=None, tiers=[], trust="third_party", status="active",
        auth_kind="oauth_client_credentials",
        auth_config={"token_url": url, "client_id": "c1", "client_auth": "client_secret"},
    )
    route = http_mock.post(url)
    try:
        route.mock(return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": "soon"}))
        assert await connectors.client_credentials_token(conn, "pw") == "tok-1"
        assert await connectors.client_credentials_token(conn, "pw") == "tok-1"  # cached (60 s fallback)
        assert route.call_count == 1
        for status, body in ((400, ["not", "an", "object"]), (200, [1, 2]), (200, {"access_token": ""}),
                             (200, {"token_type": "Bearer"})):
            connectors.clear_token_cache(TENANT_A, "odd-idp")
            route.mock(return_value=httpx.Response(status, json=body))
            with pytest.raises(TokenError):
                await connectors.client_credentials_token(conn, "pw")
    finally:
        connectors.clear_token_cache(TENANT_A, "odd-idp")
    assert connectors._expires_in("3600") == 3600
    assert connectors._expires_in(10**12) == 86_400
    assert connectors._expires_in(-5) == 0
    assert connectors._expires_in(None) == connectors._expires_in(True) == connectors._expires_in(float("inf")) == 60
