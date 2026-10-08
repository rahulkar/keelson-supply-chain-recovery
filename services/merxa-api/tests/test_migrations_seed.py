"""Migrations are idempotent; both seed profiles are deterministic and support the demo story."""

from __future__ import annotations

import io
import json
import math
import time
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import create_database, drop_database, fresh_engine
from merxa_api import tables as t
from merxa_api.calendar import iso_week
from merxa_api.config import Settings
from merxa_api.migrate import discover, migrate
from merxa_api.security.principal import Principal
from merxa_api.seed import generator as gen
from merxa_api.seed import loader
from merxa_api.seed import reference as ref
from merxa_api.seed import reference_full as reff
from merxa_api.seed.__main__ import main as seed_cli
from merxa_api.seed.cities_extract import extract as cities_extract
from merxa_api.seed.generator import UnsupportedProfile, generate
from merxa_api.seed.loader import dataset_fingerprint, db_fingerprint, golden, load, reset, seed
from merxa_api.seed.split import share_split
from merxa_api.seed.stores_full import load_cities
from merxa_api.seed.story import (
    KNIT_PREFIXES,
    DcProjection,
    LaneInfo,
    StoryInputs,
    closure_shifts,
    inputs_from_dataset,
    load_inputs,
    project,
    recoverable,
    story_report,
    transfer_recoverable,
)
from merxa_api.services.admin import AdminService

PEAK = ["2026-W45", "2026-W46", "2026-W47", "2026-W48"]
PRE_PEAK = ["2026-W41", "2026-W42", "2026-W43", "2026-W44"]

# Golden fingerprints of the generated datasets (sha256 over the per-table COPY CSV, see
# seed/dataset.py). Bump deliberately when the seed changes and say why:
# 2026-10 (M2) — forecasts stored per store (cluster sums unchanged), M2 tables (ports,
# lanes, sailings, shipments, production, online orders, promotions, sales history),
# BD wave C ex-factory 5 Oct (Cape sailing); `full` profile added.
# 2026-10 (M4, docs/contracts/90 §8) — Vietnam lanes SGN-HAM-SEA / SGN-GDN-SEA run via SUEZ
# with new Cape alternates SGN-HAM-CAPE / SGN-GDN-CAPE (+12 days, rate cards) — both
# profiles (shared lane table); `full`: VN_W47_CARRY 0.5 → 0.2 (VN wave-1 quantities) so a
# 21-day Suez closure runs DE1/PL1 short of knit in W46–W48.
GOLDEN_DATASET_SHA256 = {
    "small": "fa541cebad5bc84e920fdcd3db4d072245c42ca1e66e65d484e88de88382b313",
    "full": "3dcb482c3d4bc4d4577c00316c939031fd47302d429f7cf84421d1aac096bea5",
}

# Demo-story targets for "Bangladesh PO lines +7 days".
AT_RISK_RANGE = {"small": (1_500, 4_000), "full": (10_000, 40_000)}
W45_COVERED = {"small": ref.W45_COVERED_STYLES, "full": reff.W45_COVERED_STYLES}


async def test_migrations_are_idempotent() -> None:
    name, url = await create_database()
    engine = await fresh_engine(url)
    try:
        first = await migrate(engine)
        assert first == [m.version for m in discover()]
        assert first[:2] == ["0001", "0002"]
        assert await migrate(engine) == []  # runner skips applied files
        # The SQL itself is re-runnable (IF NOT EXISTS / OR REPLACE everywhere).
        async with engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            for mig in discover():
                await raw.execute(mig.sql)
            n = await raw.fetchval("SELECT count(*) FROM merxa.schema_migrations")
            assert n == len(discover())
            # PG18 uuidv7() defaults and version/updated_at columns are in place.
            await raw.execute("INSERT INTO merxa.market(code, name, currency) VALUES ('XX','Test','EUR')")
            row = await raw.fetchrow("SELECT id, version FROM merxa.market WHERE code='XX'")
            assert row["id"].version == 7 and row["version"] == 1
            await raw.execute("UPDATE merxa.market SET name='Test 2' WHERE code='XX'")
            assert await raw.fetchval("SELECT version FROM merxa.market WHERE code='XX'") == 2
            # PG18 temporal primary key: overlapping rate-card periods for one lane are refused.
            await raw.execute("INSERT INTO merxa.carrier(code, name, mode) VALUES ('C1','Test','road')")
            await raw.execute(
                "INSERT INTO merxa.lane(code, origin, dest, mode, carrier_code, transit_days) "
                "VALUES ('L1','AAA','BBB','road','C1',1)"
            )
            await raw.execute(
                "INSERT INTO merxa.rate_card(lane_code, valid, basis, rate_cents, weekly_capacity) "
                "VALUES ('L1','[2026-01-01,2026-02-01)','per_unit',10,100)"
            )
            await raw.execute(
                "INSERT INTO merxa.rate_card(lane_code, valid, basis, rate_cents, weekly_capacity) "
                "VALUES ('L1','[2026-02-01,2026-03-01)','per_unit',12,100)"
            )
            with pytest.raises(Exception, match=r"conflicting key|overlap|exclusion"):
                await raw.execute(
                    "INSERT INTO merxa.rate_card(lane_code, valid, basis, rate_cents, weekly_capacity) "
                    "VALUES ('L1','[2026-01-15,2026-02-15)','per_unit',11,100)"
                )
    finally:
        await engine.dispose()
        await drop_database(name)


