"""A full-sized ``scn/1.0`` snapshot in a throwaway Postgres (``80-m3-deterministic-core.md`` §1).

* :data:`DDL` — the entity tables of the platform migration ``migrations/0004_scn.sql`` (the
  section between its "scn/1.0 entities" and "RLS" headers) when the file exists, else an
  equivalent built from ``SCN_COLUMNS``: one ``scn_<entity>`` table per entity with the
  ``25-scn-1.0`` columns + ``tenant`` + ``snapshot_id``, PK ``(tenant, snapshot_id, source_id)``.
* :func:`full_snapshot` — rows sized like Merxa's ``full`` profile and **derived from**
  :class:`fakes.merxa_full.FullMerxaStub` (same DCs, clusters, 451 stores, 180 SKUs, store
  inventory, POs, cluster forecast — split to stores here, 121,770 rows), plus the M3 entities
  (ports, factories, BOM, lanes + rate cards like Merxa's, sailings, production orders,
  ~93k store allocations). The SQL path can therefore be checked against the MCP path.
* :class:`ScnDatabase` — creates a fresh database on ``SCN_TEST_ADMIN_DSN`` (fallback
  ``E1_TEST_ADMIN_DSN``), COPYs a snapshot in and hands out :class:`SessionReader` sessions.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from keelson_contracts.scn import SCN_COLUMNS

from keelson_engine.runtime.scn_store import SessionReader

from .merxa_full import CLUSTERS, DCS, SIZES, STYLES, FullMerxaStub

ADMIN_DSN = os.environ.get("SCN_TEST_ADMIN_DSN") or os.environ.get(
    "E1_TEST_ADMIN_DSN", "postgresql://postgres:test@e1-test-pg:5432/postgres"
)

_TYPES: dict[str, str] = {
    "lat": "double precision", "lon": "double precision",
    "size_seq": "bigint", "retail_price_cents": "bigint", "unit_cost_cents": "bigint", "qty_per_unit": "bigint",
    "transit_days": "bigint", "rate_cents": "bigint", "weekly_capacity": "bigint", "capacity_teu": "bigint",
    "line_no": "bigint", "qty_ordered": "bigint", "qty_received": "bigint", "seq": "bigint", "on_hand": "bigint",
    "allocated": "bigint", "reserved": "bigint", "in_transit": "bigint", "units": "bigint", "share_bp": "bigint",
    "qty": "bigint", "reserved_qty": "bigint", "discount_bp": "bigint", "uplift_bp": "bigint",
    "src_version": "bigint", "forecast_version": "bigint",
    "valid_from": "date", "valid_to": "date", "cutoff": "date", "etd": "date", "eta": "date",
    "ex_factory_date": "date", "eta_dc": "date", "planned_start": "date", "planned_end": "date",
    "ship_date": "date", "eta_date": "date", "promise_date": "date", "period_start": "date", "period_end": "date",
    "via": "text[]", "markets": "text[]", "grades": "text[]", "style_codes": "text[]",
}
_INDEXES: dict[str, tuple[str, ...]] = {
    "location": ("kind", "code"), "item": ("kind", "code"), "po_line": ("po_code", "line_no"),
    "inventory_position": ("location_kind", "location_code", "sku_code"),
    "demand_forecast": ("iso_week", "location_code"), "allocation": ("sku_code", "dc_code", "ship_week"),
    "production_order": ("line_code", "seq"), "lane": ("code",), "sailing": ("code",),
}


def ddl() -> str:
    out: list[str] = []
    for entity, cols in SCN_COLUMNS.items():
        defs = ["tenant text NOT NULL", "snapshot_id uuid NOT NULL"]
        defs += [f"{c} {_TYPES.get(c, 'text')}" + (" NOT NULL" if c == "source_id" else "") for c in cols]
        defs.append("PRIMARY KEY (tenant, snapshot_id, source_id)")
        out.append(f"CREATE TABLE scn_{entity} ({', '.join(defs)});")
        if entity in _INDEXES:
            out.append(f"CREATE INDEX ON scn_{entity} (tenant, snapshot_id, {', '.join(_INDEXES[entity])});")
    return "\n".join(out)


MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "0004_scn.sql"


def migration_entity_ddl() -> str | None:
    """The ``scn_<entity>`` tables + indexes of the platform migration (no RLS, no registry)."""
    try:
        text = MIGRATION.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"--[^\n]*scn/1\.0 entities[^\n]*\n(.*?)\n--[^\n]*RLS", text, re.S)
    return m.group(1) if m and "CREATE TABLE" in m.group(1) else None


DDL = migration_entity_ddl() or ddl()
DDL_SOURCE = "migration" if migration_entity_ddl() else "fixture"

DC_COORDS = {"UK1": (51.4935, 0.3529), "NL1": (51.3704, 6.1724), "DE1": (50.9848, 11.0299), "PL1": (52.4064, 16.9252)}
PORTS = (
    ("BDCGP", "Chittagong", "BD", 22.3089, 91.80), ("LKCMB", "Colombo", "LK", 6.9497, 79.8428),
    ("VNSGN", "Ho Chi Minh City", "VN", 10.7626, 106.788), ("GBFXT", "Felixstowe", "GB", 51.9563, 1.3301),
    ("NLRTM", "Rotterdam", "NL", 51.9496, 4.1453), ("DEHAM", "Hamburg", "DE", 53.5395, 9.9663),
    ("PLGDN", "Gdansk", "PL", 54.4013, 18.6763), ("AEJEA", "Jebel Ali", "AE", 25.0115, 55.0618),
    ("TRIST", "Istanbul", "TR", 40.9667, 28.6833), ("MATNG", "Tanger Med", "MA", 35.8833, -5.5),
)
SUPPLIERS = (("TRIM-BD-01", "BD"), ("SUP-VN-01", "VN"), ("CMT-BD-01", "BD"), ("CMT-TR-01", "TR"),
             ("CMT-MA-01", "MA"), ("FAB-CN-01", "CN"), ("TRIM-TR-01", "TR"))
FACTORIES = (("F-BD-01", "BD", "CMT-BD-01"), ("F-VN-01", "VN", "SUP-VN-01"), ("F-TR-01", "TR", "CMT-TR-01"),
             ("F-MA-01", "MA", "CMT-MA-01"))
# code, origin, dest, mode, transit, via, alt  (Merxa's lane table, 22-merxa-m2 §1)
LANES = (
    ("CGP-FXT-SEA", "BDCGP", "GBFXT", "sea", 32, ["LKCMB", "CAPE"], "CGP-UK1-SA"),
    ("CGP-RTM-SEA", "BDCGP", "NLRTM", "sea", 32, ["LKCMB", "CAPE"], "CGP-NL1-SA"),
    ("CGP-UK1-SA", "BDCGP", "UK1", "sea_air", 16, ["AEJEA"], "CGP-FXT-SEA"),
    ("CGP-NL1-SA", "BDCGP", "NL1", "sea_air", 16, ["AEJEA"], "CGP-RTM-SEA"),
    ("SGN-HAM-SEA", "VNSGN", "DEHAM", "sea", 35, ["CAPE"], "SGN-DE1-SA"),
    ("SGN-GDN-SEA", "VNSGN", "PLGDN", "sea", 36, ["CAPE"], "SGN-PL1-SA"),
    ("SGN-DE1-SA", "VNSGN", "DE1", "sea_air", 15, ["AEJEA"], "SGN-HAM-SEA"),
    ("SGN-PL1-SA", "VNSGN", "PL1", "sea_air", 16, ["AEJEA"], "SGN-GDN-SEA"),
    ("FXT-UK1-RD", "GBFXT", "UK1", "road", 1, [], None),
    ("RTM-NL1-RD", "NLRTM", "NL1", "road", 1, [], None),
    ("HAM-DE1-RD", "DEHAM", "DE1", "road", 1, [], None),
    ("HAM-PL1-RD", "DEHAM", "PL1", "road", 2, [], None),
    ("GDN-PL1-RD", "PLGDN", "PL1", "road", 1, [], None),
    ("GDN-DE1-RD", "PLGDN", "DE1", "road", 2, [], None),
    ("IST-DE1-RD", "TRIST", "DE1", "road", 5, [], None),
    ("IST-NL1-RD", "TRIST", "NL1", "road", 6, [], None),
    ("TNG-NL1-RD", "MATNG", "NL1", "road", 5, [], None),
    ("TNG-UK1-RD", "MATNG", "UK1", "road", 6, [], None),
    ("DE1-NL1-RD", "DE1", "NL1", "road", 2, [], None),
)
SEASON = (dt.date(2026, 7, 1), dt.date(2027, 2, 28))
ROAD = {"FXT-UK1-RD": (18, 60000), "RTM-NL1-RD": (16, 60000), "HAM-DE1-RD": (20, 50000), "HAM-PL1-RD": (32, 40000),
        "GDN-PL1-RD": (15, 40000), "GDN-DE1-RD": (30, 40000), "IST-DE1-RD": (55, 15000), "IST-NL1-RD": (62, 15000),
        "TNG-NL1-RD": (58, 15000), "TNG-UK1-RD": (70, 12000), "DE1-NL1-RD": (35, 20000)}
SEA_TEU = {"CGP-FXT-SEA": (380000, 400), "CGP-RTM-SEA": (370000, 400), "SGN-HAM-SEA": (350000, 350),
           "SGN-GDN-SEA": (360000, 300)}
SEA_AIR = {"CGP-UK1-SA": (145, 26000, 185, 19500), "CGP-NL1-SA": (140, 20000, 180, 15000),
           "SGN-DE1-SA": (160, 20000, 200, 15000), "SGN-PL1-SA": (165, 20000, 205, 15000)}
SAILINGS = (("SAIL-0001", "CGP-FXT-SEA"), ("SAIL-0002", "CGP-RTM-SEA"), ("SAIL-0003", "SGN-HAM-SEA"),
            ("SAIL-0004", "SGN-GDN-SEA"))
ALLOC_WEEKS = ("2026-W41", "2026-W42")
INFERRED_PO = "PO-VN-0009"
"""Its lines have no sailing: the inbound lane is inferred from the transit time (sea-air to DE1)."""


def _split(units: int, n: int, salt: int) -> list[int]:
    """Deterministic uneven split of ``units`` over ``n`` stores (sums exactly)."""
    weights = [1 + (salt * 7 + i * 5) % 4 for i in range(n)]
    total = sum(weights)
    base = [units * w // total for w in weights]
    for i in sorted(range(n), key=lambda i: (-(units * weights[i] % total), i))[: units - sum(base)]:
        base[i] += 1
    return base


def full_snapshot(stub: FullMerxaStub | None = None) -> dict[str, list[tuple[Any, ...]]]:
    """Rows per entity (tuples in ``SCN_COLUMNS`` order)."""
    stub = stub or FullMerxaStub()
    rows: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    loc = rows["location"]
    for dc in DCS:
        lat, lon = DC_COORDS[dc]
        loc.append((f"dc:{dc}", "dc", dc, f"{dc} DC", dc[:2], None, lat, lon, None, None, None, None, 1))
    stores_by_cluster: dict[str, list[str]] = defaultdict(list)
    for cl, dc in CLUSTERS:
        loc.append((f"cluster:{cl}", "cluster", cl, cl, dc[:2], None, None, None, dc, cl.split("-")[2],
                    cl.split("-")[1].lower(), None, 1))
    for i, store in enumerate(stub.stores):
        lat, lon = DC_COORDS[store["home_dc"]]
        lat += ((i * 37) % 200 - 100) / 100.0
        lon += ((i * 53) % 200 - 100) / 100.0
        stores_by_cluster[store["cluster"]].append(store["code"])
        loc.append((f"store:{store['code']}", "store", store["code"], f"Store {i}", "XX", None, round(lat, 4),
                    round(lon, 4), store["cluster"], "A", "mild", None, 1))
    for code, name, country, lat, lon in PORTS:
        loc.append((f"port:{code}", "port", code, name, country, None, lat, lon, None, None, None, code, 1))
    loc.append(("airport:LHR", "airport", "LHR", "London Heathrow", "GB", None, 51.47, -0.4543, None, None, None,
                "GBLHR", 1))
    for code, country in SUPPLIERS:
        loc.append((f"supplier:{code}", "supplier", code, code, country, None, None, None, None, None, None, None, 1))
    for code, country, supplier in FACTORIES:
        loc.append((f"factory:{code}", "factory", code, code, country, None, None, None, supplier, None, None, None, 1))

    item = rows["item"]
    for style, family, price, cost in STYLES:
        item.append((f"style:{style}", "style", style, style, family, "black", None, None, price, cost, "EUR", 1))
        for seq, size in enumerate(SIZES, start=1):
            code = f"{style}-{size}"
            item.append((f"sku:{code}", "sku", code, style, family, "black", size, seq, price, cost, "EUR", 1))

    bom = rows["bom_line"]
    for style, family, *_ in STYLES:
        if family == "Outerwear":
            entries = [("CMP-SHL-01", "FAB-CN-01", None), ("CMP-WAD-01", "TRIM-BD-01", "F-BD-01"),
                       ("CMP-WAD-03", "TRIM-TR-01", "F-TR-01"), ("CMP-WAD-03", "TRIM-TR-01", "F-MA-01")]
        else:
            entries = [("CMP-YRN-01", "SUP-VN-01", "F-VN-01"), ("CMP-YRN-02", "CMT-TR-01", "F-TR-01")]
        for comp, supplier, factory in entries:
            bom.append((f"{style}|{comp}|{factory or '*'}", style, comp, supplier, factory, 1, 1))

    for code, origin, dest, mode, transit, via, alt in LANES:
        rows["lane"].append((code, code, origin, dest, mode, "CARR", transit, via, alt, 1))
    for code, (rate, cap) in ROAD.items():
        rows["lane_rate"].append((f"{code}|{SEASON[0]}", code, SEASON[0], SEASON[1], "per_unit", rate, cap, 1))
    for code, (rate, cap) in SEA_TEU.items():
        rows["lane_rate"].append((f"{code}|{SEASON[0]}", code, SEASON[0], SEASON[1], "per_teu", rate, cap, 1))
    peak = dt.date(2026, 11, 1)
    for code, (std, cap, pk, pcap) in SEA_AIR.items():
        rows["lane_rate"].append((f"{code}|{SEASON[0]}", code, SEASON[0], peak - dt.timedelta(days=1), "per_unit", std, cap, 1))
        rows["lane_rate"].append((f"{code}|{peak}", code, peak, SEASON[1], "per_unit", pk, pcap, 1))
    for code, lane in SAILINGS:
        rows["sailing"].append((code, code, lane, dt.date(2026, 10, 6), dt.date(2026, 10, 8), dt.date(2026, 11, 9), 1200,
                                "scheduled", 1))

    sailing_for = {("F-BD-01", "UK1"): "SAIL-0001", ("F-BD-01", "NL1"): "SAIL-0002",
                   ("F-VN-01", "DE1"): "SAIL-0003", ("F-VN-01", "PL1"): "SAIL-0004"}
    lines_by_factory: dict[str, list[str]] = {"F-BD-01": ["FL-BD01-A", "FL-BD01-B"], "F-VN-01": ["FL-VN01-A"]}
    seq_by_line: dict[str, int] = defaultdict(int)
    for po_code, po in sorted(stub.pos.items()):
        for line in po["lines"]:
            ref = f"{po_code}#{line['line_no']}"
            sailing = sailing_for.get((po["factory_code"], po["dest_dc"]))
            ex_factory = dt.date.fromisoformat(po["ex_factory_date"])
            if po_code == INFERRED_PO:  # no sailing: the lane is inferred from eta − ex_factory (15 days ⇒ sea-air)
                sailing = None
                ex_factory = dt.date.fromisoformat(line["eta_dc"]) - dt.timedelta(days=15)
            rows["po_line"].append(
                (ref, po_code, line["line_no"], po["supplier_code"], po["factory_code"], po["dest_dc"], line["sku"],
                 line["qty_ordered"], line["qty_received"], ex_factory,
                 dt.date.fromisoformat(line["eta_dc"]), line["status"], f"SHP-{po_code}", sailing, line["version"])
            )
            fl = lines_by_factory[po["factory_code"]]
            line_code = fl[line["line_no"] % len(fl)]
            seq_by_line[line_code] += 1
            seq = seq_by_line[line_code]
            start = dt.date(2026, 9, 20) + dt.timedelta(days=3 * seq)
            status = "done" if line["status"] == "received" else ("running" if seq == 1 else "planned")
            rows["production_order"].append(
                (f"MO-{len(rows['production_order']) + 1:06d}", f"MO-{len(rows['production_order']) + 1:06d}", ref,
                 line_code, seq, start, start + dt.timedelta(days=2), status, None, 1)
            )

    for r in stub.dc_rows:
        rows["inventory_position"].append(
            (f"dc/{r['location_code']}/{r['sku']}", "dc", r["location_code"], r["sku"], r["on_hand"], r["allocated"],
             r["reserved"], r["in_transit"], 1)
        )
    for s_idx, store in enumerate(stub.stores):
        for k_idx, (sku, _) in enumerate(stub.skus):
            rows["inventory_position"].append(
                (f"store/{store['code']}/{sku}", "store", store["code"], sku, 2 + (s_idx * 7 + k_idx * 3) % 9,
                 (s_idx + k_idx) % 2, 0, 0, 1)
            )

    for n, r in enumerate(stub.forecast):
        stores = stores_by_cluster[r["cluster_code"]]
        for store, units in zip(stores, _split(r["units"], len(stores), n), strict=True):
            rows["demand_forecast"].append(
                (f"{r['style_code']}|{store}|{r['iso_week']}", r["style_code"], "store", store, r["iso_week"], units, 1)
            )
    for c in stub.size_curves:
        rows["size_curve"].append(
            (f"{c['family']}|{c['cluster_code']}|{c['size']}", c["family"], c["cluster_code"], c["size"], c["share_bp"])
        )

    n_alloc = 0
    for s_idx, store in enumerate(stub.stores):
        for k_idx, (sku, _) in enumerate(stub.skus):
            if (s_idx + k_idx) % 7 >= 4:
                continue
            for week in ALLOC_WEEKS:
                n_alloc += 1
                code = f"AL-{n_alloc:07d}"
                rows["allocation"].append(
                    (code, code, sku, store["home_dc"], store["code"], week, 1 + (s_idx + 2 * k_idx) % 5,
                     "released" if week == "2026-W41" else "planned", 1 + (s_idx % 3 == 0))
                )
    # one DC-level allocation (no store) must be ignored; one cancelled must be ignored
    rows["allocation"].append(("AL-DCONLY", "AL-DCONLY", stub.skus[0][0], "UK1", None, "2026-W42", 50, "planned", 1))
    rows["allocation"].append(("AL-CANCEL", "AL-CANCEL", stub.skus[0][0], "UK1", stub.stores[0]["code"], "2026-W42",
                               70, "cancelled", 1))
    rows["transfer_order_line"].append(("TO-1#1", "TO-1", "DE1", "NL1", dt.date(2026, 10, 12), None, stub.skus[0][0], 10,
                                        "planned", 1))
    rows["online_order_line"].append(("OO-1#1", "OO-1", "GB", "online", None, stub.skus[0][0], 1, 1, None, "open", 1))
    rows["promotion"].append(("PROMO-1", "PROMO-1", ["GB"], ["A"], [STYLES[0][0]], dt.date(2026, 11, 23),
                              dt.date(2026, 11, 29), 3000, 4000, 1))
    for entity, cols in SCN_COLUMNS.items():
        for row in rows.get(entity, []):
            assert len(row) == len(cols), (entity, row)
    return dict(rows)


class ScnDatabase:
    """A fresh database with the ``scn_*`` tables on the admin DSN (dropped by :meth:`close`)."""

    def __init__(self, admin_dsn: str = ADMIN_DSN) -> None:
        self.admin_dsn = admin_dsn
        self.name = f"er_scn_{uuid.uuid4().hex[:10]}"
        self.engine: Any = None

    @property
    def dsn(self) -> str:
        return self.admin_dsn.rsplit("/", 1)[0] + f"/{self.name}"

    async def open(self) -> ScnDatabase:
        import asyncpg  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        admin = await asyncpg.connect(self.admin_dsn, timeout=5)
        try:
            await admin.execute(f'CREATE DATABASE "{self.name}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(self.dsn)
        try:
            await conn.execute(DDL)
        finally:
            await conn.close()
        self.engine = create_async_engine("postgresql+asyncpg://" + self.dsn.split("://", 1)[1], pool_size=2)
        return self

    async def load(self, tenant: str, snapshot_id: str, rows: dict[str, Iterable[tuple[Any, ...]]]) -> None:
        import asyncpg  # noqa: PLC0415

        conn = await asyncpg.connect(self.dsn)
        try:
            for entity, cols in SCN_COLUMNS.items():
                records = [(tenant, uuid.UUID(snapshot_id), *r) for r in rows.get(entity, [])]
                if records:
                    await conn.copy_records_to_table(
                        f"scn_{entity}", records=records, columns=["tenant", "snapshot_id", *cols]
                    )
            await conn.execute("ANALYZE")
        finally:
            await conn.close()

    @contextlib.asynccontextmanager
    async def reader(self, tenant: str) -> AsyncIterator[SessionReader]:
        async with self.engine.connect() as conn:
            yield SessionReader(conn)

    async def close(self) -> None:
        import asyncpg  # noqa: PLC0415

        if self.engine is not None:
            await self.engine.dispose()
        admin = await asyncpg.connect(self.admin_dsn, timeout=5)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        finally:
            await admin.close()

