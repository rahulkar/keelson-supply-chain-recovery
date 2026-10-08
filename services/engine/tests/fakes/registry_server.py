"""A Tier-A "foreign ERP" MCP server for the M2 connector-registry tests, shaped like Merxa's
``/mcp-lite`` (``22-merxa-m2.md`` §6): plain CRUD tools (``list_items(type, filter_json?,
page?, page_size<=200)``, ``get_item``, ``update_item`` and ``create_transfer_order`` writes),
sparse annotations (``readOnlyHint`` on reads only), no change sets.

It is served over real streamable HTTP (FastMCP ``http_app``, stateless JSON) through an
``httpx2.ASGITransport``, behind an auth middleware that requires the static header
``X-Merxa-Api-Key`` — so the engine's production transport factory, header handling and
``tools/list`` verification run end-to-end without a network. Every request's auth header and
every tool call are recorded.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from typing import Any

import httpx2
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from mcp import types as mcp_types

from keelson_engine.runtime.deps import ToolInfo
from keelson_engine.runtime.mcp_client import proxied_transport_factory
from keelson_engine.runtime.toolsets import definition_hash, wire_tool

API_KEY_HEADER = "X-Merxa-Api-Key"
READ = mcp_types.ToolAnnotations(read_only_hint=True)

PURCHASE_ORDERS = [
    {"code": "PO-BD-0001", "supplier_code": "TRIM-BD-01", "dest_dc": "UK1", "status": "open", "eta_dc": "2026-10-29", "version": 1},
    {"code": "PO-BD-0002", "supplier_code": "TRIM-BD-01", "dest_dc": "NL1", "status": "open", "eta_dc": "2026-11-03", "version": 1},
    {"code": "PO-BD-0003", "supplier_code": "TRIM-BD-01", "dest_dc": "UK1", "status": "open", "eta_dc": "2026-11-10", "version": 2},
    {"code": "PO-VN-0001", "supplier_code": "SUP-VN-01", "dest_dc": "PL1", "status": "in_production", "eta_dc": "2026-11-17", "version": 1},
    {"code": "PO-KH-0001", "supplier_code": "TRIM-KH-01", "dest_dc": "DE1", "status": "received", "eta_dc": "2026-09-30", "version": 3},
]
TYPES = ("sku", "style", "store", "dc", "purchase_order", "po_line", "inventory", "forecast", "allocation", "transfer_order")


@dataclass
class RegistryServer:
    """One Tier-A server instance; ``url`` is what the connector row registers."""

    api_key: str = "mx_test_secretvalue"
    url: str = "http://erp.example.test/mcp-lite"
    vendor: str = "Northwind ERP"
    records: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"purchase_order": copy.deepcopy(PURCHASE_ORDERS)}
    )
    seen_keys: list[str | None] = field(default_factory=list)
    """Value of the API key header on every HTTP request (``None`` = absent)."""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    writes: int = 0
    seen_bearer: int = 0
    _task: asyncio.Task[None] | None = None
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.mcp = self._build()
        path = httpx2.URL(self.url).path
        inner = self.mcp.http_app(path=path, stateless_http=True, json_response=True)
        self._inner = inner
        self.app = self._auth(inner)

    # ─────────────────────────────── HTTP plumbing ───────────────────────────────

    def _auth(self, inner: Any) -> Any:
        server = self

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await inner(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            key = headers.get(API_KEY_HEADER.lower())
            bearer = headers.get("authorization", "")
            if key is None and bearer.lower().startswith("bearer "):
                key = bearer[7:]  # /mcp-lite also accepts a Bearer token (client-credentials connectors)
                server.seen_bearer += 1
            server.seen_keys.append(key)
            if key != server.api_key:
                body = json.dumps({"error": "invalid_api_key"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            await inner(scope, receive, send)

        return app

    async def start(self) -> RegistryServer:
        """Run the app lifespan (FastMCP's session manager) in its own task until :meth:`stop`
        (enter and exit must happen in the same context). Idempotent."""
        if self._task is not None:
            return self
        started, self._stopping = asyncio.Event(), asyncio.Event()

        async def lifespan() -> None:
            async with self._inner.router.lifespan_context(self._inner):
                started.set()
                await self._stopping.wait()

        self._task = asyncio.create_task(lifespan())
        await started.wait()
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._stopping.set()
            await self._task
            self._task = None

    def asgi_transport(self) -> httpx2.ASGITransport:
        return httpx2.ASGITransport(app=self.app)

    async def snapshot(
        self,
        *,
        status: dict[str, str] | None = None,
        trust: dict[str, str] | None = None,
        access: dict[str, str] | None = None,
    ) -> list[ToolInfo]:
        """What the admin registry stores after test + review (M2 classification columns):
        reads ``approved``; writes ``approved`` too but ``access=write`` (never bound to agents);
        overrides per tool name."""
        if self._task is None:
            await self.start()
        factory = proxied_transport_factory(None, client_transport=self.asgi_transport())
        async with Client(factory(self.url, "", headers={API_KEY_HEADER: self.api_key})) as client:
            tools = await client.list_tools()
        out: list[ToolInfo] = []
        for tool in tools:
            wire = wire_tool(tool)
            ann = wire.get("annotations") or {}
            read = bool(ann.get("readOnlyHint"))
            out.append(
                ToolInfo(
                    name=tool.name,
                    title=wire.get("title"),
                    description=wire.get("description") or "",
                    input_schema=dict(wire.get("inputSchema") or {}),
                    output_schema=wire.get("outputSchema"),
                    access=(access or {}).get(tool.name, "read" if read else "write"),
                    content_trust=(trust or {}).get(tool.name, "trusted_data"),
                    status=(status or {}).get(tool.name, "approved"),
                    definition_hash=definition_hash(wire),
                    annotations=dict(ann),
                )
            )
        return out

    # ─────────────────────────────────── tools ───────────────────────────────────

    def _build(self) -> FastMCP:
        mcp = FastMCP(name="Tier-A CRUD (test)")
        server = self

        @mcp.tool(name="list_items", annotations=READ,
                  description="List records of a type. filter_json: JSON object of field equality filters.")
        async def list_items(type: str, filter_json: str | None = None, page: int = 1, page_size: int = 50) -> ToolResult:
            server.calls.append(("list_items", {"type": type, "filter_json": filter_json, "page": page}))
            if type not in TYPES:
                raise ToolError(f"invalid_parameter: unknown type {type}")
            if page_size > 200:
                raise ToolError("invalid_parameter: page_size must be <= 200")
            filters = json.loads(filter_json) if filter_json else {}
            rows = [r for r in server.records.get(type, []) if all(r.get(k) == v for k, v in filters.items())]
            start = (page - 1) * page_size
            chunk = rows[start : start + page_size]
            nxt = page + 1 if start + page_size < len(rows) else None
            return ToolResult(content=f"{len(chunk)} {type} records",
                              structured_content={"items": copy.deepcopy(chunk), "page": page, "page_size": page_size,
                                                  "total": len(rows), "next_page": nxt})

        @mcp.tool(name="get_item", annotations=READ, description="Get one record by type and key.")
        async def get_item(type: str, key: str) -> ToolResult:
            server.calls.append(("get_item", {"type": type, "key": key}))
            for row in server.records.get(type, []):
                if row.get("code") == key:
                    return ToolResult(content=key, structured_content={"type": type, "key": key, "data": copy.deepcopy(row)})
            raise ToolError(f"not_found: {type} {key}")

        @mcp.tool(name="update_item", description="Update fields of a record (write).")
        async def update_item(type: str, key: str, fields_json: str, expected_version: int,
                              request_id: str | None = None) -> ToolResult:
            server.calls.append(("update_item", {"type": type, "key": key}))
            server.writes += 1
            return ToolResult(content="updated", structured_content={"ok": True})

        @mcp.tool(name="create_transfer_order", description="Create a transfer order (write).")
        async def create_transfer_order(from_dc: str, to_dc: str, ship_date: str, lines_json: str) -> ToolResult:
            server.calls.append(("create_transfer_order", {"from_dc": from_dc, "to_dc": to_dc}))
            server.writes += 1
            return ToolResult(content="created", structured_content={"code": "TO-9999"})

        return mcp
