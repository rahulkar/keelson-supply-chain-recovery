"""Opt-in remote embedding profile (``docs/contracts/123`` §6, plan §14 "Embeddings").

Off by default (``SIGNALS_EMBED_PROVIDER=local``): ingest, replay and the pack validator always use
the deterministic local term vectors (:mod:`signal_hub.embeddings`). With
``SIGNALS_EMBED_PROVIDER=litellm`` **and** the ``vector`` extension in the signals database:

* **Profile** (:func:`ensure_profile`, bootstrap): one ``active`` row in ``embedding_profile`` for
  ``(SIGNALS_EMBED_ALIAS, SIGNALS_EMBED_DIMS)``. A different alias/dims retires the old row and drops
  its table (clustering windows are 72 h, so the 14-day backfill replaces a shadow re-embed).
  Vectors live in ``signal_vec_<id>``: ``vector(d)`` up to 2000 dims, ``halfvec(d)`` up to 4000,
  an HNSW cosine index and the mixed RLS policies of ``signal``. Table names are built from the
  integer profile id only.
* **Jobs** (:func:`enqueue`, :func:`process_jobs`, run by the maintenance pass per scope): current,
  non-simulated signals of the last 14 days get an ``embedding_job``; the API embeds them in
  batches with :class:`LiteLlmEmbedder` (``POST /v1/embeddings`` on the internal LiteLLM, the
  dedicated hub key). Only the structured :func:`~signal_hub.embeddings.cluster_text` is embedded —
  never a headline unless ``SIGNALS_EMBED_INCLUDE_HEADLINE=1``, never a flagged item (flagged items
  never become signals). 401/402/403/408/429/5xx/transport errors keep a job pending with backoff;
  invalid output (count, dims, non-finite values) counts towards ``SIGNALS_EXTRACTION_MAX_ATTEMPTS``.
* **Use**: only the background merge pass compares events by their remote centroids
  (:func:`centroid_similarity_bp`); ingest never waits for the gateway.

Vectors are written as text literals, so the Python pgvector package is not needed.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol

import asyncpg
import httpx

from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.embeddings import cluster_text
from signal_hub.logs import get_logger
from signal_hub.quarantine import LlmBadOutput, LlmUnavailable, backoff

__all__ = [
    "BACKFILL_WINDOW",
    "ActiveProfile",
    "Embedder",
    "LiteLlmEmbedder",
    "active_profile",
    "centroid_similarity_bp",
    "create_table",
    "enqueue",
    "ensure_profile",
    "pgvector_available",
    "process_jobs",
    "remote_status",
    "storage_type",
    "table_name",
    "vector_literal",
]

log = get_logger(__name__)
VECTOR_MAX_DIMS: Final = 2000
HALFVEC_MAX_DIMS: Final = 4000
BACKFILL_WINDOW: Final = dt.timedelta(days=14)
ENQUEUE_LIMIT: Final = 1000
JOB_LEASE: Final = dt.timedelta(minutes=10)
_ALIAS_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True, slots=True)
class ActiveProfile:
    id: int
    alias: str
    dims: int

    @property
    def name(self) -> str:
        """The similarity profile id recorded on memberships (``embed-default@1536``)."""
        return f"{self.alias}@{self.dims}"

    @property
    def table(self) -> str:
        return table_name(self.id)


def storage_type(dims: int) -> str:
    """``vector(d)`` up to 2000 dims, ``halfvec(d)`` up to 4000 (pgvector's HNSW limits)."""
    if isinstance(dims, bool) or not isinstance(dims, int) or not 1 <= dims <= HALFVEC_MAX_DIMS:
        raise ValueError(f"embedding dims must be 1..{HALFVEC_MAX_DIMS}")
    return f"vector({dims})" if dims <= VECTOR_MAX_DIMS else f"halfvec({dims})"


def table_name(profile_id: int) -> str:
    """The vectors table of a profile (integer id only: never user text in an identifier)."""
    if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id < 1:
        raise ValueError("profile id must be a positive integer")
    return f"signal_vec_{profile_id}"


def vector_literal(values: Sequence[float]) -> str:
    """pgvector text input ``[x,y,…]`` (finite floats only)."""
    out = []
    for v in values:
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("embedding values must be finite")
        out.append(repr(f))
    return "[" + ",".join(out) + "]"


async def pgvector_available(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))


