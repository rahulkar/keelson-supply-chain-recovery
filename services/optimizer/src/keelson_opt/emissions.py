"""Incremental transport CO₂e of a plan (``docs/contracts/126-m8-co2-robustness.md`` §3).

Inputs travel in the request (the optimizer stays stateless): ``Sku.unit_weight_g``,
``Lane.distance_km`` (DC transfers, by road), ``InboundLane.legs`` and the factor set
``OptimizeRequest.emissions``. A lane's intensity is ``I = Σ legs km x g CO₂e/tkm`` (integer); one
unit of weight ``w`` grams on it emits ``w x I / 10⁹`` kg. Versus doing nothing, the plan adds:

* ``transfer``  — every transferred unit on its DC lane (road): ``+ qty x w x I(lane)``;
* ``expedite``  — a split unit flies instead of its booked lane: ``qty x w x (I(fast) - I(booked))``;
* ``nearshore`` — an alternate-source unit replaces one on the PO line's lane:
  ``qty x w x (I(alternate) - I(booked))`` (can be negative: near-shore road may beat deep sea);
* ``rebook``    — (M5) a decidable capacity booking the plan keeps / cancels differently from the
  baseline moves its units between the booked fast lane and its sea fallback;
* resequences and allocations move no goods: 0.

Each component total rounds half up to kg; ``incremental_co2e_kg`` is the sum of the rounded
components. Units whose weight or lane data is missing are left out and reported (``coverage_bp``,
``uncovered``). Scope: transport only, well-to-wheel per the factor set; production, packaging and
the last mile are out of scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from keelson_contracts.optimizer import Co2eBreakdown, EmissionsSpec, EmissionsSummary

from keelson_opt.plan import Plan
from keelson_opt.scn import Scenario

__all__ = ["Co2eResult", "plan_emissions"]

_G_PER_KG_X_TKM = 10**9
"""``w [g] x I [g/t]``: grams x grams per tonne → kg needs ``/ 10⁹``."""
MAX_UNCOVERED = 20
MAX_REF_CHARS = 128
"""``EmissionsSummary.uncovered`` entries are codes of at most 128 characters."""


def _kg(num: int) -> int:
    return (2 * num + _G_PER_KG_X_TKM) // (2 * _G_PER_KG_X_TKM)


@dataclass(slots=True)
class Co2eResult:
    incremental_kg: int
    breakdown: Co2eBreakdown
    summary: EmissionsSummary


@dataclass(slots=True)
class _Tally:
    covered: int = 0
    total: int = 0
    uncovered: set[str] = field(default_factory=set)


class _Intensities:
    def __init__(self, scn: Scenario, spec: EmissionsSpec) -> None:
        data = scn.data
        self.factors: Mapping[str, int] = {f.mode: f.g_co2e_per_tkm for f in spec.factors}
        self.weights: dict[str, int | None] = {s.code: s.unit_weight_g for s in data.skus} if data else {}
        self.inbound: dict[str, int | None] = {}
        self.dc: dict[str, int | None] = {}
        for il in data.inbound_lanes if data else ():
            self.inbound[il.code] = self._legs([(leg.mode, leg.distance_km) for leg in il.legs])
        road = self.factors.get("road")
        for ln in data.lanes if data else ():
            self.dc[ln.code] = None if ln.distance_km is None or road is None else ln.distance_km * road

    def _legs(self, legs: list[tuple[str, int]]) -> int | None:
        if not legs or any(mode not in self.factors for mode, _km in legs):
            return None
        return sum(km * self.factors[mode] for mode, km in legs)

    def per_unit(self, tally: _Tally, qty: int, sku: str, *lanes: tuple[str, int | None]) -> tuple[int, ...] | None:
        """``w x I`` per lane for ``qty`` units of ``sku`` (``None`` = not covered; recorded)."""
        tally.total += qty
        missing = [f"lane:{code}" for code, value in lanes if value is None]
        w = self.weights.get(sku)
        if w is None:
            missing.append(f"sku:{sku}")
        if missing:
            tally.uncovered.update(missing)
            return None
        tally.covered += qty
        return tuple(w * value for _code, value in lanes)  # type: ignore[operator]


def plan_emissions(scn: Scenario, plan: Plan, spec: EmissionsSpec) -> Co2eResult:
    lanes = _Intensities(scn, spec)
    tally = _Tally()

    def inbound(code: str | None) -> tuple[str, int | None]:
        return (code or "unbooked", lanes.inbound.get(code) if code else None)

    transfer = 0
    for tr in plan.transfers:
        got = lanes.per_unit(tally, tr.qty, tr.sku, (tr.lane.code, lanes.dc.get(tr.lane.code)))
        if got is not None:
            transfer += tr.qty * got[0]
    expedite = 0
    for e in plan.expedites:
        pol = scn.po_lines.get(e.ref)
        got = lanes.per_unit(tally, e.qty, e.sku, inbound(e.lane.code), inbound(pol.lane_code if pol else None))
        if got is not None:
            expedite += e.qty * (got[0] - got[1])
    nearshore = 0
    for a in plan.alt:
        pol = scn.po_lines.get(a.ref)
        got = lanes.per_unit(tally, a.qty, a.sku, inbound(a.source.lane_code), inbound(pol.lane_code if pol else None))
        if got is not None:
            nearshore += a.qty * (got[0] - got[1])
    rebook = 0
    kept = plan.rebook.kept if plan.rebook is not None else scn.baseline_kept
    for code in scn.decidable:
        change = int(code in kept) - int(code in scn.baseline_kept)
        if not change:
            continue
        b = scn.bookings[code]
        fast = scn.inbound_lanes[b.lane]
        for ln in b.lines:
            if ln.qty <= 0 or ln.revert_eta is None:
                continue  # units without a sea route are lost either way: no transport moves
            got = lanes.per_unit(tally, ln.qty, ln.sku, inbound(fast.code), inbound(fast.alt_lane_code))
            if got is not None:
                rebook += change * ln.qty * (got[0] - got[1])

    baseline = 0
    for pol in scn.po_lines.values():
        intensity = lanes.inbound.get(pol.lane_code) if pol.lane_code else None
        w = lanes.weights.get(pol.sku)
        if intensity is not None and w is not None:
            baseline += pol.qty * w * intensity

    breakdown = Co2eBreakdown(
        transfer_kg=_kg(transfer), expedite_kg=_kg(expedite), nearshore_kg=_kg(nearshore), rebook_kg=_kg(rebook)
    )
    total = breakdown.transfer_kg + breakdown.expedite_kg + breakdown.nearshore_kg + breakdown.rebook_kg
    summary = EmissionsSummary(
        source_id=spec.source_id,
        basis=spec.basis,
        coverage_bp=tally.covered * 10_000 // tally.total if tally.total else 10_000,
        baseline_inbound_kg=_kg(baseline),
        uncovered=[ref[:MAX_REF_CHARS] for ref in sorted(tally.uncovered)[:MAX_UNCOVERED]],
    )
    return Co2eResult(total, breakdown, summary)
