"""M6 Signal Hub models (``docs/contracts/100-m6-signals.md`` §2, §5, §7).

* :class:`DisruptionSignal` — one normalized observation from a feed (GDACS, USGS), a news item
  (RSS, quarantined extraction), a connected system (Merxa webhook), chat or a demo pack, with
  its :class:`SourceRef` attribution. Ids are deterministic (:func:`signal_id`: ``uuid5`` of
  connector + native id / canonical URL + version).
* :class:`DisruptionEvent` — a cluster of signals (authoritative feed signals are anchors),
  mapped deterministically to a taxonomy key (:func:`taxonomy_key_for`).
* :class:`Exposure` / :class:`ExposureSummary` — which tenant nodes/lanes an event touches
  (PostGIS geo-match or entity match; counts only from the tenant network, never from an LLM).
* :class:`Alert` / :class:`AlertRule` (+ :data:`DEFAULT_ALERT_RULES`, :func:`decide_alert_action`,
  :func:`should_renotify`) — the per-tenant alert inbox and its rules.
* :class:`ScenarioPack` — a deterministic demo timeline replayed through the real adapters.
* :class:`TenantNetwork` — what the engine pushes after each scn sync (§5).

Geometry is GeoJSON with **float WGS84** ``[lon, lat]`` positions (stored as PostGIS
``geography``). Geometry is never part of a change-set or any other canonical hash; everything
that is compared or hashed (severity, confidence, weights, counts, distances) is an integer
(basis points / km). Convert positions for the ``network_map@1`` block with
:func:`keelson_contracts.scn.to_e6`.

Untrusted text (``raw_text``, news titles/summaries, supplier messages) is data: it is only
ever handed to the quarantined extractor (no tools) and never drives routing or approval.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from .common import (
    BP_ONE,
    ContractModel,
    EventTypeKey,
    NonNegInt,
    PositiveInt,
    TenantSlug,
    canonical_json,
)

__all__ = [
    "ALERT_ACTIONS",
    "ALERT_SEVERITIES",
    "ALERT_STATUSES",
    "Alert",
    "AlertAction",
    "AlertCondition",
    "AlertDetail",
    "AlertRule",
    "AlertSeverity",
    "AlertStatus",
    "AlertStatusUpdate",
    "BasisPoints10k",
    "ClusteringBackend",
    "ClusteringConfig",
    "ClusteringConfigUpdate",
    "ClusteringCounts",
    "ClusteringStatus",
    "CONNECTOR_ID_PATTERN",
    "DEFAULT_ALERT_RULES",
    "DEFAULT_CLUSTERING_CONFIG",
    "DisruptionEvent",
    "DisruptionSignal",
    "EmbeddingProfileStatus",
    "EventMembership",
    "EVENT_STATUSES",
    "EventStatus",
    "EventWindow",
    "EXPOSURE_METHODS",
    "Exposure",
    "ExposureMethod",
    "ExposureSummary",
    "GEOMETRY_TYPES",
    "Geometry",
    "GeometryCollection",
    "LOCAL_SIMILARITY_PROFILE",
    "LOCATION_REF_PATTERN",
    "LOCATION_REF_SCHEMES",
    "LineString",
    "MAX_ATTACH_BP",
    "MAX_GEOMETRY_POSITIONS",
    "MAX_MEMBERSHIP_EXPLANATION",
    "MAX_PACK_STEPS",
    "MAX_RAW_TEXT_CHARS",
    "MembershipMethod",
    "MEMBERSHIP_METHODS",
    "MIN_ATTACH_BP",
    "MultiLineString",
    "MultiPoint",
    "MultiPolygon",
    "NODE_TYPES",
    "NetworkLane",
    "NetworkNode",
    "NodeCounts",
    "NodeType",
    "PACK_ADAPTERS",
    "PACK_ID_PATTERN",
    "PackAdapter",
    "Point",
    "Polygon",
    "Position",
    "RemoteEmbeddingProfile",
    "RemoteProfileReason",
    "SIGNALS_NAMESPACE",
    "SIGNAL_KINDS",
    "SIGNAL_ORIGINS",
    "SIGNAL_PROVENANCES",
    "SimHashSettings",
    "SIMILARITY_PROFILE_PATTERN",
    "SOURCE_URL_PATTERN",
    "SUBTYPE_PATTERN",
    "SUBTYPE_TAXONOMY",
    "ScenarioPack",
    "ScenarioPackStep",
    "SeverityNative",
    "SignalKind",
    "SignalOrigin",
    "SignalProvenance",
    "SourceRef",
    "TenantNetwork",
    "UUID_PATTERN",
    "Uuid",
    "VectorSettings",
    "VectorThresholds",
    "alert_severity_for",
    "combine_confidence_bp",
    "decide_alert_action",
    "event_id",
    "geometry_positions",
    "is_uuid",
    "pack_step_id",
    "should_renotify",
    "signal_id",
    "taxonomy_key_for",
]

# ─────────────────────────────── vocabularies ───────────────────────────────

SignalOrigin = Literal["feed", "news", "connected_system", "chat", "demo"]
SIGNAL_ORIGINS: Final[tuple[str, ...]] = get_args(SignalOrigin)

SignalKind = Literal[
    "natural_hazard",
    "weather",
    "geopolitical",
    "labor",
    "logistics",
    "regulatory",
    "cyber",
    "supplier",
    "demand",
    "hypothetical",
]
SIGNAL_KINDS: Final[tuple[str, ...]] = get_args(SignalKind)

SignalProvenance = Literal["fact", "inference", "simulated"]
"""``fact``: deterministic parse of a structured feed / connector field; ``inference``: from the
quarantined news/free-text extractor; ``simulated``: a demo pack (DEMO badge)."""
SIGNAL_PROVENANCES: Final[tuple[str, ...]] = get_args(SignalProvenance)

EventStatus = Literal["candidate", "confirmed", "monitoring", "resolved"]
EVENT_STATUSES: Final[tuple[str, ...]] = get_args(EventStatus)

NodeType = Literal["supplier", "factory", "port", "dc", "store", "lane"]
NODE_TYPES: Final[tuple[str, ...]] = get_args(NodeType)

ExposureMethod = Literal["intersects", "dwithin_km", "lane_crosses", "entity_match"]
EXPOSURE_METHODS: Final[tuple[str, ...]] = get_args(ExposureMethod)

AlertStatus = Literal["new", "seen", "snoozed", "dismissed", "opened", "resolved"]
ALERT_STATUSES: Final[tuple[str, ...]] = get_args(AlertStatus)

AlertSeverity = Literal["low", "medium", "high", "critical"]
ALERT_SEVERITIES: Final[tuple[str, ...]] = get_args(AlertSeverity)

AlertAction = Literal["alert", "alert_and_draft_case", "ignore"]
ALERT_ACTIONS: Final[tuple[str, ...]] = get_args(AlertAction)

PackAdapter = Literal["gdacs", "usgs", "rss", "merxa_inject", "chat_hint"]
PACK_ADAPTERS: Final[tuple[str, ...]] = get_args(PackAdapter)

# ───────────────────────────────── scalars ──────────────────────────────────

UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UUID_RE: Final = re.compile(UUID_PATTERN)
Uuid = Annotated[str, Field(pattern=UUID_PATTERN)]
"""A canonical lowercase UUID string (signal, event and alert ids)."""

BasisPoints10k = Annotated[StrictInt, Field(ge=0, le=BP_ONE)]
"""An integer in ``0..10000`` basis points."""

SUBTYPE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
Subtype = Annotated[str, Field(pattern=SUBTYPE_PATTERN)]

CONNECTOR_ID_PATTERN: Final = r"^[a-z][a-z0-9_.-]{0,63}$"
ConnectorId = Annotated[str, Field(pattern=CONNECTOR_ID_PATTERN)]

LOCATION_REF_SCHEMES: Final = (
    "UNLOCODE", "ISO3166", "CHOKEPOINT", "REGION", "SUPPLIER", "FACTORY", "DC", "STORE", "LANE",
)
LOCATION_REF_PATTERN: Final = (
    r"^(UNLOCODE|ISO3166|CHOKEPOINT|REGION|SUPPLIER|FACTORY|DC|STORE|LANE):[A-Za-z0-9._>-]{1,64}$"
)
"""``SCHEME:code``: ``UNLOCODE:VNSGN``, ``ISO3166:VN``, ``CHOKEPOINT:SUEZ``, ``SUPPLIER:SUP-VN-01``…"""
LocationRef = Annotated[str, Field(pattern=LOCATION_REF_PATTERN)]
_UNLOCODE_RE: Final = re.compile(r"^[A-Z]{2}[A-Z2-9]{3}$")
_ISO3166_RE: Final = re.compile(r"^[A-Z]{2}$")

SOURCE_URL_PATTERN: Final = r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\x00-\x20\x7f\\]*)?$"
"""Attribution links rendered in the UI: absolute ``http(s)`` with a plain host (no userinfo,
whitespace, control characters or backslashes), so ``javascript:``/``data:`` never reach an href."""
SourceUrl = Annotated[str, Field(min_length=1, max_length=2000, pattern=SOURCE_URL_PATTERN)]

PACK_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"

MAX_RAW_TEXT_CHARS: Final = 4000
"""``raw_text`` cap (news keeps headline + a <= 300 char snippet; supplier messages are longer)."""
MAX_GEOMETRY_POSITIONS: Final = 50_000
"""Most positions one geometry may carry (TC cones are a few hundred)."""
MAX_PACK_STEPS: Final = 500

NonEmptyStr = Annotated[str, Field(min_length=1)]
Title = Annotated[str, Field(min_length=1, max_length=300)]


def is_uuid(value: object) -> bool:
    """True for a canonical lowercase UUID string."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))


