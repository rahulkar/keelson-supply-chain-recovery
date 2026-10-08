"""Server-side state for the browser: BFF sessions, pending OAuth flows, console tickets.

* Cookie ``keelson_session`` carries a random value; the row id is
  ``HMAC-SHA256(SESSION_SECRET, value)`` so the DB never holds a usable cookie.
* Tokens (``access``, ``refresh``, ``id_token``) are AES-GCM encrypted with AAD ``bff|<id>``.
* Sessions have an idle timeout (``SESSION_IDLE_SECONDS``, sliding) and an absolute one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core.crypto import decrypt_str, encrypt_str
from keelson_engine.core.db import system_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.oidc import random_token
from keelson_engine.core.sql import dumps

__all__ = [
    "CONSOLE_COOKIE",
    "LOGIN_COOKIE",
    "SESSION_COOKIE",
    "SessionData",
    "consume_console_ticket",
    "consume_flow",
    "create_console_session",
    "create_console_ticket",
    "create_flow",
    "create_session",
    "delete_console_session",
    "delete_session",
    "load_console_session",
    "load_session",
    "session_alive",
    "session_id_for",
    "session_tokens",
    "sha256_hex",
]

log = get_logger(__name__)

SESSION_COOKIE = "keelson_session"
LOGIN_COOKIE = "keelson_login"
CONSOLE_COOKIE = "keelson_console"

_ephemeral_secret: bytes | None = None


def _session_secret() -> bytes:
    global _ephemeral_secret
    value = get_settings().session_secret.get_secret_value()
    if value:
        return value.encode("utf-8")
    if _ephemeral_secret is None:
        log.warning("SESSION_SECRET is not set; using an ephemeral per-process secret")
        _ephemeral_secret = secrets.token_bytes(32)
    return _ephemeral_secret


def session_id_for(cookie_value: str) -> str:
    return hmac.new(_session_secret(), cookie_value.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(slots=True)
class SessionData:
    id: str
    sub: str
    username: str
    name: str
    email: str | None
    roles: list[str]
    tenants: list[str]
    active_tenant: str | None
    csrf_token: str
    demo: bool
    auth_time: dt.datetime | None
    merxa_username: str | None
    expires_at: dt.datetime
    extra: dict[str, Any] = field(default_factory=dict)

    def actor(self) -> dict[str, Any]:
        return {"sub": self.sub, "username": self.username, "name": self.name, "roles": list(self.roles)}

    def has_any(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)

    def user_json(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "username": self.username,
            "name": self.name,
            "email": self.email,
            "roles": list(self.roles),
            "tenants": list(self.tenants),
            "active_tenant": self.active_tenant,
        }


_SESSION_COLS = (
    "id, sub, username, name, email, roles, tenants, active_tenant, csrf_token, demo, auth_time, "
    "merxa_username, expires_at, idle_expires_at, last_seen_at"
)


def _session(m: Any) -> SessionData:
    return SessionData(
        id=m["id"],
        sub=m["sub"],
        username=m["username"],
        name=m["name"],
        email=m["email"],
        roles=list(m["roles"] or []),
        tenants=list(m["tenants"] or []),
        active_tenant=m["active_tenant"],
        csrf_token=m["csrf_token"],
        demo=bool(m["demo"]),
        auth_time=m["auth_time"],
        merxa_username=m["merxa_username"],
        expires_at=m["expires_at"],
    )


async def create_session(
    *,
    sub: str,
    username: str,
    name: str,
    email: str | None,
    roles: list[str],
    tenants: list[str],
    active_tenant: str | None,
    tokens: dict[str, Any],
    demo: bool,
    auth_time: dt.datetime | None,
    merxa_username: str | None,
) -> tuple[str, SessionData]:
    """Create a session row; returns ``(cookie_value, session)``."""
    settings = get_settings()
    cookie_value = random_token(43)
    sid = session_id_for(cookie_value)
    now = _now()
    expires = now + dt.timedelta(seconds=settings.session_max_seconds)
    idle = min(expires, now + dt.timedelta(seconds=settings.session_idle_seconds))
    stored_tokens = {k: tokens.get(k) for k in ("access_token", "refresh_token", "id_token") if tokens.get(k)}
    tokens_enc = encrypt_str(json.dumps(stored_tokens), f"bff|{sid}") if stored_tokens else None
    csrf = random_token(43)
    async with system_session() as s:
        res = await s.execute(
            text(
                f"""
                INSERT INTO bff_session (id, sub, username, name, email, roles, tenants, active_tenant, merxa_username,
                                         demo, csrf_token, tokens_enc, auth_time, idle_expires_at, expires_at)
                VALUES (:id, :sub, :username, :name, :email, CAST(:roles AS jsonb), CAST(:tenants AS jsonb), :active,
                        :merxa, :demo, :csrf, :tok, :auth_time, :idle, :exp)
                RETURNING {_SESSION_COLS}
                """
            ),
            {
                "id": sid, "sub": sub, "username": username, "name": name, "email": email,
                "roles": dumps(sorted(set(roles))), "tenants": dumps(list(dict.fromkeys(tenants))),
                "active": active_tenant, "merxa": merxa_username, "demo": demo, "csrf": csrf, "tok": tokens_enc,
                "auth_time": auth_time, "idle": idle, "exp": expires,
            },
        )
        row = res.mappings().one()
    return cookie_value, _session(row)


async def load_session(cookie_value: str | None) -> SessionData | None:
    """Session for a cookie value, or None (unknown / expired). Slides the idle timeout."""
    if not cookie_value or len(cookie_value) > 200:
        return None
    sid = session_id_for(cookie_value)
    settings = get_settings()
    now = _now()
    async with system_session() as s:
        res = await s.execute(text(f"SELECT {_SESSION_COLS} FROM bff_session WHERE id = :id"), {"id": sid})
        m = res.mappings().first()
        if m is None:
            return None
        if m["expires_at"] <= now or m["idle_expires_at"] <= now:
            await s.execute(text("DELETE FROM bff_session WHERE id = :id"), {"id": sid})
            return None
        if (now - m["last_seen_at"]).total_seconds() > 60:
            idle = min(m["expires_at"], now + dt.timedelta(seconds=settings.session_idle_seconds))
            await s.execute(
                text("UPDATE bff_session SET last_seen_at = :now, idle_expires_at = :idle WHERE id = :id"),
                {"now": now, "idle": idle, "id": sid},
            )
    return _session(m)


async def session_alive(session_id: str) -> bool:
    """True while the session row exists and has not reached its absolute expiry (used by
    long-lived SSE streams to stop after logout / revocation)."""
    async with system_session() as s:
        res = await s.execute(
            text("SELECT 1 FROM bff_session WHERE id = :id AND expires_at > now()"), {"id": session_id}
        )
        return res.first() is not None


async def session_tokens(session_id: str) -> dict[str, Any]:
    async with system_session() as s:
        res = await s.execute(text("SELECT tokens_enc FROM bff_session WHERE id = :id"), {"id": session_id})
        enc = res.scalar_one_or_none()
    if not enc:
        return {}
    return dict(json.loads(decrypt_str(enc, f"bff|{session_id}")))


async def delete_session(session_id: str) -> None:
    async with system_session() as s:
        await s.execute(text("DELETE FROM bff_session WHERE id = :id"), {"id": session_id})
        await s.execute(text("DELETE FROM bff_session WHERE expires_at < now() - interval '1 day'"))


# ───────────────────────────── OAuth flows ─────────────────────────────


@dataclass(slots=True)
class FlowData:
    kind: str
    session_id: str | None
    keelson_sub: str | None
    tenant: str | None
    connector: str | None
    nonce: str
    code_verifier: str
    return_to: str


async def create_flow(
    *,
    state: str,
    kind: str,
    nonce: str,
    code_verifier: str,
    return_to: str,
    session_id: str | None = None,
    keelson_sub: str | None = None,
    tenant: str | None = None,
    connector: str | None = None,
    ttl_seconds: int = 600,
) -> None:
    state_hash = sha256_hex(state)
    async with system_session() as s:
        await s.execute(text("DELETE FROM oauth_flow WHERE expires_at < now()"))
        await s.execute(
            text(
                """
                INSERT INTO oauth_flow (state_hash, kind, session_id, keelson_sub, tenant, connector, nonce,
                                        code_verifier_enc, return_to, expires_at)
                VALUES (:h, :kind, :sid, :sub, :tenant, :conn, :nonce, :cv, :rt,
                        now() + make_interval(secs => :ttl))
                """
            ),
            {
                "h": state_hash, "kind": kind, "sid": session_id, "sub": keelson_sub, "tenant": tenant,
                "conn": connector, "nonce": nonce, "cv": encrypt_str(code_verifier, f"flow|{state_hash}"),
                "rt": return_to, "ttl": ttl_seconds,
            },
        )


async def consume_flow(state: str, kind: str) -> FlowData | None:
    """Single use: deletes and returns the pending flow (None if unknown/expired/other kind)."""
    if not state or len(state) > 200:
        return None
    state_hash = sha256_hex(state)
    async with system_session() as s:
        res = await s.execute(
            text(
                "DELETE FROM oauth_flow WHERE state_hash = :h RETURNING kind, session_id, keelson_sub, tenant, "
                "connector, nonce, code_verifier_enc, return_to, expires_at"
            ),
            {"h": state_hash},
        )
        m = res.mappings().first()
    if m is None or m["kind"] != kind or m["expires_at"] <= _now():
        return None
    return FlowData(
        kind=m["kind"],
        session_id=m["session_id"],
        keelson_sub=m["keelson_sub"],
        tenant=m["tenant"],
        connector=m["connector"],
        nonce=m["nonce"],
        code_verifier=decrypt_str(m["code_verifier_enc"], f"flow|{state_hash}"),
        return_to=m["return_to"],
    )


# ───────────────────────────── console handoff ─────────────────────────────


async def create_console_ticket(*, target: str, sub: str, username: str, roles: list[str]) -> str:
    ticket = random_token(43)
    ttl = get_settings().console_ticket_seconds
    async with system_session() as s:
        await s.execute(text("DELETE FROM console_ticket WHERE expires_at < now() - interval '1 hour'"))
        await s.execute(
            text(
                "INSERT INTO console_ticket (id_hash, target, sub, username, roles, expires_at) "
                "VALUES (:h, :t, :sub, :u, CAST(:roles AS jsonb), now() + make_interval(secs => :ttl))"
            ),
            {"h": sha256_hex(ticket), "t": target, "sub": sub, "u": username, "roles": dumps(roles), "ttl": ttl},
        )
    return ticket


async def consume_console_ticket(ticket: str, target: str) -> dict[str, Any] | None:
    if not ticket or len(ticket) > 200:
        return None
    async with system_session() as s:
        res = await s.execute(
            text(
                "UPDATE console_ticket SET used_at = now() WHERE id_hash = :h AND used_at IS NULL "
                "AND expires_at > now() AND target = :t RETURNING sub, username, roles"
            ),
            {"h": sha256_hex(ticket), "t": target},
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def create_console_session(*, target: str, sub: str, username: str, roles: list[str]) -> str:
    value = random_token(43)
    ttl = get_settings().console_session_seconds
    async with system_session() as s:
        await s.execute(text("DELETE FROM console_session WHERE expires_at < now()"))
        await s.execute(
            text(
                "INSERT INTO console_session (id_hash, target, sub, username, roles, expires_at) "
                "VALUES (:h, :t, :sub, :u, CAST(:roles AS jsonb), now() + make_interval(secs => :ttl))"
            ),
            {"h": session_id_for(value), "t": target, "sub": sub, "u": username, "roles": dumps(roles), "ttl": ttl},
        )
    return value


async def load_console_session(value: str | None, target: str) -> dict[str, Any] | None:
    if not value or len(value) > 200:
        return None
    async with system_session() as s:
        res = await s.execute(
            text(
                "SELECT sub, username, roles FROM console_session WHERE id_hash = :h AND target = :t "
                "AND expires_at > now()"
            ),
            {"h": session_id_for(value), "t": target},
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def delete_console_session(value: str) -> None:
    async with system_session() as s:
        await s.execute(text("DELETE FROM console_session WHERE id_hash = :h"), {"h": session_id_for(value)})
