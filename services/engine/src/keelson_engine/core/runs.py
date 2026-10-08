"""Runs queue (engine-api → engine-worker hand-off).

* :func:`enqueue_run` inserts ``run(status='queued')`` (deduplicated on
  ``(tenant, case_id, Idempotency-Key)``) and ``XADD keelson:runs:wake``.
* :func:`claim_next_run` — ``UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)``
  picking the oldest queued run whose case has no running run; the partial unique index
  ``run_one_running_per_case_uq`` is the per-case lease that makes this race-free across workers.
* :func:`heartbeat` extends the lease; :func:`reap_expired` re-queues runs whose lease expired as
  ``kind='crash_retry'`` (``origin_kind`` + ``invocation_id`` + ``session_id`` kept, so the
  worker resumes the same ADK invocation), or fails them after ``max_attempts``.
* Cancellation is a flag (:func:`request_cancel` / :func:`is_cancel_requested`); a queued run is
  cancelled immediately.

ADK session id = run id for new runs; resume runs reuse the paused run's session id.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_contracts.common import canonical_json, new_run_id
from keelson_contracts.runs import RUNS_WAKE_STREAM, RunRequest
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps
from keelson_engine.core.valkey import get_valkey
from keelson_engine.public_errors import new_error_id, user_message

__all__ = [
    "IdempotencyConflict",
    "RunNotFound",
    "RunRow",
    "claim_next_run",
    "emit_run_error",
    "enqueue_run",
    "finish_run",
    "get_run",
    "heartbeat",
    "insert_run",
    "is_cancel_requested",
    "reap_expired",
    "request_cancel",
    "set_run_invocation",
    "wake_workers",
]

log = get_logger(__name__)

RunStatus = Literal["queued", "running", "succeeded", "failed", "paused", "cancelled"]
FinalStatus = Literal["succeeded", "failed", "paused", "cancelled"]


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used for a different request on this case."""

    def __init__(self, run_id: str) -> None:
        super().__init__("idempotency key reused with a different request")
        self.run_id = run_id


class RunNotFound(LookupError):
    pass


@dataclass(slots=True)
class RunRow:
    id: str
    tenant: str
    case_id: str
    kind: str
    """``chat|action|signal|resume`` or ``crash_retry`` (re-queued by the reaper)."""
    payload: dict[str, Any]
    actor: dict[str, Any]
    attempt: int
    invocation_id: str | None
    session_id: str
    agent_spec_version: str
    created_by: str
    origin_kind: str = "chat"
    """The run's original kind (equals ``kind`` unless ``kind == 'crash_retry'``)."""
    status: str = "queued"
    cancel_requested: bool = False
    lease_owner: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def request(self) -> RunRequest:
        """The typed :class:`RunRequest` (uses ``origin_kind`` for crash retries)."""
        return RunRequest.model_validate(
            {
                "run_id": self.id,
                "tenant": self.tenant,
                "case_id": self.case_id,
                "kind": self.origin_kind,
                "actor": self.actor,
                "payload": self.payload,
            }
        )


_RUN_COLUMNS = (
    "id, tenant, case_id, kind, origin_kind, status, payload, actor, attempt, invocation_id, session_id, "
    "agent_spec_version, created_by, cancel_requested, lease_owner, error"
)


def _row(m: Any) -> RunRow:
    d = dict(m)
    return RunRow(
        id=d["id"],
        tenant=d["tenant"],
        case_id=d["case_id"],
        kind=d["kind"],
        payload=d["payload"],
        actor=d["actor"],
        attempt=int(d["attempt"]),
        invocation_id=d["invocation_id"],
        session_id=d["session_id"],
        agent_spec_version=d["agent_spec_version"],
        created_by=d["created_by"],
        origin_kind=d["origin_kind"],
        status=d["status"],
        cancel_requested=bool(d["cancel_requested"]),
        lease_owner=d["lease_owner"],
        error=d["error"],
    )


def _request_hash(kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json({"kind": kind, "payload": payload}).encode("utf-8")).hexdigest()


async def _agent_spec_version(session: AsyncSession, tenant: str) -> str:
    res = await session.execute(
        text("SELECT version FROM agent_spec WHERE tenant = :t ORDER BY seq DESC NULLS LAST, created_at DESC LIMIT 1"),
        {"t": tenant},
    )
    version = res.scalar_one_or_none()
    return str(version) if version else "default"


