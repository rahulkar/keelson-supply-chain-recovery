"""M8 Signals admin sources (``docs/contracts/124-m8-signal-feeds.md`` §4): the engine passes the
hub's new catalogue facts through (cleaned; a non-https terms link is dropped), takes an opt-in
source's default from the hub (unsubscribed until the admin enables it) and never re-enables one
on a cadence-only change. The hub is mocked (``FakeHub``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from e1_signal_hub import HUB, HUB_SECRET, FakeHub
from keelson_engine.config import Settings
from keelson_engine.core.db import system_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

SIG = "/api/admin/signals"
NHC = {"connector_id": "nhc", "name": "NOAA National Hurricane Center", "kind": "hazard", "provenance": "fact",
       "license": "Public domain (NOAA/NWS)", "licence_class": "public_domain",
       "attribution": "Source: NOAA National Hurricane Center", "terms_url": "https://www.weather.gov/disclaimer",
       "authority_bp": 9500, "default_cadence_s": 900, "min_cadence_s": 300, "max_cadence_s": 86400,
       "polled": True, "opt_in": False, "coverage": "Atlantic, East and Central Pacific tropical cyclones",
       "description": "Authoritative advisories with the forecast cone and track (KMZ)."}
GDELT = {"connector_id": "gdelt", "name": "GDELT Project 2.0 events", "kind": "news", "provenance": "inference",
         "license": "GDELT Project (open; citation required)", "licence_class": "open_attribution",
         "attribution": "Source: The GDELT Project", "terms_url": "javascript:alert(document.cookie)",
         "authority_bp": 4000, "default_cadence_s": 900, "polled": True, "opt_in": True,
         "coverage": "x" * 400, "description": "Low-authority inference."}
METEO = {"connector_id": "meteoalarm.netherlands", "name": "MeteoAlarm warnings: Netherlands", "kind": "weather",
         "provenance": "fact", "license": "MeteoAlarm / EUMETNET", "licence_class": "terms_apply",
         "terms_url": "https://user:pw@evil.example/", "polled": False, "opt_in": True, "default_cadence_s": 900}


@pytest_asyncio.fixture(loop_scope="session")
async def hub(platform: None, settings: Settings, http_mock: respx.Router,
              monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeHub]:
    monkeypatch.setattr(settings, "signal_hub_url", HUB)
    monkeypatch.setattr(settings, "signal_hub_shared_secret", SecretStr(HUB_SECRET))
    fake = FakeHub()
    fake.catalogue = [*fake.catalogue, NHC, GDELT, METEO]
    fake.install(http_mock)
    async with system_session() as s:
        await s.execute(text("UPDATE tenant SET config = config - 'signal_sources' WHERE slug = :t"), {"t": TENANT_A})
    yield fake


@pytest_asyncio.fixture(loop_scope="session")
async def sam(browser_factory: Any, platform: None) -> Browser:
    return await browser_factory("sam")


async def _items(sam: Browser) -> dict[str, dict[str, Any]]:
    r = await sam.get(f"{SIG}/sources")
    assert r.status_code == 200, r.text
    return {s["id"]: s for s in r.json()["items"]}


async def test_catalogue_facts_pass_through_cleaned(sam: Browser, hub: FakeHub) -> None:
    items = await _items(sam)
    nhc = items["nhc"]
    assert (nhc["provenance"], nhc["licence_class"], nhc["terms_url"]) == ("fact", "public_domain",
                                                                          "https://www.weather.gov/disclaimer")
    assert nhc["opt_in"] is False and nhc["polled"] is True and nhc["authority_bp"] == 9500
    assert (nhc["min_cadence_s"], nhc["max_cadence_s"]) == (300, 86400)
    assert nhc["licence"] == "Public domain (NOAA/NWS)" and nhc["attribution"].startswith("Source: NOAA")
    assert nhc["coverage"].startswith("Atlantic") and nhc["description"].startswith("Authoritative")
    gdelt, meteo = items["gdelt"], items["meteoalarm.netherlands"]
    assert gdelt["terms_url"] is None and meteo["terms_url"] is None  # javascript: / userinfo never reach the web
    assert gdelt["provenance"] == "inference" and len(gdelt["coverage"]) == 300  # clipped
    assert meteo["polled"] is False and meteo["licence_class"] == "terms_apply"
    # opt-in sources default to unsubscribed (the hub's answer), the M7 ones to subscribed
    assert gdelt["enabled"] is False and meteo["enabled"] is False and nhc["enabled"] is True
    old = items["gdacs"]  # an M7-shaped row: the new fields are simply absent
    assert old["provenance"] is None and old["opt_in"] is None and old["terms_url"] is None


async def test_opt_in_subscribe_and_cadence_only_changes(sam: Browser, hub: FakeHub) -> None:
    # a cadence-only change on an opt-in source the tenant never enabled keeps it unsubscribed
    r = await sam.patch(f"{SIG}/sources/gdelt", json={"cadence_s": 3600})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["cadence_s"] == 3600, r.text
    assert {x["connector_id"]: x for x in hub.subscriptions[TENANT_A]}["gdelt"] == {
        "connector_id": "gdelt", "enabled": False, "cadence_s": 3600}
    # subscribing is an explicit enabled: true (pushed in the whole map)
    r = await sam.patch(f"{SIG}/sources/gdelt", json={"enabled": True})
    assert r.json()["enabled"] is True and r.json()["hub_synced"] is True
    assert (await _items(sam))["gdelt"]["enabled"] is True
    # an M7 source without a stored wish still defaults to subscribed
    r = await sam.patch(f"{SIG}/sources/nhc", json={"cadence_s": 1800})
    assert r.json()["enabled"] is True
    # hub down: the unknown default falls back to the M7 rule (subscribed)
    hub.down = True
    r = await sam.patch(f"{SIG}/sources/meteoalarm.netherlands", json={"cadence_s": 1800})
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["hub_synced"] is False
    hub.down = False
