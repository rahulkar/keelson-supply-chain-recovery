"""Canonical supply-network model ``scn/1.0`` (``docs/contracts/25-scn-1.0.md``): one row
model per entity, plus the snapshot manifest/export shapes of the Tier-B tools
(``scn.snapshot_open``, ``scn.snapshot_export``, ``scn.changes_since``,
``22-merxa-m2.md`` §4). Used by the engine sync (``80-m3-deterministic-core.md`` §1).

Row conventions (as in the Parquet files): integers for quantities and money (minor
units, ``currency`` where relevant), ISO dates, ISO weeks ``YYYY-Www``; every row has
``source_id`` (stable key in the source system) and, where the entity is versioned,
``src_version`` (int ETag, 0 if not versioned). ``location.lat``/``lon`` are float64
degrees in ``scn/1.0``; :func:`to_e6` converts them to the integer ``lat_e6``/``lon_e6``
used by ``ProblemData`` and the map blocks.

Columns are listed in :data:`SCN_COLUMNS` in contract order (e.g. for ``COPY``);
``SCN_ROW_MODELS`` maps entity names to row models. Integer columns are strict; key and
quantity columns are required, descriptive columns are nullable because vendors differ.
Unknown columns are rejected so a schema drift fails the sync loudly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

from .common import IsoDate, IsoWeek, Sha256Hex

__all__ = [
    "AsOfRange",
    "CANONICAL_MODEL",
    "CRAWLED_ADVISORY_TEXT",
    "SnapshotConsistency",
    "SnapshotLabel",
    "SCN_COLUMNS",
    "SCN_ENTITIES",
    "SCN_ROW_MODELS",
    "AllocationRow",
    "BomLineRow",
    "ChangeEntry",
    "ChangesSince",
    "DemandForecastRow",
    "InventoryPositionRow",
    "ItemRow",
    "LaneRateRow",
    "LaneRow",
    "LocationRow",
    "OnlineOrderLineRow",
    "PoLineRow",
    "ProductionOrderRow",
    "PromotionRow",
    "SailingRow",
    "ScnRow",
    "SizeCurveRow",
    "SnapshotEntityManifest",
    "SnapshotExport",
    "SnapshotManifest",
    "SnapshotPart",
    "SnapshotStatus",
    "TransferOrderLineRow",
    "part_uri_allowed",
    "parse_row",
    "to_e6",
]

CANONICAL_MODEL: Final = "scn/1.0"

SnapshotStatus = Literal["loading", "ready", "failed", "superseded"]
"""Engine-side ``scn_snapshot.status``."""

SnapshotConsistency = Literal["consistent", "crawled"]
"""M7 (``110`` §4): ``consistent`` = one point-in-time export (tier B, ``as_of``); ``crawled`` =
gathered by paging plain read tools (tier A) over a time range (``as_of_range``). Plans built on
a crawled snapshot are advisory."""

CRAWLED_ADVISORY_TEXT: Final = "Data gathered by paging, not a consistent snapshot"
"""The advisory label shown on blocks and in the narrative of plans built on crawled data."""

Int = StrictInt
NonNegInt = Annotated[StrictInt, Field(ge=0)]
Code = Annotated[str, Field(min_length=1)]
Version = Annotated[StrictInt, Field(ge=0)]
"""``src_version``: int ETag, 0 if not versioned."""


def _none_to_empty(value: Any) -> Any:
    return [] if value is None else value


StrList = Annotated[list[str], BeforeValidator(_none_to_empty)]
"""A ``list<string>`` column (a null list reads as ``[]``)."""


def _degrees(limit: int) -> AfterValidator:
    def check(value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or abs(value) > limit):
            raise ValueError(f"degrees must be finite and within +/-{limit}")
        return value

    return AfterValidator(check)


Lat = Annotated[StrictFloat | StrictInt | None, _degrees(90)]
"""Latitude in float degrees (``scn/1.0`` keeps float64; see :func:`to_e6`)."""
Lon = Annotated[StrictFloat | StrictInt | None, _degrees(180)]


def to_e6(degrees: float | int | None) -> int | None:
    """Degrees -> integer degrees x 1e6 (half away from zero on the decimal value):
    ``51.5072 -> 51507200``; ``None`` stays ``None``."""
    if degrees is None:
        return None
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)) or not math.isfinite(degrees):
        raise ValueError(f"not a finite coordinate: {degrees!r}")
    return int((Decimal(repr(degrees)) * 1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class ScnRow(BaseModel):
    """Base of every ``scn/1.0`` row model (closed, frozen)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ─────────────────────────────────── entities ───────────────────────────────


