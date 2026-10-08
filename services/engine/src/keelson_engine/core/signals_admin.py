"""Signals back-office (``docs/contracts/110-m7-backoffice-generic.md`` §5): source subscriptions,
watch regions and alert rules of a tenant — the engine side of the Signals admin page.

The engine is the system of record for the tenant's **settings** (``tenant.config``:
``signal_sources``, ``watch_regions``, ``alert_rules``) and pushes them to the Signal Hub with the
service JWT (tenant claim = the admin's tenant), so a hub restart never loses them and the
engine's own alert auto-open keeps using the same rule set (``core.alerts.rules_for``). Every push
is best effort: the answer says ``hub_synced`` (``false`` + ``hub_error`` when the hub is down or
refused); the tenant network push after every scn sync (``core.network``) pushes them again.

Hub endpoints (service JWT, ``tenant`` claim; ``services/signal-hub`` ``app.py``):

==================================================  ============================================
``GET  /v1/tenants/{tenant}/sources``               catalogue + subscription + poll state
                                                    (fallback ``GET /v1/sources``: feed status)
``PUT  /v1/tenants/{tenant}/subscriptions``         ``{sources: [{connector_id, enabled, cadence_s?}]}``
``PUT  /v1/tenants/{tenant}/watch-regions``         ``{regions: [hub WatchRegionSetItem]}``
``GET|PUT /v1/tenants/{tenant}/alert-rules``        ``{rules: [AlertRule]}`` (M6)
==================================================  ============================================

A **watch region** adds exposure on top of the tenant network: ``{id, name, kind: polygon,
geometry (GeoJSON Polygon / MultiPolygon, WGS84), node_types?}`` or ``{id, name, kind: radius,
node_type, node_code, radius_km}`` (a circle around one of the tenant's named nodes). Accepted
input spellings: ``polygon`` for ``geometry`` (a GeoJSON object or just the outer ring
``[[lon, lat], …]``, closed if needed) and ``node: {type, code}`` for ``node_type``/``node_code``;
a radius region without ``node_type`` takes the type of the one network node with that code. The
answer lists each region with ``geometry`` **and** ``polygon`` (outer ring of the first polygon),
``node`` and the flat node fields. Free text (names) is the admin's own and never reaches an LLM.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator
from sqlalchemy import text

from keelson_contracts.signals import DEFAULT_ALERT_RULES, AlertRule, MultiPolygon, NodeType, Polygon
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import check_tenant_slug, system_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "CADENCE_RANGE",
    "MAX_WATCH_REGIONS",
    "SourceSetting",
    "WatchRegion",
    "get_alert_rules",
    "get_network_codes",
    "get_sources",
    "get_watch_regions",
    "push_settings",
    "put_alert_rules",
    "put_source",
    "put_watch_regions",
]

log = get_logger(__name__)

MAX_WATCH_REGIONS: Final = 50
MAX_ALERT_RULES: Final = 50
MAX_SUBSCRIPTIONS: Final = 100
"""Source subscriptions stored per tenant (a subscription names a hub source id; bounded so a
PUT loop cannot grow ``tenant.config`` without limit)."""
MAX_REGION_POSITIONS: Final = 2000
CADENCE_RANGE: Final = (60, 86_400)
"""Allowed polling cadence wish (the hub clamps it to each source's own limits)."""
_SOURCE_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,79}")
_TERMS_URL_RE: Final = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[^\s\x00-\x1f\x7f\\\"'<>]{0,500}")
"""A source's terms link as the web may render it: absolute https, plain host (M8, ``124`` §4)."""
_PROVENANCES: Final = ("fact", "inference")
_LICENCE_CLASSES: Final = ("public_domain", "open_attribution", "terms_apply", "publisher_copyright")
_ID_PATTERN: Final = r"^[a-z][a-z0-9_-]{0,39}$"
_NODE_CODE_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.:/#>-]*$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _ring_to_polygon(ring: list[Any]) -> dict[str, Any]:
    """``[[lon, lat], …]`` (open or closed) → GeoJSON ``Polygon`` (closed)."""
    points = [list(p) for p in ring]
    if points and points[0] != points[-1]:
        points.append(list(points[0]))
    return {"type": "Polygon", "coordinates": [points]}


