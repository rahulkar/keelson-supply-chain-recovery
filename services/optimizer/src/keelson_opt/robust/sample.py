"""Sampled futures of one ``/v1/optimize`` request (robustness ``mc/1``, ``126`` §5).

:class:`Futures` draws, per sample, everything the futures depend on — never the plan:

* **perturbation values** (``RobustnessSpec.value_bands``): a ``lead_time_shift``'s ``days``, a
  ``capacity_scale`` / ``demand_scale``'s ``factor_bp`` from their own ``p10`` / ``p90``;
* **window durations** (``window_bands``): the window keeps its start and lasts the sampled number
  of days;
* **transit noise** (``lead_time_noise``) per arrival, by the mode of the lane it arrives on;
* **demand noise** (``demand_noise_bp``): one factor per (cluster, style), constant across sizes
  and weeks (a forecast bias).

Arrivals are dated: :func:`plan_arrivals` mirrors :func:`keelson_opt.plan.plan_lines` (bookings
kept / cancelled, resequence moves, reductions from the latest arrival, then the new expedite and
alternate-source arrivals) but keeps each arrival's nominal date. Per sample:

* a receipt (``line``) or a booking's sea fallback (``revert``) is ready ``Δ`` days later, where
  ``Δ`` is the sampled readiness delay minus the nominal one (the matching lead-time shifts, as
  :func:`keelson_opt.scn.build_scenario` matches them); a lane with a closure in that sample (the
  closures are rebuilt from the sampled capacity perturbations) applies
  :func:`keelson_opt.scn.closure_delay` again — the receipt may then be later, earlier or lost;
* a resequenced line moves by its PO line's ``Δ``; an expedite split by ``max(0, Δ)`` (a booked
  departure cannot leave earlier; capacity is assumed available); an alternate-source buy does
  not move;
* every arrival then gets its transit noise. Lines lost in the nominal run stay lost.

With every sampled value at its nominal value and zero noise each arrival lands in its nominal
week, so the batch simulator reproduces :func:`keelson_opt.impact.simulate` exactly.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np
from keelson_contracts.optimizer import RobustnessSpec

from keelson_opt.plan import Plan
from keelson_opt.robust.draws import IntArray, quantile, uniform_bp
from keelson_opt.scn import (
    MODE_SPEED,
    Closure,
    InboundLaneInfo,
    ReceiptLine,
    Scenario,
    _get,  # noqa: PLC2701 - shared matching rules of the scenario builder
    _network_attrs,  # noqa: PLC2701
    _Target,  # noqa: PLC2701
    _target,  # noqa: PLC2701
    closure_delay,
)

__all__ = ["Arrival", "ArrivalKind", "Futures", "RobustnessError", "plan_arrivals"]

ArrivalKind = Literal["line", "revert", "moved", "expedite", "alt"]
_SAMPLED_OPS = frozenset({"lead_time_shift", "capacity_scale", "demand_scale"})
_ONE_WEEK = dt.timedelta(days=6)


class RobustnessError(ValueError):
    """The robustness spec does not fit the request (mapped to HTTP 422)."""


@dataclass(frozen=True, slots=True)
class Arrival:
    """One dated arrival of a plan (or of the do-nothing baseline)."""

    key: str
    """Draw key of its transit noise (stable across the options of a plan)."""
    dc: str
    sku: str
    qty: int
    date: dt.date
    """Nominal arrival date."""
    week: int
    """Nominal bucket, as the plan ledger has it (``>= horizon`` = after the horizon)."""
    kind: ArrivalKind
    ref: str | None
    mode: str
    lane: str | None = None
    """``line`` / ``revert``: the inbound lane whose closures apply."""
    ready: dt.date | None = None
    """``line`` / ``revert``: nominal readiness (arrival = ready + closure delay)."""
    receipt: tuple[str, str, str, dt.date] | None = None
    """``line``: the receipt ``(ref, dc, sku, eta)`` (its own readiness delta)."""


@dataclass(frozen=True, slots=True)
class _Param:
    """A sampled perturbation: nominal value, per-sample values and window end."""

    index: int
    op: str
    target: _Target
    nominal: int
    values: IntArray
    """Per-sample value (``days`` or ``factor_bp``)."""
    end: dt.date | None
    """Nominal window end."""
    ends: IntArray | None
    """Per-sample window end (ordinal days) when the duration is sampled."""
    weeks: frozenset[str] | None = None
    """``demand_scale.weeks``."""

    def end_ordinals(self, n: int) -> IntArray | None:
        """Per-sample window end as ordinals (``None`` = open-ended)."""
        if self.ends is not None:
            return self.ends
        if self.end is None:
            return None
        return np.full(n, self.end.toordinal(), dtype=np.int64)


def _band_of(p: Any, nominal: int) -> tuple[int, int, int] | None:
    p10, p90 = _get(p, "p10"), _get(p, "p90")
    low = int(p10) if isinstance(p10, int) and not isinstance(p10, bool) else nominal
    high = int(p90) if isinstance(p90, int) and not isinstance(p90, bool) else nominal
    if low == nominal == high:
        return None
    return (min(low, nominal), nominal, max(high, nominal))


class Futures:
    """The ``n`` sampled futures of a request (shared by the baseline and the plan batch)."""

    def __init__(self, scn: Scenario, perturbations: Sequence[Any], spec: RobustnessSpec, n: int) -> None:
        self.scn = scn
        self.n = n
        self.seed = spec.seed
        self.start_ord = scn.start.toordinal()
        self.sampled: set[str] = set()
        windows = {w.perturbation: w.duration_days for w in spec.window_bands}
        self.params: list[_Param] = []
        for idx, p in enumerate(perturbations):
            op = str(_get(p, "op", ""))
            band_w = windows.get(idx)
            if op not in _SAMPLED_OPS:
                if band_w is not None:
                    raise RobustnessError(f"robustness.window_bands: perturbation {idx} ({op}) cannot be sampled")
                continue
            tgt = _target(p)
            nominal = int(_get(p, "days", 0) if op == "lead_time_shift" else _get(p, "factor_bp", 10000))
            values = np.full(n, nominal, dtype=np.int64)
            band = _band_of(p, nominal) if spec.value_bands else None
            if band is not None:
                floor = None if op == "lead_time_shift" else 0
                values = quantile(uniform_bp(self.seed, f"value:{idx}", "", n), band, minimum=floor)
                self.sampled.add("disruption_severity")
            ends: IntArray | None = None
            if band_w is not None:
                if tgt.start is None or tgt.end is None:
                    raise RobustnessError(f"robustness.window_bands: perturbation {idx} has no bounded window")
                length = (tgt.end - tgt.start).days + 1
                if band_w.p50 != length:
                    raise RobustnessError(
                        f"robustness.window_bands: perturbation {idx} lasts {length} days, the band's p50 is {band_w.p50}"
                    )
                days = quantile(uniform_bp(self.seed, f"window:{idx}", "", n), (band_w.p10, band_w.p50, band_w.p90),
                                minimum=1)
                ends = tgt.start.toordinal() + days - 1
                self.sampled.add("disruption_duration")
            wk = _get(p, "weeks") if op == "demand_scale" else None
            self.params.append(_Param(idx, op, tgt, nominal, values, tgt.end, ends, frozenset(wk) if wk else None))
        self.noise_bands: dict[str, tuple[int, int, int]] = {
            ln.mode: (ln.days.p10, ln.days.p50, ln.days.p90) for ln in spec.lead_time_noise
        }
        if any(b != (0, 0, 0) for b in self.noise_bands.values()):
            self.sampled.add("lead_time")
        self.demand_noise = None
        if spec.demand_noise_bp is not None:
            b = spec.demand_noise_bp
            self.demand_noise = (b.p10, b.p50, b.p90)
            if self.demand_noise != (10000, 10000, 10000):
                self.sampled.add("demand")
        self._noise: dict[str, IntArray] = {}
        self._demand: dict[tuple[str, tuple[str, ...], tuple[str, ...]], IntArray] = {}
        self._closure_memo: dict[tuple[int, str, int], tuple[int, bool]] = {}
        self._readiness()
        self._closures()

    # ───────────────────────────── readiness ─────────────────────────────

    def _readiness(self) -> None:
        """Per-receipt and per-PO-line readiness delta (sampled minus nominal days)."""
        scn, n = self.scn, self.n
        shifts = [p for p in self.params if p.op == "lead_time_shift"]
        self.receipt_delta: dict[tuple[str, str, str, dt.date], IntArray] = {}
        self.ref_delta: dict[str, IntArray] = {}
        if not shifts or scn.data is None:
            return
        varied = [p for p in shifts if p.ends is not None or bool((p.values != p.nominal).any())]
        if not varied:
            return
        ends = {p.index: p.end_ordinals(n) for p in shifts}
        ref_nom: dict[str, int] = {}
        ref_s: dict[str, IntArray] = {}
        for r in scn.data.receipts:
            pol = scn.po_lines.get(r.ref)
            attrs: dict[str, Any] = {
                "receipt": r.ref,
                "po_line": r.ref,
                "po": r.ref.split("#", 1)[0],
                "dc": r.dc_code,
                "sku": r.sku,
                "style": scn.sku_style[r.sku],
            }
            if pol is not None:
                attrs.update(_network_attrs(pol, scn.bom_by_style))
            nominal = 0
            sampled = np.zeros(n, dtype=np.int64)
            eta = r.eta.toordinal()
            for p in shifts:
                if not p.target.matches(attrs):
                    continue
                start_ok = p.target.start is None or r.eta >= p.target.start
                if start_ok and (p.end is None or r.eta <= p.end):
                    nominal += p.nominal
                e = ends[p.index]
                if start_ok:
                    covers = np.ones(n, dtype=bool) if e is None else e >= eta
                    sampled += np.where(covers, p.values, 0)
            delta = sampled - nominal
            key = (r.ref, r.dc_code, r.sku, r.eta)
            if delta.any():
                self.receipt_delta[key] = delta
            ref_nom[r.ref] = max(ref_nom.get(r.ref, nominal), nominal)
            ref_s[r.ref] = np.maximum(ref_s[r.ref], sampled) if r.ref in ref_s else sampled
        for ref, s in ref_s.items():
            delta = s - ref_nom[ref]
            if delta.any():
                self.ref_delta[ref] = delta

    def readiness_delta(self, a: Arrival) -> IntArray | None:
        if a.kind == "line":
            return self.receipt_delta.get(a.receipt) if a.receipt is not None else None
        if a.kind == "alt" or a.ref is None:
            return None
        delta = self.ref_delta.get(a.ref)
        if delta is not None and a.kind == "expedite":
            delta = np.maximum(delta, 0)
            return delta if delta.any() else None
        return delta

    # ───────────────────────────── closures ─────────────────────────────

    def _closures(self) -> None:
        """Lane closures per sample: one inbound-lane table per distinct closure pattern."""
        scn, n = self.scn, self.n
        caps = [p for p in self.params if p.op == "capacity_scale"]
        self.sample_config = [0] * n
        self.configs: list[Mapping[str, InboundLaneInfo]] = [scn.inbound_lanes]
        self.closable: frozenset[str] = frozenset(code for code, lane in scn.inbound_lanes.items() if lane.closures)
        sampled = [p for p in caps if p.ends is not None or bool((p.values != p.nominal).any())]
        if not sampled:
            return
        matched: dict[str, list[_Param]] = defaultdict(list)
        for code, lane in scn.inbound_lanes.items():
            attrs: dict[str, Any] = {"lane": code, "port": frozenset(lane.points), "chokepoint": frozenset(lane.points)}
            for p in caps:
                if p.target.matches(attrs):
                    matched[code].append(p)
        ends = {p.index: p.end_ordinals(n) for p in caps}
        index: dict[tuple[tuple[bool, int | None], ...], int] = {}
        self.configs = []
        closable: set[str] = set()
        for s in range(n):
            pattern = tuple(
                (bool(p.values[s] == 0), None if ends[p.index] is None else int(ends[p.index][s]))  # type: ignore[index]
                for p in caps
            )
            cfg = index.get(pattern)
            if cfg is None:
                cfg = index[pattern] = len(self.configs)
                lanes = dict(scn.inbound_lanes)
                for code, ps in matched.items():
                    closures = tuple(
                        Closure(p.target.start, None if ends[p.index] is None else dt.date.fromordinal(int(ends[p.index][s])))  # type: ignore[index]
                        for p in ps
                        if p.values[s] == 0
                    )
                    lanes[code] = replace(lanes[code], closures=closures)
                    if closures:
                        closable.add(code)
                self.configs.append(lanes)
            self.sample_config[s] = cfg
        self.closable = frozenset(closable)

    def _closure(self, cfg: int, lane: str, ready: int) -> tuple[int, bool]:
        key = (cfg, lane, ready)
        hit = self._closure_memo.get(key)
        if hit is None:
            lanes = self.configs[cfg]
            info = lanes[lane]
            hit = closure_delay(info, dt.date.fromordinal(ready), lanes) if info.closures else (0, False)  # type: ignore[arg-type]
            self._closure_memo[key] = hit
        return hit

    # ───────────────────────────── noise ─────────────────────────────

    def noise(self, key: str, mode: str) -> IntArray | None:
        band = self.noise_bands.get(mode) or self.noise_bands.get("default")
        if band is None or band == (0, 0, 0):
            return None
        cache_key = f"{mode}|{key}"
        out = self._noise.get(cache_key)
        if out is None:
            out = self._noise[cache_key] = quantile(uniform_bp(self.seed, "lt", key, self.n), band)
        return out

    # ───────────────────────────── arrival weeks ─────────────────────────────

    def weeks(self, arrivals: Sequence[Arrival]) -> IntArray:
        """Per-sample buckets of ``arrivals``: ``(len(arrivals), n)``, ``-1`` = lost in that sample."""
        n = self.n
        dates = np.array([a.date.toordinal() for a in arrivals], dtype=np.int64)
        arrival = np.repeat(dates[:, None], n, axis=1)
        lost = np.zeros(arrival.shape, dtype=bool)
        for i, a in enumerate(arrivals):
            delta = self.readiness_delta(a)
            if a.kind in ("line", "revert") and a.lane in self.closable and a.ready is not None:
                ready = a.ready.toordinal() + (np.zeros(n, dtype=np.int64) if delta is None else delta)
                for s in range(n):
                    transport, gone = self._closure(self.sample_config[s], a.lane, int(ready[s]))  # type: ignore[arg-type]
                    arrival[i, s] = ready[s] + transport
                    lost[i, s] = gone
            elif delta is not None:
                arrival[i] += delta
            noise = self.noise(a.key, a.mode)
            if noise is not None:
                arrival[i] += noise
        nominal = np.maximum(0, np.floor_divide(dates - self.start_ord, 7))
        correction = np.array([a.week for a in arrivals], dtype=np.int64) - nominal
        weeks = np.maximum(0, np.floor_divide(arrival - self.start_ord, 7)) + correction[:, None]
        return np.where(lost, -1, weeks).astype(np.int64)

    # ───────────────────────────── demand ─────────────────────────────

    def demand(self, dc: str, clusters: Sequence[str], skus: Sequence[str]) -> IntArray:
        """Sampled demand of ``dc``: ``(n, len(skus), len(clusters), horizon)`` int64 (computed
        once per DC; the baseline and the plan batch read the same array)."""
        key = (dc, tuple(clusters), tuple(skus))
        hit = self._demand.get(key)
        if hit is None:
            hit = self._demand[key] = self._sample_demand(dc, clusters, skus)
            hit.flags.writeable = False
        return hit

    def _sample_demand(self, dc: str, clusters: Sequence[str], skus: Sequence[str]) -> IntArray:
        scn, n, h = self.scn, self.n, self.scn.horizon
        raw = self._raw_demand()
        k_of = {s: i for i, s in enumerate(skus)}
        a_of = {c: i for i, c in enumerate(clusters)}
        base = np.zeros((len(skus), len(clusters), h), dtype=np.int64)
        keys: list[tuple[str, str]] = []
        for c in clusters:
            for sku, series in raw.get(c, {}).items():
                base[k_of[sku], a_of[c]] = series
                keys.append((c, sku))
        out = np.broadcast_to(base, (n, *base.shape)).copy()
        for p in self.params:
            if p.op != "demand_scale" or not keys:
                continue
            mask = np.zeros((len(skus), len(clusters)), dtype=bool)
            for c, sku in keys:
                attrs = {"cluster": c, "dc": dc, "sku": sku, "style": scn.sku_style[sku]}
                if p.target.matches(attrs):
                    mask[k_of[sku], a_of[c]] = True
            if not mask.any():
                continue
            in_weeks = np.array([p.weeks is None or scn.weeks[t] in p.weeks for t in range(h)], dtype=bool)
            mondays = np.array([m.toordinal() for m in scn.week_mondays], dtype=np.int64)
            start_ok = np.ones(h, dtype=bool) if p.target.start is None else mondays + 6 >= p.target.start.toordinal()
            ends = p.end_ordinals(n)
            end_ok = np.ones((n, h), dtype=bool) if ends is None else mondays[None, :] <= ends[:, None]
            week_mask = (in_weeks & start_ok)[None, :] & end_ok  # (n, h)
            sel = mask[None, :, :, None] & week_mask[:, None, None, :]
            scaled = (out * p.values[:, None, None, None] + 5000) // 10000
            out = np.where(sel, scaled, out)
        if self.demand_noise is not None and self.demand_noise != (10000, 10000, 10000):
            factor = np.ones((n, len(skus), len(clusters)), dtype=np.int64) * 10000
            for c in clusters:
                for style in sorted({scn.sku_style[s] for s in raw.get(c, {})}):
                    f = quantile(uniform_bp(self.seed, "dem", f"{c}|{style}", n), self.demand_noise, minimum=0)
                    for sku in raw.get(c, {}):
                        if scn.sku_style[sku] == style:
                            factor[:, k_of[sku], a_of[c]] = f
            out = (out * factor[..., None] + 5000) // 10000
        return out

    def _raw_demand(self) -> dict[str, dict[str, list[int]]]:
        """cluster → sku → raw weekly units inside the horizon (as ``build_scenario`` reads them)."""
        cached: dict[str, dict[str, list[int]]] | None = self.scn.memo.get("robust_raw_demand")
        if cached is not None:
            return cached
        scn = self.scn
        pos = {w: i for i, w in enumerate(scn.weeks)}
        raw: dict[str, dict[str, list[int]]] = defaultdict(dict)
        for row in scn.data.demand if scn.data is not None else ():
            t = pos.get(row.iso_week)
            if t is None:
                continue
            raw[row.cluster_code].setdefault(row.sku, [0] * scn.horizon)[t] += row.units
        out = scn.memo["robust_raw_demand"] = dict(raw)
        return out


# ───────────────────────────── dated plan arrivals ─────────────────────────────


@dataclass(frozen=True, slots=True)
class _Revert:
    date: dt.date
    lane: InboundLaneInfo | None


def _reverts(scn: Scenario) -> dict[int, _Revert]:
    """``id(ReceiptLine)`` of every booking's sea-fallback line → its revert date and sea lane."""
    out: dict[int, _Revert] = {}
    for b in scn.bookings.values():
        lane = scn.inbound_lanes[b.lane]
        sea = scn.inbound_lanes.get(lane.alt_lane_code) if lane.alt_lane_code else None
        if sea is not None and MODE_SPEED.get(sea.mode, 0) > MODE_SPEED.get(lane.mode, 0):
            sea = None
        for ln in b.lines:
            if ln.qty > 0 and ln.keep and ln.revert_eta is not None and ln.cancel:
                out[id(ln.cancel[-1])] = _Revert(ln.revert_eta, sea)
    return out


