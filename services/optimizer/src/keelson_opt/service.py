"""Use-case layer for ``/v1/impact`` and ``/v1/optimize``.

Both are pure functions of the request apart from ``provenance.computed_at`` (impact) and
``solver.solve_ms`` (optimize).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from importlib.metadata import version
from typing import Any

from keelson_contracts.changeset import MAX_PO_CREATE_LINES
from keelson_contracts.optimizer import (
    AllocationDecision,
    AltSupplierDecision,
    CostBreakdown,
    Decision,
    EmissionsSummary,
    EtaChange,
    ExpediteDecision,
    Explain,
    ImpactKpis,
    ImpactRequest,
    ImpactResponse,
    KpiDelta,
    LeverDecision,
    OptimizeRequest,
    Provenance,
    ResequenceDecision,
    RobustnessResult,
    Scorecard,
    SolverInfo,
    TransferOrderCreatePayload,
    TransferOrderLine,
)

from keelson_opt import ENGINE_NAME, __version__
from keelson_opt.canonical import model_hash, sha256_hex
from keelson_opt.emissions import plan_emissions
from keelson_opt.graph import build_graph
from keelson_opt.impact import SimResult, Transfer, by_dc, kpis, simulate, timeline
from keelson_opt.levers import REALLOCATE_INVENTORY
from keelson_opt.plan import Plan, base_lines, plan_lines, receipt_series
from keelson_opt.planner import make_plan
from keelson_opt.robust import evaluate as evaluate_robustness
from keelson_opt.scn import Scenario, build_scenario
from keelson_opt.tts import network_tts, tts_by_dc, tts_report
from keelson_opt.wire_m5 import CANCEL_OP, ActionM5, OptimizeResponseM5

log = logging.getLogger(__name__)

ORTOOLS_VERSION = version("ortools")
PLACEHOLDER_VERSION = 1
"""``expected_version`` used for PO-line ops when ``ProblemData.po_lines`` carry no version."""


class PlanSpecError(ValueError):
    """The plan spec is well-formed but not executable (mapped to HTTP 422)."""


def run_impact(req: ImpactRequest) -> ImpactResponse:
    inputs_hash = model_hash(req)
    baseline = build_scenario(req.data, ())
    scn = build_scenario(req.data, req.perturbations)
    res = simulate(scn)
    warnings = list(scn.warnings)
    graph = None
    tts: list = []  # type: ignore[type-arg]
    bottlenecks: list = []  # type: ignore[type-arg]
    if req.include_graph:
        graph, gw = build_graph(scn, res)
        warnings.extend(gw)
        tts, bottlenecks = tts_report(scn, base_lines(scn))
    return ImpactResponse(
        kpis=kpis(res),
        by_dc=by_dc(res),
        timeline=timeline(res),
        baseline_kpis=kpis(simulate(baseline)),
        provenance=Provenance(
            engine=ENGINE_NAME,
            version=__version__,
            inputs_hash=inputs_hash,
            computed_at=dt.datetime.now(dt.UTC).replace(microsecond=0),
        ),
        warnings=warnings,
        graph=graph,
        tts=tts,
        bottlenecks=bottlenecks,
    )


def run_optimize(req: OptimizeRequest) -> OptimizeResponseM5:
    """``req`` may be the shared model or :class:`keelson_opt.wire_m5.OptimizeRequestM5`
    (with ``data.capacity_bookings``); the response model is the shared one plus the M5
    ``capacity_booking.cancel`` op. M8: ``emissions`` / ``robustness`` add CO₂e and bands to
    the scorecard (:func:`m8_results`)."""
    dumped = req.model_dump(mode="json")
    inputs_hash = sha256_hex(dumped)
    spec = req.plan_spec
    codes = [lv.code for lv in spec.levers]
    if len(codes) != len(set(codes)):
        raise PlanSpecError("each lever may appear at most once in a plan spec")

    scn = build_scenario(req.data, req.perturbations)
    warnings = list(scn.warnings)
    res_before = simulate(scn)
    before = kpis(res_before)

    outcome = make_plan(scn, spec, req.solver, before.margin_at_risk_cents)
    warnings.extend(outcome.warnings)
    plan = outcome.plan
    res_after = _simulate_plan(scn, plan)
    after = kpis(res_after) if not plan.empty else before
    actions, action_warnings = build_plan_actions(scn, plan)
    warnings.extend(action_warnings)
    margin_recovered = before.margin_at_risk_cents - after.margin_at_risk_cents
    incremental = plan.incremental_cost
    n_transfers = sum(1 for a in actions if a.op == "transfer_order.create")
    card, emissions, robustness = m8_results(
        req, dumped, scn, plan,
        scorecard(scn, plan, res_before, res_after, before, after, len(actions), seed=req.solver.seed),
    )
    resp = OptimizeResponseM5(
        status=outcome.status,
        plan_spec_id=spec.id,
        kpis_before=before,
        kpis_after=after,
        kpi_delta=KpiDelta(
            units_recovered=before.units_at_risk - after.units_at_risk,
            margin_recovered_cents=margin_recovered,
            transfer_cost_cents=plan.transfer_cost,
            net_benefit_cents=margin_recovered - incremental,
            transfers=n_transfers,
        ),
        decisions=build_decisions(scn, plan),
        actions=actions,
        explain=Explain(binding_constraints=outcome.binding),
        solver=SolverInfo(
            name=outcome.solver_name,
            ortools=ORTOOLS_VERSION,
            params=outcome.params,
            seed=req.solver.seed,
            inputs_hash=inputs_hash,
            solve_ms=outcome.solve_ms,
            status_detail=outcome.status_detail,
        ),
        warnings=warnings,
        scorecard=card,
        emissions=emissions,
        robustness=robustness,
    )
    log.info(
        "optimize done",
        extra={
            "plan_spec_id": spec.id,
            "status": outcome.status,
            "inputs_hash": inputs_hash,
            "solve_ms": outcome.solve_ms,
            "actions": len(actions),
            "units_recovered": resp.kpi_delta.units_recovered,
        },
    )
    return resp


def m8_results(
    req: OptimizeRequest, dumped: dict[str, Any], scn: Scenario, plan: Plan, card: Scorecard
) -> tuple[Scorecard, EmissionsSummary | None, RobustnessResult | None]:
    """M8 (``126``): the scorecard with ``incremental_co2e_kg`` / ``co2e_breakdown`` (request
    ``emissions``) and ``bands`` (request ``robustness``), plus the response summaries. Without
    either block the scorecard is returned unchanged."""
    update: dict[str, Any] = {}
    emissions = robustness = None
    if req.emissions is not None:
        co2 = plan_emissions(scn, plan, req.emissions)
        update["incremental_co2e_kg"] = co2.incremental_kg
        update["co2e_breakdown"] = co2.breakdown.model_dump(mode="json")
        emissions = co2.summary
    if req.robustness is not None:
        crn_key = sha256_hex({k: dumped.get(k) for k in ("data", "perturbations", "robustness")})
        rob = evaluate_robustness(
            scn, plan, req.robustness, req.perturbations, incremental_cost=plan.incremental_cost, crn_key=crn_key
        )
        update["bands"] = rob.bands.model_dump(mode="json")
        robustness = rob.result
    if update:
        card = Scorecard.model_validate({**card.model_dump(mode="json"), **update})
    return card, emissions, robustness


def _simulate_plan(scn: Scenario, plan: Plan) -> SimResult:
    if plan.empty:
        return simulate(scn)
    lines = plan_lines(scn, plan)
    return simulate(scn, plan.transfers, receipts=receipt_series(lines, scn.horizon), overrides=plan.overrides)


# ───────────────────────────── scorecard ─────────────────────────────


def _served_and_demand(res: SimResult) -> tuple[int, int]:
    demand = lost = 0
    for cs in res.series.values():
        demand += sum(cs.demand)
        lost += sum(cs.lost)
    return demand - lost, demand


def _earliest_breach(k: ImpactKpis) -> dt.date | None:
    return min((fb.date for fb in k.first_breach), default=None)


def scorecard(
    scn: Scenario,
    plan: Plan,
    res_before: SimResult,
    res_after: SimResult,
    before: ImpactKpis,
    after: ImpactKpis,
    changes: int,
    *,
    seed: int,
) -> Scorecard:
    """Plan KPIs, all recomputed from the rounded plan:

    ``fill_rate_bp`` = served / demand (floor, 10000 without demand); ``shortage_days`` = 7 x
    short cluster-weeks; ``first_breach_shift_days`` = days the earliest first breach moves
    later (a breach that disappears counts as moved to the day after the horizon);
    ``tts_gain_days`` = change of the weakest DC's time-to-survive (on-time supply only)."""
    served, demand = _served_and_demand(res_after)
    fill = served * 10000 // demand if demand else 10000
    horizon_end = scn.week_mondays[-1] + dt.timedelta(days=7)
    b0, b1 = _earliest_breach(before), _earliest_breach(after)
    shift = ((b1 or horizon_end) - (b0 or horizon_end)).days
    tts_before = network_tts(tts_by_dc(scn, base_lines(scn), seed=seed), scn)
    if plan.empty:
        tts_after = tts_before
    else:
        tts_after = network_tts(tts_by_dc(scn, plan_lines(scn, plan), plan.transfers, seed=seed), scn)
    margin_recovered = before.margin_at_risk_cents - after.margin_at_risk_cents
    return Scorecard(
        fill_rate_bp=fill,
        lost_units=after.units_at_risk,
        lost_margin_cents=after.margin_at_risk_cents,
        incremental_cost_cents=plan.incremental_cost,
        cost_breakdown=CostBreakdown(
            transfer=plan.transfer_cost,
            expedite_premium=plan.expedite_premium,
            nearshore_premium=plan.nearshore_premium,
        ),
        net_benefit_cents=margin_recovered - plan.incremental_cost,
        shortage_days=7 * after.shortage_cluster_weeks,
        first_breach_shift_days=shift,
        tts_gain_days=tts_after - tts_before,
        changes=changes,
    )


