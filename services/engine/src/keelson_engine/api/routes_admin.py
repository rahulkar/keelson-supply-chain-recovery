"""Back-office API ``/api/admin/*`` (``docs/contracts/70-backoffice-m2.md`` §1–4 + audit).

Session + CSRF + Origin like every mutating route; role ``tenant-admin`` (scoped to the active
tenant) unless marked platform-admin; see :mod:`keelson_engine.api.admin_deps`. Every mutation
writes ``admin_audit``. Errors: ``{code, message, ...}``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from keelson_engine.api.admin_deps import AdminRead, AdminWrite, require_platform
from keelson_engine.api.errors import ApiError
from keelson_engine.core import agent_specs, connector_admin, gateway, policies, tenants
from keelson_engine.core.audit import list_audit
from keelson_engine.core.packs import PackError, list_packs

__all__ = ["router"]

router = APIRouter(prefix="/api/admin")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ───────────────────────────── agents ─────────────────────────────


class SpecPutBody(_Body):
    base_version: str = Field(min_length=1, max_length=40)
    spec_patch: dict[str, Any]


class RestoreBody(_Body):
    version: str = Field(min_length=1, max_length=40)


@router.get("/agents/spec")
async def get_spec(ctx: AdminRead) -> dict[str, Any]:
    return await agent_specs.latest(ctx.tenant)


@router.get("/agents/spec/versions")
async def spec_versions(ctx: AdminRead) -> list[dict[str, Any]]:
    return await agent_specs.list_versions(ctx.tenant)


@router.get("/agents/spec/versions/{version}")
async def spec_version(version: str, ctx: AdminRead) -> dict[str, Any]:
    return await agent_specs.get_version(ctx.tenant, version)


@router.put("/agents/spec")
async def put_spec(body: SpecPutBody, ctx: AdminWrite) -> JSONResponse:
    result, created = await agent_specs.put_spec(ctx.tenant, body.base_version, body.spec_patch, ctx.actor(),
                                                 platform_admin=ctx.platform)
    return JSONResponse({**result, "unchanged": not created}, status_code=201 if created else 200)


@router.post("/agents/spec/restore", status_code=201)
async def restore_spec(body: RestoreBody, ctx: AdminWrite) -> dict[str, Any]:
    return await agent_specs.restore(ctx.tenant, body.version, ctx.actor(), platform_admin=ctx.platform)


@router.get("/agents/catalog")
async def agents_catalog(ctx: AdminRead) -> dict[str, Any]:
    return await agent_specs.catalog(ctx.tenant)


# ───────────────────────────── policies (M5, 95 §1) ─────────────────────────────


class PoliciesPutBody(_Body):
    base_version: str = Field(min_length=1, max_length=40)
    """The agent spec version the edit is based on (optimistic concurrency, 409 otherwise)."""
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    version: str | None = Field(default=None, min_length=1, max_length=64)
    """New policy version; omitted = bumped automatically when the rules change."""


class PolicyTestBody(_Body):
    context: dict[str, Any]
    rules: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=50)
    """Draft rules to test (default: the active rule set)."""
    version: str | None = Field(default=None, min_length=1, max_length=64)


@router.get("/policies")
async def get_policies(ctx: AdminRead) -> dict[str, Any]:
    return await policies.get_policies(ctx.tenant)


@router.put("/policies")
async def put_policies(body: PoliciesPutBody, ctx: AdminWrite) -> JSONResponse:
    result, created = await policies.put_policies(ctx.tenant, body.base_version, body.rules, ctx.actor(),
                                                  platform_admin=ctx.platform, version=body.version)
    return JSONResponse({**result, "unchanged": not created}, status_code=201 if created else 200)


@router.post("/policies/test")
async def test_policies(body: PolicyTestBody, ctx: AdminWrite) -> dict[str, Any]:
    return await policies.test_policy(ctx.tenant, body.context, rules=body.rules, version=body.version)


# ───────────────────────────── AI gateway ─────────────────────────────


class ModelBody(_Body):
    provider_model: str = Field(min_length=1, max_length=200)
    mode: str = "chat"
    params: dict[str, Any] = Field(default_factory=dict)
    grant_all_tenants: bool = False


class TenantPatchBody(_Body):
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    budget_eur: float | None = None
    budget_duration: str | None = None


@router.get("/gateway/models")
async def gateway_models(ctx: AdminRead) -> dict[str, Any]:
    items = await gateway.list_models(ctx.tenant, platform_view=ctx.platform)
    return {"items": items, "view": "platform" if ctx.platform else "tenant"}


@router.put("/gateway/models/{alias:path}")
async def gateway_model_upsert(alias: str, body: ModelBody, ctx: AdminWrite) -> dict[str, Any]:
    require_platform(ctx)
    return await gateway.upsert_model(alias, body.model_dump(), ctx.actor())


@router.delete("/gateway/models/{alias:path}")
async def gateway_model_delete(alias: str, ctx: AdminWrite) -> dict[str, Any]:
    require_platform(ctx)
    return await gateway.delete_model(alias, ctx.actor())


@router.get("/gateway/tenant")
async def gateway_tenant(ctx: AdminRead) -> dict[str, Any]:
    return await gateway.tenant_view(ctx.tenant)


@router.patch("/gateway/tenant")
async def gateway_tenant_patch(body: TenantPatchBody, ctx: AdminWrite) -> dict[str, Any]:
    return await gateway.patch_tenant(ctx.tenant, body.model_dump(exclude_none=True), ctx.actor(),
                                      platform_admin=ctx.platform)


@router.post("/gateway/tenant/rotate-key")
async def gateway_rotate_key(ctx: AdminWrite) -> dict[str, Any]:
    return await gateway.rotate_key(ctx.tenant, ctx.actor())


@router.get("/gateway/usage")
async def gateway_usage(ctx: AdminRead, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    return await gateway.usage(ctx.tenant, days)


# ───────────────────────────── connectors ─────────────────────────────


class AuthBody(_Body):
    kind: Literal["none", "static_header", "oauth_client_credentials", "oauth_platform_merxa"] = "none"
    header_name: str | None = Field(default=None, max_length=64)
    secret: str | None = Field(default=None, max_length=4096)
    token_url: str | None = Field(default=None, max_length=2048)
    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, max_length=4096)
    client_auth: Literal["client_secret", "private_key_jwt"] | None = None
    scopes: list[str] | str | None = None
    resource: str | None = Field(default=None, max_length=2048)
    audience: str | None = Field(default=None, max_length=2048)


class ConnectorBody(_Body):
    slug: str = Field(min_length=1, max_length=63)
    name: str = Field(default="", max_length=200)
    mcp_url: str = Field(min_length=1, max_length=2048)
    trust_level: Literal["first_party", "third_party"] = "third_party"
    auth: AuthBody = Field(default_factory=AuthBody)
    webhook_secret_ref: str | None = Field(default=None, max_length=63)


class ToolPatchBody(_Body):
    access: Literal["read", "write", "destructive"] | None = None
    content_trust: Literal["trusted_data", "untrusted_text"] | None = None
    egress: Literal["internal", "external"] | None = None
    idempotency: Literal["enforced", "best_effort", "none"] | None = None
    status: Literal["approved", "needs_review", "disabled"] | None = None
    binding: Literal["auto", "approval_gate", "deny"] | None = None
    """M7 agent tool binding (on its own; ``auto`` on a write tool is a guarded change)."""


@router.get("/connectors")
async def connectors_list(ctx: AdminRead) -> dict[str, Any]:
    return {"items": await connector_admin.list_admin(ctx.tenant)}


@router.post("/connectors", status_code=201)
async def connectors_create(body: ConnectorBody, ctx: AdminWrite) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    data["auth"] = body.auth.model_dump(exclude_none=True)
    return await connector_admin.create_connector(ctx.tenant, data, ctx.actor(), platform_admin=ctx.platform)


@router.get("/connectors/{slug}")
async def connectors_detail(slug: str, ctx: AdminRead) -> dict[str, Any]:
    return await connector_admin.detail_admin(ctx.tenant, slug)


@router.post("/connectors/{slug}/test")
async def connectors_test(slug: str, ctx: AdminWrite) -> dict[str, Any]:
    return await connector_admin.test_connection(ctx.tenant, slug, ctx.actor())


@router.get("/connectors/{slug}/tools")
async def connectors_tools(slug: str, ctx: AdminRead) -> dict[str, Any]:
    from keelson_engine.core import connectors as reg

    await connector_admin.detail_admin(ctx.tenant, slug)  # 404 for unknown/foreign connectors
    return {"items": [t.admin_json() for t in await reg.list_tools(ctx.tenant, slug)]}


@router.patch("/connectors/{slug}/tools/{name}")
async def connectors_tool_patch(slug: str, name: str, body: ToolPatchBody, ctx: AdminWrite) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise ApiError(422, "empty_patch", "nothing to change")
    return await connector_admin.update_tool(ctx.tenant, slug, name, patch, ctx.actor(), platform_admin=ctx.platform)


@router.post("/connectors/{slug}/activate")
async def connectors_activate(slug: str, ctx: AdminWrite) -> dict[str, Any]:
    """Activating a connector with approved external-egress tools is guarded (M7: ``202`` + a
    change request for tenant-admins)."""
    return await connector_admin.set_status(ctx.tenant, slug, "activate", ctx.actor(), platform_admin=ctx.platform)


@router.post("/connectors/{slug}/disable")
async def connectors_disable(slug: str, ctx: AdminWrite) -> dict[str, Any]:
    return await connector_admin.set_status(ctx.tenant, slug, "disable", ctx.actor(), platform_admin=ctx.platform)


@router.delete("/connectors/{slug}", status_code=204)
async def connectors_delete(slug: str, ctx: AdminWrite) -> Response:
    """204 for platform-admins; M7: ``202`` + a change request for tenant-admins (four-eyes)."""
    await connector_admin.delete_connector(ctx.tenant, slug, ctx.actor(), platform_admin=ctx.platform)
    return Response(status_code=204)


@router.get("/connectors/{slug}/events")
async def connectors_events(slug: str, ctx: AdminRead, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"items": await connector_admin.list_events(ctx.tenant, slug, limit)}


# ───────────────────────────── canonical snapshot sync (M3, contract 80 §1) ─────────────────────────────


@router.post("/connectors/{slug}/sync", status_code=202)
async def connectors_sync(slug: str, ctx: AdminWrite) -> dict[str, Any]:
    """Queue a canonical snapshot sync **now** (the worker runs it via ``scn_sync.due_syncs``);
    202 ``{status: "queued", state, current}``. 404 unknown connector; 409 ``connector_inactive``
    / ``sync_unsupported`` (no approved ``scn.snapshot_open`` + ``scn.snapshot_export``)."""
    from keelson_engine.core import scn_sync
    from keelson_engine.core.repo import admin_audit

    try:
        state = await scn_sync.request_sync(ctx.tenant, slug, reason=f"admin:{ctx.sess.username or 'admin'}")
    except scn_sync.SyncUnavailable as exc:
        if exc.code == "not_found":
            raise ApiError(404, "not_found", "connector not found") from None
        raise ApiError(409, exc.code, exc.message) from None
    current = await scn_sync.current_snapshot(ctx.tenant, slug)
    await admin_audit(ctx.actor(), "connector.sync", f"connector:{slug}", None,
                      {"current_snapshot_id": current.id if current else None}, tenant=ctx.tenant)
    return {"status": "queued", "connector": slug, "state": state, "current": current.to_json() if current else None}


@router.get("/connectors/{slug}/snapshots")
async def connectors_snapshots(slug: str, ctx: AdminRead, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Sync history (newest first: status, row counts, bytes, duration, error, pins), the current
    pointer and the trigger state (stale/due/back-off)."""
    from keelson_engine.core import connectors as reg
    from keelson_engine.core import scn_sync

    try:
        await reg.get_connector(ctx.tenant, slug)
    except reg.ConnectorNotFound:
        raise ApiError(404, "not_found", "connector not found") from None
    current = await scn_sync.current_snapshot(ctx.tenant, slug)
    return {
        "connector": slug,
        "current": current.to_json() if current else None,
        "state": await scn_sync.sync_state(ctx.tenant, slug),
        "items": [r.to_json() for r in await scn_sync.list_snapshots(ctx.tenant, slug, limit)],
    }


