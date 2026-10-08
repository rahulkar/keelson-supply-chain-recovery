"""``/mcp-lite`` — the "foreign ERP" Tier-A profile (docs/contracts/22-merxa-m2.md §6).

Deliberately plain and un-curated: generic CRUD tools over a handful of tables, no change
sets, no snapshots, no operator guide, sparse annotations (``readOnlyHint`` on reads only;
the write tools carry none). Auth: ``X-Merxa-Api-Key: mx_…`` **or** ``Authorization:
Bearer <token>`` where the token is a Merxa API key or a merxa-realm JWT whose ``aud``
contains ``MCP_RESOURCE`` (the same audience as ``/mcp``).

Scopes: reads need the type's read scope; writes need ``changes:apply`` (bearer tokens
additionally need the ``ops-director`` role, as everywhere in Merxa; an API key's scopes
are its grant).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from pydantic import Field
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.types import ASGIApp, Receive, Scope, Send

from merxa_api import tables as t
from merxa_api.errors import Problem
from merxa_api.logs import get_logger
from merxa_api.security.principal import ROLE_OPS_DIRECTOR, SCOPE_APPLY, Principal
from merxa_api.services import audit, events, idempotency
from merxa_api.services.apikeys import ApiKeyInfo, looks_like_key
from merxa_api.services.audit import Attribution
from merxa_api.services.catalog import sim_today
from merxa_api.services.common import jsonable, row_dict
from merxa_api.services.ops import ApplyContext, ItemSpec, Simulation, TransferOrderCreate
from merxa_api.state import AppState

log = get_logger(__name__)

LITE_TOOL_ORDER: tuple[str, ...] = ("list_items", "get_item", "update_item", "create_transfer_order")
API_KEY_HEADER = b"x-merxa-api-key"
VIA = "MCP-lite"


class ApiKeyAccessToken(AccessToken):
    """Access token minted for a valid Merxa API key (never for a JWT)."""

    api_key_id: str
    api_key_name: str
    api_key_prefix: str


class LiteTokenVerifier(TokenVerifier):
    """Accepts Merxa API keys (``mx_…``) and merxa-realm JWTs (delegated to the shared verifier)."""

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state

    async def verify_token(self, token: str) -> AccessToken | None:
        if looks_like_key(token):
            info = await self.state.apikeys.verify(token)
            if info is None:
                return None
            return ApiKeyAccessToken(
                token=token,
                client_id=f"apikey:{info.prefix}",
                scopes=list(info.scopes),
                expires_at=None,
                claims={"sub": f"apikey:{info.id}", "azp": f"apikey:{info.prefix}"},
                api_key_id=str(info.id),
                api_key_name=info.name,
                api_key_prefix=info.prefix,
            )
        return await self.state.token_verifier.verify_token(token)


class ApiKeyHeaderMiddleware:
    """Maps ``X-Merxa-Api-Key`` to ``Authorization: Bearer`` (an explicit Authorization wins)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = list(scope.get("headers", []))
            names = {k.lower() for k, _ in headers}
            key = next((v for k, v in headers if k.lower() == API_KEY_HEADER), None)
            if key is not None and b"authorization" not in names:
                headers.append((b"authorization", b"Bearer " + key.strip()))
                scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


# ───────────────────────── type registry ─────────────────────────
@dataclass(frozen=True)
class LiteType:
    table: Any
    scope: str
    columns: tuple[str, ...]
    key_cols: tuple[str, ...]
    key_sep: str = ""
    updatable: frozenset[str] = frozenset()
    entity_type: str = ""
    order: tuple[str, ...] = ()

    def key_of(self, row: Any) -> str:
        return self.key_sep.join(str(getattr(row, c)) for c in self.key_cols)


