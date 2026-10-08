"""A real FastMCP 4 server on 127.0.0.1 (uvicorn, random port) for connector-registry tests.

Tools mimic a "foreign ERP" (/mcp-lite style, sparse annotations): plain reads, writes with
``request_id`` / ``idempotency_key``, a free-text reader and an open-world search. ``list_page_size=2``
forces ``tools/list`` pagination. An ASGI gate in front checks credentials: a static header
(``X-Api-Key``) **or** a Bearer JWT signed by one of the trusted fake realms.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import jwt
import uvicorn
from fastmcp import FastMCP

API_KEY_HEADER = "x-api-key"
API_KEY = "mx_test_static_secret"
READ = {"readOnlyHint": True}


def build_mcp() -> FastMCP:
    mcp = FastMCP("foreign-erp-test", list_page_size=2)

    @mcp.tool(name="list_items", description="List rows of an entity type.", annotations=READ)
    def list_items(type: str, page: int = 1) -> dict[str, Any]:
        return {"items": [{"type": type, "key": "PO-1"}], "page": page}

    @mcp.tool(name="get_item", description="Get one row by key.", annotations=READ)
    def get_item(type: str, key: str) -> dict[str, Any]:
        return {"type": type, "key": key, "version": 1}

    @mcp.tool(name="update_item", description="Update fields of one row.")
    def update_item(type: str, key: str, fields_json: str, expected_version: int,
                    request_id: str | None = None) -> dict[str, Any]:
        return {"ok": True}

    @mcp.tool(name="create_transfer_order", description="Create a transfer order between DCs.",
              annotations={"readOnlyHint": False, "destructiveHint": True})
    def create_transfer_order(from_dc: str, to_dc: str, idempotency_key: str) -> dict[str, Any]:
        return {"ok": True}

    @mcp.tool(name="list_supplier_messages", description="Messages from suppliers (free text).",
              annotations=READ)
    def list_supplier_messages() -> dict[str, Any]:
        return {"items": []}

    @mcp.tool(name="search_web", description="Search the public web.",
              annotations={"readOnlyHint": True, "openWorldHint": True})
    def search_web(q: str) -> dict[str, Any]:
        return {"items": []}

    return mcp


def add_tier_tools(mcp: FastMCP) -> None:
    """Snapshot (B), change feed (C) and change-set (W) tools."""
    for name in ("scn.snapshot_open", "scn.snapshot_export", "scn.changes_since"):
        mcp.tool(name=name, description=f"{name} (canonical snapshot)", annotations=READ)(lambda: {"ok": True})
    for name in ("create_change_set", "add_change_set_items", "validate_change_set", "apply_change_set"):
        mcp.tool(name=name, description=f"{name}")(lambda idempotency_key="": {"ok": True})


def change_description(mcp: FastMCP, name: str, description: str) -> None:
    """Drift: replace a tool with the same function and a new description."""
    tool = next(t for t in mcp.local_provider._components.values() if getattr(t, "name", None) == name)
    fn = tool.fn  # type: ignore[attr-defined]
    annotations = tool.annotations
    mcp.local_provider.remove_tool(name)
    mcp.tool(name=name, description=description, annotations=annotations)(fn)


@dataclass
class GateState:
    require_auth: bool = True
    api_key: str = API_KEY
    realms: list[Any] = field(default_factory=list)
    seen_headers: list[dict[str, str]] = field(default_factory=list)

    def check(self, headers: dict[str, str]) -> bool:
        if not self.require_auth:
            return True
        if headers.get(API_KEY_HEADER) == self.api_key:
            return True
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            for realm in self.realms:
                try:
                    jwt.decode(token, realm.key.public_key(), algorithms=["RS256"], options={"verify_aud": False})
                    return True
                except jwt.PyJWTError:
                    continue
        return False


class AuthGate:
    def __init__(self, app: Any, state: GateState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            self.state.seen_headers.append(headers)
            if not self.state.check(headers):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer resource_metadata="/.well-known/oauth-protected-resource"'),
                ]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class McpTestServer:
    def __init__(self) -> None:
        self.mcp = build_mcp()
        self.gate = GateState()
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[Any] | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def start(self) -> None:
        app = self.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
        config = uvicorn.Config(AuthGate(app, self.gate), host="127.0.0.1", port=0, log_level="warning",
                                lifespan="on")
        self.server = uvicorn.Server(config)
        # _serve(): like serve() but without capturing SIGINT/SIGTERM (other tests chain those)
        self.task = asyncio.create_task(self.server._serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        assert self.server.started, "MCP test server did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=10)
