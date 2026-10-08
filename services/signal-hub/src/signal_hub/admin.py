"""Per-tenant Signals admin (``110`` §5): source subscriptions, watch regions, alert-rule checks.

Everything here is reached through ``/v1/tenants/{tenant}/…`` with the engine's service JWT (the
engine checks the admin role and proxies); the token's tenant scopes every read and write (RLS).

**Sources.** The catalogue is the configured feeds (:mod:`signal_hub.sources`: GDACS, USGS, each
``RSS_FEEDS`` entry, NHC, Copernicus EMS, OFAC, GDELT, each ``METEOALARM_COUNTRIES`` entry). A tenant
subscription (``source_subscription``; no row = subscribed, default cadence — except for an
**opt-in** source, GDELT and MeteoAlarm, where no row means *not* subscribed) can

* disable a source: events whose current members all come from disabled sources raise no alert for
  that tenant, and its open alerts on them resolve (:func:`event_subscribed`); the tenant's own
  signals (connector events, demo packs) always count. Global data stays readable (MCP, search);
* ask for another cadence within :data:`CADENCE_LIMITS`. Polling is shared, so the poll cadence of a
  source is the fastest cadence any subscribed tenant wants (default when none overrides), and a
  source every registered tenant disabled is paused (:func:`recompute_schedule`, ``source_schedule``);
  an opt-in source is paused until at least one tenant enabled it.

**Watch regions** (``watch_region``) extend exposure (:mod:`signal_hub.exposure`):

* ``radius``: ``radius_km`` around one named tenant node (or lane) — the node is exposed
  (``dwithin_km``; a lane: ``lane_crosses``) whenever an event footprint comes that close, beyond the
  kind radius;
* ``polygon``: when an event footprint touches the polygon, the tenant nodes (optionally only
  ``node_types``) inside it are exposed (``dwithin_km`` with the node's real distance, weight
  :data:`WATCH_POLYGON_WEIGHT_BP`) and lanes crossing it (``lane_crosses``, same weight).

**Alert rules**: :func:`rule_problems` adds semantic checks on top of the contract model.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, Discriminator, Field, StrictBool, StrictInt, field_validator, model_validator

from keelson_contracts.signals import (
    DEFAULT_ALERT_RULES,
    NODE_TYPES,
    AlertRule,
    MultiPolygon,
    NodeType,
    Polygon,
    geometry_positions,
)
from signal_hub import sources
from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.exposure import WATCH_POLYGON_WEIGHT_BP
from signal_hub.logs import get_logger

__all__ = [
    "CADENCE_LIMITS",
    "MAX_RADIUS_KM",
    "MAX_REGION_AREA_KM2",
    "MAX_REGION_POSITIONS",
    "MAX_RULES",
    "MAX_WATCH_REGIONS",
    "MIN_HYSTERESIS_BP",
    "WATCH_POLYGON_WEIGHT_BP",
    "AdminError",
    "Schedule",
    "SourceInfo",
    "SubscriptionSet",
    "SubscriptionSetItem",
    "SubscriptionUpdate",
    "WatchRegionBody",
    "WatchRegionIn",
    "WatchRegionSet",
    "WatchRegionSetItem",
    "delete_watch_region",
    "event_subscribed",
    "get_watch_region",
    "hard_rule_problems",
    "list_sources",
    "list_watch_regions",
    "load_rules_for_admin",
    "load_schedule",
    "put_subscription",
    "put_subscription_set",
    "recompute_schedule",
    "replace_watch_regions",
    "rule_problems",
    "save_rules",
    "source_catalogue",
    "upsert_watch_region",
]

log = get_logger(__name__)

CADENCE_LIMITS: Final[dict[str, tuple[int, int]]] = sources.CADENCE_LIMITS
"""Allowed poll cadence per adapter (seconds): politeness floors towards the publishers, one day max."""
CADENCE_DB_RANGE: Final = (60, 86_400)
"""``source_subscription.cadence_s`` CHECK range (migration 0003)."""
MAX_WATCH_REGIONS: Final = 100
MAX_REGION_POSITIONS: Final = 5000
MAX_REGION_AREA_KM2: Final = 20_000_000
"""About 4 % of the Earth: a larger "watch everything" polygon would expose the whole network."""
MAX_RADIUS_KM: Final = 2000
"""Largest radius region (the engine's Signals admin allows the same; migration 0003 CHECK)."""
MAX_RULES: Final = 50
MIN_HYSTERESIS_BP: Final = 100
MIN_UNEXPOSED_ALERT_BP: Final = 4000
_REGION_ID = r"^[a-z][a-z0-9_-]{0,63}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class AdminError(ValueError):
    """A request the hub refuses: ``status`` 404 (unknown), 409 (conflict) or 422 (invalid)."""

    def __init__(self, status: int, detail: str, problems: list[dict[str, str]] | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.problems = problems or []


# ─────────────────────────────── sources ───────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceInfo:
    connector_id: str
    adapter: str
    name: str
    license: str
    authority_bp: int
    host: str
    default_cadence_s: int
    min_cadence_s: int
    max_cadence_s: int
    polled: bool
    """The poller runs this adapter (``SIGNAL_ADAPTERS``)."""
    kind: str = "hazard"
    """Catalogue grouping: ``hazard`` / ``weather`` / ``regulatory`` / ``news`` (not a SignalKind)."""
    provenance: str = "fact"
    licence_class: str = "open_attribution"
    attribution: str | None = None
    terms_url: str | None = None
    opt_in: bool = False
    """No subscription row = not subscribed; paused until a tenant enables it (GDELT, MeteoAlarm)."""
    coverage: str | None = None
    description: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id, "adapter": self.adapter, "name": self.name, "license": self.license,
            "authority_bp": self.authority_bp, "host": self.host, "default_cadence_s": self.default_cadence_s,
            "min_cadence_s": self.min_cadence_s, "max_cadence_s": self.max_cadence_s, "polled": self.polled,
            "kind": self.kind, "provenance": self.provenance, "licence_class": self.licence_class,
            "attribution": self.attribution, "terms_url": self.terms_url, "opt_in": self.opt_in,
            "coverage": self.coverage, "description": self.description,
        }


def _info(settings: Settings, spec: sources.SourceSpec) -> SourceInfo:
    lo, hi = CADENCE_LIMITS[spec.adapter]
    default = spec.default_cadence_s
    # a deployment may configure a faster default than the floor; the default itself always stays valid,
    # but a stored override never leaves the table's CHECK range (source_subscription.cadence_s)
    lo = max(CADENCE_DB_RANGE[0], min(lo, default))
    hi = min(CADENCE_DB_RANGE[1], max(hi, default))
    info = spec.info
    return SourceInfo(spec.connector_id, spec.adapter, spec.name, info.license, info.authority_bp, spec.host,
                      default, lo, hi, spec.adapter in settings.enabled_adapters, kind=info.kind,
                      provenance=info.provenance, licence_class=info.licence_class, attribution=info.attribution,
                      terms_url=info.terms_url, opt_in=info.opt_in, coverage=spec.coverage,
                      description=info.description)


def source_catalogue(settings: Settings) -> list[SourceInfo]:
    """The configured sources (:func:`signal_hub.sources.catalogue`) with their admin facts."""
    return [_info(settings, spec) for spec in sources.catalogue(settings)]


def _catalogue_map(settings: Settings) -> dict[str, SourceInfo]:
    return {s.connector_id: s for s in source_catalogue(settings)}


class SubscriptionUpdate(BaseModel):
    """``PUT /v1/tenants/{tenant}/sources/{connector_id}``. ``cadence_s: null`` = the default."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    cadence_s: StrictInt | None = None


@dataclass(frozen=True, slots=True)
class Schedule:
    cadence_s: int
    paused: bool
    enabled_tenants: int
    disabled_tenants: int


def _clamp(info: SourceInfo, cadence: int | None) -> int:
    if cadence is None:
        return info.default_cadence_s
    return max(info.min_cadence_s, min(info.max_cadence_s, cadence))


async def recompute_schedule(pool: asyncpg.Pool, settings: Settings) -> dict[str, Schedule]:
    """Rebuild ``source_schedule`` from every registered tenant's subscriptions.

    One system transaction holds a transaction-scoped advisory lock (concurrent admin edits are
    serialised, the last one sees every committed subscription) and reads each tenant's rows by
    switching ``app.tenant_id`` per query, so RLS still scopes every read to one tenant."""
    catalogue = _catalogue_map(settings)
    out: dict[str, Schedule] = {}
    async with tx(pool, None) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('source_schedule', 0))")
        tenants = [r["tenant"] for r in await conn.fetch("SELECT tenant FROM tenant_registry ORDER BY tenant")]
        subs: dict[str, dict[str, asyncpg.Record]] = {}
        for tenant in tenants:
            await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
            subs[tenant] = {r["connector_id"]: r for r in await conn.fetch(
                "SELECT connector_id, enabled, cadence_s FROM source_subscription WHERE tenant = $1", tenant)}
        await conn.execute("SELECT set_config('app.tenant_id', '', true)")
        for cid, info in catalogue.items():
            wanted: list[int] = []
            disabled = 0
            for tenant in tenants:
                row = subs[tenant].get(cid)
                if (row is not None and not row["enabled"]) or (row is None and info.opt_in):
                    disabled += 1
                    continue
                wanted.append(_clamp(info, row["cadence_s"] if row is not None else None))
            cadence = min(wanted) if wanted else None
            await conn.execute(
                """INSERT INTO source_schedule (connector_id, cadence_s, enabled_tenants, disabled_tenants, updated_at)
                   VALUES ($1, $2, $3, $4, now())
                   ON CONFLICT (connector_id) DO UPDATE SET cadence_s = EXCLUDED.cadence_s,
                     enabled_tenants = EXCLUDED.enabled_tenants, disabled_tenants = EXCLUDED.disabled_tenants,
                     updated_at = now()""",
                cid, cadence, len(wanted), disabled)
            paused = not wanted if info.opt_in else bool(tenants) and not wanted
            out[cid] = Schedule(cadence or info.default_cadence_s, paused, len(wanted), disabled)
    return out


async def load_schedule(pool: asyncpg.Pool, settings: Settings) -> dict[str, Schedule]:
    """The poll schedule per catalogue source (poller; system context, global rows only).

    A source is paused only when every registered tenant disabled it (a tenant registered after the
    last recompute counts as subscribed); an opt-in source is paused until a tenant enabled it (a
    tenant registered later counts as not subscribed). Cadences are re-clamped to the current limits."""
    catalogue = _catalogue_map(settings)
    async with tx(pool, None) as conn:
        rows = {r["connector_id"]: r for r in await conn.fetch("SELECT * FROM source_schedule")}
        registered = int(await conn.fetchval("SELECT count(*) FROM tenant_registry"))
    out: dict[str, Schedule] = {}
    for cid, info in catalogue.items():
        row = rows.get(cid)
        if row is None:
            out[cid] = (Schedule(info.default_cadence_s, True, 0, registered) if info.opt_in
                        else Schedule(info.default_cadence_s, False, registered, 0))
            continue
        disabled = int(row["disabled_tenants"])
        enabled = int(row["enabled_tenants"])
        cadence = _clamp(info, row["cadence_s"]) if row["cadence_s"] is not None else info.default_cadence_s
        if info.opt_in:
            out[cid] = Schedule(cadence, enabled == 0, enabled, max(disabled, registered - enabled))
            continue
        paused = registered > 0 and disabled >= registered
        if registered > enabled + disabled:
            cadence = min(cadence, info.default_cadence_s)  # new tenants poll at the default until recomputed
        out[cid] = Schedule(cadence, paused, enabled, disabled)
    return out


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


async def list_sources(pool: asyncpg.Pool, settings: Settings, tenant: str) -> list[dict[str, Any]]:
    catalogue = source_catalogue(settings)
    schedule = await load_schedule(pool, settings)
    async with tx(pool, None) as conn:
        state = {r["connector_id"]: r for r in await conn.fetch(
            """SELECT connector_id, last_polled_at, last_status, last_error, consecutive_failures, next_poll_at,
                      items_total FROM source_cursor""")}
    async with tx(pool, tenant) as conn:
        subs = {r["connector_id"]: r for r in await conn.fetch(
            "SELECT connector_id, enabled, cadence_s, updated_at, updated_by FROM source_subscription WHERE tenant = $1",
            tenant)}
    out: list[dict[str, Any]] = []
    for info in catalogue:
        sub = subs.get(info.connector_id)
        sched = schedule[info.connector_id]
        cur = state.get(info.connector_id)
        out.append({
            **info.as_dict(),
            "enabled": bool(sub["enabled"]) if sub is not None else not info.opt_in,
            "cadence_s": sub["cadence_s"] if sub is not None else None,
            "updated_at": _iso(sub["updated_at"]) if sub is not None else None,
            "updated_by": sub["updated_by"] if sub is not None else None,
            "effective_cadence_s": sched.cadence_s,
            "paused": sched.paused,
            "poll": None if cur is None else {
                "last_polled_at": _iso(cur["last_polled_at"]), "last_status": cur["last_status"],
                "last_error": (cur["last_error"] or None) and str(cur["last_error"])[:300],
                "consecutive_failures": int(cur["consecutive_failures"]), "next_poll_at": _iso(cur["next_poll_at"]),
                "items_total": int(cur["items_total"]),
            },
        })
    return out


async def put_subscription(pool: asyncpg.Pool, settings: Settings, tenant: str, connector_id: str,
                           body: SubscriptionUpdate, actor: str | None) -> dict[str, Any]:
    info = _catalogue_map(settings).get(connector_id)
    if info is None:
        raise AdminError(404, f"unknown source {connector_id!r}")
    if body.cadence_s is not None and not info.min_cadence_s <= body.cadence_s <= info.max_cadence_s:
        raise AdminError(422, f"cadence_s must be between {info.min_cadence_s} and {info.max_cadence_s} seconds "
                              f"for {connector_id}", [{"field": "cadence_s", "problem": "out of range"}])
    async with tx(pool, None) as conn:
        await conn.execute("INSERT INTO tenant_registry (tenant) VALUES ($1) ON CONFLICT DO NOTHING", tenant)
    async with tx(pool, tenant) as conn:
        await conn.execute(
            """INSERT INTO source_subscription (tenant, connector_id, enabled, cadence_s, updated_at, updated_by)
               VALUES ($1, $2, $3, $4, now(), $5)
               ON CONFLICT (tenant, connector_id) DO UPDATE SET enabled = EXCLUDED.enabled,
                 cadence_s = EXCLUDED.cadence_s, updated_at = now(), updated_by = EXCLUDED.updated_by""",
            tenant, connector_id, body.enabled, body.cadence_s, actor)
    await recompute_schedule(pool, settings)
    return next(s for s in await list_sources(pool, settings, tenant) if s["connector_id"] == connector_id)


class SubscriptionSetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_id: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: StrictBool
    cadence_s: StrictInt | None = None
    """Omitted = keep the tenant's stored cadence wish; ``null`` = the default."""


class SubscriptionSet(BaseModel):
    """``PUT /v1/tenants/{t}/subscriptions``: the tenant's complete subscription map (the engine's
    sync, ``{sources: [{connector_id, enabled}]}``). A catalogue source that is not listed goes back
    to its default — subscribed, or not subscribed for an opt-in source — (its cadence wish is
    kept); ids the hub does not configure are ignored."""

    model_config = ConfigDict(extra="forbid")
    sources: Annotated[list[SubscriptionSetItem], Field(max_length=200)]

    @model_validator(mode="after")
    def _unique(self) -> SubscriptionSet:
        ids = [x.connector_id for x in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("connector ids must be unique")
        return self


async def put_subscription_set(pool: asyncpg.Pool, settings: Settings, tenant: str, body: SubscriptionSet,
                               actor: str | None) -> dict[str, Any]:
    """The engine's whole-map sync. The engine is the system of record and accepts any cadence wish
    in 60 s–1 d ("the hub clamps it to each source's own limits"), so an out-of-range wish is
    clamped to the source's limits and reported in ``warnings`` instead of refusing the whole map
    (a 422 here would stop every subscription — enable/disable included — from syncing on each
    re-push). The single-source ``PUT …/sources/{id}`` stays strict (422)."""
    catalogue = _catalogue_map(settings)
    known: list[SubscriptionSetItem] = []
    warnings: list[dict[str, str]] = []
    for x in body.sources:
        info = catalogue.get(x.connector_id)
        if info is None:
            continue
        if x.cadence_s is not None and not info.min_cadence_s <= x.cadence_s <= info.max_cadence_s:
            clamped = _clamp(info, x.cadence_s)
            warnings.append({"source": x.connector_id, "field": "cadence_s",
                             "problem": f"{x.cadence_s} s is outside {info.min_cadence_s}–{info.max_cadence_s} s "
                                        f"for {x.connector_id}; clamped to {clamped} s"})
            x = x.model_copy(update={"cadence_s": clamped})
        known.append(x)
    ignored = sorted(x.connector_id for x in body.sources if x.connector_id not in catalogue)
    async with tx(pool, None) as conn:
        await conn.execute("INSERT INTO tenant_registry (tenant) VALUES ($1) ON CONFLICT DO NOTHING", tenant)
    async with tx(pool, tenant) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"subscriptions:{tenant}")
        listed = [x.connector_id for x in known]
        opt_in = sorted(cid for cid, info in catalogue.items() if info.opt_in)
        await conn.execute(
            """UPDATE source_subscription SET enabled = true, updated_at = now(), updated_by = $2
               WHERE tenant = $1 AND NOT enabled AND NOT (connector_id = ANY($3::text[]))
                 AND NOT (connector_id = ANY($4::text[]))""",
            tenant, actor, listed, opt_in)
        await conn.execute(  # an unlisted opt-in source goes back to not subscribed (124 §1)
            """UPDATE source_subscription SET enabled = false, updated_at = now(), updated_by = $2
               WHERE tenant = $1 AND enabled AND connector_id = ANY($4::text[])
                 AND NOT (connector_id = ANY($3::text[]))""",
            tenant, actor, listed, opt_in)
        for x in known:
            keep_cadence = "cadence_s" not in x.model_fields_set
            await conn.execute(
                """INSERT INTO source_subscription (tenant, connector_id, enabled, cadence_s, updated_at, updated_by)
                   VALUES ($1, $2, $3, $4, now(), $5)
                   ON CONFLICT (tenant, connector_id) DO UPDATE SET enabled = EXCLUDED.enabled,
                     cadence_s = CASE WHEN $6 THEN source_subscription.cadence_s ELSE EXCLUDED.cadence_s END,
                     updated_at = now(), updated_by = EXCLUDED.updated_by
                   WHERE source_subscription.enabled IS DISTINCT FROM EXCLUDED.enabled
                      OR (NOT $6 AND source_subscription.cadence_s IS DISTINCT FROM EXCLUDED.cadence_s)""",
                tenant, x.connector_id, x.enabled, x.cadence_s, actor, keep_cadence)
    await recompute_schedule(pool, settings)
    return {"items": await list_sources(pool, settings, tenant), "ignored": ignored, "warnings": warnings}


async def event_subscribed(conn: asyncpg.Connection, tenant: str, event_id: str) -> bool:
    """Whether ``event_id`` counts for ``tenant`` (``conn`` in that tenant's context): it has a
    current visible member that is the tenant's own, comes from a source the tenant did not
    disable, or comes from an opt-in source (:data:`signal_hub.sources.OPT_IN_ADAPTERS`) the tenant
    explicitly enabled. An event without visible current members keeps counting (nothing to filter on)."""
    row = await conn.fetchrow(
        """SELECT count(*) AS members,
                  count(*) FILTER (WHERE s.tenant IS NOT NULL
                      OR (NOT (split_part(s.connector_id, '.', 1) = ANY($3::text[])) AND NOT EXISTS (
                          SELECT 1 FROM source_subscription ss
                          WHERE ss.tenant = $2 AND ss.connector_id = s.connector_id AND NOT ss.enabled))
                      OR (split_part(s.connector_id, '.', 1) = ANY($3::text[]) AND EXISTS (
                          SELECT 1 FROM source_subscription ss
                          WHERE ss.tenant = $2 AND ss.connector_id = s.connector_id AND ss.enabled))) AS wanted
           FROM signal s WHERE s.event_id = $1 AND s.is_current""",
        event_id, tenant, sorted(sources.OPT_IN_ADAPTERS))
    return row is None or int(row["members"]) == 0 or int(row["wanted"]) > 0


# ─────────────────────────────── watch regions ───────────────────────────────

RegionGeometry = Annotated[Polygon | MultiPolygon, Discriminator("type")]


class WatchRegionBody(BaseModel):
    """A watch region without its id (``PUT /v1/tenants/{t}/watch-regions/{id}``)."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Literal["polygon", "radius"]
    geometry: RegionGeometry | None = None
    node_type: NodeType | None = None
    node_code: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    radius_km: Annotated[StrictInt, Field(ge=1, le=MAX_RADIUS_KM)] | None = None
    node_types: Annotated[tuple[NodeType, ...], Field(max_length=len(NODE_TYPES))] = ()
    enabled: StrictBool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value or _CONTROL.search(value):
            raise ValueError("name must be one non-empty line without control characters")
        return value

    @model_validator(mode="after")
    def _kind(self) -> WatchRegionBody:
        if len(set(self.node_types)) != len(self.node_types):
            raise ValueError("node_types must be unique")
        if self.kind == "polygon":
            if self.geometry is None:
                raise ValueError("a polygon region needs geometry (Polygon or MultiPolygon)")
            if self.node_type is not None or self.node_code is not None or self.radius_km is not None:
                raise ValueError("a polygon region takes no node_type, node_code or radius_km")
            if len(geometry_positions(self.geometry)) > MAX_REGION_POSITIONS:
                raise ValueError(f"geometry has more than {MAX_REGION_POSITIONS} positions; simplify it")
        else:
            if self.geometry is not None or self.node_types:
                raise ValueError("a radius region takes no geometry or node_types")
            if self.node_type is None or self.node_code is None or self.radius_km is None:
                raise ValueError("a radius region needs node_type, node_code and radius_km")
        return self


class WatchRegionIn(WatchRegionBody):
    """``POST /v1/tenants/{t}/watch-regions``."""

    id: Annotated[str, Field(pattern=_REGION_ID)]


def _region_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"], "geometry": row["geometry"],
        "node_type": row["node_type"], "node_code": row["node_code"], "radius_km": row["radius_km"],
        "node_types": list(row["node_types"] or []), "enabled": row["enabled"],
        "area_km2": row["area_km2"], "node_found": row["node_found"],
        "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"]), "updated_by": row["updated_by"],
    }


_REGION_SELECT = """
SELECT w.*, CASE WHEN w.geog IS NOT NULL THEN round(ST_Area(w.geog) / 1e6)::bigint END AS area_km2,
       CASE WHEN w.kind <> 'radius' THEN NULL
            WHEN w.node_type = 'lane' THEN EXISTS (SELECT 1 FROM network_lane l WHERE l.tenant = w.tenant AND l.code = w.node_code)
            ELSE EXISTS (SELECT 1 FROM network_node n WHERE n.tenant = w.tenant AND n.type = w.node_type
                                                         AND n.code = w.node_code) END AS node_found
FROM watch_region w WHERE w.tenant = $1
"""


async def list_watch_regions(conn: asyncpg.Connection, tenant: str) -> list[dict[str, Any]]:
    return [_region_dict(r) for r in await conn.fetch(_REGION_SELECT + " ORDER BY w.id", tenant)]


async def get_watch_region(conn: asyncpg.Connection, tenant: str, region_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(_REGION_SELECT + " AND w.id = $2", tenant, region_id)
    return _region_dict(row) if row is not None else None


async def _region_problems(conn: asyncpg.Connection, tenant: str, body: WatchRegionBody, *,
                           require_node: bool = True) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    if body.kind == "polygon":
        assert body.geometry is not None
        gj = body.geometry.model_dump_json()
        row = await conn.fetchrow(
            """SELECT ST_IsValid(g) AS valid, ST_IsValidReason(g) AS reason,
                      CASE WHEN ST_IsValid(g) THEN ST_Area(g::geography) / 1e6 END AS area_km2
               FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS g) x""", gj)
        if not row["valid"]:
            problems.append({"field": "geometry", "problem": f"invalid polygon: {str(row['reason'])[:200]}"})
        elif row["area_km2"] is not None and row["area_km2"] > MAX_REGION_AREA_KM2:
            problems.append({"field": "geometry",
                             "problem": f"area {int(row['area_km2']):,} km² exceeds {MAX_REGION_AREA_KM2:,} km²"})
    elif require_node:
        if body.node_type == "lane":
            found = await conn.fetchval("SELECT 1 FROM network_lane WHERE tenant = $1 AND code = $2", tenant, body.node_code)
        else:
            found = await conn.fetchval("SELECT 1 FROM network_node WHERE tenant = $1 AND type = $2 AND code = $3",
                                        tenant, body.node_type, body.node_code)
        if not found:
            problems.append({"field": "node_code",
                             "problem": f"{body.node_type} {body.node_code!r} is not in the tenant network (push the network first)"})
    return problems


async def upsert_watch_region(conn: asyncpg.Connection, tenant: str, region_id: str, body: WatchRegionBody,
                              actor: str | None, *, create_only: bool) -> tuple[dict[str, Any], bool]:
    """Create (``create_only``: 409 when it exists) or replace one region → (region, created)."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"watch_region:{tenant}")
    exists = await conn.fetchval("SELECT 1 FROM watch_region WHERE tenant = $1 AND id = $2", tenant, region_id)
    if exists and create_only:
        raise AdminError(409, f"watch region {region_id!r} already exists")
    if not exists and int(await conn.fetchval("SELECT count(*) FROM watch_region WHERE tenant = $1", tenant)) >= MAX_WATCH_REGIONS:
        raise AdminError(422, f"at most {MAX_WATCH_REGIONS} watch regions per tenant")
    problems = await _region_problems(conn, tenant, body)
    if problems:
        raise AdminError(422, "invalid watch region", problems)
    await _write_region(conn, tenant, region_id, body, actor)
    region = await get_watch_region(conn, tenant, region_id)
    assert region is not None
    return region, not exists


async def _write_region(conn: asyncpg.Connection, tenant: str, region_id: str, body: WatchRegionBody,
                        actor: str | None) -> None:
    """Insert or replace one validated region. An identical replace changes nothing, so the
    engine's re-push after every sync does not churn ``updated_at``/``updated_by``."""
    geometry = body.geometry.model_dump(mode="json") if body.geometry is not None else None
    await conn.execute(
        """INSERT INTO watch_region (tenant, id, name, kind, geometry, geog, node_type, node_code, radius_km, node_types,
                                     enabled, created_at, updated_at, updated_by)
           VALUES ($1, $2, $3, $4, $5,
                   CASE WHEN $5::jsonb IS NULL THEN NULL ELSE ST_SetSRID(ST_GeomFromGeoJSON($5::jsonb::text), 4326)::geography END,
                   $6, $7, $8, $9, $10, now(), now(), $11)
           ON CONFLICT (tenant, id) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
             geometry = EXCLUDED.geometry, geog = EXCLUDED.geog, node_type = EXCLUDED.node_type,
             node_code = EXCLUDED.node_code, radius_km = EXCLUDED.radius_km, node_types = EXCLUDED.node_types,
             enabled = EXCLUDED.enabled, updated_at = now(), updated_by = EXCLUDED.updated_by
           WHERE (watch_region.name, watch_region.kind, watch_region.geometry, watch_region.node_type,
                  watch_region.node_code, watch_region.radius_km, watch_region.node_types, watch_region.enabled)
                 IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.kind, EXCLUDED.geometry, EXCLUDED.node_type,
                  EXCLUDED.node_code, EXCLUDED.radius_km, EXCLUDED.node_types, EXCLUDED.enabled)""",
        tenant, region_id, body.name, body.kind, geometry, body.node_type, body.node_code, body.radius_km,
        list(body.node_types), body.enabled, actor)


class WatchRegionSetItem(WatchRegionIn):
    """One region of a whole-set ``PUT /v1/tenants/{t}/watch-regions``. Also accepts the engine's
    Signals-admin shape (``keelson_engine.core.signals_admin.WatchRegion``): ``polygon`` for
    ``geometry`` and ``node: {type, code}`` for ``node_type``/``node_code``."""

    @model_validator(mode="before")
    @classmethod
    def _engine_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "polygon" in data:
            polygon = data.pop("polygon")
            if polygon is not None:
                if data.get("geometry") is not None:
                    raise ValueError("give geometry or polygon, not both")
                data["geometry"] = polygon
        if "node" in data:
            node = data.pop("node")
            if node is not None:
                if not isinstance(node, dict) or set(node) - {"type", "code"}:
                    raise ValueError("node must be {type, code}")
                if data.get("node_type") is not None or data.get("node_code") is not None:
                    raise ValueError("give node or node_type/node_code, not both")
                data["node_type"], data["node_code"] = node.get("type"), node.get("code")
        return data


class WatchRegionSet(BaseModel):
    """``PUT /v1/tenants/{t}/watch-regions``: the tenant's complete region set (the engine's sync)."""

    model_config = ConfigDict(extra="forbid")
    regions: Annotated[list[WatchRegionSetItem], Field(max_length=MAX_WATCH_REGIONS)]

    @model_validator(mode="after")
    def _unique(self) -> WatchRegionSet:
        ids = [r.id for r in self.regions]
        if len(set(ids)) != len(ids):
            raise ValueError("watch region ids must be unique")
        return self


