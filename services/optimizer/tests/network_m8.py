"""Hand-made problems for the M8 CP-SAT stages (``docs/contracts/125-m8-optimizer-cpsat.md``).

:func:`sourcing_problem` extends :mod:`tests.network` with more PO lines competing for one
alternate factory, so MOQ per style, order multiples, capacity groups, split limits and the
expedite-vs-re-source choice all bite:

* UK1 (clusters UK1-A, UK1-B) receives three PO lines from F-1: PO-1#1 500 TOP-M, PO-1#2 250
  TOP-S (style TOP) and PO-1#3 300 TEE-M (style TEE); DE1 (DE1-A) receives PO-2#1 150 KNIT-M
  from F-2. Every line arrives in W44 and covers W44-W48 exactly; stock covers W41-W43.
* ``TRIM_DELAYS``: TRIM-1 (TOP and TEE at F-1) and TRIM-3 (KNIT at F-2) are 14 days late, so
  every line lands in W46 and W44-W45 demand is lost (TOP-M 200, TOP-S 100, TEE-M 120,
  KNIT-M 60 units).
* Alternate factory F-N (SUP-N, 1300 c/unit, lead 7 d) ships to UK1 over the road lane NS-UK
  (TOP and TEE) and to DE1 over NS-DE (KNIT): two :class:`AltSource` rows of one factory.
  Expedite lanes: SA-UK (sea-air, 300 units/week) for the UK1 lines only.

:func:`random_problem` draws small networks of the same shape (seeded) for property tests.
"""

from __future__ import annotations

import copy
import random
from collections import defaultdict
from typing import Any

from keelson_contracts.optimizer import AltSource, OptimizeRequest, OptimizeResponse

from .network import WEEKS, network_problem

TRIM_DELAYS = [
    {"op": "lead_time_shift", "target": {"kinds": ["supplier"], "ids": ["TRIM-1"]}, "days": 14, "prov": "fact"},
    {"op": "lead_time_shift", "target": {"kinds": ["supplier"], "ids": ["TRIM-3"]}, "days": 14, "prov": "fact"},
]

_UK_LINES = [
    # ref, sku, qty, demand UK1-A, demand UK1-B
    ("PO-1#1", "TOP-M", 500, 60, 40),
    ("PO-1#2", "TOP-S", 250, 30, 20),
    ("PO-1#3", "TEE-M", 300, 30, 30),
]


def _po_line(ref: str, sku: str, qty: int, dc: str, factory: str, lane: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "po_code": ref.split("#")[0],
        "supplier_code": "SUP-1" if factory == "F-1" else "SUP-2",
        "factory_code": factory,
        "dest_dc": dc,
        "sku": sku,
        "qty": qty,
        "eta": "2026-10-26",
        "ex_factory": "2026-09-25",
        "mode": "sea",
        "lane_code": lane,
    }


def alt_source(lane: str, styles: list[str], **over: Any) -> dict[str, Any]:
    src: dict[str, Any] = {
        "supplier_code": "SUP-N",
        "factory_code": "F-N",
        "style_codes": styles,
        "lead_time_days": 7,
        "unit_cost_cents": 1300,
        "moq_units": 200,
        "weekly_capacity_units": 1000,
        "approved": True,
        "compliance_ok": True,
        "lane_code": lane,
    }
    src.update(over)
    return src


