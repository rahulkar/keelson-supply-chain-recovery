"""The sidecar's MCP server (FastMCP 4, stateless streamable HTTP) mounted at ``/mcp``.

Tools are registered — and listed — in a fixed order (:func:`tool_order`): reads first, then the
change-set writes (omitted when the backend is read-only, so a REST sidecar is tiers A·B·C and a
CSV / SQL one A·B·C·W for ``keelson_contracts.connectors.detect_tiers``). Every tool has an output
schema and returns ``structuredContent`` plus a one-line summary. Annotations: reads
``readOnlyHint``; create / add / validate non-destructive and idempotent; apply / rollback
destructive and idempotent; ``openWorldHint`` false everywhere (Keelson suggests internal egress).
Write tools require ``idempotency_key`` (Keelson classifies them ``idempotency: enforced``), and
``add_change_set_items`` publishes the configured ops as an ``op`` enum (Keelson's
``ops_from_schema``).

Descriptions never use the words Keelson's classifier reads as untrusted free text (message,
e-mail, event, news, comment, note): every tool here returns structured system data.

Errors are tool errors ``"<code>: <detail> (reason: <reason>)"`` — the Merxa format.
"""

# NB: no ``from __future__ import annotations`` here: the change-set item model is built at
# runtime from the configured ops and the tool signatures reference it from this closure.

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.base import ToolResult
from keelson_contracts.scn import CANONICAL_MODEL, SCN_ENTITIES
from pydantic import BaseModel, ConfigDict, Field, create_model

from adapter_sidecar import models as m
from adapter_sidecar.auth import Principal
from adapter_sidecar.config import (
    READ_SCOPES,
    SCOPE_APPLY,
    SCOPE_APPLY_AUTO,
    SCOPE_CATALOG,
    SCOPE_ORDERS,
    SCOPE_PREVIEW,
    SCOPE_SNAPSHOT,
    SidecarConfig,
)
from adapter_sidecar.errors import Problem
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)

READ_TOOLS: tuple[str, ...] = (
    "get_system_info",
    "scn.snapshot_open",
    "scn.snapshot_export",
    "scn.changes_since",
    "get_change_set",
    "get_audit_trail",
    "get_operator_guide",
    "list_transfer_orders",
)
WRITE_TOOLS: tuple[str, ...] = (
    "create_change_set",
    "add_change_set_items",
    "validate_change_set",
    "apply_change_set",
    "rollback_change_set",
)

READ_ANN = mcp_types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE_ANN = mcp_types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                      open_world_hint=False)
APPLY_ANN = mcp_types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                      open_world_hint=False)

IdemKey = Annotated[
    str, Field(min_length=1, max_length=255, description="client-chosen key; retries with the same key are replay-safe"),
]
Receipt = Annotated[str, Field(min_length=1, description="ES256 JWT, typ approval-receipt+jwt, issued by Keelson")]


def tool_order(cfg: SidecarConfig) -> tuple[str, ...]:
    reads = tuple(n for n in READ_TOOLS if n != "list_transfer_orders" or cfg.views.transfer_orders is not None)
    return reads + (WRITE_TOOLS if cfg.writable else ())


def tiers_offered(cfg: SidecarConfig) -> list[str]:
    return ["A", "B", "C", "W"] if cfg.writable else ["A", "B", "C"]


def item_model(ops: list[str]) -> type[BaseModel]:
    """``add_change_set_items`` item: ``op`` is an enum of the configured ops."""
    op_type: Any = Literal.__getitem__(tuple(ops)) if ops else str
    return create_model(
        "ChangeSetItemIn",
        __config__=ConfigDict(extra="forbid"),
        op=(op_type, Field(description=" | ".join(ops) or "no ops configured")),
        target_ref=(str | None, Field(None, max_length=200, description="null for *.create ops, else the record code")),
        expected_version=(int | None, Field(None, ge=1, description="record version the change was planned against")),
        payload=(dict[str, Any], Field(default_factory=dict, description="op payload: integers, strings, ISO dates")),
    )


