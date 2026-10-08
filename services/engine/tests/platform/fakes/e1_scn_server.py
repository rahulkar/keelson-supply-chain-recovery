"""A fake Tier-B snapshot source for ``core.scn_sync`` tests: a real FastMCP 4 server on 127.0.0.1
(uvicorn, random port) with ``scn.snapshot_open`` / ``scn.snapshot_export`` / ``scn.changes_since``
and the signed-URL download route ``GET /v1/snapshots/<id>/<entity>/part-NNNN.parquet`` on the
same origin, serving Parquet parts written with pyarrow (zstd) like Merxa's exporter.

Knobs (``ScnState``) make it misbehave: corrupt part bytes (sha256 mismatch), lie about row
counts, hand out part URLs on a foreign host, omit an entity, delay ``snapshot_open`` (single-flight),
mutate rows (FK / data errors) and script ``changes_since`` answers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from keelson_contracts.scn import SCN_COLUMNS, SCN_ENTITIES

READ = {"readOnlyHint": True}

DATE_COLS = frozenset({"valid_from", "valid_to", "cutoff", "etd", "eta", "ex_factory_date", "eta_dc",
                       "planned_start", "planned_end", "ship_date", "eta_date", "promise_date", "period_start",
                       "period_end"})
FLOAT_COLS = frozenset({"lat", "lon"})
LIST_COLS = frozenset({"via", "markets", "grades", "style_codes"})
INT_COLS = frozenset({"size_seq", "retail_price_cents", "unit_cost_cents", "qty_per_unit", "transit_days",
                      "rate_cents", "weekly_capacity", "capacity_teu", "line_no", "qty_ordered", "qty_received",
                      "seq", "on_hand", "allocated", "reserved", "in_transit", "units", "forecast_version",
                      "share_bp", "qty", "reserved_qty", "discount_bp", "uplift_bp", "src_version"})


def arrow_type(col: str) -> pa.DataType:
    if col in DATE_COLS:
        return pa.date32()
    if col in FLOAT_COLS:
        return pa.float64()
    if col in LIST_COLS:
        return pa.list_(pa.string())
    if col in INT_COLS:
        return pa.int64()
    return pa.string()


def schema(entity: str) -> pa.Schema:
    return pa.schema([(c, arrow_type(c)) for c in SCN_COLUMNS[entity]])


D = dt.date


def small_dataset(stores: int = 3, forecast_weeks: int = 3) -> dict[str, list[dict[str, Any]]]:
    """A tiny consistent network (2 DCs, clusters, stores, a port, a factory, a supplier, one style)."""
    loc = [
        {"source_id": "dc:UK1", "kind": "dc", "code": "UK1", "name": "Thrapston", "country": "GB", "city": "Thrapston",
         "lat": 52.397, "lon": -0.538, "src_version": 1},
        {"source_id": "dc:NL1", "kind": "dc", "code": "NL1", "name": "Venlo", "country": "NL", "city": "Venlo",
         "lat": 51.37, "lon": 6.17, "src_version": 1},
        {"source_id": "cluster:UK1-COLD-A", "kind": "cluster", "code": "UK1-COLD-A", "name": "UK1 cold A",
         "country": "GB", "parent_code": "UK1", "grade": "A", "climate": "cold", "src_version": 0},
        {"source_id": "port:GBFXT", "kind": "port", "code": "GBFXT", "name": "Felixstowe", "country": "GB",
         "lat": 51.96, "lon": 1.35, "unlocode": "GBFXT", "src_version": 0},
        {"source_id": "factory:F-BD-01", "kind": "factory", "code": "F-BD-01", "name": "Gazipur", "country": "BD",
         "parent_code": "TRIM-BD-01", "src_version": 0},
        {"source_id": "supplier:TRIM-BD-01", "kind": "supplier", "code": "TRIM-BD-01", "name": "Meghna Trims",
         "country": "BD", "src_version": 0},
    ]
    store_codes = [f"S{i:03d}" for i in range(1, stores + 1)]
    for code in store_codes:
        loc.append({"source_id": f"store:{code}", "kind": "store", "code": code, "name": f"Store {code}",
                    "country": "GB", "city": "London", "lat": 51.5072, "lon": -0.1276, "parent_code": "UK1-COLD-A",
                    "grade": "A", "climate": "cold", "src_version": 2})
    items = [
        {"source_id": "style:ST1", "kind": "style", "code": "ST1", "style_code": "ST1", "family": "Outerwear",
         "retail_price_cents": 3500, "unit_cost_cents": 1100, "currency": "EUR", "src_version": 1},
        {"source_id": "sku:ST1-M", "kind": "sku", "code": "ST1-M", "style_code": "ST1", "family": "Outerwear",
         "colour": "black", "size": "M", "size_seq": 3, "retail_price_cents": 3500, "unit_cost_cents": 1100,
         "currency": "EUR", "src_version": 1},
        {"source_id": "sku:ST1-L", "kind": "sku", "code": "ST1-L", "style_code": "ST1", "family": "Outerwear",
         "colour": "black", "size": "L", "size_seq": 4, "retail_price_cents": 3500, "unit_cost_cents": 1100,
         "currency": "EUR", "src_version": 1},
        {"source_id": "component:ZIP-01", "kind": "component", "code": "ZIP-01", "src_version": 0},
    ]
    weeks = [f"2026-W{44 + i:02d}" for i in range(forecast_weeks)]
    forecast = [
        {"source_id": f"fc:ST1:{s}:{w}", "style_code": "ST1", "location_kind": "store", "location_code": s,
         "iso_week": w, "units": 10 + i, "forecast_version": 3}
        for i, (s, w) in enumerate((s, w) for s in store_codes for w in weeks)
    ]
    return {
        "location": loc,
        "item": items,
        "bom_line": [{"source_id": "bom:ST1:ZIP-01", "style_code": "ST1", "component_code": "ZIP-01",
                      "supplier_code": "TRIM-BD-01", "factory_code": None, "qty_per_unit": 1, "src_version": 0}],
        "lane": [{"source_id": "lane:BDCGP-UK1-SEA", "code": "BDCGP-UK1-SEA", "origin_code": "BDCGP",
                  "dest_code": "UK1", "mode": "sea", "carrier_code": "MSK", "transit_days": 38,
                  "via": ["CAPE"], "alt_lane_code": None, "src_version": 1}],
        "lane_rate": [{"source_id": "rate:BDCGP-UK1-SEA:2026-01-01", "lane_code": "BDCGP-UK1-SEA",
                       "valid_from": D(2026, 1, 1), "valid_to": None, "basis": "per_unit", "rate_cents": 45,
                       "weekly_capacity": 20000, "src_version": 0}],
        "sailing": [{"source_id": "sailing:SAIL-1", "code": "SAIL-1", "lane_code": "BDCGP-UK1-SEA",
                     "cutoff": D(2026, 9, 20), "etd": D(2026, 9, 22), "eta": D(2026, 10, 30), "capacity_teu": 800,
                     "status": "departed", "src_version": 2}],
        "po_line": [{"source_id": "PO-1#1", "po_code": "PO-1", "line_no": 1, "supplier_code": "TRIM-BD-01",
                     "factory_code": "F-BD-01", "dest_dc_code": "UK1", "sku_code": "ST1-M", "qty_ordered": 1200,
                     "qty_received": 0, "ex_factory_date": D(2026, 9, 15), "eta_dc": D(2026, 11, 2),
                     "status": "open", "shipment_code": "SH-1", "sailing_code": "SAIL-1", "src_version": 4}],
        "production_order": [{"source_id": "prod:PRD-1", "code": "PRD-1", "po_line_ref": "PO-1#1",
                              "line_code": "F-BD-01-L1", "seq": 1, "planned_start": D(2026, 9, 1),
                              "planned_end": D(2026, 9, 10), "status": "done", "hold_reason": None,
                              "src_version": 1}],
        "inventory_position": [
            {"source_id": "inv:dc:UK1:ST1-M", "location_kind": "dc", "location_code": "UK1", "sku_code": "ST1-M",
             "on_hand": 500, "allocated": 100, "reserved": 0, "in_transit": 0, "src_version": 7},
            {"source_id": "inv:store:S001:ST1-L", "location_kind": "store", "location_code": "S001",
             "sku_code": "ST1-L", "on_hand": 12, "allocated": 0, "reserved": 1, "in_transit": 4, "src_version": 3},
        ],
        "demand_forecast": forecast,
        "size_curve": [{"source_id": "sc:Outerwear:UK1-COLD-A:M", "family": "Outerwear",
                        "cluster_code": "UK1-COLD-A", "size": "M", "share_bp": 2500}],
        "allocation": [{"source_id": "alloc:AL-1", "code": "AL-1", "sku_code": "ST1-M", "dc_code": "UK1",
                        "store_code": "S001", "ship_week": "2026-W45", "qty": 24, "status": "planned",
                        "src_version": 1}],
        "transfer_order_line": [{"source_id": "TO-1#1", "to_code": "TO-1", "from_dc": "NL1", "to_dc": "UK1",
                                 "ship_date": D(2026, 10, 1), "eta_date": D(2026, 10, 3), "sku_code": "ST1-M",
                                 "qty": 40, "status": "shipped", "src_version": 1}],
        "online_order_line": [{"source_id": "OO-1#1", "order_code": "OO-1", "market": "GB", "channel": "online",
                               "store_code": None, "sku_code": "ST1-L", "qty": 1, "reserved_qty": 1,
                               "promise_date": D(2026, 11, 10), "status": "open", "src_version": 0}],
        "promotion": [{"source_id": "promo:BF", "code": "BF", "markets": ["GB", "IE"], "grades": ["A"],
                       "style_codes": ["ST1"], "period_start": D(2026, 11, 23), "period_end": D(2026, 11, 29),
                       "discount_bp": 2000, "uplift_bp": 3500, "src_version": 0}],
    }


def parquet_bytes(entity: str, rows: list[dict[str, Any]]) -> bytes:
    sch = schema(entity)
    arrays = [pa.array([r.get(c) for r in rows], type=f.type) for c, f in zip(sch.names, sch, strict=True)]
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_arrays(arrays, schema=sch), buf, compression="zstd")
    return buf.getvalue()


def bulk_forecast_parts(n: int, part_rows: int, *, stores: int = 451, weeks: int = 9) -> list[tuple[bytes, int]]:
    """``n`` forecast rows written column-wise (no per-row dicts), in parts of ``part_rows``."""
    out: list[tuple[bytes, int]] = []
    sch = schema("demand_forecast")
    week_names = [f"2026-W{40 + (w % weeks):02d}" for w in range(weeks)]
    for start in range(0, n, part_rows):
        idx = range(start, min(n, start + part_rows))
        cols = {
            "source_id": [f"fc:{i}" for i in idx],
            "style_code": ["ST1"] * len(idx),
            "location_kind": ["store"] * len(idx),
            "location_code": [f"S{(i % stores) % 3 + 1:03d}" for i in idx],
            "iso_week": [week_names[i % weeks] for i in idx],
            "units": [i % 97 for i in idx],
            "forecast_version": [3] * len(idx),
        }
        table = pa.Table.from_arrays([pa.array(cols[c], type=f.type) for c, f in zip(sch.names, sch, strict=True)],
                                     schema=sch)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd")
        out.append((buf.getvalue(), len(idx)))
    return out


@dataclass
class Snapshot:
    id: str
    parts: dict[str, list[tuple[bytes, int]]]
    watermark: str


@dataclass
class ScnState:
    dataset: dict[str, list[dict[str, Any]]] = field(default_factory=small_dataset)
    part_rows: int = 50_000
    bulk: dict[str, list[tuple[bytes, int]]] = field(default_factory=dict)
    """Pre-built parts that replace an entity's rows (perf runs)."""
    corrupt: set[str] = field(default_factory=set)
    """Entities whose first part is served with one flipped byte (sha256 mismatch)."""
    lie_rows: dict[str, int] = field(default_factory=dict)
    """Entity -> delta added to the manifest + export row counts (a consistent lie)."""
    foreign_host: str | None = None
    omit: set[str] = field(default_factory=set)
    open_delay: float = 0.0
    mutate: Callable[[dict[str, list[dict[str, Any]]]], None] | None = None
    changes: dict[str, Any] = field(default_factory=lambda: {"changes": [], "next_watermark": "ox:1"})
    tool_error: str | None = None
    open_calls: int = 0
    export_calls: int = 0
    downloads: list[str] = field(default_factory=list)
    changes_calls: list[str] = field(default_factory=list)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    seq: int = 0
    manifest_extra: dict[str, Any] = field(default_factory=dict)
    """M8: extra manifest keys (e.g. ``consistency: crawled`` + ``as_of_range`` of an adapter sidecar)."""

    def build(self) -> Snapshot:
        data = {e: [dict(r) for r in rows] for e, rows in self.dataset.items()}
        if self.mutate is not None:
            self.mutate(data)
        parts: dict[str, list[tuple[bytes, int]]] = {}
        for entity in SCN_ENTITIES:
            if entity in self.bulk:
                parts[entity] = list(self.bulk[entity])
                continue
            rows = data.get(entity, [])
            chunks = [rows[i:i + self.part_rows] for i in range(0, len(rows), self.part_rows)] or [[]]
            parts[entity] = [(parquet_bytes(entity, c), len(c)) for c in chunks]
        self.seq += 1
        snap = Snapshot(str(uuid.uuid4()), parts, f"ox:{self.seq}")
        self.snapshots[snap.id] = snap
        return snap


