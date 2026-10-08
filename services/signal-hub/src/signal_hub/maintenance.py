"""Background cluster maintenance (``docs/contracts/123`` §4): the vector **merge pass** and the opt-in
remote **embedding jobs**.

* :func:`merge_pass` (one scope: global, a tenant's own events, or one demo pack) compares every
  open un-anchored event of the scope with the other open events of the scope
  (:func:`signal_hub.cluster.choose_merge`: compatible kind, overlapping windows, similarity >=
  ``merge_bp``, the geography gate; anchored targets first, else the newer event merges into the
  older one; an event with an opened case is never merged away; no chains within one pass).
  Similarity is the remote centroid cosine when an active remote profile has a vector for every
  current member of both events, else the local term-vector centroid (sum of the members'
  vectors). A merge moves the source's signals (same scope, like the v1 absorb), labels their
  memberships ``merged_vector``, resolves the source with ``merged_into`` and re-evaluates both
  events' exposure and alerts (the source's alert resolves). Everything runs under
  :data:`~signal_hub.pipeline.CLUSTER_LOCK`.
* :func:`run` — the API loop (``SIGNALS_CLUSTER_MAINTENANCE_S``) and ``poller --once --adapter
  cluster``: per scope (global, then every registered tenant; demo packs only via replay), the
  embedding jobs of the active remote profile (when configured) and then the merge pass.
* Demo replays call :func:`merge_pass` for their (tenant, pack) after every step with the sim
  clock and the local profile only, so packs stay deterministic.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

import asyncpg

from keelson_contracts.signals import LOCAL_SIMILARITY_PROFILE, ClusteringConfig
from signal_hub import embeddings, membership, vectors
from signal_hub.cluster import Candidate, EventFeatures, choose_merge
from signal_hub.clustering_config import load_config
from signal_hub.db import tx
from signal_hub.logs import get_logger
from signal_hub.pipeline import CLUSTER_LOCK, Pipeline

__all__ = ["MAX_EVENTS", "merge_pass", "run"]

log = get_logger(__name__)
MAX_EVENTS: Final = 500


def _scope_sql(tenant: str | None, pack_id: str | None) -> tuple[str, list[Any]]:
    if pack_id is not None:
        return "e.tenant = $1 AND e.pack_id = $2", [tenant, pack_id]
    if tenant is not None:
        return "e.tenant = $1 AND e.pack_id IS NULL AND NOT e.simulated", [tenant]
    return "e.tenant IS NULL AND NOT e.simulated", []


async def _open_case_events(pipeline: Pipeline, tenant: str | None, event_ids: Sequence[str]) -> set[str]:
    """Events (of ``event_ids``) with an alert that a case was opened from, in any tenant that can
    have one (a global event: every registered tenant; else the event's own tenant)."""
    if not event_ids:
        return set()
    tenants = [tenant] if tenant is not None else await pipeline.tenants()
    out: set[str] = set()
    for t in tenants:
        async with tx(pipeline.pool, t) as conn:
            out |= {str(r["event_id"]) for r in await conn.fetch(
                "SELECT event_id FROM alert WHERE tenant = $1 AND case_id IS NOT NULL AND event_id = ANY($2::uuid[])",
                t, list(event_ids))}
    return out


async def _load_events(conn: asyncpg.Connection, tenant: str | None, pack_id: str | None) -> list[asyncpg.Record]:
    scope_sql, args = _scope_sql(tenant, pack_id)
    return list(await conn.fetch(
        f"""SELECT e.id, e.kind, e.subtype, e.anchor_signal_id IS NOT NULL AS anchored, e.window_start, e.window_end,
                   e.last_observed_at, e.geog IS NOT NULL AS located
            FROM event e WHERE e.status <> 'resolved' AND e.merged_into IS NULL AND {scope_sql}
            ORDER BY e.window_start, e.id LIMIT {MAX_EVENTS}""",
        *args,
    ))


async def _features(conn: asyncpg.Connection, ids: list[str], tenant: str | None
                    ) -> tuple[dict[str, dict[str, int]], dict[str, frozenset[str]]]:
    """Per event: the local centroid (sum of current members' term vectors) and the members' refs
    (the event's own scope only, like its aggregate)."""
    rows = await conn.fetch(
        """SELECT event_id, cluster_terms, location_refs FROM signal
           WHERE event_id = ANY($1::uuid[]) AND is_current AND tenant IS NOT DISTINCT FROM $2::text
           ORDER BY event_id, id""",
        ids, tenant,
    )
    vecs: dict[str, list[dict[str, int]]] = {}
    refs: dict[str, set[str]] = {}
    for r in rows:
        ev = str(r["event_id"])
        vecs.setdefault(ev, []).append(embeddings.local_vector(r["cluster_terms"] or ()))
        refs.setdefault(ev, set()).update(r["location_refs"] or ())
    return ({k: embeddings.add_vectors(v) for k, v in vecs.items()},
            {k: frozenset(v) for k, v in refs.items()})


async def _distances(conn: asyncpg.Connection, ids: list[str], max_km: int) -> dict[tuple[str, str], float]:
    rows = await conn.fetch(
        """SELECT a.id AS a, b.id AS b, ST_Distance(a.geog, b.geog) / 1000.0 AS km
           FROM event a JOIN event b ON a.id <> b.id AND ST_DWithin(a.geog, b.geog, $2 * 1000.0)
           WHERE a.id = ANY($1::uuid[]) AND b.id = ANY($1::uuid[])""",
        ids, max_km,
    )
    return {(str(r["a"]), str(r["b"])): float(r["km"]) for r in rows}


async def merge_pass(pipeline: Pipeline, *, now: dt.datetime, tenant: str | None = None, pack_id: str | None = None,
                     profile: vectors.ActiveProfile | None = None) -> list[tuple[str, str]]:
    """One merge pass over one scope; returns ``[(merged source, target)]`` (see the module doc)."""
    async with tx(pipeline.pool, tenant) as conn:
        cfg = await load_config(conn)
        if not cfg.vector.enabled:
            return []
        candidates = [str(r["id"]) for r in await _load_events(conn, tenant, pack_id) if not r["anchored"]]
    if not candidates:
        return []
    blocked = await _open_case_events(pipeline, tenant, candidates)
    merges: list[tuple[str, str]] = []
    async with tx(pipeline.pool, tenant) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", CLUSTER_LOCK)
        cfg = await load_config(conn)
        events = await _load_events(conn, tenant, pack_id)
        ids = [str(r["id"]) for r in events]
        centroids, refs = await _features(conn, ids, tenant)
        distances = await _distances(conn, ids, cfg.vector.max_distance_km)
        located = {str(r["id"]): bool(r["located"]) for r in events}
        merged_src: set[str] = set()
        merged_dst: set[str] = set()
        remote_cache: dict[tuple[str, str], int | None] = {}
        for src in sorted((r for r in events if not r["anchored"]), key=lambda r: (r["window_start"], str(r["id"])),
                          reverse=True):
            sid = str(src["id"])
            if sid in merged_dst or sid in merged_src:
                continue
            features = EventFeatures(
                event_id=sid, kind=src["kind"], subtype=src["subtype"], anchored=False,
                window_start=src["window_start"], window_end=src["window_end"], last_observed=src["last_observed_at"],
                refs=refs.get(sid, frozenset()), open_case=sid in blocked,
            )
            options: list[Candidate] = []
            for t in events:
                tid = str(t["id"])
                if tid == sid or tid in merged_src:
                    continue
                if located[sid] and located[tid]:
                    if (sid, tid) not in distances:
                        continue  # both located but farther apart than max_distance_km
                    distance: float | None = distances[(sid, tid)]
                else:
                    distance = None
                similarity, profile_name = await _similarity(conn, cfg, profile, centroids, sid, tid, tenant,
                                                             remote_cache)
                options.append(Candidate(
                    event_id=tid, kind=t["kind"], subtype=t["subtype"], anchored=bool(t["anchored"]),
                    anchor_connector=None, window_start=t["window_start"], window_end=t["window_end"],
                    last_observed=t["last_observed_at"], distance_km=distance, refs=refs.get(tid, frozenset()),
                    similarity_bp=similarity, similarity_profile=profile_name,
                ))
            decision = choose_merge(features, options, cfg)
            if decision is None or decision.event_id is None:
                continue
            target = decision.event_id
            await membership.move(conn, from_event=sid, tenant=tenant, method="merged_vector", gaz=pipeline.gaz,
                                  decided_at=now, config_version=cfg.version, similarity_bp=decision.similarity_bp,
                                  similarity_profile=decision.similarity_profile, distance_km=decision.distance_km)
            await conn.execute(
                """UPDATE signal SET event_id = $1::uuid, doc = jsonb_set(doc, '{event_id}', to_jsonb($1::text))
                   WHERE event_id = $2 AND tenant IS NOT DISTINCT FROM $3::text""",
                target, sid, tenant)
            await conn.execute("UPDATE event SET merged_into = $1, status = 'resolved', updated_at = $3 WHERE id = $2",
                               target, sid, now)
            await pipeline.recompute_event(conn, target, tenant, now)
            await pipeline.recompute_event(conn, sid, tenant, now)
            merged_src.add(sid)
            merged_dst.add(target)
            merges.append((sid, target))
            log.info("cluster.merge", extra={"source": sid, "target": target, "similarity_bp": decision.similarity_bp,
                                             "profile": decision.similarity_profile, "distance_km": decision.distance_km,
                                             "tenant": tenant, "pack": pack_id})
    for ev in sorted({ev for pair in merges for ev in pair}):
        await pipeline.refresh_event(ev, tenant, now)  # the source's alert resolves, the target's updates
    return merges


async def _similarity(conn: asyncpg.Connection, cfg: ClusteringConfig, profile: vectors.ActiveProfile | None,
                      centroids: dict[str, dict[str, int]], a: str, b: str, tenant: str | None,
                      cache: dict[tuple[str, str], int | None]) -> tuple[int, str]:
    if profile is not None:
        key = (min(a, b), max(a, b))
        if key not in cache:
            cache[key] = await vectors.centroid_similarity_bp(conn, profile, a, b, tenant=tenant)
        remote = cache[key]
        if remote is not None:
            return remote, profile.name
    return embeddings.similarity_bp(centroids.get(a, {}), centroids.get(b, {})), LOCAL_SIMILARITY_PROFILE


async def run(pipeline: Pipeline, *, now: dt.datetime, embedder: vectors.Embedder | None = None, batch: int = 16,
              max_attempts: int = 8, include_headline: bool = False) -> dict[str, Any]:
    """One maintenance round over every non-demo scope (see the module docstring)."""
    profile: vectors.ActiveProfile | None = None
    if embedder is not None:
        async with tx(pipeline.pool, None) as conn:
            if await vectors.pgvector_available(conn):
                profile = await vectors.active_profile(conn)
        if profile is not None and (profile.alias, profile.dims) != (embedder.alias, embedder.dims):
            log.warning("active embedding profile differs from the configured one; re-run the bootstrap",
                        extra={"profile": profile.name, "configured": f"{embedder.alias}@{embedder.dims}"})
            profile = None
    report: dict[str, Any] = {"embedded": 0, "embedding_failed": 0, "merged": []}
    for scope in [None, *await pipeline.tenants()]:
        if profile is not None and embedder is not None:
            await vectors.enqueue(pipeline.pool, profile, tenant=scope, now=now)
            out = await vectors.process_jobs(pipeline.pool, embedder, profile, tenant=scope, now=now, batch=batch,
                                             max_attempts=max_attempts, include_headline=include_headline)
            report["embedded"] += out["embedded"]
            report["embedding_failed"] += out["failed"]
        report["merged"] += await merge_pass(pipeline, now=now, tenant=scope, profile=profile)
    return report