def _arrival(scn: Scenario, rl: ReceiptLine, reverts: Mapping[int, _Revert]) -> Arrival:
    rev = reverts.get(id(rl))
    if rev is not None:
        return Arrival(
            key=f"{rl.ref}|revert", dc=rl.dc, sku=rl.sku, qty=rl.qty, date=rl.eta_perturbed, week=rl.week,
            kind="revert", ref=rl.ref, mode=rev.lane.mode if rev.lane is not None else "default",
            lane=rev.lane.code if rev.lane is not None else None,
            ready=rev.date + dt.timedelta(days=rl.prod_delay),
        )
    pol = scn.po_lines.get(rl.ref)
    lane = scn.inbound_lanes.get(pol.lane_code) if pol is not None and pol.lane_code else None
    mode = lane.mode if lane is not None else (pol.mode if pol is not None else "default")
    return Arrival(
        key=f"{rl.ref}|{rl.dc}|{rl.sku}|{rl.eta.isoformat()}", dc=rl.dc, sku=rl.sku, qty=rl.qty,
        date=rl.eta_perturbed, week=rl.week, kind="line", ref=rl.ref, mode=mode,
        lane=lane.code if lane is not None else None, ready=rl.eta + dt.timedelta(days=rl.prod_delay),
        receipt=(rl.ref, rl.dc, rl.sku, rl.eta),
    )


