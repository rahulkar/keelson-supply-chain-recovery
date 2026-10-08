"""JWKS, readiness, Caddy forward-auth and the console handoff."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from e1_browser import Browser
from e1_env import LITELLM
from keelson_engine.api import routes_system

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_jwks_endpoint(api: httpx.AsyncClient) -> None:
    resp = await api.get("/api/.well-known/jwks.json")
    assert resp.status_code == 200
    assert [k["kid"] for k in resp.json()["keys"]] == ["ca-1", "ar-1"]
    assert "max-age" in resp.headers["cache-control"]


async def test_readyz_reports_each_dependency(api: httpx.AsyncClient, http_mock: Any) -> None:
    route = http_mock.get(f"{LITELLM}/model/info", name="litellm-model-info").mock(
        return_value=httpx.Response(200, json={"data": [{"model_name": "fast-default"}]})
    )
    routes_system._ready_cache = None
    resp = await api.get("/api/readyz")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "checks": {"db": True, "valkey": True, "litellm_alias": True,
                                                  "keycloak": True}}
    route.mock(return_value=httpx.Response(200, json={"data": []}))
    routes_system._ready_cache = None
    resp = await api.get("/api/readyz")
    assert resp.status_code == 503 and resp.json()["checks"]["litellm_alias"] is False


async def test_forward_auth_with_session(api: httpx.AsyncClient, browser_factory: Any) -> None:
    assert (await api.get("/api/internal/forward-auth", params={"role": "platform-admin"})).status_code == 401
    marco: Browser = await browser_factory("marco")
    resp = await marco.get("/api/internal/forward-auth", params={"role": "platform-admin"})
    assert resp.status_code == 403
    ava: Browser = await browser_factory("ava")
    resp = await ava.get("/api/internal/forward-auth", params={"role": "platform-admin"})
    assert resp.status_code == 200
    assert resp.headers["x-auth-user"] == "ava" and resp.headers["x-auth-roles"] == "platform-admin"
    assert (await ava.get("/api/internal/forward-auth", params={"role": "root"})).status_code == 400


async def test_console_handoff(api: httpx.AsyncClient, browser_factory: Any) -> None:
    marco: Browser = await browser_factory("marco")
    assert (await marco.get("/api/console/handoff", params={"target": "gateway"})).status_code == 403
    ava: Browser = await browser_factory("ava")
    assert (await ava.get("/api/console/handoff", params={"target": "evil"})).status_code == 400
    resp = await ava.get("/api/console/handoff", params={"target": "gateway"})
    assert resp.status_code == 302
    loc = urlsplit(resp.headers["location"])
    assert loc.scheme == "http" and loc.netloc == "gateway.localhost" and loc.path == "/__keelson/handoff"
    ticket = parse_qs(loc.query)["ticket"][0]

    api.cookies.clear()
    wrong_host = await api.get("/__keelson/handoff", params={"ticket": ticket},
                               headers={"Host": "traces.localhost"})
    assert wrong_host.status_code == 401
    landing = await api.get("/__keelson/handoff", params={"ticket": ticket}, headers={"Host": "gateway.localhost"})
    assert landing.status_code == 302 and landing.headers["location"] == "/"
    set_cookie = landing.headers["set-cookie"].lower()
    assert "keelson_console=" in set_cookie and "httponly" in set_cookie and "domain" not in set_cookie
    console = landing.cookies["keelson_console"]
    api.cookies.clear()
    reuse = await api.get("/__keelson/handoff", params={"ticket": ticket}, headers={"Host": "gateway.localhost"})
    assert reuse.status_code == 401

    ok = await api.get("/api/internal/forward-auth", params={"role": "platform-admin"},
                       headers={"Host": "gateway.localhost", "Cookie": f"keelson_console={console}"})
    assert ok.status_code == 200 and ok.headers["x-auth-user"] == "ava"
    other = await api.get("/api/internal/forward-auth", params={"role": "platform-admin"},
                          headers={"Host": "traces.localhost", "Cookie": f"keelson_console={console}"})
    assert other.status_code == 401
    out = await api.get("/__keelson/logout", headers={"Host": "gateway.localhost",
                                                      "Cookie": f"keelson_console={console}"})
    assert out.status_code == 200
    after = await api.get("/api/internal/forward-auth", params={"role": "platform-admin"},
                          headers={"Host": "gateway.localhost", "Cookie": f"keelson_console={console}"})
    assert after.status_code == 401


async def test_handoff_unknown_host(api: httpx.AsyncClient) -> None:
    resp = await api.get("/__keelson/handoff", params={"ticket": "x"}, headers={"Host": "keelson.localhost"})
    assert resp.status_code == 404


async def test_console_handoff_keeps_the_public_port(api: httpx.AsyncClient, browser_factory: Any,
                                                     settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with HTTP_PORT != 80 the handoff must land on the same port."""
    ava: Browser = await browser_factory("ava")
    monkeypatch.setattr(settings, "public_url", "http://keelson.localhost:8080")
    resp = await ava.get("/api/console/handoff", params={"target": "traces"})
    assert resp.status_code == 302 and resp.headers["cache-control"] == "no-store"
    loc = urlsplit(resp.headers["location"])
    assert loc.netloc == "traces.localhost:8080" and loc.path == "/__keelson/handoff"
    ticket = parse_qs(loc.query)["ticket"][0]
    api.cookies.clear()
    landing = await api.get("/__keelson/handoff", params={"ticket": ticket},
                            headers={"Host": "traces.localhost:8080"})
    assert landing.status_code == 302
    console = landing.cookies["keelson_console"]
    ok = await api.get("/api/internal/forward-auth", params={"role": "platform-admin"},
                       headers={"Host": "traces.localhost:8080", "Cookie": f"keelson_console={console}"})
    assert ok.status_code == 200
