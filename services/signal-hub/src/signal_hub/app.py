"""Signal Hub API (``signal-hub-api``, port 8300, internal only): REST ``/v1``, MCP ``/mcp``,
demo fixture route ``/demo-feeds`` (DEMO_MODE), health. Background loops in the API process:
pending-extraction retries (it is the process on the ``ai`` network), demo auto-advance, expiry sweep,
cluster maintenance (M8: merge pass + opt-in embedding jobs, ``signal_hub.clustering_api``).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import asyncpg
import httpx
import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from keelson_contracts.signals import (
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    CONNECTOR_ID_PATTERN,
    DEFAULT_ALERT_RULES,
    PACK_ID_PATTERN,
    UUID_PATTERN,
    AlertRule,
    AlertStatusUpdate,
    SignalKind,
    TenantNetwork,
)
from signal_hub import admin, queries
from signal_hub.adapters.connector import SUPPORTED_TYPES, ConnectorEvent, parse_connector_event
from signal_hub.auth import Principal, check_tenant
from signal_hub.clustering_api import build_clustering_router, run_maintenance
from signal_hub.config import Settings, load_settings
from signal_hub.db import create_pool, tx
from signal_hub.gazetteer import Gazetteer, load_gazetteer
from signal_hub.httpfetch import Fetcher, build_internal_client
from signal_hub.llm_key import load_stored_key, resolve_llm_key
from signal_hub.logs import configure_logging, get_logger
from signal_hub.mcp_server import build_mcp
from signal_hub.models import SourceContext
from signal_hub.pipeline import Pipeline
from signal_hub.quarantine import LiteLlmClient, LlmClient
from signal_hub.replay import FIXTURE_TYPES, PackError, Replay, fixture_path, list_packs
from signal_hub.timeutil import utc_now
from signal_hub.vectors import Embedder, LiteLlmEmbedder

__all__ = ["HubState", "build_state", "create_app", "refresh_llm_key", "run"]

log = get_logger(__name__)
Clock = Callable[[], dt.datetime]
MAX_BODY_BYTES = {"/v1/tenants/": 8 * 1024 * 1024}
DEFAULT_MAX_BODY = 512 * 1024
TenantPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
UuidPath = Annotated[str, Path(pattern=UUID_PATTERN)]
PackPath = Annotated[str, Path(pattern=PACK_ID_PATTERN)]
ConnectorPath = Annotated[str, Path(pattern=CONNECTOR_ID_PATTERN)]
RegionPath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
RulePath = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


@dataclass(slots=True)
class HubState:
    settings: Settings
    pool: asyncpg.Pool
    gazetteer: Gazetteer
    pipeline: Pipeline
    replay: Replay
    clock: Clock = utc_now
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    llm: LlmClient | None = None
    embedder: Embedder | None = None
    """Remote embeddings (``SIGNALS_EMBED_PROVIDER=litellm`` only; the merge pass uses them)."""


class BodyLimit:
    """Reject request bodies over the per-path limit (413) before they are parsed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        limit = next((v for k, v in MAX_BODY_BYTES.items() if path.startswith(k)), DEFAULT_MAX_BODY)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
            return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited, send)


def _hub(request: Request) -> HubState:
    hub: HubState = request.app.state.hub
    return hub


def _require_demo(hub: HubState, tenant: str) -> None:
    if not hub.settings.demo_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "demo mode is off")
    if hub.settings.demo_tenants and tenant not in hub.settings.demo_tenants:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant is not a demo tenant")


class AdvanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: Annotated[int, Field(ge=0, le=90 * 24 * 3600)] | None = None
    to_step: Annotated[int, Field(ge=0, le=10_000)] | None = None


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: Annotated[float, Field(ge=0, le=100_000)] = 0.0


class RulesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: Annotated[list[AlertRule], Field(max_length=admin.MAX_RULES)]


def _csv(value: str | None, allowed: tuple[str, ...], name: str) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise HTTPException(422, f"unknown {name}: {', '.join(bad[:5])}")
    return items


