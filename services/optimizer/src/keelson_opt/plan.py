"""A plan's physical effect on the scenario, independent of how it was found.

Every lever result is reduced to the same ledger so that one simulator run recomputes all
KPIs from the *rounded* plan (never from an objective value):

* ``transfers`` - DC-to-DC transfer orders (``reallocate_inventory``);
* receipt-line edits - expedite splits and alternate-source buys *reduce* a PO line and add
  a new on-time arrival; a resequence *moves* PO lines to new arrival weeks;
* ``overrides`` - firm allocations per (dc, sku, week) (``prioritize_allocation``);
* ``rebook`` (M5) - which decidable capacity bookings fly; the PO lines of the cancelled
  ones arrive by sea (``None`` = the scenario baseline, see :mod:`keelson_opt.scn`).

Receipt lines carry a ``late`` flag (arrives later than originally planned, or is a
disrupted line): the time-to-survive model only counts on-time supply.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from keelson_contracts.optimizer import Allocation, AltSource

from keelson_opt.impact import Transfer
from keelson_opt.scn import InboundLaneInfo, ReceiptLine, Scenario


@dataclass(frozen=True, slots=True)
class RLine:
    """An arrival of ``qty`` units of ``sku`` at ``dc`` in bucket ``week`` (``>= horizon``
    = after the horizon)."""

    key: str
    """PO line ref (or a synthetic id for new arrivals)."""
    dc: str
    sku: str
    qty: int
    week: int
    late: bool


@dataclass(frozen=True, slots=True)
class Expedite:
    ref: str
    sku: str
    dc: str
    lane: InboundLaneInfo
    qty: int
    book_week: str
    """ISO week of the departure (capacity booking week)."""
    arrive_week: int
    eta: dt.date
    premium_per_unit: int

    @property
    def premium_cents(self) -> int:
        return self.qty * self.premium_per_unit


@dataclass(frozen=True, slots=True)
class AltBuy:
    source: AltSource
    ref: str
    """The PO line this buy replaces (reduced by ``qty``)."""
    dc: str
    sku: str
    qty: int
    week: int
    eta: dt.date
    premium_per_unit: int

    @property
    def source_key(self) -> tuple[str, str, str]:
        return (self.source.supplier_code, self.source.factory_code, self.source.lane_code)

    @property
    def premium_cents(self) -> int:
        return self.qty * self.premium_per_unit


@dataclass(frozen=True, slots=True)
class EtaMove:
    """A resequenced PO line: new eta and bucket."""

    ref: str
    eta_before: dt.date
    """The PO line's eta in the source system."""
    eta_after: dt.date
    week: int
    late: bool


@dataclass(frozen=True, slots=True)
class LineSequence:
    line_code: str
    factory_code: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    refs: tuple[str, ...]
    """PO lines whose eta this resequence changes."""


@dataclass(frozen=True, slots=True)
class AllocChange:
    allocation: Allocation
    qty_after: int


@dataclass(frozen=True, slots=True)
class Rebook:
    """The plan's choice among the decidable capacity bookings (over-booked cells)."""

    kept: frozenset[str]
    cancelled: tuple[str, ...]
    """Decidable bookings the plan cancels (sorted codes): ``capacity_booking.cancel``."""
    keys: frozenset[tuple[str, str]]
    """(dc, sku) pairs whose arrivals differ from the scenario baseline."""


def make_rebook(scn: Scenario, kept: frozenset[str] | set[str]) -> Rebook:
    kept = frozenset(kept) & frozenset(scn.decidable)
    keys = {
        (ln.dc, ln.sku)
        for code in scn.decidable
        if (code in kept) != (code in scn.baseline_kept)
        for ln in scn.bookings[code].lines
        if ln.qty
    }
    return Rebook(kept, tuple(c for c in scn.decidable if c not in kept), frozenset(keys))


def rebook_lines(scn: Scenario, lines: Iterable[RLine], kept: frozenset[str]) -> list[RLine]:
    """``lines`` (any state of the decidable bookings' PO lines) with exactly the bookings
    in ``kept`` flying and the other decidable ones cancelled. Those PO lines are locked for
    every other lever, so their lines carry no other plan change."""
    if not scn.decidable:
        return list(lines)
    states: list[tuple[str, tuple[ReceiptLine, ...]]] = []
    for code in scn.decidable:
        for ln in scn.bookings[code].lines:
            states.append((ln.ref, ln.keep if code in kept else ln.cancel))
    refs = {ref for ref, _s in states}
    out = [ln for ln in lines if ln.key not in refs]
    for _ref, state in states:
        for rl in state:
            if not rl.lost and rl.qty > 0:
                out.append(RLine(rl.ref, rl.dc, rl.sku, rl.qty, rl.week, rl.affected))
    return out


