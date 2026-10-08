"""A small hand-made network problem (PO lines, BOM, inbound lanes, alternate source,
factory line) with numbers simple enough to reason about in tests.

* Horizon 2026-W41..W48, as_of Monday 2026-10-05. DCs UK1 (clusters UK1-A, UK1-B) and DE1
  (DE1-A). SKUs TOP-M (style TOP, 3000 / 1000 cents) and KNIT-M (style KNIT, 2000 / 800).
* Demand per week: UK1-A 60 TOP, UK1-B 40 TOP, DE1-A 30 KNIT. Cluster stock covers W41, DC
  stock W42-W43; PO-1#1 (500 TOP -> UK1, eta Mon 2026-10-26 = W44, lane SEA-UK via CAPE)
  and PO-2#1 (150 KNIT -> DE1, eta W44, lane SEA-DE via SUEZ) cover W44-W48 exactly.
* BOM: TOP uses CMP-Z (TRIM-1, only at F-1) and CMP-L (TRIM-2, every factory); KNIT uses
  CMP-Y (TRIM-3, at F-2).
* Lanes: SEA-UK (PORT-A -> UK1, sea, 31 d, alt SEA-UK-2), SEA-UK-2 (PORT-A -> UK1, sea, 41 d
  via SUEZ), SA-UK (PORT-A -> UK1, sea-air, 14 d via HUB, 300 units/week, 150 c/unit),
  SEA-DE (PORT-B -> DE1, sea, 31 d via SUEZ), NS-UK (PORT-N -> UK1, road, 4 d).
* Alternate source F-N (SUP-N) for TOP via NS-UK: lead 7 d, 1300 c/unit, MOQ 200,
  1000 units/week.
"""

from __future__ import annotations

import copy
from typing import Any

WEEKS = [f"2026-W{w}" for w in range(41, 49)]


