"""ONE linear model (GLOP via ``model_builder``) for every continuous v1 lever of a plan.

It extends the M1 ``reallocate_inventory`` LP (see :mod:`keelson_opt.levers.reallocate`;
with only that lever the model is built row for row identically). On top of the DC balance
``I[d,k,t] = I[d,k,t-1] + R + arrivals - served - outbound`` each lever adds:

``expedite_mode_shift``
  ``e[p, E] >= 0``: units of PO line ``p`` split onto inbound lane ``E`` (mode in
  ``params.modes``, destination = the line's DC, same origin as the line's lane - or an
  origin the line's factory ships from -, open for the shipment, ``allowed_lanes``).
  Departure (Merxa rule) = ``max(as_of + 1, ex_factory + readiness delay + 1)``, postponed
  to the day after a temporary closure of the lane whose window the transit would overlap
  (an open-ended closure rules the lane out) and to the first booking week with capacity;
  arrival = departure + transit. The units leave the line's (perturbed) arrival week and
  arrive in the split's week. Rows: per lane and booking (departure) week ``sum e <= weekly
  capacity x capacity_scale``; per line ``sum_E e <= open qty - 1`` (Merxa: qty below the
  open quantity); ``max_units_total``, ``max_premium_cents``. Cost: premium per unit =
  ``max(0, cost(E) - cost(line's lane))``.

``resource_alt_supplier``
  ``n[a, p, w] >= 0``: units bought from usable alternate source ``a`` (approved, compliant,
  not under ``compliance_block``, ``allowed_factories``) arriving in week ``w >=`` the week
  of ``as_of + lead_time_days (+ the wait for a temporary closure of a.lane) + transit(a.lane)``
  (eta = that date, or the Monday of a later week), replacing PO line ``p`` (same destination
  DC, style in ``a.style_codes``, another factory, arriving earlier than ``p``). The same
  units leave ``p``'s arrival week. Rows: per source and arrival week ``sum n <=
  weekly_capacity_units``; per line ``sum e + sum n <= open qty``; budgets. Cost:
  ``max(0, unit_cost(a) + cost(a.lane) - unit_cost(sku) - cost(line's lane))`` per unit.
  The MOQ is enforced after the solve by a deterministic repair (see
  :mod:`keelson_opt.planner`). M8: sources sharing a ``capacity_group`` get one row per group
  and arrival week (``sum n <=`` the smallest weekly capacity of the group's rows); the other
  M8 sourcing terms (order multiples, MOQ per style, ``max_orders``, ``max_buys_per_line``) are
  integer rules, applied by the repair or the CP-SAT stage (:mod:`keelson_opt.levers.alt_integer`).

Candidates are PO lines with a disrupted receipt (delayed or lost) unless the params list
them explicitly, never lines locked by another lever (Merxa accepts one op per PO line per
change set). A tiny ``EPS_UNIT`` cost per unit avoids pointless splits on ties.

Capacity bookings (M5, :mod:`keelson_opt.scn`): bookings that fly draw their units from
the lane's capacity in their week (``load``), so new splits only get the rest. The
bookings of an over-booked (lane, week) cell are decision variables ``k[b] in [0, 1]``
(share kept, solved together with every lever): the lines are given with those bookings
cancelled and ``k[b]`` moves the booked units from their sea arrival back to the booked
one; row per cell ``sum_b qty_b * k[b] + sum e <= capacity``. Keeping is preferred on
ties (``-EPS_UNIT`` per booked unit); the planner rounds the shares to whole bookings.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from keelson_contracts.optimizer import (
    AltSource,
    BindingConstraint,
    ExpediteModeShiftParams,
    OptimizeStatus,
    PlanWeights,
    ReallocateInventoryParams,
    ResourceAltSupplierParams,
    SolverOptions,
)
from ortools.linear_solver.python import model_builder as mb

from keelson_opt.impact import Transfer
from keelson_opt.isoweeks import iso_week_label, short_week, week_index
from keelson_opt.levers import reallocate as _r
from keelson_opt.plan import RLine
from keelson_opt.scn import InboundLaneInfo, LaneInfo, Scenario

log = logging.getLogger(__name__)

EPS_UNIT = 0.01
_ROUND_TOL = 1e-6
OBJ_SCALE = 40_000
"""Integer scale of the LP objective used by the CP-SAT stage (M8): every coefficient of the LP
(``margin * w_lost``, ``0.25 * margin * w_lost`` for buffers, ``w_cost * premium + 0.01``, with
weights in basis points / 10000) times 40000 is an exact integer."""


def unit_cost(w_tr: float, premium: int) -> float:
    """LP objective cost of one expedited or re-sourced unit (weighted cents)."""
    return w_tr * premium + EPS_UNIT


def unit_cost_scaled(transfer_cost_bp: int, premium: int) -> int:
    """:func:`unit_cost` x :data:`OBJ_SCALE` in exact integers (the CP-SAT stage's coefficient):
    ``40000 * (bp / 10000 * premium + 0.01) = 4 * bp * premium + 400``."""
    return 4 * transfer_cost_bp * premium + round(OBJ_SCALE * EPS_UNIT)


@dataclass(frozen=True, slots=True)
class ExpCand:
    ref: str
    dc: str
    sku: str
    lane: InboundLaneInfo
    ub: int
    open_qty: int
    from_week: int | None
    """The line's current arrival week (``None``: lost or after the horizon)."""
    arrive_week: int
    eta: dt.date
    book_week: int
    book_label: str
    premium: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.ref, self.lane.code)


