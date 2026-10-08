"""M8 signal feeds contracts (``docs/contracts/124-m8-signal-feeds.md``): the new demo-pack step
adapters and the subtype → taxonomy entries of the weather feeds. Everything else (the source
catalogue, ``match_keys``) is internal to the Signal Hub and not part of the contract."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.common import EVENT_TYPE_KEYS
from keelson_contracts.signals import PACK_ADAPTERS, SUBTYPE_TAXONOMY, ScenarioPack, taxonomy_key_for


def _pack(adapter: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "feeds-test",
        "title": "Feeds (simulated)",
        "description": "SIMULATED.",
        "start_at": "2026-10-07T00:00:00Z",
        "steps": [{"t_offset_s": 0, "adapter": adapter, "payload": payload, "note": ""}],
    }


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        ("nhc", {"fixture": "nhc-1.json", "gis": {"al992026": ["nhc-1-cone.kml"]}}),
        ("copernicus_ems", {"fixture": "ems-1.json"}),
        ("meteoalarm", {"fixture": "meteoalarm-1.atom", "connector_id": "meteoalarm.netherlands"}),
        ("gdelt", {"fixture": "gdelt-1.CSV"}),
    ],
)
def test_new_pack_adapters_validate(adapter: str, payload: dict[str, Any]) -> None:
    pack = ScenarioPack.model_validate(_pack(adapter, payload))
    assert pack.steps[0].adapter == adapter
    assert adapter in PACK_ADAPTERS


def test_ofac_is_not_a_pack_adapter() -> None:
    # a simulated designation matching seed supplier names is not acceptable demo data (124 §5)
    assert "ofac" not in PACK_ADAPTERS
    with pytest.raises(ValidationError):
        ScenarioPack.model_validate(_pack("ofac", {"fixture": "SDN.CSV"}))


def test_m6_adapters_unchanged() -> None:
    assert PACK_ADAPTERS[:5] == ("gdacs", "usgs", "rss", "merxa_inject", "chat_hint")


@pytest.mark.parametrize(
    ("subtype", "key"),
    [
        ("wind_storm", "NAT.storm"),
        ("coastal_flood", "NAT.flood"),
        ("heavy_rain", "NAT.flood"),
        ("tropical_cyclone", "NAT.storm"),
        ("flood", "NAT.flood"),
    ],
)
def test_weather_subtypes_map(subtype: str, key: str) -> None:
    assert SUBTYPE_TAXONOMY[subtype] == key
    assert taxonomy_key_for(subtype) == key


@pytest.mark.parametrize(
    "subtype",
    ["sanctions_designation", "blockade", "armed_conflict", "civil_unrest", "trade_restriction",
     "state_of_emergency", "snow_ice", "thunderstorm", "heat", "cold", "wildfire", "landslide", "volcano"],
)
def test_other_feed_subtypes_stay_other(subtype: str) -> None:
    # no REG.* key in M8 (the intake enum would change and miss every recorded replay cassette)
    assert subtype not in SUBTYPE_TAXONOMY
    assert taxonomy_key_for(subtype) == "OTHER"


def test_taxonomy_values_are_event_type_keys() -> None:
    assert set(SUBTYPE_TAXONOMY.values()) <= set(EVENT_TYPE_KEYS)
