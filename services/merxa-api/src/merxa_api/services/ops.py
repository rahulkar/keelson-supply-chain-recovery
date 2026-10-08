"""Change-set operation handlers: validate/preview + apply with version checks + inverse.

Each handler implements two phases:

* ``preview`` — read-only. Validates the payload against current data and a running
  :class:`Simulation` (so several items drawing on the same DC stock are checked
  cumulatively) and returns before/after snapshots.
* ``apply`` — runs inside the single apply transaction. It locks its target rows,
  re-checks versions/ATP and raises :class:`Conflict` on any mismatch (the caller
  rolls the whole set back), otherwise mutates and returns audit entries. It records what
  it created / the versions it produced in ``ctx.results[seq]``.
* ``inverse`` — the compensating items that undo an applied item (used by rollback), built
  from the item, its stored ``result`` and ``before`` snapshot. Compensating items carry
  ``expected_version`` = the version the apply produced, so a rollback conflicts (instead of
  overwriting) when somebody changed the row since.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, ClassVar, Protocol

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t

AuditEntry = tuple[str, str, str, Any, Any]  # entity_type, entity_ref, action, before, after

MUTABLE_ALLOCATION = ("planned", "released")
RESCHEDULABLE_PO_LINE = ("open", "in_production", "shipped")
QTY_CHANGEABLE_PO_LINE = ("open", "in_production")
# Planning horizon for dates in payloads (also keeps date arithmetic far from date.max).
MAX_HORIZON_DAYS = 730


@dataclass
class ItemSpec:
    seq: int
    op: str
    target_ref: str | None
    expected_version: int | None
    payload: dict[str, Any]


@dataclass
class ItemPreview:
    before: Any = None
    after: Any = None
    errors: list[str] = field(default_factory=list)
    units_moved: int = 0
    value_cents: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ConflictInfo:
    seq: int
    target_ref: str | None
    expected_version: int | None
    actual_version: int | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "target_ref": self.target_ref,
            "expected_version": self.expected_version,
            "actual_version": self.actual_version,
            "reason": self.reason,
        }


class Conflict(Exception):
    def __init__(self, info: ConflictInfo) -> None:
        super().__init__(info.reason)
        self.info = info


@dataclass
class Simulation:
    """Cumulative effects of earlier items during validation (ATP drawn per DC × SKU,
    lane capacity drawn per lane × ISO week)."""

    today: date
    atp_drawn: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    capacity_drawn: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    touched: set[str] = field(default_factory=set)


@dataclass
class ApplyContext:
    change_set_code: str
    today: date
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    via: str | None = None  # ``created_via`` override (e.g. "mcp-lite")

    @property
    def created_via(self) -> str:
        return self.via or f"change_set:{self.change_set_code}"


InverseItem = dict[str, Any]  # {op, target_ref, expected_version, payload}


class NotReversible(Exception):
    """An applied item cannot be compensated (missing apply result / unsupported)."""


class OpHandler(Protocol):
    op: str

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview: ...

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None: ...

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]: ...

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]: ...


def _need(result: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    if not result or any(k not in result for k in keys):
        raise NotReversible("the item has no apply result (applied before rollback support?)")
    return result


# ───────────────────────── payload helpers ─────────────────────────
def _int(payload: Mapping[str, Any], key: str, errors: list[str], *, minimum: int | None = None) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"payload.{key} must be an integer")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"payload.{key} must be >= {minimum}")
        return None
    return value


def _date(payload: Mapping[str, Any], key: str, errors: list[str]) -> date | None:
    value = payload.get(key)
    if not isinstance(value, str):
        errors.append(f"payload.{key} must be an ISO date string (YYYY-MM-DD)")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"payload.{key} must be an ISO date string (YYYY-MM-DD)")
        return None


def _str(payload: Mapping[str, Any], key: str, errors: list[str]) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        errors.append(f"payload.{key} must be a non-empty string")
        return None
    return value


def _unknown_keys(payload: Mapping[str, Any], allowed: set[str] | frozenset[str], errors: list[str]) -> None:
    extra = sorted(set(payload) - allowed)
    if extra:
        errors.append(f"payload has unknown fields: {extra}")


def parse_po_line_ref(ref: str | None) -> tuple[str, int] | None:
    if not ref or "#" not in ref:
        return None
    po_code, _, line = ref.rpartition("#")
    if not po_code or not line.isdigit():
        return None
    return po_code, int(line)


async def _unit_cost(conn: AsyncConnection, sku_codes: list[str]) -> dict[str, int]:
    if not sku_codes:
        return {}
    rows = await conn.execute(
        select(t.sku.c.code, t.style.c.unit_cost_cents)
        .select_from(t.sku.join(t.style, t.style.c.code == t.sku.c.style_code))
        .where(t.sku.c.code.in_(sku_codes))
    )
    return {r.code: int(r.unit_cost_cents) for r in rows}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def transit_days(from_dc: Mapping[str, Any], to_dc: Mapping[str, Any]) -> int:
    """Road transit estimate between DCs: 500 km/day, +1 day for a Channel crossing."""
    km = _haversine_km(from_dc["lat"], from_dc["lon"], to_dc["lat"], to_dc["lon"])
    days = max(1, math.ceil(km / 500))
    if (from_dc["country"] == "GB") != (to_dc["country"] == "GB"):
        days += 1
    return days


# ───────────────────────── transfer_order.create ─────────────────────────
class TransferOrderCreate:
    op = "transfer_order.create"

    def _parse(self, item: ItemSpec, errors: list[str]) -> tuple[str, str, date, list[tuple[str, int]]] | None:
        if item.target_ref is not None:
            errors.append("target_ref must be null for transfer_order.create")
        if item.expected_version is not None:
            errors.append("expected_version must be null for transfer_order.create")
        p = item.payload
        _unknown_keys(p, {"from_dc", "to_dc", "ship_date", "lines"}, errors)
        from_dc = _str(p, "from_dc", errors)
        to_dc = _str(p, "to_dc", errors)
        ship_date = _date(p, "ship_date", errors)
        lines_raw = p.get("lines")
        lines: list[tuple[str, int]] = []
        if not isinstance(lines_raw, list) or not lines_raw:
            errors.append("payload.lines must be a non-empty array")
        else:
            seen: set[str] = set()
            for i, line in enumerate(lines_raw):
                if not isinstance(line, dict) or set(line) - {"sku", "qty"}:
                    errors.append(f"payload.lines[{i}] must be {{sku, qty}}")
                    continue
                sku = line.get("sku")
                qty = line.get("qty")
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
        if from_dc and to_dc and from_dc == to_dc:
            errors.append("from_dc and to_dc must differ")
        if errors or from_dc is None or to_dc is None or ship_date is None:
            return None
        return from_dc, to_dc, ship_date, lines

    async def _dcs(self, conn: AsyncConnection, codes: list[str]) -> dict[str, dict[str, Any]]:
        rows = await conn.execute(
            select(t.dc.c.code, t.dc.c.lat, t.dc.c.lon, t.dc.c.country).where(t.dc.c.code.in_(codes))
        )
        return {r.code: dict(r._mapping) for r in rows}

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            return ItemPreview(errors=errors)
        from_dc, to_dc, ship_date, lines = parsed
        dcs = await self._dcs(conn, [from_dc, to_dc])
        for code in (from_dc, to_dc):
            if code not in dcs:
                errors.append(f"unknown dc {code}")
        if ship_date < sim.today:
            errors.append(f"ship_date {ship_date.isoformat()} is in the past (today {sim.today.isoformat()})")
        elif ship_date > sim.today + timedelta(days=MAX_HORIZON_DAYS):
            errors.append(f"ship_date {ship_date.isoformat()} is more than {MAX_HORIZON_DAYS} days ahead")
        skus = [s for s, _ in lines]
        known = {r.code for r in await conn.execute(select(t.sku.c.code).where(t.sku.c.code.in_(skus)))}
        for s in skus:
            if s not in known:
                errors.append(f"unknown sku {s}")
        if errors:
            return ItemPreview(errors=errors)

        ip = t.inventory_position
        inv_rows = await conn.execute(
            select(
                ip.c.location_code, ip.c.sku_code, ip.c.on_hand, ip.c.allocated, ip.c.reserved, ip.c.in_transit
            ).where(ip.c.location_type == "dc", ip.c.location_code.in_([from_dc, to_dc]), ip.c.sku_code.in_(skus))
        )
        inv = {(r.location_code, r.sku_code): r for r in inv_rows}
        costs = await _unit_cost(conn, skus)
        before_lines, after_lines = [], []
        units = value = 0
        for sku, qty in lines:
            src = inv.get((from_dc, sku))
            dst = inv.get((to_dc, sku))
            atp = (src.on_hand - src.allocated - src.reserved) if src else 0
            available = atp - sim.atp_drawn[(from_dc, sku)]
            if qty > available:
                errors.append(
                    f"insufficient ATP for {sku} at {from_dc}: requested {qty}, available {max(available, 0)}"
                )
            to_in_transit = dst.in_transit if dst else 0
            before_lines.append({"sku": sku, "from_atp": available, "to_in_transit": to_in_transit})
            after_lines.append(
                {"sku": sku, "qty": qty, "from_atp": available - qty, "to_in_transit": to_in_transit + qty}
            )
            units += qty
            value += qty * costs.get(sku, 0)
        if not errors:
            for sku, qty in lines:
                sim.atp_drawn[(from_dc, sku)] += qty
        eta = ship_date + timedelta(days=transit_days(dcs[from_dc], dcs[to_dc]))
        return ItemPreview(
            before={"transfer_order": None, "from_dc": from_dc, "to_dc": to_dc, "lines": before_lines},
            after={
                "transfer_order": {
                    "from_dc": from_dc,
                    "to_dc": to_dc,
                    "ship_date": ship_date.isoformat(),
                    "eta_date": eta.isoformat(),
                    "status": "planned",
                },
                "from_dc": from_dc,
                "to_dc": to_dc,
                "lines": after_lines,
            },
            errors=errors,
            units_moved=units,
            value_cents=value,
        )

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None:
        """Lock source stock and verify ATP (cumulative across items) without mutating."""
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            return ConflictInfo(item.seq, item.target_ref, None, None, "invalid_payload")
        from_dc, _to_dc, _ship, lines = parsed
        ip = t.inventory_position
        rows = {
            r.sku_code: r
            for r in await conn.execute(
                select(ip.c.sku_code, ip.c.on_hand, ip.c.allocated, ip.c.reserved, ip.c.version)
                .where(
                    ip.c.location_type == "dc", ip.c.location_code == from_dc, ip.c.sku_code.in_([s for s, _ in lines])
                )
                .order_by(ip.c.sku_code)
                .with_for_update()
            )
        }
        for sku, qty in lines:
            row = rows.get(sku)
            atp = (row.on_hand - row.allocated - row.reserved) if row else 0
            if atp - sim.atp_drawn[(from_dc, sku)] < qty:
                return ConflictInfo(
                    item.seq, f"{from_dc}/{sku}", None, row.version if row else None, "insufficient_atp"
                )
        for sku, qty in lines:
            sim.atp_drawn[(from_dc, sku)] += qty
        return None

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        parsed = self._parse(item, errors)
        if parsed is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, None, None, "invalid_payload"))
        from_dc, to_dc, ship_date, lines = parsed
        dcs = await self._dcs(conn, [from_dc, to_dc])
        if len(dcs) != 2:
            raise Conflict(ConflictInfo(item.seq, None, None, None, "unknown_dc"))
        ip = t.inventory_position
        audit: list[AuditEntry] = []
        skus = [s for s, _ in lines]
        locked = {
            r.sku_code: r
            for r in await conn.execute(
                select(ip.c.sku_code, ip.c.on_hand, ip.c.allocated, ip.c.reserved, ip.c.version)
                .where(ip.c.location_type == "dc", ip.c.location_code == from_dc, ip.c.sku_code.in_(skus))
                .order_by(ip.c.sku_code)
                .with_for_update()
            )
        }
        for sku, qty in lines:
            row = locked.get(sku)
            atp = (row.on_hand - row.allocated - row.reserved) if row else 0
            if row is None or atp < qty:
                raise Conflict(
                    ConflictInfo(item.seq, f"{from_dc}/{sku}", None, row.version if row else None, "insufficient_atp")
                )

        seq_no = (await conn.execute(text("SELECT nextval('merxa.transfer_order_code_seq')"))).scalar_one()
        to_code = f"TO-{seq_no:06d}"
        eta = ship_date + timedelta(days=transit_days(dcs[from_dc], dcs[to_dc]))
        header = {
            "code": to_code,
            "from_dc": from_dc,
            "to_dc": to_dc,
            "ship_date": ship_date,
            "eta_date": eta,
            "status": "planned",
            "created_via": ctx.created_via,
        }
        to_version = (
            await conn.execute(t.transfer_order.insert().values(**header).returning(t.transfer_order.c.version))
        ).scalar_one()
        ctx.results[item.seq] = {"to_code": to_code, "version": to_version}
        await conn.execute(
            t.transfer_order_line.insert(),
            [
                {"to_code": to_code, "line_no": i, "sku_code": sku, "qty": qty}
                for i, (sku, qty) in enumerate(lines, start=1)
            ],
        )
        audit.append(
            (
                "transfer_order",
                to_code,
                "create",
                None,
                {**header, "lines": [{"sku": s, "qty": q} for s, q in lines]},
            )
        )
        for sku, qty in lines:
            row = locked[sku]
            await conn.execute(
                update(ip)
                .where(ip.c.location_type == "dc", ip.c.location_code == from_dc, ip.c.sku_code == sku)
                .values(reserved=ip.c.reserved + qty)
            )
            audit.append(
                (
                    "inventory_position",
                    f"dc/{from_dc}/{sku}",
                    "reserve",
                    {"reserved": row.reserved, "atp": row.on_hand - row.allocated - row.reserved},
                    {"reserved": row.reserved + qty, "atp": row.on_hand - row.allocated - row.reserved - qty},
                )
            )
            prev = (
                await conn.execute(
                    select(ip.c.in_transit).where(
                        ip.c.location_type == "dc", ip.c.location_code == to_dc, ip.c.sku_code == sku
                    )
                )
            ).scalar_one_or_none()
            stmt = pg_insert(ip).values(location_type="dc", location_code=to_dc, sku_code=sku, in_transit=qty)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ip.c.location_type, ip.c.location_code, ip.c.sku_code],
                set_={"in_transit": ip.c.in_transit + qty},
            )
            await conn.execute(stmt)
            audit.append(
                (
                    "inventory_position",
                    f"dc/{to_dc}/{sku}",
                    "in_transit",
                    {"in_transit": prev or 0},
                    {"in_transit": (prev or 0) + qty},
                )
            )
        return audit

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "to_code", "version")
        return [
            {"op": "revert.transfer_order", "target_ref": r["to_code"], "expected_version": r["version"], "payload": {}}
        ]


# ───────────────────────── allocation.set_qty ─────────────────────────
class AllocationSetQty:
    op = "allocation.set_qty"

    def _parse(self, item: ItemSpec, errors: list[str]) -> int | None:
        if not item.target_ref:
            errors.append("target_ref (allocation code) is required")
        if item.expected_version is None:
            errors.append("expected_version is required")
        _unknown_keys(item.payload, {"qty"}, errors)
        qty = _int(item.payload, "qty", errors, minimum=0)
        return None if errors else qty

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        qty = self._parse(item, errors)
        if qty is None:
            return ItemPreview(errors=errors)
        a = t.allocation
        row = (await conn.execute(select(a).where(a.c.code == item.target_ref))).first()
        if row is None:
            return ItemPreview(errors=[f"allocation {item.target_ref} not found"])
        if item.target_ref in sim.touched:
            errors.append(f"allocation {item.target_ref} is changed by an earlier item")
        if row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if row.status not in MUTABLE_ALLOCATION:
            errors.append(f"allocation status {row.status!r} cannot be changed")
        delta = qty - row.qty
        if row.status == "released" and delta > 0 and not errors:
            ip = t.inventory_position
            inv = (
                await conn.execute(
                    select(ip.c.on_hand, ip.c.allocated, ip.c.reserved).where(
                        ip.c.location_type == "dc", ip.c.location_code == row.dc_code, ip.c.sku_code == row.sku_code
                    )
                )
            ).first()
            atp = (inv.on_hand - inv.allocated - inv.reserved) if inv else 0
            available = atp - sim.atp_drawn[(row.dc_code, row.sku_code)]
            if delta > available:
                errors.append(
                    f"insufficient ATP at {row.dc_code} for {row.sku_code}: need {delta}, available {available}"
                )
            else:
                sim.atp_drawn[(row.dc_code, row.sku_code)] += delta
        sim.touched.add(str(item.target_ref))
        costs = await _unit_cost(conn, [row.sku_code])
        before = {
            "qty": row.qty,
            "status": row.status,
            "sku": row.sku_code,
            "dc_code": row.dc_code,
            "store_code": row.store_code,
            "ship_week": row.ship_week,
            "version": row.version,
        }
        after = {**before, "qty": qty, "version": row.version + 1}
        return ItemPreview(
            before=before,
            after=after,
            errors=errors,
            units_moved=abs(delta),
            value_cents=abs(delta) * costs.get(row.sku_code, 0),
        )

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None:
        errors: list[str] = []
        qty = self._parse(item, errors)
        if qty is None:
            return ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "invalid_payload")
        a = t.allocation
        row = (await conn.execute(select(a).where(a.c.code == item.target_ref).with_for_update())).first()
        if row is None:
            return ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "not_found")
        if row.version != item.expected_version:
            return ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "version_mismatch")
        if row.status not in MUTABLE_ALLOCATION:
            return ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "status_changed")
        delta = qty - row.qty
        if row.status == "released" and delta > 0:
            ip = t.inventory_position
            inv = (
                await conn.execute(
                    select(ip.c.on_hand, ip.c.allocated, ip.c.reserved)
                    .where(ip.c.location_type == "dc", ip.c.location_code == row.dc_code, ip.c.sku_code == row.sku_code)
                    .with_for_update()
                )
            ).first()
            atp = (inv.on_hand - inv.allocated - inv.reserved) if inv else 0
            if atp - sim.atp_drawn[(row.dc_code, row.sku_code)] < delta:
                return ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "insufficient_atp")
            sim.atp_drawn[(row.dc_code, row.sku_code)] += delta
        return None

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        qty = self._parse(item, errors)
        if qty is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "invalid_payload"))
        a = t.allocation
        row = (await conn.execute(select(a).where(a.c.code == item.target_ref).with_for_update())).first()
        if row is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "not_found"))
        if row.version != item.expected_version:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "version_mismatch")
            )
        if row.status not in MUTABLE_ALLOCATION:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "status_changed")
            )
        res = await conn.execute(
            update(a)
            .where(a.c.code == item.target_ref, a.c.version == item.expected_version)
            .values(qty=qty)
            .returning(a.c.version)
        )
        new_version = res.scalar_one_or_none()
        if new_version is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "version_mismatch"))
        ctx.results[item.seq] = {"version": new_version, "previous": {"qty": row.qty}}
        audit: list[AuditEntry] = [
            (
                "allocation",
                str(item.target_ref),
                "set_qty",
                {"qty": row.qty, "version": row.version},
                {"qty": qty, "version": new_version},
            )
        ]
        delta = qty - row.qty
        if row.status == "released" and delta:
            ip = t.inventory_position
            try:
                inv = (
                    await conn.execute(
                        update(ip)
                        .where(
                            ip.c.location_type == "dc", ip.c.location_code == row.dc_code, ip.c.sku_code == row.sku_code
                        )
                        .values(allocated=ip.c.allocated + delta)
                        .returning(ip.c.allocated)
                    )
                ).first()
            except IntegrityError as exc:
                raise Conflict(
                    ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "insufficient_atp")
                ) from exc
            if inv is not None:
                audit.append(
                    (
                        "inventory_position",
                        f"dc/{row.dc_code}/{row.sku_code}",
                        "allocate",
                        {"allocated": inv.allocated - delta},
                        {"allocated": inv.allocated},
                    )
                )
        return audit

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {
                "op": self.op,
                "target_ref": item.target_ref,
                "expected_version": r["version"],
                "payload": {"qty": r["previous"]["qty"]},
            }
        ]


# ───────────────────────── po_line.* ─────────────────────────
class _PoLineOp:
    op = ""
    allowed_status: ClassVar[tuple[str, ...]] = ()
    payload_keys: ClassVar[frozenset[str]] = frozenset()

    def _parse(self, item: ItemSpec, errors: list[str]) -> tuple[str, int] | None:
        ref = parse_po_line_ref(item.target_ref)
        if ref is None:
            errors.append("target_ref must be '<po_code>#<line_no>'")
        if item.expected_version is None:
            errors.append("expected_version is required")
        _unknown_keys(item.payload, self.payload_keys, errors)
        return ref

    async def _row(self, conn: AsyncConnection, ref: tuple[str, int], *, lock: bool = False) -> Any:
        pl = t.po_line
        q = select(pl).where(pl.c.po_code == ref[0], pl.c.line_no == ref[1])
        if lock:
            q = q.with_for_update()
        return (await conn.execute(q)).first()

    async def _common_preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation, errors: list[str]) -> Any:
        ref = self._parse(item, errors)
        if ref is None:
            return None
        row = await self._row(conn, ref)
        if row is None:
            errors.append(f"po line {item.target_ref} not found")
            return None
        if str(item.target_ref) in sim.touched:
            errors.append(f"po line {item.target_ref} is changed by an earlier item")
        sim.touched.add(str(item.target_ref))
        if item.expected_version is not None and row.version != item.expected_version:
            errors.append(f"version_mismatch: expected {item.expected_version}, current {row.version}")
        if row.status not in self.allowed_status:
            errors.append(f"po line status {row.status!r} does not allow {self.op}")
        return row

    async def _locked(self, conn: AsyncConnection, item: ItemSpec) -> Any:
        ref = parse_po_line_ref(item.target_ref)
        if ref is None or item.expected_version is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "invalid_payload"))
        row = await self._row(conn, ref, lock=True)
        if row is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "not_found"))
        if row.version != item.expected_version:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "version_mismatch")
            )
        if row.status not in self.allowed_status:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "status_changed")
            )
        return row

    async def check(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ConflictInfo | None:
        try:
            await self._locked(conn, item)
        except Conflict as exc:
            return exc.info
        return None

    def inverse(self, item: ItemSpec, result: dict[str, Any] | None, before: Any) -> list[InverseItem]:
        r = _need(result, "version", "previous")
        return [
            {"op": self.op, "target_ref": item.target_ref, "expected_version": r["version"], "payload": r["previous"]}
        ]

    async def _update(self, conn: AsyncConnection, item: ItemSpec, row: Any, values: dict[str, Any]) -> int:
        pl = t.po_line
        res = await conn.execute(
            update(pl)
            .where(pl.c.po_code == row.po_code, pl.c.line_no == row.line_no, pl.c.version == item.expected_version)
            .values(**values)
            .returning(pl.c.version)
        )
        version = res.scalar_one_or_none()
        if version is None:
            raise Conflict(ConflictInfo(item.seq, item.target_ref, item.expected_version, None, "version_mismatch"))
        return int(version)


class PoLineReschedule(_PoLineOp):
    op = "po_line.reschedule"
    allowed_status = RESCHEDULABLE_PO_LINE
    payload_keys = frozenset({"eta_dc"})

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        eta = _date(item.payload, "eta_dc", errors)
        row = await self._common_preview(conn, item, sim, errors)
        if row is None:
            return ItemPreview(errors=errors)
        if eta is not None and eta < sim.today:
            errors.append(f"eta_dc {eta.isoformat()} is in the past (today {sim.today.isoformat()})")
        elif eta is not None and eta > sim.today + timedelta(days=MAX_HORIZON_DAYS):
            errors.append(f"eta_dc {eta.isoformat()} is more than {MAX_HORIZON_DAYS} days ahead")
        before = {
            "eta_dc": row.eta_dc.isoformat(),
            "sku": row.sku_code,
            "qty_open": row.qty_ordered - row.qty_received,
            "status": row.status,
            "version": row.version,
        }
        after = {**before, "eta_dc": eta.isoformat() if eta else None, "version": row.version + 1}
        return ItemPreview(before=before, after=after, errors=errors)

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        eta = _date(item.payload, "eta_dc", errors)
        row = await self._locked(conn, item)
        if eta is None:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "invalid_payload")
            )
        version = await self._update(conn, item, row, {"eta_dc": eta})
        ctx.results[item.seq] = {"version": version, "previous": {"eta_dc": row.eta_dc.isoformat()}}
        return [
            (
                "po_line",
                str(item.target_ref),
                "reschedule",
                {"eta_dc": row.eta_dc.isoformat(), "version": row.version},
                {"eta_dc": eta.isoformat(), "version": version},
            )
        ]


class PoLineChangeQty(_PoLineOp):
    op = "po_line.change_qty"
    allowed_status = QTY_CHANGEABLE_PO_LINE
    payload_keys = frozenset({"qty"})

    async def preview(self, conn: AsyncConnection, item: ItemSpec, sim: Simulation) -> ItemPreview:
        errors: list[str] = []
        qty = _int(item.payload, "qty", errors, minimum=0)
        row = await self._common_preview(conn, item, sim, errors)
        if row is None:
            return ItemPreview(errors=errors)
        if qty is not None and qty < row.qty_received:
            errors.append(f"qty {qty} is below qty_received {row.qty_received}")
        delta = abs((qty if qty is not None else row.qty_ordered) - row.qty_ordered)
        costs = await _unit_cost(conn, [row.sku_code])
        before = {
            "qty_ordered": row.qty_ordered,
            "qty_received": row.qty_received,
            "sku": row.sku_code,
            "status": row.status,
            "version": row.version,
        }
        after = {**before, "qty_ordered": qty, "version": row.version + 1}
        return ItemPreview(
            before=before, after=after, errors=errors, units_moved=delta, value_cents=delta * costs.get(row.sku_code, 0)
        )

    async def apply(self, conn: AsyncConnection, item: ItemSpec, ctx: ApplyContext) -> list[AuditEntry]:
        errors: list[str] = []
        qty = _int(item.payload, "qty", errors, minimum=0)
        row = await self._locked(conn, item)
        if qty is None or qty < row.qty_received:
            raise Conflict(
                ConflictInfo(item.seq, item.target_ref, item.expected_version, row.version, "invalid_payload")
            )
        version = await self._update(conn, item, row, {"qty_ordered": qty})
        ctx.results[item.seq] = {"version": version, "previous": {"qty": row.qty_ordered}}
        return [
            (
                "po_line",
                str(item.target_ref),
                "change_qty",
                {"qty_ordered": row.qty_ordered, "version": row.version},
                {"qty_ordered": qty, "version": version},
            )
        ]


def _handlers() -> dict[str, OpHandler]:
    from merxa_api.services import ops_m2

    handlers: list[OpHandler] = [TransferOrderCreate(), AllocationSetQty(), PoLineReschedule(), PoLineChangeQty()]
    handlers.extend(ops_m2.HANDLERS)
    return {h.op: h for h in handlers}


HANDLERS: dict[str, OpHandler] = _handlers()
# Ops clients may add to change sets (``revert.*`` ops only appear in compensating sets).
PUBLIC_OPS: tuple[str, ...] = tuple(op for op in HANDLERS if not op.startswith("revert."))
SUPPORTED_OPS: tuple[str, ...] = PUBLIC_OPS
