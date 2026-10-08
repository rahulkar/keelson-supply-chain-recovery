"""Manual changes made by people in the Merxa UI (or plain REST) — M5 Guide mode
(docs/contracts/95 §2).

A Guide-mode step is performed by hand in Merxa: create a transfer order, edit an
allocation quantity, reschedule / change the quantity of a PO line, book or cancel lane
capacity. Each save runs **the same op handler** a change set would use (same validation,
version checks, locks and audit rows) in one transaction, without a change set:

* ``Idempotency-Key`` is required (a retried save replays instead of acting twice);
  record edits also need ``If-Match: "<version>"``;
* every save writes audit rows (``via`` = the caller's channel, e.g. ``Merxa UI``) and one
  outbox event that webhooks deliver (``transfer_order.created``, ``allocation.updated``,
  ``po_line.updated``, ``capacity_booking.created``, ``capacity_booking.cancelled``) — the
  trigger for Keelson's checklist auto-tick, with the entity's state after the change;
* only people may do this: Keelson's OAuth clients (``keelson-*``, including delegated user
  tokens) are refused — Keelson changes data through approved change sets only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from merxa_api import tables as t
from merxa_api.config import Settings
from merxa_api.errors import Problem
from merxa_api.security.principal import ROLE_OPS_DIRECTOR, ROLE_PLANNER, SCOPE_APPLY, SCOPE_PREVIEW, Principal
from merxa_api.services import audit, events, idempotency
from merxa_api.services.audit import Attribution
from merxa_api.services.catalog import sim_today
from merxa_api.services.changesets import Outcome, normalize_items
from merxa_api.services.common import jsonable
from merxa_api.services.ops import HANDLERS, ApplyContext, Conflict, ConflictInfo, ItemPreview, ItemSpec, Simulation

MANUAL_ROLES: tuple[str, ...] = (ROLE_PLANNER, ROLE_OPS_DIRECTOR)
SOURCE = "manual"

Render = Callable[[AsyncConnection, ItemSpec, dict[str, Any], ItemPreview], Awaitable[tuple[str, dict[str, Any]]]]


def require_manual_writer(principal: Principal) -> None:
    """People with a business role (UI session or a realm token that also carries a change
    scope, so an OAuth app holding only read scopes cannot write for them) and API keys with
    ``changes:apply``; never Keelson's clients."""
    if (principal.client_id or "").startswith("keelson-"):
        raise Problem(
            403,
            "manual_edit_forbidden",
            "integration clients change Merxa data through approved change sets, not manual edits",
        )
    if principal.kind == "api_key":
        if not principal.has_scope(SCOPE_APPLY):
            raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_APPLY}")
        return
    if not any(principal.has_role(r) for r in MANUAL_ROLES):
        raise Problem(403, "missing_role", f"requires role {' or '.join(MANUAL_ROLES)}")
    if not principal.has_any_scope((SCOPE_PREVIEW, SCOPE_APPLY)):
        raise Problem(403, "insufficient_scope", f"requires scope {SCOPE_PREVIEW} or {SCOPE_APPLY}")


def _validation_problem(errors: list[str]) -> Problem:
    extra = {"errors": errors}
    if any(e.startswith("version_mismatch") for e in errors):
        return Problem(412, "precondition_failed", "; ".join(errors), extra=extra)
    if any(e.endswith(" not found") for e in errors):
        return Problem(404, "not_found", "; ".join(errors), extra=extra)
    return Problem(422, "validation_failed", "; ".join(errors), extra=extra)


def _conflict_problem(info: ConflictInfo) -> Problem:
    extra = {"conflict": info.to_dict()}
    if info.reason == "version_mismatch":
        return Problem(412, "precondition_failed", f"{info.target_ref} changed meanwhile", extra=extra)
    if info.reason == "not_found":
        return Problem(404, "not_found", f"{info.target_ref} not found", extra=extra)
    return Problem(409, "conflict", f"{info.target_ref}: {info.reason}", extra=extra)


def _actor(principal: Principal) -> dict[str, Any]:
    return {"username": principal.username, "display": principal.actor_display}