# ────────────────────────────── deterministic ids ───────────────────────────

SIGNALS_NAMESPACE: Final = uuid.UUID("5b0f7e43-6a2d-5c1e-9b7a-6b6565c50a6e")
"""UUIDv5 namespace of every Signal Hub id (fixed forever: changing it re-keys stored rows)."""


def _uuid5(kind: str, *parts: str | int) -> str:
    for p in parts:
        if isinstance(p, bool) or not isinstance(p, (str, int)):
            raise TypeError(f"id parts must be str or int, got {type(p).__name__}")
    return str(uuid.uuid5(SIGNALS_NAMESPACE, canonical_json([kind, *parts])))


def signal_id(connector_id: str, native_key: str, version: int) -> str:
    """Id of one signal version: ``uuid5(connector_id, native id or canonical URL, version)``.

    The same item re-polled yields the same id (dedupe); an update bumps ``version`` and gets a
    new id whose ``supersedes`` is the previous one.
    """
    if not native_key:
        raise ValueError("native_key must not be empty")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be an int >= 1")
    return _uuid5("signal", connector_id, native_key, version)


def event_id(*parts: str | int) -> str:
    """Deterministic event id, e.g. ``event_id(connector_id, native_key)`` for an anchor (stable
    across versions) or ``event_id("signal", first_signal_id)`` for a news-only candidate."""
    if not parts:
        raise ValueError("event_id needs at least one part")
    return _uuid5("event", *parts)


def pack_step_id(pack_id: str, step_index: int) -> str:
    """``uuid5(scenario, step)`` used by demo replay (same pack step → same id on every run)."""
    return _uuid5("pack_step", pack_id, step_index)


# ───────────────────────────────── GeoJSON ──────────────────────────────────

Lon = Annotated[StrictFloat, Field(ge=-180, le=180, allow_inf_nan=False)]
Lat = Annotated[StrictFloat, Field(ge=-90, le=90, allow_inf_nan=False)]
Position = tuple[Lon, Lat]
"""``[lon, lat]`` in WGS84 degrees (floats; ints are accepted). Altitude/depth is dropped by the
adapters (USGS ``[lon, lat, depth]`` → ``[lon, lat]``)."""

GEOMETRY_TYPES: Final = (
    "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection",
)