@dataclass(frozen=True, slots=True)
class AltCand:
    source: AltSource
    ref: str
    dc: str
    sku: str
    ub: int
    open_qty: int
    from_week: int | None
    week: int
    eta: dt.date
    premium: int

    @property
    def source_key(self) -> tuple[str, str, str]:
        return (self.source.supplier_code, self.source.factory_code, self.source.lane_code)

    @property
    def key(self) -> tuple[tuple[str, str, str], str, int]:
        return (self.source_key, self.ref, self.week)


@dataclass(frozen=True, slots=True)
class KeepCand:
    """A decidable capacity booking: ``delta`` = arrivals while it flies minus arrivals
    once cancelled, per (dc, sku, week) inside the horizon."""

    code: str
    lane: str
    week: int
    label: str
    qty: int
    cap: int
    delta: tuple[tuple[str, str, int, int], ...]

    @property
    def cell(self) -> tuple[str, int]:
        return (self.lane, self.week)


def keep_candidates(scn: Scenario) -> list[KeepCand]:
    out: list[KeepCand] = []
    for code in scn.decidable:
        b = scn.bookings[code]
        delta: dict[tuple[str, str, int], int] = defaultdict(int)
        for ln in b.lines:
            for state, sign in ((ln.keep, 1), (ln.cancel, -1)):
                for rl in state:
                    if not rl.lost and rl.qty > 0 and rl.week < scn.horizon:
                        delta[(rl.dc, rl.sku, rl.week)] += sign * rl.qty
        out.append(
            KeepCand(
                code,
                b.lane,
                b.week,
                b.iso_week,
                b.qty,
                b.cap,
                tuple((dc, sku, t, u) for (dc, sku, t), u in sorted(delta.items()) if u),
            )
        )
    return out


@dataclass(slots=True)
class LeverParams:
    realloc: ReallocateInventoryParams | None = None
    expedite: ExpediteModeShiftParams | None = None
    alt: ResourceAltSupplierParams | None = None


@dataclass(slots=True)
class Fixed:
    """Phase 2: only these alternate-source variables exist (fixed to the repaired integer
    quantities); expedite stays free except on the PO lines in ``exp_blocked``. M8: with
    ``exp`` (the CP-SAT stage's whole-unit splits) only those expedite variables exist too,
    fixed like the buys, so only the transfers are re-optimised."""

    alt: dict[tuple[tuple[str, str, str], str, int], int] = field(default_factory=dict)
    exp_blocked: set[str] = field(default_factory=set)
    exp: dict[tuple[str, str], int] | None = None


