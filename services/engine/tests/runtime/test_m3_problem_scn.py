"""ProblemData v2 from a full-sized ``scn/1.0`` snapshot in Postgres (``80-m3-deterministic-core.md`` §2, §4).

The snapshot is derived from the same ground truth as :class:`fakes.merxa_full.FullMerxaStub`, so
the SQL path must produce *exactly* the M1 fields the MCP gatherer produces (store forecast and
store inventory are aggregated to clusters in SQL here, by Merxa there), plus correct v2 lists:
cluster allocation groups (with per-store expansion), chained inbound lanes with rate-card terms,
PO-line lanes (sailing or transit inference), production lines, derived alternate sources and map
locations. Build time < 2 s on the full-sized snapshot.

Needs a throwaway Postgres (``SCN_TEST_ADMIN_DSN`` / ``E1_TEST_ADMIN_DSN``); skipped without one.
"""

from __future__ import annotations

import datetime as dt
import math
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fakes.core import LANES, FakeCore
from fakes.merxa_full import CLUSTERS, N_STORES, FullMerxaStub
from fakes.scn_fixture import ADMIN_DSN, INFERRED_PO, ScnDatabase, full_snapshot
from fastmcp.client.transports import FastMCPTransport
from keelson_contracts.optimizer import ProblemData
from keelson_contracts.scn import SCN_COLUMNS, parse_row

from keelson_engine.runtime.commit import split_allocation
from keelson_engine.runtime.geo import GeoCatalog, e6, get_geo
from keelson_engine.runtime.mcp_client import McpGateway
from keelson_engine.runtime.problem import gather_problem_data
from keelson_engine.runtime.problem_scn import (
    GROUP_ALLOC_PREFIX,
    build_problem_from_snapshot,
    compose_inbound_lanes,
    expand_allocation_group,
    parse_allocation_group,
    receipts_for_entity,
)
from keelson_engine.runtime.scn_store import SnapshotRef
from keelson_engine.runtime.spec import AltSourcingDefaults, PlanningDefaults

pytestmark = pytest.mark.asyncio(loop_scope="module")

TENANT = "demo-retail"
AS_OF = dt.date(2026, 10, 5)
MAX_BUILD_S = 2.0


class Env:
    def __init__(self, db: ScnDatabase, rows: dict[str, list[tuple[Any, ...]]], snapshot: SnapshotRef,
                 stub: FullMerxaStub) -> None:
        self.db, self.rows, self.snapshot, self.stub = db, rows, snapshot, stub

    def dicts(self, entity: str) -> list[dict[str, Any]]:
        cols = SCN_COLUMNS[entity]
        return [dict(zip(cols, r, strict=True)) for r in self.rows[entity]]

    async def build(self, **kw: Any) -> Any:
        async with self.db.reader(TENANT) as reader:
            return await build_problem_from_snapshot(
                TENANT, reader, snapshot=self.snapshot, lanes=LANES, as_of=AS_OF, horizon_weeks=9, **kw
            )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[Env]:
    import asyncpg

    try:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no throwaway Postgres for the scn tests ({type(exc).__name__}); set SCN_TEST_ADMIN_DSN")
    await conn.close()
    stub = FullMerxaStub()
    rows = full_snapshot(stub)
    db = await ScnDatabase().open()
    snapshot_id = str(uuid.uuid4())
    other = str(uuid.uuid4())
    try:
        await db.load(TENANT, snapshot_id, rows)
        # noise that must never leak in: another tenant and an older snapshot of this tenant
        await db.load("northwind-test", snapshot_id, {"location": rows["location"][:10], "po_line": rows["po_line"]})
        await db.load(TENANT, other, {"po_line": rows["po_line"][:3], "allocation": rows["allocation"][:50]})
        yield Env(db, rows, SnapshotRef(id=snapshot_id, connector="merxa", as_of=AS_OF), stub)
    finally:
        await db.close()


