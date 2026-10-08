"""``resequence_production``: reorder the movable orders of a factory line so that orders
whose materials are available and whose styles are short are produced first.

Line model: the line works its movable orders one after the other from the anchor (the
earliest planned start of a movable order) at ``daily_capacity_units`` per day (a fluid
single machine: an order of ``q`` units takes ``q / capacity`` days; a line without a
capacity uses each order's planned duration in days instead). Fixed (running/done) orders
keep their slots. The reference is the current sequence with every material on time; an
order whose PO line carries a readiness delay ``d`` (late trims or materials:
``lead_time_shift``) cannot start before its on-time start + ``d`` (its release); every
other order can start at the anchor (materials on hand). In the scenario a delayed order is
late by its own delay only (the delay is not cascaded onto the orders behind it), so the
reference completion of an order is its on-time completion + its own delay. Completion
day = the day in which the order's last unit is produced.

Objective: minimise ``sum_o w_o * C_o`` (weighted completion, in production units) with the
criticality ``w_o = (retail - cost) * min(qty_o, lost units of the order's (DC, SKU))``
from the perturbed simulation. Solved deterministically:

* no release later than the anchor: Smith's rule (WSPT, ``w/p`` descending, ties by the
  current sequence) - provably optimal for ``1 || sum w C``;
* up to :data:`ENUMERATE_MAX` orders: exact enumeration of all permutations (ties keep the
  lexicographically first, i.e. the current order when it is optimal);
* up to :data:`CPSAT_MAX_ORDERS` orders: CP-SAT (``num_workers`` = env ``CPSAT_NUM_WORKERS``,
  default and compose value 1, ``random_seed=seed``, a
  *deterministic* time limit :data:`CPSAT_DETERMINISTIC_TIME` so the result never depends on
  machine speed), hinted with the release-aware greedy order (kept if CP-SAT finds nothing);
* larger lines: the release-aware greedy order (WSPT among the released orders) - a
  deterministic heuristic; on the full-profile lines CP-SAT never improved on it within
  a 5 s budget.

A new sequence is kept only if its objective is strictly lower than the reference's and
the simulator (on the affected DC x SKU pairs) confirms a strictly lower lost margin.
Each PO line's goods are ready earlier/later by its order's completion-day change against
the reference (pure reordering effect; Merxa additionally re-dates the orders back to
back, which is not counted as earlier supply) and then pass the lane-closure rules of
:mod:`keelson_opt.scn` again (a temporary port closure may now be missed or hit); a
sequence that would make a receipt miss its lane altogether is not used.
"""

from __future__ import annotations

import datetime as dt
import functools
import heapq
import itertools
import logging
import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from keelson_contracts.optimizer import ProductionLine, ProductionOrder, ResequenceProductionParams
from ortools.sat.python import cp_model

from keelson_opt.impact import lost_margin, simulate
from keelson_opt.plan import EtaMove, LineSequence, RLine, apply_moves, receipt_series
from keelson_opt.scn import ReceiptLine, Scenario, closure_delay

log = logging.getLogger(__name__)

ENUMERATE_MAX = 7
CPSAT_MAX_ORDERS = 40
"""Lines with more movable orders use the release-aware greedy order (CP-SAT never beat
it on the full-profile lines and costs ~5x its deterministic budget in wall time)."""
DEFAULT_MAX_ORDERS = 400
CPSAT_DETERMINISTIC_TIME = 0.05


def cpsat_workers() -> int:
    """``CPSAT_NUM_WORKERS`` (compose sets 1; default 1). More than one worker makes CP-SAT
    non-deterministic, so only 1 keeps the optimizer's determinism guarantee."""
    try:
        return max(1, int(os.environ.get("CPSAT_NUM_WORKERS", "1")))
    except ValueError:
        return 1


@dataclass(slots=True)
class ReseqOutcome:
    moves: dict[str, EtaMove] = field(default_factory=dict)
    sequences: list[LineSequence] = field(default_factory=list)
    methods: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Job:
    order: ProductionOrder
    dur: int
    """Processing time in time units (units of product, or days without a capacity)."""
    release: int
    """Earliest start, time units after the anchor."""
    weight: int


def _schedule(jobs: Sequence[_Job], seq: Sequence[int]) -> list[int]:
    """Completion (exclusive end, in time units after the anchor) per job index."""
    end = [0] * len(jobs)
    cursor = 0
    for i in seq:
        start = max(cursor, jobs[i].release)
        end[i] = start + jobs[i].dur
        cursor = end[i]
    return end


def _objective(jobs: Sequence[_Job], seq: Sequence[int]) -> int:
    end = _schedule(jobs, seq)
    return sum(jobs[i].weight * end[i] for i in range(len(jobs)))


def _wspt(jobs: Sequence[_Job]) -> list[int]:
    def cmp(a: int, b: int) -> int:
        # w_a / p_a > w_b / p_b  <=>  w_a * p_b > w_b * p_a  (integers, no floats)
        lhs, rhs = jobs[a].weight * jobs[b].dur, jobs[b].weight * jobs[a].dur
        return -1 if lhs > rhs else (1 if lhs < rhs else a - b)

    return sorted(range(len(jobs)), key=functools.cmp_to_key(cmp))


def _greedy(jobs: Sequence[_Job]) -> list[int]:
    """Release-aware WSPT: at each step the released job with the best ratio ``w/p`` (ties:
    lowest index); when none is released, the next job to be released (ties: lowest index).
    O(n log n) with a heap keyed by the exact ratio."""
    order = sorted(range(len(jobs)), key=lambda i: (jobs[i].release, i))
    heap: list[tuple[Fraction, int]] = []
    seq: list[int] = []
    cursor = k = 0
    while len(seq) < len(jobs):
        while k < len(order) and jobs[order[k]].release <= cursor:
            i = order[k]
            heapq.heappush(heap, (-Fraction(jobs[i].weight, jobs[i].dur), i))
            k += 1
        if heap:
            pick = heapq.heappop(heap)[1]
        else:
            pick = order[k]
            k += 1
        seq.append(pick)
        cursor = max(cursor, jobs[pick].release) + jobs[pick].dur
    return seq


def _enumerate(jobs: Sequence[_Job]) -> list[int]:
    best: tuple[int, ...] = tuple(range(len(jobs)))
    best_obj = _objective(jobs, best)
    for perm in itertools.permutations(range(len(jobs))):
        obj = _objective(jobs, perm)
        if obj < best_obj:
            best, best_obj = perm, obj
    return list(best)


def _cpsat(jobs: Sequence[_Job], seed: int) -> tuple[list[int], str]:
    hint = _greedy(jobs)
    hint_end = _schedule(jobs, hint)
    total = sum(j.dur for j in jobs) + max(j.release for j in jobs)
    model = cp_model.CpModel()
    starts, ends, intervals = [], [], []
    for i, j in enumerate(jobs):
        s = model.new_int_var(j.release, total, f"s{i}")
        e = model.new_int_var(j.release + j.dur, total + j.dur, f"e{i}")
        intervals.append(model.new_interval_var(s, j.dur, e, f"iv{i}"))
        starts.append(s)
        ends.append(e)
        model.add_hint(s, hint_end[i] - j.dur)
    model.add_no_overlap(intervals)
    model.minimize(sum(j.weight * ends[i] for i, j in enumerate(jobs)))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = cpsat_workers()
    solver.parameters.random_seed = seed
    solver.parameters.max_deterministic_time = CPSAT_DETERMINISTIC_TIME
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return hint, "greedy"
    order = sorted(range(len(jobs)), key=lambda i: (solver.value(starts[i]), i))
    name = "cpsat_optimal" if status == cp_model.OPTIMAL else "cpsat_feasible"
    return order, name


def _moved_eta(scn: Scenario, rl: ReceiptLine, delta_days: int) -> dt.date | None:
    """Arrival of a receipt whose production completes ``delta_days`` later (negative =
    earlier) than in the scenario: the goods are ready ``delta_days`` later and then go
    through the lane-closure rules again (a temporary closure may now be missed or hit).
    ``None`` = the receipt would be lost (closed lane without a usable alternative)."""
    ready = rl.eta + dt.timedelta(days=rl.prod_delay + delta_days)
    pol = scn.po_lines.get(rl.ref)
    lane = scn.inbound_lanes.get(pol.lane_code) if pol is not None and pol.lane_code else None
    if lane is None or not lane.closures:
        return ready
    transport, lost = closure_delay(lane, ready, scn.inbound_lanes)
    return None if lost else ready + dt.timedelta(days=transport)


def plan_resequence(
    scn: Scenario,
    lines: list[RLine],
    params: ResequenceProductionParams,
    *,
    seed: int,
    w_lost_bp: int,
    pinned: frozenset[str] = frozenset(),
) -> ReseqOutcome:
    """``pinned`` PO lines (the lines carried by capacity bookings: already-applied
    decisions, M5) must keep their arrival: a new sequence that would move one is not used."""
    out = ReseqOutcome()
    data = scn.data
    if data is None or not data.production:
        out.warnings.append("resequence_production: ProblemData has no production lines")
        return out
    wanted = set(params.line_codes) if params.line_codes is not None else None
    known = {pl.line_code for pl in data.production}
    if wanted is not None and (unknown := sorted(wanted - known)):
        out.warnings.append(f"resequence_production: unknown line_codes {unknown}")
    max_orders = params.max_orders_per_line or DEFAULT_MAX_ORDERS

    # Criticality: lost units per (dc, sku) of the perturbed scenario.
    base = simulate(scn, receipts=receipt_series(lines, scn.horizon))
    lost: dict[tuple[str, str], int] = defaultdict(int)
    for (cl, sku), cs in base.series.items():
        lost[(scn.cluster_dc[cl], sku)] += sum(cs.lost)
    prod_delay: dict[str, int] = defaultdict(int)
    for rl in scn.receipt_lines:
        prod_delay[rl.ref] = max(prod_delay[rl.ref], rl.prod_delay)

    current = list(lines)
    for pl in sorted(data.production, key=lambda p: p.line_code):
        if wanted is not None and pl.line_code not in wanted:
            continue
        result = _line(scn, pl, lost, prod_delay, max_orders, seed, out)
        if result is None:
            continue
        seq, moves = result
        if hit := sorted(set(moves) & pinned):
            out.warnings.append(
                f"resequence_production: {pl.line_code} new sequence would move {hit[0]}, a PO line carried by a "
                "capacity booking (already-applied decision); kept as is"
            )
            out.methods.pop(pl.line_code, None)
            continue
        trial = apply_moves(current, moves)
        keys = {(pol.dest_dc, pol.sku) for ref in moves if (pol := scn.po_lines.get(ref)) is not None}
        before = lost_margin(simulate(scn, receipts=receipt_series(current, scn.horizon), keys=keys))
        after = lost_margin(simulate(scn, receipts=receipt_series(trial, scn.horizon), keys=keys))
        if w_lost_bp * (before - after) <= 0:
            out.warnings.append(
                f"resequence_production: {pl.line_code} new sequence does not reduce the lost margin; kept as is"
            )
            continue
        current = trial
        out.moves.update(moves)
        out.sequences.append(seq)
    return out


def _line(
    scn: Scenario,
    pl: ProductionLine,
    lost: dict[tuple[str, str], int],
    prod_delay: dict[str, int],
    max_orders: int,
    seed: int,
    out: ReseqOutcome,
) -> tuple[LineSequence, dict[str, EtaMove]] | None:
    movable = sorted((o for o in pl.orders if o.movable), key=lambda o: (o.seq, o.code))
    if len(movable) < 2:
        return None
    if len(movable) > max_orders:
        out.warnings.append(
            f"resequence_production: {pl.line_code} has {len(movable)} movable orders (> {max_orders}); unchanged"
        )
        return None
    rate = pl.daily_capacity_units  # time unit: one unit of product (or one day, rate 1)
    per_day = max(rate, 1)
    durs = [max(1, o.qty) if rate > 0 else (o.planned_end - o.planned_start).days + 1 for o in movable]
    identity = list(range(len(movable)))
    # The scenario's reference: the current sequence with every material on time. A delayed
    # order's receipt is late by its own readiness delay only (the scenario does not
    # cascade it onto the orders behind it), so its release is its on-time start + delay.
    on_time = _schedule([_Job(o, d, 0, 0) for o, d in zip(movable, durs, strict=True)], identity)
    delays = [max(0, prod_delay.get(o.po_line_ref, 0)) for o in movable]
    jobs: list[_Job] = []
    for i, o in enumerate(movable):
        pol = scn.po_lines.get(o.po_line_ref)
        release = on_time[i] - durs[i] + delays[i] * per_day if delays[i] else 0
        weight = 0
        if pol is not None:
            margin = scn.price_cents[pol.sku] - scn.cost_cents[pol.sku]
            weight = max(0, margin) * min(o.qty, lost.get((pol.dest_dc, pol.sku), 0))
        jobs.append(_Job(o, durs[i], release, weight))
    if not any(j.weight for j in jobs):
        return None
    if all(j.release == 0 for j in jobs):
        seq, method = _wspt(jobs), "wspt"
    elif len(jobs) <= ENUMERATE_MAX:
        seq, method = _enumerate(jobs), "enumeration"
    elif len(jobs) <= CPSAT_MAX_ORDERS:
        seq, method = _cpsat(jobs, seed)
    else:
        seq, method = _greedy(jobs), "greedy"
    # Scenario completion: on time, plus the order's own readiness delay.
    reference = [on_time[i] + delays[i] * per_day for i in range(len(jobs))]
    if _objective(jobs, seq) >= sum(j.weight * reference[i] for i, j in enumerate(jobs)):
        return None
    out.methods[pl.line_code] = method
    base_day = [(e - 1) // per_day for e in reference]
    new_day = [(e - 1) // per_day for e in _schedule(jobs, seq)]
    delta_by_ref: dict[str, int] = {}
    for i, j in enumerate(jobs):
        ref = j.order.po_line_ref
        d = new_day[i] - base_day[i]
        delta_by_ref[ref] = max(delta_by_ref.get(ref, d), d)
    moves: dict[str, EtaMove] = {}
    new_eta: dict[str, dt.date] = {}
    cur_eta: dict[str, dt.date] = {}
    for rl in scn.receipt_lines:
        d = delta_by_ref.get(rl.ref)
        if d is None or rl.lost:
            continue
        eta = _moved_eta(scn, rl, d)
        if eta is None:
            out.warnings.append(
                f"resequence_production: {pl.line_code} new sequence would make {rl.ref} miss its lane; kept as is"
            )
            out.methods.pop(pl.line_code, None)
            return None
        new_eta[rl.ref] = max(new_eta.get(rl.ref, eta), eta)
        cur_eta[rl.ref] = max(cur_eta.get(rl.ref, rl.eta_perturbed), rl.eta_perturbed)
    for ref, eta in sorted(new_eta.items()):
        pol = scn.po_lines.get(ref)
        if eta == cur_eta[ref] or pol is None:
            continue
        # Kept even when eta_after equals the system eta (a gain that cancels the
        # perturbation): the arrival still moves; no reschedule op is needed for it.
        eta_after = max(scn.as_of, eta)
        moves[ref] = EtaMove(ref, pol.eta, eta_after, scn.week_of(eta_after), eta_after > pol.eta)
    if not moves:
        return None
    return (
        LineSequence(
            line_code=pl.line_code,
            factory_code=pl.factory_code,
            before=tuple(j.order.code for j in jobs),
            after=tuple(jobs[i].order.code for i in seq),
            refs=tuple(sorted(moves)),
        ),
        moves,
    )
