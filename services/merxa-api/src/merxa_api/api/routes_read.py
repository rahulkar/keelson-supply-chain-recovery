"""Read endpoints under ``/v1`` (cursor pagination ``?limit=&cursor=`` → ``{items, next_cursor}``)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from merxa_api.api.deps import StateDep, etag, require_scope
from merxa_api.security.principal import Principal
from merxa_api.services import catalog
from merxa_api.services.common import clamp_limit

router = APIRouter(prefix="/v1", tags=["read"])

Limit = Annotated[int | None, Query(ge=1, le=1000, description="page size (default 50, max 1000)")]
Cursor = Annotated[str | None, Query(description="opaque cursor from a previous page's next_cursor")]

CatalogRead = Annotated[Principal, Depends(require_scope("catalog:read"))]
PoRead = Annotated[Principal, Depends(require_scope("po:read"))]
InventoryRead = Annotated[Principal, Depends(require_scope("inventory:read"))]
OrdersRead = Annotated[Principal, Depends(require_scope("orders:read"))]


@router.get("/dcs")
async def dcs(state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_dcs(conn, limit=clamp_limit(limit), cursor=cursor)


@router.get("/clusters")
async def clusters(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, dc_code: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_clusters(conn, limit=clamp_limit(limit), cursor=cursor, dc_code=dc_code)


@router.get("/stores")
async def stores(
    state: StateDep,
    _: CatalogRead,
    limit: Limit = None,
    cursor: Cursor = None,
    cluster_code: str | None = None,
    dc_code: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_stores(
            conn, limit=clamp_limit(limit), cursor=cursor, cluster_code=cluster_code, dc_code=dc_code, market=market
        )


@router.get("/suppliers")
async def suppliers(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, kind: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_suppliers(conn, limit=clamp_limit(limit), cursor=cursor, kind=kind)


@router.get("/factories")
async def factories(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, supplier_code: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_factories(conn, limit=clamp_limit(limit), cursor=cursor, supplier_code=supplier_code)


@router.get("/styles")
async def styles(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, family: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_styles(conn, limit=clamp_limit(limit), cursor=cursor, family=family)


@router.get("/skus")
async def skus(
    state: StateDep, _: CatalogRead, limit: Limit = None, cursor: Cursor = None, style_code: str | None = None
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_skus(conn, limit=clamp_limit(limit), cursor=cursor, style_code=style_code)


@router.get("/purchase-orders")
async def purchase_orders(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    status: str | None = None,
    supplier_code: str | None = None,
    factory_code: str | None = None,
    dest_dc: str | None = None,
    eta_from: str | None = None,
    eta_to: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_purchase_orders(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            status=status,
            supplier_code=supplier_code,
            factory_code=factory_code,
            dest_dc=dest_dc,
            eta_from=eta_from,
            eta_to=eta_to,
        )


@router.get("/purchase-orders/{code}")
async def purchase_order(code: str, state: StateDep, _: PoRead, response: Response) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        po = await catalog.get_purchase_order(conn, code)
    response.headers["ETag"] = etag(po["version"])
    return po


@router.get("/inventory-positions")
async def inventory_positions(
    state: StateDep,
    _: InventoryRead,
    limit: Limit = None,
    cursor: Cursor = None,
    location_type: str | None = None,
    location_code: Annotated[list[str] | None, Query()] = None,
    sku: Annotated[list[str] | None, Query()] = None,
    style_code: str | None = None,
    aggregate: Annotated[str | None, Query(description="none | cluster | dc")] = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_inventory(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            location_codes=location_code,
            sku_codes=sku,
            style_code=style_code,
            location_type=location_type,
            aggregate=aggregate,
        )


@router.get("/demand-forecast")
async def demand_forecast(
    state: StateDep,
    _: InventoryRead,
    limit: Limit = None,
    cursor: Cursor = None,
    style_code: str | None = None,
    cluster_code: Annotated[list[str] | None, Query()] = None,
    week: Annotated[list[str] | None, Query()] = None,
    store_code: Annotated[list[str] | None, Query()] = None,
    aggregate: Annotated[str | None, Query(description="cluster (default) | store")] = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.get_demand_forecast(
            conn,
            style_code=style_code,
            cluster_codes=cluster_code,
            weeks=week,
            store_codes=store_code,
            aggregate=aggregate,
            limit=clamp_limit(limit),
            cursor=cursor,
        )


@router.get("/allocations")
async def allocations(
    state: StateDep,
    _: OrdersRead,
    limit: Limit = None,
    cursor: Cursor = None,
    dc_code: str | None = None,
    store_code: str | None = None,
    cluster_code: str | None = None,
    sku: Annotated[list[str] | None, Query()] = None,
    status: str | None = None,
    ship_week: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_allocations(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            dc_code=dc_code,
            store_code=store_code,
            cluster_code=cluster_code,
            sku_codes=sku,
            status=status,
            ship_week=ship_week,
        )


@router.get("/transfer-orders")
async def transfer_orders(
    state: StateDep,
    _: OrdersRead,
    limit: Limit = None,
    cursor: Cursor = None,
    status: str | None = None,
    from_dc: str | None = None,
    to_dc: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_transfer_orders(
            conn, limit=clamp_limit(limit), cursor=cursor, status=status, from_dc=from_dc, to_dc=to_dc
        )


@router.get("/supplier-messages")
async def supplier_messages(
    state: StateDep,
    _: PoRead,
    limit: Limit = None,
    cursor: Cursor = None,
    supplier_code: str | None = None,
    since: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_supplier_messages(
            conn, limit=clamp_limit(limit), cursor=cursor, supplier_code=supplier_code, since=since, status=status
        )


@router.get("/audit-log")
async def audit_log(
    state: StateDep,
    _: CatalogRead,
    limit: Limit = None,
    cursor: Cursor = None,
    change_set_code: str | None = None,
    entity_type: str | None = None,
    entity_ref: str | None = None,
) -> dict[str, Any]:
    async with state.engine.connect() as conn:
        return await catalog.list_audit(
            conn,
            limit=clamp_limit(limit),
            cursor=cursor,
            change_set_code=change_set_code,
            entity_type=entity_type,
            entity_ref=entity_ref,
        )


@router.get("/locations")
async def locations(state: StateDep, _: CatalogRead) -> dict[str, Any]:
    """DCs + clusters with store counts (same payload as MCP ``list_locations``)."""
    async with state.engine.connect() as conn:
        return await catalog.list_locations(conn)
