#!/usr/bin/env python3
"""Generate the Fernhill ERP demo data (``golden/*.csv``) — deterministic, stdlib only.

Fernhill is a small fictional outdoor-apparel retailer whose ERP is "a directory of CSV
exports": two warehouses (Leeds GB, Rotterdam NL), twelve shops in four store clusters, two
suppliers, two styles in five sizes, four purchase orders, stock per site and SKU, a nine-week
store forecast (2026-W41..W49), size mixes, store allocations for the W45 drop and one transfer.

The story (contract 122 §7): the Navy parka purchase order for **Leeds** lands only in W47, so
the Leeds shops run short in W45 while **Rotterdam** holds transferable surplus — a transfer
FH-RTM → FH-LDS plus allocation trims is the obvious recovery.

Usage: ``python generate.py [OUT_DIR]`` (default: ``golden/`` next to this file). The output is
byte-identical for every run (``random.Random(20261008)``; cells formatted like the sidecar's
CSV write-back: integers in decimal, shortest round-trip reals, ``\\n`` line ends).
"""

from __future__ import annotations

import csv
import io
import random
import sys
from pathlib import Path
from typing import Any

SEED = 20261008
WEEKS = [f"2026-W{w}" for w in range(41, 50)]
SIZES = [("XS", 1), ("S", 2), ("M", 3), ("L", 4), ("XL", 5)]

WAREHOUSES = [
    ("FH-LDS", "Fernhill Leeds DC", "GB", "Leeds", 53.8008, -1.5491),
    ("FH-RTM", "Fernhill Rotterdam DC", "NL", "Rotterdam", 51.9244, 4.4777),
]
SHOPS = [
    # code, name, country, city, lat, lon, grade, home warehouse
    ("FH-S01", "Fernhill Leeds Briggate", "GB", "Leeds", 53.7984, -1.5426, "A", "FH-LDS"),
    ("FH-S02", "Fernhill Manchester Arndale", "GB", "Manchester", 53.4831, -2.2399, "A", "FH-LDS"),
    ("FH-S03", "Fernhill Edinburgh Princes St", "GB", "Edinburgh", 55.9521, -3.1965, "A", "FH-LDS"),
    ("FH-S04", "Fernhill York Coney St", "GB", "York", 53.9597, -1.0842, "B", "FH-LDS"),
    ("FH-S05", "Fernhill Sheffield Fargate", "GB", "Sheffield", 53.3817, -1.4691, "B", "FH-LDS"),
    ("FH-S06", "Fernhill Harrogate", "GB", "Harrogate", 53.9921, -1.5418, "C", "FH-LDS"),
    ("FH-S07", "Fernhill Rotterdam Lijnbaan", "NL", "Rotterdam", 51.9198, 4.4747, "A", "FH-RTM"),
    ("FH-S08", "Fernhill Amsterdam Kalverstraat", "NL", "Amsterdam", 52.3702, 4.8913, "A", "FH-RTM"),
    ("FH-S09", "Fernhill Antwerpen Meir", "BE", "Antwerp", 51.2182, 4.4096, "A", "FH-RTM"),
    ("FH-S10", "Fernhill Utrecht Oudegracht", "NL", "Utrecht", 52.0907, 5.1214, "B", "FH-RTM"),
    ("FH-S11", "Fernhill Den Haag Spuistraat", "NL", "The Hague", 52.0786, 4.3109, "B", "FH-RTM"),
    ("FH-S12", "Fernhill Eindhoven", "NL", "Eindhoven", 51.4416, 5.4697, "C", "FH-RTM"),
]
SUPPLIERS = [
    ("SUP-TR-01", "Izmir Outerwear AS", "TR"),
    ("SUP-PT-01", "Porto Knit Lda", "PT"),
]
PRODUCTS = [
    # code, name, family, colour, retail cents, unit cost cents, supplier
    ("FH-PARKA-NVY", "Harbour Parka Navy", "outerwear", "navy", 12900, 4100, "SUP-TR-01"),
    ("FH-FLEECE-GRN", "Fell Fleece Green", "fleece", "green", 4900, 1450, "SUP-PT-01"),
]
PURCHASE_ORDERS = [
    # po, supplier, dest, ex-factory, eta, style
    ("PO-FH-0001", "SUP-TR-01", "FH-LDS", "2026-10-26", "2026-11-16", "FH-PARKA-NVY"),
    ("PO-FH-0002", "SUP-TR-01", "FH-RTM", "2026-10-05", "2026-10-26", "FH-PARKA-NVY"),
    ("PO-FH-0003", "SUP-PT-01", "FH-LDS", "2026-10-12", "2026-10-23", "FH-FLEECE-GRN"),
    ("PO-FH-0004", "SUP-PT-01", "FH-RTM", "2026-10-12", "2026-10-21", "FH-FLEECE-GRN"),
]
SIZE_MIX_BP = {"A": [800, 2200, 3200, 2400, 1400], "BC": [1000, 2400, 3000, 2300, 1300]}


