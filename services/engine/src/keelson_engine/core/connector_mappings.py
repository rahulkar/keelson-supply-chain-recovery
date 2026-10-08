"""Tier-A connector mappings: storage, validation, capabilities and the action dry-run
(``docs/contracts/110-m7-backoffice-generic.md`` §4).

Table ``connector_mapping(tenant, connector, kind read|action, version, mapping, summary,
created_by, created_at)`` (migration 0010, RLS FORCE; deleted with the connector). Each ``PUT``
stores a new version (``1, 2, …``); the latest version is the active one. A mapping is validated by
the contract model (``ReadMapping`` / ``ActionMapping``: grammar, required columns, templates,
idempotency rules) **and** against the connector's approved tool catalogue
(``read_mapping_violations`` / ``action_mapping_violations``: every crawl source an approved,
trusted ``read`` tool; every action tool an approved write tool whose confirmed idempotency is at
least the claim; every read-back an approved read tool). The catalogue is checked again before
every crawl (a tool may have been reclassified since) and before every dry-run.

**Dry-run** (``POST /api/admin/connectors/{slug}/mappings/action/dry-run``): renders the exact
tool call the commit service would make for a sample action — the action tool is **never** called
— and, with ``run_readback``, runs the read-back query (an approved read tool, reader principal,
egress proxy) and reports whether the sample's effect is visible. Unmapped ops answer the guided
reason.

Mapping changes are not guarded (they cannot widen what a tool may do: the classification and the
binding are); they are audited (``connector.mapping.update``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import ValidationError
from sqlalchemy import text

from keelson_contracts.changeset import OP_PAYLOAD_MODELS
from keelson_contracts.connectors import ConnectorCapabilities, ToolClassification, summarize_capabilities
from keelson_contracts.mapping import (
    ActionDryRunRequest,
    ActionDryRunResult,
    ActionMapping,
    ReadMapping,
    action_mapping_violations,
    read_mapping_violations,
)
from keelson_engine.core import connectors as reg
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.mapping_eval import (
    ActionContext,
    MappingEvalError,
    canonical_json,
    readback_items,
    readback_match,
    render_template,
)
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "MappingKind",
    "approved_classifications",
    "capabilities",
    "dry_run",
    "get_action_mapping",
    "get_mapping",
    "get_read_mapping",
    "list_versions",
    "put_mapping",
    "tool_call",
]

log = get_logger(__name__)

MappingKind = Literal["read", "action"]
KINDS: Final = ("read", "action")
_MODELS: Final[Mapping[str, type[ReadMapping] | type[ActionMapping]]] = {"read": ReadMapping, "action": ActionMapping}


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise AdminError(404, "not_found", "unknown mapping kind (read or action)")
    return kind


def _errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))[:300]}
            for e in exc.errors()[:30]]


async def approved_classifications(tenant: str, connector: str) -> dict[str, ToolClassification]:
    """Approved tools of the connector (any connector status) → admin-confirmed classification."""
    return {t.name: t.classification for t in await reg.list_tools(tenant, connector, include_removed=False)
            if t.status == "approved"}


def _row(m: Mapping[str, Any]) -> dict[str, Any]:
    return {"connector": m["connector"], "kind": m["kind"], "version": int(m["version"]), "mapping": m["mapping"],
            "summary": m["summary"], "created_by": m["created_by"],
            "created_at": m["created_at"].isoformat() if m["created_at"] else None}


async def get_mapping(tenant: str, connector: str, kind: str, version: int | None = None) -> dict[str, Any] | None:
    """The latest (or a given) version as ``{connector, kind, version, mapping, summary, created_by,
    created_at}``, or ``None``."""
    _kind(kind)
    sql = ("SELECT connector, kind, version, mapping, summary, created_by, created_at FROM connector_mapping "
           "WHERE tenant = :t AND connector = :c AND kind = :k "
           + ("AND version = :v" if version is not None else "ORDER BY version DESC LIMIT 1"))
    params: dict[str, Any] = {"t": tenant, "c": connector, "k": kind}
    if version is not None:
        params["v"] = int(version)
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(sql), params)).mappings().first()
    return _row(m) if m is not None else None


async def list_versions(tenant: str, connector: str, kind: str) -> list[dict[str, Any]]:
    _kind(kind)
    async with tenant_session(tenant) as s:
        rows = (await s.execute(
            text("SELECT connector, kind, version, mapping, summary, created_by, created_at FROM connector_mapping "
                 "WHERE tenant = :t AND connector = :c AND kind = :k ORDER BY version DESC"),
            {"t": tenant, "c": connector, "k": kind})).mappings().all()
    return [{k: v for k, v in _row(r).items() if k != "mapping"} for r in rows]


async def get_read_mapping(tenant: str, connector: str) -> ReadMapping | None:
    """The active read mapping (``None`` when absent or no longer valid under the contract)."""
    row = await get_mapping(tenant, connector, "read")
    if row is None:
        return None
    try:
        return ReadMapping.model_validate(row["mapping"])
    except ValidationError as exc:
        log.warning("stored read mapping is invalid", tenant=tenant, connector=connector, error=str(exc)[:300])
        return None


async def _stored_action_mapping(tenant: str, connector: str) -> ActionMapping | None:
    row = await get_mapping(tenant, connector, "action")
    if row is None:
        return None
    try:
        return ActionMapping.model_validate(row["mapping"])
    except ValidationError as exc:
        log.warning("stored action mapping is invalid", tenant=tenant, connector=connector, error=str(exc)[:300])
        return None


async def get_action_mapping(tenant: str, connector: str) -> ActionMapping | None:
    """The active action mapping (the runtime's per-op commit reads it; ``None`` = every op guided).

    Re-checked against the **current** approved tool catalogue, like every crawl and dry-run: an op
    whose action tool is no longer an approved write tool (confirming the claimed idempotency), or
    whose read-back tool is no longer an approved read tool (e.g. reclassified as write, disabled,
    removed), is left out — it becomes a guided step instead of a call to a tool the admins no
    longer vouch for. ``None`` when no op survives."""
    mapping = await _stored_action_mapping(tenant, connector)
    if mapping is None:
        return None
    tools = await approved_classifications(tenant, connector)
    kept = [op for op in mapping.ops if not action_mapping_violations(ActionMapping(ops=[op]), tools=tools)]
    if len(kept) != len(mapping.ops):
        log.warning("action mapping ops dropped: their tools changed", tenant=tenant, connector=connector,
                    ops=sorted({o.op for o in mapping.ops} - {o.op for o in kept}))
    if not kept:
        return None
    return mapping if len(kept) == len(mapping.ops) else ActionMapping(ops=kept)


def _summary(kind: str, model: ReadMapping | ActionMapping) -> str:
    if isinstance(model, ReadMapping):
        return f"{len(model.entities)} entity mappings: {', '.join(model.mapped_entities)}"[:300]
    return f"{len(model.ops)} ops: {', '.join(model.mapped_ops)}"[:300]


def violations(model: ReadMapping | ActionMapping, tools: Mapping[str, ToolClassification]) -> list[str]:
    if isinstance(model, ReadMapping):
        return read_mapping_violations(model, tools=tools)
    return action_mapping_violations(model, tools=tools)


async def put_mapping(tenant: str, connector: str, kind: str, mapping: Any, *, base_version: int | None,
                      actor: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Validate + store a new version → ``(row, created)`` (``created`` False when unchanged).
    422 ``mapping_invalid`` (``errors`` = contract errors, ``problems`` = catalogue cross-checks);
    409 ``version_conflict`` when ``base_version`` is not the latest version (``None`` = none yet)."""
    _kind(kind)
    try:
        conn = await reg.get_connector(tenant, connector)
    except reg.ConnectorNotFound:
        raise AdminError(404, "not_found", "connector not found") from None
    if conn.is_internal:
        raise AdminError(409, "provisioned_connector", "internal connectors take no mapping")
    if not isinstance(mapping, dict):
        raise AdminError(422, "mapping_invalid", "the mapping must be an object")
    try:
        model = _MODELS[kind].model_validate({"kind": kind, **mapping})
    except ValidationError as exc:
        raise AdminError(422, "mapping_invalid", f"the {kind} mapping is invalid", errors=_errors(exc)) from None
    problems = violations(model, await approved_classifications(tenant, connector))
    if problems:
        raise AdminError(422, "mapping_invalid", problems[0], problems=problems[:30])
    stored = model.model_dump(mode="json", by_alias=True)
    async with tenant_session(tenant) as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                        {"k": f"connector_mapping|{tenant}|{connector}|{kind}"})
        latest = (await s.execute(
            text("SELECT version, mapping FROM connector_mapping WHERE tenant = :t AND connector = :c AND kind = :k "
                 "ORDER BY version DESC LIMIT 1"), {"t": tenant, "c": connector, "k": kind})).first()
        current = int(latest[0]) if latest is not None else None
        if current != base_version:
            raise AdminError(409, "version_conflict", "the mapping changed since you loaded it",
                             latest_version=current)
        if latest is not None and canonical_json(latest[1]) == canonical_json(stored):
            unchanged = True
        else:
            unchanged = False
            await s.execute(
                text("INSERT INTO connector_mapping (tenant, connector, kind, version, mapping, summary, created_by) "
                     "VALUES (:t, :c, :k, :v, CAST(:m AS jsonb), :sum, :by)"),
                {"t": tenant, "c": connector, "k": kind, "v": (current or 0) + 1, "m": dumps(stored),
                 "sum": _summary(kind, model), "by": actor.get("username")},
            )
    row = await get_mapping(tenant, connector, kind)
    assert row is not None
    if unchanged:
        return row, False
    await admin_audit(actor, "connector.mapping.update", f"connector:{connector}/mapping:{kind}",
                      {"version": current}, {"version": row["version"], "summary": row["summary"]}, tenant=tenant)
    return row, True


