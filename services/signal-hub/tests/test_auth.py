"""Service JWT auth for REST and MCP (no database needed: every request is rejected before)."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import jwt
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from conftest import SECRET, make_token, running_app
from signal_hub.app import HubState, create_app
from signal_hub.auth import TokenError, verify_service_token
from signal_hub.config import Settings


def test_valid_token(settings: Settings) -> None:
    p = verify_service_token(make_token("demo-retail", sub="engine"), settings)
    assert p.tenant == "demo-retail" and p.subject == "engine"
    # the contract names iss/aud/tenant/exp only: a token without iat is accepted
    assert verify_service_token(make_token(iat=None), settings).tenant == "demo-retail"
    future_iat = make_token(iat=int(time.time()) + 3600)
    with pytest.raises(TokenError):  # iat in the future (beyond leeway) is rejected by PyJWT
        verify_service_token(future_iat, settings)


@pytest.mark.parametrize("token", [
    make_token(secret="wrong-secret-" + "w" * 40),
    make_token(aud="keelson-optimizer"),
    make_token(iss="someone-else"),
    make_token(ttl=-60),
    make_token(ttl=3600),  # too long-lived
    make_token(tenant=None),
    make_token(tenant="Bad Tenant"),
    make_token(tenant=""),
    jwt.encode({"iss": "keelson-engine", "aud": "signal-hub", "tenant": "t", "iat": int(time.time()),
                "exp": int(time.time()) + 60}, SECRET, algorithm="HS512"),
    "not-a-jwt",
])
def test_invalid_tokens(settings: Settings, token: str) -> None:
    with pytest.raises(TokenError):
        verify_service_token(token, settings)


def test_unconfigured_secret_rejects_everything() -> None:
    with pytest.raises(TokenError):
        verify_service_token(make_token(), Settings(shared_secret=None))


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[Any]:
    state = HubState(settings, None, None, None, None)  # type: ignore[arg-type]
    async with running_app(create_app(settings, state=state, background=False)) as application:
        yield application


async def test_rest_requires_service_token(app: Any) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signal-hub-api:8300") as c:
        assert (await c.get("/healthz")).status_code == 200
        for method, path in [("GET", "/v1/alerts"), ("GET", "/v1/alerts/changes"), ("GET", "/v1/signals"),
                             ("POST", "/v1/ingest/connector-event"), ("PUT", "/v1/tenants/demo-retail/network"),
                             ("POST", "/v1/demo/packs/typhoon-test/start"), ("GET", "/v1/sources")]:
            r = await c.request(method, path, json={})
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.headers["www-authenticate"].startswith("Bearer")
            r = await c.request(method, path, json={}, headers={"Authorization": f"Bearer {make_token(ttl=-60)}"})
            assert r.status_code == 401


async def test_rest_tenant_path_must_match_token(app: Any) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signal-hub-api:8300") as c:
        r = await c.put("/v1/tenants/other-tenant/network", json={"nodes": [], "lanes": []},
                        headers={"Authorization": f"Bearer {make_token('demo-retail')}"})
        assert r.status_code == 403
        r = await c.get("/v1/tenants/other-tenant/alert-rules", headers={"Authorization": f"Bearer {make_token('demo-retail')}"})
        assert r.status_code == 403


async def test_body_limit(app: Any) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signal-hub-api:8300") as c:
        r = await c.post("/v1/ingest/connector-event", content=b"{" + b" " * 600_000 + b"}",
                         headers={"Authorization": f"Bearer {make_token()}", "content-type": "application/json"})
        assert r.status_code == 413


def _mcp(app: Any, token: str | None) -> Client:
    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("follow_redirects", None)
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), follow_redirects=True, **kwargs)

    return Client(StreamableHttpTransport("http://signal-hub-api:8300/mcp", auth=token, httpx_client_factory=factory))


async def test_mcp_requires_service_token(app: Any) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signal-hub-api:8300") as c:
        for token in (None, make_token(aud="other"), make_token(ttl=-60), make_token(ttl=3600), "garbage"):
            h = dict(headers) | ({"Authorization": f"Bearer {token}"} if token else {})
            r = await c.post("/mcp", json=body, headers=h)
            assert r.status_code == 401, (token, r.status_code, r.text[:200])
    with pytest.raises(Exception):  # noqa: B017 - the MCP client surfaces the 401 as an error
        async with _mcp(app, None) as client:
            await client.list_tools()


async def test_mcp_lists_read_only_tools(app: Any) -> None:
    async with _mcp(app, make_token()) as client:
        tools = await client.list_tools()
    names = [t.name for t in tools]
    assert names == ["search_signals", "get_event", "hazards_near_node", "assess_exposure"]
    for t in tools:
        assert t.annotations is not None and t.annotations.read_only_hint is True
        assert "untrusted" in (t.description or "").lower() or t.name in ("hazards_near_node", "assess_exposure")