async def test_tables_match_the_platform_migration(env: Env) -> None:
    """The fixture loads the platform's own ``0004_scn.sql`` entity DDL; its column order and types
    equal the contract (``SCN_COLUMNS``) and the fixture's fallback DDL."""
    import asyncpg
    from fakes.scn_fixture import DDL_SOURCE, ddl

    assert DDL_SOURCE == "migration"
    conn = await asyncpg.connect(env.db.dsn)
    try:
        await conn.execute("CREATE SCHEMA fallback")
        await conn.execute("SET search_path = fallback")
        await conn.execute(ddl())
        rows = await conn.fetch(
            "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_name LIKE 'scn\\_%' ORDER BY table_schema, table_name, ordinal_position"
        )
    finally:
        await conn.close()
    cols: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        cols[(r["table_schema"], r["table_name"])].append((r["column_name"], r["data_type"]))
    for entity, names in SCN_COLUMNS.items():
        platform = cols[("public", f"scn_{entity}")]
        assert [c for c, _ in platform] == ["tenant", "snapshot_id", *names], entity
        assert platform == cols[("fallback", f"scn_{entity}")], entity


async def test_fixture_rows_are_valid_scn_rows() -> None:
    rows = full_snapshot()
    assert len(rows["demand_forecast"]) == 30 * N_STORES * 9 == 121_770
    assert 90_000 < len(rows["allocation"]) < 100_000
    for entity, cols in SCN_COLUMNS.items():
        for row in rows.get(entity, [])[:400]:
            parse_row(entity, dict(zip(cols, row, strict=True)))


async def _mcp_problem(stub: FullMerxaStub) -> ProblemData:
    core = FakeCore()
    gateway = McpGateway(lambda: core.deps(), url_for=lambda t, c: "http://merxa.localhost/mcp",
                         transport_factory=lambda url, token: FastMCPTransport(stub.mcp))
    async with gateway.session(tenant=TENANT, connector="merxa", principal="reader") as session:
        bundle = await gather_problem_data(TENANT, session, lanes=LANES, as_of=AS_OF, horizon_weeks=9)
    return bundle.data


async def test_m1_fields_equal_the_mcp_gatherer(env: Env) -> None:
    bundle = await env.build()
    data, mcp = bundle.data, await _mcp_problem(env.stub)
    assert bundle.source == "scn" and bundle.snapshot_id == env.snapshot.id and not bundle.advisory
    for field in ("dcs", "clusters", "skus", "inventory", "cluster_inventory", "receipts", "lanes", "horizon"):
        assert getattr(data, field) == getattr(mcp, field), field
    # store forecast summed per cluster in SQL, split to sizes like the gatherer
    assert sorted(data.demand, key=lambda d: (d.cluster_code, d.sku, d.iso_week)) == sorted(
        mcp.demand, key=lambda d: (d.cluster_code, d.sku, d.iso_week)
    )
    assert len(data.clusters) == len(CLUSTERS) == 18 and len(data.skus) == 180
    assert bundle.counts["forecast_rows"] == 30 * 18 * 9 and bundle.counts["stores"] == N_STORES
    assert bundle.counts["inventory_positions"] == 4 * 180 + N_STORES * 180


async def test_build_time_under_two_seconds(env: Env) -> None:
    await env.build()  # warm the connection pool
    timings = []
    for _ in range(2):
        started = time.perf_counter()
        bundle = await env.build()
        timings.append(time.perf_counter() - started)
    print(f"problem_scn build: {[round(t, 3) for t in timings]} s, "
          f"{len(bundle.data.demand)} demand rows, {len(bundle.data.allocations)} allocations")
    assert min(timings) < MAX_BUILD_S


