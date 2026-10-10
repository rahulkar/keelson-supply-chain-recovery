"""Batch PAB simulator: :func:`keelson_opt.impact.simulate` for ``n`` sampled futures at once.

Per DC the state is an int64 tensor ``(samples, SKUs, clusters of the DC)`` and only the weeks
are looped. The rules are exactly the scalar simulator's (``impact`` module docstring):

* the pool starts at the DC supply minus the plan's outbound transfers and receives the sampled
  receipts and the inbound transfers of each week;
* every cluster requests ``max(0, demand + protected_min - stock)``; a short pool is rationed
  pro-rata (floor, then one unit each to the largest remainders, ties by cluster order) — clusters
  without demand, stock or a protected minimum request nothing, so padding every DC to all its
  clusters changes no allocation (a zero request never has a remainder);
* firm allocations (``prioritize_allocation``) take their units first, the other clusters share
  the rest;
* ``lost = demand - min(stock + alloc, demand)``; a cluster x SKU x week is short when
  ``PAB < protected_min``.

With zero noise and every sampled value at its nominal value the per-sample results equal the
scalar simulator's (``tests/test_batch_sim_m8.py``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from keelson_opt.impact import Transfer
from keelson_opt.robust.draws import IntArray
from keelson_opt.robust.sample import Arrival, Futures
from keelson_opt.scn import Scenario

__all__ = ["BatchResult", "ration", "simulate_batch"]


@dataclass(slots=True)
class BatchResult:
    """Per-sample totals (int64 vectors of length ``samples``)."""

    lost_units: IntArray
    lost_margin_cents: IntArray
    demand: IntArray
    short_cluster_weeks: IntArray
    lost_by_key: dict[tuple[str, str], IntArray] | None = None
    """``(cluster, sku) → lost units per sample`` (only with ``keep_keys``; tests)."""

    @property
    def fill_rate_bp(self) -> IntArray:
        served = self.demand - self.lost_units
        return np.where(self.demand > 0, served * 10_000 // np.maximum(self.demand, 1), 10_000)


def ration(pool: IntArray, requests: IntArray) -> IntArray:
    """Vectorised :func:`keelson_opt.impact.ration`: ``pool`` ``(...)``, ``requests``
    ``(..., m)`` → allocation ``(..., m)`` (largest remainder, ties by position)."""
    total = requests.sum(axis=-1)
    full = total <= pool
    out = requests.copy()
    if requests.shape[-1] == 0 or full.all():
        return out
    short = ~full
    p = np.maximum(pool[short], 0)
    r = requests[short]
    t = np.maximum(total[short], 1)[:, None]
    num = p[:, None] * r
    alloc = num // t
    rem = num % t
    leftover = p - alloc.sum(axis=-1)
    order = np.argsort(-rem, axis=-1, kind="stable")
    rank = np.empty_like(order)
    np.put_along_axis(rank, order, np.broadcast_to(np.arange(r.shape[-1]), order.shape), axis=-1)
    out[short] = alloc + (rank < leftover[:, None])
    return out


def _firm_then_fair(pool: IntArray, reqs: IntArray, fixed: Sequence[int], wanted: Sequence[int]) -> IntArray:
    """Vectorised ``impact._firm_then_fair`` for one (dc, sku, week): ``pool`` ``(n,)``,
    ``reqs`` ``(n, clusters)``."""
    n, m = reqs.shape
    want = np.broadcast_to(np.asarray(wanted, dtype=np.int64), (n, len(fixed)))
    given = ration(pool, want)
    rest = pool - given.sum(axis=1)
    alloc = np.zeros((n, m), dtype=np.int64)
    alloc[:, list(fixed)] = given
    free = [i for i in range(m) if i not in set(fixed)]
    if free:
        alloc[:, free] = ration(rest, reqs[:, free])
    return alloc


def simulate_batch(
    futures: Futures,
    arrivals: Sequence[Arrival],
    transfers: Sequence[Transfer] = (),
    *,
    overrides: Mapping[tuple[str, str, int], Mapping[str, int]] | None = None,
    keep_keys: bool = False,
) -> BatchResult:
    """Simulate ``arrivals`` (+ transfers, firm overrides) in every sampled future."""
    scn: Scenario = futures.scn
    n, h = futures.n, scn.horizon
    skus = scn.skus
    k_of = {s: i for i, s in enumerate(skus)}
    margin = np.array([scn.price_cents[s] - scn.cost_cents[s] for s in skus], dtype=np.int64)

    by_dc: dict[str, list[Arrival]] = defaultdict(list)
    for a in arrivals:
        by_dc[a.dc].append(a)
    outbound: dict[tuple[str, str], int] = defaultdict(int)
    inbound: dict[str, IntArray] = {}
    for tr in transfers:
        outbound[(tr.lane.from_dc, tr.sku)] += tr.qty
        if tr.arrive_week < h:
            arr = inbound.setdefault(tr.lane.to_dc, np.zeros((len(skus), h), dtype=np.int64))
            arr[k_of[tr.sku], tr.arrive_week] += tr.qty
    firm: dict[str, dict[int, list[tuple[int, Mapping[str, int]]]]] = defaultdict(lambda: defaultdict(list))
    for (dc, sku, t), alloc in (overrides or {}).items():
        if alloc:
            firm[dc][t].append((k_of[sku], alloc))

    lost_units = np.zeros(n, dtype=np.int64)
    lost_margin = np.zeros(n, dtype=np.int64)
    demand_total = np.zeros(n, dtype=np.int64)
    short_cw = np.zeros(n, dtype=np.int64)
    lost_by_key: dict[tuple[str, str], IntArray] | None = {} if keep_keys else None

    for dc in scn.dcs:
        clusters = scn.dc_clusters[dc]
        if not clusters:
            continue
        m = len(clusters)
        dem = futures.demand(dc, clusters, skus)  # (n, K, m, h)
        pmin = np.array([[scn.protected_min.get((c, s), 0) for c in clusters] for s in skus], dtype=np.int64)
        stock = np.broadcast_to(
            np.array([[scn.cluster_on_hand.get((c, s), 0) for c in clusters] for s in skus], dtype=np.int64),
            (n, len(skus), m),
        ).copy()
        supply = np.array([scn.dc_supply.get((dc, s), 0) - outbound.get((dc, s), 0) for s in skus], dtype=np.int64)
        if (supply < 0).any():
            raise ValueError(f"transfers out of {dc} exceed its supply")
        pool = np.broadcast_to(supply, (n, len(skus))).copy()
        receipts = np.zeros((n, len(skus), h), dtype=np.int64)
        rows = by_dc.get(dc, ())
        if rows:
            weeks = futures.weeks(rows)  # (L, n)
            k_idx = np.repeat(np.array([k_of[a.sku] for a in rows], dtype=np.int64), n)
            qty = np.repeat(np.array([a.qty for a in rows], dtype=np.int64), n)
            s_idx = np.tile(np.arange(n, dtype=np.int64), len(rows))
            w = weeks.reshape(-1)
            ok = (w >= 0) & (w < h)
            np.add.at(receipts, (s_idx[ok], k_idx[ok], w[ok]), qty[ok])
        inb = inbound.get(dc)
        lost_dc = np.zeros((n, len(skus), m), dtype=np.int64) if keep_keys else None
        for t in range(h):
            pool += receipts[:, :, t]
            if inb is not None:
                pool += inb[:, t]
            d_t = dem[:, :, :, t]
            reqs = np.maximum(0, d_t + pmin - stock)
            alloc = ration(pool, reqs)
            for k, firm_alloc in firm[dc].get(t, ()):
                fixed = [i for i, c in enumerate(clusters) if c in firm_alloc]
                wanted = [firm_alloc[clusters[i]] for i in fixed]
                alloc[:, k, :] = _firm_then_fair(pool[:, k], reqs[:, k, :], fixed, wanted)
            pool -= alloc.sum(axis=-1)
            available = stock + alloc
            pab = available - d_t
            lost = d_t - np.minimum(available, d_t)
            short = pab < pmin
            stock = np.maximum(pab, 0)
            lost_units += lost.sum(axis=(1, 2))
            lost_margin += (lost.sum(axis=2) * margin).sum(axis=1)
            demand_total += d_t.sum(axis=(1, 2))
            short_cw += short.any(axis=1).sum(axis=1)
            if lost_dc is not None:
                lost_dc += lost
        if lost_by_key is not None and lost_dc is not None:
            for a_i, c in enumerate(clusters):
                for k, s in enumerate(skus):
                    col = lost_dc[:, k, a_i]
                    if col.any():
                        lost_by_key[(c, s)] = col.copy()
    return BatchResult(lost_units, lost_margin, demand_total, short_cw, lost_by_key)
