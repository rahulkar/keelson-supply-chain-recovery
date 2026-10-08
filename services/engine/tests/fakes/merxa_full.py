"""A read-only Merxa MCP stub sized like the M2 ``full`` seed (``22-merxa-m2.md`` §2):
4 DCs, 18 non-empty clusters, 451 stores, 2 families × 15 style-colours × 6 sizes = 180 SKUs,
12 POs (2 received), forecast style × cluster × week for W41–W49.

Only the reads ``gather_problem_data`` uses, with Merxa's argument names, page limits
(``limit <= 1000``, string cursors) and the M2 aggregates. Store-level inventory exists
(81,180 positions) and the ``cluster`` aggregate is computed from it, so tests can check the
aggregate path against ground truth. ``latency_s`` simulates a network round trip per call;
``max_in_flight`` records the observed concurrency.
"""

from __future__ import annotations

import asyncio
import copy
from collections import Counter, defaultdict
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from mcp import types as mcp_types

READ = mcp_types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
DCS = ("DE1", "NL1", "PL1", "UK1")
CLIMATES = ("cold", "mild", "warm")
GRADES = ("A", "BC")
SIZES = ("XS", "S", "M", "L", "XL", "XXL")
SIZE_BP = (800, 1700, 2600, 2400, 1600, 900)
WEEKS = tuple(f"2026-W{w}" for w in range(41, 50))
N_STORES = 451
MAX_LIMIT = 1000


def _clusters() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for dc in DCS:
        for climate in CLIMATES:
            for grade in GRADES:
                if climate == "warm" and dc in ("UK1", "PL1"):
                    continue  # no warm stores served from UK1 / PL1
                if climate == "warm" and grade == "BC":
                    continue
                out.append((f"{dc}-{climate.upper()}-{grade}", dc))
    return out


CLUSTERS = _clusters()
STYLES = tuple(
    [(f"PUF-{i:02d}", "Outerwear", 4500 + 100 * i, 1600 + 40 * i) for i in range(1, 16)]
    + [(f"KNT-{i:02d}", "Knit", 2200 + 50 * i, 700 + 20 * i) for i in range(1, 16)]
)


