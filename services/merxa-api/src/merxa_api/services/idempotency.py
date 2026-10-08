"""Idempotency records keyed by ``(principal, key)`` (24 h retention).

Protocol (shared by REST ``Idempotency-Key`` and MCP ``idempotency_key``):

1. :func:`begin` inserts an ``in_flight`` row (own short transaction).
   * existing ``done`` row, same request hash → :class:`Replay` (stored status + body)
   * existing row, different request hash → 422 ``idempotency_key_reused``
   * existing ``in_flight`` row → 409 ``idempotency_in_flight`` (unless stale, then taken over)
2. The operation runs; on success :func:`complete` stores the response **inside the
   operation's own transaction** where possible, so the effect and the record commit
   atomically (exactly-once apply).
3. On a client/server error :func:`abandon` deletes the row so the client may retry
   with the same key (only successful outcomes — and change-set conflicts, which are a
   business outcome — are replayable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from merxa_api import tables as t
from merxa_api.canonical import sha256_hex
from merxa_api.errors import Problem

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
        raise Problem(400, "idempotency_key_required", "Idempotency-Key is required for this request")
    key = key.strip()
    if len(key) > MAX_KEY_LEN or not key.isprintable():
        raise Problem(400, "invalid_idempotency_key", f"Idempotency-Key must be 1..{MAX_KEY_LEN} printable characters")
    return key


async def begin(
    engine: AsyncEngine,
    *,
    principal: str,
    key: str,
    request_sha256: str,
    stale_after_s: int = 120,
) -> Claim | Replay:
    key = validate_key(key)
    ir = t.idempotency_record
    async with engine.begin() as conn:
        inserted = await conn.execute(
            insert(ir)
            .values(principal=principal, key=key, request_sha256=request_sha256, state="in_flight")
            .on_conflict_do_nothing(index_elements=[ir.c.principal, ir.c.key])
            .returning(ir.c.key)
        )
        if inserted.first() is not None:
            return Claim(principal, key, request_sha256)
        row = (
            await conn.execute(
                select(
                    ir.c.request_sha256,
                    ir.c.state,
                    ir.c.status_code,
                    ir.c.response,
                    (func.now() - ir.c.created_at).label("age"),
                )
                .where(ir.c.principal == principal, ir.c.key == key)
                .with_for_update()
            )
        ).first()
        if row is None:  # deleted concurrently; let the client retry
            raise Problem(409, "idempotency_in_flight", "a request with this Idempotency-Key is in progress")
        if row.request_sha256 != request_sha256:
            raise Problem(
                422,
                "idempotency_key_reused",
                "this Idempotency-Key was already used with a different request body",
            )
        if row.state == "done":
            return Replay(int(row.status_code or 200), dict(row.response or {}))
        if row.age is not None and row.age > timedelta(seconds=stale_after_s):
            await conn.execute(
                update(ir)
                .where(ir.c.principal == principal, ir.c.key == key)
                .values(created_at=func.now(), state="in_flight")
            )
            return Claim(principal, key, request_sha256)
        raise Problem(409, "idempotency_in_flight", "a request with this Idempotency-Key is in progress")


async def complete(conn: AsyncConnection, claim: Claim, status_code: int, response: dict[str, Any]) -> None:
    """Mark the claim done with its response. Upsert: the in-flight row may have vanished
    meanwhile (``scenario:reset`` truncates the table it runs under), and a completed
    request must still be replayable."""
    ir = t.idempotency_record
    stmt = insert(ir).values(
        principal=claim.principal,
        key=claim.key,
        request_sha256=claim.request_sha256,
        state="done",
        status_code=status_code,
        response=response,
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[ir.c.principal, ir.c.key],
            set_={
                "state": stmt.excluded.state,
                "status_code": stmt.excluded.status_code,
                "response": stmt.excluded.response,
            },
        )
    )


async def abandon(engine: AsyncEngine, claim: Claim) -> None:
    ir = t.idempotency_record
    async with engine.begin() as conn:
        await conn.execute(
            delete(ir).where(ir.c.principal == claim.principal, ir.c.key == claim.key, ir.c.state == "in_flight")
        )


async def purge_expired(engine: AsyncEngine, retention_h: int = 24) -> int:
    ir = t.idempotency_record
    async with engine.begin() as conn:
        res = await conn.execute(delete(ir).where(ir.c.created_at < func.now() - timedelta(hours=retention_h)))
    return int(res.rowcount or 0)