LineCoords = Annotated[list[Position], Field(min_length=2)]
Ring = Annotated[list[Position], Field(min_length=4)]
PolygonCoords = Annotated[list[Ring], Field(min_length=1)]


def _check_rings(rings: Iterable[list[tuple[float, float]]]) -> None:
    for ring in rings:
        if ring[0] != ring[-1]:
            raise ValueError("polygon rings must be closed (first position == last)")


class Point(ContractModel):
    type: Literal["Point"]
    coordinates: Position


class MultiPoint(ContractModel):
    type: Literal["MultiPoint"]
    coordinates: Annotated[list[Position], Field(min_length=1)]


class LineString(ContractModel):
    type: Literal["LineString"]
    coordinates: LineCoords


class MultiLineString(ContractModel):
    type: Literal["MultiLineString"]
    coordinates: Annotated[list[LineCoords], Field(min_length=1)]


class Polygon(ContractModel):
    type: Literal["Polygon"]
    coordinates: PolygonCoords

    @model_validator(mode="after")
    def _closed(self) -> Polygon:
        _check_rings(self.coordinates)
        return self


class MultiPolygon(ContractModel):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[PolygonCoords], Field(min_length=1)]

    @model_validator(mode="after")
    def _closed(self) -> MultiPolygon:
        for polygon in self.coordinates:
            _check_rings(polygon)
        return self


SimpleGeometry = Annotated[
    Point | MultiPoint | LineString | MultiLineString | Polygon | MultiPolygon,
    Discriminator("type"),
]


class GeometryCollection(ContractModel):
    """A merged event footprint (e.g. TC track + cones). Members are simple geometries
    (nested collections are not allowed, per RFC 7946 §3.1.8 guidance)."""

    type: Literal["GeometryCollection"]
    geometries: Annotated[list[SimpleGeometry], Field(min_length=1)]


Geometry = Annotated[
    Point | MultiPoint | LineString | MultiLineString | Polygon | MultiPolygon | GeometryCollection,
    Discriminator("type"),
]
"""A GeoJSON geometry object (RFC 7946), float WGS84 ``[lon, lat]`` positions."""


def geometry_positions(geometry: Any) -> list[tuple[float, float]]:
    """Every position of a geometry model, flattened (for bbox / size checks)."""
    if isinstance(geometry, GeometryCollection):
        return [p for g in geometry.geometries for p in geometry_positions(g)]
    coords = getattr(geometry, "coordinates", None)
    out: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if isinstance(node, tuple):
            out.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(coords)
    return out


def _check_geometry_size(geometry: Any) -> None:
    if geometry is not None and len(geometry_positions(geometry)) > MAX_GEOMETRY_POSITIONS:
        raise ValueError(f"geometry has more than {MAX_GEOMETRY_POSITIONS} positions; simplify it first")


# ───────────────────────────────── signals ──────────────────────────────────


class SeverityNative(ContractModel):
    """The source's own severity terms, kept for display/audit: ``{scheme, …}``.

    ``scheme`` names the vocabulary (``gdacs``, ``usgs``, ``none`` …); the other keys are JSON
    scalars (e.g. ``alertlevel: "red"``, ``alertscore: 2.5``, ``mag: 6.8``, ``tsunami: false``).
    Never hashed; floats are allowed here.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)
    __pydantic_extra__: dict[str, str | StrictInt | StrictFloat | StrictBool | None]

    scheme: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,31}$")]

    @model_validator(mode="after")
    def _extras(self) -> SeverityNative:
        extra = self.__pydantic_extra__ or {}
        if len(extra) > 20:
            raise ValueError("severity_native carries at most 20 keys")
        for key, value in extra.items():
            if not re.match(r"^[a-z][a-z0-9_]{0,31}$", key):
                raise ValueError(f"severity_native key {key[:40]!r} must be snake_case (<= 32 chars)")
            if isinstance(value, str) and len(value) > 200:
                raise ValueError(f"severity_native.{key} is longer than 200 chars")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"severity_native.{key} must be finite")
        return self


class SourceRef(ContractModel):
    """Attribution of a signal (shown with its licence in the alert drawer)."""

    connector_id: ConnectorId
    """Signal Hub source id (``gdacs``, ``usgs``, ``rss.theloadstar``, ``merxa`` …)."""
    name: Annotated[str, Field(min_length=1, max_length=120)]
    url: SourceUrl | None
    license: Annotated[str, Field(min_length=1, max_length=120)]
    authority_bp: BasisPoints10k
    retrieved_at: AwareDatetime
    native_id: Annotated[str, Field(min_length=1, max_length=500)]
    """The source's id (GDACS ``eventid:episodeid``, USGS id, RSS guid / canonical URL …)."""