class ManualChangeService:
    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    # ───────────── public operations ─────────────
    async def create_transfer_order(
        self, principal: Principal, *, payload: dict[str, Any], idempotency_key: str | None
    ) -> Outcome:
        return await self._run(
            principal,
            idempotency_key,
            {"op": "transfer_order.create", "target_ref": None, "expected_version": None, "payload": payload},
            201,
            _render_transfer_order,
        )

    async def set_allocation_qty(
        self, principal: Principal, code: str, *, qty: Any, version: int, idempotency_key: str | None
    ) -> Outcome:
        return await self._run(
            principal,
            idempotency_key,
            {"op": "allocation.set_qty", "target_ref": code, "expected_version": version, "payload": {"qty": qty}},
            200,
            _render_allocation,
        )

    async def update_po_line(
        self,
        principal: Principal,
        po_code: str,
        line_no: int,
        *,
        eta_dc: Any = None,
        qty_ordered: Any = None,
        version: int,
        idempotency_key: str | None,
    ) -> Outcome:
        if (eta_dc is None) == (qty_ordered is None):
            raise Problem(400, "invalid_parameter", "give exactly one of eta_dc or qty_ordered")
        if eta_dc is not None:
            item = {"op": "po_line.reschedule", "payload": {"eta_dc": eta_dc}}
        else:
            item = {"op": "po_line.change_qty", "payload": {"qty": qty_ordered}}
        item.update(target_ref=f"{po_code}#{line_no}", expected_version=version)
        return await self._run(principal, idempotency_key, item, 200, _render_po_line)

    async def create_capacity_booking(
        self, principal: Principal, *, payload: dict[str, Any], idempotency_key: str | None
    ) -> Outcome:
        return await self._run(
            principal,
            idempotency_key,
            {"op": "capacity_booking.create", "target_ref": None, "expected_version": None, "payload": payload},
            201,
            _render_booking_created,
        )

    async def cancel_capacity_booking(
        self, principal: Principal, code: str, *, version: int, idempotency_key: str | None
    ) -> Outcome:
        return await self._run(
            principal,
            idempotency_key,
            {"op": "capacity_booking.cancel", "target_ref": code, "expected_version": version, "payload": {}},
            200,
            _render_booking_cancelled,
        )

    # ───────────── engine ─────────────
    async def _run(
        self, principal: Principal, key: str | None, item: dict[str, Any], status_code: int, render: Render
    ) -> Outcome:
        require_manual_writer(principal)
        (normalized,) = normalize_items([item])  # same structural checks as change-set items
        claim = await idempotency.begin(
            self.engine,
            principal=principal.sub,
            key=idempotency.validate_key(key),
            request_sha256=idempotency.request_fingerprint(f"manual.{normalized['op']}", normalized),
            stale_after_s=self.settings.idempotency_stale_in_flight_s,
        )
        if isinstance(claim, idempotency.Replay):
            return Outcome(claim.status_code, claim.response, replayed=True)
        spec = ItemSpec(
            1, normalized["op"], normalized["target_ref"], normalized["expected_version"], normalized["payload"]
        )
        handler = HANDLERS[spec.op]
        try:
            async with self.engine.begin() as conn:
                today = await sim_today(conn)
                preview = await handler.preview(conn, spec, Simulation(today=today))
                if not preview.ok:
                    raise _validation_problem(preview.errors)
                info = await handler.check(conn, spec, Simulation(today=today))
                if info is not None:
                    raise _conflict_problem(info)
                ctx = ApplyContext(change_set_code="", today=today, via="ui" if principal.kind == "session" else "api")
                try:
                    entries = await handler.apply(conn, spec, ctx)
                except Conflict as exc:  # a row changed between the check and the write
                    raise _conflict_problem(exc.info) from exc
                await audit.write_many(conn, Attribution.of(principal), entries)
                event_type, data = await render(conn, spec, ctx.results.get(1) or {}, preview)
                data = {**data, "source": SOURCE, "via": principal.via, "changed_by": _actor(principal)}
                await events.emit(conn, event_type, data, await events.changes_for(conn, entries))
                response = {**jsonable(data), "event": event_type}
                await idempotency.complete(conn, claim, status_code, response)
        except BaseException:
            await idempotency.abandon(self.engine, claim)
            raise
        return Outcome(status_code, response)


