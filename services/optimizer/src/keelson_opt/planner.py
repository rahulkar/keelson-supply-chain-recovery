"""Plan construction for ``/v1/optimize``: every lever of a plan spec in one pass.

1. ``resequence_production`` (combinatorial, :mod:`keelson_opt.levers.resequence`) moves PO
   line arrivals; moved lines are locked for the other levers (Merxa accepts one op per PO
   line per change set).
2. ONE LP (:mod:`keelson_opt.levers.composed`) for transfers, expedite splits,
   alternate-source buys and the allocation split, over the resequenced arrivals.
3. Integer repair, deterministic: every quantity is floored; a line both split and
   re-sourced stays with the larger of the two (ties: expedite); a purchase order below the
   source's MOQ is either topped up to the MOQ (headroom on its lines, in order of current
   quantity, within capacity and budgets) or dropped, whichever gives the higher
   recomputed weighted net benefit (ties: drop). When the LP bought anything from an
   alternate source, it is solved again (phase 2) with the repaired buys fixed and
   expedite forbidden on the re-sourced lines, so splits, transfers and allocations adapt.
   Expedited lines keep their largest split only (one ``po_line.split`` per line).
   When the resequence moved PO lines and expedite / re-sourcing is requested, the LP levers
   are also solved without the resequence (its lines unlocked); the better phase-1 plan by
   recomputed weighted net benefit is kept (ties: with the resequence) and only it runs
   phase 2 - locking the lines must not make the plan worse than the other levers alone.
4. ``prioritize_allocation``: in the (dc, sku, week) cells that stay scarce under the
   plan, the clusters' allocations become the single-week optimum split (protected
   allocations, then sales needs, then buffers; see :func:`_allocations`), limited to
   ``max_changes``. Not an LP decision: the LP treats protected-minimum buffers as
   rotating (non-sellable) stock while the simulator sells them under shortage, and mapping
   the LP's cluster split onto allocations lost margin on the full-profile story.
5. Never worse than doing nothing: if the rounded plan does not improve the recomputed
   weighted net benefit ``w_lost * margin recovered - w_cost * incremental cost`` the
   do-nothing plan is returned; with several levers, a lever whose decisions lower that
   net benefit is dropped first (catalogue order).

M5 re-planning (capacity bookings, :mod:`keelson_opt.scn`): bookings that fly draw lane
capacity, and their PO lines are locked for every lever (already-applied decisions are
fixed; the plan is a delta). When a lane's capacity in a week is below what is booked, the
cell's bookings are decided first, by the same LP with every chosen lever plus a kept
share per booking (lost margin and urgency come from the demand rows; :func:`_decide_bookings`):
shares are rounded to whole bookings (largest share first, ties by code, each one that
fits), then the levers are solved around that choice. In step 5 the choice must beat the
baseline (first-come-first-served, see ``scn``) on the PO lines it changes, else the
baseline choice is used; the do-nothing fallback still cancels the bookings that do not
fit (the baseline's), because the capacity cut leaves no plan that keeps them. A plan
spec without levers stays the pure baseline (no actions).
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction

from keelson_contracts.optimizer import (
    LEVER_CODES,
    Allocation,
    BindingConstraint,
    OptimizeStatus,
    PlanSpec,
    PrioritizeAllocationParams,
    SolverOptions,
)

from keelson_opt.impact import SimResult, lost_margin, simulate
from keelson_opt.levers import composed
from keelson_opt.levers.composed import AltCand, ExpCand, Fixed, LeverParams, LpOut, keep_candidates
from keelson_opt.levers.resequence import cpsat_workers, plan_resequence
from keelson_opt.plan import (
    AllocChange,
    AltBuy,
    Expedite,
    Plan,
    RLine,
    apply_moves,
    base_lines,
    make_rebook,
    plan_lines,
    rebook_lines,
    receipt_series,
    touched_keys,
)
from keelson_opt.scn import InboundLaneInfo, Scenario

log = logging.getLogger(__name__)

_TOL = 1e-6
MAX_FRACTIONAL_OPTIONS = 16
"""Booking rounding: at most this many "fractional booking first" candidates are scored by
the simulator (the most-kept shares first), bounding the work on huge requests."""


@dataclass(slots=True)
class PlanOutcome:
    status: OptimizeStatus
    status_detail: str | None
    plan: Plan
    binding: list[BindingConstraint]
    params: dict[str, int | str | bool]
    solve_ms: int
    solver_name: str
    warnings: list[str] = field(default_factory=list)


def weighted_net(scn: Scenario, plan: Plan, spec: PlanSpec, margin_before: int) -> int:
    """``w_lost * (margin before - margin after) - w_cost * incremental cost`` (exact ints)."""
    margin_after = lost_margin(evaluate(scn, plan))
    w = spec.weights
    return w.lost_margin * (margin_before - margin_after) - w.transfer_cost * plan.incremental_cost


def evaluate(
    scn: Scenario,
    plan: Plan,
    keys: set[tuple[str, str]] | None = None,
    priority: dict[tuple[str, str, int], dict[str, int]] | None = None,
) -> SimResult:
    lines = plan_lines(scn, plan)
    return simulate(
        scn,
        plan.transfers,
        receipts=receipt_series(lines, scn.horizon),
        overrides=plan.overrides,
        keys=keys,
        priority=priority,
    )


def make_plan(scn: Scenario, spec: PlanSpec, options: SolverOptions, margin_before: int) -> PlanOutcome:
    levers = {lv.code: lv.params for lv in spec.levers}
    params: dict[str, int | str | bool] = {"levers": ",".join(lv.code for lv in spec.levers) or "none"}
    warnings: list[str] = []
    plan = Plan()
    if not levers:
        return PlanOutcome("OPTIMAL", None, plan, [], params, 0, "none", warnings)

    names: list[str] = []
    status: OptimizeStatus = "OPTIMAL"
    detail: str | None = None
    binding: list[BindingConstraint] = []
    solve_ms = 0
    lines0 = base_lines(scn)

    reseq = levers.get("resequence_production")
    reseq_name: str | None = None
    if reseq is not None:
        t0 = time.perf_counter()
        rs = plan_resequence(
            scn,
            lines0,
            reseq,  # type: ignore[arg-type]
            seed=options.seed,
            w_lost_bp=spec.weights.lost_margin,
            # every booked PO line is an already-applied decision (not only the over-booked
            # cells' lines): moving one would make it miss its booked departure
            pinned=scn.booked_refs,
        )
        solve_ms += round((time.perf_counter() - t0) * 1000)
        plan.moves, plan.sequences = rs.moves, rs.sequences
        warnings.extend(rs.warnings)
        if rs.methods:
            params["resequence_methods"] = ",".join(f"{k}:{v}" for k, v in sorted(rs.methods.items()))
        if any(m.startswith("cpsat") for m in rs.methods.values()):
            params["cpsat_num_workers"] = cpsat_workers()
        methods = set(rs.methods.values())
        if any(m.startswith("cpsat") for m in methods):
            reseq_name = "CP-SAT"
        elif "greedy" in methods:
            reseq_name = "greedy-sequencing"
        elif methods:
            reseq_name = "exact-sequencing"

    lp_levers = LeverParams(
        realloc=levers.get("reallocate_inventory"),  # type: ignore[arg-type]
        expedite=levers.get("expedite_mode_shift"),  # type: ignore[arg-type]
        alt=levers.get("resource_alt_supplier"),  # type: ignore[arg-type]
    )
    booked = set(scn.booked_refs)
    if scn.decidable:
        kept, k_ms, k_warn = _decide_bookings(
            scn, apply_moves(lines0, plan.moves), lp_levers, spec, options, set(plan.moves) | booked
        )
        plan.rebook = make_rebook(scn, kept)
        solve_ms += k_ms
        # lever warnings of that solve repeat in the levers' own solve below
        warnings.extend(w for w in k_warn if w.startswith("capacity_bookings:"))
        names.append("GLOP")
        lines0 = rebook_lines(scn, lines0, plan.rebook.kept)
    load = scn.booking_load(plan.rebook.kept if plan.rebook is not None else None)
    if any((lp_levers.realloc, lp_levers.expedite, lp_levers.alt)):
        if "GLOP" not in names:
            names.append("GLOP")
        compare = bool(plan.moves) and bool(lp_levers.expedite or lp_levers.alt)
        lp = _lp_pass(
            scn,
            apply_moves(lines0, plan.moves),
            lp_levers,
            spec,
            options,
            set(plan.moves) | booked,
            plan,
            not compare,
            load,
        )
        if compare and lp.ok:
            # Resequenced PO lines are locked for expedite / re-sourcing (one op per PO line):
            # keep the resequence only if the (phase-1) plan is at least as good as the other
            # levers acting on those lines instead; phase 2 then runs for the winner only.
            other = Plan(rebook=plan.rebook)
            lp2 = _lp_pass(scn, lines0, lp_levers, spec, options, set(booked), other, False, load)
            if lp2.ok and weighted_net(scn, other, spec, margin_before) > weighted_net(scn, plan, spec, margin_before):
                warnings.append(
                    "resequence_production: dropped; the other levers recover more on the PO lines it would lock"
                )
                params["resequence_dropped"] = True
                lp2.solve_ms += lp.solve_ms
                plan, lp, reseq_name = other, lp2, None
            else:
                lp.solve_ms += lp2.solve_ms
            _phase2(scn, lp_levers, spec, options, plan, lp)
        solve_ms += lp.solve_ms
        warnings.extend(lp.warnings)
        params.update(lp.params)
        status, detail, binding = lp.status, lp.status_detail, lp.binding
        if not lp.ok:
            plan = Plan(rebook=plan.rebook)  # no usable LP solution: nothing from any lever
    if reseq_name is not None:
        names.append(reseq_name)
    prio = levers.get("prioritize_allocation")
    if prio is not None and status in ("OPTIMAL", "FEASIBLE"):
        _allocations(scn, plan, prio, warnings)  # type: ignore[arg-type]
        names.append("priority-split")

    if not plan.empty:
        plan = _keep_improving(scn, spec, plan, margin_before, params, warnings)
        if plan.empty:
            binding = []
    if scn.decidable:
        params["bookings_decidable"] = len(scn.decidable)
        params["bookings_cancelled"] = len(plan.rebook.cancelled) if plan.rebook is not None else 0
    return PlanOutcome(status, detail, plan, binding, params, solve_ms, "+".join(names) or "none", warnings)


@dataclass(slots=True)
class _LpPass:
    status: OptimizeStatus
    status_detail: str | None
    binding: list[BindingConstraint]
    params: dict[str, int | str | bool]
    warnings: list[str]
    solve_ms: int
    lines: list[RLine] = field(default_factory=list)
    locked: set[str] = field(default_factory=set)
    load: dict[tuple[str, int], int] = field(default_factory=dict)
    out: LpOut | None = None
    alt: list[tuple[AltCand, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def _lp_pass(
    scn: Scenario,
    lines: list[RLine],
    lp_levers: LeverParams,
    spec: PlanSpec,
    options: SolverOptions,
    locked: set[str],
    plan: Plan,
    phase2: bool = True,
    load: dict[tuple[str, int], int] | None = None,
) -> _LpPass:
    """The LP levers on ``lines`` (``locked`` PO lines excluded, ``load`` = lane capacity
    drawn by the bookings that fly): phase 1 and the integer repair, then (``phase2``) the
    re-solve with the buys fixed; the rounded transfers, buys and splits are written into
    ``plan``."""
    load = load or {}
    out = composed.solve_composed(scn, lines, lp_levers, spec.weights, options, locked=locked, load=load)
    lp = _LpPass(
        out.status, out.status_detail, [], dict(out.params), list(out.warnings), out.solve_ms, lines, locked, load
    )
    if not lp.ok:
        return lp
    lp.out, lp.binding = out, out.binding
    lp.alt = _alt_decisions(scn, out, spec, plan, lp_levers)
    _write(plan, lp)
    if phase2:
        _phase2(scn, lp_levers, spec, options, plan, lp)
    return lp


def _write(plan: Plan, lp: _LpPass) -> None:
    if lp.out is None:
        return
    blocked = {a.ref for a, _q in lp.alt}
    plan.transfers = lp.out.transfers
    plan.alt = [_alt(a, q) for a, q in lp.alt]
    plan.expedites = [_expedite(c, q) for c, q in _exp_decisions(lp.out, blocked)]


def _phase2(
    scn: Scenario, lp_levers: LeverParams, spec: PlanSpec, options: SolverOptions, plan: Plan, lp: _LpPass
) -> None:
    """Phase 2: alternate buys fixed to their repaired integers, expedite and transfers
    re-optimised around them (lines bought elsewhere cannot split)."""
    if lp.out is None or not any(v >= 1 - _TOL for v in lp.out.alt_values.values()):
        return
    fixed = Fixed(alt={a.key: q for a, q in lp.alt}, exp_blocked={a.ref for a, _q in lp.alt})
    out2 = composed.solve_composed(
        scn, lp.lines, lp_levers, spec.weights, options, locked=lp.locked, fixed=fixed, load=lp.load
    )
    lp.solve_ms += out2.solve_ms
    lp.params["phase2"] = True
    if out2.status in ("OPTIMAL", "FEASIBLE"):
        lp.out, lp.binding = out2, out2.binding
        lp.status, lp.status_detail = out2.status, out2.status_detail
        _write(plan, lp)
    else:
        lp.warnings.append(f"phase-2 re-solve failed ({out2.status_detail}); phase-1 decisions kept")


# ───────────────────────────── capacity bookings (M5) ─────────────────────────────


def _decide_bookings(
    scn: Scenario,
    lines: list[RLine],
    lp_levers: LeverParams,
    spec: PlanSpec,
    options: SolverOptions,
    locked: set[str],
) -> tuple[frozenset[str], int, list[str]]:
    """Which decidable bookings fly: the plan LP (every chosen lever) with a kept share per
    booking, then integer rounding per over-booked cell - each order below keeps every
    booking that still fits, in that order:

    1. largest kept share first (ties: higher margin per booked unit, then code);
    2. higher margin per booked unit first (ties: larger share, then code);
    3. for each fractionally kept booking, that booking first, then order 1;
    4. the baseline (first-come-first-served).

    The candidate sets are scored by the simulator (lost margin on the bookings' DC x SKU
    pairs, with the LP's transfers); the lowest wins, ties in the order above. Falls back
    to the baseline choice when the LP has no solution."""
    cands = keep_candidates(scn)
    out = composed.solve_composed(
        scn,
        rebook_lines(scn, lines, frozenset()),
        lp_levers,
        spec.weights,
        options,
        locked=locked,
        keep=cands,
        load=scn.booking_load(frozenset()),
    )
    warnings = list(out.warnings)
    if out.status not in ("OPTIMAL", "FEASIBLE"):
        warnings.append(
            f"capacity_bookings: the booking decision model failed ({out.status_detail}); "
            "the first-come-first-served choice is used"
        )
        return scn.baseline_kept, out.solve_ms, warnings
    share = {c.code: round(out.keep_values.get(c.code, 0.0), 6) for c in cands}
    density: dict[str, Fraction] = {}
    for c in cands:
        b = scn.bookings[c.code]
        value = sum(ln.qty * (scn.price_cents[ln.sku] - scn.cost_cents[ln.sku]) for ln in b.lines)
        density[c.code] = Fraction(value, max(1, b.qty))
    fixed = scn.booking_load(frozenset())
    by_cell: dict[tuple[str, int], list[str]] = defaultdict(list)
    for c in cands:
        by_cell[c.cell].append(c.code)

    def fill(rank: Callable[[str], tuple[object, ...]]) -> frozenset[str]:
        kept: set[str] = set()
        for cell in sorted(by_cell):
            room = scn.bookings[by_cell[cell][0]].cap - fixed.get(cell, 0)
            for code in sorted(by_cell[cell], key=rank):
                if scn.bookings[code].qty <= room:
                    kept.add(code)
                    room -= scn.bookings[code].qty
        return frozenset(kept)

    def by_share(code: str) -> tuple[object, ...]:
        return (-share[code], -density[code], code)

    options_k = [fill(by_share), fill(lambda c: (-density[c], -share[c], c))]
    fractional = sorted((c for c, v in share.items() if 0 < v < 1), key=by_share)[:MAX_FRACTIONAL_OPTIONS]
    for f in sorted(fractional):
        options_k.append(fill(lambda c, f=f: (c != f, *by_share(c))))  # type: ignore[misc]
    options_k.append(scn.baseline_kept)
    unique = list(dict.fromkeys(options_k))
    if len(unique) == 1:
        return unique[0], out.solve_ms, warnings
    keys = {(ln.dc, ln.sku) for code in scn.decidable for ln in scn.bookings[code].lines}

    def lost(kept: frozenset[str]) -> int:
        arr = receipt_series(rebook_lines(scn, lines, kept), scn.horizon)
        return lost_margin(simulate(scn, out.transfers, receipts=arr, keys=keys))

    scores = [lost(k) for k in unique]
    best = min(range(len(unique)), key=lambda i: (scores[i], i))
    return unique[best], out.solve_ms, warnings


# ───────────────────────────── integer repair ─────────────────────────────


def _exp_decisions(out: LpOut, blocked: set[str]) -> list[tuple[ExpCand, int]]:
    """Floored splits, one lane per PO line (the largest; ties by lane code)."""
    best: dict[str, tuple[int, str]] = {}
    for (ref, lane), value in sorted(out.exp_values.items()):
        q = math.floor(value + _TOL)
        if q > 0 and ref not in blocked and (ref not in best or q > best[ref][0]):
            best[ref] = (q, lane)
    return [(out.exp_cands[(ref, lane)], q) for ref, (q, lane) in sorted(best.items())]


AltKey = tuple[tuple[str, str, str], str, int]


def _alt_decisions(
    scn: Scenario, out: LpOut, spec: PlanSpec, plan: Plan, lp_levers: LeverParams
) -> list[tuple[AltCand, int]]:
    """Floored alternate buys; a line the LP also splits stays with the larger of the two
    (ties: expedite); then the MOQ repair."""
    exp = {c.ref: q for c, q in _exp_decisions(out, set())}
    alt_q: dict[AltKey, int] = {}
    for key, value in sorted(out.alt_values.items()):
        q = math.floor(value + _TOL)
        if q > 0:
            alt_q[key] = q
    per_ref: dict[str, int] = defaultdict(int)
    for (_s, ref, _w), q in alt_q.items():
        per_ref[ref] += q
    alt_q = {k: q for k, q in alt_q.items() if per_ref[k[1]] > exp.get(k[1], 0)}
    alt_q = _moq_repair(scn, out, spec, plan, lp_levers, exp, alt_q)
    return [(out.alt_cands[k], q) for k, q in sorted(alt_q.items())]


def _moq_repair(
    scn: Scenario,
    out: LpOut,
    spec: PlanSpec,
    plan: Plan,
    lp_levers: LeverParams,
    exp: dict[str, int],
    alt_q: dict[AltKey, int],
) -> dict[AltKey, int]:
    """Each purchase order (source, arrival week) below the source MOQ is topped up to the
    MOQ (headroom of its candidate lines, largest current quantity first, within capacity,
    open quantities and the lever budgets) or dropped - whichever has the higher recomputed
    weighted net benefit on the affected DC x SKU pairs (ties: drop). A dropped line is
    valued with the split the expedite lever would likely make instead."""
    params = lp_levers.alt
    by_po: dict[tuple[tuple[str, str, str], int], list[AltKey]] = defaultdict(list)
    for key in alt_q:
        by_po[(key[0], key[2])].append(key)
    for (skey, week), keys in sorted(by_po.items()):
        src = out.alt_cands[keys[0]].source
        total = sum(alt_q[k] for k in keys)
        if src.moq_units <= total:
            continue
        down = {k: v for k, v in alt_q.items() if k not in keys}
        up: dict[AltKey, int] | None = None
        need = src.moq_units - total
        if src.moq_units <= src.weekly_capacity_units:
            up = dict(alt_q)
            used: dict[str, int] = defaultdict(int)
            for k, v in alt_q.items():
                used[k[1]] += v
            pool = sorted(
                (c for c in out.alt_cands.values() if c.source_key == skey and c.week == week and c.ref not in exp),
                key=lambda c: (-alt_q.get(c.key, 0), c.ref),
            )
            for c in pool:
                if not need:
                    break
                add = min(max(0, min(c.ub - up.get(c.key, 0), c.open_qty - used[c.ref])), need)
                if add:
                    up[c.key] = up.get(c.key, 0) + add
                    used[c.ref] += add
                    need -= add
            if need or (params is not None and not _within_budget(out, up, params)):
                up = None
        if up is None:
            alt_q = down
            continue
        changed = {k for k in set(up) | set(down) if up.get(k, 0) != down.get(k, 0)}
        keys_touched = {(out.alt_cands[k].dc, out.alt_cands[k].sku) for k in changed}
        dropped = {k[1] for k in keys} - {k[1] for k in down}
        v_up = _partial_net(scn, spec, plan, out, exp, up, keys_touched, set())
        v_down = _partial_net(scn, spec, plan, out, exp, down, keys_touched, dropped if lp_levers.expedite else set())
        alt_q = up if v_up > v_down else down
    return alt_q


def _within_budget(out: LpOut, alt_q: dict[AltKey, int], params: object) -> bool:
    units = sum(alt_q.values())
    premium = sum(q * out.alt_cands[k].premium for k, q in alt_q.items())
    max_units = getattr(params, "max_units_total", None)
    max_premium = getattr(params, "max_premium_cents", None)
    return (max_units is None or units <= max_units) and (max_premium is None or premium <= max_premium)


def _partial_net(
    scn: Scenario,
    spec: PlanSpec,
    plan: Plan,
    out: LpOut,
    exp: dict[str, int],
    alt_q: dict[AltKey, int],
    keys: set[tuple[str, str]],
    fallback_refs: set[str],
) -> int:
    alt_refs = {k[1] for k in alt_q}
    expedites = [_expedite(c, q) for c, q in _exp_decisions(out, alt_refs) if c.ref in exp]
    have = {e.ref for e in expedites}
    for ref in sorted(fallback_refs - have):
        cands = sorted((c for c in out.exp_cands.values() if c.ref == ref), key=lambda c: (c.arrive_week, c.lane.code))
        if cands:
            expedites.append(_expedite(cands[0], cands[0].ub))
    trial = Plan(
        transfers=out.transfers,
        expedites=expedites,
        alt=[_alt(out.alt_cands[k], q) for k, q in sorted(alt_q.items())],
        moves=plan.moves,
        sequences=plan.sequences,
        rebook=plan.rebook,
    )
    margin = lost_margin(evaluate(scn, trial, keys))
    cost = trial.expedite_premium + trial.nearshore_premium
    return -spec.weights.lost_margin * margin - spec.weights.transfer_cost * cost


def _expedite(c: ExpCand, q: int) -> Expedite:
    return Expedite(c.ref, c.sku, c.dc, c.lane, q, c.book_label, c.arrive_week, c.eta, c.premium)


def _alt(a: AltCand, q: int) -> AltBuy:
    return AltBuy(a.source, a.ref, a.dc, a.sku, q, a.week, a.eta, a.premium)


# ───────────────────────────── allocations ─────────────────────────────


def in_scope_allocations(scn: Scenario, params: PrioritizeAllocationParams) -> list[Allocation]:
    """Allocations ``prioritize_allocation`` may change (inside the horizon and the params)."""
    if scn.data is None:
        return []
    weeks = set(scn.weeks)
    dcs = set(params.dc_codes) if params.dc_codes is not None else None
    wks = set(params.ship_weeks) if params.ship_weeks is not None else None
    return sorted(
        (
            a
            for a in scn.data.allocations
            if a.ship_week in weeks and (dcs is None or a.dc_code in dcs) and (wks is None or a.ship_week in wks)
        ),
        key=lambda a: a.code,
    )


def _allocations(scn: Scenario, plan: Plan, params: PrioritizeAllocationParams, warnings: list[str]) -> None:
    """Re-split the scarce (dc, sku, week) cells that have allocations in scope: protected
    allocations first (``respect_protected``), then each cluster's sales need, then the
    buffers - the single-week optimum (one SKU has one margin, so the lost margin of a week
    is minimised by selling before stocking). The clusters' draws become the new allocation
    quantities (split over a cluster's allocation rows pro-rata to their current quantity;
    a protected row is never cut). Grades act through protected allocations and protected
    minimums (ProblemData carries no grade)."""
    rows = in_scope_allocations(scn, params)
    foreign = [a.code for a in rows if scn.cluster_dc.get(a.cluster_code) != a.dc_code]
    if foreign:
        # The simulator only lets a DC's own clusters draw from it: such a row could not be
        # re-split consistently (and its set_qty would not change the recomputed KPIs).
        warnings.append(
            f"prioritize_allocation: {len(foreign)} allocation(s) name a cluster of another DC and are left "
            f"unchanged (e.g. {sorted(foreign)[:3]})"
        )
        rows = [a for a in rows if scn.cluster_dc.get(a.cluster_code) == a.dc_code]
    if not rows:
        warnings.append("prioritize_allocation: no allocations in scope")
        return
    week_pos = {w: i for i, w in enumerate(scn.weeks)}
    cells: dict[tuple[str, str, int], dict[str, list[Allocation]]] = defaultdict(lambda: defaultdict(list))
    for al in rows:
        cells[(al.dc_code, al.sku, week_pos[al.ship_week])][al.cluster_code].append(al)
    keys = {(dc, sku) for dc, sku, _t in cells}
    scarce = evaluate(scn, plan, keys).scarce
    floors: dict[tuple[str, str, int], dict[str, int]] = {}
    for cell in sorted(cells):
        if cell in scarce:
            floors[cell] = {
                cl: sum(a.qty for a in allocs if a.protected) if params.respect_protected else 0
                for cl, allocs in cells[cell].items()
            }
    if not floors:
        return
    draws = evaluate(scn, plan, keys, priority=floors).draws
    changes: list[AllocChange] = []
    for cell in sorted(floors):
        got = draws.get(cell, {})
        for cl, allocs in sorted(cells[cell].items()):
            for al, q in zip(allocs, _split(got.get(cl, 0), [a.qty for a in allocs]), strict=True):
                if params.respect_protected and al.protected:
                    q = max(q, al.qty)
                if q != al.qty:
                    changes.append(AllocChange(al, q))
    if params.max_changes is not None and len(changes) > params.max_changes:
        # A cell's changes only work together (units move from one cluster to another):
        # keep whole cells, most valuable first, within the budget of actions.
        by_cell: dict[tuple[str, str, int], list[AllocChange]] = defaultdict(list)
        for c in changes:
            by_cell[(c.allocation.dc_code, c.allocation.sku, week_pos[c.allocation.ship_week])].append(c)

        def value(cell: tuple[str, str, int]) -> int:
            margin = scn.price_cents[cell[1]] - scn.cost_cents[cell[1]]
            return sum(abs(c.qty_after - c.allocation.qty) for c in by_cell[cell]) * margin

        kept: list[AllocChange] = []
        for cell in sorted(by_cell, key=lambda cell: (-value(cell), cell)):
            if len(kept) + len(by_cell[cell]) <= params.max_changes:
                kept.extend(by_cell[cell])
        if len(kept) < len(changes):
            warnings.append(
                f"prioritize_allocation: max_changes={params.max_changes} keeps {len(kept)} of {len(changes)} "
                "allocation changes (whole cells only)"
            )
        changes = kept
    changes.sort(key=lambda c: c.allocation.code)
    after = {c.allocation.code: c.qty_after for c in changes}
    overrides: dict[tuple[str, str, int], dict[str, int]] = {}
    for c in changes:
        al = c.allocation
        cell = (al.dc_code, al.sku, week_pos[al.ship_week])
        if cell not in overrides:
            overrides[cell] = {
                cl: sum(after.get(a.code, a.qty) for a in allocs) for cl, allocs in sorted(cells[cell].items())
            }
    plan.alloc_changes = changes
    plan.overrides = overrides


def _split(total: int, weights: list[int]) -> list[int]:
    """``total`` over rows pro-rata to their current quantity (largest remainder, ties by
    position); all to the first row when every weight is zero."""
    if not weights:
        return []
    s = sum(weights)
    if s <= 0:
        return [total] + [0] * (len(weights) - 1)
    base = [total * w // s for w in weights]
    left = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-((total * weights[i]) % s), i))
    for i in order[:left]:
        base[i] += 1
    return base


# ───────────────────────────── never worse than doing nothing ─────────────────────────────


def _levers_used(plan: Plan) -> list[str]:
    used = {
        "reallocate_inventory": bool(plan.transfers),
        "expedite_mode_shift": bool(plan.expedites),
        "resource_alt_supplier": bool(plan.alt),
        "prioritize_allocation": bool(plan.alloc_changes),
        "resequence_production": bool(plan.sequences),
    }
    return [code for code in LEVER_CODES if used[code]]


def _keep_improving(
    scn: Scenario,
    spec: PlanSpec,
    plan: Plan,
    margin_before: int,
    params: dict[str, int | str | bool],
    warnings: list[str],
) -> Plan:
    """Drop levers whose decisions lower the net benefit, then the do-nothing guard. A
    lever's contribution is measured on the (dc, sku) pairs its decisions touch only
    (pairs are independent in the simulator), so the check is exact and cheap."""
    w = spec.weights
    if plan.rebook is not None and plan.rebook.keys:
        # The booking choice must beat the baseline's on the PO lines it changes.
        trial = plan.without("")
        trial.rebook = make_rebook(scn, scn.baseline_kept)
        keys = set(plan.rebook.keys)
        gain = w.lost_margin * (lost_margin(evaluate(scn, trial, keys)) - lost_margin(evaluate(scn, plan, keys)))
        if gain < 0:
            # The splits were planned against the capacity the optimised choice leaves free;
            # under the baseline choice the splits of an over-booked cell must go (review fix:
            # never book more than the cut lane holds), and the switch is then judged on the
            # whole plan.
            over = _overbooked_cells(scn, trial)
            if over:
                trial.expedites = [e for e in trial.expedites if (e.lane.code, e.book_week) not in over]
            if not over or weighted_net(scn, trial, spec, margin_before) > weighted_net(scn, plan, spec, margin_before):
                warnings.append(
                    "capacity_bookings: the optimised booking choice lost more margin than first-come-first-served; "
                    "the first-come-first-served choice is used"
                )
                plan = trial
    used = _levers_used(plan)
    if len(used) > 1 or (used and plan.rebook is not None and plan.rebook.keys):
        for code in used:
            trial = plan.without(code)
            keys = _lever_keys(plan, code, scn)
            m_with = lost_margin(evaluate(scn, plan, keys))
            m_without = lost_margin(evaluate(scn, trial, keys))
            saved_cost = plan.incremental_cost - trial.incremental_cost
            gain = w.lost_margin * (m_without - m_with) - w.transfer_cost * saved_cost
            if gain < 0:
                warnings.append(f"{code}: its decisions lowered the recomputed weighted net benefit; dropped")
                plan = trial
    margin_after = lost_margin(evaluate(scn, plan))
    net = w.lost_margin * (margin_before - margin_after) - w.transfer_cost * plan.incremental_cost
    if net > 0:
        return plan
    label = ",".join(_levers_used(plan)) or params.get("levers", "plan")
    cost = plan.incremental_cost
    kind = "transfer cost" if label == "reallocate_inventory" else "incremental cost"
    warnings.append(
        f"{label}: the rounded LP plan did not improve the recomputed weighted net benefit "
        f"(margin {margin_before - margin_after} cents, {kind} {cost} cents); returning the do-nothing plan"
    )
    params["plan_fallback"] = "do_nothing"
    if scn.decidable:
        warnings.append(
            "capacity_bookings: the do-nothing plan still cancels the bookings that no longer fit "
            "(first-come-first-served)"
        )
        return Plan(rebook=make_rebook(scn, scn.baseline_kept))
    return Plan()


def _overbooked_cells(scn: Scenario, plan: Plan) -> set[tuple[str, str]]:
    """(lane code, ISO booking week) cells where the plan's splits plus the bookings that fly
    under ``plan.rebook`` exceed the lane's capacity in that week."""
    if not plan.expedites:
        return set()
    load = scn.booking_load(plan.rebook.kept if plan.rebook is not None else None)
    index = {label: t for t, label in enumerate(scn.weeks)}
    used: dict[tuple[str, str], int] = defaultdict(int)
    lanes: dict[str, InboundLaneInfo] = {}
    for e in plan.expedites:
        used[(e.lane.code, e.book_week)] += e.qty
        lanes[e.lane.code] = e.lane
    over: set[tuple[str, str]] = set()
    for (code, label), qty in sorted(used.items()):
        t = index.get(label)
        if t is None or qty + load.get((code, t), 0) > lanes[code].cap_in_week(t):
            over.add((code, label))
    return over


def _lever_keys(plan: Plan, code: str, scn: Scenario) -> set[tuple[str, str]]:
    """(dc, sku) pairs one lever's decisions touch."""
    part = Plan()
    if code == "reallocate_inventory":
        part.transfers = plan.transfers
    elif code == "expedite_mode_shift":
        part.expedites = plan.expedites
    elif code == "resource_alt_supplier":
        part.alt = plan.alt
    elif code == "prioritize_allocation":
        part.overrides = plan.overrides
    elif code == "resequence_production":
        part.moves = plan.moves
    return touched_keys(part, scn)


__all__ = ["PlanOutcome", "RLine", "evaluate", "make_plan", "weighted_net"]