async def insert_run(
    session: AsyncSession,
    *,
    tenant: str,
    case_id: str,
    kind: str,
    payload: dict[str, Any],
    actor: dict[str, Any],
    idempotency_key: str | None,
    session_id: str | None = None,
    invocation_id: str | None = None,
    run_id: str | None = None,
) -> tuple[str, bool]:
    """Insert a queued run inside an open tenant transaction. Returns ``(run_id, created)``;
    ``created`` is False when the idempotency key matched an earlier identical request."""
    rid = run_id or new_run_id()
    request = RunRequest.model_validate(
        {"run_id": rid, "tenant": tenant, "case_id": case_id, "kind": kind, "actor": actor, "payload": payload}
    )
    payload_json = request.payload.model_dump(mode="json", exclude_none=False)
    actor_json = request.actor.model_dump(mode="json")
    req_hash = _request_hash(kind, payload_json)
    if idempotency_key is not None:
        if not (1 <= len(idempotency_key) <= 200):
            raise ValueError("Idempotency-Key must be 1..200 characters")
        existing = await session.execute(
            text(
                "SELECT id, request_hash FROM run WHERE tenant = :t AND case_id = :c AND idempotency_key = :k"
            ),
            {"t": tenant, "c": case_id, "k": idempotency_key},
        )
        found = existing.first()
        if found is not None:
            if found[1] != req_hash:
                raise IdempotencyConflict(found[0])
            return str(found[0]), False
    spec_version = await _agent_spec_version(session, tenant)
    await session.execute(
        text(
            """
            INSERT INTO run (id, tenant, case_id, kind, origin_kind, status, payload, actor, idempotency_key,
                             request_hash, invocation_id, session_id, agent_spec_version, created_by)
            VALUES (:id, :tenant, :case_id, :kind, :kind, 'queued', CAST(:payload AS jsonb), CAST(:actor AS jsonb),
                    :key, :hash, :inv, :sid, :spec, :created_by)
            """
        ),
        {
            "id": rid,
            "tenant": tenant,
            "case_id": case_id,
            "kind": kind,
            "payload": dumps(payload_json),
            "actor": dumps(actor_json),
            "key": idempotency_key,
            "hash": req_hash,
            "inv": invocation_id,
            "sid": session_id or rid,
            "spec": spec_version,
            "created_by": actor_json["sub"],
        },
    )
    return rid, True


async def wake_workers(run_id: str, tenant: str) -> None:
    """``XADD keelson:runs:wake`` (best effort: workers also poll the table)."""
    try:
        await get_valkey().xadd(RUNS_WAKE_STREAM, {"run_id": run_id, "tenant": tenant}, maxlen=1000, approximate=True)
    except Exception as exc:  # noqa: BLE001 - wake-up is an optimisation only
        log.warning("run wake-up failed", run_id=run_id, error=str(exc))


async def enqueue_run(
    *,
    tenant: str,
    case_id: str,
    kind: str,
    payload: dict[str, Any],
    actor: dict[str, Any],
    idempotency_key: str | None,
    user_message: str | None = None,
) -> str:
    """Queue a run (dedupe on ``idempotency_key``) and wake the workers; returns the run id.

    ``user_message`` (chat runs) is stored as a ``case_message`` in the same transaction.
    Raises :class:`IdempotencyConflict` when the key was used for a different request.
    """
    try:
        async with tenant_session(tenant) as s:
            run_id, created = await insert_run(
                s, tenant=tenant, case_id=case_id, kind=kind, payload=payload, actor=actor,
                idempotency_key=idempotency_key,
            )
            if created and user_message is not None:
                await s.execute(
                    text(
                        "INSERT INTO case_message (tenant, case_id, role, text, run_id, author_sub) "
                        "VALUES (:t, :c, 'user', :text, :r, :a)"
                    ),
                    {"t": tenant, "c": case_id, "text": user_message, "r": run_id, "a": actor.get("sub")},
                )
            if created:
                preview = (user_message or "")[:200] or None
                await s.execute(
                    text(
                        'UPDATE "case" SET updated_at = now(), '
                        "last_message_preview = coalesce(:p, last_message_preview) WHERE id = :c"
                    ),
                    {"p": preview, "c": case_id},
                )
    except IntegrityError:
        # Concurrent request with the same key won the insert: return its run.
        if idempotency_key is None:
            raise
        async with tenant_session(tenant) as s:
            res = await s.execute(
                text("SELECT id, request_hash FROM run WHERE tenant = :t AND case_id = :c AND idempotency_key = :k"),
                {"t": tenant, "c": case_id, "k": idempotency_key},
            )
            found = res.first()
        if found is None:
            raise
        expected = _request_hash(kind, RunRequest.model_validate(
            {"run_id": "x", "tenant": tenant, "case_id": case_id, "kind": kind, "actor": actor, "payload": payload}
        ).payload.model_dump(mode="json"))
        if found[1] != expected:
            raise IdempotencyConflict(found[0]) from None
        return str(found[0])
    if created:
        await wake_workers(run_id, tenant)
    return run_id


