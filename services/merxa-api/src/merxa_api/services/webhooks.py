"""Outbox-driven webhook delivery (Standard Webhooks, docs/contracts/22-merxa-m2.md §5).

* **Fan-out** — unpublished outbox rows become one ``webhook_delivery`` per active
  subscription that wants the event type (``UNIQUE (subscription_id, event_id)`` dedupes).
  A demo reset (outbox ``scenario.reset``) is delivered as ``merxa.scenario.reset``
  (docs/contracts/90 §10); "all events" (the seeded subscription, ``*``) includes it.
* **Delivery** — due deliveries are leased (``next_attempt_at`` pushed out), POSTed outside
  any transaction and settled: 2xx → ``delivered``; otherwise retried after 1 s, 5 s, 30 s,
  2 min, 10 min; after the 6th failed attempt → ``failed``.
* **Signature** — ``webhook-id`` (= outbox event id, identical on every retry, so receivers
  dedupe), ``webhook-timestamp`` (unix seconds of the attempt) and
  ``webhook-signature: v1,<base64 HMAC-SHA256(key, id.timestamp.body)>`` where ``key`` is
  the base64-decoded part of the ``whsec_…`` secret.

Secrets are stored encrypted (AES-256-GCM, key = HKDF-SHA256 of ``MERXA_DATA_KEY``, falling
back to ``SESSION_SECRET``) and are shown once when a subscription is created.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api import tables as t
from merxa_api.config import Settings
from merxa_api.errors import Problem, not_found
from merxa_api.logs import get_logger
from merxa_api.security.principal import Principal
from merxa_api.services import audit
from merxa_api.services.audit import Attribution
from merxa_api.services.common import jsonable, row_dict
from merxa_api.services.events import (
    INTERNAL_EVENTS,
    M2_WEBHOOK_EVENTS,
    SUBSCRIBABLE_EVENTS,
    WEBHOOK_TYPE_ALIASES,
    webhook_type,
)

log = get_logger(__name__)

BACKOFF_S: tuple[int, ...] = (1, 5, 30, 120, 600)
MAX_ATTEMPTS = len(BACKOFF_S) + 1  # 6
LEASE_S = 60
SEEDED_NAME = "Keelson (demo-retail)"
SUBSCRIBABLE: tuple[str, ...] = SUBSCRIBABLE_EVENTS  # M2 + M5 events + merxa.scenario.reset (M4)


# ───────────────────────── signing (Standard Webhooks) ─────────────────────────
def new_secret() -> str:
    return "whsec_" + base64.b64encode(secrets.token_bytes(24)).decode("ascii")


def secret_key(secret: str) -> bytes:
    raw = secret.removeprefix("whsec_")
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("webhook secret must be 'whsec_<base64>'") from exc


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    to_sign = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(secret_key(secret), to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode("ascii")


# ───────────────────────── secrets at rest ─────────────────────────
class SecretBox:
    AAD = b"merxa:webhook_subscription:secret"

    def __init__(self, material: str) -> None:
        self._aead = AESGCM(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=b"merxa-secret-box", info=b"v1").derive(
                material.encode("utf-8")
            )
        )

    def seal(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), self.AAD)
        return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def open(self, sealed: str) -> str:
        if not sealed.startswith("v1:"):
            raise ValueError("unknown secret format")
        blob = base64.urlsafe_b64decode(sealed[3:])
        return self._aead.decrypt(blob[:12], blob[12:], self.AAD).decode("utf-8")


def _check_events(events: Any) -> list[str]:
    if not isinstance(events, list) or not events:
        raise Problem(400, "invalid_parameter", "events must be a non-empty array")
    allowed = set(SUBSCRIBABLE) | set(INTERNAL_EVENTS) | {"*"}
    bad = [e for e in events if e not in allowed]
    if bad:
        raise Problem(400, "invalid_parameter", f"unknown event types {bad}; allowed: {sorted(allowed)}")
    # An outbox type with a webhook alias is stored under the type the webhook carries.
    return list(dict.fromkeys(WEBHOOK_TYPE_ALIASES.get(e, e) for e in events))


def wants(sub_events: list[str] | None, event_type: str) -> bool:
    """Does a subscription with ``sub_events`` receive outbox events of ``event_type``?
    ``"*"`` means every subscribable type; a pre-M4 row may still list the outbox name of an
    aliased type (``scenario.reset``)."""
    events = sub_events or []
    wtype = webhook_type(event_type)
    return wtype in events or event_type in events or ("*" in events and wtype in SUBSCRIBABLE)


def _check_url(url: Any) -> str:
    try:
        parts = urlsplit(str(url or ""))
        _ = parts.port  # raises for a malformed port
        parsed = httpx.URL(str(url))  # the client must be able to use it (IDNA, brackets, ...)
        _ = parsed.host  # decoded lazily: an invalid A-label (e.g. "xn--") raises only here
    except ValueError, TypeError, httpx.InvalidURL:
        parts = parsed = None
    if (
        parts is None
        or parsed is None
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or len(str(url)) > 2048
    ):
        raise Problem(400, "invalid_parameter", "url must be an http(s) URL")
    return str(url)


@dataclass
class Delivery:
    id: uuid.UUID
    subscription_id: uuid.UUID
    event_id: uuid.UUID
    attempt: int


class WebhookService:
    def __init__(self, engine: AsyncEngine, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.engine = engine
        self.settings = settings
        self.box = SecretBox(settings.data_key or settings.session_secret)
        self._client = http_client
        self._wake = asyncio.Event()

    # ── subscriptions ──
    async def ensure_seeded(self) -> None:
        """Seeded subscription (``KEELSON_WEBHOOK_URL`` + ``MERXA_WEBHOOK_SECRET``), all events."""
        url, secret = self.settings.keelson_webhook_url, self.settings.webhook_secret
        if not url or not secret:
            return
        try:
            secret_key(secret)
        except ValueError:
            log.error("MERXA_WEBHOOK_SECRET is not a 'whsec_<base64>' secret; seeded webhook skipped")
            return
        ws = t.webhook_subscription
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(ws).where(ws.c.name == SEEDED_NAME).with_for_update())).first()
            if row is None:
                await conn.execute(
                    ws.insert().values(
                        name=SEEDED_NAME,
                        url=url,
                        events=list(SUBSCRIBABLE),
                        secret_enc=self.box.seal(secret),
                        active=True,
                        created_by="system",
                    )
                )
                log.info("seeded webhook subscription", extra={"url": url})
                return
            try:
                same = self.box.open(row.secret_enc) == secret
            except Exception:
                same = False
            values: dict[str, Any] = {}
            if not same or row.url != url:
                values.update(url=url, secret_enc=self.box.seal(secret))
            # "All events": a seeded row that still lists every M2 type gains the types added
            # since (M4: merxa.scenario.reset; M5: manual-change and lane events). A row an admin
            # narrowed down is left alone.
            current = list(row.events or [])
            # Compared with the M2 list (not WEBHOOK_EVENTS, which grows: M5 added six types).
            if set(M2_WEBHOOK_EVENTS) <= set(current) and not set(SUBSCRIBABLE) <= set(current):
                values["events"] = list(dict.fromkeys([*current, *SUBSCRIBABLE]))
            if values:
                await conn.execute(update(ws).where(ws.c.id == row.id).values(**values))

    async def list_subscriptions(self) -> dict[str, Any]:
        ws, wd = t.webhook_subscription, t.webhook_delivery
        stats = (
            select(
                wd.c.subscription_id,
                func.count().filter(wd.c.status == "delivered").label("delivered"),
                func.count().filter(wd.c.status == "pending").label("pending"),
                func.count().filter(wd.c.status == "failed").label("failed"),
                func.max(wd.c.delivered_at).label("last_delivered_at"),
            )
            .group_by(wd.c.subscription_id)
            .subquery()
        )
        q = (
            select(
                ws.c.id,
                ws.c.name,
                ws.c.url,
                ws.c.events,
                ws.c.active,
                ws.c.created_by,
                ws.c.created_at,
                ws.c.version,
                func.coalesce(stats.c.delivered, 0).label("delivered"),
                func.coalesce(stats.c.pending, 0).label("pending"),
                func.coalesce(stats.c.failed, 0).label("failed"),
                stats.c.last_delivered_at,
            )
            .select_from(ws.outerjoin(stats, stats.c.subscription_id == ws.c.id))
            .order_by(ws.c.created_at, ws.c.id)
        )
        async with self.engine.connect() as conn:
            return {"items": [row_dict(r) for r in await conn.execute(q)]}

    async def create_subscription(self, principal: Principal, *, name: Any, url: Any, events: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise Problem(400, "invalid_parameter", "name must be 1..100 characters")
        url = _check_url(url)
        events = _check_events(events)
        secret = new_secret()
        ws = t.webhook_subscription
        async with self.engine.begin() as conn:
            exists = (await conn.execute(select(ws.c.id).where(ws.c.name == name.strip()))).first()
            if exists is not None:
                raise Problem(409, "already_exists", f"a subscription named {name!r} exists")
            row = (
                await conn.execute(
                    ws.insert()
                    .values(
                        name=name.strip(),
                        url=url,
                        events=events,
                        secret_enc=self.box.seal(secret),
                        active=True,
                        created_by=principal.username or principal.sub,
                    )
                    .returning(ws.c.id, ws.c.created_at, ws.c.version)
                )
            ).one()
            await audit.write(
                conn,
                Attribution.of(principal),
                entity_type="webhook_subscription",
                entity_ref=str(row.id),
                action="create",
                after={"name": name.strip(), "url": url, "events": events},
            )
        return {
            "id": str(row.id),
            "name": name.strip(),
            "url": url,
            "events": events,
            "active": True,
            "created_at": jsonable(row.created_at),
            "version": row.version,
            "secret": secret,
            "secret_shown_once": True,
        }

    async def update_subscription(
        self, principal: Principal, sub_id: str, expected_version: int, changes: dict[str, Any]
    ) -> dict[str, Any]:
        ws = t.webhook_subscription
        values: dict[str, Any] = {}
        if "url" in changes:
            values["url"] = _check_url(changes["url"])
        if "events" in changes:
            values["events"] = _check_events(changes["events"])
        if "active" in changes:
            if not isinstance(changes["active"], bool):
                raise Problem(400, "invalid_parameter", "active must be a boolean")
            values["active"] = changes["active"]
        if not values:
            raise Problem(400, "invalid_parameter", "nothing to change (url, events, active)")
        sid = _uuid(sub_id, "webhook subscription")
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(ws).where(ws.c.id == sid).with_for_update())).first()
            if row is None:
                raise not_found("webhook subscription", sub_id)
            if row.version != expected_version:
                raise Problem(
                    412,
                    "precondition_failed",
                    f"version is {row.version}, If-Match was {expected_version}",
                    extra={"current_version": row.version},
                )
            new = (await conn.execute(update(ws).where(ws.c.id == sid).values(**values).returning(ws))).one()
            await audit.write(
                conn,
                Attribution.of(principal),
                entity_type="webhook_subscription",
                entity_ref=sub_id,
                action="update",
                before={k: jsonable(getattr(row, k)) for k in values},
                after=jsonable(values),
            )
        return row_dict(new, exclude=("secret_enc", "updated_at"))

    async def delete_subscription(self, principal: Principal, sub_id: str) -> None:
        ws = t.webhook_subscription
        sid = _uuid(sub_id, "webhook subscription")
        async with self.engine.begin() as conn:
            row = (await conn.execute(ws.delete().where(ws.c.id == sid).returning(ws.c.name))).first()
            if row is None:
                raise not_found("webhook subscription", sub_id)
            await audit.write(
                conn,
                Attribution.of(principal),
                entity_type="webhook_subscription",
                entity_ref=sub_id,
                action="delete",
                before={"name": row.name},
            )

    async def list_deliveries(
        self, *, subscription_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        wd, ob = t.webhook_delivery, t.outbox
        q = (
            select(
                wd.c.id,
                wd.c.subscription_id,
                wd.c.event_id,
                wd.c.event_type,
                ob.c.seq.label("event_seq"),
                wd.c.attempt,
                wd.c.status,
                wd.c.response_code,
                wd.c.last_error,
                wd.c.next_attempt_at,
                wd.c.created_at,
                wd.c.delivered_at,
            )
            .select_from(wd.outerjoin(ob, ob.c.id == wd.c.event_id))
            .order_by(wd.c.created_at.desc(), wd.c.id.desc())
            .limit(min(max(limit, 1), 500))
        )
        if subscription_id:
            q = q.where(wd.c.subscription_id == _uuid(subscription_id, "webhook subscription"))
        if status:
            q = q.where(wd.c.status == status)
        async with self.engine.connect() as conn:
            return {"items": [row_dict(r) for r in await conn.execute(q)]}

    # ── worker ──
    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook worker iteration failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.webhook_poll_s)
            except TimeoutError:
                pass
            self._wake.clear()

    async def run_once(self) -> dict[str, int]:
        fanned = await self.fan_out()
        delivered = failed = retried = 0
        for d in await self._lease_due():
            outcome = await self._deliver(d)
            if outcome == "delivered":
                delivered += 1
            elif outcome == "failed":
                failed += 1
            else:
                retried += 1
        return {"fanned_out": fanned, "delivered": delivered, "retrying": retried, "failed": failed}

    async def fan_out(self, batch: int = 200) -> int:
        ob, ws, wd = t.outbox, t.webhook_subscription, t.webhook_delivery
        async with self.engine.begin() as conn:
            rows = list(
                await conn.execute(
                    select(ob.c.id, ob.c.event_type)
                    .where(ob.c.published_at.is_(None))
                    .order_by(ob.c.seq)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            )
            if not rows:
                return 0
            subs = list(await conn.execute(select(ws.c.id, ws.c.events).where(ws.c.active.is_(True))))
            values = [
                {
                    "subscription_id": s.id,
                    "event_id": r.id,
                    "event_type": webhook_type(r.event_type),
                    "attempt": 0,
                    "status": "pending",
                    "next_attempt_at": func.now(),
                }
                for r in rows
                for s in subs
                if wants(s.events, r.event_type)
            ]
            for v in values:
                await conn.execute(
                    pg_insert(wd)
                    .values(**v)
                    .on_conflict_do_nothing(index_elements=[wd.c.subscription_id, wd.c.event_id])
                )
            await conn.execute(update(ob).where(ob.c.id.in_([r.id for r in rows])).values(published_at=func.now()))
        return len(values)

    async def _lease_due(self, batch: int = 20) -> list[Delivery]:
        wd = t.webhook_delivery
        async with self.engine.begin() as conn:
            rows = list(
                await conn.execute(
                    select(wd.c.id, wd.c.subscription_id, wd.c.event_id, wd.c.attempt)
                    .where(wd.c.status == "pending", wd.c.next_attempt_at <= func.now())
                    .order_by(wd.c.next_attempt_at, wd.c.id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            )
            if rows:
                await conn.execute(
                    update(wd)
                    .where(wd.c.id.in_([r.id for r in rows]))
                    .values(next_attempt_at=func.now() + timedelta(seconds=LEASE_S))
                )
        return [Delivery(r.id, r.subscription_id, r.event_id, r.attempt) for r in rows]

    async def _deliver(self, d: Delivery) -> str:
        ws, ob = t.webhook_subscription, t.outbox
        async with self.engine.connect() as conn:
            sub = (await conn.execute(select(ws).where(ws.c.id == d.subscription_id))).first()
            ev = (
                await conn.execute(
                    select(ob.c.id, ob.c.event_type, ob.c.created_at, ob.c.payload).where(ob.c.id == d.event_id)
                )
            ).first()
        if sub is None or ev is None or not sub.active:
            return await self._settle(d, ok=False, code=None, error="subscription or event gone", final=True)
        body = json.dumps(
            {
                "type": webhook_type(ev.event_type),
                "id": str(ev.id),
                "timestamp": jsonable(ev.created_at),
                "tenant_hint": None,
                "data": jsonable(ev.payload),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        ts = int(time.time())
        msg_id = str(ev.id)
        try:
            secret = self.box.open(sub.secret_enc)
            headers = {
                "content-type": "application/json",
                "user-agent": "Merxa-Webhooks/1.0",
                "webhook-id": msg_id,
                "webhook-timestamp": str(ts),
                "webhook-signature": sign(secret, msg_id, ts, body),
            }
        except Exception as exc:
            return await self._settle(
                d, ok=False, code=None, error=f"secret unusable: {type(exc).__name__}", final=True
            )
        code: int | None = None
        error: str | None = None
        try:
            # Only the status code matters: the response body is never read (a receiver that
            # streams without end must not grow this process' memory or hold the worker).
            if self._client is not None:
                code = await self._post(self._client, sub.url, body, headers)
            else:
                async with httpx.AsyncClient(timeout=self.settings.webhook_timeout_s, follow_redirects=False) as client:
                    code = await self._post(client, sub.url, body, headers)
            ok = 200 <= code < 300
            if not ok:
                error = f"HTTP {code}"
        except Exception as exc:  # any client failure is a failed attempt (retried, then failed)
            # Not only httpx.HTTPError: e.g. an unencodable host raises idna's ValueError, which
            # escaped run_once() and left this delivery (and the rest of its batch) leased forever.
            ok = False
            error = f"{type(exc).__name__}: {exc}"[:500]
        return await self._settle(d, ok=ok, code=code, error=error, final=False)

    async def _post(self, client: httpx.AsyncClient, url: str, body: bytes, headers: dict[str, str]) -> int:
        async with client.stream(
            "POST",
            url,
            content=body,
            headers=headers,
            timeout=self.settings.webhook_timeout_s,
            follow_redirects=False,
        ) as resp:
            return resp.status_code

    async def _settle(self, d: Delivery, *, ok: bool, code: int | None, error: str | None, final: bool) -> str:
        wd = t.webhook_delivery
        attempt = d.attempt + 1
        if ok:
            values: dict[str, Any] = {
                "status": "delivered",
                "attempt": attempt,
                "response_code": code,
                "last_error": None,
                "delivered_at": func.now(),
                "next_attempt_at": None,
            }
            outcome = "delivered"
        elif final or attempt >= MAX_ATTEMPTS:
            values = {
                "status": "failed",
                "attempt": attempt,
                "response_code": code,
                "last_error": error,
                "next_attempt_at": None,
            }
            outcome = "failed"
        else:
            delay = BACKOFF_S[attempt - 1]
            values = {
                "attempt": attempt,
                "response_code": code,
                "last_error": error,
                "next_attempt_at": func.now() + timedelta(seconds=delay),
            }
            outcome = "retrying"
        async with self.engine.begin() as conn:
            await conn.execute(update(wd).where(wd.c.id == d.id).values(**values))
        if outcome != "delivered":
            log.info("webhook delivery " + outcome, extra={"delivery": str(d.id), "attempt": attempt, "error": error})
        return outcome


def _uuid(value: str, entity: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise not_found(entity, value) from exc


def now_utc() -> datetime:
    return datetime.now(UTC)
