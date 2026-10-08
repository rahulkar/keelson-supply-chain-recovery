"""Pure logic: severity/confidence formulas, clustering (anchor-attach), event aggregates, geometry
helpers, exposure merge."""

from __future__ import annotations

import datetime as dt

import pytest

from conftest import T0
from keelson_contracts.signals import Exposure
from signal_hub import geo, scoring
from signal_hub.cluster import Candidate, MemberRow, SignalFeatures, aggregate_event, choose_event, kinds_compatible, radius_km
from signal_hub.exposure import merge_matches

EV = "11111111-1111-5111-8111-111111111111"
EV2 = "22222222-2222-5222-8222-222222222222"
H = dt.timedelta(hours=1)


# ─────────────────────────────── formulas ───────────────────────────────

@pytest.mark.parametrize(("etype", "level", "score", "sev", "expected"), [
    ("TC", "Orange", 2.0, 150.0, 6155),
    ("TC", "red", 3.0, 185.0, 8830),
    ("TC", "red", 3.0, 400.0, 9500),  # bonus capped at 1000
    ("TC", "green", 1.0, 90.0, 2000),  # no negative bonus
    ("EQ", "red", 2.5, 7.1, 9500),
    ("EQ", "orange", 2.0, 6.4, 6400),
    ("FL", "orange", 2.0, 0.0, 6250),
    ("FL", "red", None, None, 8500),
    ("VO", "weird", None, None, 2000),
])
def test_gdacs_severity(etype: str, level: str, score: float | None, sev: float | None, expected: int) -> None:
    assert scoring.gdacs_severity_bp(etype, level, alertscore=score, severity=sev) == expected


@pytest.mark.parametrize(("mag", "alert", "tsunami", "expected"), [
    (4.6, None, 0, 1000), (5.4, None, 0, 2500), (6.2, None, 0, 4000), (6.8, "yellow", 1, 6500),
    (7.2, "orange", 0, 7500), (7.9, "red", 1, 10000), (None, None, 0, 0), (6.5, None, 0, 5500),
])
def test_usgs_severity(mag: float | None, alert: str | None, tsunami: int, expected: int) -> None:
    assert scoring.usgs_severity_bp(mag, alert=alert, tsunami=tsunami) == expected


def test_recency_and_confidence() -> None:
    assert scoring.recency_bp(T0, T0 + 24 * H) == 10_000
    assert scoring.recency_bp(T0, T0 + 168 * H) == 5000
    assert scoring.recency_bp(T0, T0 + 96 * H) == 7500
    assert scoring.recency_bp(T0 + H, T0) == 10_000  # future observation: fresh
    assert scoring.corroboration_bp(1, anchored=True) == 10_000
    assert [scoring.corroboration_bp(n, anchored=False) for n in (1, 2, 3, 9)] == [6000, 8000, 10_000, 10_000]
    assert scoring.signal_confidence_bp(9500, anchored=True, observed_at=T0, now=T0) == 9500
    assert scoring.signal_confidence_bp(7000, anchored=False, observed_at=T0, now=T0 + 96 * H) == 3150
    assert scoring.delay_severity_bp(0) == 1500 and scoring.delay_severity_bp(30) == 9000


def test_event_severity() -> None:
    assert scoring.event_severity_bp([6155, 8830], [6155, 8830, 3000]) == 8830
    assert scoring.event_severity_bp([], [3000, 4000, 5000, 6000]) == 5000  # p75 nearest rank
    assert scoring.event_severity_bp([], [4500]) == 4500
    assert scoring.event_severity_bp([], []) == 0


# ─────────────────────────────── clustering ───────────────────────────────

def _cand(**over: object) -> Candidate:
    base: dict[str, object] = dict(event_id=EV, kind="natural_hazard", subtype="tropical_cyclone", anchored=True,
                                   anchor_connector="gdacs", window_start=T0, window_end=None, last_observed=T0 + 6 * H,
                                   distance_km=120.0, refs=frozenset({"ISO3166:VN"}))
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


def _news(**over: object) -> SignalFeatures:
    base: dict[str, object] = dict(kind="logistics", subtype="port_closure", connector_id="rss.x", authoritative=False,
                                   starts_at=T0 + 8 * H, ends_at=None, refs=frozenset({"UNLOCODE:VNSGN", "ISO3166:VN"}))
    base.update(over)
    return SignalFeatures(**base)  # type: ignore[arg-type]


def test_news_attaches_to_anchor() -> None:
    assert choose_event(_news(), [_cand()]) == EV


def test_attach_by_shared_ref_without_geometry() -> None:
    assert choose_event(_news(), [_cand(distance_km=None)]) == EV
    assert choose_event(_news(refs=frozenset()), [_cand(distance_km=None)]) is None


