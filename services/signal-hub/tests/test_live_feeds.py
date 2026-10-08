"""Live checks of the M8 feeds against the real publishers (``124`` §2) — opt-in only.

Run where outbound HTTP is allowed (through ``HTTPS_PROXY`` when set), never in the default suite::

    SIGNAL_HUB_LIVE=1 pytest -m live tests/test_live_feeds.py

They confirm the recorded fixture shapes still hold: every parser runs on today's data without an
exception and every draft validates against the contract. Nothing is stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import AsyncIterator

import pytest

from keelson_contracts.signals import signal_id
from signal_hub.adapters import copernicus_ems, gdelt, kml, meteoalarm, nhc, ofac
from signal_hub.config import load_settings
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher, build_feed_client
from signal_hub.models import SignalDraft, SourceContext

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("SIGNAL_HUB_LIVE"), reason="live feed checks are opt-in (SIGNAL_HUB_LIVE=1)"),
]


def _valid(drafts: list[SignalDraft]) -> None:
    for d in drafts:
        d.to_signal(signal_id=signal_id(d.connector_id, d.native_key, 1), version=1, supersedes=None,
                    confidence_bp=9000, event_id=None)


@pytest.fixture
async def fetcher() -> AsyncIterator[Fetcher]:
    settings = load_settings()
    client = build_feed_client(settings)  # the production client: egress proxy, destination guard
    yield Fetcher(client, min_interval_s=1.0, max_bytes=settings.http_max_bytes)
    await client.aclose()


def _ctx() -> SourceContext:
    return SourceContext(retrieved_at=dt.datetime.now(dt.UTC))


async def test_live_nhc(fetcher: Fetcher) -> None:
    settings = load_settings()
    parsed = json.loads((await fetcher.get("nhc", settings.nhc_url, accept="application/json")).body)
    gis: dict[str, list[dict[str, object]]] = {}
    for storm in nhc.storms(parsed)[:1]:
        for url in nhc.gis_urls(storm):
            body = (await fetcher.get("nhc", url)).body
            gis.setdefault(storm["id"].lower(), []).extend(kml.kml_geometries(kml.read_kml_or_kmz(body)))
    _valid(nhc.parse_nhc(parsed, _ctx(), gis=gis))


async def test_live_copernicus_ems(fetcher: Fetcher, gaz: Gazetteer) -> None:
    body = (await fetcher.get("copernicus_ems", load_settings().copernicus_ems_url, accept="application/json")).body
    drafts, _ = copernicus_ems.parse_ems(json.loads(body), _ctx(), gaz)
    assert drafts
    _valid(drafts)


async def test_live_ofac_sdn_parses(fetcher: Fetcher) -> None:
    settings = load_settings()
    files = {n: (await fetcher.get("ofac", u, accept="text/csv", max_bytes=settings.ofac_max_bytes)).body
             for n, u in (("SDN", settings.ofac_sdn_url), ("ALT", settings.ofac_alt_url), ("ADD", settings.ofac_add_url))}
    entities = ofac.parse_entities(files["SDN"], files["ALT"], files["ADD"])
    assert len(entities) > 1000
    sample = dict(list(entities.items())[:2000])
    draft = ofac.publication_draft(sample, ofac.Delta(sorted(sample), [], []), _ctx(), published_at=dt.datetime.now(dt.UTC))
    _valid([draft])


async def test_live_gdelt(fetcher: Fetcher, gaz: Gazetteer) -> None:
    settings = load_settings()
    latest = gdelt.parse_lastupdate((await fetcher.get("gdelt", settings.gdelt_lastupdate_url)).body)
    body = (await fetcher.get("gdelt", latest.url, max_bytes=gdelt.MAX_EXPORT_BYTES)).body
    assert len(body) == latest.size and hashlib.md5(body).hexdigest() == latest.md5  # noqa: S324
    agg: dict[str, dict[str, object]] = {}
    keys = gdelt.aggregate(agg, gdelt.matching_rows(gdelt.read_export(body), gdelt.DEFAULT_FILTER, gaz))
    _valid(gdelt.drafts_for(agg, keys, _ctx(), gaz))


async def test_live_meteoalarm(fetcher: Fetcher, gaz: Gazetteer) -> None:
    settings = load_settings()
    slug = (settings.meteoalarm_countries or ("netherlands",))[0]
    body = (await fetcher.get("meteoalarm", meteoalarm.feed_url(settings.meteoalarm_url_template, slug))).body
    warnings = meteoalarm.parse_atom(body)
    details = {}
    for ident, url in list(meteoalarm.detail_urls(warnings).items())[:1]:
        details[ident] = meteoalarm.parse_cap_detail((await fetcher.get("meteoalarm", url)).body)
    drafts, _ = meteoalarm.parse_meteoalarm(warnings, _ctx(), connector=meteoalarm.connector_id(slug),
                                            country_iso=gaz.country(slug.replace("-", " ")), min_level=2, details=details)
    _valid(drafts)
