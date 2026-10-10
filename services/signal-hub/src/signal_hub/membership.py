"""Signal memberships (``docs/contracts/123`` §5): why each signal belongs to its event.

One ``signal_membership`` row per signal (every version), written in the same transaction (and RLS
scope) as the assignment it explains:

* :func:`record` — a clustering decision (``Pipeline.ingest``);
* :func:`copy_previous` — a re-versioned signal keeps its previous version's decision;
* :func:`move` — members moved to another event (``absorbed`` by a new anchor, ``merged_vector``
  by the merge pass): the method changes, the original method is kept as ``prior_method``.

Also the read side (:func:`load`, for alert details, event bundles and MCP ``get_event``), the
current members' cluster terms of candidate events (:func:`member_terms`) and the status counts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

import asyncpg
from pydantic import ValidationError

from keelson_contracts.signals import EventMembership
from signal_hub.cluster import Decision
from signal_hub.explain import explain
from signal_hub.gazetteer import Gazetteer
from signal_hub.logs import get_logger

__all__ = ["MOVED", "copy_previous", "counts", "load", "member_terms", "move", "record"]

log = get_logger(__name__)
MOVED: Final = frozenset({"absorbed", "merged_vector"})
_COLUMNS: Final = ("signal_id, tenant, method, prior_method, from_event_id, distance_km, shared_refs, similarity_bp, "
                   "similarity_profile, hamming_bits, duplicate_of, config_version, explanation, decided_at")


async def record(conn: asyncpg.Connection, *, signal_id: str, tenant: str | None, decision: Decision,
                 config_version: int, explanation: str, decided_at: dt.datetime) -> None:
    """Store the decision that placed ``signal_id`` (replaces an earlier row of the same signal)."""
    await conn.execute(
        f"""INSERT INTO signal_membership ({_COLUMNS})
            VALUES ($1, $2, $3, NULL, NULL, $4, $5::text[], $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (signal_id) DO UPDATE SET method = EXCLUDED.method, prior_method = NULL,
              from_event_id = NULL, distance_km = EXCLUDED.distance_km, shared_refs = EXCLUDED.shared_refs,
              similarity_bp = EXCLUDED.similarity_bp, similarity_profile = EXCLUDED.similarity_profile,
              hamming_bits = EXCLUDED.hamming_bits, duplicate_of = EXCLUDED.duplicate_of,
              config_version = EXCLUDED.config_version, explanation = EXCLUDED.explanation,
              decided_at = EXCLUDED.decided_at, recorded_at = now()""",
        signal_id, tenant, decision.method, decision.distance_km, list(decision.shared_refs), decision.similarity_bp,
        decision.similarity_profile, decision.hamming_bits, decision.duplicate_of, config_version, explanation,
        decided_at,
    )


async def copy_previous(conn: asyncpg.Connection, *, new_id: str, prev_id: str) -> bool:
    """A new version of a signal inherits the previous version's membership (same decision, same
    evidence) and its near-duplicate link. False when the previous version has no row (pre-M8)."""
    await conn.execute(
        "UPDATE signal SET near_duplicate_of = (SELECT near_duplicate_of FROM signal WHERE id = $2) WHERE id = $1",
        new_id, prev_id,
    )
    copied = await conn.fetchval(
        f"""WITH c AS (
              INSERT INTO signal_membership ({_COLUMNS})
              SELECT $1, tenant, method, prior_method, from_event_id, distance_km, shared_refs, similarity_bp,
                     similarity_profile, hamming_bits, duplicate_of, config_version, explanation, decided_at
              FROM signal_membership WHERE signal_id = $2
              ON CONFLICT (signal_id) DO NOTHING RETURNING 1)
            SELECT count(*) FROM c""",
        new_id, prev_id,
    )
    return bool(copied)


async def move(conn: asyncpg.Connection, *, from_event: str, tenant: str | None, method: str, gaz: Gazetteer | None,
               decided_at: dt.datetime, config_version: int, anchor_connector: str | None = None,
               similarity_bp: int | None = None, similarity_profile: str | None = None,
               distance_km: int | None = None) -> int:
    """Re-label the memberships of the signals of ``from_event`` (scope ``tenant``) that are about to
    move to another event. Call it **before** the signals' ``event_id`` changes. Returns the rows."""
    rows = await conn.fetch(
        """SELECT m.signal_id, m.method, m.prior_method, m.hamming_bits, m.duplicate_of FROM signal_membership m
           JOIN signal s ON s.id = m.signal_id
           WHERE s.event_id = $1 AND s.tenant IS NOT DISTINCT FROM $2::text""",
        from_event, tenant,
    )
    for r in rows:
        prior = r["prior_method"] if r["method"] in MOVED else r["method"]
        keep_dup = prior == "near_duplicate"
        text = explain(method, gaz=gaz, prior_method=prior, anchor_connector=anchor_connector,
                       similarity_bp=similarity_bp, similarity_profile=similarity_profile)
        await conn.execute(
            """UPDATE signal_membership SET method = $2, prior_method = $3, from_event_id = $4, distance_km = $5,
                 shared_refs = '{}', similarity_bp = $6, similarity_profile = $7, hamming_bits = $8,
                 duplicate_of = $9, config_version = $10, explanation = $11, decided_at = $12, recorded_at = now()
               WHERE signal_id = $1""",
            r["signal_id"], method, prior, from_event, distance_km, similarity_bp, similarity_profile,
            r["hamming_bits"] if keep_dup else None, r["duplicate_of"] if keep_dup else None, config_version,
            text, decided_at,
        )
    return len(rows)