@dataclass(slots=True)
class LpOut:
    status: OptimizeStatus
    status_detail: str | None
    transfers: list[Transfer]
    exp_values: dict[tuple[str, str], float]
    alt_values: dict[tuple[tuple[str, str, str], str, int], float]
    exp_cands: dict[tuple[str, str], ExpCand]
    alt_cands: dict[tuple[tuple[str, str, str], str, int], AltCand]
    binding: list[BindingConstraint]
    params: dict[str, int | str | bool]
    solve_ms: int
    warnings: list[str] = field(default_factory=list)
    keep_values: dict[str, float] = field(default_factory=dict)
    """Share kept per decidable capacity booking (M5)."""


# ───────────────────────────── candidates ─────────────────────────────


def line_weeks(scn: Scenario, lines: list[RLine]) -> tuple[dict[str, int | None], dict[str, int]]:
    """Per PO line ref: current arrival week (latest line; ``None`` = lost / after the
    horizon) and the arriving quantity (lost lines count, they can be re-sourced)."""
    week: dict[str, int | None] = {}
    qty: dict[str, int] = defaultdict(int)
    for ln in lines:
        w = ln.week if ln.week < scn.horizon else None
        cur = week.get(ln.key)
        week[ln.key] = w if ln.key not in week else (None if (cur is None or w is None) else max(cur, w))
        qty[ln.key] += ln.qty
    for rl in scn.receipt_lines:
        if rl.lost:
            week[rl.ref] = None
            qty[rl.ref] += rl.qty
    return week, dict(qty)


def _affected_refs(scn: Scenario) -> set[str]:
    return {rl.ref for rl in scn.receipt_lines if rl.affected}


def expedite_candidates(
    scn: Scenario,
    params: ExpediteModeShiftParams,
    lines: list[RLine],
    locked: set[str],
    load: Mapping[tuple[str, int], int] | None = None,
) -> tuple[list[ExpCand], list[str]]:
    warnings: list[str] = []
    modes = set(params.modes)
    allowed = set(params.allowed_lanes) if params.allowed_lanes is not None else None
    if allowed is not None and (unknown := sorted(allowed - set(scn.inbound_lanes))):
        warnings.append(f"expedite_mode_shift: allowed_lanes not present in ProblemData.inbound_lanes: {unknown}")
    refs = set(params.po_line_refs) if params.po_line_refs is not None else _affected_refs(scn)
    week, qty = line_weeks(scn, lines)
    delay: dict[str, int] = defaultdict(int)
    for rl in scn.receipt_lines:
        delay[rl.ref] = max(delay[rl.ref], rl.prod_delay)
    origins: dict[str, set[str]] = defaultdict(set)
    for p in scn.po_lines.values():
        if p.lane_code and p.lane_code in scn.inbound_lanes:
            origins[p.factory_code].add(scn.inbound_lanes[p.lane_code].origin)
    out: list[ExpCand] = []
    for ref in sorted(refs - locked):
        pol = scn.po_lines.get(ref)
        if pol is None or ref not in qty:
            continue
        ub = min(pol.qty, qty[ref]) - 1
        if ub <= 0:
            continue
        own = scn.inbound_lanes.get(pol.lane_code) if pol.lane_code else None
        ready = max(scn.as_of + dt.timedelta(days=1), pol.ex_factory + dt.timedelta(days=delay[ref] + 1))
        for code, lane in scn.inbound_lanes.items():
            if lane.mode not in modes or (allowed is not None and code not in allowed):
                continue
            if code == pol.lane_code or lane.dest_dc != pol.dest_dc:
                continue
            if own is not None:
                if lane.origin != own.origin:
                    continue
            elif lane.origin not in origins.get(pol.factory_code, ()) and lane.origin != pol.factory_code:
                continue
            dep = _departure(scn, lane, ready, load or {})
            if dep is None:
                continue
            eta = dep + dt.timedelta(days=lane.transit_days)
            arrive = scn.week_of(eta)
            cur = week.get(ref)
            if arrive >= scn.horizon or (cur is not None and arrive >= cur):
                continue
            book = week_index(dep, scn.start)
            premium = max(0, lane.cost_per_unit_cents - (own.cost_per_unit_cents if own is not None else 0))
            out.append(
                ExpCand(
                    ref, pol.dest_dc, pol.sku, lane, ub, ub + 1, cur, arrive, eta, book, iso_week_label(dep), premium
                )
            )
    return out, warnings


