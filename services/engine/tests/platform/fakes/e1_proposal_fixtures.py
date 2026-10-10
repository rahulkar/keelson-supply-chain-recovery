"""Fixtures of the M8 mapping proposer tests (no database, no network): the ``/mcp-lite`` fake's
tools with their **exact** MCP schemas as an approved catalogue (``connectors.ToolRow``), and an
in-memory FastMCP client over the fake (every call recorded; write calls land in
:attr:`e1_lite_server.LiteServer.writes`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Client
from fastmcp.exceptions import ToolError

from e1_lite_server import LiteServer
from keelson_contracts.connectors import ToolClassification
from keelson_engine.core.connectors import ToolRow
from keelson_engine.core.scn_crawl import CrawlError

READ = {"access": "read", "content_trust": "trusted_data", "egress": "internal", "idempotency": "enforced"}
WRITE = {"access": "write", "content_trust": "trusted_data", "egress": "internal", "idempotency": "best_effort"}
CLASSES: dict[str, dict[str, str]] = {
    "list_items": READ, "get_item": READ, "list_cursor": READ,
    "list_notes": {**READ, "content_trust": "untrusted_text"},
    "update_item": WRITE, "create_transfer_order": WRITE,
}
"""As ``register_lite`` approves them (reads trusted, ``list_notes`` untrusted, writes best_effort)."""


async def lite_tools(server: LiteServer, *, overrides: dict[str, dict[str, Any]] | None = None) -> list[ToolRow]:
    """The fake's tools as approved catalogue rows (``overrides``: tool → fields such as a
    ``description`` or a ``classification`` dict)."""
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
    out: list[ToolRow] = []
    for i, t in enumerate(tools):
        over = (overrides or {}).get(t.name, {})
        cls = ToolClassification.model_validate({**CLASSES.get(t.name, READ), **over.get("classification", {})})
        out.append(ToolRow(
            name=t.name, title=None, description=over.get("description", t.description or ""),
            input_schema=over.get("input_schema", dict(t.input_schema or {})),
            output_schema=dict(t.output_schema) if t.output_schema else None, annotations={}, classification=cls,
            definition_hash=f"{i:02d}" + "0" * 62, status=over.get("status", "approved"), connector="lite"))
    return out


@asynccontextmanager
async def lite_caller(server: LiteServer) -> AsyncIterator[Any]:
    """A ``ToolCaller`` over the fake in memory (errors as the crawl's ``CrawlError``)."""
    async with Client(server.mcp) as client:

        async def call(tool: str, args: dict[str, Any]) -> Any:
            try:
                result = await client.call_tool(tool, args)
            except ToolError as exc:
                raise CrawlError("tool_error", f"{tool} failed: {str(exc)[:200]}") from None
            return result.structured_content

        yield call
