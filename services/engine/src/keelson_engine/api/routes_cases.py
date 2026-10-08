"""Cases, messages, actions, the SSE event stream and block data (``30-engine-api-events.md``)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text

from keelson_contracts.common import is_block_id
from keelson_contracts.runs import MAX_CHAT_CHARS, MAX_PAYLOAD_KEYS, ActionPayload, CaseCursor
from keelson_engine.api.deps import (
    READ_ROLES,
    WRITE_ROLES,
    CurrentSession,
    MutatingSession,
    load_case_or_404,
    require_any_role,
)
from keelson_engine.api.errors import ApiError
from keelson_engine.api.sessions import session_alive
from keelson_engine.api.shutdown import until_shutdown
from keelson_engine.core import checklists, repo
from keelson_engine.core.db import tenant_session
from keelson_engine.core.events import get_event_bus
from keelson_engine.core.interrupts import quorum_progress
from keelson_engine.core.runs import IdempotencyConflict, enqueue_run

__all__ = ["router"]

router = APIRouter()

_DEFAULT_TITLE = "New case"


def case_title_from(text: str, limit: int = 72) -> str:
    """Short, human case title from the first chat line: whole words only, with an ellipsis if cut."""
    first = " ".join((text.splitlines() or [""])[0].split())
    if len(first) <= limit:
        return first
    cut = first[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-—") or first[:limit]
    return cut + "…"


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise ApiError(400, "idempotency_key_required", "Idempotency-Key header is required")
    key = value.strip()
    if len(key) > 200:
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key is too long")
    return key


# ───────────────────────────── cases ─────────────────────────────


@router.get("/api/cases")
async def list_cases(sess: CurrentSession, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading cases")
    rows = await repo.list_cases(limit, tenant=tenant)
    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "mode": r["mode"],
                "status": r["status"],
                "updated_at": _iso(r["updated_at"]),
                "last_message_preview": r["last_message_preview"],
                # M5: newest re-plan signal routed to the case ("Updated" badge)
                "last_signal_at": _iso(r.get("last_signal_at")),
            }
            for r in rows
        ]
    }


class CreateCaseBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)


@router.post("/api/cases", status_code=201)
async def create_case(sess: MutatingSession, body: CreateCaseBody | None = None) -> dict[str, Any]:
    tenant = require_any_role(sess, WRITE_ROLES, "creating cases")
    case_id = await repo.create_case((body.title if body else None) or _DEFAULT_TITLE, created_by=sess.actor(),
                                     tenant=tenant)
    return {"id": case_id}


@router.get("/api/cases/{case_id}")
async def get_case(case_id: str, sess: CurrentSession) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading cases")
    case = await load_case_or_404(tenant, case_id)
    # One snapshot, cursor first (see repo.case_snapshot): the client renders this, then
    # follows the SSE stream from last_event_seq without missing an interrupt.
    snap = await repo.case_snapshot(case_id, tenant=tenant)
    messages, blocks, interrupts, last_seq = snap.messages, snap.blocks, snap.open_interrupts, snap.last_event_seq
    # M5: approval progress ("1 of 2 approvals: Marco ✓ · waiting for compliance")
    quorum = await quorum_progress(tenant, [i["id"] for i in interrupts if i["kind"] == "approval"])
    # Queued runs that no user message announces (M6: the ``signal`` run of a case opened from an
    # alert): the web shows them as a waiting turn until RUN_STARTED arrives.
    async with tenant_session(tenant) as s:
        pending = (await s.execute(
            text("SELECT id, kind FROM run WHERE case_id = :c AND status = 'queued' AND NOT cancel_requested "
                 "ORDER BY created_at, id LIMIT 20"), {"c": case_id})).all()
    return {
        "case": {
            "id": case["id"],
            "title": case["title"],
            "mode": case["mode"],
            "status": case["status"],
            "created_by": case["created_by"],
            "created_at": _iso(case["created_at"]),
        },
        "messages": [
            {"id": m["id"], "role": m["role"], "text": m["text"], "run_id": m["run_id"],
             "created_at": _iso(m["created_at"])}
            for m in messages
        ],
        "blocks": blocks,
        "open_interrupts": [
            {"id": i["id"], "kind": i["kind"], "message": i["message"], "payload": i["payload"],
             "response_schema": i["response_schema"],
             **({"quorum": quorum[i["id"]].model_dump(mode="json")} if i["id"] in quorum else {})}
            for i in interrupts
        ],
        "last_event_seq": last_seq,
        "pending_runs": [{"id": str(r[0]), "kind": str(r[1])} for r in pending],
    }


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_CHAT_CHARS)


@router.post("/api/cases/{case_id}/messages", status_code=202)
async def post_message(
    case_id: str,
    body: MessageBody,
    sess: MutatingSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    tenant = require_any_role(sess, WRITE_ROLES, "sending messages")
    key = _idempotency_key(idempotency_key)
    case = await load_case_or_404(tenant, case_id)
    text_value = body.text.strip()
    if not text_value:
        raise ApiError(422, "empty_message", "message text is empty")
    try:
        run_id = await enqueue_run(
            tenant=tenant, case_id=case_id, kind="chat", payload={"text": text_value}, actor=sess.actor(),
            idempotency_key=key, user_message=text_value,
        )
    except IdempotencyConflict as exc:
        raise ApiError(422, "idempotency_key_reused", "Idempotency-Key was used for a different request") from exc
    if case["title"] == _DEFAULT_TITLE:
        first_line = case_title_from(text_value)
        if first_line:
            await repo.set_case_title(case_id, first_line, tenant=tenant)
    return {"run_id": run_id}


UNDOABLE_CHANGE_SET_STATUSES = frozenset({"applied", "verified"})
"""Change set statuses the commit tracker's **Undo** (case action ``rollback``) accepts (``95`` §3)."""


