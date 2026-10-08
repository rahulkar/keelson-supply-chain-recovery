"""Knowledge base (RAG) — ``docs/contracts/90-m4-investigation.md`` §7.

Tables (migration ``0005_kb``; all tenant tables RLS FORCE): ``kb``, ``kb_document``, ``kb_chunk``,
``kb_reembed_job`` and the platform table ``embedding_profile``. Vectors live in one table per
profile, ``kb_embedding_<profile id>(chunk_id, tenant, embedding)`` — ``vector(dims)`` for dims ≤
2000, ``halfvec(dims)`` up to 4000 — with an HNSW cosine index and RLS FORCE, created here when a
profile is created.

* **Ingestion** (:func:`ingest_document`): ``.md`` / ``.txt`` / ``.pdf`` ≤ 5 MB → sections →
  heading-aware chunks (``core.kb_text``) → LiteLLM ``POST /embeddings`` with the **tenant's**
  virtual key, the active profile's alias and ``dimensions`` → returned dims validated → stored.
  Idempotent by ``sha256`` per knowledge base (same bytes = same document; a failed one is retried).
* **Search** (:func:`search`): the query is embedded the same way; cosine similarity over the
  tenant's ready documents. Results are **untrusted text** (``content_trust=untrusted_text``):
  they may only reach a model through the quarantine wrapper.
* **Re-embed** (:func:`start_reembed`): ``platform`` scope = new profile version → shadow table →
  every tenant's chunks embedded with that tenant's key → swap (old profile retired, its table
  dropped) under an exclusive advisory lock; ``tenant`` scope = embed the tenant's chunks that have
  no vector in the active profile (repairs documents whose embedding failed).

Concurrency: ingestion writes vectors for the active profile (required) and the building profile
of a live platform job (best effort) inside a transaction holding
``pg_advisory_xact_lock_shared(KB_PROFILE_LOCK)``; search queries under the same shared lock (a
swap cannot drop the table it reads); the swap holds the exclusive lock and only flips when no
chunk of a ready/failed document lacks a vector in the new table. A building profile whose job
died is ignored by ingestion and discarded by the next platform job.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_engine.config import get_settings
from keelson_engine.core.db import check_tenant_slug, system_session, tenant_session
from keelson_engine.core.http import internal_http
from keelson_engine.core.kb_pdf import extract_pdf_isolated
from keelson_engine.core.kb_text import (
    Chunk,
    KbTextError,
    chunk_sections,
    detect_mime,
    embedding_input,
    extract,
    safe_filename,
)
from keelson_engine.core.logs import get_logger

__all__ = [
    "KB_PROFILE_LOCK",
    "EmbeddingProfile",
    "KbError",
    "active_profile",
    "cancel_jobs",
    "create_kb",
    "delete_document",
    "delete_kb",
    "embed_texts",
    "ensure_profile",
    "get_kb",
    "get_or_create_kb",
    "ingest_document",
    "list_documents",
    "list_kbs",
    "list_profiles",
    "list_reembed_jobs",
    "run_reembed",
    "search",
    "seed_dir",
    "seed_tenant",
    "start_reembed",
    "tenant_has_documents",
    "vector_type",
    "wait_for_jobs",
]

log = get_logger(__name__)

KB_PROFILE_LOCK = 0x4B42_5052_4F46  # "KBPROF"
MAX_VECTOR_DIMS = 2000
"""pgvector HNSW limit for ``vector``; above this (≤ 4000) the profile uses ``halfvec``."""
MAX_HALFVEC_DIMS = 4000
MAX_QUERY_CHARS = 2000
MAX_TOP_K = 50
FILL_PAGE = 256
_EMBED_RETRIES = 1


class KbError(Exception):
    """``status`` (HTTP) + ``code`` (enum-like) + safe ``message`` (+ extra JSON fields)."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# ───────────────────────────── profiles ─────────────────────────────


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    id: int
    alias: str
    provider_model: str
    dims: int
    version: int
    active: bool
    status: str

    @property
    def table(self) -> str:
        return f"kb_embedding_{int(self.id)}"

    @property
    def vtype(self) -> str:
        return vector_type(self.dims)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "alias": self.alias, "provider_model": self.provider_model, "dims": self.dims,
                "version": self.version, "active": self.active, "status": self.status,
                "storage": f"{self.vtype}({self.dims})"}


def vector_type(dims: int) -> str:
    if not 1 <= int(dims) <= MAX_HALFVEC_DIMS:
        raise KbError(422, "invalid_dims", f"embedding dims must be between 1 and {MAX_HALFVEC_DIMS}")
    return "vector" if dims <= MAX_VECTOR_DIMS else "halfvec"


_PROFILE_COLS = "id, alias, provider_model, dims, version, active, status"


def _profile(row: Any) -> EmbeddingProfile:
    m = row._mapping
    return EmbeddingProfile(int(m["id"]), m["alias"], m["provider_model"], int(m["dims"]), int(m["version"]),
                            bool(m["active"]), m["status"])


async def active_profile() -> EmbeddingProfile | None:
    async with system_session() as s:
        row = (await s.execute(text(f"SELECT {_PROFILE_COLS} FROM embedding_profile WHERE active"))).first()
    return _profile(row) if row else None


async def _live_profiles(s: AsyncSession) -> list[EmbeddingProfile]:
    """Active + building profiles (ingestion writes vectors into those it embedded for)."""
    rows = (await s.execute(text(
        f"SELECT {_PROFILE_COLS} FROM embedding_profile WHERE status IN ('active', 'building') ORDER BY id"))).all()
    return [_profile(r) for r in rows]


async def _ingest_targets() -> list[EmbeddingProfile]:
    """The active profile + building profiles of a **live** platform re-embed job (running, with
    progress within ``KB_REEMBED_STALE_SECONDS``). A building profile left behind by a crashed job
    is not embedded into (it would double every upload's embedding cost, and fail uploads when its
    model is broken); the next platform job discards it."""
    stale_s = get_settings().kb_reembed_stale_seconds
    async with system_session() as s:
        rows = (await s.execute(text(
            f"SELECT {_PROFILE_COLS} FROM embedding_profile p WHERE p.status = 'active' OR (p.status = 'building' "
            "AND EXISTS (SELECT 1 FROM kb_reembed_job j WHERE j.to_profile_id = p.id AND j.status = 'running' "
            "AND j.updated_at >= now() - make_interval(secs => :stale))) ORDER BY p.id"), {"stale": stale_s})).all()
    return [_profile(r) for r in rows]


async def list_profiles() -> list[dict[str, Any]]:
    async with system_session() as s:
        rows = (await s.execute(text(f"SELECT {_PROFILE_COLS} FROM embedding_profile ORDER BY id DESC LIMIT 20"))).all()
    return [_profile(r).to_json() for r in rows]