# ───────────────────────────── decisions ─────────────────────────────


def build_decisions(scn: Scenario, plan: Plan) -> list[LeverDecision]:
    out: list[LeverDecision] = [
        Decision(
            lever=REALLOCATE_INVENTORY,
            from_dc=tr.lane.from_dc,
            to_dc=tr.lane.to_dc,
            sku=tr.sku,
            qty=tr.qty,
            ship_week=scn.weeks[tr.ship_week],
            arrive_week=scn.weeks[tr.arrive_week],
        )
        for tr in plan.transfers
    ]
    for e in sorted(plan.expedites, key=lambda e: (e.ref, e.lane.code)):
        out.append(
            ExpediteDecision(
                lever="expedite_mode_shift",
                po_line_ref=e.ref,
                sku=e.sku,
                dest_dc=e.dc,
                lane_code=e.lane.code,
                mode=e.lane.mode,  # type: ignore[arg-type]
                qty=e.qty,
                book_week=e.book_week,
                arrive_week=scn.weeks[e.arrive_week],
                premium_cents=e.premium_cents,
            )
        )
    for a in sorted(plan.alt, key=lambda a: (a.source_key, a.week, a.ref)):
        out.append(
            AltSupplierDecision(
                lever="resource_alt_supplier",
                supplier_code=a.source.supplier_code,
                factory_code=a.source.factory_code,
                dest_dc=a.dc,
                sku=a.sku,
                qty=a.qty,
                eta=a.eta,
                replaces_po_line_ref=a.ref,
                premium_cents=a.premium_cents,
            )
        )
    for c in plan.alloc_changes:
        al = c.allocation
        out.append(
            AllocationDecision(
                lever="prioritize_allocation",
                allocation_code=al.code,
                sku=al.sku,
                dc_code=al.dc_code,
                cluster_code=al.cluster_code,
                ship_week=al.ship_week,
                qty_before=al.qty,
                qty_after=c.qty_after,
            )
        )
    for seq in plan.sequences:
        out.append(
            ResequenceDecision(
                lever="resequence_production",
                line_code=seq.line_code,
                factory_code=seq.factory_code,
                order_codes_before=list(seq.before),
                order_codes_after=list(seq.after),
                eta_changes=[
                    EtaChange(po_line_ref=ref, eta_before=m.eta_before, eta_after=m.eta_after)
                    for ref in seq.refs
                    if (m := plan.moves[ref]).eta_after != m.eta_before
                ],
            )
        )
    return out