def _types() -> dict[str, LiteType]:
    return {
        "sku": LiteType(t.sku, "catalog:read", ("code", "style_code", "size", "size_seq", "version"), ("code",)),
        "style": LiteType(
            t.style,
            "catalog:read",
            ("code", "name", "family", "season", "colour", "retail_price_cents", "unit_cost_cents", "version"),
            ("code",),
            updatable=frozenset({"name", "retail_price_cents"}),
            entity_type="style",
        ),
        "store": LiteType(
            t.store,
            "catalog:read",
            ("code", "name", "market_code", "city", "grade", "cluster_code", "home_dc_code", "version"),
            ("code",),
            updatable=frozenset({"name"}),
            entity_type="store",
        ),
        "dc": LiteType(
            t.dc,
            "catalog:read",
            ("code", "name", "country", "city", "inbound_cap_units_wk", "outbound_cap_units_wk", "version"),
            ("code",),
            updatable=frozenset({"inbound_cap_units_wk", "outbound_cap_units_wk"}),
            entity_type="dc",
        ),
        "purchase_order": LiteType(
            t.purchase_order,
            "po:read",
            ("code", "supplier_code", "factory_code", "dest_dc_code", "status", "ex_factory_date", "eta_dc", "version"),
            ("code",),
            updatable=frozenset({"status", "eta_dc", "ex_factory_date"}),
            entity_type="purchase_order",
        ),
        "po_line": LiteType(
            t.po_line,
            "po:read",
            ("po_code", "line_no", "sku_code", "qty_ordered", "qty_received", "eta_dc", "status", "version"),
            ("po_code", "line_no"),
            "#",
            updatable=frozenset({"qty_ordered", "eta_dc", "status"}),
            entity_type="po_line",
        ),
        "inventory": LiteType(
            t.inventory_position,
            "inventory:read",
            ("location_type", "location_code", "sku_code", "on_hand", "allocated", "reserved", "in_transit", "version"),
            ("location_type", "location_code", "sku_code"),
            "/",
            updatable=frozenset({"on_hand"}),
            entity_type="inventory_position",
        ),
        "forecast": LiteType(
            t.demand_forecast,
            "inventory:read",
            ("style_code", "store_code", "cluster_code", "iso_week", "units", "version"),
            ("style_code", "store_code", "iso_week"),
            "|",
            updatable=frozenset({"units"}),
            entity_type="demand_forecast",
        ),
        "allocation": LiteType(
            t.allocation,
            "orders:read",
            ("code", "sku_code", "dc_code", "store_code", "ship_week", "qty", "status", "version"),
            ("code",),
            updatable=frozenset({"qty", "status"}),
            entity_type="allocation",
        ),
        "transfer_order": LiteType(
            t.transfer_order,
            "orders:read",
            ("code", "from_dc", "to_dc", "ship_date", "eta_date", "status", "created_via", "version"),
            ("code",),
            updatable=frozenset({"status", "ship_date", "eta_date"}),
            entity_type="transfer_order",
        ),
    }


TYPES = _types()
TYPE_NAMES = tuple(TYPES)


def _lt(type_: str) -> LiteType:
    lt = TYPES.get(type_)
    if lt is None:
        raise ToolError(f"unknown type {type_!r}; one of {list(TYPE_NAMES)}")
    return lt


def _key_cond(lt: LiteType, key: str) -> Any:
    parts = key.split(lt.key_sep, len(lt.key_cols) - 1) if lt.key_sep else [key]
    if len(parts) != len(lt.key_cols) or not all(parts):
        raise ToolError(f"key must look like {lt.key_sep.join('<' + c + '>' for c in lt.key_cols)}")
    conds = []
    for col, val in zip(lt.key_cols, parts, strict=True):
        c = lt.table.c[col]
        if col == "line_no":
            if not val.isdigit():
                raise ToolError("line_no must be a number")
            conds.append(c == int(val))
        else:
            conds.append(c == val)
    return conds