async def _create_embedding_table(s: AsyncSession, profile: EmbeddingProfile) -> None:
    t, vt = profile.table, profile.vtype
    statements = [
        f"CREATE TABLE IF NOT EXISTS {t} ("
        f" chunk_id uuid PRIMARY KEY,"
        f" tenant text NOT NULL REFERENCES tenant(slug),"
        f" embedding {vt}({int(profile.dims)}) NOT NULL,"
        f" FOREIGN KEY (tenant, chunk_id) REFERENCES kb_chunk (tenant, id) ON DELETE CASCADE)",
        f"CREATE INDEX IF NOT EXISTS {t}_tenant_idx ON {t} (tenant)",
        f"CREATE INDEX IF NOT EXISTS {t}_hnsw_idx ON {t} USING hnsw (embedding {vt}_cosine_ops)",
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS tenant_isolation ON {t}",
        f"CREATE POLICY tenant_isolation ON {t} AS PERMISSIVE FOR ALL "
        "USING (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on') "
        "WITH CHECK (tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on')",
    ]
    for stmt in statements:
        await s.execute(text(stmt))


async def _new_profile(alias: str, provider_model: str, dims: int, *, active: bool) -> EmbeddingProfile:
    vector_type(dims)
    async with system_session() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": KB_PROFILE_LOCK})
        version = (await s.execute(text("SELECT coalesce(max(version), 0) + 1 FROM embedding_profile WHERE alias = :a"),
                                   {"a": alias})).scalar_one()
        if active and (await s.execute(text("SELECT 1 FROM embedding_profile WHERE active"))).first():
            raise KbError(409, "profile_exists", "an active embedding profile exists; use a re-embed to change it")
        row = (await s.execute(text(
            "INSERT INTO embedding_profile (alias, provider_model, dims, version, active, status, activated_at) "
            "VALUES (:a, :m, :d, :v, :act, :st, CASE WHEN :act THEN now() END) "
            f"RETURNING {_PROFILE_COLS}"),
            {"a": alias, "m": provider_model, "d": int(dims), "v": int(version), "act": active,
             "st": "active" if active else "building"})).one()
        profile = _profile(row)
        await _create_embedding_table(s, profile)
    log.info("embedding profile created", profile_id=profile.id, alias=alias, dims=dims, active=active)
    return profile


def _same_spec(p: EmbeddingProfile, alias: str, provider_model: str, dims: int) -> bool:
    return p.alias == alias and p.provider_model == provider_model and p.dims == int(dims)


async def ensure_profile(alias: str, provider_model: str, dims: int, *, actor: str = "bootstrap") -> EmbeddingProfile:
    """Bootstrap: the active profile for ``alias`` / ``provider_model`` / ``dims``. Creates the first
    profile (active at once); when the active one differs (models.lock changed) runs a platform
    re-embed (shadow table + swap) and returns the new profile — or, if that fails, keeps the old one
    (logged) so bootstrap never loses the working index."""
    current = await active_profile()
    if current is None:
        try:
            return await _new_profile(alias, provider_model, dims, active=True)
        except KbError as exc:
            if exc.code != "profile_exists":
                raise
            current = await active_profile()
            assert current is not None
    if _same_spec(current, alias, provider_model, dims):
        return current
    log.warning("embedding profile changed; re-embedding", from_profile=current.to_json(),
                alias=alias, provider_model=provider_model, dims=dims)
    try:
        job = await _create_job("platform", None, actor, from_profile=current.id)
        await run_reembed(job["id"], alias=alias, provider_model=provider_model, dims=dims)
    except KbError as exc:
        log.error("re-embed failed; keeping the previous profile", code=exc.code, error=exc.message)
        return current
    new = await active_profile()
    assert new is not None
    return new


# ───────────────────────────── embeddings (LiteLLM) ─────────────────────────────


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def embed_texts(tenant: str, profile: EmbeddingProfile, texts: Sequence[str]) -> list[list[float]]:
    """``POST {LITELLM_BASE_URL}/embeddings`` with the tenant's virtual key, ``model`` = the
    profile's alias and ``dimensions`` = its dims, in batches of ``KB_EMBED_BATCH``. Every vector
    must have exactly ``profile.dims`` finite numbers (else ``502 embedding_dims_mismatch`` /
    ``embedding_invalid``); transport errors / non-2xx → ``502 embedding_failed`` (the key is never
    logged or returned)."""
    from keelson_engine.core.connectors import LlmKeyMissing, get_tenant_llm_key

    if not texts:
        return []
    try:
        key = await get_tenant_llm_key(tenant)
    except LlmKeyMissing:
        raise KbError(503, "llm_key_missing", "the tenant has no AI gateway key (run bootstrap)") from None
    st = get_settings()
    batch = max(1, int(st.kb_embed_batch))
    out: list[list[float]] = []
    for start in range(0, len(texts), batch):
        out.extend(await _embed_batch(st.litellm_base_url, key, profile, list(texts[start:start + batch]),
                                      st.llm_request_timeout_s))
    return out


async def _embed_batch(base: str, key: str, profile: EmbeddingProfile, texts: list[str],
                       timeout_s: float) -> list[list[float]]:
    body = {"model": profile.alias, "input": texts, "dimensions": profile.dims, "encoding_format": "float"}
    headers = {"Authorization": f"Bearer {key}"}
    resp: httpx.Response | None = None
    for attempt in range(_EMBED_RETRIES + 1):
        try:
            resp = await internal_http().post(f"{base}/embeddings", json=body, headers=headers, timeout=timeout_s)
        except httpx.HTTPError as exc:
            log.warning("embedding request failed", alias=profile.alias, error=type(exc).__name__, attempt=attempt)
            resp = None
        if resp is not None and (resp.status_code < 500 and resp.status_code != 429):
            break
        if attempt < _EMBED_RETRIES:
            await asyncio.sleep(1.0)
    if resp is None:
        raise KbError(502, "embedding_failed", "the embedding service is not reachable")
    if resp.status_code != 200:
        log.warning("embedding request rejected", alias=profile.alias, status=resp.status_code)
        code = "budget_exhausted" if resp.status_code in (402, 422) and b"budget" in resp.content.lower() \
            else "embedding_failed"
        raise KbError(502, code, f"the embedding service answered HTTP {resp.status_code}")
    try:
        data = resp.json()["data"]
        items = sorted(data, key=lambda d: int(d.get("index", 0)))
        vectors = [d["embedding"] for d in items]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise KbError(502, "embedding_invalid", "the embedding service returned an invalid response") from None
    if len(vectors) != len(texts):
        raise KbError(502, "embedding_invalid", "the embedding service returned the wrong number of vectors")
    for vec in vectors:
        if not isinstance(vec, list):
            raise KbError(502, "embedding_invalid", "the embedding service returned an invalid vector")
        if len(vec) != profile.dims:
            raise KbError(502, "embedding_dims_mismatch",
                          f"the embedding model returned {len(vec)} dimensions, the profile expects {profile.dims}",
                          expected_dims=profile.dims, returned_dims=len(vec))
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vec):
            raise KbError(502, "embedding_invalid", "the embedding service returned a non-numeric vector")
    return [[float(x) for x in v] for v in vectors]


