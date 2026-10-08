"""Canonical ``scn/1.0`` snapshot export (docs/contracts/25-scn-1.0.md, 22-merxa-m2.md §4).

``open_snapshot`` runs one ``REPEATABLE READ, READ ONLY`` transaction: it records the
watermark (``ox:<outbox seq>`` — see :mod:`merxa_api.services.events` — plus the WAL LSN)
and writes immutable Parquet files (pyarrow, zstd, ≤ 50 000 rows per part) to
``<SNAPSHOT_DIR>/<id>/<entity>/part-NNNN.parquet``. The manifest (rows, per-part sha256)
is stored in ``merxa.scn_snapshot``. ``export_urls`` returns HMAC-signed download URLs
(``/v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=<unix>&sig=<hex>``) that need no auth
header. Snapshots expire after ``SNAPSHOT_TTL_S`` (2 h); a housekeeping task deletes them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api import tables as t
from merxa_api.config import Settings
from merxa_api.errors import Problem, not_found
from merxa_api.logs import get_logger
from merxa_api.services import events
from merxa_api.services.common import jsonable

log = get_logger(__name__)

CANONICAL_MODEL = "scn/1.0"

COMPONENT_FAMILY_SQL = (
    "CASE WHEN strpos(lower(name), replace(lower(kind), '_', ' ')) > 0 THEN name "
    "ELSE name || ' (' || replace(lower(kind), '_', ' ') || ')' END"
)
"""``item.family`` of a component row (scn/1.0 has no item ``name`` column): the material's name
plus its kind when the name does not already say it ("Polyester wadding 120 gsm",
"Polyester teddy fleece (fleece fabric)"), so a consumer can match material words ("wadding",
"fleece", "nylon shell") against live data. Styles and SKUs carry the style family."""
PART_ROWS = 50_000
PART_RE = re.compile(r"^part-(\d{4})\.parquet$")
SIG_RE = re.compile(r"^[0-9a-f]{64}$")

S, I64, F64, D32 = pa.string(), pa.int64(), pa.float64(), pa.date32()
LS = pa.list_(pa.string())

# entity → (arrow schema, SQL producing the columns in schema order)
ENTITIES: dict[str, tuple[pa.Schema, str]] = {
    "location": (
        pa.schema(
            [
                ("source_id", S),
                ("kind", S),
                ("code", S),
                ("name", S),
                ("country", S),
                ("city", S),
                ("lat", F64),
                ("lon", F64),
                ("parent_code", S),
                ("grade", S),
                ("climate", S),
                ("unlocode", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT 'dc:' || code, 'dc', code, name, country, city, lat, lon, NULL, NULL, NULL, NULL, version
          FROM merxa.dc
        UNION ALL
        SELECT 'cluster:' || c.code, 'cluster', c.code, c.dc_code || ' ' || c.climate || ' ' || c.grade_band,
               d.country, NULL, NULL::float8, NULL::float8, c.dc_code, c.grade_band, c.climate, NULL, c.version
          FROM merxa.store_cluster c JOIN merxa.dc d ON d.code = c.dc_code
        UNION ALL
        SELECT 'store:' || s.code, 'store', s.code, s.name, s.market_code, s.city, s.lat, s.lon, s.cluster_code,
               s.grade, c.climate, NULL, s.version
          FROM merxa.store s JOIN merxa.store_cluster c ON c.code = s.cluster_code
        UNION ALL
        SELECT 'port:' || unlocode, 'port', unlocode, name, country, NULL, lat, lon, NULL, NULL, NULL, unlocode, version
          FROM merxa.port
        UNION ALL
        SELECT 'supplier:' || code, 'supplier', code, name, country, NULL, NULL, NULL, NULL, NULL, NULL, NULL, version
          FROM merxa.supplier
        UNION ALL
        SELECT 'factory:' || code, 'factory', code, name, country, NULL, NULL, NULL, supplier_code, NULL, NULL, NULL,
               version
          FROM merxa.factory
        ORDER BY 1
        """,
    ),
    "item": (
        pa.schema(
            [
                ("source_id", S),
                ("kind", S),
                ("code", S),
                ("style_code", S),
                ("family", S),
                ("colour", S),
                ("size", S),
                ("size_seq", I64),
                ("retail_price_cents", I64),
                ("unit_cost_cents", I64),
                ("currency", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT 'style:' || code, 'style', code, code, family, colour, NULL, NULL, retail_price_cents,
               unit_cost_cents, 'EUR', version
          FROM merxa.style
        UNION ALL
        SELECT 'sku:' || k.code, 'sku', k.code, k.style_code, s.family, s.colour, k.size, k.size_seq,
               s.retail_price_cents, s.unit_cost_cents, 'EUR', k.version
          FROM merxa.sku k JOIN merxa.style s ON s.code = k.style_code
        UNION ALL
        SELECT 'component:' || code, 'component', code, NULL, {COMPONENT_FAMILY_SQL}, NULL, NULL, NULL, NULL, NULL,
               NULL, version
          FROM merxa.component
        ORDER BY 1
        """.replace("{COMPONENT_FAMILY_SQL}", COMPONENT_FAMILY_SQL),
    ),
    "bom_line": (
        pa.schema(
            [
                ("source_id", S),
                ("style_code", S),
                ("component_code", S),
                ("supplier_code", S),
                ("factory_code", S),
                ("qty_per_unit", I64),
                ("src_version", I64),
            ]
        ),
        """
        SELECT b.style_code || '|' || b.component_code || '|' || coalesce(b.factory_code, '*'),
               b.style_code, b.component_code, c.supplier_code, b.factory_code, b.qty_per_unit, b.version
          FROM merxa.bom_line b JOIN merxa.component c ON c.code = b.component_code
         ORDER BY 1
        """,
    ),
    "lane": (
        pa.schema(
            [
                ("source_id", S),
                ("code", S),
                ("origin_code", S),
                ("dest_code", S),
                ("mode", S),
                ("carrier_code", S),
                ("transit_days", I64),
                ("via", LS),
                ("alt_lane_code", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT code, code, origin, dest, mode, carrier_code, transit_days, via, alt_lane_code, version
          FROM merxa.lane ORDER BY code
        """,
    ),
    "lane_rate": (
        pa.schema(
            [
                ("source_id", S),
                ("lane_code", S),
                ("valid_from", D32),
                ("valid_to", D32),
                ("basis", S),
                ("rate_cents", I64),
                ("weekly_capacity", I64),
                ("src_version", I64),
            ]
        ),
        """
        SELECT lane_code || '|' || lower(valid)::text, lane_code, lower(valid), upper(valid) - 1, basis, rate_cents,
               weekly_capacity, version
          FROM merxa.rate_card ORDER BY lane_code, lower(valid)
        """,
    ),
    "sailing": (
        pa.schema(
            [
                ("source_id", S),
                ("code", S),
                ("lane_code", S),
                ("cutoff", D32),
                ("etd", D32),
                ("eta", D32),
                ("capacity_teu", I64),
                ("status", S),
                ("src_version", I64),
            ]
        ),
        "SELECT code, code, lane_code, cutoff, etd, eta, capacity_teu, status, version"
        " FROM merxa.sailing ORDER BY code",
    ),
    "po_line": (
        pa.schema(
            [
                ("source_id", S),
                ("po_code", S),
                ("line_no", I64),
                ("supplier_code", S),
                ("factory_code", S),
                ("dest_dc_code", S),
                ("sku_code", S),
                ("qty_ordered", I64),
                ("qty_received", I64),
                ("ex_factory_date", D32),
                ("eta_dc", D32),
                ("status", S),
                ("shipment_code", S),
                ("sailing_code", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT l.po_code || '#' || l.line_no, l.po_code, l.line_no, p.supplier_code, p.factory_code, p.dest_dc_code,
               l.sku_code, l.qty_ordered, l.qty_received, p.ex_factory_date, l.eta_dc, l.status,
               sh.code, sh.sailing_code, l.version
          FROM merxa.po_line l
          JOIN merxa.purchase_order p ON p.code = l.po_code
          LEFT JOIN LATERAL (
                SELECT s.code, s.sailing_code
                  FROM merxa.shipment_line sl JOIN merxa.shipment s ON s.code = sl.shipment_code
                 WHERE sl.po_line_ref = l.po_code || '#' || l.line_no AND s.status <> 'cancelled'
                 ORDER BY s.code DESC LIMIT 1
          ) sh ON true
         ORDER BY l.po_code, l.line_no
        """,
    ),
    "production_order": (
        pa.schema(
            [
                ("source_id", S),
                ("code", S),
                ("po_line_ref", S),
                ("line_code", S),
                ("seq", I64),
                ("planned_start", D32),
                ("planned_end", D32),
                ("status", S),
                ("hold_reason", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT code, code, po_line_ref, line_code, seq, planned_start, planned_end, status, hold_reason, version
          FROM merxa.production_order ORDER BY code
        """,
    ),
    "inventory_position": (
        pa.schema(
            [
                ("source_id", S),
                ("location_kind", S),
                ("location_code", S),
                ("sku_code", S),
                ("on_hand", I64),
                ("allocated", I64),
                ("reserved", I64),
                ("in_transit", I64),
                ("src_version", I64),
            ]
        ),
        """
        SELECT location_type || '/' || location_code || '/' || sku_code, location_type, location_code, sku_code,
               on_hand, allocated, reserved, in_transit, version
          FROM merxa.inventory_position ORDER BY location_type, location_code, sku_code
        """,
    ),
    "demand_forecast": (
        pa.schema(
            [
                ("source_id", S),
                ("style_code", S),
                ("location_kind", S),
                ("location_code", S),
                ("iso_week", S),
                ("units", I64),
                ("forecast_version", I64),
            ]
        ),
        """
        SELECT style_code || '|' || coalesce(store_code, cluster_code) || '|' || iso_week, style_code,
               CASE WHEN store_code IS NULL THEN 'cluster' ELSE 'store' END, coalesce(store_code, cluster_code),
               iso_week, units, version
          FROM merxa.demand_forecast ORDER BY style_code, coalesce(store_code, cluster_code), iso_week
        """,
    ),
    "size_curve": (
        pa.schema([("source_id", S), ("family", S), ("cluster_code", S), ("size", S), ("share_bp", I64)]),
        """
        SELECT family || '|' || cluster_code || '|' || size, family, cluster_code, size, share_bp
          FROM merxa.size_curve ORDER BY family, cluster_code, size
        """,
    ),
    "allocation": (
        pa.schema(
            [
                ("source_id", S),
                ("code", S),
                ("sku_code", S),
                ("dc_code", S),
                ("store_code", S),
                ("ship_week", S),
                ("qty", I64),
                ("status", S),
                ("src_version", I64),
            ]
        ),
        "SELECT code, code, sku_code, dc_code, store_code, ship_week, qty, status, version"
        " FROM merxa.allocation ORDER BY code",
    ),
    "transfer_order_line": (
        pa.schema(
            [
                ("source_id", S),
                ("to_code", S),
                ("from_dc", S),
                ("to_dc", S),
                ("ship_date", D32),
                ("eta_date", D32),
                ("sku_code", S),
                ("qty", I64),
                ("status", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT l.to_code || '#' || l.line_no, l.to_code, o.from_dc, o.to_dc, o.ship_date, o.eta_date, l.sku_code,
               l.qty, o.status, l.version + o.version - 1
          FROM merxa.transfer_order_line l JOIN merxa.transfer_order o ON o.code = l.to_code
         ORDER BY l.to_code, l.line_no
        """,
    ),
    "online_order_line": (
        pa.schema(
            [
                ("source_id", S),
                ("order_code", S),
                ("market", S),
                ("channel", S),
                ("store_code", S),
                ("sku_code", S),
                ("qty", I64),
                ("reserved_qty", I64),
                ("promise_date", D32),
                ("status", S),
                ("src_version", I64),
            ]
        ),
        """
        SELECT l.order_code || '#' || l.line_no, l.order_code, o.market, o.channel, o.store_code, l.sku_code, l.qty,
               l.reserved_qty, o.promise_date, o.status, l.version + o.version - 1
          FROM merxa.online_order_line l JOIN merxa.online_order o ON o.code = l.order_code
         ORDER BY l.order_code, l.line_no
        """,
    ),
    "promotion": (
        pa.schema(
            [
                ("source_id", S),
                ("code", S),
                ("markets", LS),
                ("grades", LS),
                ("style_codes", LS),
                ("period_start", D32),
                ("period_end", D32),
                ("discount_bp", I64),
                ("uplift_bp", I64),
                ("src_version", I64),
            ]
        ),
        """
        SELECT code, code, markets, grades, style_codes, lower(period), upper(period) - 1, discount_bp, uplift_bp,
               version
          FROM merxa.promotion ORDER BY code
        """,
    ),
}
ENTITY_NAMES: tuple[str, ...] = tuple(ENTITIES)


def _write_part(path: Path, schema: pa.Schema, rows: list[Any]) -> dict[str, Any]:
    columns = list(zip(*rows, strict=True)) if rows else [[] for _ in schema.names]
    table = pa.Table.from_arrays(
        [pa.array(list(col), type=f.type) for col, f in zip(columns, schema, strict=True)], schema=schema
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    data = tmp.read_bytes()
    tmp.replace(path)
    path.chmod(0o444)
    return {"rows": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class SnapshotService:
    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.root = Path(settings.snapshot_dir)
        secret = settings.snapshot_secret
        if not secret:
            log.warning("MERXA_SNAPSHOT_SECRET is not set; signed snapshot URLs are valid for this process only")
            secret = secrets.token_urlsafe(32)
        self._secret = secret.encode("utf-8")
        # One build at a time: each holds up to PART_ROWS fetched rows plus their Arrow table
        # (tens of MB for `full`); parallel builds would exceed the container's memory limit.
        self._build_slot = asyncio.Semaphore(1)

    # ── open ──
    async def open_snapshot(self, entities: Any, *, created_by: str | None = None) -> dict[str, Any]:
        if not isinstance(entities, list) or not entities or not all(isinstance(e, str) for e in entities):
            raise Problem(400, "invalid_parameter", f"entities must be a non-empty list of {list(ENTITY_NAMES)}")
        unknown = sorted(set(entities) - set(ENTITY_NAMES))
        if unknown:
            raise Problem(400, "unknown_entity", f"unknown scn/1.0 entities {unknown}; known: {list(ENTITY_NAMES)}")
        wanted = [e for e in ENTITY_NAMES if e in set(entities)]
        async with self._build_slot:
            return await self._build(wanted, created_by)

    async def _build(self, wanted: list[str], created_by: str | None) -> dict[str, Any]:
        snap_id = uuid.uuid4()
        base = self.root / str(snap_id)
        started = time.perf_counter()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.settings.snapshot_ttl_s)
        manifest: dict[str, Any] = {}
        try:
            async with self.engine.connect() as conn:
                conn = await conn.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
                async with conn.begin():
                    watermark = await events.current_watermark(conn)
                    try:
                        lsn = (await conn.execute(text("SELECT pg_current_wal_lsn()::text"))).scalar_one()
                    except Exception:  # pragma: no cover - restricted roles
                        lsn = None
                    raw = (await conn.get_raw_connection()).driver_connection
                    for entity in wanted:
                        schema, sql = ENTITIES[entity]
                        parts: list[dict[str, Any]] = []
                        cursor = await raw.cursor(sql)
                        n = 0
                        while True:
                            rows = await cursor.fetch(PART_ROWS)
                            if not rows and n > 0:
                                break
                            n += 1
                            name = f"part-{n:04d}.parquet"
                            info = await asyncio.to_thread(
                                _write_part, base / entity / name, schema, [tuple(r) for r in rows]
                            )
                            parts.append({"file": name, **info})
                            if len(rows) < PART_ROWS:
                                break
                        manifest[entity] = {"rows": sum(p["rows"] for p in parts), "parts": parts}
            async with self.engine.begin() as conn:
                await conn.execute(
                    t.scn_snapshot.insert().values(
                        id=snap_id,
                        expires_at=expires,
                        as_of=now,
                        watermark=events.format_watermark(watermark),
                        wal_lsn=lsn,
                        manifest={"canonical_model": CANONICAL_MODEL, "entities": manifest},
                        created_by=created_by,
                    )
                )
        except OSError as exc:
            shutil.rmtree(base, ignore_errors=True)
            log.error("snapshot storage not writable", extra={"dir": str(self.root), "error": str(exc)})
            raise Problem(503, "snapshot_storage_unavailable", "snapshot storage is not writable") from exc
        except BaseException:
            shutil.rmtree(base, ignore_errors=True)
            raise
        log.info(
            "scn snapshot written",
            extra={"snapshot": str(snap_id), "entities": wanted, "ms": round((time.perf_counter() - started) * 1000)},
        )
        return {
            "snapshot_id": str(snap_id),
            "as_of": jsonable(now),
            "watermark": events.format_watermark(watermark),
            "wal_lsn": lsn,
            "canonical_model": CANONICAL_MODEL,
            "expires_at": jsonable(expires),
            "entities": {
                e: {"rows": m["rows"], "parts": len(m["parts"]), "sha256": [p["sha256"] for p in m["parts"]]}
                for e, m in manifest.items()
            },
        }

    async def _load(self, snapshot_id: str) -> Any:
        try:
            sid = uuid.UUID(str(snapshot_id))
        except ValueError as exc:
            raise not_found("snapshot", snapshot_id) from exc
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(t.scn_snapshot).where(t.scn_snapshot.c.id == sid))).first()
        if row is None or row.expires_at <= datetime.now(UTC):
            raise not_found("snapshot", snapshot_id)
        return row

    # ── export ──
    async def export_urls(self, snapshot_id: str, entity: str) -> dict[str, Any]:
        row = await self._load(snapshot_id)
        entry = (row.manifest.get("entities") or {}).get(entity)
        if entry is None:
            raise Problem(404, "not_found", f"entity {entity!r} is not part of snapshot {snapshot_id}")
        exp = int(row.expires_at.timestamp())
        base = self.settings.public_url.rstrip("/")
        parts = []
        for p in entry["parts"]:
            path = f"{row.id}/{entity}/{p['file']}"
            parts.append(
                {
                    "uri": f"{base}/v1/snapshots/{path}?exp={exp}&sig={self.sign(path, exp)}",
                    "sha256": p["sha256"],
                    "rows": p["rows"],
                    "bytes": p["bytes"],
                }
            )
        return {
            "snapshot_id": str(row.id),
            "entity": entity,
            "canonical_model": CANONICAL_MODEL,
            "rows": entry["rows"],
            "expires_at": jsonable(row.expires_at),
            "parts": parts,
        }

    def sign(self, path: str, exp: int) -> str:
        return hmac.new(self._secret, f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()

    def resolve_download(self, snapshot_id: str, entity: str, file: str, exp: str | None, sig: str | None) -> Path:
        """Check the signature/expiry of a download URL and return the file path."""
        try:
            sid = str(uuid.UUID(snapshot_id))
        except ValueError as exc:
            raise Problem(404, "not_found", "no such snapshot file") from exc
        if entity not in ENTITY_NAMES or not PART_RE.match(file):
            raise Problem(404, "not_found", "no such snapshot file")
        if not exp or not sig or not exp.isascii() or not exp.isdigit() or len(exp) > 12:
            raise Problem(403, "invalid_signature", "exp and sig are required")
        if not SIG_RE.match(sig):  # also keeps non-ASCII out of hmac.compare_digest (TypeError → 500)
            raise Problem(403, "invalid_signature", "signature does not match")
        path = f"{sid}/{entity}/{file}"
        if not hmac.compare_digest(self.sign(path, int(exp)), sig):
            raise Problem(403, "invalid_signature", "signature does not match")
        if int(exp) < int(time.time()):
            raise Problem(403, "url_expired", "this download URL has expired")
        full = self.root / sid / entity / file
        if not full.is_file():
            raise Problem(404, "not_found", "no such snapshot file")
        return full

    # ── housekeeping ──
    async def cleanup(self) -> int:
        now = datetime.now(UTC)
        async with self.engine.begin() as conn:
            gone = [
                str(r.id)
                for r in await conn.execute(
                    delete(t.scn_snapshot).where(t.scn_snapshot.c.expires_at <= now).returning(t.scn_snapshot.c.id)
                )
            ]
            live = {str(r.id) for r in await conn.execute(select(t.scn_snapshot.c.id))}
        removed = 0
        if self.root.is_dir():
            cutoff = time.time() - self.settings.snapshot_ttl_s
            for d in self.root.iterdir():
                if not d.is_dir():
                    continue
                stale_orphan = d.name not in live and d.stat().st_mtime < cutoff
                if d.name in gone or stale_orphan:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
        return removed
