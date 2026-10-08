"""Severity and confidence formulas (``100`` §2, §4). Pure integer arithmetic (basis points),
deterministic for a given input; documented in the README ("Formulas").

Severity (0..10000 bp) per source:

* **GDACS** — alert level base (green 2000, orange 6000, red 8500) + a hazard bonus capped at
  1000: TC ``(max wind km/h − 119) × 5`` (119 km/h = typhoon threshold), EQ
  ``(magnitude − 6.0) × 1000``, other hazards ``alertscore``-based ``(score − level floor) × 500``.
* **USGS** — ``max(magnitude band, PAGER band)`` (+1000 when ``tsunami = 1``): magnitude bands
  < 5.0 → 1000, < 6.0 → 2500, < 6.5 → 4000, < 7.0 → 5500, < 7.5 → 7000, else 8500; PAGER
  green 3000, yellow 5500, orange 7500, red 9000.
* **News / free text** (``inference``) — a per-subtype prior (:data:`NEWS_SEVERITY`, default 3000);
  never derived from LLM numbers.
* **Connector events** (``fact``) — from structured fields: delays ``1500 + 700 × days`` (cap 9000),
  lane capacity cuts ``10000 − factor_bp``.
* **M8 feeds** (``124`` §2): NHC by intensity (Saffir-Simpson bands, :func:`nhc_severity_bp`);
  Copernicus EMS by activation category (:data:`COPERNICUS_EMS_SEVERITY`); MeteoAlarm by awareness
  level (:data:`METEOALARM_SEVERITY`: yellow 3000, orange 6000, red 8500); an OFAC SDN publication
  :data:`OFAC_SEVERITY_BP`; GDELT a subtype prior (:data:`GDELT_PRIOR`) capped at
  ``GDELT_MAX_SEVERITY_BP`` (3900), below the default alert threshold.

Confidence = ``authority × corroboration × recency`` (:func:`keelson_contracts.signals.combine_confidence_bp`):

* authority: per source (GDACS/USGS/NHC/OFAC 9500, Copernicus EMS/MeteoAlarm 9000, connector structured
  8500, trade press 7000, free text 6000, GDELT 4000);
* corroboration: anchored by an authoritative feed 10000; else 1 source 6000, 2 sources 8000, ≥ 3 10000;
* recency: 10000 up to 24 h old, then linear down to 5000 at 7 days, 5000 after.

Event severity: the authoritative (anchor) members' maximum when there is one, else the p75
(nearest rank) of the current members' severities.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from keelson_contracts.signals import combine_confidence_bp

__all__ = [
    "AUTHORITY",
    "COPERNICUS_EMS_SEVERITY",
    "GDELT_PRIOR",
    "METEOALARM_SEVERITY",
    "NEWS_SEVERITY",
    "OFAC_SEVERITY_BP",
    "clamp_bp",
    "corroboration_bp",
    "event_severity_bp",
    "gdacs_severity_bp",
    "gdelt_severity_bp",
    "news_severity_bp",
    "nhc_severity_bp",
    "recency_bp",
    "signal_confidence_bp",
    "usgs_severity_bp",
]

AUTHORITY: Final[dict[str, int]] = {
    "gdacs": 9500,
    "usgs": 9500,
    "connector": 8500,
    "rss": 7000,
    "free_text": 6000,
    "chat": 6000,
    # M8 feeds (124 §2)
    "nhc": 9500,
    "copernicus_ems": 9000,
    "ofac": 9500,
    "gdelt": 4000,
    "meteoalarm": 9000,
}

NEWS_SEVERITY: Final[dict[str, int]] = {
    "chokepoint_closure": 7500,
    "port_closure": 6500,
    "strike": 5500,
    "tropical_cyclone": 5500,
    "typhoon": 5500,
    "hurricane": 5500,
    "cyclone": 5500,
    "tropical_storm": 5000,
    "storm": 5000,
    "earthquake": 5500,
    "flood": 5000,
    "capacity_loss": 5000,
    "supplier_delay": 4500,
    "demand_spike": 3500,
    "demand_drop": 3500,
}
DEFAULT_NEWS_SEVERITY: Final = 3000

COPERNICUS_EMS_SEVERITY: Final[dict[str, int]] = {
    "flood": 6500,
    "wind_storm": 6500,
    "earthquake": 7000,
    "wildfire": 5000,
    "volcano": 6000,
    "landslide": 5000,
}
"""Copernicus EMS rapid-mapping activation (by mapped subtype): an activation means a civil-protection
authority asked for emergency maps, i.e. a significant event."""
METEOALARM_SEVERITY: Final[dict[int, int]] = {2: 3000, 3: 6000, 4: 8500}
"""MeteoAlarm awareness level (2 yellow, 3 orange, 4 red)."""
OFAC_SEVERITY_BP: Final = 8000
"""One SDN publication with new or changed organisations: an exposed supplier/factory drafts a case
under the default rules (awareness only; never a screening verdict)."""
GDELT_PRIOR: Final[dict[str, int]] = {
    "strike": 5500,
    "blockade": 6000,
    "armed_conflict": 6500,
    "civil_unrest": 4500,
    "trade_restriction": 5000,
    "state_of_emergency": 5000,
}
"""GDELT subtype priors, always capped by ``GDELT_MAX_SEVERITY_BP`` (kept apart from
:data:`NEWS_SEVERITY` so the M6/M7 news severities do not change)."""

_GDACS_LEVEL: Final[dict[str, int]] = {"green": 2000, "orange": 6000, "red": 8500}
_GDACS_SCORE_FLOOR: Final[dict[str, Decimal]] = {"green": Decimal(0), "orange": Decimal("1.5"), "red": Decimal("2.5")}
_PAGER: Final[dict[str, int]] = {"green": 3000, "yellow": 5500, "orange": 7500, "red": 9000}


def clamp_bp(value: int) -> int:
    return max(0, min(10_000, int(value)))


def _dec(value: float | int | str | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except ArithmeticError:
        return None
    return d if d.is_finite() else None


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def gdacs_severity_bp(
    eventtype: str, alertlevel: str, *, alertscore: float | None = None, severity: float | None = None
) -> int:
    level = alertlevel.strip().lower()
    base = _GDACS_LEVEL.get(level, 2000)
    bonus = Decimal(0)
    sev = _dec(severity)
    if eventtype == "TC" and sev is not None:
        bonus = (sev - 119) * 5
    elif eventtype == "EQ" and sev is not None:
        bonus = (sev - Decimal("6.0")) * 1000
    else:
        score = _dec(alertscore)
        if score is not None:
            bonus = (score - _GDACS_SCORE_FLOOR.get(level, Decimal(0))) * 500
    bonus = max(Decimal(0), min(Decimal(1000), bonus))
    return clamp_bp(base + _round(bonus))


def usgs_severity_bp(mag: float | None, *, alert: str | None = None, tsunami: int | bool | None = None) -> int:
    m = _dec(mag)
    if m is None:
        mag_band = 0
    elif m < 5:
        mag_band = 1000
    elif m < 6:
        mag_band = 2500
    elif m < Decimal("6.5"):
        mag_band = 4000
    elif m < 7:
        mag_band = 5500
    elif m < Decimal("7.5"):
        mag_band = 7000
    else:
        mag_band = 8500
    pager = _PAGER.get((alert or "").strip().lower(), 0)
    severity = max(mag_band, pager)
    if tsunami and not isinstance(tsunami, str):
        severity += 1000
    return clamp_bp(severity)


def news_severity_bp(subtype: str) -> int:
    return NEWS_SEVERITY.get(subtype, DEFAULT_NEWS_SEVERITY)


def nhc_severity_bp(intensity_kt: int | None) -> int:
    """Maximum sustained wind (knots) → severity: < 34 kt (depression) 3000; tropical storm 34–49
    5000, 50–63 5500; Saffir-Simpson H1 64–82 6500, H2 83–95 7500, H3 96–112 8500, H4 113–136 9200,
    H5 >= 137 9700. Unknown → 3000."""
    if intensity_kt is None or intensity_kt < 34:
        return 3000
    for floor, bp in ((137, 9700), (113, 9200), (96, 8500), (83, 7500), (64, 6500), (50, 5500)):
        if intensity_kt >= floor:
            return bp
    return 5000


def gdelt_severity_bp(subtype: str, cap_bp: int) -> int:
    """The GDELT prior of ``subtype`` (else the news default), never above ``cap_bp``."""
    return clamp_bp(min(GDELT_PRIOR.get(subtype, DEFAULT_NEWS_SEVERITY), cap_bp))


def delay_severity_bp(days: int) -> int:
    return clamp_bp(min(9000, 1500 + 700 * max(0, days)))


def corroboration_bp(distinct_sources: int, *, anchored: bool) -> int:
    if anchored:
        return 10_000
    if distinct_sources <= 1:
        return 6000
    if distinct_sources == 2:
        return 8000
    return 10_000


def recency_bp(observed_at: dt.datetime, now: dt.datetime) -> int:
    age_s = max(0, int((now - observed_at).total_seconds()))
    day, week = 24 * 3600, 7 * 24 * 3600
    if age_s <= day:
        return 10_000
    if age_s >= week:
        return 5000
    # linear 10000 → 5000 between 24 h and 7 d (half-up)
    num = 5000 * (age_s - day)
    den = week - day
    q, r = divmod(num, den)
    return 10_000 - (q + (1 if 2 * r >= den else 0))


def signal_confidence_bp(authority_bp: int, *, anchored: bool, observed_at: dt.datetime, now: dt.datetime) -> int:
    return combine_confidence_bp(
        clamp_bp(authority_bp), corroboration_bp(1, anchored=anchored), recency_bp(observed_at, now)
    )


def event_severity_bp(anchor_severities: Sequence[int], member_severities: Sequence[int]) -> int:
    if anchor_severities:
        return clamp_bp(max(anchor_severities))
    if not member_severities:
        return 0
    ordered = sorted(member_severities)
    # p75, nearest rank: ceil(0.75 * n) (1-based)
    rank = -(-3 * len(ordered) // 4)
    return clamp_bp(ordered[max(rank, 1) - 1])