class DisruptionSignal(ContractModel):
    """One normalized observation (``100`` §2). ``tenant`` is ``None`` for global feed/news signals
    and set for tenant-origin ones (connected system, chat). ``geometry`` is ``None`` until an
    item is geocoded (e.g. a supplier message matched only by ``location_refs``)."""

    id: Uuid
    tenant: TenantSlug | None
    origin: SignalOrigin
    kind: SignalKind
    subtype: Subtype
    title: Title
    summary: Annotated[str, Field(max_length=2000)] | None
    raw_text: Annotated[str, Field(max_length=MAX_RAW_TEXT_CHARS)] | None
    """Untrusted text (news: headline + snippet only). Never shown to a tool-using LLM."""
    geometry: Geometry | None
    location_refs: Annotated[list[LocationRef], Field(max_length=200)]
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None
    severity_bp: BasisPoints10k
    severity_native: SeverityNative
    confidence_bp: BasisPoints10k
    provenance: SignalProvenance
    extracted_by: Annotated[str, Field(min_length=1, max_length=120)] | None
    """Who produced the inferred fields (LiteLLM alias such as ``extract-fast``, or ``prebaked``)."""
    source: SourceRef
    event_id: Uuid | None
    version: PositiveInt
    supersedes: Uuid | None

    @model_validator(mode="after")
    def _consistent(self) -> DisruptionSignal:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("ends_at must not be before starts_at")
        if self.origin in ("connected_system", "chat") and self.tenant is None:
            raise ValueError(f"{self.origin} signals are tenant-scoped (tenant required)")
        if self.origin in ("feed", "news") and self.tenant is not None:
            raise ValueError(f"{self.origin} signals are global (tenant must be null)")
        if self.origin == "demo" and self.provenance != "simulated":
            raise ValueError("demo signals must have provenance 'simulated'")
        if self.provenance == "inference" and self.extracted_by is None:
            raise ValueError("inference signals must name extracted_by")
        if self.provenance == "fact" and self.extracted_by is not None:
            raise ValueError("fact signals are parsed deterministically (extracted_by must be null)")
        if self.supersedes is not None and (self.version < 2 or self.supersedes == self.id):
            raise ValueError("supersedes is only set on version >= 2 and never names the signal itself")
        if len(set(self.location_refs)) != len(self.location_refs):
            raise ValueError("location_refs must be unique")
        for ref in self.location_refs:
            scheme, code = ref.split(":", 1)
            if scheme == "UNLOCODE" and not _UNLOCODE_RE.match(code):
                raise ValueError(f"{ref!r}: UN/LOCODE is 2 letters + 3 letters/digits (e.g. VNSGN)")
            if scheme == "ISO3166" and not _ISO3166_RE.match(code):
                raise ValueError(f"{ref!r}: ISO 3166-1 alpha-2 country code expected (e.g. VN)")
        _check_geometry_size(self.geometry)
        return self

    @property
    def simulated(self) -> bool:
        return self.provenance == "simulated"


def combine_confidence_bp(authority_bp: int, corroboration_bp: int, recency_bp: int) -> int:
    """``confidence = authority × corroboration × recency`` in basis points (half-up, 0..10000)."""
    for name, v in (("authority_bp", authority_bp), ("corroboration_bp", corroboration_bp), ("recency_bp", recency_bp)):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= BP_ONE:
            raise ValueError(f"{name} must be an int in 0..{BP_ONE}")
    num = authority_bp * corroboration_bp * recency_bp
    den = BP_ONE * BP_ONE
    q, r = divmod(num, den)
    return q + (1 if 2 * r >= den else 0)


# ───────────────────────────────── events ───────────────────────────────────


class EventWindow(ContractModel):
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None

    @model_validator(mode="after")
    def _ordered(self) -> EventWindow:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("window ends_at must not be before starts_at")
        return self


SUBTYPE_TAXONOMY: Final[Mapping[str, EventTypeKey]] = {
    "tropical_cyclone": "NAT.storm",
    "tropical_storm": "NAT.storm",
    "typhoon": "NAT.storm",
    "hurricane": "NAT.storm",
    "cyclone": "NAT.storm",
    "storm": "NAT.storm",
    "earthquake": "NAT.earthquake",
    "flood": "NAT.flood",
    "port_closure": "LOG.port_closure",
    "chokepoint_closure": "LOG.chokepoint_closure",
    "strike": "LOG.strike",
    "supplier_delay": "SUP.delay",
    "capacity_loss": "SUP.capacity_loss",
    "demand_spike": "DEM.spike",
    "demand_drop": "DEM.drop",
}
"""Deterministic event subtype → taxonomy key (``100`` §6); anything else is ``OTHER``.

Regional names of a tropical cyclone (``typhoon``, ``hurricane``, ``cyclone``, ``tropical_storm``)
map like ``tropical_cyclone``: the news extractor's free ``subtype`` must not turn a typhoon
headline into an ``OTHER`` event."""


def taxonomy_key_for(subtype: str) -> EventTypeKey:
    """The taxonomy key of an event subtype (``tropical_cyclone`` → ``NAT.storm``; unknown → ``OTHER``)."""
    return SUBTYPE_TAXONOMY.get(subtype, "OTHER")


class DisruptionEvent(ContractModel):
    """A cluster of signals (``100`` §2, §4). ``anchor_signal_id`` is the authoritative feed signal
    (GDACS/USGS) when there is one and is always a member. ``summary`` is ``None`` when nothing
    deterministic is known and no (optional) LLM summary was written."""

    id: Uuid
    tenant: TenantSlug | None
    kind: SignalKind
    subtype: Subtype
    title: Title
    summary: Annotated[str, Field(max_length=2000)] | None
    anchor_signal_id: Uuid | None
    member_signal_ids: Annotated[list[Uuid], Field(min_length=1, max_length=1000)]
    geometry: Geometry | None
    window: EventWindow
    severity_bp: BasisPoints10k
    confidence_bp: BasisPoints10k
    status: EventStatus
    taxonomy_key: EventTypeKey
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> DisruptionEvent:
        if len(set(self.member_signal_ids)) != len(self.member_signal_ids):
            raise ValueError("member_signal_ids must be unique")
        if self.anchor_signal_id is not None and self.anchor_signal_id not in self.member_signal_ids:
            raise ValueError("anchor_signal_id must be one of member_signal_ids")
        if self.taxonomy_key != taxonomy_key_for(self.subtype):
            raise ValueError(
                f"taxonomy_key {self.taxonomy_key!r} does not match subtype {self.subtype!r} "
                f"(expected {taxonomy_key_for(self.subtype)!r})"
            )
        _check_geometry_size(self.geometry)
        return self


# ──────────────────────────────── exposure ──────────────────────────────────

NodeCode = Annotated[str, Field(min_length=1, max_length=120)]


class Exposure(ContractModel):
    """One tenant node or lane an event touches (``100`` §5)."""

    event_id: Uuid
    tenant: TenantSlug
    node_type: NodeType
    node_code: NodeCode
    method: ExposureMethod
    distance_km: NonNegInt | None
    """Whole kilometres from the footprint (``dwithin_km`` requires it; ``intersects`` → 0 or null)."""
    weight_bp: BasisPoints10k

    @model_validator(mode="after")
    def _consistent(self) -> Exposure:
        if self.method == "dwithin_km" and self.distance_km is None:
            raise ValueError("dwithin_km exposures carry distance_km")
        if self.method == "lane_crosses" and self.node_type != "lane":
            raise ValueError("lane_crosses applies to lanes only")
        if self.node_type == "lane" and self.method not in ("lane_crosses", "entity_match"):
            raise ValueError("lanes are exposed by lane_crosses or entity_match")
        return self