class LocationRow(ScnRow):
    source_id: Code
    kind: Literal["dc", "store", "cluster", "port", "factory", "supplier", "airport"]
    code: Code
    name: str | None = None
    country: str | None = None
    city: str | None = None
    lat: Lat = None
    lon: Lon = None
    parent_code: str | None = None
    """store -> cluster, cluster -> dc, factory -> supplier."""
    grade: str | None = None
    climate: str | None = None
    unlocode: str | None = None
    src_version: Version

    @property
    def lat_e6(self) -> int | None:
        return to_e6(self.lat)

    @property
    def lon_e6(self) -> int | None:
        return to_e6(self.lon)


class ItemRow(ScnRow):
    source_id: Code
    kind: Literal["sku", "style", "component"]
    code: Code
    style_code: str | None = None
    family: str | None = None
    colour: str | None = None
    size: str | None = None
    size_seq: Int | None = None
    retail_price_cents: NonNegInt | None = None
    unit_cost_cents: NonNegInt | None = None
    currency: str | None = None
    src_version: Version


class BomLineRow(ScnRow):
    source_id: Code
    style_code: Code
    component_code: Code
    supplier_code: Code
    factory_code: str | None = None
    """``None`` = every factory."""
    qty_per_unit: NonNegInt
    src_version: Version


class LaneRow(ScnRow):
    source_id: Code
    code: Code
    origin_code: Code
    dest_code: Code
    mode: Literal["sea", "air", "sea_air", "road"]
    carrier_code: str | None = None
    transit_days: NonNegInt
    via: StrList = Field(default_factory=list)
    alt_lane_code: str | None = None
    src_version: Version


class LaneRateRow(ScnRow):
    source_id: Code
    lane_code: Code
    valid_from: IsoDate
    valid_to: IsoDate | None = None
    basis: Code
    rate_cents: NonNegInt
    weekly_capacity: NonNegInt | None = None
    src_version: Version

    @model_validator(mode="after")
    def _ordered(self) -> LaneRateRow:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to before valid_from")
        return self


class SailingRow(ScnRow):
    source_id: Code
    code: Code
    lane_code: Code
    cutoff: IsoDate | None = None
    etd: IsoDate | None = None
    eta: IsoDate | None = None
    capacity_teu: NonNegInt | None = None
    status: Code
    src_version: Version


class PoLineRow(ScnRow):
    source_id: Code
    """``<po>#<line>``."""
    po_code: Code
    line_no: Annotated[StrictInt, Field(ge=1)]
    supplier_code: Code
    factory_code: str | None = None
    dest_dc_code: Code
    sku_code: Code
    qty_ordered: NonNegInt
    qty_received: NonNegInt
    ex_factory_date: IsoDate | None = None
    eta_dc: IsoDate | None = None
    status: Code
    shipment_code: str | None = None
    sailing_code: str | None = None
    src_version: Version

    @property
    def open_qty(self) -> int:
        return max(self.qty_ordered - self.qty_received, 0)


class ProductionOrderRow(ScnRow):
    source_id: Code
    code: Code
    po_line_ref: str | None = None
    line_code: Code
    seq: NonNegInt
    planned_start: IsoDate
    planned_end: IsoDate
    status: Code
    hold_reason: str | None = None
    src_version: Version


class InventoryPositionRow(ScnRow):
    source_id: Code
    location_kind: Literal["dc", "store"]
    location_code: Code
    sku_code: Code
    on_hand: NonNegInt
    allocated: NonNegInt
    reserved: NonNegInt
    in_transit: NonNegInt
    src_version: Version


class DemandForecastRow(ScnRow):
    source_id: Code
    style_code: Code
    location_kind: Literal["store", "cluster"]
    location_code: Code
    iso_week: IsoWeek
    units: NonNegInt
    forecast_version: Version


class SizeCurveRow(ScnRow):
    source_id: Code
    family: Code
    cluster_code: Code
    size: Code
    share_bp: Annotated[StrictInt, Field(ge=0, le=10_000)]


class AllocationRow(ScnRow):
    source_id: Code
    code: Code
    sku_code: Code
    dc_code: Code
    store_code: str | None = None
    ship_week: IsoWeek
    qty: NonNegInt
    status: Code
    src_version: Version


