"""KML / KMZ geometry reader (NHC cone and track products; ``124`` §2).

Only geometry is read: every ``Polygon`` (its outer + inner ``LinearRing`` boundaries), ``LineString``
and ``Point`` anywhere in the document (``MultiGeometry`` and folders are walked), whatever the KML
namespace (NHC still publishes cones as ``http://earth.google.com/kml/2.1`` and tracks as
``http://www.opengis.net/kml/2.2``). Names, descriptions, styles and links are ignored. KML
``coordinates`` are ``lon,lat[,alt]`` tuples separated by whitespace; altitude is dropped.

Hardening: XML through ``defusedxml`` (no entity expansion, no external entities or DTDs); a KMZ
through :func:`signal_hub.safezip.read_member` (exactly one ``.kml`` member, size and ratio caps);
at most :data:`MAX_GEOMETRIES` geometries and :data:`MAX_POSITIONS` positions per document, then
:func:`signal_hub.geo.clean_geometry` reduces each one before contract validation.
"""

from __future__ import annotations

from typing import Any, Final
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from signal_hub import geo, safezip

__all__ = ["MAX_GEOMETRIES", "MAX_KML_BYTES", "MAX_POSITIONS", "KmlError", "kml_geometries", "read_kml_or_kmz"]

MAX_KML_BYTES: Final = 8 * 1024 * 1024
MAX_GEOMETRIES: Final = 200
MAX_POSITIONS: Final = 50_000


class KmlError(ValueError):
    """Not a usable KML / KMZ document."""


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _positions(text: str | None, budget: list[int]) -> list[list[float]]:
    out: list[list[float]] = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        budget[0] -= 1
        if budget[0] < 0:
            raise KmlError(f"more than {MAX_POSITIONS} coordinates")
        out.append([lon, lat])
    return out


def _coords(el: Element, budget: list[int]) -> list[list[float]]:
    node = next((c for c in el.iter() if _local(c.tag) == "coordinates"), None)
    return _positions(node.text if node is not None else None, budget)


def _polygon(el: Element, budget: list[int]) -> dict[str, Any] | None:
    rings: list[list[list[float]]] = []
    for boundary in el:
        name = _local(boundary.tag)
        if name not in ("outerBoundaryIs", "innerBoundaryIs"):
            continue
        ring = next((r for r in boundary.iter() if _local(r.tag) == "LinearRing"), None)
        pts = _coords(ring, budget) if ring is not None else []
        if name == "outerBoundaryIs":
            rings.insert(0, pts)
        else:
            rings.append(pts)
    if not rings or len(rings[0]) < 3:
        return None
    return {"type": "Polygon", "coordinates": rings}


def kml_geometries(body: bytes) -> list[dict[str, Any]]:
    """Cleaned GeoJSON geometries of a KML document (document order)."""
    if len(body) > MAX_KML_BYTES:
        raise KmlError(f"KML larger than {MAX_KML_BYTES} bytes")
    try:
        root = SafeET.fromstring(body)
    except Exception as exc:  # defusedxml raises its own DTD/entity errors besides ParseError
        raise KmlError(f"not well-formed KML ({type(exc).__name__})") from None
    if _local(root.tag) != "kml":
        raise KmlError("root element is not <kml>")
    budget = [MAX_POSITIONS]
    out: list[dict[str, Any]] = []
    for el in root.iter():
        name = _local(el.tag)
        geometry: dict[str, Any] | None = None
        if name == "Polygon":
            geometry = _polygon(el, budget)
        elif name == "LineString":
            geometry = {"type": "LineString", "coordinates": _coords(el, budget)}
        elif name == "Point":
            pts = _coords(el, budget)
            geometry = {"type": "Point", "coordinates": pts[0]} if pts else None
        if geometry is None:
            continue
        cleaned = geo.clean_geometry(geometry)
        if cleaned is not None:
            out.append(cleaned)
            if len(out) > MAX_GEOMETRIES:
                raise KmlError(f"more than {MAX_GEOMETRIES} geometries")
    return out


def read_kml_or_kmz(body: bytes) -> bytes:
    """The KML document of a KML or KMZ (zip) body."""
    if safezip.is_zip(body):
        return safezip.read_member(body, suffixes=(".kml",), max_uncompressed=MAX_KML_BYTES)
    return body