def _departure(
    scn: Scenario, lane: InboundLaneInfo, ready: dt.date, load: Mapping[tuple[str, int], int]
) -> dt.date | None:
    """First departure on/after ``ready`` that misses the lane's closures and books a week
    with capacity left after the bookings that fly (a week overlapping a closure has none);
    ``None`` if there is none inside the horizon."""
    dep = lane.open_departure(ready)
    while dep is not None:
        t = week_index(dep, scn.start)
        if lane.cap_in_week(t) - load.get((lane.code, t), 0) > 0:
            return dep
        if t + 1 >= scn.horizon:
            return None
        dep = lane.open_departure(scn.week_mondays[max(0, t + 1)])
    return None


def capacity_groups(scn: Scenario) -> dict[str, int]:
    """M8: weekly capacity per ``AltSource.capacity_group`` = the smallest
    ``weekly_capacity_units`` of the group's rows (every row, usable or not)."""
    out: dict[str, int] = {}
    for src in scn.data.alt_sources if scn.data is not None else ():
        if src.capacity_group is not None:
            cap = out.get(src.capacity_group)
            out[src.capacity_group] = src.weekly_capacity_units if cap is None else min(cap, src.weekly_capacity_units)
    return out


def alt_candidates(
    scn: Scenario, params: ResourceAltSupplierParams, lines: list[RLine], locked: set[str]
) -> tuple[list[AltCand], list[str]]:
    warnings: list[str] = []
    data = scn.data
    if data is None or not data.alt_sources:
        return [], ["resource_alt_supplier: ProblemData has no alt_sources"]
    allowed = set(params.allowed_factories) if params.allowed_factories is not None else None
    styles = set(params.style_codes) if params.style_codes is not None else None
    affected = _affected_refs(scn)
    week, qty = line_weeks(scn, lines)
    out: list[AltCand] = []
    for src in sorted(data.alt_sources, key=lambda s: (s.supplier_code, s.factory_code, s.lane_code)):
        if not src.usable or src.supplier_code in scn.blocked or src.factory_code in scn.blocked:
            continue
        if allowed is not None and src.factory_code not in allowed:
            continue
        lane = scn.inbound_lanes[src.lane_code]
        dep = lane.open_departure(scn.as_of + dt.timedelta(days=src.lead_time_days))
        if dep is None:
            warnings.append(f"resource_alt_supplier: lane {lane.code} of {src.factory_code} is closed")
            continue
        eta0 = dep + dt.timedelta(days=lane.transit_days)
        w0 = scn.week_of(eta0)
        src_styles = set(src.style_codes)
        for ref in sorted(set(qty) - locked):
            pol = scn.po_lines.get(ref)
            if pol is None or pol.dest_dc != lane.dest_dc or pol.factory_code == src.factory_code:
                continue
            if pol.style not in src_styles:
                continue
            if (styles is not None and pol.style not in styles) or (styles is None and ref not in affected):
                continue
            ub = min(pol.qty, qty[ref])
            if ub <= 0:
                continue
            own = scn.inbound_lanes.get(pol.lane_code) if pol.lane_code else None
            premium = max(
                0,
                src.unit_cost_cents
                + lane.cost_per_unit_cents
                - scn.cost_cents[pol.sku]
                - (own.cost_per_unit_cents if own is not None else 0),
            )
            cur = week.get(ref)
            for w in range(w0, scn.horizon):
                if cur is not None and w >= cur:
                    break
                eta = max(eta0, scn.week_mondays[w])
                cap = min(ub, src.weekly_capacity_units)
                out.append(AltCand(src, ref, pol.dest_dc, pol.sku, cap, ub, cur, w, eta, premium))
    return out, warnings


# ───────────────────────────── model ─────────────────────────────


