"""Regression tests from the independent review (offline: no database, no network, no LLM).

* feed SSRF guard: every request and every redirect hop of the feed client must go to a public
  destination (no internal names, no private/link-local/metadata addresses);
* parsers survive malformed-but-JSON feed shapes instead of raising (a crash used to end the poller);
* GDACS latest episode per (type, id) with numeric-string ids;
* future-dated news / connector timestamps are capped at the retrieval time;
* an unavailable LLM never counts towards the invalid-output limit.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from conftest import T0, load_fixture
from fakes import FakeLlm
from signal_hub.adapters.connector import ConnectorEvent, parse_connector_event
from signal_hub.adapters.gdacs import footprint_urls, latest_episodes, parse_gdacs
from signal_hub.adapters.rss import parse_feed
from signal_hub.adapters.usgs import parse_usgs
from signal_hub.config import Settings
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher, FetchError, build_feed_client, host_is_public_name
from signal_hub.models import SourceContext
from signal_hub.quarantine import run_quarantine

PUBLIC = "93.184.216.34"


# ───────────────────────────── SSRF guard ─────────────────────────────


@pytest.mark.parametrize("host", ["www.gdacs.org", "earthquake.usgs.gov", "theloadstar.com", PUBLIC, "[2606:4700::1111]"])
def test_public_hosts_allowed(host: str) -> None:
    assert host_is_public_name(host)


@pytest.mark.parametrize("host", [
    "litellm", "pg-core", "signal-hub-api", "localhost", "merxa.localhost", "metadata.google.internal",
    "printer.local", "router.lan", "127.0.0.1", "10.1.2.3", "172.20.0.5", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "[::1]", "[fd00::1]", "[fe80::1]", "[::ffff:10.0.0.1]", "224.0.0.1", "",
])
def test_internal_hosts_refused(host: str) -> None:
    assert not host_is_public_name(host)


def _redirecting(target: str) -> httpx.MockTransport:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "news.example.com":
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=b"<secret/>")

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
    "http://litellm:4000/v1/models",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://10.8.0.3:5432/",
    "http://[::1]:8300/v1/alerts",
])
async def test_feed_redirect_to_internal_destination_is_refused(target: str) -> None:
    transport = _redirecting(target)
    # with an egress proxy configured the guard checks names/literals (the proxy blocks the rest)
    client = build_feed_client(Settings(outbound_proxy="http://egress:3128"), transport=transport)
    fetcher = Fetcher(client, min_interval_s=0)
    with pytest.raises(FetchError, match="not allowed"):
        await fetcher.get("rss.x", "https://news.example.com/feed/")
    assert transport.seen == ["https://news.example.com/feed/"]  # type: ignore[attr-defined]  # never reached
    await client.aclose()


async def test_without_proxy_names_resolving_to_private_addresses_are_refused() -> None:
    dns = {"news.example.com": [PUBLIC], "rebind.example.net": ["10.0.0.7"], "mixed.example.org": [PUBLIC, "127.0.0.1"]}

    async def resolver(host: str, _port: int) -> list[str]:
        if host not in dns:
            raise OSError("NXDOMAIN")
        return dns[host]

    for target, ok in (("https://rebind.example.net/x", False), ("https://mixed.example.org/x", False),
                       ("https://news.example.com/final", True)):
        transport = _redirecting(target) if target != "https://news.example.com/final" else httpx.MockTransport(
            lambda _r: httpx.Response(200, content=b"<rss/>"))
        client = build_feed_client(Settings(outbound_proxy=None), resolver=resolver, transport=transport)
        fetcher = Fetcher(client, min_interval_s=0)
        if ok:
            assert (await fetcher.get("rss.x", target)).body == b"<rss/>"
        else:
            with pytest.raises(FetchError, match="non-public"):
                await fetcher.get("rss.x", "https://news.example.com/feed/")
        await client.aclose()
    client = build_feed_client(Settings(outbound_proxy=None), resolver=resolver,
                               transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(FetchError):
        await Fetcher(client, min_interval_s=0).get("rss.x", "https://unknown.example/feed")
    await client.aclose()


# ───────────────────────────── malformed feed shapes ─────────────────────────────


def _ctx() -> SourceContext:
    return SourceContext(retrieved_at=T0)


@pytest.mark.parametrize("doc", [
    [], "x", 5, None, {"features": "x"}, {"features": {"a": 1}},
    {"features": [1, "x", None, {"properties": ["TC"]}, {"properties": "TC", "geometry": {"type": "Point"}}]},
    {"features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [106.7, 10.8]},
                   "properties": {"eventtype": "TC", "eventid": 7, "affectedcountries": 5, "url": "x",
                                  "severitydata": [1], "alertlevel": ["Red"]}}]},
])
def test_gdacs_parser_survives_malformed_shapes(doc: Any, gaz: Gazetteer) -> None:
    drafts = parse_gdacs(doc, _ctx(), gaz)
    assert isinstance(drafts, list)
    assert footprint_urls(doc, allowed_host="www.gdacs.org") == {}
    assert isinstance(latest_episodes(doc), dict)


@pytest.mark.parametrize("doc", [
    [], {"features": "x"}, {"features": [{"id": "us1", "properties": "mag 5"}, {"id": "us2", "properties": ["x"]},
                                         {"id": "us3", "properties": {"mag": 5.1}, "geometry": ["x"]}]},
])
def test_usgs_parser_survives_malformed_shapes(doc: Any, gaz: Gazetteer) -> None:
    assert parse_usgs(doc, _ctx(), gaz) == []


def test_gdacs_latest_episode_per_type_and_id() -> None:
    feats = [
        {"properties": {"eventtype": "TC", "eventid": "1001102", "episodeid": "13"}},
        {"properties": {"eventtype": "TC", "eventid": 1001102, "episodeid": 15}},
        {"properties": {"eventtype": "TC", "eventid": 1001102, "episodeid": 14}},
        {"properties": {"eventtype": "EQ", "eventid": 1001102, "episodeid": 2}},  # same number, other type
        {"properties": {"eventtype": "FL", "eventid": 9}},
    ]
    assert latest_episodes({"features": feats}) == {("TC", 1001102): 15, ("EQ", 1001102): 2, ("FL", 9): None}


# ───────────────────────────── timestamps ─────────────────────────────


def test_future_rss_pubdate_is_capped_at_retrieval() -> None:
    body = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Port of Felixstowe congestion</title><link>https://news.example.com/a</link>
      <guid>a</guid><pubDate>Fri, 01 Jan 2100 00:00:00 +0000</pubDate></item>
      <item><title>Older item</title><link>https://news.example.com/b</link>
      <guid>b</guid><pubDate>Tue, 06 Oct 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    future, past = parse_feed(body, _ctx(), connector_id="rss.x", source_name="X")
    assert future.published_at == T0 and past.published_at == dt.datetime(2026, 10, 6, 10, tzinfo=dt.UTC)


def test_future_connector_timestamps_are_capped_at_retrieval() -> None:
    ev = ConnectorEvent.model_validate({
        "type": "supplier_message.received", "id": "m-1", "timestamp": "2099-01-01T00:00:00+00:00",
        "data": {"supplier_code": "SUP-1", "subject": "Delay", "body": "Late by a week",
                 "received_at": "2099-01-01T00:00:00+00:00"}})
    _drafts, (text,) = parse_connector_event(ev, SourceContext(retrieved_at=T0, tenant="demo-retail"))
    assert text.published_at == T0
    ev = ConnectorEvent.model_validate({
        "type": "lane.capacity_changed", "id": "l-1", "timestamp": "2099-01-01T00:00:00+00:00",
        "data": {"lane_code": "SGN-HAM-SEA", "factor_bp": 5000}})
    (draft,), _ = parse_connector_event(ev, SourceContext(retrieved_at=T0, tenant="demo-retail"))
    assert draft.observed_at == T0 and draft.starts_at == T0


# ───────────────────────────── quarantine attempts ─────────────────────────────


async def test_unavailable_llm_never_counts_as_a_bad_output() -> None:
    out = await run_quarantine("Cat Lai terminal closes", FakeLlm(unavailable=402), attempts_so_far=7, max_attempts=8)
    assert out.status == "pending_extraction" and not out.bad_output
    out = await run_quarantine("Cat Lai terminal closes", FakeLlm(bad_output=True), attempts_so_far=0, max_attempts=8)
    assert out.status == "pending_extraction" and out.bad_output
    out = await run_quarantine("Cat Lai terminal closes", FakeLlm(bad_output=True), attempts_so_far=7, max_attempts=8)
    assert out.status == "failed" and out.bad_output


def test_fixture_eventlist_episode_map() -> None:
    doc = json.loads(load_fixture("gdacs_eventlist.json"))
    assert latest_episodes(doc)[("TC", 1001102)] == 14
