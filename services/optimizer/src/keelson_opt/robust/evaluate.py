"""Robustness of one plan (``mc/1``): the plan and the do-nothing baseline, simulated in the same
sampled futures (``126`` §5).

* ``samples`` = the requested count, capped deterministically by :data:`CELL_BUDGET` sample-cells
  (``samples x SKUs x clusters x weeks``; never by wall clock, so a result never depends on load);
* per sample ``net_benefit = (baseline lost margin - plan lost margin) - incremental cost`` (costs
  are not sampled);
* bands are nearest-rank quantiles (index ``ceil(q * n / 100) - 1`` of the sorted values);
* ``stockout_chance_bp`` = share of the samples in which the plan loses some demand.

Not sampled (documented in the contract): costs and rates, time-to-survive and the first-breach
shift, capacity for bookings already decided, the recovery tail after a disruption (TTR).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from keelson_contracts.optimizer import Band, RobustnessResult, RobustnessSpec, ScorecardBands

from keelson_opt.plan import Plan
from keelson_opt.robust.batch import BatchResult, simulate_batch
from keelson_opt.robust.draws import IntArray
from keelson_opt.robust.sample import Futures, plan_arrivals
from keelson_opt.scn import Scenario

__all__ = ["CELL_BUDGET", "Robustness", "effective_samples", "evaluate", "nearest_rank"]

CELL_BUDGET = 2_500_000
"""Sample-cells (samples x SKUs x clusters x weeks) one robustness run may simulate."""
_STREAM_ORDER = ("disruption_severity", "disruption_duration", "lead_time", "demand")


@dataclass(frozen=True, slots=True)
class Robustness:
    bands: ScorecardBands
    result: RobustnessResult


def effective_samples(scn: Scenario, requested: int) -> int:
    cells = max(1, len(scn.skus) * len(scn.clusters) * scn.horizon)
    return min(requested, max(8, CELL_BUDGET // cells))


def nearest_rank(values: IntArray, q: int) -> int:
    ordered = np.sort(values)
    return int(ordered[max(0, math.ceil(q * len(ordered) / 100) - 1)])


def _band(values: IntArray) -> Band:
    return Band(p10=nearest_rank(values, 10), p50=nearest_rank(values, 50), p90=nearest_rank(values, 90))


def evaluate(
    scn: Scenario,
    plan: Plan,
    spec: RobustnessSpec,
    perturbations: Sequence[Any],
    *,
    incremental_cost: int,
    crn_key: str,
) -> Robustness:
    n = effective_samples(scn, spec.samples)
    futures = Futures(scn, perturbations, spec, n)
    baseline = simulate_batch(futures, plan_arrivals(scn, None))
    if plan.empty:
        after: BatchResult = baseline
    else:
        after = simulate_batch(futures, plan_arrivals(scn, plan), plan.transfers, overrides=plan.overrides)
    net = (baseline.lost_margin_cents - after.lost_margin_cents) - incremental_cost
    bands = ScorecardBands(
        samples=n,
        fill_rate_bp=_band(after.fill_rate_bp),
        lost_units=_band(after.lost_units),
        lost_margin_cents=_band(after.lost_margin_cents),
        net_benefit_cents=_band(net),
        shortage_days=_band(7 * after.short_cluster_weeks),
        stockout_chance_bp=int((after.lost_units > 0).sum()) * 10_000 // n,
    )
    result = RobustnessResult(
        method="mc/1",
        seed=spec.seed,
        samples=n,
        requested_samples=spec.samples,
        sampled=[s for s in _STREAM_ORDER if s in futures.sampled],  # type: ignore[misc]
        crn_key=crn_key,
        per_sample_net_benefit_cents=[int(v) for v in net],
        per_sample_lost_margin_cents=[int(v) for v in after.lost_margin_cents],
    )
    return Robustness(bands, result)
