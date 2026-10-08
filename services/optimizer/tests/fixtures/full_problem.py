"""A ``ProblemData`` shaped like the Merxa ``full`` seed story (docs/contracts/22-merxa-m2.md §2,
``services/merxa-api/src/merxa_api/seed``), generated deterministically (integer arithmetic,
no randomness) at test time:

* 4 DCs (UK1 Thurrock, NL1 Venlo, DE1 Erfurt, PL1 Poznan), 18 clusters (DC x climate x grade
  band), 30 style-colours (15 outerwear + 15 knit, Merxa prices) x 6 sizes = **180 SKUs**,
  9 ISO weeks 2026-W41..W49 (peak W45-W48, Black Friday W48).
* **12 POs**: Bangladesh outerwear PO-BD-0001..0006 (waves A/B by sea-air via Jebel Ali
  ``AEJEA``, ETA W44/W45; wave C by sea around the Cape, ETA W46), Vietnamese knit
  PO-VN-0001..0004 (Ho Chi Minh City ``VNSGN`` -> Hamburg / Gdansk, ETA W46/W47), the
  received Cambodian PO-KH-0001 (DE1) and Turkish PO-TR-0001 (NL1) (open qty 0).
* BOM as Merxa (TRIM-BD-01 wadding + zips at F-BD-01/02; Saigon Knit yarn/fleece at F-VN-01).
* Alternate sources: F-TR-01 Istanbul (-> NL1 outerwear+knit, -> DE1 knit), F-MA-01 Tanger
  (-> UK1 outerwear), plus a non-compliant F-MA-01 -> NL1 row that must never be used.
* 3 factory lines (FL-BD01-A, FL-BD02-A, FL-VN01-A) with one production order per PO line;
  wave B and VN wave 2 orders are planned (movable), earlier waves running/done.
* Planned cluster allocations for UK1/NL1 outerwear in W45/W46 (gilets at A-grade clusters
  protected)
  and protected minimums for A-grade clusters' core sizes.

Story invariants (asserted by ``tests/test_full_options.py``): baseline 0 units at risk; a
+7-day delay of the TRIM-BD-01 trims (BD lines) leaves UK1/NL1 short in W45-W46 while DE1/PL1
keep transferable surplus; a 10-day closure of VNSGN leaves DE1/PL1 short of knit from W46.
"""

from __future__ import annotations

import copy
import datetime as dt
from functools import cache
from typing import Any

WEEKS = [f"2026-W{w}" for w in range(41, 50)]
PEAK_BP = {"2026-W45": 18000, "2026-W46": 20000, "2026-W47": 20000, "2026-W48": 24000}
SIZES = ("XS", "S", "M", "L", "XL", "XXL")
CORE = ("M", "L")
TIGHT = ("XS", "XXL")
CURVE_BP = {"O": (600, 1600, 2800, 2600, 1600, 800), "K": (700, 1700, 2700, 2500, 1600, 800)}
CLIMATE_BP = {"O": {"COLD": 11500, "MILD": 10000, "WARM": 7500}, "K": {"COLD": 10500, "MILD": 10000, "WARM": 9000}}
UNITS_K = 6

