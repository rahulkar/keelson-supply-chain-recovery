"""Deterministic synthetic problem generator (Merxa ``full``-like scale) for performance
tests: ``n_styles`` style-colours x 6 sizes SKUs, 4 DCs x ``clusters_per_dc`` clusters,
9 weekly buckets from 2026-W41, BD receipts into UK1/NL1 delayed by 7 days, DE1/PL1
surplus, the five tenant lanes."""

from __future__ import annotations

import copy
import random
from typing import Any

SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
SIZE_BP = [5000, 8000, 12000, 12000, 9000, 6000]
DCS = ["UK1", "NL1", "DE1", "PL1"]
LANES = [
    {"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000},
    {"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 12000},
    {"from_dc": "PL1", "to_dc": "NL1", "transit_days": 3, "cost_per_unit_cents": 45, "max_units_wk": 15000},
    {"from_dc": "PL1", "to_dc": "DE1", "transit_days": 2, "cost_per_unit_cents": 30, "max_units_wk": 15000},
    {"from_dc": "NL1", "to_dc": "UK1", "transit_days": 2, "cost_per_unit_cents": 50, "max_units_wk": 15000},
]
WEEKS = [f"2026-W{w}" for w in range(41, 50)]
PEAK = {"2026-W45", "2026-W46", "2026-W47", "2026-W48"}


def synthetic_request(n_styles: int = 30, clusters_per_dc: int = 10, seed: int = 20261005) -> dict[str, Any]:
    rng = random.Random(seed)
    clusters = [(f"{dc}-C{i:02d}", dc) for dc in DCS for i in range(clusters_per_dc)]
    skus = []
    for s in range(n_styles):
        style = f"STY-{s:03d}"
        price = rng.choice([1500, 2000, 2500, 3000, 3500])
        cost = price * rng.choice([30, 35, 40]) // 100
        skus += [
            {"code": f"{style}-{sz}", "style_code": style, "retail_price_cents": price, "unit_cost_cents": cost}
            for sz in SIZES
        ]
    demand, dem = [], {}
    cl_weight = {c: rng.randint(6, 14) for c, _ in clusters}
    for s_idx, sku in enumerate(skus):
        size_bp = SIZE_BP[s_idx % 6]
        for cl, _dc in clusters:
            for w in WEEKS:
                base = (8 if w in PEAK else 4) * cl_weight[cl]
                units = base * size_bp // 10000 + rng.randint(0, 3)
                dem[(cl, sku["code"], w)] = units
                demand.append({"cluster_code": cl, "sku": sku["code"], "iso_week": w, "units": units})

    def need(dc: str, sku: str, weeks: list[str]) -> int:
        return sum(dem[(cl, sku, w)] for cl, d in clusters if d == dc for w in weeks)

    inventory, receipts, cluster_inventory = [], [], []
    for sku in (s["code"] for s in skus):
        for cl, _dc in clusters:
            cluster_inventory.append({"cluster_code": cl, "sku": sku, "on_hand": dem[(cl, sku, WEEKS[0])]})
        for dc in ("UK1", "NL1"):
            on_hand = need(dc, sku, WEEKS[1:4]) + rng.randint(0, 20)
            inventory.append(
                {
                    "location_type": "dc",
                    "location_code": dc,
                    "sku": sku,
                    "on_hand": on_hand,
                    "allocated": on_hand // 10,
                    "in_transit": 0,
                }
            )
        for dc, surplus in (("DE1", 0.5), ("PL1", 0.3)):
            own = need(dc, sku, WEEKS[1:])
            extra = int(surplus * (need("UK1", sku, ["2026-W45"]) + need("NL1", sku, ["2026-W45"])))
            inventory.append(
                {
                    "location_type": "dc",
                    "location_code": dc,
                    "sku": sku,
                    "on_hand": own + extra,
                    "allocated": (own + extra) // 10,
                    "in_transit": 0,
                }
            )
        for po, dc, eta, weeks in (
            ("PO-BD-1", "UK1", "2026-11-02", WEEKS[4:6]),
            ("PO-BD-2", "NL1", "2026-11-04", WEEKS[4:7]),
            ("PO-BD-3", "UK1", "2026-11-16", WEEKS[6:]),
            ("PO-BD-4", "NL1", "2026-11-23", WEEKS[7:]),
        ):
            receipts.append({"ref": f"{po}#{sku}", "dc_code": dc, "sku": sku, "qty": need(dc, sku, weeks), "eta": eta})
    return {
        "data": {
            "tenant": "demo-retail",
            "as_of": "2026-10-05",
            "horizon": {"start_week": WEEKS[0], "weeks": len(WEEKS)},
            "dcs": [{"code": dc, "outbound_cap_units_wk": 100000} for dc in DCS],
            "clusters": [{"code": c, "dc_code": d} for c, d in clusters],
            "skus": skus,
            "inventory": inventory,
            "cluster_inventory": cluster_inventory,
            "receipts": receipts,
            "demand": demand,
            "lanes": copy.deepcopy(LANES),
            "protected_min": [],
        },
        "perturbations": [
            {
                "op": "lead_time_shift",
                "target": {"kinds": ["po"], "ids": ["PO-BD-1", "PO-BD-2", "PO-BD-3", "PO-BD-4"]},
                "days": 7,
                "prov": "decision",
            },
        ],
        "plan_spec": {
            "id": "opt-A",
            "title": "Rebalance between DCs",
            "levers": [{"code": "reallocate_inventory", "params": {"max_units_total": 200000}}],
            "weights": {"lost_margin": 10000, "transfer_cost": 10000},
        },
        "solver": {"time_limit_s": 5, "seed": 7},
    }
