"""Opt-in remote embedding profile against PostGIS + pgvector (docs/contracts/123 §6): bootstrap
extension (optional, pinned), profile tables (vector / halfvec, HNSW, RLS), embedding jobs through a
mock LiteLLM ``/v1/embeddings`` (done / 402 pending with backoff / invalid output failed), the 14-day
backfill after a profile change, the remote-centroid merge and the status reasons. Skipped when the
test server has no pgvector (marker ``pgvector``)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from conftest import T0
from fakes import FakeLlm, MockLiteLlm, vector_for
from keelson_contracts.signals import ClusteringConfigUpdate
from signal_hub import maintenance, vectors
from signal_hub.bootstrap import BootstrapError, _ensure_vector, ensure_database
from signal_hub.clustering_config import load_config, save_config
from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.embeddings import cluster_text
from signal_hub.gazetteer import Gazetteer
from signal_hub.llm_key import LiteLlmAdmin, provision_signals_key
from signal_hub.models import NewsItemDraft
from signal_hub.pipeline import Pipeline
from signal_hub.quarantine import backoff
from test_m7_admin_logic import MemConn

pytestmark = [pytest.mark.postgis, pytest.mark.pgvector, pytest.mark.usefixtures("clean")]
H = dt.timedelta(hours=1)
DIMS = 16


@pytest.fixture(autouse=True)
async def _vector_tables(pool: asyncpg.Pool) -> AsyncIterator[None]:
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'"):
            pytest.skip("pgvector is not installed on the test server")
    yield
    async with pool.acquire() as conn:
        for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE tablename LIKE 'signal\\_vec\\_%'"):
            await conn.execute(f'DROP TABLE IF EXISTS "{r["tablename"]}"')


def _settings(settings: Settings, **over: Any) -> Settings:
    return dataclasses.replace(settings, embed_provider="litellm", embed_alias="embed-default", embed_dims=DIMS,
                               litellm_base_url="http://litellm:4000", **over)


async def _key(mock: MockLiteLlm, settings: Settings) -> str:
    """Provision the hub key through the mock (models include embed-default with provider litellm)."""
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        await provision_signals_key(settings, MemConn(), LiteLlmAdmin(http, base_url=settings.litellm_base_url,  # type: ignore[arg-type]
                                                                      master_key=mock.master_key))
    (info,) = mock.active_keys(settings.llm_key_alias)
    assert info["models"] == ["embed-default", "extract-fast", "guard-default"]
    return next(iter(mock.raw.values()))


def _embedder(mock: MockLiteLlm, key: str | None, dims: int = DIMS) -> vectors.LiteLlmEmbedder:
    return vectors.LiteLlmEmbedder(httpx.AsyncClient(transport=mock.transport()), base_url="http://litellm:4000",
                                   key=lambda: key, alias="embed-default", dims=dims)


def _extract(text: str) -> dict[str, Any]:
    ports = ["Port of Hamburg"] if "Port of Hamburg" in text else ["Hamburg-Waltershof terminal"] if "Waltershof" in text else []
    return {"kind": "labor", "subtype": "strike", "ports": ports}


def _item(title: str, *, native: str, connector: str, published: dt.datetime) -> NewsItemDraft:
    return NewsItemDraft(tenant=None, connector_id=connector, native_key=native, origin="news", source_name=connector,
                         license="headline only", authority_bp=7000, url=f"https://news.example/{native}", title=title,
                         snippet="Union and terminal operators remain far apart after a night of talks in the city.",
                         published_at=published, retrieved_at=published, context={"native_id": native})


async def _ingest(pool: asyncpg.Pool, gaz: Gazetteer, now: dt.datetime) -> Pipeline:
    pipe = Pipeline(pool, gaz, FakeLlm(extract_fn=_extract))
    old = now - dt.timedelta(days=20)
    for title, native, connector, at in (
        ("Dockworkers strike halts container handling at the Port of Hamburg", "a-1", "rss.a", now - 2 * H),
        ("Hamburg-Waltershof terminal workers join the dock strike on the Elbe", "c-1", "rss.b", now - H),
        ("Old strike at the Port of Hamburg ended weeks ago after a pay deal", "old-1", "rss.c", old),
    ):
        ids = await pipe.queue_news([_item(title, native=native, connector=connector, published=at)], now=at)
        await pipe.process_news(ids, tenant=None, now=at)
    return pipe


async def _vector_off(pool: asyncpg.Pool, **remote: int) -> None:
    async with tx(pool, None) as conn:
        cur = await load_config(conn)
        vec = cur.vector.model_copy(update={"enabled": not remote.pop("off", 0)})
        if remote:
            vec = vec.model_copy(update={"remote": vec.remote.model_copy(update=remote)})
        await save_config(conn, ClusteringConfigUpdate.model_validate(
            {"base_version": cur.version, "simhash": cur.simhash.model_dump(mode="json"),
             "vector": vec.model_dump(mode="json")}), T0)


# ─────────────────────────────── bootstrap ───────────────────────────────


async def test_bootstrap_creates_the_extension_optionally(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")

    class NoVector:  # a server without pgvector: a warning, never an error
        async def fetchval(self, sql: str, *args: Any) -> Any:
            return None

        async def execute(self, sql: str, *args: Any) -> str:
            raise AssertionError("must not try to create the extension")

    assert await _ensure_vector(NoVector(), "0.8.5") is False  # type: ignore[arg-type]
    with pytest.raises(BootstrapError):
        await ensure_database("postgresql://x@y/z", role="signals", password="x", database="d", vector_version="0.8; DROP")


async def test_profile_table_jobs_and_remote_merge(pool: asyncpg.Pool, gaz: Gazetteer, settings: Settings) -> None:
    now = T0 + 24 * H
    await _vector_off(pool, off=1)  # ingest without vector attach: the two reports found two events
    pipe = await _ingest(pool, gaz, now)
    mock = MockLiteLlm()
    s = _settings(settings)
    key = await _key(mock, s)
    async with pool.acquire() as conn:
        assert await vectors.ensure_profile(conn, dataclasses.replace(s, embed_provider="local"), now=now) is None
        profile = await vectors.ensure_profile(conn, s, now=now)
        assert profile is not None and (profile.alias, profile.dims, profile.name) == ("embed-default", DIMS, "embed-default@16")
        assert await vectors.ensure_profile(conn, s, now=now) == profile  # idempotent
        col = await conn.fetchval("""SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                                     WHERE attrelid = $1::regclass AND attname = 'embedding'""", profile.table)
        assert col == "vector(16)"
        assert await conn.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = $1", f"{profile.table}_hnsw")
        assert await conn.fetchval("SELECT relforcerowsecurity FROM pg_class WHERE relname = $1", profile.table)
    async with tx(pool, None) as conn:
        _, remote, reason = await vectors.remote_status(conn, s)
    assert remote is None or remote["pending"] == 0
    assert reason == "key_missing"  # settings carry no key (the embedder has its own)
    # one maintenance round: jobs for current signals of the last 14 days, embedded through the gateway
    await _vector_off(pool, attach_bp=5000, merge_bp=5000, strict_bp=5000)
    report = await maintenance.run(pipe, now=now, embedder=_embedder(mock, key))
    assert report["embedded"] == 2 and report["embedding_failed"] == 0
    async with tx(pool, None) as conn:
        jobs = {r["native_key"]: r["status"] for r in await conn.fetch(
            "SELECT s.native_key, j.status FROM embedding_job j JOIN signal s ON s.id = j.signal_id")}
        stored = await conn.fetch(f"SELECT signal_id, embedding::text AS e FROM {profile.table}")
        terms = {r["native_key"]: (list(r["cluster_terms"]), list(r["location_refs"])) for r in await conn.fetch(
            "SELECT native_key, cluster_terms, location_refs FROM signal")}
    assert jobs == {"a-1": "done", "c-1": "done"}  # the 20-day-old signal is outside the backfill window
    assert len(stored) == 2
    assert sorted(mock.embed_inputs) == sorted(cluster_text(*terms[n]) for n in ("a-1", "c-1"))
    assert all("Dockworkers" not in t and "Waltershof terminal workers" not in t for t in mock.embed_inputs)  # no headline
    # the merge pass used the remote centroids (both events fully embedded)
    assert len(report["merged"]) == 1
    async with tx(pool, None) as conn:
        m = await conn.fetchrow("""SELECT m.method, m.similarity_bp, m.similarity_profile FROM signal_membership m
                                   JOIN signal s ON s.id = m.signal_id WHERE s.native_key = 'c-1'""")
    a_vec, c_vec = (vector_for(cluster_text(*terms[n]), DIMS) for n in ("a-1", "c-1"))
    expected = sum(x * y for x, y in zip(a_vec, c_vec, strict=True)) / (math.hypot(*a_vec) * math.hypot(*c_vec))
    assert (m["method"], m["similarity_profile"]) == ("merged_vector", "embed-default@16")
    assert abs(m["similarity_bp"] - round(expected * 10_000)) <= 2  # float4 storage


async def test_gateway_unavailable_and_invalid_output(pool: asyncpg.Pool, gaz: Gazetteer, settings: Settings) -> None:
    now = T0 + 24 * H
    pipe = await _ingest(pool, gaz, now)
    s = _settings(settings)
    mock = MockLiteLlm(embed_status=402)
    key = await _key(mock, s)
    async with pool.acquire() as conn:
        profile = await vectors.ensure_profile(conn, s, now=now)
    assert profile is not None
    await vectors.enqueue(pool, profile, tenant=None, now=now)
    out = await vectors.process_jobs(pool, _embedder(mock, key), profile, tenant=None, now=now)
    assert out == {"embedded": 0, "pending": 2, "failed": 0}
    async with tx(pool, None) as conn:
        rows = await conn.fetch("SELECT status, attempts, bad_outputs, next_attempt_at, last_error FROM embedding_job")
    assert {(r["status"], r["attempts"], r["bad_outputs"]) for r in rows} == {("pending", 1, 0)}
    assert {r["next_attempt_at"] for r in rows} == {backoff(1, now)} and all("402" in r["last_error"] for r in rows)
    # not due yet: nothing is sent
    mock.embed_status = 200
    assert (await vectors.process_jobs(pool, _embedder(mock, key), profile, tenant=None, now=now))["embedded"] == 0
    # wrong dims: invalid output counts towards max_attempts and fails the jobs
    mock.embed_wrong_dims = DIMS + 1
    out = await vectors.process_jobs(pool, _embedder(mock, key), profile, tenant=None, now=now + H, max_attempts=1)
    assert out == {"embedded": 0, "pending": 0, "failed": 2}
    async with tx(pool, None) as conn:
        assert {r["status"] for r in await conn.fetch("SELECT status FROM embedding_job")} == {"failed"}
        _, remote, reason = await vectors.remote_status(conn, dataclasses.replace(s, litellm_key=key))
    assert reason is None and remote == {"alias": "embed-default", "dims": DIMS, "storage": "vector(16)",
                                         "status": "active", "pending": 0}
    del pipe


async def test_profile_change_retires_backfills_and_halfvec(pool: asyncpg.Pool, gaz: Gazetteer, settings: Settings) -> None:
    now = T0 + 24 * H
    await _ingest(pool, gaz, now)
    s = _settings(settings)
    async with pool.acquire() as conn:
        first = await vectors.ensure_profile(conn, s, now=now)
        assert first is not None
    await vectors.enqueue(pool, first, tenant=None, now=now)
    wide = dataclasses.replace(s, embed_dims=2048)
    async with pool.acquire() as conn:
        second = await vectors.ensure_profile(conn, wide, now=now + H)
        assert second is not None and second.id != first.id
        assert await conn.fetchval("SELECT to_regclass($1)", first.table) is None  # dropped
        assert await conn.fetchval("SELECT status FROM embedding_profile WHERE id = $1", first.id) == "retired"
        assert await conn.fetchval("SELECT count(*) FROM embedding_job WHERE profile_id = $1", first.id) == 2
        col = await conn.fetchval("""SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                                     WHERE attrelid = $1::regclass AND attname = 'embedding'""", second.table)
        assert col == "halfvec(2048)"
    assert await vectors.enqueue(pool, second, tenant=None, now=now) == 2  # backfill: the last 14 days only
    mock = MockLiteLlm()
    key = await _key(mock, wide)
    out = await vectors.process_jobs(pool, _embedder(mock, key, dims=2048), second, tenant=None, now=now)
    assert out["embedded"] == 2
    async with tx(pool, None) as conn:
        a_ev = str(await conn.fetchval("SELECT event_id FROM signal WHERE native_key = 'a-1'"))
        c_ev = str(await conn.fetchval("SELECT event_id FROM signal WHERE native_key = 'c-1'"))
        sim = await vectors.centroid_similarity_bp(conn, second, a_ev, c_ev, tenant=None)
    assert sim is not None and 0 <= sim <= 10_000  # avg(halfvec) + cosine distance work on halfvec


async def test_status_reasons_and_local_clustering_without_pgvector(pool: asyncpg.Pool, gaz: Gazetteer, settings: Settings,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(settings, litellm_key="sk-virtual-x")
    async with tx(pool, None) as conn:
        assert (await vectors.remote_status(conn, settings))[2] == "provider_local"
        assert (await vectors.remote_status(conn, s))[2] == "profile_missing"
    monkeypatch.setattr(vectors, "pgvector_available", _false)
    async with tx(pool, None) as conn:
        assert await vectors.remote_status(conn, s) == (False, None, "pgvector_missing")
    async with pool.acquire() as conn:
        assert await vectors.ensure_profile(conn, s, now=T0) is None
    pipe = await _ingest(pool, gaz, T0 + 24 * H)  # local clustering is unaffected
    async with tx(pool, None) as conn:
        methods = sorted(r["method"] for r in await conn.fetch(
            "SELECT m.method FROM signal_membership m JOIN signal s ON s.id = m.signal_id WHERE s.native_key IN ('a-1', 'c-1')"))
    assert methods == ["attach_vector", "founded"]
    report = await maintenance.run(pipe, now=T0 + 24 * H, embedder=_embedder(MockLiteLlm(), "sk-x"))
    assert report == {"embedded": 0, "embedding_failed": 0, "merged": []}


async def _false(conn: Any) -> bool:
    return False
