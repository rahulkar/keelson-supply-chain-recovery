"""Append-only audit trail + transactional outbox writers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.logs import current_trace_id
from merxa_api.security.principal import Principal
from merxa_api.services.common import jsonable


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who/why for audit rows. Built from the access token (+ verified receipt on apply)."""

    actor_sub: str | None
    actor_username: str | None
    actor_display: str | None
    client_id: str | None
    via: str | None
    approval_ref: str | None = None
    approved_by: str | None = None
    initiated_by: str | None = None
    change_set_code: str | None = None
    co_approved_by: tuple[str, ...] | None = None  # M5: other four-eyes quorum members (receipt claim)

    @classmethod
    def of(cls, principal: Principal, **extra: Any) -> Attribution:
        return cls(
            actor_sub=principal.sub,
            actor_username=principal.username,
            actor_display=principal.actor_display,
            client_id=principal.client_id,
            via=principal.via,
            **extra,
        )

    @classmethod
    def system(cls, via: str, **extra: Any) -> Attribution:
        return cls(
            actor_sub=None, actor_username="system", actor_display="Merxa system", client_id=None, via=via, **extra
        )


async def write(
    conn: AsyncConnection,
    attribution: Attribution,
    *,
    entity_type: str,
    entity_ref: str,
    action: str,
    before: Any = None,
    after: Any = None,
) -> None:
    await write_many(conn, attribution, [(entity_type, entity_ref, action, before, after)])


async def write_many(
    conn: AsyncConnection,
    attribution: Attribution,
    rows: list[tuple[str, str, str, Any, Any]],
) -> None:
    if not rows:
        return
    trace_id = current_trace_id()
    base = {
        "actor_sub": attribution.actor_sub,
        "actor_username": attribution.actor_username,
        "actor_display": attribution.actor_display,
        "client_id": attribution.client_id,
        "via": attribution.via,
        "approval_ref": attribution.approval_ref,
        "approved_by": attribution.approved_by,
        "initiated_by": attribution.initiated_by,
        "change_set_code": attribution.change_set_code,
        "co_approved_by": list(attribution.co_approved_by) if attribution.co_approved_by else None,
        "trace_id": trace_id,
    }
    await conn.execute(
        insert(t.audit_log),
        [
            {
                **base,
                "entity_type": entity_type,
                "entity_ref": entity_ref,
                "action": action,
                "before": jsonable(before) if before is not None else None,
                "after": jsonable(after) if after is not None else None,
            }
            for entity_type, entity_ref, action, before, after in rows
        ],
    )


async def emit(
    conn: AsyncConnection, event_type: str, payload: dict[str, Any], changes: list[dict[str, Any]] | None = None
) -> None:
    """Write an outbox event (see :mod:`merxa_api.services.events`)."""
    from merxa_api.services import events

    await events.emit(conn, event_type, payload, changes)
