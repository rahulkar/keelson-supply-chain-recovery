"""Ingest pipeline: dedupe/version → cluster → event aggregate → exposure → alerts.

All writes run in the RLS context of the data they touch (see :mod:`signal_hub.db`). Signal and
event mutations are serialised by one transaction-scoped advisory lock (:data:`CLUSTER_LOCK`) so the
poller and the API can ingest concurrently; alert evaluation takes a per-(tenant, event) lock.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

import asyncpg
from pydantic import ValidationError

from keelson_contracts.llm import NewsExtraction
from keelson_contracts.signals import NodeCounts, TenantNetwork
from keelson_contracts.signals import event_id as make_event_id
from keelson_contracts.signals import signal_id as make_signal_id
from signal_hub import scoring
from signal_hub.admin import event_subscribed
from signal_hub.alerts import AlertChange, evaluate_alert
from signal_hub.cluster import Candidate, MemberRow, SignalFeatures, aggregate_event, choose_event
from signal_hub.db import tx
from signal_hub.exposure import compute_exposures, persist_exposures
from signal_hub.gazetteer import Gazetteer
from signal_hub.logs import get_logger
from signal_hub.models import NewsItemDraft, SignalDraft, clip
from signal_hub.names import match_key
from signal_hub.quarantine import ExtractionOutcome, LlmClient, backoff, normalise_subtype, run_quarantine

__all__ = ["CLUSTER_LOCK", "IngestReport", "Pipeline", "draft_from_extraction"]

log = get_logger(__name__)
CLUSTER_LOCK: Final = 0x5348_434C_5354  # "SHCLST"
NEWS_TTL: Final = dt.timedelta(hours=72)
TEXT_TTL: Final = dt.timedelta(days=14)
EXTRACTION_LEASE: Final = dt.timedelta(minutes=15)
"""How long a claimed news item is hidden from other workers while its guard/extractor calls run."""


@dataclass(slots=True)
class IngestReport:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    invalid: int = 0
    signal_ids: list[str] = field(default_factory=list)
    events: set[str] = field(default_factory=set)
    alert_changes: list[AlertChange] = field(default_factory=list)
    news_queued: int = 0
    news_status: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "new": self.new, "updated": self.updated, "unchanged": self.unchanged, "invalid": self.invalid,
            "signal_ids": self.signal_ids, "events": sorted(self.events),
            "alerts": [{"alert_id": c.alert_id, "change": c.change, "action": c.action} for c in self.alert_changes],
            "news_queued": self.news_queued, "news_status": dict(sorted(self.news_status.items())),
        }

    def merge(self, other: IngestReport) -> None:
        self.new += other.new
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.invalid += other.invalid
        self.signal_ids += other.signal_ids
        self.events |= other.events
        self.alert_changes += other.alert_changes
        self.news_queued += other.news_queued
        for k, v in other.news_status.items():
            self.news_status[k] = self.news_status.get(k, 0) + v


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def draft_from_extraction(item: dict[str, Any], extraction: NewsExtraction, extracted_by: str, gaz: Gazetteer) -> SignalDraft:
    """A news/free-text item + its (validated, unflagged) extraction → an ``inference`` signal draft.

    Mentions only *select* gazetteer entries; the trusted refs of the item context (e.g. the supplier
    code of a Merxa message) come first. Severity is the subtype prior, never an LLM number."""
    subtype = normalise_subtype(extraction.subtype)
    geo_result = gaz.geocode(places=extraction.places, ports=extraction.ports, chokepoints=extraction.chokepoints)
    refs: list[str] = []
    for ref in [*(item.get("context") or {}).get("refs", []), *geo_result.location_refs]:
        if isinstance(ref, str) and ref not in refs:
            refs.append(ref)
    observed = item.get("published_at") or item["retrieved_at"]
    demo = item["origin"] == "demo"
    ttl = NEWS_TTL if item["origin"] == "news" or item["connector_id"].startswith(("rss.", "demo.rss.")) else TEXT_TTL
    raw = clip(f"{item['title']}\n{item.get('snippet') or ''}", 4000)
    summary = f"Extracted {extraction.kind}/{subtype} ({'simulated' if demo else 'inference'})" + (
        f"; locations: {', '.join(refs[:12])}" if refs else "; no known location")
    return SignalDraft(
        tenant=item.get("tenant"),
        connector_id=item["connector_id"],
        native_key=item["native_key"],
        origin=item["origin"],
        kind=extraction.kind,
        subtype=subtype,
        title=clip(item["title"], 300) or "News item",
        summary=clip(summary, 2000),
        raw_text=raw,
        geometry=geo_result.merged_geometry(),
        location_refs=refs[:200],
        starts_at=observed,
        ends_at=None,
        observed_at=observed,
        expires_at=observed + ttl,
        severity_bp=scoring.news_severity_bp(subtype),
        severity_native={"scheme": "extraction_prior", "subtype": subtype,
                         "unresolved_mentions": len(geo_result.unresolved)},
        provenance="simulated" if demo else "inference",
        extracted_by=extracted_by,
        source_name=item["source_name"],
        source_url=item.get("url"),
        license=item["license"],
        authority_bp=int(item["authority_bp"]),
        retrieved_at=item["retrieved_at"],
        native_id=str((item.get("context") or {}).get("native_id") or item["native_key"])[:500],
        authoritative=False,
        pack_id=item.get("pack_id"),
    )


class Pipeline:
    def __init__(self, pool: asyncpg.Pool, gaz: Gazetteer, llm: LlmClient | None = None, *,
                 extraction_max_attempts: int = 8) -> None:
        self.pool = pool
        self.gaz = gaz
        self.llm = llm
        self.extraction_max_attempts = extraction_max_attempts

    # ─────────────────────────── tenants ───────────────────────────
    async def ensure_tenant(self, tenant: str) -> None:
        async with tx(self.pool, None) as conn:
            await conn.execute("INSERT INTO tenant_registry (tenant) VALUES ($1) ON CONFLICT DO NOTHING", tenant)

    async def tenants(self) -> list[str]:
        async with tx(self.pool, None) as conn:
            return [r["tenant"] for r in await conn.fetch("SELECT tenant FROM tenant_registry ORDER BY tenant")]

    async def put_network(self, tenant: str, network: TenantNetwork, now: dt.datetime) -> tuple[int, list[AlertChange]]:
        """Replace the tenant's network snapshot, then re-evaluate every open event for it."""
        await self.ensure_tenant(tenant)
        async with tx(self.pool, tenant) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"network:{tenant}")
            await conn.execute("DELETE FROM network_node WHERE tenant = $1", tenant)
            await conn.execute("DELETE FROM network_lane WHERE tenant = $1", tenant)
            if network.nodes:
                # name_key: the organisation-name match key (124 §1) — only ever compared inside this
                # tenant's RLS context against the public keys a global signal names (OFAC)
                await conn.executemany(
                    """INSERT INTO network_node (tenant, type, code, name, geog, name_key)
                       VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography, $7)""",
                    [(tenant, n.type, n.code, n.name, n.lon, n.lat, match_key(n.name)) for n in network.nodes],
                )
            if network.lanes:
                await conn.executemany(
                    """INSERT INTO network_lane (tenant, code, mode, path)
                       VALUES ($1, $2, $3, ST_GeomFromGeoJSON($4)::geography)""",
                    [(tenant, ln.code, ln.mode, json.dumps({"type": "LineString",
                                                            "coordinates": [[lon, lat] for lon, lat in ln.path]}))
                     for ln in network.lanes],
                )
        async with tx(self.pool, None) as conn:
            version = await conn.fetchval(
                """UPDATE tenant_registry SET network_version = network_version + 1, network_updated_at = $2
                   WHERE tenant = $1 RETURNING network_version""", tenant, now)
        return int(version), await self.refresh_tenant(tenant, now)

    # ─────────────────────────── signals ───────────────────────────
    async def _upsert(self, conn: asyncpg.Connection, draft: SignalDraft, now: dt.datetime) -> tuple[str, bool, str | None, int]:
        """→ (signal id, changed, event id of the previous version, version)."""
        scope = draft.tenant or ""
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                           f"sig:{scope}:{draft.connector_id}:{draft.native_key}")
        current = await conn.fetchrow(
            """SELECT id, version, content_hash, event_id FROM signal
               WHERE coalesce(tenant, '') = $1 AND connector_id = $2 AND native_key = $3 AND is_current""",
            scope, draft.connector_id, draft.native_key,
        )
        content_hash = draft.content_hash()
        if current is not None and current["content_hash"] == content_hash:
            return str(current["id"]), False, _s(current["event_id"]), int(current["version"])
        max_version = await conn.fetchval(
            "SELECT max(version) FROM signal WHERE coalesce(tenant, '') = $1 AND connector_id = $2 AND native_key = $3",
            scope, draft.connector_id, draft.native_key,
        )
        version = int(max_version or 0) + 1
        sid = make_signal_id(draft.connector_id, draft.native_key, version)
        confidence = scoring.signal_confidence_bp(draft.authority_bp, anchored=draft.authoritative,
                                                  observed_at=draft.observed_at, now=now)
        prev_event = _s(current["event_id"]) if current is not None else None
        signal = draft.to_signal(signal_id=sid, version=version, supersedes=_s(current["id"]) if current else None,
                                 confidence_bp=confidence, event_id=prev_event)
        doc = signal.model_dump(mode="json")
        if current is not None:
            await conn.execute("UPDATE signal SET is_current = false WHERE id = $1", current["id"])
        await conn.execute(
            """
            INSERT INTO signal (id, tenant, connector_id, native_key, version, supersedes, is_current, content_hash,
                                origin, kind, subtype, title, provenance, authoritative, severity_bp, confidence_bp,
                                authority_bp, observed_at, starts_at, ends_at, expires_at, location_refs, geog,
                                event_id, pack_id, doc)
            VALUES ($1, $2, $3, $4, $5, $6, true, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                    $21::text[],
                    CASE WHEN $22::text IS NULL THEN NULL ELSE ST_GeomFromGeoJSON($22::text)::geography END,
                    $23, $24, $25)
            """,
            sid, draft.tenant, draft.connector_id, draft.native_key, version, _s(current["id"]) if current else None,
            content_hash, signal.origin, signal.kind, signal.subtype, signal.title, signal.provenance,
            draft.authoritative, signal.severity_bp, signal.confidence_bp, draft.authority_bp, signal.observed_at,
            signal.starts_at, signal.ends_at, signal.expires_at, list(signal.location_refs),
            json.dumps(doc["geometry"]) if doc["geometry"] is not None else None,
            prev_event, draft.pack_id, doc,
        )
        if draft.match_keys:  # 124 §1: public organisation-name keys (OFAC); empty for every other source
            await conn.execute("UPDATE signal SET match_keys = $2::text[] WHERE id = $1",
                               sid, sorted(set(draft.match_keys)))
        return sid, True, prev_event, version

    async def _candidates(self, conn: asyncpg.Connection, sid: str, draft: SignalDraft) -> list[Candidate]:
        if draft.pack_id is not None:
            scope_sql, args = "e.tenant = $2 AND e.pack_id = $3", [draft.tenant, draft.pack_id]
        elif draft.tenant is not None:
            scope_sql, args = "(e.tenant IS NULL OR e.tenant = $2) AND e.pack_id IS NULL AND NOT e.simulated", [draft.tenant]
        else:
            scope_sql, args = "e.tenant IS NULL AND NOT e.simulated", []
        rows = await conn.fetch(
            f"""
            SELECT e.id, e.kind, e.subtype, e.anchor_signal_id, a.connector_id AS anchor_connector,
                   e.window_start, e.window_end, e.last_observed_at,
                   CASE WHEN e.geog IS NULL OR s.geog IS NULL THEN NULL
                        ELSE ST_Distance(e.geog, s.geog) / 1000.0 END AS distance_km,
                   coalesce((SELECT array_agg(DISTINCT r) FROM signal m, unnest(m.location_refs) r
                             WHERE m.event_id = e.id AND m.is_current), '{{}}'::text[]) AS refs
            FROM event e
            JOIN signal s ON s.id = $1
            LEFT JOIN signal a ON a.id = e.anchor_signal_id
            WHERE e.status <> 'resolved' AND e.merged_into IS NULL
              AND e.window_start <= coalesce(s.ends_at, s.starts_at) + interval '72 hours'
              AND greatest(coalesce(e.window_end, e.last_observed_at, e.window_start), e.window_start)
                  >= s.starts_at - interval '72 hours'
              AND {scope_sql}
            """,
            sid, *args,
        )
        return [
            Candidate(
                event_id=str(r["id"]), kind=r["kind"], subtype=r["subtype"], anchored=r["anchor_signal_id"] is not None,
                anchor_connector=r["anchor_connector"], window_start=r["window_start"], window_end=r["window_end"],
                last_observed=r["last_observed_at"],
                distance_km=float(r["distance_km"]) if r["distance_km"] is not None else None,
                refs=frozenset(r["refs"] or ()),
            )
            for r in rows
        ]

    async def _event_tenant(self, conn: asyncpg.Connection, event_id: str) -> tuple[bool, str | None]:
        row = await conn.fetchrow("SELECT tenant FROM event WHERE id = $1", event_id)
        return (row is not None, row["tenant"] if row is not None else None)

    async def recompute_event(self, conn: asyncpg.Connection, event_id: str, event_tenant: str | None,
                              now: dt.datetime) -> asyncpg.Record | None:
        """Re-derive the event row from its member signals (``conn`` in the event's own scope)."""
        rows = await conn.fetch(
            """SELECT id, connector_id, authoritative, is_current, kind, subtype, title, doc->>'summary' AS summary,
                      severity_bp, authority_bp, observed_at, starts_at, ends_at, expires_at, provenance, pack_id,
                      doc->'geometry' AS geometry, doc->'severity_native' AS severity_native, created_at
               FROM signal WHERE event_id = $1 AND tenant IS NOT DISTINCT FROM $2::text""",
            event_id, event_tenant,
        )
        existing = await conn.fetchrow("SELECT merged_into FROM event WHERE id = $1", event_id)
        merged = existing is not None and existing["merged_into"] is not None
        members = [
            MemberRow(
                id=str(r["id"]), connector_id=r["connector_id"], authoritative=r["authoritative"],
                is_current=r["is_current"], kind=r["kind"], subtype=r["subtype"], title=r["title"],
                summary=r["summary"], severity_bp=r["severity_bp"], authority_bp=r["authority_bp"],
                observed_at=r["observed_at"], starts_at=r["starts_at"], ends_at=r["ends_at"],
                expires_at=r["expires_at"], provenance=r["provenance"], pack_id=r["pack_id"],
                geometry=r["geometry"], severity_native=r["severity_native"] or {}, created_at=r["created_at"],
            )
            for r in rows
        ]
        agg = aggregate_event(members, now=now, merged=merged)
        if agg is None:
            if existing is not None:
                await conn.execute("UPDATE event SET status = 'resolved', updated_at = $2 WHERE id = $1", event_id, now)
            return await conn.fetchrow("SELECT * FROM event WHERE id = $1", event_id)
        geometry_json = json.dumps(agg.geometry) if agg.geometry is not None else None
        return await conn.fetchrow(
            """
            INSERT INTO event (id, tenant, kind, subtype, title, summary, anchor_signal_id, geometry, geog,
                               window_start, window_end, severity_bp, confidence_bp, status, taxonomy_key, radius_km,
                               simulated, pack_id, expires_at, last_observed_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::text::jsonb,
                    CASE WHEN $8::text IS NULL THEN NULL ELSE ST_GeomFromGeoJSON($8::text)::geography END,
                    $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
            ON CONFLICT (id) DO UPDATE SET
              kind = EXCLUDED.kind, subtype = EXCLUDED.subtype, title = EXCLUDED.title, summary = EXCLUDED.summary,
              anchor_signal_id = EXCLUDED.anchor_signal_id, geometry = EXCLUDED.geometry, geog = EXCLUDED.geog,
              window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end,
              severity_bp = EXCLUDED.severity_bp, confidence_bp = EXCLUDED.confidence_bp,
              status = CASE WHEN event.merged_into IS NOT NULL THEN 'resolved' ELSE EXCLUDED.status END,
              taxonomy_key = EXCLUDED.taxonomy_key, radius_km = EXCLUDED.radius_km, simulated = EXCLUDED.simulated,
              pack_id = EXCLUDED.pack_id, expires_at = EXCLUDED.expires_at,
              last_observed_at = EXCLUDED.last_observed_at, updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            event_id, event_tenant, agg.kind, agg.subtype, agg.title, agg.summary, agg.anchor_signal_id,
            geometry_json, agg.window_start, agg.window_end, agg.severity_bp, agg.confidence_bp, agg.status,
            agg.taxonomy_key, agg.radius_km, agg.simulated, agg.pack_id, agg.expires_at, agg.last_observed_at, now,
        )

    async def _absorb(self, conn: asyncpg.Connection, anchor_event: str, now: dt.datetime) -> list[str]:
        """Merge open un-anchored events of the same scope that would attach to a new anchor event."""
        anchor = await conn.fetchrow(
            "SELECT id, kind, subtype, tenant, pack_id, window_start, window_end, last_observed_at FROM event WHERE id = $1",
            anchor_event,
        )
        if anchor is None:
            return []
        anchor_refs = frozenset(await conn.fetchval(
            """SELECT coalesce(array_agg(DISTINCT r), '{}'::text[]) FROM signal m, unnest(m.location_refs) r
               WHERE m.event_id = $1 AND m.is_current""", anchor_event) or ())
        rows = await conn.fetch(
            """
            SELECT e.id, e.kind, e.subtype, e.window_start, e.window_end, e.last_observed_at,
                   CASE WHEN e.geog IS NULL OR a.geog IS NULL THEN NULL ELSE ST_Distance(e.geog, a.geog) / 1000.0 END AS distance_km,
                   coalesce((SELECT array_agg(DISTINCT r) FROM signal m, unnest(m.location_refs) r
                             WHERE m.event_id = e.id AND m.is_current), '{}'::text[]) AS refs
            FROM event e JOIN event a ON a.id = $1
            WHERE e.id <> a.id AND e.anchor_signal_id IS NULL AND e.status <> 'resolved' AND e.merged_into IS NULL
              AND e.tenant IS NOT DISTINCT FROM a.tenant AND e.pack_id IS NOT DISTINCT FROM a.pack_id
              AND e.simulated = a.simulated
            ORDER BY e.id
            """,
            anchor_event,
        )
        absorbed: list[str] = []
        target = Candidate(
            event_id=anchor_event, kind=anchor["kind"], subtype=anchor["subtype"], anchored=True, anchor_connector=None,
            window_start=anchor["window_start"], window_end=anchor["window_end"], last_observed=anchor["last_observed_at"],
            distance_km=None, refs=anchor_refs,
        )
        for r in rows:
            features = SignalFeatures(
                kind=r["kind"], subtype=r["subtype"], connector_id="", authoritative=False,
                starts_at=r["window_start"], ends_at=r["window_end"] or r["last_observed_at"], refs=frozenset(r["refs"] or ()),
            )
            cand = replace(target, distance_km=float(r["distance_km"]) if r["distance_km"] is not None else None)
            if choose_event(features, [cand]) != anchor_event:
                continue
            await conn.execute(
                """UPDATE signal SET event_id = $1::uuid, doc = jsonb_set(doc, '{event_id}', to_jsonb($1::text))
                   WHERE event_id = $2 AND tenant IS NOT DISTINCT FROM $3::text""",
                anchor_event, r["id"], anchor["tenant"],
            )
            await conn.execute(
                "UPDATE event SET merged_into = $1, status = 'resolved', updated_at = $3 WHERE id = $2",
                anchor_event, r["id"], now,
            )
            absorbed.append(str(r["id"]))
        return absorbed

    async def ingest(self, drafts: Sequence[SignalDraft], *, now: dt.datetime) -> IngestReport:
        """Store, cluster and evaluate a batch of signal drafts."""
        report = IngestReport()
        global_events: set[str] = set()
        scoped: dict[str, set[str]] = {}  # event -> tenants to refresh (tenant-scoped or tenant-only touch)
        event_scope: dict[str, str | None] = {}
        for draft in drafts:
            try:
                async with tx(self.pool, draft.tenant) as conn:
                    await conn.execute("SELECT pg_advisory_xact_lock($1)", CLUSTER_LOCK)
                    sid, changed, prev_event, version = await self._upsert(conn, draft, now)
                    if not changed:
                        report.unchanged += 1
                        continue
                    report.signal_ids.append(sid)
                    if version == 1:
                        report.new += 1
                    else:
                        report.updated += 1
                    created = False
                    if prev_event is not None:
                        ev_id = prev_event
                        _, ev_tenant = await self._event_tenant(conn, ev_id)
                    else:
                        features = SignalFeatures(
                            kind=draft.kind, subtype=draft.subtype, connector_id=draft.connector_id,
                            authoritative=draft.authoritative, starts_at=draft.starts_at, ends_at=draft.ends_at,
                            refs=frozenset(draft.location_refs),
                        )
                        chosen = choose_event(features, await self._candidates(conn, sid, draft))
                        if chosen is None:
                            ev_id = (make_event_id(draft.connector_id, draft.native_key) if draft.authoritative
                                     else make_event_id("signal", draft.connector_id, draft.native_key))
                            ev_tenant = draft.tenant
                            created = True
                        else:
                            ev_id = chosen
                            _, ev_tenant = await self._event_tenant(conn, ev_id)
                        await conn.execute("UPDATE signal SET event_id = $1::uuid, doc = jsonb_set(doc, '{event_id}', to_jsonb($1::text)) WHERE id = $2",
                                           ev_id, sid)
                    if ev_tenant == draft.tenant:
                        row = await self.recompute_event(conn, ev_id, ev_tenant, now)
                        absorbed = await self._absorb(conn, ev_id, now) if created and row is not None and row["anchor_signal_id"] else []
                        for merged in absorbed:
                            event_scope[merged] = ev_tenant
                            if ev_tenant is None:
                                global_events.add(merged)
                            else:
                                scoped.setdefault(merged, set()).add(ev_tenant)
                    event_scope[ev_id] = ev_tenant
                    report.events.add(ev_id)
                    if ev_tenant is None and draft.tenant is None:
                        global_events.add(ev_id)
                    else:
                        touch = draft.tenant if draft.tenant is not None else ev_tenant
                        if touch is not None:
                            scoped.setdefault(ev_id, set()).add(touch)
            except (ValidationError, ValueError) as exc:
                report.invalid += 1
                log.warning("signal draft rejected", extra={"connector": draft.connector_id, "error": str(exc)[:500]})
        all_tenants: list[str] | None = None
        for ev_id in sorted(global_events):
            if all_tenants is None:
                all_tenants = await self.tenants()
            report.alert_changes += await self.refresh_event(ev_id, None, now, tenants=all_tenants)
        for ev_id in sorted(scoped):
            tenants = sorted(t for t in scoped[ev_id] if t)
            if ev_id in global_events:
                continue
            report.alert_changes += await self.refresh_event(ev_id, event_scope.get(ev_id), now, tenants=tenants)
        return report

    # ─────────────────────────── exposure + alerts ───────────────────────────
    async def refresh_event(self, event_id: str, event_tenant: str | None, now: dt.datetime, *,
                            tenants: Iterable[str] | None = None) -> list[AlertChange]:
        """Recompute exposures + alerts of one event for the given tenants (default: the event's
        tenant, or every registered tenant for a global event)."""
        if tenants is None:
            tenants = [event_tenant] if event_tenant is not None else await self.tenants()
        changes: list[AlertChange] = []
        for tenant in tenants:
            async with tx(self.pool, tenant) as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"alert:{tenant}:{event_id}")
                event = await conn.fetchrow("SELECT * FROM event WHERE id = $1", event_id)
                if event is None:
                    continue
                view: Any = event
                if event["status"] != "resolved" and not await event_subscribed(conn, tenant, event_id):
                    # every member comes from a source this tenant disabled (110 §5): no exposure,
                    # and an open alert resolves as if the event had ended for this tenant
                    view = {**dict(event), "status": "resolved"}
                exposures = await compute_exposures(conn, event_id, tenant) if view["status"] != "resolved" else []
                await persist_exposures(conn, event_id, tenant, exposures, now)
                change = await evaluate_alert(conn, tenant=tenant, event=view, counts=NodeCounts.of(exposures), now=now)
                if change is not None:
                    changes.append(change)
        return changes

    async def refresh_tenant(self, tenant: str, now: dt.datetime, *, pack_id: str | None = None) -> list[AlertChange]:
        """All open events visible to ``tenant`` (+ events with an unresolved alert) for that tenant."""
        async with tx(self.pool, tenant) as conn:
            pack_sql = "AND e.pack_id = $2" if pack_id else "AND (e.pack_id IS NULL OR e.tenant = $1)"
            args: list[Any] = [tenant] + ([pack_id] if pack_id else [])
            rows = await conn.fetch(
                f"""SELECT e.id, e.tenant FROM event e
                    WHERE (e.status <> 'resolved'
                           OR e.id IN (SELECT event_id FROM alert WHERE tenant = $1 AND status <> 'resolved'))
                    {pack_sql}
                    ORDER BY e.id""",
                *args,
            )
        changes: list[AlertChange] = []
        for r in rows:
            changes += await self.refresh_event(str(r["id"]), r["tenant"], now, tenants=[tenant])
        return changes

    async def sweep(self, now: dt.datetime, *, tenant: str | None = None, pack_id: str | None = None) -> list[AlertChange]:
        """Expiry/monitoring transitions. Global events (``tenant=None``, wall clock) or one tenant's
        events (its non-simulated ones, or one demo pack's with the pack's sim clock)."""
        if pack_id is not None:
            where, args = "tenant = $1 AND pack_id = $2", [tenant, pack_id]
        elif tenant is not None:
            where, args = "tenant = $1 AND NOT simulated", [tenant]
        else:
            where, args = "tenant IS NULL AND NOT simulated", []
        changed: list[tuple[str, str | None]] = []
        async with tx(self.pool, tenant) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", CLUSTER_LOCK)
            rows = await conn.fetch(f"SELECT id, status FROM event WHERE status <> 'resolved' AND {where}", *args)
            for r in rows:
                row = await self.recompute_event(conn, str(r["id"]), tenant, now)
                if row is not None and row["status"] != r["status"]:
                    changed.append((str(r["id"]), tenant))
        changes: list[AlertChange] = []
        for ev_id, ev_tenant in changed:
            changes += await self.refresh_event(ev_id, ev_tenant, now)
        return changes

    # ─────────────────────────── news / free text ───────────────────────────
    async def queue_news(self, items: Sequence[NewsItemDraft], *, now: dt.datetime) -> list[str]:
        """Store new items as ``pending_extraction`` (dedupe by native id / canonical URL)."""
        ids: list[str] = []
        for item in items:
            nid = make_event_id("news_item", item.tenant or "", item.connector_id, item.native_key)
            async with tx(self.pool, item.tenant) as conn:
                inserted = await conn.fetchval(
                    """
                    INSERT INTO news_item (id, tenant, connector_id, native_key, origin, source_name, license, authority_bp,
                                           url, title, snippet, published_at, retrieved_at, status, attempts,
                                           next_attempt_at, context, pack_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'pending_extraction', 0, $14, $15, $16)
                    ON CONFLICT DO NOTHING RETURNING id
                    """,
                    nid, item.tenant, item.connector_id, item.native_key, item.origin, item.source_name, item.license,
                    item.authority_bp, item.url, item.title, item.snippet, item.published_at, item.retrieved_at, now,
                    item.context, item.pack_id,
                )
            if inserted is not None:
                ids.append(nid)
        return ids

    async def process_news(self, item_ids: Sequence[str], *, tenant: str | None, now: dt.datetime) -> IngestReport:
        """Run the quarantine for the given items (one tenant scope) and ingest usable results.

        Each item is *claimed* in a short transaction (row lock + a lease on ``next_attempt_at``, so a
        concurrent worker skips it); the guard/extractor calls run outside any transaction, then the
        outcome is written back. A crashed worker's lease simply expires and the item is retried."""
        report = IngestReport()
        for nid in item_ids:
            async with tx(self.pool, tenant) as conn:
                row = await conn.fetchrow(
                    """SELECT * FROM news_item WHERE id = $1 AND tenant IS NOT DISTINCT FROM $2::text
                       AND status = 'pending_extraction' AND (next_attempt_at IS NULL OR next_attempt_at <= $3)
                       FOR UPDATE SKIP LOCKED""",
                    nid, tenant, now,
                )
                if row is None:
                    continue
                await conn.execute("UPDATE news_item SET next_attempt_at = $2 WHERE id = $1", nid, now + EXTRACTION_LEASE)
            item = dict(row)
            context = item.get("context") or {}
            prebaked = context.get("prebaked") if isinstance(context.get("prebaked"), dict) else None
            outcome: ExtractionOutcome = await run_quarantine(
                clip(f"{item['title']}\n{item.get('snippet') or ''}", 4000) or item["title"],
                self.llm,
                prebaked=prebaked,
                attempts_so_far=int(item.get("bad_outputs") or 0),
                max_attempts=self.extraction_max_attempts,
            )
            report.news_status[outcome.status] = report.news_status.get(outcome.status, 0) + 1
            draft: SignalDraft | None = None
            status = outcome.status
            if outcome.status == "extracted" and outcome.extraction is not None:
                try:
                    draft = draft_from_extraction(item, outcome.extraction, outcome.extracted_by or "unknown", self.gaz)
                except (ValueError, KeyError) as exc:
                    status, outcome.error = "rejected", f"draft: {exc}"[:300]
            attempts = int(item["attempts"]) + (0 if prebaked is not None else 1)
            async with tx(self.pool, tenant) as conn:
                await conn.execute(
                    """UPDATE news_item SET status = $2, attempts = $3, next_attempt_at = $4, last_error = $5,
                       guard = $6, extraction = $7, extracted_by = $8, updated_at = $9,
                       bad_outputs = bad_outputs + $10
                       WHERE id = $1 AND status = 'pending_extraction'""",
                    nid, status, attempts,
                    backoff(attempts, now) if status == "pending_extraction" else None,
                    outcome.error, outcome.guard or None,
                    outcome.extraction.model_dump(mode="json") if outcome.extraction is not None else None,
                    outcome.extracted_by, now, 1 if outcome.bad_output else 0,
                )
            if draft is not None:
                sub = await self.ingest([draft], now=now)
                report.merge(sub)
                async with tx(self.pool, tenant) as conn:
                    if sub.invalid:
                        await conn.execute(
                            "UPDATE news_item SET status = 'rejected', last_error = 'signal validation failed' WHERE id = $1", nid)
                    else:
                        sid = await conn.fetchval(
                            """SELECT id FROM signal WHERE coalesce(tenant, '') = $1 AND connector_id = $2
                               AND native_key = $3 AND is_current""",
                            tenant or "", draft.connector_id, draft.native_key,
                        )
                        await conn.execute("UPDATE news_item SET signal_id = $2 WHERE id = $1", nid, sid)
        return report

    async def process_pending(self, *, now: dt.datetime, limit: int = 8) -> IngestReport:
        """Retry due ``pending_extraction`` items of every scope (global + registered tenants)."""
        report = IngestReport()
        if self.llm is None:
            return report
        scopes: list[str | None] = [None, *await self.tenants()]
        for scope in scopes:
            async with tx(self.pool, scope) as conn:
                ids = [str(r["id"]) for r in await conn.fetch(
                    """SELECT id FROM news_item WHERE status = 'pending_extraction'
                       AND tenant IS NOT DISTINCT FROM $1::text AND (next_attempt_at IS NULL OR next_attempt_at <= $2)
                       ORDER BY next_attempt_at NULLS FIRST, id LIMIT $3""",
                    scope, now, limit,
                )]
            if ids:
                report.merge(await self.process_news(ids, tenant=scope, now=now))
        return report