def cluster_of(grade: str, warehouse: str) -> str:
    return f"{warehouse}-{'A' if grade == 'A' else 'BC'}"


def climate_of(country: str) -> str:
    return "cool" if country == "GB" else "mild"


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def render(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return buf.getvalue().encode("utf-8")


def build() -> dict[str, bytes]:
    rnd = random.Random(SEED)
    files: dict[str, bytes] = {}
    files["warehouses.csv"] = render(["code", "name", "country", "city", "lat", "lon", "rev"],
                                     [[*w, 1] for w in WAREHOUSES])
    files["shops.csv"] = render(
        ["code", "name", "country", "city", "lat", "lon", "grade", "cluster", "climate", "home_wh", "rev"],
        [[c, n, co, ci, la, lo, g, cluster_of(g, wh), climate_of(co), wh, 1] for c, n, co, ci, la, lo, g, wh in SHOPS],
    )
    files["suppliers.csv"] = render(["code", "name", "country", "rev"], [[*s, 1] for s in SUPPLIERS])
    files["products.csv"] = render(
        ["code", "name", "family", "colour", "retail_price_cents", "currency", "supplier", "rev"],
        [[c, n, f, col, retail, "GBP", sup, 1] for c, n, f, col, retail, _cost, sup in PRODUCTS],
    )
    skus = [(f"{p[0]}-{size}", p[0], size, seq, p[5]) for p in PRODUCTS for size, seq in SIZES]
    files["skus.csv"] = render(["sku", "style", "size", "size_seq", "unit_cost_cents", "rev"],
                               [[*s, 1] for s in skus])

    po_rows, line_rows = [], []
    for po, sup, dest, exf, eta, style in PURCHASE_ORDERS:
        po_rows.append([po, sup, dest, exf, eta, "open", 1])
        for line_no, (size, _seq) in enumerate(SIZES, start=1):
            qty = rnd.randrange(120, 361, 10)
            line_rows.append([po, line_no, f"{style}-{size}", qty, 0, eta, "open", 1])
    files["purchase_orders.csv"] = render(["po", "supplier", "dest_wh", "ex_factory", "eta", "status", "rev"], po_rows)
    files["purchase_lines.csv"] = render(
        ["po", "line_no", "sku", "qty_ordered", "qty_received", "eta", "status", "rev"], line_rows)

    # Allocations for the W45 drop: every shop gets every parka size (the story) and fleece M/L.
    alloc_rows: list[list[Any]] = []
    allocated: dict[tuple[str, str], int] = {}
    n = 0
    for shop in SHOPS:
        code, _n, _co, _ci, _la, _lo, grade, wh = shop
        for sku, style, size, _seq, _cost in skus:
            if style == "FH-FLEECE-GRN" and size not in ("M", "L"):
                continue
            n += 1
            qty = rnd.randrange(4, 13) if grade == "A" else rnd.randrange(2, 8)
            status = "released" if n % 7 == 0 else "planned"
            alloc_rows.append([f"AL-{n:04d}", sku, wh, code, "2026-W45", qty, status, 1])
            if status == "released":
                allocated[(wh, sku)] = allocated.get((wh, sku), 0) + qty
    files["store_allocations.csv"] = render(
        ["code", "sku", "from_wh", "shop", "ship_week", "qty", "status", "rev"], alloc_rows)

    # Stock: Leeds is short of parkas (its PO lands in W47); Rotterdam holds surplus.
    stock_rows: list[list[Any]] = []
    for wh, *_rest in WAREHOUSES:
        for sku, style, _size, _seq, _cost in skus:
            if style == "FH-PARKA-NVY":
                on_hand = rnd.randrange(10, 31) if wh == "FH-LDS" else rnd.randrange(260, 421, 10)
            else:
                on_hand = rnd.randrange(80, 201, 10)
            alloc = allocated.get((wh, sku), 0)
            reserved = rnd.randrange(0, 11)
            stock_rows.append([wh, "dc", sku, on_hand + alloc, alloc, reserved, 0, 1])
    for shop in SHOPS:
        for sku, _style, _size, _seq, _cost in skus:
            stock_rows.append([shop[0], "store", sku, rnd.randrange(0, 9), 0, 0, 0, 1])
    # the existing transfer below is in transit to Leeds
    for row in stock_rows:
        if row[0] == "FH-LDS" and row[2] in ("FH-FLEECE-GRN-M", "FH-FLEECE-GRN-L"):
            row[6] = 40
    files["stock.csv"] = render(["site", "site_kind", "sku", "on_hand", "allocated", "reserved", "in_transit", "rev"],
                                stock_rows)

    forecast_rows: list[list[Any]] = []
    for style, *_ in PRODUCTS:
        for shop in SHOPS:
            code, _n, _co, _ci, _la, _lo, grade, wh = shop
            base = 26 if grade == "A" else 14 if grade == "B" else 9
            if style == "FH-FLEECE-GRN":
                base = base * 2 // 3
            for i, week in enumerate(WEEKS):
                peak = 1.0 + 0.6 * (week == "2026-W45") + 0.2 * (i > 4)
                units = int(base * peak) + rnd.randrange(0, 5)
                if style == "FH-PARKA-NVY" and wh == "FH-LDS" and week == "2026-W45":
                    units += 12
                forecast_rows.append([style, code, week, units, 1])
    files["weekly_forecast.csv"] = render(["style", "shop", "iso_week", "units", "rev"], forecast_rows)

    mix_rows: list[list[Any]] = []
    for family in ("outerwear", "fleece"):
        for wh, *_ in WAREHOUSES:
            for band in ("A", "BC"):
                for (size, _seq), bp in zip(SIZES, SIZE_MIX_BP[band], strict=True):
                    mix_rows.append([family, f"{wh}-{band}", size, bp])
    files["size_mix.csv"] = render(["family", "cluster", "size", "share_bp"], mix_rows)

    files["transfers.csv"] = render(
        ["code", "from_wh", "to_wh", "ship_date", "eta_date", "status", "created_via", "rev"],
        [["TR-00001", "FH-RTM", "FH-LDS", "2026-10-06", "2026-10-09", "shipped", "manual", 2]],
    )
    files["transfer_lines.csv"] = render(
        ["transfer_code", "line_no", "sku", "qty", "rev"],
        [["TR-00001", 1, "FH-FLEECE-GRN-M", 40, 1], ["TR-00001", 2, "FH-FLEECE-GRN-L", 40, 1]],
    )
    return files


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent / "golden"
    out.mkdir(parents=True, exist_ok=True)
    files = build()
    for name, data in sorted(files.items()):
        (out / name).write_bytes(data)
    print(f"wrote {len(files)} files to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