def test_generator_is_deterministic() -> None:
    a, b = generate("small"), generate("small")
    assert dataset_fingerprint(a) == dataset_fingerprint(b)
    assert a.count("dc") == 4
    assert 8 <= a.count("store_cluster") <= 12
    assert 55 <= a.count("store") <= 65
    assert a.count("sku") == 30
    assert 6 <= a.count("purchase_order") <= 8
    with pytest.raises(UnsupportedProfile):
        generate("medium")


def test_golden_dataset_fingerprints() -> None:
    for profile, expected in GOLDEN_DATASET_SHA256.items():
        assert golden(profile).fingerprint == expected, profile


def test_full_profile_shape() -> None:
    ds = generate("full")
    stores = ds.dicts("store")
    assert len(stores) == 451 and ds.count("market") == 16
    assert Counter(s["market_code"] for s in stores) == Counter(reff.STORE_COUNTS)
    assert {s["home_dc_code"] for s in stores if s["market_code"] in ("ES", "PT")} == {"NL1"}
    assert {s["home_dc_code"] for s in stores if s["market_code"] in ("IT", "SI")} == {"DE1"}
    assert {s["home_dc_code"] for s in stores if s["market_code"] in ("RO", "HU", "SK")} == {"PL1"}
    clusters = ds.dicts("store_cluster")
    assert len(clusters) <= 24 and 16 <= len(clusters) <= 20
    assert all(c["code"] == f"{c['dc_code']}-{c['climate'].upper()}-{c['grade_band']}" for c in clusters)
    # population weighted: the biggest city of each market has the most stores; ~2 km jitter
    cities = {(c.country, c.name): c for c in load_cities()}
    for s in stores:
        c = cities[(s["market_code"], s["city"])]
        assert abs(s["lat"] - c.lat) < 0.05 and abs(s["lon"] - c.lon) < 0.12, s["code"]
    gb = Counter(s["city"] for s in stores if s["market_code"] == "GB")
    assert gb.most_common(1)[0][0] == "London"
    assert len({s["code"] for s in stores}) == 451

    assert ds.count("style") == 30 and ds.count("sku") == 180
    assert {s["family"] for s in ds.dicts("style")} == {"Outerwear", "Knit"}
    assert ds.count("purchase_order") == 12 and ds.count("sailing") == 4
    pos = {p["code"]: p for p in ds.dicts("purchase_order")}
    assert {c for c in pos if c.startswith("PO-VN-")} == {f"PO-VN-000{i}" for i in range(1, 5)}
    assert {pos[c]["dest_dc_code"] for c in pos if c.startswith("PO-VN-")} == {"DE1", "PL1"}
    assert {iso_week(pos[c]["eta_dc"]) for c in pos if c.startswith("PO-VN-")} == {"2026-W46", "2026-W47"}
    factories = {f["code"] for f in ds.dicts("factory")}
    assert {"F-BD-01", "F-BD-02", "F-KH-01", "F-VN-01", "F-TR-01", "F-MA-01"} <= factories
    assert {"TRIM-BD-01", "SUP-VN-01"} <= {s["code"] for s in ds.dicts("supplier")}
    lanes = {ln["code"]: ln for ln in ds.dicts("lane")}
    assert lanes["CGP-UK1-SA"]["mode"] == "sea_air" and lanes["CGP-UK1-SA"]["via"] == ["AEJEA"]
    assert "CAPE" in lanes["CGP-FXT-SEA"]["via"]
    assert {"DE1-NL1-RD", "DE1-UK1-RD", "PL1-NL1-RD", "PL1-DE1-RD", "NL1-UK1-RD"} <= set(lanes)
    assert {r["lane_code"] for r in ds.dicts("rate_card") if r["basis"] == "per_unit"} >= {"CGP-UK1-SA"}
    assert ds.count("online_order") == 600
    promise_weeks = {iso_week(o["promise_date"]) for o in ds.dicts("online_order")}
    assert promise_weeks <= set(PEAK)
    assert {p["code"] for p in ds.dicts("promotion")} == {"PROMO-BF26", "PROMO-WW26", "PROMO-KN26"}
    bf = next(p for p in ds.dicts("promotion") if p["code"] == "PROMO-BF26")
    assert iso_week(bf["period"].lower) == "2026-W48"
    assert {r[2] for r in ds.tables["sales_history"].rows} == {f"2026-W{w}" for w in range(33, 41)}
    # forecasts are stored per style × store × week
    assert ds.count("demand_forecast") == 30 * 451 * 9
    assert all(r[2] is not None for r in ds.tables["demand_forecast"].rows)


async def test_seed_shape_and_story_dates(engine: AsyncEngine, reseed: None) -> None:
    async with engine.connect() as conn:
        pos = {r.code: r for r in await conn.execute(select(t.purchase_order))}
        factories = {r.code: r.country for r in await conn.execute(select(t.factory))}
        n_forecast = (await conn.execute(select(func.count()).select_from(t.demand_forecast))).scalar_one()
        null_store = (
            await conn.execute(select(func.count()).where(t.demand_forecast.c.store_code.is_(None)))
        ).scalar_one()
    bd = [p for p in pos.values() if factories[p.factory_code] == "BD"]
    assert {p.dest_dc_code for p in bd} == {"UK1", "NL1"}
    assert {iso_week(p.eta_dc) for p in bd} == {"2026-W44", "2026-W45", "2026-W46"}
    kh = pos["PO-KH-0001"]
    assert (kh.dest_dc_code, kh.status, iso_week(kh.eta_dc)) == ("DE1", "received", "2026-W40")
    assert n_forecast == 62 * 5 * 9 and null_store == 0


