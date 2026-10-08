"""Platform-side MCP access: ``tools/list`` for connector tool snapshots and the
connection test of the back-office (``POST /api/admin/connectors/{slug}/test``).

Uses the FastMCP 4 client over streamable HTTP with an ``httpx2`` client routed through
the egress proxy (``CONNECTOR_PROXY_URL``). The runtime's tool *calls* live in
``runtime/mcp_client.py`` (E2); the platform only needs the catalogue.
"""

from __future__ import annotations

from typing import Any

import httpx2

from keelson_engine.config import get_settings

__all__ = [
    "CALL_TIMEOUT_S",
    "CONNECT_TIMEOUT_S",
    "http_client_factory",
    "list_remote_tools",
    "list_remote_tools_with_headers",
    "list_tools_paged",
    "mcp_client",
    "tool_to_dict",
]

CONNECT_TIMEOUT_S = 15.0
CALL_TIMEOUT_S = 120.0
MAX_TOOL_PAGES = 50


def http_client_factory(proxy: str | None, *, call_timeout: float = CALL_TIMEOUT_S,
                        connect_timeout: float = CONNECT_TIMEOUT_S) -> Any:
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **_: Any,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(call_timeout, connect=connect_timeout),
            auth=auth,
            proxy=proxy,
            trust_env=False,
            follow_redirects=False,
        )

    return factory


# M1 name kept for callers
_client_factory = http_client_factory


def tool_to_dict(tool: Any) -> dict[str, Any]:
    """MCP ``Tool`` (model or dict) → wire-shaped dict (camelCase keys, no nulls)."""
    if hasattr(tool, "model_dump"):
        data: dict[str, Any] = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        data = dict(tool)
    return data


def _proxy(proxy: str | None) -> str | None:
    """``None`` = the egress proxy (``CONNECTOR_PROXY_URL``); ``""`` = direct (internal first-party
    services such as the M6 Signal Hub, which the egress proxy must not be asked to reach)."""
    if proxy == "":
        return None
    return proxy if proxy is not None else get_settings().connector_proxy_url


def mcp_client(url: str, headers: dict[str, str] | None, *, proxy: str | None = None,
               timeout: float = CALL_TIMEOUT_S, connect_timeout: float = CONNECT_TIMEOUT_S) -> Any:
    """An unconnected FastMCP client (``mode="auto"``: ``server/discover``, falling back to the
    ``initialize`` handshake) sending ``headers`` on every request, via the egress proxy."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url, headers=dict(headers or {}),
        httpx_client_factory=http_client_factory(_proxy(proxy), call_timeout=timeout,
                                                 connect_timeout=min(connect_timeout, timeout)),
    )
    return Client(transport, timeout=timeout, init_timeout=timeout)


async def list_tools_paged(client: Any, *, max_pages: int = MAX_TOOL_PAGES) -> tuple[list[dict[str, Any]], int]:
    """``tools/list`` page by page (explicit cursors) on a connected client → (tools, pages)."""
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    pages = 0
    while pages < max_pages:
        result = await client.list_tools_mcp(cursor=cursor)
        pages += 1
        tools.extend(tool_to_dict(t) for t in result.tools)
        cursor = result.next_cursor
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return tools, pages


async def list_remote_tools_with_headers(url: str, headers: dict[str, str], *,
                                         proxy: str | None = None) -> list[dict[str, Any]]:
    """``tools/list`` (all pages) as wire dicts, in server order."""
    async with mcp_client(url, headers, proxy=proxy) as client:
        tools, _pages = await list_tools_paged(client)
    return tools


async def list_remote_tools(url: str, bearer: str, *, proxy: str | None = None) -> list[dict[str, Any]]:
    """``tools/list`` (all pages) with a Bearer token."""
    return await list_remote_tools_with_headers(url, {"Authorization": f"Bearer {bearer}"}, proxy=proxy)