DCS = [
    ("UK1", 110000, 51493500, 352900),
    ("NL1", 100000, 51370400, 6172400),
    ("DE1", 85000, 50984800, 11029900),
    ("PL1", 55000, 52406400, 16925200),
]
# cluster code, dc, weight (store index)
CLUSTERS = [
    ("UK1-COLD-A", "UK1", 14), ("UK1-COLD-BC", "UK1", 10), ("UK1-MILD-A", "UK1", 16), ("UK1-MILD-BC", "UK1", 12),
    ("NL1-COLD-A", "NL1", 5), ("NL1-COLD-BC", "NL1", 4), ("NL1-MILD-A", "NL1", 10), ("NL1-MILD-BC", "NL1", 8),
    ("NL1-WARM-A", "NL1", 9), ("NL1-WARM-BC", "NL1", 8),
    ("DE1-COLD-A", "DE1", 8), ("DE1-COLD-BC", "DE1", 6), ("DE1-MILD-A", "DE1", 7), ("DE1-MILD-BC", "DE1", 6),
    ("PL1-COLD-A", "PL1", 6), ("PL1-COLD-BC", "PL1", 6), ("PL1-MILD-A", "PL1", 3), ("PL1-MILD-BC", "PL1", 3),
]  # fmt: skip
# Merxa reference_full.STYLES: code, retail, cost, popularity x100, family
STYLES = [
    ("PUF-W-BLK", 3500, 1150, 130, "O"), ("PUF-W-CRM", 3500, 1150, 80, "O"), ("PUF-W-KHK", 2800, 900, 90, "O"),
    ("PUF-M-BLK", 3200, 1050, 120, "O"), ("PUF-M-NVY", 3200, 1050, 100, "O"), ("PUF-W-SGE", 2800, 900, 70, "O"),
    ("PUF-W-PNK", 3500, 1150, 60, "O"), ("PUF-M-OLV", 3200, 1050, 80, "O"), ("PRK-W-BLK", 4000, 1350, 100, "O"),
    ("PRK-W-CML", 4000, 1350, 70, "O"), ("PRK-M-BLK", 4200, 1400, 90, "O"), ("PRK-M-KHK", 4200, 1400, 80, "O"),
    ("PRK-M-NVY", 4200, 1400, 60, "O"), ("GIL-M-BLK", 1800, 600, 70, "O"), ("GIL-W-NVY", 1800, 600, 60, "O"),
    ("FLC-W-GRY", 1500, 450, 120, "K"), ("FLC-W-CRM", 1500, 450, 100, "K"), ("FLC-W-PNK", 1500, 450, 70, "K"),
    ("FLC-M-NVY", 1400, 420, 110, "K"), ("FLC-M-BLK", 1400, 420, 100, "K"), ("FLC-M-GRN", 1400, 420, 60, "K"),
    ("KNT-W-OAT", 1600, 500, 110, "K"), ("KNT-W-BRG", 1600, 500, 80, "K"), ("KNT-W-BLK", 1600, 500, 90, "K"),
    ("KNT-M-NVY", 1500, 460, 100, "K"), ("KNT-M-GRY", 1500, 460, 90, "K"), ("KNT-M-ECR", 1500, 460, 60, "K"),
    ("CRD-W-CML", 1800, 560, 70, "K"), ("CRD-W-GRN", 1800, 560, 50, "K"), ("HDY-U-BLK", 1300, 380, 100, "K"),
]  # fmt: skip
W45_COVERED = {"PUF-W-CRM", "PUF-M-NVY", "PRK-W-CML", "PRK-M-NVY", "GIL-W-NVY"}

