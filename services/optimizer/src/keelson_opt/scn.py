"""Scenario normalisation: ``ProblemData`` + perturbations -> dense, integer, week-bucketed
arrays used by the impact simulator, the TTS LP, the impact graph and the lever models.

All iteration orders derive from *sorted* codes, so results never depend on input order
or on Python's per-process string hash seed.

Perturbations may be the generic ``keelson_contracts.scenario`` models, the local M1 models
or plain dicts; only the fields below are read. ``p10``/``p90`` bands are ignored (the
primary value is the p50). An optional ``window {start, end|null}`` (inclusive dates)
restricts where a perturbation applies, as noted per op. Target kinds per op follow
``keelson_contracts.optimizer.TARGET_KINDS_BY_OP`` (80 §2); the resolution is deterministic:

* ``lead_time_shift`` moves a receipt's ``eta`` by ``days`` (negative = earlier) and
  re-buckets it into the ISO week of the new date. A shifted eta before the horizon start
  lands in week 0; after the horizon end the receipt does not arrive within the horizon.
  Target kinds: ``receipt``/``po_line`` (receipt ref ``PO#line``), ``po`` (ref prefix
  before ``#``), ``dc``, ``sku``, ``style`` (M1) and, through ``po_lines`` + ``bom`` (M3):
  ``factory`` (the PO line's factory), ``supplier`` (the PO line's own supplier and every
  supplier whose BOM component the line's style uses *at that factory* - a BOM row with
  ``factory_code=null`` applies to every factory) and ``component`` (same BOM rule).
  ``window``: only receipts whose original eta is inside it. Several matching shifts add
  up. A lead-time shift delays the goods' *readiness* (ex-factory): a faster lane cannot
  recover it, it can only save transit time (see ``expedite_mode_shift``).
* ``demand_scale`` multiplies demand units by ``factor_bp / 10000`` (integer, round half
  up) for matching rows in ``weeks`` (all weeks when omitted) whose ISO week overlaps
  ``window``. Target kinds: ``cluster``, ``dc`` (all clusters of that DC), ``sku``,
  ``style``. Several matching scales apply sequentially in request order.
* ``capacity_scale`` multiplies a lane's weekly capacity by ``factor_bp / 10000``
  (rounded down) for weeks overlapping ``window`` (all weeks when omitted). Target kinds:
  ``lane`` (a DC lane ``FROM>TO`` or an inbound lane code) and ``port``/``chokepoint``
  (every inbound lane whose ``origin``, ``dest_dc`` or ``via`` includes the id).
  ``factor_bp = 0`` *closes* the lane: a receipt whose PO line ships on it and whose
  transit ``[eta - transit_days, eta]`` overlaps the closure is

  - delayed by the closure's length (``end - start + 1`` days: the backlog clears at the
    rate it built up) when the closure has an end date, or
  - re-routed onto the lane's catalogued ``alt_lane_code`` when that lane is open for it
    and is not a *faster* (premium) mode - a switch to sea-air/air is a lever decision
    (``expedite_mode_shift``), never automatic - with ``max(0, alt transit - transit)``
    extra days,

  whichever is earlier; with neither option the receipt is **lost** (not counted). A
  partial scale only lowers the lane's capacity for *new* bookings (expedite splits);
  receipts already booked on it keep their dates.
* ``compliance_block`` on ``supplier``/``factory``: those alternate sources may not be
  used by ``resource_alt_supplier`` (the op has no other effect in the simulator). A block
  whose ``window`` ended before ``as_of`` or starts after the horizon is ignored.

Anything else (other ops, other target kinds, ids matching nothing) is ignored and
reported in ``warnings``.

**Capacity bookings (M5 re-planning, ``ProblemData.capacity_bookings``,
:mod:`keelson_opt.wire_m5`).** A booking draws ``qty_units`` from its inbound lane in its
departure week (Merxa draws lane capacity per booking, never per PO line, so PO lines on a
premium lane consume capacity only through the bookings listed). A booking whose week is
before the ``as_of`` week has departed and is never questioned. When a lane's capacity in a
week (after ``capacity_scale``) is below what is booked there, every booking of that
(lane, week) cell becomes *decidable*: the plan keeps some and cancels the others, whose
PO lines then arrive by sea - at ``revert_eta`` (or, without it, ``max(as_of + 1,
ex_factory + 1) + transit`` of the booking lane's ``alt_lane_code``, when that lane is not
a faster mode), plus the line's readiness delay and the sea lane's closure rules; with no
sea route the cancelled units are lost. Without a plan (the scenario baseline, i.e.
``kpis_before`` and ``/v1/impact``) the carrier is assumed to honour the cell's bookings
first-come-first-served: in booking-code order, each one that still fits is kept; the
others roll to their sea arrival.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from keelson_contracts.optimizer import TARGET_KINDS_BY_OP, ProblemData

from keelson_opt.isoweeks import horizon_weeks, monday_of, parse_iso_week, week_index

SUPPORTED_KINDS: dict[str, frozenset[str]] = {
    **{op: frozenset(kinds) for op, kinds in TARGET_KINDS_BY_OP.items()},
    "compliance_block": frozenset({"supplier", "factory"}),
}
_ONE_WEEK = dt.timedelta(days=6)
MODE_SPEED: dict[str, int] = {"sea": 0, "road": 0, "sea_air": 1, "air": 2}
"""Mode ranking: an automatic re-route never switches to a faster (premium) mode."""


@dataclass(frozen=True, slots=True)
class LaneInfo:
    code: str
    from_dc: str
    to_dc: str
    transit_days: int
    lag_weeks: int
    """``ceil(transit_days / 7)``: a transfer shipped in week t arrives in week t + lag."""
    cost_per_unit_cents: int
    caps: tuple[int, ...]
    """Per ship week: ``max_units_wk`` after capacity_scale perturbations."""


@dataclass(frozen=True, slots=True)
class Closure:
    """A zero-capacity period of an inbound lane (``start``/``end`` ``None`` = unbounded)."""

    start: dt.date | None
    end: dt.date | None

    def overlaps(self, first: dt.date, last: dt.date) -> bool:
        return (self.start is None or last >= self.start) and (self.end is None or first <= self.end)

    @property
    def days(self) -> int | None:
        if self.start is None or self.end is None:
            return None
        return (self.end - self.start).days + 1


@dataclass(frozen=True, slots=True)
class InboundLaneInfo:
    code: str
    origin: str
    dest_dc: str
    mode: str
    transit_days: int
    cost_per_unit_cents: int
    weekly_capacity_units: int
    via: tuple[str, ...]
    alt_lane_code: str | None
    caps: tuple[int, ...]
    """Weekly capacity per horizon week after capacity_scale perturbations."""
    closures: tuple[Closure, ...]

    @property
    def points(self) -> tuple[str, ...]:
        """Origin, via points and destination (port/chokepoint matching)."""
        return (self.origin, *self.via, self.dest_dc)

    def closed(self, first: dt.date, last: dt.date) -> bool:
        return any(c.overlaps(first, last) for c in self.closures)

    def open_departure(self, earliest: dt.date) -> dt.date | None:
        """First departure on/after ``earliest`` whose transit misses every closure (a new
        booking waits for a temporary closure to end); ``None`` when an open-ended closure
        blocks it."""
        dep = earliest
        for _ in range(len(self.closures) + 1):
            hit = [c for c in self.closures if c.overlaps(dep, dep + dt.timedelta(days=self.transit_days))]
            if not hit:
                return dep
            ends = [c.end for c in hit]
            if any(e is None for e in ends):
                return None
            dep = max(e for e in ends if e is not None) + dt.timedelta(days=1)
        return None

    def cap_in_week(self, t: int) -> int:
        """Capacity of horizon week ``t`` (unscaled beyond the horizon)."""
        return self.caps[t] if 0 <= t < len(self.caps) else self.weekly_capacity_units


@dataclass(frozen=True, slots=True)
class PoLineInfo:
    ref: str
    po_code: str
    supplier_code: str
    factory_code: str
    dest_dc: str
    sku: str
    style: str
    qty: int
    eta: dt.date
    ex_factory: dt.date
    mode: str
    lane_code: str | None
    version: int | None
    """Optimistic-concurrency version when ProblemData carries one (``None`` otherwise)."""


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    """One receipt after perturbation (``ref`` like ``PO-26-0101#1``)."""

    ref: str
    dc: str
    sku: str
    qty: int
    eta: dt.date
    """Original (unperturbed) eta."""
    prod_delay: int
    """Days added by ``lead_time_shift`` (readiness / ex-factory delay; may be negative)."""
    transport_delay: int
    """Days added by lane / port / chokepoint closures."""
    lost: bool
    """Closed lane without a usable alternative: the receipt does not arrive."""
    week: int
    """Bucket of the perturbed eta (``>= horizon`` = after the horizon; meaningless if lost)."""

    @property
    def delay(self) -> int:
        return self.prod_delay + self.transport_delay

    @property
    def affected(self) -> bool:
        """Disrupted: arrives later than planned, or not at all."""
        return self.lost or self.delay > 0

    @property
    def eta_perturbed(self) -> dt.date:
        return self.eta + dt.timedelta(days=self.delay)


@dataclass(frozen=True, slots=True)
class BookedLineInfo:
    """A PO line carried by a booking, in both states (ReceiptLines of the line's ref)."""

    ref: str
    dc: str
    sku: str
    qty: int
    """Units moved by a cancellation: the whole line's receipt units when it has a sea
    arrival (its ``po_line.reschedule`` moves the whole line), else the booked qty (at most
    the line's receipt units), which is lost."""
    revert_eta: dt.date | None
    """``eta_dc`` after a cancellation (system value; ``None``: no sea route, units lost)."""
    keep: tuple[ReceiptLine, ...]
    """The ref's receipt lines while the booking flies."""
    cancel: tuple[ReceiptLine, ...]
    """The ref's receipt lines once the booking is cancelled."""


@dataclass(frozen=True, slots=True)
class BookingInfo:
    code: str
    lane: str
    iso_week: str
    week: int
    """Departure week index relative to the horizon start (may be < 0 or >= horizon)."""
    qty: int
    version: int | None
    cap: int
    """Capacity of the lane in that week after ``capacity_scale``."""
    departed: bool
    lines: tuple[BookedLineInfo, ...]

    @property
    def cell(self) -> tuple[str, int]:
        return (self.lane, self.week)


@dataclass(slots=True)
class Scenario:
    """Week-bucketed, perturbed problem. Keys are ``(location_code, sku)`` tuples; weekly
    series are lists of length ``horizon``."""

    tenant: str
    as_of: dt.date
    weeks: list[str]
    week_mondays: list[dt.date]
    dcs: list[str]
    clusters: list[str]
    cluster_dc: dict[str, str]
    dc_clusters: dict[str, list[str]]
    skus: list[str]
    sku_style: dict[str, str]
    price_cents: dict[str, int]
    cost_cents: dict[str, int]
    dc_supply: dict[tuple[str, str], int]
    """Units available to the DC at t=0: (on_hand - reserved) + in_transit. Allocated
    stock still serves this DC's clusters, so it is part of the supply; reserved stock is
    already committed to outbound transfers (and counted in the destination's
    in_transit), so it is not."""
    dc_atp: dict[tuple[str, str], int]
    """Transferable now (Merxa ATP): max(0, on_hand - allocated - reserved)."""
    receipts: dict[tuple[str, str], list[int]]
    cluster_on_hand: dict[tuple[str, str], int]
    demand: dict[tuple[str, str], list[int]]
    protected_min: dict[tuple[str, str], int]
    lanes: list[LaneInfo]
    first_ship_week: int
    """First week index whose Monday is on/after ``as_of`` (ship dates are Mondays)."""
    warnings: list[str] = field(default_factory=list)
    # ── M3 (network / levers) ──
    data: ProblemData | None = None
    receipt_lines: list[ReceiptLine] = field(default_factory=list)
    po_lines: dict[str, PoLineInfo] = field(default_factory=dict)
    inbound_lanes: dict[str, InboundLaneInfo] = field(default_factory=dict)
    disrupted: dict[str, set[str]] = field(default_factory=dict)
    """Directly targeted entity ids per kind (only targets that matched something)."""
    blocked: set[str] = field(default_factory=set)
    """Supplier / factory codes under ``compliance_block``."""
    bom_by_style: dict[str, list[tuple[str, str, str | None]]] = field(default_factory=dict)
    """style -> [(component, supplier, factory|None)]."""
    memo: dict[str, Any] = field(default_factory=dict)
    """Derived data computed once per request (e.g. the LP requirements)."""
    # ── M5 (capacity bookings) ──
    bookings: dict[str, BookingInfo] = field(default_factory=dict)
    decidable: tuple[str, ...] = ()
    """Bookings of over-booked (lane, week) cells (not departed), by code."""
    baseline_kept: frozenset[str] = frozenset()
    """Decidable bookings the baseline keeps (first-come-first-served)."""
    booked_refs: frozenset[str] = frozenset()
    """PO lines carried by any booking (never split, re-sourced or re-booked by a lever)."""

    def booking_load(self, kept: frozenset[str] | None = None) -> dict[tuple[str, int], int]:
        """Capacity drawn per (lane, week index) by the bookings that fly: every
        non-decidable booking plus the decidable ones in ``kept`` (default: the baseline)."""
        kept = self.baseline_kept if kept is None else kept
        dec = set(self.decidable)
        load: dict[tuple[str, int], int] = defaultdict(int)
        for code, b in self.bookings.items():
            if code not in dec or code in kept:
                load[b.cell] += b.qty
        return dict(load)

    @property
    def horizon(self) -> int:
        return len(self.weeks)

    @property
    def start(self) -> dt.date:
        return self.week_mondays[0]

    def week_of(self, day: dt.date) -> int:
        """Bucket of ``day`` (before the horizon start -> 0; may be >= horizon)."""
        return max(0, week_index(day, self.week_mondays[0]))


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a Pydantic model or a dict."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


Attr = str | frozenset[str]


@dataclass(frozen=True, slots=True)
class _Target:
    kinds: tuple[str, ...]
    ids: frozenset[str]
    start: dt.date | None
    end: dt.date | None

    def matches(self, attrs: dict[str, Attr]) -> bool:
        """Any of the target's kinds has the entity's attribute (or one of its attribute
        values, for set-valued attributes such as BOM suppliers) among ``ids``."""
        for kind in self.kinds:
            value = attrs.get(kind)
            if value is None:
                continue
            if isinstance(value, str):
                if value in self.ids:
                    return True
            elif not self.ids.isdisjoint(value):
                return True
        return False

    def matched_ids(self, attrs: dict[str, Attr]) -> Iterable[tuple[str, str]]:
        for kind in self.kinds:
            value = attrs.get(kind)
            if value is None:
                continue
            for v in (value,) if isinstance(value, str) else sorted(value):
                if v in self.ids:
                    yield kind, v

    def covers_day(self, day: dt.date) -> bool:
        return (self.start is None or day >= self.start) and (self.end is None or day <= self.end)

    def covers_week(self, monday: dt.date) -> bool:
        return (self.start is None or monday + _ONE_WEEK >= self.start) and (self.end is None or monday <= self.end)


def _as_date(value: Any) -> dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _target(p: Any) -> _Target:
    t = _get(p, "target")
    win = _get(p, "window")
    return _Target(
        kinds=tuple(str(k) for k in (_get(t, "kinds") or ())) if t is not None else (),
        ids=frozenset(str(i) for i in (_get(t, "ids") or ())) if t is not None else frozenset(),
        start=_as_date(_get(win, "start")) if win is not None else None,
        end=_as_date(_get(win, "end")) if win is not None else None,
    )


def round_bp(units: int, factor_bp: int) -> int:
    """``units * factor_bp / 10000`` rounded half up (non-negative integers)."""
    return (units * factor_bp + 5000) // 10000


class _Hits:
    """Per-perturbation match counter + disrupted entity collector."""

    def __init__(self) -> None:
        self.count: dict[int, int] = defaultdict(int)
        self.disrupted: dict[str, set[str]] = defaultdict(set)

    def hit(self, idx: int, tgt: _Target, attrs: dict[str, Attr]) -> None:
        self.count[idx] += 1
        for kind, v in tgt.matched_ids(attrs):
            self.disrupted[kind].add(v)


def build_scenario(data: ProblemData, perturbations: Sequence[Any] = ()) -> Scenario:
    warnings: list[str] = []
    weeks, mondays = horizon_weeks(data.horizon.start_week, data.horizon.weeks)
    start = mondays[0]
    horizon = len(weeks)
    week_pos = {w: i for i, w in enumerate(weeks)}

    dcs = sorted(d.code for d in data.dcs)
    clusters = sorted(c.code for c in data.clusters)
    cluster_dc = {c.code: c.dc_code for c in data.clusters}
    dc_clusters: dict[str, list[str]] = {d: [] for d in dcs}
    for c in clusters:
        dc_clusters[cluster_dc[c]].append(c)
    skus = sorted(s.code for s in data.skus)
    sku_style = {s.code: s.style_code for s in data.skus}

    # ── perturbations: split by op, warn about everything ignored ──
    shifts: list[tuple[int, _Target, int]] = []
    scales: list[tuple[int, _Target, int, frozenset[str] | None]] = []
    caps: list[tuple[int, _Target, int]] = []
    blocks: list[tuple[int, _Target]] = []
    for idx, p in enumerate(perturbations):
        op = str(_get(p, "op", ""))
        if op not in SUPPORTED_KINDS:
            warnings.append(f"perturbation[{idx}] op '{op}' is not supported by the optimizer and was ignored")
            continue
        tgt = _target(p)
        unsupported = sorted(set(tgt.kinds) - SUPPORTED_KINDS[op])
        if unsupported:
            warnings.append(f"perturbation[{idx}] {op}: target kinds {unsupported} not supported; ignored")
        if op == "lead_time_shift":
            shifts.append((idx, tgt, int(_get(p, "days", 0))))
        elif op == "demand_scale":
            wk = _get(p, "weeks")
            scales.append((idx, tgt, int(_get(p, "factor_bp", 10000)), frozenset(wk) if wk else None))
        elif op == "capacity_scale":
            caps.append((idx, tgt, int(_get(p, "factor_bp", 10000))))
        else:
            blocks.append((idx, tgt))
    hits = _Hits()

    # ── DC stock ──
    dc_supply: dict[tuple[str, str], int] = defaultdict(int)
    dc_atp: dict[tuple[str, str], int] = defaultdict(int)
    store_rows = 0
    for inv in data.inventory:
        if inv.location_type != "dc":
            store_rows += 1
            continue
        key = (inv.location_code, inv.sku)
        # ``reserved`` stock is committed to outbound transfer orders (Merxa reserves at
        # the source and adds the same units to ``in_transit`` at the destination when a
        # transfer_order.create is applied): it must not serve this DC's clusters, or
        # the units would be counted twice across the network.
        dc_supply[key] += max(0, inv.on_hand - inv.reserved) + inv.in_transit
        dc_atp[key] += max(0, inv.on_hand - inv.allocated - inv.reserved)
    if store_rows:
        warnings.append(f"{store_rows} store-level inventory rows ignored (use cluster_inventory)")

    # ── network master data (M3) ──
    bom_by_style: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for b in sorted(data.bom, key=lambda b: (b.style_code, b.component_code, b.supplier_code, b.factory_code or "")):
        bom_by_style[b.style_code].append((b.component_code, b.supplier_code, b.factory_code))
    po_lines = {
        pl.ref: PoLineInfo(
            ref=pl.ref,
            po_code=pl.po_code,
            supplier_code=pl.supplier_code,
            factory_code=pl.factory_code,
            dest_dc=pl.dest_dc,
            sku=pl.sku,
            style=sku_style[pl.sku],
            qty=pl.qty,
            eta=pl.eta,
            ex_factory=pl.ex_factory,
            mode=pl.mode,
            lane_code=pl.lane_code,
            version=_version(pl),
        )
        for pl in sorted(data.po_lines, key=lambda x: x.ref)
    }
    inbound = _inbound_lanes(data, caps, hits, mondays)

    # ── receipts (+ lead_time_shift, + closures) ──
    lines: list[ReceiptLine] = []
    for r in sorted(data.receipts, key=lambda r: (r.ref, r.dc_code, r.sku, r.eta)):
        pol = po_lines.get(r.ref)
        attrs: dict[str, Attr] = {
            "receipt": r.ref,
            "po_line": r.ref,
            "po": r.ref.split("#", 1)[0],
            "dc": r.dc_code,
            "sku": r.sku,
            "style": sku_style[r.sku],
        }
        if pol is not None:
            attrs.update(_network_attrs(pol, bom_by_style))
        days = 0
        for idx, tgt, d in shifts:
            if tgt.matches(attrs) and tgt.covers_day(r.eta):
                days += d
                hits.hit(idx, tgt, attrs)
        ready_eta = r.eta + dt.timedelta(days=days)
        transport, lost = 0, False
        lane = inbound.get(pol.lane_code) if pol is not None and pol.lane_code else None
        if lane is not None and lane.closures:
            transport, lost = closure_delay(lane, ready_eta, inbound)
        week = max(0, week_index(ready_eta + dt.timedelta(days=transport), start))
        lines.append(ReceiptLine(r.ref, r.dc_code, r.sku, r.qty, r.eta, days, transport, lost, week))
    warnings.extend(
        f"lead_time_shift #{n + 1} matched no receipts" for n, (idx, _t, _d) in enumerate(shifts) if not hits.count[idx]
    )

    # ── capacity bookings (M5): over-booked cells, first-come-first-served baseline ──
    raw_bookings = list(getattr(data, "capacity_bookings", None) or ())
    bookings: dict[str, BookingInfo] = {}
    decidable: list[str] = []
    baseline_kept: set[str] = set()
    if raw_bookings:
        bookings = _bookings(data, raw_bookings, lines, po_lines, inbound, caps, start)
        lines, decidable, baseline_kept = _booking_baseline(bookings, lines, warnings)

    receipts: dict[tuple[str, str], list[int]] = {}
    beyond = lost_units = 0
    for rl in lines:
        if rl.lost:
            lost_units += rl.qty
            continue
        if rl.week >= horizon:
            beyond += rl.qty
            continue
        receipts.setdefault((rl.dc, rl.sku), [0] * horizon)[rl.week] += rl.qty
    if beyond:
        warnings.append(f"{beyond} receipt units arrive after the horizon end and are not counted")
    if lost_units:
        warnings.append(f"{lost_units} receipt units are lost: their lane is closed and no alternative lane is usable")

    # ── cluster stock, protected minimum ──
    cluster_on_hand: dict[tuple[str, str], int] = defaultdict(int)
    for ci in data.cluster_inventory:
        cluster_on_hand[(ci.cluster_code, ci.sku)] += ci.on_hand
    pmin: dict[tuple[str, str], int] = defaultdict(int)
    for pm in data.protected_min:
        pmin[(pm.cluster_code, pm.sku)] += pm.units

    # ── demand (+ demand_scale) ──
    raw: dict[tuple[str, str], list[int]] = {}
    outside = 0
    for row in data.demand:
        t_opt = week_pos.get(row.iso_week)
        if t_opt is None:
            outside += 1
            continue
        raw.setdefault((row.cluster_code, row.sku), [0] * horizon)[t_opt] += row.units
    if outside:
        warnings.append(f"{outside} demand rows outside the horizon ignored")
    demand: dict[tuple[str, str], list[int]] = {}
    for key in sorted(raw):
        cl, sku = key
        series = raw[key]
        attrs = {"cluster": cl, "dc": cluster_dc[cl], "sku": sku, "style": sku_style[sku]}
        for idx, tgt, factor, wks in scales:
            if not tgt.matches(attrs):
                continue
            hits.hit(idx, tgt, attrs)
            series = [
                round_bp(u, factor) if (wks is None or weeks[t] in wks) and tgt.covers_week(mondays[t]) else u
                for t, u in enumerate(series)
            ]
        demand[key] = series
    warnings.extend(
        f"demand_scale #{n + 1} matched no demand" for n, (idx, *_r) in enumerate(scales) if not hits.count[idx]
    )

    # ── DC lanes (+ capacity_scale) ──
    lanes: list[LaneInfo] = []
    for ln in sorted(data.lanes, key=lambda x: x.code):
        week_caps = [ln.max_units_wk] * horizon
        for idx, tgt, factor in caps:
            attrs = {"lane": ln.code}
            if tgt.matches(attrs):
                hits.hit(idx, tgt, attrs)
                week_caps = [c * factor // 10000 if tgt.covers_week(mondays[t]) else c for t, c in enumerate(week_caps)]
        lanes.append(
            LaneInfo(
                code=ln.code,
                from_dc=ln.from_dc,
                to_dc=ln.to_dc,
                transit_days=ln.transit_days,
                lag_weeks=math.ceil(ln.transit_days / 7),
                cost_per_unit_cents=ln.cost_per_unit_cents,
                caps=tuple(week_caps),
            )
        )
    warnings.extend(
        f"capacity_scale #{n + 1} matched no lanes" for n, (idx, *_r) in enumerate(caps) if not hits.count[idx]
    )

    # ── compliance blocks (alternate sources only) ──
    blocked: set[str] = set()
    known = {s.supplier_code for s in data.alt_sources} | {s.factory_code for s in data.alt_sources}
    known |= {p.supplier_code for p in po_lines.values()} | {p.factory_code for p in po_lines.values()}
    horizon_end = mondays[-1] + _ONE_WEEK
    for n, (_idx, tgt) in enumerate(blocks):
        if (tgt.end is not None and tgt.end < data.as_of) or (tgt.start is not None and tgt.start > horizon_end):
            warnings.append(f"compliance_block #{n + 1} window is outside [as_of, horizon end]; ignored")
            continue
        ids = {i for i in tgt.ids if i in known} if {"supplier", "factory"} & set(tgt.kinds) else set()
        if not ids:
            warnings.append(f"compliance_block #{n + 1} matched no supplier or factory")
            continue
        blocked |= ids
        for kind in ("supplier", "factory"):
            if kind in tgt.kinds:
                hits.disrupted[kind].update(ids)

    first_ship = week_index(data.as_of, start) + (0 if monday_of(data.as_of) == data.as_of else 1)

    return Scenario(
        tenant=data.tenant,
        as_of=data.as_of,
        weeks=weeks,
        week_mondays=mondays,
        dcs=dcs,
        clusters=clusters,
        cluster_dc=cluster_dc,
        dc_clusters=dc_clusters,
        skus=skus,
        sku_style=sku_style,
        price_cents={s.code: s.retail_price_cents for s in data.skus},
        cost_cents={s.code: s.unit_cost_cents for s in data.skus},
        dc_supply=dict(dc_supply),
        dc_atp=dict(dc_atp),
        receipts=receipts,
        cluster_on_hand=dict(cluster_on_hand),
        demand=demand,
        protected_min=dict(pmin),
        lanes=lanes,
        first_ship_week=max(0, first_ship),
        warnings=warnings,
        data=data,
        receipt_lines=lines,
        po_lines=po_lines,
        inbound_lanes=inbound,
        disrupted={k: set(v) for k, v in sorted(hits.disrupted.items())},
        blocked=blocked,
        bom_by_style=dict(bom_by_style),
        bookings=bookings,
        decidable=tuple(decidable),
        baseline_kept=frozenset(baseline_kept),
        booked_refs=frozenset(ln.ref for b in bookings.values() for ln in b.lines),
    )


def _version(pl: Any) -> int | None:
    """``PoLine.version`` when the shared model carries one (forward compatible)."""
    v = getattr(pl, "version", None)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def _network_attrs(pol: PoLineInfo, bom_by_style: dict[str, list[tuple[str, str, str | None]]]) -> dict[str, Attr]:
    comps: set[str] = set()
    sups: set[str] = {pol.supplier_code}
    for comp, sup, fac in bom_by_style.get(pol.style, ()):
        if fac is None or fac == pol.factory_code:
            comps.add(comp)
            sups.add(sup)
    attrs: dict[str, Attr] = {"factory": pol.factory_code, "supplier": frozenset(sups)}
    if comps:
        attrs["component"] = frozenset(comps)
    return attrs


def _inbound_lanes(
    data: ProblemData, caps: list[tuple[int, _Target, int]], hits: _Hits, mondays: list[dt.date]
) -> dict[str, InboundLaneInfo]:
    horizon = len(mondays)
    out: dict[str, InboundLaneInfo] = {}
    for il in sorted(data.inbound_lanes, key=lambda x: x.code):
        week_caps = [il.weekly_capacity_units] * horizon
        closures: list[Closure] = []
        attrs: dict[str, Attr] = {
            "lane": il.code,
            "port": frozenset((il.origin, *il.via, il.dest_dc)),
            "chokepoint": frozenset((il.origin, *il.via, il.dest_dc)),
        }
        for idx, tgt, factor in caps:
            if not tgt.matches(attrs):
                continue
            hits.hit(idx, tgt, attrs)
            week_caps = [c * factor // 10000 if tgt.covers_week(mondays[t]) else c for t, c in enumerate(week_caps)]
            if factor == 0:
                closures.append(Closure(tgt.start, tgt.end))
        out[il.code] = InboundLaneInfo(
            code=il.code,
            origin=il.origin,
            dest_dc=il.dest_dc,
            mode=il.mode,
            transit_days=il.transit_days,
            cost_per_unit_cents=il.cost_per_unit_cents,
            weekly_capacity_units=il.weekly_capacity_units,
            via=tuple(il.via),
            alt_lane_code=il.alt_lane_code,
            caps=tuple(week_caps),
            closures=tuple(closures),
        )
    return out


def closure_delay(lane: InboundLaneInfo, eta: dt.date, lanes: dict[str, InboundLaneInfo]) -> tuple[int, bool]:
    """(extra transport days, lost) for a shipment arriving at ``eta`` on ``lane``."""
    dep = eta - dt.timedelta(days=lane.transit_days)
    hit = [c for c in lane.closures if c.overlaps(dep, eta)]
    if not hit:
        return 0, False
    options: list[int] = []
    durations = [c.days for c in hit]
    if all(d is not None for d in durations):
        options.append(max(d for d in durations if d is not None))
    alt = lanes.get(lane.alt_lane_code) if lane.alt_lane_code else None
    if alt is not None and MODE_SPEED.get(alt.mode, 0) <= MODE_SPEED.get(lane.mode, 0):
        alt_eta = dep + dt.timedelta(days=alt.transit_days)
        if not alt.closed(dep, alt_eta):
            options.append(max(0, alt.transit_days - lane.transit_days))
    if not options:
        return 0, True
    return min(options), False


# ───────────────────────────── capacity bookings (M5) ─────────────────────────────


def _week_capacity(il: Any, caps: list[tuple[int, _Target, int]], monday: dt.date) -> int:
    """Capacity of inbound lane ``il`` in the week of ``monday`` after ``capacity_scale``
    (same rule as :func:`_inbound_lanes`, for any week - also outside the horizon)."""
    attrs: dict[str, Attr] = {
        "lane": il.code,
        "port": frozenset((il.origin, *il.via, il.dest_dc)),
        "chokepoint": frozenset((il.origin, *il.via, il.dest_dc)),
    }
    cap = int(il.weekly_capacity_units)
    for _idx, tgt, factor in caps:
        if tgt.matches(attrs) and tgt.covers_week(monday):
            cap = cap * factor // 10000
    return cap


def _bookings(
    data: ProblemData,
    raw: list[Any],
    lines: list[ReceiptLine],
    po_lines: dict[str, PoLineInfo],
    inbound: dict[str, InboundLaneInfo],
    caps: list[tuple[int, _Target, int]],
    start: dt.date,
) -> dict[str, BookingInfo]:
    by_ref: dict[str, list[ReceiptLine]] = defaultdict(list)
    for rl in lines:
        by_ref[rl.ref].append(rl)
    raw_lanes = {il.code: il for il in data.inbound_lanes}
    as_of_monday = monday_of(data.as_of)
    out: dict[str, BookingInfo] = {}
    for b in sorted(raw, key=lambda b: str(_get(b, "code"))):
        code = str(_get(b, "code"))
        lane = inbound[str(_get(b, "lane_code"))]
        iso_week = str(_get(b, "iso_week"))
        monday = parse_iso_week(iso_week)
        sea = inbound.get(lane.alt_lane_code) if lane.alt_lane_code else None
        if sea is not None and MODE_SPEED.get(sea.mode, 0) > MODE_SPEED.get(lane.mode, 0):
            sea = None
        infos: list[BookedLineInfo] = []
        for bl in sorted(_get(b, "lines") or (), key=lambda x: str(_get(x, "po_line_ref"))):
            ref = str(_get(bl, "po_line_ref"))
            pol = po_lines[ref]
            rls = by_ref.get(ref, [])
            units = sum(rl.qty for rl in rls)
            q = min(int(_get(bl, "qty")), units)
            revert = _as_date(_get(bl, "revert_eta"))
            if revert is None and sea is not None:
                revert = max(data.as_of, pol.ex_factory) + dt.timedelta(days=1 + sea.transit_days)
            if revert is not None:
                # The cancellation's ``po_line.reschedule`` moves the whole PO line in the
                # target system, so the whole line arrives at its sea date (review fix).
                q = units
            infos.append(
                BookedLineInfo(
                    ref=ref,
                    dc=pol.dest_dc,
                    sku=pol.sku,
                    qty=q,
                    revert_eta=revert,
                    keep=tuple(rls),
                    cancel=_cancelled(rls, q, revert, sea, inbound, start),
                )
            )
        out[code] = BookingInfo(
            code=code,
            lane=lane.code,
            iso_week=iso_week,
            week=week_index(monday, start),
            qty=int(_get(b, "qty_units")),
            version=_version(b),
            cap=_week_capacity(raw_lanes[lane.code], caps, monday),
            departed=monday < as_of_monday,
            lines=tuple(infos),
        )
    return out


def _cancelled(
    rls: list[ReceiptLine],
    q: int,
    revert: dt.date | None,
    sea: InboundLaneInfo | None,
    inbound: dict[str, InboundLaneInfo],
    start: dt.date,
) -> tuple[ReceiptLine, ...]:
    """The ref's receipt lines once ``q`` booked units go back to their sea arrival: the
    units leave the lost and then the latest lines first; they arrive at ``revert`` plus
    the line's readiness delay and the sea lane's closure rules (``None``: lost)."""
    if q <= 0 or not rls:
        return tuple(rls)
    qty = [rl.qty for rl in rls]
    left = q
    for i in sorted(range(len(rls)), key=lambda i: (not rls[i].lost, -rls[i].week, i)):
        take = min(left, qty[i])
        qty[i] -= take
        left -= take
        if not left:
            break
    out = [replace(rl, qty=n) for rl, n in zip(rls, qty, strict=True) if n > 0]
    base = rls[0]
    prod = max(rl.prod_delay for rl in rls)
    if revert is None:
        out.append(ReceiptLine(base.ref, base.dc, base.sku, q, base.eta, prod, 0, True, 0))
        return tuple(out)
    ready = revert + dt.timedelta(days=prod)
    transport, lost = (0, False)
    if sea is not None and sea.closures:
        transport, lost = closure_delay(sea, ready, inbound)
    arrival = ready + dt.timedelta(days=transport)
    out.append(
        ReceiptLine(
            base.ref,
            base.dc,
            base.sku,
            q,
            base.eta,
            prod,
            (arrival - base.eta).days - prod,
            lost,
            max(0, week_index(arrival, start)),
        )
    )
    return tuple(out)


def _booking_baseline(
    bookings: dict[str, BookingInfo], lines: list[ReceiptLine], warnings: list[str]
) -> tuple[list[ReceiptLine], list[str], set[str]]:
    """Over-booked (lane, week) cells: all their bookings become decidable; the baseline
    keeps them first-come-first-served (booking code order, each one that still fits) and
    rolls the others to their sea arrival."""
    cells: dict[tuple[str, int], list[BookingInfo]] = defaultdict(list)
    for b in bookings.values():
        if not b.departed:
            cells[b.cell].append(b)
    decidable: list[str] = []
    kept: set[str] = set()
    for cell in sorted(cells):
        group = sorted(cells[cell], key=lambda b: b.code)
        total, cap = sum(b.qty for b in group), group[0].cap
        if total <= cap:
            continue
        used = 0
        for b in group:
            decidable.append(b.code)
            if used + b.qty <= cap:
                kept.add(b.code)
                used += b.qty
        rolled = [b.code for b in group if b.code not in kept]
        shown = ", ".join(rolled[:5]) + (f" +{len(rolled) - 5} more" if len(rolled) > 5 else "")
        warnings.append(
            f"capacity_bookings: {group[0].lane} {group[0].iso_week} has {total} units booked but a capacity of "
            f"{cap}; without a plan the carrier is assumed to keep the earliest bookings that fit and to roll "
            f"{len(rolled)} ({shown}) to their sea arrival"
        )
    swap = {ln.ref: ln.cancel for code in decidable if code not in kept for ln in bookings[code].lines}
    if swap:
        out: list[ReceiptLine] = []
        done: set[str] = set()
        for rl in lines:
            if rl.ref in swap:
                if rl.ref not in done:
                    out.extend(swap[rl.ref])
                    done.add(rl.ref)
                continue
            out.append(rl)
        lines = out
    return lines, sorted(decidable), kept
