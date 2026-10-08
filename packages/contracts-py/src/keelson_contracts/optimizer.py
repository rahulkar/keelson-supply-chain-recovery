"""Optimizer service API models (contracts: ``docs/contracts/40-optimizer.md`` and
``80-m3-deterministic-core.md`` §2-§3).

Shared by the engine (client) and the optimizer service (server). Conventions from
``00-overview.md``: every quantity is an integer number of eaches, money is integer
EUR cents (``*_cents``), ratios are integer basis points (``*_bp``, 10000 = x1.0),
dates are ISO ``YYYY-MM-DD``, time buckets are ISO weeks ``YYYY-Www`` and map
coordinates are integer degrees x 1e6 (``lat_e6`` / ``lon_e6``).
Integer fields are *strict*: floats, booleans and numeric strings are rejected so that
no float can ever leak into a hashed payload.

M3 additions are all optional and backward compatible: ``ProblemData`` v2 lists
(``locations``, ``po_lines``, ``bom``, ``inbound_lanes``, ``alt_sources``,
``production``, ``allocations``), ``ImpactRequest.include_graph`` with
``ImpactResponse.graph`` / ``tts`` / ``bottlenecks``, the five v1 levers (discriminated
on ``code``, catalogue in :data:`LEVERS`), ``OptimizeResponse.scorecard``, typed
decisions per lever, and :class:`Action` for every v1 op with an atomic ``group``.

Perturbations: the generic scenario union lives in ``keelson_contracts.scenario``.
When that module is importable, :data:`Perturbation` is re-exported from it; otherwise
the M1 subset defined here is used. The local models (:class:`LeadTimeShift`,
:class:`DemandScale`, :class:`CapacityScale`) are always available and are
structurally identical to the shapes in ``40-optimizer.md``; any other ``op`` is
accepted as :class:`OtherPerturbation` and ignored (with a warning) by the optimizer.
Target kinds the optimizer resolves are listed in :data:`TARGET_KINDS_BY_OP`.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Strict,
    StrictBool,
    StringConstraints,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)

from . import changeset as _cs
from .common import IsoDate as _IsoDate

__all__ = [
    "DC",
    "LEVERS",
    "LEVER_CODES",
    "LEVER_OPS",
    "MAX_GRAPH_NODES",
    "PERTURBATION_SOURCE",
    "SCORECARD_METRICS",
    "TARGET_KINDS",
    "TARGET_KINDS_BY_OP",
    "Action",
    "ActionOp",
    "ActionPayload",
    "AllocationDecision",
    "AltSource",
    "AltSupplierDecision",
    "Allocation",
    "BindingConstraint",
    "BomLine",
    "Bottleneck",
    "CapacityScale",
    "Cluster",
    "ClusterInventory",
    "CostBreakdown",
    "DcRisk",
    "Decision",
    "DemandRow",
    "DemandScale",
    "EtaChange",
    "ExpediteDecision",
    "ExpediteModeShiftLever",
    "ExpediteModeShiftParams",
    "Explain",
    "FirstBreach",
    "GraphEdge",
    "GraphEdgeKind",
    "GraphNode",
    "GraphNodeKind",
    "Horizon",
    "ImpactGraph",
    "ImpactKpis",
    "ImpactRequest",
    "ImpactResponse",
    "InboundLane",
    "InboundMode",
    "InventoryPosition",
    "KpiDelta",
    "Lane",
    "LatE6",
    "LeadTimeShift",
    "LeverCode",
    "LeverDecision",
    "LeverDef",
    "LeverInfo",
    "LeverSpec",
    "Location",
    "LocationKind",
    "LonE6",
    "M1Perturbation",
    "NodeStatus",
    "OptimizeRequest",
    "OptimizeResponse",
    "OptimizeStatus",
    "OtherPerturbation",
    "Perturbation",
    "PerturbationTarget",
    "PlanSpec",
    "PlanWeights",
    "PoLine",
    "PrioritizeAllocationLever",
    "PrioritizeAllocationParams",
    "ProblemData",
    "ProductionLine",
    "ProductionOrder",
    "ProtectedMin",
    "Prov",
    "Provenance",
    "ReallocateDecision",
    "ReallocateInventoryLever",
    "ReallocateInventoryParams",
    "Receipt",
    "ResequenceDecision",
    "ResequenceProductionLever",
    "ResequenceProductionParams",
    "ResourceAltSupplierLever",
    "ResourceAltSupplierParams",
    "Scorecard",
    "ScorecardMetric",
    "Sku",
    "SolverInfo",
    "SolverOptions",
    "TargetKind",
    "TimelinePoint",
    "TransferOrderCreatePayload",
    "TransferOrderLine",
    "TtsRow",
    "TtsVerdict",
    "lane_code",
    "lever_catalogue",
    "lever_params_schema",
]

# ───────────────────────────── scalar aliases ─────────────────────────────

Int = Annotated[int, Strict()]
"""Any integer (may be negative, e.g. deltas, shadow prices, day shifts)."""
NonNegInt = Annotated[int, Strict(), Field(ge=0)]
PosInt = Annotated[int, Strict(), Field(ge=1)]
Bp = Annotated[int, Strict(), Field(ge=0, le=1_000_000)]
"""Basis points; 10000 = x1.0. Upper bound x100 guards against unit mistakes."""
ShareBp = Annotated[int, Strict(), Field(ge=0, le=10_000)]
"""A share in basis points (0..10000 = 0..100 %)."""

Code = Annotated[str, StringConstraints(min_length=1, max_length=128)]

LatE6 = Annotated[int, Strict(), Field(ge=-90_000_000, le=90_000_000)]
"""Latitude in integer degrees x 1e6 (``51.5072`` -> ``51507200``)."""
LonE6 = Annotated[int, Strict(), Field(ge=-180_000_000, le=180_000_000)]
"""Longitude in integer degrees x 1e6."""

PoLineRef = Annotated[str, StringConstraints(pattern=_cs.PO_LINE_REF_PATTERN)]
"""``<po_code>#<line_no>``, e.g. ``PO-26-0101#1``."""