async def capabilities(tenant: str, connector: str) -> dict[str, Any]:
    """``ConnectorCapabilities`` JSON (tiers + mapping versions → sync / commit mode / advisory)."""
    try:
        conn = await reg.get_connector(tenant, connector)
    except reg.ConnectorNotFound:
        raise AdminError(404, "not_found", "connector not found") from None
    read = await get_mapping(tenant, connector, "read")
    action = await get_action_mapping(tenant, connector)
    action_row = await get_mapping(tenant, connector, "action") if action is not None else None
    tiers = [t for t in (conn.tiers or []) if t in ("A", "B", "C", "W")]
    caps: ConnectorCapabilities = summarize_capabilities(
        connector, tiers, read_mapping_version=read["version"] if read else None,
        action_mapping_version=action_row["version"] if action_row else None,
        mapped_ops=action.mapped_ops if action is not None else ())
    return caps.model_dump(mode="json")


# ───────────────────────────── tool calls (reader principal) ─────────────────────────────


async def tool_call(conn: reg.ConnectorRow, tool: str, args: dict[str, Any], *, principal: str = "reader") -> Any:
    """One MCP tool call (``structuredContent``) through the egress proxy (internal connectors:
    direct). Raises :class:`MappingToolError` (message without secrets)."""
    from mcp import types as mcp_types

    from keelson_engine.core.mcp import mcp_client

    try:
        headers = await reg.headers_for(conn, principal)
    except Exception as exc:  # noqa: BLE001 - token errors: class name only
        raise MappingToolError(f"credentials unavailable ({type(exc).__name__})") from None
    try:
        async with mcp_client(conn.url, headers, proxy="" if conn.is_internal else None, timeout=60.0) as client:
            result = await client.call_tool_mcp(tool, args)
    except Exception as exc:  # noqa: BLE001 - transport / auth: class name only (URLs may carry secrets)
        raise MappingToolError(f"{tool} call failed ({type(exc).__name__})") from None
    if result.is_error:
        msg = " ".join(b.text for b in result.content if isinstance(b, mcp_types.TextContent))
        raise MappingToolError(f"{tool} failed: {msg[:200]}")
    if result.structured_content is not None:
        return result.structured_content
    import json

    texts = [b.text for b in result.content if isinstance(b, mcp_types.TextContent)]
    try:
        return json.loads(texts[0]) if len(texts) == 1 else None
    except ValueError:
        return None


