"""Deterministic optimizer stub behind an ``httpx.MockTransport`` (contract models only).

M1 behaviour is unchanged (``reallocate_inventory`` → one ``transfer_order.create``). M3 (``80`` §2–§3):
``include_graph`` returns a small grouped graph, TTS rows and bottlenecks; the impact timeline has
short weeks for the perturbed DCs' clusters; every v1 lever emits contract-valid actions when the
ProblemData v2 lists allow it (expedite → ``po_line.split`` + ``capacity_booking.create``,
alt-supplier → ``po.create`` + ``po_line.change_qty`` in one atomic group, prioritise → one
``allocation.set_qty``, resequence → ``production_order.resequence`` + ``po_line.reschedule``), and
every response carries a scorecard. M8: ``integer_stage="cpsat"`` requests echo ``SolverInfo.stages``
and the integer params."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import httpx
import jwt
from keelson_contracts.common import add_weeks, iso_week_of, monday_of, week_range
from keelson_contracts.optimizer import (
    Action,
    BindingConstraint,
    CostBreakdown,
    DcRisk,
    Explain,
    FirstBreach,
    GraphEdge,
    GraphNode,
    ImpactGraph,
    ImpactKpis,
    ImpactRequest,
    ImpactResponse,
    KpiDelta,
    OptimizeRequest,
    OptimizeResponse,
    Provenance,
    Scorecard,
    SolverInfo,
    SolverStage,
    TimelinePoint,
    TtsRow,
)

SECRET = "test-optimizer-shared-secret-0123456789abcdef"


class OptimizerStub:
    def __init__(self, *, units_per_transfer: tuple[int, int] = (120, 180), fallback: bool = False,
                 scorecards: bool = True) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.units_per_transfer = units_per_transfer
        self.fallback = fallback
        self.scorecards = scorecards
        """``False`` = an M1 optimizer without ``scorecard`` (the engine derives one)."""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        try:
            claims = jwt.decode(auth.removeprefix("Bearer "), SECRET, algorithms=["HS256"], audience="keelson-optimizer")
        except jwt.PyJWTError:
            return httpx.Response(401, json={"detail": "unauthorized"})
        body = json.loads(request.content)
        if claims.get("iss") != "keelson-engine" or claims.get("tenant") != body["data"]["tenant"]:
            return httpx.Response(403, json={"detail": "forbidden"})
        self.requests.append((request.url.path, body))
        if request.url.path == "/v1/impact":
            return httpx.Response(200, json=self.impact(ImpactRequest.model_validate(body)).model_dump(mode="json"))
        if request.url.path == "/v1/optimize":
            return httpx.Response(200, json=self.optimize(OptimizeRequest.model_validate(body)).model_dump(mode="json"))
        return httpx.Response(404)

    @staticmethod
    def _hash(req: Any) -> str:
        return hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _shifted(req: ImpactRequest | OptimizeRequest) -> set[str]:
        shifted: set[str] = set()
        for p in req.perturbations:
            if getattr(p, "op", None) == "lead_time_shift":
                shifted |= set(p.target.ids)
        return shifted

    def _kpis(self, req: ImpactRequest | OptimizeRequest, *, perturbed: bool, recovered: int = 0) -> ImpactKpis:
        shifted = self._shifted(req) if perturbed else set()
        units = sum(r.qty for r in req.data.receipts if r.ref in shifted) // 3
        units = max(0, units - recovered)
        sku = req.data.skus[0]
        breach = []
        if units:
            week = add_weeks(req.data.horizon.start_week, 4 if recovered == 0 else 5)
            cluster = next((c for c in req.data.clusters if c.dc_code == "UK1"), req.data.clusters[0])
            breach = [FirstBreach(dc_code=cluster.dc_code, cluster_code=cluster.code, style_code=sku.style_code,
                                  week=week, date=monday_of(week))]
        return ImpactKpis(
            units_at_risk=units,
            revenue_at_risk_cents=units * sku.retail_price_cents,
            margin_at_risk_cents=units * (sku.retail_price_cents - sku.unit_cost_cents),
            shortage_cluster_weeks=2 if units else 0,
            first_breach=breach,
        )

    def _timeline(self, req: ImpactRequest, kpis: ImpactKpis) -> list[TimelinePoint]:
        weeks = week_range(req.data.horizon.start_week, req.data.horizon.weeks)
        style = req.data.skus[0].style_code
        out: list[TimelinePoint] = []
        for i, cluster in enumerate(req.data.clusters[:4]):
            for w, week in enumerate(weeks):
                short = (kpis.units_at_risk // (2 + i)) if kpis.units_at_risk and w >= 4 + (i % 2) else 0
                pab = -short if short else (40 if w < 3 else 5)
                out.append(TimelinePoint(cluster_code=cluster.code, style_code=style, week=week,
                                         projected_available=pab, demand=20, shortage=short))
        return out

    def _graph(self, req: ImpactRequest) -> ImpactGraph:
        shifted = self._shifted(req)
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        lines = [r for r in req.data.receipts]
        for r in lines:
            po = r.ref.split("#", 1)[0]
            status = "disrupted" if r.ref in shifted else "ok"
            prev = nodes.get(f"po:{po}")
            if prev is None or status == "disrupted":
                nodes[f"po:{po}"] = GraphNode(id=f"po:{po}", kind="po", label=po, status=status, units_at_risk=0)
            nodes.setdefault(f"dc:{r.dc_code}", GraphNode(id=f"dc:{r.dc_code}", kind="dc", label=r.dc_code,
                                                          status="impacted" if r.ref in shifted else "ok", units_at_risk=0))
            edge = GraphEdge(source=f"po:{po}", target=f"dc:{r.dc_code}", kind="ships")
            if edge not in edges:
                edges.append(edge)
        for c in req.data.clusters:
            dc = nodes.get(f"dc:{c.dc_code}")
            status = "at_risk" if dc is not None and dc.status == "impacted" else "ok"
            nodes[f"cluster:{c.code}"] = GraphNode(
                id=f"cluster:{c.code}", kind="cluster", label=c.code, status=status,
                units_at_risk=10 if status == "at_risk" else 0,
                first_breach_week=add_weeks(req.data.horizon.start_week, 4) if status == "at_risk" else None,
            )
            if f"dc:{c.dc_code}" in nodes:
                edges.append(GraphEdge(source=f"dc:{c.dc_code}", target=f"cluster:{c.code}", kind="serves"))
        return ImpactGraph(nodes=sorted(nodes.values(), key=lambda n: n.id), edges=edges)

    def impact(self, req: ImpactRequest) -> ImpactResponse:
        kpis = self._kpis(req, perturbed=True)
        extra: dict[str, Any] = {}
        if req.include_graph:
            extra = {
                "graph": self._graph(req),
                "tts": [TtsRow(dc_code=d.code, tts_days=14, ttr_days=21 if d.code == "UK1" else 7,
                               verdict="loss" if d.code == "UK1" else "ok") for d in req.data.dcs],
                "bottlenecks": [BindingConstraint(name="lane DE1>NL1 cap W43", shadow_price_cents=120)],
            }
        return ImpactResponse(
            kpis=kpis,
            by_dc=[DcRisk(dc_code="UK1", units_at_risk=kpis.units_at_risk, revenue_at_risk_cents=kpis.revenue_at_risk_cents)],
            timeline=self._timeline(req, kpis),
            baseline_kpis=self._kpis(req, perturbed=False),
            provenance=Provenance(version="0.1.0", inputs_hash=self._hash(req), computed_at=dt.datetime(2026, 10, 5, tzinfo=dt.UTC)),
            **extra,
        )

    # ───────────────────────────────── optimize ─────────────────────────────────

    def _lever_actions(self, req: OptimizeRequest) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        data = req.data
        actions: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        cost = {"transfer": 0, "expedite_premium": 0, "nearshore_premium": 0}
        ship_week = add_weeks(iso_week_of(data.as_of), 1)
        for lever in req.plan_spec.levers:
            if lever.code == "reallocate_inventory":
                skus = [s.code for s in data.skus][:2]
                lines = [{"sku": sku, "qty": qty} for sku, qty in zip(skus, self.units_per_transfer, strict=False)]
                actions.append({"op": "transfer_order.create", "target_ref": None, "expected_version": None,
                                "payload": {"from_dc": "DE1", "to_dc": "NL1",
                                            "ship_date": monday_of(ship_week).isoformat(), "lines": lines}})
                for line in lines:
                    decisions.append({"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "NL1", "sku": line["sku"],
                                      "qty": line["qty"], "ship_week": ship_week, "arrive_week": add_weeks(ship_week, 1)})
                cost["transfer"] += sum(self.units_per_transfer) * 35
            elif lever.code == "expedite_mode_shift":
                shifted = self._shifted(req)
                line = next((pl for pl in data.po_lines if pl.ref in shifted and pl.qty > 1), None)
                lane = next((il for il in data.inbound_lanes if line and il.dest_dc == line.dest_dc and il.mode == "sea_air"), None)
                if line is None or lane is None:
                    continue
                qty = line.qty // 2
                actions.append({"op": "po_line.split", "target_ref": line.ref, "expected_version": 1,
                                "payload": {"mode": "sea_air", "qty": qty, "lane_code": lane.code}, "group": "exp-1"})
                actions.append({"op": "capacity_booking.create", "target_ref": None, "expected_version": None,
                                "payload": {"lane_code": lane.code, "iso_week": ship_week, "qty_units": qty},
                                "group": "exp-1"})
                decisions.append({"lever": "expedite_mode_shift", "po_line_ref": line.ref, "sku": line.sku,
                                  "dest_dc": line.dest_dc, "lane_code": lane.code, "mode": "sea_air", "qty": qty,
                                  "book_week": ship_week, "arrive_week": add_weeks(ship_week, 2),
                                  "premium_cents": qty * 30})
                cost["expedite_premium"] += qty * 30
            elif lever.code == "resource_alt_supplier":
                shifted = self._shifted(req)
                style_of = {s.code: s.style_code for s in data.skus}
                lanes = {il.code: il for il in data.inbound_lanes}
                pick = None
                for src in data.alt_sources:
                    if not src.usable or src.lane_code not in lanes:
                        continue
                    line = next((pl for pl in data.po_lines if pl.ref in shifted and style_of.get(pl.sku) in src.style_codes
                                 and lanes[src.lane_code].dest_dc == pl.dest_dc), None)
                    if line is not None:
                        pick = (src, line)
                        break
                if pick is None:
                    continue
                src, line = pick
                qty = min(line.qty, max(src.moq_units, line.qty // 3))
                eta = data.as_of + dt.timedelta(days=src.lead_time_days)
                actions.append({"op": "po.create", "target_ref": None, "expected_version": None, "group": "alt-1",
                                "payload": {"supplier_code": src.supplier_code, "factory_code": src.factory_code,
                                            "dest_dc": line.dest_dc, "eta_dc": eta.isoformat(),
                                            "lines": [{"sku": line.sku, "qty": qty}]}})
                remaining = line.qty - qty
                if remaining:
                    actions.append({"op": "po_line.change_qty", "target_ref": line.ref, "expected_version": 1,
                                    "payload": {"qty": remaining}, "group": "alt-1"})
                else:
                    actions.append({"op": "po_line.cancel", "target_ref": line.ref, "expected_version": 1,
                                    "payload": {}, "group": "alt-1"})
                decisions.append({"lever": "resource_alt_supplier", "supplier_code": src.supplier_code,
                                  "factory_code": src.factory_code, "dest_dc": line.dest_dc, "sku": line.sku, "qty": qty,
                                  "eta": eta.isoformat(), "replaces_po_line_ref": line.ref, "premium_cents": qty * 90})
                cost["nearshore_premium"] += qty * 90
            elif lever.code == "prioritize_allocation":
                alloc = next((a for a in data.allocations if not a.protected and a.qty > 1), None)
                if alloc is None:
                    continue
                actions.append({"op": "allocation.set_qty", "target_ref": alloc.code, "expected_version": alloc.version,
                                "payload": {"qty": alloc.qty - alloc.qty // 4}})
                decisions.append({"lever": "prioritize_allocation", "allocation_code": alloc.code, "sku": alloc.sku,
                                  "dc_code": alloc.dc_code, "cluster_code": alloc.cluster_code,
                                  "ship_week": alloc.ship_week, "qty_before": alloc.qty,
                                  "qty_after": alloc.qty - alloc.qty // 4})
            elif lever.code == "resequence_production":
                line = next((pl for pl in data.production if sum(o.movable for o in pl.orders) >= 2), None)
                if line is None:
                    continue
                movable = [o for o in sorted(line.orders, key=lambda o: o.seq) if o.movable]
                after = [o.code for o in reversed(movable)]
                actions.append({"op": "production_order.resequence", "target_ref": line.line_code,
                                "expected_version": None, "group": "rsq-1",
                                "payload": {"line_code": line.line_code, "order_codes": after}})
                po_lines = {pl.ref: pl for pl in data.po_lines}
                first = movable[-1]
                changes = []
                if first.po_line_ref in po_lines:
                    pl = po_lines[first.po_line_ref]
                    new_eta = pl.eta - dt.timedelta(days=3)
                    actions.append({"op": "po_line.reschedule", "target_ref": pl.ref, "expected_version": 1,
                                    "payload": {"eta_dc": new_eta.isoformat()}, "group": "rsq-1"})
                    changes.append({"po_line_ref": pl.ref, "eta_before": pl.eta.isoformat(), "eta_after": new_eta.isoformat()})
                decisions.append({"lever": "resequence_production", "line_code": line.line_code,
                                  "factory_code": line.factory_code, "order_codes_before": [o.code for o in movable],
                                  "order_codes_after": after, "eta_changes": changes})
        for seq, action in enumerate(actions, start=1):
            action["seq"] = seq
        return actions, decisions, cost

    def _solver_info(self, req: OptimizeRequest, *, do_nothing: bool, fallback: bool) -> SolverInfo:
        """M8: ``integer_stage="cpsat"`` requests get per-solve records (an LP stage, plus the whole-unit
        CP-SAT stage when the plan re-sources) and the integer params, like the real optimizer."""
        params: dict[str, int | str | bool] = {"plan_fallback": "do_nothing"} if fallback else {}
        stages: list[SolverStage] = []
        if req.solver.integer_stage == "cpsat" and not do_nothing:
            params["integer_stage"] = "cpsat"
            seed = req.solver.seed
            stages.append(SolverStage(name="lp_phase1", solver="GLOP", status="OPTIMAL", used=True, seed=seed,
                                      model_hash="1" * 64, num_variables=10, num_constraints=5))
            if any(lv.code == "resource_alt_supplier" for lv in req.plan_spec.levers):
                params.update(integer_method="cpsat_optimal", integer_gap_bp=0, cpsat_num_workers=1,
                              cpsat_deterministic=True)
                stages.append(SolverStage(name="integer", solver="CP-SAT", status="OPTIMAL", used=True, seed=seed,
                                          model_hash="2" * 64, objective=100, bound=100, det_time_limit_ms=150,
                                          det_time_ms=12, num_workers=1, num_variables=40, num_constraints=30))
        return SolverInfo(name="none" if do_nothing else "GLOP", ortools="9.15.6755", params=params,
                          seed=req.solver.seed, inputs_hash=self._hash(req), solve_ms=3, stages=stages)

    def optimize(self, req: OptimizeRequest) -> OptimizeResponse:
        before = self._kpis(req, perturbed=True)
        do_nothing = not req.plan_spec.levers or self.fallback
        actions: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        cost = {"transfer": 0, "expedite_premium": 0, "nearshore_premium": 0}
        if not do_nothing:
            actions, decisions, cost = self._lever_actions(req)
        moved = sum(int(line["qty"]) for a in actions for line in a["payload"].get("lines") or [])
        moved += sum(int(a["payload"].get("qty") or 0) for a in actions if a["op"] == "po_line.split")
        recovered = min(before.units_at_risk, max(0, moved - 20)) if actions else 0
        fallback = bool(req.plan_spec.levers) and (self.fallback or not actions)
        after = self._kpis(req, perturbed=True, recovered=recovered)
        margin = before.margin_at_risk_cents - after.margin_at_risk_cents
        total_cost = sum(cost.values())
        demand = sum(d.units for d in req.data.demand) or 1
        scorecard = Scorecard(
            fill_rate_bp=max(0, min(10_000, round(10_000 * (demand - after.units_at_risk) / demand))),
            lost_units=after.units_at_risk, lost_margin_cents=after.margin_at_risk_cents,
            incremental_cost_cents=total_cost, cost_breakdown=CostBreakdown(**cost),
            net_benefit_cents=margin - total_cost, shortage_days=7 * after.shortage_cluster_weeks,
            first_breach_shift_days=7 if recovered and after.first_breach else 0, tts_gain_days=0,
            changes=len(actions),
        )
        return OptimizeResponse(
            status="OPTIMAL",
            plan_spec_id=req.plan_spec.id,
            kpis_before=before,
            kpis_after=after,
            kpi_delta=KpiDelta(units_recovered=recovered, margin_recovered_cents=margin, transfer_cost_cents=cost["transfer"],
                               net_benefit_cents=margin - total_cost,
                               transfers=sum(1 for a in actions if a["op"] == "transfer_order.create")),
            decisions=decisions,
            actions=[Action.model_validate(a) for a in actions],
            explain=Explain(binding_constraints=[]),
            solver=self._solver_info(req, do_nothing=do_nothing, fallback=fallback),
            warnings=["no plan improves the outcome"] if fallback else [],
            scorecard=scorecard if self.scorecards else None,
        )