class TransferOrderLineRow(ScnRow):
    source_id: Code
    to_code: Code
    from_dc: Code
    to_dc: Code
    ship_date: IsoDate
    eta_date: IsoDate | None = None
    sku_code: Code
    qty: NonNegInt
    status: Code
    src_version: Version


class OnlineOrderLineRow(ScnRow):
    source_id: Code
    order_code: Code
    market: Code
    channel: Code
    store_code: str | None = None
    sku_code: Code
    qty: NonNegInt
    reserved_qty: NonNegInt
    promise_date: IsoDate | None = None
    status: Code
    src_version: Version


class PromotionRow(ScnRow):
    source_id: Code
    code: Code
    markets: StrList = Field(default_factory=list)
    grades: StrList = Field(default_factory=list)
    style_codes: StrList = Field(default_factory=list)
    period_start: IsoDate
    period_end: IsoDate
    discount_bp: Annotated[StrictInt, Field(ge=0, le=10_000)]
    uplift_bp: NonNegInt
    src_version: Version

    @model_validator(mode="after")
    def _ordered(self) -> PromotionRow:
        if self.period_end < self.period_start:
            raise ValueError("period_end before period_start")
        return self


SCN_ROW_MODELS: Final[Mapping[str, type[ScnRow]]] = {
    "location": LocationRow,
    "item": ItemRow,
    "bom_line": BomLineRow,
    "lane": LaneRow,
    "lane_rate": LaneRateRow,
    "sailing": SailingRow,
    "po_line": PoLineRow,
    "production_order": ProductionOrderRow,
    "inventory_position": InventoryPositionRow,
    "demand_forecast": DemandForecastRow,
    "size_curve": SizeCurveRow,
    "allocation": AllocationRow,
    "transfer_order_line": TransferOrderLineRow,
    "online_order_line": OnlineOrderLineRow,
    "promotion": PromotionRow,
}
"""Entity name -> row model, in ``25-scn-1.0.md`` order."""

SCN_ENTITIES: Final[tuple[str, ...]] = tuple(SCN_ROW_MODELS)

SCN_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    name: tuple(model.model_fields) for name, model in SCN_ROW_MODELS.items()
}
"""Entity name -> column names in contract order."""


def parse_row(entity: str, row: Mapping[str, Any]) -> ScnRow:
    """Validate one row of ``entity`` (raises ``KeyError`` for an unknown entity)."""
    model = SCN_ROW_MODELS.get(entity)
    if model is None:
        raise KeyError(f"unknown scn/1.0 entity {entity!r}; known: {', '.join(SCN_ENTITIES)}")
    return model.model_validate(row)


# ─────────────────────────── snapshot tools (Tier B/C) ──────────────────────