class NodeCounts(ContractModel):
    """Exposed nodes per type (every type present, 0 when none)."""

    supplier: NonNegInt
    factory: NonNegInt
    port: NonNegInt
    dc: NonNegInt
    store: NonNegInt
    lane: NonNegInt

    @classmethod
    def of(cls, exposures: Iterable[Exposure]) -> NodeCounts:
        """Count distinct ``(node_type, node_code)`` pairs."""
        seen = {(e.node_type, e.node_code) for e in exposures}
        counts = dict.fromkeys(NODE_TYPES, 0)
        for node_type, _ in seen:
            counts[node_type] += 1
        return cls(**counts)

    def get(self, node_type: str) -> int:
        value = getattr(self, node_type)
        assert isinstance(value, int)
        return value

    @property
    def sites(self) -> int:
        """Exposed non-lane nodes."""
        return self.supplier + self.factory + self.port + self.dc + self.store

    @property
    def total(self) -> int:
        return self.sites + self.lane


class ExposureSummary(ContractModel):
    """Counts for the inbox row ("3 sites · 4 lanes · 2 open POs"). All numbers come from the tenant
    network snapshot / ERP reads, never from an LLM; ``None`` = not computed."""

    nodes_by_type: NodeCounts
    open_po_lines: NonNegInt | None
    units_inbound: NonNegInt | None


# ───────────────────────────────── alerts ───────────────────────────────────

_SEVERITY_BANDS: Final[tuple[tuple[int, AlertSeverity], ...]] = (
    (8500, "critical"),
    (7000, "high"),
    (4000, "medium"),
    (0, "low"),
)


def alert_severity_for(severity_bp: int) -> AlertSeverity:
    """Alert severity band of an event severity: ``>= 8500`` critical, ``>= 7000`` high (the
    default draft-case threshold), ``>= 4000`` medium (the default alert threshold), else low."""
    if isinstance(severity_bp, bool) or not isinstance(severity_bp, int) or not 0 <= severity_bp <= BP_ONE:
        raise ValueError(f"severity_bp must be an int in 0..{BP_ONE}")
    for floor, band in _SEVERITY_BANDS:
        if severity_bp >= floor:
            return band
    raise AssertionError("unreachable")


class Alert(ContractModel):
    """A tenant's alert for one event (``100`` §2, §5). ``why`` is a deterministic sentence built
    from the rule and the exposure; ``case_id`` is set once a case is opened (one per alert)."""

    id: Uuid
    tenant: TenantSlug
    event_id: Uuid
    status: AlertStatus
    severity: AlertSeverity
    title: Title
    why: Annotated[str, Field(min_length=1, max_length=500)]
    exposure_summary: ExposureSummary
    first_raised_at: AwareDatetime
    updated_at: AwareDatetime
    case_id: Annotated[str, Field(min_length=1, max_length=200)] | None
    simulated: StrictBool

    @model_validator(mode="after")
    def _consistent(self) -> Alert:
        if self.updated_at < self.first_raised_at:
            raise ValueError("updated_at must not be before first_raised_at")
        if self.status == "opened" and self.case_id is None:
            raise ValueError("an opened alert names its case_id")
        return self


class AlertStatusUpdate(ContractModel):
    """Body of ``POST /v1/alerts/{id}/status``. ``opened`` (set by the engine's open-case) carries
    the ``case_id``; other statuses must not. ``new`` cannot be set back by hand."""

    status: Literal["seen", "snoozed", "dismissed", "opened", "resolved"]
    case_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _case(self) -> AlertStatusUpdate:
        if (self.status == "opened") != (self.case_id is not None):
            raise ValueError("case_id is required for 'opened' and not allowed otherwise")
        return self


# ─────────────────────── event membership (M8, ``123`` §5) ───────────────────────

MembershipMethod = Literal[
    "anchor",
    "same_hazard",
    "founded",
    "attach_geo",
    "attach_ref",
    "attach_vector",
    "near_duplicate",
    "absorbed",
    "merged_vector",
]
"""Why a signal is a member of its event (clustering v2, ``docs/contracts/123``):

* ``anchor`` — an authoritative feed signal (GDACS, USGS) that founded the event;
* ``same_hazard`` — a second authoritative feed reporting the same hazard (v1 rule);
* ``founded`` — the first report of a news-only / connector event (nothing to join);
* ``attach_ref`` / ``attach_geo`` — the v1 attach rule by shared place code / by distance;
* ``attach_vector`` — similar cluster terms (local term vectors) where v1 found nothing;
* ``near_duplicate`` — a syndicated copy (SimHash) of a report already in the event;
* ``absorbed`` — moved when an official report founded an event it attaches to (v1);
* ``merged_vector`` — moved by the background merge pass from a similar candidate event."""
MEMBERSHIP_METHODS: Final[tuple[str, ...]] = get_args(MembershipMethod)
_MOVED_METHODS: Final = frozenset({"absorbed", "merged_vector"})
_NO_EVIDENCE_METHODS: Final = frozenset({"anchor", "founded"})

SIMILARITY_PROFILE_PATTERN: Final = r"^[a-z0-9][a-z0-9.@_-]{0,63}$"
"""A similarity profile id: ``local-terms-v1`` (the deterministic term vectors) or a remote
embedding profile ``<alias>@<dims>`` (e.g. ``embed-default@1536``)."""
SimilarityProfile = Annotated[str, Field(pattern=SIMILARITY_PROFILE_PATTERN)]
MAX_MEMBERSHIP_EXPLANATION: Final = 400
HammingBits = Annotated[StrictInt, Field(ge=0, le=64)]


