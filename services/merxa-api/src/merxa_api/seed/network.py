"""Seed builders for the M2 domain (shared by both profiles): ports, carriers, lanes, rate
cards, sailings, capacity bookings, shipments, factory lines + calendars, production orders,
online orders, promotions and sales history.

Logistics story (sim today 2026-10-05, W41):

* Bangladesh waves A/B (ETA W44/W45) ship sea-air via Jebel Ali (``CGP-UK1-SA`` /
  ``CGP-NL1-SA``, confirmed capacity bookings); wave C (ETA W46) is gated in for the
  Chittagong sailings around the Cape (``SAIL-0001`` → Felixstowe, ``SAIL-0002`` →
  Rotterdam, cutoff 6 Oct). Their production orders run on the Bangladesh factory lines.
* Vietnamese knit (``full``): W46 orders wait at Cat Lai for ``SAIL-0003`` (→ Hamburg,
  ETD 6 Oct); W47 orders are in production for ``SAIL-0004`` (→ Gdańsk, ETD 13 Oct). Both
  lanes run **via Suez** (``via: ["SUEZ"]``); their ``alt_lane_code`` is the Cape route
  (``SGN-HAM-CAPE`` / ``SGN-GDN-CAPE``, +12 days, own rate cards, no sailings of their own):
  a Suez closure re-routes sailings departing inside the window round the Cape.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from merxa_api.calendar import iso_week
from merxa_api.seed import reference_full as reff
from merxa_api.seed.dataset import Dataset, DateRange

TODAY = date(2026, 10, 5)
STD = DateRange(date(2026, 7, 1), date(2026, 11, 1))
PEAK = DateRange(date(2026, 11, 1), date(2027, 3, 1))
SEASON = DateRange(date(2026, 7, 1), date(2027, 3, 1))

# unlocode, name, country, lat, lon, kind
PORTS: tuple[tuple[str, str, str, float, float, str], ...] = (
    ("BDCGP", "Chittagong", "BD", 22.3089, 91.8000, "sea"),
    ("LKCMB", "Colombo", "LK", 6.9497, 79.8428, "sea"),
    ("VNSGN", "Ho Chi Minh City (Cat Lai)", "VN", 10.7626, 106.7880, "sea"),
    ("KHPNH", "Phnom Penh", "KH", 11.5730, 104.9282, "sea"),
    ("NLRTM", "Rotterdam", "NL", 51.9496, 4.1453, "sea"),
    ("GBFXT", "Felixstowe", "GB", 51.9563, 1.3301, "sea"),
    ("DEHAM", "Hamburg", "DE", 53.5395, 9.9663, "sea"),
    ("PLGDN", "Gdańsk", "PL", 54.4013, 18.6763, "sea"),
    ("AEJEA", "Jebel Ali", "AE", 25.0115, 55.0618, "sea"),
    ("TRIST", "Istanbul (Ambarli)", "TR", 40.9667, 28.6833, "sea"),
    ("MATNG", "Tanger Med", "MA", 35.8833, -5.5000, "sea"),
)

CARRIERS: tuple[tuple[str, str, str], ...] = (
    ("MSCU", "Mediterranean Shipping Company", "sea"),
    ("MAEU", "Maersk", "sea"),
    ("EKSC", "Emirates SkyCargo (sea-air)", "air"),
    ("GXOR", "GXO Road Europe", "road"),
    ("DSVR", "DSV Road", "road"),
)

# code, origin, dest, mode, carrier, transit days, via, alt lane
LANES: tuple[tuple[str, str, str, str, str, int, tuple[str, ...], str | None], ...] = (
    ("CGP-FXT-SEA", "BDCGP", "GBFXT", "sea", "MSCU", 32, ("LKCMB", "CAPE"), "CGP-UK1-SA"),
    ("CGP-RTM-SEA", "BDCGP", "NLRTM", "sea", "MSCU", 32, ("LKCMB", "CAPE"), "CGP-NL1-SA"),
    ("CGP-UK1-SA", "BDCGP", "UK1", "sea_air", "EKSC", 16, ("AEJEA",), "CGP-FXT-SEA"),
    ("CGP-NL1-SA", "BDCGP", "NL1", "sea_air", "EKSC", 16, ("AEJEA",), "CGP-RTM-SEA"),
    # Vietnam → North Europe runs via Suez (carriers partly back in the Red Sea since Sept 2026);
    # the Cape of Good Hope lanes are the catalogued fallback (+12 days).
    ("SGN-HAM-SEA", "VNSGN", "DEHAM", "sea", "MAEU", 35, ("SUEZ",), "SGN-HAM-CAPE"),
    ("SGN-GDN-SEA", "VNSGN", "PLGDN", "sea", "MAEU", 36, ("SUEZ",), "SGN-GDN-CAPE"),
    ("SGN-HAM-CAPE", "VNSGN", "DEHAM", "sea", "MAEU", 47, ("CAPE",), None),
    ("SGN-GDN-CAPE", "VNSGN", "PLGDN", "sea", "MAEU", 48, ("CAPE",), None),
    ("SGN-DE1-SA", "VNSGN", "DE1", "sea_air", "EKSC", 15, ("AEJEA",), "SGN-HAM-SEA"),
    ("SGN-PL1-SA", "VNSGN", "PL1", "sea_air", "EKSC", 16, ("AEJEA",), "SGN-GDN-SEA"),
    ("PNH-SGN-RIV", "KHPNH", "VNSGN", "road", "GXOR", 2, (), None),
    ("FXT-UK1-RD", "GBFXT", "UK1", "road", "GXOR", 1, (), None),
    ("RTM-NL1-RD", "NLRTM", "NL1", "road", "DSVR", 1, (), None),
    ("HAM-DE1-RD", "DEHAM", "DE1", "road", "DSVR", 1, (), None),
    ("HAM-PL1-RD", "DEHAM", "PL1", "road", "DSVR", 2, (), None),
    ("GDN-PL1-RD", "PLGDN", "PL1", "road", "DSVR", 1, (), None),
    ("GDN-DE1-RD", "PLGDN", "DE1", "road", "DSVR", 2, (), None),
    ("IST-DE1-RD", "TRIST", "DE1", "road", "GXOR", 5, (), None),
    ("IST-NL1-RD", "TRIST", "NL1", "road", "GXOR", 6, (), None),
    ("TNG-NL1-RD", "MATNG", "NL1", "road", "GXOR", 5, (), None),
    ("TNG-UK1-RD", "MATNG", "UK1", "road", "GXOR", 6, (), None),
    # DC ↔ DC transfer lanes (mirror infra/tenants.yaml)
    ("DE1-NL1-RD", "DE1", "NL1", "road", "DSVR", 2, (), None),
    ("DE1-UK1-RD", "DE1", "UK1", "road", "DSVR", 4, (), None),
    ("PL1-NL1-RD", "PL1", "NL1", "road", "DSVR", 3, (), None),
    ("PL1-DE1-RD", "PL1", "DE1", "road", "DSVR", 2, (), None),
    ("NL1-UK1-RD", "NL1", "UK1", "road", "DSVR", 2, (), None),
)

# lane → (period, basis, rate cents, weekly capacity); sea-air capacity is sized from bookings.
ROAD_RATES: dict[str, tuple[int, int]] = {
    "PNH-SGN-RIV": (8, 30000),
    "FXT-UK1-RD": (18, 60000),
    "RTM-NL1-RD": (16, 60000),
    "HAM-DE1-RD": (20, 50000),
    "HAM-PL1-RD": (32, 40000),
    "GDN-PL1-RD": (15, 40000),
    "GDN-DE1-RD": (30, 40000),
    "IST-DE1-RD": (55, 15000),
    "IST-NL1-RD": (62, 15000),
    "TNG-NL1-RD": (58, 15000),
    "TNG-UK1-RD": (70, 12000),
    "DE1-NL1-RD": (35, 20000),
    "DE1-UK1-RD": (60, 12000),
    "PL1-NL1-RD": (45, 15000),
    "PL1-DE1-RD": (30, 15000),
    "NL1-UK1-RD": (50, 15000),
}
SEA_RATES: dict[str, tuple[int, int]] = {  # per TEU, TEU per week
    "CGP-FXT-SEA": (380000, 400),
    "CGP-RTM-SEA": (370000, 400),
    "SGN-HAM-SEA": (350000, 350),
    "SGN-GDN-SEA": (360000, 300),
    "SGN-HAM-CAPE": (395000, 350),
    "SGN-GDN-CAPE": (405000, 300),
}
SEA_AIR_RATES: dict[str, tuple[int, int]] = {  # per unit: standard, peak (from 1 Nov)
    "CGP-UK1-SA": (145, 185),
    "CGP-NL1-SA": (140, 180),
    "SGN-DE1-SA": (160, 200),
    "SGN-PL1-SA": (165, 205),
}

# code, lane, vessel, cutoff, etd, eta (port), capacity TEU
SAILINGS: tuple[tuple[str, str, str, date, date, date, int], ...] = (
    ("SAIL-0001", "CGP-FXT-SEA", "MSC Meghna", date(2026, 10, 6), date(2026, 10, 8), date(2026, 11, 9), 1200),
    ("SAIL-0002", "CGP-RTM-SEA", "MSC Karnaphuli", date(2026, 10, 6), date(2026, 10, 8), date(2026, 11, 9), 1100),
    ("SAIL-0003", "SGN-HAM-SEA", "Maersk Mekong", date(2026, 10, 3), date(2026, 10, 6), date(2026, 11, 10), 1400),
    ("SAIL-0004", "SGN-GDN-SEA", "Maersk Saigon", date(2026, 10, 10), date(2026, 10, 13), date(2026, 11, 18), 1300),
)

# factory → [(line code, capabilities)], minutes/day
FACTORY_LINES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "F-BD-01": (("FL-BD01-A", ("outerwear", "padding")), ("FL-BD01-B", ("outerwear", "padding"))),
    "F-BD-02": (("FL-BD02-A", ("outerwear", "padding")), ("FL-BD02-B", ("outerwear", "padding"))),
    "F-KH-01": (("FL-KH01-A", ("outerwear", "padding")),),
    "F-VN-01": (("FL-VN01-A", ("knit", "fleece")), ("FL-VN01-B", ("knit", "fleece"))),
    "F-TR-01": (("FL-TR01-A", ("outerwear", "knit")),),
    "F-MA-01": (("FL-MA01-A", ("outerwear",)),),
}
LINE_MINUTES_DAY = 28800  # 60 operators × 480 min
CALENDAR_FROM = date(2026, 9, 21)
CALENDAR_TO = date(2026, 12, 6)
BD_HOLIDAYS = {date(2026, 10, 20)}  # Durga Puja (Bijoya Dashami)
PRODUCTION_WINDOW_DAYS = 10


def build(ds: Dataset, spec: Any, ss_extra: Any) -> None:
    rng_orders, rng_history = (np.random.default_rng(s) for s in ss_extra.spawn(2))
    pos = {p["code"]: p for p in ds.dicts("purchase_order")}
    lines_by_po: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ln in ds.iter_dicts("po_line"):
        lines_by_po[ln["po_code"]].append(ln)
    factory_country = {f["code"]: f["country"] for f in ds.dicts("factory")}

    _logistics(ds, pos, lines_by_po, factory_country)
    _production(ds, pos, lines_by_po)
    _online_orders(ds, spec, rng_orders)
    _promotions(ds, spec)
    _sales_history(ds, rng_history)


# ───────────────────────── logistics ─────────────────────────
def _logistics(
    ds: Dataset,
    pos: dict[str, dict[str, Any]],
    lines_by_po: dict[str, list[dict[str, Any]]],
    factory_country: dict[str, str],
) -> None:
    for unlocode, name, country, lat, lon, kind in PORTS:
        ds.add("port", {"unlocode": unlocode, "name": name, "country": country, "lat": lat, "lon": lon, "kind": kind})
    for code, name, mode in CARRIERS:
        ds.add("carrier", {"code": code, "name": name, "mode": mode})
    for code, origin, dest, mode, carrier, transit, via, alt in LANES:
        ds.add(
            "lane",
            {
                "code": code,
                "origin": origin,
                "dest": dest,
                "mode": mode,
                "carrier_code": carrier,
                "transit_days": transit,
                "via": list(via),
                "alt_lane_code": alt,
            },
        )

    # Capacity bookings for the sea-air waves (confirmed, week of departure).
    bookings: list[dict[str, Any]] = []
    shipments: list[dict[str, Any]] = []
    ship_lines: list[dict[str, Any]] = []
    booked: dict[tuple[str, str], int] = defaultdict(int)
    sailing_by_lane = {s[1]: s for s in SAILINGS}
    for code in sorted(pos):
        po = pos[code]
        lines = sorted(lines_by_po[code], key=lambda ln: ln["line_no"])
        total = sum(ln["qty_ordered"] for ln in lines)
        country = factory_country[po["factory_code"]]
        dc = po["dest_dc_code"]
        shp = {
            "code": f"SHP-{len(shipments) + 1:06d}",
            "po_code": code,
            "mode": "sea",
            "lane_code": None,
            "sailing_code": None,
            "booking_code": None,
            "origin": "",
            "dest_dc_code": dc,
            "etd": po["ex_factory_date"],
            "eta": po["eta_dc"],
            "ata": None,
            "status": "booked",
            "created_via": "ui",
        }
        if country == "BD" and po["status"] == "in_production":
            lane = f"CGP-{dc}-SA"
            etd = po["ex_factory_date"] + timedelta(days=1)
            bk = f"CB-{len(bookings) + 1:04d}"
            bookings.append(
                {
                    "code": bk,
                    "lane_code": lane,
                    "iso_week": iso_week(etd),
                    "qty_units": total,
                    "basis": "per_unit",
                    "status": "confirmed",
                    "expires_at": None,
                    "created_via": "ui",
                }
            )
            booked[(lane, iso_week(etd))] += total
            shp.update(mode="sea_air", lane_code=lane, booking_code=bk, origin="BDCGP", etd=etd)
        elif country == "BD":
            lane = "CGP-FXT-SEA" if dc == "UK1" else "CGP-RTM-SEA"
            sail = sailing_by_lane[lane]
            shp.update(mode="sea", lane_code=lane, sailing_code=sail[0], origin="BDCGP", etd=sail[4])
        elif country == "VN":
            lane = "SGN-HAM-SEA" if po["status"] == "shipped" else "SGN-GDN-SEA"
            sail = sailing_by_lane[lane]
            shp.update(
                mode="sea",
                lane_code=lane,
                sailing_code=sail[0],
                origin="VNSGN",
                etd=sail[4],
                status="booked" if po["status"] == "shipped" else "planned",
            )
        elif country == "KH":
            shp.update(
                mode="sea",
                lane_code="SGN-HAM-SEA",
                origin="KHPNH",
                etd=po["ex_factory_date"] + timedelta(days=3),
                ata=po["eta_dc"],
                status="arrived",
            )
        else:  # near-shore road (TR / MA)
            shp.update(
                mode="road",
                lane_code=f"IST-{dc}-RD" if country == "TR" else f"TNG-{dc}-RD",
                origin="TRIST" if country == "TR" else "MATNG",
                etd=po["ex_factory_date"],
                ata=po["eta_dc"],
                status="arrived",
            )
        shipments.append(shp)
        for i, ln in enumerate(lines, start=1):
            ship_lines.append(
                {
                    "shipment_code": shp["code"],
                    "line_no": i,
                    "po_line_ref": f"{code}#{ln['line_no']}",
                    "sku_code": ln["sku_code"],
                    "qty": ln["qty_ordered"],
                }
            )

    for lane, (rate, cap) in sorted(ROAD_RATES.items()):
        _rate(ds, lane, SEASON, "per_unit", rate, cap)
    for lane, (rate, cap) in sorted(SEA_RATES.items()):
        _rate(ds, lane, SEASON, "per_teu", rate, cap)
    for lane, (std, peak) in sorted(SEA_AIR_RATES.items()):
        peak_booked = max([q for (ln, _), q in booked.items() if ln == lane] or [0])
        cap = max(20000, math.ceil(1.5 * peak_booked / 1000) * 1000)
        _rate(ds, lane, STD, "per_unit", std, cap)
        _rate(ds, lane, PEAK, "per_unit", peak, int(cap * 0.75))

    for code, lane, vessel, cutoff, etd, eta, teu in SAILINGS:
        ds.add(
            "sailing",
            {
                "code": code,
                "lane_code": lane,
                "vessel": vessel,
                "cutoff": cutoff,
                "etd": etd,
                "eta": eta,
                "capacity_teu": teu,
                "status": "scheduled",
            },
        )
    ds.ensure(
        "capacity_booking",
        ("code", "lane_code", "iso_week", "qty_units", "basis", "status", "expires_at", "created_via"),
    )
    ds.add_many("capacity_booking", bookings)
    ds.add_many("shipment", shipments)
    ds.add_many("shipment_line", ship_lines)


def _rate(ds: Dataset, lane: str, valid: DateRange, basis: str, rate: int, cap: int) -> None:
    ds.add(
        "rate_card",
        {
            "lane_code": lane,
            "valid": valid,
            "basis": basis,
            "rate_cents": rate,
            "currency": "EUR",
            "weekly_capacity": cap,
        },
    )


# ───────────────────────── production ─────────────────────────
def _closed(factory: str, day: date) -> tuple[str, int]:
    if factory.startswith("F-BD-"):
        if day.weekday() == 4 or day in BD_HOLIDAYS:  # Friday weekend + holiday
            return "closed", 0
        return "open", LINE_MINUTES_DAY
    if day.weekday() == 6:
        return "closed", 0
    if day.weekday() == 5:
        return "reduced", LINE_MINUTES_DAY // 2
    return "open", LINE_MINUTES_DAY


def _production(ds: Dataset, pos: dict[str, dict[str, Any]], lines_by_po: dict[str, list[dict[str, Any]]]) -> None:
    factories = {f["code"] for f in ds.dicts("factory")}
    cal_rows: list[tuple[Any, ...]] = []
    for factory, lines in FACTORY_LINES.items():
        if factory not in factories:
            continue
        for code, caps in lines:
            ds.add(
                "factory_line",
                {
                    "code": code,
                    "factory_code": factory,
                    "capabilities": list(caps),
                    "std_minutes_day": LINE_MINUTES_DAY,
                },
            )
            day = CALENDAR_FROM
            while day <= CALENDAR_TO:
                status, minutes = _closed(factory, day)
                cal_rows.append((code, day, minutes, status))
                day += timedelta(days=1)
    ds.put("line_calendar", ("line_code", "day", "available_minutes", "status"), cal_rows)

    # One production order per open PO line, scheduled backwards from the ex-factory date in
    # a window of up to 10 days per (factory line, PO); POs on a line never overlap.
    orders: list[dict[str, Any]] = []
    seq_by_line: dict[str, int] = defaultdict(int)
    cursor: dict[str, date] = {}
    open_pos = [p for p in pos.values() if p["status"] in ("open", "in_production", "shipped")]
    for po in sorted(open_pos, key=lambda p: (p["ex_factory_date"], p["code"])):
        lines_cfg = FACTORY_LINES.get(po["factory_code"])
        if not lines_cfg:
            continue
        line_codes = [c for c, _ in lines_cfg]
        assigned: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for i, ln in enumerate(sorted(lines_by_po[po["code"]], key=lambda x: x["line_no"])):
            assigned[line_codes[i % len(line_codes)]].append(ln)
        end = po["ex_factory_date"]
        for lc in line_codes:
            batch = assigned[lc]
            if not batch:
                continue
            start = max(
                cursor.get(lc, end - timedelta(days=PRODUCTION_WINDOW_DAYS - 1)),
                end - timedelta(days=PRODUCTION_WINDOW_DAYS - 1),
            )
            start = min(start, end)
            span = (end - start).days + 1
            n = len(batch)
            for k, ln in enumerate(batch):
                s = start + timedelta(days=(k * span) // n)
                e = max(s, start + timedelta(days=((k + 1) * span) // n - 1))
                status = "done" if e < TODAY else ("running" if s <= TODAY else "planned")
                seq_by_line[lc] += 1
                orders.append(
                    {
                        "code": f"MO-{len(orders) + 1:06d}",
                        "po_line_ref": f"{po['code']}#{ln['line_no']}",
                        "line_code": lc,
                        "seq": seq_by_line[lc],
                        "planned_start": s,
                        "planned_end": e,
                        "status": status,
                        "hold_reason": None,
                    }
                )
            cursor[lc] = end + timedelta(days=1)
    ds.ensure(
        "production_order",
        ("code", "po_line_ref", "line_code", "seq", "planned_start", "planned_end", "status", "hold_reason"),
    )
    ds.add_many("production_order", orders)


# ───────────────────────── online orders ─────────────────────────
def _online_orders(ds: Dataset, spec: Any, rng: np.random.Generator) -> None:
    stores_by_market: dict[str, list[str]] = defaultdict(list)
    for st in ds.iter_dicts("store"):
        stores_by_market[st["market_code"]].append(st["code"])
    markets = sorted(stores_by_market)
    weights = np.array([len(stores_by_market[m]) for m in markets], dtype=float)
    weights /= weights.sum()
    skus = [s["code"] for s in ds.iter_dicts("sku")]
    first = date(2026, 11, 2)
    orders: list[dict[str, Any]] = []
    lines: list[tuple[Any, ...]] = []
    for n in range(1, spec.online_orders + 1):
        market = markets[int(rng.choice(len(markets), p=weights))]
        click = bool(rng.random() < 0.3)
        store = stores_by_market[market][int(rng.integers(0, len(stores_by_market[market])))] if click else None
        promise = first + timedelta(days=int(rng.integers(0, 28)))
        created = datetime(2026, 10, 1, 9, 0, tzinfo=UTC) + timedelta(minutes=int(rng.integers(0, 4 * 24 * 60)))
        code = f"OO-{n:06d}"
        orders.append(
            {
                "code": code,
                "market": market,
                "channel": "click_collect" if click else "online",
                "store_code": store,
                "promise_date": promise,
                "status": "open",
                "created_at": created,
            }
        )
        n_lines = 1 + int(rng.integers(0, 3))
        picks = rng.choice(len(skus), size=n_lines, replace=False)
        at_risk = bool(rng.random() < 0.12)
        for i, idx in enumerate(sorted(int(p) for p in picks), start=1):
            qty = 1 + int(rng.random() < 0.25)
            reserved = qty
            if at_risk and i == 1:
                reserved = int(rng.integers(0, qty))
            lines.append((code, i, skus[idx], qty, reserved))
    ds.add_many("online_order", orders)
    ds.put("online_order_line", ("order_code", "line_no", "sku_code", "qty", "reserved_qty"), lines)


# ───────────────────────── promotions ─────────────────────────
def _period(start: date, end_inclusive: date) -> DateRange:
    return DateRange(start, end_inclusive + timedelta(days=1))


def _promotions(ds: Dataset, spec: Any) -> None:
    markets = [m[0] for m in spec.markets]
    outer = [s.code for s in spec.styles if s.family != reff.FAMILY_KNIT]
    knit = [s.code for s in spec.styles if s.family == reff.FAMILY_KNIT]
    promos = [
        {
            "code": "PROMO-BF26",
            "name": "Black Friday",
            "markets": markets,
            "grades": [],
            "style_codes": [],
            "period": _period(date(2026, 11, 23), date(2026, 11, 29)),
            "discount_bp": 3000,
            "uplift_bp": 4000,
        },
        {
            "code": "PROMO-WW26",
            "name": "Winter Warmers",
            "markets": [m for m in ("DE", "AT", "IT", "PL", "CZ", "SK", "HU", "RO", "SI") if m in markets],
            "grades": ["A", "B"],
            "style_codes": outer,
            "period": _period(date(2026, 11, 9), date(2026, 11, 22)),
            "discount_bp": 2000,
            "uplift_bp": 2500,
        },
    ]
    if knit:
        promos.append(
            {
                "code": "PROMO-KN26",
                "name": "Cosy Knit Week",
                "markets": [m for m in ("GB", "IE", "NL", "BE") if m in markets],
                "grades": ["A", "B"],
                "style_codes": knit,
                "period": _period(date(2026, 11, 2), date(2026, 11, 8)),
                "discount_bp": 1500,
                "uplift_bp": 2000,
            }
        )
    else:
        promos.append(
            {
                "code": "PROMO-PW26",
                "name": "Puffer Week",
                "markets": [m for m in ("GB", "IE") if m in markets],
                "grades": ["A", "B", "C"],
                "style_codes": outer,
                "period": _period(date(2026, 11, 2), date(2026, 11, 8)),
                "discount_bp": 1500,
                "uplift_bp": 2000,
            }
        )
    ds.add_many("promotion", promos)


# ───────────────────────── sales history ─────────────────────────
HISTORY_WEEKS: tuple[str, ...] = tuple(f"2026-W{w}" for w in range(33, 41))


def _sales_history(ds: Dataset, rng: np.random.Generator) -> None:
    """Sell-through W33–W40 per style × store: the W41 forecast ramped down (0.55 → 0.83)."""
    td = ds.tables["demand_forecast"]
    ci = {c: i for i, c in enumerate(td.columns)}
    base = [
        (r[ci["style_code"]], r[ci["store_code"]], r[ci["units"]]) for r in td.rows if r[ci["iso_week"]] == "2026-W41"
    ]
    base.sort(key=lambda r: (r[0], r[1]))
    n_weeks = len(HISTORY_WEEKS)
    noise = np.clip(rng.normal(1.0, 0.08, size=(len(base), n_weeks)), 0.75, 1.25)
    rows: list[tuple[Any, ...]] = []
    for i, (style, store, units) in enumerate(base):
        for j, wk in enumerate(HISTORY_WEEKS):
            ramp = 0.55 + 0.04 * j
            rows.append((style, store, wk, max(0, round(units * ramp * float(noise[i, j])))))
    ds.put("sales_history", ("style_code", "store_code", "iso_week", "units"), rows)