def build_mcp(state: ScnState, base_url: Callable[[], str]) -> FastMCP:
    mcp = FastMCP("scn-source-test")

    @mcp.tool(name="scn.snapshot_open", description="Open a canonical scn/1.0 snapshot.", annotations=READ)
    async def snapshot_open(entities: list[str]) -> dict[str, Any]:
        state.open_calls += 1
        if state.tool_error:
            raise ValueError(state.tool_error)
        if state.open_delay:
            await asyncio.sleep(state.open_delay)
        snap = state.build()
        now = dt.datetime.now(dt.UTC)
        manifest = {}
        for e in entities:
            if e in state.omit or e not in snap.parts:
                continue
            ps = snap.parts[e]
            manifest[e] = {"rows": sum(n for _, n in ps) + state.lie_rows.get(e, 0), "parts": len(ps),
                           "sha256": [hashlib.sha256(b).hexdigest() for b, _ in ps]}
        return {"snapshot_id": snap.id, "as_of": now.isoformat(), "watermark": snap.watermark,
                "canonical_model": "scn/1.0", "expires_at": (now + dt.timedelta(hours=2)).isoformat(),
                "entities": manifest, **state.manifest_extra}

    @mcp.tool(name="scn.snapshot_export", description="Signed part URLs of one entity.", annotations=READ)
    def snapshot_export(snapshot_id: str, entity: str) -> dict[str, Any]:
        state.export_calls += 1
        snap = state.snapshots[snapshot_id]
        base = base_url() if state.foreign_host is None else state.foreign_host
        parts = []
        for i, (data, n) in enumerate(snap.parts[entity], start=1):
            rows = n + (state.lie_rows.get(entity, 0) if i == 1 else 0)
            parts.append({"uri": f"{base}/v1/snapshots/{snapshot_id}/{entity}/part-{i:04d}.parquet?exp=9999999999"
                                 f"&sig={'0' * 64}",
                          "sha256": hashlib.sha256(data).hexdigest(), "rows": rows, "bytes": len(data)})
        return {"snapshot_id": snapshot_id, "entity": entity, "canonical_model": "scn/1.0", "parts": parts}

    @mcp.tool(name="scn.changes_since", description="Changes after a watermark.", annotations=READ)
    def changes_since(watermark: str) -> dict[str, Any]:
        state.changes_calls.append(watermark)
        return dict(state.changes)

    @mcp.custom_route("/v1/snapshots/{snap}/{entity}/{file}", methods=["GET"])
    async def download(request: Request) -> Response:
        snap_id, entity, file = (request.path_params[k] for k in ("snap", "entity", "file"))
        state.downloads.append(f"{entity}/{file}")
        snap = state.snapshots.get(snap_id)
        if snap is None or entity not in snap.parts:
            return JSONResponse({"error": "not_found"}, status_code=404)
        index = int(file.removeprefix("part-").removesuffix(".parquet"))
        data, _ = snap.parts[entity][index - 1]
        if entity in state.corrupt and index == 1:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        return Response(data, media_type="application/vnd.apache.parquet")

    return mcp