@dataclass(slots=True)
class Plan:
    transfers: list[Transfer] = field(default_factory=list)
    expedites: list[Expedite] = field(default_factory=list)
    alt: list[AltBuy] = field(default_factory=list)
    moves: dict[str, EtaMove] = field(default_factory=dict)
    sequences: list[LineSequence] = field(default_factory=list)
    alloc_changes: list[AllocChange] = field(default_factory=list)
    overrides: dict[tuple[str, str, int], dict[str, int]] = field(default_factory=dict)
    rebook: Rebook | None = None

    @property
    def empty(self) -> bool:
        return not (
            self.transfers
            or self.expedites
            or self.alt
            or self.sequences
            or self.alloc_changes
            or (self.rebook is not None and self.rebook.cancelled)
        )

    def without(self, lever: str) -> Plan:
        """This plan minus one lever's decisions."""
        p = Plan(
            transfers=list(self.transfers),
            expedites=list(self.expedites),
            alt=list(self.alt),
            moves=dict(self.moves),
            sequences=list(self.sequences),
            alloc_changes=list(self.alloc_changes),
            overrides=dict(self.overrides),
            rebook=self.rebook,
        )
        if lever == "reallocate_inventory":
            p.transfers = []
        elif lever == "expedite_mode_shift":
            p.expedites = []
        elif lever == "resource_alt_supplier":
            p.alt = []
        elif lever == "prioritize_allocation":
            p.alloc_changes, p.overrides = [], {}
        elif lever == "resequence_production":
            p.sequences, p.moves = [], {}
        return p

    # ── costs ──
    @property
    def transfer_cost(self) -> int:
        return sum(tr.qty * tr.lane.cost_per_unit_cents for tr in self.transfers)

    @property
    def expedite_premium(self) -> int:
        return sum(e.premium_cents for e in self.expedites)

    @property
    def nearshore_premium(self) -> int:
        return sum(a.premium_cents for a in self.alt)

    @property
    def incremental_cost(self) -> int:
        return self.transfer_cost + self.expedite_premium + self.nearshore_premium

    def reductions(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for e in self.expedites:
            out[e.ref] += e.qty
        for a in self.alt:
            out[a.ref] += a.qty
        return dict(out)


def base_lines(scn: Scenario) -> list[RLine]:
    """Perturbed receipt lines (lost ones do not arrive); built once per scenario, a fresh
    list per call."""
    cached: list[RLine] | None = scn.memo.get("base_lines")
    if cached is None:
        cached = scn.memo["base_lines"] = [
            RLine(rl.ref, rl.dc, rl.sku, rl.qty, rl.week, rl.affected)
            for rl in scn.receipt_lines
            if not rl.lost and rl.qty > 0
        ]
    return list(cached)


def apply_moves(lines: Iterable[RLine], moves: Mapping[str, EtaMove]) -> list[RLine]:
    return [
        RLine(ln.key, ln.dc, ln.sku, ln.qty, moves[ln.key].week, moves[ln.key].late) if ln.key in moves else ln
        for ln in lines
    ]


def plan_lines(scn: Scenario, plan: Plan, lines: list[RLine] | None = None) -> list[RLine]:
    """Receipt lines with the plan applied: the bookings it keeps / cancels (when ``lines``
    is the scenario baseline), resequence moves, then reductions (taken from the line's
    latest arrival first), then the new on-time arrivals."""
    if lines is None:
        lines = base_lines(scn)
        if plan.rebook is not None:
            lines = rebook_lines(scn, lines, plan.rebook.kept)
    current = apply_moves(lines, plan.moves)
    cut = plan.reductions()
    out: list[RLine] = []
    if cut:
        by_ref: dict[str, list[int]] = defaultdict(list)
        for i, ln in enumerate(current):
            by_ref[ln.key].append(i)
        qty = [ln.qty for ln in current]
        for ref, units in cut.items():
            for i in sorted(by_ref.get(ref, ()), key=lambda i: (-current[i].week, i)):
                take = min(units, qty[i])
                qty[i] -= take
                units -= take
                if not units:
                    break
        out = [RLine(ln.key, ln.dc, ln.sku, q, ln.week, ln.late) for ln, q in zip(current, qty, strict=True) if q > 0]
    else:
        out = current
    for e in plan.expedites:
        out.append(RLine(f"{e.ref}@{e.lane.code}", e.dc, e.sku, e.qty, e.arrive_week, False))
    for a in plan.alt:
        out.append(RLine(f"{a.source.factory_code}@{a.week}:{a.ref}", a.dc, a.sku, a.qty, a.week, False))
    return out


def receipt_series(lines: Iterable[RLine], horizon: int) -> dict[tuple[str, str], list[int]]:
    out: dict[tuple[str, str], list[int]] = {}
    for ln in lines:
        if 0 <= ln.week < horizon and ln.qty:
            out.setdefault((ln.dc, ln.sku), [0] * horizon)[ln.week] += ln.qty
    return out


def touched_keys(plan: Plan, scn: Scenario) -> set[tuple[str, str]]:
    """(dc, sku) pairs whose supply the plan changes (for partial re-simulation)."""
    keys: set[tuple[str, str]] = set()
    for tr in plan.transfers:
        keys.add((tr.lane.from_dc, tr.sku))
        keys.add((tr.lane.to_dc, tr.sku))
    for e in plan.expedites:
        keys.add((e.dc, e.sku))
    for a in plan.alt:
        keys.add((a.dc, a.sku))
    for ref in plan.moves:
        pol = scn.po_lines.get(ref)
        if pol is not None:
            keys.add((pol.dest_dc, pol.sku))
    for dc, sku, _t in plan.overrides:
        keys.add((dc, sku))
    if plan.rebook is not None:
        keys |= plan.rebook.keys
    return keys
