"""Alerts API for the web (``docs/contracts/100-m6-signals.md`` §6, §8).

All routes: session (+ CSRF/Origin on POST), the active tenant only, roles as below; the Signal
Hub is called with a service JWT for that tenant, so another tenant's alert is a hub 404 → 404.

==============================================  =======================  ==================================
``GET  /api/alerts?status=&severity=&limit=``   read roles               ``{items: [Alert], new_count}``
``GET  /api/alerts/count``                      read roles               ``{new}`` (Home pill)
``GET  /api/alerts/events``                     read roles               SSE: ``CUSTOM sc.alert.*`` (``core.alerts``)
``GET  /api/alerts/{id}``                       read roles               ``AlertDetail`` + ``map`` (``core.alert_map``)
``POST /api/alerts/{id}/open-case``             planner/approver/admin   ``{alert_id, event_id, case_id, …}``
``POST /api/alerts/{id}/dismiss|snooze``        planner/approver/admin   the updated ``Alert``
``POST /api/alerts/{id}/seen``                  read roles               the updated ``Alert``
==============================================  =======================  ==================================

Hub unavailable → ``503 signal_hub_unavailable`` (the inbox shows "alerts unavailable"); hub not
configured → ``503 signal_hub_not_configured``. Alert titles/why texts are passed through as data;
the web renders them as text.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from keelson_contracts.signals import ALERT_SEVERITIES, ALERT_STATUSES, Alert, AlertStatusUpdate
from keelson_engine.api.deps import READ_ROLES, WRITE_ROLES, CurrentSession, MutatingSession, require_any_role
from keelson_engine.api.errors import ApiError
from keelson_engine.api.sessions import session_alive
from keelson_engine.api.shutdown import until_shutdown
from keelson_engine.core import alert_map, alerts, signal_hub

__all__ = ["router"]

router = APIRouter()

SESSION_RECHECK_S = 30.0


def _hub_error(exc: signal_hub.HubError) -> ApiError:
    if isinstance(exc, signal_hub.HubNotFound):
        return ApiError(404, "not_found", "alert not found")
    if isinstance(exc, signal_hub.HubNotConfigured):
        return ApiError(503, "signal_hub_not_configured", "alerts are not configured on this deployment")
    if isinstance(exc, signal_hub.HubRejected):
        return ApiError(409, "signal_hub_rejected", "the Signal Hub refused this change", hub_status=exc.status)
    return ApiError(503, "signal_hub_unavailable", "alerts are temporarily unavailable; try again shortly")


def _alert_id(alert_id: str) -> str:
    if not signal_hub.is_hub_id(alert_id):
        raise ApiError(404, "not_found", "alert not found")
    return alert_id


async def _views(tenant: str, items: list[Alert]) -> list[Alert]:
    mapping = await alerts.cases_for(tenant, [a.id for a in items])
    return [alerts.apply_local(a, mapping.get(a.id)) for a in items]


@router.get("/api/alerts")
async def list_alerts(
    sess: CurrentSession,
    status: Annotated[str | None, Query(max_length=20)] = None,
    severity: Annotated[str | None, Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading alerts")
    if status is not None and status not in ALERT_STATUSES:
        raise ApiError(422, "invalid_status", f"status must be one of {', '.join(ALERT_STATUSES)}")
    if severity is not None and severity not in ALERT_SEVERITIES:
        raise ApiError(422, "invalid_severity", f"severity must be one of {', '.join(ALERT_SEVERITIES)}")
    # "opened" may also be an alert whose case was opened here but not yet synced to the hub, so it
    # is filtered locally (like severity) on the full list
    local_filter = status == "opened" or severity is not None
    fetch = 200 if local_filter else limit
    try:
        items = await signal_hub.list_alerts(tenant, status=None if status == "opened" else status, limit=fetch)
        views = await _views(tenant, items)
        if status is None and len(items) < fetch:
            counted = views  # the complete list: count locally
        else:
            counted = await _views(tenant, await signal_hub.list_alerts(
                tenant, status="new", limit=signal_hub.MAX_LIST_LIMIT))
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from exc
    if status is not None:
        views = [v for v in views if v.status == status]
    if severity is not None:
        views = [v for v in views if v.severity == severity]
    return {"items": [v.model_dump(mode="json") for v in views[:limit]], "new_count": alerts.new_count(counted)}


@router.get("/api/alerts/count")
async def alert_count(sess: CurrentSession) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading alerts")
    try:
        items = await signal_hub.list_alerts(tenant, status="new", limit=signal_hub.MAX_LIST_LIMIT)
        views = await _views(tenant, items)
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from exc
    return {"new": alerts.new_count(views)}


class _AlertStream(BaseModel):
    tenant: str
    after: str | None
    session_id: str


async def _alert_stream(
    sess: CurrentSession,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> _AlertStream:
    tenant = require_any_role(sess, READ_ROLES, "following alerts")
    if not signal_hub.is_configured():
        raise ApiError(503, "signal_hub_not_configured", "alerts are not configured on this deployment")
    after = last_event_id.strip() if last_event_id else None
    return _AlertStream(tenant=tenant, after=after or None, session_id=sess.id)


@router.get("/api/alerts/events", response_class=EventSourceResponse)
async def alert_events(request: Request,
                       target: Annotated[_AlertStream, Depends(_alert_stream)]) -> AsyncIterator[ServerSentEvent]:
    """Tenant-level live channel: on connect one ``sc.alert.count`` (fresh from the hub, skipped while
    it is unavailable), then the tenant's alert stream from ``Last-Event-ID`` (a stream entry id;
    otherwise from now). Keep-alive comments every 15 s (FastAPI); ends on logout/revocation (re-check
    every 30 s) and at once on process shutdown."""
    # the resume point is fixed before anything is sent: nothing published after the client got
    # its first frame can be missed
    start = target.after or await alerts.stream_tip(target.tenant)
    yield ServerSentEvent(retry=3000, comment="keelson alerts")
    try:
        items = await signal_hub.list_alerts(target.tenant, status="new", limit=signal_hub.MAX_LIST_LIMIT)
        count = alerts.new_count(await _views(target.tenant, items))
        event = alerts.custom_event(alerts.ALERT_COUNT, {"new": count})
        yield ServerSentEvent(raw_data=json.dumps(event, separators=(",", ":")), event="message")
    except signal_hub.HubError:
        pass
    checked = time.monotonic()
    async for entry_id, event in until_shutdown(alerts.tail(target.tenant, start)):
        if time.monotonic() - checked >= SESSION_RECHECK_S:
            if not await session_alive(target.session_id):
                return
            checked = time.monotonic()
        data = json.dumps(event, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        yield ServerSentEvent(raw_data=data, event="message", id=entry_id)


@router.get("/api/alerts/{alert_id}")
async def get_alert(alert_id: str, sess: CurrentSession) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading alerts")
    _alert_id(alert_id)
    try:
        detail = await signal_hub.get_alert(tenant, alert_id)
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from exc
    view = (await _views(tenant, [detail.alert]))[0]
    out = detail.model_dump(mode="json")
    out["alert"] = view.model_dump(mode="json")
    # the drawer map: footprint + exposed sites / lanes at the tenant's own positions (or null)
    out["map"] = await alert_map.alert_map(tenant, detail)
    return out


@router.post("/api/alerts/{alert_id}/open-case")
async def open_case(alert_id: str, sess: MutatingSession) -> dict[str, Any]:
    """Idempotent: one case per alert (a repeated call returns the same case, ``created: false``)."""
    tenant = require_any_role(sess, WRITE_ROLES, "opening cases from alerts")
    _alert_id(alert_id)
    try:
        result = await alerts.open_case(tenant, alert_id, actor=sess.actor())
    except alerts.AlertNotFound as exc:
        raise ApiError(404, "not_found", "alert not found") from exc
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from exc
    return result.to_json()


_STATUS_FOR: dict[str, Literal["dismissed", "snoozed", "seen"]] = {
    "dismiss": "dismissed", "snooze": "snoozed", "seen": "seen",
}


@router.post("/api/alerts/{alert_id}/{action}")
async def set_status(alert_id: str, action: str, sess: MutatingSession) -> dict[str, Any]:
    if action not in _STATUS_FOR:
        raise ApiError(404, "not_found", "unknown alert action")
    roles = READ_ROLES if action == "seen" else WRITE_ROLES
    tenant = require_any_role(sess, roles, f"marking alerts {_STATUS_FOR[action]}")
    _alert_id(alert_id)
    status = _STATUS_FOR[action]
    try:
        updated = await signal_hub.set_alert_status(tenant, alert_id, AlertStatusUpdate(status=status))
        if updated is None:
            updated = (await signal_hub.get_alert(tenant, alert_id)).alert
    except signal_hub.HubError as exc:
        raise _hub_error(exc) from exc
    view = (await _views(tenant, [updated]))[0]
    if status != "seen":
        from keelson_engine.core.repo import admin_audit

        await admin_audit(sess.actor(), f"alert.{status}", f"alert:{alert_id}", None, {"status": status},
                          tenant=tenant)
    alerts.poke()
    return view.model_dump(mode="json")
