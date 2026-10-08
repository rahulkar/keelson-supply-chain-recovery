"""Merxa UI (BFF) sessions stored in Postgres, plus HMAC-signed short-lived cookies.

The browser holds an opaque random token in cookie ``merxa_session``; the database key
is ``HMAC-SHA256(SESSION_SECRET, token)`` so a database leak does not yield usable
cookies. Tokens from Keycloak are not kept except the ``id_token`` (logout hint).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api import tables as t

SESSION_COOKIE = "merxa_session"
OAUTH_COOKIE = "merxa_oauth"


@dataclass(frozen=True, slots=True)
class SessionUser:
    sub: str
    username: str
    name: str | None
    email: str | None
    roles: tuple[str, ...]
    demo: bool
    id_token: str | None


def _key(secret: str, token: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


async def create(
    engine: AsyncEngine,
    secret: str,
    *,
    sub: str,
    username: str,
    name: str | None,
    email: str | None,
    roles: Iterable[str],
    id_token: str | None,
    demo: bool,
    ttl_s: int,
) -> str:
    token = secrets.token_urlsafe(32)
    async with engine.begin() as conn:
        await conn.execute(
            t.ui_session.insert().values(
                id=_key(secret, token),
                sub=sub,
                username=username,
                name=name,
                email=email,
                roles=sorted(set(roles)),
                id_token=id_token,
                demo=demo,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_s),
            )
        )
        # opportunistic cleanup
        await conn.execute(delete(t.ui_session).where(t.ui_session.c.expires_at < func.now()))
    return token


async def load(engine: AsyncEngine, secret: str, token: str | None) -> SessionUser | None:
    if not token or len(token) > 128:
        return None
    s = t.ui_session
    key = _key(secret, token)
    async with engine.begin() as conn:
        row = (await conn.execute(select(s).where(s.c.id == key, s.c.expires_at > func.now()))).first()
        if row is None:
            return None
        await conn.execute(update(s).where(s.c.id == key).values(last_seen_at=func.now()))
    return SessionUser(
        sub=row.sub,
        username=row.username,
        name=row.name,
        email=row.email,
        roles=tuple(row.roles or ()),
        demo=row.demo,
        id_token=row.id_token,
    )


async def destroy(engine: AsyncEngine, secret: str, token: str | None) -> SessionUser | None:
    if not token:
        return None
    user = await load(engine, secret, token)
    async with engine.begin() as conn:
        await conn.execute(delete(t.ui_session).where(t.ui_session.c.id == _key(secret, token)))
    return user


# ───────────── signed cookie for the OIDC round trip (state, nonce, PKCE verifier) ─────────────
def sign(secret: str, data: dict[str, Any], ttl_s: int = 600) -> str:
    body = dict(data, exp=int(time.time()) + ttl_s)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    )
    return f"{raw}.{mac}"


def unsign(secret: str, value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    raw, mac = value.rsplit(".", 1)
    expected = (
        base64.urlsafe_b64encode(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    )
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
        return None
    return data