async def claim_next_run(worker_id: str, lease_seconds: int = 60) -> RunRow | None:
    """Claim the oldest runnable queued run (FIFO per case; one running run per case)."""
    for _ in range(5):
        try:
            async with system_session() as s:
                res = await s.execute(
                    text(
                        f"""
                        UPDATE run SET status = 'running', lease_owner = :w,
                               lease_expires_at = now() + make_interval(secs => :lease),
                               heartbeat_at = now(), started_at = coalesce(started_at, now()),
                               attempt = attempt + 1, updated_at = now()
                        WHERE id = (
                          SELECT r.id FROM run r
                          WHERE r.status = 'queued' AND NOT r.cancel_requested
                            AND NOT EXISTS (SELECT 1 FROM run x WHERE x.case_id = r.case_id AND x.status = 'running')
                            AND NOT EXISTS (SELECT 1 FROM run y WHERE y.case_id = r.case_id AND y.status = 'queued'
                                            AND (y.created_at, y.id) < (r.created_at, r.id))
                          ORDER BY r.created_at, r.id
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1)
                        RETURNING {_RUN_COLUMNS}
                        """
                    ),
                    {"w": worker_id, "lease": lease_seconds},
                )
                m = res.mappings().first()
                return _row(m) if m is not None else None
        except IntegrityError:
            # Another worker started a run of the same case concurrently (per-case lease).
            continue
    return None


async def heartbeat(run_id: str, worker_id: str, lease_seconds: int = 60) -> bool:
    """Extend the lease; False when the run is no longer ours (reaped / finished)."""
    async with system_session() as s:
        res = await s.execute(
            text(
                "UPDATE run SET lease_expires_at = now() + make_interval(secs => :lease), heartbeat_at = now(), "
                "updated_at = now() WHERE id = :id AND lease_owner = :w AND status = 'running' RETURNING id"
            ),
            {"id": run_id, "w": worker_id, "lease": lease_seconds},
        )
        return res.first() is not None


async def set_run_invocation(run_id: str, invocation_id: str) -> None:
    """Record the ADK invocation id as soon as it is known (crash retries resume it)."""
    async with system_session() as s:
        await s.execute(
            text("UPDATE run SET invocation_id = :inv, updated_at = now() WHERE id = :id"),
            {"id": run_id, "inv": invocation_id},
        )


async def finish_run(
    run_id: str,
    status: FinalStatus,
    *,
    invocation_id: str | None = None,
    error: str | None = None,
    worker_id: str | None = None,
) -> bool:
    """Record the run's final status. Returns False (and changes nothing) when the run is
    already final (e.g. failed/cancelled by the reaper) or — with ``worker_id`` — when its
    lease now belongs to another worker (a zombie must never clear a live worker's lease and
    so break the one-running-run-per-case rule)."""
    if status not in ("succeeded", "failed", "paused", "cancelled"):
        raise ValueError(f"invalid final status {status!r}")
    async with system_session() as s:
        res = await s.execute(
            text(
                "UPDATE run SET status = :st, invocation_id = coalesce(:inv, invocation_id), error = :err, "
                "finished_at = now(), lease_owner = NULL, lease_expires_at = NULL, updated_at = now() "
                "WHERE id = :id AND status IN ('queued', 'running') "
                "AND (CAST(:w AS text) IS NULL OR lease_owner IS NULL OR lease_owner = :w) "
                "RETURNING id"
            ),
            {"id": run_id, "st": status, "inv": invocation_id, "err": (error or None) and error[:4000],
             "w": worker_id},
        )
        updated = res.first() is not None
    if not updated:
        log.warning("finish_run ignored: run already final or leased by another worker", run_id=run_id,
                    status=status)
    return updated