# ───────────── event / response bodies (entity state after the change) ─────────────
async def _render_transfer_order(
    conn: AsyncConnection, spec: ItemSpec, result: dict[str, Any], preview: ItemPreview
) -> tuple[str, dict[str, Any]]:
    to, tl = t.transfer_order, t.transfer_order_line
    code = result["to_code"]
    head = (await conn.execute(select(to).where(to.c.code == code))).one()
    lines = [
        {"line_no": r.line_no, "sku": r.sku_code, "qty": r.qty}
        for r in await conn.execute(select(tl).where(tl.c.to_code == code).order_by(tl.c.line_no))
    ]
    return "transfer_order.created", {
        "entity_type": "transfer_order",
        "entity_ref": code,
        "code": code,
        "from_dc": head.from_dc,
        "to_dc": head.to_dc,
        "ship_date": head.ship_date,
        "eta_date": head.eta_date,
        "status": head.status,
        "created_via": head.created_via,
        "lines": lines,
        "units": sum(ln["qty"] for ln in lines),
        "version": head.version,
    }


async def _render_allocation(
    conn: AsyncConnection, spec: ItemSpec, result: dict[str, Any], preview: ItemPreview
) -> tuple[str, dict[str, Any]]:
    a = t.allocation
    row = (await conn.execute(select(a).where(a.c.code == spec.target_ref))).one()
    return "allocation.updated", {
        "entity_type": "allocation",
        "entity_ref": row.code,
        "code": row.code,
        "qty": row.qty,
        "qty_before": result["previous"]["qty"],
        "status": row.status,
        "sku": row.sku_code,
        "dc_code": row.dc_code,
        "store_code": row.store_code,
        "ship_week": row.ship_week,
        "version": row.version,
    }


async def _render_po_line(
    conn: AsyncConnection, spec: ItemSpec, result: dict[str, Any], preview: ItemPreview
) -> tuple[str, dict[str, Any]]:
    pl = t.po_line
    po_code, _, line_no = str(spec.target_ref).rpartition("#")
    row = (await conn.execute(select(pl).where(pl.c.po_code == po_code, pl.c.line_no == int(line_no)))).one()
    if spec.op == "po_line.reschedule":
        changed = {"eta_dc": {"before": result["previous"]["eta_dc"], "after": row.eta_dc}}
    else:
        changed = {"qty_ordered": {"before": result["previous"]["qty"], "after": row.qty_ordered}}
    return "po_line.updated", {
        "entity_type": "po_line",
        "entity_ref": spec.target_ref,
        "ref": spec.target_ref,
        "po_code": po_code,
        "line_no": row.line_no,
        "action": "reschedule" if spec.op == "po_line.reschedule" else "change_qty",
        "sku": row.sku_code,
        "eta_dc": row.eta_dc,
        "qty_ordered": row.qty_ordered,
        "qty_received": row.qty_received,
        "status": row.status,
        "changed": changed,
        "version": row.version,
    }


async def _booking_data(conn: AsyncConnection, code: str) -> dict[str, Any]:
    cb = t.capacity_booking
    row = (await conn.execute(select(cb).where(cb.c.code == code))).one()
    return {
        "entity_type": "capacity_booking",
        "entity_ref": row.code,
        "code": row.code,
        "lane_code": row.lane_code,
        "iso_week": row.iso_week,
        "qty_units": row.qty_units,
        "basis": row.basis,
        "status": row.status,
        "created_via": row.created_via,
        "version": row.version,
    }


async def _render_booking_created(
    conn: AsyncConnection, spec: ItemSpec, result: dict[str, Any], preview: ItemPreview
) -> tuple[str, dict[str, Any]]:
    return "capacity_booking.created", await _booking_data(conn, result["code"])


async def _render_booking_cancelled(
    conn: AsyncConnection, spec: ItemSpec, result: dict[str, Any], preview: ItemPreview
) -> tuple[str, dict[str, Any]]:
    data = await _booking_data(conn, str(spec.target_ref))
    after = preview.after if isinstance(preview.after, dict) else {}
    return "capacity_booking.cancelled", {
        **data,
        "status_before": result["previous"]["status"],
        "linked_shipments": after.get("linked_shipments") or [],
    }
