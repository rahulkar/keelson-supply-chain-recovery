"""Gazetteer over data/geo (ports.json is new in M6)."""

from __future__ import annotations

import json
import re

import pytest

from conftest import GEO_DIR, REPO
from signal_hub.gazetteer import Gazetteer, normalise


@pytest.mark.parametrize(("mention", "code"), [
    ("Cat Lai terminal", "VNSGN"), ("Ho Chi Minh City", "VNSGN"), ("Saigon", "VNSGN"), ("VNSGN", "VNSGN"),
    ("the port of Rotterdam", "NLRTM"), ("Gdańsk", "PLGDN"), ("GDANSK port", "PLGDN"), ("Chattogram", "BDCGP"),
    ("Port Klang", "MYPKG"), ("Jebel Ali Port", "AEJEA"), ("Yantian", "CNYTN"), ("Antwerp-Bruges", "BEANR"),
])
def test_ports(gaz: Gazetteer, mention: str, code: str) -> None:
    port = gaz.port(mention)
    assert port is not None and port.code == code


@pytest.mark.parametrize("mention", ["", "Atlantis", "x" * 81, "port", "the"])
def test_unknown_ports(gaz: Gazetteer, mention: str) -> None:
    assert gaz.port(mention) is None


def test_chokepoints_countries_regions(gaz: Gazetteer) -> None:
    assert gaz.chokepoint("Suez Canal").code == "SUEZ"  # type: ignore[union-attr]
    assert gaz.chokepoint("the Red Sea").code == "BAB_EL_MANDEB"  # type: ignore[union-attr]
    assert gaz.chokepoint("Strait of Malacca").code == "MALACCA"  # type: ignore[union-attr]
    assert gaz.country("Viet Nam") == "VN" and gaz.country("Vietnamese") == "VN" and gaz.country("VNM") == "VN"
    assert gaz.iso2("PHL") == "PH" and gaz.iso2("vn") == "VN" and gaz.iso2("ZZZ") is None
    assert gaz.region("European Union") == "EU"
    ring = gaz.chokepoints["SUEZ"].ring
    assert ring[0] == ring[-1] and all(isinstance(c, float) for p in ring for c in p)


def test_geocode(gaz: Gazetteer) -> None:
    res = gaz.geocode(places=["Ho Chi Minh City", "Viet Nam", "Europe", "Narnia"], ports=["Cat Lai"],
                      chokepoints=["Suez"])
    assert res.location_refs == ["UNLOCODE:VNSGN", "ISO3166:VN", "CHOKEPOINT:SUEZ", "REGION:EUROPE"]
    assert res.unresolved == ["Narnia"]
    geom = res.merged_geometry()
    assert geom is not None and geom["type"] == "GeometryCollection"
    assert [g["type"] for g in geom["geometries"]] == ["Point", "Polygon"]
    assert gaz.geocode().merged_geometry() is None


def test_ports_json_covers_every_merxa_and_route_port() -> None:
    ports = json.loads((GEO_DIR / "ports.json").read_text(encoding="utf-8"))["ports"]
    for code, p in ports.items():
        assert re.match(r"^[A-Z]{2}[A-Z2-9]{3}$", code)
        assert -90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180 and isinstance(p["lat"], float)
        assert p["country"] == code[:2]
    routes = json.loads((GEO_DIR / "sea_routes.json").read_text(encoding="utf-8"))
    route_ports = {k for k, v in routes["places"].items() if v["kind"] == "port"}
    assert route_ports <= set(ports)
    network = REPO / "services" / "merxa-api" / "src" / "merxa_api" / "seed" / "network.py"
    if network.is_file():
        merxa_ports = set(re.findall(r'^\s*\("([A-Z]{2}[A-Z2-9]{3})", "', network.read_text(encoding="utf-8"), re.M))
        assert merxa_ports and merxa_ports <= set(ports)
    assert len(ports) >= 60


def test_normalise() -> None:
    assert normalise("  Gdańsk,  PORT! ") == "gdansk port"


def test_gazetteer_loads_from_dir() -> None:
    assert Gazetteer(GEO_DIR).ports["VNSGN"].country == "VN"