class EventMembership(ContractModel):
    """Why one signal belongs to its event (``123`` §5): the method, its evidence and a
    deterministic server-built ``explanation`` that never contains untrusted text (it is built from
    the method, integers, place codes and the gazetteer's curated names only).

    Evidence per method: ``attach_ref`` names ``shared_refs``; ``attach_geo`` and ``same_hazard``
    carry ``distance_km``; ``attach_vector`` / ``merged_vector`` carry ``similarity_bp`` +
    ``similarity_profile``; ``near_duplicate`` carries ``hamming_bits`` (of 64 SimHash bits) and the
    report it copies (``duplicate_of_signal_id``); ``absorbed`` / ``merged_vector`` name the event
    the signal came from (``from_event_id``) and its original method (``prior_method``);
    ``anchor`` / ``founded`` carry no evidence."""

    signal_id: Uuid
    event_id: Uuid
    method: MembershipMethod
    prior_method: MembershipMethod | None
    from_event_id: Uuid | None
    distance_km: NonNegInt | None
    shared_refs: Annotated[list[LocationRef], Field(max_length=50)]
    similarity_bp: BasisPoints10k | None
    similarity_profile: SimilarityProfile | None
    hamming_bits: HammingBits | None
    duplicate_of_signal_id: Uuid | None
    config_version: PositiveInt
    """``ClusteringConfig.version`` in force when the decision was made."""
    explanation: Annotated[str, Field(min_length=1, max_length=MAX_MEMBERSHIP_EXPLANATION)]
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _evidence(self) -> EventMembership:
        m = self.method
        if len(set(self.shared_refs)) != len(self.shared_refs):
            raise ValueError("shared_refs must be unique")
        if m in _NO_EVIDENCE_METHODS and (
            self.distance_km is not None or self.shared_refs or self.similarity_bp is not None
            or self.similarity_profile is not None or self.hamming_bits is not None
            or self.duplicate_of_signal_id is not None
        ):
            raise ValueError(f"{m} memberships carry no evidence")
        if m in ("attach_vector", "merged_vector") and (self.similarity_bp is None or self.similarity_profile is None):
            raise ValueError(f"{m} memberships carry similarity_bp and similarity_profile")
        if (self.similarity_bp is None) != (self.similarity_profile is None):
            raise ValueError("similarity_bp and similarity_profile go together")
        if m == "near_duplicate" and (self.hamming_bits is None or self.duplicate_of_signal_id is None):
            raise ValueError("near_duplicate memberships carry hamming_bits and duplicate_of_signal_id")
        if self.duplicate_of_signal_id is not None and self.duplicate_of_signal_id == self.signal_id:
            raise ValueError("a signal is never a near-duplicate of itself")
        if m == "attach_ref" and not self.shared_refs:
            raise ValueError("attach_ref memberships name the shared_refs")
        if m in ("attach_geo", "same_hazard") and self.distance_km is None:
            raise ValueError(f"{m} memberships carry distance_km")
        if m in _MOVED_METHODS:
            if self.from_event_id is None or self.prior_method is None:
                raise ValueError(f"{m} memberships name from_event_id and prior_method")
            if self.from_event_id == self.event_id:
                raise ValueError("from_event_id must differ from event_id")
        elif self.from_event_id is not None or self.prior_method is not None:
            raise ValueError("only absorbed / merged_vector memberships carry from_event_id / prior_method")
        if self.prior_method in _MOVED_METHODS:
            raise ValueError("prior_method is the original grouping method (never absorbed / merged_vector)")
        return self


def _is_empty(value: Any) -> bool:
    return not value


class AlertDetail(ContractModel):
    """``GET /v1/alerts/{id}``: the alert with its event, member signals (attribution) and exposures.

    ``memberships`` (M8, optional): why each member signal belongs to the event. Defaults to ``[]``
    and is omitted from dumps while empty, so M6/M7 payloads are unchanged."""

    alert: Alert
    event: DisruptionEvent
    signals: list[DisruptionSignal]
    exposures: list[Exposure]
    memberships: Annotated[list[EventMembership], Field(max_length=1000)] = Field(
        default_factory=list, exclude_if=_is_empty
    )

    @model_validator(mode="after")
    def _consistent(self) -> AlertDetail:
        if self.alert.event_id != self.event.id:
            raise ValueError("alert.event_id must be event.id")
        members = set(self.event.member_signal_ids)
        if any(s.id not in members for s in self.signals):
            raise ValueError("signals must be members of the event")
        if any(e.event_id != self.event.id or e.tenant != self.alert.tenant for e in self.exposures):
            raise ValueError("exposures must belong to the alert's event and tenant")
        if any(m.event_id != self.event.id or m.signal_id not in members for m in self.memberships):
            raise ValueError("memberships must belong to the event and name one of its member signals")
        if len({m.signal_id for m in self.memberships}) != len(self.memberships):
            raise ValueError("one membership per signal")
        return self


class AlertCondition(ContractModel):
    """When a rule matches. Empty ``kinds`` / ``node_types`` = any; ``min_exposed_nodes`` counts
    exposed nodes of ``node_types`` (all types, lanes included, when empty).

    Immutable (frozen, tuple fields) so the shared :data:`DEFAULT_ALERT_RULES` can never be
    changed in place by one tenant's rule handling; derive variants with ``model_copy(update=…)``."""

    model_config = ConfigDict(frozen=True)

    min_severity_bp: BasisPoints10k
    kinds: Annotated[tuple[SignalKind, ...], Field(max_length=len(SIGNAL_KINDS))]
    min_confidence_bp: BasisPoints10k
    min_exposed_nodes: NonNegInt
    node_types: Annotated[tuple[NodeType, ...], Field(max_length=len(NODE_TYPES))]

    @model_validator(mode="after")
    def _unique(self) -> AlertCondition:
        if len(set(self.kinds)) != len(self.kinds) or len(set(self.node_types)) != len(self.node_types):
            raise ValueError("kinds and node_types must be unique")
        return self

    def matches(self, *, severity_bp: int, confidence_bp: int, kind: str, counts: NodeCounts) -> bool:
        if severity_bp < self.min_severity_bp or confidence_bp < self.min_confidence_bp:
            return False
        if self.kinds and kind not in self.kinds:
            return False
        exposed = sum(counts.get(t) for t in (self.node_types or NODE_TYPES))
        return exposed >= self.min_exposed_nodes


