"""Demo-pack steps of the M8 feed adapters (``124`` §5), shared by the replay, the pack tests and
``data/scenarios/validate.py``.

Payloads (``ScenarioPackStep.payload``):

* ``nhc``: ``{fixture: "<file>.json", gis?: {<storm id>: ["<file>.kml" | "<file>.kmz", …]}}`` — a
  ``CurrentStorms.json``-shaped document plus the cone / track products per storm;
* ``copernicus_ems``: ``{fixture: "<file>.json"}`` — a ``public-activations-info`` answer;
* ``meteoalarm``: ``{fixture: "<file>.atom", connector_id?: "meteoalarm.<country>", details?:
  {<CAP identifier>: "<file>.xml"}}`` — a legacy Atom feed plus the CAP documents with polygons;
* ``gdelt``: ``{fixture: "<file>.CSV" | "<file>.zip"}`` — one 15-minute export.

``ofac`` has no pack step on purpose (a simulated designation that matches seed supplier names is
not acceptable demo data).

Everything goes through the real parse functions with the pack's :class:`SourceContext` (origin
``demo``, provenance ``simulated``, tenant/pack-scoped keys, sim-clock times). The deployment's
``GDELT_*`` / ``METEOALARM_MIN_LEVEL`` tuning never applies to a replay: the code defaults do, so a
pack replays identically everywhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final

from signal_hub.adapters import copernicus_ems, gdelt, kml, meteoalarm, nhc
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext

__all__ = ["ADAPTERS", "DEMO_METEOALARM", "StepError", "fixture_names", "parse_step"]

ADAPTERS: Final = ("nhc", "copernicus_ems", "meteoalarm", "gdelt")
DEMO_METEOALARM: Final = "meteoalarm.demo"
_METEOALARM_ID_RE: Final = re.compile(r"^meteoalarm\.[a-z][a-z-]{1,40}$")
MAX_FILES: Final = 50


class StepError(ValueError):
    """A step payload the M8 adapters cannot use."""


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise StepError(f"{what} must be a fixture file name")
    return value


def fixture_names(adapter: str, payload: Mapping[str, Any]) -> list[str]:
    """Every fixture file a step reads (the main ``fixture`` first)."""
    names = [_name(payload.get("fixture"), "payload.fixture")]
    if adapter == "nhc":
        gis = payload.get("gis") or {}
        if not isinstance(gis, dict):
            raise StepError("payload.gis must map storm ids to file lists")
        for files in gis.values():
            if not isinstance(files, list):
                raise StepError("payload.gis values must be lists of file names")
            names += [_name(f, "payload.gis file") for f in files]
    elif adapter == "meteoalarm":
        details = payload.get("details") or {}
        if not isinstance(details, dict):
            raise StepError("payload.details must map CAP identifiers to file names")
        names += [_name(f, "payload.details file") for f in details.values()]
    if len(names) > MAX_FILES:
        raise StepError(f"a step reads at most {MAX_FILES} fixture files")
    return list(dict.fromkeys(names))


def meteoalarm_connector(payload: Mapping[str, Any]) -> str:
    cid = payload.get("connector_id") or DEMO_METEOALARM
    if not isinstance(cid, str) or not _METEOALARM_ID_RE.match(cid):
        raise StepError("payload.connector_id must look like 'meteoalarm.<country>'")
    return cid


def parse_step(adapter: str, payload: Mapping[str, Any], blobs: Mapping[str, bytes], ctx: SourceContext,
               gaz: Gazetteer) -> list[SignalDraft]:
    """The signal drafts of one M8 pack step (``blobs``: fixture name → bytes, :func:`fixture_names`)."""
    body = blobs[_name(payload.get("fixture"), "payload.fixture")]
    if adapter == "nhc":
        gis: dict[str, list[dict[str, Any]]] = {}
        for storm, files in sorted((payload.get("gis") or {}).items()):
            for f in files:
                gis.setdefault(str(storm).lower(), []).extend(kml.kml_geometries(kml.read_kml_or_kmz(blobs[f])))
        return nhc.parse_nhc(json.loads(body), ctx, gis=gis)
    if adapter == "copernicus_ems":
        drafts, _ = copernicus_ems.parse_ems(json.loads(body), ctx, gaz)
        return drafts
    if adapter == "meteoalarm":
        cid = meteoalarm_connector(payload)
        details = {str(k): meteoalarm.parse_cap_detail(blobs[f]) for k, f in (payload.get("details") or {}).items()}
        slug = cid.split(".", 1)[1]
        drafts, _ = meteoalarm.parse_meteoalarm(
            meteoalarm.parse_atom(body), ctx, connector=cid,
            country_iso=None if cid == DEMO_METEOALARM else gaz.country(slug.replace("-", " ")), details=details)
        return drafts
    if adapter == "gdelt":
        agg: dict[str, dict[str, Any]] = {}
        keys = gdelt.aggregate(agg, gdelt.matching_rows(gdelt.read_export(body), gdelt.DEFAULT_FILTER, gaz))
        return gdelt.drafts_for(agg, keys, ctx, gaz)
    raise StepError(f"not an M8 feed adapter: {adapter!r}")
