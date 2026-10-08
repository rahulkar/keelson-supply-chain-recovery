"""Source registry (``124`` §0): the one place that defines every feed source the hub can poll.

Each adapter has static facts (:data:`ADAPTER_INFO`: catalogue kind, provenance, licence and its
class, attribution, terms link, cadence limits, authority, tenant opt-in, coverage) and the
deployment configures its connectors (:func:`catalogue`: URLs, default cadences, the ``RSS_FEEDS``
entries and the ``METEOALARM_COUNTRIES`` slugs). Everything else derives from here: the Signals
admin catalogue and cadence limits (:mod:`signal_hub.admin`), the poller's adapter list and the
feed runner's dispatch (:mod:`signal_hub.feeds`), and the tenant opt-in rule of
:func:`signal_hub.admin.event_subscribed`.

Two gates decide whether a source is polled:

1. the deployment allow-list ``SIGNAL_ADAPTERS`` (operator; nothing outside it ever leaves through
   the egress proxy) — reported as ``polled``;
2. tenant subscriptions. A default source is subscribed unless a tenant disabled it; an **opt-in**
   source (:data:`OPT_IN_ADAPTERS`: GDELT, MeteoAlarm) is subscribed only by an explicit ``enabled``
   row and stays paused until at least one tenant enabled it.

Catalogue ``kind`` is a grouping for the admin page (``hazard`` / ``weather`` / ``regulatory`` /
``news``), not a :data:`~keelson_contracts.signals.SignalKind`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from signal_hub import scoring
from signal_hub.adapters import copernicus_ems, gdacs, gdelt, meteoalarm, nhc, ofac, rss, usgs
from signal_hub.config import Settings

__all__ = [
    "ADAPTERS",
    "ADAPTER_INFO",
    "CADENCE_LIMITS",
    "MULTI_CONNECTOR",
    "OPT_IN_ADAPTERS",
    "AdapterInfo",
    "SourceSpec",
    "adapter_of",
    "catalogue",
    "is_opt_in",
]

CatalogueKind = Literal["hazard", "weather", "regulatory", "news"]
LicenceClass = Literal["public_domain", "open_attribution", "terms_apply", "publisher_copyright"]


@dataclass(frozen=True, slots=True)
class AdapterInfo:
    """Static facts of one adapter (identical for all its connectors)."""

    adapter: str
    kind: CatalogueKind
    provenance: Literal["fact", "inference"]
    license: str
    licence_class: LicenceClass
    attribution: str
    terms_url: str | None
    cadence_limits: tuple[int, int]
    """Allowed poll cadence (seconds): a politeness floor towards the publisher, one day max."""
    opt_in: bool
    coverage: str
    description: str

    @property
    def authority_bp(self) -> int:
        return scoring.AUTHORITY[self.adapter]


ADAPTER_INFO: Final[dict[str, AdapterInfo]] = {
    a.adapter: a
    for a in (
        AdapterInfo("gdacs", "hazard", "fact", gdacs.LICENSE, "open_attribution",
                    "Source: GDACS (European Commission JRC / UN OCHA), CC BY 4.0",
                    "https://www.gdacs.org/About/termofuse.aspx", (300, 86_400), False,
                    "Global tropical cyclones, earthquakes and floods (orange and red alerts)",
                    "Authoritative hazard alerts with forecast cones and footprints."),
        AdapterInfo("usgs", "hazard", "fact", usgs.LICENSE, "public_domain", "Source: U.S. Geological Survey",
                    "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits", (120, 86_400),
                    False, "Global earthquakes, magnitude 4.5 and above",
                    "Authoritative earthquake reports (magnitude, PAGER alert, tsunami flag)."),
        AdapterInfo("rss", "news", "inference", rss.LICENSE, "publisher_copyright",
                    "Headlines © their publishers; headline, snippet and link only", None, (600, 86_400), False,
                    "Trade-press headlines",
                    "Untrusted news text, screened and extracted in quarantine before it becomes a signal."),
        AdapterInfo("nhc", "hazard", "fact", nhc.LICENSE, "public_domain", nhc.ATTRIBUTION,
                    "https://www.weather.gov/disclaimer", (300, 86_400), False,
                    "Atlantic, East and Central Pacific tropical cyclones",
                    "Authoritative advisories with the forecast cone and track (KMZ)."),
        AdapterInfo("copernicus_ems", "hazard", "fact", copernicus_ems.LICENSE, "open_attribution",
                    copernicus_ems.ATTRIBUTION, "https://www.copernicus.eu/en/access-data/copyright-and-licences",
                    (900, 86_400), False, "Rapid-mapping activations worldwide (floods, storms, earthquakes, wildfires …)",
                    "Authoritative emergency-mapping activations (activation centroid and countries)."),
        AdapterInfo("ofac", "regulatory", "fact", ofac.LICENSE, "public_domain", ofac.ATTRIBUTION,
                    "https://ofac.treasury.gov/sanctions-list-service", (1800, 86_400), False,
                    "U.S. Treasury SDN list: new and changed organisations",
                    "Exact organisation-name awareness matching against your suppliers and factories; "
                    "not a sanctions-screening service."),
        AdapterInfo("gdelt", "news", "inference", gdelt.LICENSE, "open_attribution", gdelt.ATTRIBUTION,
                    "https://www.gdeltproject.org/about.html#termsofuse", (900, 86_400), True,
                    "Machine-coded news events near ports and chokepoints (strikes, blockades, unrest …)",
                    "Low-authority inference coded by GDELT (no LLM): corroborates other reports and never alerts on "
                    "its own with the default rules. About 100 MB a day, so it is opt-in."),
        AdapterInfo("meteoalarm", "weather", "fact", meteoalarm.LICENSE, "terms_apply", meteoalarm.ATTRIBUTION,
                    "https://www.meteoalarm.org/en/live/page/terms-of-use", (600, 86_400), True,
                    "Orange and red severe-weather warnings",
                    "National weather-service warnings via EUMETNET MeteoAlarm. Redistribution terms apply; the "
                    "operator enables it (SIGNAL_ADAPTERS), then each workspace opts in."),
    )
}
ADAPTERS: Final[tuple[str, ...]] = tuple(ADAPTER_INFO)
"""Every feed adapter, in catalogue order (the poller adds ``extraction`` and ``sweep``)."""
CADENCE_LIMITS: Final[dict[str, tuple[int, int]]] = {a: i.cadence_limits for a, i in ADAPTER_INFO.items()}
MULTI_CONNECTOR: Final = frozenset({"rss", "meteoalarm"})
"""Adapters with one connector per configured entry (``rss.<slug>``, ``meteoalarm.<country>``)."""
OPT_IN_ADAPTERS: Final = frozenset(a for a, i in ADAPTER_INFO.items() if i.opt_in)


def adapter_of(connector_id: str) -> str:
    """``rss.theloadstar`` → ``rss``; ``nhc`` → ``nhc``."""
    return connector_id.split(".", 1)[0]


def is_opt_in(connector_id: str) -> bool:
    return adapter_of(connector_id) in OPT_IN_ADAPTERS


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One configured source (connector) of the catalogue."""

    connector_id: str
    info: AdapterInfo
    name: str
    url: str
    default_cadence_s: int
    coverage: str

    @property
    def adapter(self) -> str:
        return self.info.adapter

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


