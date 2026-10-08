"""M8 CO₂e inputs in the engine (``126-m8-co2-robustness.md`` §2): the factor file and its
validation, unit weights by style prefix, lane legs from the map catalogue (sea around the Cape is
longer than through Suez; sea-air = the sea leg into the hub + great circle + 95 km by air; road
legs x 1.2), and the request enrichment (deterministic, idempotent, a no-op when off, never
touching the stored problem)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from keelson_contracts.optimizer import InboundLane, ProblemData
from test_m3_visuals import problem

from keelson_engine.runtime import emissions as em
from keelson_engine.runtime.geo import distance_km, get_geo
from keelson_engine.runtime.spec import PlanningDefaults
from keelson_engine.runtime.spec_m8 import UnitWeightRule
from keelson_engine.runtime.visuals import _locate

GOOD: dict[str, Any] = {
    "version": 1,
    "sets": [{
        "id": "test-set",
        "label": "Test factors",
        "basis": "WTW",
        "unit": "g CO2e/tkm",
        "verified": True,
        "source": {"title": "T", "publisher": "P", "url": "https://example.org/factors", "table": "Table 1",
                   "retrieved": "2026-10-01"},
        "factors": {"sea": 10, "air": 500, "road": 60},
    }],
}


def _km(x: float) -> int:
    return int(x + 0.5)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    yield
    em.set_factor_sets(None)


def test_platform_factor_file_loads() -> None:
    sets = em.load_factor_sets()
    fs = sets["glec-v3-indicative"]
    assert fs.basis == "WTW" and fs.unit == "g CO2e/tkm" and fs.verified is False
    assert set(fs.factors) == {"sea", "air", "road", "rail"} and all(v > 0 for v in fs.factors.values())
    assert fs.distance.road_circuity_bp == 12_000 and fs.distance.air_uplift_km == 95
    spec = fs.spec()
    assert spec.source_id == "glec-v3-indicative" and [f.mode for f in spec.factors] == ["air", "rail", "road", "sea"]
    # air per tonne-km is far above sea (the reason sea-air costs CO2e)
    assert fs.factors["air"] > 20 * fs.factors["sea"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["sets"].append(copy.deepcopy(d["sets"][0])),
        lambda d: d["sets"][0]["factors"].__setitem__("sea", 10.5),
        lambda d: d["sets"][0]["factors"].__setitem__("sea", 0),
        lambda d: d["sets"][0]["factors"].__setitem__("barge", 5),
        lambda d: d["sets"][0]["factors"].clear(),
        lambda d: d["sets"][0].pop("source"),
        lambda d: d["sets"][0]["source"].__setitem__("url", "not a url"),
        lambda d: d["sets"][0].__setitem__("unit", "kg CO2e/tkm"),
        lambda d: d.__setitem__("version", 2),
        lambda d: d["sets"][0].__setitem__("extra", True),
        lambda d: d["sets"][0].__setitem__("distance", {"road_circuity_bp": 9000}),
    ],
)
def test_factor_file_validation(mutate: Any) -> None:
    doc = copy.deepcopy(GOOD)
    mutate(doc)
    with pytest.raises(em.EmissionsDataError):
        em.parse_factor_sets(doc)
    with pytest.raises(em.EmissionsDataError):
        em.parse_factor_sets(["not", "a", "mapping"])


def test_unit_weight_longest_prefix_wins() -> None:
    rules = (UnitWeightRule(style_prefix="PUF", grams=1100), UnitWeightRule(style_prefix="PUF-W", grams=900))
    assert em.unit_weight_g("PUF-W-BLK", rules, 800) == 900
    assert em.unit_weight_g("PUF-M-NVY", rules, 800) == 1100
    assert em.unit_weight_g("KNT-W-OAT", rules, 800) == 800
    assert em.unit_weight_g("XPUF", rules, 800) == 800  # a prefix, never a substring or a pattern
    with pytest.raises(ValueError):
        UnitWeightRule(style_prefix="PU.*", grams=1)


def _legs(p: ProblemData) -> dict[str, list[tuple[str, int]]]:
    geo = get_geo()
    where = _locate(p, geo)
    return {il.code: [(leg.mode, leg.distance_km) for leg in em.lane_legs(il, where, geo, circuity_bp=12_000,
                                                                          air_uplift_km=95)]
            for il in p.inbound_lanes}


def test_lane_legs_follow_the_map_catalogue() -> None:
    p = problem()
    geo = get_geo()
    legs = _legs(p)
    sea = legs["CGP-FXT-SEA+FXT-UK1-RD"]
    assert [m for m, _ in sea] == ["sea", "road"]
    uk1 = (int(0.35e6), int(51.49e6))
    assert sea[1][1] == _km(distance_km(geo.place("GBFXT"), uk1) * 1.2)  # type: ignore[arg-type]
    sa = legs["CGP-UK1-SA"]
    assert [m for m, _ in sa] == ["sea", "air"]
    assert sa[1][1] == _km(distance_km(geo.place("AEJEA"), uk1) + 95)  # type: ignore[arg-type]
    road = legs["IST-DE1-RD"]
    de1 = (int(11.03e6), int(50.98e6))
    assert road == [("road", _km(distance_km(geo.place("TRIST"), de1) * 1.2))]  # type: ignore[arg-type]
    # the sea leg follows the catalogued route (around the Cape), far longer than the great circle
    assert sea[0][1] > 1.5 * distance_km(geo.place("BDCGP"), geo.place("GBFXT"))  # type: ignore[arg-type]


def test_cape_is_longer_than_suez() -> None:
    p = problem()
    cape = p.inbound_lanes[0]
    suez = cape.model_copy(update={"code": "CGP-FXT-SUEZ", "via": ["LKCMB", "SUEZ", "GBFXT"]})
    legs = _legs(p.model_copy(update={"inbound_lanes": [cape, suez]}))
    assert legs["CGP-FXT-SEA+FXT-UK1-RD"][0][1] > legs["CGP-FXT-SUEZ"][0][1] + 3000


def test_lanes_without_coordinates_get_no_legs() -> None:
    p = problem()
    lost = InboundLane(code="X-UK1", origin="NOWHERE", dest_dc="UK1", mode="sea", transit_days=30,
                       cost_per_unit_cents=10, weekly_capacity_units=100)
    assert _legs(p.model_copy(update={"inbound_lanes": [lost]})) == {"X-UK1": []}


def test_enrichment_is_deterministic_idempotent_and_leaves_the_problem_alone() -> None:
    p = problem()
    before = p.model_dump(mode="json")
    planning = PlanningDefaults(emissions={"unit_weights": [{"style_prefix": "PUF", "grams": 1100}]})
    out = em.enrich_for_emissions(p, planning, get_geo())
    assert out is not None
    enriched, fs = out
    assert fs.id == "glec-v3-indicative"
    assert {s.code: s.unit_weight_g for s in enriched.skus} == {"PUF-01-S": 1100, "PUF-01-M": 1100, "KNT-01-M": 800}
    [lane] = enriched.lanes
    assert lane.distance_km == _km(distance_km((int(11.03e6), int(50.98e6)), (int(0.35e6), int(51.49e6))) * 1.2)
    assert all(il.legs for il in enriched.inbound_lanes)
    assert p.model_dump(mode="json") == before  # the stored problem evidence is never changed
    again = em.enrich_for_emissions(enriched, planning, get_geo())
    assert again is not None and again[0] == enriched
    assert ProblemData.model_validate(enriched.model_dump(mode="json")) == enriched
    default_weight = em.enrich_for_emissions(p, PlanningDefaults(emissions={"default_unit_weight_g": 650}), get_geo())
    assert default_weight is not None and {s.unit_weight_g for s in default_weight[0].skus} == {650}


def test_enrichment_off_or_without_factors_is_a_no_op() -> None:
    p = problem()
    assert em.enrich_for_emissions(p, PlanningDefaults(emissions={"enabled": False}), get_geo()) is None
    assert em.enrich_for_emissions(p, PlanningDefaults(emissions={"factors_id": "unknown-set"}), get_geo()) is None
    em.set_factor_sets(em.parse_factor_sets(GOOD))
    out = em.enrich_for_emissions(p, PlanningDefaults(emissions={"factors_id": "test-set"}), get_geo())
    assert out is not None and out[1].spec().model_dump()["factors"] == [
        {"mode": "air", "g_co2e_per_tkm": 500}, {"mode": "road", "g_co2e_per_tkm": 60},
        {"mode": "sea", "g_co2e_per_tkm": 10}]


def test_missing_factor_file_disables_co2e(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setattr(em, "emissions_dirs", lambda: [tmp_path])
    em._default.cache_clear()
    try:
        assert em.factor_set("glec-v3-indicative") is None
        with pytest.raises(em.EmissionsDataError):
            em.load_factor_sets()
    finally:
        em._default.cache_clear()


def test_planning_settings_validation() -> None:
    for bad in (
        {"emissions": {"unit_weights": [{"style_prefix": "PUF", "grams": 1}, {"style_prefix": "PUF", "grams": 2}]}},
        {"emissions": {"unit_weights": [{"style_prefix": "PUF", "grams": 0}]}},
        {"emissions": {"factors_id": "Not A Slug"}},
        {"emissions": {"default_unit_weight_g": 0}},
    ):
        with pytest.raises(ValueError):
            PlanningDefaults.model_validate(bad)
