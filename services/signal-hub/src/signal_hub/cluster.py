"""Clustering v1 (``100`` §4) — pure decision logic, unit-tested without a database.

* Authoritative feed signals (GDACS, USGS) are **anchors**: each one founds its own event (id
  ``event_id(connector, native_key)``, stable across versions). A second authoritative feed reporting
  the *same* hazard (same subtype, <= 100 km, starts within 6 h, different connector) joins that event.
* Every other signal (news, connector, chat) **attaches** to an open event when the kind is
  compatible (:data:`COMPATIBLE`), the time windows overlap within ±72 h, and the geometry is within
  300 km or a ``location_ref`` matches. Ranking: anchored events first, then most shared specific refs
  (port/chokepoint/supplier/…), then nearest, then id. Otherwise it founds its own *candidate* event
  (id ``event_id("signal", connector, native_key)``).
* When an anchor event is founded, open un-anchored events that would attach to it are absorbed
  (members moved; the old event is resolved with ``merged_into``).

Scope: global signals only see global events; a tenant's signals see global + that tenant's events;
demo-pack signals only see events of the same tenant and pack.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from keelson_contracts.signals import combine_confidence_bp, taxonomy_key_for
from signal_hub import geo, scoring

__all__ = [
    "ATTACH_RADIUS_KM",
    "ATTACH_WINDOW",
    "COMPATIBLE",
    "Candidate",
    "EventAggregate",
    "MemberRow",
    "SignalFeatures",
    "aggregate_event",
    "choose_event",
    "kinds_compatible",
    "radius_km",
]

ATTACH_WINDOW: Final = dt.timedelta(hours=72)
ATTACH_RADIUS_KM: Final = 300.0
SAME_HAZARD_RADIUS_KM: Final = 100.0
SAME_HAZARD_WINDOW: Final = dt.timedelta(hours=6)
MONITORING_AFTER: Final = dt.timedelta(hours=48)
STRONG_SCHEMES: Final = frozenset({"UNLOCODE", "CHOKEPOINT", "SUPPLIER", "FACTORY", "DC", "STORE", "LANE"})

_PAIRS: Final = (
    ("natural_hazard", "weather"),
    ("natural_hazard", "logistics"),
    ("natural_hazard", "supplier"),
    ("weather", "logistics"),
    ("weather", "supplier"),
    ("weather", "demand"),
    ("geopolitical", "logistics"),
    ("geopolitical", "regulatory"),
    ("labor", "logistics"),
    ("labor", "supplier"),
    ("cyber", "logistics"),
    ("cyber", "supplier"),
    ("logistics", "supplier"),
)
COMPATIBLE: Final[dict[str, frozenset[str]]] = {}
for _a, _b in _PAIRS:
    COMPATIBLE[_a] = COMPATIBLE.get(_a, frozenset()) | {_b}
    COMPATIBLE[_b] = COMPATIBLE.get(_b, frozenset()) | {_a}

_STORM: Final = frozenset(k for k in ("tropical_cyclone", "tropical_storm", "typhoon", "hurricane", "cyclone", "storm"))
_RADIUS: Final[dict[str, int]] = {
    "flood": 50,
    "port_closure": 10,
    "strike": 25,
    "chokepoint_closure": 25,
}
DEFAULT_RADIUS_KM: Final = 50


def kinds_compatible(a: str, b: str) -> bool:
    """Same kind, or a related pair (e.g. a typhoon and a port closure); ``hypothetical`` only
    matches itself."""
    return a == b or b in COMPATIBLE.get(a, frozenset())


def radius_km(subtype: str, *, magnitude: float | None = None) -> int:
    """Kind-specific exposure radius (``100`` §5): TC 300 km of track/cone, EQ >= 6.5 → 250 km else
    100 km, FL 50 km, port events 10 km (the port node, plus entity match), else 50 km."""
    if subtype in _STORM or taxonomy_key_for(subtype) == "NAT.storm":
        return 300
    if subtype == "earthquake":
        return 250 if magnitude is not None and magnitude >= 6.5 else 100
    return _RADIUS.get(subtype, DEFAULT_RADIUS_KM)


@dataclass(frozen=True, slots=True)
class SignalFeatures:
    kind: str
    subtype: str
    connector_id: str
    authoritative: bool
    starts_at: dt.datetime
    ends_at: dt.datetime | None
    refs: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Candidate:
    event_id: str
    kind: str
    subtype: str
    anchored: bool
    anchor_connector: str | None
    window_start: dt.datetime
    window_end: dt.datetime | None
    last_observed: dt.datetime | None
    distance_km: float | None
    refs: frozenset[str] = frozenset()


def _strong(refs: Iterable[str]) -> set[str]:
    return {r for r in refs if r.split(":", 1)[0] in STRONG_SCHEMES}


def _overlaps(sig: SignalFeatures, cand: Candidate, window: dt.timedelta) -> bool:
    s_start, s_end = sig.starts_at, sig.ends_at or sig.starts_at
    c_end = cand.window_end or cand.last_observed or cand.window_start
    return cand.window_start - window <= s_end and s_start <= max(c_end, cand.window_start) + window


def choose_event(sig: SignalFeatures, candidates: Sequence[Candidate]) -> str | None:
    """The event a new signal joins, or None (it founds its own)."""
    if sig.authoritative:
        same = [
            c for c in candidates
            if c.anchored and c.anchor_connector not in (None, sig.connector_id) and c.subtype == sig.subtype
            and c.distance_km is not None and c.distance_km <= SAME_HAZARD_RADIUS_KM
            and abs((c.window_start - sig.starts_at).total_seconds()) <= SAME_HAZARD_WINDOW.total_seconds()
        ]
        same.sort(key=lambda c: (c.distance_km or 0.0, c.event_id))
        return same[0].event_id if same else None
    ranked: list[tuple[int, int, float, str]] = []
    for c in candidates:
        if not kinds_compatible(sig.kind, c.kind) or not _overlaps(sig, c, ATTACH_WINDOW):
            continue
        near = c.distance_km is not None and c.distance_km <= ATTACH_RADIUS_KM
        shared = sig.refs & c.refs
        if not near and not shared:
            continue
        ranked.append((0 if c.anchored else 1, -len(_strong(shared)),
                       c.distance_km if c.distance_km is not None else float("inf"), c.event_id))
    ranked.sort()
    return ranked[0][3] if ranked else None


# ─────────────────────────────── event aggregate ───────────────────────────────


@dataclass(frozen=True, slots=True)
class MemberRow:
    """One member signal as stored (only what the aggregate needs)."""

    id: str
    connector_id: str
    authoritative: bool
    is_current: bool
    kind: str
    subtype: str
    title: str
    summary: str | None
    severity_bp: int
    authority_bp: int
    observed_at: dt.datetime
    starts_at: dt.datetime
    ends_at: dt.datetime | None
    expires_at: dt.datetime | None
    provenance: str
    pack_id: str | None
    geometry: dict[str, Any] | None
    severity_native: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class EventAggregate:
    kind: str
    subtype: str
    title: str
    summary: str | None
    anchor_signal_id: str | None
    geometry: dict[str, Any] | None
    window_start: dt.datetime
    window_end: dt.datetime | None
    severity_bp: int
    confidence_bp: int
    status: str
    taxonomy_key: str
    radius_km: int
    simulated: bool
    pack_id: str | None
    expires_at: dt.datetime | None
    last_observed_at: dt.datetime


def _magnitude(native: dict[str, Any]) -> float | None:
    for key in ("mag", "magnitude"):
        value = native.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def aggregate_event(members: Sequence[MemberRow], *, now: dt.datetime, merged: bool = False) -> EventAggregate | None:
    """Deterministic event fields from its member signals (only current versions count)."""
    current = sorted((m for m in members if m.is_current), key=lambda m: (m.starts_at, m.id))
    if not current:
        return None
    anchors = [m for m in current if m.authoritative]
    lead = anchors[0] if anchors else min(current, key=lambda m: (m.observed_at, m.id))
    sources = {m.connector_id for m in current}
    last_observed = max(m.observed_at for m in current)
    expiries = [m.expires_at for m in current]
    expires_at = None if any(e is None for e in expiries) else max(e for e in expiries if e is not None)
    ends = [m.ends_at for m in current]
    window_end = None if any(e is None for e in ends) else max(e for e in ends if e is not None)
    window_start = min(m.starts_at for m in current)
    if window_end is not None and window_end < window_start:
        window_end = None
    severity = scoring.event_severity_bp([m.severity_bp for m in anchors], [m.severity_bp for m in current])
    confidence = combine_confidence_bp(
        scoring.clamp_bp(max(m.authority_bp for m in current)),
        scoring.corroboration_bp(len(sources), anchored=bool(anchors)),
        scoring.recency_bp(last_observed, now),
    )
    if merged or (expires_at is not None and expires_at <= now):
        status = "resolved"
    elif now - last_observed > MONITORING_AFTER:
        status = "monitoring"
    elif anchors or len(sources) >= 2:
        status = "confirmed"
    else:
        status = "candidate"
    ordered_geoms = [m.geometry for m in ([*anchors, *[m for m in current if not m.authoritative]]) if m.geometry]
    geometry = geo.merge(ordered_geoms)
    if anchors:
        summary = lead.summary
    else:
        n = len(current)
        names = sorted(sources)
        summary = f"{n} report{'s' if n != 1 else ''} from {len(names)} source{'s' if len(names) != 1 else ''} ({', '.join(names[:5])})"
    packs = sorted({m.pack_id for m in current if m.pack_id})
    return EventAggregate(
        kind=lead.kind,
        subtype=lead.subtype,
        title=lead.title,
        summary=summary,
        anchor_signal_id=anchors[0].id if anchors else None,
        geometry=geometry,
        window_start=window_start,
        window_end=window_end,
        severity_bp=severity,
        confidence_bp=confidence,
        status=status,
        taxonomy_key=taxonomy_key_for(lead.subtype),
        radius_km=radius_km(lead.subtype, magnitude=_magnitude(lead.severity_native)),
        simulated=any(m.provenance == "simulated" for m in current),
        pack_id=packs[0] if packs else None,
        expires_at=expires_at,
        last_observed_at=last_observed,
    )
