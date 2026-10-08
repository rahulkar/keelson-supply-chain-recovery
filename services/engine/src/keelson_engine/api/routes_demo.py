"""``POST /api/demo/reset`` — full demo reset (``docs/contracts/95-m5-remediation.md`` §6).

DEMO_MODE only (404 otherwise); session + CSRF + Origin like every mutation; role
``platform-admin`` or ``demo-presenter`` (403 otherwise; a presenter must also be a member of the
demo tenant, since realm roles are global). Body ``{keep_consents?: bool}``
(``keelson_contracts.demo.DemoResetRequest``, may be empty). Answers ``200`` with the
``DemoResetResult`` step log — also when a step failed (``ok: false``, the failing step says why);
``409 reset_in_progress`` while another reset of the demo tenant runs. See
:mod:`keelson_engine.core.demo_reset` for the steps.

M6 (``docs/contracts/100-m6-signals.md`` §7): ``GET /api/demo/packs`` lists the installed packs with
their replay state for the demo tenant (``{packs: [{id, title, description, status, steps_applied,
steps_total}]}``, same role guards, no CSRF: a read); ``POST /api/demo/packs/{pack}/start|advance|reset`` —
the presenter's scenario-pack controls, proxied to the Signal Hub's ``/v1/demo/packs/...`` with a
service JWT for the demo tenant. Same guards as the reset (DEMO_MODE, session + CSRF + Origin,
platform-admin or demo-presenter who belongs to the demo tenant). Body: an optional JSON object
(<= 4 KiB, e.g. ``{"speed": 60}`` / ``{"steps": 1}``) passed through; the answer is the hub's JSON.
Errors: unknown pack/action ``404``, hub refusal ``409 signal_hub_rejected`` (+ ``hub_status``),
hub down ``503 signal_hub_unavailable``, hub not configured ``409 not_configured``.
"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from keelson_contracts.demo import DemoResetRequest
from keelson_engine.api.deps import CurrentSession, MutatingSession
from keelson_engine.api.errors import ApiError
from keelson_engine.config import get_settings
from keelson_engine.core import demo_reset

__all__ = ["DEMO_RESET_ROLES", "MAX_PACK_BODY_BYTES", "router"]

router = APIRouter()

DEMO_RESET_ROLES = ("platform-admin", "demo-presenter")
MAX_PACK_BODY_BYTES = 4096


def _presenter_tenant(sess: Any, what: str) -> str:
    """DEMO_MODE + presenter role + membership of the demo tenant (platform admins: anywhere)."""
    if not get_settings().demo_mode:
        raise ApiError(404, "not_found", "not found")
    if not sess.has_any(*DEMO_RESET_ROLES):
        raise ApiError(403, "forbidden", f"{what} requires a platform admin or demo presenter")
    try:
        tenant = demo_reset.demo_tenant()
    except demo_reset.StepFailed as exc:
        raise ApiError(409, "not_configured", str(exc)) from exc
    # Realm roles are global: a demo-presenter may only act on a demo tenant they belong to
    # (platform admins may from anywhere).
    if not sess.has_any("platform-admin") and tenant not in sess.tenants:
        raise ApiError(403, "forbidden", "you are not a member of the demo tenant")
    return tenant


@router.post("/api/demo/reset")
async def reset(request: Request, sess: MutatingSession) -> dict[str, Any]:
    tenant = _presenter_tenant(sess, "the demo reset")
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip() else {}
        body = DemoResetRequest.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise ApiError(422, "invalid_request", "body must be {} or {\"keep_consents\": true|false}") from exc
    try:
        result = await demo_reset.reset_demo(keep_consents=body.keep_consents, actor=sess.actor(), tenant=tenant)
    except demo_reset.DemoResetBusy as exc:
        raise ApiError(409, "reset_in_progress", "a demo reset is already running") from exc
    except demo_reset.StepFailed as exc:
        raise ApiError(409, "not_configured", str(exc)) from exc
    return result.model_dump(mode="json")


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _finite(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):  # e.g. 1e400
        raise ValueError("number out of range")
    return value


@router.get("/api/demo/packs")
async def list_packs(sess: CurrentSession) -> dict[str, Any]:
    from keelson_engine.core import signal_hub

    tenant = _presenter_tenant(sess, "demo pack control")
    try:
        packs = await signal_hub.list_demo_packs(tenant)
    except signal_hub.HubNotConfigured as exc:
        raise ApiError(409, "not_configured", "the Signal Hub is not configured") from exc
    except signal_hub.HubRejected as exc:
        raise ApiError(409, "signal_hub_rejected", "the Signal Hub refused to list the packs",
                       hub_status=exc.status) from exc
    except signal_hub.HubError as exc:
        raise ApiError(503, "signal_hub_unavailable", "the Signal Hub is unavailable") from exc
    return {"packs": packs}


@router.post("/api/demo/packs/{pack}/{action}")
async def pack_control(pack: str, action: str, request: Request, sess: MutatingSession) -> dict[str, Any]:
    from keelson_engine.core import alerts, signal_hub
    from keelson_engine.core.repo import admin_audit

    tenant = _presenter_tenant(sess, "demo pack control")
    if action not in signal_hub.DEMO_PACK_ACTIONS or not signal_hub.is_pack_id(pack):
        raise ApiError(404, "not_found", "unknown demo pack or action")
    raw = await request.body()
    if len(raw) > MAX_PACK_BODY_BYTES:
        raise ApiError(413, "payload_too_large", "the body exceeds the size limit")
    try:
        # NaN/Infinity are not JSON: rejected here, not a 500 when the proxy re-encodes the body
        body = json.loads(raw, parse_constant=_no_constant, parse_float=_finite) if raw.strip() else {}
    except ValueError as exc:
        raise ApiError(422, "invalid_request", "body must be a JSON object") from exc
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_request", "body must be a JSON object")
    try:
        out = await signal_hub.demo_pack(tenant, pack, action, body)
    except signal_hub.HubNotConfigured as exc:
        raise ApiError(409, "not_configured", "the Signal Hub is not configured") from exc
    except signal_hub.HubNotFound as exc:
        raise ApiError(404, "not_found", "unknown demo pack") from exc
    except signal_hub.HubRejected as exc:
        raise ApiError(409, "signal_hub_rejected", "the Signal Hub refused this pack action",
                       hub_status=exc.status) from exc
    except signal_hub.HubError as exc:
        raise ApiError(503, "signal_hub_unavailable", "the Signal Hub is unavailable") from exc
    await admin_audit(sess.actor(), f"demo.pack.{action}", f"pack:{pack}", None,
                      {"tenant": tenant, "body": body}, tenant=tenant)
    alerts.poke()  # new signals → alerts: poll now instead of at the next interval
    return out