class ActionBody(BaseModel):
    """Option actions carry ``option_id`` + ``plan_version``; ``rollback`` (M5) only
    ``change_set_id`` (an applied change set of this case)."""

    type: Literal["apply", "guide", "promote", "confirm_assumptions", "rollback"]
    option_id: str | None = Field(default=None, min_length=1, max_length=200)
    plan_version: int | None = Field(default=None, ge=1)
    change_set_id: str | None = Field(default=None, min_length=1, max_length=200)
    # confirm_assumptions (docs/contracts/90 §5): the unconfirmed keys the user confirms
    assumption_keys: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default=[], max_length=MAX_PAYLOAD_KEYS)  # contracts + web: at most 50


@router.post("/api/cases/{case_id}/actions", status_code=202)
async def post_action(
    case_id: str,
    body: ActionBody,
    sess: MutatingSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    tenant = require_any_role(sess, WRITE_ROLES, "acting on plans")
    key = _idempotency_key(idempotency_key)
    await load_case_or_404(tenant, case_id)
    if body.type == "rollback":
        return await _rollback_action(tenant, case_id, body, sess, key)
    if body.option_id is None or body.plan_version is None or body.change_set_id is not None:
        raise ApiError(422, "invalid_request", f"{body.type} requires option_id and plan_version (and no change_set_id)")
    plan = await repo.latest_plan(case_id, tenant=tenant)
    if plan is None or plan.version != body.plan_version:
        raise ApiError(409, "stale_plan", "the plan changed since this card was shown",
                       current_plan_version=plan.version if plan else None)
    if plan.option(body.option_id) is None:
        raise ApiError(422, "unknown_option", "the option is not part of this plan")
    try:
        payload = ActionPayload(type=body.type, option_id=body.option_id, plan_version=body.plan_version,
                                assumption_keys=list(body.assumption_keys))
    except ValueError as exc:
        raise ApiError(422, "invalid_action", "assumption_keys are required for confirm_assumptions "
                       "and not allowed for other actions") from exc
    try:
        run_id = await enqueue_run(
            tenant=tenant, case_id=case_id, kind="action", payload=payload.model_dump(mode="json"),
            actor=sess.actor(), idempotency_key=key,
        )
    except IdempotencyConflict as exc:
        raise ApiError(422, "idempotency_key_reused", "Idempotency-Key was used for a different request") from exc
    return {"run_id": run_id}


async def _rollback_action(tenant: str, case_id: str, body: ActionBody, sess: Any, key: str) -> dict[str, Any]:
    """``rollback {change_set_id}`` (M5 Undo): the change set must belong to the case and be applied
    (or verified); the worker then runs policy (always human) → Merxa rollback → reconcile."""
    if (body.change_set_id is None or body.option_id is not None or body.plan_version is not None
            or body.assumption_keys):
        raise ApiError(422, "invalid_request", "rollback requires change_set_id only")
    cs = await repo.get_change_set(body.change_set_id, tenant=tenant)
    if cs is None or cs.get("case_id") != case_id or cs.get("tenant") != tenant:
        raise ApiError(404, "not_found", "change set not found")
    if cs.get("status") not in UNDOABLE_CHANGE_SET_STATUSES:
        raise ApiError(409, "not_undoable", "only applied changes can be undone", change_set_status=cs.get("status"))
    payload = ActionPayload(type="rollback", change_set_id=body.change_set_id)
    pending = await _pending_rollback(tenant, case_id, body.change_set_id)
    if pending is not None and pending["key"] != key:
        # one Undo per change set at a time (a second one would only open a second approval card)
        raise ApiError(409, "rollback_pending", "an undo of these changes is already in progress",
                       run_id=pending["id"])
    try:
        run_id = await enqueue_run(
            tenant=tenant, case_id=case_id, kind="action", payload=payload.model_dump(mode="json"),
            actor=sess.actor(), idempotency_key=key,
        )
    except IdempotencyConflict as exc:
        raise ApiError(422, "idempotency_key_reused", "Idempotency-Key was used for a different request") from exc
    return {"run_id": run_id}


async def _pending_rollback(tenant: str, case_id: str, change_set_id: str) -> dict[str, Any] | None:
    """A rollback run of this change set that is still queued/running or waits on an open approval."""
    async with tenant_session(tenant) as s:
        row = (await s.execute(
            text(
                """
                SELECT r.id, r.idempotency_key FROM run r
                WHERE r.case_id = :c AND r.kind = 'action' AND r.payload->>'type' = 'rollback'
                  AND r.payload->>'change_set_id' = :cs
                  AND (r.status IN ('queued', 'running')
                       OR (r.status = 'paused' AND EXISTS (SELECT 1 FROM interrupt i
                                                           WHERE i.run_id = r.id AND i.status = 'PENDING')))
                ORDER BY r.created_at DESC LIMIT 1
                """
            ),
            {"c": case_id, "cs": change_set_id},
        )).first()
    return {"id": str(row[0]), "key": row[1]} if row is not None else None


# ───────────────────────────── Guide checklists (M5, 95 §2) ─────────────────────────────


class StepBody(BaseModel):
    status: Literal["done", "skipped"]


@router.get("/api/cases/{case_id}/checklists")
async def list_case_checklists(case_id: str, sess: CurrentSession) -> dict[str, Any]:
    tenant = require_any_role(sess, READ_ROLES, "reading cases")
    await load_case_or_404(tenant, case_id)
    return {"items": jsonable_items(await checklists.list_checklists(tenant, case_id))}


def jsonable_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: _iso(v) for k, v in item.items()} for item in items]