# ───────────────────────────── knowledge bases ─────────────────────────────


def _uuid(value: str, what: str = "knowledge base") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise KbError(404, "not_found", f"{what} not found") from None


def _kb_json(m: Any) -> dict[str, Any]:
    return {"id": str(m["id"]), "name": m["name"], "description": m["description"],
            "created_by": m["created_by"], "created_at": m["created_at"].isoformat(),
            "documents": int(m.get("documents") or 0), "ready_documents": int(m.get("ready_documents") or 0),
            "chunks": int(m.get("chunks") or 0)}


_KB_SELECT = """
SELECT k.id, k.name, k.description, k.created_by, k.created_at,
       (SELECT count(*) FROM kb_document d WHERE d.tenant = k.tenant AND d.kb_id = k.id) AS documents,
       (SELECT count(*) FROM kb_document d WHERE d.tenant = k.tenant AND d.kb_id = k.id
          AND d.status = 'ready') AS ready_documents,
       (SELECT coalesce(sum(d.chunks), 0) FROM kb_document d WHERE d.tenant = k.tenant AND d.kb_id = k.id) AS chunks
FROM kb k
"""


async def list_kbs(tenant: str) -> list[dict[str, Any]]:
    async with tenant_session(tenant) as s:
        rows = (await s.execute(text(_KB_SELECT + " WHERE k.tenant = :t ORDER BY k.created_at"), {"t": tenant})).all()
    return [_kb_json(r._mapping) for r in rows]


async def get_kb(tenant: str, kb_id: str) -> dict[str, Any]:
    kid = _uuid(kb_id)
    async with tenant_session(tenant) as s:
        row = (await s.execute(text(_KB_SELECT + " WHERE k.tenant = :t AND k.id = CAST(:k AS uuid)"),
                               {"t": tenant, "k": kid})).first()
    if row is None:
        raise KbError(404, "not_found", "knowledge base not found")
    return _kb_json(row._mapping)


async def create_kb(tenant: str, name: str, description: str = "", created_by: str | None = None) -> dict[str, Any]:
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 120:
        raise KbError(422, "invalid_name", "the name must be 1-120 characters")
    try:
        async with tenant_session(tenant) as s:
            row = (await s.execute(text(
                "INSERT INTO kb (tenant, name, description, created_by) VALUES (:t, :n, :d, :c) RETURNING id"),
                {"t": tenant, "n": name, "d": (description or "")[:2000], "c": created_by})).one()
    except IntegrityError:
        raise KbError(409, "name_taken", "a knowledge base with this name exists") from None
    return await get_kb(tenant, str(row[0]))


async def get_or_create_kb(tenant: str, name: str, description: str = "",
                           created_by: str | None = None) -> dict[str, Any]:
    async with tenant_session(tenant) as s:
        row = (await s.execute(text("SELECT id FROM kb WHERE tenant = :t AND lower(name) = lower(:n)"),
                               {"t": tenant, "n": name})).first()
    if row is not None:
        return await get_kb(tenant, str(row[0]))
    try:
        return await create_kb(tenant, name, description, created_by)
    except KbError as exc:
        if exc.code != "name_taken":
            raise
        return await get_or_create_kb(tenant, name, description, created_by)


_DOC_COLS = ("id, kb_id, title, filename, mime, bytes, sha256, status, error, chunks, tokens, created_by, "
             "created_at, updated_at")


def _doc_json(m: Any) -> dict[str, Any]:
    return {"id": str(m["id"]), "kb_id": str(m["kb_id"]), "title": m["title"], "filename": m["filename"],
            "mime": m["mime"], "bytes": int(m["bytes"]), "sha256": m["sha256"], "status": m["status"],
            "error": m["error"], "chunks": int(m["chunks"]), "tokens": int(m["tokens"]),
            "created_by": m["created_by"], "created_at": m["created_at"].isoformat(),
            "updated_at": m["updated_at"].isoformat()}


async def list_documents(tenant: str, kb_id: str) -> list[dict[str, Any]]:
    await get_kb(tenant, kb_id)
    async with tenant_session(tenant) as s:
        rows = (await s.execute(text(
            f"SELECT {_DOC_COLS} FROM kb_document WHERE tenant = :t AND kb_id = CAST(:k AS uuid) "
            "ORDER BY created_at DESC"), {"t": tenant, "k": _uuid(kb_id)})).all()
    return [_doc_json(r._mapping) for r in rows]


async def _get_document(s: AsyncSession, tenant: str, doc_id: str) -> dict[str, Any] | None:
    row = (await s.execute(text(f"SELECT {_DOC_COLS} FROM kb_document WHERE tenant = :t AND id = CAST(:d AS uuid)"),
                           {"t": tenant, "d": doc_id})).first()
    return _doc_json(row._mapping) if row else None


async def _kb_in_use(tenant: str, kb_id: str) -> bool:
    """The latest agent spec names the knowledge base (``investigation.kb_ids``)."""
    from keelson_engine.core import agent_specs

    spec = (await agent_specs.latest(tenant)).get("spec") or {}
    ids = (spec.get("investigation") or {}).get("kb_ids") if isinstance(spec, dict) else None
    return isinstance(ids, list) and any(str(i).lower() == kb_id for i in ids)


def _kb_base(kb: dict[str, Any]) -> str:
    """``base_version`` of a ``kb_delete`` request: the knowledge base as the requester saw it (name +
    document count); approval answers 409 ``stale_base`` when documents were added meanwhile."""
    import hashlib

    raw = f"{kb.get('id')}|{kb.get('name')}|{kb.get('documents')}".encode()
    return "state:" + hashlib.sha256(raw).hexdigest()[:24]