def plan_arrivals(scn: Scenario, plan: Plan | None) -> list[Arrival]:
    """Dated arrivals of ``plan`` (``None`` = the scenario baseline), in the order and with the
    quantities and buckets of :func:`keelson_opt.plan.plan_lines`."""
    reverts = _reverts(scn)
    lines = [_arrival(scn, rl, reverts) for rl in scn.receipt_lines if not rl.lost and rl.qty > 0]
    if plan is None:
        return lines
    if plan.rebook is not None and scn.decidable:
        refs = {ln.ref for code in scn.decidable for ln in scn.bookings[code].lines}
        kept = plan.rebook.kept
        out = [a for a in lines if a.ref not in refs]
        for code in scn.decidable:
            for ln in scn.bookings[code].lines:
                for rl in ln.keep if code in kept else ln.cancel:
                    if not rl.lost and rl.qty > 0:
                        out.append(_arrival(scn, rl, reverts))
        lines = out
    if plan.moves:
        lines = [
            replace(a, kind="moved", date=plan.moves[a.ref].eta_after, week=plan.moves[a.ref].week, lane=None,
                    ready=None, receipt=None)
            if a.ref in plan.moves
            else a
            for a in lines
        ]
    cut = plan.reductions()
    if cut:
        by_ref: dict[str, list[int]] = defaultdict(list)
        for i, a in enumerate(lines):
            if a.ref is not None:
                by_ref[a.ref].append(i)
        qty = [a.qty for a in lines]
        for ref, units in cut.items():
            for i in sorted(by_ref.get(ref, ()), key=lambda i: (-lines[i].week, i)):
                take = min(units, qty[i])
                qty[i] -= take
                units -= take
                if not units:
                    break
        lines = [replace(a, qty=q) for a, q in zip(lines, qty, strict=True) if q > 0]
    for e in plan.expedites:
        lines.append(Arrival(key=f"{e.ref}@{e.lane.code}", dc=e.dc, sku=e.sku, qty=e.qty, date=e.eta,
                             week=e.arrive_week, kind="expedite", ref=e.ref, mode=e.lane.mode))
    for b in plan.alt:
        alt_lane = scn.inbound_lanes.get(b.source.lane_code)
        lines.append(Arrival(key=f"{b.source.factory_code}@{b.week}:{b.ref}", dc=b.dc, sku=b.sku, qty=b.qty,
                             date=b.eta, week=b.week, kind="alt", ref=b.ref,
                             mode=alt_lane.mode if alt_lane is not None else "default"))
    return lines
