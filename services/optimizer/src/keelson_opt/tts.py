"""Time-to-survive (Simchi-Levi) per DC, time-to-recover and bottlenecks.

TTS asks how long a DC's network (DC stock + the stock of its clusters) can keep serving
demand from supply that is *not* disrupted. Per DC an LP (GLOP via ``model_builder``):

    y[t] in [0, 1]       share of week t's demand served (for every SKU alike)
    y[t] <= y[t-1]        survival is continuous: once short, the rest is not covered
    sum_{tau<=t} y[tau] * D[k,tau] <= S[k,t]      for every SKU k and week t

``S[k,t]`` = DC supply (on_hand - reserved + in_transit, net of the plan's outbound
transfers) + its clusters' on-hand + on-time arrivals up to week t (receipt lines that
arrive no later than originally planned, plan arrivals). Rows that can never bind
(``S >= cumulative demand``) are dropped. ``TTS = floor(7 * sum_t y[t])`` days from the
horizon start (= the full horizon when it never runs short).

The objective is ``max V * 7 * sum_t y[t]`` with ``V`` = the DC's average daily demand
margin (cents per day of survival). Scaling does not change the optimum but makes the
duals cents: the shadow price of a binding SKU-week row is the survival value (cents) of
one more on-time unit there. Those rows are the **bottlenecks**.

TTR (time to recover) is measured from the horizon start to the earliest perturbed arrival
of the DC's disrupted receipts (delayed by a perturbation; lost receipts never recover).
Verdict: TTS covers the horizon or ``TTR <= TTS`` -> ``ok``; ``TTS < TTR <= TTS + 7`` (within
one weekly bucket) -> ``expedite``; otherwise (or nothing recovers) -> ``loss``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from keelson_contracts.optimizer import BindingConstraint, TtsRow, TtsVerdict
from ortools.linear_solver.python import model_builder as mb

from keelson_opt.impact import Transfer
from keelson_opt.isoweeks import short_week
from keelson_opt.plan import RLine
from keelson_opt.scn import Scenario

EXPEDITE_BAND_DAYS = 7
_MAX_BOTTLENECKS = 25
_TOL = 1e-6


@dataclass(frozen=True, slots=True)
class DcTts:
    dc: str
    days: int
    binding: tuple[BindingConstraint, ...]


def tts_by_dc(
    scn: Scenario, lines: Iterable[RLine], transfers: Sequence[Transfer] = (), *, seed: int = 0
) -> dict[str, DcTts]:
    horizon = scn.horizon
    arrivals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * horizon)
    for ln in lines:
        if not ln.late and 0 <= ln.week < horizon:
            arrivals[(ln.dc, ln.sku)][ln.week] += ln.qty
    outbound: dict[tuple[str, str], int] = defaultdict(int)
    for tr in transfers:
        outbound[(tr.lane.from_dc, tr.sku)] += tr.qty
        if tr.arrive_week < horizon:
            arrivals[(tr.lane.to_dc, tr.sku)][tr.arrive_week] += tr.qty
    out: dict[str, DcTts] = {}
    for dc in scn.dcs:
        out[dc] = _solve_dc(scn, dc, arrivals, outbound, seed)
    return out


def _solve_dc(
    scn: Scenario,
    dc: str,
    arrivals: dict[tuple[str, str], list[int]],
    outbound: dict[tuple[str, str], int],
    seed: int,
) -> DcTts:
    horizon = scn.horizon
    full = 7 * horizon
    demand: dict[str, list[int]] = {}
    for c in scn.dc_clusters[dc]:
        for sku in scn.skus:
            series = scn.demand.get((c, sku))
            if series is not None and any(series):
                acc = demand.setdefault(sku, [0] * horizon)
                for t, u in enumerate(series):
                    acc[t] += u
    if not demand:
        return DcTts(dc, full, ())
    margin_total = sum(sum(d) * (scn.price_cents[k] - scn.cost_cents[k]) for k, d in demand.items())
    value_per_day = max(margin_total, 1) / full

    rows: list[tuple[str, int, list[int], float]] = []  # (sku, t, demand prefix, supply)
    for sku in sorted(demand):
        dem = demand[sku]
        stock = scn.dc_supply.get((dc, sku), 0) - outbound.get((dc, sku), 0)
        stock += sum(scn.cluster_on_hand.get((c, sku), 0) for c in scn.dc_clusters[dc])
        arr = arrivals.get((dc, sku))
        cum_d = cum_s = 0
        for t in range(horizon):
            cum_d += dem[t]
            cum_s += arr[t] if arr else 0
            if dem[t] and stock + cum_s < cum_d:
                rows.append((sku, t, dem[: t + 1], float(stock + cum_s)))
    if not rows:
        return DcTts(dc, full, ())

    model = mb.Model()  # type: ignore[no-untyped-call]
    model.name = f"tts_{dc}"
    h = model.helper
    y = []
    for _ in range(horizon):
        i = h.add_var()
        h.set_var_lower_bound(i, 0.0)
        h.set_var_upper_bound(i, 1.0)
        y.append(i)
    for t in range(1, horizon):
        mono = h.add_linear_constraint()
        h.set_constraint_lower_bound(mono, -math.inf)
        h.set_constraint_upper_bound(mono, 0.0)
        h.add_term_to_constraint(mono, y[t], 1.0)
        h.add_term_to_constraint(mono, y[t - 1], -1.0)
    named: list[tuple[int, str, float]] = []
    for sku, t, prefix, supply in rows:
        row = h.add_linear_constraint()
        h.set_constraint_lower_bound(row, -math.inf)
        h.set_constraint_upper_bound(row, supply)
        for tau, d in enumerate(prefix):
            if d:
                h.add_term_to_constraint(row, y[tau], float(d))
        named.append((row, f"{dc} {sku} supply by {short_week(scn.weeks[t])}", supply))
    h.clear_objective()
    h.set_maximize(False)
    h.set_objective_coefficients(y, [-7.0 * value_per_day] * horizon)

    solver = mb.Solver("glop")
    solver.set_solver_specific_parameters(f"random_seed:{seed}")
    status = solver.solve(model)
    if status != mb.SolveStatus.OPTIMAL:  # pragma: no cover - y = 0 is always feasible
        return DcTts(dc, 0, ())
    total = sum(float(solver.value(mb.Variable(h, v))) for v in y)
    days = min(full, math.floor(7.0 * total + _TOL))
    binding: list[BindingConstraint] = []
    for ct, name, ub in named:
        lc = mb.LinearConstraint(h, index=ct)
        price = -float(solver.dual_value(lc))
        if price < 0.5 or float(solver.activity(lc)) < ub - 1e-6 * max(1.0, abs(ub)):
            continue
        binding.append(BindingConstraint(name=name, shadow_price_cents=round(price)))
    return DcTts(dc, days, tuple(binding))


def ttr_by_dc(scn: Scenario) -> dict[str, int | None]:
    """Days from the horizon start to the first arrival of a disrupted receipt per DC
    (``None``: the DC has no disrupted receipt that ever arrives)."""
    out: dict[str, int | None] = {}
    for rl in scn.receipt_lines:
        if not rl.affected:
            continue
        out.setdefault(rl.dc, None)
        if rl.lost:
            continue
        days = max(0, (rl.eta_perturbed - scn.start).days)
        cur = out[rl.dc]
        out[rl.dc] = days if cur is None else min(cur, days)
    return out


def verdict(tts: int, ttr: int | None, horizon_days: int) -> TtsVerdict:
    if tts >= horizon_days:
        return "ok"
    if ttr is None:
        return "loss"
    if ttr <= tts:
        return "ok"
    if ttr <= tts + EXPEDITE_BAND_DAYS:
        return "expedite"
    return "loss"


def tts_report(scn: Scenario, lines: Iterable[RLine], *, seed: int = 0) -> tuple[list[TtsRow], list[BindingConstraint]]:
    """``ImpactResponse.tts`` rows (one per DC, sorted) and the bottlenecks (top 25)."""
    by_dc = tts_by_dc(scn, lines, seed=seed)
    ttr = ttr_by_dc(scn)
    full = 7 * scn.horizon
    rows = [
        TtsRow(
            dc_code=dc,
            cluster_code=None,
            tts_days=by_dc[dc].days,
            ttr_days=ttr.get(dc),
            verdict=verdict(by_dc[dc].days, ttr.get(dc), full),
        )
        for dc in scn.dcs
    ]
    binding = sorted((b for d in by_dc.values() for b in d.binding), key=lambda b: (-b.shadow_price_cents, b.name))
    return rows, binding[:_MAX_BOTTLENECKS]


def network_tts(by_dc: dict[str, DcTts], scn: Scenario) -> int:
    """The weakest DC's TTS (DCs without demand do not count)."""
    full = 7 * scn.horizon
    with_demand = [
        d.days for dc, d in by_dc.items() if any((c, k) in scn.demand for c in scn.dc_clusters[dc] for k in scn.skus)
    ]
    return min(with_demand, default=full)
