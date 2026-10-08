"""Generate the NHC-shaped advisories of the ``hurricane-cape-route`` demo pack (SIMULATED data).

Fictional Hurricane ALBATROSS (``al992026``: 90-99 are NHC's invest numbers, never a named storm's,
so the id can never collide with a real advisory) forms off Guinea and tracks north-west along the
West African coast, right over the Cape-of-Good-Hope shipping lanes off Senegal and Mauritania. Three
advisories, 6 h apart (numbers 7, 8, 9): a category-1 hurricane that rapidly intensifies to category 3.

Each advisory is a ``CurrentStorms.json``-shaped document with one storm (fields as recorded from the
live NHC feed, 2026-10-08: string ``intensity``/``pressure``, numeric and text positions, product
objects ``{advNum, issuance, fileUpdateTime, url | kmzFile, zipFile}``) plus the two GIS products the
Signal Hub's NHC adapter reads: the 5-day cone (``…-cone.kml``, KML 2.1 namespace like NHC's cone)
and the forecast track (``…-track.kml``, KML 2.2, a line plus forecast points). The pack payload
names them under ``gis`` (the replay never downloads the ``*.example`` KMZ links).

How the cone is built (deterministic, pure Python): the "true" track is a fixed polyline of centre
positions by hour (:data:`BEST_TRACK`); each advisory forecasts it from its own time; the cone is the
convex hull of the uncertainty circles around the forecast positions (radius grows with lead time,
:data:`CONE_RADIUS_KM`, roughly NHC's Atlantic two-thirds error circles), counter-clockwise, closed.

Run from the repo root::

    python data/scenarios/tools/gen_hurricane_cape_route.py          # (re)write the fixtures
    python data/scenarios/tools/gen_hurricane_cape_route.py --check  # exit 1 if the committed files differ
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

PACK_DIR = Path(__file__).resolve().parents[1] / "hurricane-cape-route"
STORM_ID = "al992026"
NAME = "Albatross"
T0 = dt.datetime(2026, 10, 12, 0, 0, tzinfo=dt.UTC)  # advisory 7 (= pack start_at)
DEMO_HOST = "https://nhc.demo.example"  # reserved .example domain: never a real product link

# hour since advisory 7 → (lon, lat) of the storm centre
BEST_TRACK: tuple[tuple[float, float, float], ...] = (
    (0, -14.4, 9.3),
    (6, -15.0, 9.9),
    (12, -15.7, 10.5),
    (24, -17.0, 11.8),
    (36, -18.2, 13.3),
    (48, -19.2, 15.0),
    (72, -20.6, 18.6),
    (96, -21.6, 22.4),
    (120, -22.0, 26.0),
)
LEADS_H: tuple[int, ...] = (0, 12, 24, 36, 48, 72)
CONE_RADIUS_KM: tuple[tuple[int, float], ...] = ((0, 25), (12, 50), (24, 80), (36, 105), (48, 130), (72, 195))
CIRCLE_VERTICES = 24
# advisory number → (hour, intensity kt, pressure mb, classification)
ADVISORIES: tuple[tuple[int, int, int, int, str], ...] = (
    (7, 0, 70, 985, "HU"),
    (8, 6, 100, 958, "HU"),
    (9, 12, 110, 950, "HU"),
)


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
    return round(lon, 2), round(lat, 2)


def _circle(lon: float, lat: float, r_km: float, n: int = CIRCLE_VERTICES) -> list[tuple[float, float]]:
    kx = 111.320 * math.cos(math.radians(lat))
    ky = 110.574
    return [(round(lon + r_km * math.cos(2 * math.pi * i / n) / kx, 3), round(lat + r_km * math.sin(2 * math.pi * i / n) / ky, 3))
            for i in range(n)]


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain; counter-clockwise, collinear points dropped, ring closed."""
    pts = sorted(set(points))
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


def forecast(hour: int) -> list[tuple[int, float, float]]:
    return [(lead, *centre(hour + lead)) for lead in LEADS_H]


