"""Generate the GDACS-shaped advisories of the ``typhoon-hcmc`` demo pack (SIMULATED data).

Fictional Typhoon KESTREL-26 crosses the South China Sea westwards and makes landfall near Vung Tau,
passing over Ho Chi Minh City (Cat Lai, VNSGN) about 24 h after the first advisory. Four advisories,
6 h apart, each a ``geteventlist``-shaped FeatureCollection (one ``Point_Centroid`` event feature) with
the ``getgeometry`` footprint of the same episode embedded (uncertainty cone polygon, forecast track
line, forecast track points), exactly as the Signal Hub's GDACS adapter parses them.

Real-feed shape (checked against live GDACS API v2 responses, 2026-10-08): ``alertscore`` is the
integer level score (Green 1, Orange 2, Red 3) and ``episodealertscore`` a float; ``getgeometry``
track points are small ``Polygon`` circles with ``Class`` ``Point_Polygon_Point_<n>`` (``n`` from 0)
and a ``polygonlabel`` like ``05/10 06:00 UTC``; track lines carry the storm category as label.

How the cone is built (deterministic, pure Python, no dependencies):

* the storm's "true" track is a fixed polyline of centre positions by hour (:data:`BEST_TRACK`);
* each advisory forecasts that track from its own time, with a northward bias that shrinks as the
  storm approaches (early advisories aim at Binh Thuan / the Central Highlands, later ones at
  Vung Tau / HCMC: the cone "tracks towards Ho Chi Minh City");
* the cone is the convex hull of the uncertainty circles around the forecast positions (radius grows
  with lead time, :data:`CONE_RADIUS_KM`), rounded to 3 decimals, counter-clockwise exterior ring
  (RFC 7946), closed. The hull of circles along a near-straight track is the classic cone shape.

Run from the repo root::

    python data/scenarios/tools/gen_typhoon_hcmc.py          # (re)write the fixtures
    python data/scenarios/tools/gen_typhoon_hcmc.py --check  # exit 1 if the committed files differ
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

PACK_DIR = Path(__file__).resolve().parents[1] / "typhoon-hcmc"
EVENT_ID = 9_260_041  # fictional (outside the range of real GDACS TC event ids in use)
EPISODE_BASE = 12  # the storm had 11 earlier (green) episodes before the first orange one
NAME = "KESTREL-26"
T0 = dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.UTC)  # advisory 1 (= pack start_at)
GENESIS = dt.datetime(2026, 10, 2, 6, 0, tzinfo=dt.UTC)
DEMO_HOST = "https://gdacs.demo.example"  # reserved .example domain: never a real report link

# hour since advisory 1 → (lon, lat) of the storm centre
BEST_TRACK: tuple[tuple[float, float, float], ...] = (
    (0, 112.0, 11.30),
    (6, 110.7, 11.00),
    (12, 109.4, 10.75),
    (18, 108.1, 10.55),
    (24, 106.85, 10.55),
    (36, 105.5, 9.80),
    (48, 104.2, 9.20),
    (60, 102.8, 8.70),
)
LEADS_H: tuple[int, ...] = (0, 6, 12, 18, 24, 36, 48)  # forecast ends as the storm dissipates
# lead hour → cone radius (km); linear in between
CONE_RADIUS_KM: tuple[tuple[int, float], ...] = ((0, 30), (12, 50), (24, 75), (36, 100), (48, 125))
CIRCLE_VERTICES = 24

# per advisory: (hour, alert level, max wind km/h, northward bias deg/h, countries)
ADVISORIES: tuple[tuple[int, str, int, float, tuple[tuple[str, str, str], ...]], ...] = (
    (0, "Orange", 130, 0.015, (("VN", "VNM", "Viet Nam"),)),
    (6, "Orange", 150, 0.010, (("VN", "VNM", "Viet Nam"),)),
    (12, "Red", 175, 0.005, (("VN", "VNM", "Viet Nam"), ("KH", "KHM", "Cambodia"))),
    (18, "Red", 185, 0.0, (("VN", "VNM", "Viet Nam"), ("KH", "KHM", "Cambodia"))),
)
# GDACS: alertscore is the integer score of the alert level (live API, 2026-10-08)
LEVEL_SCORE: dict[str, int] = {"Green": 1, "Orange": 2, "Red": 3}
TRACK_POINT_RADIUS_KM = 3.5  # getgeometry track points are small circles (Polygon), not Points
TRACK_POINT_VERTICES = 16
TRACK_LABEL = "TY"  # storm category of the track line (JTWC: TD/TS/TY/STY)


def _interp(table: tuple[tuple[float, ...], ...], x: float) -> tuple[float, ...]:
    if x <= table[0][0]:
        return table[0][1:]
    for (x0, *a), (x1, *b) in itertools.pairwise(table):
        if x0 <= x <= x1:
            f = (x - x0) / (x1 - x0)
            return tuple(av + (bv - av) * f for av, bv in zip(a, b, strict=True))
    return table[-1][1:]


def centre(hour: float) -> tuple[float, float]:
    lon, lat = _interp(BEST_TRACK, hour)
    return lon, lat


def cone_radius_km(lead: float) -> float:
    (r,) = _interp(CONE_RADIUS_KM, lead)  # type: ignore[arg-type]
    return r


def forecast(hour: int, bias: float) -> list[tuple[int, float, float]]:
    out = []
    for lead in LEADS_H:
        lon, lat = centre(hour + lead)
        out.append((lead, round(lon, 3), round(lat + bias * lead, 3)))
    return out


def _circle(lon: float, lat: float, r_km: float, n: int = CIRCLE_VERTICES) -> list[tuple[float, float]]:
    kx = 111.320 * math.cos(math.radians(lat))
    ky = 110.574
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        pts.append((round(lon + r_km * math.cos(a) / kx, 3), round(lat + r_km * math.sin(a) / ky, 3)))
    return pts


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain; counter-clockwise, collinear points dropped, ring closed."""
    pts = sorted(set(points))
    if len(pts) < 3:
        raise ValueError("need at least 3 distinct points")
    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    ring = lower[:-1] + upper[:-1]
    return [*ring, ring[0]]