# ───────────────────────────── tenants & packs (platform-admin) ─────────────────────────────


class TenantBody(_Body):
    slug: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=200)
    pack: str = Field(min_length=1, max_length=63)
    admin_usernames: list[str] = Field(default_factory=list, max_length=20)


@router.get("/packs")
async def packs(ctx: AdminRead) -> dict[str, Any]:
    require_platform(ctx)
    try:
        return {"items": [p.summary() for p in list_packs()]}
    except PackError as exc:
        raise ApiError(500, "invalid_pack", str(exc)) from exc


@router.get("/tenants")
async def tenants_list(ctx: AdminRead) -> dict[str, Any]:
    require_platform(ctx)
    return {"items": await tenants.list_tenants()}


class TenantStatusBody(_Body):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/tenants/{slug}/suspend")
async def tenants_suspend(slug: str, ctx: AdminWrite, body: TenantStatusBody | None = None) -> dict[str, Any]:
    """M7 (110 §2): suspend a tenant — blocks its logins and every tenant-scoped API call."""
    require_platform(ctx)
    return await tenants.set_status(slug, "suspended", ctx.actor(), reason=body.reason if body else None)


@router.post("/tenants/{slug}/reactivate")
@router.post("/tenants/{slug}/resume")
async def tenants_reactivate(slug: str, ctx: AdminWrite, body: TenantStatusBody | None = None) -> dict[str, Any]:
    require_platform(ctx)
    return await tenants.set_status(slug, "active", ctx.actor(), reason=body.reason if body else None)


@router.post("/tenants")
async def tenants_create(body: TenantBody, ctx: AdminWrite) -> JSONResponse:
    require_platform(ctx)
    result, created = await tenants.create_tenant(body.model_dump(), ctx.actor())
    return JSONResponse(result, status_code=201 if created else 200)


# ───────────────────────────── audit ─────────────────────────────


@router.get("/audit")
async def audit(
    ctx: AdminRead,
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, ge=1),
    action: str | None = Query(None, max_length=100),
    actor: str | None = Query(None, max_length=100),
    target: str | None = Query(None, max_length=200),
    scope: Literal["tenant", "platform"] = "tenant",
    category: Literal["runs", "approvals", "change_sets", "admin", "signals"] | None = None,
) -> dict[str, Any]:
    """``admin_audit`` rows, newest first; each row carries its ``category`` (M7, 110 §6: by action
    prefix), and ``category=`` filters on it. The cross-source timeline (runs, approvals, change
    sets, admin changes, signals) is ``GET /api/admin/audit/activity``."""
    if scope == "platform":
        require_platform(ctx)
    return await list_audit(ctx.tenant, platform=scope == "platform", limit=limit, before=before, action=action,
                            actor=actor, target=target, category=category)