async def test_allocations_are_cluster_groups_of_open_store_allocations(env: Env) -> None:
    data = (await env.build()).data
    cluster_of = {s["code"]: s["cluster"] for s in env.stub.stores}
    truth: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in env.dicts("allocation"):
        if a["store_code"] is None or a["status"] not in ("planned", "released"):
            continue
        truth[(a["sku_code"], a["dc_code"], cluster_of[a["store_code"]], a["ship_week"], a["status"])].append(a)
    assert len(data.allocations) == len(truth)
    by_key = {(a.sku, a.dc_code, a.cluster_code, a.ship_week, "released" if a.protected else "planned"): a
              for a in data.allocations}
    assert set(by_key) == set(truth)
    for key, members in truth.items():
        alloc = by_key[key]
        assert alloc.qty == sum(m["qty"] for m in members)
        assert alloc.version == max(m["src_version"] for m in members)
        if len(members) == 1:
            assert alloc.code == members[0]["code"]  # a group of one keeps the Merxa code
        else:
            assert alloc.code.startswith(GROUP_ALLOC_PREFIX) and parse_allocation_group(alloc.code) == {
                "dc": key[1], "cluster": key[2], "sku": key[0], "week": key[3], "status": key[4]}
    assert all(a.protected == (a.ship_week == "2026-W41") for a in data.allocations)
    assert not any(a.code in ("AL-DCONLY", "AL-CANCEL") for a in data.allocations)


async def test_allocation_group_expands_to_store_allocations(env: Env) -> None:
    data = (await env.build()).data
    group = next(a for a in data.allocations if a.code.startswith(GROUP_ALLOC_PREFIX) and a.qty > 10)
    async with env.db.reader(TENANT) as reader:
        members = await expand_allocation_group(reader, tenant=TENANT, snapshot_id=env.snapshot.id,
                                                group_code=group.code)
    assert len(members) > 1 and sum(m.qty for m in members) == group.qty
    assert max(m.version for m in members) == group.version
    assert members == sorted(members, key=lambda m: m.code)
    split = split_allocation(group.qty - 7, members)
    assert sum(q for _, q in split) == group.qty - 7 and all(q >= 0 for _, q in split)
    # another snapshot / tenant never leaks into the members
    assert all(m.code.startswith("AL-") for m in members)


async def test_inbound_lanes_chain_legs_with_rate_card_terms(env: Env) -> None:
    data = (await env.build()).data
    lanes = {ln.code: ln for ln in data.inbound_lanes}
    sea = lanes["CGP-FXT-SEA+FXT-UK1-RD"]
    assert (sea.origin, sea.dest_dc, sea.mode, sea.transit_days) == ("BDCGP", "UK1", "sea", 33)
    assert sea.cost_per_unit_cents == math.ceil(380000 / 4000) + 18  # per TEU converted with units_per_teu
    assert sea.weekly_capacity_units == 60000  # min(400 TEU × 4000, road 60000)
    assert sea.via == ["LKCMB", "CAPE", "GBFXT"] and sea.alt_lane_code == "CGP-UK1-SA"
    sa = lanes["CGP-UK1-SA"]
    # the horizon (W41-W49) crosses the peak card from 2026-11-01: binding terms = peak capacity, peak rate
    assert (sa.mode, sa.cost_per_unit_cents, sa.weekly_capacity_units) == ("sea_air", 185, 19500)
    assert sa.alt_lane_code == "CGP-FXT-SEA+FXT-UK1-RD"
    assert lanes["SGN-HAM-SEA+HAM-PL1-RD"].dest_dc == "PL1"
    assert lanes["IST-DE1-RD"].mode == "road" and lanes["TNG-UK1-RD"].transit_days == 6
    assert not any(code.startswith("DE1-") for code in lanes)  # DC→DC lanes are tenant transfer lanes
    peak = (await env.build(planning=PlanningDefaults(units_per_teu=2000))).data
    assert {ln.code: ln for ln in peak.inbound_lanes}["CGP-FXT-SEA+FXT-UK1-RD"].cost_per_unit_cents == 190 + 18


