"""Standard Webhooks receiver (``docs/contracts/70-backoffice-m2.md`` §5).

``POST /api/webhooks/{tenant}/{connector}``: the tenant comes **from the path only**; the secret
from the connector's ``webhook_secret_ref`` (an env var / ``/run/secrets`` name, e.g.
``MERXA_WEBHOOK_SECRET``). Verification per https://www.standardwebhooks.com:

* ``webhook-signature``: space-separated ``v1,<base64(HMAC-SHA256(key, id + "." + timestamp +
  "." + body))>`` entries, any match (constant-time compare). ``key`` = base64-decoded secret
  after the ``whsec_`` prefix (a secret without the prefix is used as raw UTF-8 bytes);
* ``|now - webhook-timestamp| <= 300 s``;
* dedupe on ``webhook-id`` (``connector_event`` unique ``(tenant, connector, event_id)``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any

from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.secrets import SecretNotFound, read_secret
from keelson_engine.core.sql import dumps

__all__ = ["WebhookRejected", "receive", "sign", "verify_signature", "webhook_key"]

log = get_logger(__name__)

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}_WEBHOOK_SECRET$")
"""Only ``*_WEBHOOK_SECRET`` variables may key a receiver: a mis-configured ref can never turn an
unrelated platform secret (session key, master key, …) into an HMAC oracle."""


class WebhookRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _no_constants(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def webhook_key(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        raw = secret[len("whsec_"):]
        try:
            # Same as the Merxa sender: tolerate a secret whose base64 padding was stripped.
            return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
        except (binascii.Error, ValueError):
            pass
    return secret.encode("utf-8")


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """``v1,<signature>`` (senders/tests)."""
    mac = hmac.new(webhook_key(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode("ascii")


def verify_signature(secret: str, msg_id: str, timestamp: str, body: bytes, header: str) -> bool:
    expected = sign(secret, msg_id, timestamp, body).split(",", 1)[1].encode("ascii")
    for part in header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and sig and hmac.compare_digest(sig.encode("ascii", "ignore"), expected):
            return True
    return False


async def _connector_secret_ref(tenant: str, connector: str) -> str | None:
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT webhook_secret_ref FROM connector WHERE slug = :c AND status <> 'disabled'"),
            {"c": connector},
        )
        return res.scalar_one_or_none()


async def receive(tenant: str, connector: str, headers: dict[str, str], body: bytes) -> bool:
    """Verify + store one delivery. Returns True when stored, False for a duplicate.
    Raises :class:`WebhookRejected`."""
    not_found = WebhookRejected(404, "not_found", "unknown webhook endpoint")
    ref = await _connector_secret_ref(tenant, connector)
    if not ref:
        raise not_found
    if not SECRET_REF_RE.match(ref):
        log.error("webhook secret ref refused", tenant=tenant, connector=connector, secret_ref=ref)
        raise not_found
    try:
        secret = read_secret(ref.lower())
    except (SecretNotFound, ValueError):
        log.error("webhook secret not configured", tenant=tenant, connector=connector, secret_ref=ref)
        raise not_found from None
    msg_id = headers.get("webhook-id", "")
    ts_raw = headers.get("webhook-timestamp", "")
    sig = headers.get("webhook-signature", "")
    if not msg_id or not ts_raw or not sig:
        raise WebhookRejected(400, "missing_headers", "webhook-id, webhook-timestamp and webhook-signature are required")
    if not EVENT_ID_RE.match(msg_id):
        raise WebhookRejected(400, "invalid_webhook_id", "invalid webhook-id")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise WebhookRejected(400, "invalid_timestamp", "webhook-timestamp must be unix seconds") from None
    if abs(time.time() - ts) > get_settings().webhook_tolerance_seconds:
        raise WebhookRejected(401, "timestamp_out_of_range", "webhook-timestamp is too old or in the future")
    if not verify_signature(secret, msg_id, ts_raw, body, sig):
        log.warning("webhook signature mismatch", tenant=tenant, connector=connector)
        raise WebhookRejected(401, "invalid_signature", "signature verification failed")
    try:
        payload = json.loads(body, parse_constant=_no_constants)
    except (ValueError, UnicodeDecodeError):
        raise WebhookRejected(400, "invalid_json", "the body must be a JSON object") from None
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "invalid_json", "the body must be a JSON object")
    event_type = payload.get("type")
    event_type = str(event_type)[:200] if isinstance(event_type, str) and event_type else "unknown"
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                """
                INSERT INTO connector_event (tenant, connector, event_id, type, payload)
                VALUES (:t, :c, :e, :type, CAST(:p AS jsonb))
                ON CONFLICT (tenant, connector, event_id) DO NOTHING
                RETURNING id
                """
            ),
            {"t": tenant, "c": connector, "e": msg_id, "type": event_type, "p": dumps(payload)},
        )
        stored = res.first() is not None
    log.info("webhook received", tenant=tenant, connector=connector, type=event_type, duplicate=not stored)
    if stored:
        await _mark_snapshot_stale(tenant, connector, event_type)
    # M6: forward signal-relevant events to the Signal Hub (tenant = this path's tenant). Best effort,
    # never fails the delivery; a pending forward is retried by engine-api's alert watcher.
    from keelson_engine.core.hub_ingest import forward_stored

    await forward_stored(tenant, connector, msg_id, event_type)
    # Also for a duplicate (a sender retry): routing is idempotent per (case, event), so a retry
    # recovers a routing that failed on an earlier attempt.
    await _route_replan(tenant, connector, msg_id, event_type, payload)
    return stored


async def _route_replan(tenant: str, connector: str, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """M5: plan-relevant structured events (``core.replan``) enqueue ``signal`` re-plan runs on the
    cases whose plans depend on them (idempotent per case + event). A routing failure answers
    ``503 replan_routing_failed`` (the event itself is stored): the sender retries with the same
    ``webhook-id`` and the duplicate delivery routes again, so a transient failure never loses
    the re-plan (at-least-once; Merxa retries 6 times with backoff)."""
    from keelson_engine.core import replan

    if event_type not in replan.enabled_event_types():
        return
    try:
        await replan.route_event(tenant, connector, event_id, event_type, payload)
    except Exception as exc:  # noqa: BLE001 - the event is stored; the sender retries the delivery
        log.error("replan routing failed", tenant=tenant, connector=connector, type=event_type,
                  error=type(exc).__name__)
        raise WebhookRejected(503, "replan_routing_failed",
                              "the event was stored but could not be routed yet; retry the delivery") from exc


async def _mark_snapshot_stale(tenant: str, connector: str, event_type: str) -> None:
    """Data-changing events (``change_set.*``, ``po_line.*``, ``inventory.*``, ``sailing.*``) and source
    resets (``scenario.reset``, any ``merxa.*`` reset event; contract 90 §10) make the
    connector's canonical snapshot stale; the worker syncs it after the debounce
    (``core.scn_sync.mark_stale`` / ``due_syncs``). Never fails the delivery (the event is stored)."""
    from keelson_engine.core import scn_sync

    if not scn_sync.is_stale_event(event_type):
        return
    try:
        await scn_sync.mark_stale(tenant, connector, f"webhook:{event_type}")
    except Exception as exc:  # noqa: BLE001 - the event is stored; the tier-C poll catches up
        log.warning("scn stale mark failed", tenant=tenant, connector=connector, error=type(exc).__name__)
