"""M8 mapping compiler + proposed mappings admin API (``docs/contracts/121-m8-mapping-compiler.md``
§2.5, §3), under ``/api/admin``. Same rules as :mod:`keelson_engine.api.routes_admin_m7` (session +
CSRF + Origin on mutations, ``tenant-admin`` / ``platform-admin``, tenant = the active one or a
platform-admin's audited ``?tenant=``).

===============================================================  ========================================
``POST /connectors/{slug}/mappings/read/compile {mapping?}``     explain: ``MappingPlan`` (no vendor call)
``POST /connectors/{slug}/mappings/read/preview``                ``ReadPreviewRequest`` → ``ReadPreview``
``GET  /connectors/{slug}/staged``                               the last crawl's raw pages (404 none)
``GET  /mappings/proposer``                                      proposer mode + AI availability
``POST /connectors/{slug}/mappings/proposals``                   ``MappingProposeRequest`` → 201 proposal
``GET  /connectors/{slug}/mappings/proposals?limit=``            newest first (summaries)
``GET  /connectors/{slug}/mappings/proposals/{id}``              ``MappingProposal``
``POST /connectors/{slug}/mappings/proposals/{id}/discard``
===============================================================  ========================================

Accepting a draft is ``PUT /connectors/{slug}/mappings/{kind} {…, proposal_id}`` (routes_admin_m7).
Previews run in engine-api with a capped DuckDB (``DUCKDB_MEMORY_LIMIT_MB``, compose: 128 MB) and at
most :data:`MAX_PREVIEWS` at a time (429 ``preview_busy``); staged previews map at most
``MAPPING_PREVIEW_MAX_ITEMS`` items per source.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from keelson_contracts.mapping import ReadMapping, ReadPreviewRequest
from keelson_contracts.mapping_proposal import MappingProposeRequest
from keelson_engine.api.admin_deps import AdminCtx, AdminRead, AdminWrite
from keelson_engine.api.errors import ApiError
from keelson_engine.core import connector_mappings, mapping_proposals
from keelson_engine.core.repo import admin_audit

__all__ = ["MAX_PREVIEWS", "router"]

router = APIRouter(prefix="/api/admin")

MAX_PREVIEWS: Final = 2
"""Compiled previews per engine-api process (each holds one capped DuckDB session)."""
_previews: dict[str, int] = {"running": 0}


class CompileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mapping: dict[str, Any] | None = None
    """A draft read mapping (default: the saved one)."""


async def _connector_or_404(ctx: AdminCtx, slug: str) -> Any:
    from keelson_engine.core import connectors as reg

    try:
        return await reg.get_connector(ctx.tenant, slug)
    except reg.ConnectorNotFound:
        raise ApiError(404, "not_found", "connector not found") from None


async def _read_mapping(ctx: AdminCtx, slug: str, draft: dict[str, Any] | None) -> ReadMapping:
    if draft is not None:
        try:
            return ReadMapping.model_validate({"kind": "read", **draft})
        except ValidationError as exc:
            raise ApiError(422, "mapping_invalid", "the read mapping is invalid",
                           errors=[{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg"))[:300]}
                                   for e in exc.errors()[:30]]) from None
    stored = await connector_mappings.get_read_mapping(ctx.tenant, slug)
    if stored is None:
        raise ApiError(404, "not_found", "this connector has no read mapping")
    return stored


@router.post("/connectors/{slug}/mappings/read/compile")
async def mappings_read_compile(slug: str, ctx: AdminWrite, body: CompileBody | None = None) -> dict[str, Any]:
    """What the DuckDB compiler makes of a read mapping (deduplicated sources, lookups, joins,
    grouping, per-column SQL, checks). Catalogue problems are listed as warnings (nothing is called)."""
    from keelson_engine.core.mapping_compiler import CompileError, compile_read_mapping, explain

    await _connector_or_404(ctx, slug)
    mapping = await _read_mapping(ctx, slug, (body or CompileBody()).mapping)
    try:
        plan = explain(compile_read_mapping(mapping)).model_dump(mode="json")
    except CompileError as exc:
        raise ApiError(422, "mapping_invalid", f"the mapping cannot be compiled: {exc}"[:300]) from None
    problems = connector_mappings.violations(mapping, await connector_mappings.approved_classifications(ctx.tenant, slug))
    plan["warnings"] = [*(f"catalogue: {p}"[:500] for p in problems[:20]), *plan["warnings"]][:50]
    return plan


@router.post("/connectors/{slug}/mappings/read/preview")
async def mappings_read_preview(slug: str, body: ReadPreviewRequest, ctx: AdminWrite) -> dict[str, Any]:
    """Map a draft (or the saved) read mapping through the compiler — ``live``: at most
    ``max_pages`` page(s) per distinct source with the reader credentials (409 when the connector
    is not active); ``staged``: the last crawl's raw pages stored in Keelson, no vendor call (works
    with the connector disabled; 404 ``no_staged_data``). Nothing is stored."""
    from keelson_engine.config import get_settings
    from keelson_engine.core import mapping_exec

    conn = await _connector_or_404(ctx, slug)
    mapping = await _read_mapping(ctx, slug, body.mapping)
    problems = connector_mappings.violations(mapping, await connector_mappings.approved_classifications(ctx.tenant, slug))
    if problems:
        raise ApiError(422, "mapping_invalid", problems[0], problems=problems[:30])
    if body.source == "live" and not conn.usable:
        raise ApiError(409, "connector_inactive", f"the connector is {conn.status}: activate it, or preview the last "
                                                  "crawl's staged data")
    if _previews["running"] >= MAX_PREVIEWS:
        raise ApiError(429, "preview_busy", "two previews are already running; try again in a moment")
    settings = get_settings()
    _previews["running"] += 1
    try:
        result = await mapping_exec.preview(
            ctx.tenant, mapping, source=body.source, conn=conn, max_pages=body.max_pages,
            max_items=min(body.max_items_per_source, settings.mapping_preview_max_items), sample=body.sample)
    except mapping_exec.NoStagedData:
        raise ApiError(404, "no_staged_data", "no crawl of this connector has staged data yet: crawl it once, or "
                                              "preview live") from None
    finally:
        _previews["running"] -= 1
    await admin_audit(ctx.actor(), "connector.mapping.preview", f"connector:{slug}/mapping:read", None,
                      {"source": body.source, "engine": result.get("engine"), "ok": result.get("ok"),
                       "capped": result.get("capped")}, tenant=ctx.tenant)
    return result


@router.get("/connectors/{slug}/staged")
async def connectors_staged(slug: str, ctx: AdminRead) -> dict[str, Any]:
    """The raw pages a staged preview maps: the newest complete raw set (the current snapshot's or
    the latest failed attempt's) — sources, pages, items, bytes and the fetch range."""
    from keelson_engine.core import scn_staging

    await _connector_or_404(ctx, slug)
    staged = await scn_staging.latest_set(ctx.tenant, slug)
    if staged is None:
        raise ApiError(404, "no_staged_data", "no crawl of this connector has staged data yet")
    return {
        "connector": slug, "snapshot_id": staged["snapshot_id"],
        "status": "failed" if staged["snapshot_status"] == "failed" else "complete",
        "mapping_version": staged["mapping_version"], "mapping_hash": staged["mapping_hash"],
        "sources": [{k: s.get(k) for k in ("key", "tool", "pages", "items", "truncated")} for s in staged["sources"]],
        "pages": staged["pages"], "items": staged["items"], "bytes": staged["bytes"],
        "fetched_from": staged["fetched_from"], "fetched_to": staged["fetched_to"],
    }


# ───────────────────────────── proposals ─────────────────────────────


async def proposer_availability(tenant: str) -> dict[str, Any]:
    """``{mode, enabled, llm: {available, reason, message}}`` for the proposal panel."""
    from keelson_contracts.agents import is_pro_model

    from keelson_engine.config import get_settings
    from keelson_engine.core import connectors as reg
    from keelson_engine.runtime.errors import RuntimeConfigError
    from keelson_engine.runtime.replay import replay_mode

    settings = get_settings()
    mode = settings.mapping_proposer
    llm: dict[str, Any] = {"available": False, "reason": None, "message": None}
    if mode != "auto":
        llm.update(reason="disabled", message="AI refinement is switched off in this installation: rule-based "
                                              "suggestions only.")
    elif is_pro_model(settings.mapping_proposer_alias):
        llm.update(reason="pro_alias", message="The configured proposer model is not allowed (Pro).")
    elif not await reg.get_tenant_llm_key_version(tenant):
        llm.update(reason="no_key", message="No AI model is configured for this tenant: rule-based suggestions only.")
    else:
        try:
            replay = replay_mode()
        except RuntimeConfigError:
            replay = "invalid"
        llm["available"] = True
        if replay in ("replay", "invalid"):
            llm.update(reason="replay_only", message="This installation only replays recorded model answers; a new "
                                                     "proposal falls back to rule-based suggestions.")
    return {"mode": mode, "enabled": mode != "off", "alias": settings.mapping_proposer_alias, "llm": llm}


@router.get("/mappings/proposer")
async def mappings_proposer(ctx: AdminRead) -> dict[str, Any]:
    return await proposer_availability(ctx.tenant)


@router.post("/connectors/{slug}/mappings/proposals")
async def mappings_propose(slug: str, body: MappingProposeRequest, ctx: AdminWrite) -> JSONResponse:
    """Sample the approved trusted read tools (shape-only profiles), propose read / action drafts
    (rules, refined by the AI when available) and store them for review. Never activates anything
    and never calls a write tool."""
    actor = {**ctx.actor(), "roles": list(ctx.sess.roles)}
    proposal = await mapping_proposals.create(ctx.tenant, slug, body, actor)
    return JSONResponse(proposal, status_code=201)


@router.get("/connectors/{slug}/mappings/proposals")
async def mappings_proposals(slug: str, ctx: AdminRead, limit: int = Query(20, ge=1, le=20)) -> dict[str, Any]:
    return {"items": await mapping_proposals.list_proposals(ctx.tenant, slug, limit=limit)}


@router.get("/connectors/{slug}/mappings/proposals/{proposal_id}")
async def mappings_proposal(slug: str, proposal_id: str, ctx: AdminRead) -> dict[str, Any]:
    return await mapping_proposals.get_proposal(ctx.tenant, slug, proposal_id)


@router.post("/connectors/{slug}/mappings/proposals/{proposal_id}/discard")
async def mappings_proposal_discard(slug: str, proposal_id: str, ctx: AdminWrite) -> dict[str, Any]:
    return await mapping_proposals.discard(ctx.tenant, slug, proposal_id, ctx.actor())