def _same_outer_ring(geometry: Any, polygon: Any) -> bool:
    """Whether ``polygon`` (a GeoJSON Polygon) is the outer ring of ``geometry``'s first polygon
    (what :meth:`WatchRegion.view` answers next to ``geometry``)."""
    try:
        coords = geometry["coordinates"] if geometry["type"] == "Polygon" else geometry["coordinates"][0]
        return [list(p) for p in coords[0]] == [list(p) for p in polygon["coordinates"][0]]
    except (KeyError, IndexError, TypeError):
        return False


def _positions(geometry: Polygon | MultiPolygon) -> int:
    polys = [geometry.coordinates] if isinstance(geometry, Polygon) else list(geometry.coordinates)
    return sum(len(ring) for poly in polys for ring in poly)


class WatchRegion(_Model):
    """One watch region (the hub's ``WatchRegionSetItem`` shape; see the module docstring for the
    accepted input spellings)."""

    id: Annotated[str, Field(pattern=_ID_PATTERN)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Literal["polygon", "radius"]
    geometry: Annotated[Polygon | MultiPolygon, Field(discriminator="type")] | None = None
    node_type: NodeType | None = None
    node_code: Annotated[str, Field(min_length=1, max_length=120, pattern=_NODE_CODE_PATTERN)] | None = None
    radius_km: Annotated[StrictInt, Field(ge=1, le=2000)] | None = None
    node_types: Annotated[tuple[NodeType, ...], Field(max_length=6)] = ()
    enabled: StrictBool = True

    @model_validator(mode="before")
    @classmethod
    def _spellings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = {k: v for k, v in data.items()
                if k not in ("created_at", "created_by", "updated_at", "updated_by", "area_km2", "node_found")}
        polygon = data.pop("polygon", None)
        if polygon is not None:
            if isinstance(polygon, list):
                if len(polygon) > MAX_REGION_POSITIONS or not all(isinstance(p, list) for p in polygon):
                    raise ValueError("polygon must be a list of [lon, lat] positions")
                polygon = _ring_to_polygon(polygon)
            geometry = data.get("geometry")
            if geometry is not None and not _same_outer_ring(geometry, polygon):
                raise ValueError("give geometry or polygon, not both")  # (both = an unchanged GET answer)
            if geometry is None:
                data["geometry"] = polygon
        node = data.pop("node", None)
        if node is not None:
            if not isinstance(node, dict) or set(node) - {"type", "code"}:
                raise ValueError("node must be {type, code}")
            flat = (data.get("node_type"), data.get("node_code"))
            if flat != (None, None) and flat != (node.get("type"), node.get("code")):
                raise ValueError("give node or node_type/node_code, not both")
            data["node_type"], data["node_code"] = node.get("type"), node.get("code")
        if data.get("node_types") is None:
            data.pop("node_types", None)
        return data

    @model_validator(mode="after")
    def _shape(self) -> WatchRegion:
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in self.name) or self.name != self.name.strip():
            raise ValueError("name must be one trimmed line")
        if len(set(self.node_types)) != len(self.node_types):
            raise ValueError("node_types must be unique")
        if self.kind == "polygon":
            if self.geometry is None or self.node_type is not None or self.node_code is not None \
                    or self.radius_km is not None:
                raise ValueError("a polygon region has geometry and no node / radius_km")
            if _positions(self.geometry) > MAX_REGION_POSITIONS:
                raise ValueError(f"a polygon has at most {MAX_REGION_POSITIONS} positions")
        elif self.geometry is not None or self.node_types or self.node_code is None or self.radius_km is None:
            raise ValueError("a radius region names a node and radius_km and has no geometry / node_types")
        return self

    def hub(self) -> dict[str, Any]:
        """The hub's ``WatchRegionSetItem`` JSON (also the stored form)."""
        return self.model_dump(mode="json")

    def view(self) -> dict[str, Any]:
        out = self.model_dump(mode="json")
        ring = None
        if self.geometry is not None:
            first = self.geometry.coordinates if isinstance(self.geometry, Polygon) else self.geometry.coordinates[0]
            ring = [list(p) for p in first[0]]
        out["polygon"] = ring
        out["node"] = ({"type": self.node_type, "code": self.node_code}
                       if self.node_type is not None and self.node_code is not None else None)
        return out


