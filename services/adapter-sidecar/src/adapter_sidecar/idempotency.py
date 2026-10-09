"""Idempotency records keyed by ``(principal, key)`` — the Merxa protocol (contract 20 / 22):

1. :func:`begin` inserts an ``in_flight`` row in its own short transaction:
   * a ``done`` row with the same request hash → :class:`Replay` (stored status + body);
   * a row with a different request hash → 422 ``idempotency_key_reused``;
   * an ``in_flight`` row → 409 ``idempotency_in_flight`` (transient for Keelson), unless it is
     older than ``IDEMPOTENCY_STALE_S`` — then it is taken over (the first attempt died).
2. The operation completes the record **inside its own state transaction** (statement from
   :func:`complete_statement`), so the effect and the stored response commit atomically.
3. :func:`abandon` drops an ``in_flight`` row after an error, so the client may retry.

The request fingerprint never includes the approval receipt: a re-minted receipt for the same
approval replays instead of tripping the reuse check.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter_sidecar.backends import Stmt
from adapter_sidecar.db import begin_write, iso, jdump, jload, utcnow
from adapter_sidecar.errors import Problem
from keelson_contracts.common import sha256_hex

MAX_KEY_LEN = 255


@dataclass(frozen=True, slots=True)
class Replay:
    status_code: int
    response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Claim:
    principal: str
    key: str
    request_sha256: str


def request_fingerprint(operation: str, body: Any) -> str:
    return sha256_hex({"op": operation, "body": body})


def validate_key(key: str | None) -> str:
    if key is None or not key.strip():
        raise Problem(400, "idempotency_key_required", "idempotency_key is required for this tool")
    key = key.strip()
    if len(key) > MAX_KEY_LEN or not key.isprintable():
        raise Problem(400, "invalid_idempotency_key", f"idempotency_key must be 1..{MAX_KEY_LEN} printable characters")
    return key


async def begin(engine: AsyncEngine, *, principal: str, key: str, request_sha256: str,
                stale_after_s: int = 120) -> Claim | Replay:
    key = validate_key(key)
    now = utcnow()
    async with begin_write(engine) as conn:
        row = (await conn.execute(
            text("SELECT request_sha256, state, status_code, response, created_at FROM idempotency_record "
                 "WHERE principal = :p AND key = :k"),
            {"p": principal, "k": key},
        )).first()
        if row is None:
            await conn.execute(
                text("INSERT INTO idempotency_record (principal, key, request_sha256, state, created_at) "
                     "VALUES (:p, :k, :h, 'in_flight', :t)"),
                {"p": principal, "k": key, "h": request_sha256, "t": iso(now)},
            )
            return Claim(principal, key, request_sha256)
        if row.request_sha256 != request_sha256:
            raise Problem(422, "idempotency_key_reused",
                          "this idempotency_key was already used with a different request")
        if row.state == "done":
            return Replay(int(row.status_code or 200), dict(jload(row.response) or {}))
        started = dt.datetime.fromisoformat(str(row.created_at).replace("Z", "+00:00"))
        if (now - started).total_seconds() > stale_after_s:
            await conn.execute(
                text("UPDATE idempotency_record SET created_at = :t WHERE principal = :p AND key = :k"),
                {"p": principal, "k": key, "t": iso(now)},
            )
            return Claim(principal, key, request_sha256)
    raise Problem(409, "idempotency_in_flight", "a request with this idempotency_key is in progress; retry",
                  headers={"Retry-After": "2"})


def complete_statement(claim: Claim, status_code: int, response: dict[str, Any]) -> Stmt:
    """Mark the claim done (an upsert: the in-flight row may have been reset meanwhile)."""
    return (
        "INSERT INTO idempotency_record (principal, key, request_sha256, state, status_code, response, created_at) "
        "VALUES (:p, :k, :h, 'done', :s, :r, :t) ON CONFLICT (principal, key) DO UPDATE SET "
        "state = 'done', status_code = :s, response = :r",
        {"p": claim.principal, "k": claim.key, "h": claim.request_sha256, "s": status_code, "r": jdump(response),
         "t": iso(utcnow())},
    )


async def abandon(engine: AsyncEngine, claim: Claim) -> None:
    async with begin_write(engine) as conn:
        await conn.execute(
            text("DELETE FROM idempotency_record WHERE principal = :p AND key = :k AND state = 'in_flight'"),
            {"p": claim.principal, "k": claim.key},
        )


async def purge_expired(engine: AsyncEngine, retention_h: int = 24) -> int:
    cut = iso(utcnow() - dt.timedelta(hours=retention_h))
    async with begin_write(engine) as conn:
        res = await conn.execute(text("DELETE FROM idempotency_record WHERE created_at < :c"), {"c": cut})
    return int(res.rowcount or 0)