async def reap_expired(max_attempts: int = 3) -> list[str]:
    """Re-queue runs whose lease expired (``kind='crash_retry'``); fail those that already
    used ``max_attempts``. Returns the re-queued run ids."""
    async with system_session() as s:
        res = await s.execute(
            text(
                """
                UPDATE run SET status = 'queued', kind = 'crash_retry', lease_owner = NULL,
                       lease_expires_at = NULL, updated_at = now()
                WHERE id IN (SELECT id FROM run WHERE status = 'running' AND lease_expires_at < now()
                             AND attempt < :max AND NOT cancel_requested FOR UPDATE SKIP LOCKED)
                RETURNING id
                """
            ),
            {"max": max_attempts},
        )
        requeued = [str(r[0]) for r in res.all()]
        res = await s.execute(
            text(
                """
                UPDATE run SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                       error = CASE WHEN cancel_requested THEN 'cancelled (lease expired)'
                                    ELSE 'lease expired after ' || attempt || ' attempts' END,
                       finished_at = now(), lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id IN (SELECT id FROM run WHERE status = 'running' AND lease_expires_at < now()
                             FOR UPDATE SKIP LOCKED)
                RETURNING id, tenant, case_id, status
                """
            )
        )
        failed = [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in res.all()]
    for rid in requeued:
        log.warning("run lease expired; re-queued as crash_retry", run_id=rid)
    for rid, tenant, case_id, status in failed:
        if status == "failed":
            error_id = new_error_id()
            log.error("run lease expired; giving up", run_id=rid, status=status, error_id=error_id)
            await emit_run_error(tenant, case_id, rid, code="CRASH_LIMIT", error_id=error_id,
                                 message=user_message(error_id, "The run stopped unexpectedly and could not be resumed."))
        else:
            log.warning("run lease expired while cancelling", run_id=rid, status=status)
            await emit_run_error(tenant, case_id, rid, message="Run cancelled.", code="CANCELLED")
    for rid in requeued:
        await wake_workers(rid, "")
    return requeued


async def emit_run_error(
    tenant: str, case_id: str, run_id: str, *, message: str, code: str, error_id: str | None = None
) -> None:
    """Append ``RUN_ERROR`` for a run the API / reaper finished (``metadata.sc.runId`` = the run).

    ``message`` must be user-facing (fixed text, never exception text); ``error_id`` is added as
    ``metadata.errorId`` for failures that were logged under that id."""
    from keelson_contracts.agui import ScMetadata, Stage, run_error
    from keelson_engine.core.events import get_event_bus

    try:
        sc = ScMetadata(tenant_id=tenant, case_id=case_id, run_id=run_id, stage=Stage.UNDERSTANDING,
                        prov="decision", label=message)
        event = run_error(sc, message=message, code=code)
        if error_id:
            event["metadata"]["errorId"] = error_id
        await get_event_bus().append(tenant=tenant, case_id=case_id, run_id=run_id, event=event)
    except Exception as exc:  # noqa: BLE001 - the run state is already final; the event is informative
        log.warning("could not emit RUN_ERROR", run_id=run_id, error=type(exc).__name__)


async def is_cancel_requested(run_id: str) -> bool:
    async with system_session() as s:
        res = await s.execute(text("SELECT cancel_requested FROM run WHERE id = :id"), {"id": run_id})
        value = res.scalar_one_or_none()
        return bool(value)


async def get_run(tenant: str, run_id: str) -> RunRow | None:
    async with tenant_session(tenant) as s:
        res = await s.execute(text(f"SELECT {_RUN_COLUMNS} FROM run WHERE id = :id"), {"id": run_id})
        m = res.mappings().first()
        return _row(m) if m is not None else None


async def request_cancel(tenant: str, run_id: str) -> tuple[str, str]:
    """Flag a run for cancellation; a queued run is cancelled at once.

    Returns ``(previous_status, new_status)``; raises :class:`RunNotFound`.
    """
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT status FROM run WHERE id = :id FOR UPDATE"), {"id": run_id})
        previous = res.scalar_one_or_none()
        if previous is None:
            raise RunNotFound(run_id)
        if previous in ("succeeded", "failed", "paused", "cancelled"):
            return str(previous), str(previous)
        new_status = "cancelled" if previous == "queued" else str(previous)
        await s.execute(
            text(
                """
                UPDATE run SET cancel_requested = true, status = :st,
                       finished_at = CASE WHEN :st = 'cancelled' THEN now() ELSE finished_at END,
                       error = CASE WHEN :st = 'cancelled' THEN 'cancelled before start' ELSE error END,
                       updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": run_id, "st": new_status},
        )
    return str(previous), new_status