def network_problem(**over: Any) -> dict[str, Any]:
    demand = []
    for cl, sku, units in (("UK1-A", "TOP-M", 60), ("UK1-B", "TOP-M", 40), ("DE1-A", "KNIT-M", 30)):
        demand += [{"cluster_code": cl, "sku": sku, "iso_week": w, "units": units} for w in WEEKS]
    data: dict[str, Any] = {
        "tenant": "demo-retail",
        "as_of": "2026-10-05",
        "horizon": {"start_week": WEEKS[0], "weeks": len(WEEKS)},
        "dcs": [{"code": "UK1"}, {"code": "DE1"}],
        "clusters": [
            {"code": "UK1-A", "dc_code": "UK1"},
            {"code": "UK1-B", "dc_code": "UK1"},
            {"code": "DE1-A", "dc_code": "DE1"},
        ],
        "skus": [
            {"code": "TOP-M", "style_code": "TOP", "retail_price_cents": 3000, "unit_cost_cents": 1000},
            {"code": "KNIT-M", "style_code": "KNIT", "retail_price_cents": 2000, "unit_cost_cents": 800},
        ],
        "inventory": [
            {"location_type": "dc", "location_code": "UK1", "sku": "TOP-M", "on_hand": 200},
            {"location_type": "dc", "location_code": "DE1", "sku": "KNIT-M", "on_hand": 60},
        ],
        "cluster_inventory": [
            {"cluster_code": "UK1-A", "sku": "TOP-M", "on_hand": 60},
            {"cluster_code": "UK1-B", "sku": "TOP-M", "on_hand": 40},
            {"cluster_code": "DE1-A", "sku": "KNIT-M", "on_hand": 30},
        ],
        "receipts": [
            {"ref": "PO-1#1", "dc_code": "UK1", "sku": "TOP-M", "qty": 500, "eta": "2026-10-26"},
            {"ref": "PO-2#1", "dc_code": "DE1", "sku": "KNIT-M", "qty": 150, "eta": "2026-10-26"},
        ],
        "demand": demand,
        "po_lines": [
            {
                "ref": "PO-1#1",
                "po_code": "PO-1",
                "supplier_code": "SUP-1",
                "factory_code": "F-1",
                "dest_dc": "UK1",
                "sku": "TOP-M",
                "qty": 500,
                "eta": "2026-10-26",
                "ex_factory": "2026-09-25",
                "mode": "sea",
                "lane_code": "SEA-UK",
            },
            {
                "ref": "PO-2#1",
                "po_code": "PO-2",
                "supplier_code": "SUP-2",
                "factory_code": "F-2",
                "dest_dc": "DE1",
                "sku": "KNIT-M",
                "qty": 150,
                "eta": "2026-10-26",
                "ex_factory": "2026-09-25",
                "mode": "sea",
                "lane_code": "SEA-DE",
            },
        ],
        "bom": [
            {"style_code": "TOP", "component_code": "CMP-Z", "supplier_code": "TRIM-1", "factory_code": "F-1"},
            {"style_code": "TOP", "component_code": "CMP-L", "supplier_code": "TRIM-2", "factory_code": None},
            {"style_code": "KNIT", "component_code": "CMP-Y", "supplier_code": "TRIM-3", "factory_code": "F-2"},
        ],
        "inbound_lanes": [
            {
                "code": "SEA-UK",
                "origin": "PORT-A",
                "dest_dc": "UK1",
                "mode": "sea",
                "transit_days": 31,
                "cost_per_unit_cents": 20,
                "weekly_capacity_units": 10000,
                "via": ["CAPE", "PORT-UK"],
                "alt_lane_code": "SEA-UK-2",
            },
            {
                "code": "SEA-UK-2",
                "origin": "PORT-A",
                "dest_dc": "UK1",
                "mode": "sea",
                "transit_days": 41,
                "cost_per_unit_cents": 22,
                "weekly_capacity_units": 10000,
                "via": ["SUEZ", "PORT-UK"],
            },
            {
                "code": "SA-UK",
                "origin": "PORT-A",
                "dest_dc": "UK1",
                "mode": "sea_air",
                "transit_days": 14,
                "cost_per_unit_cents": 150,
                "weekly_capacity_units": 300,
                "via": ["HUB"],
            },
            {
                "code": "SEA-DE",
                "origin": "PORT-B",
                "dest_dc": "DE1",
                "mode": "sea",
                "transit_days": 31,
                "cost_per_unit_cents": 20,
                "weekly_capacity_units": 10000,
                "via": ["SUEZ"],
            },
            {
                "code": "NS-UK",
                "origin": "PORT-N",
                "dest_dc": "UK1",
                "mode": "road",
                "transit_days": 4,
                "cost_per_unit_cents": 40,
                "weekly_capacity_units": 10000,
                "via": [],
            },
        ],
        "alt_sources": [
            {
                "supplier_code": "SUP-N",
                "factory_code": "F-N",
                "style_codes": ["TOP"],
                "lead_time_days": 7,
                "unit_cost_cents": 1300,
                "moq_units": 200,
                "weekly_capacity_units": 1000,
                "approved": True,
                "compliance_ok": True,
                "lane_code": "NS-UK",
            },
        ],
    }
    data.update(copy.deepcopy(over))
    return data


TRIM_DELAY = {"op": "lead_time_shift", "target": {"kinds": ["supplier"], "ids": ["TRIM-1"]}, "days": 14, "prov": "fact"}


def request(
    levers: list[dict[str, Any]], perturbations: list[dict[str, Any]] | None = None, **data_over: Any
) -> dict[str, Any]:
    return {
        "data": network_problem(**data_over),
        "perturbations": copy.deepcopy([TRIM_DELAY] if perturbations is None else perturbations),
        "plan_spec": {"id": "opt-net", "title": "network", "levers": copy.deepcopy(levers)},
        "solver": {"time_limit_s": 5, "seed": 3},
    }
