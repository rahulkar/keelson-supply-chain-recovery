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

Clustering v2 (M8, ``docs/contracts/123`` §4) adds evidence v1 cannot see, never overriding it:
:func:`decide` = authoritative rules (v1) → **near-duplicate** (a SimHash copy joins its original's
event) → v1 attach → **vector attach** (similar cluster terms, kind / window / geography gates,
per-profile thresholds) → found. :func:`choose_merge` picks the event an un-anchored candidate event
merges into (background pass). Near-duplicate members never add a source to corroboration.
:func:`choose_event` (v1 only) is unchanged.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from keelson_contracts.signals import (
    LOCAL_SIMILARITY_PROFILE,
    ClusteringConfig,
    VectorThresholds,
    combine_confidence_bp,
    taxonomy_key_for,
)
from signal_hub import geo, scoring

__all__ = [
    "ATTACH_RADIUS_KM",
    "ATTACH_WINDOW",
    "COMPATIBLE",
    "Candidate",
    "Decision",
    "EventAggregate",
    "EventFeatures",
    "MemberRow",
    "SignalFeatures",
    "aggregate_event",
    "choose_event",
    "choose_merge",
    "countries_of",
    "decide",
    "kinds_compatible",
    "km",
    "merge_target_possible",
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
    near_dup_event: str | None = None
    """v2: the current event of the signal this one is a SimHash near-duplicate of (only when the copy
    names no place word its original does not)."""
    near_dup_bits: int | None = None
    duplicate_of: str | None = None
    """v2: the id of that original signal."""


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
    similarity_bp: int | None = None
    """v2: similarity of the signal (or event) to this candidate, ``None`` = not computed."""
    similarity_profile: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    """Where a signal goes and why (``123`` §4/§5): ``event_id`` ``None`` = it founds its own event
    (method ``anchor`` or ``founded``). Distances are whole kilometres (half-up)."""

    event_id: str | None
    method: str
    distance_km: int | None = None
    shared_refs: tuple[str, ...] = ()
    similarity_bp: int | None = None
    similarity_profile: str | None = None
    hamming_bits: int | None = None
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class EventFeatures:
    """A merge source (an open event, background pass)."""

    event_id: str
    kind: str
    subtype: str
    anchored: bool
    window_start: dt.datetime
    window_end: dt.datetime | None
    last_observed: dt.datetime | None
    refs: frozenset[str] = frozenset()
    open_case: bool = False
    """A tenant opened a case from this event's alert: never merged away."""


def _strong(refs: Iterable[str]) -> set[str]:
    return {r for r in refs if r.split(":", 1)[0] in STRONG_SCHEMES}


def _overlaps(sig: SignalFeatures, cand: Candidate, window: dt.timedelta) -> bool:
    s_start, s_end = sig.starts_at, sig.ends_at or sig.starts_at
    c_end = cand.window_end or cand.last_observed or cand.window_start
    return cand.window_start - window <= s_end and s_start <= max(c_end, cand.window_start) + window


def _same_hazard(sig: SignalFeatures, candidates: Sequence[Candidate]) -> Candidate | None:
    same = [
        c for c in candidates
        if c.anchored and c.anchor_connector not in (None, sig.connector_id) and c.subtype == sig.subtype
        and c.distance_km is not None and c.distance_km <= SAME_HAZARD_RADIUS_KM
        and abs((c.window_start - sig.starts_at).total_seconds()) <= SAME_HAZARD_WINDOW.total_seconds()
    ]
    same.sort(key=lambda c: (c.distance_km or 0.0, c.event_id))
    return same[0] if same else None


def _v1_attach(sig: SignalFeatures, candidates: Sequence[Candidate]) -> Candidate | None:
    ranked: list[tuple[int, int, float, str, Candidate]] = []
    for c in candidates:
        if not kinds_compatible(sig.kind, c.kind) or not _overlaps(sig, c, ATTACH_WINDOW):
            continue
        near = c.distance_km is not None and c.distance_km <= ATTACH_RADIUS_KM
        shared = sig.refs & c.refs
        if not near and not shared:
            continue
        ranked.append((0 if c.anchored else 1, -len(_strong(shared)),
                       c.distance_km if c.distance_km is not None else float("inf"), c.event_id, c))
    ranked.sort(key=lambda r: r[:4])
    return ranked[0][4] if ranked else None


def choose_event(sig: SignalFeatures, candidates: Sequence[Candidate]) -> str | None:
    """The event a new signal joins, or None (it founds its own) — clustering v1 only."""
    chosen = _same_hazard(sig, candidates) if sig.authoritative else _v1_attach(sig, candidates)
    return chosen.event_id if chosen is not None else None


# ─────────────────────────────── clustering v2 ───────────────────────────────


def km(distance: float | None) -> int | None:
    """Whole kilometres, half-up (``None`` stays ``None``)."""
    if distance is None:
        return None
    return int(Decimal(repr(float(distance))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def countries_of(refs: Iterable[str]) -> frozenset[str]:
    """ISO 3166 alpha-2 countries named by refs (``ISO3166:XX``, or a UN/LOCODE's country prefix)."""
    out: set[str] = set()
    for ref in refs:
        scheme, _, code = ref.partition(":")
        if scheme == "ISO3166" and len(code) == 2:
            out.add(code)
        elif scheme == "UNLOCODE" and len(code) == 5:
            out.add(code[:2])
    return frozenset(out)


def _strong_first(refs: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(refs, key=lambda r: (r.split(":", 1)[0] not in STRONG_SCHEMES, r)))


def _thresholds(cfg: ClusteringConfig, profile: str | None) -> VectorThresholds:
    return cfg.vector.local if profile in (None, LOCAL_SIMILARITY_PROFILE) else cfg.vector.remote


def _geo_ok(distance_km: float | None, own_refs: frozenset[str], other_refs: frozenset[str], similarity: int,
            thresholds: VectorThresholds, max_distance_km: int) -> bool:
    """Vector geography gate: both located → within ``max_distance_km``; otherwise a shared country,
    or (no refs of its own at all) a similarity of at least ``strict_bp``."""
    if distance_km is not None:
        return distance_km <= max_distance_km
    if countries_of(own_refs) & countries_of(other_refs):
        return True
    return not own_refs and similarity >= thresholds.strict_bp


def _vector_attach(sig: SignalFeatures, candidates: Sequence[Candidate], cfg: ClusteringConfig) -> Candidate | None:
    window = dt.timedelta(hours=cfg.vector.window_hours)
    ranked: list[tuple[int, int, float, str, Candidate]] = []
    for c in candidates:
        if c.similarity_bp is None or not kinds_compatible(sig.kind, c.kind) or not _overlaps(sig, c, window):
            continue
        thresholds = _thresholds(cfg, c.similarity_profile)
        if c.similarity_bp < thresholds.attach_bp:
            continue
        if not _geo_ok(c.distance_km, sig.refs, c.refs, c.similarity_bp, thresholds, cfg.vector.max_distance_km):
            continue
        ranked.append((0 if c.anchored else 1, -c.similarity_bp,
                       c.distance_km if c.distance_km is not None else float("inf"), c.event_id, c))
    ranked.sort(key=lambda r: r[:4])
    return ranked[0][4] if ranked else None


def decide(sig: SignalFeatures, candidates: Sequence[Candidate], cfg: ClusteringConfig) -> Decision:
    """Clustering v2 for one new signal (the first rule that applies wins):

    1. authoritative: the v1 same-hazard rule (``same_hazard``), else it founds (``anchor``);
    2. a near-duplicate joins its original's event when that event is an open, in-scope candidate
       of a compatible kind and the copy names no other place (no refs, or a ref shared with the
       event; the caller sets ``near_dup_event`` only when the copy names no place word its original
       does not, :func:`signal_hub.embeddings.names_no_other_place`) (``near_duplicate``;
       ``simhash.enabled``);
    3. v1 attach, same ranking (``attach_ref`` when a ref is shared, else ``attach_geo``);
    4. vector attach (``vector.enabled``): compatible kind, windows within ``window_hours``,
       similarity >= the profile's ``attach_bp``, the geography gate (:func:`_geo_ok`); ranked
       anchored first, then most similar, then nearest, then id (``attach_vector``);
    5. otherwise it founds a candidate event (``founded``).

    With both v2 switches off the chosen event equals :func:`choose_event`."""
    if sig.authoritative:
        same = _same_hazard(sig, candidates)
        if same is not None:
            return Decision(same.event_id, "same_hazard", distance_km=km(same.distance_km))
        return Decision(None, "anchor")
    if cfg.simhash.enabled and sig.near_dup_event is not None and sig.duplicate_of is not None \
            and sig.near_dup_bits is not None:
        original = next((c for c in candidates if c.event_id == sig.near_dup_event), None)
        if original is not None and kinds_compatible(sig.kind, original.kind) \
                and (not sig.refs or sig.refs & original.refs):
            return Decision(original.event_id, "near_duplicate", hamming_bits=sig.near_dup_bits,
                            duplicate_of=sig.duplicate_of)
    attached = _v1_attach(sig, candidates)
    if attached is not None:
        shared = sig.refs & attached.refs
        if shared:
            return Decision(attached.event_id, "attach_ref", distance_km=km(attached.distance_km),
                            shared_refs=_strong_first(shared)[:50])
        return Decision(attached.event_id, "attach_geo", distance_km=km(attached.distance_km))
    if cfg.vector.enabled:
        similar = _vector_attach(sig, candidates, cfg)
        if similar is not None:
            return Decision(similar.event_id, "attach_vector", distance_km=km(similar.distance_km),
                            similarity_bp=similar.similarity_bp,
                            similarity_profile=similar.similarity_profile or LOCAL_SIMILARITY_PROFILE)
    return Decision(None, "founded")


def merge_target_possible(src: EventFeatures, c: Candidate, cfg: ClusteringConfig) -> bool:
    """The similarity-independent gates of :func:`choose_merge` for one target: another event that is
    anchored or older than the source, of a compatible kind, with windows overlapping within
    ``vector.window_hours`` (the merge pass checks these before computing any similarity)."""
    if c.event_id == src.event_id:
        return False
    if not c.anchored and (c.window_start, c.event_id) >= (src.window_start, src.event_id):
        return False
    as_signal = SignalFeatures(kind=src.kind, subtype=src.subtype, connector_id="", authoritative=False,
                               starts_at=src.window_start, ends_at=src.window_end or src.last_observed, refs=src.refs)
    return kinds_compatible(src.kind, c.kind) and _overlaps(as_signal, c, dt.timedelta(hours=cfg.vector.window_hours))


def choose_merge(src: EventFeatures, candidates: Sequence[Candidate], cfg: ClusteringConfig) -> Decision | None:
    """The event an open candidate event merges into (background pass), or ``None``.

    Never for an anchored source or one with an opened case. A target is an open event of the same
    scope with a compatible kind, overlapping windows (``window_hours``), similarity >= the
    profile's ``merge_bp`` and the geography gate, that is anchored or **older** than the source
    (window start, then id: the newer of two candidates merges into the older, never both ways).
    Preference: anchored, then earliest, then id."""
    if src.anchored or src.open_case or not cfg.vector.enabled:
        return None
    ranked: list[tuple[int, dt.datetime, str, Candidate]] = []
    for c in candidates:
        if c.similarity_bp is None or not merge_target_possible(src, c, cfg):
            continue
        thresholds = _thresholds(cfg, c.similarity_profile)
        if c.similarity_bp < thresholds.merge_bp:
            continue
        if not _geo_ok(c.distance_km, src.refs, c.refs, c.similarity_bp, thresholds, cfg.vector.max_distance_km):
            continue
        ranked.append((0 if c.anchored else 1, c.window_start, c.event_id, c))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[:3])
    target = ranked[0][3]
    return Decision(target.event_id, "merged_vector", distance_km=km(target.distance_km),
                    similarity_bp=target.similarity_bp,
                    similarity_profile=target.similarity_profile or LOCAL_SIMILARITY_PROFILE)


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
    near_duplicate_of: str | None = None
    """v2: the original signal this member is a SimHash copy of (it adds no source when the original
    is a member of the same event)."""


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
    # syndicated copies (SimHash near-duplicates of a member) never corroborate their original
    member_ids = {m.id for m in members}
    independent = {m.connector_id for m in current
                   if m.near_duplicate_of is None or m.near_duplicate_of not in member_ids} or sources
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
        scoring.corroboration_bp(len(independent), anchored=bool(anchors)),
        scoring.recency_bp(last_observed, now),
    )
    if merged or (expires_at is not None and expires_at <= now):
        status = "resolved"
    elif now - last_observed > MONITORING_AFTER:
        status = "monitoring"
    elif anchors or len(independent) >= 2:
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
        if len(independent) != len(names):
            summary += f"; {len(independent)} independent"
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
