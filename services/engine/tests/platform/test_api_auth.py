"""BFF auth: session required, CSRF + Origin, demo login-as gating, code+PKCE login, logout."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import text

from e1_browser import login_as
from e1_env import PUBLIC, TENANT_A
from e1_idp import FakeRealm
from keelson_engine.config import Settings
from keelson_engine.core.db import system_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_health_is_public(api: httpx.AsyncClient) -> None:
    resp = await api.get("/api/healthz")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert resp.headers["x-request-id"]


async def test_me_requires_session(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/me")).status_code == 401
    resp = await api.get("/api/me", headers={"Cookie": "keelson_session=forged"})
    assert resp.status_code == 401 and resp.json()["code"] == "unauthenticated"


async def test_demo_login_as_creates_session_without_tokens(api: httpx.AsyncClient) -> None:
    resp = await api.post("/api/demo/login-as", json={"username": "marco"}, headers={"Origin": PUBLIC})
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "access_token" not in body and "refresh_token" not in body and "id_token" not in body
    set_cookie = resp.headers["set-cookie"].lower()
    assert "keelson_session=" in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    data = resp.json()
    assert data["user"]["username"] == "marco"
    assert data["user"]["roles"] == ["approver", "planner"]
    assert data["user"]["tenants"] == [TENANT_A] and data["user"]["active_tenant"] == TENANT_A
    assert data["demo_mode"] is True and data["brand"] == {"engine": "Keelson", "vendor": "Merxa"}
    async with system_session() as s:
        row = (await s.execute(text(
            "SELECT action, tenant, after FROM admin_audit WHERE action = 'demo.login_as' ORDER BY id DESC LIMIT 1"
        ))).one()
    assert row[0] == "demo.login_as" and row[1] == TENANT_A and row[2]["username"] == "marco"
    # the DB never stores the cookie value or plaintext tokens
    cookie = resp.cookies["keelson_session"]
    async with system_session() as s:
        stored = (await s.execute(text("SELECT id, tokens_enc FROM bff_session WHERE sub = 'sub-marco' "
                                       "ORDER BY created_at DESC LIMIT 1"))).one()
    assert stored[0] != cookie and stored[1].startswith("dk-1:")


async def test_login_as_requires_same_origin(api: httpx.AsyncClient) -> None:
    resp = await api.post("/api/demo/login-as", json={"username": "marco"})
    assert resp.status_code == 403 and resp.json()["code"] == "bad_origin"
    resp = await api.post("/api/demo/login-as", json={"username": "marco"}, headers={"Origin": "http://evil.test"})
    assert resp.status_code == 403


async def test_login_as_disabled_without_demo_mode(api: httpx.AsyncClient, settings: Settings,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_mode", False)
    assert (await api.get("/api/demo/personas")).status_code == 404
    resp = await api.post("/api/demo/login-as", json={"username": "marco"}, headers={"Origin": PUBLIC})
    assert resp.status_code == 404


async def test_hosted_demo_needs_access_secret_and_hides_platform_admin(
    api: httpx.AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = (await api.get("/api/demo/personas")).json()
    assert "ava" in {p["username"] for p in local}
    assert {"username", "name", "role_label", "initials"} <= set(local[0])
    monkeypatch.setattr(settings, "public_base_domain", "demo.example.com")
    hosted = (await api.get("/api/demo/personas")).json()
    assert "ava" not in {p["username"] for p in hosted}
    resp = await api.post("/api/demo/login-as", json={"username": "marco"}, headers={"Origin": PUBLIC})
    assert resp.status_code == 403 and resp.json()["code"] == "demo_access_required"
    resp = await api.post("/api/demo/login-as", json={"username": "marco"},
                          headers={"Origin": PUBLIC, "X-Demo-Access": "wrong"})
    assert resp.status_code == 403
    resp = await api.post("/api/demo/login-as", json={"username": "marco"},
                          headers={"Origin": PUBLIC, "X-Demo-Access": "demo-access"})
    assert resp.status_code == 200
    api.cookies.clear()
    resp = await api.post("/api/demo/login-as", json={"username": "ava"},
                          headers={"Origin": PUBLIC, "X-Demo-Access": "demo-access"})
    assert resp.status_code == 404


async def test_login_as_unknown_persona(api: httpx.AsyncClient) -> None:
    resp = await api.post("/api/demo/login-as", json={"username": "mallory"}, headers={"Origin": PUBLIC})
    assert resp.status_code == 404


async def test_switching_persona_needs_csrf_and_replaces_session(api: httpx.AsyncClient) -> None:
    priya = await login_as(api, "priya")
    resp = await api.post("/api/demo/login-as", json={"username": "marco"},
                          headers={"Origin": PUBLIC, "Cookie": f"keelson_session={priya.cookie}"})
    assert resp.status_code == 403 and resp.json()["code"] == "csrf_failed"
    resp = await priya.post("/api/demo/login-as", json={"username": "marco"})
    assert resp.status_code == 200
    assert (await priya.get("/api/me")).status_code == 401  # old session revoked


async def test_csrf_and_origin_enforced_on_mutations(api: httpx.AsyncClient) -> None:
    marco = await login_as(api, "marco")
    cookie = {"Cookie": f"keelson_session={marco.cookie}"}
    no_token = await api.post("/api/cases", json={}, headers={**cookie, "Origin": PUBLIC})
    assert no_token.status_code == 403 and no_token.json()["code"] == "csrf_failed"
    wrong = await api.post("/api/cases", json={}, headers={**cookie, "Origin": PUBLIC, "X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    no_origin = await api.post("/api/cases", json={}, headers={**cookie, "X-CSRF-Token": marco.csrf})
    assert no_origin.status_code == 403 and no_origin.json()["code"] == "bad_origin"
    ok = await marco.post("/api/cases", json={"title": "csrf ok"})
    assert ok.status_code == 201
    # GETs need neither
    assert (await marco.get("/api/cases")).status_code == 200


async def test_logout(api: httpx.AsyncClient) -> None:
    marco = await login_as(api, "marco")
    resp = await api.post("/api/auth/logout", headers={"Cookie": f"keelson_session={marco.cookie}", "Origin": PUBLIC})
    assert resp.status_code == 403  # CSRF required while a session exists
    resp = await marco.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["logout_url"].startswith("http://auth.localhost/realms/keelson/protocol/openid-connect/logout")
    assert "id_token_hint" not in resp.json()["logout_url"]
    assert (await marco.get("/api/me")).status_code == 401


async def test_code_flow_login_with_pkce(api: httpx.AsyncClient, keelson_realm: FakeRealm) -> None:
    api.cookies.clear()
    resp = await api.get("/api/auth/login", params={"next": "/cases/abc?tab=1"})
    assert resp.status_code == 302
    loc = urlsplit(resp.headers["location"])
    params = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == keelson_realm.auth_endpoint
    assert params["client_id"] == "keelson-bff" and params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == "http://keelson.localhost/api/auth/callback"
    assert params["state"] and params["nonce"] and len(params["code_challenge"]) == 43
    binding = resp.cookies["keelson_login"]
    code = keelson_realm.authorize(params, "priya")

    # without the browser-binding cookie the callback is refused (login CSRF)
    api.cookies.clear()
    rejected = await api.get("/api/auth/callback", params={"code": code, "state": params["state"]})
    assert rejected.status_code == 302 and "auth_error=invalid_state" in rejected.headers["location"]

    # fresh flow, completed properly
    resp = await api.get("/api/auth/login", params={"next": "/cases/abc?tab=1"})
    params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
    binding = resp.cookies["keelson_login"]
    code = keelson_realm.authorize(params, "priya")
    api.cookies.clear()
    done = await api.get("/api/auth/callback", params={"code": code, "state": params["state"]},
                         headers={"Cookie": f"keelson_login={binding}"})
    assert done.status_code == 302 and done.headers["location"] == "/cases/abc?tab=1"
    cookie = done.cookies["keelson_session"]
    me = await api.get("/api/me", headers={"Cookie": f"keelson_session={cookie}"})
    assert me.status_code == 200 and me.json()["user"]["username"] == "priya"
    # state is single use
    replay = await api.get("/api/auth/callback", params={"code": code, "state": params["state"]},
                           headers={"Cookie": f"keelson_login={binding}"})
    assert "auth_error=invalid_state" in replay.headers["location"]


async def test_login_rejects_open_redirects(api: httpx.AsyncClient, keelson_realm: FakeRealm) -> None:
    for bad in ("//evil.test/x", "https://evil.test", "/\\evil", "javascript:alert(1)"):
        api.cookies.clear()
        resp = await api.get("/api/auth/login", params={"next": bad})
        params = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
        binding = resp.cookies["keelson_login"]
        code = keelson_realm.authorize(params, "priya")
        api.cookies.clear()
        done = await api.get("/api/auth/callback", params={"code": code, "state": params["state"]},
                             headers={"Cookie": f"keelson_login={binding}"})
        assert done.headers["location"] == "/", bad


async def test_user_without_tenant_is_forbidden_on_tenant_routes(api: httpx.AsyncClient,
                                                                 keelson_realm: FakeRealm) -> None:
    keelson_realm.add_user("drifter", roles=["planner"], name="No Tenant", tenants=[])
    from keelson_engine.api import routes_auth

    original = routes_auth.PERSONAS
    routes_auth.PERSONAS = (*original, {"username": "drifter", "name": "x", "role_label": "x", "initials": "x"})
    try:
        drifter = await login_as(api, "drifter")
    finally:
        routes_auth.PERSONAS = original
    assert drifter.me["user"]["active_tenant"] is None
    resp = await drifter.get("/api/cases")
    assert resp.status_code == 403 and resp.json()["code"] == "no_tenant"