@router.post("/api/cases/{case_id}/checklists/{checklist_id}/steps/{seq}")
async def tick_checklist_step(case_id: str, checklist_id: str, seq: int, body: StepBody,
                              sess: MutatingSession) -> dict[str, Any]:
    """A person marks a guide step ``done`` or ``skipped`` (audited). ``verified`` is set only by
    read-back; a verified step answers 409 ``already_verified``."""
    tenant = require_any_role(sess, WRITE_ROLES, "updating guide steps")
    await load_case_or_404(tenant, case_id)
    if not 1 <= seq <= 1000 or not checklist_id or len(checklist_id) > 200:
        raise ApiError(404, "not_found", "step not found")
    try:
        out = await checklists.set_step_status(tenant, case_id, checklist_id, seq, body.status, sess.actor())
    except checklists.ChecklistError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    out["checklist"] = {k: _iso(v) for k, v in out["checklist"].items()}
    return out


# ───────────────────────────── SSE ─────────────────────────────


SESSION_RECHECK_S = 30.0
"""How often (at most) an open stream re-checks that its BFF session still exists."""


class _StreamTarget(BaseModel):
    tenant: str
    case_id: str
    after: int
    session_id: str


async def _stream_target(
    case_id: str,
    sess: CurrentSession,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[str | None, Query()] = None,
) -> _StreamTarget:
    """Resolved before the stream starts, so auth/404/400 are real HTTP errors."""
    tenant = require_any_role(sess, READ_ROLES, "following cases")
    await load_case_or_404(tenant, case_id)
    try:
        cursor = CaseCursor.from_request(tenant=tenant, case_id=case_id, last_event_id=last_event_id, after=after)
    except (ValueError, ValidationError) as exc:
        raise ApiError(400, "invalid_cursor", "Last-Event-ID / after must be a non-negative integer") from exc
    return _StreamTarget(tenant=tenant, case_id=case_id, after=cursor.after_seq, session_id=sess.id)


@router.get("/api/cases/{case_id}/events", response_class=EventSourceResponse)
async def case_events(
    request: Request, target: Annotated[_StreamTarget, Depends(_stream_target)]
) -> AsyncIterator[ServerSentEvent]:
    """Replay ``seq > Last-Event-ID`` from Postgres, then tail Valkey. Keep-alive comments every
    15 s (FastAPI). Disconnecting never cancels the run. The stream ends (the browser then
    reconnects and gets 401) once the session was logged out / revoked, and at once when the
    process shuts down (``api.shutdown``)."""
    bus = get_event_bus()
    yield ServerSentEvent(retry=3000, comment="keelson stream")
    checked = time.monotonic()
    events = bus.replay_then_tail(tenant=target.tenant, case_id=target.case_id, after=target.after)
    # Ends at once on process shutdown (SIGTERM), so uvicorn does not wait for / cancel open
    # streams; the browser reconnects with Last-Event-ID.
    async for seq, event in until_shutdown(events):
        if time.monotonic() - checked >= SESSION_RECHECK_S:
            if not await session_alive(target.session_id):
                return
            checked = time.monotonic()
        data = json.dumps(event, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        yield ServerSentEvent(raw_data=data, event="message", id=str(seq) if seq else None)


# ───────────────────────────── blocks ─────────────────────────────


@router.get("/api/blocks/{block_id}/data")
async def block_data(block_id: str, sess: CurrentSession) -> JSONResponse:
    tenant = require_any_role(sess, READ_ROLES, "reading blocks")
    if not is_block_id(block_id):
        raise ApiError(404, "not_found", "block not found")
    block = await repo.get_block(block_id, tenant=tenant)
    if block is None or not block["has_data"]:
        raise ApiError(404, "not_found", "block data not found")
    data = await repo.get_block_data(block_id, tenant=tenant)
    return JSONResponse(data, headers={"Cache-Control": "private, max-age=60"})

