"""Generate ``small_problem.json`` — an OptimizeRequest shaped like the Merxa ``small``
seed story (docs/contracts/20-merxa.md):

* 4 DCs (UK1 Thurrock, NL1 Venlo, DE1 Erfurt, PL1 Poznan), 8 clusters, 3 puffer
  style-colours x 3 sizes = 9 SKUs, horizon 2026-W41 .. 2026-W49 (peak W45-W48).
* Bangladesh POs (factories F-BD-01 / F-BD-02, trims from TRIM-BD-01) land in UK1/NL1 in
  W45 and W47/W48. UK1/NL1 stock covers W41-W44 only, so delaying the BD receipts by 7
  days leaves UK1/NL1 short in peak week W45.
* The Cambodia PO (F-KH-01) already arrived in DE1 in W40, so DE1 (and PL1) hold
  transferable surplus. Lanes are the tenant lanes from ``infra/tenants.yaml``.

Run: ``python tests/fixtures/make_small_problem.py`` (deterministic, no randomness).
"""

from __future__ import annotations

import json
from pathlib import Path

WEEKS = [f"2026-W{w}" for w in range(41, 50)]
PEAK = {"2026-W45", "2026-W46", "2026-W47", "2026-W48"}
DCS = [("UK1", 50000), ("NL1", 60000), ("DE1", 60000), ("PL1", 40000)]
CLUSTERS = [
    ("UK1-COLD-A", "UK1", 12000),
    ("UK1-MILD-BC", "UK1", 8000),
    ("NL1-MILD-A", "NL1", 12000),
    ("NL1-MILD-BC", "NL1", 8000),
    ("DE1-COLD-A", "DE1", 12000),
    ("DE1-MILD-BC", "DE1", 8000),
    ("PL1-COLD-A", "PL1", 12000),
    ("PL1-COLD-BC", "PL1", 8000),
]
STYLES = [("PUF-W-BLK", 12000, 2500, 900), ("PUF-W-NVY", 10000, 2500, 900), ("PUF-M-OLV", 8000, 3000, 1100)]
SIZES = [("S", 8000), ("M", 12000), ("L", 10000)]
LANES = [  # infra/tenants.yaml (demo-retail -> merxa -> lanes)
    {"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000},
    {"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 12000},
    {"from_dc": "PL1", "to_dc": "NL1", "transit_days": 3, "cost_per_unit_cents": 45, "max_units_wk": 15000},
    {"from_dc": "PL1", "to_dc": "DE1", "transit_days": 2, "cost_per_unit_cents": 30, "max_units_wk": 15000},
    {"from_dc": "NL1", "to_dc": "UK1", "transit_days": 2, "cost_per_unit_cents": 50, "max_units_wk": 15000},
]


def bp(x: int, *factors: int) -> int:
    for f in factors:
        x = (x * f + 5000) // 10000
    return x


def main() -> None:
    skus = [
        {"code": f"{st}-{sz}", "style_code": st, "retail_price_cents": price, "unit_cost_cents": cost}
        for st, _sf, price, cost in STYLES
        for sz, _zf in SIZES
    ]
    sf = {st: f for st, f, _p, _c in STYLES}
    zf = {sz: f for sz, f in SIZES}

    demand = []
    dem: dict[tuple[str, str, str], int] = {}
    for cl, _dc, cf in CLUSTERS:
        for st, _f, _p, _c in STYLES:
            for sz, _z in SIZES:
                sku = f"{st}-{sz}"
                for w in WEEKS:
                    units = bp(90 if w in PEAK else 40, sf[st], zf[sz], cf)
                    dem[(cl, sku, w)] = units
                    demand.append({"cluster_code": cl, "sku": sku, "iso_week": w, "units": units})

    def need(dc: str, sku: str, weeks: list[str]) -> int:
        return sum(dem[(cl, sku, w)] for cl, d, _f in CLUSTERS if d == dc for w in weeks)

    cluster_inventory = [
        {"cluster_code": cl, "sku": s["code"], "on_hand": dem[(cl, s["code"], WEEKS[0])]}
        for cl, _dc, _f in CLUSTERS
        for s in skus
    ]

    def cl_stock(dc: str, sku: str) -> int:
        return sum(dem[(cl, sku, WEEKS[0])] for cl, d, _f in CLUSTERS if d == dc)

    inventory, receipts = [], []
    for s in skus:
        sku = s["code"]
        # UK1 / NL1: cover W41-W44 (+3% buffer); BD receipts cover the peak.
        for dc in ("UK1", "NL1"):
            on_hand = bp(need(dc, sku, WEEKS[0:4]), 10300) - cl_stock(dc, sku)
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
        # DE1: own season + Cambodia PO surplus; PL1: own season + small surplus.
        shortfall = need("UK1", sku, ["2026-W45"]) + need("NL1", sku, ["2026-W45"])
        for dc, surplus_bp in (("DE1", 7000), ("PL1", 4000)):
            own = need(dc, sku, WEEKS) - cl_stock(dc, sku)
            on_hand = own + bp(shortfall, surplus_bp)
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
        for po, dc, eta, weeks in (
            ("PO-26-0101", "UK1", "2026-11-02", WEEKS[4:6]),
            ("PO-26-0102", "NL1", "2026-11-04", WEEKS[4:7]),
            ("PO-26-0103", "UK1", "2026-11-16", WEEKS[6:9]),
            ("PO-26-0104", "NL1", "2026-11-23", WEEKS[7:9]),
        ):
            receipts.append(
                {
                    "ref": f"{po}#{len([r for r in receipts if r['ref'].startswith(po)]) + 1}",
                    "dc_code": dc,
                    "sku": sku,
                    "qty": bp(need(dc, sku, weeks), 10300),
                    "eta": eta,
                }
            )
        # Cambodia PO for PL1 (not affected by the BD disruption).
        receipts.append(
            {
                "ref": f"PO-26-0106#{len([r for r in receipts if r['ref'].startswith('PO-26-0106')]) + 1}",
                "dc_code": "PL1",
                "sku": sku,
                "qty": bp(need("PL1", sku, ["2026-W46"]), 5000),
                "eta": "2026-11-09",
            }
        )

    bd_refs = sorted(
        r["ref"] for r in receipts if r["ref"].split("#")[0] in {"PO-26-0101", "PO-26-0102", "PO-26-0103", "PO-26-0104"}
    )
    request = {
        "data": {
            "tenant": "demo-retail",
            "as_of": "2026-10-05",
            "horizon": {"start_week": "2026-W41", "weeks": 9},
            "dcs": [{"code": c, "outbound_cap_units_wk": cap} for c, cap in DCS],
            "clusters": [{"code": c, "dc_code": d} for c, d, _f in CLUSTERS],
            "skus": skus,
            "inventory": inventory,
            "cluster_inventory": cluster_inventory,
            "receipts": receipts,
            "demand": demand,
            "lanes": LANES,
            "protected_min": [
                {"cluster_code": "DE1-COLD-A", "sku": "PUF-W-BLK-M", "units": 10},
                {"cluster_code": "PL1-COLD-A", "sku": "PUF-W-BLK-M", "units": 10},
            ],
        },
        "perturbations": [
            {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": bd_refs}, "days": 7, "prov": "decision"},
        ],
        "plan_spec": {
            "id": "opt-A",
            "title": "Rebalance between DCs",
            "levers": [
                {
                    "code": "reallocate_inventory",
                    "params": {"max_units_total": 20000, "allowed_lanes": ["DE1>NL1", "DE1>UK1", "PL1>NL1"]},
                }
            ],
            "weights": {"lost_margin": 10000, "transfer_cost": 10000},
        },
        "solver": {"time_limit_s": 5, "seed": 7},
    }
    out = Path(__file__).with_name("small_problem.json")
    out.write_text(json.dumps(request, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
