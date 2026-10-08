"""Gather the optimizer's ``ProblemData`` from the connector (deterministic MCP reads).

Reads (reader principal, explicit pagination), sized for the M2 ``full`` seed (451 stores,
~18 clusters, 30 styles / 180 SKUs, 12 POs):

* ``list_locations`` — DCs and store clusters;
* ``get_demand_forecast(weeks, aggregate="cluster")`` (+ size curves) — style × cluster × week;
* ``get_entity(style)`` per forecast style — prices and SKU codes;
* ``get_inventory(aggregate="cluster")`` — DC rows unchanged plus one row per cluster × SKU
  (store rows summed by Merxa, ``22-merxa-m2.md`` §3): a few pages instead of ~80k store rows;
* ``list_purchase_orders`` + ``get_purchase_order`` per open PO (open receipts per PO line).

Independent single reads (styles, POs) run concurrently on the one session (at most
:data:`READ_CONCURRENCY` in flight) and are recorded as evidence in a deterministic order.
A connector without the M2 aggregates (a tool error on ``aggregate``, or store rows returned
anyway) falls back to the M1 reads: DC + store inventory pages and ``list_stores`` for the
store → cluster map, summed here.

Lanes are tenant network config (``infra/tenants.yaml``), not ERP data. Every read is recorded
as evidence (``prov: fact``) through the ``record`` callback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from keelson_contracts.common import iso_week_of, week_range
from keelson_contracts.optimizer import (
    DC,
    Cluster,
    ClusterInventory,
    DemandRow,
    Horizon,
    InventoryPosition,
    Lane,
    Location,
    ProblemData,
    Receipt,
    Sku,
)

from .errors import McpToolError

__all__ = ["READ_CONCURRENCY", "ProblemBundle", "ToolCaller", "gather_problem_data", "receipt_ref"]

log = structlog.get_logger(__name__)

CLOSED_PO_STATUSES = frozenset({"received", "cancelled"})
CLOSED_LINE_STATUSES = frozenset({"received", "cancelled", "closed"})
PAGE_LIMIT = 1000
MAX_PAGES = 50
READ_CONCURRENCY = 8
"""Single-entity reads in flight at once on one MCP session."""


class ToolCaller(Protocol):
    async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]: ...


Recorder = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]
"""``(tool, args, result) -> evidence_id``."""


@dataclass
class ProblemBundle:
    data: ProblemData
    evidence_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    po_lines: list[dict[str, Any]] = field(default_factory=list)
    """Open PO lines ``{po_code, line_no, sku, qty, eta_dc, dest_dc, supplier_code, factory_code}``."""
    source: str = "mcp"
    """``scn`` (built from a synced snapshot) or ``mcp`` (live reads — advisory, ``80`` §4)."""
    snapshot_id: str | None = None
    snapshot_as_of: dt.date | None = None
    snapshot_label: dict[str, Any] | None = None
    """M7 (``110`` §4): ``{consistency, as_of_range}`` of the snapshot (``crawled`` = tier-A paging)."""

    @property
    def crawled(self) -> bool:
        return self.source == "scn" and (self.snapshot_label or {}).get("consistency") == "crawled"

    @property
    def advisory(self) -> bool:
        """Live reads (no snapshot) and crawled snapshots are advisory (``80`` §4, ``110`` §4)."""
        return self.source != "scn" or self.crawled


def receipt_ref(po_code: str, line_no: int) -> str:
    return f"{po_code}#{line_no}"


class _Reader:
    def __init__(self, caller: ToolCaller, record: Recorder | None, concurrency: int) -> None:
        self._caller = caller
        self._record = record
        self._slots = asyncio.Semaphore(max(1, concurrency))
        self.evidence_ids: list[str] = []
        self.calls = 0

    async def _raw(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self._slots:
            self.calls += 1
            return await self._caller.call(tool, args)

    async def _keep(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if self._record is not None:
            self.evidence_ids.append(await self._record(tool, args, result))

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._raw(tool, args)
        await self._keep(tool, args, result)
        return result

    async def many(self, tool: str, args_list: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Concurrent independent reads; evidence is recorded afterwards in ``args_list`` order.

        The first failure cancels the reads still in flight (they would otherwise keep using a
        session that the caller is about to close) and is re-raised unchanged."""
        tasks = [asyncio.ensure_future(self._raw(tool, args)) for args in args_list]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for args, result in zip(args_list, results, strict=True):
            await self._keep(tool, args, result)
        return list(results)

    async def pages(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = await self.call(tool, {**args, "cursor": cursor} if cursor else dict(args))
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return items
        raise RuntimeError(f"{tool}: more than {MAX_PAGES} pages")


def _size_of(sku_code: str, style_code: str) -> str:
    prefix = f"{style_code}-"
    return sku_code[len(prefix) :] if sku_code.startswith(prefix) else sku_code.rsplit("-", 1)[-1]


def _split_units(units: int, shares: Sequence[tuple[str, int]]) -> dict[str, int]:
    """Split ``units`` by basis-point shares; remainders go to the largest shares (deterministic)."""
    if not shares:
        return {}
    total_bp = sum(bp for _, bp in shares)
    if total_bp <= 0:
        equal = units // len(shares)
        out = {key: equal for key, _ in shares}
        for key, _ in shares[: units - equal * len(shares)]:
            out[key] += 1
        return out
    out = {key: (units * bp) // total_bp for key, bp in shares}
    leftover = units - sum(out.values())
    for key, _ in sorted(shares, key=lambda kv: (-kv[1], kv[0]))[:leftover]:
        out[key] += 1
    return out


def _atp(row: Mapping[str, Any]) -> int:
    atp = row.get("atp")
    if atp is None:
        atp = int(row["on_hand"]) - int(row.get("allocated") or 0) - int(row.get("reserved") or 0)
    return max(0, int(atp))


async def _forecast_pages(reader: _Reader, args: dict[str, Any]) -> dict[str, Any]:
    """All pages of one forecast read: Merxa M2 pages ``get_demand_forecast`` (``limit`` ≤ 10,000,
    ``next_cursor``), so a long horizon or a large network must not silently lose demand rows.
    ``cursor`` is only sent when the server returned one (a pre-M2 server has no such argument);
    size curves come with every page and are taken from the first."""
    first = await reader.call("get_demand_forecast", dict(args))
    items = list(first.get("items") or [])
    cursor = first.get("next_cursor")
    for _ in range(MAX_PAGES):
        if not cursor:
            return {**first, "items": items, "next_cursor": None}
        page = await reader.call("get_demand_forecast", {**args, "cursor": cursor})
        items.extend(page.get("items") or [])
        cursor = page.get("next_cursor")
    raise RuntimeError(f"get_demand_forecast: more than {MAX_PAGES} pages")


async def _forecast(reader: _Reader, weeks: list[str]) -> dict[str, Any]:
    try:
        return await _forecast_pages(reader, {"weeks": weeks, "aggregate": "cluster"})
    except McpToolError as exc:  # a pre-M2 server: cluster rows are its only shape
        log.warning("forecast aggregate unsupported; reading the default shape", code=exc.code)
        return await _forecast_pages(reader, {"weeks": weeks})


async def _inventory(
    reader: _Reader, *, dc_codes: set[str], cluster_codes: set[str], sku_codes: set[str]
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], int], int]:
    """``(dc rows, {(cluster, sku): atp}, rows read)`` — cluster aggregates when the server has
    them, else store rows summed per cluster here (M1 fallback)."""
    rows: list[dict[str, Any]] | None
    try:
        rows = await reader.pages("get_inventory", {"aggregate": "cluster", "limit": PAGE_LIMIT})
    except McpToolError as exc:
        log.warning("inventory aggregate unsupported; reading store positions", code=exc.code)
        rows = None
    cluster_stock: dict[tuple[str, str], int] = defaultdict(int)
    if rows is not None and not any(r.get("location_type") == "store" for r in rows):
        dc_rows = [r for r in rows if r.get("location_type") == "dc"]
        for row in rows:
            if row.get("location_type") == "cluster" and row["location_code"] in cluster_codes and row["sku"] in sku_codes:
                cluster_stock[(row["location_code"], row["sku"])] += _atp(row)
        return dc_rows, cluster_stock, len(rows)
    if rows is None:
        dc_rows = await reader.pages("get_inventory", {"location_type": "dc", "limit": PAGE_LIMIT})
        store_rows = await reader.pages("get_inventory", {"location_type": "store", "limit": PAGE_LIMIT})
    else:  # the server ignored ``aggregate`` and returned store rows
        dc_rows = [r for r in rows if r.get("location_type") == "dc"]
        store_rows = [r for r in rows if r.get("location_type") == "store"]
    stores = await reader.pages("list_stores", {"limit": PAGE_LIMIT})
    store_cluster = {s["code"]: s["cluster"] for s in stores}
    for row in store_rows:
        cluster = store_cluster.get(row["location_code"])
        if cluster in cluster_codes and row["sku"] in sku_codes:
            cluster_stock[(cluster, row["sku"])] += _atp(row)
    return dc_rows, cluster_stock, len(dc_rows) + len(store_rows)


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def _is_coord(value: Any, limit: int) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= limit


