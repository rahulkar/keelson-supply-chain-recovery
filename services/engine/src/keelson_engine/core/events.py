"""Durable case event stream: Postgres ``run_event`` (source of truth, ``seq`` = its id)
plus the Valkey stream ``keelson:case:<case_id>`` (low-latency live tail).

Write path (:meth:`EventBus.append`):

1. validate the AG-UI event (``keelson_contracts.agui.validate_event``) and its ``metadata.sc``;
2. ``persist`` → take the per-case advisory lock (:func:`lock_case_stream`; held until commit,
   so ids of one case commit in order even with concurrent writers) and insert into
   ``run_event`` with ``metadata.sc.seq`` = the new id (one statement);
3. ``live`` → ``XADD keelson:case:<id> MAXLEN ~ N`` with fields ``seq`` (``""`` for live-only)
   and ``data`` (the JSON event), plus ``EXPIRE`` so idle cases do not hold memory
   (Valkey runs with ``noeviction``).

Read path (:meth:`EventBus.replay_then_tail`): remember the stream's last entry id, replay
``seq > after`` from Postgres, then ``XREAD BLOCK`` from the remembered id. For entries with a
``seq`` the Valkey message is only a wake-up: events are re-read from Postgres (``seq > last``),
so ordering and completeness come from Postgres even if an ``XADD`` was lost. Live-only
entries (reasoning deltas) are yielded with ``seq = 0`` straight from Valkey. On an idle
timeout, or while Valkey is unreachable, Postgres is polled, so a subscriber never stalls.

Reasoning coalescing (:class:`ReasoningCoalescer`): ``REASONING_MESSAGE_CONTENT`` deltas go
to Valkey only; at ``REASONING_MESSAGE_END`` one aggregated ``REASONING_MESSAGE_CONTENT`` is
persisted (``live=False``), so a replay shows the full thought summary once.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_contracts.agui import ScMetadata, validate_event
from keelson_contracts.runs import case_stream_key
from keelson_engine.config import get_settings
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps
from keelson_engine.core.valkey import get_valkey

__all__ = ["EventBus", "ReasoningCoalescer", "get_event_bus", "last_seq_in", "lock_case_stream", "set_event_bus"]

log = get_logger(__name__)

_REPLAY_PAGE = 500


async def lock_case_stream(session: AsyncSession, case_id: str) -> None:
    """Transaction-scoped advisory lock on the case's event stream (released at commit).

    ``run_event.id`` comes from a sequence, so two concurrent writers of one case (e.g. the
    API's cancel ``RUN_ERROR`` racing the worker) could otherwise commit out of seq order and
    a tailing reader that already passed the higher seq would never deliver the lower one.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('keelson.run_event:' || :c, 0))"), {"c": case_id}
    )


async def last_seq_in(session: AsyncSession, case_id: str) -> int:
    """Highest persisted ``seq`` of the case visible to ``session`` (0 if none)."""
    res = await session.execute(text("SELECT coalesce(max(id), 0) FROM run_event WHERE case_id = :c"), {"c": case_id})
    return int(res.scalar_one())


def _sc(event: Mapping[str, Any]) -> dict[str, Any]:
    try:
        sc = event["metadata"]["sc"]
    except (KeyError, TypeError) as exc:
        raise ValueError("event has no metadata.sc") from exc
    if not isinstance(sc, dict):
        raise ValueError("metadata.sc must be an object")
    return sc