def _coerce(lt: LiteType, col: str, value: Any) -> Any:
    ctype = lt.table.c[col].type.python_type
    if ctype is date:
        if not isinstance(value, str):
            raise ToolError(f"{col} must be YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ToolError(f"{col} must be YYYY-MM-DD") from exc
    if ctype is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{col} must be an integer")
    elif ctype is str and not isinstance(value, str):
        raise ToolError(f"{col} must be a string")
    return value


def _json_arg(raw: str | None, name: str, kind: type) -> Any:
    if raw is None or raw == "":
        return kind()
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ToolError(f"{name} is not valid JSON") from exc
    if not isinstance(value, kind):
        raise ToolError(f"{name} must be a JSON {kind.__name__}")
    return value


def _principal(state: AppState) -> Principal:
    token = get_access_token()
    if token is None:
        raise ToolError("unauthorized")
    if isinstance(token, ApiKeyAccessToken):
        principal = ApiKeyInfo(
            id=uuid.UUID(token.api_key_id),
            name=token.api_key_name,
            prefix=token.api_key_prefix,
            scopes=tuple(token.scopes),
        ).principal()
    else:
        principal = Principal.from_claims(token.claims)
    state.clients_seen.touch_soon(principal, "mcp-lite")
    return principal


def _require(principal: Principal, scope: str) -> None:
    if not principal.has_scope(scope):
        raise ToolError(f"insufficient_scope: requires scope {scope}")


def _require_write(principal: Principal) -> None:
    _require(principal, SCOPE_APPLY)
    if principal.kind != "api_key" and not principal.has_role(ROLE_OPS_DIRECTOR):
        raise ToolError(f"forbidden: requires role {ROLE_OPS_DIRECTOR}")


async def _idempotent(
    state: AppState,
    principal: Principal,
    request_id: str | None,
    op: str,
    body: Any,
    run: Callable[[], Awaitable[dict]],
) -> dict[str, Any]:
    if not request_id:
        return await run()
    try:
        claim = await idempotency.begin(
            state.engine,
            principal=principal.sub,
            key=f"lite:{request_id}",
            request_sha256=idempotency.request_fingerprint(op, body),
        )
    except Problem as exc:
        raise ToolError(f"{exc.code}: {exc.detail}") from exc
    if isinstance(claim, idempotency.Replay):
        return {**claim.response, "replayed": True}
    try:
        result = await run()
    except BaseException:
        await idempotency.abandon(state.engine, claim)
        raise
    async with state.engine.begin() as conn:
        await idempotency.complete(conn, claim, 200, result)
    return result


def build_lite(state: AppState) -> FastMCP:
    mcp = FastMCP(
        name="Merxa Retail Suite (lite)",
        instructions="Merxa data access. List, get and update records by type.",
        version="1.0.0",
        auth=LiteTokenVerifier(state),
        mask_error_details=True,
    )
    engine = state.engine
    read_ann = mcp_types.ToolAnnotations(read_only_hint=True)

    @mcp.tool(name="list_items", annotations=read_ann, description="List records of a type.")
    async def list_items(
        type: Annotated[str, Field(description=f"one of {', '.join(TYPE_NAMES)}")],
        filter_json: Annotated[
            str | None, Field(description='JSON object of column filters, e.g. {"status":"open"}')
        ] = None,
        page: Annotated[int, Field(ge=1, le=1_000_000)] = 1,
        page_size: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        principal = _principal(state)
        lt = _lt(type)
        _require(principal, lt.scope)
        filters = _json_arg(filter_json, "filter_json", dict)
        q = select(*[lt.table.c[c] for c in lt.columns])
        cq = select(func.count()).select_from(lt.table)
        for col, value in filters.items():
            if col not in lt.columns or col == "version":
                raise ToolError(f"cannot filter on {col!r}; columns: {list(lt.columns)}")
            if isinstance(value, (dict, list)):
                raise ToolError(f"filter {col} must be a scalar")
            if value is None:
                cond = lt.table.c[col].is_(None)
            elif isinstance(value, bool):
                cond = cast(lt.table.c[col], Text) == ("true" if value else "false")
            else:
                cond = cast(lt.table.c[col], Text) == (value if isinstance(value, str) else str(value))
            q, cq = q.where(cond), cq.where(cond)
        q = q.order_by(*[lt.table.c[c] for c in lt.key_cols]).offset((page - 1) * page_size).limit(page_size)
        async with engine.connect() as conn:
            total = int((await conn.execute(cq)).scalar_one())
            rows = list(await conn.execute(q))
        items = [{"key": lt.key_of(r), **row_dict(r)} for r in rows]
        return {"type": type, "page": page, "page_size": page_size, "total": total, "items": items}

    @mcp.tool(name="get_item", annotations=read_ann, description="Get one record by type and key.")
    async def get_item(type: str, key: str) -> dict[str, Any]:
        principal = _principal(state)
        lt = _lt(type)
        _require(principal, lt.scope)
        async with engine.connect() as conn:
            row = (await conn.execute(select(*[lt.table.c[c] for c in lt.columns]).where(*_key_cond(lt, key)))).first()
        if row is None:
            raise ToolError(f"not_found: {type} {key!r}")
        return {"type": type, "key": key, "item": row_dict(row)}

    @mcp.tool(name="update_item", description="Update fields of one record (optimistic concurrency).")
    async def update_item(
        type: str,
        key: str,
        fields_json: Annotated[str, Field(description='JSON object of field values, e.g. {"qty": 12}')],
        expected_version: Annotated[int, Field(ge=1)],
        request_id: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict[str, Any]:
        principal = _principal(state)
        lt = _lt(type)
        _require_write(principal)
        fields = _json_arg(fields_json, "fields_json", dict)
        if not fields:
            raise ToolError("fields_json must name at least one field")
        bad = sorted(set(fields) - lt.updatable)
        if bad:
            raise ToolError(f"fields {bad} are not updatable on {type}; updatable: {sorted(lt.updatable)}")
        values = {k: _coerce(lt, k, v) for k, v in fields.items()}

        async def run() -> dict[str, Any]:
            async with engine.begin() as conn:
                row = (await conn.execute(select(lt.table).where(*_key_cond(lt, key)).with_for_update())).first()
                if row is None:
                    raise ToolError(f"not_found: {type} {key!r}")
                if row.version != expected_version:
                    raise ToolError(f"version_conflict: expected {expected_version}, current {row.version}")
                try:
                    async with conn.begin_nested():
                        new = (
                            await conn.execute(
                                update(lt.table)
                                .where(*_key_cond(lt, key))
                                .values(**values)
                                .returning(*[lt.table.c[c] for c in lt.columns])
                            )
                        ).one()
                except (IntegrityError, DBAPIError) as exc:
                    raise ToolError(f"invalid_value: the update violates a constraint of {type}") from exc
                before = {k: jsonable(getattr(row, k)) for k in values}
                after = {k: jsonable(getattr(new, k)) for k in values}
                entry = (
                    lt.entity_type or type,
                    key,
                    "update",
                    {**before, "version": row.version},
                    {**after, "version": new.version},
                )
                await audit.write_many(conn, _attribution(principal), [entry])
                changes = await events.changes_for(conn, [entry])
                if type == "inventory":
                    await events.emit(
                        conn,
                        "inventory.adjusted",
                        {"location": key, "before": before, "after": after, "via": VIA},
                        changes,
                    )
                elif type == "po_line" and "eta_dc" in values:
                    await events.emit(
                        conn,
                        "po_line.eta_changed",
                        {
                            "lines": [{"ref": key, "eta_dc_before": before["eta_dc"], "eta_dc": after["eta_dc"]}],
                            "via": VIA,
                        },
                        changes,
                    )
                else:
                    await events.emit(
                        conn,
                        "entity.updated",
                        {"type": type, "key": key, "fields": sorted(values), "via": VIA},
                        changes,
                    )
            return {"type": type, "key": key, "item": row_dict(new)}

        return await _idempotent(
            state,
            principal,
            request_id,
            "lite.update_item",
            {"type": type, "key": key, "fields": fields, "expected_version": expected_version},
            run,
        )

    @mcp.tool(name="create_transfer_order", description="Create a transfer order between two DCs.")
    async def create_transfer_order(
        from_dc: str,
        to_dc: str,
        ship_date: Annotated[str, Field(description="YYYY-MM-DD")],
        lines_json: Annotated[str, Field(description='JSON array, e.g. [{"sku":"PUF-W-BLK-M","qty":10}]')],
        request_id: Annotated[str | None, Field(max_length=200)] = None,
    ) -> dict[str, Any]:
        principal = _principal(state)
        _require_write(principal)
        lines = _json_arg(lines_json, "lines_json", list)
        spec = ItemSpec(
            1,
            "transfer_order.create",
            None,
            None,
            {
                "from_dc": from_dc,
                "to_dc": to_dc,
                "ship_date": ship_date,
                "lines": lines,
            },
        )
        handler = TransferOrderCreate()

        async def run() -> dict[str, Any]:
            async with engine.begin() as conn:
                today = await sim_today(conn)
                preview = await handler.preview(conn, spec, Simulation(today=today))
                if not preview.ok:
                    raise ToolError("invalid: " + "; ".join(preview.errors))
                info = await handler.check(conn, spec, Simulation(today=today))
                if info is not None:
                    raise ToolError(f"conflict: {info.reason} ({info.target_ref})")
                ctx = ApplyContext(change_set_code="", today=today, via="mcp-lite")
                entries = await handler.apply(conn, spec, ctx)
                await audit.write_many(conn, _attribution(principal), entries)
                await events.emit(
                    conn,
                    "entity.updated",
                    {"type": "transfer_order", "key": ctx.results[1]["to_code"], "via": VIA},
                    await events.changes_for(conn, entries),
                )
            return {"code": ctx.results[1]["to_code"], "status": "planned", "transfer_order": jsonable(preview.after)}

        try:
            return await _idempotent(
                state,
                principal,
                request_id,
                "lite.create_transfer_order",
                {"from_dc": from_dc, "to_dc": to_dc, "ship_date": ship_date, "lines": lines},
                run,
            )
        except Problem as exc:
            raise ToolError(f"{exc.code}: {exc.detail}") from exc

    return mcp


def _attribution(principal: Principal) -> Attribution:
    return Attribution(
        actor_sub=principal.sub,
        actor_username=principal.username,
        actor_display=principal.actor_display,
        client_id=principal.client_id,
        via=VIA,
    )
