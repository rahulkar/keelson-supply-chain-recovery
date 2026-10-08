"""Store placement for the ``full`` profile: 451 stores in 16 markets from the bundled city
table, population-weighted (``population ** POPULATION_EXPONENT``) with ~2 km jitter."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from decimal import Decimal
from importlib import resources
from typing import Any, NamedTuple

import numpy as np

from merxa_api.seed import reference as ref_small
from merxa_api.seed import reference_full as ref
from merxa_api.seed.split import largest_remainder


class City(NamedTuple):
    name: str
    country: str
    lat: float
    lon: float
    population: int


def load_cities() -> list[City]:
    """The bundled extract (``seed/data/cities.csv``), in file order."""
    text = resources.files("merxa_api.seed").joinpath("data/cities.csv").read_text(encoding="utf-8")
    out = []
    for row in csv.DictReader(text.splitlines()):
        out.append(City(row["name"], row["country"], float(row["lat"]), float(row["lon"]), int(row["population"])))
    return out


def climate_of(country: str, lat: float, lon: float) -> str:
    if (
        (country in ref.COLD_MARKETS and lat >= 45.0)
        or lat >= ref.COLD_LAT
        or (country == "DE" and lon >= ref.DE_EAST_LON)
    ):
        return "cold"
    if lat < ref.WARM_LAT:
        return "warm"
    return "mild"


def _grade(rng: np.random.Generator, population: int) -> str:
    for threshold, probs in ref.GRADE_P:
        if population >= threshold:
            return str(rng.choice(["A", "B", "C"], p=list(probs)))
    raise AssertionError("unreachable")


def place_stores(rng: np.random.Generator) -> list[dict[str, Any]]:
    """Return store dicts (with private ``_climate``) ordered by market (contract order) then
    city population. Codes ``ST-<CC>-NNN``."""
    by_country: dict[str, list[City]] = defaultdict(list)
    for c in load_cities():
        by_country[c.country].append(c)
    stores: list[dict[str, Any]] = []
    for market, n_stores in ref.STORE_COUNTS.items():
        cities = sorted(by_country[market], key=lambda c: (-c.population, c.name))
        if not cities:
            raise ValueError(f"no cities for market {market}")
        counts = largest_remainder(n_stores, [c.population**ref.POPULATION_EXPONENT for c in cities])
        n = 0
        for city, count in zip(cities, counts, strict=True):
            for k in range(count):
                n += 1
                dlat, dlon = rng.normal(0.0, ref.JITTER_DEG, size=2)
                lat = city.lat + float(np.clip(dlat, -3 * ref.JITTER_DEG, 3 * ref.JITTER_DEG))
                lon = city.lon + float(np.clip(dlon, -3 * ref.JITTER_DEG, 3 * ref.JITTER_DEG)) / max(
                    0.3, math.cos(math.radians(city.lat))
                )
                grade = _grade(rng, city.population)
                lo, hi = ref_small.GRADE_INDEX_RANGE[grade]
                sales_index = round(float(rng.uniform(lo, hi)), 3)
                climate = climate_of(market, city.lat, city.lon)
                dc_code = ref.MARKET_DC[market]
                band = "A" if grade == "A" else "BC"
                suffix = ref.STORE_SUFFIXES[k % len(ref.STORE_SUFFIXES)] if count > 1 else "Centre"
                if count > len(ref.STORE_SUFFIXES) and k >= len(ref.STORE_SUFFIXES):
                    suffix = f"{suffix} {k // len(ref.STORE_SUFFIXES) + 1}"
                stores.append(
                    {
                        "code": f"ST-{market}-{n:03d}",
                        "name": f"{city.name} {suffix}",
                        "market_code": market,
                        "city": city.name,
                        "lat": round(lat, 4),
                        "lon": round(lon, 4),
                        "grade": grade,
                        "cluster_code": f"{dc_code}-{climate.upper()}-{band}",
                        "home_dc_code": dc_code,
                        "sales_index": Decimal(f"{sales_index:.3f}"),
                        "_climate": climate,
                        "_population": city.population,
                    }
                )
    return stores
