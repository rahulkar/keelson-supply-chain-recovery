"""Impact simulation: sharing rule, KPIs, timeline and the small-seed story."""

from __future__ import annotations

import datetime as dt
from typing import Any

from keelson_contracts.optimizer import ImpactRequest, ProblemData

from keelson_opt.impact import kpis, ration, simulate, timeline
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_impact


def test_ration_is_pro_rata_with_largest_remainder() -> None:
    assert ration(100, [10, 20, 30]) == [10, 20, 30]  # enough supply: requests filled
    assert ration(0, [5, 5]) == [0, 0]
    assert ration(10, [1, 1, 1]) == [1, 1, 1]
    # 7 units over requests 3:3:4 -> 2.1, 2.1, 2.8 -> floors 2,2,2 + 1 to the largest remainder.
    assert ration(7, [3, 3, 4]) == [2, 2, 3]
    # Equal remainders: tie broken by position (= sorted cluster code).
    assert ration(1, [1, 1]) == [1, 0]
    assert sum(ration(97, [13, 41, 7, 66])) == 97


def two_cluster_problem() -> dict[str, Any]:
    return {
        "tenant": "t1",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 3},
        "dcs": [{"code": "D"}],
        "clusters": [{"code": "D-A", "dc_code": "D"}, {"code": "D-B", "dc_code": "D"}],
        "skus": [{"code": "S1", "style_code": "ST", "retail_price_cents": 2000, "unit_cost_cents": 500}],
        "inventory": [{"location_type": "dc", "location_code": "D", "sku": "S1", "on_hand": 30}],
        "cluster_inventory": [{"cluster_code": "D-A", "sku": "S1", "on_hand": 10}],
        "receipts": [{"ref": "PO#1", "dc_code": "D", "sku": "S1", "qty": 100, "eta": "2026-10-21"}],
        "demand": [
            {"cluster_code": c, "sku": "S1", "iso_week": w, "units": u}
            for c, w, u in [
                ("D-A", "2026-W41", 20),
                ("D-B", "2026-W41", 30),
                ("D-A", "2026-W42", 20),
                ("D-B", "2026-W42", 20),
                ("D-A", "2026-W43", 20),
                ("D-B", "2026-W43", 20),
            ]
        ],
        "protected_min": [{"cluster_code": "D-B", "sku": "S1", "units": 5}],
    }


def test_sharing_rule_hand_computed() -> None:
    scn = build_scenario(ProblemData.model_validate(two_cluster_problem()))
    res = simulate(scn)
    a, b = res.series[("D-A", "S1")], res.series[("D-B", "S1")]
    # W41: requests A = 20 - 10 = 10, B = 30 + 5 = 35; pool 30 < 45 -> 30*10/45 = 6.67,
    # 30*35/45 = 23.33 -> floors 6, 23, leftover 1 to A (remainder .67 > .33).
    assert (a.pab[0], b.pab[0]) == (10 + 7 - 20, 23 - 30)
    assert (a.lost[0], b.lost[0]) == (3, 7)
    assert a.short[0] and b.short[0]
    # W42: pool empty, receipt (eta W43) not yet in -> everything lost.
    assert (a.lost[1], b.lost[1]) == (20, 20)
    assert b.gap[1] == 5 + 20  # protected_min - PAB
    # W43: receipt 100 -> requests 20 and 25 filled; B keeps its protected minimum.
    assert (a.pab[2], b.pab[2]) == (0, 5)
    assert not a.short[2] and not b.short[2]
    assert res.dc_end[("D", "S1")] == [0, 0, 55]

    k = kpis(res)
    assert k.units_at_risk == 3 + 7 + 20 + 20
    assert k.revenue_at_risk_cents == 50 * 2000
    assert k.margin_at_risk_cents == 50 * 1500
    assert k.shortage_cluster_weeks == 4
    assert [(b.cluster_code, b.week, b.date) for b in k.first_breach] == [
        ("D-A", "2026-W41", dt.date(2026, 10, 5)),
        ("D-B", "2026-W41", dt.date(2026, 10, 5)),
    ]


def test_network_conservation_in_simulation(small_raw: dict[str, Any]) -> None:
    req = ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"])
    scn = build_scenario(req.data, req.perturbations)
    res = simulate(scn)
    for sku in scn.skus:
        supply = sum(scn.dc_supply.get((d, sku), 0) + sum(scn.receipts.get((d, sku), [0])) for d in scn.dcs)
        supply += sum(scn.cluster_on_hand.get((c, sku), 0) for c in scn.clusters)
        served = sum(sum(cs.demand) - sum(cs.lost) for (c, k), cs in res.series.items() if k == sku)
        end_clusters = sum(max(cs.pab[-1], 0) for (c, k), cs in res.series.items() if k == sku)
        end_dcs = sum(res.dc_end[(d, sku)][-1] for d in scn.dcs)
        assert supply == served + end_clusters + end_dcs, sku
        assert all(v >= 0 for d in scn.dcs for v in res.dc_end[(d, sku)])


def test_small_story_impact(small_raw: dict[str, Any]) -> None:
    resp = run_impact(ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"]))
    # Without the delay the seed plan is healthy.
    assert resp.baseline_kpis.units_at_risk == 0
    assert resp.baseline_kpis.shortage_cluster_weeks == 0
    assert resp.baseline_kpis.first_breach == []
    # +7 days on the Bangladesh receipts: UK1 and NL1 run short in peak week W45 only.
    k = resp.kpis
    assert k.units_at_risk > 2000
    assert k.revenue_at_risk_cents > k.margin_at_risk_cents > 0
    assert k.shortage_cluster_weeks == 4  # 2 UK1 + 2 NL1 clusters x W45
    breach_dcs = {b.dc_code for b in k.first_breach}
    assert breach_dcs == {"UK1", "NL1"}
    assert {b.week for b in k.first_breach} == {"2026-W45"}
    assert {b.date for b in k.first_breach} == {dt.date(2026, 11, 2)}
    assert len(k.first_breach) == 4 * 3  # 4 clusters x 3 styles
    # by_dc: one row per DC, sums match the totals, DE1/PL1 untouched.
    by = {r.dc_code: r for r in resp.by_dc}
    assert set(by) == {"UK1", "NL1", "DE1", "PL1"}
    assert sum(r.units_at_risk for r in resp.by_dc) == k.units_at_risk
    assert sum(r.revenue_at_risk_cents for r in resp.by_dc) == k.revenue_at_risk_cents
    assert by["DE1"].units_at_risk == by["PL1"].units_at_risk == 0
    # Timeline: cluster x style x week, consistent with demand and breaches.
    assert len(resp.timeline) == 8 * 3 * 9
    total_demand = sum(r["units"] for r in small_raw["data"]["demand"])
    assert sum(p.demand for p in resp.timeline) == total_demand
    short_points = {(p.cluster_code, p.style_code, p.week) for p in resp.timeline if p.shortage > 0}
    assert short_points == {(b.cluster_code, b.style_code, b.week) for b in k.first_breach}
    assert resp.provenance.engine == "keelson-opt"
    assert len(resp.provenance.inputs_hash) == 64
    assert resp.warnings == []


def test_timeline_sums_sizes_per_style(small_raw: dict[str, Any]) -> None:
    req = ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"])
    res = simulate(build_scenario(req.data, req.perturbations))
    points = {(p.cluster_code, p.style_code, p.week): p for p in timeline(res)}
    p = points[("UK1-COLD-A", "PUF-W-BLK", "2026-W45")]
    sizes = [res.series[("UK1-COLD-A", f"PUF-W-BLK-{s}")] for s in ("S", "M", "L")]
    assert p.demand == sum(cs.demand[4] for cs in sizes)
    assert p.projected_available == sum(cs.pab[4] for cs in sizes)
    assert p.shortage == sum(cs.gap[4] for cs in sizes) > 0