def _membership(row: asyncpg.Record, event_id: str) -> EventMembership | None:
    try:
        return EventMembership.model_validate({
            "signal_id": str(row["signal_id"]), "event_id": event_id, "method": row["method"],
            "prior_method": row["prior_method"],
            "from_event_id": str(row["from_event_id"]) if row["from_event_id"] is not None else None,
            "distance_km": row["distance_km"], "shared_refs": list(row["shared_refs"] or []),
            "similarity_bp": row["similarity_bp"], "similarity_profile": row["similarity_profile"],
            "hamming_bits": row["hamming_bits"],
            "duplicate_of_signal_id": str(row["duplicate_of"]) if row["duplicate_of"] is not None else None,
            "config_version": row["config_version"], "explanation": row["explanation"],
            "decided_at": row["decided_at"],
        })
    except ValidationError as exc:
        log.warning("membership row skipped (does not match the contract)",
                    extra={"signal_id": str(row["signal_id"]), "errors": exc.error_count()})
        return None


async def load(conn: asyncpg.Connection, event_id: str, member_ids: Sequence[str]) -> list[EventMembership]:
    """Memberships of the given member signals of ``event_id`` (in that order; RLS-scoped)."""
    if not member_ids:
        return []
    rows = await conn.fetch(
        """SELECT m.* FROM signal_membership m JOIN signal s ON s.id = m.signal_id
           WHERE m.signal_id = ANY($1::uuid[]) AND s.event_id = $2""",
        list(member_ids), event_id,
    )
    by_id = {str(r["signal_id"]): r for r in rows}
    out: list[EventMembership] = []
    for sid in member_ids:
        row = by_id.get(sid)
        if row is not None and (m := _membership(row, event_id)) is not None:
            out.append(m)
    return out


async def member_terms(conn: asyncpg.Connection, event_ids: Sequence[str], *,
                       tenant: str | None = None, scoped: bool = False) -> dict[str, list[tuple[str, ...]]]:
    """``event id → [cluster terms of each current member]``. ``scoped``: only members of scope
    ``tenant`` (an event's own aggregate); otherwise every member the RLS context can see."""
    if not event_ids:
        return {}
    rows = await conn.fetch(
        """SELECT event_id, cluster_terms FROM signal
           WHERE event_id = ANY($1::uuid[]) AND is_current AND (NOT $2 OR tenant IS NOT DISTINCT FROM $3::text)
           ORDER BY event_id, id""",
        list(event_ids), scoped, tenant,
    )
    out: dict[str, list[tuple[str, ...]]] = {}
    for r in rows:
        out.setdefault(str(r["event_id"]), []).append(tuple(r["cluster_terms"] or ()))
    return out


async def counts(conn: asyncpg.Connection, since: dt.datetime) -> dict[str, Any]:
    """Status counts since ``since`` (wall clock) in the connection's RLS scope: SimHash results,
    reused extractions, current signals' membership methods, embedding jobs."""
    news = await conn.fetchrow(
        """SELECT count(*) FILTER (WHERE near_duplicate_of IS NOT NULL) AS near,
                  count(*) FILTER (WHERE status = 'duplicate') AS dup,
                  count(*) FILTER (WHERE extracted_by LIKE 'simhash:%') AS reused
           FROM news_item WHERE created_at >= $1""",
        since,
    )
    methods = {r["method"]: int(r["n"]) for r in await conn.fetch(
        """SELECT m.method, count(*) AS n FROM signal_membership m JOIN signal s ON s.id = m.signal_id
           WHERE s.is_current AND m.recorded_at >= $1 GROUP BY m.method""", since)}
    jobs = {r["status"]: int(r["n"]) for r in await conn.fetch(  # the active profile's (a retired one's never run)
        """SELECT j.status, count(*) AS n FROM embedding_job j JOIN embedding_profile p ON p.id = j.profile_id
           WHERE p.status = 'active' AND j.status IN ('pending', 'failed') GROUP BY j.status""")}
    out: dict[str, Any] = {
        "simhash_near_duplicates": int(news["near"]), "same_source_duplicates": int(news["dup"]),
        "extractions_reused": int(news["reused"]),
    }
    for method in ("anchor", "same_hazard", "founded", "attach_geo", "attach_ref", "attach_vector", "near_duplicate",
                   "absorbed", "merged_vector"):
        out[method] = methods.get(method, 0)
    out["embeddings_pending"] = jobs.get("pending", 0)
    out["embeddings_failed"] = jobs.get("failed", 0)
    return out