class SourceSetting(_Model):
    """``PUT|PATCH /api/admin/signals/sources/{id}``: at least one field. ``cadence_s: null`` =
    back to the source's default cadence."""

    enabled: StrictBool | None = None
    cadence_s: Annotated[StrictInt, Field(ge=CADENCE_RANGE[0], le=CADENCE_RANGE[1])] | None = None

    @model_validator(mode="after")
    def _some(self) -> SourceSetting:
        if not self.model_fields_set:
            raise ValueError("set enabled and/or cadence_s")
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled must be true or false")
        return self


# ───────────────────────────── tenant config ─────────────────────────────


async def _config(tenant: str) -> dict[str, Any]:
    check_tenant_slug(tenant)
    async with system_session() as s:
        cfg = (await s.execute(text("SELECT config FROM tenant WHERE slug = :t"), {"t": tenant})).scalar_one_or_none()
    return dict(cfg) if isinstance(cfg, dict) else {}


async def _set_config(tenant: str, key: str, value: Any) -> Any:
    """Set one ``tenant.config`` key (row-locked read-modify-write); returns the previous value."""
    async with system_session() as s:
        cfg = (await s.execute(text("SELECT config FROM tenant WHERE slug = :t FOR UPDATE"),
                               {"t": tenant})).scalar_one_or_none()
        if cfg is None:
            raise AdminError(404, "not_found", "tenant not found")
        cfg = dict(cfg)
        before = cfg.get(key)
        cfg[key] = value
        await s.execute(text("UPDATE tenant SET config = CAST(:c AS jsonb), updated_at = now() WHERE slug = :t"),
                        {"c": dumps(cfg), "t": tenant})
    return before


async def _push(tenant: str, path: str, body: Any) -> tuple[bool, str | None]:
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured():
        return False, "signal_hub_not_configured"
    try:
        await signal_hub.request_json("PUT", f"/v1/tenants/{tenant}/{path}", tenant=tenant, json_body=body,
                                      allow_empty=True)
    except signal_hub.HubNotFound:
        return False, "signal_hub_unsupported"
    except signal_hub.HubError as exc:
        log.warning("signals settings not pushed", tenant=tenant, path=path, error=exc.code, status=exc.status)
        return False, exc.code
    return True, None


# ───────────────────────────── sources ─────────────────────────────