class EventBus:
    def __init__(
        self,
        valkey: Callable[[], Redis] = get_valkey,
        *,
        stream_maxlen: int | None = None,
        stream_ttl_s: int | None = None,
        block_ms: int = 5000,
        poll_interval_s: float = 1.0,
    ) -> None:
        settings = get_settings()
        self._valkey = valkey
        self._maxlen = stream_maxlen or settings.event_stream_maxlen
        self._ttl = stream_ttl_s or settings.event_stream_ttl_seconds
        self._block_ms = block_ms
        self._poll_interval = poll_interval_s

    # ─────────────────────────────── write ───────────────────────────────

    async def append(
        self,
        *,
        tenant: str,
        case_id: str,
        run_id: str | None,
        event: dict[str, Any],
        persist: bool = True,
        live: bool = True,
    ) -> int:
        """Append one AG-UI event; returns its ``seq`` (``0`` for live-only events).

        ``metadata.sc.seq`` is set on the stored/streamed copy; the caller's dict is not mutated.
        """
        if not (persist or live):
            raise ValueError("an event must be persisted, streamed live, or both")
        out = copy.deepcopy(event)
        sc = _sc(out)
        if sc.get("tenantId") != tenant or sc.get("caseId") != case_id:
            raise ValueError("metadata.sc tenantId/caseId do not match the append target")
        sc.pop("seq", None)
        ScMetadata.model_validate(sc)  # stage/prov vocabulary
        validate_event(out)
        etype = str(out.get("type"))
        seq = 0
        if persist:
            async with tenant_session(tenant) as s:
                # Serialise writers of one case until commit, so seq order == commit order for
                # the case (a reader that has seen seq N can never later miss a seq < N).
                await lock_case_stream(s, case_id)
                res = await s.execute(
                    text(
                        """
                        WITH n AS (SELECT nextval(pg_get_serial_sequence('run_event', 'id')) AS id)
                        INSERT INTO run_event (id, tenant, case_id, run_id, type, payload)
                        SELECT n.id, :tenant, :case_id, :run_id, :type,
                               jsonb_set(CAST(:payload AS jsonb), '{metadata,sc,seq}', to_jsonb(n.id))
                        FROM n
                        RETURNING id
                        """
                    ),
                    {"tenant": tenant, "case_id": case_id, "run_id": run_id, "type": etype, "payload": dumps(out)},
                )
                seq = int(res.scalar_one())
            sc["seq"] = seq
        if live:
            await self._publish(case_id, seq, out)
        return seq

    async def _publish(self, case_id: str, seq: int, event: Mapping[str, Any]) -> None:
        key = case_stream_key(case_id)
        try:
            pipe = self._valkey().pipeline(transaction=False)
            pipe.xadd(
                key,
                {"seq": str(seq) if seq else "", "data": json.dumps(event, ensure_ascii=False, allow_nan=False)},
                maxlen=self._maxlen,
                approximate=True,
            )
            pipe.expire(key, self._ttl)
            await pipe.execute()
        except (RedisError, OSError) as exc:
            # Postgres already has persisted events; subscribers fall back to polling.
            log.warning("event live publish failed", case_id=case_id, seq=seq, error=str(exc))

    def coalescer(self) -> ReasoningCoalescer:
        return ReasoningCoalescer(self)

    # ─────────────────────────────── read ────────────────────────────────

    async def last_seq(self, *, tenant: str, case_id: str) -> int:
        async with tenant_session(tenant) as s:
            return await last_seq_in(s, case_id)

    async def replay(self, *, tenant: str, case_id: str, after: int) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Persisted events with ``seq > after`` in order (paged, short transactions)."""
        cursor = after
        while True:
            async with tenant_session(tenant) as s:
                res = await s.execute(
                    text(
                        "SELECT id, payload FROM run_event WHERE case_id = :c AND id > :after "
                        "ORDER BY id LIMIT :lim"
                    ),
                    {"c": case_id, "after": cursor, "lim": _REPLAY_PAGE},
                )
                rows = res.all()
            for row in rows:
                cursor = int(row[0])
                yield cursor, row[1]
            if len(rows) < _REPLAY_PAGE:
                return

    async def _stream_tip(self, key: str) -> str | None:
        try:
            last = await self._valkey().xrevrange(key, count=1)
        except (RedisError, OSError) as exc:
            log.warning("event stream tip unavailable", key=key, error=str(exc))
            return None
        return str(last[0][0]) if last else "0-0"

    async def replay_then_tail(
        self, *, tenant: str, case_id: str, after: int | None
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Replay ``seq > after`` from Postgres, then follow the case live (never returns
        on its own; the consumer stops iterating when the client disconnects)."""
        key = case_stream_key(case_id)
        last = after or 0
        stream_id = await self._stream_tip(key)
        async for seq, ev in self.replay(tenant=tenant, case_id=case_id, after=last):
            last = seq
            yield seq, ev
        while True:
            if stream_id is None:
                await asyncio.sleep(self._poll_interval)
                stream_id = await self._stream_tip(key)
                async for seq, ev in self.replay(tenant=tenant, case_id=case_id, after=last):
                    last = seq
                    yield seq, ev
                continue
            try:
                resp = await self._valkey().xread({key: stream_id}, count=200, block=self._block_ms)
            except (RedisError, OSError) as exc:
                log.warning("event tail read failed; polling Postgres", key=key, error=str(exc))
                stream_id = None
                continue
            entries = _entries(resp, key)
            if not entries:
                async for seq, ev in self.replay(tenant=tenant, case_id=case_id, after=last):
                    last = seq
                    yield seq, ev
                continue
            for entry_id, fields in entries:
                stream_id = entry_id
                raw_seq = fields.get("seq") or ""
                if raw_seq:
                    if int(raw_seq) > last:
                        async for seq, ev in self.replay(tenant=tenant, case_id=case_id, after=last):
                            last = seq
                            yield seq, ev
                else:
                    try:
                        yield 0, json.loads(fields.get("data") or "{}")
                    except json.JSONDecodeError:
                        log.warning("dropping malformed live event", key=key, entry=entry_id)


def _entries(resp: Any, key: str) -> list[tuple[str, dict[str, str]]]:
    """Entries of ``key`` from an XREAD reply (RESP2: ``[[stream, [(id, fields), ...]], ...]``)."""
    items: list[Any] = []
    for stream_name, stream_items in resp or []:
        if str(stream_name) == key:
            items = stream_items
    return [(str(eid), dict(fields)) for eid, fields in items]


class ReasoningCoalescer:
    """Routes reasoning events: deltas live-only, aggregate persisted once at message end."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._buffers: dict[str, list[str]] = {}
        self._templates: dict[str, dict[str, Any]] = {}

    async def append(self, *, tenant: str, case_id: str, run_id: str | None, event: dict[str, Any]) -> int:
        etype = event.get("type")
        if etype == "REASONING_MESSAGE_CONTENT":
            mid = str(event.get("messageId"))
            self._buffers.setdefault(mid, []).append(str(event.get("delta", "")))
            self._templates[mid] = event
            return await self._bus.append(
                tenant=tenant, case_id=case_id, run_id=run_id, event=event, persist=False, live=True
            )
        if etype == "REASONING_MESSAGE_END":
            mid = str(event.get("messageId"))
            parts = self._buffers.pop(mid, [])
            template = self._templates.pop(mid, None)
            aggregated = "".join(parts)
            if template is not None and aggregated:
                agg = copy.deepcopy(template)
                agg["delta"] = aggregated
                await self._bus.append(
                    tenant=tenant, case_id=case_id, run_id=run_id, event=agg, persist=True, live=False
                )
        return await self._bus.append(tenant=tenant, case_id=case_id, run_id=run_id, event=event)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus
