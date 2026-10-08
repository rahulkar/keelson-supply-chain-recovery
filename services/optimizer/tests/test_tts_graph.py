"""``/v1/impact`` with ``include_graph``: TTS LP per DC, TTR verdicts, bottlenecks from duals,
the grouped impact graph (statuses, size cap) and a port closure via a chokepoint."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from functools import cache
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import MAX_GRAPH_NODES, ImpactRequest, ImpactResponse, ProblemData

from keelson_opt.graph import build_graph
from keelson_opt.impact import simulate
from keelson_opt.plan import base_lines
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_impact
from keelson_opt.tts import tts_by_dc, verdict

from .fixtures.full_problem import BD_TRIMS_DELAY, CAPE_CLOSURE, VN_PORT_CLOSURE, full_data
from .network import TRIM_DELAY, network_problem


def impact(data: dict[str, Any], perturbations: list[dict[str, Any]], graph: bool = True) -> ImpactResponse:
    return run_impact(ImpactRequest(data=data, perturbations=perturbations, include_graph=graph))


@cache
def full_impact(name: str) -> ImpactResponse:
    pert = {"bd": [BD_TRIMS_DELAY], "vn": [VN_PORT_CLOSURE], "cape": [CAPE_CLOSURE]}[name]
    return impact(full_data(), pert)


# ───────────────────────────── TTS ─────────────────────────────


def one_dc(stock: int, receipt: tuple[int, str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenant": "t1",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 6},
        "dcs": [{"code": "D"}],
        "clusters": [{"code": "D-A", "dc_code": "D"}],
        "skus": [
            {"code": "S1", "style_code": "ST", "retail_price_cents": 2000, "unit_cost_cents": 500},
            {"code": "S2", "style_code": "ST", "retail_price_cents": 2000, "unit_cost_cents": 500},
        ],
        "inventory": [
            {"location_type": "dc", "location_code": "D", "sku": "S1", "on_hand": stock},
            {"location_type": "dc", "location_code": "D", "sku": "S2", "on_hand": 1000},
        ],
        "demand": [
            {"cluster_code": "D-A", "sku": s, "iso_week": f"2026-W{w}", "units": 10}
            for s in ("S1", "S2")
            for w in range(41, 47)
        ],
    }
    if receipt:
        data["receipts"] = [{"ref": "PO-9#1", "dc_code": "D", "sku": "S1", "qty": receipt[0], "eta": receipt[1]}]
    return data


def tts_days(data: dict[str, Any], perturbations: list[dict[str, Any]] | None = None) -> int:
    req = ImpactRequest(data=data, perturbations=perturbations or [])
    scn = build_scenario(req.data, req.perturbations)
    return tts_by_dc(scn, base_lines(scn))["D"].days


def test_tts_hand_computed_and_monotone_in_stock() -> None:
    assert tts_days(one_dc(25)) == 17  # 10 units/week: 2.5 weeks -> 17.5 days
    assert tts_days(one_dc(0)) == 0
    assert tts_days(one_dc(60)) == 42  # covers the whole horizon
    days = [tts_days(one_dc(s)) for s in range(0, 70, 5)]
    assert days == sorted(days) and days[0] < days[-1]  # more stock -> never less TTS
    # an on-time receipt extends it, a disrupted (delayed) one does not count
    assert tts_days(one_dc(25, (30, "2026-10-19"))) == 38  # 55 units: 5.5 weeks
    assert tts_days(one_dc(25, (35, "2026-10-19"))) == 42
    assert (
        tts_days(
            one_dc(25, (35, "2026-10-19")),
            [{"op": "lead_time_shift", "target": {"kinds": ["po"], "ids": ["PO-9"]}, "days": 3, "prov": "fact"}],
        )
        == 17
    )


def test_tts_sanity_on_the_full_fixture_more_stock_more_tts() -> None:
    def uk1(extra: int) -> int:
        data = full_data()
        for row in data["inventory"]:
            if row["location_code"] == "UK1":
                row["on_hand"] += extra
        req = ImpactRequest(data=data, perturbations=[BD_TRIMS_DELAY])
        scn = build_scenario(req.data, req.perturbations)
        return tts_by_dc(scn, base_lines(scn))["UK1"].days

    a, b, c = uk1(0), uk1(40), uk1(5000)
    assert a < b < c and c == 63


def test_verdict_rules() -> None:
    assert verdict(63, None, 63) == "ok"  # survives the horizon
    assert verdict(20, 15, 63) == "ok"
    assert verdict(20, 20, 63) == "ok"
    assert verdict(20, 27, 63) == "expedite"  # within one weekly bucket
    assert verdict(20, 28, 63) == "loss"
    assert verdict(20, None, 63) == "loss"  # nothing recovers


def test_story_verdicts_and_bottlenecks() -> None:
    bd = {r.dc_code: r for r in full_impact("bd").tts}
    assert (bd["UK1"].verdict, bd["NL1"].verdict) == ("expedite", "expedite")
    assert bd["UK1"].ttr_days == 31  # first delayed wave: 2026-10-29 + 7 d, from W41 Monday
    assert (bd["DE1"].verdict, bd["DE1"].tts_days, bd["DE1"].ttr_days) == ("ok", 63, None)
    vn = {r.dc_code: r for r in full_impact("vn").tts}
    assert vn["DE1"].verdict == vn["PL1"].verdict == "loss" and vn["UK1"].verdict == "ok"
    for resp in (full_impact("bd"), full_impact("vn")):
        b = resp.bottlenecks
        assert b and len(b) <= 25 and all(x.shadow_price_cents > 0 for x in b)
        assert b == sorted(b, key=lambda x: (-x.shadow_price_cents, x.name))
    assert all(x.name.split()[0] in ("UK1", "NL1") for x in full_impact("bd").bottlenecks)


# ───────────────────────────── graph ─────────────────────────────


def nodes(resp: ImpactResponse) -> dict[str, Any]:
    assert resp.graph is not None
    return {n.id: n for n in resp.graph.nodes}


def test_graph_statuses_on_the_bd_trims_delay() -> None:
    resp = full_impact("bd")
    g = nodes(resp)
    assert len(g) <= MAX_GRAPH_NODES
    assert g["supplier:TRIM-BD-01"].status == "disrupted"
    assert g["component:CMP-WAD-01"].status == "impacted" and g["component:CMP-WAD-01"].units_at_risk > 0
    assert g["dc:UK1"].status == g["dc:NL1"].status == "impacted"
    assert g["dc:UK1"].units_at_risk == next(r.units_at_risk for r in resp.by_dc if r.dc_code == "UK1")
    assert g["dc:UK1"].first_breach_week == "2026-W45"
    assert g["dc:DE1"].status == "ok" and g["po:PO-VN-0001"].status == "ok"
    assert g["po:PO-BD-0003"].status == "impacted" and g["factory:F-BD-01"].status == "impacted"
    assert g["lane:CGP-FXT-SEA"].status == "impacted"
    kinds = {n.kind for n in g.values()}
    assert {"supplier", "component", "factory", "po", "lane", "port", "chokepoint", "dc", "cluster", "style"} <= kinds
    assert not any(k in kinds for k in ("sku", "store", "po_line"))  # grouped
    edges = {(e.source, e.target, e.kind) for e in resp.graph.edges}  # type: ignore[union-attr]
    for e in [
        ("supplier:TRIM-BD-01", "component:CMP-WAD-01", "supplies"),
        ("component:CMP-WAD-01", "factory:F-BD-01", "supplies"),
        ("factory:F-BD-01", "po:PO-BD-0003", "produces"),
        ("po:PO-BD-0003", "lane:CGP-FXT-SEA", "ships"),
        ("chokepoint:CAPE", "lane:CGP-FXT-SEA", "ships"),
        ("lane:CGP-FXT-SEA", "dc:UK1", "ships"),
        ("dc:UK1", "cluster:UK1-COLD-A", "serves"),
        ("dc:UK1", "style:PUF-W-BLK", "stocks"),
        ("style:PUF-W-BLK", "cluster:UK1-COLD-A", "serves"),
    ]:
        assert e in edges
    assert g["port:BDCGP"].label == "Chittagong"  # labels from locations


def test_port_closure_via_chokepoint() -> None:
    resp = full_impact("cape")
    g = nodes(resp)
    assert g["chokepoint:CAPE"].status == "disrupted"
    for lane in ("CGP-FXT-SEA", "CGP-RTM-SEA", "SGN-HAM-SEA", "SGN-GDN-SEA"):
        assert g[f"lane:{lane}"].status == "impacted"
    assert g["lane:CGP-UK1-SA"].status == "ok"  # via Jebel Ali, open
    assert resp.kpis.units_at_risk > 0 and any("receipt units are lost" in w for w in resp.warnings)
    tts = {r.dc_code: r for r in resp.tts}
    assert tts["UK1"].ttr_days is None and tts["UK1"].verdict == "loss"  # lost wave never recovers


def test_vn_port_closure_marks_the_port() -> None:
    g = nodes(full_impact("vn"))
    assert g["port:VNSGN"].status == "disrupted"
    assert g["dc:DE1"].status == "impacted" and g["dc:UK1"].status == "ok"


def test_graph_size_cap_keeps_the_most_severe_nodes() -> None:
    req = ImpactRequest(data=full_data(), perturbations=[BD_TRIMS_DELAY])
    scn = build_scenario(req.data, req.perturbations)
    graph, warnings = build_graph(scn, simulate(scn), max_nodes=40)
    assert len(graph.nodes) == 40 and warnings == ["impact graph truncated to 40 of 91 nodes"]
    kept = {n.id: n.status for n in graph.nodes}
    assert kept["supplier:TRIM-BD-01"] == "disrupted" and "dc:UK1" in kept
    assert "ok" not in kept.values()
    assert all(e.source in kept and e.target in kept for e in graph.edges)


def test_big_network_stays_within_300_nodes() -> None:
    data = full_data()
    extra = [
        {"code": f"XTRA-{i:03d}-M", "style_code": f"XTRA-{i:03d}", "retail_price_cents": 1000, "unit_cost_cents": 400}
        for i in range(400)
    ]
    data["skus"] += extra
    data["demand"] += [
        {"cluster_code": "UK1-COLD-A", "sku": s["code"], "iso_week": "2026-W45", "units": 3} for s in extra
    ]
    resp = impact(data, [BD_TRIMS_DELAY])
    assert resp.graph is not None and len(resp.graph.nodes) == MAX_GRAPH_NODES
    assert any("truncated" in w for w in resp.warnings)


def test_without_include_graph_the_response_is_m1_shaped() -> None:
    resp = impact(network_problem(), [TRIM_DELAY], graph=False)
    assert resp.graph is None and resp.tts == [] and resp.bottlenecks == []
    assert set(json.loads(resp.model_dump_json())) == {
        "kpis",
        "by_dc",
        "timeline",
        "baseline_kpis",
        "provenance",
        "warnings",
    }
    # include_graph=false is not part of the inputs hash (M1 hashes unchanged)
    with_flag = ImpactRequest(data=network_problem(), perturbations=[TRIM_DELAY], include_graph=False)
    assert "include_graph" not in with_flag.model_dump(mode="json")


def test_impact_graph_over_http(client: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    body = {"data": network_problem(), "perturbations": [TRIM_DELAY], "include_graph": True}
    r = client.post("/v1/impact", json=body, headers=auth())
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert {n["id"] for n in out["graph"]["nodes"]} >= {"supplier:TRIM-1", "dc:UK1", "po:PO-1", "lane:SEA-UK"}
    assert [t["dc_code"] for t in out["tts"]] == ["DE1", "UK1"]
    uk1 = out["tts"][1]
    assert uk1["verdict"] == "loss" and uk1["ttr_days"] == 35  # W44 + 14 d, stock lasts 3 weeks
    assert out["bottlenecks"][0]["name"].startswith("UK1 TOP-M supply by")


@pytest.mark.parametrize("seed", [0, 5])
def test_impact_with_graph_is_deterministic(seed: int) -> None:
    data = copy.deepcopy(full_data())
    if seed:
        data["demand"].reverse()
        data["po_lines"].reverse()
    a = impact(data, [BD_TRIMS_DELAY]).model_dump(mode="json", exclude={"provenance"})
    b = full_impact("bd").model_dump(mode="json", exclude={"provenance"})
    assert a == b
    assert ProblemData  # imported for type checkers
