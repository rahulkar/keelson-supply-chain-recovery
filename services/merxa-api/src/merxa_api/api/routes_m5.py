"""M5 REST endpoints (docs/contracts/95 §2): manual changes behind the Merxa UI's pre-filled
Guide-mode forms, plus the single-record reads those forms load.

Writes need an ``Idempotency-Key``; record edits also ``If-Match: "<version>"``. Each write
runs the change-set op handler for that change (validation, locks, audit) and emits one
webhook event (see :mod:`merxa_api.services.manual`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from merxa_api import tables as t
from merxa_api.api.deps import IdempotencyKey, IfMatch, PrincipalDep, StateDep, etag, parse_if_match, require_scope
from merxa_api.errors import not_found
from merxa_api.security.principal import Principal
from merxa_api.services import supply
from merxa_api.services.catalog import sim_today
from merxa_api.services.changesets import Outcome
from merxa_api.services.common import row_dict

router = APIRouter(prefix="/v1", tags=["manual changes"])

PoRead = Annotated[Principal, Depends(require_scope("po:read"))]
OrdersRead = Annotated[Principal, Depends(require_scope("orders:read"))]
LineNo = Annotated[int, Path(ge=1, le=1_000_000)]


class TransferLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=64)
    qty: int


class TransferOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_dc: str = Field(min_length=1, max_length=64)
    to_dc: str = Field(min_length=1, max_length=64)
    ship_date: str = Field(min_length=1, max_length=10, description="YYYY-MM-DD")
    lines: list[TransferLine] = Field(min_length=1, max_length=500)


class AllocationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty: int


class PoLinePatch(BaseModel):
    """Exactly one field: ``eta_dc`` (reschedule) or ``qty_ordered`` (change quantity)."""

    model_config = ConfigDict(extra="forbid")
    eta_dc: str | None = Field(None, max_length=10, description="YYYY-MM-DD")
    qty_ordered: int | None = None


class BookingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lane_code: str = Field(min_length=1, max_length=64)
    iso_week: str = Field(min_length=1, max_length=8, description="YYYY-Www")
    qty_units: int


def _respond(outcome: Outcome, *, with_etag: bool = False) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else {}
    version = outcome.body.get("version")
    if with_etag and isinstance(version, int):
        headers["ETag"] = etag(version)
    return JSONResponse(outcome.body, status_code=outcome.status_code, headers=headers)


# ───────────── transfer orders ─────────────
@router.post("/transfer-orders", status_code=201)
async def create_transfer_order(
    body: TransferOrderIn, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    outcome = await state.manual.create_transfer_order(
        principal, payload=body.model_dump(), idempotency_key=idempotency_key
    )
    return _respond(outcome, with_etag=True)


# ───────────── allocations ─────────────
@router.get("/allocations/{code}")
async def get_allocation(code: str, state: StateDep, _: OrdersRead) -> JSONResponse:
    a = t.allocation
    async with state.engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    a.c.code,
                    a.c.sku_code.label("sku"),
                    a.c.dc_code,
                    a.c.store_code,
                    a.c.ship_week,
                    a.c.qty,
                    a.c.status,
                    a.c.version,
                ).where(a.c.code == code)
            )
        ).first()
    if row is None:
        raise not_found("allocation", code)
    return JSONResponse(row_dict(row), headers={"ETag": etag(row.version)})


@router.patch("/allocations/{code}")
async def patch_allocation(
    code: str,
    body: AllocationPatch,
    state: StateDep,
    principal: PrincipalDep,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    version = parse_if_match(if_match)
    outcome = await state.manual.set_allocation_qty(
        principal, code, qty=body.qty, version=version, idempotency_key=idempotency_key
    )
    return _respond(outcome, with_etag=True)


# ───────────── purchase-order lines ─────────────
@router.patch("/purchase-orders/{po_code}/lines/{line_no}")
async def patch_po_line(
    po_code: str,
    line_no: LineNo,
    body: PoLinePatch,
    state: StateDep,
    principal: PrincipalDep,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    version = parse_if_match(if_match)
    outcome = await state.manual.update_po_line(
        principal,
        po_code,
        line_no,
        eta_dc=body.eta_dc,
        qty_ordered=body.qty_ordered,
        version=version,
        idempotency_key=idempotency_key,
    )
    return _respond(outcome, with_etag=True)


# ───────────── capacity bookings ─────────────
@router.post("/capacity-bookings", status_code=201)
async def create_capacity_booking(
    body: BookingIn, state: StateDep, principal: PrincipalDep, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    outcome = await state.manual.create_capacity_booking(
        principal, payload=body.model_dump(), idempotency_key=idempotency_key
    )
    return _respond(outcome, with_etag=True)


@router.get("/capacity-bookings/{code}")
async def get_capacity_booking(code: str, state: StateDep, _: PoRead) -> JSONResponse:
    """One booking + the shipments that reference it + the lane's remaining capacity in its week."""
    cb, sh = t.capacity_booking, t.shipment
    async with state.engine.connect() as conn:
        row = (
            await conn.execute(select(*[c for c in cb.c if c.key not in ("id", "updated_at")]).where(cb.c.code == code))
        ).first()
        if row is None:
            raise not_found("capacity booking", code)
        shipments = [
            row_dict(r)
            for r in await conn.execute(
                select(sh.c.code, sh.c.po_code, sh.c.mode, sh.c.status, sh.c.etd, sh.c.eta)
                .where(sh.c.booking_code == code)
                .order_by(sh.c.code)
            )
        ]
        remaining, rate = await supply.remaining_capacity(conn, row.lane_code, row.iso_week)
        today = await sim_today(conn)
    body: dict[str, Any] = {
        **row_dict(row),
        "shipments": shipments,
        "lane_week": {
            "remaining_capacity": remaining,
            "weekly_capacity": int(rate.weekly_capacity) if rate is not None else None,
            "basis": rate.basis if rate is not None else None,
        },
        "today": today.isoformat(),
    }
    return JSONResponse(body, headers={"ETag": etag(row.version)})


@router.post("/capacity-bookings/{code}:cancel")
async def cancel_capacity_booking(
    code: str,
    state: StateDep,
    principal: PrincipalDep,
    if_match: IfMatch = None,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    version = parse_if_match(if_match)
    outcome = await state.manual.cancel_capacity_booking(
        principal, code, version=version, idempotency_key=idempotency_key
    )
    return _respond(outcome, with_etag=True)