def build_router() -> APIRouter:
    r = APIRouter()

    # ─────────────── alerts ───────────────
    @r.get("/v1/alerts")
    async def list_alerts(
        request: Request, principal: Principal,
        status_: Annotated[str | None, Query(alias="status")] = None,
        severity: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        updated_since: dt.datetime | None = None,
    ) -> dict[str, Any]:
        hub = _hub(request)
        if updated_since is not None and updated_since.tzinfo is None:
            raise HTTPException(422, "updated_since needs a timezone offset")
        async with tx(hub.pool, principal.tenant) as conn:
            alerts = await queries.list_alerts(
                conn, principal.tenant, statuses=_csv(status_, ALERT_STATUSES, "status"),
                severities=_csv(severity, ALERT_SEVERITIES, "severity"), limit=limit, updated_since=updated_since,
            )
            new_count = await conn.fetchval("SELECT count(*) FROM alert WHERE tenant = $1 AND status = 'new'", principal.tenant)
        return {"items": [a.model_dump(mode="json") for a in alerts], "new_count": new_count}

    @r.get("/v1/alerts/changes")
    async def alert_changes(
        request: Request, principal: Principal,
        after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        hub = _hub(request)
        async with tx(hub.pool, principal.tenant) as conn:
            return await queries.alert_changes(conn, principal.tenant, after=after, limit=limit)

    @r.get("/v1/alerts/{alert_id}")
    async def get_alert(request: Request, principal: Principal, alert_id: UuidPath) -> dict[str, Any]:
        hub = _hub(request)
        async with tx(hub.pool, principal.tenant) as conn:
            detail = await queries.alert_detail(conn, principal.tenant, alert_id)
        if detail is None:
            raise HTTPException(404, "alert not found")
        return detail.model_dump(mode="json")

    @r.post("/v1/alerts/{alert_id}/status")
    async def set_status(request: Request, principal: Principal, alert_id: UuidPath, body: AlertStatusUpdate) -> dict[str, Any]:
        hub = _hub(request)
        try:
            async with tx(hub.pool, principal.tenant) as conn:
                alert = await queries.set_alert_status(conn, principal.tenant, alert_id, body, hub.clock())
        except queries.AlertConflict as exc:
            raise HTTPException(409, str(exc)) from None
        if alert is None:
            raise HTTPException(404, "alert not found")
        return alert.model_dump(mode="json")

    # ─────────────── events / signals ───────────────
    @r.get("/v1/events/{event_id}")
    async def get_event(request: Request, principal: Principal, event_id: UuidPath) -> dict[str, Any]:
        hub = _hub(request)
        async with tx(hub.pool, principal.tenant) as conn:
            bundle = await queries.event_bundle(conn, principal.tenant, event_id)
        if bundle is None:
            raise HTTPException(404, "event not found")
        return bundle

    @r.get("/v1/signals")
    async def search(
        request: Request, principal: Principal,
        query: Annotated[str | None, Query(max_length=200)] = None,
        kind: str | None = None, since: dt.datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        hub = _hub(request)
        if since is not None and since.tzinfo is None:
            raise HTTPException(422, "since needs a timezone offset")
        kinds = _csv(kind, tuple(SignalKind.__args__), "kind")  # type: ignore[attr-defined]
        async with tx(hub.pool, principal.tenant) as conn:
            signals = await queries.search_signals(conn, query=query, kinds=kinds, since=since, near=None, limit=limit)
        return {"items": [s.model_dump(mode="json") for s in signals], "content_trust": "untrusted_text"}

    @r.get("/v1/sources")
    async def sources(request: Request, principal: Principal) -> dict[str, Any]:
        hub = _hub(request)
        async with tx(hub.pool, None) as conn:
            rows = await conn.fetch(
                """SELECT connector_id, last_polled_at, last_status, last_error, consecutive_failures, next_poll_at,
                          items_total FROM source_cursor ORDER BY connector_id""")
            pending = await conn.fetchval("SELECT count(*) FROM news_item WHERE status = 'pending_extraction'")
        # the caller's own free-text items (connector events, its demo packs) by status: counts only
        async with tx(hub.pool, principal.tenant) as conn:
            mine = await conn.fetch("SELECT status, count(*) AS n FROM news_item WHERE tenant = $1 GROUP BY status",
                                    principal.tenant)
        # poll state + catalogue facts (name, licence, the shared effective cadence) per source,
        # including configured sources not polled yet (the engine's Signals admin lists these)
        catalogue = {s.connector_id: s for s in admin.source_catalogue(hub.settings)}
        schedule = await admin.load_schedule(hub.pool, hub.settings)
        by_id: dict[str, dict[str, Any]] = {
            str(row["connector_id"]): {k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in dict(row).items()}
            for row in rows}
        for cid, info in catalogue.items():
            item = by_id.setdefault(cid, {"connector_id": cid, "last_polled_at": None, "last_status": None,
                                          "last_error": None, "consecutive_failures": 0, "next_poll_at": None,
                                          "items_total": 0})
            item.update(name=info.name, license=info.license, authority_bp=info.authority_bp,
                        cadence_s=schedule[cid].cadence_s, paused=schedule[cid].paused, polled=info.polled)
        return {"sources": [by_id[k] for k in sorted(by_id)],
                "pending_extraction": pending, "llm_configured": bool(hub.settings.litellm_key),
                "llm_key_source": hub.settings.llm_key_source,
                "tenant_news": {r["status"]: int(r["n"]) for r in mine}}

    # ─────────────── tenant network + rules ───────────────
    async def _put_network(request: Request, principal: Principal, tenant: str, body: TenantNetwork) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        now = hub.clock()
        version, changes = await hub.pipeline.put_network(tenant, body, now)
        return {"tenant": tenant, "network_version": version, "nodes": len(body.nodes), "lanes": len(body.lanes),
                "alerts": [{"alert_id": c.alert_id, "change": c.change, "action": c.action} for c in changes]}

    @r.put("/v1/tenants/{tenant}/network")
    async def put_network(request: Request, principal: Principal, tenant: TenantPath, body: TenantNetwork) -> dict[str, Any]:
        return await _put_network(request, principal, tenant, body)

    @r.post("/v1/tenants/{tenant}/network")
    async def post_network(request: Request, principal: Principal, tenant: TenantPath, body: TenantNetwork) -> dict[str, Any]:
        return await _put_network(request, principal, tenant, body)

    @r.get("/v1/tenants/{tenant}/network")
    async def get_network(request: Request, principal: Principal, tenant: TenantPath) -> dict[str, Any]:
        """The stored network's version and node codes by type plus lane codes (codes only: no
        names, no geometry) — lets the engine and ops confirm what the last push stored."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, None) as conn:
            reg = await conn.fetchrow(
                "SELECT network_version, network_updated_at FROM tenant_registry WHERE tenant = $1", tenant)
        async with tx(hub.pool, tenant) as conn:
            nodes = await conn.fetch("SELECT type, code FROM network_node WHERE tenant = $1 ORDER BY type, code", tenant)
            lanes = await conn.fetch("SELECT code FROM network_lane WHERE tenant = $1 ORDER BY code", tenant)
        by_type: dict[str, list[str]] = {}
        for n in nodes:
            by_type.setdefault(str(n["type"]), []).append(str(n["code"]))
        updated = reg["network_updated_at"] if reg is not None else None
        return {"tenant": tenant, "network_version": int(reg["network_version"]) if reg is not None else 0,
                "updated_at": updated.isoformat() if updated is not None else None,
                "nodes": by_type, "lanes": [str(x["code"]) for x in lanes]}

    # ─────────────── signals admin (110 §5): alert rules, sources, watch regions ───────────────
    def _admin_error(exc: admin.AdminError) -> HTTPException:
        detail: Any = {"message": exc.detail, "problems": exc.problems} if exc.problems else exc.detail
        return HTTPException(exc.status, detail)

    async def _refresh(hub: HubState, tenant: str) -> int:
        return len(await hub.pipeline.refresh_tenant(tenant, hub.clock()))

    @r.get("/v1/tenants/{tenant}/alert-rules")
    async def get_rules(request: Request, principal: Principal, tenant: TenantPath) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, tenant) as conn:
            custom, rules, meta = await admin.load_rules_for_admin(conn, tenant)
        return {"tenant": tenant, "custom": custom, "rules": [x.model_dump(mode="json") for x in rules], **meta,
                "defaults": [x.model_dump(mode="json") for x in DEFAULT_ALERT_RULES]}

    @r.put("/v1/tenants/{tenant}/alert-rules")
    async def put_rules(request: Request, principal: Principal, tenant: TenantPath, body: RulesBody,
                        strict: bool = False) -> dict[str, Any]:
        """Replace the whole set (empty = back to the defaults). The engine's sync path: duplicate ids
        and too many rules refuse it; the editor's semantic checks come back as ``warnings`` (all of
        them refuse it with ``?strict=true``) — see ``admin.save_rules``."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        try:
            async with tx(hub.pool, tenant) as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"rules:{tenant}")
                warnings = await admin.save_rules(conn, tenant, body.rules, principal.subject, strict=strict)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return {"tenant": tenant, "rules": len(body.rules), "warnings": warnings,
                "alerts_changed": await _refresh(hub, tenant)}

    @r.post("/v1/tenants/{tenant}/alert-rules/validate")
    async def validate_rules(principal: Principal, tenant: TenantPath, body: RulesBody) -> dict[str, Any]:
        """Check a rule set without saving it (the editor's live validation)."""
        check_tenant(principal, tenant)
        problems = admin.rule_problems(body.rules)
        return {"valid": not problems, "problems": problems}

    async def _edit_rules(request: Request, principal: Principal, tenant: str,
                          edit: Callable[[list[AlertRule]], list[AlertRule]], *,
                          checked: tuple[str, ...]) -> tuple[list[AlertRule], list[dict[str, str]], int]:
        """Read-modify-write one rule. The editor checks refuse only problems of the rule written
        (``checked``); problems of the other rules (e.g. an engine-pushed set) come back as warnings."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        try:
            async with tx(hub.pool, tenant) as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"rules:{tenant}")
                _custom, rules, _meta = await admin.load_rules_for_admin(conn, tenant)
                rules = edit(list(rules))
                warnings = await admin.save_rules(conn, tenant, rules, principal.subject, strict=False,
                                                  strict_rules=checked)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return rules, warnings, await _refresh(hub, tenant)

    @r.post("/v1/tenants/{tenant}/alert-rules", status_code=201)
    async def create_rule(request: Request, principal: Principal, tenant: TenantPath, body: AlertRule) -> dict[str, Any]:
        """Add one rule (a tenant still on the defaults starts from a copy of them)."""

        def edit(rules: list[AlertRule]) -> list[AlertRule]:
            if any(x.id == body.id for x in rules):
                raise admin.AdminError(409, f"alert rule {body.id!r} already exists")
            return [*rules, body]

        rules, warnings, changed = await _edit_rules(request, principal, tenant, edit, checked=(body.id,))
        return {"tenant": tenant, "rule": body.model_dump(mode="json"), "rules": len(rules), "warnings": warnings,
                "alerts_changed": changed}

    @r.get("/v1/tenants/{tenant}/alert-rules/{rule_id}")
    async def get_rule(request: Request, principal: Principal, tenant: TenantPath, rule_id: RulePath) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, tenant) as conn:
            _custom, rules, _meta = await admin.load_rules_for_admin(conn, tenant)
        found = next((x for x in rules if x.id == rule_id), None)
        if found is None:
            raise HTTPException(404, "alert rule not found")
        return found.model_dump(mode="json")

    @r.put("/v1/tenants/{tenant}/alert-rules/{rule_id}")
    async def put_rule(request: Request, principal: Principal, tenant: TenantPath, rule_id: RulePath,
                       body: AlertRule) -> dict[str, Any]:
        """Replace one rule in place (``body.id`` may rename it; 404 when ``rule_id`` does not exist)."""

        def edit(rules: list[AlertRule]) -> list[AlertRule]:
            idx = next((i for i, x in enumerate(rules) if x.id == rule_id), None)
            if idx is None:
                raise admin.AdminError(404, "alert rule not found")
            if body.id != rule_id and any(x.id == body.id for x in rules):
                raise admin.AdminError(409, f"alert rule {body.id!r} already exists")
            return [*rules[:idx], body, *rules[idx + 1:]]

        rules, warnings, changed = await _edit_rules(request, principal, tenant, edit, checked=(body.id,))
        return {"tenant": tenant, "rule": body.model_dump(mode="json"), "rules": len(rules), "warnings": warnings,
                "alerts_changed": changed}

    @r.delete("/v1/tenants/{tenant}/alert-rules/{rule_id}")
    async def delete_rule(request: Request, principal: Principal, tenant: TenantPath, rule_id: RulePath) -> dict[str, Any]:
        """Remove one rule; removing the last one returns the tenant to the defaults."""

        def edit(rules: list[AlertRule]) -> list[AlertRule]:
            if not any(x.id == rule_id for x in rules):
                raise admin.AdminError(404, "alert rule not found")
            return [x for x in rules if x.id != rule_id]

        rules, warnings, changed = await _edit_rules(request, principal, tenant, edit, checked=())
        return {"tenant": tenant, "deleted": rule_id, "rules": len(rules), "custom": bool(rules),
                "warnings": warnings, "alerts_changed": changed}

    @r.get("/v1/tenants/{tenant}/sources")
    async def list_sources(request: Request, principal: Principal, tenant: TenantPath) -> dict[str, Any]:
        """The source catalogue with this tenant's subscription, the shared poll schedule and poll state."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        return {"tenant": tenant, "items": await admin.list_sources(hub.pool, hub.settings, tenant)}

    @r.put("/v1/tenants/{tenant}/sources/{connector_id}")
    async def put_source(request: Request, principal: Principal, tenant: TenantPath, connector_id: ConnectorPath,
                         body: admin.SubscriptionUpdate) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        try:
            item = await admin.put_subscription(hub.pool, hub.settings, tenant, connector_id, body, principal.subject)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return {"tenant": tenant, "source": item, "alerts_changed": await _refresh(hub, tenant)}

    @r.put("/v1/tenants/{tenant}/subscriptions")
    async def put_subscriptions(request: Request, principal: Principal, tenant: TenantPath,
                                body: admin.SubscriptionSet) -> dict[str, Any]:
        """The whole subscription map (the engine's Signals-admin sync, ``{sources: [{connector_id, enabled}]}``)."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        try:
            out = await admin.put_subscription_set(hub.pool, hub.settings, tenant, body, principal.subject)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return {"tenant": tenant, **out, "alerts_changed": await _refresh(hub, tenant)}

    @r.get("/v1/tenants/{tenant}/watch-regions")
    async def list_regions(request: Request, principal: Principal, tenant: TenantPath) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, tenant) as conn:
            items = await admin.list_watch_regions(conn, tenant)
        return {"tenant": tenant, "items": items, "limits": {
            "max_regions": admin.MAX_WATCH_REGIONS, "max_positions": admin.MAX_REGION_POSITIONS,
            "max_area_km2": admin.MAX_REGION_AREA_KM2, "radius_km": [1, admin.MAX_RADIUS_KM]}}

    @r.put("/v1/tenants/{tenant}/watch-regions")
    async def put_regions(request: Request, principal: Principal, tenant: TenantPath,
                          body: admin.WatchRegionSet) -> dict[str, Any]:
        """Replace the whole region set (the engine's Signals-admin sync; ``admin.replace_watch_regions``)."""
        check_tenant(principal, tenant)
        hub = _hub(request)
        await hub.pipeline.ensure_tenant(tenant)
        try:
            async with tx(hub.pool, tenant) as conn:
                items, deleted = await admin.replace_watch_regions(conn, tenant, body.regions, principal.subject)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return {"tenant": tenant, "items": items, "deleted": deleted, "alerts_changed": await _refresh(hub, tenant)}

    async def _save_region(request: Request, principal: Principal, tenant: str, region_id: str,
                           body: admin.WatchRegionBody, *, create_only: bool) -> tuple[dict[str, Any], bool, int]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        await hub.pipeline.ensure_tenant(tenant)
        try:
            async with tx(hub.pool, tenant) as conn:
                region, created = await admin.upsert_watch_region(conn, tenant, region_id, body, principal.subject,
                                                                  create_only=create_only)
        except admin.AdminError as exc:
            raise _admin_error(exc) from None
        return region, created, await _refresh(hub, tenant)

    @r.post("/v1/tenants/{tenant}/watch-regions", status_code=201)
    async def create_region(request: Request, principal: Principal, tenant: TenantPath,
                            body: admin.WatchRegionIn) -> dict[str, Any]:
        fields = admin.WatchRegionBody.model_validate(body.model_dump(exclude={"id"}))
        region, _created, changed = await _save_region(request, principal, tenant, body.id, fields, create_only=True)
        return {"tenant": tenant, "region": region, "alerts_changed": changed}

    @r.get("/v1/tenants/{tenant}/watch-regions/{region_id}")
    async def get_region(request: Request, principal: Principal, tenant: TenantPath, region_id: RegionPath) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, tenant) as conn:
            region = await admin.get_watch_region(conn, tenant, region_id)
        if region is None:
            raise HTTPException(404, "watch region not found")
        return region

    @r.put("/v1/tenants/{tenant}/watch-regions/{region_id}")
    async def put_region(request: Request, response: Response, principal: Principal, tenant: TenantPath,
                         region_id: RegionPath, body: admin.WatchRegionBody) -> dict[str, Any]:
        """Create or replace one region (201 when created)."""
        region, created, changed = await _save_region(request, principal, tenant, region_id, body, create_only=False)
        if created:
            response.status_code = 201
        return {"tenant": tenant, "region": region, "created": created, "alerts_changed": changed}

    @r.delete("/v1/tenants/{tenant}/watch-regions/{region_id}")
    async def delete_region(request: Request, principal: Principal, tenant: TenantPath, region_id: RegionPath) -> dict[str, Any]:
        check_tenant(principal, tenant)
        hub = _hub(request)
        async with tx(hub.pool, tenant) as conn:
            deleted = await admin.delete_watch_region(conn, tenant, region_id)
        if not deleted:
            raise HTTPException(404, "watch region not found")
        return {"tenant": tenant, "deleted": region_id, "alerts_changed": await _refresh(hub, tenant)}

    # ─────────────── connector events ───────────────
    @r.post("/v1/ingest/connector-event", status_code=202)
    async def connector_event(request: Request, principal: Principal, body: ConnectorEvent) -> dict[str, Any]:
        hub = _hub(request)
        now = hub.clock()
        if body.type not in SUPPORTED_TYPES:
            return {"accepted": False, "reason": f"event type {body.type!r} is not used by the signal hub"}
        await hub.pipeline.ensure_tenant(principal.tenant)
        drafts, texts = parse_connector_event(body, SourceContext(retrieved_at=now, tenant=principal.tenant))
        report = await hub.pipeline.ingest(drafts, now=now)
        ids = await hub.pipeline.queue_news(texts, now=now)
        report.news_queued += len(ids)
        if ids:
            report.merge(await hub.pipeline.process_news(ids, tenant=principal.tenant, now=now))
        return {"accepted": True, **report.as_dict()}

    # ─────────────── demo packs ───────────────
    @r.get("/v1/demo/packs")
    async def demo_packs(request: Request, principal: Principal) -> dict[str, Any]:
        hub = _hub(request)
        _require_demo(hub, principal.tenant)
        return {"packs": list_packs(hub.settings.scenarios_dir)}

    async def _demo(request: Request, principal: Principal, op: Callable[[HubState], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        hub = _hub(request)
        _require_demo(hub, principal.tenant)
        try:
            return await op(hub)
        except PackError as exc:
            raise HTTPException(404 if "not found" in str(exc) else 409, str(exc)) from None

    @r.get("/v1/demo/packs/{pack}")
    async def demo_state(request: Request, principal: Principal, pack: PackPath) -> dict[str, Any]:
        return await _demo(request, principal, lambda hub: hub.replay.state(principal.tenant, pack, wall_now=hub.clock()))

    @r.post("/v1/demo/packs/{pack}/start")
    async def demo_start(request: Request, principal: Principal, pack: PackPath, body: StartBody | None = None) -> dict[str, Any]:
        speed = body.speed if body else 0.0
        return await _demo(request, principal,
                           lambda hub: hub.replay.start(principal.tenant, pack, wall_now=hub.clock(), speed=speed))

    @r.post("/v1/demo/packs/{pack}/advance")
    async def demo_advance(request: Request, principal: Principal, pack: PackPath, body: AdvanceBody | None = None) -> dict[str, Any]:
        b = body or AdvanceBody()
        return await _demo(request, principal, lambda hub: hub.replay.advance(
            principal.tenant, pack, wall_now=hub.clock(), seconds=b.seconds, to_step=b.to_step))

    @r.post("/v1/demo/packs/{pack}/reset")
    async def demo_reset(request: Request, principal: Principal, pack: PackPath) -> dict[str, Any]:
        return await _demo(request, principal, lambda hub: hub.replay.reset(principal.tenant, pack))

    # The engine's demo reset calls this first (all of the tenant's packs in one call).
    @r.post("/v1/demo/reset")
    async def demo_reset_all(request: Request, principal: Principal) -> dict[str, Any]:
        return await _demo(request, principal, lambda hub: hub.replay.reset_all(principal.tenant))

    # Fixture route for the real adapters during a replay (DEMO_MODE only; static repo data, no tenant data).
    @r.get("/demo-feeds/{pack}/{path:path}", include_in_schema=False)
    async def demo_feed(request: Request, pack: PackPath, path: str) -> Response:
        hub = _hub(request)
        if not hub.settings.demo_mode:
            raise HTTPException(404, "not found")
        try:
            target = fixture_path(hub.settings.scenarios_dir, pack, path)
        except PackError:
            raise HTTPException(404, "not found") from None
        return FileResponse(target, media_type=FIXTURE_TYPES[target.suffix.lower()])

    return r


async def _loop(name: str, interval_s: float, fn: Callable[[], Awaitable[Any]]) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background loop failed", extra={"loop": name})
        await asyncio.sleep(interval_s)


def build_state(settings: Settings, pool: asyncpg.Pool, *, llm: LlmClient | Literal["auto"] | None = "auto",
                clock: Clock = utc_now, internal_client: httpx.AsyncClient | None = None) -> HubState:
    gaz = load_gazetteer(settings.geo_dir)
    clients: list[httpx.AsyncClient] = []
    if internal_client is None:
        internal_client = build_internal_client(settings)
        clients.append(internal_client)
    if llm == "auto":
        llm = LiteLlmClient(internal_client, base_url=settings.litellm_base_url, api_key=settings.litellm_key,
                            extract_alias=settings.extract_alias, guard_alias=settings.guard_alias,
                            timeout_s=settings.llm_timeout_s)
    pipeline = Pipeline(pool, gaz, llm, extraction_max_attempts=settings.extraction_max_attempts)
    replay = Replay(settings.scenarios_dir, settings.demo_feeds_base_url, pipeline, gaz,
                    Fetcher(internal_client, min_interval_s=0, max_bytes=settings.http_max_bytes))
    state = HubState(settings, pool, gaz, pipeline, replay, clock, clients, llm)
    if settings.embed_provider == "litellm":
        state.embedder = LiteLlmEmbedder(internal_client, base_url=settings.litellm_base_url,
                                         key=lambda: state.settings.litellm_key, alias=settings.embed_alias,
                                         dims=settings.embed_dims, timeout_s=settings.llm_timeout_s)
    return state


async def refresh_llm_key(hub: HubState) -> bool:
    """Re-read the provisioned extraction key (a bootstrap re-run may have generated a new one).
    Never touches an environment key (``LITELLM_SIGNALS_KEY`` wins). True when the key changed."""
    if hub.settings.llm_key_source == "env" or not isinstance(hub.llm, LiteLlmClient):
        return False
    key = await load_stored_key(hub.settings, hub.pool)
    if key == hub.settings.litellm_key:
        return False
    hub.llm.set_key(key)
    hub.settings = dataclasses.replace(hub.settings, litellm_key=key, llm_key_source="db" if key else None)
    log.info("extraction key reloaded", extra={"configured": bool(key), "key_alias": hub.settings.llm_key_alias})
    return True


def create_app(settings: Settings | None = None, *, state: HubState | None = None, background: bool = True) -> FastAPI:
    settings = settings or (state.settings if state else load_settings())
    holder: dict[str, HubState] = {}
    if state is not None:
        holder["state"] = state

    mcp = build_mcp(settings, lambda: holder["state"])
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True, host_origin_protection=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_pool = None
        if "state" not in holder:
            owned_pool = await create_pool(settings.database_url)
            # LITELLM_SIGNALS_KEY wins; else the key the bootstrap provisioned (signal_hub.llm_key)
            holder["state"] = build_state(await resolve_llm_key(settings, owned_pool), owned_pool)
        hub = holder["state"]
        app.state.hub = hub
        tasks: list[asyncio.Task[None]] = []
        if background:
            tasks.append(asyncio.create_task(_loop(
                "extraction", 30.0, lambda: hub.pipeline.process_pending(now=hub.clock(), limit=settings.extraction_batch))))
            tasks.append(asyncio.create_task(_loop("sweep", 60.0, lambda: _sweep_all(hub))))
            if hub.settings.llm_key_source != "env":
                tasks.append(asyncio.create_task(_loop("llm-key", hub.settings.llm_key_refresh_s,
                                                       lambda: refresh_llm_key(hub))))
            if settings.demo_mode:
                tasks.append(asyncio.create_task(_loop("demo", settings.demo_tick_s, lambda: hub.replay.tick(wall_now=hub.clock()))))
            if hub.settings.cluster_maintenance_s > 0:
                tasks.append(asyncio.create_task(_loop("cluster", hub.settings.cluster_maintenance_s,
                                                       lambda: run_maintenance(hub))))
        async with mcp_app.router.lifespan_context(mcp_app):
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for client in hub.clients:
                    await client.aclose()
                if owned_pool is not None:
                    await owned_pool.close()

    app = FastAPI(title="Keelson Signal Hub", version="1.0.0", lifespan=lifespan, docs_url="/v1/docs",
                  openapi_url="/v1/openapi.json", redoc_url=None)
    app.state.ready = False
    if state is not None:
        app.state.hub = state

    @app.exception_handler(ValidationError)
    async def _validation(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": "invalid data", "errors": exc.error_count()}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        ok = bool(request.app.state.ready)
        try:
            async with tx(_hub(request).pool, None) as conn:
                await conn.fetchval("SELECT 1")
        except Exception:
            ok = False
        return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503)

    app.include_router(build_router())
    app.include_router(build_clustering_router())
    app.mount("/", mcp_app)
    app.add_middleware(BodyLimit)
    return app


async def _sweep_all(hub: HubState) -> None:
    now = hub.clock()
    await hub.pipeline.sweep(now)
    for tenant in await hub.pipeline.tenants():
        await hub.pipeline.sweep(now, tenant=tenant)


def run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.auth_configured:
        log.warning("SIGNAL_HUB_SHARED_SECRET is not set: every request will be rejected")
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None, proxy_headers=False,
                timeout_graceful_shutdown=5)


if __name__ == "__main__":
    run()