def _principal() -> Principal:
    token: AccessToken | None = get_access_token()
    if token is None:
        raise ToolError("unauthorized: a bearer token or adapter key is required")
    return Principal.of(token)


def _authorize(*any_of: str) -> Principal:
    principal = _principal()
    if not principal.has_any(*any_of):
        log.info("mcp tool call denied: insufficient scope",
                 extra={"client_id": principal.client_id, "required_any_of": list(any_of)})
        raise ToolError(f"insufficient_scope: this tool requires scope {' or '.join(any_of)}")
    return principal


async def _call(fn: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await fn()
    except Problem as exc:
        raise ToolError(exc.tool_text()) from exc


def _result(summary: str, data: dict[str, Any]) -> ToolResult:
    return ToolResult(content=summary, structured_content=data)


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def build_mcp(sidecar: Any, auth: AuthProvider | None) -> FastMCP:
    """``sidecar``: :class:`adapter_sidecar.service.Sidecar`."""
    cfg: SidecarConfig = sidecar.cfg
    name = cfg.system.name
    writable = cfg.writable
    mcp = FastMCP(
        name=f"{name} (Keelson adapter sidecar)",
        instructions=(
            f"{name} ({cfg.system.vendor}) behind a Keelson adapter sidecar. scn.* tools export canonical scn/1.0 "
            "snapshots and the change feed."
            + (" Changes are made only through change sets: create_change_set → add_change_set_items → "
               "validate_change_set (returns preview_hash) → apply_change_set with a Keelson approval receipt."
               if writable else " This system is read-only.")
        ),
        version="1.0.0",
        auth=auth,
        mask_error_details=True,
    )
    snaps = sidecar.snapshots
    svc = sidecar.changesets
    ItemIn = item_model(sorted(cfg.ops))  # noqa: N806 - a model class

    @mcp.tool(name="get_system_info", annotations=READ_ANN, output_schema=_schema(m.SystemInfoOut),
              description="What this system offers: backend, scn/1.0 entity coverage, supported change-set ops and "
                          "capability tiers. Requires scope: catalog:read or snapshot:read.")
    async def get_system_info() -> ToolResult:
        _authorize(*READ_SCOPES)
        data = {
            "system": name, "vendor": cfg.system.vendor, "backend": cfg.backend.kind,
            "consistency": cfg.consistency, "canonical_model": CANONICAL_MODEL,
            "entities": {e: ("mapped" if e in cfg.entities else "empty") for e in SCN_ENTITIES},
            "ops": sorted(cfg.ops) if writable else [], "tiers_offered": tiers_offered(cfg),
        }
        return _result(f"{name}: {len(cfg.entities)} mapped scn/1.0 entities, {len(data['ops'])} op(s).", data)

    @mcp.tool(name="scn.snapshot_open", annotations=READ_ANN, output_schema=_schema(m.SnapshotOpenOut),
              description="Open a canonical scn/1.0 snapshot (Parquet) of the given entities; returns snapshot_id, "
                          f"watermark, consistency and row counts. Entities: {', '.join(SCN_ENTITIES)}. "
                          "Requires scope: snapshot:read.")
    async def snapshot_open(
        entities: Annotated[list[str], Field(min_length=1, description="scn/1.0 entity names")],
    ) -> ToolResult:
        principal = _authorize(SCOPE_SNAPSHOT)
        data = await _call(lambda: snaps.open_snapshot(entities, created_by=principal.client_id))
        rows = sum(e["rows"] for e in data["entities"].values())
        return _result(f"Snapshot {data['snapshot_id']} ({rows} rows, watermark {data['watermark']}).", data)

    @mcp.tool(name="scn.snapshot_export", annotations=READ_ANN, output_schema=_schema(m.SnapshotExportOut),
              description="Signed download URLs (no auth header needed, expire with the snapshot) for one entity's "
                          "Parquet parts, with sha256/rows/bytes per part. Requires scope: snapshot:read.")
    async def snapshot_export(snapshot_id: str, entity: str) -> ToolResult:
        _authorize(SCOPE_SNAPSHOT)
        data = await _call(lambda: snaps.export_urls(snapshot_id, entity))
        links = [
            mcp_types.ResourceLink(type="resource_link", name=f"{entity}-part-{i:04d}.parquet", uri=p["uri"],
                                   mime_type="application/vnd.apache.parquet", size=p["bytes"])
            for i, p in enumerate(data["parts"], start=1)
        ]
        summary = mcp_types.TextContent(type="text", text=f"{len(data['parts'])} part(s), {data['rows']} row(s) of {entity}.")
        return ToolResult(content=[summary, *links], structured_content=data)

    @mcp.tool(name="scn.changes_since", annotations=READ_ANN, output_schema=_schema(m.ChangesOut),
              description="scn/1.0 entity changes after a watermark (from scn.snapshot_open or a previous call); "
                          "full_resync_required when the source changed in ways not listed. "
                          "Requires scope: snapshot:read.")
    async def changes_since(watermark: Annotated[str, Field(description="e.g. j:42")]) -> ToolResult:
        _authorize(SCOPE_SNAPSHOT)
        data = await _call(lambda: sidecar.changes_since(watermark))
        return _result(f"{len(data['changes'])} change(s); next_watermark {data['next_watermark']}"
                       + ("; full resync required." if data.get("full_resync_required") else "."), data)

    @mcp.tool(name="get_change_set", annotations=READ_ANN, output_schema=_schema(m.ChangeSetOut),
              description="Full change set: items with before/after, status, result references and attribution. "
                          "Requires scope: changes:preview or any read scope.")
    async def get_change_set(
        change_set_id: Annotated[str, Field(description="id (uuid) or code (CS-0001)")],
    ) -> ToolResult:
        _authorize(SCOPE_PREVIEW, *READ_SCOPES)
        data = await _call(lambda: svc.get(change_set_id))
        return _result(f"{data['code']}: {data['status']}, {data['items_count']} item(s).", data)

    @mcp.tool(name="get_audit_trail", annotations=READ_ANN, output_schema=_schema(m.AuditOut),
              description="Append-only audit rows (who changed which record, via which client and approval), newest "
                          "first. Requires scope: catalog:read.")
    async def get_audit_trail(
        change_set_code: str | None = None,
        entity_type: str | None = None,
        entity_ref: str | None = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> ToolResult:
        _authorize(SCOPE_CATALOG)
        data = await _call(lambda: svc.audit(change_set_code=change_set_code, entity_type=entity_type,
                                             entity_ref=entity_ref, limit=limit))
        return _result(f"{len(data['items'])} audit row(s).", data)

    @mcp.tool(name="get_operator_guide", annotations=READ_ANN, output_schema=_schema(m.GuideOut),
              description=f"Manual step-by-step instructions to carry out a change set by hand in {name}. "
                          "Requires scope: catalog:read.")
    async def get_operator_guide(change_set_id: str, locale: str = "en") -> ToolResult:
        _authorize(SCOPE_CATALOG)
        data = await _call(lambda: svc.operator_guide(change_set_id, locale))
        return _result(f"{len(data['steps'])} manual step(s) for {data['code']}.", data)

    if cfg.views.transfer_orders is not None:
        @mcp.tool(name="list_transfer_orders", annotations=READ_ANN, output_schema=_schema(m.TransferOrdersOut),
                  description="Transfer orders between warehouses, with lines and created_via. "
                              "Requires scope: orders:read.")
        async def list_transfer_orders(status: str | None = None, from_dc: str | None = None,
                                       to_dc: str | None = None) -> ToolResult:
            _authorize(SCOPE_ORDERS)
            data = await _call(lambda: svc.transfer_orders(status=status, from_dc=from_dc, to_dc=to_dc))
            return _result(f"{len(data['items'])} transfer order(s).", data)

    if not writable:
        return mcp

    @mcp.tool(name="create_change_set", annotations=WRITE_ANN, output_schema=_schema(m.CreateOut),
              description="Create an empty draft change set. Requires scope: changes:preview.")
    async def create_change_set(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        idempotency_key: IdemKey,
        origin: m.OriginIn | None = None,
    ) -> ToolResult:
        principal = _authorize(SCOPE_PREVIEW)
        data = await _call(lambda: svc.create(principal, title=title,
                                              origin=origin.model_dump(exclude_none=True) if origin else {},
                                              idempotency_key=idempotency_key))
        return _result(f"Created draft change set {data['code']}.", data)

    @mcp.tool(name="add_change_set_items", annotations=WRITE_ANN, output_schema=_schema(m.AddItemsOut),
              description="Append items to a draft/validated change set (resets it to draft). "
                          "Requires scope: changes:preview.")
    async def add_change_set_items(
        change_set_id: str,
        items: Annotated[list[ItemIn], Field(min_length=1, max_length=500)],  # type: ignore[valid-type]
        idempotency_key: IdemKey,
    ) -> ToolResult:
        principal = _authorize(SCOPE_PREVIEW)
        raw = [it.model_dump() for it in items]
        data = await _call(lambda: svc.add_items(principal, change_set_id, items=raw, idempotency_key=idempotency_key))
        return _result(f"Change set now has {data['items_count']} item(s).", data)

    @mcp.tool(name="validate_change_set", annotations=WRITE_ANN, output_schema=_schema(m.ValidateOut),
              description="Validate items against current data (all-or-nothing preview); returns before/after per "
                          "item, totals and preview_hash. Requires scope: changes:preview.")
    async def validate_change_set(change_set_id: str, idempotency_key: IdemKey) -> ToolResult:
        principal = _authorize(SCOPE_PREVIEW)
        data = await _call(lambda: svc.validate(principal, change_set_id, idempotency_key=idempotency_key))
        bad = sum(1 for it in data["items"] if not it["ok"])
        return _result(f"{data['status']}: {data['totals']['lines']} line(s), {data['totals']['units_moved']} units"
                       + (f", {bad} item(s) with errors" if bad else "") + f"; preview_hash {data['preview_hash']}.",
                       data)

    @mcp.tool(name="apply_change_set", annotations=APPLY_ANN, output_schema=_schema(m.ApplyOut),
              description="Apply a validated change set atomically. Requires preview_hash and a Keelson approval "
                          "receipt. Requires scope: changes:apply or changes:apply:auto.")
    async def apply_change_set(
        change_set_id: str,
        preview_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        approval_ref: Annotated[str, Field(min_length=1, max_length=200)],
        approval_receipt: Receipt,
        idempotency_key: IdemKey,
    ) -> ToolResult:
        principal = _authorize(SCOPE_APPLY, SCOPE_APPLY_AUTO)
        data = await _call(lambda: svc.apply(principal, change_set_id, preview_hash=preview_hash,
                                             approval_ref=approval_ref, approval_receipt=approval_receipt,
                                             idempotency_key=idempotency_key))
        if data["status"] == "applied":
            return _result(f"Applied {data['applied']} item(s) as {data['change_set_code']}.", data)
        return _result(f"Conflict: {len(data['conflicts'])} item(s) changed since validation; nothing applied.", data)

    @mcp.tool(name="rollback_change_set", annotations=APPLY_ANN, output_schema=_schema(m.RollbackOut),
              description="Roll back an applied change set: a compensating change set is applied atomically "
                          "(conflicts if the records changed since). The approval receipt binds to the original "
                          "change set and its preview_hash. Requires scope: changes:apply.")
    async def rollback_change_set(
        change_set_id: str,
        approval_ref: Annotated[str, Field(min_length=1, max_length=200)],
        approval_receipt: Receipt,
        idempotency_key: IdemKey,
    ) -> ToolResult:
        principal = _authorize(SCOPE_APPLY)
        data = await _call(lambda: svc.rollback(principal, change_set_id, approval_ref=approval_ref,
                                                approval_receipt=approval_receipt, idempotency_key=idempotency_key))
        if data["status"] == "rolled_back":
            return _result(f"Rolled back {data['change_set_code']} with {data['compensating_code']} "
                           f"({data['applied']} change(s)).", data)
        return _result(f"Conflict: {len(data['conflicts'])} record(s) changed since; nothing rolled back.", data)

    return mcp