# ───────────────────────────── actions ─────────────────────────────


def build_actions(scn: Scenario, transfers: list[Transfer]) -> list[ActionM5]:
    """One ``transfer_order.create`` per (ship_week, from_dc, to_dc) with lines sorted by
    SKU; ``seq`` counts from 1 in that order; ``ship_date`` = Monday of the ship week."""
    groups: dict[tuple[int, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for tr in transfers:
        groups[(tr.ship_week, tr.lane.from_dc, tr.lane.to_dc)][tr.sku] += tr.qty
    actions: list[ActionM5] = []
    for seq, (t, src, dst) in enumerate(sorted(groups), start=1):
        lines = groups[(t, src, dst)]
        actions.append(
            ActionM5(
                seq=seq,
                op="transfer_order.create",
                target_ref=None,
                expected_version=None,
                payload=TransferOrderCreatePayload(
                    from_dc=src,
                    to_dc=dst,
                    ship_date=scn.week_mondays[t],
                    lines=[TransferOrderLine(sku=sku, qty=qty) for sku, qty in sorted(lines.items())],
                ),
            )
        )
    return actions


class _Uf:
    def __init__(self) -> None:
        self.parent: dict[object, object] = {}

    def find(self, x: object) -> object:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: object, b: object) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb, key=repr)] = min(ra, rb, key=repr)