async def delete_kb(tenant: str, kb_id: str, actor: dict[str, Any], *, platform_admin: bool,
                    cosigned: bool = False, base_version: str | None = None) -> dict[str, Any]:
    """M7: delete a knowledge base with its documents, chunks and vectors (FK cascade). Guarded:
    a tenant-admin's delete becomes a four-eyes change request (``kb_delete``); 409 ``kb_in_use``
    while the latest agent spec names it in ``investigation.kb_ids``."""
    from keelson_engine.core.repo import admin_audit

    kb = await get_kb(tenant, kb_id)
    kid = kb["id"]
    if await _kb_in_use(tenant, kid):
        raise KbError(409, "kb_in_use", "the agent spec still searches this knowledge base (investigation.kb_ids)")
    if cosigned and base_version is not None and _kb_base(kb) != base_version:
        raise KbError(409, "stale_base", "the knowledge base changed since this request was made")
    if not (platform_admin or cosigned):
        from keelson_engine.core.change_requests import GuardedChange

        raise GuardedChange(
            tenant=tenant, actor=actor, kind="kb_delete", target=f"kb:{kid}",
            reasons=[{"code": "kb_deleted", "field": f"kb:{kid}",
                      "reason": f"deletes the knowledge base {kb['name'][:120]!r} and its documents"}],
            payload={"kb_id": kid},
            diff=[{"op": "remove", "path": f"/kb/{kid}",
                   "before": {"name": kb["name"], "documents": kb.get("documents")}}],
            base_version=_kb_base(kb),
            message="deleting a knowledge base needs a second admin")
    async with tenant_session(tenant) as s:
        row = (await s.execute(text("DELETE FROM kb WHERE tenant = :t AND id = CAST(:k AS uuid) RETURNING id"),
                               {"t": tenant, "k": kid})).first()
    if row is None:
        raise KbError(404, "not_found", "knowledge base not found")
    await admin_audit(actor, "kb.delete", f"kb:{kid}", {"name": kb["name"], "documents": kb.get("documents")}, None,
                      tenant=tenant)
    return {"id": kid, "deleted": True}


async def delete_document(tenant: str, kb_id: str, doc_id: str) -> dict[str, Any]:
    """Deletes the document, its chunks and (cascade) their vectors in every profile table."""
    kid, did = _uuid(kb_id), _uuid(doc_id, "document")
    async with tenant_session(tenant) as s:
        row = (await s.execute(text(
            f"DELETE FROM kb_document WHERE tenant = :t AND kb_id = CAST(:k AS uuid) AND id = CAST(:d AS uuid) "
            f"RETURNING {_DOC_COLS}"), {"t": tenant, "k": kid, "d": did})).first()
    if row is None:
        raise KbError(404, "not_found", "document not found")
    return _doc_json(row._mapping)


async def tenant_has_documents(tenant: str) -> bool:
    """True when the tenant has any ready or in-flight document (bootstrap seeds only empty KBs)."""
    async with tenant_session(tenant) as s:
        row = (await s.execute(text(
            "SELECT 1 FROM kb_document WHERE tenant = :t AND status IN ('ready', 'processing') LIMIT 1"),
            {"t": tenant})).first()
    return row is not None


# ───────────────────────────── ingestion ─────────────────────────────


async def _insert_vectors(s: AsyncSession, tenant: str, profile: EmbeddingProfile,
                          rows: list[tuple[str, list[float]]]) -> None:
    if not rows:
        return
    # INSERT … SELECT … WHERE EXISTS: a chunk deleted meanwhile (document deleted) is skipped
    await s.execute(
        text(f"INSERT INTO {profile.table} (chunk_id, tenant, embedding) "
             f"SELECT CAST(:c AS uuid), :t, CAST(:e AS {profile.vtype}) "
             "WHERE EXISTS (SELECT 1 FROM kb_chunk WHERE tenant = :t AND id = CAST(:c AS uuid)) "
             "ON CONFLICT (chunk_id) DO NOTHING"),
        [{"c": cid, "t": tenant, "e": _vector_literal(vec)} for cid, vec in rows],
    )


async def _claim_document(tenant: str, kb_id: str, *, sha: str, title: str, filename: str, mime: str, size: int,
                          chunks: list[Chunk], created_by: str | None) -> tuple[dict[str, Any], bool, list[str]]:
    """Insert (or re-claim a failed / abandoned) document row + its chunks.
    Returns ``(document, claimed, chunk ids)``; ``claimed=False`` = an existing document is returned."""
    stale_s = get_settings().kb_reembed_stale_seconds
    total_tokens = sum(c.tokens for c in chunks)
    async with tenant_session(tenant) as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                        {"k": f"keelson:kb_doc|{tenant}|{kb_id}|{sha}"})
        row = (await s.execute(text(
            f"SELECT {_DOC_COLS}, (updated_at < now() - make_interval(secs => :stale)) AS abandoned "
            "FROM kb_document WHERE tenant = :t AND kb_id = CAST(:k AS uuid) AND sha256 = :sha"),
            {"t": tenant, "k": kb_id, "sha": sha, "stale": stale_s})).first()
        if row is not None:
            m = row._mapping
            if m["status"] == "ready" or (m["status"] == "processing" and not m["abandoned"]):
                return _doc_json(m), False, []
            doc_id = str(m["id"])
            await s.execute(text("DELETE FROM kb_chunk WHERE tenant = :t AND document_id = CAST(:d AS uuid)"),
                            {"t": tenant, "d": doc_id})
            await s.execute(text(
                "UPDATE kb_document SET status = 'processing', error = NULL, title = :title, filename = :fn, "
                "chunks = :n, tokens = :tok, updated_at = now() WHERE tenant = :t AND id = CAST(:d AS uuid)"),
                {"t": tenant, "d": doc_id, "title": title, "fn": filename, "n": len(chunks), "tok": total_tokens})
        else:
            doc_id = str(uuid.uuid7())
            await s.execute(text(
                "INSERT INTO kb_document (id, tenant, kb_id, title, filename, mime, bytes, sha256, status, chunks, "
                "tokens, created_by) VALUES (CAST(:d AS uuid), :t, CAST(:k AS uuid), :title, :fn, :mime, :b, :sha, "
                "'processing', :n, :tok, :by)"),
                {"d": doc_id, "t": tenant, "k": kb_id, "title": title, "fn": filename, "mime": mime, "b": size,
                 "sha": sha, "n": len(chunks), "tok": total_tokens, "by": created_by})
        chunk_ids = [str(uuid.uuid7()) for _ in chunks]
        if chunks:
            await s.execute(text(
                "INSERT INTO kb_chunk (id, tenant, document_id, ordinal, text, tokens, heading) "
                "VALUES (CAST(:id AS uuid), :t, CAST(:d AS uuid), :o, :x, :tok, :h)"),
                [{"id": cid, "t": tenant, "d": doc_id, "o": c.ordinal, "x": c.text, "tok": c.tokens, "h": c.heading}
                 for cid, c in zip(chunk_ids, chunks, strict=True)])
        doc = await _get_document(s, tenant, doc_id)
    assert doc is not None
    return doc, True, chunk_ids