class AlertRule(ContractModel):
    """A per-tenant alert rule (``100`` §2, §5). ``hysteresis_bp``: an update edits the existing
    alert and only re-notifies when severity rose by at least this much. Immutable, like
    :class:`AlertCondition` (the defaults are shared module state)."""

    model_config = ConfigDict(frozen=True)

    id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
    when: AlertCondition
    action: AlertAction
    hysteresis_bp: BasisPoints10k


DEFAULT_ALERT_RULES: Final[tuple[AlertRule, ...]] = (
    AlertRule(
        id="default-alert",
        when=AlertCondition(min_severity_bp=4000, kinds=(), min_confidence_bp=0, min_exposed_nodes=1, node_types=()),
        action="alert",
        hysteresis_bp=1000,
    ),
    AlertRule(
        id="default-draft-case",
        when=AlertCondition(
            min_severity_bp=7000, kinds=(), min_confidence_bp=0, min_exposed_nodes=1,
            node_types=("supplier", "factory", "port"),
        ),
        action="alert_and_draft_case",
        hysteresis_bp=1000,
    ),
)
"""``100`` §5 defaults: alert at severity >= 4000 bp with >= 1 exposed node; draft a case at
>= 7000 bp with an exposed supplier/factory/port; re-notify on a rise of >= 1000 bp."""

_ACTION_RANK: Final[Mapping[str, int]] = {"alert": 1, "alert_and_draft_case": 2}


def decide_alert_action(
    rules: Iterable[AlertRule],
    *,
    severity_bp: int,
    confidence_bp: int,
    kind: str,
    counts: NodeCounts,
) -> tuple[AlertAction, AlertRule] | None:
    """Evaluate a tenant's rules for one (event, exposure).

    ``None`` when no rule matches (no alert). A matching ``ignore`` rule suppresses the event
    (explicit suppression wins, first such rule returned). Otherwise the strongest matching action
    wins (``alert_and_draft_case`` > ``alert``; ties → first rule in order). Never commits anything.
    """
    best: tuple[AlertAction, AlertRule] | None = None
    for rule in rules:
        if not rule.when.matches(severity_bp=severity_bp, confidence_bp=confidence_bp, kind=kind, counts=counts):
            continue
        if rule.action == "ignore":
            return ("ignore", rule)
        if best is None or _ACTION_RANK[rule.action] > _ACTION_RANK[best[0]]:
            best = (rule.action, rule)
    return best


def should_renotify(notified_severity_bp: int, new_severity_bp: int, hysteresis_bp: int) -> bool:
    """Hysteresis: re-notify only when severity rose by at least ``hysteresis_bp`` since the last
    notification (an update below that just edits the alert)."""
    return new_severity_bp - notified_severity_bp >= max(hysteresis_bp, 1)


# ───────────────────────────── tenant network (§5) ──────────────────────────


class NetworkNode(ContractModel):
    """A tenant node (stores aggregated to clusters), float WGS84 position."""

    type: Literal["supplier", "factory", "port", "dc", "store"]
    code: NodeCode
    name: Annotated[str, Field(max_length=200)]
    lat: Lat
    lon: Lon


class NetworkLane(ContractModel):
    """A lane with its path ``[[lon, lat], …]`` (from ``data/geo/sea_routes.json``)."""

    code: NodeCode
    mode: Annotated[str, Field(min_length=1, max_length=32)]
    path: Annotated[list[Position], Field(min_length=2, max_length=5000)]


class TenantNetwork(ContractModel):
    """Body of ``PUT /v1/tenants/{tenant}/network`` (pushed by the engine after every scn sync)."""

    nodes: Annotated[list[NetworkNode], Field(max_length=20_000)]
    lanes: Annotated[list[NetworkLane], Field(max_length=5_000)]

    @model_validator(mode="after")
    def _unique(self) -> TenantNetwork:
        keys = [(n.type, n.code) for n in self.nodes]
        if len(set(keys)) != len(keys):
            raise ValueError("nodes must be unique by (type, code)")
        codes = [lane.code for lane in self.lanes]
        if len(set(codes)) != len(codes):
            raise ValueError("lane codes must be unique")
        return self


# ─────────────────────────────── demo packs (§7) ────────────────────────────


class ScenarioPackStep(ContractModel):
    """One timeline step: at ``t_offset_s`` after the pack start, feed ``payload`` to ``adapter``
    (fixture served at ``/demo-feeds/<pack>/…`` so the real adapter parses it)."""

    t_offset_s: NonNegInt
    adapter: PackAdapter
    payload: dict[str, Any]
    note: Annotated[str, Field(max_length=500)]


class ScenarioPack(ContractModel):
    """A deterministic demo pack (``data/scenarios/<id>/pack.yaml``). Everything it produces is
    ``origin demo`` / ``provenance simulated`` and alerts are ``simulated: true``."""

    id: Annotated[str, Field(pattern=PACK_ID_PATTERN)]
    title: Title
    description: Annotated[str, Field(max_length=2000)]
    start_at: AwareDatetime
    steps: Annotated[list[ScenarioPackStep], Field(min_length=1, max_length=MAX_PACK_STEPS)]

    @model_validator(mode="after")
    def _ordered(self) -> ScenarioPack:
        offsets = [s.t_offset_s for s in self.steps]
        if offsets != sorted(offsets):
            raise ValueError("steps must be ordered by t_offset_s")
        return self

    def due_steps(self, now: dt.datetime, *, after_index: int = -1) -> list[tuple[int, ScenarioPackStep]]:
        """``(index, step)`` pairs due at ``now`` (sim clock) with index > ``after_index``."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        elapsed = (now - self.start_at).total_seconds()
        return [(i, s) for i, s in enumerate(self.steps) if i > after_index and s.t_offset_s <= elapsed]

    def step_ids(self) -> list[str]:
        """Deterministic id per step (:func:`pack_step_id`)."""
        return [pack_step_id(self.id, i) for i in range(len(self.steps))]


# ──────────────────── clustering v2 settings + status (M8, ``123`` §7) ────────────────────

LOCAL_SIMILARITY_PROFILE: Final = "local-terms-v1"
"""The deterministic local similarity profile (weighted cluster-term vectors, integer cosine)."""
MIN_ATTACH_BP: Final = 5000
MAX_ATTACH_BP: Final = 9900


class SimHashSettings(ContractModel):
    """Near-duplicate detection of news items (64-bit SimHash over headline + snippet).

    ``max_hamming_bits`` <= 7 keeps the 8 x 8-bit band index exact (any pair within 7 bits shares
    a band); ``reuse_extraction`` lets a cross-source copy that names every entity of its original
    reuse the original's extraction (no LLM call) after its own pattern screen."""

    model_config = ConfigDict(frozen=True)

    enabled: StrictBool
    max_hamming_bits: Annotated[StrictInt, Field(ge=0, le=7)]
    window_hours: Annotated[StrictInt, Field(ge=1, le=168)]
    reuse_extraction: StrictBool