def build_plan_actions(scn: Scenario, plan: Plan) -> tuple[list[ActionM5], list[str]]:
    """All change-set actions in a deterministic order: transfers (M1 order), cancelled
    capacity bookings (by code; M5), expedite splits (by PO line), alternate-source groups,
    allocation changes (by code), resequence groups (by line). Atomic groups (``po.create``
    + the ``po_line.change_qty`` / ``po_line.cancel`` it compensates;
    ``production_order.resequence`` + its ``po_line.reschedule``;
    ``capacity_booking.cancel`` + the ``po_line.reschedule`` of its lines back to their sea
    arrival) share a ``group`` id and are contiguous. Only changes are emitted: bookings that
    keep flying, and the PO lines they carry, produce no action (delta change set)."""
    actions = build_actions(scn, plan.transfers)
    rows: list[dict[str, object]] = []
    placeholder: set[str] = set()
    warnings: list[str] = []

    def version_of(ref: str) -> int:
        pol = scn.po_lines.get(ref)
        if pol is not None and pol.version is not None:
            return pol.version
        placeholder.add(ref)
        return PLACEHOLDER_VERSION

    booking_placeholder: list[str] = []
    n_rebook = 0
    for code in plan.rebook.cancelled if plan.rebook is not None else ():
        b = scn.bookings[code]
        back = [
            (ln.ref, ln.revert_eta)
            for ln in b.lines
            if ln.revert_eta is not None and ln.revert_eta != scn.po_lines[ln.ref].eta
        ]
        stranded = [ln.ref for ln in b.lines if ln.revert_eta is None]
        if stranded:
            warnings.append(
                f"capacity_bookings: {code} is cancelled but {len(stranded)} of its PO line(s) have no sea route "
                f"(e.g. {stranded[0]}); they are counted as not arriving and need a new booking"
            )
        group: str | None = None
        if back:
            n_rebook += 1
            group = f"rebook-{n_rebook}"
        if b.version is None:
            booking_placeholder.append(code)
        rows.append(
            {
                "op": CANCEL_OP,
                "target_ref": code,
                "expected_version": b.version if b.version is not None else PLACEHOLDER_VERSION,
                "payload": {"booking_code": code},
                "group": group,
            }
        )
        for ref, eta in back:
            rows.append(
                {
                    "op": "po_line.reschedule",
                    "target_ref": ref,
                    "expected_version": version_of(ref),
                    "payload": {"eta_dc": eta.isoformat()},  # type: ignore[union-attr]
                    "group": group,
                }
            )

    for e in sorted(plan.expedites, key=lambda e: (e.ref, e.lane.code)):
        rows.append(
            {
                "op": "po_line.split",
                "target_ref": e.ref,
                "expected_version": version_of(e.ref),
                "payload": {"mode": e.lane.mode, "qty": e.qty, "lane_code": e.lane.code},
            }
        )

    # alternate-source purchase orders, grouped with the PO lines they reduce
    pos: dict[tuple[tuple[str, str, str], int], list] = defaultdict(list)  # type: ignore[type-arg]
    for a in plan.alt:
        pos[(a.source_key, a.week)].append(a)
    uf = _Uf()
    for key, buys in pos.items():
        for b in buys:
            uf.union(("po", key), ("ref", b.ref))
    comps: dict[object, list[tuple[tuple[str, str, str], int]]] = defaultdict(list)
    for key in sorted(pos):
        comps[uf.find(("po", key))].append(key)
    cut: dict[str, int] = defaultdict(int)
    for a in plan.alt:
        cut[a.ref] += a.qty
    for n, root in enumerate(sorted(comps, key=lambda r: comps[r][0]), start=1):
        group = f"alt-{n}"
        refs: set[str] = set()
        for key in comps[root]:
            buys = pos[key]
            first = buys[0]
            qty_by_sku: dict[str, int] = defaultdict(int)
            for b in buys:
                qty_by_sku[b.sku] += b.qty
                refs.add(b.ref)
            items = sorted(qty_by_sku.items())
            for i in range(0, len(items), MAX_PO_CREATE_LINES):
                rows.append(
                    {
                        "op": "po.create",
                        "target_ref": None,
                        "expected_version": None,
                        "payload": {
                            "supplier_code": first.source.supplier_code,
                            "factory_code": first.source.factory_code,
                            "dest_dc": first.dc,
                            "eta_dc": min(b.eta for b in buys).isoformat(),
                            "lines": [{"sku": s, "qty": q} for s, q in items[i : i + MAX_PO_CREATE_LINES]],
                        },
                        "group": group,
                    }
                )
        for ref in sorted(refs):
            remaining = scn.po_lines[ref].qty - cut[ref]
            if remaining <= 0:
                rows.append(
                    {
                        "op": "po_line.cancel",
                        "target_ref": ref,
                        "expected_version": version_of(ref),
                        "payload": {},
                        "group": group,
                    }
                )
            else:
                rows.append(
                    {
                        "op": "po_line.change_qty",
                        "target_ref": ref,
                        "expected_version": version_of(ref),
                        "payload": {"qty": remaining},
                        "group": group,
                    }
                )

    for c in plan.alloc_changes:
        rows.append(
            {
                "op": "allocation.set_qty",
                "target_ref": c.allocation.code,
                "expected_version": c.allocation.version,
                "payload": {"qty": c.qty_after},
            }
        )

    # resequences (lines sharing a rescheduled PO line form one group)
    uf2 = _Uf()
    owner: dict[str, str] = {}
    for seq in plan.sequences:
        uf2.find(seq.line_code)
        for ref in seq.refs:
            if ref in owner:
                uf2.union(owner[ref], seq.line_code)
            else:
                owner[ref] = seq.line_code
    line_groups: dict[object, list[str]] = defaultdict(list)
    for seq in plan.sequences:
        line_groups[uf2.find(seq.line_code)].append(seq.line_code)
    by_line = {s.line_code: s for s in plan.sequences}
    for n, root in enumerate(sorted(line_groups, key=lambda r: sorted(line_groups[r])[0]), start=1):
        group = f"resequence-{n}"
        lines = sorted(line_groups[root])
        refs_out: list[str] = []
        for code in lines:
            s = by_line[code]
            rows.append(
                {
                    "op": "production_order.resequence",
                    "target_ref": s.line_code,
                    "expected_version": None,
                    "payload": {"line_code": s.line_code, "order_codes": list(s.after)},
                    "group": group,
                }
            )
            refs_out.extend(r for r in s.refs if owner.get(r) == code)
        for ref in sorted(refs_out):
            m = plan.moves[ref]
            if m.eta_after == m.eta_before:
                continue
            rows.append(
                {
                    "op": "po_line.reschedule",
                    "target_ref": ref,
                    "expected_version": version_of(ref),
                    "payload": {"eta_dc": m.eta_after.isoformat()},
                    "group": group,
                }
            )

    for row in rows:
        actions.append(ActionM5.model_validate({"seq": len(actions) + 1, **row}))
    if booking_placeholder:
        warnings.append(
            f"ProblemData.capacity_bookings carry no version: {len(booking_placeholder)} "
            f"{CANCEL_OP} action(s) use expected_version={PLACEHOLDER_VERSION}; refresh it from the target system "
            "before compiling"
        )
    if placeholder:
        warnings.append(
            f"ProblemData.po_lines carry no version: {len(placeholder)} PO-line action(s) use "
            f"expected_version={PLACEHOLDER_VERSION}; refresh it from the target system before compiling"
        )
    return actions, warnings
