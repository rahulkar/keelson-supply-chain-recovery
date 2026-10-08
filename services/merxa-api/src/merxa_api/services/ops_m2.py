"""M2 change-set ops (docs/contracts/22-merxa-m2.md §3) and the internal ``revert.*`` ops
used by compensating (rollback) change sets.

Public: ``po_line.cancel``, ``po.create``, ``po_line.split``, ``capacity_booking.create``,
``capacity_booking.cancel`` (M5), ``production_order.resequence``, ``promotion.reschedule``,
``online_order.repromise``.
Internal: ``revert.transfer_order``, ``revert.po_create``, ``revert.po_line_status``,
``revert.po_line_split``, ``revert.capacity_booking``, ``revert.capacity_booking_status`` (M5),
``revert.production_schedule``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.calendar import is_iso_week, iso_week, week_monday
from merxa_api.services import supply
from merxa_api.services.ops import (
    MAX_HORIZON_DAYS,
    ApplyContext,
    AuditEntry,
    Conflict,
    ConflictInfo,
    InverseItem,
    ItemPreview,
    ItemSpec,
    Simulation,
    _date,
    _int,
    _need,
    _str,
    _unit_cost,
    _unknown_keys,
    parse_po_line_ref,
)

CANCELLABLE_PO_LINE = ("open", "in_production")
SPLITTABLE_PO_LINE = ("open", "in_production")
MOVABLE_ORDER = ("planned", "held")
REPROMISABLE_ORDER = ("open", "picking")
CANCELLABLE_BOOKING = ("held", "confirmed")
MAX_PO_LINES = 500


def _horizon(d: date, sim: Simulation, name: str, errors: list[str], *, allow_past: bool = False) -> None:
    if not allow_past and d < sim.today:
        errors.append(f"{name} {d.isoformat()} is in the past (today {sim.today.isoformat()})")
    elif d > sim.today + timedelta(days=MAX_HORIZON_DAYS):
        errors.append(f"{name} {d.isoformat()} is more than {MAX_HORIZON_DAYS} days ahead")
    elif d < sim.today - timedelta(days=MAX_HORIZON_DAYS):
        errors.append(f"{name} {d.isoformat()} is more than {MAX_HORIZON_DAYS} days in the past")


def _conflict(item: ItemSpec, reason: str, actual: int | None = None, ref: str | None = None) -> Conflict:
    return Conflict(ConflictInfo(item.seq, ref or item.target_ref, item.expected_version, actual, reason))


class _Base:
    op: ClassVar[str] = ""

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None:
        """Phase 1: lock targets and re-validate (default: run the locking part of apply checks)."""
        try:
            await self._lock(conn, item, sim)
        except Conflict as exc:
            return exc.info
        return None

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        raise NotImplementedError


# ───────────────────────── po_line.* (cancel / restore status) ─────────────────────────
async def _po_line(conn: AsyncConnection, ref: tuple[str, int], *, lock: bool = False) -> Any:
    pl = t.po_line
    q = select(pl).where(pl.c.po_code == ref[0], pl.c.line_no == ref[1])
    if lock:
        q = q.with_for_update()
    return (await conn.execute(q)).first()


async def _locked_po_line(conn: AsyncConnection, item: ItemSpec, allowed: tuple[str, ...]) -> Any:
    ref = parse_po_line_ref(item.target_ref)
    if ref is None or item.expected_version is None:
        raise _conflict(item, "invalid_payload")
    row = await _po_line(conn, ref, lock=True)
    if row is None:
        raise _conflict(item, "not_found")
    if row.version != item.expected_version:
        raise _conflict(item, "version_mismatch", row.version)
    if row.status not in allowed:
        raise _conflict(item, "status_changed", row.version)
    return row


async def _update_po_line(conn: AsyncConnection, item: ItemSpec, row: Any, values: dict[str, Any]) -> int:
    pl = t.po_line
    version = (
        await conn.execute(
            update(pl)
            .where(pl.c.po_code == row.po_code, pl.c.line_no == row.line_no, pl.c.version == row.version)
            .values(**values)
            .returning(pl.c.version)
        )
    ).scalar_one_or_none()
    if version is None:
        raise _conflict(item, "version_mismatch")
    return int(version)


def _po_line_preview_common(item: ItemSpec, sim: Simulation, errors: list[str]) -> tuple[str, int] | None:
    ref = parse_po_line_ref(item.target_ref)
    if ref is None:
        errors.append("target_ref must be '<po_code>#<line_no>'")
    if item.expected_version is None:
        errors.append("expected_version is required")
    if ref is not None and str(item.target_ref) in sim.touched:
        errors.append(f"po line {item.target_ref} is changed by an earlier item")
    return ref


class PoLineCancel(_Base):
    op = "po_line.cancel"

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        _unknown_keys(item.payload, frozenset(), errors)
        ref = _po_line_preview_common(item, sim, errors)
        if ref is None:
            return ItemPreview(errors=errors)
        row = await _po_line(conn, ref)
        if row is None:
            return ItemPreview(errors=[f"po line {item.target_ref} not found"])
        sim.touched.add(str(item.target_ref))
        if item.expected_version is not None and row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if row.status not in CANCELLABLE_PO_LINE:
            errors.append(f"po line status {row.status!r} cannot be cancelled")
        open_qty = row.qty_ordered - row.qty_received
        costs = await _unit_cost(conn, [row.sku_code])
        before = {"status": row.status, "sku": row.sku_code, "qty_open": open_qty, "version": row.version}
        after = {**before, "status": "cancelled", "qty_open": 0, "version": row.version + 1}
        return ItemPreview(
            before=before,
            after=after,
            errors=errors,
            units_moved=open_qty,
            value_cents=open_qty * costs.get(row.sku_code, 0),
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        return await _locked_po_line(conn, item, CANCELLABLE_PO_LINE)

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        row = await _locked_po_line(conn, item, CANCELLABLE_PO_LINE)
        version = await _update_po_line(conn, item, row, {"status": "cancelled"})
        ctx.results[item.seq] = {"version": version, "previous": {"status": row.status}}
        return [
            (
                "po_line",
                str(item.target_ref),
                "cancel",
                {"status": row.status, "version": row.version},
                {"status": "cancelled", "version": version},
            )
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {
                "op": "revert.po_line_status",
                "target_ref": item.target_ref,
                "expected_version": r["version"],
                "payload": {"status": r["previous"]["status"]},
            }
        ]


class RevertPoLineStatus(_Base):
    op = "revert.po_line_status"

    def _status(self, item: ItemSpec, errors: list[str]) -> str | None:
        _unknown_keys(item.payload, frozenset({"status"}), errors)
        status = _str(item.payload, "status", errors)
        if status is not None and status not in ("open", "in_production", "shipped"):
            errors.append("payload.status must be open, in_production or shipped")
            return None
        return status

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        status = self._status(item, errors)
        ref = _po_line_preview_common(item, sim, errors)
        row = await _po_line(conn, ref) if ref else None
        if row is None:
            return ItemPreview(errors=errors or [f"po line {item.target_ref} not found"])
        sim.touched.add(str(item.target_ref))
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        before = {"status": row.status, "version": row.version}
        return ItemPreview(before=before, after={"status": status, "version": row.version + 1}, errors=errors)

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        return await _locked_po_line(conn, item, ("cancelled",))

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        status = self._status(item, errors)
        row = await _locked_po_line(conn, item, ("cancelled",))
        if status is None:
            raise _conflict(item, "invalid_payload", row.version)
        version = await _update_po_line(conn, item, row, {"status": status})
        ctx.results[item.seq] = {"version": version}
        return [
            ("po_line", str(item.target_ref), "restore", {"status": row.status}, {"status": status, "version": version})
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── po.create ─────────────────────────
class PoCreate(_Base):
    op = "po.create"
    keys = frozenset({"supplier_code", "factory_code", "dest_dc", "eta_dc", "lines"})

    def _parse(self, item: ItemSpec, errors: list[str]) -> dict[str, Any] | None:
        if item.target_ref is not None:
            errors.append("target_ref must be null for po.create")
        if item.expected_version is not None:
            errors.append("expected_version must be null for po.create")
        p = item.payload
        _unknown_keys(p, self.keys, errors)
        out = {
            "supplier_code": _str(p, "supplier_code", errors),
            "factory_code": _str(p, "factory_code", errors),
            "dest_dc": _str(p, "dest_dc", errors),
            "eta_dc": _date(p, "eta_dc", errors),
        }
        lines_raw = p.get("lines")
        lines: list[tuple[str, int]] = []
        if not isinstance(lines_raw, list) or not lines_raw:
            errors.append("payload.lines must be a non-empty array")
        elif len(lines_raw) > MAX_PO_LINES:
            errors.append(f"payload.lines holds at most {MAX_PO_LINES} lines")
        else:
            seen: set[str] = set()
            for i, line in enumerate(lines_raw):
                if not isinstance(line, dict) or set(line) - {"sku", "qty"}:
                    errors.append(f"payload.lines[{i}] must be {{sku, qty}}")
                    continue
                sku, qty = line.get("sku"), line.get("qty")
                if not isinstance(sku, str) or not sku:
                    errors.append(f"payload.lines[{i}].sku must be a string")
                    continue
                if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
                    errors.append(f"payload.lines[{i}].qty must be a positive integer")
                    continue
                if sku in seen:
                    errors.append(f"payload.lines has duplicate sku {sku}")
                    continue
                seen.add(sku)
                lines.append((sku, qty))
        out["lines"] = lines
        return None if errors else out

    async def _validate(
        self, conn: AsyncConnection, p: dict[str, Any], today: date, errors: list[str]
    ) -> tuple[str, date, int] | None:
        """→ (factory country, ex-factory date, transit days)."""
        sup = (await conn.execute(select(t.supplier.c.code).where(t.supplier.c.code == p["supplier_code"]))).first()
        if sup is None:
            errors.append(f"unknown supplier {p['supplier_code']}")
        fac = (
            await conn.execute(
                select(t.factory.c.code, t.factory.c.country, t.factory.c.supplier_code).where(
                    t.factory.c.code == p["factory_code"]
                )
            )
        ).first()
        if fac is None:
            errors.append(f"unknown factory {p['factory_code']}")
        elif fac.supplier_code != p["supplier_code"]:
            errors.append(f"factory {fac.code} belongs to supplier {fac.supplier_code}, not {p['supplier_code']}")
        dc = (await conn.execute(select(t.dc.c.code).where(t.dc.c.code == p["dest_dc"]))).first()
        if dc is None:
            errors.append(f"unknown dc {p['dest_dc']}")
        skus = [s for s, _ in p["lines"]]
        known = {r.code for r in await conn.execute(select(t.sku.c.code).where(t.sku.c.code.in_(skus)))}
        errors.extend(f"unknown sku {s}" for s in skus if s not in known)
        if errors or fac is None:
            return None
        eta: date = p["eta_dc"]
        if eta > today + timedelta(days=MAX_HORIZON_DAYS):
            errors.append(f"eta_dc {eta.isoformat()} is more than {MAX_HORIZON_DAYS} days ahead")
            return None
        origin = supply.ORIGIN_PORT.get(fac.country)
        route = await supply.fastest_transit(conn, origin, p["dest_dc"]) if origin else None
        if route is None:
            errors.append(f"no lane from factory {fac.code} ({fac.country}) to {p['dest_dc']}")
            return None
        transit, _lane = route
        ex_factory = eta - timedelta(days=transit)
        if ex_factory <= today:
            earliest = today + timedelta(days=transit + 1)
            errors.append(
                f"eta_dc {eta.isoformat()} is not reachable from {fac.code}: transit is {transit} days, "
                f"earliest ETA {earliest.isoformat()}"
            )
            return None
        return fac.country, ex_factory, transit

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        p = self._parse(item, errors)
        if p is None:
            return ItemPreview(errors=errors)
        found = await self._validate(conn, p, sim.today, errors)
        if found is None:
            return ItemPreview(errors=errors)
        country, ex_factory, transit = found
        costs = await _unit_cost(conn, [s for s, _ in p["lines"]])
        units = sum(q for _, q in p["lines"])
        return ItemPreview(
            before={"purchase_order": None},
            after={
                "purchase_order": {
                    "supplier_code": p["supplier_code"],
                    "factory_code": p["factory_code"],
                    "dest_dc": p["dest_dc"],
                    "status": "open",
                    "ex_factory_date": ex_factory.isoformat(),
                    "eta_dc": p["eta_dc"].isoformat(),
                    "transit_days": transit,
                    "origin_country": country,
                },
                "lines": [{"sku": s, "qty": q} for s, q in p["lines"]],
                "total_qty": units,
            },
            units_moved=units,
            value_cents=sum(q * costs.get(s, 0) for s, q in p["lines"]),
        )

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None:
        errors: list[str] = []
        p = self._parse(item, errors)
        if p is None or await self._validate(conn, p, sim.today, errors) is None:
            return ConflictInfo(item.seq, item.target_ref, None, None, "invalid_payload")
        return None

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        p = self._parse(item, errors)
        found = await self._validate(conn, p, ctx.today, errors) if p else None
        if p is None or found is None:
            raise _conflict(item, "invalid_payload")
        country, ex_factory, _transit = found
        n = (await conn.execute(text("SELECT nextval('merxa.po_code_seq')"))).scalar_one()
        code = f"PO-{country}-{n:04d}"
        header = {
            "code": code,
            "supplier_code": p["supplier_code"],
            "factory_code": p["factory_code"],
            "dest_dc_code": p["dest_dc"],
            "status": "open",
            "ex_factory_date": ex_factory,
            "eta_dc": p["eta_dc"],
        }
        version = (
            await conn.execute(t.purchase_order.insert().values(**header).returning(t.purchase_order.c.version))
        ).scalar_one()
        await conn.execute(
            t.po_line.insert(),
            [
                {
                    "po_code": code,
                    "line_no": i,
                    "sku_code": s,
                    "qty_ordered": q,
                    "qty_received": 0,
                    "eta_dc": p["eta_dc"],
                    "status": "open",
                }
                for i, (s, q) in enumerate(p["lines"], start=1)
            ],
        )
        ctx.results[item.seq] = {"po_code": code, "version": version}
        return [
            (
                "purchase_order",
                code,
                "create",
                None,
                {**header, "created_via": ctx.created_via, "lines": [{"sku": s, "qty": q} for s, q in p["lines"]]},
            )
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "po_code", "version")
        return [{"op": "revert.po_create", "target_ref": r["po_code"], "expected_version": r["version"], "payload": {}}]


class RevertPoCreate(_Base):
    """Cancel a purchase order created by ``po.create`` (header + lines), if untouched."""

    op = "revert.po_create"

    async def _rows(self, conn: AsyncConnection, item: ItemSpec, *, lock: bool) -> tuple[Any, list[Any]]:
        q = select(t.purchase_order).where(t.purchase_order.c.code == item.target_ref)
        lq = select(t.po_line).where(t.po_line.c.po_code == item.target_ref).order_by(t.po_line.c.line_no)
        if lock:
            q, lq = q.with_for_update(), lq.with_for_update()
        return (await conn.execute(q)).first(), list(await conn.execute(lq))

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        po, lines = await self._rows(conn, item, lock=False)
        if po is None:
            return ItemPreview(errors=[f"purchase order {item.target_ref} not found"])
        errors = []
        if po.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {po.version}")
        if po.status != "open" or any(ln.qty_received or ln.status != "open" for ln in lines):
            errors.append(f"purchase order {po.code} is no longer an untouched open order")
        return ItemPreview(
            before={"status": po.status, "lines": len(lines), "version": po.version},
            after={"status": "cancelled", "lines": len(lines), "version": po.version + 1},
            errors=errors,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        po, lines = await self._rows(conn, item, lock=True)
        if po is None:
            raise _conflict(item, "not_found")
        if po.version != item.expected_version:
            raise _conflict(item, "version_mismatch", po.version)
        if po.status != "open" or any(ln.qty_received or ln.status != "open" for ln in lines):
            raise _conflict(item, "status_changed", po.version)
        return po, lines

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        po, lines = await self._lock(conn, item, Simulation(ctx.today))
        await conn.execute(update(t.po_line).where(t.po_line.c.po_code == po.code).values(status="cancelled"))
        version = (
            await conn.execute(
                update(t.purchase_order)
                .where(t.purchase_order.c.code == po.code)
                .values(status="cancelled")
                .returning(t.purchase_order.c.version)
            )
        ).scalar_one()
        ctx.results[item.seq] = {"version": version}
        return [
            ("purchase_order", po.code, "cancel", {"status": po.status}, {"status": "cancelled", "lines": len(lines)})
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── po_line.split (sea-air) ─────────────────────────
class PoLineSplit(_Base):
    op = "po_line.split"

    def _parse(self, item: ItemSpec, errors: list[str], sim: Simulation | None = None) -> tuple[str, int, str] | None:
        _unknown_keys(item.payload, frozenset({"mode", "qty", "lane_code"}), errors)
        mode = item.payload.get("mode")
        if mode != "sea_air":
            errors.append("payload.mode must be 'sea_air'")
        qty = _int(item.payload, "qty", errors, minimum=1)
        lane = _str(item.payload, "lane_code", errors)
        if parse_po_line_ref(item.target_ref) is None:
            errors.append("target_ref must be '<po_code>#<line_no>'")
        if item.expected_version is None:
            errors.append("expected_version is required")
        if errors or qty is None or lane is None:
            return None
        return str(item.target_ref), qty, lane

    async def _plan(
        self,
        conn: AsyncConnection,
        ref: tuple[str, int],
        qty: int,
        lane_code: str,
        today: date,
        errors: list[str],
        *,
        lock: bool = False,
    ) -> dict[str, Any] | None:
        row = await _po_line(conn, ref, lock=lock)
        if row is None:
            errors.append(f"po line {ref[0]}#{ref[1]} not found")
            return None
        po = (
            await conn.execute(
                select(t.purchase_order, t.factory.c.country)
                .select_from(t.purchase_order.join(t.factory, t.factory.c.code == t.purchase_order.c.factory_code))
                .where(t.purchase_order.c.code == ref[0])
            )
        ).first()
        lane = (await conn.execute(select(t.lane).where(t.lane.c.code == lane_code))).first()
        if lane is None:
            errors.append(f"unknown lane {lane_code}")
            return None
        if lane.mode != "sea_air":
            errors.append(f"lane {lane_code} is not a sea-air lane (mode {lane.mode})")
        origin = supply.ORIGIN_PORT.get(po.country)
        if lane.origin != origin or lane.dest != po.dest_dc_code:
            errors.append(
                f"lane {lane_code} runs {lane.origin} → {lane.dest}, the PO needs {origin} → {po.dest_dc_code}"
            )
        if row.status not in SPLITTABLE_PO_LINE:
            errors.append(f"po line status {row.status!r} cannot be split")
        open_qty = row.qty_ordered - row.qty_received
        if qty >= open_qty:
            errors.append(f"qty {qty} must be smaller than the open quantity {open_qty}")
        departure = max(today + timedelta(days=1), po.ex_factory_date + timedelta(days=1))
        week = iso_week(departure)
        eta = departure + timedelta(days=lane.transit_days)
        return {"row": row, "po": po, "lane": lane, "week": week, "eta": eta, "departure": departure}

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            return ItemPreview(errors=errors)
        ref_s, qty, lane_code = parsed
        if ref_s in sim.touched:
            errors.append(f"po line {ref_s} is changed by an earlier item")
        sim.touched.add(ref_s)
        plan = await self._plan(conn, parse_po_line_ref(ref_s), qty, lane_code, sim.today, errors)  # type: ignore[arg-type]
        if plan is None:
            return ItemPreview(errors=errors)
        row = plan["row"]
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        remaining, rate = await supply.remaining_capacity(conn, lane_code, plan["week"])
        if rate is None:
            errors.append(f"lane {lane_code} has no rate card for {plan['week']}")
        else:
            available = remaining - sim.capacity_drawn[(lane_code, plan["week"])]
            if qty > available:
                errors.append(
                    f"insufficient capacity on {lane_code} in {plan['week']}: requested {qty}, "
                    f"available {max(available, 0)}"
                )
            elif not errors:
                sim.capacity_drawn[(lane_code, plan["week"])] += qty
        costs = await _unit_cost(conn, [row.sku_code])
        before = {
            "qty_ordered": row.qty_ordered,
            "eta_dc": row.eta_dc.isoformat(),
            "sku": row.sku_code,
            "version": row.version,
        }
        after = {
            **before,
            "qty_ordered": row.qty_ordered - qty,
            "version": row.version + 1,
            "split": {
                "qty": qty,
                "lane_code": lane_code,
                "mode": "sea_air",
                "iso_week": plan["week"],
                "etd": plan["departure"].isoformat(),
                "eta_dc": plan["eta"].isoformat(),
                "days_earlier": (row.eta_dc - plan["eta"]).days,
                "freight_cents": qty * int(rate.rate_cents) if rate else None,
            },
        }
        return ItemPreview(
            before=before, after=after, errors=errors, units_moved=qty, value_cents=qty * costs.get(row.sku_code, 0)
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            raise _conflict(item, "invalid_payload")
        ref_s, qty, lane_code = parsed
        await conn.execute(select(t.lane.c.code).where(t.lane.c.code == lane_code).with_for_update())
        plan = await self._plan(conn, parse_po_line_ref(ref_s), qty, lane_code, sim.today, errors, lock=True)  # type: ignore[arg-type]
        if plan is None:
            raise _conflict(item, "not_found")
        if plan["row"].version != item.expected_version:
            raise _conflict(item, "version_mismatch", plan["row"].version)
        if errors:
            raise _conflict(item, "status_changed", plan["row"].version)
        remaining, rate = await supply.remaining_capacity(conn, lane_code, plan["week"])
        if rate is None or qty > remaining - sim.capacity_drawn[(lane_code, plan["week"])]:
            raise _conflict(item, "insufficient_capacity", plan["row"].version, ref=f"{lane_code}/{plan['week']}")
        sim.capacity_drawn[(lane_code, plan["week"])] += qty
        return plan

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        plan = await self._lock(conn, item, Simulation(ctx.today))
        _ref, qty, lane_code = self._parse(item, [])  # type: ignore[misc]
        row, po, lane = plan["row"], plan["po"], plan["lane"]
        pl = t.po_line
        version = await _update_po_line(conn, item, row, {"qty_ordered": row.qty_ordered - qty})
        new_no = (
            int((await conn.execute(select(func.max(pl.c.line_no)).where(pl.c.po_code == row.po_code))).scalar_one())
            + 1
        )
        new_version = (
            await conn.execute(
                pl.insert()
                .values(
                    po_code=row.po_code,
                    line_no=new_no,
                    sku_code=row.sku_code,
                    qty_ordered=qty,
                    qty_received=0,
                    eta_dc=plan["eta"],
                    status=row.status,
                )
                .returning(pl.c.version)
            )
        ).scalar_one()
        new_ref = f"{row.po_code}#{new_no}"
        bk_n = (await conn.execute(text("SELECT nextval('merxa.capacity_booking_code_seq')"))).scalar_one()
        booking = f"CB-{bk_n:04d}"
        await conn.execute(
            t.capacity_booking.insert().values(
                code=booking,
                lane_code=lane_code,
                iso_week=plan["week"],
                qty_units=qty,
                basis="per_unit",
                status="confirmed",
                created_via=ctx.created_via,
            )
        )
        sh_n = (await conn.execute(text("SELECT nextval('merxa.shipment_code_seq')"))).scalar_one()
        shipment = f"SHP-{sh_n:06d}"
        await conn.execute(
            t.shipment.insert().values(
                code=shipment,
                po_code=row.po_code,
                mode="sea_air",
                lane_code=lane_code,
                booking_code=booking,
                origin=lane.origin,
                dest_dc_code=po.dest_dc_code,
                etd=plan["departure"],
                eta=plan["eta"],
                status="booked",
                created_via=ctx.created_via,
            )
        )
        await conn.execute(
            t.shipment_line.insert().values(
                shipment_code=shipment, line_no=1, po_line_ref=new_ref, sku_code=row.sku_code, qty=qty
            )
        )
        # The original shipment carries less now.
        sl = t.shipment_line
        orig = (
            await conn.execute(
                select(sl.c.shipment_code, sl.c.line_no, sl.c.qty)
                .select_from(sl.join(t.shipment, t.shipment.c.code == sl.c.shipment_code))
                .where(sl.c.po_line_ref == item.target_ref, t.shipment.c.status != "cancelled", sl.c.qty > qty)
                .order_by(sl.c.shipment_code)
                .limit(1)
                .with_for_update(of=sl)
            )
        ).first()
        if orig is not None:
            await conn.execute(
                update(sl)
                .where(sl.c.shipment_code == orig.shipment_code, sl.c.line_no == orig.line_no)
                .values(qty=orig.qty - qty)
            )
        ctx.results[item.seq] = {
            "version": version,
            "new_line_ref": new_ref,
            "new_line_version": new_version,
            "qty": qty,
            "shipment_code": shipment,
            "booking_code": booking,
            "orig_shipment_line": [orig.shipment_code, orig.line_no] if orig else None,
        }
        return [
            (
                "po_line",
                str(item.target_ref),
                "split",
                {"qty_ordered": row.qty_ordered, "version": row.version},
                {"qty_ordered": row.qty_ordered - qty, "version": version, "split_to": new_ref},
            ),
            (
                "po_line",
                new_ref,
                "create",
                None,
                {"sku": row.sku_code, "qty_ordered": qty, "eta_dc": plan["eta"], "lane_code": lane_code},
            ),
            (
                "capacity_booking",
                booking,
                "create",
                None,
                {"lane_code": lane_code, "iso_week": plan["week"], "qty_units": qty},
            ),
            ("shipment", shipment, "create", None, {"mode": "sea_air", "lane_code": lane_code, "eta": plan["eta"]}),
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "new_line_ref", "new_line_version", "qty", "shipment_code", "booking_code")
        return [
            {
                "op": "revert.po_line_split",
                "target_ref": item.target_ref,
                "expected_version": r["version"],
                "payload": {
                    "new_line_ref": r["new_line_ref"],
                    "new_line_version": r["new_line_version"],
                    "qty": r["qty"],
                    "shipment_code": r["shipment_code"],
                    "booking_code": r["booking_code"],
                    "orig_shipment_line": r.get("orig_shipment_line"),
                },
            }
        ]


class RevertPoLineSplit(_Base):
    op = "revert.po_line_split"

    async def _state(self, conn: AsyncConnection, item: ItemSpec, *, lock: bool) -> tuple[Any, Any]:
        p = item.payload
        orig = await _po_line(conn, parse_po_line_ref(item.target_ref) or ("", 0), lock=lock)
        new = await _po_line(conn, parse_po_line_ref(str(p.get("new_line_ref"))) or ("", 0), lock=lock)
        return orig, new

    def _problem(self, item: ItemSpec, orig: Any, new: Any) -> str | None:
        p = item.payload
        if orig is None or new is None:
            return "not_found"
        if orig.version != item.expected_version or new.version != p.get("new_line_version"):
            return "version_mismatch"
        if new.qty_received or new.status in ("received", "cancelled"):
            return "status_changed"
        return None

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        orig, new = await self._state(conn, item, lock=False)
        why = self._problem(item, orig, new)
        if orig is None or new is None:
            return ItemPreview(errors=[f"po line {item.target_ref} or {item.payload.get('new_line_ref')} not found"])
        qty = int(item.payload.get("qty") or 0)
        return ItemPreview(
            before={
                "qty_ordered": orig.qty_ordered,
                "split_line": {"ref": item.payload.get("new_line_ref"), "status": new.status},
            },
            after={
                "qty_ordered": orig.qty_ordered + qty,
                "split_line": {"ref": item.payload.get("new_line_ref"), "status": "cancelled"},
            },
            errors=[why] if why else [],
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        orig, new = await self._state(conn, item, lock=True)
        why = self._problem(item, orig, new)
        if why:
            raise _conflict(item, why, orig.version if orig else None)
        return orig, new

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        orig, new = await self._lock(conn, item, Simulation(ctx.today))
        p = item.payload
        qty = int(p["qty"])
        version = await _update_po_line(conn, item, orig, {"qty_ordered": orig.qty_ordered + qty})
        pl = t.po_line
        await conn.execute(
            update(pl).where(pl.c.po_code == new.po_code, pl.c.line_no == new.line_no).values(status="cancelled")
        )
        await conn.execute(update(t.shipment).where(t.shipment.c.code == p["shipment_code"]).values(status="cancelled"))
        await conn.execute(
            update(t.capacity_booking).where(t.capacity_booking.c.code == p["booking_code"]).values(status="released")
        )
        if p.get("orig_shipment_line"):
            sc, ln = p["orig_shipment_line"]
            sl = t.shipment_line
            await conn.execute(
                update(sl).where(sl.c.shipment_code == sc, sl.c.line_no == ln).values(qty=sl.c.qty + qty)
            )
        ctx.results[item.seq] = {"version": version}
        return [
            (
                "po_line",
                str(item.target_ref),
                "unsplit",
                {"qty_ordered": orig.qty_ordered},
                {"qty_ordered": orig.qty_ordered + qty, "version": version},
            ),
            ("po_line", str(p["new_line_ref"]), "cancel", {"status": new.status}, {"status": "cancelled"}),
            ("shipment", str(p["shipment_code"]), "cancel", None, {"status": "cancelled"}),
            ("capacity_booking", str(p["booking_code"]), "release", None, {"status": "released"}),
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── capacity_booking.create ─────────────────────────
class CapacityBookingCreate(_Base):
    op = "capacity_booking.create"

    def _parse(self, item: ItemSpec, errors: list[str]) -> tuple[str, str, int] | None:
        if item.target_ref is not None:
            errors.append("target_ref must be null for capacity_booking.create")
        if item.expected_version is not None:
            errors.append("expected_version must be null for capacity_booking.create")
        _unknown_keys(item.payload, frozenset({"lane_code", "iso_week", "qty_units"}), errors)
        lane = _str(item.payload, "lane_code", errors)
        week = _str(item.payload, "iso_week", errors)
        qty = _int(item.payload, "qty_units", errors, minimum=1)
        if week is not None and not is_iso_week(week):
            errors.append("payload.iso_week must be an ISO week (YYYY-Www)")
        if errors or lane is None or week is None or qty is None:
            return None
        return lane, week, qty

    async def _capacity(
        self, conn: AsyncConnection, lane: str, week: str, today: date, errors: list[str]
    ) -> int | None:
        if (await conn.execute(select(t.lane.c.code).where(t.lane.c.code == lane))).first() is None:
            errors.append(f"unknown lane {lane}")
            return None
        if week_monday(week) + timedelta(days=6) < today:
            errors.append(f"iso_week {week} is in the past")
            return None
        if week_monday(week) > today + timedelta(days=MAX_HORIZON_DAYS):
            errors.append(f"iso_week {week} is more than {MAX_HORIZON_DAYS} days ahead")
            return None
        remaining, rate = await supply.remaining_capacity(conn, lane, week)
        if rate is None:
            errors.append(f"lane {lane} has no rate card for {week}")
            return None
        if rate.basis != "per_unit":
            errors.append(f"lane {lane} is booked per {rate.basis.removeprefix('per_')}: use its sailings")
            return None
        return remaining

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            return ItemPreview(errors=errors)
        lane, week, qty = parsed
        remaining = await self._capacity(conn, lane, week, sim.today, errors)
        if remaining is None:
            return ItemPreview(errors=errors)
        available = remaining - sim.capacity_drawn[(lane, week)]
        if qty > available:
            errors.append(f"insufficient capacity on {lane} in {week}: requested {qty}, available {max(available, 0)}")
        else:
            sim.capacity_drawn[(lane, week)] += qty
        rate = await supply.rate_on(conn, lane, week_monday(week))
        return ItemPreview(
            before={"lane_code": lane, "iso_week": week, "remaining_capacity": available},
            after={
                "capacity_booking": {"lane_code": lane, "iso_week": week, "qty_units": qty, "status": "confirmed"},
                "remaining_capacity": available - qty,
                "freight_cents": qty * int(rate.rate_cents) if rate else None,
            },
            errors=errors,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            raise _conflict(item, "invalid_payload")
        lane, week, qty = parsed
        await conn.execute(select(t.lane.c.code).where(t.lane.c.code == lane).with_for_update())
        remaining = await self._capacity(conn, lane, week, sim.today, errors)
        if remaining is None:
            raise _conflict(item, "invalid_payload")
        if qty > remaining - sim.capacity_drawn[(lane, week)]:
            raise _conflict(item, "insufficient_capacity", ref=f"{lane}/{week}")
        sim.capacity_drawn[(lane, week)] += qty
        return parsed

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        lane, week, qty = await self._lock(conn, item, Simulation(ctx.today))
        n = (await conn.execute(text("SELECT nextval('merxa.capacity_booking_code_seq')"))).scalar_one()
        code = f"CB-{n:04d}"
        version = (
            await conn.execute(
                t.capacity_booking.insert()
                .values(
                    code=code,
                    lane_code=lane,
                    iso_week=week,
                    qty_units=qty,
                    basis="per_unit",
                    status="confirmed",
                    created_via=ctx.created_via,
                )
                .returning(t.capacity_booking.c.version)
            )
        ).scalar_one()
        ctx.results[item.seq] = {"code": code, "version": version}
        return [("capacity_booking", code, "create", None, {"lane_code": lane, "iso_week": week, "qty_units": qty})]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "code", "version")
        return [
            {"op": "revert.capacity_booking", "target_ref": r["code"], "expected_version": r["version"], "payload": {}}
        ]


class RevertCapacityBooking(_Base):
    op = "revert.capacity_booking"

    async def _row(self, conn: AsyncConnection, item: ItemSpec, *, lock: bool) -> Any:
        q = select(t.capacity_booking).where(t.capacity_booking.c.code == item.target_ref)
        return (await conn.execute(q.with_for_update() if lock else q)).first()

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        row = await self._row(conn, item, lock=False)
        if row is None:
            return ItemPreview(errors=[f"capacity booking {item.target_ref} not found"])
        errors = [] if row.version == item.expected_version else [f"version_mismatch: current {row.version}"]
        return ItemPreview(
            before={"status": row.status, "version": row.version},
            after={"status": "released", "version": row.version + 1},
            errors=errors,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        row = await self._row(conn, item, lock=True)
        if row is None:
            raise _conflict(item, "not_found")
        if row.version != item.expected_version:
            raise _conflict(item, "version_mismatch", row.version)
        if row.status == "released":
            raise _conflict(item, "status_changed", row.version)
        return row

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        row = await self._lock(conn, item, Simulation(ctx.today))
        await conn.execute(
            update(t.capacity_booking).where(t.capacity_booking.c.code == row.code).values(status="released")
        )
        ctx.results[item.seq] = {}
        return [("capacity_booking", row.code, "release", {"status": row.status}, {"status": "released"})]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── capacity_booking.cancel (M5) ─────────────────────────
async def _booking(conn: AsyncConnection, code: str | None, *, lock: bool = False) -> Any:
    q = select(t.capacity_booking).where(t.capacity_booking.c.code == code)
    return (await conn.execute(q.with_for_update() if lock else q)).first()


async def _booking_shipments(conn: AsyncConnection, code: str) -> list[str]:
    sh = t.shipment
    rows = await conn.execute(
        select(sh.c.code).where(sh.c.booking_code == code, sh.c.status != "cancelled").order_by(sh.c.code)
    )
    return [r.code for r in rows]


def _week_past(week: str, today: date) -> bool:
    return week_monday(week) + timedelta(days=6) < today


class CapacityBookingCancel(_Base):
    """Release a held/confirmed capacity booking (docs/contracts/95 §4: the re-plan's delta
    change set cancels bookings on a lane whose capacity was cut).

    target = booking code, ``expected_version`` required, payload ``{}``. The freed per-unit
    capacity is credited to the running simulation, so a later ``capacity_booking.create`` /
    ``po_line.split`` in the same change set may use it. Shipments that reference the booking
    are listed in the preview (``linked_shipments``) and left unchanged. Rollback restores the
    previous status (``revert.capacity_booking_status``)."""

    op = "capacity_booking.cancel"

    def _parse(self, item: ItemSpec, errors: list[str]) -> bool:
        if not item.target_ref:
            errors.append("target_ref (capacity booking code) is required")
        if item.expected_version is None:
            errors.append("expected_version is required")
        _unknown_keys(item.payload, frozenset(), errors)
        return not errors

    @staticmethod
    def _credit(sim: Simulation, row: Any) -> None:
        if row.basis == "per_unit":
            sim.capacity_drawn[(row.lane_code, row.iso_week)] -= int(row.qty_units)

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        if not self._parse(item, errors):
            return ItemPreview(errors=errors)
        ref = str(item.target_ref)
        row = await _booking(conn, ref)
        if row is None:
            return ItemPreview(errors=[f"capacity booking {ref} not found"])
        if ref in sim.touched:
            errors.append(f"capacity booking {ref} is changed by an earlier item")
        sim.touched.add(ref)
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if row.status not in CANCELLABLE_BOOKING:
            errors.append(f"capacity booking status {row.status!r} cannot be cancelled")
        if _week_past(row.iso_week, sim.today):
            errors.append(f"capacity booking week {row.iso_week} is in the past")
        remaining, _rate = await supply.remaining_capacity(conn, row.lane_code, row.iso_week)
        available = None if remaining is None else remaining - sim.capacity_drawn[(row.lane_code, row.iso_week)]
        if not errors:
            self._credit(sim, row)
        freed = int(row.qty_units) if row.basis == "per_unit" else 0
        before = {
            "code": row.code,
            "lane_code": row.lane_code,
            "iso_week": row.iso_week,
            "qty_units": row.qty_units,
            "status": row.status,
            "version": row.version,
            "remaining_capacity": available,
        }
        after = {
            **before,
            "status": "released",
            "version": row.version + 1,
            "remaining_capacity": None if available is None else available + freed,
            "linked_shipments": await _booking_shipments(conn, row.code),
        }
        return ItemPreview(before=before, after=after, errors=errors)

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        if not self._parse(item, []):
            raise _conflict(item, "invalid_payload")
        row = await _booking(conn, item.target_ref, lock=True)
        if row is None:
            raise _conflict(item, "not_found")
        if row.version != item.expected_version:
            raise _conflict(item, "version_mismatch", row.version)
        if row.status not in CANCELLABLE_BOOKING:
            raise _conflict(item, "status_changed", row.version)
        if _week_past(row.iso_week, sim.today):
            raise _conflict(item, "booking_week_past", row.version)
        self._credit(sim, row)
        return row

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        row = await self._lock(conn, item, Simulation(ctx.today))
        cb = t.capacity_booking
        version = (
            await conn.execute(
                update(cb)
                .where(cb.c.code == row.code, cb.c.version == row.version)
                .values(status="released")
                .returning(cb.c.version)
            )
        ).scalar_one_or_none()
        if version is None:
            raise _conflict(item, "version_mismatch")
        ctx.results[item.seq] = {"code": row.code, "version": int(version), "previous": {"status": row.status}}
        return [
            (
                "capacity_booking",
                row.code,
                "cancel",
                {"status": row.status, "version": row.version},
                {
                    "status": "released",
                    "version": int(version),
                    "lane_code": row.lane_code,
                    "iso_week": row.iso_week,
                    "qty_units": row.qty_units,
                },
            )
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {
                "op": "revert.capacity_booking_status",
                "target_ref": item.target_ref,
                "expected_version": r["version"],
                "payload": {"status": r["previous"]["status"]},
            }
        ]


class RevertCapacityBookingStatus(_Base):
    """Compensation of ``capacity_booking.cancel``: back to the status before the cancel.

    Restores the exact earlier state (no capacity check: the booking held that capacity
    before); any change to the booking since the cancel is a version conflict."""

    op = "revert.capacity_booking_status"

    def _status(self, item: ItemSpec, errors: list[str]) -> str | None:
        _unknown_keys(item.payload, frozenset({"status"}), errors)
        status = _str(item.payload, "status", errors)
        if status is not None and status not in CANCELLABLE_BOOKING:
            errors.append("payload.status must be held or confirmed")
            return None
        return status

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        status = self._status(item, errors)
        row = await _booking(conn, item.target_ref)
        if row is None:
            return ItemPreview(errors=[f"capacity booking {item.target_ref} not found"])
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if row.status != "released":
            errors.append(f"capacity booking status {row.status!r} is not released")
        return ItemPreview(
            before={"status": row.status, "version": row.version},
            after={"status": status, "version": row.version + 1},
            errors=errors,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        row = await _booking(conn, item.target_ref, lock=True)
        if row is None:
            raise _conflict(item, "not_found")
        if row.version != item.expected_version:
            raise _conflict(item, "version_mismatch", row.version)
        if row.status != "released":
            raise _conflict(item, "status_changed", row.version)
        return row

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        status = self._status(item, errors)
        row = await self._lock(conn, item, Simulation(ctx.today))
        if status is None:
            raise _conflict(item, "invalid_payload", row.version)
        cb = t.capacity_booking
        version = (
            await conn.execute(
                update(cb)
                .where(cb.c.code == row.code, cb.c.version == row.version)
                .values(status=status)
                .returning(cb.c.version)
            )
        ).scalar_one_or_none()
        if version is None:
            raise _conflict(item, "version_mismatch")
        ctx.results[item.seq] = {"version": int(version)}
        return [("capacity_booking", row.code, "restore", {"status": row.status}, {"status": status})]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


class RevertTransferOrder(_Base):
    """Cancel a planned transfer order and release its stock effects."""

    op = "revert.transfer_order"

    async def _rows(self, conn: AsyncConnection, item: ItemSpec, *, lock: bool) -> tuple[Any, list[Any]]:
        q = select(t.transfer_order).where(t.transfer_order.c.code == item.target_ref)
        lq = select(t.transfer_order_line).where(t.transfer_order_line.c.to_code == item.target_ref)
        if lock:
            q = q.with_for_update()
        return (await conn.execute(q)).first(), list(await conn.execute(lq.order_by(t.transfer_order_line.c.line_no)))

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        to, lines = await self._rows(conn, item, lock=False)
        if to is None:
            return ItemPreview(errors=[f"transfer order {item.target_ref} not found"])
        errors = []
        if to.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {to.version}")
        if to.status != "planned":
            errors.append(f"transfer order status {to.status!r} cannot be cancelled")
        units = sum(ln.qty for ln in lines)
        return ItemPreview(
            before={"status": to.status, "from_dc": to.from_dc, "to_dc": to.to_dc, "units": units},
            after={"status": "cancelled", "from_dc": to.from_dc, "to_dc": to.to_dc, "units": units},
            errors=errors,
            units_moved=units,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        to, lines = await self._rows(conn, item, lock=True)
        if to is None:
            raise _conflict(item, "not_found")
        if to.version != item.expected_version:
            raise _conflict(item, "version_mismatch", to.version)
        if to.status != "planned":
            raise _conflict(item, "status_changed", to.version)
        return to, lines

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        to, lines = await self._lock(conn, item, Simulation(ctx.today))
        ip = t.inventory_position
        audit: list[AuditEntry] = [
            ("transfer_order", to.code, "cancel", {"status": to.status}, {"status": "cancelled"})
        ]
        try:
            for ln in lines:
                await conn.execute(
                    update(ip)
                    .where(ip.c.location_type == "dc", ip.c.location_code == to.from_dc, ip.c.sku_code == ln.sku_code)
                    .values(reserved=ip.c.reserved - ln.qty)
                )
                await conn.execute(
                    update(ip)
                    .where(ip.c.location_type == "dc", ip.c.location_code == to.to_dc, ip.c.sku_code == ln.sku_code)
                    .values(in_transit=ip.c.in_transit - ln.qty)
                )
                audit.append(
                    (
                        "inventory_position",
                        f"dc/{to.from_dc}/{ln.sku_code}",
                        "unreserve",
                        None,
                        {"reserved": f"-{ln.qty}"},
                    )
                )
                audit.append(
                    (
                        "inventory_position",
                        f"dc/{to.to_dc}/{ln.sku_code}",
                        "in_transit",
                        None,
                        {"in_transit": f"-{ln.qty}"},
                    )
                )
        except IntegrityError as exc:
            raise _conflict(item, "stock_changed", to.version) from exc
        await conn.execute(
            update(t.transfer_order).where(t.transfer_order.c.code == to.code).values(status="cancelled")
        )
        ctx.results[item.seq] = {}
        return audit

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── production_order.resequence ─────────────────────────
class ProductionOrderResequence(_Base):
    op = "production_order.resequence"

    def _parse(self, item: ItemSpec, errors: list[str]) -> tuple[str, list[str]] | None:
        _unknown_keys(item.payload, frozenset({"line_code", "order_codes"}), errors)
        line = _str(item.payload, "line_code", errors)
        codes = item.payload.get("order_codes")
        if not isinstance(codes, list) or not codes or not all(isinstance(c, str) and c for c in codes):
            errors.append("payload.order_codes must be a non-empty array of production order codes")
            codes = None
        elif len(set(codes)) != len(codes):
            errors.append("payload.order_codes has duplicates")
        if item.target_ref is not None and line is not None and item.target_ref != line:
            errors.append("target_ref must be null or equal payload.line_code")
        if errors or line is None or codes is None:
            return None
        return line, codes

    async def _load(self, conn: AsyncConnection, line: str, *, lock: bool) -> tuple[Any, list[Any]]:
        fq = select(t.factory_line).where(t.factory_line.c.code == line)
        po = t.production_order
        oq = select(po).where(po.c.line_code == line).order_by(po.c.seq)
        if lock:
            fq, oq = fq.with_for_update(), oq.with_for_update()
        return (await conn.execute(fq)).first(), list(await conn.execute(oq))

    @staticmethod
    def _schedule(movable: list[Any], new_order: list[str]) -> list[dict[str, Any]]:
        by_code = {o.code: o for o in movable}
        anchor = min(o.planned_start for o in movable)
        seqs = sorted(o.seq for o in movable)
        out = []
        cur = anchor
        for seq, code in zip(seqs, new_order, strict=True):
            o = by_code[code]
            days = (o.planned_end - o.planned_start).days
            out.append({"code": code, "seq": seq, "planned_start": cur, "planned_end": cur + timedelta(days=days)})
            cur = cur + timedelta(days=days + 1)
        return out

    async def _plan(
        self, conn: AsyncConnection, item: ItemSpec, errors: list[str], *, lock: bool = False
    ) -> tuple[Any, list[Any], list[dict[str, Any]]] | None:
        parsed = self._parse(item, errors)
        if parsed is None:
            return None
        line, codes = parsed
        fl, orders = await self._load(conn, line, lock=lock)
        if fl is None:
            errors.append(f"factory line {line} not found")
            return None
        if item.expected_version is not None and fl.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {fl.version}")
        movable = [o for o in orders if o.status in MOVABLE_ORDER]
        known = {o.code for o in orders}
        unknown = [c for c in codes if c not in known]
        if unknown:
            errors.append(f"orders not on line {line}: {unknown}")
        fixed = [c for c in codes if c in known and c not in {o.code for o in movable}]
        if fixed:
            errors.append(f"orders already running/done cannot move: {fixed}")
        if not unknown and not fixed and set(codes) != {o.code for o in movable}:
            missing = sorted({o.code for o in movable} - set(codes))
            errors.append(f"order_codes must list every planned/held order on {line}; missing {missing}")
        if errors:
            return None
        return fl, movable, self._schedule(movable, codes)

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        plan = await self._plan(conn, item, errors)
        if plan is None:
            return ItemPreview(errors=errors)
        fl, movable, new = plan
        if fl.code in sim.touched:
            return ItemPreview(errors=[f"factory line {fl.code} is changed by an earlier item"])
        sim.touched.add(fl.code)
        refs = {o.code: o.po_line_ref for o in movable}
        late = await self._late(conn, new, refs)
        before = [
            {"code": o.code, "seq": o.seq, "planned_start": o.planned_start, "planned_end": o.planned_end}
            for o in movable
        ]
        return ItemPreview(
            before={"line_code": fl.code, "version": fl.version, "orders": before},
            after={"line_code": fl.code, "version": fl.version + 1, "orders": new, "late_vs_ex_factory": late},
        )

    async def _late(self, conn: AsyncConnection, new: list[dict[str, Any]], refs: dict[str, str]) -> list[str]:
        po_codes = sorted({refs[o["code"]].rpartition("#")[0] for o in new})
        ex = {
            r.code: r.ex_factory_date
            for r in await conn.execute(
                select(t.purchase_order.c.code, t.purchase_order.c.ex_factory_date).where(
                    t.purchase_order.c.code.in_(po_codes)
                )
            )
        }
        return [
            o["code"]
            for o in new
            if (d := ex.get(refs[o["code"]].rpartition("#")[0])) is not None and o["planned_end"] > d
        ]

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        errors: list[str] = []
        plan = await self._plan(conn, item, errors, lock=True)
        if plan is None:
            reason = "version_mismatch" if any(e.startswith("version_mismatch") for e in errors) else "status_changed"
            raise _conflict(item, reason)
        return plan

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        fl, movable, new = await self._lock(conn, item, Simulation(ctx.today))
        po = t.production_order
        previous = [
            {
                "code": o.code,
                "seq": o.seq,
                "planned_start": o.planned_start.isoformat(),
                "planned_end": o.planned_end.isoformat(),
            }
            for o in movable
        ]
        audit: list[AuditEntry] = []
        old = {o.code: o for o in movable}
        for o in new:
            await conn.execute(
                update(po)
                .where(po.c.code == o["code"])
                .values(seq=o["seq"], planned_start=o["planned_start"], planned_end=o["planned_end"])
            )
            prev = old[o["code"]]
            if (prev.seq, prev.planned_start) != (o["seq"], o["planned_start"]):
                audit.append(
                    (
                        "production_order",
                        o["code"],
                        "resequence",
                        {"seq": prev.seq, "planned_start": prev.planned_start, "planned_end": prev.planned_end},
                        {"seq": o["seq"], "planned_start": o["planned_start"], "planned_end": o["planned_end"]},
                    )
                )
        version = (
            await conn.execute(
                update(t.factory_line)
                .where(t.factory_line.c.code == fl.code)
                .values(std_minutes_day=t.factory_line.c.std_minutes_day)
                .returning(t.factory_line.c.version)
            )
        ).scalar_one()
        ctx.results[item.seq] = {"version": version, "line_code": fl.code, "previous": previous}
        audit.append(("factory_line", fl.code, "resequence", {"version": fl.version}, {"version": version}))
        return audit

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "line_code", "previous")
        return [
            {
                "op": "revert.production_schedule",
                "target_ref": r["line_code"],
                "expected_version": r["version"],
                "payload": {"orders": r["previous"]},
            }
        ]


class RevertProductionSchedule(_Base):
    op = "revert.production_schedule"

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        fl = (await conn.execute(select(t.factory_line).where(t.factory_line.c.code == item.target_ref))).first()
        if fl is None:
            return ItemPreview(errors=[f"factory line {item.target_ref} not found"])
        errors = [] if fl.version == item.expected_version else [f"version_mismatch: current {fl.version}"]
        return ItemPreview(
            before={"line_code": fl.code, "version": fl.version},
            after={"line_code": fl.code, "orders": item.payload.get("orders")},
            errors=errors,
        )

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        fl = (
            await conn.execute(select(t.factory_line).where(t.factory_line.c.code == item.target_ref).with_for_update())
        ).first()
        if fl is None:
            raise _conflict(item, "not_found")
        if fl.version != item.expected_version:
            raise _conflict(item, "version_mismatch", fl.version)
        codes = [o["code"] for o in item.payload.get("orders") or []]
        po = t.production_order
        rows = list(await conn.execute(select(po.c.code, po.c.status).where(po.c.code.in_(codes)).with_for_update()))
        if len(rows) != len(codes) or any(r.status not in MOVABLE_ORDER for r in rows):
            raise _conflict(item, "status_changed", fl.version)
        return fl

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        fl = await self._lock(conn, item, Simulation(ctx.today))
        po = t.production_order
        audit: list[AuditEntry] = []
        for o in item.payload["orders"]:
            await conn.execute(
                update(po)
                .where(po.c.code == o["code"])
                .values(
                    seq=o["seq"],
                    planned_start=date.fromisoformat(o["planned_start"]),
                    planned_end=date.fromisoformat(o["planned_end"]),
                )
            )
            audit.append(("production_order", o["code"], "restore", None, o))
        await conn.execute(
            update(t.factory_line)
            .where(t.factory_line.c.code == fl.code)
            .values(std_minutes_day=t.factory_line.c.std_minutes_day)
        )
        ctx.results[item.seq] = {}
        return audit

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        raise NotImplementedError("compensating items are not reversible")


# ───────────────────────── promotion.reschedule / online_order.repromise ─────────────────────────
class _VersionedFieldOp(_Base):
    """Shared shape: target code + expected_version, version-checked single-row update."""

    table: ClassVar[Any] = None
    entity: ClassVar[str] = ""

    async def _row(self, conn: AsyncConnection, item: ItemSpec, *, lock: bool) -> Any:
        q = select(self.table).where(self.table.c.code == item.target_ref)
        return (await conn.execute(q.with_for_update() if lock else q)).first()

    def _common(self, item: ItemSpec, errors: list[str]) -> None:
        if not item.target_ref:
            errors.append(f"target_ref ({self.entity} code) is required")
        if item.expected_version is None:
            errors.append("expected_version is required")

    def _status_ok(self, row: Any) -> bool:
        return True

    async def _lock(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> Any:
        row = await self._row(conn, item, lock=True)
        if row is None:
            raise _conflict(item, "not_found")
        if row.version != item.expected_version:
            raise _conflict(item, "version_mismatch", row.version)
        if not self._status_ok(row):
            raise _conflict(item, "status_changed", row.version)
        return row

    async def _write(self, conn: AsyncConnection, item: ItemSpec, row: Any, values: dict[str, Any]) -> int:
        version = (
            await conn.execute(
                update(self.table)
                .where(self.table.c.code == row.code, self.table.c.version == row.version)
                .values(**values)
                .returning(self.table.c.version)
            )
        ).scalar_one_or_none()
        if version is None:
            raise _conflict(item, "version_mismatch")
        return int(version)


class PromotionReschedule(_VersionedFieldOp):
    op = "promotion.reschedule"
    table = t.promotion
    entity = "promotion"

    def _parse(self, item: ItemSpec, errors: list[str]) -> tuple[date, date] | None:
        self._common(item, errors)
        _unknown_keys(item.payload, frozenset({"period_start", "period_end"}), errors)
        start = _date(item.payload, "period_start", errors)
        end = _date(item.payload, "period_end", errors)
        if start and end and end < start:
            errors.append("period_end must not be before period_start")
        return None if errors or start is None or end is None else (start, end)

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            return ItemPreview(errors=errors)
        start, end = parsed
        _horizon(end, sim, "period_end", errors)
        _horizon(start, sim, "period_start", errors, allow_past=True)
        row = await self._row(conn, item, lock=False)
        if row is None:
            return ItemPreview(errors=[f"promotion {item.target_ref} not found"])
        if str(item.target_ref) in sim.touched:
            errors.append(f"promotion {item.target_ref} is changed by an earlier item")
        sim.touched.add(str(item.target_ref))
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        before = {
            "period_start": row.period.lower.isoformat(),
            "period_end": (row.period.upper - timedelta(days=1)).isoformat(),
            "version": row.version,
        }
        after = {"period_start": start.isoformat(), "period_end": end.isoformat(), "version": row.version + 1}
        return ItemPreview(before=before, after=after, errors=errors)

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        parsed = self._parse(item, [])
        row = await self._lock(conn, item, Simulation(ctx.today))
        if parsed is None:
            raise _conflict(item, "invalid_payload", row.version)
        start, end = parsed
        prev = {
            "period_start": row.period.lower.isoformat(),
            "period_end": (row.period.upper - timedelta(days=1)).isoformat(),
        }
        version = await self._write(conn, item, row, {"period": Range(start, end + timedelta(days=1), bounds="[)")})
        ctx.results[item.seq] = {"version": version, "previous": prev}
        return [
            (
                "promotion",
                row.code,
                "reschedule",
                {**prev, "version": row.version},
                {"period_start": start.isoformat(), "period_end": end.isoformat(), "version": version},
            )
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {"op": self.op, "target_ref": item.target_ref, "expected_version": r["version"], "payload": r["previous"]}
        ]


class OnlineOrderRepromise(_VersionedFieldOp):
    op = "online_order.repromise"
    table = t.online_order
    entity = "online order"

    def _status_ok(self, row: Any) -> bool:
        return row.status in REPROMISABLE_ORDER

    def _parse(self, item: ItemSpec, errors: list[str]) -> date | None:
        self._common(item, errors)
        _unknown_keys(item.payload, frozenset({"promise_date"}), errors)
        d = _date(item.payload, "promise_date", errors)
        return None if errors else d

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        d = self._parse(item, errors)
        if d is None:
            return ItemPreview(errors=errors)
        _horizon(d, sim, "promise_date", errors)
        row = await self._row(conn, item, lock=False)
        if row is None:
            return ItemPreview(errors=[f"online order {item.target_ref} not found"])
        if str(item.target_ref) in sim.touched:
            errors.append(f"online order {item.target_ref} is changed by an earlier item")
        sim.touched.add(str(item.target_ref))
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if not self._status_ok(row):
            errors.append(f"online order status {row.status!r} cannot be re-promised")
        before = {"promise_date": row.promise_date.isoformat(), "status": row.status, "version": row.version}
        return ItemPreview(
            before=before, after={**before, "promise_date": d.isoformat(), "version": row.version + 1}, errors=errors
        )

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        d = self._parse(item, [])
        row = await self._lock(conn, item, Simulation(ctx.today))
        if d is None:
            raise _conflict(item, "invalid_payload", row.version)
        version = await self._write(conn, item, row, {"promise_date": d})
        ctx.results[item.seq] = {"version": version, "previous": {"promise_date": row.promise_date.isoformat()}}
        return [
            (
                "online_order",
                row.code,
                "repromise",
                {"promise_date": row.promise_date, "version": row.version},
                {"promise_date": d, "version": version},
            )
        ]

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {"op": self.op, "target_ref": item.target_ref, "expected_version": r["version"], "payload": r["previous"]}
        ]


HANDLERS: list[Any] = [
    PoLineCancel(),
    PoCreate(),
    PoLineSplit(),
    CapacityBookingCreate(),
    CapacityBookingCancel(),
    ProductionOrderResequence(),
    PromotionReschedule(),
    OnlineOrderRepromise(),
    RevertTransferOrder(),
    RevertPoCreate(),
    RevertPoLineStatus(),
    RevertPoLineSplit(),
    RevertCapacityBooking(),
    RevertCapacityBookingStatus(),
    RevertProductionSchedule(),
]