async def _fail_document(tenant: str, doc_id: str, code: str) -> None:
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE kb_document SET status = 'failed', error = :e, updated_at = now() "
                             "WHERE tenant = :t AND id = CAST(:d AS uuid)"), {"t": tenant, "d": doc_id, "e": code})


async def ingest_document(tenant: str, kb_id: str, filename: str, data: bytes, *, title: str | None = None,
                          created_by: str | None = None) -> tuple[dict[str, Any], bool]:
    """Extract → chunk → embed → store one upload. Returns ``(document, created)``; ``created=False``
    when the same bytes are already a ready (or in-flight) document of this knowledge base.
    Raises :class:`KbError` (413/415/422 refused upload, nothing stored; 502/503 embedding failure,
    the document is kept as ``failed`` with ``error`` = the code and is retried by the next upload
    of the same file or a tenant re-embed)."""
    check_tenant_slug(tenant)
    st = get_settings()
    kid = _uuid(kb_id)
    await get_kb(tenant, kid)
    if len(data) > st.kb_max_upload_bytes:
        raise KbError(413, "file_too_large", f"files may be at most {st.kb_max_upload_bytes // (1024 * 1024)} MB")
    if not data:
        raise KbError(422, "empty_file", "the file is empty")
    filename = safe_filename(filename)

    # every chunk holds ≤ 4 * kb_chunk_tokens chars: longer text cannot fit the chunk limit
    max_chars = st.kb_max_chunks_per_document * st.kb_chunk_tokens * 4
    try:
        # CPU-bound work never runs on the event loop: PDFs in a resource-limited child process
        # (core/kb_pdf.py), text in a worker thread; the chunker stops once over the chunk limit
        if detect_mime(filename, data) == "application/pdf":
            extracted = await extract_pdf_isolated(filename, data, title=title, max_pages=st.kb_max_pdf_pages,
                                                   max_chars=max_chars, timeout_s=st.kb_pdf_timeout_s,
                                                   memory_mb=st.kb_pdf_memory_mb)
        else:
            extracted = await asyncio.to_thread(extract, filename, data, title=title)
        chunks = await asyncio.to_thread(chunk_sections, extracted.sections, max_tokens=st.kb_chunk_tokens,
                                         overlap_tokens=st.kb_chunk_overlap_tokens,
                                         max_chunks=st.kb_max_chunks_per_document)
    except KbTextError as exc:
        raise KbError(exc.status, exc.code, exc.message) from None
    if not chunks:
        raise KbError(422, "no_text", "the document contains no extractable text")
    if len(chunks) > st.kb_max_chunks_per_document:
        raise KbError(422, "too_many_chunks", f"the document is too long (more than {st.kb_max_chunks_per_document} "
                                              "chunks)")
    sha = hashlib.sha256(data).hexdigest()
    doc, claimed, chunk_ids = await _claim_document(
        tenant, kid, sha=sha, title=extracted.title, filename=filename, mime=extracted.mime, size=len(data),
        chunks=chunks, created_by=created_by)
    if not claimed:
        return doc, False
    inputs = [embedding_input(extracted.title, c.heading, c.text) for c in chunks]
    try:
        await _store_vectors(tenant, doc["id"], chunk_ids, inputs)
    except KbError as exc:
        await _fail_document(tenant, doc["id"], exc.code)
        raise KbError(exc.status, exc.code, exc.message, document_id=doc["id"], **exc.extra) from None
    except BaseException:
        await asyncio.shield(_fail_document(tenant, doc["id"], "internal_error"))
        raise
    async with tenant_session(tenant) as s:
        final = await _get_document(s, tenant, doc["id"])
    assert final is not None
    log.info("kb document ingested", tenant=tenant, kb_id=kid, document_id=doc["id"], chunks=len(chunks),
             mime=extracted.mime)
    return final, True


async def _store_vectors(tenant: str, doc_id: str, chunk_ids: list[str], inputs: list[str]) -> None:
    """Embed for the active profile (required) and the building profile of a live re-embed (best
    effort: a failure there is logged and skipped — the re-embed's fill pass embeds whatever is
    missing before it swaps), then write the vectors + flip the document to ready in one transaction
    under the shared profile lock. If the active profile was swapped between embedding and writing,
    embed for the new one and retry."""
    vectors: dict[int, list[list[float]] | None] = {}
    seen: set[int] = set()
    for _ in range(4):
        profiles = await _ingest_targets()
        seen.update(p.id for p in profiles)
        if not any(p.active for p in profiles):
            raise KbError(503, "no_embedding_profile", "no embedding profile is configured (run bootstrap)")
        for p in sorted(profiles, key=lambda p: not p.active):  # the active profile first
            if p.id in vectors:
                continue
            try:
                vectors[p.id] = await embed_texts(tenant, p, inputs)
            except KbError as exc:
                if p.active:
                    raise
                log.warning("embedding for the building profile failed; the re-embed fills it", tenant=tenant,
                            document_id=doc_id, profile_id=p.id, code=exc.code)
                vectors[p.id] = None
        async with tenant_session(tenant) as s:
            await s.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KB_PROFILE_LOCK})
            current = await _live_profiles(s)
            active = next((p for p in current if p.active), None)
            if active is None or vectors.get(active.id) is None:
                continue  # swapped meanwhile: embed for the new active profile
            fresh = [p.id for p in current if p.id not in seen]
            seen.update(p.id for p in current)
            if fresh:
                continue  # a re-embed started meanwhile: embed for its profile too if its job is live
            for p in current:
                vecs = vectors.get(p.id)
                if vecs is not None:
                    await _insert_vectors(s, tenant, p, list(zip(chunk_ids, vecs, strict=True)))
            await s.execute(text("UPDATE kb_document SET status = 'ready', error = NULL, updated_at = now() "
                                 "WHERE tenant = :t AND id = CAST(:d AS uuid)"), {"t": tenant, "d": doc_id})
            return
    raise KbError(503, "profile_changing", "the embedding profile is changing; retry the upload")


# ───────────────────────────── search ─────────────────────────────