class VectorThresholds(ContractModel):
    """Similarity thresholds of one profile (basis points of cosine similarity):
    ``attach_bp`` joins a signal to an event v1 could not place; ``merge_bp`` merges two candidate
    events; ``strict_bp`` applies when there is no geometry on one side and no shared country."""

    model_config = ConfigDict(frozen=True)

    attach_bp: BasisPoints10k
    merge_bp: BasisPoints10k
    strict_bp: BasisPoints10k

    @model_validator(mode="after")
    def _ordered(self) -> VectorThresholds:
        if not MIN_ATTACH_BP <= self.attach_bp <= MAX_ATTACH_BP:
            raise ValueError(f"attach_bp must be between {MIN_ATTACH_BP} and {MAX_ATTACH_BP}")
        if self.merge_bp < self.attach_bp:
            raise ValueError("merge_bp must be >= attach_bp")
        if self.strict_bp < self.attach_bp:
            raise ValueError("strict_bp must be >= attach_bp")
        return self


class VectorSettings(ContractModel):
    """Vector attach / merge (clustering v2). ``local`` thresholds apply to the deterministic term
    vectors (ingest and replay), ``remote`` to an opt-in embedding profile (background merges)."""

    model_config = ConfigDict(frozen=True)

    enabled: StrictBool
    window_hours: Annotated[StrictInt, Field(ge=1, le=168)]
    max_distance_km: Annotated[StrictInt, Field(ge=50, le=3000)]
    local: VectorThresholds
    remote: VectorThresholds


class ClusteringConfig(ContractModel):
    """Platform-wide clustering settings (one versioned row on the hub; global feeds cluster once
    for every tenant, so no tenant can change them). Immutable: the default is module state."""

    model_config = ConfigDict(frozen=True)

    version: PositiveInt
    simhash: SimHashSettings
    vector: VectorSettings
    updated_at: AwareDatetime | None
    updated_by: Annotated[str, Field(min_length=1, max_length=200)] | None


DEFAULT_CLUSTERING_CONFIG: Final = ClusteringConfig(
    version=1,
    simhash=SimHashSettings(enabled=True, max_hamming_bits=6, window_hours=72, reuse_extraction=True),
    vector=VectorSettings(
        enabled=True,
        window_hours=72,
        max_distance_km=1000,
        local=VectorThresholds(attach_bp=7000, merge_bp=7500, strict_bp=7500),
        remote=VectorThresholds(attach_bp=8500, merge_bp=9000, strict_bp=9300),
    ),
    updated_at=None,
    updated_by=None,
)
"""Calibrated against ``services/signal-hub/tests/fixtures/m8_cluster_pairs.yaml`` (every labelled
pair is classified correctly with >= 300 bp margin; ``123`` §4)."""


class ClusteringConfigUpdate(ContractModel):
    """``PUT /v1/clustering/config`` (platform scope): the whole settings, optimistic on
    ``base_version`` (a stale base answers 409)."""

    base_version: PositiveInt
    simhash: SimHashSettings
    vector: VectorSettings
    updated_by: Annotated[str, Field(min_length=1, max_length=200)] | None = None


EmbeddingProfileStatus = Literal["active", "backfilling"]
RemoteProfileReason = Literal["provider_local", "pgvector_missing", "profile_missing", "key_missing"]


class RemoteEmbeddingProfile(ContractModel):
    """The active remote embedding profile (opt-in ``SIGNALS_EMBED_PROVIDER=litellm``)."""

    alias: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    dims: Annotated[StrictInt, Field(ge=1, le=4000)]
    storage: Annotated[str, Field(pattern=r"^(vector|halfvec)\([1-9][0-9]{0,3}\)$")]
    status: EmbeddingProfileStatus
    pending: NonNegInt


class ClusteringBackend(ContractModel):
    """Which similarity backends are available: the local term vectors always; a remote profile
    only with pgvector, the litellm provider and a provisioned profile (else ``remote_reason``)."""

    pgvector: StrictBool
    local_profile: Literal["local-terms-v1"]
    remote: RemoteEmbeddingProfile | None
    remote_reason: RemoteProfileReason | None


class ClusteringCounts(ContractModel):
    """Counts over the status window (global rows + the caller tenant's own rows)."""

    simhash_near_duplicates: NonNegInt
    same_source_duplicates: NonNegInt
    extractions_reused: NonNegInt
    anchor: NonNegInt
    same_hazard: NonNegInt
    founded: NonNegInt
    attach_geo: NonNegInt
    attach_ref: NonNegInt
    attach_vector: NonNegInt
    near_duplicate: NonNegInt
    absorbed: NonNegInt
    merged_vector: NonNegInt
    embeddings_pending: NonNegInt
    embeddings_failed: NonNegInt


class ClusteringStatus(ContractModel):
    """``GET /v1/clustering``: settings, backends and the window's counts."""

    config: ClusteringConfig
    vector_backend: ClusteringBackend
    window_days: PositiveInt
    counts: ClusteringCounts
