"""M8 feed parsers (``docs/contracts/124-m8-signal-feeds.md`` §2) on recorded real-shaped fixtures:
NHC (+ KML/KMZ), Copernicus EMS, MeteoAlarm (+ CAP detail), OFAC SDN CSV trio, GDELT 2.0 exports.
Offline; every draft is also validated against the contract model."""

from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pytest

from conftest import FIXTURES, T0, load_fixture
from keelson_contracts.signals import DisruptionSignal, signal_id
from signal_hub.adapters import copernicus_ems, gdelt, kml, meteoalarm, nhc, ofac
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext

CTX = SourceContext(retrieved_at=T0)
LATER = SourceContext(retrieved_at=T0 + dt.timedelta(hours=12))
DEMO = SourceContext(retrieved_at=T0, tenant="demo-retail", pack_id="feeds-test")


def _valid(draft: SignalDraft, version: int = 1) -> DisruptionSignal:
    return draft.to_signal(signal_id=signal_id(draft.connector_id, draft.native_key, version), version=version,
                           supersedes=None, confidence_bp=9000, event_id=None)


def kmz(*members: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


# ───────────────────────────── NHC ─────────────────────────────


def _storms() -> dict[str, object]:
    return json.loads(load_fixture("nhc_current_storms.json"))


def _tern_gis() -> list[dict[str, object]]:
    cone = kml.kml_geometries(kml.read_kml_or_kmz(kmz(("al142026_012adv_CONE.kml", load_fixture("nhc_al_cone.kml")))))
    track = kml.kml_geometries(kml.read_kml_or_kmz(kmz(("al142026_012adv_TRACK.kml", load_fixture("nhc_al_track.kml")),
                                                       ("mPoint.png", b"\x89PNG not really"))))
    return [*cone, *track]


def test_nhc_storms_severity_and_times() -> None:
    drafts = {d.native_key: d for d in nhc.parse_nhc(json.loads(load_fixture("nhc_current_storms.json")), CTX)}
    assert set(drafts) == {"al142026", "ep192026", "al152026"}  # the malformed id is dropped
    hu, ts, ptc = drafts["al142026"], drafts["ep192026"], drafts["al152026"]
    for d in drafts.values():
        sig = _valid(d)
        assert sig.provenance == "fact" and sig.origin == "feed" and sig.tenant is None and sig.extracted_by is None
        assert d.kind == "natural_hazard" and d.subtype == "tropical_cyclone" and d.authoritative
        assert d.location_refs == [] and d.connector_id == "nhc"
        assert d.license == "Public domain (NOAA/NWS)" and d.authority_bp == 9500
    issued = dt.datetime(2026, 10, 6, 21, tzinfo=dt.UTC)
    assert hu.severity_bp == 8500 and hu.severity_native["category"] == 3  # 105 kt = H3
    assert ts.severity_bp == 5000 and ptc.severity_bp == 5000  # 45 kt / 35 kt
    assert hu.title == "Hurricane Tern (NHC advisory 12)" and ptc.title == "Potential Tropical Cyclone Fifteen (NHC advisory 2A)"
    assert hu.native_id == "al142026:012" and ptc.native_id == "al152026:002A"
    assert hu.starts_at == issued - dt.timedelta(hours=6 * 11)  # genesis estimate from the advisory number
    assert ptc.starts_at == issued - dt.timedelta(hours=6)  # "002A" → 2
    assert hu.observed_at == issued and hu.expires_at == issued + dt.timedelta(hours=12) and hu.ends_at is None
    assert hu.severity_native == {"scheme": "nhc", "storm_id": "al142026", "basin": "AL", "classification": "HU",
                                  "category": 3, "intensity_kt": 105, "pressure_mb": 955, "adv_num": "012"}
    assert hu.source_url == "https://www.nhc.noaa.gov/text/MIATCPAT1.shtml"
    assert hu.geometry == {"type": "Point", "coordinates": [-71.2, 24.5]}  # no GIS: the position only
    assert "105 kt (category 3)" in (hu.summary or "") and "moving 315° at 11 kt" in (hu.summary or "")


def test_nhc_cone_and_track_kmz_merge_with_the_position() -> None:
    (hu,) = [d for d in nhc.parse_nhc(_storms(), CTX, gis={"al142026": _tern_gis()}) if d.native_key == "al142026"]
    assert hu.geometry is not None and hu.geometry["type"] == "GeometryCollection"
    types = [g["type"] for g in hu.geometry["geometries"]]
    assert types == ["Polygon", "LineString", "Point"]  # the track's forecast points are dropped
    cone = hu.geometry["geometries"][0]["coordinates"][0]
    assert cone[0] == cone[-1] and all(len(p) == 2 for p in cone)  # closed, altitude dropped
    _valid(hu)


def test_nhc_gis_urls_only_https_on_the_nhc_host() -> None:
    storms = {s["id"]: s for s in nhc.storms(_storms())}
    assert nhc.gis_urls(storms["al142026"]) == [
        "https://www.nhc.noaa.gov/storm_graphics/api/AL142026_012adv_CONE.kmz",
        "https://www.nhc.noaa.gov/storm_graphics/api/AL142026_012adv_TRACK.kmz",
    ]
    assert nhc.gis_urls(storms["al152026"]) == []  # another host / plain http
    assert nhc.gis_urls({"trackCone": {"kmzFile": "https://www.nhc.noaa.gov:8443/x.kmz"}}) == []
    assert nhc.gis_urls({"trackCone": {"kmzFile": "https://user@www.nhc.noaa.gov/x.kmz"}}) == []
    assert nhc.advisory_tag(storms["al152026"]) == "002A"


def test_nhc_malformed_feed_is_an_error_not_an_empty_list() -> None:
    for doc in ({}, {"activeStorms": "none"}, [], None):
        with pytest.raises(nhc.NhcError):
            nhc.parse_nhc(doc, CTX)
    assert nhc.parse_nhc({"activeStorms": []}, CTX) == []


def test_nhc_demo_context_is_simulated_and_sanitises_names() -> None:
    doc = _storms()
    doc["activeStorms"][0]["name"] = "<script>alert(1)</script>"  # type: ignore[index]
    (hu, *_) = nhc.parse_nhc(doc, DEMO)
    sig = _valid(hu)
    assert sig.origin == "demo" and sig.provenance == "simulated" and sig.tenant == "demo-retail"
    assert hu.connector_id == "demo.nhc" and hu.native_key == "demo-retail/feeds-test/al142026"
    assert hu.title == "Hurricane Unnamed (NHC advisory 12)" and hu.source_name.endswith("(demo replay)")


def test_kml_refuses_entities_and_non_kml() -> None:
    bomb = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><kml>&b;</kml>'
    with pytest.raises(kml.KmlError):
        kml.kml_geometries(bomb)
    with pytest.raises(kml.KmlError):
        kml.kml_geometries(b"<html><body/></html>")
    with pytest.raises(kml.KmlError):
        kml.kml_geometries(b"not xml")


# ───────────────────────────── Copernicus EMS ─────────────────────────────


def test_ems_category_mapping_closed_and_refs(gaz: Gazetteer) -> None:
    drafts, skipped = copernicus_ems.parse_ems(json.loads(load_fixture("copernicus_ems_activations.json")), CTX, gaz)
    by = {d.native_key: d for d in drafts}
    assert set(by) == {"EMSR995", "EMSR994", "EMSR993", "EMSR991"} and skipped == 2  # industrial accident, bad code
    flood, storm, fire, slide = by["EMSR995"], by["EMSR994"], by["EMSR993"], by["EMSR991"]
    for d in drafts:
        _valid(d)
        assert d.authoritative and d.provenance == "fact" and d.authority_bp == 9000
    assert (flood.kind, flood.subtype, flood.severity_bp) == ("natural_hazard", "flood", 6500)
    assert (storm.kind, storm.subtype, storm.severity_bp) == ("weather", "wind_storm", 6500)
    assert (fire.subtype, fire.severity_bp) == ("wildfire", 5000) and (slide.subtype, slide.severity_bp) == ("landslide", 5000)
    assert flood.location_refs == ["ISO3166:GR"] and fire.location_refs == ["ISO3166:ES", "ISO3166:PT"]
    assert slide.location_refs == ["ISO3166:VN"]  # "Viet Nam"
    assert flood.geometry == {"type": "Point", "coordinates": [22.4191, 39.639]}
    assert slide.geometry is None  # bad WKT is ignored
    assert flood.starts_at == dt.datetime(2026, 10, 5, 7, tzinfo=dt.UTC)
    assert flood.expires_at == dt.datetime(2026, 10, 19, 11, 2, tzinfo=dt.UTC) and flood.ends_at is None  # activation + 14 d
    closed_at = dt.datetime(2026, 9, 17, 12, 50, 19, 784825, tzinfo=dt.UTC)
    assert fire.expires_at == closed_at and fire.ends_at == closed_at  # closed: the last update, not the poll time
    assert fire.title == "EMSR993: Wildfire in Huelva Province, Spain"  # control character stripped
    assert flood.severity_native["gdacs_id"] == "FL1104200" and flood.severity_native["products"] == 14
    assert flood.source_url == "https://mapping.emergency.copernicus.eu/activations/EMSR995/"
    # the same answer polled later is the same content (no new version every poll)
    later, _ = copernicus_ems.parse_ems(json.loads(load_fixture("copernicus_ems_activations.json")), LATER, gaz)
    assert [d.content_hash() for d in later] == [d.content_hash() for d in drafts]


def test_ems_centroid_forms_and_bad_answers() -> None:
    assert copernicus_ems.parse_centroid("POINT (-7.21 37.78)") == {"type": "Point", "coordinates": [-7.21, 37.78]}
    assert copernicus_ems.parse_centroid({"type": "Point", "coordinates": [1, 2]}) == {"type": "Point", "coordinates": [1.0, 2.0]}
    assert copernicus_ems.parse_centroid({"lat": 2, "lon": 1}) == {"type": "Point", "coordinates": [1.0, 2.0]}
    assert copernicus_ems.parse_centroid("POLYGON ((0 0, 1 1, 0 1, 0 0))") is None
    assert copernicus_ems.parse_centroid("POINT (500 10)") is None
    with pytest.raises(copernicus_ems.EmsError):
        copernicus_ems.parse_ems({"code": "Activation code parameter is mandatory"}, CTX, None)  # type: ignore[arg-type]


# ───────────────────────────── MeteoAlarm ─────────────────────────────


def test_meteoalarm_atom_level_filter_and_types() -> None:
    warnings = meteoalarm.parse_atom(load_fixture("meteoalarm_netherlands.atom"))
    assert len(warnings) == 7
    drafts, skipped = meteoalarm.parse_meteoalarm(warnings, LATER, connector="meteoalarm.netherlands", country_iso="NL")
    by = {d.native_key.split("#")[1]: d for d in drafts}
    # yellow (below orange), the cancelled warning and fog are skipped
    assert set(by) == {"NL812", "NL811", "NL001", "NL808"} and skipped == 3
    wind, coast, rain = by["NL812"], by["NL001"], by["NL808"]
    assert (wind.kind, wind.subtype, wind.severity_bp) == ("weather", "wind_storm", 6000)
    assert (coast.kind, coast.subtype, coast.severity_bp) == ("weather", "coastal_flood", 8500)
    assert (rain.subtype, rain.severity_bp) == ("heavy_rain", 6000)
    for d in drafts:
        sig = _valid(d)
        assert sig.provenance == "fact" and not d.authoritative and d.location_refs == ["ISO3166:NL"]
        assert d.connector_id == "meteoalarm.netherlands" and d.geometry is None  # no CAP detail fetched
    assert wind.native_key == "2.49.0.0.528.0.NL.20261007091503.test-orange-wind_3#NL812"
    assert wind.starts_at == dt.datetime(2026, 10, 7, 18, tzinfo=dt.UTC)
    assert wind.ends_at == wind.expires_at == dt.datetime(2026, 10, 8, 6, tzinfo=dt.UTC)
    assert wind.title == "Orange wind warning: Zierikzee (MeteoAlarm)"
    assert wind.severity_native["awareness_level"] == 3 and wind.severity_native["emma_id"] == "NL812"
    assert wind.source_url is not None and wind.source_url.startswith("https://meteoalarm.org/?geocode=")
    red_only, _ = meteoalarm.parse_meteoalarm(warnings, LATER, connector="meteoalarm.netherlands", country_iso="NL",
                                              min_level=4)
    assert [d.subtype for d in red_only] == ["coastal_flood"]


def test_meteoalarm_cap_detail_polygon_circle_and_level() -> None:
    detail = meteoalarm.parse_cap_detail(load_fixture("meteoalarm_cap_detail.xml"))
    assert detail.level == 3 and detail.type_no == 1 and set(detail.areas) == {"NL812", "NL811"}
    ring = detail.areas["NL812"]["coordinates"][0]
    assert ring[0] == [4.06, 51.85] and ring[0] == ring[-1]  # CAP "lat,lon" → GeoJSON [lon, lat]
    circle = detail.areas["NL811"]
    assert circle["type"] == "Polygon" and len(circle["coordinates"][0]) == 33  # 32-gon, closed
    warnings = meteoalarm.parse_atom(load_fixture("meteoalarm_netherlands.atom"))
    ident = "2.49.0.0.528.0.NL.20261007091503.test-orange-wind_3"
    drafts, _ = meteoalarm.parse_meteoalarm(warnings, LATER, connector="meteoalarm.netherlands", country_iso="NL",
                                            details={ident: detail})
    by = {d.native_key.split("#")[1]: d for d in drafts}
    assert by["NL812"].geometry == detail.areas["NL812"] and by["NL811"].geometry == circle
    assert by["NL808"].geometry is None
    for d in drafts:
        _valid(d)


def test_meteoalarm_detail_urls_are_host_restricted() -> None:
    warnings = meteoalarm.parse_atom(load_fixture("meteoalarm_netherlands.atom"))
    urls = meteoalarm.detail_urls(warnings)
    assert "2.49.0.0.528.0.NL.20261007080000.test-orange-rain_1" not in urls  # plain http
    assert all(u.startswith("https://feeds.meteoalarm.org/api/") for u in urls.values()) and len(urls) == 5
    assert meteoalarm.detail_urls(warnings, allowed_host="feeds.example.org") == {}


def test_meteoalarm_refuses_entities_and_non_atom() -> None:
    bomb = (b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>')
    with pytest.raises(meteoalarm.MeteoAlarmError):
        meteoalarm.parse_atom(bomb)
    with pytest.raises(meteoalarm.MeteoAlarmError):
        meteoalarm.parse_atom(b"<rss/>")
    with pytest.raises(meteoalarm.MeteoAlarmError):
        meteoalarm.parse_cap_detail(b"<feed/>")
    assert meteoalarm.feed_url("https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}", "united-kingdom") \
        == "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"


# ───────────────────────────── OFAC ─────────────────────────────


def _ofac(n: int) -> dict[str, ofac.Entity]:
    d = FIXTURES / "ofac"
    return ofac.parse_entities((d / f"SDN_{n}.CSV").read_bytes(), (d / f"ALT_{n}.CSV").read_bytes(),
                               (d / f"ADD_{n}.CSV").read_bytes())


def test_ofac_csv_quirks_and_organisations_only() -> None:
    one = _ofac(1)
    assert sorted(one) == ["9001", "9002", "9005", "9006"]  # the individual and the vessel are skipped
    assert one["9001"].programs == ["IRAN", "SDGT"] and one["9001"].aliases == ["EXAMPLE TRADING HOUSE"]
    assert one["9002"].name == "FICTIONAL FREIGHT FORWARDERS S.A." and one["9002"].countries == ["Cuba"]
    assert one["9005"].aliases == ["SAMPLE METALLURGICAL WORKS OJSC"]  # fka counts
    rows = ofac.parse_rows(b'1,"A, B",-0- ,"X"\r\n\r\n2,"C",-0-,"Y"\r\n\x1a', min_columns=4)
    assert rows == [["1", "A, B", None, "X"], ["2", "C", None, "Y"]]
    two = _ofac(2)  # cp1252
    assert two["9008"].name == "SOCIÉTÉ EXEMPLE SARL"
    with pytest.raises(ofac.OfacError):
        ofac.parse_rows(b'1,"only two"\r\n', min_columns=4)
    with pytest.raises(ofac.OfacError):
        ofac.parse_rows(b'1,"unterminated\r\n', min_columns=1)


def test_ofac_entity_hash_and_diff() -> None:
    one, two = _ofac(1), _ofac(2)
    assert ofac.hashes(one) == ofac.hashes(_ofac(1))  # stable
    delta = ofac.diff(ofac.hashes(two), ofac.hashes(one))
    assert delta.new == ["9007", "9008"] and delta.changed == ["9002"] and delta.removed == ["9006"] and delta.any
    assert not ofac.diff(ofac.hashes(one), ofac.hashes(one)).any


def test_ofac_publication_draft() -> None:
    one, two = _ofac(1), _ofac(2)
    delta = ofac.diff(ofac.hashes(two), ofac.hashes(one))
    published = dt.datetime(2026, 10, 6, 19, 35, 12, tzinfo=dt.UTC)
    draft = ofac.publication_draft(two, delta, CTX, published_at=published)
    sig = _valid(draft)
    assert (sig.kind, sig.subtype, sig.severity_bp) == ("regulatory", "sanctions_designation", 8000)
    assert sig.geometry is None and sig.location_refs == [] and draft.authoritative
    assert draft.native_key == "sdn:20261006T193512Z" and draft.expires_at == published + dt.timedelta(days=30)
    assert draft.match_keys == ["example shell fabrics", "example shell textiles", "fictional forwarding",
                                "fictional freight forwarders", "societe exemple"]
    assert draft.severity_native["new"] == 2 and draft.severity_native["changed"] == 1
    assert draft.severity_native["removed"] == 1 and draft.severity_native["programs"] == "CUBA, IRAN-EO13846, SDGT"
    assert "EXAMPLE" not in draft.title and "SHELL" not in (draft.summary or "")  # no entity names in text
    assert "not a sanctions-screening result" in (draft.summary or "")
    many = {str(i): ofac.Entity(str(i), f"Org Number {i:06d} Trading") for i in range(25_000)}
    big = ofac.publication_draft(many, ofac.Delta(sorted(many), [], []), CTX, published_at=published)
    assert len(big.match_keys) == ofac.MAX_MATCH_KEYS


# ───────────────────────────── GDELT ─────────────────────────────


def test_gdelt_lastupdate_host_and_path() -> None:
    lu = gdelt.parse_lastupdate(load_fixture("gdelt_lastupdate.txt"))
    assert lu.slot == "20261007101500" and lu.size == 47561 and lu.md5 == "4f7c0a6e1b2d3c4e5f60718293a4b5c6"
    assert lu.url == "https://data.gdeltproject.org/gdeltv2/20261007101500.export.CSV.zip"  # upgraded to https
    for bad in (b"1 0123456789abcdef0123456789abcdef http://evil.example/gdeltv2/20261007101500.export.CSV.zip",
                b"1 0123456789abcdef0123456789abcdef http://data.gdeltproject.org/other/20261007101500.export.CSV.zip",
                b"x 0123456789abcdef0123456789abcdef http://data.gdeltproject.org/gdeltv2/20261007101500.export.CSV.zip",
                b"1 nothex http://data.gdeltproject.org/gdeltv2/20261007101500.export.CSV.zip",
                b"nothing here"):
        with pytest.raises(gdelt.GdeltError):
            gdelt.parse_lastupdate(bad)


def test_gdelt_missed_slots() -> None:
    assert gdelt.missed_slots("20261007101500", None, 4) == ["20261007101500"]
    assert gdelt.missed_slots("20261007101500", "20261007101500", 4) == []
    assert gdelt.missed_slots("20261007100000", "20261007101500", 4) == []  # a stale lastupdate.txt
    assert gdelt.processed_slot("20261007100000", "20261007101500") == "20261007101500"  # never backwards
    assert gdelt.processed_slot("20261007101500", "20261007100000") == "20261007101500"
    assert gdelt.processed_slot("20261007101500", None) == "20261007101500"
    assert gdelt.missed_slots("20261007101500", "20261007094500", 4) == ["20261007100000", "20261007101500"]
    assert gdelt.missed_slots("20261007101500", "20261006000000", 2) == ["20261007094500", "20261007100000",
                                                                          "20261007101500"]


def test_gdelt_filter_and_aggregation(gaz: Gazetteer) -> None:
    body = load_fixture("gdelt_export.CSV")
    rows = gdelt.matching_rows(gdelt.read_export(kmz(("20261007101500.export.CSV", body))), gdelt.DEFAULT_FILTER, gaz)
    keys = sorted({r.key for r in rows})
    assert keys == ["20261007:armed_conflict:UNLOCODE:BDCGP", "20261007:blockade:CHOKEPOINT:BAB_EL_MANDEB",
                    "20261007:civil_unrest:UNLOCODE:DEHAM", "20261007:strike:UNLOCODE:BDCGP",
                    "20261007:strike:UNLOCODE:NLRTM", "20261007:trade_restriction:UNLOCODE:SGSIN"]
    agg: dict[str, dict[str, object]] = {}
    touched = gdelt.aggregate(agg, rows)
    assert touched == set(keys)
    ctg = agg["20261007:strike:UNLOCODE:BDCGP"]
    assert ctg["events"] == 3 and ctg["articles"] == 17 and ctg["codes"] == ["143", "1431", "1434"]
    assert ctg["url"] == "https://news.example/chattogram-dock-workers-strike"  # the javascript: URL is dropped
    narrow = gdelt.Filter(codes=("143",), min_articles=5)
    assert {r.key for r in gdelt.matching_rows(body, narrow, gaz)} == {"20261007:strike:UNLOCODE:BDCGP"}


def test_gdelt_drafts_are_capped_inference(gaz: Gazetteer) -> None:
    agg: dict[str, dict[str, object]] = {}
    keys = gdelt.aggregate(agg, gdelt.matching_rows(load_fixture("gdelt_export.CSV"), gdelt.DEFAULT_FILTER, gaz))
    drafts = {d.native_key: d for d in gdelt.drafts_for(agg, keys, LATER, gaz)}
    ctg = drafts["20261007:strike:UNLOCODE:BDCGP"]
    sig = _valid(ctg)
    assert sig.provenance == "inference" and sig.extracted_by == "gdelt-cameo" and sig.origin == "news"
    assert (ctg.kind, ctg.subtype, ctg.severity_bp, ctg.authority_bp) == ("labor", "strike", 3900, 4000)
    assert not ctg.authoritative and ctg.location_refs == ["UNLOCODE:BDCGP", "ISO3166:BD"]
    assert ctg.title == "Strike or boycott reported near Chittagong (GDELT, 16+ articles)"
    assert ctg.geometry == {"type": "Point", "coordinates": [91.8, 22.3089]}
    assert ctg.starts_at == dt.datetime(2026, 10, 7, tzinfo=dt.UTC)
    assert ctg.expires_at == dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
    assert ctg.severity_native["articles_bucket"] == 16 and ctg.severity_native["events_bucket"] == 2
    bab = drafts["20261007:blockade:CHOKEPOINT:BAB_EL_MANDEB"]
    assert bab.location_refs == ["CHOKEPOINT:BAB_EL_MANDEB"] and bab.geometry is not None
    assert bab.geometry["type"] == "Polygon" and bab.kind == "logistics"  # 191 and 144x are one subtype
    for d in drafts.values():
        _valid(d)
        assert d.severity_bp <= 3900
    # a bucket change is a new version; more articles inside the same bucket are not
    hashes = {k: d.content_hash() for k, d in drafts.items()}
    agg["20261007:strike:UNLOCODE:BDCGP"]["articles"] = 31
    same = {d.native_key: d.content_hash() for d in gdelt.drafts_for(agg, keys, LATER, gaz)}
    assert same["20261007:strike:UNLOCODE:BDCGP"] == hashes["20261007:strike:UNLOCODE:BDCGP"]  # 31 is still 16+
    agg["20261007:strike:UNLOCODE:BDCGP"]["articles"] = 32
    bumped = {d.native_key: d.content_hash() for d in gdelt.drafts_for(agg, keys, LATER, gaz)}
    assert bumped["20261007:strike:UNLOCODE:BDCGP"] != hashes["20261007:strike:UNLOCODE:BDCGP"]
    assert gdelt.drafts_for(agg, keys, LATER, gaz, cap_bp=1000)[0].severity_bp == 1000


def test_gdelt_prune_bounds_the_cursor() -> None:
    agg = {f"2026100{d}:strike:UNLOCODE:BDCGP": {"events": 1, "articles": 3} for d in (1, 5, 6, 7)}
    gdelt.prune(agg, T0 + dt.timedelta(hours=12))  # keeps 2026-10-05 onwards
    assert sorted(k[:8] for k in agg) == ["20261005", "20261006", "20261007"]