async def active_profile(conn: asyncpg.Connection) -> ActiveProfile | None:
    row = await conn.fetchrow("SELECT id, alias, dims FROM embedding_profile WHERE status = 'active'")
    return ActiveProfile(int(row["id"]), str(row["alias"]), int(row["dims"])) if row is not None else None


async def create_table(conn: asyncpg.Connection, profile: ActiveProfile) -> None:
    """The profile's vectors table + HNSW index + mixed RLS (idempotent)."""
    t = profile.table
    typ = storage_type(profile.dims)
    ops = "vector_cosine_ops" if typ.startswith("vector") else "halfvec_cosine_ops"
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {t} (
          signal_id  uuid PRIMARY KEY REFERENCES signal (id) ON DELETE CASCADE,
          tenant     text,
          embedding  {typ} NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        )""")
    await conn.execute(f"CREATE INDEX IF NOT EXISTS {t}_hnsw ON {t} USING hnsw (embedding {ops})")
    await conn.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    await conn.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    for name, sql in (
        ("read", "FOR SELECT USING (tenant IS NULL OR tenant = sh_tenant())"),
        ("insert", "FOR INSERT WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant())"),
        ("update", "FOR UPDATE USING (tenant IS NOT DISTINCT FROM sh_tenant()) WITH CHECK (tenant IS NOT DISTINCT FROM sh_tenant())"),
        ("delete", "FOR DELETE USING (tenant IS NOT DISTINCT FROM sh_tenant())"),
    ):
        await conn.execute(f"DROP POLICY IF EXISTS {t}_{name} ON {t}")
        await conn.execute(f"CREATE POLICY {t}_{name} ON {t} {sql}")


async def ensure_profile(conn: asyncpg.Connection, settings: Settings, *, now: dt.datetime) -> ActiveProfile | None:
    """Bootstrap step (system context, never fatal for the caller): make ``(alias, dims)`` the
    active profile when the provider is ``litellm`` and pgvector exists. ``None`` otherwise."""
    if settings.embed_provider != "litellm":
        return None
    if not await pgvector_available(conn):
        log.warning("SIGNALS_EMBED_PROVIDER=litellm but the vector extension is missing: remote embeddings off")
        return None
    if not _ALIAS_RE.match(settings.embed_alias):
        raise ValueError("SIGNALS_EMBED_ALIAS must be a lowercase LiteLLM alias")
    storage_type(settings.embed_dims)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('embedding_profile', 0))")
        current = await active_profile(conn)
        if current is not None and (current.alias, current.dims) == (settings.embed_alias, settings.embed_dims):
            await create_table(conn, current)
            return current
        if current is not None:
            await conn.execute("UPDATE embedding_profile SET status = 'retired', retired_at = $2 WHERE id = $1",
                               current.id, now)
            await conn.execute(f"DROP TABLE IF EXISTS {current.table}")
            log.info("embedding profile retired", extra={"profile": current.name})
        new_id = await conn.fetchval(
            "INSERT INTO embedding_profile (alias, dims, status, created_at) VALUES ($1, $2, 'active', $3) RETURNING id",
            settings.embed_alias, settings.embed_dims, now)
        profile = ActiveProfile(int(new_id), settings.embed_alias, settings.embed_dims)
        await create_table(conn, profile)
    log.info("embedding profile active", extra={"profile": profile.name, "storage": storage_type(profile.dims)})
    return profile


async def remote_status(conn: asyncpg.Connection, settings: Settings) -> tuple[bool, dict[str, Any] | None, str | None]:
    """``(pgvector, remote profile view | None, reason | None)`` for ``GET /v1/clustering``."""
    pgvector = await pgvector_available(conn)
    profile = await active_profile(conn)
    if settings.embed_provider != "litellm":
        return pgvector, None, "provider_local"
    if not pgvector:
        return pgvector, None, "pgvector_missing"
    if profile is None or (profile.alias, profile.dims) != (settings.embed_alias, settings.embed_dims):
        return pgvector, None, "profile_missing"
    pending = int(await conn.fetchval(
        "SELECT count(*) FROM embedding_job WHERE profile_id = $1 AND status = 'pending'", profile.id))
    view = {"alias": profile.alias, "dims": profile.dims, "storage": storage_type(profile.dims),
            "status": "backfilling" if pending else "active", "pending": pending}
    return pgvector, view, (None if settings.litellm_key else "key_missing")


# ───────────────────────────── embedding jobs ─────────────────────────────


class Embedder(Protocol):
    @property
    def alias(self) -> str: ...

    @property
    def dims(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LiteLlmEmbedder:
    """``POST {LITELLM_BASE_URL}/v1/embeddings`` with the dedicated hub key (read through ``key``,
    so a re-provisioned key is picked up). Validates count, order, dims and finiteness like the
    engine's KB embeddings."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str, key: Callable[[], str | None], alias: str,
                 dims: int, timeout_s: float = 30.0) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._key = key
        self._alias = alias
        self._dims = dims
        self._timeout = timeout_s

    @property
    def alias(self) -> str:
        return self._alias

    @property
    def dims(self) -> int:
        return self._dims

    def __repr__(self) -> str:  # never print the key
        return f"LiteLlmEmbedder(base_url={self._base!r}, alias={self._alias!r}, dims={self._dims})"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        key = self._key()
        if not key:
            raise LlmUnavailable("LITELLM_SIGNALS_KEY is not configured")
        body = {"model": self._alias, "input": list(texts), "dimensions": self._dims, "encoding_format": "float"}
        try:
            resp = await self._client.post(f"{self._base}/v1/embeddings", json=body,
                                           headers={"Authorization": f"Bearer {key}"}, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"gateway unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 400:
            raise LlmUnavailable(f"gateway HTTP {resp.status_code}", status=resp.status_code)
        try:
            data = resp.json()["data"]
            items = sorted(data, key=lambda d: int(d["index"]))
            if len(items) != len(texts) or [int(d["index"]) for d in items] != list(range(len(texts))):
                raise ValueError("embedding count/index mismatch")
            out: list[list[float]] = []
            for d in items:
                vec = d["embedding"]
                if not isinstance(vec, list) or len(vec) != self._dims:
                    raise ValueError("embedding dims mismatch")
                floats = [float(v) for v in vec]
                if not all(math.isfinite(v) for v in floats) or any(isinstance(v, bool) for v in vec):
                    raise ValueError("non-finite embedding value")
                out.append(floats)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmBadOutput(f"embeddings: invalid model output ({type(exc).__name__}: {str(exc)[:80]})") from exc
        return out


async def enqueue(pool: asyncpg.Pool, profile: ActiveProfile, *, tenant: str | None, now: dt.datetime) -> int:
    """Create pending jobs for current, non-simulated signals of scope ``tenant`` observed in the
    last 14 days that have none for the profile (also the backfill after a profile change)."""
    async with tx(pool, tenant) as conn:
        return int(await conn.fetchval(
            """WITH n AS (
                 INSERT INTO embedding_job (signal_id, profile_id, tenant, status, created_at, updated_at)
                 SELECT s.id, $1, s.tenant, 'pending', $2::timestamptz, $2::timestamptz FROM signal s
                 WHERE s.is_current AND s.tenant IS NOT DISTINCT FROM $3::text AND s.pack_id IS NULL
                   AND s.provenance <> 'simulated' AND s.observed_at >= $2::timestamptz - $4::interval
                   AND NOT EXISTS (SELECT 1 FROM embedding_job j WHERE j.signal_id = s.id AND j.profile_id = $1)
                 ORDER BY s.observed_at DESC, s.id
                 LIMIT $5
                 ON CONFLICT DO NOTHING RETURNING 1)
               SELECT count(*) FROM n""",
            profile.id, now, tenant, BACKFILL_WINDOW, ENQUEUE_LIMIT,
        ))


async def process_jobs(pool: asyncpg.Pool, embedder: Embedder, profile: ActiveProfile, *, tenant: str | None,
                       now: dt.datetime, batch: int = 16, max_attempts: int = 8,
                       include_headline: bool = False) -> dict[str, int]:
    """Embed one batch of due jobs of scope ``tenant``. Never raises for gateway problems."""
    async with tx(pool, tenant) as conn:
        rows = await conn.fetch(
            """SELECT j.signal_id, j.attempts, j.bad_outputs, s.cluster_terms, s.location_refs, s.title, s.tenant
               FROM embedding_job j JOIN signal s ON s.id = j.signal_id
               WHERE j.profile_id = $1 AND j.status = 'pending' AND j.tenant IS NOT DISTINCT FROM $2::text
                 AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= $3)
               ORDER BY j.next_attempt_at NULLS FIRST, j.signal_id LIMIT $4
               FOR UPDATE OF j SKIP LOCKED""",
            profile.id, tenant, now, max(1, batch),
        )
        if not rows:
            return {"embedded": 0, "pending": 0, "failed": 0}
        await conn.execute(
            "UPDATE embedding_job SET next_attempt_at = $3 WHERE profile_id = $1 AND signal_id = ANY($2::uuid[])",
            profile.id, [r["signal_id"] for r in rows], now + JOB_LEASE)
    texts = [cluster_text(list(r["cluster_terms"] or ()), list(r["location_refs"] or ()),
                          headline=r["title"] if include_headline else None) for r in rows]
    ids = [r["signal_id"] for r in rows]
    try:
        vectors = await embedder.embed(texts)
    except LlmUnavailable as exc:
        async with tx(pool, tenant) as conn:
            for r in rows:
                attempts = int(r["attempts"]) + 1
                await conn.execute(
                    """UPDATE embedding_job SET attempts = $3, next_attempt_at = $4, last_error = $5, updated_at = $6
                       WHERE profile_id = $1 AND signal_id = $2""",
                    profile.id, r["signal_id"], attempts, backoff(attempts, now), str(exc)[:300], now)
        log.info("embedding jobs deferred (gateway unavailable)", extra={"jobs": len(rows), "status": exc.status})
        return {"embedded": 0, "pending": len(rows), "failed": 0}
    except LlmBadOutput as exc:
        failed = 0
        async with tx(pool, tenant) as conn:
            for r in rows:
                attempts, bad = int(r["attempts"]) + 1, int(r["bad_outputs"]) + 1
                status = "failed" if bad >= max_attempts else "pending"
                failed += status == "failed"
                await conn.execute(
                    """UPDATE embedding_job SET status = $3, attempts = $4, bad_outputs = $5, next_attempt_at = $6,
                         last_error = $7, updated_at = $8 WHERE profile_id = $1 AND signal_id = $2""",
                    profile.id, r["signal_id"], status, attempts, bad,
                    backoff(attempts, now) if status == "pending" else None, str(exc)[:300], now)
        log.warning("embedding output invalid", extra={"jobs": len(rows), "failed": failed})
        return {"embedded": 0, "pending": len(rows) - failed, "failed": failed}
    typ = storage_type(profile.dims).split("(", 1)[0]
    async with tx(pool, tenant) as conn:
        for sid, row, vec in zip(ids, rows, vectors, strict=True):
            await conn.execute(
                f"""INSERT INTO {profile.table} (signal_id, tenant, embedding) VALUES ($1, $2, $3::{typ})
                    ON CONFLICT (signal_id) DO UPDATE SET embedding = EXCLUDED.embedding, created_at = now()""",
                sid, row["tenant"], vector_literal(vec))
        await conn.execute(
            """UPDATE embedding_job SET status = 'done', attempts = attempts + 1, next_attempt_at = NULL,
                 last_error = NULL, updated_at = $3 WHERE profile_id = $1 AND signal_id = ANY($2::uuid[])""",
            profile.id, ids, now)
    return {"embedded": len(rows), "pending": 0, "failed": 0}


async def centroid_similarity_bp(conn: asyncpg.Connection, profile: ActiveProfile, a_event: str, b_event: str, *,
                                 tenant: str | None) -> int | None:
    """Cosine similarity (bp, half-up) of two events' remote centroids (average member vector),
    ``None`` unless every current member of both events (scope ``tenant``) has a vector."""
    t = profile.table
    row = await conn.fetchrow(
        f"""WITH a AS (SELECT count(*) AS n, count(v.signal_id) AS nv, avg(v.embedding) AS c
                       FROM signal s LEFT JOIN {t} v ON v.signal_id = s.id
                       WHERE s.event_id = $1 AND s.is_current AND s.tenant IS NOT DISTINCT FROM $3::text),
                 b AS (SELECT count(*) AS n, count(v.signal_id) AS nv, avg(v.embedding) AS c
                       FROM signal s LEFT JOIN {t} v ON v.signal_id = s.id
                       WHERE s.event_id = $2 AND s.is_current AND s.tenant IS NOT DISTINCT FROM $3::text)
            SELECT a.n AS an, a.nv AS anv, b.n AS bn, b.nv AS bnv,
                   CASE WHEN a.c IS NULL OR b.c IS NULL THEN NULL ELSE 1 - (a.c <=> b.c) END AS sim
            FROM a, b""",
        a_event, b_event, tenant,
    )
    if row is None or not row["an"] or not row["bn"] or row["an"] != row["anv"] or row["bn"] != row["bnv"]:
        return None
    sim = row["sim"]
    if sim is None or not math.isfinite(float(sim)):
        return None
    bp = int((Decimal(repr(float(sim))) * 10_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(0, min(10_000, bp))
