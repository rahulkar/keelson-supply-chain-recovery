"""Deterministic seed generator (``numpy.random.SeedSequence(20261005)``).

``generate("small")`` (62 stores, 30 SKUs — fast tests) and ``generate("full")`` (451 stores
in 16 markets, 180 SKUs — the demo default from M2) return a :class:`Dataset` whose tables
are loaded in :data:`TABLE_ORDER` (foreign keys). The supply plan is *derived from the
generated demand* so the demo story holds at SKU level for any random draw. Stock is
sized as **network** stock per DC x SKU: DC ``on_hand`` (which still includes the released
W41 allocations) **plus** the store on-hand of that DC's clusters, because consumers such
as the Keelson optimizer count store stock as cluster supply (``story.py`` mirrors that):

* ``UK1``/``NL1`` outerwear: network stock covers W41-W44 plus a small buffer; three
  Bangladesh POs per DC (ETA W44/W45/W46) feed the peak. Wave A (W44) covers part of W45
  (or all of it for ``W45_COVERED_STYLES``), wave B (W45) leaves ``STORY_W46_CARRY`` of W46
  demand in stock, wave C (W46, on the Chittagong sailings around the Cape) covers the
  rest. Shifting the PO lines by +7 days pushes each receipt one ISO week later: both DCs
  lose roughly ``(1 - STORY_W46_CARRY) x W46 demand`` in W45-W46, with first breaches in
  W45 and (for the W45-covered styles) W46.
* ``DE1``/``PL1`` outerwear: Cambodian stock (PO arrived W40) — 25-30 % more than the
  full-horizon demand in the core sizes but only ~4 % in ``TIGHT_SIZES``, so a DC-to-DC
  rebalance recovers most, not all, of the units at risk.
* ``full`` only — knit: ``UK1``/``NL1`` are fully stocked; ``DE1``/``PL1`` cover W41-W45
  from stock and depend on four Vietnamese POs (ETA W46/W47 via Ho Chi Minh City and the
  Suez Canal). A 10-day closure of VNSGN pushes both waves one week later: DE1/PL1 run short
  of knit in W46. A 21-day closure of Suez re-routes both sailings round the Cape (+12 days):
  DE1/PL1 run short of knit in W46, W47 and W48.

Forecasts are stored per **style x store x ISO week**: the cluster forecast (with noise)
is split over the cluster's stores by sales index (largest remainder), so cluster sums are
exact.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from merxa_api.calendar import DEMO_TODAY
from merxa_api.seed import reference as ref
from merxa_api.seed import reference_full as reff
from merxa_api.seed.dataset import Dataset
from merxa_api.seed.split import largest_remainder, share_split, sku_split

SEED_ENTROPY = 20261005
SEA_AIR_TRANSIT_DAYS = 16  # Chittagong → Jebel Ali (sea) → EU (air)
SEA_TRANSIT_DAYS = 35

TABLE_ORDER: tuple[str, ...] = (
    "market",
    "dc",
    "store_cluster",
    "store",
    "supplier",
    "factory",
    "component",
    "style",
    "sku",
    "bom_line",
    "size_curve",
    "purchase_order",
    "po_line",
    "inventory_position",
    "demand_forecast",
    "allocation",
    "transfer_order",
    "transfer_order_line",
    "supplier_message",
    "sim_clock",
    "port",
    "carrier",
    "lane",
    "rate_card",
    "sailing",
    "capacity_booking",
    "shipment",
    "shipment_line",
    "factory_line",
    "line_calendar",
    "production_order",
    "online_order",
    "online_order_line",
    "promotion",
    "sales_history",
)

PROFILES: tuple[str, ...] = ("small", "full")


class UnsupportedProfile(ValueError):
    """Unknown seed profile."""


@dataclass
class Spec:
    """Everything that differs between the profiles."""

    profile: str
    markets: Sequence[tuple[str, str, str]]
    dcs: Sequence[tuple[str, str, str, str, float, float, int, int]]
    suppliers: Sequence[tuple[str, str, str, str]]
    factories: Sequence[tuple[str, str, str, str]]
    components: Sequence[tuple[str, str, str, str]]
    styles: Sequence[reff.StyleRef]
    bom: dict[str, Sequence[tuple[str, str | None, int]]]
    base_curves: dict[str, Sequence[int]]
    climate_factor: dict[str, dict[str, float]]
    base_units_per_index: float
    allocation_weeks: Sequence[str]
    w45_covered: Sequence[str]
    online_orders: int
    stores: list[dict[str, Any]] = field(default_factory=list)


def generate(profile: str = "small") -> Dataset:
    if profile == "small":
        return _generate_small()
    if profile == "full":
        return _generate_full()
    raise UnsupportedProfile(f"unknown seed profile {profile!r} (expected 'small' or 'full')")


def _store_code(market: str, n: int) -> str:
    return f"ST-{market}-{n:02d}"


# ───────────────────────── profiles ─────────────────────────
def _generate_small() -> Dataset:
    ss = np.random.SeedSequence(SEED_ENTROPY)
    ss_stores, ss_curves, ss_demand = ss.spawn(3)
    (ss_extra,) = ss.spawn(1)
    rng_stores = np.random.default_rng(ss_stores)

    stores: list[dict[str, Any]] = []
    per_market: dict[str, int] = defaultdict(int)
    for s in ref.STORES:
        per_market[s.market] += 1
        lo, hi = ref.GRADE_INDEX_RANGE[s.grade]
        sales_index = round(float(rng_stores.uniform(lo, hi)), 3)
        dc_code = ref.MARKET_DC[s.market]
        band = "A" if s.grade == "A" else "BC"
        stores.append(
            {
                "code": _store_code(s.market, per_market[s.market]),
                "name": s.name,
                "market_code": s.market,
                "city": s.city,
                "lat": s.lat,
                "lon": s.lon,
                "grade": s.grade,
                "cluster_code": f"{dc_code}-{s.climate.upper()}-{band}",
                "home_dc_code": dc_code,
                "sales_index": Decimal(f"{sales_index:.3f}"),
                "_climate": s.climate,
            }
        )
    spec = Spec(
        profile="small",
        markets=ref.MARKETS,
        dcs=ref.DCS,
        suppliers=ref.SUPPLIERS,
        factories=ref.FACTORIES,
        components=ref.COMPONENTS,
        styles=[reff.StyleRef(c, n, col, r, cost, pop, ref.FAMILY) for c, n, col, r, cost, pop in ref.STYLES],
        bom={ref.FAMILY: ref.BOM_TEMPLATE},
        base_curves={ref.FAMILY: ref.BASE_SIZE_CURVE_BP},
        climate_factor={ref.FAMILY: ref.CLIMATE_FACTOR},
        base_units_per_index=ref.BASE_UNITS_PER_INDEX,
        allocation_weeks=ref.FORECAST_WEEKS,
        w45_covered=ref.W45_COVERED_STYLES,
        online_orders=80,
        stores=stores,
    )
    return _build(spec, np.random.default_rng(ss_curves), np.random.default_rng(ss_demand), ss_extra)


def _generate_full() -> Dataset:
    from merxa_api.seed.stores_full import place_stores

    ss = np.random.SeedSequence(SEED_ENTROPY)
    ss_stores, ss_curves, ss_demand, ss_extra = ss.spawn(4)
    stores = place_stores(np.random.default_rng(ss_stores))
    spec = Spec(
        profile="full",
        markets=reff.MARKETS,
        dcs=reff.DCS,
        suppliers=reff.SUPPLIERS,
        factories=reff.FACTORIES,
        components=reff.COMPONENTS,
        styles=reff.STYLES,
        bom={reff.FAMILY_OUTERWEAR: reff.BOM_OUTERWEAR, reff.FAMILY_KNIT: reff.BOM_KNIT},
        base_curves=reff.BASE_SIZE_CURVE_BP,
        climate_factor=reff.CLIMATE_FACTOR,
        base_units_per_index=reff.BASE_UNITS_PER_INDEX,
        allocation_weeks=reff.ALLOCATION_WEEKS,
        w45_covered=reff.W45_COVERED_STYLES,
        online_orders=600,
        stores=stores,
    )
    return _build(spec, np.random.default_rng(ss_curves), np.random.default_rng(ss_demand), ss_extra)


# ───────────────────────── core ─────────────────────────
def _build(spec: Spec, rng_curves: np.random.Generator, rng_demand: np.random.Generator, ss_extra: Any) -> Dataset:
    ds = Dataset(profile=spec.profile)
    weeks = list(ref.FORECAST_WEEKS)
    stores = spec.stores

    # ── master data ──
    for code, name, currency in spec.markets:
        ds.add("market", {"code": code, "name": name, "currency": currency})
    for code, name, country, city, lat, lon, cap_in, cap_out in spec.dcs:
        ds.add(
            "dc",
            {
                "code": code,
                "name": name,
                "country": country,
                "city": city,
                "lat": lat,
                "lon": lon,
                "inbound_cap_units_wk": cap_in,
                "outbound_cap_units_wk": cap_out,
            },
        )
    clusters: dict[str, dict[str, Any]] = {}
    for st in stores:
        cc = st["cluster_code"]
        if cc not in clusters:
            dc_code, climate, band = cc.split("-")
            clusters[cc] = {"code": cc, "dc_code": dc_code, "climate": climate.lower(), "grade_band": band}
    cluster_codes = sorted(clusters)
    ds.add_many("store_cluster", (clusters[c] for c in cluster_codes))
    ds.add_many("store", ({k: v for k, v in st.items() if not k.startswith("_")} for st in stores))
    for code, name, country, kind in spec.suppliers:
        ds.add("supplier", {"code": code, "name": name, "country": country, "kind": kind})
    for code, name, country, supplier in spec.factories:
        ds.add("factory", {"code": code, "name": name, "country": country, "supplier_code": supplier})
    for code, name, kind, supplier in spec.components:
        ds.add("component", {"code": code, "name": name, "kind": kind, "supplier_code": supplier})

    families = list(dict.fromkeys(s.family for s in spec.styles))
    style_by_code = {s.code: s for s in spec.styles}
    skus: list[dict[str, Any]] = []
    for s in spec.styles:
        ds.add(
            "style",
            {
                "code": s.code,
                "name": f"{s.name} — {s.colour}",
                "family": s.family,
                "season": ref.SEASON,
                "colour": s.colour,
                "retail_price_cents": s.retail_cents,
                "unit_cost_cents": s.cost_cents,
            },
        )
        for seq, size in enumerate(ref.SIZES, start=1):
            skus.append({"code": f"{s.code}-{size}", "style_code": s.code, "size": size, "size_seq": seq})
    ds.add_many("sku", skus)
    for s in spec.styles:
        for comp, fac, qty in spec.bom[s.family]:
            ds.add("bom_line", {"style_code": s.code, "component_code": comp, "factory_code": fac, "qty_per_unit": qty})

    # ── size curves per family × cluster (perturbed base curve, renormalised to 10 000 bp) ──
    curves: dict[tuple[str, str], list[int]] = {}
    for family in families:
        for cc in cluster_codes:
            noise = rng_curves.integers(-120, 121, size=len(ref.SIZES))
            weights = [max(100, b + int(n)) for b, n in zip(spec.base_curves[family], noise, strict=True)]
            curves[(family, cc)] = largest_remainder(10000, [float(w) for w in weights])
            for size, bp in zip(ref.SIZES, curves[(family, cc)], strict=True):
                ds.add("size_curve", {"family": family, "cluster_code": cc, "size": size, "share_bp": bp})

    # ── weekly cluster demand per style, split over the cluster's stores ──
    stores_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cluster_index: dict[str, float] = defaultdict(float)
    cluster_climate: dict[str, str] = {}
    for st in stores:
        stores_by_cluster[st["cluster_code"]].append(st)
        cluster_index[st["cluster_code"]] += float(st["sales_index"])
        cluster_climate[st["cluster_code"]] = st["_climate"]
    store_weights = {cc: [float(m["sales_index"]) for m in stores_by_cluster[cc]] for cc in cluster_codes}
    forecast: dict[tuple[str, str, str], int] = {}
    fc_rows: list[tuple[Any, ...]] = []
    for s in spec.styles:
        for cc in cluster_codes:
            members = stores_by_cluster[cc]
            for wk in weeks:
                mean = (
                    cluster_index[cc]
                    * spec.base_units_per_index
                    * s.popularity
                    * spec.climate_factor[s.family][cluster_climate[cc]]
                    * ref.WEEK_UPLIFT[wk]
                )
                noise = float(np.clip(rng_demand.normal(1.0, 0.04), 0.9, 1.1))
                units = max(1, round(mean * noise))
                forecast[(s.code, cc, wk)] = units
                for st, qty in zip(members, largest_remainder(units, store_weights[cc]), strict=True):
                    fc_rows.append((s.code, cc, st["code"], wk, qty))
    ds.put("demand_forecast", ("style_code", "cluster_code", "store_code", "iso_week", "units"), fc_rows)

    # SKU × DC × week demand (exactly what story.py recomputes from the DB)
    dc_codes = [d[0] for d in spec.dcs]
    sku_codes = [s["code"] for s in skus]
    style_of = {s["code"]: s["style_code"] for s in skus}
    size_of = {s["code"]: s["size"] for s in skus}
    family_of_sku = {s["code"]: style_by_code[s["style_code"]].family for s in skus}
    demand: dict[tuple[str, str], dict[str, int]] = {
        (dc, sku): dict.fromkeys(weeks, 0) for dc in dc_codes for sku in sku_codes
    }
    # Rounding slack: how many more units a consumer splitting with ``share_split`` would
    # see over the horizon (end-of-horizon buffers absorb it).
    rounding_slack: dict[tuple[str, str], int] = defaultdict(int)
    for (style_code, cc, wk), units in forecast.items():
        dc_code = clusters[cc]["dc_code"]
        curve = curves[(style_by_code[style_code].family, cc)]
        alt = share_split(units, curve)
        for size, qty, qty_alt in zip(ref.SIZES, sku_split(units, curve), alt, strict=True):
            demand[(dc_code, f"{style_code}-{size}")][wk] += qty
            rounding_slack[(dc_code, f"{style_code}-{size}")] += qty_alt - qty

    # ── allocations (store share of cluster SKU demand); W41 released, later weeks planned ──
    released_at_dc: dict[tuple[str, str], int] = defaultdict(int)
    store_w41: dict[tuple[str, str], int] = defaultdict(int)
    alloc_rows: list[tuple[str, str, str, str, int, str]] = []
    for wk in weeks:
        keep = wk in spec.allocation_weeks
        if not keep and wk != "2026-W41":
            continue
        for cc in cluster_codes:
            members = stores_by_cluster[cc]
            for s in spec.styles:
                size_units = sku_split(forecast[(s.code, cc, wk)], curves[(s.family, cc)])
                for size, units in zip(ref.SIZES, size_units, strict=True):
                    sku_code = f"{s.code}-{size}"
                    for st, qty in zip(members, largest_remainder(units, store_weights[cc]), strict=True):
                        if qty <= 0:
                            continue
                        status = "released" if wk == "2026-W41" else "planned"
                        alloc_rows.append((sku_code, st["home_dc_code"], st["code"], wk, qty, status))
                        if status == "released":
                            released_at_dc[(st["home_dc_code"], sku_code)] += qty
                            store_w41[(st["code"], sku_code)] += qty
    alloc_rows.sort(key=lambda r: (r[3], r[2], r[0]))
    ds.put(
        "allocation",
        ("code", "sku_code", "dc_code", "store_code", "ship_week", "qty", "status"),
        [(f"AL-{n:06d}", *r) for n, r in enumerate(alloc_rows, start=1)],
    )

    # ── store stock: 1.5 weeks of the W41 allocation on hand (+ the W41 release in transit) ──
    store_on_hand: dict[tuple[str, str], int] = {}
    cluster_store_stock: dict[tuple[str, str], int] = defaultdict(int)  # (dc, sku) → Σ store on-hand
    for st in stores:
        for sku_code in sku_codes:
            qty = round(store_w41[(st["code"], sku_code)] * 1.5)
            store_on_hand[(st["code"], sku_code)] = qty
            cluster_store_stock[(st["home_dc_code"], sku_code)] += qty

    # ── supply plan derived from demand ──
    plan = _SupplyPlan()
    for dc_code in dc_codes:
        for sku_code in sku_codes:
            key = (dc_code, sku_code)
            ctx = _PlanInput(
                d=demand[key],
                allocated=released_at_dc[key],
                store_stock=cluster_store_stock[key],
                slack=max(0, rounding_slack[key]),
            )
            family = family_of_sku[sku_code]
            if family == reff.FAMILY_KNIT:
                if dc_code in ref.SHORT_DCS:
                    plan.surplus(key, ctx, reff.KNIT_SURPLUS_RATIO)
                else:
                    plan.vietnam(key, ctx)
            elif dc_code in ref.SHORT_DCS:
                plan.bangladesh(key, ctx, covered=style_of[sku_code] in spec.w45_covered)
            else:
                tight = size_of[sku_code] in ref.TIGHT_SIZES
                plan.surplus(key, ctx, ref.TIGHT_SURPLUS_RATIO if tight else ref.SURPLUS_RATIO[dc_code])

    outer_skus = [s for s in sku_codes if family_of_sku[s] != reff.FAMILY_KNIT]
    knit_skus = [s for s in sku_codes if family_of_sku[s] == reff.FAMILY_KNIT]
    _purchase_orders(ds, plan, outer_skus, knit_skus)

    # Historic DE1 → PL1 replenishment transfer (received W40).
    ds.add(
        "transfer_order",
        {
            "code": "TO-000001",
            "from_dc": "DE1",
            "to_dc": "PL1",
            "ship_date": date(2026, 9, 30),
            "eta_date": date(2026, 10, 1),
            "status": "received",
            "created_via": "ui",
        },
    )
    for line_no, sku_code in enumerate(outer_skus, start=1):
        ds.add(
            "transfer_order_line",
            {
                "to_code": "TO-000001",
                "line_no": line_no,
                "sku_code": sku_code,
                "qty": max(1, round(0.4 * plan.atp_open[("PL1", sku_code)])),
            },
        )

    # ── inventory positions ──
    inv_rows: list[tuple[Any, ...]] = []
    for dc_code in dc_codes:
        for sku_code in sku_codes:
            inv_rows.append(
                (
                    "dc",
                    dc_code,
                    sku_code,
                    plan.dc_on_hand[(dc_code, sku_code)],
                    released_at_dc[(dc_code, sku_code)],
                    0,
                    0,
                )
            )
    for st in stores:
        for sku_code in sku_codes:
            weekly = store_w41[(st["code"], sku_code)]
            inv_rows.append(("store", st["code"], sku_code, store_on_hand[(st["code"], sku_code)], 0, 0, weekly))
    ds.put(
        "inventory_position",
        ("location_type", "location_code", "sku_code", "on_hand", "allocated", "reserved", "in_transit"),
        inv_rows,
    )

    # ── supplier portal inbox (untrusted free text) ──
    messages = [
        {
            "supplier_code": "CMT-KH-01",
            "received_at": datetime(2026, 9, 29, 8, 12, tzinfo=UTC),
            "subject": "PO-KH-0001 delivered to Erfurt DC",
            "body": "Dear team, PO-KH-0001 was delivered complete to Erfurt DC today. POD attached in the portal. "
            "Kind regards, Mekong Garment Industries logistics.",
            "status": "read",
        },
        {
            "supplier_code": "TRIM-BD-01",
            "received_at": datetime(2026, 10, 1, 6, 40, tzinfo=UTC),
            "subject": "Durga Puja holiday schedule",
            "body": "Please note our Gazipur warehouse operates reduced hours 17-21 October for Durga Puja. "
            "Trim deliveries already scheduled for your Bangladesh factories are not affected.",
            "status": "read",
        },
    ]
    if spec.profile == "full":
        messages.append(
            {
                "supplier_code": "SUP-VN-01",
                "received_at": datetime(2026, 10, 3, 2, 5, tzinfo=UTC),
                "subject": "PO-VN-0001 / PO-VN-0002 gated in at Cat Lai terminal",
                "body": "Good morning, both W46 knitwear orders were gated in at Cat Lai (Ho Chi Minh City) on "
                "3 October for the 6 October sailing to Hamburg. The W47 orders are on schedule for the "
                "13 October sailing to Gdansk. Best regards, Saigon Knit Co. shipping desk.",
                "status": "read",
            }
        )
    ds.add_many("supplier_message", messages)
    ds.add("sim_clock", {"singleton": True, "today": DEMO_TODAY})

    from merxa_api.seed import network

    network.build(ds, spec, ss_extra)
    for table in TABLE_ORDER:
        if table not in ds.tables:
            raise AssertionError(f"seed table {table} was not generated")
    return ds


# ───────────────────────── supply plan ─────────────────────────
@dataclass
class _PlanInput:
    d: dict[str, int]
    allocated: int
    store_stock: int
    slack: int


@dataclass
class _SupplyPlan:
    dc_on_hand: dict[tuple[str, str], int] = field(default_factory=dict)
    atp_open: dict[tuple[str, str], int] = field(default_factory=dict)
    bd_qty: dict[tuple[str, str], tuple[int, int, int]] = field(default_factory=dict)
    vn_qty: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def _stock(self, key: tuple[str, str], ctx: _PlanInput, network_open: int) -> int:
        """DC on-hand for a target network stock; returns the actual network stock."""
        on_hand = max(ctx.allocated, network_open - ctx.store_stock)
        self.dc_on_hand[key] = on_hand
        self.atp_open[key] = on_hand - ctx.allocated
        return on_hand + ctx.store_stock

    def surplus(self, key: tuple[str, str], ctx: _PlanInput, ratio: float) -> None:
        """Network stock (DC + store on-hand) = full-horizon demand + surplus."""
        cum = sum(ctx.d.values())
        self._stock(key, ctx, cum + max(ref.SURPLUS_MIN_UNITS, round(ratio * cum)) + ctx.slack)

    def bangladesh(self, key: tuple[str, str], ctx: _PlanInput, *, covered: bool) -> None:
        d = ctx.d
        weeks = list(d)
        d41, d45, d46, d49 = d["2026-W41"], d["2026-W45"], d["2026-W46"], d["2026-W49"]
        cum49 = sum(d.values())
        cum44 = sum(d[w] for w in weeks[:4])
        b1 = max(1, round(ref.STORY_PRE_A_BUFFER * d41))
        b2 = max(1, round(ref.STORY_W46_CARRY * d46))
        b3 = max(ref.STORY_END_MIN_UNITS, round(ref.STORY_END_BUFFER * d49)) + ctx.slack
        # Network opening stock (DC incl. released allocations + the clusters' store
        # on-hand) covers W41–W44 plus b1. Consumers (the optimizer's impact model)
        # count store stock as cluster supply, so it is netted out of the DC.
        network_open = self._stock(key, ctx, cum44 + b1)
        if covered:
            qty_a = max(1, d45 - b1 + max(1, round(ref.STORY_W45_COVER * d45)))
        else:
            qty_a = max(1, round(0.33 * d45))
        qty_b = max(1, d45 + b2 - b1 - qty_a)
        qty_c = max(1, cum49 - (network_open + qty_a + qty_b) + b3)
        self.bd_qty[key] = (qty_a, qty_b, qty_c)

    def vietnam(self, key: tuple[str, str], ctx: _PlanInput) -> None:
        d = ctx.d
        weeks = list(d)
        cum45 = sum(d[w] for w in weeks[:5])
        cum49 = sum(d.values())
        b1 = max(1, round(reff.VN_PRE_BUFFER * d["2026-W45"]))
        carry = max(1, round(reff.VN_W47_CARRY * d["2026-W47"]))
        b3 = max(ref.STORY_END_MIN_UNITS, round(ref.STORY_END_BUFFER * d["2026-W49"])) + ctx.slack
        network_open = self._stock(key, ctx, cum45 + b1)
        qty1 = max(1, d["2026-W46"] + carry - (network_open - cum45))
        qty2 = max(1, cum49 - (network_open + qty1) + b3)
        self.vn_qty[key] = (qty1, qty2)


# ───────────────────────── purchase orders ─────────────────────────
# Bangladesh POs: (code, dc, factory, eta, status, wave). Waves A/B ship by sea-air via
# Jebel Ali; wave C rides the Chittagong sailings around the Cape (cutoff 6 Oct).
BD_POS: tuple[tuple[str, str, str, date, str, int], ...] = (
    ("PO-BD-0001", "UK1", "F-BD-01", date(2026, 10, 29), "in_production", 0),
    ("PO-BD-0002", "UK1", "F-BD-02", date(2026, 11, 3), "in_production", 1),
    ("PO-BD-0003", "UK1", "F-BD-01", date(2026, 11, 10), "open", 2),
    ("PO-BD-0004", "NL1", "F-BD-02", date(2026, 10, 29), "in_production", 0),
    ("PO-BD-0005", "NL1", "F-BD-01", date(2026, 11, 3), "in_production", 1),
    ("PO-BD-0006", "NL1", "F-BD-02", date(2026, 11, 10), "open", 2),
)
BD_WAVE_C_EX_FACTORY = date(2026, 10, 5)
# Vietnamese knit POs (full): (code, dc, eta, status, wave, ex-factory)
VN_POS: tuple[tuple[str, str, date, str, int, date], ...] = (
    ("PO-VN-0001", "DE1", date(2026, 11, 11), "shipped", 0, date(2026, 10, 2)),
    ("PO-VN-0002", "PL1", date(2026, 11, 12), "shipped", 0, date(2026, 10, 2)),
    ("PO-VN-0003", "PL1", date(2026, 11, 19), "in_production", 1, date(2026, 10, 9)),
    ("PO-VN-0004", "DE1", date(2026, 11, 20), "in_production", 1, date(2026, 10, 9)),
)


def _po_header(code: str, supplier: str, factory: str, dc: str, status: str, ex_factory: date, eta: date) -> dict:
    return {
        "code": code,
        "supplier_code": supplier,
        "factory_code": factory,
        "dest_dc_code": dc,
        "status": status,
        "ex_factory_date": ex_factory,
        "eta_dc": eta,
    }


def _po_lines(
    ds: Dataset, code: str, skus: Sequence[str], qty: Callable[[str], int], eta: date, status: str, received: bool
) -> None:
    for line_no, sku_code in enumerate(skus, start=1):
        q = qty(sku_code)
        ds.add(
            "po_line",
            {
                "po_code": code,
                "line_no": line_no,
                "sku_code": sku_code,
                "qty_ordered": q,
                "qty_received": q if received else 0,
                "eta_dc": eta,
                "status": status,
            },
        )


def _purchase_orders(ds: Dataset, plan: _SupplyPlan, outer_skus: list[str], knit_skus: list[str]) -> None:
    for code, dc_code, factory_code, eta, status, wave in BD_POS:
        ex_factory = BD_WAVE_C_EX_FACTORY if wave == 2 else eta - timedelta(days=SEA_AIR_TRANSIT_DAYS)
        ds.add("purchase_order", _po_header(code, "CMT-BD-01", factory_code, dc_code, status, ex_factory, eta))
        _po_lines(ds, code, outer_skus, lambda s, dc=dc_code, w=wave: plan.bd_qty[(dc, s)][w], eta, status, False)

    # Cambodia PO → DE1, received in W40 (history; explains DE1's stock).
    kh_eta = date(2026, 9, 29)
    ds.add(
        "purchase_order",
        _po_header(
            "PO-KH-0001", "CMT-KH-01", "F-KH-01", "DE1", "received", kh_eta - timedelta(days=SEA_TRANSIT_DAYS), kh_eta
        ),
    )
    _po_lines(
        ds, "PO-KH-0001", outer_skus, lambda s: max(1, round(0.7 * plan.atp_open[("DE1", s)])), kh_eta, "received", True
    )
    if not knit_skus:
        return
    for code, dc_code, eta, status, wave, ex_factory in VN_POS:
        ds.add("purchase_order", _po_header(code, "SUP-VN-01", "F-VN-01", dc_code, status, ex_factory, eta))
        _po_lines(ds, code, knit_skus, lambda s, dc=dc_code, w=wave: plan.vn_qty[(dc, s)][w], eta, status, False)
    # Near-shore knit PO from Istanbul → NL1, received in W39 (history).
    tr_eta = date(2026, 9, 22)
    ds.add(
        "purchase_order",
        _po_header("PO-TR-0001", "CMT-TR-01", "F-TR-01", "NL1", "received", tr_eta - timedelta(days=6), tr_eta),
    )
    _po_lines(
        ds, "PO-TR-0001", knit_skus, lambda s: max(1, round(0.5 * plan.atp_open[("NL1", s)])), tr_eta, "received", True
    )


TRIM_DELAY_MESSAGE: dict[str, str] = {
    "supplier_code": "TRIM-BD-01",
    "subject": "URGENT: wadding and zip deliveries delayed 7 days",
    "body": (
        "Dear Merxa buying team, due to a dyeing-line breakdown at our Tongi plant, deliveries of "
        "CMP-WAD-01 (polyester wadding) and CMP-ZIP-01 (two-way zips) to Gazipur Outerwear Unit 1 (F-BD-01) "
        "and Narayanganj Outerwear Unit 2 (F-BD-02) will arrive 7 days later than planned. Garment ex-factory "
        "dates for all open puffer jacket orders will move by about one week. We apologise for the "
        "inconvenience. Meghna Trims Ltd, customer service."
    ),
}

TYPHOON_MESSAGE: dict[str, str] = {
    "supplier_code": "SUP-VN-01",
    "subject": "Typhoon: Cat Lai terminal (Ho Chi Minh City) closed for about 10 days",
    "body": (
        "Dear partners, following the typhoon landfall the port authority has closed Cat Lai terminal "
        "(VNSGN). Vessel departures are suspended for about 10 days; our gated-in knitwear for Hamburg and "
        "the Gdansk sailing will leave once operations resume. Expect arrivals in Europe roughly 10 days "
        "later than confirmed. Saigon Knit Co. shipping desk."
    ),
}
