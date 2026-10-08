"""Connector webhooks → Signal Hub (``docs/contracts/100-m6-signals.md`` §3 "Connector events").

The webhook receiver (``core.webhooks.receive``) stores every verified delivery in
``connector_event`` first; then, for the types in ``SIGNAL_HUB_FORWARD_EVENT_TYPES``
(``supplier_message.received``, ``lane.capacity_changed``, ``sailing.updated``,
``po_line.updated``), :func:`forward_stored` posts it to the hub's
``POST /v1/ingest/connector-event`` with a service JWT whose ``tenant`` claim is the tenant **of the
webhook path** (the only tenant the delivery was authenticated for). The forward is best effort and
never fails the delivery:

* inline with a short timeout (``SIGNAL_HUB_FORWARD_INLINE_TIMEOUT_S``);
* a failed forward stays pending (``connector_event.hub_forwarded_at IS NULL``) and the alert watcher
  of engine-api retries it (:func:`retry_pending`, events of the last 24 h, at most
  :data:`MAX_ATTEMPTS` tries); a hub that *refuses* the event (4xx other than 429) is not retried;
* a duplicate delivery (sender retry) forwards again only while the event is still pending (the hub
  dedupes on ``connector + event_id`` anyway).

The engine does not interpret the free text here: supplier messages reach the hub's quarantined
extractor, never an engine LLM.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger

__all__ = ["MAX_ATTEMPTS", "forward_stored", "forward_types", "retry_pending"]

log = get_logger(__name__)

MAX_ATTEMPTS: Final = 10
GAVE_UP: Final = 1000
"""``hub_forward_attempts`` of an event the hub refused (never retried)."""
RETRY_WINDOW_HOURS: Final = 24


def forward_types() -> frozenset[str]:
    raw = get_settings().signal_hub_forward_event_types
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


async def _forward_row(tenant: str, connector: str, row: Mapping[str, Any], *, timeout: float | None) -> bool:
    from keelson_engine.core import signal_hub

    payload = row["payload"] if isinstance(row["payload"], Mapping) else {}
    try:
        await signal_hub.ingest_connector_event(
            tenant, connector=connector, event_id=str(row["event_id"]), event_type=str(row["type"]),
            received_at=_iso(row["received_at"]), payload=dict(payload), timeout=timeout,
        )
    except signal_hub.HubRejected as exc:
        log.warning("signal hub refused a connector event (not retried)", tenant=tenant, connector=connector,
                    type=row["type"], status=exc.status)
        await _mark(tenant, int(row["id"]), forwarded=False, give_up=True)
        return False
    except signal_hub.HubError as exc:
        log.info("connector event forward pending", tenant=tenant, connector=connector, type=row["type"],
                 error=exc.code)
        await _mark(tenant, int(row["id"]), forwarded=False, give_up=False)
        return False
    await _mark(tenant, int(row["id"]), forwarded=True, give_up=False)
    return True


async def _mark(tenant: str, row_id: int, *, forwarded: bool, give_up: bool) -> None:
    async with tenant_session(tenant) as s:
        if forwarded:
            await s.execute(text("UPDATE connector_event SET hub_forwarded_at = now(), "
                                 "hub_forward_attempts = hub_forward_attempts + 1 WHERE id = :i"), {"i": row_id})
        else:
            await s.execute(
                text("UPDATE connector_event SET hub_forward_attempts = CASE WHEN :g THEN :gave "
                     "ELSE hub_forward_attempts + 1 END WHERE id = :i"),
                {"i": row_id, "g": give_up, "gave": GAVE_UP},
            )


async def forward_stored(tenant: str, connector: str, event_id: str, event_type: str) -> bool:
    """Forward one stored delivery inline (see module docstring). Never raises."""
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured() or event_type not in forward_types():
        return False
    try:
        async with tenant_session(tenant) as s:
            row = (await s.execute(
                text("SELECT id, event_id, type, payload, received_at, hub_forwarded_at, hub_forward_attempts "
                     "FROM connector_event WHERE connector = :c AND event_id = :e"),
                {"c": connector, "e": event_id},
            )).mappings().first()
        if row is None or row["hub_forwarded_at"] is not None or int(row["hub_forward_attempts"]) >= MAX_ATTEMPTS:
            return False
        return await _forward_row(tenant, connector, dict(row),
                                  timeout=get_settings().signal_hub_forward_inline_timeout_s)
    except Exception as exc:  # noqa: BLE001 - the delivery is stored; the watcher retries
        log.warning("connector event forward failed", tenant=tenant, connector=connector, error=type(exc).__name__)
        return False


async def retry_pending(limit: int = 50) -> int:
    """Retry pending forwards (all tenants, oldest first). Returns how many were forwarded."""
    from keelson_engine.core import signal_hub

    types = sorted(forward_types())
    if not signal_hub.is_configured() or not types:
        return 0
    async with system_session() as s:
        rows = (await s.execute(
            text(
                """
                SELECT id, tenant, connector, event_id, type, payload, received_at FROM connector_event
                WHERE hub_forwarded_at IS NULL AND hub_forward_attempts < :max AND type = ANY(:types)
                  AND received_at > now() - make_interval(hours => :h)
                ORDER BY received_at, id LIMIT :n
                """
            ),
            {"max": MAX_ATTEMPTS, "types": types, "h": RETRY_WINDOW_HOURS, "n": max(1, limit)},
        )).mappings().all()
    done = 0
    for row in rows:
        try:
            if await _forward_row(str(row["tenant"]), str(row["connector"]), dict(row), timeout=None):
                done += 1
        except Exception as exc:  # noqa: BLE001 - keep going with the next event
            log.warning("connector event forward retry failed", tenant=row["tenant"], error=type(exc).__name__)
    return done
