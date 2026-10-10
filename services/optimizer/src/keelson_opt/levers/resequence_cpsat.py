"""``resequence_production`` v2 for ``integer_stage="cpsat"`` (M8,
``docs/contracts/125-m8-optimizer-cpsat.md`` §5).

Same line model as M3 (:mod:`keelson_opt.levers.resequence`: durations, readiness releases, the
on-time + own-delay reference, the frozen prefix), but the sequence is chosen by what the
simulator values - lost margin by arrival week - instead of the weighted-completion proxy:

* **Value tables.** For every PO line made on the line and every completion-day change
  ``delta`` its orders can see (``delta = max over its orders of (completion day - reference
  day)``, as M3 moves the line), the receipt's new eta follows the lane-closure rules
  (``resequence._moved_eta``); days that would lose the receipt are removed from the domain,
  and a pinned (capacity-booked) PO line may only take days that keep its current eta - the
  rest of the line can still move (M3 rejects the whole line). Each distinct arrival week is
  priced once: ``w_lost_bp * lost margin`` of the line's DC x SKU pair with only this PO line
  moved there (single-pair simulation).
* **CP-SAT, time-indexed, two lexicographic solves.** ``x[o, d] = 1`` when order ``o`` completes
  on day ``d`` (one day per order, from its earliest completion - release + duration - to the
  line's horizon; forbidden days left out). The line's fluid capacity holds cumulatively: for
  every day ``D`` and every release threshold ``R``, the orders released at ``R`` or later that
  complete by ``D`` need at most ``(D + 1) * units_per_day - R`` units (the frozen prefix is a
  release for everything else). A single-order PO line's table cost sits directly on its
  ``x``; a PO line with several orders on the line prices ``max(day - reference day)`` through
  ``add_max_equality`` + ``add_element``. (A) minimise ``sum table cost + 10000 *
  lateness_cents_per_unit_day * sum qty x days late`` (late = completion day after the PO
  line's ex-factory date, Merxa's ``late_vs_ex_factory``); (B) once (A) is proven optimal, keep
  that optimum and minimise units x days late first, then the total day deviation from the
  current back-to-back schedule (stability: nothing to gain, no change), with the budget (A)
  left, hinted with (A)'s solution ((A) itself is not hinted: from the M3 sequence the search
  stayed at its cost). Both solves use one worker, the request seed and a
  deterministic budget of ``min(0.15, 0.02 + 0.0007 * orders)`` per line, at most 0.3 per
  request.
* **Choice.** The orders are sequenced by their CP-SAT day (ties: the M3 order) and applied
  back to back, as Merxa re-dates them (a back-to-back order can finish earlier than its CP-SAT
  day). The result is kept only when the simulator rates it strictly better than the M3 result
  on the PO lines either moves (ties: M3, so nothing changes when the two agree); otherwise the
  M3 path continues as in ``repair`` mode. Lines over :data:`RESEQ_V2_MAX_ORDERS` orders,
  :data:`RESEQ_V2_MAX_DAYS` days or :data:`RESEQ_V2_MAX_CELLS` order-days fall back with
  ``model_too_large``.

The value tables price one PO line at a time and the cumulative capacity is the fluid
relaxation of the sequence; both approximations are checked by the final simulator comparison
on the realised back-to-back sequence.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from keelson_contracts.optimizer import IntegerFallbackReason, ResequenceProductionParams
from ortools.sat.python import cp_model

from keelson_opt.cpsat import OBJECTIVE_LIMIT, CpRun, StageLog, objective_fits, run_cpsat, set_hints
from keelson_opt.impact import lost_margin, simulate
from keelson_opt.levers.resequence import (
    LineModel,
    ReseqOutcome,
    _Job,
    _moved_eta,
    _schedule,
    line_moves,
    m3_sequence,
)
from keelson_opt.plan import EtaMove, LineSequence, RLine, apply_moves, receipt_series
from keelson_opt.scn import ReceiptLine, Scenario

log = logging.getLogger(__name__)

RESEQ_V2_MAX_ORDERS = 400
RESEQ_V2_MAX_DAYS = 400
RESEQ_V2_MAX_CELLS = 60_000
"""Order x day variables at most."""
MAX_RELEASE_THRESHOLDS = 8
"""Cumulative capacity rows per day: the distinct release times (evenly thinned above 8)."""
RESEQ_V2_DET_TIME = 0.15
"""Deterministic seconds per line at most (``RESEQ_V2_DET_BASE + RESEQ_V2_DET_PER_ORDER * orders`` below)."""
RESEQ_V2_DET_BASE = 0.02
RESEQ_V2_DET_PER_ORDER = 0.0007
RESEQ_V2_REQUEST_CAP = 0.3
"""Deterministic seconds all lines of one request may use together."""
_LATENESS_SCALE = 10_000
"""``lateness_cents_per_unit_day`` enters the objective at weight x1.0 (basis points)."""


@dataclass(slots=True)
class _Table:
    """Cost per completion-day change of one PO line (``None`` = forbidden day)."""

    ref: str
    jobs: list[int]
    lo: int
    costs: list[int | None]


@dataclass(slots=True)
class V2Result:
    fallback: IntegerFallbackReason | None
    seq: list[int] = field(default_factory=list)
    run_a: CpRun | None = None
    run_b: CpRun | None = None
    det_a: float | None = None
    det_b: float | None = None
    fallback_b: IntegerFallbackReason | None = None
    """Why the tie-break (B) gave no usable result (the line keeps (A)'s sequence)."""


def line_v2(
    scn: Scenario,
    model: LineModel,
    current: list[RLine],
    params: ResequenceProductionParams,
    *,
    seed: int,
    w_lost_bp: int,
    pinned: frozenset[str],
    stages: StageLog,
    out: ReseqOutcome,
    time_limit_s: int,
) -> tuple[LineSequence, dict[str, EtaMove]] | None:
    """The line's sequence in ``cpsat`` mode: v2 when the simulator rates it strictly better than
    the M3 result, else exactly the M3 path (methods, warnings)."""
    pl = model.line
    n = len(model.jobs)
    m3 = m3_sequence(model, seed)
    m3_res: tuple[LineSequence, dict[str, EtaMove]] | None = None
    m3_missed: str | None = None
    if m3 is not None:
        m3_res, m3_missed = line_moves(scn, model, m3[0])
    m3_valid = m3_res is not None and not set(m3_res[1]) & pinned
    hint = m3[0] if m3 is not None else list(range(n))
    try:
        v2 = solve_line(scn, model, current, params, w_lost_bp, pinned, hint, seed, stages, time_limit_s)
    except Exception:  # noqa: BLE001 - a failing stage falls back to the M3 sequence, never fails the request
        log.exception("resequence v2 failed; M3 sequence kept", extra={"line_code": pl.line_code})
        v2 = V2Result("solver_error")
    reason = v2.fallback
    v2_res: tuple[LineSequence, dict[str, EtaMove]] | None = None
    if reason is None:
        res, missed = line_moves(scn, model, v2.seq)
        if missed is not None or (res is not None and set(res[1]) & pinned):
            reason = "simulator_not_better"  # the back-to-back schedule hits a lane closure / a booking
        elif res is None or (m3_valid and m3_res is not None and res[1] == m3_res[1]):
            reason = "no_improvement"
        else:
            v2_res = res
    if v2_res is not None:
        rival = m3_res[1] if m3_valid and m3_res is not None else {}
        keys = {
            (pol.dest_dc, pol.sku)
            for ref in set(v2_res[1]) | set(rival)
            if (pol := scn.po_lines.get(ref)) is not None
        }
        lm_v2 = _lost(scn, current, v2_res[1], keys)
        lm_rival = _lost(scn, current, rival, keys)
        gain = w_lost_bp * (lm_rival - lm_v2)
        if gain <= 0:  # ties keep the M3 result (nothing changes when the two agree)
            reason = "simulator_not_better" if gain < 0 else "no_improvement"
            v2_res = None
    used = v2_res is not None
    name = f"resequence:{pl.line_code}"
    payload = {"line": pl.line_code, "frozen": model.frozen, "pinned": sorted(pinned), "hint": hint}
    if v2.run_a is not None:
        stages.cpsat_runs += 1
    stages.cp(name, v2.run_a, payload, used=used, fallback=reason, det_limit=v2.det_a)
    if v2.run_b is not None:
        # a tie-break without a usable result (e.g. stopped by the wall-clock guard) leaves (A)'s
        # sequence in place: recorded as its own fallback, never as a used solve
        stages.cp(
            f"{name}:tiebreak", v2.run_b, payload, used=used and v2.fallback_b is None,
            fallback=v2.fallback_b or reason, det_limit=v2.det_b,
        )  # fmt: skip
    if used and v2_res is not None and v2.run_a is not None:
        optimal = v2.run_a.status == "OPTIMAL" and (
            v2.run_b is None or (v2.run_b.status == "OPTIMAL" and v2.fallback_b is None)
        )
        out.methods[pl.line_code] = "cpsat_v2_optimal" if optimal else "cpsat_v2_feasible"
        return v2_res
    # The M3 path, as in repair mode.
    if m3 is None:
        return None
    out.methods[pl.line_code] = m3[1]
    if m3_missed is not None:
        out.warnings.append(
            f"resequence_production: {pl.line_code} new sequence would make {m3_missed} miss its lane; kept as is"
        )
        out.methods.pop(pl.line_code, None)
        return None
    return m3_res


def _lost(scn: Scenario, current: list[RLine], moves: dict[str, EtaMove], keys: set[tuple[str, str]]) -> int:
    lines = apply_moves(current, moves) if moves else current
    return lost_margin(simulate(scn, receipts=receipt_series(lines, scn.horizon), keys=keys))


def solve_line(
    scn: Scenario,
    model: LineModel,
    current: list[RLine],
    params: ResequenceProductionParams,
    w_lost_bp: int,
    pinned: frozenset[str],
    hint: Sequence[int],
    seed: int,
    stages: StageLog,
    time_limit_s: int,
) -> V2Result:
    """The v2 sequence of one line (module doc), or a fallback reason."""
    jobs = model.jobs
    n = len(jobs)
    per_day = model.per_day
    k = model.frozen
    if n > RESEQ_V2_MAX_ORDERS:
        return V2Result("model_too_large")
    current_end = _schedule(jobs, list(range(n)))
    current_day = [(e - 1) // per_day for e in current_end]
    prefix_end = current_end[k - 1] if k else 0
    release = [max(j.release, prefix_end) for j in jobs]
    total = max([prefix_end, *release[k:]]) + sum(j.dur for j in jobs[k:])
    last_day = (total - 1) // per_day
    if last_day + 1 > RESEQ_V2_MAX_DAYS:
        return V2Result("model_too_large")
    day_lo = [current_day[i] if i < k else (release[i] + j.dur - 1) // per_day for i, j in enumerate(jobs)]
    day_hi = [current_day[i] if i < k else last_day for i in range(n)]
    if sum(day_hi[i] - day_lo[i] + 1 for i in range(k, n)) > RESEQ_V2_MAX_CELLS:
        return V2Result("model_too_large")
    want = min(RESEQ_V2_DET_TIME, RESEQ_V2_DET_BASE + RESEQ_V2_DET_PER_ORDER * n)
    det_a = stages.budget.take(want, kind="resequence", cap=RESEQ_V2_REQUEST_CAP)
    if det_a is None:
        return V2Result("budget_exhausted")

    base_day = model.base_day
    tables = _value_tables(scn, model, current, w_lost_bp, pinned, day_lo, day_hi)
    single = {t.jobs[0]: t for t in tables if len(t.jobs) == 1 and t.jobs[0] >= k}
    multi = [t for t in tables if not (len(t.jobs) == 1 and t.jobs[0] >= k)]
    lateness_w = _LATENESS_SCALE * params.lateness_cents_per_unit_day
    due: list[int | None] = []
    for j in jobs:
        pol = scn.po_lines.get(j.order.po_line_ref)
        due.append((pol.ex_factory - model.anchor).days if pol is not None else None)

    def late_days(i: int, d: int) -> int:
        d_due = due[i]
        return max(0, d - d_due) if d_due is not None else 0

    m = cp_model.CpModel()
    x: dict[int, dict[int, cp_model.IntVar]] = {}
    cost_a: list[tuple[int, cp_model.IntVar, int, int]] = []
    for i in range(k, n):
        t = single.get(i)
        cells: dict[int, cp_model.IntVar] = {}
        for d in range(day_lo[i], day_hi[i] + 1):
            c = t.costs[d - base_day[i] - t.lo] if t is not None else 0
            if c is None:
                continue  # the receipt would be lost / a booked PO line would move
            v = cells[d] = m.new_bool_var("")
            coef = c + lateness_w * _qty(jobs, i) * late_days(i, d)
            if coef:
                cost_a.append((coef, v, 0, 1))
        if not cells:
            return V2Result("infeasible", det_a=det_a)
        m.add_exactly_one(cells.values())
        x[i] = cells

    # fluid capacity, cumulatively per day and release threshold
    thresholds = sorted({release[i] for i in range(k, n)})
    if len(thresholds) > MAX_RELEASE_THRESHOLDS:
        step = len(thresholds) / MAX_RELEASE_THRESHOLDS
        thresholds = sorted({thresholds[int(s * step)] for s in range(MAX_RELEASE_THRESHOLDS)})
    first_day = min(day_lo[i] for i in range(k, n))
    chains: list[list[tuple[int, cp_model.IntVar, list[tuple[int, int]]]]] = []
    """Per threshold, per day: the cumulative units variable and the (job, units) completing that day."""
    for r in thresholds:
        by_day: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for i in range(k, n):
            if release[i] >= r:
                for d in x[i]:
                    by_day[d].append((i, jobs[i].dur))
        chain: list[tuple[int, cp_model.IntVar, list[tuple[int, int]]]] = []
        done: cp_model.IntVar | None = None  # units of the group completed by the end of day d
        for d in range(first_day, last_day + 1):
            cum = m.new_int_var(0, max(0, (d + 1) * per_day - r), "")
            today = by_day.get(d, [])
            m.add(cum == (done if done is not None else 0) + sum(q * x[i][d] for i, q in today))
            chain.append((d, cum, today))
            done = cum
        chains.append(chain)
    refs: list[tuple[_Table, cp_model.IntVar, cp_model.IntVar]] = []
    for t in multi:
        allowed = [v for v, c in enumerate(t.costs) if c is not None]
        if not allowed:
            return V2Result("infeasible", det_a=det_a)
        idx = m.new_int_var_from_domain(cp_model.Domain.from_values(allowed), "")
        day_exprs = [current_day[i] if i < k else sum(d * v for d, v in x[i].items()) for i in t.jobs]
        m.add_max_equality(idx, [e - base_day[i] - t.lo for e, i in zip(day_exprs, t.jobs, strict=True)])
        filled = [c if c is not None else 0 for c in t.costs]
        lo_c, hi_c = min(filled), max(filled)
        cost = m.new_int_var(lo_c, hi_c, "")
        m.add_element(idx, filled, cost)
        cost_a.append((1, cost, lo_c, hi_c))
        refs.append((t, idx, cost))
    if not objective_fits((c, lb, ub) for c, _v, lb, ub in cost_a):
        return V2Result("objective_overflow", det_a=det_a)
    m.minimize(sum(c * v for c, v, _lb, _ub in cost_a))

    def hints(day: Mapping[int, int]) -> dict[int, int]:
        """Every variable's value for a feasible order -> day assignment."""
        values = {v.index: int(d == day[i]) for i in range(k, n) for d, v in x[i].items()}
        for chain in chains:
            done = 0
            for d, cum, today in chain:
                done += sum(q for i, q in today if day[i] == d)
                values[cum.index] = done
        for t, idx, cost in refs:
            delta = max((current_day[i] if i < k else day[i]) - base_day[i] for i in t.jobs) - t.lo
            values[idx.index] = delta
            values[cost.index] = t.costs[delta] or 0
        return values

    # (A) is not hinted: started from the M3 sequence, CP-SAT stayed at the hint's cost on the
    # full-profile VN line, while the unhinted search reached 1 % of the bound in the same
    # budget. The M3 sequence still competes in the final simulator comparison.
    position = {job: p for p, job in enumerate(hint)}
    run_a = run_cpsat(m, seed=seed, det_limit=det_a, time_limit_s=time_limit_s, presolve=False)
    stages.budget.spend(run_a.det_time_s, kind="resequence")
    if (reason := run_a.fallback) is not None or run_a.objective is None:
        return V2Result(reason or "time_limit_no_solution", run_a=run_a, det_a=det_a)
    day_a = _days(run_a, x, k, n)
    seq = _sequence(day_a, k, n, position)

    # (B) the same optimum, fewest units x days late, then the smallest day deviation - only once
    # (A) is proven optimal (else "the same optimum" is not known), with what (A) left over.
    dev_max = sum(max(abs(day_lo[i] - current_day[i]), abs(day_hi[i] - current_day[i])) for i in range(k, n))
    late_max = sum(_qty(jobs, i) * late_days(i, day_hi[i]) for i in range(k, n))
    det_b = stages.budget.take(det_a - run_a.det_time_s, kind="resequence", cap=RESEQ_V2_REQUEST_CAP)
    if run_a.status != "OPTIMAL" or det_b is None or (late_max + 1) * (dev_max + 1) >= OBJECTIVE_LIMIT:
        return V2Result(None, seq=seq, run_a=run_a, det_a=det_a)  # tie-break skipped: keep (A)
    m.add(sum(c * v for c, v, _lb, _ub in cost_a) <= run_a.objective)
    m.clear_objective()
    m.minimize(
        sum(
            ((dev_max + 1) * _qty(jobs, i) * late_days(i, d) + abs(d - current_day[i])) * v
            for i in range(k, n)
            for d, v in x[i].items()
        )
    )
    set_hints(m, hints(day_a))
    run_b = run_cpsat(m, seed=seed, det_limit=det_b, time_limit_s=time_limit_s, presolve=False)
    stages.budget.spend(run_b.det_time_s, kind="resequence")
    fallback_b = run_b.fallback if not run_b.has_solution else None
    if run_b.has_solution:
        seq = _sequence(_days(run_b, x, k, n), k, n, position)
    elif fallback_b is None:  # pragma: no cover - (A)'s solution satisfies (B), so it has one
        fallback_b = "time_limit_no_solution"
    return V2Result(None, seq=seq, run_a=run_a, run_b=run_b, det_a=det_a, det_b=det_b, fallback_b=fallback_b)


def _qty(jobs: Sequence[_Job], i: int) -> int:
    return jobs[i].order.qty


def _days(run: CpRun, x: dict[int, dict[int, cp_model.IntVar]], k: int, n: int) -> dict[int, int]:
    return {i: next(d for d, v in x[i].items() if run.value(v)) for i in range(k, n)}


def _sequence(day: Mapping[int, int], k: int, n: int, position: Mapping[int, int]) -> list[int]:
    """The frozen prefix, then the other orders by CP-SAT day (ties: the hint's order)."""
    return list(range(k)) + sorted(range(k, n), key=lambda i: (day[i], position.get(i, i), i))


def _value_tables(
    scn: Scenario,
    model: LineModel,
    current: list[RLine],
    w_lost_bp: int,
    pinned: frozenset[str],
    day_lo: list[int],
    day_hi: list[int],
) -> list[_Table]:
    """One table per PO line of the line whose arrival the sequence can change (constant
    tables are left out)."""
    horizon = scn.horizon
    base_day = model.base_day
    by_ref: dict[str, list[int]] = defaultdict(list)
    for i, j in enumerate(model.jobs):
        by_ref[j.order.po_line_ref].append(i)
    receipts_of: dict[str, list[ReceiptLine]] = defaultdict(list)
    for rl in scn.receipt_lines:
        if not rl.lost and rl.ref in by_ref:
            receipts_of[rl.ref].append(rl)
    lines_of: dict[str, list[RLine]] = defaultdict(list)
    for ln in current:
        if ln.key in by_ref:
            lines_of[ln.key].append(ln)
    series = receipt_series(current, horizon)
    tables: list[_Table] = []
    for ref, idxs in sorted(by_ref.items()):
        pol = scn.po_lines.get(ref)
        rls = receipts_of.get(ref, [])
        if pol is None or not rls:
            continue
        key = (pol.dest_dc, pol.sku)
        lo = max(day_lo[i] - base_day[i] for i in idxs)
        hi = max(day_hi[i] - base_day[i] for i in idxs)
        cur_eta = max(rl.eta_perturbed for rl in rls)
        priced: dict[int | None, int] = {}
        costs: list[int | None] = []
        for delta in range(lo, hi + 1):
            etas = [_moved_eta(scn, rl, delta) for rl in rls]
            if any(e is None for e in etas):
                costs.append(None)
                continue
            eta = max(e for e in etas if e is not None)
            week: int | None
            if eta == cur_eta:
                week = None  # the arrival does not move
            elif ref in pinned:
                costs.append(None)
                continue
            else:
                week = min(scn.week_of(max(scn.as_of, eta)), horizon)
            if week not in priced:
                priced[week] = w_lost_bp * _pair_lost(scn, key, series.get(key), lines_of.get(ref, []), week)
            costs.append(priced[week])
        if None not in costs and len(set(costs)) == 1:
            continue
        tables.append(_Table(ref, idxs, lo, costs))
    return tables


def _pair_lost(
    scn: Scenario, key: tuple[str, str], base: list[int] | None, ref_lines: list[RLine], week: int | None
) -> int:
    """Lost margin of one DC x SKU pair with one PO line's arrivals moved to ``week`` (``None`` =
    unchanged; ``>= horizon`` = after the horizon)."""
    horizon = scn.horizon
    rec = list(base) if base is not None else [0] * horizon
    if week is not None:
        for ln in ref_lines:
            if 0 <= ln.week < horizon:
                rec[ln.week] -= ln.qty
            if week < horizon:
                rec[week] += ln.qty
    return lost_margin(simulate(scn, receipts={key: rec}, keys={key}))


__all__ = [
    "RESEQ_V2_DET_TIME",
    "RESEQ_V2_MAX_DAYS",
    "RESEQ_V2_MAX_ORDERS",
    "RESEQ_V2_REQUEST_CAP",
    "V2Result",
    "line_v2",
    "solve_line",
]