def sourcing_problem(uk: dict[str, Any] | None = None, de: dict[str, Any] | None = None) -> dict[str, Any]:
    """The module's network; ``uk`` / ``de`` override fields of the two F-N rows."""
    data = network_problem()
    data["skus"] += [
        {"code": "TOP-S", "style_code": "TOP", "retail_price_cents": 3000, "unit_cost_cents": 1000},
        {"code": "TEE-M", "style_code": "TEE", "retail_price_cents": 2500, "unit_cost_cents": 1000},
    ]
    data["po_lines"] = [_po_line(ref, sku, qty, "UK1", "F-1", "SEA-UK") for ref, sku, qty, _a, _b in _UK_LINES]
    data["po_lines"].append(_po_line("PO-2#1", "KNIT-M", 150, "DE1", "F-2", "SEA-DE"))
    data["receipts"] = [
        {"ref": p["ref"], "dc_code": p["dest_dc"], "sku": p["sku"], "qty": p["qty"], "eta": p["eta"]}
        for p in data["po_lines"]
    ]
    data["demand"] = [d for d in data["demand"] if d["sku"] == "KNIT-M"]
    data["inventory"] = [i for i in data["inventory"] if i["sku"] == "KNIT-M"]
    data["cluster_inventory"] = [c for c in data["cluster_inventory"] if c["sku"] == "KNIT-M"]
    for _ref, sku, _qty, a, b in _UK_LINES:
        for cl, units in (("UK1-A", a), ("UK1-B", b)):
            data["demand"] += [{"cluster_code": cl, "sku": sku, "iso_week": w, "units": units} for w in WEEKS]
            data["cluster_inventory"].append({"cluster_code": cl, "sku": sku, "on_hand": units})
        data["inventory"].append({"location_type": "dc", "location_code": "UK1", "sku": sku, "on_hand": 2 * (a + b)})
    data["bom"].append({"style_code": "TEE", "component_code": "CMP-Z", "supplier_code": "TRIM-1", "factory_code": "F-1"})
    data["inbound_lanes"].append(
        {
            "code": "NS-DE",
            "origin": "PORT-N",
            "dest_dc": "DE1",
            "mode": "road",
            "transit_days": 4,
            "cost_per_unit_cents": 40,
            "weekly_capacity_units": 10000,
            "via": [],
        }
    )
    data["alt_sources"] = [alt_source("NS-UK", ["TOP", "TEE"], **(uk or {})), alt_source("NS-DE", ["KNIT"], **(de or {}))]
    return data


def request(
    levers: list[dict[str, Any]],
    data: dict[str, Any] | None = None,
    *,
    stage: str | None = "cpsat",
    perturbations: list[dict[str, Any]] | None = None,
    seed: int = 3,
) -> dict[str, Any]:
    solver: dict[str, Any] = {"time_limit_s": 5, "seed": seed}
    if stage is not None:
        solver["integer_stage"] = stage
    return {
        "data": data if data is not None else sourcing_problem(),
        "perturbations": copy.deepcopy(TRIM_DELAYS if perturbations is None else perturbations),
        "plan_spec": {"id": "opt-m8", "title": "m8", "levers": copy.deepcopy(levers)},
        "solver": solver,
    }