def cone(fc: list[tuple[int, float, float]]) -> list[list[float]]:
    pts: list[tuple[float, float]] = []
    for lead, lon, lat in fc:
        pts.extend(_circle(lon, lat, cone_radius_km(lead)))
    return [[x, y] for x, y in convex_hull(pts)]


def _gdacs_ts(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")  # GDACS: naive UTC


def _track_label(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%d/%m %H:%M UTC")  # getgeometry polygonlabel of a track point


def _html_date(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%d %b %Y")


def track_point(lon: float, lat: float) -> list[list[float]]:
    """A getgeometry track point: a small counter-clockwise closed circle (``Point_Polygon_Point_n``)."""
    pts = _circle(lon, lat, TRACK_POINT_RADIUS_KM, TRACK_POINT_VERTICES)
    return [[x, y] for x, y in [*pts, pts[0]]]


def advisory(index: int) -> dict[str, Any]:
    hour, level, wind, bias, countries = ADVISORIES[index]
    score = LEVEL_SCORE[level]
    episode = EPISODE_BASE + index
    issued = T0 + dt.timedelta(hours=hour)
    fc = forecast(hour, bias)
    ring = cone(fc)
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    bbox = [min(lons), min(lats), max(lons), max(lats)]
    lon0, lat0 = fc[0][1], fc[0][2]
    q = f"eventtype=TC&eventid={EVENT_ID}&episodeid={episode}"
    country_names = ", ".join(c[2] for c in countries)
    base_props = {"eventtype": "TC", "eventid": EVENT_ID, "episodeid": episode}
    features: list[dict[str, Any]] = [
        {
            "type": "Feature",
            "bbox": bbox,
            "geometry": {"type": "Point", "coordinates": [lon0, lat0]},
            "properties": {
                **base_props,
                "eventname": NAME,
                "glide": "",
                "name": f"Tropical Cyclone {NAME}",
                "description": f"Tropical Cyclone {NAME}",
                "htmldescription": (f"{level} Tropical Cyclone {NAME} in {country_names} from: {_html_date(GENESIS)}  "
                                    f"to: {_html_date(issued)} . (SIMULATED demo)"),
                "icon": f"{DEMO_HOST}/images/gdacs_icons/maps/{level}/TC.png",
                "iconoverall": f"{DEMO_HOST}/images/gdacs_icons/maps/{level}/TC.png",
                "url": {
                    "geometry": f"{DEMO_HOST}/gdacsapi/api/polygons/getgeometry?{q}",
                    "report": f"{DEMO_HOST}/report.aspx?eventid={EVENT_ID}&episodeid={episode}&eventtype=TC",
                    "details": f"{DEMO_HOST}/gdacsapi/api/events/geteventdata?eventtype=TC&eventid={EVENT_ID}",
                },
                "alertlevel": level,
                "alertscore": score,
                "episodealertlevel": level,
                "episodealertscore": float(score),
                "istemporary": "false",
                "iscurrent": "true",
                "country": countries[0][2],
                "countryonland": "",
                "fromdate": _gdacs_ts(GENESIS),
                "todate": _gdacs_ts(issued),
                "datemodified": _gdacs_ts(issued),
                "iso3": countries[0][1],
                "source": "SIMULATED",
                "sourceid": "",
                "polygonlabel": "Centroid",
                "Class": "Point_Centroid",
                "affectedcountries": [{"iso2": a, "iso3": b, "countryname": c} for a, b, c in countries],
                "severitydata": {
                    "severity": float(wind),
                    "severitytext": f"Typhoon (maximum wind speed of {wind} km/h)",
                    "severityunit": "km/h",
                },
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {**base_props, "Class": "Poly_Cones", "polygonlabel": "Uncertainty Cones"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[lon, lat] for _, lon, lat in fc]},
            "properties": {**base_props, "Class": "Line_Line_0", "polygonlabel": TRACK_LABEL},
        },
    ]
    for n, (lead, lon, lat) in enumerate(fc):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [track_point(lon, lat)]},
            "properties": {
                **base_props,
                "Class": f"Point_Polygon_Point_{n}",
                "polygonlabel": _track_label(issued + dt.timedelta(hours=lead)),
            },
        })
    return {
        "_fixture": (
            f"SIMULATED demo data (Keelson pack typhoon-hcmc): advisory {index + 1}/4 for the fictional "
            f"Typhoon {NAME}, shaped like a GDACS API v2 geteventlist response with the getgeometry "
            "footprint of the same episode embedded. Not a real GDACS product; generated by "
            "data/scenarios/tools/gen_typhoon_hcmc.py."
        ),
        "type": "FeatureCollection",
        "features": features,
    }


def _dump(value: Any, indent: int = 0) -> str:
    """JSON with coordinate arrays kept on one line (readable diffs)."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f'{pad}  {json.dumps(k)}: {_dump(v, indent + 1)}' for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value) or _numeric_nest(value):
            return json.dumps(value, separators=(", ", ": "))
        items = [f"{pad}  {_dump(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def _numeric_nest(value: Any) -> bool:
    if isinstance(value, list):
        return all(_numeric_nest(v) for v in value)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render() -> dict[str, str]:
    return {f"gdacs-adv{i + 1}.geojson": _dump(advisory(i)) + "\n" for i in range(len(ADVISORIES))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if the committed fixtures differ")
    args = ap.parse_args(argv)
    stale = []
    for name, text in render().items():
        path = PACK_DIR / name
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    if stale:
        print(f"stale fixtures (re-run without --check): {', '.join(stale)}", file=sys.stderr)
        return 1
    if args.check:
        print("typhoon-hcmc fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