class FullMerxaStub:
    def __init__(self, *, latency_s: float = 0.0, forecast_page_size: int = 10_000) -> None:
        """``forecast_page_size``: rows per ``get_demand_forecast`` page (Merxa: ``limit`` ≤ 10,000,
        then ``next_cursor``)."""
        self.latency_s = latency_s
        self.forecast_page_size = forecast_page_size
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.stores = [
            {"code": f"S{n:04d}", "cluster": CLUSTERS[n % len(CLUSTERS)][0], "home_dc": CLUSTERS[n % len(CLUSTERS)][1]}
            for n in range(N_STORES)
        ]
        self.skus = [(f"{style}-{size}", style) for style, *_ in STYLES for size in SIZES]
        self.dc_rows = [
            self._row("dc", dc, sku, on_hand=600 + (i * 37) % 900, allocated=(i * 11) % 80, reserved=0, in_transit=0)
            for dc in DCS for i, (sku, _) in enumerate(self.skus)
        ]
        # store positions (ground truth) → cluster aggregates
        sums: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
        for s_idx, store in enumerate(self.stores):
            for k_idx, (sku, _) in enumerate(self.skus):
                on_hand = 2 + (s_idx * 7 + k_idx * 3) % 9
                allocated = (s_idx + k_idx) % 2
                acc = sums[(store["cluster"], sku)]
                acc.update({"on_hand": on_hand, "allocated": allocated, "atp": on_hand - allocated, "store_count": 1})
        self.cluster_truth = {key: c["atp"] for key, c in sums.items()}
        self.cluster_rows = [
            {"location_type": "cluster", "location_code": cl, "sku": sku, "on_hand": c["on_hand"],
             "allocated": c["allocated"], "reserved": 0, "in_transit": 0, "atp": c["atp"], "store_count": c["store_count"]}
            for (cl, sku), c in sorted(sums.items())
        ]
        self.forecast = [
            {"style_code": style, "cluster_code": cl, "iso_week": week,
             "units": (40 + (si * 13 + ci * 5) % 60) * (3 if week in ("2026-W45", "2026-W46", "2026-W47", "2026-W48") else 1)}
            for si, (style, *_) in enumerate(STYLES) for ci, (cl, _) in enumerate(CLUSTERS) for week in WEEKS
        ]
        self.size_curves = [
            {"family": family, "cluster_code": cl, "size": size, "share_bp": bp}
            for family in ("Outerwear", "Knit") for cl, _ in CLUSTERS for size, bp in zip(SIZES, SIZE_BP, strict=True)
        ]
        self.pos: dict[str, dict[str, Any]] = {}
        for n in range(12):
            code = f"PO-{'BD' if n < 8 else 'VN'}-{n + 1:04d}"
            status = "received" if n in (3, 9) else ("open" if n % 2 else "in_production")
            style = STYLES[(n * 5) % len(STYLES)][0]
            eta = f"2026-{10 + (n % 3)}-{10 + n:02d}"
            self.pos[code] = {
                "code": code, "supplier_code": "TRIM-BD-01" if n < 8 else "SUP-VN-01",
                "factory_code": "F-BD-01" if n < 8 else "F-VN-01", "dest_dc": DCS[n % 4], "status": status,
                "ex_factory_date": "2026-10-01", "eta_dc": eta, "version": 1,
                "lines": [{"line_no": i + 1, "sku": f"{style}-{size}", "qty_ordered": 1200, "version": 1,
                           "qty_received": 1200 if status == "received" else 0, "eta_dc": eta,
                           "status": "received" if status == "received" else "open"}
                          for i, size in enumerate(SIZES)],
            }
        self.mcp = self._build()

    @staticmethod
    def _row(kind: str, code: str, sku: str, **v: int) -> dict[str, Any]:
        return {"location_type": kind, "location_code": code, "sku": sku, **v,
                "atp": v["on_hand"] - v["allocated"] - v["reserved"], "version": 1}

    def count(self, tool: str) -> int:
        return sum(1 for name, _ in self.calls if name == tool)

    async def _enter(self, tool: str, args: dict[str, Any]) -> None:
        self.calls.append((tool, args))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)

    def _leave(self) -> None:
        self.in_flight -= 1

    @staticmethod
    def _page(rows: list[dict[str, Any]], limit: int, cursor: str | None) -> dict[str, Any]:
        if limit > MAX_LIMIT:
            raise ToolError("invalid_parameter: limit must be <= 1000")
        start = int(cursor or 0)
        nxt = str(start + limit) if start + limit < len(rows) else None
        return {"items": copy.deepcopy(rows[start : start + limit]), "next_cursor": nxt}

    def _build(self) -> FastMCP:
        mcp = FastMCP(name="Merxa Retail Suite (full-size stub)")
        stub = self

        @mcp.tool(name="list_locations", annotations=READ)
        async def list_locations() -> ToolResult:
            await stub._enter("list_locations", {})
            try:
                counts = Counter(s["cluster"] for s in stub.stores)
                data = {
                    "dcs": [{"code": dc, "name": dc, "country": dc[:2], "lat": 0.0, "lon": 0.0} for dc in DCS],
                    "clusters": [{"code": cl, "dc_code": dc, "climate": cl.split("-")[1].lower(),
                                  "grade_band": cl.split("-")[2], "store_count": counts[cl]} for cl, dc in CLUSTERS],
                }
                return ToolResult(content="locations", structured_content=data)
            finally:
                stub._leave()

        @mcp.tool(name="get_demand_forecast", annotations=READ)
        async def get_demand_forecast(style_code: str | None = None, cluster_codes: list[str] | None = None,
                                      weeks: list[str] | None = None,
                                      aggregate: Literal["store", "cluster"] | None = None,
                                      limit: int = 10_000, cursor: str | None = None) -> ToolResult:
            await stub._enter("get_demand_forecast", {"aggregate": aggregate, "cursor": cursor})
            try:
                if aggregate == "store":
                    raise ToolError("invalid_parameter: store-level forecast is not served by this stub")
                items = [r for r in stub.forecast if (not weeks or r["iso_week"] in weeks)
                         and style_code in (None, r["style_code"]) and (not cluster_codes or r["cluster_code"] in cluster_codes)]
                size = min(limit, stub.forecast_page_size)
                start = int(cursor or 0)
                nxt = str(start + size) if start + size < len(items) else None
                return ToolResult(content=f"{len(items)} rows",
                                  structured_content={"items": copy.deepcopy(items[start : start + size]),
                                                      "next_cursor": nxt,
                                                      "size_curves": copy.deepcopy(stub.size_curves)})
            finally:
                stub._leave()

        @mcp.tool(name="get_entity", annotations=READ)
        async def get_entity(type: str, code: str) -> ToolResult:
            await stub._enter("get_entity", {"type": type, "code": code})
            try:
                for style, family, price, cost in STYLES:
                    if type == "style" and code == style:
                        data = {"code": style, "family": family, "retail_price_cents": price, "unit_cost_cents": cost,
                                "skus": [f"{style}-{size}" for size in SIZES], "version": 1}
                        return ToolResult(content=code, structured_content={"type": type, "code": code, "data": data, "version": 1})
                raise ToolError(f"not_found: {type} {code}")
            finally:
                stub._leave()

        @mcp.tool(name="get_inventory", annotations=READ)
        async def get_inventory(location_codes: list[str] | None = None, sku_codes: list[str] | None = None,
                                style_code: str | None = None, location_type: Literal["dc", "store"] | None = None,
                                aggregate: Literal["none", "cluster", "dc"] | None = None,
                                limit: int = 500, cursor: str | None = None) -> ToolResult:
            await stub._enter("get_inventory", {"aggregate": aggregate, "location_type": location_type, "cursor": cursor})
            try:
                if aggregate != "cluster":
                    raise ToolError("invalid_parameter: this stub serves only aggregate=cluster (81,180 store rows)")
                rows = [r for r in stub.dc_rows + stub.cluster_rows if location_type in (None, "dc") or r["location_type"] != "dc"]
                return ToolResult(content="positions", structured_content=stub._page(rows, limit, cursor))
            finally:
                stub._leave()

        @mcp.tool(name="list_purchase_orders", annotations=READ)
        async def list_purchase_orders(status: str | None = None, limit: int = 50, cursor: str | None = None) -> ToolResult:
            await stub._enter("list_purchase_orders", {"cursor": cursor})
            try:
                rows = [{k: v for k, v in po.items() if k != "lines"} | {"line_count": len(po["lines"])}
                        for _, po in sorted(stub.pos.items()) if status in (None, po["status"])]
                return ToolResult(content="POs", structured_content=stub._page(rows, limit, cursor))
            finally:
                stub._leave()

        @mcp.tool(name="get_purchase_order", annotations=READ)
        async def get_purchase_order(code: str) -> ToolResult:
            await stub._enter("get_purchase_order", {"code": code})
            try:
                if code not in stub.pos:
                    raise ToolError(f"not_found: purchase order {code}")
                return ToolResult(content=code, structured_content=copy.deepcopy(stub.pos[code]))
            finally:
                stub._leave()

        return mcp