def cone(fc: list[tuple[int, float, float]]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for lead, lon, lat in fc:
        (r,) = _interp(CONE_RADIUS_KM, lead)  # type: ignore[arg-type]
        pts.extend(_circle(lon, lat, r))
    return convex_hull(pts)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _heading(hour: int) -> tuple[int, int]:
    (lon0, lat0), (lon1, lat1) = centre(hour), centre(hour + 6)
    dx = (lon1 - lon0) * 60 * math.cos(math.radians((lat0 + lat1) / 2))
    dy = (lat1 - lat0) * 60
    return round(math.degrees(math.atan2(dx, dy))) % 360, round(math.hypot(dx, dy) / 6)


def _coord_text(lat: float, lon: float) -> tuple[str, str]:
    return f"{abs(lat):.1f}{'N' if lat >= 0 else 'S'}", f"{abs(lon):.1f}{'W' if lon < 0 else 'E'}"


def _product(adv: str, issued: dt.datetime, **links: str) -> dict[str, Any]:
    return {"advNum": adv, "issuance": _iso(issued), "fileUpdateTime": _iso(issued - dt.timedelta(minutes=6)), **links}


def storms_doc(index: int) -> dict[str, Any]:
    num, hour, kt, mb, klass = ADVISORIES[index]
    adv = f"{num:03d}"
    issued = T0 + dt.timedelta(hours=hour)
    lon, lat = centre(hour)
    lat_text, lon_text = _coord_text(lat, lon)
    direction, speed = _heading(hour)
    upper = STORM_ID.upper()
    storm = {
        "id": STORM_ID,
        "binNumber": "AT5",
        "name": NAME,
        "classification": klass,
        "intensity": str(kt),
        "pressure": str(mb),
        "latitude": lat_text,
        "longitude": lon_text,
        "latitudeNumeric": lat,
        "longitudeNumeric": lon,
        "movementDir": direction,
        "movementSpeed": speed,
        "lastUpdate": _iso(issued),
        "publicAdvisory": _product(adv, issued, url=f"{DEMO_HOST}/text/MIATCPAT5.shtml"),
        "forecastAdvisory": _product(adv, issued, url=f"{DEMO_HOST}/text/MIATCMAT5.shtml"),
        "forecastTrack": _product(adv, issued, zipFile=f"{DEMO_HOST}/gis/forecast/archive/{STORM_ID}_5day_{adv}.zip",
                                  kmzFile=f"{DEMO_HOST}/storm_graphics/api/{upper}_{adv}adv_TRACK.kmz"),
        "windWatchesWarnings": None,
        "trackCone": _product(adv, issued, zipFile=f"{DEMO_HOST}/gis/forecast/archive/{STORM_ID}_5day_{adv}.zip",
                              kmzFile=f"{DEMO_HOST}/storm_graphics/api/{upper}_{adv}adv_CONE.kmz"),
        "peakSurgeKML": None,
    }
    return {
        "_fixture": (f"SIMULATED demo data (Keelson pack hurricane-cape-route): advisory {num} of the fictional "
                     f"Hurricane {NAME.upper()} ({upper}), shaped like the NHC CurrentStorms.json feed. "
                     "Not an NHC product; generated by data/scenarios/tools/gen_hurricane_cape_route.py."),
        "activeStorms": [storm],
    }


def _kml_coords(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{lon:.3f},{lat:.3f},0" for lon, lat in points)


def cone_kml(index: int) -> str:
    num, hour, _kt, _mb, _klass = ADVISORIES[index]
    ring = cone(forecast(hour))
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<!-- SIMULATED demo data (Keelson pack hurricane-cape-route): forecast cone of the fictional Hurricane
     {NAME.upper()}, advisory {num}, shaped like an NHC 5-day cone KML. Not an NHC product. -->
<kml xmlns='http://earth.google.com/kml/2.1'>
<Document>
<name>Hurricane {NAME} (Advisory #{num}) - Forecast Track Uncertainty (SIMULATED)</name>
    <Style id='120'>
            <LineStyle><color>ffff7b00</color><width>2</width></LineStyle>
            <PolyStyle><color>33eae12e</color></PolyStyle>
    </Style>
    <Placemark>
      <styleUrl>#120</styleUrl>
        <Polygon>
            <outerBoundaryIs><LinearRing>
                <coordinates>
                    {_kml_coords(ring)}
                </coordinates>
            </LinearRing></outerBoundaryIs>
        </Polygon>
        <ExtendedData>
            <Data name="stormType"><value>HU</value></Data>
            <Data name="basin"><value>AL</value></Data>
            <Data name="atcfid"><value>{STORM_ID.upper()}</value></Data>
            <Data name="advisoryNum"><value>{num}</value></Data>
            <Data name="stormName"><value>{NAME}</value></Data>
        </ExtendedData>
    </Placemark>
</Document>
</kml>
"""


def track_kml(index: int) -> str:
    num, hour, kt, _mb, _klass = ADVISORIES[index]
    fc = forecast(hour)
    points = "\n".join(
        f"""       <Placemark>
          <description><![CDATA[<table><tr><td>{lead} hr Forecast</td></tr></table>]]></description>
          <styleUrl>#m_point</styleUrl>
          <Point><coordinates> {lon:.2f},{lat:.2f},0 </coordinates></Point>
       </Placemark>""" for lead, lon, lat in fc[1:])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- SIMULATED demo data (Keelson pack hurricane-cape-route): forecast track of the fictional Hurricane
     {NAME.upper()}, advisory {num}, shaped like an NHC forecast-track KML. Not an NHC product. -->
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document>
  <name>{STORM_ID.upper()} (Advisory #{num}) - Forecast Track (SIMULATED)</name>
  <Folder id="Forecast Track">
       <Placemark>
          <styleUrl>#72_line</styleUrl>
          <LineString><coordinates> {_kml_coords([(lon, lat) for _, lon, lat in fc])} </coordinates></LineString>
          <ExtendedData><Data name="maxWindKnots"><value>{kt}</value></Data></ExtendedData>
       </Placemark>
{points}
  </Folder>
</Document>
</kml>
"""


def render() -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(len(ADVISORIES)):
        out[f"nhc-adv{i + 1}.json"] = json.dumps(storms_doc(i), indent=2, ensure_ascii=False) + "\n"
        out[f"nhc-adv{i + 1}-cone.kml"] = cone_kml(i)
        out[f"nhc-adv{i + 1}-track.kml"] = track_kml(i)
    return out


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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    if stale:
        print(f"stale fixtures (re-run without --check): {', '.join(stale)}", file=sys.stderr)
        return 1
    if args.check:
        print("hurricane-cape-route fixtures up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
