"""Whole units for ``resource_alt_supplier`` and the expedite splits that compete with it for
the same PO lines (M8, ``docs/contracts/125-m8-optimizer-cpsat.md`` §3-§4).

**Sourcing terms in the repair** (both ``integer_stage`` modes; :func:`repair_terms`). When an
alternate source carries an M8 term (``order_multiple_units``, ``moq_basis="style"``,
``capacity_group``, ``max_orders``) or the lever sets ``max_buys_per_line``, the floored LP buys
are repaired in this order, each step only removing units except the last:

1. ``max_buys_per_line``: a PO line keeps its largest buys (ties: earliest week, then source);
2. order multiples: each (source, arrival week, SKU) total - one ``po.create`` line - is floored
   to a multiple ``m``, the remainder taken from the highest PO-line refs first;
3. ``max_orders``: a source keeps its largest purchase orders (ties: earliest week);
4. capacity groups: a group week over its capacity loses whole multiples from its last
   (source, SKU) blocks (the LP row normally prevents this);
5. MOQ per basis (the purchase order, or each style-colour within it) with the effective MOQ
   ``ceil(moq / m) * m``: a unit below it is topped up in multiples of ``m`` (headroom of its
   candidate lines, largest current buy first, within the source and group capacity, open
   quantities, ``max_buys_per_line`` and the budgets) or dropped, whichever has the higher
   recomputed weighted net benefit (ties: drop) - the M7 rule, per unit.

Without any M8 term the planner keeps the M7 repair (:func:`keelson_opt.planner._moq_repair`)
unchanged, so M7 responses stay byte-identical.

**CP-SAT stage** (``integer_stage="cpsat"``; :func:`solve_integer`). One integer model over every
phase-1 candidate (not only those the LP used): ``e[ref, lane] in [0, open - 1]``, ``n[source,
ref, week] in [0, min(open, weekly capacity)]``; per PO line at most one op - one split lane or
re-sourcing (``sum sel + r <= 1``, replacing the M7 "larger lever wins" rule) - and ``sum n <=
open``; expedite capacity per lane and booking week net of the bookings that fly; the lever
budgets; per purchase order (source, arrival week) a bool ``y`` with ``sum n <= capacity * y``
and the MOQ per basis (``>= moq_eff * y`` or per style ``y_st``); order multiples per
(source, week, SKU) ``sum n = m * k``; capacity-group rows; ``sum_w y <= max_orders``;
``max_buys_per_line`` through per-buy bools. The objective is the phase-1 LP objective on the
touched DC x SKU pairs, with the floored phase-1 transfers fixed, times
:data:`~keelson_opt.levers.composed.OBJ_SCALE` (exact integers): per pair and week ``inv[t] =
inv[t-1] + fixed supply + arrivals - removals at the line's week - served sales - served
buffer``, ``inv >= 0``; minimise ``4 * margin * w_lost * lost sales + margin * w_lost * lost
buffer + (4 * w_cost * premium + 400) * units``. All clusters of a DC draw from one pool at
one SKU margin, so aggregating the LP's cluster rows per DC is exact. The repair decisions
(plus a greedy recourse) are the complete hint, and its objective the acceptance bound.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from keelson_contracts.optimizer import IntegerFallbackReason, PlanWeights, ResourceAltSupplierParams
from ortools.sat.python import cp_model

from keelson_opt.cpsat import Budget, CpRun, objective_fits, run_cpsat, set_hints
from keelson_opt.levers import reallocate as _r
from keelson_opt.levers.composed import AltCand, ExpCand, LeverParams, LpOut, capacity_groups, unit_cost_scaled
from keelson_opt.plan import RLine, receipt_series
from keelson_opt.scn import Scenario

log = logging.getLogger(__name__)

INTEGER_DET_TIME = 0.15
"""Deterministic seconds for the integer stage (taken from the request's CP-SAT budget)."""
INTEGER_MAX_VARS = 60_000
"""Larger models fall back with ``model_too_large`` (the 512 MB container stays well clear)."""

AltKey = tuple[tuple[str, str, str], str, int]
ExpKey = tuple[str, str]
SourceKey = tuple[str, str, str]
Score = Callable[[dict[AltKey, int], set[tuple[str, str]], set[str]], int]
"""``(alt buys, DC x SKU keys, refs that fall back to an expedite split) -> weighted net``."""


# ───────────────────────────── repair with the M8 terms ─────────────────────────────


def has_terms(out: LpOut, params: ResourceAltSupplierParams | None) -> bool:
    """Whether any M8 sourcing term applies (else the M7 repair runs unchanged)."""
    if params is not None and params.max_buys_per_line is not None:
        return True
    return any(
        c.source.order_multiple_units != 1
        or c.source.moq_basis != "po"
        or c.source.capacity_group is not None
        or c.source.max_orders is not None
        for c in out.alt_cands.values()
    )


def within_budget(out: LpOut, alt_q: Mapping[AltKey, int], params: ResourceAltSupplierParams | None) -> bool:
    if params is None:
        return True
    units = sum(alt_q.values())
    premium = sum(q * out.alt_cands[k].premium for k, q in alt_q.items())
    return (params.max_units_total is None or units <= params.max_units_total) and (
        params.max_premium_cents is None or premium <= params.max_premium_cents
    )


def _moq_unit(c: AltCand, sku_style: Mapping[str, str]) -> tuple[SourceKey, int, str]:
    """The MOQ unit of a buy: its purchase order, or (``moq_basis="style"``) the style-colour
    within it (``""`` = the whole PO)."""
    return (c.source_key, c.week, sku_style.get(c.sku, c.sku) if c.source.moq_basis == "style" else "")


def repair_terms(
    scn: Scenario,
    out: LpOut,
    params: ResourceAltSupplierParams | None,
    exp: Mapping[str, int],
    alt_q: Mapping[AltKey, int],
    score: Score,
    *,
    expedite: bool,
) -> dict[AltKey, int]:
    """The floored buys ``alt_q`` repaired for the M8 sourcing terms (steps 1-5 of the module
    doc). ``exp`` = the PO lines kept on an expedite split (never topped up); ``score`` values a
    buy set on the given DC x SKU pairs (``expedite``: a dropped line is valued with the split
    the expedite lever would likely make instead, as in M7)."""
    cands = out.alt_cands
    q = {k: v for k, v in sorted(alt_q.items()) if v > 0}
    max_buys = params.max_buys_per_line if params is not None else None

    # 1. buys per PO line
    if max_buys is not None:
        by_ref: dict[str, list[AltKey]] = defaultdict(list)
        for k in q:
            by_ref[k[1]].append(k)
        for keys in by_ref.values():
            for k in sorted(keys, key=lambda k: (-q[k], k[2], k[0]))[max_buys:]:
                del q[k]

    # 2. order multiples per po.create line (source, week, SKU)
    _floor_multiples(q, cands)

    # 3. purchase orders per source
    by_src: dict[SourceKey, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for k, v in q.items():
        by_src[k[0]][k[2]] += v
    source_of = {c.source_key: c.source for c in cands.values()}
    for skey, weeks in sorted(by_src.items()):
        limit = source_of[skey].max_orders
        if limit is not None and len(weeks) > limit:
            keep = {w for w, _t in sorted(weeks.items(), key=lambda wt: (-wt[1], wt[0]))[:limit]}
            for k in [k for k in q if k[0] == skey and k[2] not in keep]:
                del q[k]

    # 4. capacity groups (safety net: the LP has a row per group and week)
    group_caps = capacity_groups(scn)
    by_gw: dict[tuple[str, int], list[AltKey]] = defaultdict(list)
    for k in q:
        group = cands[k].source.capacity_group
        if group is not None:
            by_gw[(group, k[2])].append(k)
    for (group, _w), keys in sorted(by_gw.items()):
        _trim_group(q, cands, keys, group_caps[group])

    # 5. MOQ per basis: top up in multiples or drop
    units: dict[tuple[SourceKey, int, str], list[AltKey]] = defaultdict(list)
    for k in q:
        units[_moq_unit(cands[k], scn.sku_style)].append(k)
    for unit, _keys in sorted(units.items()):
        keys = [k for k in q if _moq_unit(cands[k], scn.sku_style) == unit]
        if not keys:
            continue
        src = cands[keys[0]].source
        total = sum(q[k] for k in keys)
        moq = src.moq_effective
        if moq <= total:
            continue
        down = {k: v for k, v in q.items() if k not in keys}
        up = _top_up(scn, out, params, exp, q, unit, moq - total, group_caps, max_buys)
        if up is None:
            q = down
            continue
        changed = {k for k in set(up) | set(down) if up.get(k, 0) != down.get(k, 0)}
        touched = {(cands[k].dc, cands[k].sku) for k in changed}
        dropped = {k[1] for k in keys} - {k[1] for k in down}
        v_up = score(up, touched, set())
        v_down = score(down, touched, dropped if expedite else set())
        q = up if v_up > v_down else down
    return q


def _floor_multiples(q: dict[AltKey, int], cands: Mapping[AltKey, AltCand]) -> None:
    blocks: dict[tuple[SourceKey, int, str], list[AltKey]] = defaultdict(list)
    for k in q:
        blocks[(k[0], k[2], cands[k].sku)].append(k)
    for keys in blocks.values():
        m = cands[keys[0]].source.order_multiple_units
        rest = sum(q[k] for k in keys) % m
        _take(q, keys, rest)


def _take(q: dict[AltKey, int], keys: list[AltKey], units: int) -> None:
    """Remove ``units`` from ``keys``, highest PO-line ref first."""
    for k in sorted(keys, key=lambda k: k[1], reverse=True):
        if units <= 0:
            break
        take = min(units, q[k])
        q[k] -= take
        units -= take
        if q[k] == 0:
            del q[k]


def _trim_group(q: dict[AltKey, int], cands: Mapping[AltKey, AltCand], keys: list[AltKey], cap: int) -> None:
    excess = sum(q[k] for k in keys) - cap
    if excess <= 0:
        return
    blocks: dict[tuple[SourceKey, str], list[AltKey]] = defaultdict(list)
    for k in keys:
        blocks[(k[0], cands[k].sku)].append(k)
    for _block, bkeys in sorted(blocks.items(), reverse=True):
        if excess <= 0:
            break
        m = cands[bkeys[0]].source.order_multiple_units
        have = sum(q[k] for k in bkeys)
        cut = min(have, -(-excess // m) * m)
        _take(q, bkeys, cut)
        excess -= cut


def _top_up(
    scn: Scenario,
    out: LpOut,
    params: ResourceAltSupplierParams | None,
    exp: Mapping[str, int],
    q: Mapping[AltKey, int],
    unit: tuple[SourceKey, int, str],
    need: int,
    group_caps: Mapping[str, int],
    max_buys: int | None,
) -> dict[AltKey, int] | None:
    """``q`` with ``need`` more units in ``unit`` (whole multiples per SKU), or ``None``."""
    skey, week, _style = unit
    cands = out.alt_cands
    pool = [
        c
        for c in cands.values()
        if c.source_key == skey and c.week == week and _moq_unit(c, scn.sku_style) == unit and c.ref not in exp
    ]
    if not pool:
        return None
    src = pool[0].source
    m = src.order_multiple_units
    if src.moq_effective > src.weekly_capacity_units:
        return None
    up = dict(q)
    used: dict[str, int] = defaultdict(int)
    buys: dict[str, int] = defaultdict(int)
    for k, v in q.items():
        used[k[1]] += v
        buys[k[1]] += 1
    room = src.weekly_capacity_units - sum(v for k, v in q.items() if k[0] == skey and k[2] == week)
    group = src.capacity_group
    if group is not None:
        in_group = sum(v for k, v in q.items() if k[2] == week and cands[k].source.capacity_group == group)
        room = min(room, group_caps[group] - in_group)
    pool.sort(key=lambda c: (-q.get(c.key, 0), c.ref))
    skus = list(dict.fromkeys(c.sku for c in pool))
    for sku in skus:
        if need <= 0 or room < m:
            break
        heads: list[tuple[AltCand, int]] = []
        for c in pool:
            if c.sku != sku:
                continue
            if max_buys is not None and c.key not in q and buys[c.ref] >= max_buys:
                continue
            head = max(0, min(c.ub - up.get(c.key, 0), c.open_qty - used[c.ref]))
            if head:
                heads.append((c, head))
        add = min(sum(h for _c, h in heads), need, room) // m * m
        need -= add
        room -= add
        for c, head in heads:
            if not add:
                break
            give = min(head, add)
            up[c.key] = up.get(c.key, 0) + give
            used[c.ref] += give
            add -= give
    if need > 0 or not within_budget(out, up, params):
        return None
    return up


# ───────────────────────────── CP-SAT stage ─────────────────────────────


@dataclass(slots=True)
class IntegerOutcome:
    """The CP-SAT stage's result: ``alt`` / ``exp`` whole units when ``fallback is None``."""

    fallback: IntegerFallbackReason | None
    run: CpRun | None = None
    alt: dict[AltKey, int] = field(default_factory=dict)
    exp: dict[ExpKey, int] = field(default_factory=dict)
    hint_objective: int | None = None
    det_limit: float | None = None
    size: tuple[int, int] = (0, 0)


def solve_integer(
    scn: Scenario,
    out: LpOut,
    lines: list[RLine],
    levers: LeverParams,
    weights: PlanWeights,
    *,
    repair_alt: Mapping[AltKey, int],
    repair_exp: Mapping[ExpKey, int],
    load: Mapping[tuple[str, int], int],
    seed: int,
    time_limit_s: int,
    budget: Budget,
) -> IntegerOutcome:
    """Whole-unit buys and splits by CP-SAT (module doc); the caller decides whether to use
    them (simulator guard). Only the splits of PO lines that can also be re-sourced are decided
    here; the repair's other splits stay as they are (their arrivals, lane capacity and budget
    use enter the model as fixed) and are returned with the CP-SAT units."""
    alt_cands = [out.alt_cands[k] for k in sorted(out.alt_cands)]
    if not alt_cands:
        return IntegerOutcome("no_candidates")
    alt_refs = {a.ref for a in alt_cands}
    exp_cands = [c for k in sorted(out.exp_cands) if (c := out.exp_cands[k]).ref in alt_refs]
    kept_exp = {k: q for k, q in sorted(repair_exp.items()) if k[0] not in alt_refs}
    horizon = scn.horizon
    pairs = sorted({(c.dc, c.sku) for c in exp_cands} | {(a.dc, a.sku) for a in alt_cands})
    estimate = 3 * len(alt_cands) + 2 * len(exp_cands) + 3 * len(pairs) * horizon
    if estimate > INTEGER_MAX_VARS:
        return IntegerOutcome("model_too_large", size=(estimate, 0))
    det = budget.take(INTEGER_DET_TIME)
    if det is None:
        return IntegerOutcome("budget_exhausted")

    m = _IntegerModel(scn, out, lines, levers, weights, alt_cands, exp_cands, pairs, load, kept_exp)
    if not objective_fits(m.obj_terms(), m.offset):
        return IntegerOutcome("objective_overflow", size=m.size, det_limit=det)
    hint_obj = m.hint(repair_alt, repair_exp)
    run = run_cpsat(m.model, seed=seed, det_limit=det, time_limit_s=time_limit_s)
    budget.spend(run.det_time_s)
    outcome = IntegerOutcome(None, run=run, hint_objective=hint_obj, det_limit=det, size=m.size)
    if (reason := run.fallback) is not None:
        outcome.fallback = reason
        return outcome
    outcome.alt = {a.key: v for a in alt_cands if (v := run.value(m.n[a.key])) > 0}
    exp = dict(kept_exp)
    exp.update({c.key: v for c in exp_cands if (v := run.value(m.e[c.key])) > 0})
    outcome.exp = dict(sorted(exp.items()))
    return outcome


class _IntegerModel:
    """The CP-SAT model of :func:`solve_integer` (built in sorted order)."""

    def __init__(
        self,
        scn: Scenario,
        out: LpOut,
        lines: list[RLine],
        levers: LeverParams,
        weights: PlanWeights,
        alt_cands: list[AltCand],
        exp_cands: list[ExpCand],
        pairs: list[tuple[str, str]],
        load: Mapping[tuple[str, int], int],
        kept_exp: Mapping[ExpKey, int],
    ) -> None:
        self.model = model = cp_model.CpModel()
        self.alt_cands, self.exp_cands = alt_cands, exp_cands
        self.style_of = scn.sku_style
        horizon = scn.horizon
        w_cost = weights.transfer_cost
        self._obj: list[tuple[int, cp_model.IntVar, int, int]] = []
        """(coef, var, lb, ub) of every objective term."""

        # ── decisions ──
        self.e: dict[ExpKey, cp_model.IntVar] = {}
        self.sel: dict[ExpKey, cp_model.IntVar] = {}
        for c in exp_cands:
            v = model.new_int_var(0, c.ub, "")
            s = model.new_bool_var("")
            model.add(v <= c.ub * s)
            self.e[c.key], self.sel[c.key] = v, s
            self._obj.append((unit_cost_scaled(w_cost, c.premium), v, 0, c.ub))
        self.n: dict[AltKey, cp_model.IntVar] = {}
        for a in alt_cands:
            v = model.new_int_var(0, a.ub, "")
            self.n[a.key] = v
            self._obj.append((unit_cost_scaled(w_cost, a.premium), v, 0, a.ub))

        # ── one op per PO line, open quantities ──
        alt_by_ref: dict[str, list[AltCand]] = defaultdict(list)
        for a in alt_cands:
            alt_by_ref[a.ref].append(a)
        exp_by_ref: dict[str, list[ExpCand]] = defaultdict(list)
        for c in exp_cands:
            exp_by_ref[c.ref].append(c)
        self.r: dict[str, cp_model.IntVar] = {}
        for ref in sorted(set(alt_by_ref) | set(exp_by_ref)):
            ops = [self.sel[c.key] for c in exp_by_ref.get(ref, [])]
            if ref in alt_by_ref:
                r = self.r[ref] = model.new_bool_var("")
                for a in alt_by_ref[ref]:
                    model.add(self.n[a.key] <= a.ub * r)
                model.add(sum(self.n[a.key] for a in alt_by_ref[ref]) <= alt_by_ref[ref][0].open_qty)
                ops.append(r)
            if len(ops) > 1:
                model.add(sum(ops) <= 1)

        # ── expedite lane capacity and budgets (net of the splits kept from the repair) ──
        kept = [(out.exp_cands[k], q) for k, q in sorted(kept_exp.items())]
        kept_load: dict[tuple[str, int], int] = defaultdict(int)
        for c, q in kept:
            kept_load[(c.lane.code, c.book_week)] += q
        by_book: dict[tuple[str, int], list[ExpCand]] = defaultdict(list)
        for c in exp_cands:
            by_book[(c.lane.code, c.book_week)].append(c)
        for (code, bw), cs in sorted(by_book.items()):
            cap = max(0, cs[0].lane.cap_in_week(bw) - load.get((code, bw), 0) - kept_load[(code, bw)])
            model.add(sum(self.e[c.key] for c in cs) <= cap)
        if levers.expedite is not None and exp_cands:
            if levers.expedite.max_units_total is not None:
                room = max(0, levers.expedite.max_units_total - sum(q for _c, q in kept))
                model.add(sum(self.e.values()) <= room)
            if levers.expedite.max_premium_cents is not None:
                room = max(0, levers.expedite.max_premium_cents - sum(c.premium * q for c, q in kept))
                model.add(sum(c.premium * self.e[c.key] for c in exp_cands) <= room)

        # ── purchase orders: capacity, MOQ per basis, multiples, groups, max orders, buys per line ──
        by_po: dict[tuple[SourceKey, int], list[AltCand]] = defaultdict(list)
        for a in alt_cands:
            by_po[(a.source_key, a.week)].append(a)
        self.y: dict[tuple[SourceKey, int], cp_model.IntVar] = {}
        self.y_st: dict[tuple[SourceKey, int, str], cp_model.IntVar] = {}
        self.k: dict[tuple[SourceKey, int, str], cp_model.IntVar] = {}
        self.k_mult: dict[tuple[SourceKey, int, str], int] = {}
        for (skey, w), cands in sorted(by_po.items()):
            src = cands[0].source
            y = self.y[(skey, w)] = model.new_bool_var("")
            total = sum(self.n[a.key] for a in cands)
            model.add(total <= src.weekly_capacity_units * y)
            moq = src.moq_effective
            if src.moq_basis == "style":
                styles: dict[str, list[AltCand]] = defaultdict(list)
                for a in cands:
                    styles[scn.sku_style.get(a.sku, a.sku)].append(a)
                for style, sc in sorted(styles.items()):
                    yst = model.new_bool_var("")
                    self.y_st[(skey, w, style)] = yst
                    model.add(yst <= y)
                    for a in sc:
                        model.add(self.n[a.key] <= a.ub * yst)
                    if moq > 0:
                        model.add(sum(self.n[a.key] for a in sc) >= moq * yst)
            elif moq > 0:
                model.add(total >= moq * y)
            mult = src.order_multiple_units
            if mult > 1:
                skus: dict[str, list[AltCand]] = defaultdict(list)
                for a in cands:
                    skus[a.sku].append(a)
                for sku, sc in sorted(skus.items()):
                    kv = self.k[(skey, w, sku)] = model.new_int_var(0, src.weekly_capacity_units // mult, "")
                    self.k_mult[(skey, w, sku)] = mult
                    model.add(sum(self.n[a.key] for a in sc) == mult * kv)
        group_caps = capacity_groups(scn)
        by_gw: dict[tuple[str, int], list[AltCand]] = defaultdict(list)
        for a in alt_cands:
            if a.source.capacity_group is not None:
                by_gw[(a.source.capacity_group, a.week)].append(a)
        for (group, _w), cands in sorted(by_gw.items()):
            model.add(sum(self.n[a.key] for a in cands) <= group_caps[group])
        weeks_of: dict[SourceKey, list[tuple[SourceKey, int]]] = defaultdict(list)
        for po in sorted(self.y):
            weeks_of[po[0]].append(po)
        source_of = {a.source_key: a.source for a in alt_cands}
        for skey, pos in sorted(weeks_of.items()):
            limit = source_of[skey].max_orders
            if limit is not None and len(pos) > limit:
                model.add(sum(self.y[po] for po in pos) <= limit)
        params = levers.alt
        self.z: dict[AltKey, cp_model.IntVar] = {}
        if params is not None and params.max_buys_per_line is not None:
            for ref, cands in sorted(alt_by_ref.items()):
                if len(cands) > params.max_buys_per_line:
                    for a in cands:
                        z = self.z[a.key] = model.new_bool_var("")
                        model.add(self.n[a.key] <= a.ub * z)
                    model.add(sum(self.z[a.key] for a in cands) <= params.max_buys_per_line)
        if params is not None:
            if params.max_units_total is not None:
                model.add(sum(self.n.values()) <= params.max_units_total)
            if params.max_premium_cents is not None:
                model.add(sum(a.premium * self.n[a.key] for a in alt_cands) <= params.max_premium_cents)

        # ── recourse: the LP's DC balance on the touched pairs, transfers fixed ──
        pair_set = set(pairs)
        self.sales: dict[tuple[str, str], list[int]] = {p: [0] * horizon for p in pairs}
        self.buffer: dict[tuple[str, str], list[int]] = {p: [0] * horizon for p in pairs}
        for (cl, sku), (sales, buffer) in sorted(_r._requirements(scn).items()):
            p = (scn.cluster_dc[cl], sku)
            if p in pair_set:
                for t in range(horizon):
                    self.sales[p][t] += sales[t]
                    self.buffer[p][t] += buffer[t]
        receipts = receipt_series(lines, horizon)
        self.fixed: dict[tuple[str, str], list[int]] = {}
        for p in pairs:
            f = list(receipts.get(p, [0] * horizon))
            f[0] += scn.dc_supply.get(p, 0)
            self.fixed[p] = f
        for tr in out.transfers:
            src_p, dst_p = (tr.lane.from_dc, tr.sku), (tr.lane.to_dc, tr.sku)
            if src_p in pair_set:
                self.fixed[src_p][0] -= tr.qty
            if dst_p in pair_set and tr.arrive_week < horizon:
                self.fixed[dst_p][tr.arrive_week] += tr.qty
        for c, q in kept:
            p = (c.dc, c.sku)
            if p in pair_set:
                self.fixed[p][c.arrive_week] += q
                if c.from_week is not None:
                    self.fixed[p][c.from_week] -= q
        self.plus: dict[tuple[str, str, int], list[tuple[cp_model.IntVar, int]]] = defaultdict(list)
        self.minus: dict[tuple[str, str, int], list[tuple[cp_model.IntVar, int]]] = defaultdict(list)
        for c in exp_cands:
            self.plus[(c.dc, c.sku, c.arrive_week)].append((self.e[c.key], c.ub))
            if c.from_week is not None:
                self.minus[(c.dc, c.sku, c.from_week)].append((self.e[c.key], c.ub))
        for a in alt_cands:
            self.plus[(a.dc, a.sku, a.week)].append((self.n[a.key], a.ub))
            if a.from_week is not None:
                self.minus[(a.dc, a.sku, a.from_week)].append((self.n[a.key], a.ub))
        self.inv: dict[tuple[str, str], list[cp_model.IntVar]] = {}
        self.ss: dict[tuple[str, str], list[cp_model.IntVar | None]] = {}
        self.sb: dict[tuple[str, str], list[cp_model.IntVar | None]] = {}
        self.offset = 0
        for p in pairs:
            dc, sku = p
            mw = (scn.price_cents[sku] - scn.cost_cents[sku]) * weights.lost_margin
            inflow = sum(max(0, f) for f in self.fixed[p]) + sum(
                ub for t in range(horizon) for _v, ub in self.plus.get((dc, sku, t), ())
            )
            invs: list[cp_model.IntVar] = []
            sss: list[cp_model.IntVar | None] = []
            sbs: list[cp_model.IntVar | None] = []
            prev: cp_model.IntVar | None = None
            for t in range(horizon):
                inv = model.new_int_var(0, inflow, "")
                ss = model.new_int_var(0, self.sales[p][t], "") if self.sales[p][t] else None
                sb = model.new_int_var(0, self.buffer[p][t], "") if self.buffer[p][t] else None
                expr = inv - (prev if prev is not None else 0)
                expr -= sum(v for v, _ub in self.plus.get((dc, sku, t), ()))
                expr += sum(v for v, _ub in self.minus.get((dc, sku, t), ()))
                if ss is not None:
                    expr += ss
                    self._obj.append((-4 * mw, ss, 0, self.sales[p][t]))
                    self.offset += 4 * mw * self.sales[p][t]
                if sb is not None:
                    expr += sb
                    self._obj.append((-mw, sb, 0, self.buffer[p][t]))
                    self.offset += mw * self.buffer[p][t]
                model.add(expr == self.fixed[p][t])
                invs.append(inv)
                sss.append(ss)
                sbs.append(sb)
                prev = inv
            self.inv[p], self.ss[p], self.sb[p] = invs, sss, sbs
        model.minimize(sum(coef * v for coef, v, _lb, _ub in self._obj) + self.offset)

    @property
    def size(self) -> tuple[int, int]:
        proto = self.model.proto
        return len(proto.variables), len(proto.constraints)

    def obj_terms(self) -> list[tuple[int, int, int]]:
        return [(coef, lb, ub) for coef, _v, lb, ub in self._obj]

    def hint(self, alt: Mapping[AltKey, int], exp: Mapping[ExpKey, int]) -> int | None:
        """Hint every variable with the repair decisions and a greedy recourse (sell sales, then
        buffer, every week); returns the hint's objective, ``None`` when its recourse would go
        below zero stock (the hint is then incomplete and gives no bound)."""
        values: dict[int, int] = {}

        def put(var: cp_model.IntVar, value: int) -> None:
            values[var.index] = value

        ok = True
        for c in self.exp_cands:
            q = exp.get(c.key, 0)
            put(self.e[c.key], q)
            put(self.sel[c.key], int(q > 0))
        ref_total: dict[str, int] = defaultdict(int)
        po_total: dict[tuple[SourceKey, int], int] = defaultdict(int)
        style_total: dict[tuple[SourceKey, int, str], int] = defaultdict(int)
        sku_total: dict[tuple[SourceKey, int, str], int] = defaultdict(int)
        for a in self.alt_cands:
            q = alt.get(a.key, 0)
            put(self.n[a.key], q)
            ref_total[a.ref] += q
            po_total[(a.source_key, a.week)] += q
            sku_total[(a.source_key, a.week, a.sku)] += q
            if a.source.moq_basis == "style":
                style_total[(a.source_key, a.week, self.style_of.get(a.sku, a.sku))] += q
        for ref, r in self.r.items():
            put(r, int(ref_total[ref] > 0))
        for po, y in self.y.items():
            put(y, int(po_total[po] > 0))
        for key, yst in self.y_st.items():
            put(yst, int(style_total[key] > 0))
        for key, kv in self.k.items():
            mult = self.k_mult[key]
            if sku_total[key] % mult:
                ok = False
            put(kv, sku_total[key] // mult)
        for akey, z in self.z.items():
            put(z, int(alt.get(akey, 0) > 0))
        for p in sorted(self.inv):
            dc, sku = p
            prev = 0
            for t, inv in enumerate(self.inv[p]):
                avail = prev + self.fixed[p][t]
                avail += sum(values[v.index] for v, _ub in self.plus.get((dc, sku, t), ()))
                avail -= sum(values[v.index] for v, _ub in self.minus.get((dc, sku, t), ()))
                if avail < 0:
                    ok = False
                avail = max(0, avail)
                ss, sb = self.ss[p][t], self.sb[p][t]
                take_s = min(avail, self.sales[p][t])
                avail -= take_s
                take_b = min(avail, self.buffer[p][t])
                avail -= take_b
                if ss is not None:
                    put(ss, take_s)
                if sb is not None:
                    put(sb, take_b)
                put(inv, avail)
                prev = avail
        set_hints(self.model, values)
        if not ok:
            return None
        return sum(coef * values[v.index] for coef, v, _lb, _ub in self._obj) + self.offset


__all__ = [
    "INTEGER_DET_TIME",
    "INTEGER_MAX_VARS",
    "AltKey",
    "ExpKey",
    "IntegerOutcome",
    "has_terms",
    "repair_terms",
    "solve_integer",
    "within_budget",
]