async def test_inbound_lane_terms_bind_over_the_horizon() -> None:
    """Review regression: ``InboundLane`` has one capacity/cost for every week, so a rate card that
    starts mid-horizon (peak season: lower capacity, higher rate) must bind — otherwise plans book
    more sea-air capacity than the target system accepts in the peak weeks."""
    lanes = [{"code": "CGP-UK1-SA", "origin_code": "BDCGP", "dest_code": "UK1", "mode": "sea_air",
              "transit_days": 16, "via": ["AEJEA"], "alt_lane_code": None}]
    rates = [
        {"lane_code": "CGP-UK1-SA", "valid_from": dt.date(2026, 7, 1), "valid_to": dt.date(2026, 10, 31),
         "basis": "per_unit", "rate_cents": 145, "weekly_capacity": 26000},
        {"lane_code": "CGP-UK1-SA", "valid_from": dt.date(2026, 11, 1), "valid_to": dt.date(2027, 2, 28),
         "basis": "per_unit", "rate_cents": 185, "weekly_capacity": 19500},
    ]

    def terms(start: dt.date, until: dt.date | None) -> tuple[int, int]:
        (lane,), _ = compose_inbound_lanes(lanes, rates, dc_codes={"UK1"}, as_of=start, planning=PlanningDefaults(),
                                           until=until)
        return lane.cost_per_unit_cents, lane.weekly_capacity_units

    assert terms(dt.date(2026, 10, 5), None) == (145, 26000)  # one day: the card valid then
    assert terms(dt.date(2026, 10, 5), dt.date(2026, 10, 25)) == (145, 26000)  # horizon before the peak
    assert terms(dt.date(2026, 10, 5), dt.date(2026, 12, 6)) == (185, 19500)  # crosses into the peak
    assert terms(dt.date(2026, 11, 2), dt.date(2026, 12, 6)) == (185, 19500)
    assert terms(dt.date(2027, 3, 1), dt.date(2027, 4, 1)) == (185, 19500)  # no card valid: the last one


async def test_po_lines_carry_lanes_from_sailings_or_transit(env: Env) -> None:
    data = (await env.build()).data
    refs = {pl.ref: pl for pl in data.po_lines}
    assert {r.ref for r in data.receipts} == set(refs)
    uk = next(pl for pl in data.po_lines if pl.factory_code == "F-BD-01" and pl.dest_dc == "UK1")
    assert (uk.lane_code, uk.mode) == ("CGP-FXT-SEA+FXT-UK1-RD", "sea")  # SAIL-0001
    inferred = [pl for pl in data.po_lines if pl.po_code == INFERRED_PO]
    assert inferred and {(pl.lane_code, pl.mode) for pl in inferred} == {("SGN-DE1-SA", "sea_air")}
    bd_de = next(pl for pl in data.po_lines if pl.factory_code == "F-BD-01" and pl.dest_dc == "DE1")
    assert bd_de.lane_code is None and bd_de.mode == "sea"  # no lane from Bangladesh to DE1
    assert all(pl.ex_factory <= pl.eta for pl in data.po_lines)


async def test_production_lines_and_movable_orders(env: Env) -> None:
    data = (await env.build()).data
    lines = {pl.line_code: pl for pl in data.production}
    assert set(lines) == {"FL-BD01-A", "FL-BD01-B", "FL-VN01-A"}
    assert {pl.factory_code for pl in lines.values()} == {"F-BD-01", "F-VN-01"}
    for line in lines.values():
        assert line.daily_capacity_units == 400  # 1,200 units over 3 days
        assert any(o.movable for o in line.orders) and not all(o.movable for o in line.orders)
        assert all(o.po_line_ref.split("#")[0].startswith("PO-") for o in line.orders)
    received = {r.ref for r in data.receipts}
    assert all(o.movable is False for line in lines.values() for o in line.orders if o.po_line_ref not in received)