def test_attach_requires_kind_time_and_place() -> None:
    assert choose_event(_news(kind="cyber"), [_cand()]) is None
    assert choose_event(_news(kind="demand"), [_cand()]) is None  # natural_hazard ↔ demand: not related
    assert choose_event(_news(starts_at=T0 + 6 * H + 73 * H), [_cand()]) is None
    assert choose_event(_news(starts_at=T0 + 6 * H + 71 * H), [_cand()]) == EV
    assert choose_event(_news(starts_at=T0 - 73 * H), [_cand()]) is None
    assert choose_event(_news(refs=frozenset()), [_cand(distance_km=301.0)]) is None
    assert choose_event(_news(refs=frozenset()), [_cand(distance_km=299.0)]) == EV


def test_ranking_prefers_anchor_then_specific_refs_then_distance() -> None:
    news_only = _cand(event_id=EV2, anchored=False, anchor_connector=None, kind="logistics", distance_km=1.0,
                      refs=frozenset({"UNLOCODE:VNSGN"}))
    assert choose_event(_news(), [news_only, _cand(distance_km=250.0)]) == EV
    near = _cand(event_id=EV2, distance_km=10.0, refs=frozenset())
    assert choose_event(_news(), [near, _cand(distance_km=200.0, refs=frozenset({"UNLOCODE:VNSGN"}))]) == EV
    assert choose_event(_news(refs=frozenset()), [_cand(distance_km=200.0), _cand(event_id=EV2, distance_km=50.0)]) == EV2


def test_authoritative_signals_found_events_or_merge_same_hazard() -> None:
    usgs = SignalFeatures(kind="natural_hazard", subtype="earthquake", connector_id="usgs", authoritative=True,
                          starts_at=T0 + 0.1 * H, ends_at=T0 + 0.1 * H)
    gdacs_eq = _cand(subtype="earthquake", distance_km=20.0, window_start=T0)
    assert choose_event(usgs, [gdacs_eq]) == EV
    assert choose_event(usgs, [_cand(subtype="earthquake", distance_km=150.0)]) is None
    assert choose_event(usgs, [_cand(subtype="earthquake", distance_km=20.0, anchor_connector="usgs")]) is None
    assert choose_event(usgs, [_cand(subtype="earthquake", distance_km=20.0, window_start=T0 + 7 * H)]) is None
    tc = SignalFeatures(kind="natural_hazard", subtype="tropical_cyclone", connector_id="gdacs", authoritative=True,
                        starts_at=T0, ends_at=None)
    assert choose_event(tc, [_cand()]) is None  # never joins another anchor of its own feed


def test_kind_compatibility_is_symmetric() -> None:
    assert kinds_compatible("logistics", "natural_hazard") and kinds_compatible("natural_hazard", "logistics")
    assert kinds_compatible("hypothetical", "hypothetical") and not kinds_compatible("hypothetical", "logistics")


def test_radius() -> None:
    assert radius_km("tropical_cyclone") == 300 and radius_km("typhoon") == 300
    assert radius_km("earthquake", magnitude=6.5) == 250 and radius_km("earthquake", magnitude=6.4) == 100
    assert radius_km("earthquake") == 100 and radius_km("flood") == 50 and radius_km("port_closure") == 10
    assert radius_km("strike") == 25 and radius_km("anything") == 50


def _member(**over: object) -> MemberRow:
    base: dict[str, object] = dict(
        id="a", connector_id="gdacs", authoritative=True, is_current=True, kind="natural_hazard",
        subtype="tropical_cyclone", title="TC X", summary="GDACS Orange", severity_bp=6155, authority_bp=9500,
        observed_at=T0, starts_at=T0, ends_at=None, expires_at=T0 + 48 * H, provenance="fact", pack_id=None,
        geometry={"type": "Point", "coordinates": [110.0, 11.0]}, severity_native={})
    base.update(over)
    return MemberRow(**base)  # type: ignore[arg-type]


def test_aggregate_anchor_event() -> None:
    members = [
        _member(id="a1", is_current=False, severity_bp=5000),
        _member(id="a2", severity_bp=8830, observed_at=T0 + 6 * H, expires_at=T0 + 54 * H),
        _member(id="n1", connector_id="rss.x", authoritative=False, kind="logistics", subtype="port_closure",
                title="Cat Lai closes", summary=None, severity_bp=6500, authority_bp=7000, starts_at=T0 + 7 * H,
                observed_at=T0 + 7 * H, expires_at=T0 + 79 * H,
                geometry={"type": "Point", "coordinates": [106.788, 10.7626]}),
    ]
    agg = aggregate_event(members, now=T0 + 8 * H)
    assert agg is not None
    assert (agg.anchor_signal_id, agg.kind, agg.subtype, agg.title) == ("a2", "natural_hazard", "tropical_cyclone", "TC X")
    assert agg.severity_bp == 8830 and agg.status == "confirmed" and agg.taxonomy_key == "NAT.storm"
    assert agg.confidence_bp == 9500 and agg.radius_km == 300
    assert agg.window_start == T0 and agg.window_end is None and agg.expires_at == T0 + 79 * H
    assert agg.geometry is not None and agg.geometry["type"] == "GeometryCollection"
    assert not agg.simulated
    assert aggregate_event(members, now=T0 + 80 * H).status == "resolved"  # type: ignore[union-attr]
    assert aggregate_event(members, now=T0 + 8 * H, merged=True).status == "resolved"  # type: ignore[union-attr]


