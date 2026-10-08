"""Transport emission factors and the CO₂e inputs of an optimize request
(``docs/contracts/126-m8-co2-robustness.md`` §2).

* :func:`load_factor_sets` reads ``data/emissions/factors.yaml`` (``$EMISSIONS_DIR``,
  ``/app/emissions``, then the source tree's ``data/emissions``) and validates it: unique set ids,
  one positive integer factor per mode, a source citation. Platform data: tenants pick a set
  (``planning.emissions.factors_id``) but cannot edit it.
* :func:`enrich_for_emissions` adds what the optimizer needs to the ``ProblemData`` it sends —
  never to the stored problem evidence:

  - ``Sku.unit_weight_g``: the longest ``planning.emissions.unit_weights`` prefix of the SKU's style
    (a plain prefix, never a pattern), else ``default_unit_weight_g``, else ``grams_per_unit`` (the
    weight the per-kg rate cards assume, so cost and CO₂e share one assumption);
  - ``Lane.distance_km`` (DC transfers, road): great circle between the DCs x the set's road
    circuity;
  - ``InboundLane.legs`` from the map catalogue (:mod:`.geo`, the same stops as the network map):
    a factory origin drives to its first port; sea legs follow the catalogued routes (Cape / Suez
    from ``via``); a sea lane ends with a road leg from its last port to the DC; sea-air is the sea
    leg into the hub plus an air leg of great circle + the set's air uplift; air is great circle +
    uplift; road is great circle x circuity. A lane without coordinates gets no legs (its CO₂e is
    reported as not covered).

  Distances are computed in float (haversine) and rounded half up to integer km per leg before
  they enter the hashed request.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
import yaml
from keelson_contracts.optimizer import EmissionFactor, EmissionsSpec, InboundLane, LaneLeg, ProblemData, TransportMode
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

from .geo import GeoCatalog, Point, distance_km
from .visuals import _lane_stops, _locate  # noqa: PLC2701 - the network map's stop resolution, reused as is

__all__ = [
    "EMISSIONS_FILE",
    "EmissionsDataError",
    "FactorSet",
    "emissions_dirs",
    "enrich_for_emissions",
    "factor_set",
    "lane_legs",
    "load_factor_sets",
    "set_factor_sets",
    "unit_weight_g",
]

log = structlog.get_logger(__name__)

EMISSIONS_FILE: Final = "factors.yaml"


class EmissionsDataError(RuntimeError):
    """``factors.yaml`` is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactorSource(_Strict):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    publisher: Annotated[str, Field(min_length=1, max_length=120)]
    url: HttpUrl
    table: Annotated[str, Field(min_length=1, max_length=300)]
    retrieved: dt.date


class DistanceRules(_Strict):
    road_circuity_bp: Annotated[int, Field(ge=10_000, le=30_000)] = 12_000
    air_uplift_km: Annotated[int, Field(ge=0, le=500)] = 95


class FactorSet(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,63}$")]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    basis: Literal["WTW", "TTW"]
    unit: Literal["g CO2e/tkm"]
    verified: bool
    source: FactorSource
    factors: dict[TransportMode, Annotated[int, Field(strict=True, ge=1, le=100_000)]]
    distance: DistanceRules = Field(default_factory=DistanceRules)

    @model_validator(mode="after")
    def _some_factors(self) -> FactorSet:
        if not self.factors:
            raise ValueError("a factor set needs at least one mode")
        return self

    def spec(self) -> EmissionsSpec:
        """``OptimizeRequest.emissions`` for this set (modes in a fixed order)."""
        return EmissionsSpec(
            factors=[EmissionFactor(mode=m, g_co2e_per_tkm=v) for m, v in sorted(self.factors.items())],
            basis=self.basis,
            source_id=self.id,
        )