async def test_production_orders_without_a_po_line_are_skipped(env: Env) -> None:
    cols = SCN_COLUMNS["production_order"]
    i_ref, i_status, i_line = cols.index("po_line_ref"), cols.index("status"), cols.index("line_code")
    prod = [list(r) for r in env.rows["production_order"]]
    movable = next(r for r in prod if r[i_line] == "FL-BD01-A" and r[i_status] == "planned")
    movable[i_ref] = None  # nullable in scn/1.0: a movable order we cannot describe
    done = next(r for r in prod if r[i_line] == "FL-VN01-A" and r[i_status] == "done")
    done[i_ref] = "not-a-ref"
    sid = str(uuid.uuid4())
    await env.db.load(TENANT, sid, {"location": env.rows["location"], "item": env.rows["item"],
                                    "po_line": env.rows["po_line"], "production_order": [tuple(r) for r in prod]})
    async with env.db.reader(TENANT) as reader:
        data = (await build_problem_from_snapshot(TENANT, reader, snapshot=SnapshotRef(id=sid, connector="merxa"),
                                                  lanes=LANES, as_of=AS_OF, horizon_weeks=9)).data
    lines = {pl.line_code: pl for pl in data.production}
    assert "FL-BD01-A" not in lines  # resequencing it would have to list the unknown order
    po_cols = SCN_COLUMNS["po_line"]
    sku_of = {r[0]: r[po_cols.index("sku_code")] for r in env.rows["po_line"]}
    in_scope = {s.code for s in data.skus}
    expected = [r[0] for r in prod if r[i_line] == "FL-VN01-A" and r[i_ref] in sku_of and sku_of[r[i_ref]] in in_scope]
    assert [o.code for o in lines["FL-VN01-A"].orders] == expected and done[0] not in expected
    assert "FL-BD01-B" in lines


async def test_alternate_sources_derived_from_factory_bom(env: Env) -> None:
    data = (await env.build()).data
    by = defaultdict(list)
    for src in data.alt_sources:
        by[src.factory_code].append(src)
    tr = {s.lane_code: s for s in by["F-TR-01"]}
    assert set(tr) == {"IST-DE1-RD", "IST-NL1-RD"}
    style_cost = {s.style_code: s.unit_cost_cents for s in data.skus}
    ist = tr["IST-DE1-RD"]
    assert ist.supplier_code == "CMT-TR-01" and ist.approved and ist.compliance_ok and ist.usable
    assert ist.lead_time_days == 21 + 5
    assert ist.unit_cost_cents == math.ceil(max(style_cost[s] for s in ist.style_codes) * 1.25)
    assert len(ist.style_codes) == 30  # Istanbul has BOM lines for outerwear and knit
    assert {s.lane_code for s in by["F-MA-01"]} == {"TNG-NL1-RD", "TNG-UK1-RD"}
    assert all(s.style_codes and all(c.startswith("PUF-") for c in s.style_codes) for s in by["F-MA-01"])
    # the incumbent factory is never an alternate for the styles it supplies
    incumbent = {(pl.factory_code, next(s.style_code for s in data.skus if s.code == pl.sku)) for pl in data.po_lines}
    assert not any((s.factory_code, style) in incumbent for s in data.alt_sources for style in s.style_codes)
    blocked = (await env.build(planning=PlanningDefaults(alt_sourcing=AltSourcingDefaults(
        blocked_factories=("F-MA-01",))))).data
    assert {s.compliance_ok for s in blocked.alt_sources if s.factory_code == "F-MA-01"} == {False}
    explicit = [{"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "style_codes": ["PUF-01", "NOPE"],
                 "lead_time_days": 9, "unit_cost_cents": 2500, "moq_units": 100, "weekly_capacity_units": 900,
                 "approved": True, "compliance_ok": True, "lane_code": "IST-NL1-RD"},
                {"supplier_code": "X", "factory_code": "X", "style_codes": ["PUF-01"], "lead_time_days": 1,
                 "unit_cost_cents": 1, "moq_units": 1, "weekly_capacity_units": 1, "approved": True,
                 "compliance_ok": True, "lane_code": "UNKNOWN-LANE"}]
    cfg = (await env.build(connector_config={"alt_sources": explicit})).data
    assert [(s.factory_code, s.style_codes, s.lead_time_days) for s in cfg.alt_sources] == [("F-TR-01", ["PUF-01"], 9)]


