"""M2 REST endpoints: logistics, production, online orders, promotions, events, scn
snapshots (+ signed downloads), API keys, webhook subscriptions, connected OAuth clients."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from merxa_api import tables as t
from merxa_api.api.deps import (
    IdempotencyKey,
    IfMatch,
    StateDep,
    etag,
    parse_if_match,
    require_admin,
    require_scope,
)
from merxa_api.security.principal import Principal
from merxa_api.services import events, idempotency, supply
from merxa_api.services.common import clamp_limit, paginate, row_dict

router = APIRouter(prefix="/v1", tags=["m2"])

Limit = Annotated[int | None, Query(ge=1, le=1000, description="page size (default 50, max 1000)")]
Cursor = Annotated[str | None, Query(description="opaque cursor from a previous page's next_cursor")]
PoRead = Annotated[Principal, Depends(require_scope("po:read"))]
OrdersRead = Annotated[Principal, Depends(require_scope("orders:read"))]
CatalogRead = Annotated[Principal, Depends(require_scope("catalog:read"))]
InventoryRead = Annotated[Principal, Depends(require_scope("inventory:read"))]
SnapshotRead = Annotated[Principal, Depends(require_scope("snapshot:read"))]
Admin = Annotated[Principal, Depends(require_admin)]


# ───────────────────────── logistics ─────────────────────────
@router.get("/ports")
async def ports(state: StateDep, _: PoRead, limit: Limit = None, cursor: Cursor = None) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_ports(conn, limit=clamp_limit(limit), cursor=cursor)


@router.get("/lanes")
async def lanes(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    origin: str | None = None,
    dest: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_lanes(
            conn, limit=clamp_limit(limit), cursor=cursor, origin=origin, dest=dest, mode=mode
        )


@router.get("/lane-options")
async def lane_options(state: StateDep, _: PoRead, origin: str, dest: str, week: str | None = None) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.lane_options(conn, origin=origin, dest=dest, week=week)


@router.get("/rate-cards")
async def rate_cards(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    lane_code: str | None = None,
    valid_on: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_rate_cards(
            conn, limit=clamp_limit(limit), cursor=cursor, lane_code=lane_code, valid_on=valid_on
        )


@router.get("/sailings")
async def sailings(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    lane_code: str | None = None,
    status: str | None = None,
    etd_from: str | None = None,
    etd_to: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_sailings(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            lane_code=lane_code,
            status=status,
            etd_from=etd_from,
            etd_to=etd_to,
        )


@router.get("/capacity-bookings")
async def capacity_bookings(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    lane_code: str | None = None,
    iso_week: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_capacity_bookings(
            conn, limit=clamp_limit(limit), cursor=cursor, lane_code=lane_code, iso_week=iso_week, status=status
        )


@router.get("/shipments")
async def shipments(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    po_code: str | None = None,
    status: str | None = None,
    dest_dc: str | None = None,
    sailing_code: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_shipments(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            po_code=po_code,
            status=status,
            dest_dc=dest_dc,
            sailing_code=sailing_code,
        )


# ───────────────────────── production ─────────────────────────
@router.get("/factory-lines")
async def factory_lines(
    state: StateDep, _: PoRead, limit: Limit = None, cursor: Cursor = None, factory_code: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_factory_lines(conn, limit=clamp_limit(limit), cursor=cursor, factory_code=factory_code)


@router.get("/factories/{code}/schedule")
async def factory_schedule(
    code: str,
    state: StateDep,
    _: PoRead,
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.factory_schedule(conn, factory_code=code, date_from=date_from, date_to=date_to)


@router.get("/production-orders")
async def production_orders(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    line_code: str | None = None,
    factory_code: str | None = None,
    status: str | None = None,
    po_code: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_production_orders(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            line_code=line_code,
            factory_code=factory_code,
            status=status,
            po_code=po_code,
        )


# ───────────────────────── demand side ─────────────────────────
@router.get("/online-orders")
async def online_orders(
    state: StateDep,
    _: OrdersRead,
    limit: Limit = None,
    cursor: Cursor = None,
    market: str | None = None,
    status: str | None = None,
    promise_before: str | None = None,
    at_risk: bool | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_online_orders(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            market=market,
            status=status,
            promise_before=promise_before,
            at_risk=at_risk,
        )


@router.get("/promotions")
async def promotions(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, active_on: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await supply.list_promotions(conn, limit=clamp_limit(limit), cursor=cursor, active_on=active_on)


@router.get("/sales-history")
async def sales_history(
    state: StateDep,
    _: InventoryRead,
    limit: Limit = None,
    cursor: Cursor = None,
    style_code: str | None = None,
    store_code: str | None = None,
) -> dict[str, Any]:
    sh = t.sales_history
    q = select(sh.c.style_code, sh.c.store_code, sh.c.iso_week, sh.c.units)
    if style_code:
        q = q.where(sh.c.style_code == style_code)
    if store_code:
        q = q.where(sh.c.store_code == store_code)
    async with state.engine.connect() as conn:
        rows, nxt = await paginate(
            conn, q, [sh.c.style_code, sh.c.store_code, sh.c.iso_week], limit=clamp_limit(limit), cursor=cursor
        )
    return {"items": [row_dict(r) for r in rows], "next_cursor": nxt}


# ───────────────────────── events / scn ─────────────────────────
@router.get("/events")
async def list_events(
    state: StateDep,
    _: PoRead,
    after_cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    type: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await events.list_events(conn, after_cursor=after_cursor, limit=limit, types=type)


class SnapshotOpen(BaseModel):
    entities: list[str] = Field(min_length=1)


@router.post("/snapshots", status_code=201)
async def snapshot_open(body: SnapshotOpen, state: StateDep, principal: SnapshotRead) -> dict[str, Any]:
    return await state.snapshots.open_snapshot(body.entities, created_by=principal.client_id or principal.sub)


@router.get("/snapshots/{snapshot_id}/{entity}")
async def snapshot_export(snapshot_id: str, entity: str, state: StateDep, _: SnapshotRead) -> dict[str, Any]:
    return await state.snapshots.export_urls(snapshot_id, entity)


@router.get("/snapshots/{snapshot_id}/{entity}/{file}", include_in_schema=True)
async def snapshot_download(
    snapshot_id: str, entity: str, file: str, state: StateDep, exp: str | None = None, sig: str | None = None
) -> FileResponse:
    """Signed download (no auth header): ``?exp=<unix>&sig=<hmac>`` from ``scn.snapshot_export``."""
    path = state.snapshots.resolve_download(snapshot_id, entity, file, exp, sig)
    return FileResponse(
        path,
        media_type="application/vnd.apache.parquet",
        filename=f"{entity}-{file}",
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.get("/changes")
async def changes_since(state: StateDep, _: SnapshotRead, watermark: str | None = None) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await events.changes_since(conn, watermark)


# ───────────────────────── API keys ─────────────────────────
class MintKey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)


@router.get("/api-keys")
async def api_keys(state: StateDep, _: Admin) -> dict[str, Any]:
    return await state.apikeys.list()


@router.post("/api-keys", status_code=201)
async def mint_api_key(
    body: MintKey, state: StateDep, principal: Admin, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    """Mint a key. The key is in this response only; a replay (same Idempotency-Key) returns
    the record without it (``key: null``) — only its sha256 is stored."""
    claim = await idempotency.begin(
        state.engine,
        principal=principal.sub,
        key=idempotency.validate_key(idempotency_key),
        request_sha256=idempotency.request_fingerprint("api_key.mint", body.model_dump()),
    )
    if isinstance(claim, idempotency.Replay):
        return JSONResponse(claim.response, status_code=claim.status_code, headers={"Idempotent-Replayed": "true"})
    try:
        minted = await state.apikeys.mint(principal, name=body.name, scopes=body.scopes)
    except BaseException:
        await idempotency.abandon(state.engine, claim)
        raise
    async with state.engine.begin() as conn:
        await idempotency.complete(conn, claim, 201, {**minted, "key": None, "key_shown_once": False})
    return JSONResponse(minted, status_code=201, headers={"Cache-Control": "no-store"})


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, state: StateDep, principal: Admin) -> dict[str, Any]:
    return await state.apikeys.revoke(principal, key_id)


@router.post("/api-keys/{key_id}:revoke")
async def revoke_api_key_post(key_id: str, state: StateDep, principal: Admin) -> dict[str, Any]:
    return await state.apikeys.revoke(principal, key_id)


@router.get("/oauth-clients")
async def oauth_clients(state: StateDep, _: Admin) -> dict[str, Any]:
    """OAuth clients (``azp``) seen on /mcp, /mcp-lite and REST, with last-seen time."""
    return await state.clients_seen.list()


# ───────────────────────── webhooks ─────────────────────────
class CreateSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    events: list[str]


class PatchSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = None
    events: list[str] | None = None
    active: bool | None = None


@router.get("/webhook-subscriptions")
async def webhook_subscriptions(state: StateDep, _: Admin) -> dict[str, Any]:
    return await state.webhooks.list_subscriptions()


@router.post("/webhook-subscriptions", status_code=201)
async def create_webhook_subscription(
    body: CreateSubscription, state: StateDep, principal: Admin, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    """Create a subscription; its ``whsec_…`` secret is in this response only."""
    claim = await idempotency.begin(
        state.engine,
        principal=principal.sub,
        key=idempotency.validate_key(idempotency_key),
        request_sha256=idempotency.request_fingerprint("webhook.create", body.model_dump()),
    )
    if isinstance(claim, idempotency.Replay):
        return JSONResponse(claim.response, status_code=claim.status_code, headers={"Idempotent-Replayed": "true"})
    try:
        created = await state.webhooks.create_subscription(principal, name=body.name, url=body.url, events=body.events)
    except BaseException:
        await idempotency.abandon(state.engine, claim)
        raise
    async with state.engine.begin() as conn:
        await idempotency.complete(conn, claim, 201, {**created, "secret": None, "secret_shown_once": False})
    return JSONResponse(created, status_code=201, headers={"Cache-Control": "no-store"})


@router.patch("/webhook-subscriptions/{sub_id}")
async def patch_webhook_subscription(
    sub_id: str,
    body: PatchSubscription,
    state: StateDep,
    principal: Admin,
    response: Response,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    expected = parse_if_match(if_match)
    updated = await state.webhooks.update_subscription(principal, sub_id, expected, body.model_dump(exclude_none=True))
    response.headers["ETag"] = etag(updated["version"])
    return updated


@router.delete("/webhook-subscriptions/{sub_id}", status_code=204)
async def delete_webhook_subscription(sub_id: str, state: StateDep, principal: Admin) -> Response:
    await state.webhooks.delete_subscription(principal, sub_id)
    return Response(status_code=204)


@router.get("/webhook-subscriptions/{sub_id}/deliveries")
async def subscription_deliveries(
    sub_id: str, state: StateDep, _: Admin, status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return await state.webhooks.list_deliveries(subscription_id=sub_id, status=status, limit=limit)


@router.get("/webhook-deliveries")
async def webhook_deliveries(
    state: StateDep, _: Admin, status: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    return await state.webhooks.list_deliveries(status=status, limit=limit)
