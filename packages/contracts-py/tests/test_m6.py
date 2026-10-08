"""M6 contracts (``docs/contracts/100-m6-signals.md``): Signal Hub models, taxonomy, run payload,
``alert_origin@1``, news extraction, ``external_intel``."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.agui import AGENT_DISPLAY_NAMES
from keelson_contracts.blocks import AlertOriginProps, block_to_wire, make_block, parse_block
from keelson_contracts.common import EVENT_TYPE_KEYS, event_family_of
from keelson_contracts.llm import (
    LLM_OUTPUT_MODELS,
    NewsExtraction,
    news_extraction_violations,
    strict_schema_violations,
)
from keelson_contracts.runs import PAYLOAD_BY_KIND, RunRequest, SignalPayload
from keelson_contracts.signals import (
    DEFAULT_ALERT_RULES,
    SOURCE_URL_PATTERN,
    Alert,
    AlertCondition,
    AlertDetail,
    AlertRule,
    AlertStatusUpdate,
    DisruptionEvent,
    DisruptionSignal,
    Exposure,
    ExposureSummary,
    NodeCounts,
    ScenarioPack,
    SeverityNative,
    TenantNetwork,
    alert_severity_for,
    combine_confidence_bp,
    decide_alert_action,
    event_id,
    geometry_positions,
    is_uuid,
    pack_step_id,
    should_renotify,
    signal_id,
    taxonomy_key_for,
)

SIG_ID = signal_id("gdacs", "TC:1001234:7", 1)
EVT_ID = event_id("gdacs", "TC:1001234")
ALERT_ID = "0b8f3f5e-6a1c-5d2e-9f00-1a2b3c4d5e6f"
CONE = [[106.0, 9.5], [108.5, 9.5], [108.5, 12.0], [106.0, 12.0], [106.0, 9.5]]


def _signal(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": SIG_ID,
        "tenant": None,
        "origin": "feed",
        "kind": "natural_hazard",
        "subtype": "tropical_cyclone",
        "title": "Tropical cyclone KAI-TAK (orange)",
        "summary": "Orange alert; max sustained wind 150 km/h",
        "raw_text": None,
        "geometry": {"type": "GeometryCollection", "geometries": [
            {"type": "LineString", "coordinates": [[110.0, 10.0], [108.2, 10.6], [106.7, 10.8]]},
            {"type": "Polygon", "coordinates": [CONE]},
        ]},
        "location_refs": ["UNLOCODE:VNSGN", "ISO3166:VN"],
        "starts_at": "2026-10-07T00:00:00Z",
        "ends_at": None,
        "observed_at": "2026-10-07T06:00:00Z",
        "expires_at": "2026-10-10T00:00:00Z",
        "severity_bp": 7200,
        "severity_native": {"scheme": "gdacs", "alertlevel": "orange", "alertscore": 2.5, "episodeid": 7},
        "confidence_bp": 9000,
        "provenance": "fact",
        "extracted_by": None,
        "source": {
            "connector_id": "gdacs",
            "name": "GDACS",
            "url": "https://www.gdacs.org/report.aspx?eventtype=TC&eventid=1001234",
            "license": "CC BY 4.0",
            "authority_bp": 9500,
            "retrieved_at": "2026-10-07T06:01:00Z",
            "native_id": "TC:1001234:7",
        },
        "event_id": EVT_ID,
        "version": 1,
        "supersedes": None,
    }
    base.update(over)
    return base


def _event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": EVT_ID,
        "tenant": None,
        "kind": "natural_hazard",
        "subtype": "tropical_cyclone",
        "title": "Tropical cyclone KAI-TAK",
        "summary": None,
        "anchor_signal_id": SIG_ID,
        "member_signal_ids": [SIG_ID],
        "geometry": {"type": "Polygon", "coordinates": [CONE]},
        "window": {"starts_at": "2026-10-07T00:00:00Z", "ends_at": None},
        "severity_bp": 7200,
        "confidence_bp": 9000,
        "status": "confirmed",
        "taxonomy_key": "NAT.storm",
        "updated_at": "2026-10-07T06:01:00Z",
    }
    base.update(over)
    return base


SUMMARY = {"nodes_by_type": {"supplier": 1, "factory": 1, "port": 1, "dc": 0, "store": 0, "lane": 4},
           "open_po_lines": 2, "units_inbound": 1200}


def _alert(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": ALERT_ID,
        "tenant": "demo-retail",
        "event_id": EVT_ID,
        "status": "new",
        "severity": "high",
        "title": "Typhoon approaching Ho Chi Minh City",
        "why": "Severity 72% with 3 exposed sites (supplier, factory, port) and 4 lanes.",
        "exposure_summary": copy.deepcopy(SUMMARY),
        "first_raised_at": "2026-10-07T06:02:00Z",
        "updated_at": "2026-10-07T06:02:00Z",
        "case_id": None,
        "simulated": False,
    }
    base.update(over)
    return base


def _exposure(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"event_id": EVT_ID, "tenant": "demo-retail", "node_type": "port", "node_code": "VNSGN",
                            "method": "intersects", "distance_km": None, "weight_bp": 10000}
    base.update(over)
    return base


# ─────────────────────────────── taxonomy ───────────────────────────────────


def test_earthquake_is_a_taxonomy_key() -> None:
    assert "NAT.earthquake" in EVENT_TYPE_KEYS
    assert event_family_of("NAT.earthquake") == "NAT"
    # every other key kept its relative order
    assert [k for k in EVENT_TYPE_KEYS if k != "NAT.earthquake"] == [
        "SUP.delay", "SUP.capacity_loss", "LOG.chokepoint_closure", "LOG.port_closure",
        "LOG.strike", "NAT.storm", "NAT.flood", "DEM.spike", "DEM.drop", "OTHER",
    ]


@pytest.mark.parametrize(("subtype", "key"), [
    ("tropical_cyclone", "NAT.storm"), ("earthquake", "NAT.earthquake"), ("flood", "NAT.flood"),
    ("port_closure", "LOG.port_closure"), ("strike", "LOG.strike"), ("supplier_delay", "SUP.delay"),
    ("chokepoint_closure", "LOG.chokepoint_closure"), ("volcano", "OTHER"),
])
def test_taxonomy_key_for(subtype: str, key: str) -> None:
    assert taxonomy_key_for(subtype) == key
    assert key in EVENT_TYPE_KEYS


# ─────────────────────────────── ids ────────────────────────────────────────


def test_ids_are_deterministic_uuid5() -> None:
    assert signal_id("gdacs", "TC:1001234:7", 1) == SIG_ID
    assert is_uuid(SIG_ID) and is_uuid(EVT_ID)
    assert signal_id("gdacs", "TC:1001234:7", 2) != SIG_ID
    assert signal_id("usgs", "TC:1001234:7", 1) != SIG_ID
    # parts are joined unambiguously (canonical JSON list, not string concatenation)
    assert signal_id("a|b", "c", 1) != signal_id("a", "b|c", 1)
    assert pack_step_id("typhoon-hcmc", 0) == pack_step_id("typhoon-hcmc", 0) != pack_step_id("typhoon-hcmc", 1)
    with pytest.raises(ValueError):
        signal_id("gdacs", "", 1)
    with pytest.raises(ValueError):
        signal_id("gdacs", "x", 0)
    with pytest.raises(ValueError):
        signal_id("gdacs", "x", True)
    with pytest.raises(ValueError):
        event_id()


# ─────────────────────────────── signals ────────────────────────────────────


def test_feed_signal_round_trips() -> None:
    sig = DisruptionSignal.model_validate(_signal())
    wire = sig.model_dump(mode="json")
    assert DisruptionSignal.model_validate(wire) == sig
    assert wire["severity_native"] == {"scheme": "gdacs", "alertlevel": "orange", "alertscore": 2.5, "episodeid": 7}
    assert wire["geometry"]["geometries"][1]["coordinates"][0][0] == [106.0, 9.5]
    assert len(geometry_positions(sig.geometry)) == 3 + 5
    assert not sig.simulated
    json.dumps(wire, allow_nan=False)


@pytest.mark.parametrize(("label", "over"), [
    ("connected_system without tenant", {"origin": "connected_system"}),
    ("chat without tenant", {"origin": "chat"}),
    ("feed with tenant", {"tenant": "demo-retail"}),
    ("demo not simulated", {"origin": "demo"}),
    ("inference without extracted_by", {"origin": "news", "provenance": "inference"}),
    ("fact with extracted_by", {"extracted_by": "extract-fast"}),
    ("supersedes on v1", {"supersedes": signal_id("gdacs", "TC:1001234:6", 1)}),
    ("supersedes itself", {"version": 2, "supersedes": SIG_ID}),
    ("ends before starts", {"ends_at": "2026-10-06T00:00:00Z"}),
    ("naive datetime", {"observed_at": "2026-10-07T06:00:00"}),
    ("severity above 10000", {"severity_bp": 10001}),
    ("severity float", {"severity_bp": 72.5}),
    ("confidence negative", {"confidence_bp": -1}),
    ("bad UN/LOCODE", {"location_refs": ["UNLOCODE:VN-SGN"]}),
    ("bad ISO3166", {"location_refs": ["ISO3166:VNM"]}),
    ("unknown ref scheme", {"location_refs": ["CITY:SAIGON"]}),
    ("duplicate refs", {"location_refs": ["ISO3166:VN", "ISO3166:VN"]}),
    ("unknown kind", {"kind": "tsunami"}),
    ("unknown origin", {"origin": "email"}),
    ("bad subtype", {"subtype": "Tropical Cyclone"}),
    ("id not uuid", {"id": "sig-1"}),
    ("uppercase uuid", {"id": SIG_ID.upper()}),
    ("extra field", {"score": 3}),
    ("raw_text too long", {"raw_text": "x" * 4001}),
])
def test_signal_rejects(label: str, over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DisruptionSignal.model_validate(_signal(**over))


def test_signal_origin_rules_accept_valid_shapes() -> None:
    news = DisruptionSignal.model_validate(_signal(origin="news", provenance="inference", extracted_by="extract-fast",
                                                   raw_text="Cat Lai terminal suspends operations", geometry=None))
    assert news.geometry is None
    tenant = DisruptionSignal.model_validate(_signal(origin="connected_system", tenant="demo-retail",
                                                     kind="supplier", subtype="supplier_delay",
                                                     location_refs=["SUPPLIER:SUP-VN-01"], geometry=None))
    assert tenant.tenant == "demo-retail"
    demo = DisruptionSignal.model_validate(_signal(origin="demo", provenance="simulated", extracted_by="prebaked"))
    assert demo.simulated
    v2 = DisruptionSignal.model_validate(_signal(id=signal_id("gdacs", "TC:1001234:7", 2), version=2, supersedes=SIG_ID))
    assert v2.supersedes == SIG_ID


@pytest.mark.parametrize(("label", "geometry"), [
    ("unclosed ring", {"type": "Polygon", "coordinates": [CONE[:-1] + [[106.1, 9.5]]]}),
    ("short ring", {"type": "Polygon", "coordinates": [[[1.0, 1.0], [2.0, 2.0], [1.0, 1.0]]]}),
    ("lon out of range", {"type": "Point", "coordinates": [181.0, 10.0]}),
    ("lat out of range", {"type": "Point", "coordinates": [10.0, -90.5]}),
    ("nan", {"type": "Point", "coordinates": [float("nan"), 10.0]}),
    ("bool coordinate", {"type": "Point", "coordinates": [True, 10.0]}),
    ("string coordinate", {"type": "Point", "coordinates": ["106.7", 10.8]}),
    ("depth kept", {"type": "Point", "coordinates": [106.7, 10.8, 10.0]}),
    ("one-point line", {"type": "LineString", "coordinates": [[1.0, 1.0]]}),
    ("nested collection", {"type": "GeometryCollection", "geometries": [
        {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1.0, 1.0]}]}]}),
    ("unknown type", {"type": "Circle", "coordinates": [1.0, 1.0]}),
    ("feature instead of geometry", {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1.0, 1.0]}}),
    ("too many positions", {"type": "MultiPoint", "coordinates": [[0.0, 0.0]] * 50_001}),
])
def test_geometry_rejects(label: str, geometry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DisruptionSignal.model_validate(_signal(geometry=geometry))


def test_geometry_accepts_int_degrees_and_all_types() -> None:
    for geometry in (
        {"type": "Point", "coordinates": [106, 10]},
        {"type": "MultiPoint", "coordinates": [[106, 10], [107.5, 11]]},
        {"type": "MultiLineString", "coordinates": [[[1, 1], [2, 2]]]},
        {"type": "MultiPolygon", "coordinates": [[CONE]]},
    ):
        DisruptionSignal.model_validate(_signal(geometry=geometry))


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,x", "//evil.example/x", "http://user@host/x",
    "https://host/a b", "https://host\\x", "ftp://host/x",
])
def test_source_url_must_be_plain_http(url: str) -> None:
    bad = _signal()
    bad["source"]["url"] = url
    with pytest.raises(ValidationError):
        DisruptionSignal.model_validate(bad)
    assert not re.match(SOURCE_URL_PATTERN, url)


def test_severity_native_extras() -> None:
    assert SeverityNative.model_validate({"scheme": "usgs", "mag": 6.8, "tsunami": False, "alert": None}).scheme == "usgs"
    for bad in (
        {"scheme": "usgs", "mag": [6.8]},
        {"scheme": "usgs", "mag": float("inf")},
        {"scheme": "usgs", "Mag": 1},
        {"scheme": "usgs", "note": "x" * 201},
        {"scheme": "USGS"},
        {"scheme": "usgs", **{f"k{i}": i for i in range(21)}},
    ):
        with pytest.raises(ValidationError):
            SeverityNative.model_validate(bad)


def test_combine_confidence_bp() -> None:
    assert combine_confidence_bp(10000, 10000, 10000) == 10000
    assert combine_confidence_bp(9500, 8000, 5000) == 3800
    assert combine_confidence_bp(3333, 3333, 10000) == 1111  # 1110.9 → half-up
    assert combine_confidence_bp(0, 10000, 10000) == 0
    for bad in ((10001, 1, 1), (-1, 1, 1), (True, 1, 1)):
        with pytest.raises(ValueError):
            combine_confidence_bp(*bad)


# ─────────────────────────────── events ─────────────────────────────────────


def test_event_validates_and_maps_taxonomy() -> None:
    ev = DisruptionEvent.model_validate(_event())
    assert ev.taxonomy_key == "NAT.storm"
    eq = DisruptionEvent.model_validate(_event(subtype="earthquake", taxonomy_key="NAT.earthquake"))
    assert eq.taxonomy_key == "NAT.earthquake"
    news_only = DisruptionEvent.model_validate(_event(anchor_signal_id=None, kind="logistics", subtype="port_closure",
                                                      taxonomy_key="LOG.port_closure", status="candidate", geometry=None))
    assert news_only.anchor_signal_id is None


@pytest.mark.parametrize(("label", "over"), [
    ("anchor not a member", {"anchor_signal_id": signal_id("usgs", "us7000x", 1)}),
    ("duplicate members", {"member_signal_ids": [SIG_ID, SIG_ID]}),
    ("no members", {"member_signal_ids": []}),
    ("taxonomy mismatch", {"taxonomy_key": "NAT.flood"}),
    ("unknown taxonomy", {"taxonomy_key": "NAT.volcano"}),
    ("window reversed", {"window": {"starts_at": "2026-10-07T00:00:00Z", "ends_at": "2026-10-06T00:00:00Z"}}),
    ("unknown status", {"status": "open"}),
])
def test_event_rejects(label: str, over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DisruptionEvent.model_validate(_event(**over))


# ─────────────────────────────── exposure ───────────────────────────────────


def test_exposure_rules() -> None:
    Exposure.model_validate(_exposure())
    Exposure.model_validate(_exposure(method="dwithin_km", distance_km=42, node_type="factory", node_code="F-VN-01"))
    Exposure.model_validate(_exposure(method="lane_crosses", node_type="lane", node_code="SGN-RTM"))
    Exposure.model_validate(_exposure(method="entity_match", node_type="supplier", node_code="SUP-VN-01"))
    for bad in (
        _exposure(method="dwithin_km"),
        _exposure(method="lane_crosses"),
        _exposure(node_type="lane", method="intersects"),
        _exposure(distance_km=1.5),
        _exposure(weight_bp=10001),
        _exposure(tenant=None),
    ):
        with pytest.raises(ValidationError):
            Exposure.model_validate(bad)


def test_node_counts_of_counts_distinct_nodes() -> None:
    exposures = [Exposure.model_validate(e) for e in (
        _exposure(),
        _exposure(method="dwithin_km", distance_km=3),  # same port again
        _exposure(node_type="supplier", node_code="SUP-VN-01", method="entity_match"),
        _exposure(node_type="lane", node_code="SGN-RTM", method="lane_crosses"),
    )]
    counts = NodeCounts.of(exposures)
    assert (counts.port, counts.supplier, counts.lane, counts.dc) == (1, 1, 1, 0)
    assert (counts.sites, counts.total) == (2, 3)
    summary = ExposureSummary.model_validate(SUMMARY)
    assert summary.nodes_by_type.sites == 3
    with pytest.raises(ValidationError):
        ExposureSummary.model_validate({**SUMMARY, "nodes_by_type": {"port": 1}})  # every type is required


# ─────────────────────────────── alerts ─────────────────────────────────────


def test_alert_shapes() -> None:
    alert = Alert.model_validate(_alert())
    assert alert.exposure_summary.open_po_lines == 2
    Alert.model_validate(_alert(status="opened", case_id="case-1", simulated=True))
    for bad in (
        _alert(status="opened"),
        _alert(updated_at="2026-10-07T06:00:00Z"),
        _alert(severity="severe"),
        _alert(simulated="false"),
        _alert(why=""),
        _alert(tenant=None),
    ):
        with pytest.raises(ValidationError):
            Alert.model_validate(bad)


def test_alert_status_update() -> None:
    assert AlertStatusUpdate.model_validate({"status": "seen"}).model_dump(exclude_none=True) == {"status": "seen"}
    assert AlertStatusUpdate.model_validate({"status": "opened", "case_id": "c1"}).case_id == "c1"
    for bad in ({"status": "opened"}, {"status": "dismissed", "case_id": "c1"}, {"status": "new"}):
        with pytest.raises(ValidationError):
            AlertStatusUpdate.model_validate(bad)


def test_alert_detail_consistency() -> None:
    detail = {"alert": _alert(), "event": _event(), "signals": [_signal()], "exposures": [_exposure()]}
    AlertDetail.model_validate(detail)
    other_event = event_id("usgs", "us7000x")
    for bad in (
        {**detail, "alert": _alert(event_id=other_event)},
        {**detail, "signals": [_signal(id=signal_id("usgs", "us7000x", 1))]},
        {**detail, "exposures": [_exposure(tenant="other-tenant")]},
        {**detail, "exposures": [_exposure(event_id=other_event)]},
    ):
        with pytest.raises(ValidationError):
            AlertDetail.model_validate(bad)


@pytest.mark.parametrize(("bp", "band"), [(0, "low"), (3999, "low"), (4000, "medium"), (6999, "medium"),
                                          (7000, "high"), (8499, "high"), (8500, "critical"), (10000, "critical")])
def test_alert_severity_for(bp: int, band: str) -> None:
    assert alert_severity_for(bp) == band


def test_alert_severity_for_rejects_out_of_range() -> None:
    for bad in (-1, 10001, True):
        with pytest.raises(ValueError):
            alert_severity_for(bad)


def _counts(**over: int) -> NodeCounts:
    return NodeCounts(**{"supplier": 0, "factory": 0, "port": 0, "dc": 0, "store": 0, "lane": 0, **over})


def test_default_rules_follow_the_contract() -> None:
    def decide(sev: int, **counts: int) -> str | None:
        hit = decide_alert_action(DEFAULT_ALERT_RULES, severity_bp=sev, confidence_bp=5000, kind="natural_hazard",
                                  counts=_counts(**counts))
        return None if hit is None else hit[0]

    assert decide(3999, port=3) is None            # below the alert threshold
    assert decide(9000) is None                    # nothing exposed
    assert decide(4000, lane=1) == "alert"
    assert decide(6999, supplier=1) == "alert"
    assert decide(7000, port=1) == "alert_and_draft_case"
    assert decide(7500, factory=1) == "alert_and_draft_case"
    assert decide(9000, dc=2, lane=3) == "alert"   # no exposed supplier/factory/port → no draft


def test_ignore_rule_suppresses_and_kind_filter() -> None:
    ignore_labor = AlertRule(
        id="ignore-labor",
        when=AlertCondition(min_severity_bp=0, kinds=["labor"], min_confidence_bp=0, min_exposed_nodes=0, node_types=[]),
        action="ignore",
        hysteresis_bp=0,
    )
    rules = (*DEFAULT_ALERT_RULES, ignore_labor)
    hit = decide_alert_action(rules, severity_bp=9000, confidence_bp=9000, kind="labor", counts=_counts(port=1))
    assert hit is not None and hit[0] == "ignore" and hit[1].id == "ignore-labor"
    hit = decide_alert_action(rules, severity_bp=9000, confidence_bp=9000, kind="weather", counts=_counts(port=1))
    assert hit is not None and hit[0] == "alert_and_draft_case"
    strict_conf = AlertRule(
        id="confident",
        when=AlertCondition(min_severity_bp=0, kinds=[], min_confidence_bp=8000, min_exposed_nodes=1, node_types=["dc"]),
        action="alert",
        hysteresis_bp=500,
    )
    assert decide_alert_action([strict_conf], severity_bp=1, confidence_bp=7999, kind="cyber", counts=_counts(dc=1)) is None
    assert decide_alert_action([strict_conf], severity_bp=1, confidence_bp=8000, kind="cyber", counts=_counts(dc=1))


def test_alert_rule_validation() -> None:
    for bad in (
        {"id": "Bad Id", "when": DEFAULT_ALERT_RULES[0].when.model_dump(), "action": "alert", "hysteresis_bp": 0},
        {"id": "r", "when": {**DEFAULT_ALERT_RULES[0].when.model_dump(), "kinds": ["labor", "labor"]},
         "action": "alert", "hysteresis_bp": 0},
        {"id": "r", "when": DEFAULT_ALERT_RULES[0].when.model_dump(), "action": "page", "hysteresis_bp": 0},
        {"id": "r", "when": DEFAULT_ALERT_RULES[0].when.model_dump(), "action": "alert", "hysteresis_bp": 20000},
    ):
        with pytest.raises(ValidationError):
            AlertRule.model_validate(bad)


def test_hysteresis() -> None:
    assert not should_renotify(7200, 8100, 1000)
    assert should_renotify(7200, 8200, 1000)
    assert not should_renotify(7200, 6000, 1000)
    assert not should_renotify(7200, 7200, 0)   # an unchanged severity never re-notifies


# ─────────────────────────────── packs + network ────────────────────────────


def _pack(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "typhoon-hcmc",
        "title": "Typhoon approaching Ho Chi Minh City",
        "description": "Four GDACS advisories, one trade-press item.",
        "start_at": "2026-10-07T00:00:00Z",
        "steps": [
            {"t_offset_s": 0, "adapter": "gdacs", "payload": {"fixture": "advisory-1.geojson"}, "note": "orange"},
            {"t_offset_s": 21600, "adapter": "gdacs", "payload": {"fixture": "advisory-2.geojson"}, "note": ""},
            {"t_offset_s": 21600, "adapter": "rss", "payload": {"fixture": "feed.xml", "extraction": {"kind": "logistics"}},
             "note": "Demo Freight Daily"},
        ],
    }
    base.update(over)
    return base


def test_scenario_pack() -> None:
    pack = ScenarioPack.model_validate(_pack())
    start = dt.datetime(2026, 10, 7, tzinfo=dt.UTC)
    assert [i for i, _ in pack.due_steps(start)] == [0]
    assert [i for i, _ in pack.due_steps(start + dt.timedelta(hours=6), after_index=0)] == [1, 2]
    assert pack.step_ids() == ScenarioPack.model_validate(_pack()).step_ids()
    assert len(set(pack.step_ids())) == 3
    with pytest.raises(ValueError):
        pack.due_steps(dt.datetime(2026, 10, 7))
    for bad in (
        _pack(steps=[]),
        _pack(id="Typhoon HCMC"),
        _pack(steps=list(reversed(_pack()["steps"]))),
        _pack(steps=[{**_pack()["steps"][0], "adapter": "gdelt"}]),
        _pack(steps=[{**_pack()["steps"][0], "t_offset_s": -1}]),
        _pack(start_at="2026-10-07T00:00:00"),
    ):
        with pytest.raises(ValidationError):
            ScenarioPack.model_validate(bad)


def test_tenant_network() -> None:
    net = TenantNetwork.model_validate({
        "nodes": [{"type": "port", "code": "VNSGN", "name": "Ho Chi Minh City", "lat": 10.77, "lon": 106.7},
                  {"type": "factory", "code": "F-VN-01", "name": "Factory VN 1", "lat": 10, "lon": 106}],
        "lanes": [{"code": "SGN-RTM", "mode": "sea", "path": [[106.7, 10.77], [32.5, 30.0], [4.4, 51.9]]}],
    })
    assert net.nodes[1].lat == 10.0
    node = {"type": "port", "code": "VNSGN", "name": "x", "lat": 10.0, "lon": 106.0}
    for bad in (
        {"nodes": [node, node], "lanes": []},
        {"nodes": [{**node, "type": "lane"}], "lanes": []},
        {"nodes": [{**node, "lat": 91.0}], "lanes": []},
        {"nodes": [], "lanes": [{"code": "L", "mode": "sea", "path": [[1.0, 1.0]]}]},
        {"nodes": [], "lanes": [{"code": "L", "mode": "sea", "path": [[1.0, 1.0], [2.0, 2.0]]}] * 2},
    ):
        with pytest.raises(ValidationError):
            TenantNetwork.model_validate(bad)


# ─────────────────────────────── run payload ────────────────────────────────

ACTOR = {"sub": "system", "username": "signal-hub", "name": "Signal Hub", "roles": []}


def _run(payload: dict[str, Any]) -> RunRequest:
    return RunRequest.model_validate({"run_id": "r1", "tenant": "demo-retail", "case_id": "c1", "kind": "signal",
                                      "actor": ACTOR, "payload": payload})


def test_m5_replan_payload_unchanged() -> None:
    p = SignalPayload.model_validate({"signal_id": "5f1c0a8e-0000-7000-8000-000000000001"})
    assert p.model_dump() == {"signal_id": "5f1c0a8e-0000-7000-8000-000000000001"}
    assert p.model_dump(mode="json") == {"signal_id": "5f1c0a8e-0000-7000-8000-000000000001"}
    assert not p.is_external
    assert _run({"signal_id": "sig-1"}).model_dump(mode="json")["payload"] == {"signal_id": "sig-1"}
    assert PAYLOAD_BY_KIND["signal"] is SignalPayload


def test_m6_event_payload() -> None:
    p = SignalPayload.model_validate({"event_id": EVT_ID, "alert_id": ALERT_ID})
    assert p.is_external
    assert p.model_dump(mode="json") == {"event_id": EVT_ID, "alert_id": ALERT_ID}
    no_alert = SignalPayload.model_validate({"event_id": EVT_ID})
    assert no_alert.model_dump(mode="json") == {"event_id": EVT_ID, "alert_id": None}
    assert json.loads(no_alert.model_dump_json()) == {"event_id": EVT_ID, "alert_id": None}
    run = _run({"event_id": EVT_ID, "alert_id": None})
    assert run.model_dump(mode="json")["payload"] == {"event_id": EVT_ID, "alert_id": None}
    assert RunRequest.model_validate(run.model_dump(mode="json")) == run


@pytest.mark.parametrize("payload", [
    {},
    {"signal_id": "s", "event_id": EVT_ID},
    {"signal_id": "s", "alert_id": ALERT_ID},
    {"alert_id": ALERT_ID},
    {"event_id": ""},
    {"event_id": "x" * 201},
    {"event_id": EVT_ID, "case_id": "c1"},
])
def test_signal_payload_rejects(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SignalPayload.model_validate(payload)
    with pytest.raises(ValidationError):
        _run(payload)


def test_run_request_schema_has_both_signal_shapes() -> None:
    schema = json.dumps(RunRequest.model_json_schema())
    assert '"event_id"' in schema and '"alert_id"' in schema and '"signal_id"' in schema


# ─────────────────────────────── blocks / agents / llm ──────────────────────


def test_alert_origin_block() -> None:
    props = {"alert_id": ALERT_ID, "title": "Typhoon approaching Ho Chi Minh City", "severity": "critical",
             "kind": "natural_hazard", "source_names": ["GDACS"], "simulated": True,
             "opened_at": "2026-10-07T06:05:00Z"}
    block = make_block("alert_origin", props, title="Opened from alert",
                       provenance={"kind": "fact", "engine": "keelson-signals@0.1", "inputs_hash": None,
                                   "as_of": "2026-10-07"})
    assert block.placement == "inline"
    wire = block_to_wire(block)
    assert wire["kind"] == "alert_origin" and wire["props"]["opened_at"] == "2026-10-07T06:05:00Z"
    assert parse_block(wire) == block
    assert isinstance(block.props, AlertOriginProps)  # type: ignore[attr-defined]


def test_external_intel_agent() -> None:
    assert AGENT_DISPLAY_NAMES["external_intel"] == "External intelligence"


def _news(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"kind": "logistics", "subtype": "port_closure", "places": ["Ho Chi Minh City"],
                            "companies": [], "ports": ["Cat Lai"], "chokepoints": [], "dates_text": ["Thursday"],
                            "injection_suspected": False}
    base.update(over)
    return base


def test_news_extraction_is_strict_and_checked() -> None:
    assert NewsExtraction in LLM_OUTPUT_MODELS
    assert strict_schema_violations(NewsExtraction) == []
    ok = NewsExtraction.model_validate(_news())
    assert news_extraction_violations(ok) == []
    assert news_extraction_violations(NewsExtraction.model_validate(_news(injection_suspected=True)))
    assert news_extraction_violations(NewsExtraction.model_validate(_news(subtype="Port Closure")))
    assert news_extraction_violations(NewsExtraction.model_validate(_news(ports=["x" * 81])))
    assert news_extraction_violations(NewsExtraction.model_validate(_news(places=["p"] * 21)))
    assert news_extraction_violations(NewsExtraction.model_validate(_news(companies=["  "])))
    with pytest.raises(ValidationError):
        NewsExtraction.model_validate(_news(kind="tsunami"))
    with pytest.raises(ValidationError):
        NewsExtraction.model_validate({k: v for k, v in _news().items() if k != "dates_text"})


# ───────────────────────────── review regressions ───────────────────────────


@pytest.mark.parametrize("bad", [
    "../../v1/demo/packs/typhoon-hcmc/reset",
    "evt-1",
    EVT_ID.upper(),
    EVT_ID + "/../alerts",
    EVT_ID + "?tenant=other",
    EVT_ID + "\n",
])
def test_signal_payload_hub_ids_are_canonical_uuids(bad: str) -> None:
    """``event_id`` / ``alert_id`` end up in Signal Hub URL paths: anything but a canonical UUID is
    rejected (no path traversal / query injection into ``/v1/events/{id}``)."""
    with pytest.raises(ValidationError):
        SignalPayload.model_validate({"event_id": bad, "alert_id": None})
    with pytest.raises(ValidationError):
        SignalPayload.model_validate({"event_id": EVT_ID, "alert_id": bad})
    with pytest.raises(ValidationError):
        _run({"event_id": bad, "alert_id": None})
    # the M5 re-plan reference keeps its looser shape
    assert SignalPayload.model_validate({"signal_id": "sig-1"}).model_dump() == {"signal_id": "sig-1"}


def test_default_alert_rules_are_immutable() -> None:
    """The defaults are shared module state: one tenant's rule handling must not change them."""
    rule = DEFAULT_ALERT_RULES[0]
    with pytest.raises(ValidationError):
        rule.when.min_severity_bp = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        rule.hysteresis_bp = 0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rule.when.node_types.append("dc")  # type: ignore[attr-defined]
    assert isinstance(DEFAULT_ALERT_RULES[1].when.node_types, tuple)
    # variants are derived by copy, the defaults stay as they were
    lowered = rule.model_copy(update={"when": rule.when.model_copy(update={"min_severity_bp": 1000})})
    assert lowered.when.min_severity_bp == 1000 and DEFAULT_ALERT_RULES[0].when.min_severity_bp == 4000
    # wire shape is unchanged (JSON arrays) and round-trips
    wire = DEFAULT_ALERT_RULES[1].model_dump(mode="json")
    assert wire["when"]["node_types"] == ["supplier", "factory", "port"] and wire["when"]["kinds"] == []
    assert AlertRule.model_validate(wire) == DEFAULT_ALERT_RULES[1]
    assert AlertRule.model_validate_json(json.dumps(wire)) == DEFAULT_ALERT_RULES[1]


@pytest.mark.parametrize("subtype", ["typhoon", "hurricane", "cyclone", "tropical_storm", "tropical_cyclone"])
def test_tropical_cyclone_aliases_map_to_storm(subtype: str) -> None:
    assert taxonomy_key_for(subtype) == "NAT.storm"