# Bangladesh POs: code, dc, factory, eta, wave, inbound lane
BD_POS = [
    ("PO-BD-0001", "UK1", "F-BD-01", "2026-10-29", 0, "CGP-UK1-SA"),
    ("PO-BD-0002", "UK1", "F-BD-02", "2026-11-03", 1, "CGP-UK1-SA"),
    ("PO-BD-0003", "UK1", "F-BD-01", "2026-11-10", 2, "CGP-FXT-SEA"),
    ("PO-BD-0004", "NL1", "F-BD-02", "2026-10-29", 0, "CGP-NL1-SA"),
    ("PO-BD-0005", "NL1", "F-BD-01", "2026-11-03", 1, "CGP-NL1-SA"),
    ("PO-BD-0006", "NL1", "F-BD-02", "2026-11-10", 2, "CGP-RTM-SEA"),
]
VN_POS = [
    ("PO-VN-0001", "DE1", "2026-11-11", 0, "SGN-HAM-SEA"),
    ("PO-VN-0002", "PL1", "2026-11-12", 0, "SGN-GDN-SEA"),
    ("PO-VN-0003", "PL1", "2026-11-19", 1, "SGN-GDN-SEA"),
    ("PO-VN-0004", "DE1", "2026-11-20", 1, "SGN-HAM-SEA"),
]
INBOUND_LANES = [
    # code, origin, dest_dc, mode, transit days, cost/unit, weekly capacity, via, alt
    ("CGP-FXT-SEA", "BDCGP", "UK1", "sea", 36, 25, 60000, ["LKCMB", "CAPE", "GBFXT"], "CGP-UK1-SA"),
    ("CGP-RTM-SEA", "BDCGP", "NL1", "sea", 36, 24, 60000, ["LKCMB", "CAPE", "NLRTM"], "CGP-NL1-SA"),
    ("CGP-UK1-SA", "BDCGP", "UK1", "sea_air", 16, 185, 12000, ["AEJEA"], "CGP-FXT-SEA"),
    ("CGP-NL1-SA", "BDCGP", "NL1", "sea_air", 16, 180, 12000, ["AEJEA"], "CGP-RTM-SEA"),
    ("SGN-HAM-SEA", "VNSGN", "DE1", "sea", 36, 22, 50000, ["CAPE", "DEHAM"], "SGN-DE1-SA"),
    ("SGN-GDN-SEA", "VNSGN", "PL1", "sea", 37, 23, 45000, ["CAPE", "PLGDN"], "SGN-PL1-SA"),
    ("SGN-DE1-SA", "VNSGN", "DE1", "sea_air", 15, 200, 10000, ["AEJEA"], "SGN-HAM-SEA"),
    ("SGN-PL1-SA", "VNSGN", "PL1", "sea_air", 16, 205, 10000, ["AEJEA"], "SGN-GDN-SEA"),
    ("IST-DE1-RD", "TRIST", "DE1", "road", 5, 55, 15000, [], None),
    ("IST-NL1-RD", "TRIST", "NL1", "road", 6, 62, 15000, [], None),
    ("TNG-NL1-RD", "MATNG", "NL1", "road", 5, 58, 15000, [], None),
    ("TNG-UK1-RD", "MATNG", "UK1", "road", 6, 70, 12000, [], None),
]
DC_LANES = [  # infra/tenants.yaml
    {"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000},
    {"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 12000},
    {"from_dc": "PL1", "to_dc": "NL1", "transit_days": 3, "cost_per_unit_cents": 45, "max_units_wk": 15000},
    {"from_dc": "PL1", "to_dc": "DE1", "transit_days": 2, "cost_per_unit_cents": 30, "max_units_wk": 15000},
    {"from_dc": "NL1", "to_dc": "UK1", "transit_days": 2, "cost_per_unit_cents": 50, "max_units_wk": 15000},
]
PORTS = [
    ("BDCGP", "Chittagong", 22308900, 91800000), ("LKCMB", "Colombo", 6949700, 79842800),
    ("VNSGN", "Ho Chi Minh City (Cat Lai)", 10762600, 106788000), ("NLRTM", "Rotterdam", 51949600, 4145300),
    ("GBFXT", "Felixstowe", 51956300, 1330100), ("DEHAM", "Hamburg", 53539500, 9966300),
    ("PLGDN", "Gdansk", 54401300, 18676300), ("AEJEA", "Jebel Ali", 25011500, 55061800),
    ("TRIST", "Istanbul (Ambarli)", 40966700, 28683300), ("MATNG", "Tanger Med", 35883300, -5500000),
]  # fmt: skip
FACTORIES = [
    ("F-BD-01", "CMT-BD-01", 24000000, 90400000), ("F-BD-02", "CMT-BD-01", 23620000, 90500000),
    ("F-KH-01", "CMT-KH-01", 11556000, 104928000), ("F-VN-01", "SUP-VN-01", 10850000, 106760000),
    ("F-TR-01", "CMT-TR-01", 41030000, 28650000), ("F-MA-01", "CMT-MA-01", 35760000, -5830000),
]  # fmt: skip
BOM_O = [("CMP-SHL-01", "FAB-CN-01", None), ("CMP-LIN-01", "FAB-CN-01", None), ("CMP-LBL-01", "FAB-CN-01", None),
         ("CMP-WAD-01", "TRIM-BD-01", "F-BD-01"), ("CMP-ZIP-01", "TRIM-BD-01", "F-BD-01"),
         ("CMP-WAD-01", "TRIM-BD-01", "F-BD-02"), ("CMP-ZIP-01", "TRIM-BD-01", "F-BD-02"),
         ("CMP-WAD-02", "TRIM-KH-01", "F-KH-01"), ("CMP-ZIP-02", "TRIM-KH-01", "F-KH-01"),
         ("CMP-WAD-03", "TRIM-TR-01", "F-TR-01"), ("CMP-ZIP-03", "TRIM-TR-01", "F-TR-01"),
         ("CMP-WAD-03", "TRIM-TR-01", "F-MA-01"), ("CMP-ZIP-03", "TRIM-TR-01", "F-MA-01")]  # fmt: skip
BOM_K = [("CMP-LBL-01", "FAB-CN-01", None), ("CMP-YRN-01", "SUP-VN-01", "F-VN-01"),
         ("CMP-FLC-01", "SUP-VN-01", "F-VN-01"), ("CMP-YRN-02", "CMT-TR-01", "F-TR-01"),
         ("CMP-YRN-02", "CMT-TR-01", "F-MA-01")]  # fmt: skip


def _bp(x: int, bp: int) -> int:
    return (x * bp + 5000) // 10000


def _date(s: str, days: int = 0) -> str:
    return (dt.date.fromisoformat(s) + dt.timedelta(days=days)).isoformat()


@cache
def _full() -> dict[str, Any]:
    skus: list[dict[str, Any]] = []
    fam: dict[str, str] = {}
    for code, price, cost, _pop, family in STYLES:
        for sz in SIZES:
            skus.append(
                {"code": f"{code}-{sz}", "style_code": code, "retail_price_cents": price, "unit_cost_cents": cost}
            )
            fam[f"{code}-{sz}"] = family
    pop = {s[0]: s[3] for s in STYLES}
    outer = [s["code"] for s in skus if fam[s["code"]] == "O"]
    knit = [s["code"] for s in skus if fam[s["code"]] == "K"]

    dem: dict[tuple[str, str, str], int] = {}
    demand: list[dict[str, Any]] = []
    for cl, _dc, weight in CLUSTERS:
        climate = cl.split("-")[1]
        for s in skus:
            f = fam[s["code"]]
            size_bp = CURVE_BP[f][SIZES.index(s["code"].rsplit("-", 1)[1])]
            for w in WEEKS:
                units = (
                    weight * pop[s["style_code"]] * size_bp * CLIMATE_BP[f][climate] * PEAK_BP.get(w, 10000) * UNITS_K
                ) // (100 * 10000**3)
                dem[(cl, s["code"], w)] = units
                if units:
                    demand.append({"cluster_code": cl, "sku": s["code"], "iso_week": w, "units": units})

    def need(dc: str, sku: str, weeks: list[str]) -> int:
        return sum(dem[(cl, sku, w)] for cl, d, _w in CLUSTERS if d == dc for w in weeks)

    cluster_inventory = [
        {"cluster_code": cl, "sku": s["code"], "on_hand": dem[(cl, s["code"], WEEKS[0])]}
        for cl, _dc, _w in CLUSTERS
        for s in skus
        if dem[(cl, s["code"], WEEKS[0])]
    ]
    protected_min: list[dict[str, Any]] = []
    pmin_dc: dict[tuple[str, str], int] = {}
    for cl, dc, _w in CLUSTERS:
        if not cl.endswith("-A"):
            continue
        for sku in outer:
            if sku.rsplit("-", 1)[1] in CORE and (u := dem[(cl, sku, WEEKS[0])] // 4):
                protected_min.append({"cluster_code": cl, "sku": sku, "units": u})
                pmin_dc[(dc, sku)] = pmin_dc.get((dc, sku), 0) + u

    inventory: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    po_lines: list[dict[str, Any]] = []

    def stock(dc: str, sku: str, on_hand: int) -> None:
        inventory.append(
            {"location_type": "dc", "location_code": dc, "sku": sku, "on_hand": on_hand, "allocated": on_hand // 10}
        )

    bd_qty: dict[tuple[str, str], tuple[int, int, int]] = {}
    for sku in outer:
        style = sku.rsplit("-", 1)[0]
        size = sku.rsplit("-", 1)[1]
        for dc in ("UK1", "NL1"):
            d44, d45, d46 = (need(dc, sku, [w]) for w in WEEKS[3:6])
            stock(dc, sku, need(dc, sku, WEEKS[1:4]) + _bp(d44, 500) + pmin_dc.get((dc, sku), 0))
            if style in W45_COVERED:
                qa, qb = _bp(d45, 10500), _bp(d46, 3000)
            else:
                qa = _bp(d45, 6000)
                qb = max(1, d45 - qa) + _bp(d46, 3000)
            qc = max(1, d46 - _bp(d46, 3000)) + need(dc, sku, WEEKS[6:]) + _bp(need(dc, sku, [WEEKS[-1]]), 1000)
            bd_qty[(dc, sku)] = (max(1, qa), max(1, qb), qc)
        for dc in ("DE1", "PL1"):
            ratio = 10400 if size in TIGHT else 12800
            stock(dc, sku, _bp(need(dc, sku, WEEKS[1:]), ratio) + pmin_dc.get((dc, sku), 0))
    vn_qty: dict[tuple[str, str], tuple[int, int]] = {}
    for sku in knit:
        for dc in ("UK1", "NL1"):
            stock(dc, sku, _bp(need(dc, sku, WEEKS[1:]), 11200))
        for dc in ("DE1", "PL1"):
            d45, d46, d47 = (need(dc, sku, [w]) for w in WEEKS[4:7])
            stock(dc, sku, need(dc, sku, WEEKS[1:5]) + _bp(d45, 3000))
            q1 = max(1, d46 + _bp(d47, 5000) - _bp(d45, 3000))
            q2 = max(1, d47 - _bp(d47, 5000) + need(dc, sku, WEEKS[7:]) + _bp(need(dc, sku, [WEEKS[-1]]), 1000))
            vn_qty[(dc, sku)] = (q1, q2)

    lane_transit = {c[0]: c[4] for c in INBOUND_LANES}
    for po, dc, factory, eta, wave, lane in BD_POS:
        ex_factory = "2026-10-05" if wave == 2 else _date(eta, -(lane_transit[lane] + 1))
        for n, sku in enumerate(outer, start=1):
            qty = bd_qty[(dc, sku)][wave]
            ref = f"{po}#{n}"
            receipts.append({"ref": ref, "dc_code": dc, "sku": sku, "qty": qty, "eta": eta})
            po_lines.append(
                {"ref": ref, "po_code": po, "supplier_code": "CMT-BD-01", "factory_code": factory, "dest_dc": dc,
                 "sku": sku, "qty": qty, "eta": eta, "ex_factory": ex_factory,
                 "mode": "sea" if wave == 2 else "sea_air", "lane_code": lane}
            )  # fmt: skip
    for po, dc, eta, wave, lane in VN_POS:
        ex_factory = _date(eta, -lane_transit[lane])
        for n, sku in enumerate(knit, start=1):
            qty = vn_qty[(dc, sku)][wave]
            ref = f"{po}#{n}"
            receipts.append({"ref": ref, "dc_code": dc, "sku": sku, "qty": qty, "eta": eta})
            po_lines.append(
                {"ref": ref, "po_code": po, "supplier_code": "SUP-VN-01", "factory_code": "F-VN-01", "dest_dc": dc,
                 "sku": sku, "qty": qty, "eta": eta, "ex_factory": ex_factory, "mode": "sea", "lane_code": lane}
            )  # fmt: skip
    for po, sup, fac, dc, eta, skus_, mode in (
        ("PO-KH-0001", "CMT-KH-01", "F-KH-01", "DE1", "2026-09-29", outer, "sea"),
        ("PO-TR-0001", "CMT-TR-01", "F-TR-01", "NL1", "2026-09-22", knit, "road"),
    ):
        for n, sku in enumerate(skus_, start=1):
            po_lines.append(
                {"ref": f"{po}#{n}", "po_code": po, "supplier_code": sup, "factory_code": fac, "dest_dc": dc,
                 "sku": sku, "qty": 0, "eta": eta, "ex_factory": _date(eta, -30), "mode": mode, "lane_code": None}
            )  # fmt: skip

    production = _production(po_lines)
    allocations = [
        {"code": f"AL-{dc}-{w[-3:]}-{cl.split('-', 1)[1]}-{sku}", "sku": sku, "dc_code": dc, "cluster_code": cl,
         "ship_week": w, "qty": dem[(cl, sku, w)], "protected": cl.endswith("-A") and sku.startswith("GIL-"),
         "version": 1}
        for w in ("2026-W45", "2026-W46")
        for cl, dc, _w in CLUSTERS
        if dc in ("UK1", "NL1")
        for sku in outer
        if dem[(cl, sku, w)]
    ]  # fmt: skip

    locations = [
        {"code": dc, "kind": "dc", "name": f"{dc} DC", "lat_e6": lat, "lon_e6": lon} for dc, _c, lat, lon in DCS
    ]
    dc_pos = {dc: (lat, lon) for dc, _c, lat, lon in DCS}
    for i, (cl, dc, _w) in enumerate(CLUSTERS):
        lat, lon = dc_pos[dc]
        lat_c, lon_c = lat + 150000 * (i % 3), lon - 120000 * (i % 4)
        locations.append({"code": cl, "kind": "cluster", "name": cl, "lat_e6": lat_c, "lon_e6": lon_c, "parent": dc})
    locations += [{"code": c, "kind": "port", "name": n, "lat_e6": la, "lon_e6": lo} for c, n, la, lo in PORTS]
    locations += [
        {"code": f, "kind": "factory", "name": f, "lat_e6": la, "lon_e6": lo, "parent": s} for f, s, la, lo in FACTORIES
    ]

    alt_sources = [
        {"supplier_code": "CMT-MA-01", "factory_code": "F-MA-01", "style_codes": [s[0] for s in STYLES if s[4] == "O"],
         "lead_time_days": 12, "unit_cost_cents": 1500, "moq_units": 1500, "weekly_capacity_units": 6000,
         "approved": True, "compliance_ok": True, "lane_code": "TNG-UK1-RD"},
        {"supplier_code": "CMT-MA-01", "factory_code": "F-MA-01", "style_codes": [s[0] for s in STYLES if s[4] == "O"],
         "lead_time_days": 10, "unit_cost_cents": 1200, "moq_units": 500, "weekly_capacity_units": 9000,
         "approved": True, "compliance_ok": False, "lane_code": "TNG-NL1-RD"},
        {"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "style_codes": [s[0] for s in STYLES],
         "lead_time_days": 14, "unit_cost_cents": 1450, "moq_units": 2000, "weekly_capacity_units": 8000,
         "approved": True, "compliance_ok": True, "lane_code": "IST-NL1-RD"},
        {"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "style_codes": [s[0] for s in STYLES if s[4] == "K"],
         "lead_time_days": 14, "unit_cost_cents": 650, "moq_units": 1200, "weekly_capacity_units": 6000,
         "approved": True, "compliance_ok": True, "lane_code": "IST-DE1-RD"},
    ]  # fmt: skip
    bom = [
        {"style_code": s[0], "component_code": c, "supplier_code": sup, "factory_code": f}
        for s in STYLES
        for c, sup, f in (BOM_O if s[4] == "O" else BOM_K)
    ]
    return {
        "tenant": "demo-retail",
        "as_of": "2026-10-05",
        "horizon": {"start_week": WEEKS[0], "weeks": len(WEEKS)},
        "dcs": [{"code": dc, "outbound_cap_units_wk": cap} for dc, cap, _la, _lo in DCS],
        "clusters": [{"code": cl, "dc_code": dc} for cl, dc, _w in CLUSTERS],
        "skus": skus,
        "inventory": inventory,
        "cluster_inventory": cluster_inventory,
        "receipts": receipts,
        "demand": demand,
        "lanes": DC_LANES,
        "protected_min": protected_min,
        "locations": locations,
        "po_lines": po_lines,
        "bom": bom,
        "inbound_lanes": [
            {
                "code": c,
                "origin": o,
                "dest_dc": d,
                "mode": m,
                "transit_days": t,
                "cost_per_unit_cents": cost,
                "weekly_capacity_units": cap,
                "via": via,
                "alt_lane_code": alt,
            }
            for c, o, d, m, t, cost, cap, via, alt in INBOUND_LANES
        ],
        "alt_sources": alt_sources,
        "production": production,
        "allocations": allocations,
    }


def _production(po_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One order per open PO line: FL-BD01-A (F-BD-01), FL-BD02-A (F-BD-02), FL-VN01-A.
    Each PO is produced over the 9 days before its ex-factory date at the line's daily
    capacity; orders of waves already started (ex-factory before 2026-10-16) are fixed."""
    lines: dict[str, list[dict[str, Any]]] = {"FL-BD01-A": [], "FL-BD02-A": [], "FL-VN01-A": []}
    line_of = {"F-BD-01": "FL-BD01-A", "F-BD-02": "FL-BD02-A", "F-VN-01": "FL-VN01-A"}
    by_po: dict[str, list[dict[str, Any]]] = {}
    for pl in po_lines:
        if pl["qty"] and pl["factory_code"] in line_of:
            by_po.setdefault(pl["po_code"], []).append(pl)
    caps: dict[str, int] = {}
    for po in sorted(by_po, key=lambda p: (by_po[p][0]["ex_factory"], p)):
        rows = by_po[po]
        code = line_of[rows[0]["factory_code"]]
        total = sum(r["qty"] for r in rows)
        cap = caps.setdefault(code, max(1, total // 9))
        end = dt.date.fromisoformat(rows[0]["ex_factory"])
        movable = end >= dt.date(2026, 10, 13)
        start = end - dt.timedelta(days=9)
        done = 0
        for r in rows:
            s = start + dt.timedelta(days=done // cap)
            done += r["qty"]
            e = max(s, start + dt.timedelta(days=(done - 1) // cap))
            seq = len(lines[code]) + 1
            lines[code].append(
                {"code": f"MO-{code[3:]}-{seq:04d}", "po_line_ref": r["ref"], "sku": r["sku"], "qty": r["qty"],
                 "planned_start": s.isoformat(), "planned_end": e.isoformat(), "seq": seq, "movable": movable}
            )  # fmt: skip
    factory = {v: k for k, v in line_of.items()}
    return [
        {"line_code": code, "factory_code": factory[code], "daily_capacity_units": caps.get(code, 0), "orders": orders}
        for code, orders in sorted(lines.items())
    ]


def full_data() -> dict[str, Any]:
    return copy.deepcopy(_full())


# ── perturbations of the story ──
BD_TRIMS_DELAY = {
    "op": "lead_time_shift",
    "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]},
    "days": 7,
    "prov": "decision",
}
VN_PORT_CLOSURE = {
    "op": "capacity_scale",
    "target": {"kinds": ["port"], "ids": ["VNSGN"]},
    "factor_bp": 0,
    "window": {"start": "2026-10-06", "end": "2026-10-15"},
    "prov": "fact",
}
CAPE_CLOSURE = {
    "op": "capacity_scale",
    "target": {"kinds": ["chokepoint"], "ids": ["CAPE"]},
    "factor_bp": 0,
    "prov": "fact",
}

OPTIONS: dict[str, list[dict[str, Any]]] = {
    "A": [{"code": "reallocate_inventory"}],
    "B": [{"code": "reallocate_inventory"}, {"code": "expedite_mode_shift"}],
    "C": [{"code": "resource_alt_supplier"}, {"code": "resequence_production"}],
    "D": [
        {"code": "reallocate_inventory"},
        {"code": "expedite_mode_shift"},
        {"code": "resource_alt_supplier"},
        {"code": "prioritize_allocation"},
        {"code": "resequence_production"},
    ],
}


def full_request(
    levers: list[dict[str, Any]], perturbations: list[dict[str, Any]] | None = None, plan_id: str = "opt"
) -> dict[str, Any]:
    return {
        "data": full_data(),
        "perturbations": copy.deepcopy([BD_TRIMS_DELAY] if perturbations is None else perturbations),
        "plan_spec": {"id": plan_id, "title": plan_id, "levers": copy.deepcopy(levers)},
        "solver": {"time_limit_s": 5, "seed": 7},
    }
