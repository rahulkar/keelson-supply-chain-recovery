"""Write endpoints: change sets (+ custom methods), PATCH with If-Match, demo director."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from merxa_api import tables as t
from merxa_api.api.deps import (
    CHANGESET_READ_SCOPES,
    IdempotencyKey,
    IfMatch,
    IfNoneMatch,
    StateDep,
    etag,
    parse_if_match,
    require_demo_admin,
    require_scope,
)
from merxa_api.errors import Problem, not_found
from merxa_api.security.principal import SCOPE_APPLY, SCOPE_APPLY_AUTO, SCOPE_PREVIEW, Principal
from merxa_api.services import audit, idempotency
from merxa_api.services.audit import Attribution
from merxa_api.services.changesets import Outcome
from merxa_api.services.common import clamp_limit, jsonable, row_dict

router = APIRouter(prefix="/v1", tags=["change-sets"])

PreviewScope = Annotated[Principal, Depends(require_scope(SCOPE_PREVIEW))]
ApplyScope = Annotated[Principal, Depends(require_scope(SCOPE_APPLY, SCOPE_APPLY_AUTO))]
ChangeSetRead = Annotated[Principal, Depends(require_scope(*CHANGESET_READ_SCOPES))]
DemoAdmin = Annotated[Principal, Depends(require_demo_admin)]


class Origin(BaseModel):
    model_config = ConfigDict(extra="allow")
    system: str | None = None
    case_ref: str | None = None
    plan_version: int | str | None = None


class CreateChangeSet(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    origin: Origin = Field(default_factory=Origin)


class AddItems(BaseModel):
    items: list[dict[str, Any]]


class ApplyBody(BaseModel):
    preview_hash: str
    approval_ref: str
    approval_receipt: str


class RollbackBody(BaseModel):
    approval_ref: str
    approval_receipt: str


class PatchChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class PatchSupplierMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["new", "read"]


class InjectBody(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AdvanceClockBody(BaseModel):
    days: int


def _respond(outcome: Outcome) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else {}
    if outcome.status_code >= 400:
        body = {
            "type": "urn:merxa:problem:change_set_conflict",
            "title": "Change set conflict",
            "status": outcome.status_code,
            "code": "change_set_conflict",
            "detail": "one or more items no longer match the current data; nothing was applied",
            **outcome.body,
        }
        return JSONResponse(
            body, status_code=outcome.status_code, headers=headers, media_type="application/problem+json"
        )
    return JSONResponse(outcome.body, status_code=outcome.status_code, headers=headers)


# ───────────── change sets ─────────────
@router.get("/change-sets")
async def list_change_sets(
    state: StateDep, _: ChangeSetRead, limit: int | None = None, cursor: str | None = None, status: str | None = None
) -> dict[str, Any]:
    return await state.changesets.list(limit=clamp_limit(limit), cursor=cursor, status=status)


@router.get("/change-sets/{cs_id}")
async def get_change_set(
    cs_id: str, state: StateDep, _: ChangeSetRead, response: Response, if_none_match: IfNoneMatch = None
) -> Any:
    cs = await state.changesets.get(cs_id)
    tag = etag(cs["version"])
    if if_none_match and if_none_match.strip() == tag:
        return Response(status_code=304, headers={"ETag": tag})
    response.headers["ETag"] = tag
    return cs


@router.get("/change-sets/{cs_id}/operator-guide")
async def operator_guide(cs_id: str, state: StateDep, _: ChangeSetRead, locale: str = "en") -> dict[str, Any]:
    return await state.changesets.operator_guide(cs_id, locale)


@router.post("/change-sets", status_code=201)
async def create_change_set(
    body: CreateChangeSet, state: StateDep, principal: PreviewScope, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    outcome = await state.changesets.create(
        principal, title=body.title, origin=body.origin.model_dump(exclude_none=True), idempotency_key=idempotency_key
    )
    return _respond(outcome)


@router.post("/change-sets/{cs_id}/items")
async def add_items(
    cs_id: str, body: AddItems, state: StateDep, principal: PreviewScope, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return _respond(
        await state.changesets.add_items(principal, cs_id, items=body.items, idempotency_key=idempotency_key)
    )


@router.post("/change-sets/{cs_id}:validate")
async def validate(
    cs_id: str, state: StateDep, principal: PreviewScope, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return _respond(await state.changesets.validate(principal, cs_id, idempotency_key=idempotency_key))


@router.post("/change-sets/{cs_id}:apply")
async def apply(
    cs_id: str, body: ApplyBody, state: StateDep, principal: ApplyScope, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    outcome = await state.changesets.apply(
        principal,
        cs_id,
        preview_hash=body.preview_hash,
        approval_ref=body.approval_ref,
        approval_receipt=body.approval_receipt,
        idempotency_key=idempotency_key,
    )
    return _respond(outcome)


@router.post("/change-sets/{cs_id}:rollback")
async def rollback(
    cs_id: str,
    body: RollbackBody,
    state: StateDep,
    principal: ApplyScope,
    idempotency_key: IdempotencyKey = None,
) -> JSONResponse:
    outcome = await state.changesets.rollback(
        principal,
        cs_id,
        approval_ref=body.approval_ref,
        approval_receipt=body.approval_receipt,
        idempotency_key=idempotency_key,
    )
    return _respond(outcome)


@router.patch("/change-sets/{cs_id}")
async def patch_change_set(
    cs_id: str,
    body: PatchChangeSet,
    state: StateDep,
    principal: PreviewScope,
    response: Response,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    expected = parse_if_match(if_match)
    cs = t.change_set
    try:
        cond = cs.c.id == uuid.UUID(cs_id)
    except ValueError:
        cond = cs.c.code == cs_id
    async with state.engine.begin() as conn:
        row = (await conn.execute(select(cs).where(cond).with_for_update())).first()
        if row is None:
            raise not_found("change set", cs_id)
        if row.version != expected:
            raise Problem(
                412,
                "precondition_failed",
                f"version is {row.version}, If-Match was {expected}",
                extra={"current_version": row.version},
                headers={"ETag": etag(row.version)},
            )
        if row.status not in ("draft", "validated"):
            raise Problem(409, "invalid_state", f"cannot edit a change set in status {row.status}")
        new_version = (
            await conn.execute(update(cs).where(cs.c.id == row.id).values(title=body.title).returning(cs.c.version))
        ).scalar_one()
        await audit.write(
            conn,
            Attribution.of(principal, change_set_code=row.code),
            entity_type="change_set",
            entity_ref=row.code,
            action="update",
            before={"title": row.title},
            after={"title": body.title},
        )
    response.headers["ETag"] = etag(new_version)
    return {"change_set_id": str(row.id), "code": row.code, "title": body.title, "version": new_version}


# ───────────── supplier messages ─────────────
@router.patch("/supplier-messages/{msg_id}")
async def patch_supplier_message(
    msg_id: str,
    body: PatchSupplierMessage,
    state: StateDep,
    principal: PreviewScope,
    response: Response,
    if_match: IfMatch = None,
) -> dict[str, Any]:
    expected = parse_if_match(if_match)
    try:
        mid = uuid.UUID(msg_id)
    except ValueError as exc:
        raise not_found("supplier message", msg_id) from exc
    m = t.supplier_message
    async with state.engine.begin() as conn:
        row = (await conn.execute(select(m).where(m.c.id == mid).with_for_update())).first()
        if row is None:
            raise not_found("supplier message", msg_id)
        if row.version != expected:
            raise Problem(
                412,
                "precondition_failed",
                f"version is {row.version}, If-Match was {expected}",
                extra={"current_version": row.version},
                headers={"ETag": etag(row.version)},
            )
        updated = (await conn.execute(update(m).where(m.c.id == mid).values(status=body.status).returning(m))).one()
        await audit.write(
            conn,
            Attribution.of(principal),
            entity_type="supplier_message",
            entity_ref=msg_id,
            action="update",
            before={"status": row.status},
            after={"status": body.status},
        )
    response.headers["ETag"] = etag(updated.version)
    return row_dict(updated, exclude=("updated_at",))


# ───────────── demo director ─────────────
async def _idempotent_admin(
    state: StateDep, principal: Principal, key: str | None, op: str, body: Any, run: Any
) -> JSONResponse:
    claim = await idempotency.begin(
        state.engine,
        principal=principal.sub,
        key=idempotency.validate_key(key),
        request_sha256=idempotency.request_fingerprint(op, body),
    )
    if isinstance(claim, idempotency.Replay):
        return JSONResponse(claim.response, status_code=claim.status_code, headers={"Idempotent-Replayed": "true"})
    try:
        result = jsonable(await run())
    except BaseException:
        await idempotency.abandon(state.engine, claim)
        raise
    async with state.engine.begin() as conn:
        await idempotency.complete(conn, claim, 200, result)
    return JSONResponse(result)


@router.post("/admin/scenario:reset", tags=["demo"])
async def scenario_reset(state: StateDep, principal: DemoAdmin, idempotency_key: IdempotencyKey = None) -> JSONResponse:
    return await _idempotent_admin(
        state, principal, idempotency_key, "admin.reset", {}, lambda: state.admin.reset(principal)
    )


@router.post("/admin/scenario:inject", tags=["demo"])
async def scenario_inject(
    body: InjectBody, state: StateDep, principal: DemoAdmin, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await _idempotent_admin(
        state,
        principal,
        idempotency_key,
        "admin.inject",
        body.model_dump(),
        lambda: state.admin.inject(principal, body.kind, body.payload),
    )


@router.post("/admin/scenario:advance-clock", tags=["demo"])
async def scenario_advance_clock(
    body: AdvanceClockBody, state: StateDep, principal: DemoAdmin, idempotency_key: IdempotencyKey = None
) -> JSONResponse:
    return await _idempotent_admin(
        state,
        principal,
        idempotency_key,
        "admin.advance_clock",
        body.model_dump(),
        lambda: state.admin.advance_clock(principal, body.days),
    )