async def test_db_seed_is_deterministic(engine: AsyncEngine, settings) -> None:
    await seed(engine, settings.seed_profile, reset_first=True)
    async with engine.connect() as conn:
        first = await db_fingerprint(conn)
    await seed(engine, settings.seed_profile, reset_first=True)
    async with engine.connect() as conn:
        second = await db_fingerprint(conn)
    assert first == second
    assert set(first) >= {"store", "po_line", "inventory_position", "demand_forecast", "allocation", "sailing"}


async def _index_defs(conn) -> list[str]:
    rows = await conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE schemaname='merxa' ORDER BY tablename, indexname")
    )
    return [r.indexdef for r in rows]


async def test_copy_and_insert_loads_are_identical(
    engine: AsyncEngine, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fast COPY path (FK triggers off, PK/unique/secondary indexes dropped and rebuilt)
    stores exactly what the INSERT path (every constraint checked row by row) stores."""
    monkeypatch.setattr(loader, "BULK_ROWS", 1000)  # exercise index detaching on the small profile
    async with engine.connect() as conn:
        indexes_before = await _index_defs(conn)
    async with engine.begin() as conn:
        await reset(conn)
        await loader.load_golden(conn, golden(settings.seed_profile))
    async with engine.connect() as conn:
        via_copy = await db_fingerprint(conn)
        assert await _index_defs(conn) == indexes_before  # every index/constraint is back
    async with engine.begin() as conn:
        await reset(conn)
        await load(conn, generate(settings.seed_profile), use_copy=False)
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) == via_copy
    gs = golden(settings.seed_profile)
    assert gs.fingerprint == dataset_fingerprint(generate(settings.seed_profile))
    await seed(engine, settings.seed_profile, reset_first=True)


async def test_reset_merges_back_to_golden(engine: AsyncEngine, settings) -> None:
    """A reset of a seeded database merges: only changed rows are rewritten, the content is
    the golden content again (versions back to 1), untouched rows keep their ids."""
    await seed(engine, settings.seed_profile, reset_first=True)
    async with engine.connect() as conn:
        golden_tables = await db_fingerprint(conn)
        assert await loader.loaded_fingerprint(conn) == golden(settings.seed_profile).fingerprint
        keep_id = (await conn.execute(select(t.allocation.c.id).where(t.allocation.c.code == "AL-000002"))).scalar_one()
    async with engine.begin() as conn:  # drift: updates, inserts and deletes in many tables
        await conn.execute(update(t.allocation).where(t.allocation.c.code == "AL-000001").values(qty=999))
        await conn.execute(
            update(t.inventory_position)
            .where(t.inventory_position.c.location_code == "UK1")
            .values(reserved=0, in_transit=t.inventory_position.c.in_transit + 5)
        )
        await conn.execute(update(t.demand_forecast).where(t.demand_forecast.c.iso_week == "2026-W45").values(units=1))
        await conn.execute(t.sales_history.delete().where(t.sales_history.c.iso_week == "2026-W33"))
        await conn.execute(
            t.purchase_order.insert().values(
                code="PO-XX-0001",
                supplier_code="CMT-BD-01",
                factory_code="F-BD-01",
                dest_dc_code="UK1",
                status="open",
                ex_factory_date=date(2026, 11, 1),
                eta_dc=date(2026, 11, 20),
            )
        )
        await conn.execute(
            t.po_line.insert().values(
                po_code="PO-XX-0001",
                line_no=1,
                sku_code="PUF-W-BLK-M",
                qty_ordered=5,
                eta_dc=date(2026, 11, 20),
                status="open",
            )
        )
        await conn.execute(update(t.promotion).where(t.promotion.c.code == "PROMO-BF26").values(discount_bp=1))
        await conn.execute(update(t.sim_clock).values(today=date(2026, 10, 20)))
        await conn.execute(
            t.supplier_message.insert().values(
                supplier_code="TRIM-BD-01", received_at=datetime(2026, 10, 5, tzinfo=UTC), subject="x", body="y"
            )
        )
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) != golden_tables
    started = time.perf_counter()
    await seed(engine, settings.seed_profile, reset_first=True)
    elapsed = time.perf_counter() - started
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) == golden_tables
        assert (
            await conn.execute(select(t.allocation.c.id).where(t.allocation.c.code == "AL-000002"))
        ).scalar_one() == keep_id  # merged, not reloaded
        for tbl in (t.allocation, t.inventory_position, t.demand_forecast, t.promotion, t.po_line):
            assert (await conn.execute(select(func.max(tbl.c.version)))).scalar_one() == 1
    assert elapsed < 10


def assert_story(profile: str, base: dict[str, DcProjection], late: dict[str, DcProjection], ds) -> None:
    for dc in ("UK1", "NL1", "DE1", "PL1"):
        assert base[dc].total_shortage == 0, f"{dc} must not be short in the baseline"
        assert not base[dc].first_breach
    total = 0
    for dc in ("UK1", "NL1"):
        p = late[dc]
        assert all(p.shortage[w] == 0 for w in PRE_PEAK), f"{dc} shortage must start in peak"
        assert sum(p.shortage[w] for w in PEAK) == p.total_shortage
        assert p.shortage["2026-W45"] > 0 and p.shortage["2026-W46"] > 0, f"{dc}: short in W45 and W46"
        assert p.total_shortage >= 500, f"{dc} shortage is not material: {p.total_shortage}"
        # first breaches spread over several peak weeks (W45-covered styles breach in W46)
        assert set(p.first_breach.values()) == {"2026-W45", "2026-W46"}
        breach_styles = {style for (_, style), wk in p.first_breach.items() if wk == "2026-W46"}
        assert breach_styles == set(W45_COVERED[profile])
        total += p.total_shortage
    lo, hi = AT_RISK_RANGE[profile]
    assert lo <= total <= hi, f"units at risk {total} outside {(lo, hi)}"
    for dc in ("DE1", "PL1"):
        assert late[dc].total_shortage == 0
        assert late[dc].transferable > 0, f"{dc} must hold transferable surplus"
    # The surplus DCs can recover most of the shortfall, but not all of it (tight sizes).
    rec = recoverable(late)
    assert 0.6 * total <= rec < total, f"recoverable {rec} of {total}"
    sizes = {s["code"]: s["size"] for s in ds.dicts("sku")}
    tight = {sku for sku, size in sizes.items() if size in ref.TIGHT_SIZES}
    lost_tight = sum(q for dc in ("UK1", "NL1") for sku, q in late[dc].lost_by_sku.items() if sku in tight)
    give_tight = sum(q for dc in ("DE1", "PL1") for sku, q in late[dc].transferable_by_sku.items() if sku in tight)
    assert give_tight < lost_tight


def assert_vn_closure(base: dict[str, DcProjection], closed: dict[str, DcProjection]) -> None:
    """VNSGN closed for 10 days: DE1/PL1 run short of knit (from W46); nobody else moves."""
    for dc in ("DE1", "PL1"):
        p = closed[dc]
        assert p.shortage["2026-W46"] > 0, f"{dc} must be short of knit in W46"
        assert all(sku.split("-")[0] in KNIT_PREFIXES for sku in p.lost_by_sku), "only knit runs short"
        assert min(p.first_breach.values()) == "2026-W46"
        assert p.total_shortage >= 500
    for dc in ("UK1", "NL1"):
        assert closed[dc].total_shortage == 0
    assert all(base[dc].total_shortage == 0 for dc in base)


SUEZ_SHORT_WEEKS = ("2026-W46", "2026-W47", "2026-W48")


def assert_suez_closure(inputs: StoryInputs, base: dict[str, DcProjection]) -> dict[str, DcProjection]:
    """Act 2 (docs/contracts/90 §8): SUEZ closed 21 days from the sim clock. Resolved from the
    lane data: both VN sailings depart inside the window and switch to the Cape lanes
    (+12 days); DE1/PL1 run short of knit in W46, W47 **and** W48; nobody else moves."""
    shifts = closure_shifts(inputs, reff.SUEZ_CHOKEPOINT, days=reff.SUEZ_CLOSURE_DAYS)
    vn_refs = {r[5] for r in inputs.receipts if r[4] == "VN"}
    assert vn_refs and set(shifts) == vn_refs, "exactly the open VN lines are re-routed"
    assert set(shifts.values()) == {12}, "Cape alternate = +12 days (shorter than waiting 21 days)"
    closed = project(inputs, ref_shifts=shifts)
    for dc in ("DE1", "PL1"):
        p = closed[dc]
        for wk in SUEZ_SHORT_WEEKS:
            assert p.shortage[wk] > 0, f"{dc} must be short of knit in {wk}"
        assert min(p.first_breach.values()) == "2026-W46"
        assert all(sku.split("-")[0] in KNIT_PREFIXES for sku in p.lost_by_sku), "only knit runs short"
        assert p.shortage["2026-W48"] >= 500, "W48 (Black Friday) shortage must be material"
    for dc in ("UK1", "NL1"):
        assert closed[dc].total_shortage == 0
    assert all(base[dc].total_shortage == 0 for dc in base)
    # a closure that ends before the first VN sailing departs changes nothing
    assert closure_shifts(inputs, "SUEZ", days=1, start=date(2026, 10, 5)) == {}
    return closed


def _toy_inputs(routes: dict[str, tuple[str, date]], lanes: dict[str, LaneInfo]) -> StoryInputs:
    return StoryInputs(
        weeks=["2026-W41"],
        atp={},
        dc_supply={},
        cluster_dc={},
        cluster_stock={},
        receipts=[("DE1", f"SKU-{i}", date(2026, 11, 11), 10, "VN", ref) for i, ref in enumerate(sorted(routes))],
        demand={},
        sku_style={},
        as_of=date(2026, 10, 5),
        routes=routes,
        lanes=lanes,
    )


def test_closure_shifts_follow_the_lane_data() -> None:
    """Regression for the lane-aware closure rule (mirrors the optimizer): alt lane when it
    avoids the chokepoint and is not a faster mode, else wait for the reopening; only
    departures inside the window; lanes not via the chokepoint are untouched."""
    lanes = {
        "SUEZ-SEA": LaneInfo("sea", 35, ("SUEZ",), "CAPE-SEA"),
        "CAPE-SEA": LaneInfo("sea", 47, ("CAPE",), None),
        "SUEZ-NOALT": LaneInfo("sea", 35, ("SUEZ",), None),
        "SUEZ-ALT-SUEZ": LaneInfo("sea", 35, ("SUEZ",), "SUEZ-NOALT"),
        "SUEZ-ALT-AIR": LaneInfo("sea", 35, ("SUEZ",), "AIR"),
        "AIR": LaneInfo("sea_air", 15, ("AEJEA",), None),
        "SUEZ-LONG-ALT": LaneInfo("sea", 30, ("SUEZ",), "VERY-LONG"),
        "VERY-LONG": LaneInfo("sea", 60, ("CAPE",), None),
    }
    routes = {
        "A#1": ("SUEZ-SEA", date(2026, 10, 6)),
        "B#1": ("SUEZ-NOALT", date(2026, 10, 6)),
        "C#1": ("SUEZ-ALT-SUEZ", date(2026, 10, 6)),
        "D#1": ("SUEZ-ALT-AIR", date(2026, 10, 6)),
        "E#1": ("SUEZ-LONG-ALT", date(2026, 10, 6)),
        "F#1": ("CAPE-SEA", date(2026, 10, 6)),
        "G#1": ("SUEZ-SEA", date(2026, 10, 26)),  # departs the day the canal reopens
        "H#1": ("SUEZ-SEA", date(2026, 10, 4)),  # left before the closure
    }
    inputs = _toy_inputs(routes, lanes)
    assert closure_shifts(inputs, "SUEZ", days=21) == {"A#1": 12, "B#1": 21, "C#1": 21, "D#1": 21, "E#1": 21}
    assert closure_shifts(inputs, "SUEZ", days=7) == {"A#1": 7, "B#1": 7, "C#1": 7, "D#1": 7, "E#1": 7}
    assert closure_shifts(inputs, "CAPE", days=21) == {"F#1": 21}
    assert closure_shifts(inputs, "SUEZ", days=0) == {}
    assert closure_shifts(inputs, "SUEZ", days=21, start=date(2026, 10, 7)) == {"G#1": 12}
    # a line without a known lane is never shifted
    assert closure_shifts(_toy_inputs({"X#1": ("NOPE", date(2026, 10, 6))}, lanes), "SUEZ", days=21) == {}
    with pytest.raises(ValueError, match="start date"):
        closure_shifts(StoryInputs(**{**inputs.__dict__, "as_of": None}), "SUEZ", days=21)
    # ref_shifts move single PO lines (on top of the country shifts)
    one = StoryInputs(
        weeks=["2026-W46", "2026-W47", "2026-W48"],
        atp={},
        dc_supply={},
        cluster_dc={"CL1": "DE1"},
        cluster_stock={},
        receipts=[
            ("DE1", "S1", date(2026, 11, 11), 10, "VN", "A#1"),
            ("DE1", "S1", date(2026, 11, 11), 5, "VN", "B#1"),
        ],
        demand={("CL1", "S1"): {"2026-W46": 15, "2026-W47": 0, "2026-W48": 0}},
        sku_style={"S1": "ST1"},
    )
    assert project(one)["DE1"].total_shortage == 0
    assert project(one, ref_shifts={"A#1": 12})["DE1"].shortage == {"2026-W46": 10, "2026-W47": 0, "2026-W48": 0}
    assert project(one, ref_shifts={"A#1": 7}, shifts={"VN": 7})["DE1"].shortage["2026-W46"] == 15


def test_transfer_recoverable_follows_directional_dc_lanes() -> None:
    """Regression (M4 review): the Suez report claimed UK1/NL1 surplus could cover DE1/PL1,
    but no transfer lane runs that way. Only DCs with a lane **into** the short DC count."""
    lanes = {
        "A-B": LaneInfo("road", 2, (), None, "A", "B"),
        "C-B": LaneInfo("road", 2, (), None, "C", "B"),
        "B-D": LaneInfo("road", 2, (), None, "B", "D"),
        "PORT-B": LaneInfo("road", 1, (), None, "PORT", "B"),  # not DC-to-DC
    }
    inputs = _toy_inputs({}, lanes)
    proj = {
        "A": DcProjection(transferable_by_sku={"S1": 30, "S2": 5}),
        "B": DcProjection(lost_by_sku={"S1": 50, "S2": 10, "S3": 7}),
        "C": DcProjection(transferable_by_sku={"S1": 40}),
        "D": DcProjection(transferable_by_sku={"S3": 100}, lost_by_sku={"S2": 4}),
    }
    # B: S1 30 (A) + 20 (C), S2 5 (A), S3 none (D has no lane into B); D: S2 nothing (B has none)
    assert transfer_recoverable(inputs, proj) == 55
    assert recoverable(proj, ("B",), ("D",)) == 7, "the lane-free estimate would count D's S3"
    no_lanes = _toy_inputs({}, {})
    assert transfer_recoverable(no_lanes, proj) == 0


async def test_reset_records_scenario_reset_in_the_same_transaction(engine: AsyncEngine, settings) -> None:
    """Regression (M4 review): every reload of already-loaded data — demo director **and** the
    CLI ``--reset`` — records ``scenario.reset`` atomically with the data (it used to be a
    separate transaction in the admin service only, so a CLI reset or a crash in between left
    consumers' snapshots silently stale). A first load records nothing."""
    ob = t.outbox

    async def resets() -> list[tuple[int, dict]]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(ob.c.seq, ob.c.payload).where(ob.c.event_type == "scenario.reset").order_by(ob.c.seq)
            )
            return [(r.seq, r.payload) for r in rows]

    before = await resets()
    await seed(engine, settings.seed_profile, reset_first=True)
    after = await resets()
    assert len(after) == len(before) + 1
    assert after[-1][1] == {
        "profile": settings.seed_profile,
        "dataset_sha256": golden(settings.seed_profile).fingerprint,
        "full_resync_required": True,
    }
    assert await seed(engine, settings.seed_profile) == {}  # already seeded, no reset: no event
    assert len(await resets()) == len(after)

    # first load of an empty database: nothing to announce; a CLI-style reset then does
    name, url = await create_database()
    other = await fresh_engine(url)
    try:
        await migrate(other)
        await seed(other, "small")
        async with other.connect() as conn:
            n = (await conn.execute(select(func.count()).select_from(ob))).scalar_one()
        assert n == 0
        await seed(other, "small", reset_first=True)
        async with other.connect() as conn:
            types = [r for (r,) in await conn.execute(select(ob.c.event_type).order_by(ob.c.seq))]
        assert types == ["scenario.reset"]
    finally:
        await other.dispose()
        await drop_database(name)


def test_vn_lanes_run_via_suez_with_cape_alternates() -> None:
    """Contract 90 §8: SGN-HAM-SEA / SGN-GDN-SEA via SUEZ, alt lanes round the Cape +12 days
    with their own rate cards; the VN sailings stay on the Suez lanes."""
    ds = generate("small")  # the lane table is shared by both profiles
    lanes = {ln["code"]: ln for ln in ds.dicts("lane")}
    for sea, cape in (("SGN-HAM-SEA", "SGN-HAM-CAPE"), ("SGN-GDN-SEA", "SGN-GDN-CAPE")):
        assert lanes[sea]["via"] == ["SUEZ"] and lanes[sea]["alt_lane_code"] == cape
        assert lanes[cape]["via"] == ["CAPE"] and lanes[cape]["alt_lane_code"] is None
        assert (lanes[cape]["origin"], lanes[cape]["dest"], lanes[cape]["mode"]) == (
            lanes[sea]["origin"],
            lanes[sea]["dest"],
            "sea",
        )
        assert lanes[cape]["transit_days"] - lanes[sea]["transit_days"] == 12
    rates = {r["lane_code"] for r in ds.dicts("rate_card")}
    assert {"SGN-HAM-CAPE", "SGN-GDN-CAPE"} <= rates
    # Bangladesh still sails round the Cape; sea-air alternates unchanged
    assert "CAPE" in lanes["CGP-FXT-SEA"]["via"] and lanes["SGN-DE1-SA"]["alt_lane_code"] == "SGN-HAM-SEA"
    sailing_lanes = {s["lane_code"] for s in ds.dicts("sailing")}
    assert sailing_lanes == {"CGP-FXT-SEA", "CGP-RTM-SEA", "SGN-HAM-SEA", "SGN-GDN-SEA"}
    # small has no Vietnamese supply: no Suez story
    inputs = inputs_from_dataset(ds)
    assert closure_shifts(inputs, "SUEZ", days=21) == {}
    assert "suez_closure_21_days" not in story_report(inputs)


def test_story_small_from_generated_data() -> None:
    """Regression: +7 days on PO-BD-* gave 0 units at risk because store stock was
    double-counted (DC opening stock ignored the stores' on-hand)."""
    ds = generate("small")
    inputs = inputs_from_dataset(ds)
    base, late = project(inputs), project(inputs, shift_bd_days=7)
    assert_story("small", base, late, ds)
    # longer delays hurt more (monotone), and reach beyond W46
    later = project(inputs, shift_bd_days=14)
    assert sum(p.total_shortage for p in later.values()) > sum(p.total_shortage for p in late.values())
    assert later["UK1"].shortage["2026-W47"] > 0
    # the shift can also be given per factory country
    assert project(inputs, shifts={"BD": 7})["UK1"].shortage == late["UK1"].shortage
    # small has no Vietnamese supply: a VNSGN closure changes nothing
    assert all(p.total_shortage == 0 for p in project(inputs, shifts={"VN": 10}).values())


def test_store_stock_counts_as_cluster_supply() -> None:
    """Regression: the projection must count store stock (like the optimizer does)."""
    ds = generate("small")
    inputs = inputs_from_dataset(ds)
    assert sum(q for (cl, _), q in inputs.cluster_stock.items() if inputs.cluster_dc[cl] == "UK1") > 0
    without = StoryInputs(**{**inputs.__dict__, "cluster_stock": {}})
    late_without = project(without, shift_bd_days=7)
    late = project(inputs, shift_bd_days=7)
    assert late_without["UK1"].total_shortage > late["UK1"].total_shortage
    # Per short DC x SKU: DC stock + store stock = W41-W44 demand + buffer (never more than
    # the W41-W44 demand plus 40 % of a W41 week).
    week_dem: dict[tuple[str, str], dict[str, int]] = {}
    for (cl, sku), weeks in inputs.demand.items():
        acc = week_dem.setdefault((inputs.cluster_dc[cl], sku), dict.fromkeys(weeks, 0))
        for w, q in weeks.items():
            acc[w] += q
    for dc in ref.SHORT_DCS:
        for sku in {s for d, s in inputs.dc_supply if d == dc}:
            stores = sum(q for (cl, s), q in inputs.cluster_stock.items() if s == sku and inputs.cluster_dc[cl] == dc)
            network = inputs.dc_supply[(dc, sku)] + stores
            d = week_dem[(dc, sku)]
            cum44 = sum(d[w] for w in PRE_PEAK)
            assert cum44 < network <= cum44 + max(1, round(0.4 * d["2026-W41"])), (dc, sku, network, cum44)


def test_store_forecast_sums_to_cluster_forecast() -> None:
    """Store rows split the (noisy) cluster forecast exactly: per style × cluster × week the
    store units sum to what the cluster would have been, split by sales index."""
    ds = generate("small")
    weights = {s["code"]: float(s["sales_index"]) for s in ds.dicts("store")}
    per_cluster: dict[tuple[str, str, str], list[tuple[str, int]]] = defaultdict(list)
    for style, cc, store, wk, units in ds.tables["demand_forecast"].rows:
        per_cluster[(style, cc, wk)].append((store, units))
    for rows in per_cluster.values():
        total = sum(u for _, u in rows)
        assert total >= 1
        wsum = sum(weights[s] for s, _ in rows)
        for store, units in rows:
            assert abs(units - total * weights[store] / wsum) < 1.0


def test_story_is_robust_to_the_random_draw(monkeypatch: pytest.MonkeyPatch) -> None:
    for entropy in (1, 42, 777):
        monkeypatch.setattr(gen, "SEED_ENTROPY", entropy)
        ds = generate("small")
        inputs = inputs_from_dataset(ds)
        assert_story("small", project(inputs), project(inputs, shift_bd_days=7), ds)


def test_share_split() -> None:
    assert share_split(10, [600, 1600, 2800, 2600, 1600, 800]) == [0, 2, 3, 3, 2, 0]
    assert sum(share_split(97, [600, 1600, 2800, 2600, 1600, 800])) == 97
    assert share_split(0, [1, 2]) == [0, 0]


async def test_story_holds(engine: AsyncEngine, reseed: None) -> None:
    async with engine.connect() as conn:
        inputs = await load_inputs(conn)
    ds = generate("small")
    assert inputs == inputs_from_dataset(ds)
    assert_story("small", project(inputs), project(inputs, shift_bd_days=7), ds)


async def test_story_holds_when_db_lines_actually_move(engine: AsyncEngine, reseed: None, settings) -> None:
    """Shift the real BD PO lines by +7 days in the DB: the projection sees the same shortage."""
    async with engine.connect() as conn:
        expected = project(await load_inputs(conn), shift_bd_days=7)
    async with engine.begin() as conn:
        bd_pos = select(t.purchase_order.c.code).where(t.purchase_order.c.factory_code.in_(["F-BD-01", "F-BD-02"]))
        await conn.execute(
            update(t.po_line)
            .where(t.po_line.c.po_code.in_(bd_pos))
            .values(eta_dc=t.po_line.c.eta_dc + timedelta(days=7))
        )
    async with engine.connect() as conn:
        moved = project(await load_inputs(conn))
    for dc in ("UK1", "NL1"):
        assert moved[dc].shortage == expected[dc].shortage
        assert moved[dc].total_shortage > 0
    await seed(engine, settings.seed_profile, reset_first=True)


async def test_full_profile_story_and_reset_time() -> None:
    """The one full-profile DB test: load `full` into a fresh database, prove the story from
    what is stored (BD +7 days and the VNSGN closure), and measure the golden reset."""
    name, url = await create_database()
    engine = await fresh_engine(url)
    try:
        await migrate(engine)
        started = time.perf_counter()
        await seed(engine, "full")
        timings = [time.perf_counter() - started]  # COPY load
        bulk = ("inventory_position", "allocation", "po_line")
        async with engine.connect() as conn:
            golden_bulk = await db_fingerprint(conn, bulk)
        for _ in range(2):
            async with engine.begin() as conn:  # demo-like drift in the bulk tables
                await conn.execute(
                    update(t.inventory_position)
                    .where(t.inventory_position.c.location_type == "dc")
                    .values(reserved=t.inventory_position.c.reserved + 1)
                )
                await conn.execute(update(t.allocation).where(t.allocation.c.code < "AL-000300").values(qty=0))
                await conn.execute(
                    update(t.po_line)
                    .where(t.po_line.c.po_code.like("PO-BD-%"))
                    .values(eta_dc=t.po_line.c.eta_dc + timedelta(days=7))
                )
            started = time.perf_counter()
            await seed(engine, "full", reset_first=True)  # merge reset
            timings.append(time.perf_counter() - started)
            async with engine.connect() as conn:
                assert await db_fingerprint(conn, bulk) == golden_bulk
        async with engine.connect() as conn:
            inputs = await load_inputs(conn)
            n_store = (await conn.execute(select(func.count()).select_from(t.store))).scalar_one()
        assert n_store == 451
        ds = generate("full")
        assert inputs == inputs_from_dataset(ds)
        base = project(inputs)
        assert_story("full", base, project(inputs, shift_bd_days=7), ds)
        assert_vn_closure(base, project(inputs, shifts={"VN": 10}))
        assert inputs.as_of == date(2026, 10, 5) and len(inputs.routes) == 900
        assert_suez_closure(inputs, base)
        # The demo inject moves the real data the way the projection predicts.
        admin_svc = AdminService(engine, Settings(seed_profile="full", demo_mode=True))
        who = Principal(sub="pytest", kind="bearer", username="pytest")
        res = await admin_svc.inject(who, "typhoon", {})
        assert res["sailings"] == ["SAIL-0003", "SAIL-0004"] and res["po_lines"] == 4 * 90
        assert res["message"]["kind"] == "supplier_message"
        async with engine.connect() as conn:
            moved = project(await load_inputs(conn))
        expected = project(inputs, shifts={"VN": 10})
        for dc in ("DE1", "PL1"):
            assert moved[dc].shortage == expected[dc].shortage
        print(f"full: COPY load + merge resets: {[round(x, 2) for x in timings]} s")
        # Target < 5 s for a reset (measured 0.5-1 s here, 2-5 s on a pg-merxa-like server
        # with fsync on); generous bound against noisy CI.
        assert min(timings[1:]) < 10.0
    finally:
        await engine.dispose()
        await drop_database(name)


def test_check_story_cli_reports_both_profiles() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        assert seed_cli(["--profile", "small", "--no-db", "--check-story"]) == 0
    report = json.loads(out.getvalue())
    assert report["dataset_sha256"] == GOLDEN_DATASET_SHA256["small"]
    story = report["story_generated"]
    assert set(story) == {"small", "full"}
    for profile in ("small", "full"):
        assert story[profile]["baseline"]["units_at_risk"] == 0
        lo, hi = AT_RISK_RANGE[profile]
        assert lo <= story[profile]["bd_plus_7_days"]["units_at_risk"] <= hi
    assert "vn_port_closure_10_days" not in story["small"]
    vn = story["full"]["vn_port_closure_10_days"]["knit_shortage"]
    assert vn["DE1"] > 0 and vn["PL1"] > 0 and vn["UK1"] == vn["NL1"] == 0
    # Act 2: the Suez what-if is reported for `full` only
    assert "suez_closure_21_days" not in story["small"]
    suez = story["full"]["suez_closure_21_days"]
    assert (suez["chokepoint"], suez["days"], suez["from"]) == ("SUEZ", 21, "2026-10-05")
    assert suez["delayed_po_lines"] == 4 * 90 and suez["extra_days"] == [12]
    for dc in ("DE1", "PL1"):
        assert set(suez["dcs"][dc]["shortage_by_week"]) == set(SUEZ_SHORT_WEEKS)
        assert suez["knit_shortage"][dc] == suez["dcs"][dc]["total_shortage"] > 0
    assert suez["knit_shortage"]["UK1"] == suez["knit_shortage"]["NL1"] == 0
    # Transfer lanes are directional and none runs from UK1/NL1 (the knit surplus) into
    # DE1/PL1: a rebalance recovers nothing — Act 2's remedies are expedite / re-routing.
    assert "recoverable_from_uk1_nl1" not in suez
    assert suez["recoverable_by_dc_transfer"] == 0
    assert suez["units_at_risk"] == suez["knit_shortage"]["DE1"] + suez["knit_shortage"]["PL1"]
    assert not math.isnan(story["full"]["bd_plus_7_days"]["recoverable_by_rebalance"])


def test_cities_extract_from_geonames_dump() -> None:
    def row(gid: int, name: str, cc: str, pop: int, fclass: str = "P") -> str:
        cols = [str(gid), name, name, "", "51.5", "-0.12", fclass, "PPL", cc, "", "", "", "", "", str(pop)]
        return "\t".join([*cols, "", "", "Europe/London", "2024-01-01"]) + "\n"

    lines = [
        row(1, "Bigtown", "GB", 900_000),
        row(2, "Smallton", "GB", 20_000),
        row(3, "Hill", "GB", 50_000, "T"),
        row(4, "Elsewhere", "US", 5_000_000),
        row(5, "Midtown", "GB", 100_000),
    ]
    out = cities_extract(lines, per_market=2)
    assert [(c[0], c[1], c[4]) for c in out] == [("Bigtown", "GB", 900_000), ("Midtown", "GB", 100_000)]


async def test_audit_log_is_append_only(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            t.audit_log.insert().values(entity_type="test", entity_ref="x", action="probe", actor_username="pytest")
        )
    for stmt in (
        "UPDATE merxa.audit_log SET action='tampered' WHERE entity_type='test'",
        "DELETE FROM merxa.audit_log WHERE entity_type='test'",
        "TRUNCATE merxa.audit_log",
    ):
        with pytest.raises(Exception, match=r"append-only|permission denied"):
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT action FROM merxa.audit_log WHERE entity_type='test'"))).all()
    assert [r.action for r in rows] == ["probe"]