async def test_locations_for_the_map(env: Env) -> None:
    data = (await env.build()).data
    locs = {(loc.kind, loc.code): loc for loc in data.locations}
    assert not any(kind == "store" for kind, _ in locs)
    assert ("port", "LHR") in locs  # airport → port
    stores = [r for r in env.dicts("location") if r["kind"] == "store" and r["parent_code"] == "UK1-COLD-A"]
    cl = locs[("cluster", "UK1-COLD-A")]
    assert cl.parent == "UK1"
    assert abs(cl.lat_e6 - e6(sum(s["lat"] for s in stores) / len(stores))) <= 1
    geo = get_geo()
    bd = geo.centroids["BD"]
    f = locs[("factory", "F-BD-01")]
    assert f.parent == "CMT-BD-01" and abs(f.lon_e6 - bd[0]) <= 600_000 and abs(f.lat_e6 - bd[1]) <= 600_000
    assert ("supplier", "TRIM-BD-01") in locs and ("dc", "UK1") in locs
    no_geo = (await env.build(geo=GeoCatalog())).data  # without centroids: factories/suppliers are left off the map
    assert not any(loc.kind in ("factory", "supplier") for loc in no_geo.locations)


async def test_bom_walk_finds_affected_po_lines(env: Env) -> None:
    data = (await env.build()).data
    style = {s.code: s.style_code for s in data.skus}
    bd = receipts_for_entity(data, "supplier", "TRIM-BD-01")
    assert bd == {pl.ref for pl in data.po_lines if pl.factory_code == "F-BD-01"}
    yarn = receipts_for_entity(data, "component", "CMP-YRN-01")
    assert yarn and yarn == {pl.ref for pl in data.po_lines
                             if pl.factory_code == "F-VN-01" and style[pl.sku].startswith("KNT-")}
    assert receipts_for_entity(data, "factory", "F-VN-01") == {
        pl.ref for pl in data.po_lines if pl.factory_code == "F-VN-01"}
    assert receipts_for_entity(data, "supplier", "NOBODY") == set()


async def test_snapshot_and_tenant_isolation(env: Env) -> None:
    unknown = SnapshotRef(id=str(uuid.uuid4()), connector="merxa", as_of=AS_OF)
    async with env.db.reader(TENANT) as reader:
        empty = (await build_problem_from_snapshot(TENANT, reader, snapshot=unknown, lanes=LANES, as_of=AS_OF,
                                                   horizon_weeks=9)).data
        # the other tenant's copy of the same snapshot id is never read
        theirs = (await build_problem_from_snapshot("northwind-test", reader, snapshot=env.snapshot, lanes=LANES,
                                                    as_of=AS_OF, horizon_weeks=9)).data
    assert empty.dcs == [] and empty.po_lines == [] and empty.allocations == []
    assert theirs.po_lines == [] and len(theirs.dcs) == 4  # only the 10 location rows loaded for that tenant
    data = (await env.build()).data
    assert len(data.po_lines) == len({pl.ref for pl in data.po_lines})
    assert {a.code for a in data.allocations}.isdisjoint({"AL-DCONLY", "AL-CANCEL"})


async def test_open_ended_rate_cards() -> None:
    """Review regression: a rate card with an open start (``valid_from`` NULL) or end is valid
    on that side; it used to raise ``TypeError`` (not a fallback error, so the case run failed)."""
    lanes = [{"code": "TNG-UK1-RD", "origin_code": "MATNG", "dest_code": "UK1", "mode": "road",
              "transit_days": 6, "via": [], "alt_lane_code": None}]
    rates = [{"lane_code": "TNG-UK1-RD", "valid_from": None, "valid_to": None, "basis": "per_unit",
              "rate_cents": 70, "weekly_capacity": 12000}]
    (lane,), _ = compose_inbound_lanes(lanes, rates, dc_codes={"UK1"}, as_of=dt.date(2026, 10, 5),
                                       planning=PlanningDefaults(), until=dt.date(2026, 12, 6))
    assert (lane.cost_per_unit_cents, lane.weekly_capacity_units) == (70, 12000)