def _e6(degrees: float) -> int:
    scaled = float(degrees) * 1_000_000
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


async def gather_problem_data(
    tenant: str,
    caller: ToolCaller,
    *,
    lanes: Sequence[Mapping[str, Any]],
    as_of: dt.date,
    horizon_weeks: int,
    record: Recorder | None = None,
    concurrency: int = READ_CONCURRENCY,
) -> ProblemBundle:
    reader = _Reader(caller, record, concurrency)
    weeks = week_range(iso_week_of(as_of), horizon_weeks)
    week_set = set(weeks)

    locations = await reader.call("list_locations", {})
    dc_rows = sorted(locations.get("dcs") or [], key=lambda d: d["code"])
    cluster_rows = sorted(locations.get("clusters") or [], key=lambda c: c["code"])
    dc_codes = {d["code"] for d in dc_rows}
    dcs = [
        DC(code=d["code"], outbound_cap_units_wk=d.get("outbound_cap_units_wk"))
        for d in dc_rows
    ]
    clusters = [Cluster(code=c["code"], dc_code=c["dc_code"]) for c in cluster_rows if c["dc_code"] in dc_codes]
    cluster_codes = {c.code for c in clusters}
    locations = [  # M3 map: DC positions when the connector publishes them (clusters carry none)
        Location(code=d["code"], kind="dc", name=str(d.get("name") or d["code"]), lat_e6=_e6(d["lat"]),
                 lon_e6=_e6(d["lon"]))
        for d in dc_rows
        if _is_coord(d.get("lat"), 90) and _is_coord(d.get("lon"), 180)
    ]

    forecast = await _forecast(reader, weeks)
    forecast_items = [r for r in forecast.get("items") or [] if r.get("cluster_code")]
    style_codes = _sorted_unique(row["style_code"] for row in forecast_items)

    skus: list[Sku] = []
    sizes_by_style: dict[str, list[tuple[str, str]]] = {}
    family_by_style: dict[str, str] = {}
    entities = await reader.many("get_entity", [{"type": "style", "code": style} for style in style_codes])
    for style, entity in zip(style_codes, entities, strict=True):
        data = entity.get("data") or {}
        family_by_style[style] = str(data.get("family") or "")
        sku_codes = list(data.get("skus") or [])
        sizes_by_style[style] = [(code, _size_of(code, style)) for code in sku_codes]
        for code in sku_codes:
            skus.append(
                Sku(
                    code=code,
                    style_code=style,
                    retail_price_cents=int(data["retail_price_cents"]),
                    unit_cost_cents=int(data["unit_cost_cents"]),
                )
            )
    sku_codes_all = {s.code for s in skus}

    curves: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for row in forecast.get("size_curves") or []:
        curves[(str(row.get("family") or ""), row["cluster_code"])][row["size"]] = int(row["share_bp"])

    demand: list[DemandRow] = []
    for row in sorted(forecast_items, key=lambda r: (r["cluster_code"], r["style_code"], r["iso_week"])):
        if row["cluster_code"] not in cluster_codes or row["iso_week"] not in week_set:
            continue
        style = row["style_code"]
        curve = curves.get((family_by_style.get(style, ""), row["cluster_code"])) or curves.get(
            ("", row["cluster_code"]), {}
        )
        shares = [(code, curve.get(size, 0)) for code, size in sizes_by_style.get(style, [])]
        for sku, units in sorted(_split_units(int(row["units"]), shares).items()):
            if units > 0:
                demand.append(DemandRow(cluster_code=row["cluster_code"], sku=sku, iso_week=row["iso_week"], units=units))

    dc_inventory, cluster_stock, inventory_rows = await _inventory(
        reader, dc_codes=dc_codes, cluster_codes=cluster_codes, sku_codes=sku_codes_all
    )
    inventory = [
        InventoryPosition(
            location_type="dc",
            location_code=row["location_code"],
            sku=row["sku"],
            on_hand=int(row["on_hand"]),
            allocated=int(row.get("allocated") or 0),
            reserved=int(row.get("reserved") or 0),
            in_transit=int(row.get("in_transit") or 0),
        )
        for row in sorted(dc_inventory, key=lambda r: (r["location_code"], r["sku"]))
        if row["location_code"] in dc_codes and row["sku"] in sku_codes_all
    ]
    cluster_inventory = [
        ClusterInventory(cluster_code=cl, sku=sku, on_hand=qty)
        for (cl, sku), qty in sorted(cluster_stock.items())
        if qty > 0
    ]

    po_summaries = await reader.pages("list_purchase_orders", {"limit": PAGE_LIMIT})
    receipts: list[Receipt] = []
    po_lines: list[dict[str, Any]] = []
    open_pos = sorted(
        (p for p in po_summaries if p.get("status") not in CLOSED_PO_STATUSES and p.get("dest_dc") in dc_codes),
        key=lambda p: p["code"],
    )
    pos = await reader.many("get_purchase_order", [{"code": summary["code"]} for summary in open_pos])
    for po in pos:
        for line in sorted(po.get("lines") or [], key=lambda ln: int(ln["line_no"])):
            qty = int(line["qty_ordered"]) - int(line.get("qty_received") or 0)
            if qty <= 0 or line.get("status") in CLOSED_LINE_STATUSES or line["sku"] not in sku_codes_all:
                continue
            eta = dt.date.fromisoformat(str(line.get("eta_dc") or po["eta_dc"]))
            ref = receipt_ref(po["code"], int(line["line_no"]))
            receipts.append(Receipt(ref=ref, dc_code=po["dest_dc"], sku=line["sku"], qty=qty, eta=eta))
            po_lines.append(
                {
                    "po_code": po["code"],
                    "line_no": int(line["line_no"]),
                    "sku": line["sku"],
                    "qty": qty,
                    "eta_dc": eta.isoformat(),
                    "dest_dc": po["dest_dc"],
                    "supplier_code": po.get("supplier_code"),
                    "factory_code": po.get("factory_code"),
                    "version": line.get("version"),
                }
            )

    lane_models = [
        Lane(
            from_dc=str(lane["from_dc"]),
            to_dc=str(lane["to_dc"]),
            transit_days=int(lane["transit_days"]),
            cost_per_unit_cents=int(lane["cost_per_unit_cents"]),
            max_units_wk=int(lane["max_units_wk"]),
        )
        for lane in lanes
        if lane.get("from_dc") in dc_codes and lane.get("to_dc") in dc_codes
    ]

    data = ProblemData(
        tenant=tenant,
        as_of=as_of,
        horizon=Horizon(start_week=weeks[0], weeks=horizon_weeks),
        dcs=dcs,
        clusters=clusters,
        skus=sorted(skus, key=lambda s: s.code),
        inventory=inventory,
        cluster_inventory=cluster_inventory,
        receipts=receipts,
        demand=demand,
        lanes=lane_models,
        protected_min=[],
        locations=locations,
    )
    return ProblemBundle(
        data=data,
        evidence_ids=reader.evidence_ids,
        counts={
            "dcs": len(dcs),
            "clusters": len(clusters),
            "skus": len(skus),
            "purchase_orders": len(open_pos),
            "po_lines": len(receipts),
            "inventory_positions": inventory_rows,
            "forecast_rows": len(forecast_items),
            "connector_calls": reader.calls,
        },
        po_lines=po_lines,
    )
