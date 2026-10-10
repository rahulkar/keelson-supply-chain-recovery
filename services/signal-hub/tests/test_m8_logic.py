"""M8 feeds pure logic (``docs/contracts/124-m8-signal-feeds.md`` §0-§1): safe zip reading, name
match keys, hash stability of earlier signals, scoring tables, the per-subtype same-hazard window
and radii, the source registry and the feed settings. Offline."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import logging
import zipfile

import pytest

from conftest import T0, load_fixture
from signal_hub import admin, cluster, geo, names, poller, safezip, scoring, sources
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.adapters.usgs import parse_usgs
from signal_hub.cluster import Candidate, SignalFeatures, choose_event, radius_km, same_hazard_window
from signal_hub.config import RssFeed, Settings, load_settings, parse_cameo_codes, parse_meteoalarm_countries
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SourceContext
from signal_hub.replay_feeds import StepError, fixture_names

H = dt.timedelta(hours=1)
EV = "11111111-1111-5111-8111-111111111111"


def _zip(members: list[tuple[str, bytes]], *, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


# ───────────────────────────── safezip ─────────────────────────────


def test_safezip_reads_one_kml_member() -> None:
    body = _zip([("al142026_012adv_CONE.kml", b"<kml/>"), ("mPoint.png", b"png")])
    assert safezip.is_zip(body) and not safezip.is_zip(b"<kml/>")
    assert safezip.read_member(body, suffixes=(".kml",)) == b"<kml/>"
    assert safezip.read_member(_zip([("X.EXPORT.CSV", b"a\tb")]), suffixes=(".export.csv",)) == b"a\tb"


@pytest.mark.parametrize(
    ("members", "problem"),
    [
        ([("a.kml", b"1"), ("b.kml", b"2")], "ambiguous"),
        ([("../evil.kml", b"1")], "unsafe member name"),
        ([("/abs.kml", b"1")], "unsafe member name"),
        ([("dir\\x.kml", b"1")], "unsafe member name"),
        ([("doc.txt", b"1")], "no member"),
    ],
)
def test_safezip_refuses(members: list[tuple[str, bytes]], problem: str) -> None:
    with pytest.raises(safezip.UnsafeArchive, match=problem):
        safezip.read_member(_zip(members), suffixes=(".kml",))


def test_safezip_refuses_bombs_lies_and_encryption() -> None:
    bomb = _zip([("bomb.kml", b"\0" * (4 * 1024 * 1024))])  # ~4 MiB of zeros deflates ~1000:1
    with pytest.raises(safezip.UnsafeArchive, match="ratio"):
        safezip.read_member(bomb, suffixes=(".kml",))
    with pytest.raises(safezip.UnsafeArchive, match="declares"):
        safezip.read_member(_zip([("big.kml", b"x" * 5000)], method=zipfile.ZIP_STORED), suffixes=(".kml",),
                            max_uncompressed=1000)
    # a header that lies about the size: patch the declared size in both headers to 10 bytes
    honest = _zip([("lie.kml", b"<kml>" + b"y" * 4000 + b"</kml>")], method=zipfile.ZIP_STORED)
    info = zipfile.ZipFile(io.BytesIO(honest)).infolist()[0]
    lying = bytearray(honest)
    for offset in (22, lying.find(b"PK\x01\x02") + 24):  # local header / central directory: uncompressed size
        lying[offset:offset + 4] = (10).to_bytes(4, "little")
    assert info.file_size > 10
    with pytest.raises(safezip.UnsafeArchive):
        safezip.read_member(bytes(lying), suffixes=(".kml",), max_uncompressed=100, max_ratio=10_000)
    encrypted = bytearray(_zip([("e.kml", b"secret")], method=zipfile.ZIP_STORED))
    for sig, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = encrypted.find(sig) + flag_offset
        encrypted[at] |= 0x1
    with pytest.raises(safezip.UnsafeArchive, match="encrypted"):
        safezip.read_member(bytes(encrypted), suffixes=(".kml",))
    with pytest.raises(safezip.UnsafeArchive, match="not a zip"):
        safezip.read_member(b"PK\x03\x04garbage", suffixes=(".kml",))
    many = _zip([(f"f{i}.txt", b"") for i in range(70)])
    with pytest.raises(safezip.UnsafeArchive, match="members"):
        safezip.read_member(many, suffixes=(".kml",))


# ───────────────────────────── names ─────────────────────────────


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Ningbo Shell Fabrics Co.", "ningbo shell fabrics"),
        ("NINGBO SHELL FABRICS COMPANY LIMITED", "ningbo shell fabrics"),
        ("Example Shell Fabrics Co.", "example shell fabrics"),
        ("Société Exemple S.A.R.L.", "societe exemple"),
        ("The Anglo-Caribbean Co., Ltd.", "anglo caribbean"),
        ("A.B.C. Trading & Co", "abc trading"),
        ("Smith & Sons GmbH", "smith and sons"),
        ("Dhaka Knit Pvt. Ltd.", "dhaka knit"),
        ("CIMEX", None),
        ("Co Ltd", None),
        ("AB", None),
        ("", None),
        (None, None),
        ("x" * 600, None),
    ],
)
def test_match_key(name: str | None, key: str | None) -> None:
    assert names.match_key(name) == key


def test_match_keys_are_exact_not_fuzzy() -> None:
    assert names.match_key("Ningbo Shell Fabric") != names.match_key("Ningbo Shell Fabrics Co.")
    assert names.match_keys(["Beta Two Co", "Alpha One Ltd", "beta two company", None, "X"]) == ["alpha one", "beta two"]
    assert names.match_keys(["Alpha Beta", "Gamma Delta", "Eps Zeta"], limit=2) == ["alpha beta", "eps zeta"]


# ───────────────────────────── hashes stay byte-identical ─────────────────────────────


def test_golden_content_hashes_of_earlier_sources_unchanged(gaz: Gazetteer) -> None:
    """``SignalDraft.match_keys`` enters the hash only when set: every GDACS / USGS signal stored
    before M8 keeps its hash (no re-versioning on the first M8 poll). Values computed with the M7 code."""
    ctx = SourceContext(retrieved_at=T0)
    footprints = {("TC", 1001102): json.loads(load_fixture("gdacs_tc_geometry.json"))["features"]}
    gd = {d.native_key: d.content_hash() for d in parse_gdacs(json.loads(load_fixture("gdacs_eventlist.json")), ctx, gaz,
                                                              extra_footprints=footprints)}
    assert gd["TC:1001102"] == "b7004dcda078251ed0c03392a1828d6c8260948b15f0909a71521c4c0d844807"
    assert gd["EQ:1500321"] == "24c5c8d2846db80d2123b7b3089e44315bd2135ec2adde70a3afd6ae77a21893"
    assert gd["FL:1102800"] == "c0ac8488b8d2ed7914eb1afcf5a5dce09cbda746d068eadfc35cbbdd822baa7e"
    us = {d.native_key: d.content_hash() for d in parse_usgs(json.loads(load_fixture("usgs_4.5_day.geojson")), ctx, gaz)}
    assert us["us7000zz01"] == "884796853b8639245ef7e5574e0305b0690fbbf307e59a62f517353a1a41d781"
    draft = parse_usgs(json.loads(load_fixture("usgs_4.5_day.geojson")), ctx, gaz)[0]
    with_keys = dataclasses.replace(draft, match_keys=["ningbo shell fabrics"])
    assert with_keys.content_hash() != draft.content_hash()
    assert dataclasses.replace(draft, match_keys=["b", "a"]).content_hash() == \
        dataclasses.replace(draft, match_keys=["a", "b"]).content_hash()


# ───────────────────────────── scoring ─────────────────────────────


@pytest.mark.parametrize(
    ("kt", "bp"),
    [(None, 3000), (30, 3000), (34, 5000), (49, 5000), (50, 5500), (63, 5500), (64, 6500), (82, 6500), (83, 7500),
     (95, 7500), (96, 8500), (112, 8500), (113, 9200), (136, 9200), (137, 9700), (160, 9700)],
)
def test_nhc_severity_bands(kt: int | None, bp: int) -> None:
    assert scoring.nhc_severity_bp(kt) == bp


def test_feed_scoring_tables() -> None:
    assert scoring.AUTHORITY["nhc"] == scoring.AUTHORITY["ofac"] == 9500
    assert scoring.AUTHORITY["copernicus_ems"] == scoring.AUTHORITY["meteoalarm"] == 9000
    assert scoring.AUTHORITY["gdelt"] == 4000
    assert scoring.METEOALARM_SEVERITY == {2: 3000, 3: 6000, 4: 8500}
    assert scoring.OFAC_SEVERITY_BP == 8000
    assert all(scoring.gdelt_severity_bp(s, 3900) == 3900 for s in scoring.GDELT_PRIOR)
    assert scoring.gdelt_severity_bp("unknown", 3900) == 3000
    # the M6/M7 news priors are unchanged (GDELT keeps its own table)
    assert "blockade" not in scoring.NEWS_SEVERITY and scoring.news_severity_bp("armed_conflict") == 3000


# ───────────────────────────── clustering ─────────────────────────────


def _cand(**over: object) -> Candidate:
    base: dict[str, object] = {"event_id": EV, "kind": "natural_hazard", "subtype": "tropical_cyclone", "anchored": True,
                               "anchor_connector": "gdacs", "window_start": T0, "window_end": None,
                               "last_observed": T0, "distance_km": 20.0, "refs": frozenset()}
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


def test_same_hazard_window_per_subtype() -> None:
    assert same_hazard_window("tropical_cyclone") == same_hazard_window("flood") == 72 * H
    assert same_hazard_window("wildfire") == same_hazard_window("wind_storm") == 72 * H
    assert same_hazard_window("earthquake") == same_hazard_window("volcano") == 6 * H


def test_nhc_anchor_joins_a_gdacs_cyclone_within_72_h() -> None:
    nhc = SignalFeatures(kind="natural_hazard", subtype="tropical_cyclone", connector_id="nhc", authoritative=True,
                         starts_at=T0 + 40 * H, ends_at=None)
    assert choose_event(nhc, [_cand()]) == EV  # GDACS dates from genesis, NHC from its advisory count
    assert choose_event(nhc, [_cand(window_start=T0 - 40 * H)]) is None  # 80 h apart
    assert choose_event(nhc, [_cand(distance_km=150.0)]) is None
    assert choose_event(nhc, [_cand(anchor_connector="nhc")]) is None  # never its own feed
    ems = SignalFeatures(kind="natural_hazard", subtype="flood", connector_id="copernicus_ems", authoritative=True,
                         starts_at=T0 + 30 * H, ends_at=None)
    assert choose_event(ems, [_cand(subtype="flood", distance_km=60.0)]) == EV
    usgs = SignalFeatures(kind="natural_hazard", subtype="earthquake", connector_id="usgs", authoritative=True,
                          starts_at=T0 + 7 * H, ends_at=T0 + 7 * H)
    assert choose_event(usgs, [_cand(subtype="earthquake")]) is None  # earthquakes keep the 6 h window


def test_radius_overrides_win_over_the_storm_rule() -> None:
    assert radius_km("wind_storm") == 25 and cluster.taxonomy_key_for("wind_storm") == "NAT.storm"
    assert radius_km("tropical_cyclone") == 300 and radius_km("hurricane") == 300
    assert (radius_km("coastal_flood"), radius_km("heavy_rain"), radius_km("thunderstorm")) == (25, 25, 10)
    assert (radius_km("wildfire"), radius_km("landslide"), radius_km("volcano")) == (25, 10, 100)
    assert radius_km("sanctions_designation") == 50 and radius_km("flood") == 50


# ───────────────────────────── geo helpers ─────────────────────────────


def test_geo_point_and_ring_helpers() -> None:
    assert round(geo.haversine_km(91.8, 22.3089, 91.83, 22.33)) == 4
    ring = [[42.9, 13.1], [43.8, 13.1], [43.8, 12.2], [42.9, 12.2], [42.9, 13.1]]
    assert geo.point_in_ring(43.3, 12.6, ring) and geo.distance_to_ring_km(43.3, 12.6, ring) == 0.0
    assert 40 < geo.distance_to_ring_km(44.2, 12.6, ring) < 50  # 0.4° east at 12.6° N
    circle = geo.circle_polygon(5.1, 52.46, 8)
    assert circle is not None and len(circle["coordinates"][0]) == 33
    assert geo.circle_polygon(5.1, 52.46, 0) is None


# ───────────────────────────── registry + config ─────────────────────────────


def test_catalogue_order_limits_and_opt_in() -> None:
    s = Settings(rss_feeds=(RssFeed("rss.x", "X Daily", "https://x.example/feed"),),
                 meteoalarm_countries=("netherlands", "united-kingdom"))
    cat = {c.connector_id: c for c in admin.source_catalogue(s)}
    assert list(cat) == ["gdacs", "usgs", "rss.x", "nhc", "copernicus_ems", "ofac", "gdelt",
                         "meteoalarm.netherlands", "meteoalarm.united-kingdom"]
    assert [c for c, i in cat.items() if i.opt_in] == ["gdelt", "meteoalarm.netherlands", "meteoalarm.united-kingdom"]
    assert not cat["meteoalarm.netherlands"].polled and cat["gdelt"].polled  # not in the default SIGNAL_ADAPTERS
    assert (cat["nhc"].min_cadence_s, cat["nhc"].default_cadence_s) == (300, 900)
    assert (cat["ofac"].min_cadence_s, cat["ofac"].default_cadence_s) == (1800, 3600)
    assert cat["nhc"].license == "Public domain (NOAA/NWS)" and cat["nhc"].licence_class == "public_domain"
    assert cat["copernicus_ems"].licence_class == "open_attribution" and cat["meteoalarm.netherlands"].licence_class == "terms_apply"
    assert cat["gdelt"].provenance == "inference" and cat["gdelt"].authority_bp == 4000
    assert cat["ofac"].kind == "regulatory" and "not a sanctions-screening service" in (cat["ofac"].description or "")
    assert cat["meteoalarm.united-kingdom"].name == "MeteoAlarm warnings: United Kingdom"
    assert cat["meteoalarm.netherlands"].host == "feeds.meteoalarm.org"
    for info in cat.values():
        assert info.terms_url is None or info.terms_url.startswith("https://")
        assert len(info.license) <= 120 and info.attribution and info.coverage
    d = cat["gdelt"].as_dict()
    assert {"kind", "provenance", "licence_class", "attribution", "terms_url", "opt_in", "coverage", "description"} <= set(d)
    assert sources.is_opt_in("meteoalarm.netherlands") and sources.is_opt_in("gdelt") and not sources.is_opt_in("ofac")
    assert sources.adapter_of("rss.theloadstar") == "rss" and sources.MULTI_CONNECTOR == {"rss", "meteoalarm"}


def test_feed_settings_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_meteoalarm_countries(" Netherlands, united-kingdom ,netherlands") == ("netherlands", "united-kingdom")
    for bad in ("nl!", "x", "a" * 50):
        with pytest.raises(ValueError):
            parse_meteoalarm_countries(bad)
    assert parse_cameo_codes("143, 1724,143") == ("143", "1724")
    for bad in ("", "14x", "999", "1"):
        with pytest.raises(ValueError):
            parse_cameo_codes(bad)
    monkeypatch.setenv("METEOALARM_COUNTRIES", "germany")
    monkeypatch.setenv("GDELT_EVENT_CODES", "143,191")
    monkeypatch.setenv("CADENCE_NHC_S", "1200")
    monkeypatch.delenv("SIGNAL_ADAPTERS", raising=False)
    s = load_settings()
    assert s.meteoalarm_countries == ("germany",) and s.gdelt_event_codes == ("143", "191") and s.cadence_nhc_s == 1200
    assert s.enabled_adapters == ("gdacs", "usgs", "rss", "nhc", "copernicus_ems", "ofac", "gdelt")
    assert s.gdelt_lastupdate_url.startswith("https://data.gdeltproject.org/")
    monkeypatch.setenv("METEOALARM_COUNTRIES", "bad slug")
    with pytest.raises(ValueError):
        load_settings()


def test_pack_step_fixture_names() -> None:
    assert fixture_names("nhc", {"fixture": "a.json", "gis": {"al992026": ["c.kml", "t.kmz"]}}) == ["a.json", "c.kml", "t.kmz"]
    assert fixture_names("meteoalarm", {"fixture": "f.atom", "details": {"id-1": "d.xml"}}) == ["f.atom", "d.xml"]
    assert fixture_names("gdelt", {"fixture": "g.CSV"}) == ["g.CSV"]
    for adapter, payload in (("nhc", {}), ("nhc", {"fixture": "a.json", "gis": ["x"]}),
                             ("meteoalarm", {"fixture": "f.atom", "details": {"x": 3}})):
        with pytest.raises(StepError):
            fixture_names(adapter, payload)


async def test_once_honours_the_operator_allow_list(caplog: pytest.LogCaptureFixture) -> None:
    """``--once`` never polls a feed adapter outside ``SIGNAL_ADAPTERS`` (Cloud Run runs one job per
    adapter): the allow-list is what keeps an opt-in subscription from opening egress (124 §2.2).
    It exits 0 before touching the database or the network."""
    s = Settings(enabled_adapters=("gdacs", "usgs", "rss"), database_url="postgresql://hub@unreachable.invalid/x")
    with caplog.at_level(logging.INFO, logger="signal_hub.poller"):
        for adapter in ("ofac", "gdelt", "meteoalarm"):
            assert await poller.run_once(s, adapter) == 0
    assert sum("SIGNAL_ADAPTERS" in r.getMessage() for r in caplog.records) == 3
