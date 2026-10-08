"""Rebuild ``seed/data/cities.csv`` from a GeoNames ``cities15000.txt`` dump (offline).

    python -m merxa_api.seed.cities_extract cities15000.txt > src/merxa_api/seed/data/cities.csv

GeoNames (https://www.geonames.org/, CC BY 4.0) publishes ``cities15000.zip``: tab-separated
rows with geonameid, name, asciiname, alternatenames, latitude, longitude, feature class,
feature code, country code, …, population, … . This keeps the 16 store markets and, per
market, the most populous ``--per-market`` places (feature class ``P``). Changing the
city table changes the ``full`` seed: re-pin ``GOLDEN_DATASET_SHA256["full"]``.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict

from merxa_api.seed.reference_full import STORE_COUNTS

# GeoNames column indexes
NAME, LAT, LON, FCLASS, CC, POP = 1, 4, 5, 6, 8, 14


def extract(lines: list[str], per_market: int) -> list[tuple[str, str, float, float, int]]:
    by_cc: dict[str, list[tuple[str, str, float, float, int]]] = defaultdict(list)
    for line in lines:
        cols = line.rstrip("\n").split("\t")
        if len(cols) <= POP or cols[FCLASS] != "P" or cols[CC] not in STORE_COUNTS:
            continue
        by_cc[cols[CC]].append((cols[NAME], cols[CC], float(cols[LAT]), float(cols[LON]), int(cols[POP] or 0)))
    out = []
    for cc in STORE_COUNTS:
        out.extend(sorted(by_cc[cc], key=lambda c: (-c[4], c[0]))[:per_market])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="path to cities15000.txt")
    parser.add_argument("--per-market", type=int, default=60)
    args = parser.parse_args(argv)
    with open(args.dump, encoding="utf-8") as fh:
        rows = extract(fh.readlines(), args.per_market)
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["name", "country", "lat", "lon", "population"])
    for name, cc, lat, lon, pop in rows:
        writer.writerow([name, cc, f"{lat:.4f}", f"{lon:.4f}", pop])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