def test_aggregate_news_only_event() -> None:
    members = [
        _member(id="n1", connector_id="rss.a", authoritative=False, kind="logistics", subtype="port_closure",
                title="First", severity_bp=6500, authority_bp=7000, expires_at=None, observed_at=T0 + 2 * H),
        _member(id="n2", connector_id="rss.a", authoritative=False, kind="logistics", subtype="port_closure",
                title="Second", severity_bp=3000, authority_bp=7000, expires_at=None, observed_at=T0 + 3 * H),
    ]
    agg = aggregate_event(members, now=T0 + 3 * H)
    assert agg is not None and agg.anchor_signal_id is None and agg.status == "candidate"
    assert agg.title == "First" and agg.severity_bp == 6500 and agg.taxonomy_key == "LOG.port_closure"
    assert agg.confidence_bp == 4200  # 7000 x 6000 (one source) x 10000
    assert agg.summary == "2 reports from 1 source (rss.a)"
    agg2 = aggregate_event([*members, _member(id="n3", connector_id="rss.b", authoritative=False, kind="logistics",
                                              subtype="port_closure", authority_bp=7000, expires_at=None,
                                              observed_at=T0 + 3 * H, severity_bp=7000)], now=T0 + 3 * H)
    assert agg2 is not None and agg2.status == "confirmed" and agg2.confidence_bp == 5600
    assert aggregate_event(members, now=T0 + 60 * H).status == "monitoring"  # type: ignore[union-attr]
    assert aggregate_event([_member(is_current=False)], now=T0) is None


def test_aggregate_demo_event_is_simulated() -> None:
    agg = aggregate_event([_member(provenance="simulated", pack_id="p1")], now=T0)
    assert agg is not None and agg.simulated and agg.pack_id == "p1"


# ─────────────────────────────── geometry ───────────────────────────────

def test_clean_geometry() -> None:
    assert geo.clean_geometry({"type": "Point", "coordinates": [10.123456789, 50.0, 300.0]}) == {
        "type": "Point", "coordinates": [10.12346, 50.0]}
    assert geo.clean_geometry({"type": "Point", "coordinates": [200.0, 0.0]}) is None
    assert geo.clean_geometry({"type": "Point", "coordinates": [True, 0.0]}) is None
    assert geo.clean_geometry({"type": "Point", "coordinates": [float("nan"), 0.0]}) is None
    open_ring = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1]]]}
    closed = geo.clean_geometry(open_ring)
    assert closed is not None and closed["coordinates"][0][0] == closed["coordinates"][0][-1]
    nested = {"type": "GeometryCollection", "geometries": [{"type": "GeometryCollection", "geometries": [
        {"type": "Point", "coordinates": [1, 1]}]}, {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}]}
    flat = geo.clean_geometry(nested)
    assert flat is not None and [g["type"] for g in flat["geometries"]] == ["Point", "LineString"]
    big = {"type": "LineString", "coordinates": [[i * 0.001, 0.0] for i in range(100_000)]}
    small = geo.clean_geometry(big, max_positions=1000)
    assert small is not None and geo.count_positions(small) <= 1000
    assert small["coordinates"][0] == [0.0, 0.0] and small["coordinates"][-1] == [99.999, 0.0]
    assert geo.clean_geometry({"type": "Nope"}) is None and geo.merge([]) is None


# ─────────────────────────────── exposure merge ───────────────────────────────

def test_merge_matches_precedence() -> None:
    out = merge_matches(EV, "t1", [
        {"node_type": "port", "node_code": "VNSGN", "method": "dwithin_km", "distance_km": 12, "weight_bp": 9000},
        {"node_type": "port", "node_code": "VNSGN", "method": "entity_match", "distance_km": None, "weight_bp": 10000},
        {"node_type": "lane", "node_code": "L1", "method": "lane_crosses", "distance_km": 0, "weight_bp": 9000},
        {"node_type": "lane", "node_code": "L1", "method": "entity_match", "distance_km": None, "weight_bp": 10000},
        {"node_type": "factory", "node_code": "F1", "method": "intersects", "distance_km": 0, "weight_bp": 10000},
        {"node_type": "factory", "node_code": "F1", "method": "entity_match", "distance_km": None, "weight_bp": 10000},
    ])
    assert [(e.node_type, e.node_code, e.method) for e in out] == [
        ("factory", "F1", "intersects"), ("lane", "L1", "entity_match"), ("port", "VNSGN", "entity_match")]
    assert all(isinstance(e, Exposure) for e in out)