def _existing_iso_week(label: str) -> str:
    """Reject syntactically valid labels of weeks that do not exist (``W53`` of a 52-week
    ISO year, e.g. ``2025-W53``)."""
    year, week = label.split("-W")
    try:
        _dt.date.fromisocalendar(int(year), int(week), 1)
    except ValueError:
        raise ValueError(f"{label} is not an ISO week of year {year}") from None
    return label


IsoWeek = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$"),
    AfterValidator(_existing_iso_week),
]
LaneCode = Annotated[str, StringConstraints(pattern=r"^[^>\s]+>[^>\s]+$")]
"""DC-to-DC lane identifier ``<from_dc>><to_dc>``, e.g. ``DE1>NL1``."""

Prov = Literal["fact", "inference", "assumption", "simulated", "decision", "committed"]


def lane_code(from_dc: str, to_dc: str) -> str:
    """Canonical lane identifier used in perturbation targets and lever params."""
    return f"{from_dc}>{to_dc}"


def _dupes(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for v in values:
        if v in seen and v not in out:
            out.append(v)
        seen.add(v)
    return out


def _is_empty(value: Any) -> bool:
    return not value


def _is_none(value: Any) -> bool:
    return value is None


def _m3_list() -> Any:
    """An additive (M3) list field: defaults to ``[]`` and is omitted from dumps while empty,
    so M1 payloads (and their ``inputs_hash``) serialize exactly as before."""
    return Field(default_factory=list, exclude_if=_is_empty)


def _m3_optional() -> Any:
    """An additive (M3) optional field: defaults to ``None`` and is omitted from dumps while
    ``None``."""
    return Field(default=None, exclude_if=_is_none)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ───────────────────────────── ProblemData ─────────────────────────────


class Horizon(_Model):
    start_week: IsoWeek
    weeks: Annotated[int, Strict(), Field(ge=1, le=53)]


class DC(_Model):
    code: Code
    outbound_cap_units_wk: NonNegInt | None = None


class Cluster(_Model):
    code: Code
    dc_code: Code


class Sku(_Model):
    code: Code
    style_code: Code
    retail_price_cents: NonNegInt
    unit_cost_cents: NonNegInt


class InventoryPosition(_Model):
    """DC (or store) stock position. ``reserved`` is an optional addition mirroring
    Merxa's ``inventory_position.reserved`` (ATP = on_hand - allocated - reserved)."""

    location_type: Literal["dc", "store"]
    location_code: Code
    sku: Code
    on_hand: NonNegInt
    allocated: NonNegInt = 0
    reserved: NonNegInt = 0
    in_transit: NonNegInt = 0


class ClusterInventory(_Model):
    cluster_code: Code
    sku: Code
    on_hand: NonNegInt


class Receipt(_Model):
    """An expected inbound receipt (e.g. a PO line), ``ref`` like ``PO-26-0101#1``."""

    ref: Code
    dc_code: Code
    sku: Code
    qty: NonNegInt
    eta: _dt.date


class DemandRow(_Model):
    cluster_code: Code
    sku: Code
    iso_week: IsoWeek
    units: NonNegInt


class Lane(_Model):
    from_dc: Code
    to_dc: Code
    transit_days: NonNegInt
    cost_per_unit_cents: NonNegInt
    max_units_wk: NonNegInt

    @property
    def code(self) -> str:
        return lane_code(self.from_dc, self.to_dc)


class ProtectedMin(_Model):
    cluster_code: Code
    sku: Code
    units: NonNegInt


# ── ProblemData v2 (M3, all optional) ──

LocationKind = Literal["dc", "cluster", "store", "factory", "supplier", "port"]
InboundMode = Literal["sea", "sea_air", "air", "road"]


class Location(_Model):
    """A mapped location (map only; not used by the solvers)."""

    code: Code
    kind: LocationKind
    name: str
    lat_e6: LatE6
    lon_e6: LonE6
    parent: Code | None = None
    """Store -> cluster, cluster -> dc, factory -> supplier."""


class PoLine(_Model):
    """Provenance of a receipt for the levers (``receipts`` stays the canonical arrival
    list). ``qty`` is the open quantity; ``lane_code`` (an :class:`InboundLane` code) is
    ``None`` while the line has no booked lane."""

    ref: PoLineRef
    po_code: Code
    supplier_code: Code
    factory_code: Code
    dest_dc: Code
    sku: Code
    qty: NonNegInt
    eta: _IsoDate
    ex_factory: _IsoDate
    mode: InboundMode
    lane_code: Code | None = None

    @model_validator(mode="after")
    def _ref_matches_po(self) -> PoLine:
        if self.ref.rsplit("#", 1)[0] != self.po_code:
            raise ValueError(f"po_line {self.ref}: ref does not belong to po_code {self.po_code}")
        return self


class BomLine(_Model):
    """Style -> component usage; ``factory_code=None`` = every factory."""

    style_code: Code
    component_code: Code
    supplier_code: Code
    factory_code: Code | None = None


class InboundLane(_Model):
    """Origin (port/factory) -> DC lane. ``cost_per_unit_cents`` is derived from the rate
    card; ``via`` lists chokepoints/hubs (e.g. ``SUEZ``, ``CAPE``, ``AEJEA``).
    ``alt_lane_code`` (additive) names the catalogued fallback lane used when this lane
    is closed (``capacity_scale`` 0 on it or on a port/chokepoint it passes)."""

    code: Code
    origin: Code
    dest_dc: Code
    mode: InboundMode
    transit_days: NonNegInt
    cost_per_unit_cents: NonNegInt
    weekly_capacity_units: NonNegInt
    via: list[Code] = Field(default_factory=list)
    alt_lane_code: Code | None = None


class AltSource(_Model):
    """An alternate (e.g. near-shore) source for ``resource_alt_supplier``; only
    ``approved and compliance_ok`` sources may be used. Its lane fixes the destination DC."""

    supplier_code: Code
    factory_code: Code
    style_codes: Annotated[list[Code], Field(min_length=1)]
    lead_time_days: NonNegInt
    unit_cost_cents: NonNegInt
    moq_units: NonNegInt
    weekly_capacity_units: NonNegInt
    approved: StrictBool
    compliance_ok: StrictBool
    lane_code: Code

    @property
    def usable(self) -> bool:
        return self.approved and self.compliance_ok


class ProductionOrder(_Model):
    """A production order on a factory line. ``movable`` (additive, default ``True``)
    is ``False`` for orders the target system will not move (Merxa: running/done)."""

    code: Code
    po_line_ref: PoLineRef
    sku: Code
    qty: NonNegInt
    planned_start: _IsoDate
    planned_end: _IsoDate
    seq: NonNegInt
    movable: StrictBool = True

    @model_validator(mode="after")
    def _dates_ordered(self) -> ProductionOrder:
        if self.planned_end < self.planned_start:
            raise ValueError(f"production order {self.code}: planned_end before planned_start")
        return self


class ProductionLine(_Model):
    line_code: Code
    factory_code: Code
    daily_capacity_units: NonNegInt
    orders: list[ProductionOrder] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_orders(self) -> ProductionLine:
        if d := _dupes([o.code for o in self.orders]):
            raise ValueError(f"production line {self.line_code}: duplicate order codes {d}")
        if d := _dupes([o.seq for o in self.orders]):
            raise ValueError(f"production line {self.line_code}: duplicate seq {d}")
        return self


class Allocation(_Model):
    """A cluster-aggregated allocation that keeps the target system's allocation ``code``
    and ``version`` (the ``expected_version`` of ``allocation.set_qty``)."""

    code: Code
    sku: Code
    dc_code: Code
    cluster_code: Code
    ship_week: IsoWeek
    qty: NonNegInt
    protected: StrictBool = False
    version: PosInt


class ProblemData(_Model):
    """Shared optimizer input. Referential integrity is validated (unknown DC, cluster,
    SKU or inbound-lane references are rejected); duplicate rows for the same key are
    summed by the optimizer, duplicate master-data codes are rejected."""

    tenant: Code
    as_of: _dt.date
    horizon: Horizon
    dcs: list[DC]
    clusters: list[Cluster]
    skus: list[Sku]
    inventory: list[InventoryPosition] = Field(default_factory=list)
    cluster_inventory: list[ClusterInventory] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    demand: list[DemandRow] = Field(default_factory=list)
    lanes: list[Lane] = Field(default_factory=list)
    protected_min: list[ProtectedMin] = Field(default_factory=list)
    # v2 (M3): omitted from dumps while empty (M1 inputs hash unchanged)
    locations: list[Location] = _m3_list()
    po_lines: list[PoLine] = _m3_list()
    bom: list[BomLine] = _m3_list()
    inbound_lanes: list[InboundLane] = _m3_list()
    alt_sources: list[AltSource] = _m3_list()
    production: list[ProductionLine] = _m3_list()
    allocations: list[Allocation] = _m3_list()

    @model_validator(mode="after")
    def _check_references(self) -> ProblemData:
        def unique(codes: list[Any], what: str) -> set[Any]:
            if dupes := _dupes(codes):
                raise ValueError(f"duplicate {what} codes: {sorted(dupes)}")
            return set(codes)

        dcs = unique([d.code for d in self.dcs], "dc")
        clusters = unique([c.code for c in self.clusters], "cluster")
        skus = unique([s.code for s in self.skus], "sku")
        unique([ln.code for ln in self.lanes], "lane")
        unique([(loc.kind, loc.code) for loc in self.locations], "location (kind, code)")
        unique([pl.ref for pl in self.po_lines], "po_line")
        inbound = unique([il.code for il in self.inbound_lanes], "inbound_lane")
        # one row per (source, lane): a duplicate would double the source's capacity / MOQ
        unique(
            [(s.supplier_code, s.factory_code, s.lane_code) for s in self.alt_sources],
            "alt_source (supplier_code, factory_code, lane_code)",
        )
        unique([pl.line_code for pl in self.production], "production line")
        unique([o.code for pl in self.production for o in pl.orders], "production order")
        unique([a.code for a in self.allocations], "allocation")
        errors: list[str] = []
        for c in self.clusters:
            if c.dc_code not in dcs:
                errors.append(f"cluster {c.code}: unknown dc_code {c.dc_code}")
        for inv in self.inventory:
            if inv.sku not in skus:
                errors.append(f"inventory: unknown sku {inv.sku}")
            if inv.location_type == "dc" and inv.location_code not in dcs:
                errors.append(f"inventory: unknown dc {inv.location_code}")
        for ci in self.cluster_inventory:
            if ci.cluster_code not in clusters:
                errors.append(f"cluster_inventory: unknown cluster {ci.cluster_code}")
            if ci.sku not in skus:
                errors.append(f"cluster_inventory: unknown sku {ci.sku}")
        for r in self.receipts:
            if r.dc_code not in dcs:
                errors.append(f"receipt {r.ref}: unknown dc {r.dc_code}")
            if r.sku not in skus:
                errors.append(f"receipt {r.ref}: unknown sku {r.sku}")
        for dm in self.demand:
            if dm.cluster_code not in clusters:
                errors.append(f"demand: unknown cluster {dm.cluster_code}")
            if dm.sku not in skus:
                errors.append(f"demand: unknown sku {dm.sku}")
        for ln in self.lanes:
            if ln.from_dc not in dcs or ln.to_dc not in dcs:
                errors.append(f"lane {ln.code}: unknown dc")
            if ln.from_dc == ln.to_dc:
                errors.append(f"lane {ln.code}: from_dc == to_dc")
        for pm in self.protected_min:
            if pm.cluster_code not in clusters:
                errors.append(f"protected_min: unknown cluster {pm.cluster_code}")
            if pm.sku not in skus:
                errors.append(f"protected_min: unknown sku {pm.sku}")
        for pol in self.po_lines:
            if pol.dest_dc not in dcs:
                errors.append(f"po_line {pol.ref}: unknown dest_dc {pol.dest_dc}")
            if pol.sku not in skus:
                errors.append(f"po_line {pol.ref}: unknown sku {pol.sku}")
            if pol.lane_code is not None and pol.lane_code not in inbound:
                errors.append(f"po_line {pol.ref}: unknown inbound lane {pol.lane_code}")
        for il in self.inbound_lanes:
            if il.dest_dc not in dcs:
                errors.append(f"inbound_lane {il.code}: unknown dest_dc {il.dest_dc}")
            if il.alt_lane_code is not None and il.alt_lane_code not in inbound:
                errors.append(f"inbound_lane {il.code}: unknown alt_lane_code {il.alt_lane_code}")
            if il.alt_lane_code == il.code:
                errors.append(f"inbound_lane {il.code}: alt_lane_code is the lane itself")
        for src in self.alt_sources:
            if src.lane_code not in inbound:
                errors.append(f"alt_source {src.factory_code}: unknown inbound lane {src.lane_code}")
        for line in self.production:
            for o in line.orders:
                if o.sku not in skus:
                    errors.append(f"production order {o.code}: unknown sku {o.sku}")
        for al in self.allocations:
            if al.sku not in skus:
                errors.append(f"allocation {al.code}: unknown sku {al.sku}")
            if al.dc_code not in dcs:
                errors.append(f"allocation {al.code}: unknown dc {al.dc_code}")
            if al.cluster_code not in clusters:
                errors.append(f"allocation {al.code}: unknown cluster {al.cluster_code}")
        if errors:
            # Bounded message: enough to debug, never a multi-MB error body.
            shown = errors[:20]
            more = f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""
            raise ValueError("; ".join(shown) + more)
        return self


# ───────────────────────────── Perturbations ─────────────────────────────

TargetKind = Literal[
    "supplier",
    "component",
    "factory",
    "port",
    "chokepoint",
    "lane",
    "po",
    "po_line",
    "receipt",
    "cluster",
    "dc",
    "sku",
    "style",
]
"""Target kinds the optimizer resolves (M1 kinds plus the M3 network kinds)."""

TARGET_KINDS: Final[frozenset[str]] = frozenset(TargetKind.__args__)  # type: ignore[attr-defined]

TARGET_KINDS_BY_OP: Final[Mapping[str, frozenset[str]]] = {
    # supplier/component -> po_lines whose style's BOM uses it (at that factory);
    # factory -> its po_lines; receipt/po_line/po/dc/sku/style as in M1.
    "lead_time_shift": frozenset(
        {"receipt", "po_line", "po", "dc", "sku", "style", "supplier", "component", "factory"}
    ),
    "demand_scale": frozenset({"cluster", "dc", "sku", "style"}),
    # port/chokepoint -> inbound lanes whose origin/dest/via includes it; lane = DC lane
    # (``FROM>TO``) or inbound lane code. 0 closes the lane (receipts move to its
    # alt_lane_code, else they are lost).
    "capacity_scale": frozenset({"lane", "port", "chokepoint"}),
}
"""Deterministic resolution rules per perturbation op (``80`` §2). Other kinds are
ignored with a warning."""


class PerturbationTarget(_Model):
    """Entity selector. Kinds understood by the optimizer per op are listed in
    :data:`TARGET_KINDS_BY_OP`; other kinds are ignored with a warning."""

    kinds: list[Code]
    ids: list[Code]


class LeadTimeShift(_Model):
    op: Literal["lead_time_shift"]
    target: PerturbationTarget
    days: Int
    prov: Prov


class DemandScale(_Model):
    op: Literal["demand_scale"]
    target: PerturbationTarget
    factor_bp: Bp
    weeks: list[IsoWeek] | None = None
    """``None`` (or omitted) = every week of the horizon."""
    prov: Prov


class CapacityScale(_Model):
    op: Literal["capacity_scale"]
    target: PerturbationTarget
    factor_bp: Bp
    prov: Prov


class OtherPerturbation(BaseModel):
    """Any perturbation op outside the M1 subset; accepted and ignored with a warning."""

    model_config = ConfigDict(extra="allow", frozen=True)
    op: Code
    prov: Prov | None = None


_M1_OPS = frozenset({"lead_time_shift", "demand_scale", "capacity_scale"})


def _perturbation_tag(value: Any) -> str:
    op = value.get("op") if isinstance(value, dict) else getattr(value, "op", None)
    return op if op in _M1_OPS else "other"


M1Perturbation = Annotated[
    Annotated[LeadTimeShift, Tag("lead_time_shift")]
    | Annotated[DemandScale, Tag("demand_scale")]
    | Annotated[CapacityScale, Tag("capacity_scale")]
    | Annotated[OtherPerturbation, Tag("other")],
    Discriminator(_perturbation_tag),
]
"""Local M1 perturbation union (fallback when ``keelson_contracts.scenario`` is absent)."""

try:  # Prefer the generic union owned by the contracts builder when it exists.
    from keelson_contracts.scenario import Perturbation as Perturbation  # type: ignore[attr-defined,unused-ignore]

    PERTURBATION_SOURCE = "keelson_contracts.scenario"
except ImportError:  # module (or name) not present yet: M1 local subset
    Perturbation = M1Perturbation  # type: ignore[assignment,misc,unused-ignore]
    PERTURBATION_SOURCE = "keelson_contracts.optimizer"


# ───────────────────────────── KPIs ─────────────────────────────


class FirstBreach(_Model):
    dc_code: Code
    cluster_code: Code
    style_code: Code
    week: IsoWeek
    date: _dt.date
    """Monday of ``week``."""


class ImpactKpis(_Model):
    units_at_risk: NonNegInt
    revenue_at_risk_cents: NonNegInt
    margin_at_risk_cents: Int
    shortage_cluster_weeks: NonNegInt
    first_breach: list[FirstBreach]


class DcRisk(_Model):
    dc_code: Code
    units_at_risk: NonNegInt
    revenue_at_risk_cents: NonNegInt


class TimelinePoint(_Model):
    cluster_code: Code
    style_code: Code
    week: IsoWeek
    projected_available: Int
    demand: NonNegInt
    shortage: NonNegInt


class Provenance(_Model):
    engine: Literal["keelson-opt"] = "keelson-opt"
    version: str
    inputs_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    computed_at: _dt.datetime


# ───────────────────────────── impact graph / TTS (M3) ─────────────────────────────

MAX_GRAPH_NODES: Final = 300
"""Graph size cap: the optimizer groups stores to clusters, po_lines to POs and SKUs to
styles to stay within it."""

NodeStatus = Literal["disrupted", "impacted", "at_risk", "ok"]
GraphNodeKind = Literal[
    "supplier",
    "component",
    "factory",
    "port",
    "chokepoint",
    "lane",
    "po",
    "po_line",
    "dc",
    "cluster",
    "store",
    "style",
    "sku",
]
GraphEdgeKind = Literal["supplies", "produces", "ships", "stocks", "serves"]


class GraphNode(_Model):
    id: Code
    kind: GraphNodeKind
    label: str
    status: NodeStatus
    units_at_risk: NonNegInt
    first_breach_week: IsoWeek | None = None


class GraphEdge(_Model):
    source: Code
    target: Code
    kind: GraphEdgeKind


class ImpactGraph(_Model):
    """Impact graph (``/v1/impact`` with ``include_graph``; also the ``impact_graph@1``
    block data). At most :data:`MAX_GRAPH_NODES` nodes; edges reference node ids."""

    nodes: Annotated[list[GraphNode], Field(max_length=MAX_GRAPH_NODES)]
    edges: list[GraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ImpactGraph:
        ids = [n.id for n in self.nodes]
        if d := _dupes(ids):
            raise ValueError(f"duplicate graph node ids: {d[:10]}")
        known = set(ids)
        bad = [f"{e.source}->{e.target}" for e in self.edges if e.source not in known or e.target not in known]
        if bad:
            raise ValueError(f"graph edges reference unknown nodes: {bad[:10]}")
        if loops := [e.source for e in self.edges if e.source == e.target]:
            raise ValueError(f"graph self-loops: {loops[:10]}")
        if d := _dupes([(e.source, e.target, e.kind) for e in self.edges]):
            raise ValueError(f"duplicate graph edges: {d[:10]}")
        return self


TtsVerdict = Literal["ok", "expedite", "loss"]


class TtsRow(_Model):
    """Time-to-survive (Simchi-Levi) per DC (optionally per cluster) vs time-to-recover:
    TTR < TTS -> ``ok``, TTR ~ TTS -> ``expedite``, TTR > TTS -> ``loss``."""

    dc_code: Code
    cluster_code: Code | None = None
    tts_days: NonNegInt
    ttr_days: NonNegInt | None = None
    verdict: TtsVerdict


# ───────────────────────────── /v1/impact ─────────────────────────────


class BindingConstraint(_Model):
    name: str
    shadow_price_cents: Int
    """Objective improvement (weighted cents) per extra unit of the constraint's
    capacity, from the LP dual value, rounded to an integer."""


Bottleneck = BindingConstraint
"""``ImpactResponse.bottlenecks`` rows (``{name, shadow_price_cents}``, TTS LP duals)."""


class ImpactRequest(_Model):
    data: ProblemData
    perturbations: list[Perturbation] = Field(default_factory=list)
    include_graph: StrictBool = Field(default=False, exclude_if=_is_empty)
    """M3: also return ``graph``, ``tts`` and ``bottlenecks`` (omitted from dumps while false)."""


class ImpactResponse(_Model):
    kpis: ImpactKpis
    by_dc: list[DcRisk]
    timeline: list[TimelinePoint]
    baseline_kpis: ImpactKpis
    provenance: Provenance
    warnings: list[str] = Field(default_factory=list)
    """Additive: ignored perturbations / unsupported target kinds etc."""
    graph: ImpactGraph | None = _m3_optional()
    """M3: present when the request set ``include_graph``."""
    tts: list[TtsRow] = _m3_list()
    bottlenecks: list[BindingConstraint] = _m3_list()


# ───────────────────────────── levers ─────────────────────────────

LeverCode = Literal[
    "reallocate_inventory",
    "expedite_mode_shift",
    "resource_alt_supplier",
    "prioritize_allocation",
    "resequence_production",
]


def _unique_list(values: list[Any] | None, what: str) -> None:
    if values is not None and (d := _dupes(values)):
        raise ValueError(f"{what} must be unique (duplicates: {d})")


class ReallocateInventoryParams(_Model):
    max_units_total: NonNegInt | None = None
    """Upper bound on the total units moved across all transfers (``None`` = unbounded)."""
    allowed_lanes: list[LaneCode] | None = None
    """Lanes (``FROM>TO``) the lever may use; ``None`` = every lane in ``ProblemData``."""


def _default_modes() -> list[_cs.SplitMode]:
    return ["sea_air"]


class ExpediteModeShiftParams(_Model):
    """Move qty of affected PO lines onto a faster inbound lane (premium cost)."""

    modes: Annotated[list[_cs.SplitMode], Field(min_length=1)] = Field(default_factory=_default_modes)
    """Target modes the lever may use."""
    allowed_lanes: list[Code] | None = None
    """Inbound lane codes the lever may book; ``None`` = every lane of an allowed mode."""
    po_line_refs: list[PoLineRef] | None = None
    """PO lines the lever may split; ``None`` = every affected PO line."""
    max_units_total: NonNegInt | None = None
    max_premium_cents: NonNegInt | None = None
    """Budget for the total expedite premium (``None`` = unbounded)."""

    @model_validator(mode="after")
    def _unique(self) -> ExpediteModeShiftParams:
        _unique_list(self.modes, "modes")
        _unique_list(self.allowed_lanes, "allowed_lanes")
        _unique_list(self.po_line_refs, "po_line_refs")
        return self


class ResourceAltSupplierParams(_Model):
    """Buy from approved, compliant alternate sources (MOQ, capacity, lead time)."""

    allowed_factories: list[Code] | None = None
    """Alternate factories the lever may use; ``None`` = every usable ``alt_sources`` row."""
    style_codes: list[Code] | None = None
    """Styles the lever may re-source; ``None`` = every affected style."""
    max_units_total: NonNegInt | None = None
    max_premium_cents: NonNegInt | None = None
    """Budget for the total near-shore unit-cost premium (``None`` = unbounded)."""

    @model_validator(mode="after")
    def _unique(self) -> ResourceAltSupplierParams:
        _unique_list(self.allowed_factories, "allowed_factories")
        _unique_list(self.style_codes, "style_codes")
        return self


class PrioritizeAllocationParams(_Model):
    """Re-split scarce DC stock across clusters by grade / protected minimums."""

    dc_codes: list[Code] | None = None
    """DCs whose allocations may change; ``None`` = all."""
    ship_weeks: list[IsoWeek] | None = None
    """Ship weeks whose allocations may change; ``None`` = the whole horizon."""
    respect_protected: StrictBool = True
    """Never cut a ``protected`` allocation below its current quantity."""
    max_changes: PosInt | None = None
    """Upper bound on ``allocation.set_qty`` actions (``None`` = unbounded)."""

    @model_validator(mode="after")
    def _unique(self) -> PrioritizeAllocationParams:
        _unique_list(self.dc_codes, "dc_codes")
        _unique_list(self.ship_weeks, "ship_weeks")
        return self


class ResequenceProductionParams(_Model):
    """Reorder movable orders within a factory line (deterministic solve)."""

    line_codes: list[Code] | None = None
    """Factory lines the lever may resequence; ``None`` = every line in ``production``."""
    max_orders_per_line: PosInt | None = None
    """Lines with more movable orders are left unchanged (``None`` = solver default)."""

    @model_validator(mode="after")
    def _unique(self) -> ResequenceProductionParams:
        _unique_list(self.line_codes, "line_codes")
        return self


class ReallocateInventoryLever(_Model):
    code: Literal["reallocate_inventory"]
    params: ReallocateInventoryParams = Field(default_factory=ReallocateInventoryParams)


class ExpediteModeShiftLever(_Model):
    code: Literal["expedite_mode_shift"]
    params: ExpediteModeShiftParams = Field(default_factory=ExpediteModeShiftParams)


class ResourceAltSupplierLever(_Model):
    code: Literal["resource_alt_supplier"]
    params: ResourceAltSupplierParams = Field(default_factory=ResourceAltSupplierParams)


class PrioritizeAllocationLever(_Model):
    code: Literal["prioritize_allocation"]
    params: PrioritizeAllocationParams = Field(default_factory=PrioritizeAllocationParams)


class ResequenceProductionLever(_Model):
    code: Literal["resequence_production"]
    params: ResequenceProductionParams = Field(default_factory=ResequenceProductionParams)


LeverSpec = Annotated[
    ReallocateInventoryLever
    | ExpediteModeShiftLever
    | ResourceAltSupplierLever
    | PrioritizeAllocationLever
    | ResequenceProductionLever,
    Field(discriminator="code"),
]
"""v1 lever union, discriminated on ``code``."""


@dataclass(frozen=True, slots=True)
class LeverDef:
    code: str
    title: str
    params_model: type[_Model]
    ops: tuple[str, ...]
    """Change-set ops the lever's actions use (capability check: ops ⊆ connector ops)."""


LEVERS: Final[Mapping[str, LeverDef]] = {
    d.code: d
    for d in (
        LeverDef(
            "reallocate_inventory",
            "Rebalance inventory between DCs",
            ReallocateInventoryParams,
            ("transfer_order.create",),
        ),
        LeverDef(
            "expedite_mode_shift",
            "Expedite by sea-air / air",
            ExpediteModeShiftParams,
            ("po_line.split", "capacity_booking.create"),
        ),
        LeverDef(
            "resource_alt_supplier",
            "Re-source from an alternate supplier",
            ResourceAltSupplierParams,
            ("po.create", "po_line.change_qty", "po_line.cancel"),
        ),
        LeverDef(
            "prioritize_allocation",
            "Prioritise allocation across clusters",
            PrioritizeAllocationParams,
            ("allocation.set_qty",),
        ),
        LeverDef(
            "resequence_production",
            "Resequence factory production",
            ResequenceProductionParams,
            ("production_order.resequence", "po_line.reschedule"),
        ),
    )
}
"""The v1 lever catalogue (``GET /v1/levers``), in display order."""

LEVER_CODES: Final[tuple[str, ...]] = tuple(LEVERS)
LEVER_OPS: Final[Mapping[str, tuple[str, ...]]] = {code: d.ops for code, d in LEVERS.items()}


def lever_params_schema(code: str) -> dict[str, Any]:
    """JSON Schema (validation mode) of a lever's ``params``."""
    return LEVERS[code].params_model.model_json_schema(mode="validation")


class LeverInfo(_Model):
    code: Code
    title: str
    params_schema: dict[str, Any]
    ops: list[str]


def lever_catalogue() -> list[LeverInfo]:
    """``GET /v1/levers`` body: every v1 lever with its ``params_schema`` and ``ops``."""
    return [
        LeverInfo(code=d.code, title=d.title, params_schema=lever_params_schema(d.code), ops=list(d.ops))
        for d in LEVERS.values()
    ]


# ───────────────────────────── /v1/optimize ─────────────────────────────


class PlanWeights(_Model):
    """Objective weights in basis points (10000 = x1.0)."""

    lost_margin: Bp = 10000
    transfer_cost: Bp = 10000


class PlanSpec(_Model):
    id: Code
    title: str
    levers: list[LeverSpec] = Field(default_factory=list)
    """Empty list = "do nothing" baseline plan (no model is solved). Several levers are
    composed into ONE model (the optimizer rejects a code listed twice)."""
    weights: PlanWeights = Field(default_factory=PlanWeights)


class SolverOptions(_Model):
    time_limit_s: Annotated[int, Strict(), Field(ge=1, le=120)] = 5
    seed: NonNegInt = 0


class OptimizeRequest(_Model):
    data: ProblemData
    perturbations: list[Perturbation] = Field(default_factory=list)
    plan_spec: PlanSpec
    solver: SolverOptions = Field(default_factory=SolverOptions)


OptimizeStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE"]


class KpiDelta(_Model):
    units_recovered: Int
    margin_recovered_cents: Int
    transfer_cost_cents: NonNegInt
    net_benefit_cents: Int
    transfers: NonNegInt
    """Number of transfer orders (= ``transfer_order.create`` actions)."""


class CostBreakdown(_Model):
    """Incremental cost by component, in cents."""

    transfer: NonNegInt
    expedite_premium: NonNegInt
    nearshore_premium: NonNegInt


class Scorecard(_Model):
    """Plan KPIs recomputed from the rounded plan (never from the objective)."""

    fill_rate_bp: ShareBp
    lost_units: NonNegInt
    lost_margin_cents: Int
    incremental_cost_cents: NonNegInt
    cost_breakdown: CostBreakdown
    net_benefit_cents: Int
    shortage_days: NonNegInt
    first_breach_shift_days: Int
    """Days the earliest breach moves later (negative = earlier)."""
    tts_gain_days: Int
    changes: NonNegInt
    """Number of change-set actions."""

    @model_validator(mode="after")
    def _breakdown_adds_up(self) -> Scorecard:
        b = self.cost_breakdown
        total = b.transfer + b.expedite_premium + b.nearshore_premium
        if total != self.incremental_cost_cents:
            raise ValueError(
                f"cost_breakdown sums to {total}, incremental_cost_cents is {self.incremental_cost_cents}"
            )
        return self


ScorecardMetric = Literal[
    "fill_rate_bp",
    "lost_units",
    "lost_margin_cents",
    "incremental_cost_cents",
    "net_benefit_cents",
    "shortage_days",
    "first_breach_shift_days",
    "tts_gain_days",
    "changes",
]
SCORECARD_METRICS: Final[tuple[str, ...]] = tuple(ScorecardMetric.__args__)  # type: ignore[attr-defined]
"""Integer scorecard metrics usable as chart axes (``scenario_compare@1``)."""


class Decision(_Model):
    """``reallocate_inventory`` decision: one DC-to-DC transfer of a SKU."""

    lever: Literal["reallocate_inventory"]
    from_dc: Code
    to_dc: Code
    sku: Code
    qty: PosInt
    ship_week: IsoWeek
    arrive_week: IsoWeek


ReallocateDecision = Decision


class ExpediteDecision(_Model):
    """``expedite_mode_shift``: ``qty`` of a PO line moved onto a faster lane."""

    lever: Literal["expedite_mode_shift"]
    po_line_ref: PoLineRef
    sku: Code
    dest_dc: Code
    lane_code: Code
    mode: _cs.SplitMode
    qty: PosInt
    book_week: IsoWeek
    """ISO week of the capacity booking (departure)."""
    arrive_week: IsoWeek
    premium_cents: NonNegInt


class AltSupplierDecision(_Model):
    """``resource_alt_supplier``: ``qty`` bought from an alternate source; the replaced
    PO line is reduced by the same quantity."""

    lever: Literal["resource_alt_supplier"]
    supplier_code: Code
    factory_code: Code
    dest_dc: Code
    sku: Code
    qty: PosInt
    eta: _IsoDate
    replaces_po_line_ref: PoLineRef
    premium_cents: NonNegInt


class AllocationDecision(_Model):
    """``prioritize_allocation``: one allocation's quantity changed."""

    lever: Literal["prioritize_allocation"]
    allocation_code: Code
    sku: Code
    dc_code: Code
    cluster_code: Code
    ship_week: IsoWeek
    qty_before: NonNegInt
    qty_after: NonNegInt


class EtaChange(_Model):
    po_line_ref: PoLineRef
    eta_before: _IsoDate
    eta_after: _IsoDate


class ResequenceDecision(_Model):
    """``resequence_production``: a factory line's new order sequence and the PO-line ETA
    changes it causes."""

    lever: Literal["resequence_production"]
    line_code: Code
    factory_code: Code
    order_codes_before: Annotated[list[Code], Field(min_length=1)]
    order_codes_after: Annotated[list[Code], Field(min_length=1)]
    eta_changes: list[EtaChange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _permutation(self) -> ResequenceDecision:
        _unique_list(self.order_codes_after, "order_codes_after")
        if sorted(self.order_codes_before) != sorted(self.order_codes_after):
            raise ValueError("order_codes_after must be a permutation of order_codes_before")
        return self


LeverDecision = Annotated[
    Decision | ExpediteDecision | AltSupplierDecision | AllocationDecision | ResequenceDecision,
    Field(discriminator="lever"),
]
"""A decision of any v1 lever, discriminated on ``lever``."""


class TransferOrderLine(_Model):
    sku: Code
    qty: PosInt


class TransferOrderCreatePayload(_Model):
    """Merxa ``transfer_order.create`` payload (``20-merxa.md``)."""

    from_dc: Code
    to_dc: Code
    ship_date: _dt.date
    lines: Annotated[list[TransferOrderLine], Field(min_length=1)]


ActionOp = Literal[
    "transfer_order.create",
    "po_line.split",
    "capacity_booking.create",
    "po.create",
    "po_line.change_qty",
    "po_line.cancel",
    "allocation.set_qty",
    "production_order.resequence",
    "po_line.reschedule",
]
"""Every op a v1 lever emits (``LEVER_OPS``)."""

ActionPayload = (
    TransferOrderCreatePayload
    | _cs.PoLineSplitPayload
    | _cs.CapacityBookingCreatePayload
    | _cs.PoCreatePayload
    | _cs.PoLineChangeQtyPayload
    | _cs.PoLineCancelPayload
    | _cs.AllocationSetQtyPayload
    | _cs.ProductionOrderResequencePayload
    | _cs.PoLineReschedulePayload
)

_ACTION_PAYLOADS: Final[Mapping[str, type[BaseModel]]] = {
    "transfer_order.create": TransferOrderCreatePayload,
    **{op: _cs.OP_PAYLOAD_MODELS[op] for op in ActionOp.__args__ if op != "transfer_order.create"},  # type: ignore[attr-defined]
}


class Action(_Model):
    """A canonical change-set op (``seq, op, target_ref, expected_version, payload``) plus
    ``group``: actions that must be applied atomically (e.g. ``po.create`` with the
    ``po_line.change_qty`` it compensates) share one group id and are adjacent in
    ``seq`` order. ``group`` never enters the change-set hash.

    The payload is validated by the model of ``op``, and every action is also checked
    against the typed change-set item (``keelson_contracts.changeset``), so it always
    compiles (e.g. a transfer with ``from_dc == to_dc`` or duplicate SKU lines, which
    Merxa would reject at validation, is rejected here)."""

    seq: PosInt
    op: ActionOp
    target_ref: str | None = None
    expected_version: NonNegInt | None = None
    payload: ActionPayload
    group: Code | None = _m3_optional()

    @field_validator("payload", mode="before")
    @classmethod
    def _payload_for_op(cls, value: Any, info: ValidationInfo) -> Any:
        op = info.data.get("op")
        model = _ACTION_PAYLOADS.get(op) if isinstance(op, str) else None
        if model is not None and isinstance(value, Mapping):
            return model.model_validate(value)
        return value

    @model_validator(mode="after")
    def _check_op(self) -> Action:
        model = _ACTION_PAYLOADS[self.op]
        if not isinstance(self.payload, model):
            raise ValueError(f"payload of {self.op} must be {model.__name__}, got {type(self.payload).__name__}")
        _cs.CHANGE_SET_ITEM_ADAPTER.validate_python(self.change_set_item())
        return self

    def change_set_item(self) -> dict[str, Any]:
        """The five hashed keys as a wire dict (``group`` excluded)."""
        data: dict[str, Any] = self.model_dump(
            mode="json", include={"seq", "op", "target_ref", "expected_version", "payload"}
        )
        return data


class Explain(_Model):
    binding_constraints: list[BindingConstraint]


class SolverInfo(_Model):
    name: str
    ortools: str
    params: dict[str, int | str | bool]
    seed: NonNegInt
    inputs_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    solve_ms: NonNegInt
    status_detail: str | None = None
    """Additive: raw solver status when it is not OPTIMAL (e.g. time limit)."""


class OptimizeResponse(_Model):
    status: OptimizeStatus
    plan_spec_id: Code
    kpis_before: ImpactKpis
    kpis_after: ImpactKpis
    kpi_delta: KpiDelta
    decisions: list[LeverDecision]
    actions: list[Action]
    """Ordered deterministically with ``seq`` = 1..n; atomic groups are contiguous."""
    explain: Explain
    solver: SolverInfo
    warnings: list[str] = Field(default_factory=list)
    scorecard: Scorecard | None = _m3_optional()
    """M3: KPIs of the plan recomputed from the rounded solution."""

    @model_validator(mode="after")
    def _actions_ordered(self) -> OptimizeResponse:
        seqs = [a.seq for a in self.actions]
        if seqs != list(range(1, len(seqs) + 1)):
            raise ValueError("actions must have seq 1..n in list order")
        closed: set[str] = set()
        prev: str | None = None
        for a in self.actions:
            if a.group != prev:
                if prev is not None:
                    closed.add(prev)
                if a.group is not None and a.group in closed:
                    raise ValueError(f"actions of atomic group {a.group!r} must be contiguous")
                prev = a.group
        return self