def _country_label(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def catalogue(settings: Settings) -> list[SourceSpec]:
    """Configured sources in catalogue order: gdacs, usgs, rss.*, nhc, copernicus_ems, ofac, gdelt,
    meteoalarm.* (whether the poller runs them is ``SIGNAL_ADAPTERS``, reported by the admin catalogue)."""
    info = ADAPTER_INFO
    out = [
        SourceSpec(gdacs.CONNECTOR_ID, info["gdacs"], "GDACS (Global Disaster Alert and Coordination System)",
                   settings.gdacs_url, settings.cadence_gdacs_s, info["gdacs"].coverage),
        SourceSpec(usgs.CONNECTOR_ID, info["usgs"], "USGS earthquakes (M4.5+, past day)", settings.usgs_url,
                   settings.cadence_usgs_s, info["usgs"].coverage),
    ]
    out += [SourceSpec(f.connector_id, info["rss"], f.name, f.url, settings.cadence_rss_s, info["rss"].coverage)
            for f in settings.rss_feeds]
    out += [
        SourceSpec(nhc.CONNECTOR_ID, info["nhc"], nhc.SOURCE_NAME, settings.nhc_url, settings.cadence_nhc_s,
                   info["nhc"].coverage),
        SourceSpec(copernicus_ems.CONNECTOR_ID, info["copernicus_ems"], copernicus_ems.SOURCE_NAME,
                   settings.copernicus_ems_url, settings.cadence_copernicus_ems_s, info["copernicus_ems"].coverage),
        SourceSpec(ofac.CONNECTOR_ID, info["ofac"], ofac.SOURCE_NAME, settings.ofac_sdn_url, settings.cadence_ofac_s,
                   info["ofac"].coverage),
        SourceSpec(gdelt.CONNECTOR_ID, info["gdelt"], gdelt.SOURCE_NAME, settings.gdelt_lastupdate_url,
                   settings.cadence_gdelt_s, info["gdelt"].coverage),
    ]
    out += [
        SourceSpec(meteoalarm.connector_id(slug), info["meteoalarm"], f"MeteoAlarm warnings: {_country_label(slug)}",
                   meteoalarm.feed_url(settings.meteoalarm_url_template, slug), settings.cadence_meteoalarm_s,
                   f"{_country_label(slug)}: {info['meteoalarm'].coverage.lower()}")
        for slug in settings.meteoalarm_countries
    ]
    return out