class AsOfRange(BaseModel):
    """When a crawled snapshot's data was read: the first and the last page (wire keys ``from`` /
    ``to``; Python attributes ``from_`` / ``to``). ``to`` is never before ``from``."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_alias=True, validate_by_name=True, serialize_by_alias=True
    )

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> AsOfRange:
        if self.to < self.from_:
            raise ValueError("as_of_range.to is before as_of_range.from")
        return self


class SnapshotLabel(BaseModel):
    """How far a snapshot can be trusted (the engine's ``scn_snapshot`` JSON carries these two
    keys): ``crawled`` snapshots name their ``as_of_range``; ``consistent`` ones have none (their
    point in time is ``as_of``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    consistency: SnapshotConsistency
    as_of_range: AsOfRange | None

    @model_validator(mode="after")
    def _range_iff_crawled(self) -> SnapshotLabel:
        if (self.consistency == "crawled") != (self.as_of_range is not None):
            raise ValueError("as_of_range is set exactly for crawled snapshots")
        return self

    @property
    def advisory(self) -> bool:
        """Plans built on this snapshot are advisory."""
        return self.consistency == "crawled"


class _ToolResult(BaseModel):
    """Vendor tool results: unknown keys are ignored (vendors may add fields)."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class SnapshotEntityManifest(_ToolResult):
    rows: NonNegInt
    parts: NonNegInt
    sha256: list[Sha256Hex] = Field(default_factory=list)
    """Per-part sha256 (part order)."""

    @model_validator(mode="after")
    def _one_hash_per_part(self) -> SnapshotEntityManifest:
        if self.sha256 and len(self.sha256) != self.parts:
            raise ValueError("sha256 must list one hash per part")
        return self


class SnapshotManifest(_ToolResult):
    """``scn.snapshot_open`` result.

    M8 (``122-m8-adapter-sidecar.md`` §8), additive: a source that cannot read one point in time
    (an adapter sidecar crawling a REST API) says ``consistency: crawled`` with the
    ``as_of_range`` it read; Keelson then labels the snapshot like a tier-A crawl (advisory).
    Both default to a consistent export without a range, so Merxa manifests parse unchanged."""

    snapshot_id: Code
    as_of: AwareDatetime
    watermark: Code
    canonical_model: Literal["scn/1.0"]
    expires_at: AwareDatetime
    entities: dict[str, SnapshotEntityManifest]
    consistency: SnapshotConsistency = "consistent"
    as_of_range: AsOfRange | None = None

    @model_validator(mode="after")
    def _known_entities(self) -> SnapshotManifest:
        unknown = sorted(set(self.entities) - set(SCN_ENTITIES))
        if unknown:
            raise ValueError(f"unknown scn/1.0 entities in manifest: {unknown}")
        if (self.consistency == "crawled") != (self.as_of_range is not None):
            raise ValueError("as_of_range is set exactly for crawled snapshots")
        return self

    @property
    def label(self) -> SnapshotLabel:
        return SnapshotLabel(consistency=self.consistency, as_of_range=self.as_of_range)


_URI_FORBIDDEN_CHARS: Final = frozenset("\\ \t\r\n\x00")


def _snapshot_uri(uri: str) -> str:
    """Shape check of a part URL before anything downloads it: absolute ``http(s)`` with a
    host, no userinfo (``http://merxa.localhost@evil/`` tricks), no fragment, no
    backslashes / whitespace / control characters. The *host* allow-list is the
    consumer's job (:func:`part_uri_allowed`)."""
    if any(c in _URI_FORBIDDEN_CHARS or ord(c) < 0x20 or ord(c) == 0x7F for c in uri):
        raise ValueError("snapshot part uri contains whitespace, backslash or control characters")
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https"):
        raise ValueError("snapshot part uri must be http(s)")
    if not parts.hostname:
        raise ValueError("snapshot part uri needs a host")
    if "@" in parts.netloc:
        raise ValueError("snapshot part uri must not carry userinfo")
    if parts.fragment or uri.endswith("#"):
        raise ValueError("snapshot part uri must not carry a fragment")
    try:
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise ValueError("snapshot part uri has an invalid port") from None
    return uri


def _origin(uri: str) -> tuple[str, str, int]:
    parts = urlsplit(uri)
    host = (parts.hostname or "").rstrip(".").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, host, port


def part_uri_allowed(uri: str, allowed_origins: Iterable[str]) -> bool:
    """Whether a snapshot part URL may be fetched: its scheme, host (case-insensitive,
    trailing dot ignored) and effective port must equal one of ``allowed_origins``
    (e.g. ``["http://merxa.localhost"]`` = the connector host; ``80-m3`` §1 step 2).
    Malformed URLs are never allowed."""
    try:
        _snapshot_uri(uri)
        origin = _origin(uri)
        return any(_origin(_snapshot_uri(a)) == origin for a in allowed_origins)
    except ValueError:
        return False


class SnapshotPart(_ToolResult):
    uri: Annotated[str, AfterValidator(_snapshot_uri)]
    """Signed download URL (``http(s)`` only; check the host with :func:`part_uri_allowed`)."""
    sha256: Sha256Hex
    rows: NonNegInt
    bytes: NonNegInt


class SnapshotExport(_ToolResult):
    """``scn.snapshot_export`` result (parts in order). ``snapshot_id`` / ``entity`` echo the
    request (Merxa sends them) so a consumer can check it got the export it asked for."""

    snapshot_id: str | None = None
    entity: str | None = None
    parts: list[SnapshotPart]

    @property
    def rows(self) -> int:
        return sum(p.rows for p in self.parts)


class ChangeEntry(_ToolResult):
    entity: Code
    op: Literal["upsert", "delete"]
    key: Code
    version: Version | None = None
    at: AwareDatetime | None = None


class ChangesSince(_ToolResult):
    """``scn.changes_since`` result (any change -> full resync in M3).

    ``full_resync_required`` is set by the source when its data changed without per-row
    change entries (Merxa: a scenario reset), so ``changes`` can be empty while the
    snapshot is stale — use :attr:`requires_sync`, not ``bool(changes)``."""

    changes: list[ChangeEntry]
    next_watermark: Code
    has_more: StrictBool = False
    full_resync_required: StrictBool = False

    @property
    def requires_sync(self) -> bool:
        return self.full_resync_required or bool(self.changes)
