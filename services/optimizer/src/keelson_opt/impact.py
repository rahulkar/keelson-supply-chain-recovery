"""Deterministic impact simulation: weekly projected available balance (PAB) per
cluster x SKU, DC supply shared to clusters, KPIs, timeline.

DC -> cluster sharing rule ("fair share, pro-rata to demand"), per DC x SKU x week t:

1. ``pool`` starts at the DC's t=0 supply (on_hand - reserved + in_transit) minus all outbound
   transfers of the plan (Merxa reserves transfer quantities at the source DC when the
   transfer order is created). Each week, receipts and inbound transfers arriving in
   week t are added to the pool.
2. Each cluster c of the DC requests ``req_c = max(0, demand_c,t + protected_min_c -
   stock_c)`` where ``stock_c`` is the cluster's carried stock (initial cluster on-hand
   for t=0).
3. If ``pool >= sum(req)`` every request is filled. Otherwise the pool is rationed
   pro-rata to the requests (i.e. to this week's demand net of cluster stock):
   ``floor(pool * req_c / sum(req))``, leftover units go one each to the largest
   fractional remainders, ties broken by cluster code. Unallocated pool carries over.
4. Cluster balance: ``available = stock_c + alloc_c``; ``PAB = available - demand``;
   ``served = min(available, demand)``; ``lost = demand - served`` (lost sales, no
   backorders); carried stock = ``max(PAB, 0)``.

A cluster x SKU x week is *short* when ``PAB < protected_min`` (protected_min defaults to
0, i.e. ``PAB < 0``). KPIs:

* ``units_at_risk`` = sum of lost units; ``revenue_at_risk_cents`` = lost x retail price;
  ``margin_at_risk_cents`` = lost x (retail price - unit cost).
* ``shortage_cluster_weeks`` = number of distinct (cluster, week) pairs with at least one
  short SKU.
* ``first_breach`` = earliest short week per (dc, cluster, style), dated to the Monday.
* timeline (per cluster x style x week, summed over the style's sizes):
  ``projected_available`` = sum of signed PAB, ``demand`` = sum of demand,
  ``shortage`` = sum of ``max(0, protected_min - PAB)``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from keelson_contracts.optimizer import DcRisk, FirstBreach, ImpactKpis, TimelinePoint

from keelson_opt.scn import LaneInfo, Scenario


@dataclass(frozen=True, slots=True)
class Transfer:
    """A rounded DC->DC transfer (integer units) shipped in week ``ship_week``."""

    lane: LaneInfo
    sku: str
    qty: int
    ship_week: int

    @property
    def arrive_week(self) -> int:
        return self.ship_week + self.lane.lag_weeks


@dataclass(slots=True)
class ClusterSeries:
    demand: list[int]
    pab: list[int]
    lost: list[int]
    gap: list[int]
    """``max(0, protected_min - PAB)`` per week."""
    short: list[bool]


@dataclass(slots=True)
class SimResult:
    scn: Scenario
    series: dict[tuple[str, str], ClusterSeries]
    """Keyed by (cluster, sku); only cluster x SKU pairs with any demand, stock or
    protected minimum are present."""
    dc_end: dict[tuple[str, str], list[int]]
    """DC pool at the end of each week (after allocation to clusters)."""
    scarce: set[tuple[str, str, int]] = field(default_factory=set)
    """(dc, sku, week) where the pool could not fill every cluster request."""
    draws: dict[tuple[str, str, int], dict[str, int]] = field(default_factory=dict)
    """Cluster draws in the ``priority`` cells (what each cluster received)."""


def ration(pool: int, requests: Sequence[int]) -> list[int]:
    """Pro-rata integer allocation of ``pool`` to ``requests`` (largest remainder)."""
    total = sum(requests)
    if total <= pool:
        return list(requests)
    if pool <= 0:
        return [0] * len(requests)
    alloc = [pool * r // total for r in requests]
    leftover = pool - sum(alloc)
    order = sorted(range(len(requests)), key=lambda i: (-((pool * requests[i]) % total), i))
    for i in order[:leftover]:
        alloc[i] += 1
    return alloc


Overrides = Mapping[tuple[str, str, int], Mapping[str, int]]
"""Firm allocations ``(dc, sku, week) -> {cluster: units}`` (``prioritize_allocation``)."""


def simulate(
    scn: Scenario,
    transfers: Sequence[Transfer] = (),
    *,
    receipts: Mapping[tuple[str, str], Sequence[int]] | None = None,
    overrides: Overrides | None = None,
    keys: Collection[tuple[str, str]] | None = None,
    priority: Mapping[tuple[str, str, int], Mapping[str, int]] | None = None,
) -> SimResult:
    """Run the weekly PAB simulation.

    ``receipts`` replaces the scenario's receipt series (a plan's expedite / alternate-source /
    resequence arrivals). ``overrides`` are firm allocations: in that (dc, sku, week) the listed
    clusters receive exactly their units (rationed pro-rata when the pool is short) and no
    fair share; the other clusters share the rest under the normal rule. ``keys`` restricts
    the run to those (dc, sku) pairs (every pair is independent), for fast what-if checks.
    ``priority`` cells (``prioritize_allocation``) replace the pro-rata rule by the
    single-week optimum: protected allocation floors first, then every cluster's sales need
    ``max(0, demand - stock)``, then the remaining requests (buffers), each step pro-rata
    when short; the draws are recorded in ``SimResult.draws``.
    """
    horizon = scn.horizon
    rec_all = scn.receipts if receipts is None else receipts
    ov_all: Overrides = overrides or {}
    ov_clusters: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (dc, sku, _t), firm_alloc in ov_all.items():
        ov_clusters[(dc, sku)].update(firm_alloc)
    outbound: dict[tuple[str, str], int] = defaultdict(int)
    inbound: dict[tuple[str, str], list[int]] = {}
    for tr in transfers:
        outbound[(tr.lane.from_dc, tr.sku)] += tr.qty
        if tr.arrive_week < horizon:
            inbound.setdefault((tr.lane.to_dc, tr.sku), [0] * horizon)[tr.arrive_week] += tr.qty

    zeros = [0] * horizon
    series: dict[tuple[str, str], ClusterSeries] = {}
    dc_end: dict[tuple[str, str], list[int]] = {}
    scarce: set[tuple[str, str, int]] = set()
    draws: dict[tuple[str, str, int], dict[str, int]] = {}
    prio = priority or {}
    for dc in scn.dcs:
        clusters = scn.dc_clusters[dc]
        for sku in scn.skus:
            key = (dc, sku)
            if keys is not None and key not in keys:
                continue
            forced = ov_clusters.get(key, ())
            active = [
                c
                for c in clusters
                if (c, sku) in scn.demand
                or scn.cluster_on_hand.get((c, sku), 0)
                or scn.protected_min.get((c, sku), 0)
                or c in forced
            ]
            pool = scn.dc_supply.get(key, 0) - outbound.get(key, 0)
            if pool < 0:
                raise ValueError(f"transfers out of {dc} for {sku} exceed its supply")
            rec = rec_all.get(key, zeros)
            inb = inbound.get(key, zeros)
            if not active:
                ends = []
                for t in range(horizon):
                    pool += rec[t] + inb[t]
                    ends.append(pool)
                dc_end[key] = ends
                continue
            dem = [scn.demand.get((c, sku), zeros) for c in active]
            pmin = [scn.protected_min.get((c, sku), 0) for c in active]
            stock = [scn.cluster_on_hand.get((c, sku), 0) for c in active]
            out = [ClusterSeries([], [], [], [], []) for _ in active]
            ends = []
            for t in range(horizon):
                pool += rec[t] + inb[t]
                reqs = [max(0, dem[i][t] + pmin[i] - stock[i]) for i in range(len(active))]
                firm = ov_all.get((dc, sku, t))
                floors = prio.get((dc, sku, t))
                if firm:
                    alloc, pool, short = _firm_then_fair(pool, active, reqs, firm)
                elif floors is not None:
                    short = sum(reqs) > pool
                    needs = [max(0, dem[i][t] - stock[i]) for i in range(len(active))]
                    alloc = _priority(pool, active, reqs, needs, floors)
                    pool -= sum(alloc)
                    draws[(dc, sku, t)] = {c: a for c, a in zip(active, alloc, strict=True)}
                else:
                    short = sum(reqs) > pool
                    alloc = ration(pool, reqs)
                    pool -= sum(alloc)
                if short:
                    scarce.add((dc, sku, t))
                for i, cs in enumerate(out):
                    d = dem[i][t]
                    available = stock[i] + alloc[i]
                    pab = available - d
                    served = min(available, d)
                    cs.demand.append(d)
                    cs.pab.append(pab)
                    cs.lost.append(d - served)
                    cs.gap.append(max(0, pmin[i] - pab))
                    cs.short.append(pab < pmin[i])
                    stock[i] = max(pab, 0)
                ends.append(pool)
            dc_end[key] = ends
            for i, c in enumerate(active):
                series[(c, sku)] = out[i]
    return SimResult(scn=scn, series=series, dc_end=dc_end, scarce=scarce, draws=draws)


def _firm_then_fair(
    pool: int, active: Sequence[str], reqs: Sequence[int], firm: Mapping[str, int]
) -> tuple[list[int], int, bool]:
    """Firm allocations first (pro-rata when short), then the fair share of the rest."""
    fixed = [i for i, c in enumerate(active) if c in firm]
    wanted = [firm[active[i]] for i in fixed]
    given = ration(pool, wanted)
    pool -= sum(given)
    alloc = [0] * len(active)
    for i, g in zip(fixed, given, strict=True):
        alloc[i] = g
    free = [i for i, c in enumerate(active) if c not in firm]
    free_req = [reqs[i] for i in free]
    short = sum(given) < sum(wanted) or sum(free_req) > pool
    for i, g in zip(free, ration(pool, free_req), strict=True):
        alloc[i] = g
        pool -= g
    return alloc, pool, short


def _priority(
    pool: int, active: Sequence[str], reqs: Sequence[int], needs: Sequence[int], floors: Mapping[str, int]
) -> list[int]:
    """Protected floors, then sales needs, then the rest of the requests (pro-rata each)."""
    alloc = [0] * len(active)
    for want in (
        [min(floors.get(c, 0), reqs[i]) for i, c in enumerate(active)],
        list(needs),
        list(reqs),
    ):
        rest = [max(0, w - a) for w, a in zip(want, alloc, strict=True)]
        give = ration(pool, rest)
        pool -= sum(give)
        alloc = [a + g for a, g in zip(alloc, give, strict=True)]
    return alloc


def lost_margin(res: SimResult) -> int:
    """Lost units x (retail - unit cost), summed over the simulated pairs."""
    scn = res.scn
    return sum(sum(cs.lost) * (scn.price_cents[sku] - scn.cost_cents[sku]) for (_c, sku), cs in res.series.items())


def kpis(res: SimResult) -> ImpactKpis:
    scn = res.scn
    units = revenue = margin = 0
    short_cw: set[tuple[str, int]] = set()
    breach: dict[tuple[str, str], int] = {}
    for (cl, sku), cs in res.series.items():
        lost = sum(cs.lost)
        units += lost
        revenue += lost * scn.price_cents[sku]
        margin += lost * (scn.price_cents[sku] - scn.cost_cents[sku])
        for t, is_short in enumerate(cs.short):
            if is_short:
                short_cw.add((cl, t))
                key = (cl, scn.sku_style[sku])
                if t < breach.get(key, scn.horizon):
                    breach[key] = t
    first = sorted((t, scn.cluster_dc[cl], cl, style) for (cl, style), t in breach.items())
    return ImpactKpis(
        units_at_risk=units,
        revenue_at_risk_cents=revenue,
        margin_at_risk_cents=margin,
        shortage_cluster_weeks=len(short_cw),
        first_breach=[
            FirstBreach(dc_code=dc, cluster_code=cl, style_code=style, week=scn.weeks[t], date=scn.week_mondays[t])
            for t, dc, cl, style in first
        ],
    )


def by_dc(res: SimResult) -> list[DcRisk]:
    scn = res.scn
    units: dict[str, int] = defaultdict(int)
    revenue: dict[str, int] = defaultdict(int)
    for (cl, sku), cs in res.series.items():
        lost = sum(cs.lost)
        dc = scn.cluster_dc[cl]
        units[dc] += lost
        revenue[dc] += lost * scn.price_cents[sku]
    return [DcRisk(dc_code=dc, units_at_risk=units[dc], revenue_at_risk_cents=revenue[dc]) for dc in scn.dcs]


def timeline(res: SimResult) -> list[TimelinePoint]:
    scn = res.scn
    horizon = scn.horizon
    agg: dict[tuple[str, str], tuple[list[int], list[int], list[int]]] = {}
    for (cl, sku), cs in res.series.items():
        style = scn.sku_style[sku]
        pab, dem, gap = agg.setdefault((cl, style), ([0] * horizon, [0] * horizon, [0] * horizon))
        for t in range(horizon):
            pab[t] += cs.pab[t]
            dem[t] += cs.demand[t]
            gap[t] += cs.gap[t]
    points: list[TimelinePoint] = []
    for cl, style in sorted(agg):
        pab, dem, gap = agg[(cl, style)]
        for t in range(horizon):
            points.append(
                TimelinePoint(
                    cluster_code=cl,
                    style_code=style,
                    week=scn.weeks[t],
                    projected_available=pab[t],
                    demand=dem[t],
                    shortage=gap[t],
                )
            )
    return points