class ScnTestServer:
    def __init__(self, state: ScnState | None = None) -> None:
        self.state = state or ScnState()
        self.mcp = build_mcp(self.state, lambda: self.origin)
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[Any] | None = None
        self.port = 0

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/mcp"

    async def start(self) -> None:
        app = self.mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server._serve())
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        assert self.server.started, "scn test server did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=10)


class ForwardProxy:
    """Minimal HTTP forward proxy (absolute-form requests, no CONNECT) that records request lines —
    proves connector traffic goes through ``CONNECTOR_PROXY_URL``."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_w: asyncio.StreamWriter | None = None
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            line, _, rest = head.partition(b"\r\n")
            method, target, version = line.decode("latin-1").split(" ", 2)
            self.requests.append(f"{method} {target}")
            from urllib.parse import urlsplit

            u = urlsplit(target)
            up_r, upstream_w = await asyncio.open_connection(u.hostname, u.port or 80)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            upstream_w.write(f"{method} {path} {version}\r\n".encode("latin-1") + rest)
            await upstream_w.drain()

            async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, record: bool) -> None:
                try:
                    while True:
                        chunk = await src.read(65536)
                        if not chunk:
                            break
                        if record and chunk[:8].split(b" ")[0] in (b"GET", b"POST", b"DELETE", b"PUT"):
                            first = chunk.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                            self.requests.append(" ".join(first.split(" ")[:2]))
                        dst.write(chunk)
                        await dst.drain()
                finally:
                    dst.close()

            await asyncio.gather(pump(reader, upstream_w, True), pump(up_r, writer, False),
                                 return_exceptions=True)
        except Exception:  # noqa: BLE001 - test helper
            pass
        finally:
            writer.close()
            if upstream_w is not None:
                upstream_w.close()
