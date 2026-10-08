"""Signals a ``signal`` run reads (``docs/contracts/95-m5-remediation.md`` §4).

A webhook-routed re-plan (``core.replan.route_event``) stores one ``case_signal`` row per case and
enqueues ``RunRequest {kind: "signal", payload: {signal_id}}`` with ``signal_id`` = that row's id.
:func:`get_signal` is the runtime's read (``runtime.deps.SignalsPort.get_signal``)::

    {id, case_id, connector, event_id, type, kind, data, received_at, run_id}

``data`` holds only the event's **structured** fields as extracted by the router (facts), e.g.
``{event_id, kind: "replan", lane_code, factor_bp, weeks, plan_version, matched_by}``; free text of
the original delivery is never copied. For a ``signal_id`` that is not a case signal (an older
caller passing a ``connector_event`` id or ``webhook-id``) the stored event is returned with its
``data`` object as received (untrusted text in it stays untrusted).

M6 (``docs/contracts/100-m6-signals.md`` §6): a ``signal`` run with ``{event_id, alert_id}`` reads the
Signal Hub event through :func:`get_hub_event` (service JWT for the run's tenant).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from keelson_engine.core.db import tenant_session

__all__ = ["get_hub_event", "get_signal"]


async def get_signal(*, tenant: str, signal_id: str) -> dict[str, Any] | None:
    if not signal_id or len(signal_id) > 200:
        return None
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT id, case_id, connector, event_id, event_type, kind, payload, run_id, created_at "
                 "FROM case_signal WHERE id = :id"),
            {"id": signal_id},
        )
        m = res.mappings().first()
        if m is not None:
            payload = dict(m["payload"] or {})
            return {"id": str(m["id"]), "case_id": m["case_id"], "connector": m["connector"],
                    "event_id": m["event_id"], "type": m["event_type"], "kind": m["kind"], "data": payload,
                    "received_at": m["created_at"], "run_id": m["run_id"]}
        res = await s.execute(
            text("SELECT id, connector, event_id, type, payload, received_at FROM connector_event "
                 "WHERE id::text = :s OR event_id = :s ORDER BY id DESC LIMIT 1"),
            {"s": signal_id},
        )
        e = res.mappings().first()
    if e is None:
        return None
    payload = e["payload"] if isinstance(e["payload"], Mapping) else {}
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    return {"id": str(e["id"]), "case_id": None, "connector": e["connector"], "event_id": e["event_id"],
            "type": e["type"], "kind": None, "data": dict(data), "received_at": e["received_at"], "run_id": None}


async def get_hub_event(*, tenant: str, event_id: str, alert_id: str | None = None) -> dict[str, Any] | None:
    """The Signal Hub event of an M6 ``signal`` run (``None`` when the hub does not know it for this
    tenant). With ``alert_id`` (the alert must belong to the event) the alert's exposures and member
    signals come along::

        {event: DisruptionEvent, alert: Alert | None, exposures: [Exposure], signals: [DisruptionSignal]}

    (JSON dicts, contract-validated). Structured fields (subtype, severity/confidence bp, geometry,
    window, exposures, ``provenance``) are what a deterministic frame may use. Every free-text field
    — event/alert ``title``/``summary``/``why``, signal ``title``/``summary``/``raw_text`` — may be
    news-derived: **untrusted text**, only ever for display or the quarantined extractor. Raises
    :class:`~keelson_engine.core.signal_hub.HubError` (other than not-found) when the hub is down."""
    from keelson_engine.core import signal_hub

    try:
        if alert_id is not None:
            detail = await signal_hub.get_alert(tenant, alert_id)
            if detail.event.id != event_id:
                return None
            return {"event": detail.event.model_dump(mode="json"), "alert": detail.alert.model_dump(mode="json"),
                    "exposures": [e.model_dump(mode="json") for e in detail.exposures],
                    "signals": [x.model_dump(mode="json") for x in detail.signals]}
        event = await signal_hub.get_event(tenant, event_id)
    except signal_hub.HubNotFound:
        return None
    return {"event": event.model_dump(mode="json"), "alert": None, "exposures": [], "signals": []}