def _subscriptions(cfg: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``source id → {enabled, cadence_s?}`` (``cadence_s`` only when the admin chose one)."""
    raw = cfg.get("signal_sources")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and _SOURCE_ID_RE.fullmatch(k) and isinstance(v, dict):
                entry: dict[str, Any] = {"enabled": bool(v.get("enabled", True))}
                cad = v.get("cadence_s")
                if isinstance(cad, int) and not isinstance(cad, bool) and CADENCE_RANGE[0] <= cad <= CADENCE_RANGE[1]:
                    entry["cadence_s"] = cad
                out[k] = entry
    return out


def _subscription_body(subs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {"sources": [{"connector_id": k, "enabled": bool(v["enabled"]), "cadence_s": v.get("cadence_s")}
                        for k, v in sorted(subs.items())]}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _choice(value: Any, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _terms_url(value: Any) -> str | None:
    """Only an https link with a plain host survives (no ``javascript:``, userinfo or whitespace)."""
    return value if isinstance(value, str) and _TERMS_URL_RE.fullmatch(value) else None


async def get_sources(tenant: str) -> dict[str, Any]:
    """The hub's feeds (catalogue, cadence, licence, poll state) + this tenant's subscription. M8
    (``124`` §4) passes through the catalogue facts the hub reports — provenance, licence class,
    terms link (https only), opt-in, polled (``SIGNAL_ADAPTERS``), authority, cadence limits,
    coverage and description — each cleaned and ``None`` when absent or malformed."""
    from keelson_engine.core import signal_hub

    subs = _subscriptions(await _config(tenant))
    hub_error: str | None = None
    rows: list[dict[str, Any]] = []
    try:
        try:
            body = await signal_hub.request_json("GET", f"/v1/tenants/{tenant}/sources", tenant=tenant)
        except signal_hub.HubNotFound:  # an older hub: global feed status only
            body = await signal_hub.request_json("GET", "/v1/sources", tenant=tenant)
        raw = body.get("items", body.get("sources")) if isinstance(body, dict) else body
        rows = [r for r in (raw or []) if isinstance(r, dict)]
    except signal_hub.HubError as exc:
        hub_error = exc.code
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        sid = str(r.get("connector_id") or r.get("id") or "")
        if not _SOURCE_ID_RE.fullmatch(sid) or sid in seen:
            continue
        seen.add(sid)
        poll = r.get("poll") if isinstance(r.get("poll"), dict) else r
        sub = subs.get(sid)
        items.append({
            "id": sid,
            "name": _short(r.get("title") or r.get("name")),
            "title": _short(r.get("title") or r.get("name")),
            "kind": _short(r.get("kind"), 60),
            "licence": _short(r.get("licence") or r.get("license")),
            "attribution": _short(r.get("attribution")),
            # M8 catalogue facts (124 §4); every one optional, cleaned, never trusted as markup
            "provenance": _choice(r.get("provenance"), _PROVENANCES),
            "licence_class": _choice(r.get("licence_class"), _LICENCE_CLASSES),
            "terms_url": _terms_url(r.get("terms_url")),
            "opt_in": _bool(r.get("opt_in")),
            "polled": _bool(r.get("polled")),
            "authority_bp": _int(r.get("authority_bp")),
            "min_cadence_s": _int(r.get("min_cadence_s")),
            "max_cadence_s": _int(r.get("max_cadence_s")),
            "coverage": _short(r.get("coverage")),
            "description": _short(r.get("description"), 500),
            "cadence_s": (sub or {}).get("cadence_s", _int(r.get("cadence_s"))),
            "effective_cadence_s": _int(r.get("effective_cadence_s")),
            "default_cadence_s": _int(r.get("default_cadence_s")),
            "paused": r.get("paused") if isinstance(r.get("paused"), bool) else None,
            "last_polled_at": poll.get("last_polled_at"), "next_poll_at": poll.get("next_poll_at"),
            "last_status": _short(poll.get("last_status")), "last_error": _short(poll.get("last_error")),
            "consecutive_failures": _int(poll.get("consecutive_failures")),
            "items_total": _int(poll.get("items_total")),
            "enabled": sub["enabled"] if sub is not None else bool(r.get("enabled", True)),
        })
    for sid, sub in sorted(subs.items()):
        if sid not in seen:  # a subscription to a source the hub does not report (yet)
            items.append({"id": sid, "name": None, "title": None, "kind": None, "licence": None, "attribution": None,
                          "provenance": None, "licence_class": None, "terms_url": None, "opt_in": None,
                          "polled": None, "authority_bp": None, "min_cadence_s": None, "max_cadence_s": None,
                          "coverage": None, "description": None,
                          "cadence_s": sub.get("cadence_s"), "effective_cadence_s": None, "default_cadence_s": None,
                          "paused": None, "last_polled_at": None, "next_poll_at": None, "last_status": None,
                          "last_error": None, "consecutive_failures": None, "items_total": None,
                          "enabled": sub["enabled"]})
    return {"items": items, "hub_error": hub_error}


def _short(value: Any, limit: int = 300) -> str | None:
    if value is None:
        return None
    text_ = str(value)
    return text_ if len(text_) <= limit else text_[: limit - 1] + "…"


async def put_source(tenant: str, source_id: str, setting: SourceSetting, actor: dict[str, Any]) -> dict[str, Any]:
    """Change one subscription (``enabled`` and/or ``cadence_s``) and push the whole map."""
    if not _SOURCE_ID_RE.fullmatch(source_id or ""):
        raise AdminError(404, "not_found", "source not found")
    subs = _subscriptions(await _config(tenant))
    default = True
    if source_id not in subs and not ("enabled" in setting.model_fields_set and setting.enabled is not None):
        default = await _hub_enabled_default(tenant, source_id)  # an opt-in source starts unsubscribed (124 §1)
    before = dict(subs.get(source_id) or {"enabled": default})
    after = dict(before)
    if "enabled" in setting.model_fields_set and setting.enabled is not None:
        after["enabled"] = setting.enabled
    if "cadence_s" in setting.model_fields_set:
        if setting.cadence_s is None:
            after.pop("cadence_s", None)
        else:
            after["cadence_s"] = setting.cadence_s
    if source_id not in subs and len(subs) >= MAX_SUBSCRIPTIONS:
        raise AdminError(422, "too_many_subscriptions", f"at most {MAX_SUBSCRIPTIONS} source subscriptions")
    subs[source_id] = after
    await _set_config(tenant, "signal_sources", {k: v for k, v in sorted(subs.items())})
    synced, error = await _push(tenant, "subscriptions", _subscription_body(subs))
    await admin_audit(actor, "signals.source", f"signal_source:{source_id}", before,
                      {**after, "hub_synced": synced}, tenant=tenant)
    return {"id": source_id, "enabled": after["enabled"], "cadence_s": after.get("cadence_s"),
            "hub_synced": synced, "hub_error": error}


async def _hub_enabled_default(tenant: str, source_id: str) -> bool:
    """Whether the hub counts ``source_id`` as subscribed for a tenant without a stored wish: true,
    except for an opt-in source (GDELT, MeteoAlarm). Unknown (hub down, older hub) → true, the M7 rule."""
    from keelson_engine.core import signal_hub

    try:
        body = await signal_hub.request_json("GET", f"/v1/tenants/{tenant}/sources", tenant=tenant)
    except signal_hub.HubError:
        return True
    for r in (body.get("items") if isinstance(body, dict) else None) or []:
        if isinstance(r, dict) and r.get("connector_id") == source_id and isinstance(r.get("enabled"), bool):
            return bool(r["enabled"])
    return True


# ───────────────────────────── network codes (radius regions) ─────────────────────────────


async def get_network_codes(tenant: str) -> dict[str, Any]:
    """``{nodes: {type: [codes]}}`` of the tenant network the engine pushes to the hub (built from
    the current snapshots; stores are aggregated to their clusters, as in the push)."""
    from keelson_engine.core.network import build_tenant_network

    network = await build_tenant_network(tenant)
    nodes: dict[str, list[str]] = {}
    if network is not None:
        for n in network.nodes:
            nodes.setdefault(str(n.type), []).append(str(n.code))
    return {"nodes": {k: sorted(set(v)) for k, v in sorted(nodes.items())}}


# ───────────────────────────── watch regions ─────────────────────────────


def _regions(cfg: Mapping[str, Any]) -> list[WatchRegion]:
    out: list[WatchRegion] = []
    for raw in cfg.get("watch_regions") or []:
        try:
            out.append(WatchRegion.model_validate(raw))
        except ValidationError:
            log.warning("invalid stored watch region skipped")
    return out


async def get_watch_regions(tenant: str) -> dict[str, Any]:
    return {"items": [r.view() for r in _regions(await _config(tenant))]}


def _errors(exc: ValidationError) -> list[dict[str, str]]:
    return [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg"))[:200]}
            for e in exc.errors()[:20]]


async def put_watch_regions(tenant: str, regions: list[dict[str, Any]], actor: dict[str, Any]) -> dict[str, Any]:
    """Replace the tenant's watch regions (validated; a radius region without ``node_type`` takes the
    type of the one network node with that code) and push them to the hub."""
    if len(regions) > MAX_WATCH_REGIONS:
        raise AdminError(422, "too_many_regions", f"at most {MAX_WATCH_REGIONS} watch regions")
    network: dict[str, list[str]] | None = None
    prepared: list[dict[str, Any]] = []
    for i, raw in enumerate(regions):
        r = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(r, dict) and r.get("kind") == "radius" and r.get("node") is None \
                and r.get("node_type") is None and isinstance(r.get("node_code"), str):
            if network is None:
                network = (await get_network_codes(tenant))["nodes"]
            types = [t for t, codes in network.items() if r["node_code"] in codes and t != "lane"]
            if len(types) != 1:
                raise AdminError(422, "watch_region_invalid",
                                 f"regions[{i}]: {'no' if not types else 'more than one'} node of your network "
                                 f"has the code {str(r['node_code'])[:60]!r}; give node_type",
                                 errors=[{"loc": f"regions.{i}.node_type", "msg": "node_type needed"}])
            r["node_type"] = types[0]
        prepared.append(r)
    try:
        parsed = [WatchRegion.model_validate(r) for r in prepared]
    except ValidationError as exc:
        raise AdminError(422, "watch_region_invalid", "a watch region is invalid", errors=_errors(exc)) from None
    ids = [r.id for r in parsed]
    if len(set(ids)) != len(ids):
        raise AdminError(422, "watch_region_invalid", "watch region ids must be unique")
    data = [r.hub() for r in parsed]
    before = await _set_config(tenant, "watch_regions", data)
    synced, error = await _push(tenant, "watch-regions", {"regions": data})
    await admin_audit(actor, "signals.watch_regions", "watch_regions", {"count": len(before or [])},
                      {"count": len(data), "ids": ids, "hub_synced": synced}, tenant=tenant)
    return {"items": [r.view() for r in parsed], "hub_synced": synced, "hub_error": error}


# ───────────────────────────── alert rules ─────────────────────────────


async def get_alert_rules(tenant: str) -> dict[str, Any]:
    from keelson_engine.core import alerts

    custom = await alerts.custom_rules(tenant)
    rules = custom or DEFAULT_ALERT_RULES
    return {"custom": bool(custom), "rules": [r.model_dump(mode="json") for r in rules],
            "defaults": [r.model_dump(mode="json") for r in DEFAULT_ALERT_RULES]}


async def put_alert_rules(tenant: str, rules: list[dict[str, Any]], actor: dict[str, Any]) -> dict[str, Any]:
    """Store the tenant's alert rules (``[]`` = back to the defaults) and push them to the hub."""
    from keelson_engine.core.network import push_alert_rules

    if len(rules) > MAX_ALERT_RULES:
        raise AdminError(422, "too_many_rules", f"at most {MAX_ALERT_RULES} alert rules")
    try:
        parsed = [AlertRule.model_validate(r) for r in rules]
    except ValidationError as exc:
        raise AdminError(422, "alert_rule_invalid", "an alert rule is invalid", errors=_errors(exc)) from None
    ids = [r.id for r in parsed]
    if len(set(ids)) != len(ids):
        raise AdminError(422, "alert_rule_invalid", "alert rule ids must be unique")
    data = [r.model_dump(mode="json") for r in parsed]
    before = await _set_config(tenant, "alert_rules", data)
    from keelson_engine.core import signal_hub

    synced = await push_alert_rules(tenant) if signal_hub.is_configured() else False
    await admin_audit(actor, "signals.alert_rules", "alert_rules", {"rules": before},
                      {"rules": data, "hub_synced": synced}, tenant=tenant)
    return {**await get_alert_rules(tenant), "hub_synced": synced}


async def push_settings(tenant: str) -> dict[str, bool]:
    """Push subscriptions + watch regions (``core.network`` calls it after the network push)."""
    cfg = await _config(tenant)
    subs = _subscriptions(cfg)
    out = {"subscriptions": True, "watch_regions": True}
    if subs:
        out["subscriptions"], _ = await _push(tenant, "subscriptions", _subscription_body(subs))
    regions = _regions(cfg)
    if regions:
        out["watch_regions"], _ = await _push(tenant, "watch-regions", {"regions": [r.hub() for r in regions]})
    return out
