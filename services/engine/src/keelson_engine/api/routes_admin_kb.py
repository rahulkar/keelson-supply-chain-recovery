"""Back-office knowledge base ``/api/admin/kb*`` (``docs/contracts/90-m4-investigation.md`` §7).

Same rules as every ``/api/admin`` route (:mod:`keelson_engine.api.admin_deps`): role
``tenant-admin`` / ``platform-admin``, tenant = the active tenant (a platform admin's
``?tenant=`` is audited), CSRF + Origin on mutations, foreign objects 404, every mutation audited.

* ``GET /api/admin/kb`` → ``{items: [kb], profile: active embedding profile | null}``
* ``POST /api/admin/kb`` ``{name, description?}`` → 201 kb; 409 ``name_taken``
* ``GET /api/admin/kb/{id}/documents`` → ``{items: [document]}``
* ``POST /api/admin/kb/{id}/documents`` (multipart ``file`` + optional ``title``) → 201 document
  (200 + ``duplicate: true`` for the same bytes); 413 > 5 MB; 415 not .md/.txt/.pdf; 422 unreadable;
  502/503 embedding failure (the document is kept as ``failed``)
* ``DELETE /api/admin/kb/{id}/documents/{doc}`` → 204
* ``POST /api/admin/kb/reembed`` ``{scope?: tenant|platform, alias?, dims?}`` → 202 job (``platform`` =
  new profile + shadow table + swap, platform-admin only); 409 ``reembed_running``
* ``GET /api/admin/kb/reembed`` → ``{items: [job], profiles: [profile]}``
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from keelson_engine.api.admin_deps import AdminRead, AdminWrite, require_platform
from keelson_engine.api.errors import ApiError
from keelson_engine.config import get_settings
from keelson_engine.core import kb
from keelson_engine.core.repo import admin_audit

__all__ = ["router"]

router = APIRouter(prefix="/api/admin/kb")

MULTIPART_OVERHEAD = 64 * 1024
"""Bytes of multipart framing + small fields allowed on top of the file size limit."""


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KbBody(_Body):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ReembedBody(_Body):
    scope: Literal["tenant", "platform"] = "tenant"
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    dims: int | None = Field(default=None, ge=1, le=4000)


def _api(exc: kb.KbError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, **exc.extra)


@router.get("")
async def kb_list(ctx: AdminRead) -> dict[str, Any]:
    profile = await kb.active_profile()
    return {"items": await kb.list_kbs(ctx.tenant), "profile": profile.to_json() if profile else None}


@router.post("", status_code=201)
async def kb_create(body: KbBody, ctx: AdminWrite) -> dict[str, Any]:
    try:
        created = await kb.create_kb(ctx.tenant, body.name, body.description, ctx.sess.username)
    except kb.KbError as exc:
        raise _api(exc) from None
    await admin_audit(ctx.actor(), "kb.create", f"kb:{created['id']}", None,
                      {"name": created["name"]}, tenant=ctx.tenant)
    return created


@router.get("/reembed")
async def kb_reembed_jobs(ctx: AdminRead) -> dict[str, Any]:
    return {"items": await kb.list_reembed_jobs(ctx.tenant, platform=ctx.platform),
            "profiles": await kb.list_profiles()}


@router.post("/reembed", status_code=202)
async def kb_reembed(ctx: AdminWrite, body: ReembedBody | None = None) -> dict[str, Any]:
    body = body or ReembedBody()
    if body.scope == "platform" and not ctx.platform:
        # M7 four-eyes (110 §1): changing the active embedding profile affects every tenant; a
        # tenant-admin's request becomes a change request a platform-admin approves (202)
        from keelson_engine.core.change_requests import GuardedChange

        profile = await kb.active_profile()
        before = profile.to_json() if profile else None
        raise GuardedChange(
            tenant=ctx.tenant, actor=ctx.actor(), kind="embedding_profile", target="embedding_profile",
            reasons=[{"code": "embedding_profile_changed", "field": "embedding_profile",
                      "reason": "re-embeds every tenant's knowledge bases with a new embedding profile"}],
            payload={"alias": body.alias, "dims": body.dims},
            diff=[{"op": "replace", "path": "/embedding_profile",
                   "before": {"alias": (before or {}).get("alias"), "dims": (before or {}).get("dims")},
                   "after": {"alias": body.alias or (before or {}).get("alias"),
                             "dims": body.dims or (before or {}).get("dims")}}],
            message="changing the active embedding profile needs a platform admin's approval")
    if body.scope == "platform":
        require_platform(ctx)
    elif body.alias or body.dims:
        raise ApiError(422, "invalid_request", "alias/dims change the platform profile: use scope=platform")
    try:
        job = await kb.start_reembed(body.scope, ctx.tenant, ctx.sess.username, alias=body.alias, dims=body.dims)
    except kb.KbError as exc:
        raise _api(exc) from None
    await admin_audit(ctx.actor(), "kb.reembed", f"kb_reembed_job:{job['id']}", None,
                      {"scope": body.scope, "alias": body.alias, "dims": body.dims},
                      tenant=ctx.tenant)
    return job


@router.get("/{kb_id}/documents")
async def kb_documents(kb_id: str, ctx: AdminRead) -> dict[str, Any]:
    try:
        return {"items": await kb.list_documents(ctx.tenant, kb_id)}
    except kb.KbError as exc:
        raise _api(exc) from None


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ApiError(413, "file_too_large", "the upload exceeds the size limit")
        except ValueError:
            raise ApiError(400, "invalid_request", "invalid Content-Length") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "file_too_large", "the upload exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _parse_upload(request: Request) -> tuple[str, bytes, str | None]:
    """``(filename, bytes, title)`` from a ``multipart/form-data`` body capped **before** parsing
    (Starlette's parser would otherwise spool any size to disk)."""
    max_file = get_settings().kb_max_upload_bytes
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise ApiError(415, "unsupported_media_type", "upload the file as multipart/form-data (field 'file')")
    body = await _read_capped(request, max_file + MULTIPART_OVERHEAD)

    async def stream() -> AsyncGenerator[bytes]:
        yield body

    parser = MultiPartParser(request.headers, stream(), max_files=1, max_fields=4, max_part_size=4096)
    try:
        form = await parser.parse()
    except MultiPartException:
        raise ApiError(400, "invalid_multipart", "the multipart body is invalid") from None
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(422, "file_required", "the multipart field 'file' is required")
        data = await upload.read(max_file + 1)
        if len(data) > max_file:
            raise ApiError(413, "file_too_large", f"files may be at most {max_file // (1024 * 1024)} MB")
        title = form.get("title")
        return upload.filename or "document", data, title if isinstance(title, str) and title.strip() else None
    finally:
        await form.close()


@router.post("/{kb_id}/documents", status_code=201)
async def kb_upload(kb_id: str, request: Request, ctx: AdminWrite) -> JSONResponse:
    try:
        await kb.get_kb(ctx.tenant, kb_id)  # 404 before reading the body
    except kb.KbError as exc:
        raise _api(exc) from None
    filename, data, title = await _parse_upload(request)
    try:
        doc, created = await kb.ingest_document(ctx.tenant, kb_id, filename, data, title=title,
                                                created_by=ctx.sess.username)
    except kb.KbError as exc:
        if exc.extra.get("document_id"):
            await admin_audit(ctx.actor(), "kb.document.upload", f"kb_document:{exc.extra['document_id']}", None,
                              {"kb_id": kb_id, "filename": filename, "status": "failed", "error": exc.code},
                              tenant=ctx.tenant)
        raise _api(exc) from None
    if created:
        await admin_audit(ctx.actor(), "kb.document.upload", f"kb_document:{doc['id']}", None,
                          {"kb_id": doc["kb_id"], "title": doc["title"], "filename": doc["filename"],
                           "sha256": doc["sha256"], "bytes": doc["bytes"], "chunks": doc["chunks"]},
                          tenant=ctx.tenant)
    return JSONResponse({**doc, "duplicate": not created}, status_code=201 if created else 200)


@router.delete("/{kb_id}", status_code=204)
async def kb_delete(kb_id: str, ctx: AdminWrite) -> Response:
    """M7: delete a knowledge base (documents, chunks, vectors). Guarded: a tenant-admin's delete
    answers ``202`` + a four-eyes change request; 409 ``kb_in_use`` while the agent spec names it."""
    try:
        await kb.delete_kb(ctx.tenant, kb_id, ctx.actor(), platform_admin=ctx.platform)
    except kb.KbError as exc:
        raise _api(exc) from None
    return Response(status_code=204)


@router.delete("/{kb_id}/documents/{doc_id}", status_code=204)
async def kb_delete_document(kb_id: str, doc_id: str, ctx: AdminWrite) -> Response:
    try:
        doc = await kb.delete_document(ctx.tenant, kb_id, doc_id)
    except kb.KbError as exc:
        raise _api(exc) from None
    await admin_audit(ctx.actor(), "kb.document.delete", f"kb_document:{doc['id']}",
                      {"kb_id": doc["kb_id"], "title": doc["title"], "sha256": doc["sha256"]}, None,
                      tenant=ctx.tenant)
    return Response(status_code=204)
