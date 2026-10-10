"""M7 back-office API (``docs/contracts/110-m7-backoffice-generic.md`` §1, §2, §4–§6), under ``/api/admin``.

Same rules as :mod:`keelson_engine.api.routes_admin` (session + CSRF + Origin on mutations,
``tenant-admin`` / ``platform-admin``, tenant = active tenant or a platform-admin's audited
``?tenant=``). Guarded changes answer ``202`` + an ``AdminChangeRequest`` (the
``GuardedChange`` handler in :mod:`keelson_engine.api.errors`).

=========================================================  =======================================
``GET  /change-requests?status=&kind=&limit=``             pending changes queue (+ ``pending`` count)
``GET  /change-requests/{id}``
``POST /change-requests/{id}/approve|reject {reason}``     four-eyes decision (409 ``stale_base``)
``GET|POST /users``, ``PATCH|DELETE /users/{id}``          Keycloak users of the tenant (§2)
``PATCH /connectors/{slug} {trust_level}``                 first-party trust = guarded
``PUT  /connectors/{slug}/tools/{name}/binding``           ``auto`` on a write tool = guarded
``PUT  /connectors/{slug}/secret {secret}``                  rotate a static-header / client secret
``GET  /connectors/{slug}/capabilities``                   tiers → sync / commit mode / advisory
``GET|PUT /connectors/{slug}/mappings/{read|action}``      versioned tier-A mappings (§4)
``GET  /connectors/{slug}/mappings/{kind}/versions[/{v}]``
``POST /connectors/{slug}/mappings/read/test``             crawl 1 page per source, map, show rows
``POST /connectors/{slug}/mappings/action/dry-run``        render the call, run the read-back only
``POST /connectors/{slug}/mappings/from-pack {pack}``      install a pack's read / action mapping files
``GET|POST /connectors/{slug}/crawl``                      crawl status / queue a crawl sync now
``GET /signals/sources``, ``PUT|PATCH /signals/sources/{id}``  subscriptions + cadence (§5)
``GET|PUT /signals/watch-regions``, ``GET|PUT /signals/alert-rules``, ``GET /signals/network``
``GET  /audit/activity``                                   runs, approvals, change sets, admin, signals
``GET  /evals``                                            latest ``evals/results/*.json`` (§6)
=========================================================  =======================================
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from keelson_contracts.admin import AdminUserCreate, AdminUserPatch, ChangeRequestDecision
from keelson_contracts.mapping import ActionDryRunRequest, ReadMapping
from keelson_engine.api.admin_deps import AdminCtx, AdminRead, AdminWrite, require_platform
from keelson_engine.api.errors import ApiError
from keelson_engine.core import audit, change_requests, connector_admin, connector_mappings, signals_admin, users
from keelson_engine.core.connector_admin import AdminError

__all__ = ["router"]

router = APIRouter(prefix="/api/admin")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _auth(ctx: AdminCtx) -> tuple[dict[str, Any], list[str], list[str]]:
    actor = {**ctx.actor(), "tenants": list(ctx.sess.tenants)}
    return actor, list(ctx.sess.roles), list(ctx.sess.tenants)


# ───────────────────────────── four-eyes change requests (§1) ─────────────────────────────


@router.get("/change-requests")
async def change_requests_list(
    ctx: AdminRead,
    status: Literal["pending", "approved", "rejected", "applied", "expired"] | None = None,
    kind: str | None = Query(None, max_length=40),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    items = await change_requests.list_requests(ctx.tenant, status=status, kind=kind, limit=limit)
    return {"items": items, "pending": await change_requests.pending_count(ctx.tenant)}


@router.get("/change-requests/{request_id}")
async def change_requests_get(request_id: str, ctx: AdminRead) -> dict[str, Any]:
    return await change_requests.get_request(ctx.tenant, request_id)


@router.post("/change-requests/{request_id}/approve")
async def change_requests_approve(request_id: str, ctx: AdminWrite,
                                  body: ChangeRequestDecision | None = None) -> dict[str, Any]:
    actor, roles, tenants = _auth(ctx)
    return await change_requests.approve(ctx.tenant, request_id, actor=actor, actor_roles=roles,
                                         actor_tenants=tenants, reason=body.reason if body else None)


@router.post("/change-requests/{request_id}/reject")
async def change_requests_reject(request_id: str, ctx: AdminWrite,
                                 body: ChangeRequestDecision | None = None) -> dict[str, Any]:
    actor, roles, tenants = _auth(ctx)
    return await change_requests.reject(ctx.tenant, request_id, actor=actor, actor_roles=roles,
                                        actor_tenants=tenants, reason=body.reason if body else None)


# ───────────────────────────── users (§2) ─────────────────────────────


@router.get("/users")
async def users_list(ctx: AdminRead) -> dict[str, Any]:
    return {"items": await users.list_users(ctx.tenant)}


@router.post("/users")
async def users_create(body: AdminUserCreate, ctx: AdminWrite) -> JSONResponse:
    actor, _roles, _tenants = _auth(ctx)
    status, out = await users.create_user(ctx.tenant, body, actor)
    return JSONResponse(out, status_code=status)


@router.patch("/users/{user_id}")
async def users_patch(user_id: str, body: AdminUserPatch, ctx: AdminWrite) -> JSONResponse:
    actor, _roles, _tenants = _auth(ctx)
    status, out = await users.patch_user(ctx.tenant, user_id, body, actor)
    return JSONResponse(out, status_code=status)


@router.delete("/users/{user_id}")
async def users_disable(user_id: str, ctx: AdminWrite) -> dict[str, Any]:
    """Disables the account (never deletes it: audit history keeps its name)."""
    actor, _roles, _tenants = _auth(ctx)
    return await users.disable_user(ctx.tenant, user_id, actor)


# ───────────────────────────── connectors: trust, binding, capabilities ─────────────────────────────


class TrustBody(_Body):
    trust_level: Literal["first_party", "third_party"]


class BindingBody(_Body):
    binding: Literal["auto", "approval_gate", "deny"]


@router.patch("/connectors/{slug}")
async def connectors_patch(slug: str, body: TrustBody, ctx: AdminWrite) -> dict[str, Any]:
    return await connector_admin.set_trust(ctx.tenant, slug, body.trust_level, ctx.actor(), platform_admin=ctx.platform)


@router.put("/connectors/{slug}/tools/{name}/binding")
async def connectors_tool_binding(slug: str, name: str, body: BindingBody, ctx: AdminWrite) -> dict[str, Any]:
    return await connector_admin.set_binding(ctx.tenant, slug, name, body.binding, ctx.actor(),
                                             platform_admin=ctx.platform)


class SecretBody(_Body):
    secret: str = Field(min_length=1, max_length=4096)


@router.put("/connectors/{slug}/secret")
async def connectors_secret(slug: str, body: SecretBody, ctx: AdminWrite) -> dict[str, Any]:
    """Replace the stored credential (static header value / client secret), e.g. a rotated vendor API key."""
    return await connector_admin.rotate_secret(ctx.tenant, slug, body.secret, ctx.actor())


@router.get("/connectors/{slug}/capabilities")
async def connectors_capabilities(slug: str, ctx: AdminRead) -> dict[str, Any]:
    return await connector_mappings.capabilities(ctx.tenant, slug)


# ───────────────────────────── tier-A mappings (§4) ─────────────────────────────


class MappingPutBody(_Body):
    base_version: int | None = Field(default=None, ge=1)
    """The version the edit is based on (``null`` = there is none yet); 409 otherwise."""
    mapping: dict[str, Any]
    proposal_id: uuid.UUID | None = None
    """M8: the proposal the mapping was opened from (provenance; an action mapping then needs a
    second admin: 202 + change request ``connector_mapping``)."""


class ReadTestBody(_Body):
    mapping: dict[str, Any] | None = None
    """A draft read mapping to test (default: the stored one)."""
    max_pages: int = Field(default=1, ge=1, le=3)


async def _connector_or_404(ctx: AdminCtx, slug: str) -> Any:
    from keelson_engine.core import connectors as reg

    try:
        return await reg.get_connector(ctx.tenant, slug)
    except reg.ConnectorNotFound:
        raise ApiError(404, "not_found", "connector not found") from None


@router.get("/connectors/{slug}/mappings/{kind}")
async def mappings_get(slug: str, kind: Literal["read", "action"], ctx: AdminRead) -> dict[str, Any]:
    await _connector_or_404(ctx, slug)
    row = await connector_mappings.get_mapping(ctx.tenant, slug, kind)
    if row is None:
        return {"connector": slug, "kind": kind, "version": None, "mapping": None, "problems": []}
    problems: list[str] = []
    try:
        model: Any = ReadMapping.model_validate(row["mapping"]) if kind == "read" else None
        if kind == "action":
            from keelson_contracts.mapping import ActionMapping

            model = ActionMapping.model_validate(row["mapping"])
        problems = connector_mappings.violations(model, await connector_mappings.approved_classifications(ctx.tenant, slug))
    except ValidationError:
        problems = ["the stored mapping no longer validates; save a corrected version"]
    return {**row, "problems": problems}


@router.get("/connectors/{slug}/mappings/{kind}/versions")
async def mappings_versions(slug: str, kind: Literal["read", "action"], ctx: AdminRead) -> dict[str, Any]:
    await _connector_or_404(ctx, slug)
    return {"items": await connector_mappings.list_versions(ctx.tenant, slug, kind)}


@router.get("/connectors/{slug}/mappings/{kind}/versions/{version}")
async def mappings_version(slug: str, kind: Literal["read", "action"], version: int, ctx: AdminRead) -> dict[str, Any]:
    await _connector_or_404(ctx, slug)
    row = await connector_mappings.get_mapping(ctx.tenant, slug, kind, version)
    if row is None:
        raise ApiError(404, "not_found", "mapping version not found")
    return row


@router.put("/connectors/{slug}/mappings/{kind}")
async def mappings_put(slug: str, kind: Literal["read", "action"], body: MappingPutBody, ctx: AdminWrite) -> JSONResponse:
    row, created = await connector_mappings.put_mapping(
        ctx.tenant, slug, kind, body.mapping, base_version=body.base_version, actor=ctx.actor(),
        proposal_id=str(body.proposal_id) if body.proposal_id else None, platform_admin=ctx.platform)
    return JSONResponse({**row, "unchanged": not created}, status_code=201 if created else 200)


@router.post("/connectors/{slug}/mappings/read/test")
async def mappings_read_test(slug: str, ctx: AdminWrite, body: ReadTestBody | None = None) -> dict[str, Any]:
    """Crawl at most ``max_pages`` page(s) per source with the reader credentials and map the items
    (nothing stored): per entity mapping the row count, 5 sample rows and the first error."""
    from keelson_engine.core import scn_crawl

    conn = await _connector_or_404(ctx, slug)
    b = body or ReadTestBody()
    if b.mapping is not None:
        try:
            mapping = ReadMapping.model_validate({"kind": "read", **b.mapping})
        except ValidationError as exc:
            raise ApiError(422, "mapping_invalid", "the read mapping is invalid",
                           errors=[{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg"))[:300]}
                                   for e in exc.errors()[:30]]) from None
    else:
        stored = await connector_mappings.get_read_mapping(ctx.tenant, slug)
        if stored is None:
            raise ApiError(404, "not_found", "this connector has no read mapping")
        mapping = stored
    problems = connector_mappings.violations(mapping, await connector_mappings.approved_classifications(ctx.tenant, slug))
    if problems:
        raise ApiError(422, "mapping_invalid", problems[0], problems=problems[:30])
    if not conn.usable:
        raise ApiError(409, "connector_inactive", f"the connector is {conn.status}: activate it first")
    return await scn_crawl.preview(conn, mapping, max_pages=b.max_pages)


@router.post("/connectors/{slug}/mappings/action/dry-run")
async def mappings_action_dry_run(slug: str, body: ActionDryRunRequest, ctx: AdminWrite) -> dict[str, Any]:
    """Renders the tool call for a sample action — never executed — and runs the read-back query."""
    return await connector_mappings.dry_run(ctx.tenant, slug, body, ctx.actor())


class FromPackBody(_Body):
    pack: str = Field(min_length=1, max_length=63)
    kinds: list[Literal["read", "action"]] = Field(default_factory=lambda: ["read", "action"], min_length=1)


@router.post("/connectors/{slug}/mappings/from-pack")
async def mappings_from_pack(slug: str, body: FromPackBody, ctx: AdminWrite) -> dict[str, Any]:
    """Install a template pack's mapping files (``pack.yaml`` ``mappings``) on this connector as new
    versions (each validated exactly like ``PUT …/mappings/{kind}``; an unchanged one stays)."""
    from keelson_engine.core.packs import PackError, load_pack, load_pack_mappings

    await _connector_or_404(ctx, slug)
    try:
        docs = load_pack_mappings(load_pack(body.pack))
    except PackError as exc:
        raise ApiError(422, "unknown_pack", str(exc)[:300]) from None
    kinds = [k for k in dict.fromkeys(body.kinds) if k in docs]
    if not kinds:
        raise ApiError(422, "pack_has_no_mappings", f"pack {body.pack!r} has no {' / '.join(body.kinds)} mapping")
    out: dict[str, Any] = {}
    for kind in kinds:
        current = await connector_mappings.get_mapping(ctx.tenant, slug, kind)
        row, created = await connector_mappings.put_mapping(
            ctx.tenant, slug, kind, {k: v for k, v in docs[kind].items() if k != "kind"},
            base_version=current["version"] if current else None, actor=ctx.actor(), origin="pack")
        out[kind] = {"version": row["version"], "summary": row["summary"], "unchanged": not created}
    return {"connector": slug, "pack": body.pack, "mappings": out}


@router.get("/connectors/{slug}/crawl")
async def connectors_crawl_status(slug: str, ctx: AdminRead) -> dict[str, Any]:
    """Tier-A crawl status: the latest attempt (``queued`` while a sync is due, ``running`` while
    loading, ``succeeded`` / ``partial`` (truncated) / ``failed``), the current snapshot's label and
    the read mapping version. 404 ``no_read_mapping`` when the connector is not crawled."""
    from keelson_engine.core import scn_sync

    await _connector_or_404(ctx, slug)
    mapping = await connector_mappings.get_mapping(ctx.tenant, slug, "read")
    if mapping is None:
        raise ApiError(404, "no_read_mapping", "this connector has no read mapping (it is not crawled)")
    latest = next(iter(await scn_sync.list_snapshots(ctx.tenant, slug, 1)), None)
    current = await scn_sync.current_snapshot(ctx.tenant, slug)
    state = await scn_sync.sync_state(ctx.tenant, slug) or {}
    status = "idle"
    if latest is not None:
        status = {"loading": "running", "failed": "failed"}.get(latest.status, "succeeded")
        if status == "succeeded" and (latest.crawl or {}).get("truncated"):
            status = "partial"
    if state.get("due_at") and status in ("idle", "succeeded", "partial"):
        status = "queued"  # a requested / due sync the worker has not started yet (failures keep "failed")
    shown = latest if latest is not None else current
    label = current.label if current is not None else None
    return {
        "connector": slug, "status": status,
        "consistency": label["consistency"] if label else None,
        "as_of_range": label["as_of_range"] if label else None,
        "started_at": shown.created_at.isoformat() if shown and shown.created_at else None,
        "finished_at": shown.ready_at.isoformat() if shown and shown.ready_at else None,
        "snapshot_id": current.id if current else None,
        "read_mapping_version": mapping["version"],
        "error": latest.error if latest is not None and latest.status == "failed" else None,
        "entities": {k: v for k, v in (shown.row_counts if shown else {}).items() if v},
        "crawl": shown.crawl if shown else None,
        "state": state or None,
    }


@router.post("/connectors/{slug}/crawl", status_code=202)
async def connectors_crawl(slug: str, ctx: AdminWrite) -> dict[str, Any]:
    """Queue a crawl sync now (same as ``POST …/sync`` for a connector with a read mapping)."""
    from keelson_engine.core import scn_sync
    from keelson_engine.core.repo import admin_audit

    await _connector_or_404(ctx, slug)
    if await connector_mappings.get_mapping(ctx.tenant, slug, "read") is None:
        raise ApiError(409, "no_read_mapping", "save a read mapping first")
    try:
        state = await scn_sync.request_sync(ctx.tenant, slug, reason=f"admin:{ctx.sess.username or 'admin'}")
    except scn_sync.SyncUnavailable as exc:
        raise ApiError(404 if exc.code == "not_found" else 409, exc.code, exc.message) from None
    await admin_audit(ctx.actor(), "connector.crawl", f"connector:{slug}", None, {"state": "queued"},
                      tenant=ctx.tenant)
    return {"connector": slug, "status": "queued", "state": state}


# ───────────────────────────── signals admin (§5) ─────────────────────────────


class RegionsBody(_Body):
    regions: list[dict[str, Any]] = Field(max_length=signals_admin.MAX_WATCH_REGIONS)


class RulesBody(_Body):
    rules: list[dict[str, Any]] = Field(max_length=50)


def _hub_or_503() -> None:
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured():
        raise ApiError(503, "signal_hub_not_configured", "the Signal Hub is not configured")


@router.get("/signals/sources")
async def signals_sources(ctx: AdminRead) -> dict[str, Any]:
    _hub_or_503()
    return await signals_admin.get_sources(ctx.tenant)


@router.put("/signals/sources/{source_id}")
@router.patch("/signals/sources/{source_id}")
async def signals_source_put(source_id: str, body: signals_admin.SourceSetting, ctx: AdminWrite) -> dict[str, Any]:
    """``{enabled?, cadence_s?}`` (``cadence_s: null`` = the source's default cadence)."""
    _hub_or_503()
    return await signals_admin.put_source(ctx.tenant, source_id, body, ctx.actor())


@router.get("/signals/network")
async def signals_network(ctx: AdminRead) -> dict[str, Any]:
    """Node codes of the tenant network pushed to the hub, ``{nodes: {type: [codes]}}`` (radius
    watch regions are drawn around one of them)."""
    return await signals_admin.get_network_codes(ctx.tenant)


@router.get("/signals/watch-regions")
async def signals_regions(ctx: AdminRead) -> dict[str, Any]:
    return await signals_admin.get_watch_regions(ctx.tenant)


@router.put("/signals/watch-regions")
async def signals_regions_put(body: RegionsBody, ctx: AdminWrite) -> dict[str, Any]:
    return await signals_admin.put_watch_regions(ctx.tenant, body.regions, ctx.actor())


@router.get("/signals/alert-rules")
async def signals_rules(ctx: AdminRead) -> dict[str, Any]:
    return await signals_admin.get_alert_rules(ctx.tenant)


@router.put("/signals/alert-rules")
async def signals_rules_put(body: RulesBody, ctx: AdminWrite) -> dict[str, Any]:
    return await signals_admin.put_alert_rules(ctx.tenant, body.rules, ctx.actor())


# ───────────────────────────── audit & evals (§6) ─────────────────────────────


@router.get("/audit/activity")
async def audit_activity(
    ctx: AdminRead,
    category: list[Literal["runs", "approvals", "change_sets", "admin", "signals"]] | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    before: str | None = Query(None, max_length=300),
    actor: str | None = Query(None, max_length=200),
    case_id: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=100),
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> dict[str, Any]:
    for value in (since, until):
        if value is not None and value.tzinfo is None:
            raise ApiError(422, "invalid_request", "since / until need a timezone offset")
    try:
        return await audit.list_activity(ctx.tenant, categories=list(category) if category else None, limit=limit,
                                         before=before, actor=actor, case_id=case_id, action=action, since=since,
                                         until=until)
    except ValueError:
        raise ApiError(422, "invalid_request", "invalid cursor") from None


@router.get("/evals")
async def evals(ctx: AdminRead, limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
    """Platform-admin: the latest eval runs (results are platform-wide, not tenant data)."""
    require_platform(ctx)
    try:
        return audit.list_evals(limit)
    except OSError as exc:
        raise AdminError(503, "evals_unavailable", "the eval results could not be read") from exc