async def search(tenant: str, query: str, kb_ids: Sequence[str] | None = None, top_k: int = 6) -> list[dict[str, Any]]:
    """Top-``top_k`` chunks of the tenant's **ready** documents by cosine similarity to ``query``
    (optionally only in ``kb_ids``). Each hit: ``{chunk_id, document_title, heading, text, score}``
    with ``score`` = cosine similarity rounded to 4 decimals, best first.

    The texts are **untrusted** (``content_trust=untrusted_text``): never hand them to a model
    that has tools — only through the quarantine wrapper. Raises :class:`KbError`
    (``invalid_query``, ``no_embedding_profile``, ``embedding_*``)."""
    check_tenant_slug(tenant)
    query = " ".join((query or "").split())
    if not query:
        raise KbError(422, "invalid_query", "the query is empty")
    query = query[:MAX_QUERY_CHARS]
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    ids: list[str] | None = None
    if kb_ids is not None:
        ids = []
        for k in kb_ids:
            try:
                ids.append(str(uuid.UUID(str(k))))
            except (ValueError, TypeError, AttributeError):
                continue  # an unknown id matches nothing
        if not ids:
            return []
    profile = await active_profile()
    if profile is None:
        raise KbError(503, "no_embedding_profile", "no embedding profile is configured (run bootstrap)")
    kb_filter = " AND d.kb_id = ANY(CAST(:kbs AS uuid[]))" if ids is not None else ""
    async with tenant_session(tenant) as s:
        has_docs = (await s.execute(text(
            f"SELECT 1 FROM kb_document d WHERE d.tenant = :t AND d.status = 'ready'{kb_filter} LIMIT 1"),
            {"t": tenant, **({"kbs": ids} if ids is not None else {})})).first()
    if has_docs is None:
        return []
    rows: list[Any] | None = None
    for _attempt in range(3):
        [vec] = await embed_texts(tenant, profile, [query])
        async with tenant_session(tenant) as s:
            # the shared profile lock keeps a concurrent swap from dropping profile.table under us
            await s.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KB_PROFILE_LOCK})
            row = (await s.execute(text(f"SELECT {_PROFILE_COLS} FROM embedding_profile WHERE active"))).first()
            current = _profile(row) if row else None
            if current is None:
                raise KbError(503, "no_embedding_profile", "no embedding profile is configured (run bootstrap)")
            if current.id != profile.id:
                profile = current  # swapped while the query was embedded: embed for the new profile
                continue
            await s.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
            await s.execute(text("SELECT set_config('hnsw.ef_search', '100', true)"))
            rows = (await s.execute(text(
                f"SELECT c.id AS chunk_id, d.title AS document_title, c.heading, c.text, "
                f"       e.embedding <=> CAST(:q AS {profile.vtype}) AS distance "
                f"FROM {profile.table} e "
                "JOIN kb_chunk c ON c.tenant = e.tenant AND c.id = e.chunk_id "
                "JOIN kb_document d ON d.tenant = c.tenant AND d.id = c.document_id "
                f"WHERE e.tenant = :t AND d.status = 'ready'{kb_filter} "
                "ORDER BY distance, c.id LIMIT :k"),
                {"t": tenant, "q": _vector_literal(vec), "k": top_k,
                 **({"kbs": ids} if ids is not None else {})})).all()
        break
    if rows is None:
        raise KbError(503, "profile_changing", "the embedding profile is changing; retry the search")
    hits = []
    for r in rows:
        m = r._mapping
        distance = float(m["distance"]) if m["distance"] is not None and math.isfinite(float(m["distance"])) else 1.0
        hits.append({"chunk_id": str(m["chunk_id"]), "document_title": m["document_title"], "heading": m["heading"],
                     "text": m["text"], "score": round(1.0 - distance, 4)})
    hits.sort(key=lambda h: (-h["score"], h["chunk_id"]))  # relaxed_order iterative scans may be out of order
    return hits


# ───────────────────────────── re-embed ─────────────────────────────


_JOB_COLS = ("id, tenant, scope, from_profile_id, to_profile_id, status, chunks_total, chunks_done, error, "
             "requested_by, created_at, updated_at, finished_at")
_tasks: set[asyncio.Task[None]] = set()


def _job_json(m: Any) -> dict[str, Any]:
    def iso(v: Any) -> str | None:
        return v.isoformat() if v is not None else None

    return {"id": str(m["id"]), "tenant": m["tenant"], "scope": m["scope"], "from_profile_id": m["from_profile_id"],
            "to_profile_id": m["to_profile_id"], "status": m["status"], "chunks_total": int(m["chunks_total"]),
            "chunks_done": int(m["chunks_done"]), "error": m["error"], "requested_by": m["requested_by"],
            "created_at": iso(m["created_at"]), "updated_at": iso(m["updated_at"]),
            "finished_at": iso(m["finished_at"])}


async def _create_job(scope: str, tenant: str | None, actor: str | None, *,
                      from_profile: int | None) -> dict[str, Any]:
    stale_s = get_settings().kb_reembed_stale_seconds
    async with system_session() as s:
        # a job without progress for KB_REEMBED_STALE_SECONDS died with its process
        await s.execute(text(
            "UPDATE kb_reembed_job SET status = 'failed', error = 'interrupted', finished_at = now(), "
            "updated_at = now() "
            "WHERE status = 'running' AND coalesce(tenant, '*') = coalesce(:t, '*') "
            "AND updated_at < now() - make_interval(secs => :stale)"), {"t": tenant, "stale": stale_s})
    try:
        async with system_session() as s:
            row = (await s.execute(text(
                "INSERT INTO kb_reembed_job (tenant, scope, from_profile_id, requested_by) "
                f"VALUES (:t, :scope, :fp, :by) RETURNING {_JOB_COLS}"),
                {"t": tenant, "scope": scope, "fp": from_profile, "by": actor})).one()
    except IntegrityError:
        raise KbError(409, "reembed_running", "a re-embed job is already running") from None
    return _job_json(row._mapping)