async def replace_watch_regions(conn: asyncpg.Connection, tenant: str, regions: Sequence[WatchRegionSetItem],
                                actor: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Make ``regions`` the tenant's whole set in one transaction → (regions, deleted ids).

    A geometry problem refuses the whole set (422, ``problems[].region``). A radius region whose
    node is not in the pushed network is stored anyway (``node_found: false``; it matches once the
    node is pushed): the engine is the system of record, and one stale region must not block the rest."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"watch_region:{tenant}")
    problems: list[dict[str, str]] = []
    for reg in regions:
        problems += [{"region": reg.id, **p} for p in await _region_problems(conn, tenant, reg, require_node=False)]
    if problems:
        raise AdminError(422, "invalid watch regions", problems)
    deleted = [str(r["id"]) for r in await conn.fetch(
        "DELETE FROM watch_region WHERE tenant = $1 AND NOT (id = ANY($2::text[])) RETURNING id",
        tenant, [r.id for r in regions])]
    for reg in regions:
        await _write_region(conn, tenant, reg.id, reg, actor)
    return await list_watch_regions(conn, tenant), sorted(deleted)


async def delete_watch_region(conn: asyncpg.Connection, tenant: str, region_id: str) -> bool:
    return (await conn.execute("DELETE FROM watch_region WHERE tenant = $1 AND id = $2", tenant, region_id)) != "DELETE 0"


# ─────────────────────────────── alert rules ───────────────────────────────


def hard_rule_problems(rules: Sequence[AlertRule]) -> list[dict[str, str]]:
    """Problems no stored set may have: too many rules, duplicate ids (empty = storable)."""
    problems: list[dict[str, str]] = []
    if len(rules) > MAX_RULES:
        problems.append({"rule": "*", "problem": f"at most {MAX_RULES} rules"})
    seen: set[str] = set()
    for r in rules:
        if r.id in seen:
            problems.append({"rule": r.id, "problem": "duplicate rule id"})
        seen.add(r.id)
    return problems


def rule_problems(rules: Sequence[AlertRule]) -> list[dict[str, str]]:
    """:func:`hard_rule_problems` plus the semantic checks of the hub's rule editor (empty = valid)."""
    problems = hard_rule_problems(rules)
    for r in rules:
        w = r.when

        def bad(text: str, rule: AlertRule = r) -> None:
            problems.append({"rule": rule.id, "problem": text})

        if w.node_types and w.min_exposed_nodes == 0:
            bad("node_types has no effect while min_exposed_nodes is 0")
        if r.action == "ignore":
            if w.min_severity_bp == 0 and w.min_confidence_bp == 0 and not w.kinds and w.min_exposed_nodes == 0:
                bad("an ignore rule without any condition suppresses every alert (narrow it by kind, severity, "
                    "confidence or exposure)")
            continue
        if r.hysteresis_bp < MIN_HYSTERESIS_BP:
            bad(f"hysteresis_bp must be at least {MIN_HYSTERESIS_BP} (smaller values re-notify on every update)")
        if r.action == "alert_and_draft_case" and w.min_exposed_nodes < 1:
            bad("alert_and_draft_case needs min_exposed_nodes >= 1 (a draft case investigates exposed nodes)")
        if r.action == "alert" and w.min_exposed_nodes == 0 and w.min_severity_bp < MIN_UNEXPOSED_ALERT_BP:
            bad(f"a rule that alerts without exposure needs min_severity_bp >= {MIN_UNEXPOSED_ALERT_BP} "
                "(otherwise every minor event worldwide alerts)")
    return problems


async def load_rules_for_admin(conn: asyncpg.Connection, tenant: str) -> tuple[bool, list[AlertRule], dict[str, Any]]:
    """→ (custom, rules, meta). No stored set (or an invalid one) = the defaults."""
    row = await conn.fetchrow("SELECT rules, updated_at, updated_by FROM alert_rule_set WHERE tenant = $1", tenant)
    if row is None or not row["rules"]:
        return False, list(DEFAULT_ALERT_RULES), {"updated_at": None, "updated_by": None}
    meta = {"updated_at": _iso(row["updated_at"]), "updated_by": row["updated_by"]}
    try:
        return True, [AlertRule.model_validate(x) for x in row["rules"]], meta
    except ValueError:
        log.warning("stored alert rules invalid; showing defaults", extra={"tenant": tenant})
        return False, list(DEFAULT_ALERT_RULES), meta


async def save_rules(conn: asyncpg.Connection, tenant: str, rules: Sequence[AlertRule], actor: str | None, *,
                     strict: bool = True, strict_rules: Collection[str] = ()) -> list[dict[str, str]]:
    """Validate and store the set (empty = back to the defaults) → the warnings that did not block it.

    ``strict`` (``?strict=true``): every :func:`rule_problems` entry refuses the set
    (:class:`AdminError` 422). Otherwise (the engine's whole-set sync: the engine is the system of
    record and evaluates auto-open with the same set, so refusing it would make hub and engine
    silently disagree) only :func:`hard_rule_problems` refuse it, plus the editor checks of the
    rules named in ``strict_rules`` (a single-rule edit is strict about the rule it writes, but a
    warning on another, engine-pushed rule must not block it); the rest come back as warnings."""
    problems = rule_problems(rules)
    if strict:
        blocking = problems
    else:
        hard = hard_rule_problems(rules)
        blocking = hard + [p for p in problems if p.get("rule") in strict_rules and p not in hard]
    if blocking:
        raise AdminError(422, "invalid alert rules", blocking)
    if rules:
        await conn.execute(
            """INSERT INTO alert_rule_set (tenant, rules, updated_at, updated_by) VALUES ($1, $2, now(), $3)
               ON CONFLICT (tenant) DO UPDATE SET rules = EXCLUDED.rules, updated_at = now(),
                 updated_by = EXCLUDED.updated_by""",
            tenant, [x.model_dump(mode="json") for x in rules], actor)
    else:
        await conn.execute("DELETE FROM alert_rule_set WHERE tenant = $1", tenant)
    return problems