class MappingToolError(Exception):
    pass


# ───────────────────────────── dry-run ─────────────────────────────


def _dry_request_id(tenant: str, connector: str, req: ActionDryRunRequest) -> str:
    digest = hashlib.sha256(canonical_json([tenant, connector, req.op, req.target_ref, req.expected_version,
                                            req.payload]).encode("utf-8")).hexdigest()[:24]
    return f"dryrun-{digest}"


async def dry_run(tenant: str, connector: str, req: ActionDryRunRequest, actor: dict[str, Any]) -> dict[str, Any]:
    """Render the action's tool call (never executed) and optionally run its read-back."""
    try:
        conn = await reg.get_connector(tenant, connector)
    except reg.ConnectorNotFound:
        raise AdminError(404, "not_found", "connector not found") from None
    payload_model = OP_PAYLOAD_MODELS.get(req.op)
    if payload_model is not None:
        try:
            payload_model.model_validate(req.payload)
        except ValidationError as exc:
            raise AdminError(422, "payload_invalid", f"the sample payload is not a valid {req.op} payload",
                             errors=_errors(exc)) from None
    mapping = await _stored_action_mapping(tenant, connector)
    op_map = mapping.for_op(req.op) if mapping is not None else None
    if op_map is None:
        reason = ("no action mapping: every op is guided" if mapping is None
                  else f"{req.op} is not mapped: it becomes a guided step with generated instructions")
        return ActionDryRunResult(op=req.op, mapped=False, call=None, idempotency=None, request_id_arg=None,
                                  readback=None, guided_reason=reason).model_dump(mode="json")
    tools = await approved_classifications(tenant, connector)
    problems = action_mapping_violations(ActionMapping(ops=[op_map]), tools=tools)
    if problems:
        raise AdminError(409, "mapping_stale", problems[0], problems=problems)
    request_id = _dry_request_id(tenant, connector, req) if op_map.request_id_arg else None
    ctx = ActionContext(op=req.op, payload=req.payload, target_ref=req.target_ref,
                        expected_version=req.expected_version, request_id=request_id)
    try:
        args = render_template(op_map.args, ctx)
        if op_map.request_id_arg and request_id:
            args[op_map.request_id_arg] = request_id
        rb_call = (None if op_map.readback is None
                   else {"tool": op_map.readback.tool, "args": render_template(op_map.readback.args, ctx)})
    except MappingEvalError as exc:
        raise AdminError(422, "render_failed", f"the sample cannot be rendered: {exc}") from None
    readback: dict[str, Any] | None = None
    if rb_call is not None:
        readback = {"call": rb_call, "executed": False, "ok": None, "items_found": None, "matched": None}
        if req.run_readback:
            readback["executed"] = True
            rb_cls = tools.get(rb_call["tool"])
            if rb_cls is None or rb_cls.access.value != "read":  # defence in depth: never a write
                readback.update(ok=False, detail="the read-back tool is not an approved read tool")
            elif not conn.usable:
                readback.update(ok=False, detail=f"the connector is {conn.status}: activate it to run the read-back")
            else:
                assert op_map.readback is not None
                try:
                    result = await tool_call(conn, rb_call["tool"], dict(rb_call["args"]))
                    items = readback_items(result, op_map.readback.items_path)
                    matched = readback_match(items, op_map.readback.match, ctx)
                    readback.update(ok=True, items_found=len(items), matched=matched,
                                    detail=f"{len(items)} item(s) read back; {'match' if matched else 'no match'}")
                except MappingToolError as exc:
                    readback.update(ok=False, detail=str(exc)[:500])
                except MappingEvalError as exc:
                    readback.update(ok=False, detail=f"match: {str(exc)[:400]}")
    result_model = ActionDryRunResult.model_validate({
        "op": req.op, "mapped": True, "call": {"tool": op_map.tool, "args": args},
        "idempotency": op_map.idempotency.value, "request_id_arg": op_map.request_id_arg,
        "readback": readback, "guided_reason": None,
    })
    await admin_audit(actor, "connector.mapping.dry_run", f"connector:{connector}/mapping:action",
                      None, {"op": req.op, "readback_executed": bool(readback and readback["executed"]),
                             "matched": readback.get("matched") if readback else None}, tenant=tenant)
    return result_model.model_dump(mode="json")
