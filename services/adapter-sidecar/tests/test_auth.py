"""Inbound auth: static header keys (constant-time sha256 match, header → Bearer), per-tool scopes,
the OAuth JWT mode with protected-resource metadata, and the separate admin key."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
import yaml
from conftest import ADMIN_KEY, API_KEY, FERNHILL, RESOURCE, Running, sidecar_env, start_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.exceptions import ToolError
from helpers import TO_ITEM, call
from jwt.algorithms import RSAAlgorithm

from adapter_sidecar.errors import ConfigError

READER_KEY = "fh_reader_key_0123456789abcdefghijklmnop"
IDP = "http://idp.example.test/realms/erp"
IDP_JWKS = "http://idp.example.test/realms/erp/certs"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


async def test_missing_or_wrong_key_is_401(fernhill: Running) -> None:
    async with fernhill.http() as http:
        assert (await http.post("/mcp", json=LIST, headers=MCP_HEADERS)).status_code == 401
        for headers in ({"X-Adapter-Key": "fh_wrong_key_0123456789abcdefghijklmnop"},
                        {"X-Adapter-Key": API_KEY[:-1]}, {"Authorization": "Bearer nope"},
                        {"X-Adapter-Key": ADMIN_KEY}):
            r = await http.post("/mcp", json=LIST, headers={**MCP_HEADERS, **headers})
            assert r.status_code == 401, headers
        ok = await http.post("/mcp", json=LIST, headers={**MCP_HEADERS, "X-Adapter-Key": API_KEY})
        assert ok.status_code == 200 and "get_system_info" in ok.text


async def test_bearer_key_works(fernhill: Running) -> None:
    async with fernhill.client(api_key=None, token=API_KEY) as c:
        info = await call(c, "get_system_info", {})
    assert info["tiers_offered"] == ["A", "B", "C", "W"] and info["backend"] == "csv"


async def test_short_or_missing_key_is_a_config_error(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.app import create_app
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    for value in ("short", ""):
        settings = Settings.from_env(sidecar_env(tmp_path, data_dir, SIDECAR_API_KEY=value))
        with pytest.raises(ConfigError, match="SIDECAR_API_KEY"):
            create_app(settings, Sidecar(settings))


def _variant(tmp_path: Path, edit: Any) -> Path:
    raw = yaml.safe_load((FERNHILL / "sidecar.yaml").read_text(encoding="utf-8"))
    edit(raw)
    path = tmp_path / "variant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


async def test_scopes_are_enforced_per_tool(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    def edit(raw: dict[str, Any]) -> None:
        raw["auth"]["static_header"]["keys"].append(
            {"name": "reader", "key_env": "READER_KEY", "scopes": ["catalog:read", "snapshot:read"]})

    settings = Settings.from_env(sidecar_env(tmp_path, data_dir, _variant(tmp_path, edit), READER_KEY=READER_KEY))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar)
    run = Running(app, sidecar, data_dir, tmp_path)
    try:
        async with run.client(READER_KEY) as c:
            assert len(await c.list_tools()) == 13  # every key sees the complete list
            assert (await call(c, "scn.snapshot_open", {"entities": ["item"]}))["entities"]["item"]["rows"] == 12
            with pytest.raises(ToolError, match="insufficient_scope: .*changes:preview"):
                await c.call_tool("create_change_set", {"title": "x", "idempotency_key": "k1"})
            with pytest.raises(ToolError, match="insufficient_scope: .*orders:read"):
                await c.call_tool("list_transfer_orders", {})
            with pytest.raises(ToolError, match="insufficient_scope: .*changes:apply or changes:apply:auto"):
                await c.call_tool("apply_change_set", {"change_set_id": "CS-0001", "preview_hash": "a" * 64,
                                                       "approval_ref": "A", "approval_receipt": "x",
                                                       "idempotency_key": "k2"})
        async with run.client() as c:
            created = await call(c, "create_change_set", {"title": "x", "idempotency_key": "k3"})
            await call(c, "add_change_set_items", {"change_set_id": created["change_set_id"], "items": [TO_ITEM],
                                                   "idempotency_key": "k4"})
        async with run.client(READER_KEY) as c:  # a read scope may read a change set
            assert (await call(c, "get_change_set", {"change_set_id": "CS-0001"}))["items_count"] == 1
    finally:
        await shutdown()


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def client(self) -> httpx2.AsyncClient:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if str(request.url) == IDP_JWKS:
                jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
                jwk.update({"kid": "idp-1", "use": "sig", "alg": "RS256"})
                return httpx2.Response(200, json={"keys": [jwk]})
            return httpx2.Response(404)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    def token(self, *, aud: str = RESOURCE, iss: str = IDP, exp_in: int = 300, scope: str = "snapshot:read") -> str:
        now = int(time.time())
        return jwt.encode({"iss": iss, "aud": aud, "sub": "svc-keelson", "iat": now, "exp": now + exp_in,
                           "scope": scope, "client_id": "keelson"}, self.key, algorithm="RS256",
                          headers={"kid": "idp-1"})


async def test_oauth_mode(tmp_path: Path, data_dir: Path) -> None:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    idp = FakeIdp()

    def edit(raw: dict[str, Any]) -> None:
        raw["auth"]["oauth"] = {"issuer": IDP, "jwks_url": IDP_JWKS}

    settings = Settings.from_env(sidecar_env(tmp_path, data_dir, _variant(tmp_path, edit)))
    sidecar = Sidecar(settings)
    app, shutdown = await start_app(settings, sidecar, auth_http_client=idp.client())
    run = Running(app, sidecar, data_dir, tmp_path)
    try:
        async with run.client(api_key=None, token=idp.token()) as c:
            assert (await call(c, "scn.snapshot_open", {"entities": ["item"]}))["entities"]["item"]["rows"] == 12
            with pytest.raises(ToolError, match="insufficient_scope"):
                await c.call_tool("create_change_set", {"title": "x", "idempotency_key": "k1"})
        async with run.http() as http:
            for bad in (idp.token(aud="http://merxa.localhost/mcp"), idp.token(iss="http://evil.test"),
                        idp.token(exp_in=-120)):
                r = await http.post("/mcp", json=LIST, headers={**MCP_HEADERS, "Authorization": f"Bearer {bad}"})
                assert r.status_code == 401
            prm = await http.get("/.well-known/oauth-protected-resource/mcp")
            assert prm.status_code == 200
            doc = prm.json()
            assert doc["resource"].rstrip("/") == RESOURCE and IDP in [s.rstrip("/") for s in doc["authorization_servers"]]
            assert (await http.get("/.well-known/oauth-protected-resource")).status_code == 200
        async with run.client() as c:  # the static key still works alongside OAuth
            assert (await call(c, "get_system_info", {}))["system"] == "Fernhill ERP"
    finally:
        await shutdown()


async def test_admin_key_is_separate(fernhill: Running) -> None:
    async with fernhill.http() as http:
        assert (await http.get("/v1/admin/stats")).status_code == 401
        assert (await http.get("/v1/admin/stats", headers={"X-Admin-Key": API_KEY})).status_code == 401
        assert (await http.get("/v1/admin/stats", headers={"X-Adapter-Key": API_KEY})).status_code == 401
        ok = await http.get("/v1/admin/stats", headers={"X-Admin-Key": ADMIN_KEY})
        assert ok.status_code == 200 and ok.json()["tables"]["warehouses"] == 2
