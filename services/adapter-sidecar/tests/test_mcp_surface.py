"""The MCP surface as Keelson sees it: deterministic order, annotations, output schemas, the op enum
of ``add_change_set_items``, tier detection, idempotency classification, and descriptions free of
the words Keelson's classifier reads as untrusted free text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from conftest import Running
from keelson_contracts.connectors import ConnectorTier, detect_tiers

from adapter_sidecar.mcp_server import READ_TOOLS, WRITE_TOOLS

# Copied from services/engine/src/keelson_engine/core/connectors.py (_UNTRUSTED_WORDS /
# _mentions_untrusted): a tool whose name, title or description matches is suggested as
# untrusted_text, which would keep it from investigators and change-set binding.
_UNTRUSTED_WORDS = re.compile(r"(?<![a-z])(messages?|e-?mails?|events?|news|comments?|notes?)(?![a-z])", re.I)
_OP_TOKEN = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b")


def mentions_untrusted(tool: Mapping[str, Any]) -> bool:
    name = str(tool.get("name") or "").replace("_", " ").replace(".", " ").replace("-", " ")
    return any(_UNTRUSTED_WORDS.search(part) for part in (name, str(tool.get("title") or ""),
                                                          str(tool.get("description") or "")))


def ops_from_schema(schema: Any) -> set[str]:
    """Copied from services/engine/src/keelson_engine/runtime/candidates.py (ops_from_schema)."""
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            props = node.get("properties")
            if isinstance(props, Mapping) and isinstance(props.get("op"), Mapping):
                op = props["op"]
                for value in op.get("enum") or ():
                    if isinstance(value, str):
                        found.add(value)
                if isinstance(op.get("const"), str):
                    found.add(op["const"])
                if not op.get("enum") and isinstance(op.get("description"), str):
                    found.update(_OP_TOKEN.findall(op["description"]))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return {op for op in found if not op.startswith("revert.")}


async def test_tool_list(fernhill: Running) -> None:
    async with fernhill.client() as c:
        tools = await c.list_tools()
    names = [t.name for t in tools]
    assert names == [*READ_TOOLS, *WRITE_TOOLS]
    assert detect_tiers(names) == [ConnectorTier.A, ConnectorTier.B, ConnectorTier.C, ConnectorTier.W]
    by = {t.name: t for t in tools}
    for t in tools:
        assert t.annotations is not None and t.annotations.open_world_hint is False
        assert t.output_schema and t.output_schema["type"] == "object"
        assert "Requires scope:" in (t.description or "")
        assert not mentions_untrusted({"name": t.name, "title": t.title, "description": t.description}), t.name
    for name in READ_TOOLS:
        assert by[name].annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        ann = by[name].annotations
        assert ann.read_only_hint is False and ann.idempotent_hint is True
        # Keelson's classifier: a required idempotency_key → idempotency "enforced"
        assert "idempotency_key" in by[name].input_schema["required"]
        assert ann.destructive_hint is (name in ("apply_change_set", "rollback_change_set"))
    assert ops_from_schema(by["add_change_set_items"].input_schema) == {"transfer_order.create", "allocation.set_qty"}
    assert by["scn.snapshot_open"].input_schema["required"] == ["entities"]


async def test_system_info(fernhill: Running) -> None:
    async with fernhill.client() as c:
        res = await c.call_tool("get_system_info", {})
    info = res.structured_content
    assert info is not None
    assert info["entities"]["allocation"] == "mapped" and info["entities"]["promotion"] == "empty"
    assert sum(v == "mapped" for v in info["entities"].values()) == 8
    assert info["ops"] == ["allocation.set_qty", "transfer_order.create"]
    assert info["consistency"] == "consistent" and info["canonical_model"] == "scn/1.0"
    assert res.content[0].text.startswith("Fernhill ERP: 8 mapped")