async def _job_update(job_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    async with system_session() as s:
        await s.execute(text(f"UPDATE kb_reembed_job SET {sets}, updated_at = now() WHERE id = CAST(:id AS uuid)"),
                        {"id": job_id, **fields})


async def _job_progress(job_id: str, done: int) -> None:
    async with system_session() as s:
        await s.execute(text("UPDATE kb_reembed_job SET chunks_done = chunks_done + :n, updated_at = now() "
                             "WHERE id = CAST(:id AS uuid)"), {"id": job_id, "n": done})


async def _job_finish(job_id: str, status: str, error: str | None = None) -> None:
    async with system_session() as s:
        await s.execute(text("UPDATE kb_reembed_job SET status = :st, error = :e, finished_at = now(), "
                             "updated_at = now() WHERE id = CAST(:id AS uuid)"),
                        {"id": job_id, "st": status, "e": error})


async def list_reembed_jobs(tenant: str, *, platform: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    """The tenant's repair jobs (+ platform profile changes for platform admins), newest first."""
    where = "tenant = :t OR tenant IS NULL" if platform else "tenant = :t"
    async with system_session() as s:
        rows = (await s.execute(text(f"SELECT {_JOB_COLS} FROM kb_reembed_job WHERE {where} "
                                     "ORDER BY created_at DESC LIMIT :n"), {"t": tenant, "n": int(limit)})).all()
    return [_job_json(r._mapping) for r in rows]


_MISSING_SQL = """
SELECT c.id, c.text, c.heading, d.title
FROM kb_chunk c
JOIN kb_document d ON d.tenant = c.tenant AND d.id = c.document_id
WHERE c.tenant = :t AND d.status IN ('ready', 'failed')
  AND NOT EXISTS (SELECT 1 FROM {table} e WHERE e.chunk_id = c.id)
ORDER BY c.id
LIMIT :n
"""


async def _fill_missing(tenant: str, profile: EmbeddingProfile, job_id: str | None) -> int:
    """Embed (with ``tenant``'s key) every chunk of the tenant's ready/failed documents that has no
    vector in ``profile``'s table; returns the number written."""
    written = 0
    seen: set[str] = set()
    while True:
        async with tenant_session(tenant) as s:
            rows = (await s.execute(text(_MISSING_SQL.format(table=profile.table)),
                                    {"t": tenant, "n": FILL_PAGE})).all()
        rows = [r for r in rows if str(r[0]) not in seen]
        if not rows:
            return written
        inputs = [embedding_input(r[3], r[2], r[1]) for r in rows]
        vectors = await embed_texts(tenant, profile, inputs)
        async with tenant_session(tenant) as s:
            await s.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KB_PROFILE_LOCK})
            exists = (await s.execute(text("SELECT status FROM embedding_profile WHERE id = :p"),
                                      {"p": profile.id})).scalar_one_or_none()
            if exists not in ("active", "building"):
                raise KbError(409, "profile_changed", "the embedding profile changed during the re-embed")
            await _insert_vectors(s, tenant, profile, [(str(r[0]), v) for r, v in zip(rows, vectors, strict=True)])
        seen.update(str(r[0]) for r in rows)  # never loop on a chunk deleted concurrently
        written += len(rows)
        if job_id:
            await _job_progress(job_id, len(rows))


async def _mark_repaired(s: AsyncSession, tenant: str | None, profile: EmbeddingProfile) -> int:
    """Failed documents (embedding failures) whose chunks now all have vectors become ready."""
    where = "d.tenant = :t AND " if tenant else ""
    params = {"t": tenant} if tenant else {}
    res = await s.execute(text(
        "UPDATE kb_document d SET status = 'ready', error = NULL, updated_at = now() "
        f"WHERE {where}d.status = 'failed' AND d.chunks > 0 "
        "AND EXISTS (SELECT 1 FROM kb_chunk c WHERE c.tenant = d.tenant AND c.document_id = d.id) "
        f"AND NOT EXISTS (SELECT 1 FROM kb_chunk c WHERE c.tenant = d.tenant AND c.document_id = d.id "
        f"  AND NOT EXISTS (SELECT 1 FROM {profile.table} e WHERE e.chunk_id = c.id)) RETURNING d.id"),
        params)
    return len(res.all())


async def _tenants_with_chunks() -> list[str]:
    async with system_session() as s:
        return [r[0] for r in (await s.execute(text("SELECT DISTINCT tenant FROM kb_chunk ORDER BY tenant"))).all()]


async def _count_chunks(tenant: str | None) -> int:
    async with system_session() as s:
        if tenant:
            return int((await s.execute(text("SELECT count(*) FROM kb_chunk WHERE tenant = :t"),
                                        {"t": tenant})).scalar_one())
        return int((await s.execute(text("SELECT count(*) FROM kb_chunk"))).scalar_one())


async def run_reembed(job_id: str, *, alias: str | None = None, provider_model: str | None = None,
                      dims: int | None = None, tenant: str | None = None) -> dict[str, Any]:
    """Execute a job created by :func:`start_reembed` / :func:`ensure_profile` (awaitable; the API
    runs it as a background task). Raises :class:`KbError` after recording the failure."""
    try:
        if tenant is not None:
            await _run_tenant_repair(job_id, tenant)
        else:
            await _run_platform(job_id, alias=alias, provider_model=provider_model, dims=dims)
    except KbError as exc:
        await _job_finish(job_id, "failed", exc.code)
        raise
    except asyncio.CancelledError:
        await asyncio.shield(_job_finish(job_id, "failed", "cancelled"))
        raise
    except Exception as exc:
        await _job_finish(job_id, "failed", "internal_error")
        log.error("re-embed job failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}")
        raise KbError(500, "internal_error", "the re-embed job failed") from exc
    await _job_finish(job_id, "succeeded")
    async with system_session() as s:
        row = (await s.execute(text(f"SELECT {_JOB_COLS} FROM kb_reembed_job WHERE id = CAST(:id AS uuid)"),
                               {"id": job_id})).one()
    return _job_json(row._mapping)


async def _run_tenant_repair(job_id: str, tenant: str) -> None:
    profile = await active_profile()
    if profile is None:
        raise KbError(503, "no_embedding_profile", "no embedding profile is configured (run bootstrap)")
    await _job_update(job_id, to_profile_id=profile.id, from_profile_id=profile.id,
                      chunks_total=await _count_chunks(tenant))
    # an upload whose process died mid-embedding stays 'processing' forever: after
    # KB_REEMBED_STALE_SECONDS it counts as failed, so this repair fills and flips it like any other
    async with tenant_session(tenant) as s:
        await s.execute(text(
            "UPDATE kb_document SET status = 'failed', error = 'interrupted', updated_at = now() "
            "WHERE tenant = :t AND status = 'processing' AND updated_at < now() - make_interval(secs => :stale)"),
            {"t": tenant, "stale": get_settings().kb_reembed_stale_seconds})
    await _fill_missing(tenant, profile, job_id)
    async with tenant_session(tenant) as s:
        await s.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": KB_PROFILE_LOCK})
        repaired = await _mark_repaired(s, tenant, profile)
    log.info("kb tenant re-embed done", tenant=tenant, job_id=job_id, repaired_documents=repaired)