class _File(_Strict):
    version: Literal[1]
    sets: Annotated[tuple[FactorSet, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique(self) -> _File:
        ids = [s.id for s in self.sets]
        if len(set(ids)) != len(ids):
            raise ValueError("factor set ids must be unique")
        return self


def emissions_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("EMISSIONS_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path("/app/emissions"))
    for parent in Path(__file__).resolve().parents:
        dirs.append(parent / "data" / "emissions")
    return dirs


def parse_factor_sets(doc: Any, *, source: str = "<memory>") -> dict[str, FactorSet]:
    if not isinstance(doc, Mapping):
        raise EmissionsDataError(f"{source}: the factor file must be a mapping")
    try:
        parsed = _File.model_validate(doc)
    except ValidationError as exc:
        raise EmissionsDataError(f"{source}: invalid emission factors: {exc}") from exc
    return {s.id: s for s in parsed.sets}


@functools.cache
def _default() -> dict[str, FactorSet]:
    found = next((d / EMISSIONS_FILE for d in emissions_dirs() if (d / EMISSIONS_FILE).is_file()), None)
    if found is None:
        raise EmissionsDataError(f"{EMISSIONS_FILE} not found (searched {[str(d) for d in emissions_dirs()[:3]]})")
    try:
        doc = yaml.safe_load(found.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EmissionsDataError(f"cannot read {found}: {exc}") from exc
    sets = parse_factor_sets(doc, source=str(found))
    log.info("emission factors loaded", source=str(found), sets=sorted(sets))
    return sets


_override: dict[str, FactorSet] | None = None


def load_factor_sets() -> dict[str, FactorSet]:
    """Every factor set by id (cached; raises :class:`EmissionsDataError` when missing or invalid)."""
    return _override if _override is not None else _default()


def set_factor_sets(sets: Mapping[str, FactorSet] | None) -> None:
    """Install factor sets (tests); ``None`` restores the file-backed default."""
    global _override
    _override = dict(sets) if sets is not None else None


def factor_set(set_id: str) -> FactorSet | None:
    """The set ``set_id``, or ``None`` (logged) when the file or the set is unavailable: plans are
    then computed without CO₂e rather than failing."""
    try:
        sets = load_factor_sets()
    except EmissionsDataError as exc:
        log.warning("emission factors unavailable; plans carry no CO2e", error=str(exc)[:300])
        return None
    found = sets.get(set_id)
    if found is None:
        log.warning("unknown emission factor set; plans carry no CO2e", factors_id=set_id, known=sorted(sets))
    return found


# ───────────────────────────── enrichment ─────────────────────────────


def unit_weight_g(style_code: str, rules: Iterable[Any], default_g: int) -> int:
    """Grams per unit of ``style_code``: the longest matching prefix rule, else ``default_g``."""
    best: tuple[int, int] | None = None
    for rule in rules:
        prefix = str(rule.style_prefix)
        if style_code.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), int(rule.grams))
    return best[1] if best is not None else default_g


def _km(value: float) -> int:
    return int(math.floor(value + 0.5))


def _polyline_km(points: Sequence[Point]) -> float:
    return sum(distance_km(a, b) for a, b in zip(points, points[1:], strict=False))


def _leg(mode: TransportMode, km: float) -> list[LaneLeg]:
    rounded = _km(km)
    return [LaneLeg(mode=mode, distance_km=rounded)] if rounded >= 1 else []


def lane_legs(lane: InboundLane, where: Mapping[tuple[str, str], tuple[str, Point]], geo: GeoCatalog,
              *, circuity_bp: int, air_uplift_km: int) -> list[LaneLeg]:
    """The legs of one inbound lane (see the module docstring); ``[]`` = not locatable."""
    stops = _lane_stops(lane, where, geo)
    if stops is None:
        return []
    origin, dest = stops[0][1], stops[-1][1]

    def road(a: Point, b: Point) -> list[LaneLeg]:
        return _leg("road", distance_km(a, b) * circuity_bp / 10_000)

    def air(a: Point, b: Point) -> list[LaneLeg]:
        return _leg("air", distance_km(a, b) + air_uplift_km)

    if lane.mode == "road":
        return road(origin, dest)
    if lane.mode == "air":
        return air(origin, dest)
    ports = [s for s in stops[:-1] if s[2]]
    if not ports:
        return []
    feeder = [] if stops[0][2] else road(origin, ports[0][1])
    if lane.mode == "sea_air":
        split = geo.sea_air_legs(stops, lane.via) if not feeder else None
        if split is not None:
            sea, _hub, _arc = split
            return [*_leg("sea", _polyline_km(sea)), *air(sea[-1], dest)]
        hub = next((geo.place(v) for v in lane.via if geo.place(v) is not None), None)
        if hub is None:
            return []
        return [*feeder, *_leg("sea", distance_km(ports[0][1], hub)), *air(hub, dest)]
    # sea
    if len(ports) >= 2:
        path = geo.stops_path(ports, mode="sea", via=lane.via)
        return [*feeder, *_leg("sea", _polyline_km(path)), *road(ports[-1][1], dest)]
    short = geo.short_sea_route(ports[0][0], dest)
    if short is not None:
        return [*feeder, *_leg("sea", _polyline_km([ports[0][1], *short[1][1:]])), *road(short[1][-1], dest)]
    return [*feeder, *_leg("sea", distance_km(ports[0][1], dest))]


def enrich_for_emissions(problem: ProblemData, planning: Any, geo: GeoCatalog) -> tuple[ProblemData, FactorSet] | None:
    """``(problem with weights / distances / legs, factor set)``; ``None`` when emissions are off
    or no factor set is available (the problem is then sent unchanged)."""
    cfg = planning.emissions
    if not cfg.enabled:
        return None
    fs = factor_set(cfg.factors_id)
    if fs is None:
        return None
    default_g = cfg.default_unit_weight_g or planning.grams_per_unit
    skus = [s.model_copy(update={"unit_weight_g": unit_weight_g(s.style_code, cfg.unit_weights, default_g)})
            for s in problem.skus]
    where = _locate(problem, geo)
    circuity, uplift = fs.distance.road_circuity_bp, fs.distance.air_uplift_km
    lanes = []
    for ln in problem.lanes:
        a, b = where.get(("dc", ln.from_dc)), where.get(("dc", ln.to_dc))
        km = None if a is None or b is None else _km(distance_km(a[1], b[1]) * circuity / 10_000)
        lanes.append(ln.model_copy(update={"distance_km": km}))
    inbound = [il.model_copy(update={"legs": lane_legs(il, where, geo, circuity_bp=circuity, air_uplift_km=uplift)})
               for il in problem.inbound_lanes]
    return problem.model_copy(update={"skus": skus, "lanes": lanes, "inbound_lanes": inbound}), fs
