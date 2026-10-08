"""Demo director (DEMO_MODE=1 + role merxa-admin): reset, inject, advance clock."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from merxa_api import tables as t
from merxa_api.calendar import is_iso_week, iso_week, week_monday
from merxa_api.config import Settings
from merxa_api.errors import Problem
from merxa_api.security.principal import Principal
from merxa_api.seed.generator import TRIM_DELAY_MESSAGE, TYPHOON_MESSAGE
from merxa_api.seed.loader import golden, seed
from merxa_api.services import audit, events, supply
from merxa_api.services.audit import Attribution
from merxa_api.services.common import jsonable

INJECT_KINDS = (
    "supplier_message",
    "trim_delay",
    "po_eta_change",
    "inventory_adjust",
    "port_closure",
    "typhoon",
    "lane_capacity",
)
VIA = "Demo director"
MAX_SHIFT_DAYS = 365  # po_eta_change: |days| (keeps ETAs inside the date range and the demo horizon)
# supplier_message: free-text limits (the webhook body must stay well under a receiver's
# 256 KB cap — docs/contracts/70 §5 — even when every character needs 4 bytes).
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 20_000
MAX_REASON_CHARS = 500  # inventory_adjust: payload.reason
# lane_capacity (M5, docs/contracts/95 §4): factor in basis points of the capacity in force
# (3000 = cut to 30 %, 0 = closed, 15000 = +50 %), for 1..52 ISO weeks.
MAX_FACTOR_BP = 100_000
MAX_CAPACITY_WEEKS = 52
MAX_NOTE_CHARS = 2_000
MAX_HORIZON_DAYS = 730
PG_INT_MAX = 2_147_483_647


def _utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class AdminService:
    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def _attr(self, principal: Principal) -> Attribution:
        return Attribution(
            actor_sub=principal.sub,
            actor_username=principal.username,
            actor_display=principal.actor_display,
            client_id=principal.client_id,
            via=VIA,
        )

    async def reset(self, principal: Principal) -> dict[str, Any]:
        """Restore the golden seed: one transaction (TRUNCATE + load) under the seed advisory
        lock, so concurrent readers see either the old or the new data. The append-only audit
        log and UI sessions survive; code sequences keep counting (codes are never reused).
        The same transaction records the ``scenario.reset`` outbox event (delivered to webhook
        subscribers as ``merxa.scenario.reset``, docs/contracts/90 §10; see ``seed()``)."""
        started = time.perf_counter()
        gs = await asyncio.to_thread(golden, self.settings.seed_profile)  # cached after the first call
        fingerprint = gs.fingerprint
        counts = await seed(self.engine, self.settings.seed_profile, reset_first=True)
        duration_ms = round((time.perf_counter() - started) * 1000)
        async with self.engine.begin() as conn:
            await audit.write(
                conn,
                self._attr(principal),
                entity_type="scenario",
                entity_ref=self.settings.seed_profile,
                action="reset",
                after={"counts": counts, "dataset_sha256": fingerprint},
            )
        return {
            "status": "reset",
            "profile": self.settings.seed_profile,
            "counts": counts,
            "dataset_sha256": fingerprint,
            "duration_ms": duration_ms,
        }

    async def advance_clock(self, principal: Principal, days: Any) -> dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise Problem(400, "invalid_parameter", "days must be an integer between 1 and 90")
        sc = t.sim_clock
        async with self.engine.begin() as conn:
            before = (await conn.execute(select(sc.c.today).with_for_update())).scalar_one()
            after = before + timedelta(days=days)
            await conn.execute(update(sc).values(today=after))
            await audit.write(
                conn,
                self._attr(principal),
                entity_type="sim_clock",
                entity_ref="today",
                action="advance",
                before={"today": before},
                after={"today": after},
            )
        return {"today": after.isoformat(), "iso_week": iso_week(after), "previous": before.isoformat()}

    async def inject(self, principal: Principal, kind: Any, payload: Any) -> dict[str, Any]:
        if kind not in INJECT_KINDS:
            raise Problem(400, "invalid_parameter", f"kind must be one of {list(INJECT_KINDS)}")
        payload = payload or {}
        if not isinstance(payload, dict):
            raise Problem(400, "invalid_parameter", "payload must be an object")
        if kind in ("supplier_message", "trim_delay"):
            return await self._supplier_message(
                principal, TRIM_DELAY_MESSAGE | payload if kind == "trim_delay" else payload
            )
        if kind == "po_eta_change":
            return await self._po_eta_change(principal, payload)
        if kind in ("port_closure", "typhoon"):
            return await self._port_closure(principal, payload, with_message=kind == "typhoon")
        if kind == "lane_capacity":
            return await self._lane_capacity(principal, payload)
        return await self._inventory_adjust(principal, payload)

    async def _supplier_message(self, principal: Principal, p: dict[str, Any]) -> dict[str, Any]:
        """Insert a supplier-portal message with the caller's subject/body **verbatim** (evals
        inject adversarial text this way). It reaches ``list_supplier_messages`` and the
        ``supplier_message.received`` event/webhook, both flagged as untrusted text."""
        for field, limit in (("supplier_code", 64), ("subject", MAX_SUBJECT_CHARS), ("body", MAX_BODY_CHARS)):
            value = p.get(field)
            if not isinstance(value, str) or not value.strip():
                raise Problem(400, "invalid_parameter", f"payload.{field} is required")
            if len(value) > limit:
                raise Problem(400, "invalid_parameter", f"payload.{field} must be at most {limit} characters")
            # Postgres text cannot hold NUL; a lone surrogate cannot be encoded as UTF-8.
            if "\x00" in value or not _utf8_encodable(value):
                raise Problem(400, "invalid_parameter", f"payload.{field} contains characters that cannot be stored")
        async with self.engine.begin() as conn:
            exists = (
                await conn.execute(select(t.supplier.c.code).where(t.supplier.c.code == p["supplier_code"]))
            ).first()
            if exists is None:
                raise Problem(404, "not_found", f"supplier {p['supplier_code']!r} not found")
            row = (
                await conn.execute(
                    t.supplier_message.insert()
                    .values(
                        supplier_code=p["supplier_code"],
                        subject=p["subject"],
                        body=p["body"],
                        received_at=datetime.now(UTC),
                        status="new",
                    )
                    .returning(t.supplier_message.c.id, t.supplier_message.c.received_at)
                )
            ).one()
            await audit.write(
                conn,
                self._attr(principal),
                entity_type="supplier_message",
                entity_ref=str(row.id),
                action="inject",
                after={"supplier_code": p["supplier_code"], "subject": p["subject"]},
            )
            await events.emit(
                conn,
                "supplier_message.received",
                {
                    "id": str(row.id),
                    "supplier_code": p["supplier_code"],
                    "received_at": row.received_at,
                    "subject": p["subject"],
                    "body": p["body"],
                },
            )
        return {
            "kind": "supplier_message",
            "id": str(row.id),
            "supplier_code": p["supplier_code"],
            "received_at": jsonable(row.received_at),
        }

    async def _po_eta_change(self, principal: Principal, p: dict[str, Any]) -> dict[str, Any]:
        po_code = p.get("po_code")
        if not isinstance(po_code, str):
            raise Problem(400, "invalid_parameter", "payload.po_code is required")
        days, eta = p.get("days"), p.get("eta_dc")
        if (days is None) == (eta is None):
            raise Problem(400, "invalid_parameter", "give exactly one of payload.days or payload.eta_dc")
        if days is not None and (
            isinstance(days, bool) or not isinstance(days, int) or not -MAX_SHIFT_DAYS <= days <= MAX_SHIFT_DAYS
        ):
            raise Problem(
                400,
                "invalid_parameter",
                f"payload.days must be an integer between -{MAX_SHIFT_DAYS} and {MAX_SHIFT_DAYS}",
            )
        new_eta: date | None = None
        if eta is not None:
            try:
                new_eta = date.fromisoformat(str(eta))
            except ValueError as exc:
                raise Problem(400, "invalid_parameter", "payload.eta_dc must be YYYY-MM-DD") from exc
        line_nos = p.get("line_nos")
        if line_nos is not None and (
            not isinstance(line_nos, list)
            or not all(isinstance(n, int) and not isinstance(n, bool) and 0 < n <= 1_000_000 for n in line_nos)
        ):
            raise Problem(400, "invalid_parameter", "payload.line_nos must be an array of line numbers")
        pl = t.po_line
        changed: list[dict[str, Any]] = []
        async with self.engine.begin() as conn:
            q = select(pl).where(pl.c.po_code == po_code, pl.c.status.in_(("open", "in_production", "shipped")))
            if line_nos:
                q = q.where(pl.c.line_no.in_(line_nos))
            rows = list(await conn.execute(q.order_by(pl.c.line_no).with_for_update()))
            if not rows:
                raise Problem(404, "not_found", f"no open lines on purchase order {po_code!r}")
            entries = []
            for r in rows:
                target = new_eta or (r.eta_dc + timedelta(days=int(days)))
                await conn.execute(update(pl).where(pl.c.id == r.id).values(eta_dc=target))
                entries.append(
                    ("po_line", f"{po_code}#{r.line_no}", "eta_change", {"eta_dc": r.eta_dc}, {"eta_dc": target})
                )
                changed.append({"line_no": r.line_no, "eta_dc": target.isoformat()})
            po = t.purchase_order
            max_eta = max(date.fromisoformat(c["eta_dc"]) for c in changed)
            header = (await conn.execute(select(po.c.eta_dc).where(po.c.code == po_code))).scalar_one()
            if max_eta > header:
                await conn.execute(update(po).where(po.c.code == po_code).values(eta_dc=max_eta))
                entries.append(("purchase_order", po_code, "eta_change", {"eta_dc": header}, {"eta_dc": max_eta}))
            await audit.write_many(conn, self._attr(principal), entries)
            await events.emit(
                conn,
                "po_line.eta_changed",
                {"po_code": po_code, "lines": changed, "source": "supplier"},
                await events.changes_for(conn, entries),
            )
        return {"kind": "po_eta_change", "po_code": po_code, "lines": changed}

    async def _port_closure(self, principal: Principal, p: dict[str, Any], *, with_message: bool) -> dict[str, Any]:
        """Close a port for N days: sailings departing it within the window leave N days later;
        their shipments and the PO lines on them arrive N days later."""
        unlocode = p.get("unlocode", "VNSGN")
        days = p.get("days", 10)
        if not isinstance(unlocode, str) or isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
            raise Problem(400, "invalid_parameter", "payload needs unlocode (string) and days (1..60)")
        sa, ln, sh, sl, pl = t.sailing, t.lane, t.shipment, t.shipment_line, t.po_line
        async with self.engine.begin() as conn:
            if (await conn.execute(select(t.port.c.unlocode).where(t.port.c.unlocode == unlocode))).first() is None:
                raise Problem(404, "not_found", f"port {unlocode!r} not found")
            today = (await conn.execute(select(t.sim_clock.c.today))).scalar_one()
            start = today
            if p.get("from") is not None:
                try:
                    start = date.fromisoformat(p["from"]) if isinstance(p["from"], str) else None
                except ValueError:
                    start = None
                if start is None:
                    raise Problem(400, "invalid_parameter", "payload.from must be a date (YYYY-MM-DD)")
            end = start + timedelta(days=days)
            sailings = list(
                await conn.execute(
                    select(sa)
                    .select_from(sa.join(ln, ln.c.code == sa.c.lane_code))
                    .where(ln.c.origin == unlocode, sa.c.status == "scheduled", sa.c.etd >= start, sa.c.etd < end)
                    .order_by(sa.c.code)
                    .with_for_update(of=sa)
                )
            )
            entries: list[tuple[str, str, str, Any, Any]] = []
            line_changes: list[dict[str, Any]] = []
            delta = timedelta(days=days)
            # A PO (line) may travel in several shipments on the closed sailings: shift it once.
            shifted_refs: set[str] = set()
            delayed_pos: list[str] = []
            for s_row in sailings:
                await conn.execute(
                    update(sa)
                    .where(sa.c.code == s_row.code)
                    .values(cutoff=s_row.cutoff + delta, etd=s_row.etd + delta, eta=s_row.eta + delta)
                )
                entries.append(
                    (
                        "sailing",
                        s_row.code,
                        "delay",
                        {"etd": s_row.etd, "eta": s_row.eta},
                        {"etd": s_row.etd + delta, "eta": s_row.eta + delta},
                    )
                )
                for ship in await conn.execute(
                    select(sh)
                    .where(sh.c.sailing_code == s_row.code, sh.c.status != "cancelled")
                    .order_by(sh.c.code)
                    .with_for_update()
                ):
                    await conn.execute(
                        update(sh).where(sh.c.code == ship.code).values(etd=ship.etd + delta, eta=ship.eta + delta)
                    )
                    entries.append(("shipment", ship.code, "delay", {"eta": ship.eta}, {"eta": ship.eta + delta}))
                    if ship.po_code not in delayed_pos:
                        delayed_pos.append(ship.po_code)
                    refs = [
                        r.po_line_ref
                        for r in await conn.execute(
                            select(sl.c.po_line_ref).where(sl.c.shipment_code == ship.code).order_by(sl.c.line_no)
                        )
                    ]
                    for ref in refs:
                        if ref in shifted_refs:
                            continue
                        shifted_refs.add(ref)
                        po_code, _, line_no = ref.rpartition("#")
                        row = (
                            await conn.execute(
                                select(pl.c.eta_dc)
                                .where(
                                    pl.c.po_code == po_code,
                                    pl.c.line_no == int(line_no),
                                    pl.c.status.in_(("open", "in_production", "shipped")),
                                )
                                .with_for_update()
                            )
                        ).first()
                        if row is None:
                            continue
                        await conn.execute(
                            update(pl)
                            .where(pl.c.po_code == po_code, pl.c.line_no == int(line_no))
                            .values(eta_dc=row.eta_dc + delta)
                        )
                        entries.append(
                            ("po_line", ref, "eta_change", {"eta_dc": row.eta_dc}, {"eta_dc": row.eta_dc + delta})
                        )
                        line_changes.append({"ref": ref, "eta_dc_before": row.eta_dc, "eta_dc": row.eta_dc + delta})
            po = t.purchase_order
            for po_code in delayed_pos:
                po_row = (await conn.execute(select(po.c.eta_dc).where(po.c.code == po_code).with_for_update())).first()
                if po_row is not None:
                    await conn.execute(update(po).where(po.c.code == po_code).values(eta_dc=po_row.eta_dc + delta))
                    entries.append(
                        (
                            "purchase_order",
                            po_code,
                            "delay",
                            {"eta_dc": po_row.eta_dc},
                            {"eta_dc": po_row.eta_dc + delta},
                        )
                    )
            await audit.write_many(conn, self._attr(principal), entries)
            for s_row in sailings:
                await events.emit(
                    conn,
                    "sailing.updated",
                    {
                        "code": s_row.code,
                        "lane_code": s_row.lane_code,
                        "reason": f"port closure {unlocode}",
                        "etd_before": s_row.etd,
                        "etd": s_row.etd + delta,
                        "eta_before": s_row.eta,
                        "eta": s_row.eta + delta,
                    },
                    await events.changes_for(conn, [("sailing", s_row.code, "delay", None, None)]),
                )
            if line_changes:
                await events.emit(
                    conn,
                    "po_line.eta_changed",
                    {"source": f"port closure {unlocode}", "lines": line_changes},
                    await events.changes_for(conn, entries),
                )
        result: dict[str, Any] = {
            "kind": "port_closure",
            "unlocode": unlocode,
            "days": days,
            "from": start.isoformat(),
            "sailings": [s_row.code for s_row in sailings],
            "po_lines": len(line_changes),
        }
        if with_message:
            try:
                result["message"] = await self._supplier_message(principal, dict(TYPHOON_MESSAGE))
            except Problem as exc:  # profile without the Vietnamese supplier (small)
                if exc.status != 404:
                    raise
                result["message"] = None
        return result

    async def _inventory_adjust(self, principal: Principal, p: dict[str, Any]) -> dict[str, Any]:
        loc_type, loc, sku, delta = (
            p.get("location_type", "dc"),
            p.get("location_code"),
            p.get("sku"),
            p.get("delta_on_hand"),
        )
        if loc_type not in ("dc", "store") or not isinstance(loc, str) or not isinstance(sku, str):
            raise Problem(400, "invalid_parameter", "payload needs location_type (dc|store), location_code and sku")
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise Problem(400, "invalid_parameter", "payload.delta_on_hand must be a non-zero integer")
        # Free text (flagged untrusted in the event feed, like supplier-message text): bounded.
        reason = p.get("reason")
        if reason is not None and (
            not isinstance(reason, str)
            or len(reason) > MAX_REASON_CHARS
            or "\x00" in reason
            or not _utf8_encodable(reason)
        ):
            raise Problem(
                400, "invalid_parameter", f"payload.reason must be a string of at most {MAX_REASON_CHARS} characters"
            )
        ip = t.inventory_position
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(ip)
                    .where(ip.c.location_type == loc_type, ip.c.location_code == loc, ip.c.sku_code == sku)
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise Problem(404, "not_found", f"no inventory position {loc_type}/{loc}/{sku}")
            new = row.on_hand + delta
            if new - row.allocated - row.reserved < 0:
                raise Problem(409, "insufficient_stock", "adjustment would make ATP negative")
            await conn.execute(update(ip).where(ip.c.id == row.id).values(on_hand=new))
            entry = (
                "inventory_position",
                f"{loc_type}/{loc}/{sku}",
                "adjust",
                {"on_hand": row.on_hand},
                {"on_hand": new},
            )
            await audit.write_many(conn, self._attr(principal), [entry])
            await events.emit(
                conn,
                "inventory.adjusted",
                {
                    "location_type": loc_type,
                    "location_code": loc,
                    "sku": sku,
                    "on_hand_before": row.on_hand,
                    "on_hand": new,
                    "delta": delta,
                    "reason": reason or "demo adjustment",
                },
                await events.changes_for(conn, [entry]),
            )
        return {"kind": "inventory_adjust", "location": f"{loc_type}/{loc}", "sku": sku, "on_hand": new}

    # ───────────── lane_capacity (M5) ─────────────
    async def _lane_capacity(self, principal: Principal, p: dict[str, Any]) -> dict[str, Any]:
        """Scale a lane's weekly capacity for ``weeks`` ISO weeks from ``from_week`` (default: the
        simulation's current week) by ``factor_bp`` / 10,000 of the capacity in force.

        The rate cards overlapping the window are split at its edges (temporal key
        ``(lane_code, valid WITHOUT OVERLAPS)``): the part inside gets ``weekly_capacity`` ×
        factor (rounded down), the parts outside keep theirs. Confirmed bookings are left
        alone, so a week may end up over-booked (``overbooked_units``). One transaction: audit
        rows, scn ``lane_rate`` changes (delete + upsert) and the webhook event
        ``lane.capacity_changed`` (structured fields are facts; ``carrier_note`` is the
        caller's free text and flagged untrusted). A demo reset restores the golden cards.
        """
        lane_code, factor_bp, weeks = p.get("lane_code"), p.get("factor_bp"), p.get("weeks")
        if not isinstance(lane_code, str) or not lane_code or len(lane_code) > 64:
            raise Problem(400, "invalid_parameter", "payload.lane_code is required")
        if isinstance(factor_bp, bool) or not isinstance(factor_bp, int) or not 0 <= factor_bp <= MAX_FACTOR_BP:
            raise Problem(
                400, "invalid_parameter", f"payload.factor_bp must be an integer between 0 and {MAX_FACTOR_BP}"
            )
        if isinstance(weeks, bool) or not isinstance(weeks, int) or not 1 <= weeks <= MAX_CAPACITY_WEEKS:
            raise Problem(
                400, "invalid_parameter", f"payload.weeks must be an integer between 1 and {MAX_CAPACITY_WEEKS}"
            )
        from_week = p.get("from_week")
        if from_week is not None and (not isinstance(from_week, str) or not is_iso_week(from_week)):
            raise Problem(400, "invalid_parameter", "payload.from_week must be an ISO week (YYYY-Www)")
        note = p.get("carrier_note")
        if note is not None and (
            not isinstance(note, str)
            or not note.strip()
            or len(note) > MAX_NOTE_CHARS
            or "\x00" in note
            or not _utf8_encodable(note)
        ):
            raise Problem(
                400, "invalid_parameter", f"payload.carrier_note must be a string of 1..{MAX_NOTE_CHARS} characters"
            )
        unknown = sorted(set(p) - {"lane_code", "factor_bp", "weeks", "from_week", "carrier_note"})
        if unknown:
            raise Problem(400, "invalid_parameter", f"payload has unknown fields {unknown}")

        ln, rc = t.lane, t.rate_card
        async with self.engine.begin() as conn:
            lane = (await conn.execute(select(ln).where(ln.c.code == lane_code).with_for_update())).first()
            if lane is None:
                raise Problem(404, "not_found", f"lane {lane_code!r} not found")
            today = (await conn.execute(select(t.sim_clock.c.today))).scalar_one()
            first_week = from_week or iso_week(today)
            start = week_monday(first_week)
            if start + timedelta(days=6) < today:
                raise Problem(400, "invalid_parameter", f"payload.from_week {first_week} is in the past")
            if start > today + timedelta(days=MAX_HORIZON_DAYS):
                raise Problem(400, "invalid_parameter", f"payload.from_week {first_week} is too far ahead")
            end = start + timedelta(weeks=weeks)
            mondays = [start + timedelta(weeks=i) for i in range(weeks)]
            cards = list(
                await conn.execute(
                    select(rc)
                    .where(rc.c.lane_code == lane_code, rc.c.valid.op("&&")(Range(start, end, bounds="[)")))
                    .order_by(func.lower(rc.c.valid))
                    .with_for_update()
                )
            )
            if not cards:
                raise Problem(
                    409,
                    "no_rate_card",
                    f"lane {lane_code} has no rate card between {start.isoformat()} and {end.isoformat()}",
                )
            before = {m: await supply.rate_on(conn, lane_code, m) for m in mondays}
            entries: list[tuple[str, str, str, Any, Any]] = []
            for card in cards:
                lo, hi = card.valid.lower, card.valid.upper
                in_lo = start if lo is None or lo < start else lo
                in_hi = end if hi is None or hi > end else hi
                scaled = min(card.weekly_capacity * factor_bp // 10_000, PG_INT_MAX)  # column is an int
                pieces: list[tuple[date | None, date | None, int]] = []
                if lo is None or lo < in_lo:
                    pieces.append((lo, in_lo, card.weekly_capacity))
                pieces.append((in_lo, in_hi, scaled))
                if hi is None or in_hi < hi:
                    pieces.append((in_hi, hi, card.weekly_capacity))
                await conn.execute(delete(rc).where(rc.c.id == card.id))
                await conn.execute(
                    rc.insert(),
                    [
                        {
                            "lane_code": lane_code,
                            "valid": Range(a, b, bounds="[)"),
                            "basis": card.basis,
                            "rate_cents": card.rate_cents,
                            "currency": card.currency,
                            "weekly_capacity": cap,
                        }
                        for a, b, cap in pieces
                    ],
                )
                entries.append(
                    (
                        "rate_card",
                        _rate_key(lane_code, lo),
                        "capacity_change",
                        {"valid": _range_text(lo, hi), "weekly_capacity": card.weekly_capacity},
                        {
                            "factor_bp": factor_bp,
                            "pieces": [{"valid": _range_text(a, b), "weekly_capacity": cap} for a, b, cap in pieces],
                        },
                    )
                )
                entries.extend(
                    ("rate_card", _rate_key(lane_code, a), "create", None, {"weekly_capacity": cap})
                    for a, _b, cap in pieces
                    if a != lo
                )
            detail = await _capacity_detail(conn, lane_code, mondays, before)
            effective = [d["weekly_capacity"] for d in detail if d["weekly_capacity"] is not None]
            data: dict[str, Any] = {
                "lane_code": lane_code,
                "factor_bp": factor_bp,
                "weeks": weeks,
                "effective_capacity_units": min(effective) if effective else 0,
                "from_week": iso_week(start),
                "to_week": iso_week(end - timedelta(days=1)),
                "origin": lane.origin,
                "dest": lane.dest,
                "mode": lane.mode,
                "carrier_code": lane.carrier_code,
                "basis": cards[0].basis,
                "weeks_detail": detail,
            }
            if note is not None:
                data["carrier_note"] = note
                data["untrusted_fields"] = ["carrier_note"]
            await audit.write_many(conn, self._attr(principal), entries)
            await events.emit(conn, "lane.capacity_changed", data, await events.changes_for(conn, entries))
        return {"kind": "lane_capacity", **data}


def _rate_key(lane_code: str, lower: date | None) -> str:
    """scn ``lane_rate`` source_id: ``<lane>|<valid_from>``."""
    return f"{lane_code}|{lower.isoformat() if lower else ''}"


def _range_text(lower: date | None, upper: date | None) -> str:
    return f"[{lower.isoformat() if lower else ''},{upper.isoformat() if upper else ''})"


async def _capacity_detail(
    conn: AsyncConnection, lane_code: str, mondays: list[date], before: dict[date, Any]
) -> list[dict[str, Any]]:
    out = []
    for monday in mondays:
        week = iso_week(monday)
        rate = await supply.rate_on(conn, lane_code, monday)
        prev = before.get(monday)
        after = int(rate.weekly_capacity) if rate is not None else None
        per_unit = rate is not None and rate.basis == "per_unit"
        booked = await supply.booked_units(conn, lane_code, week) if per_unit else None
        out.append(
            {
                "iso_week": week,
                "weekly_capacity_before": int(prev.weekly_capacity) if prev is not None else None,
                "weekly_capacity": after,
                "booked_units": booked,
                "overbooked_units": max(0, booked - after) if booked is not None and after is not None else None,
            }
        )
    return out