async def _run_platform(job_id: str, *, alias: str | None, provider_model: str | None, dims: int | None) -> None:
    old = await active_profile()
    alias = alias or (old.alias if old else None)
    if not alias:
        raise KbError(422, "alias_required", "no active profile: give the embedding alias")
    dims = int(dims or (old.dims if old else 0))
    if not dims:
        raise KbError(422, "dims_required", "no active profile: give the embedding dims")
    provider_model = provider_model or (old.provider_model if old and old.alias == alias else alias)
    # only one platform job runs at a time (kb_reembed_job_one_running_uq): any building profile now
    # was left behind by a job that died (crash / OOM) — drop it and its shadow table
    async with system_session() as s:
        orphans = [_profile(r) for r in (await s.execute(text(
            f"SELECT {_PROFILE_COLS} FROM embedding_profile WHERE status = 'building'"))).all()]
    for orphan in orphans:
        log.warning("discarding an abandoned building embedding profile", profile_id=orphan.id, job_id=job_id)
        await _discard_profile(orphan)
    new = await _new_profile(alias, provider_model, dims, active=old is None)
    await _job_update(job_id, to_profile_id=new.id, chunks_total=await _count_chunks(None))
    try:
        for _attempt in range(5):
            for tenant in await _tenants_with_chunks():
                await _fill_missing(tenant, new, job_id)
            if await _swap(old, new):
                log.info("embedding profile swapped", from_profile=old.id if old else None, to_profile=new.id,
                         job_id=job_id)
                return
        raise KbError(503, "swap_not_converged", "documents kept changing during the re-embed; retry")
    except BaseException:
        if old is not None:
            await asyncio.shield(_discard_profile(new))
        raise


async def _swap(old: EmbeddingProfile | None, new: EmbeddingProfile) -> bool:
    """Flip ``new`` active (and retire + drop ``old``) when no chunk of a ready/failed document
    lacks a vector in ``new``'s table. False = more chunks arrived; fill again."""
    async with system_session() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": KB_PROFILE_LOCK})
        status = (await s.execute(text("SELECT status FROM embedding_profile WHERE id = :id"),
                                  {"id": new.id})).scalar_one_or_none()
        if status not in ("building", "active"):  # discarded meanwhile (its job was declared dead)
            raise KbError(409, "profile_changed", "the embedding profile changed during the re-embed")
        missing = (await s.execute(text(
            "SELECT count(*) FROM kb_chunk c JOIN kb_document d ON d.tenant = c.tenant AND d.id = c.document_id "
            f"WHERE d.status IN ('ready', 'failed') AND NOT EXISTS (SELECT 1 FROM {new.table} e "
            "WHERE e.chunk_id = c.id)"))).scalar_one()
        if missing:
            return False
        if old is not None and old.id != new.id:
            await s.execute(text("UPDATE embedding_profile SET active = false, status = 'retired', retired_at = now() "
                                 "WHERE id = :id"), {"id": old.id})
        await s.execute(text("UPDATE embedding_profile SET active = true, status = 'active', activated_at = now() "
                             "WHERE id = :id"), {"id": new.id})
        await _mark_repaired(s, None, new)
        if old is not None and old.id != new.id:
            await s.execute(text(f"DROP TABLE IF EXISTS {old.table}"))
    return True


async def _discard_profile(profile: EmbeddingProfile) -> None:
    try:
        async with system_session() as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": KB_PROFILE_LOCK})
            status = (await s.execute(text("SELECT status FROM embedding_profile WHERE id = :id"),
                                      {"id": profile.id})).scalar_one_or_none()
            if status != "building":
                return
            await s.execute(text("UPDATE embedding_profile SET status = 'failed' WHERE id = :id"), {"id": profile.id})
            await s.execute(text(f"DROP TABLE IF EXISTS {profile.table}"))
    except Exception as exc:  # noqa: BLE001 - cleanup; the job is already failed
        log.warning("discarding a building profile failed", profile_id=profile.id, error=type(exc).__name__)


async def start_reembed(scope: str, tenant: str, actor: str | None, *, alias: str | None = None,
                        provider_model: str | None = None, dims: int | None = None,
                        background: bool = True) -> dict[str, Any]:
    """Create a re-embed job and run it (a background task by default). ``scope='tenant'`` repairs
    ``tenant``'s missing vectors in the active profile; ``scope='platform'`` builds a new profile
    version (``alias``/``dims`` default to the active profile's) for every tenant and swaps.
    409 ``reembed_running`` while a job of the same scope runs."""
    if scope not in ("tenant", "platform"):
        raise KbError(422, "invalid_scope", "scope must be tenant or platform")
    if dims is not None:
        vector_type(dims)
    current = await active_profile()
    if scope == "tenant" and current is None:
        raise KbError(503, "no_embedding_profile", "no embedding profile is configured (run bootstrap)")
    job = await _create_job(scope, tenant if scope == "tenant" else None, actor,
                            from_profile=current.id if current else None)
    kwargs: dict[str, Any] = ({"tenant": tenant} if scope == "tenant"
                              else {"alias": alias, "provider_model": provider_model, "dims": dims})
    if not background:
        return await run_reembed(job["id"], **kwargs)

    async def runner() -> None:
        try:
            await run_reembed(job["id"], **kwargs)
        except KbError as exc:
            log.warning("re-embed job failed", job_id=job["id"], code=exc.code)

    task = asyncio.create_task(runner(), name=f"kb-reembed-{job['id']}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def wait_for_jobs() -> None:
    """Await the background re-embed tasks of this process (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


async def cancel_jobs() -> None:
    """Shutdown: cancel this process's background re-embed tasks (each records ``failed:
    cancelled``; a building profile is discarded, the active one stays)."""
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# ───────────────────────────── seed (bootstrap) ─────────────────────────────


def seed_dir() -> Path | None:
    configured = get_settings().kb_seed_dir
    candidates = [configured] if configured else []
    candidates.append(Path("/app/kb"))
    parents = Path(__file__).resolve().parents
    if len(parents) > 5:  # a source checkout: <repo>/services/engine/src/keelson_engine/core/kb.py
        candidates.append(parents[5] / "data" / "kb")
    for cand in candidates:
        if cand is not None and cand.is_dir():
            return cand
    return None


async def seed_tenant(tenant: str, directory: Path, *, actor: str = "bootstrap") -> dict[str, Any]:
    """Ingest ``directory/*.md`` into the tenant's seed knowledge base (``KB_SEED_NAME``) **only if
    the tenant has no ready / in-flight document**. Idempotent (sha256); a file that fails is logged
    and retried by the next bootstrap."""
    files = sorted(p for p in directory.glob("*.md") if p.is_file())
    if not files:
        return {"seeded": 0, "skipped": "no_files"}
    if await tenant_has_documents(tenant):
        return {"seeded": 0, "skipped": "not_empty"}
    kb = await get_or_create_kb(tenant, get_settings().kb_seed_name,
                                "Synthetic, fictional seed documents (playbooks, policies, contract extracts).",
                                created_by=actor)
    seeded, failed = 0, []
    for path in files:
        try:
            _, created = await ingest_document(tenant, kb["id"], path.name, path.read_bytes(), created_by=actor)
            seeded += int(created)
        except KbError as exc:
            failed.append({"file": path.name, "code": exc.code})
            log.warning("kb seed document failed", tenant=tenant, file=path.name, code=exc.code)
    return {"seeded": seeded, "failed": failed, "kb_id": kb["id"]}