def random_problem(seed: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A seeded variant of :func:`sourcing_problem`: quantities, delays, MOQs, multiples, styles
    bases, capacities, budgets and split limits drawn at random. Returns ``(data, levers)``."""
    rng = random.Random(seed)
    uk = {
        "moq_units": rng.choice([0, 100, 200, 350, 600]),
        "weekly_capacity_units": rng.choice([150, 300, 700, 1000]),
        "order_multiple_units": rng.choice([1, 1, 6, 12, 25]),
        "moq_basis": rng.choice(["po", "style"]),
        "unit_cost_cents": rng.choice([1100, 1300, 1700]),
        "lead_time_days": rng.choice([3, 7, 10]),
    }
    if rng.random() < 0.4:
        uk["max_orders"] = rng.choice([1, 2])
    de = {"moq_units": rng.choice([0, 50, 120]), "order_multiple_units": rng.choice([1, 10])}
    if rng.random() < 0.5:
        uk["capacity_group"] = de["capacity_group"] = "F-N"
        de["weekly_capacity_units"] = rng.choice([200, 400])
    data = sourcing_problem(uk, de)
    for p, r in zip(data["po_lines"], data["receipts"], strict=True):
        p["qty"] = r["qty"] = max(50, p["qty"] + rng.choice([-100, -50, 0, 50, 100]))
    data["inbound_lanes"][2]["weekly_capacity_units"] = rng.choice([0, 100, 300])
    alt: dict[str, Any] = {"code": "resource_alt_supplier"}
    params: dict[str, Any] = {}
    if rng.random() < 0.4:
        params["max_buys_per_line"] = 1
    if rng.random() < 0.3:
        params["max_premium_cents"] = rng.choice([50_000, 150_000])
    if params:
        alt["params"] = params
    levers = [alt]
    if rng.random() < 0.6:
        levers.insert(0, {"code": "expedite_mode_shift"})
    if rng.random() < 0.3:
        levers.insert(0, {"code": "reallocate_inventory"})
    return data, levers


def check_invariants(req: OptimizeRequest, resp: OptimizeResponse) -> None:
    """Every M8 sourcing term and the M3 plan rules hold on ``resp`` (either integer stage):
    one ``po_line`` op per PO line, splits below the open quantity and within the lane's weekly
    capacity, per ``po.create`` the order multiple on every line, the effective MOQ per basis
    and the weekly capacity, capacity groups per arrival week, ``max_orders`` per source,
    ``max_buys_per_line`` and the lever budgets."""
    data = req.data
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs)), "one op per PO line"
    pol = {p.ref: p for p in data.po_lines}
    lanes = {il.code: il for il in data.inbound_lanes}
    booked: dict[tuple[str, str], int] = defaultdict(int)
    for d in resp.decisions:
        if d.lever == "expedite_mode_shift":
            booked[(d.lane_code, d.book_week)] += d.qty
            assert d.qty < pol[d.po_line_ref].qty
    for (lane, _week), qty in booked.items():
        assert qty <= lanes[lane].weekly_capacity_units
    style = {s.code: s.style_code for s in data.skus}
    group_cap: dict[str, int] = {}
    for s in data.alt_sources:
        if s.capacity_group is not None:
            group_cap[s.capacity_group] = min(group_cap.get(s.capacity_group, s.weekly_capacity_units), s.weekly_capacity_units)

    def source_of(factory: str, dc: str) -> AltSource:
        return next(s for s in data.alt_sources if s.factory_code == factory and lanes[s.lane_code].dest_dc == dc)

    orders: dict[tuple[str, str], int] = defaultdict(int)
    group_week: dict[tuple[str, str], int] = defaultdict(int)
    for a in resp.actions:
        if a.op != "po.create":
            continue
        p = a.payload
        src = source_of(p.factory_code, p.dest_dc)
        m = src.order_multiple_units
        total = sum(line.qty for line in p.lines)
        assert all(line.qty % m == 0 for line in p.lines), (m, p.lines)
        assert total <= src.weekly_capacity_units
        if src.moq_basis == "style":
            per_style: dict[str, int] = defaultdict(int)
            for line in p.lines:
                per_style[style[line.sku]] += line.qty
            assert all(q >= src.moq_effective for q in per_style.values()), (src.moq_effective, per_style)
        else:
            assert total >= src.moq_effective
        orders[(src.factory_code, src.lane_code)] += 1
        if src.max_orders is not None:
            assert orders[(src.factory_code, src.lane_code)] <= src.max_orders
        if src.capacity_group is not None:
            group_week[(src.capacity_group, p.eta_dc.isoformat())] += total
    for (group, _eta), qty in group_week.items():
        assert qty <= group_cap[group]
    alt = next((lv.params for lv in req.plan_spec.levers if lv.code == "resource_alt_supplier"), None)
    buys: dict[str, int] = defaultdict(int)
    premium = 0
    for d in resp.decisions:
        if d.lever == "resource_alt_supplier":
            buys[d.replaces_po_line_ref] += 1
            premium += d.premium_cents
    if alt is not None and alt.max_buys_per_line is not None:  # type: ignore[union-attr]
        assert all(n <= alt.max_buys_per_line for n in buys.values())  # type: ignore[union-attr]
    if alt is not None and alt.max_premium_cents is not None:  # type: ignore[union-attr]
        assert premium <= alt.max_premium_cents  # type: ignore[union-attr]


__all__ = ["TRIM_DELAYS", "alt_source", "check_invariants", "random_problem", "request", "sourcing_problem"]