def solve_composed(
    scn: Scenario,
    lines: list[RLine],
    levers: LeverParams,
    weights: PlanWeights,
    options: SolverOptions,
    *,
    locked: set[str] | frozenset[str] = frozenset(),
    fixed: Fixed | None = None,
    keep: list[KeepCand] | None = None,
    load: Mapping[tuple[str, int], int] | None = None,
) -> LpOut:
    t0 = time.perf_counter()
    horizon = scn.horizon
    warnings: list[str] = []
    w_lost = weights.lost_margin / 10000.0
    w_tr = weights.transfer_cost / 10000.0
    receipts: dict[tuple[str, str], list[int]] = {}
    for rl in lines:
        if 0 <= rl.week < horizon and rl.qty:
            receipts.setdefault((rl.dc, rl.sku), [0] * horizon)[rl.week] += rl.qty

    # ── lanes for transfers (M1) ──
    lanes: list[LaneInfo] = []
    if levers.realloc is not None:
        params = levers.realloc
        lane_codes = [ln.code for ln in scn.lanes]
        if params.allowed_lanes is None:
            allowed = set(lane_codes)
        else:
            allowed = set(params.allowed_lanes)
            unknown = sorted(allowed - set(lane_codes))
            if unknown:
                warnings.append(f"allowed_lanes not present in ProblemData.lanes: {unknown}")
        lanes = [ln for ln in scn.lanes if ln.code in allowed and any(ln.caps)]
    has_out = {ln.from_dc for ln in lanes}

    # ── expedite / alternate-source candidates ──
    exp_cands: list[ExpCand] = []
    alt_cands: list[AltCand] = []
    if levers.expedite is not None:
        exp_cands, cand_warnings = expedite_candidates(scn, levers.expedite, lines, set(locked), load)
        warnings.extend(cand_warnings)
    if levers.alt is not None:
        alt_cands, cand_warnings = alt_candidates(scn, levers.alt, lines, set(locked))
        warnings.extend(cand_warnings)
    if fixed is not None:
        exp_cands = [c for c in exp_cands if c.ref not in fixed.exp_blocked]
        if fixed.exp is not None:
            exp_cands = [c for c in exp_cands if fixed.exp.get(c.key, 0) > 0]
        alt_cands = [c for c in alt_cands if fixed.alt.get(c.key, 0) > 0]

    reqs = _r._requirements(scn)
    dc_future: dict[tuple[str, str], list[int]] = {}
    for (cl, sku), (sales, buffer) in reqs.items():
        acc = dc_future.setdefault((scn.cluster_dc[cl], sku), [0] * (horizon + 1))
        for t in range(horizon):
            acc[t] += sales[t] + buffer[t]
    for acc in dc_future.values():
        for t in range(horizon - 1, -1, -1):
            acc[t] += acc[t + 1]

    model = mb.Model()  # type: ignore[no-untyped-call]
    model.name = "plan" if (exp_cands or alt_cands or keep) else "reallocate_inventory"
    lp = _r._LpBuilder(model)

    # ── transfer variables x[lane, sku, ship_week] (M1, unchanged order) ──
    x: dict[tuple[str, str, int], int] = {}
    x_lane: dict[str, LaneInfo] = {ln.code: ln for ln in lanes}
    out_by_dk: dict[tuple[str, str], list[int]] = defaultdict(list)
    in_by_dkt: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    by_lane_week: dict[tuple[str, int], list[int]] = defaultdict(list)
    for ln in lanes:
        for sku in scn.skus:
            if scn.dc_atp.get((ln.from_dc, sku), 0) <= 0:
                continue
            future = dc_future.get((ln.to_dc, sku))
            for t in range(scn.first_ship_week, horizon - ln.lag_weeks):
                arrive = t + ln.lag_weeks
                if ln.caps[t] <= 0:
                    continue
                if (future is None or future[arrive] <= 0) and ln.to_dc not in has_out:
                    continue
                v = lp.var(float(ln.caps[t]), ln.cost_per_unit_cents * w_tr + _r.EPS_SHIP_DELAY_CENTS * t)
                x[(ln.code, sku, t)] = v
                out_by_dk[(ln.from_dc, sku)].append(v)
                in_by_dkt[(ln.to_dc, sku, arrive)].append(v)
                by_lane_week[(ln.code, t)].append(v)

    # ── served s / lost l per cluster x SKU x week and part:  s + l = requirement ──
    served_by_dkt: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    n_demand = 0
    for (cl, sku), (sales, buffer) in reqs.items():
        dc = scn.cluster_dc[cl]
        margin = (scn.price_cents[sku] - scn.cost_cents[sku]) * w_lost
        for t in range(horizon):
            for need, penalty in ((sales[t], margin), (buffer[t], margin * _r.BUFFER_LOST_FACTOR)):
                if need <= 0:
                    continue
                s = lp.var(float(need))
                lost = lp.var(float(need), penalty)
                lp.row((s, lost), (1.0, 1.0), float(need), float(need))
                served_by_dkt[(dc, sku, t)].append(s)
                n_demand += 1

    # ── expedite e / alternate n variables ──
    plus: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    minus: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    e_vars: dict[tuple[str, str], int] = {}
    n_vars: dict[tuple[tuple[str, str, str], str, int], int] = {}
    for c in exp_cands:
        if fixed is not None and fixed.exp is not None:
            q = float(fixed.exp[c.key])
            v = lp.var(q, unit_cost(w_tr, c.premium))
            model.helper.set_var_lower_bound(v, q)
        else:
            v = lp.var(float(c.ub), unit_cost(w_tr, c.premium))
        e_vars[c.key] = v
        plus[(c.dc, c.sku, c.arrive_week)].append(v)
        if c.from_week is not None:
            minus[(c.dc, c.sku, c.from_week)].append(v)
    for a in alt_cands:
        if fixed is not None:
            q = float(fixed.alt[a.key])
            v = lp.var(q, unit_cost(w_tr, a.premium))
            model.helper.set_var_lower_bound(v, q)
        else:
            v = lp.var(float(a.ub), unit_cost(w_tr, a.premium))
        n_vars[a.key] = v
        plus[(a.dc, a.sku, a.week)].append(v)
        if a.from_week is not None:
            minus[(a.dc, a.sku, a.from_week)].append(v)

    # ── kept shares of the decidable capacity bookings (M5) ──
    k_vars: dict[str, int] = {}
    flow: dict[tuple[str, str, int], list[tuple[int, float]]] = defaultdict(list)
    for kc in keep or ():
        v = lp.var(1.0, -EPS_UNIT * kc.qty)
        k_vars[kc.code] = v
        for dc, sku, t, units in kc.delta:
            flow[(dc, sku, t)].append((v, -float(units)))

    # ── DC balances, only for DC x SKU pairs that serve demand or move stock ──
    active_dk = sorted(
        {(d, k) for (d, k, _t) in served_by_dkt}
        | set(out_by_dk)
        | {(d, k) for (d, k, _t) in in_by_dkt}
        | {(d, k) for (d, k, _t) in plus}
        | {(d, k) for (d, k, _t) in flow}
    )
    caps: list[_r._Cap] = []
    for dc, sku in active_dk:
        rec = receipts.get((dc, sku))
        outs = out_by_dk.get((dc, sku), [])
        prev = -1
        for t in range(horizon):
            inv = lp.var(math.inf)
            idx = [inv]
            coef = [1.0]
            if prev >= 0:
                idx.append(prev)
                coef.append(-1.0)
            arrivals = in_by_dkt.get((dc, sku, t), ())
            idx.extend(arrivals)
            coef.extend([-1.0] * len(arrivals))
            served = served_by_dkt.get((dc, sku, t), ())
            idx.extend(served)
            coef.extend([1.0] * len(served))
            extra_in = plus.get((dc, sku, t), ())
            idx.extend(extra_in)
            coef.extend([-1.0] * len(extra_in))
            extra_out = minus.get((dc, sku, t), ())
            idx.extend(extra_out)
            coef.extend([1.0] * len(extra_out))
            for v, k_coef in flow.get((dc, sku, t), ()):
                idx.append(v)
                coef.append(k_coef)
            rhs = float(rec[t] if rec else 0)
            if t == 0:
                rhs += scn.dc_supply.get((dc, sku), 0)
                idx.extend(outs)
                coef.extend([1.0] * len(outs))
            lp.row(idx, coef, rhs, rhs)
            prev = inv
        if outs:
            atp = float(scn.dc_atp.get((dc, sku), 0))
            name = f"supply {dc} {sku} ATP"
            caps.append(_r._Cap(name, lp.row(outs, [1.0] * len(outs), -math.inf, atp, name), atp))

    # ── lane capacities per ship week, total cap (M1) ──
    for (code, t), vs in sorted(by_lane_week.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        cap = float(x_lane[code].caps[t])
        name = f"lane {code} cap {short_week(scn.weeks[t])}"
        caps.append(_r._Cap(name, lp.row(vs, [1.0] * len(vs), -math.inf, cap, name), cap))
    if levers.realloc is not None and levers.realloc.max_units_total is not None and x:
        xs = list(x.values())
        cap = float(levers.realloc.max_units_total)
        caps.append(_r._Cap("max_units_total", lp.row(xs, [1.0] * len(xs), -math.inf, cap, "max_units_total"), cap))

    # ── expedite / alternate rows ──
    def cap_row(name: str, vs: list[int], ub: float, coef: list[float] | None = None) -> None:
        if vs:
            caps.append(_r._Cap(name, lp.row(vs, coef or [1.0] * len(vs), -math.inf, ub, name), ub))

    by_book: dict[tuple[str, int], list[ExpCand]] = defaultdict(list)
    for c in exp_cands:
        by_book[(c.lane.code, c.book_week)].append(c)
    keep_by_cell: dict[tuple[str, int], list[KeepCand]] = defaultdict(list)
    for kc in keep or ():
        keep_by_cell[kc.cell].append(kc)
    drawn = load or {}
    for cell in sorted(set(by_book) | set(keep_by_cell)):
        code, bw = cell
        cs, ks = by_book.get(cell, []), keep_by_cell.get(cell, [])
        vs = [e_vars[c.key] for c in cs] + [k_vars[k.code] for k in ks]
        cf = [1.0] * len(cs) + [float(k.qty) for k in ks]
        if ks:
            name = f"booked lane {code} cap {short_week(ks[0].label)}"
            cap = ks[0].cap
        else:
            name = f"expedite lane {code} cap {short_week(cs[0].book_label)}"
            cap = cs[0].lane.cap_in_week(bw)
        cap_row(name, vs, float(max(0, cap - drawn.get(cell, 0))), cf)
    by_src_week: dict[tuple[tuple[str, str, str], int], list[AltCand]] = defaultdict(list)
    for a in alt_cands:
        by_src_week[(a.source_key, a.week)].append(a)
    for (skey, w), cands in sorted(by_src_week.items()):
        cap_row(
            f"alt source {skey[1]}>{skey[2]} cap {short_week(scn.weeks[w])}",
            [n_vars[a.key] for a in cands],
            float(cands[0].source.weekly_capacity_units),
        )
    group_caps = capacity_groups(scn)
    by_group_week: dict[tuple[str, int], list[AltCand]] = defaultdict(list)
    for a in alt_cands:
        if a.source.capacity_group is not None:
            by_group_week[(a.source.capacity_group, a.week)].append(a)
    for (group, w), cands in sorted(by_group_week.items()):
        cap_row(
            f"alt group {group} cap {short_week(scn.weeks[w])}",
            [n_vars[a.key] for a in cands],
            float(group_caps[group]),
        )
    per_ref_e: dict[str, list[ExpCand]] = defaultdict(list)
    for c in exp_cands:
        per_ref_e[c.ref].append(c)
    per_ref_n: dict[str, list[AltCand]] = defaultdict(list)
    for a in alt_cands:
        per_ref_n[a.ref].append(a)
    for ref in sorted(set(per_ref_e) | set(per_ref_n)):
        es, ns = per_ref_e.get(ref, []), per_ref_n.get(ref, [])
        if len(es) > 1:
            cap_row(f"po_line {ref} split qty", [e_vars[c.key] for c in es], float(es[0].open_qty - 1))
        vs = [e_vars[c.key] for c in es] + [n_vars[a.key] for a in ns]
        if len(vs) > 1:
            open_qty = es[0].open_qty if es else ns[0].open_qty
            cap_row(f"po_line {ref} open qty", vs, float(open_qty))
    if levers.expedite is not None and exp_cands:
        vs = [e_vars[c.key] for c in exp_cands]
        if levers.expedite.max_units_total is not None:
            cap_row("expedite max_units_total", vs, float(levers.expedite.max_units_total))
        if levers.expedite.max_premium_cents is not None:
            prem = [float(c.premium) for c in exp_cands]
            cap_row("expedite max_premium_cents", vs, float(levers.expedite.max_premium_cents), prem)
    if levers.alt is not None and alt_cands:
        vs = [n_vars[a.key] for a in alt_cands]
        if levers.alt.max_units_total is not None:
            cap_row("alt max_units_total", vs, float(levers.alt.max_units_total))
        if levers.alt.max_premium_cents is not None:
            prem = [float(a.premium) for a in alt_cands]
            cap_row("alt max_premium_cents", vs, float(levers.alt.max_premium_cents), prem)

    lp.set_objective()

    glop_params = f"random_seed:{options.seed}"
    solver = mb.Solver("glop")
    if not solver.solver_is_supported():  # pragma: no cover - wheel always ships GLOP
        raise RuntimeError("GLOP solver backend is not available in this OR-Tools build")
    solver.set_time_limit_in_seconds(float(options.time_limit_s))
    solver.set_solver_specific_parameters(glop_params)
    t_build = time.perf_counter()
    raw_status = solver.solve(model)  # lowercase solve(); no solution callback (broken in 9.15)
    t_solve = time.perf_counter()

    params_out: dict[str, int | str | bool] = {
        "time_limit_s": options.time_limit_s,
        "glop_parameters": glop_params,
        "rounding": "floor",
        "num_variables": model.num_variables,
        "num_constraints": model.num_constraints,
        "weight_lost_margin_bp": weights.lost_margin,
        "weight_transfer_cost_bp": weights.transfer_cost,
    }
    status, detail = _r._map_status(raw_status, solver)
    transfers: list[Transfer] = []
    binding: list[BindingConstraint] = []
    exp_values: dict[tuple[str, str], float] = {}
    alt_values: dict[tuple[tuple[str, str, str], str, int], float] = {}
    keep_values: dict[str, float] = {}
    if status in ("OPTIMAL", "FEASIBLE"):
        tagged: dict[tuple[str, object], int] = {("x", k): v for k, v in x.items()}
        tagged.update({("e", k): v for k, v in e_vars.items()})
        tagged.update({("n", k): v for k, v in n_vars.items()})
        tagged.update({("k", k): v for k, v in k_vars.items()})
        values = _r._primal_values(solver, model.helper, tagged)
        if values is None:
            status, detail = "INFEASIBLE", f"{raw_status.name}: solver reported no primal solution"
        else:
            for code, sku, t in x:
                qty = math.floor(values[("x", (code, sku, t))] + _ROUND_TOL)
                if qty > 0:
                    transfers.append(Transfer(lane=x_lane[code], sku=sku, qty=qty, ship_week=t))
            exp_values = {k: values[("e", k)] for k in e_vars}
            alt_values = {k: values[("n", k)] for k in n_vars}
            keep_values = {k: values[("k", k)] for k in k_vars}
            transfers.sort(key=lambda tr: (tr.ship_week, tr.lane.from_dc, tr.lane.to_dc, tr.sku))
            if status == "OPTIMAL":
                binding = _r._binding(solver, model.helper, caps)
    solve_ms = round((time.perf_counter() - t0) * 1000)
    log.info(
        "plan model solved",
        extra={
            "status": status,
            "raw_status": raw_status.name,
            "num_variables": model.num_variables,
            "num_constraints": model.num_constraints,
            "demand_rows": n_demand,
            "expedite_vars": len(e_vars),
            "alt_vars": len(n_vars),
            "keep_vars": len(k_vars),
            "build_ms": int((t_build - t0) * 1000),
            "glop_ms": int((t_solve - t_build) * 1000),
            "transfers": len(transfers),
        },
    )
    return LpOut(
        status=status,
        status_detail=detail,
        transfers=transfers,
        exp_values=exp_values,
        alt_values=alt_values,
        exp_cands={c.key: c for c in exp_cands},
        alt_cands={a.key: a for a in alt_cands},
        binding=binding,
        params=params_out,
        solve_ms=solve_ms,
        warnings=warnings,
        keep_values=keep_values,
    )
