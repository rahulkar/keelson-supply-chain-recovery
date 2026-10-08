"""Pydantic models for MCP tool inputs/outputs (their JSON Schemas are advertised to clients)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EntityType = Literal["sku", "style", "po", "store", "dc", "supplier", "factory"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ───────────── inputs ─────────────
class OriginIn(BaseModel):
    """Where a change set comes from (free-form; integers/strings only)."""

    model_config = ConfigDict(extra="allow")
    system: str | None = Field(None, description="calling system, e.g. 'Keelson'")
    case_ref: str | None = Field(None, description="caller's case reference")
    plan_version: int | str | None = Field(None, description="caller's plan version")


class ChangeSetItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str = Field(
        description=(
            "transfer_order.create | allocation.set_qty | po_line.reschedule | po_line.change_qty | po_line.cancel | "
            "po.create | po_line.split | capacity_booking.create | capacity_booking.cancel | "
            "production_order.resequence | promotion.reschedule | online_order.repromise"
        )
    )
    target_ref: str | None = Field(
        None,
        description=(
            "null for *.create ops; allocation / promotion / online order / capacity booking code; factory "
            "line code for production_order.resequence; or '<po_code>#<line_no>'"
        ),
    )
    expected_version: int | None = Field(None, ge=1, description="record version (ETag) the change was planned against")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="op payload: integers, strings and ISO dates only"
    )


# ───────────── outputs ─────────────
class SearchItem(_Out):
    type: EntityType
    code: str
    label: str


class SearchOut(_Out):
    items: list[SearchItem]


class EntityOut(_Out):
    type: EntityType
    code: str
    data: dict[str, Any]
    version: int


class DcOut(_Out):
    code: str
    name: str
    country: str
    lat: float
    lon: float


class ClusterOut(_Out):
    code: str
    dc_code: str
    climate: str
    grade_band: str
    store_count: int


class LocationsOut(_Out):
    dcs: list[DcOut]
    clusters: list[ClusterOut]


class StoreOut(_Out):
    code: str
    name: str
    market: str
    city: str
    lat: float
    lon: float
    grade: str
    cluster: str
    home_dc: str


class StoresOut(_Out):
    items: list[StoreOut]
    next_cursor: str | None


class PoSummary(_Out):
    code: str
    supplier_code: str
    factory_code: str
    dest_dc: str
    status: str
    ex_factory_date: str
    eta_dc: str
    line_count: int
    total_qty: int
    version: int


class PoListOut(_Out):
    items: list[PoSummary]
    next_cursor: str | None


class PoLine(_Out):
    line_no: int
    sku: str
    qty_ordered: int
    qty_received: int
    eta_dc: str
    status: str
    version: int


class PoOut(PoSummary):
    lines: list[PoLine]


class BomOut(_Out):
    components: list[dict[str, Any]]
    styles: list[dict[str, Any]]
    factories: list[dict[str, Any]]
    open_po_lines: list[dict[str, Any]]


class InventoryRow(_Out):
    location_type: Literal["dc", "store", "cluster", "dc_stores"]
    location_code: str
    sku: str
    on_hand: int
    allocated: int
    reserved: int
    in_transit: int
    atp: int
    version: int | None = Field(None, description="record version (raw rows only)")
    store_count: int | None = Field(None, description="stores summed (aggregated rows only)")


class InventoryOut(_Out):
    items: list[InventoryRow]
    next_cursor: str | None


class ForecastRow(_Out):
    style_code: str
    cluster_code: str
    store_code: str | None = None
    iso_week: str
    units: int


class SizeCurveRow(_Out):
    cluster_code: str
    size: str
    share_bp: int


class ForecastOut(_Out):
    aggregate: Literal["store", "cluster"] = "cluster"
    items: list[ForecastRow]
    size_curves: list[SizeCurveRow]
    next_cursor: str | None = None


class AllocationRow(_Out):
    code: str
    sku: str
    dc_code: str
    store_code: str
    ship_week: str
    qty: int
    status: str
    version: int


class AllocationsOut(_Out):
    items: list[AllocationRow]
    next_cursor: str | None


class TransferOrdersOut(_Out):
    items: list[dict[str, Any]]


class SupplierMessage(_Out):
    id: str
    supplier_code: str
    received_at: str
    subject: str
    body: str


class SupplierMessagesOut(_Out):
    items: list[SupplierMessage]


class ChangeSetOut(_Out):
    change_set_id: str
    code: str
    title: str
    status: str
    items_count: int
    preview_hash: str | None = None
    items: list[dict[str, Any]]


class AuditOut(_Out):
    items: list[dict[str, Any]]


class GuideExpected(_Out):
    """What a read-back finds once the step is done (Guide-mode auto-tick)."""

    entity_type: str
    entity_ref: str | None
    match: dict[str, Any]


class GuideStep(_Out):
    seq: int
    item_seq: int
    op: str | None = None
    target_ref: str | None = None
    title: str
    instructions: str
    deep_link: str
    expected: GuideExpected | None = None


class GuideOut(_Out):
    steps: list[GuideStep]


class CreateOut(_Out):
    change_set_id: str
    code: str
    status: Literal["draft"]


class AddItemsOut(_Out):
    change_set_id: str
    items_count: int


class ValidatedItem(_Out):
    seq: int
    op: str
    target_ref: str | None
    before: Any = None
    after: Any = None
    ok: bool
    errors: list[str]


class Totals(_Out):
    lines: int
    units_moved: int
    value_cents: int


class ValidateOut(_Out):
    status: Literal["validated", "invalid"]
    preview_hash: str
    items: list[ValidatedItem]
    totals: Totals


class ConflictRow(_Out):
    seq: int
    target_ref: str | None
    expected_version: int | None
    actual_version: int | None
    reason: str


class ApplyOut(_Out):
    status: Literal["applied", "conflict"]
    applied: int | None = None
    audit_ref: str | None = None
    change_set_code: str | None = None
    conflicts: list[ConflictRow] | None = None


class RollbackOut(_Out):
    status: Literal["rolled_back", "conflict"]
    change_set_id: str | None = None
    change_set_code: str | None = None
    compensating_change_set_id: str | None = None
    compensating_code: str | None = None
    applied: int | None = None
    audit_ref: str | None = None
    conflicts: list[ConflictRow] | None = None


# ───────────── M2 read tools ─────────────
class Sailing(_Out):
    code: str
    lane_code: str
    vessel: str
    cutoff: str
    etd: str
    eta: str
    capacity_teu: int
    status: str
    version: int


class SailingsOut(_Out):
    items: list[Sailing]
    next_cursor: str | None = None


class ShipmentsOut(_Out):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class LaneOption(_Out):
    lane_code: str
    mode: str
    transit_days: int
    via: list[str]
    rate_cents: int | None
    basis: str | None
    remaining_capacity_week: int | None
    alt_lane_code: str | None


class LaneOptionsOut(_Out):
    origin: str
    dest: str
    week: str
    options: list[LaneOption]


class FactoryScheduleOut(_Out):
    factory_code: str
    lines: list[dict[str, Any]]


class OnlineOrdersOut(_Out):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class PromotionsOut(_Out):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class EventsOut(_Out):
    items: list[dict[str, Any]]
    next_cursor: str
    has_more: bool


class SnapshotOpenOut(_Out):
    snapshot_id: str
    as_of: str
    watermark: str
    canonical_model: Literal["scn/1.0"]
    expires_at: str
    entities: dict[str, dict[str, Any]]


class SnapshotPart(_Out):
    uri: str
    sha256: str
    rows: int
    bytes: int


class SnapshotExportOut(_Out):
    snapshot_id: str
    entity: str
    parts: list[SnapshotPart]


class ChangesOut(_Out):
    changes: list[dict[str, Any]]
    next_watermark: str
