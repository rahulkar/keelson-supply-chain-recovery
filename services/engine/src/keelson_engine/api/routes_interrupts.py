"""Interrupt decisions and run cancellation."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field, ValidationError

from keelson_contracts.runs import MAX_DECISION_REASON_CHARS, MAX_PAYLOAD_KEYS, ApproachEdit, Decision, ResumePayload
from keelson_engine.api.deps import READ_ROLES, WRITE_ROLES, MutatingSession, require_any_role
from keelson_engine.api.errors import ApiError
from keelson_engine.core import repo
from keelson_engine.core.interrupts import (
    InterruptClosed,
    InterruptNotFound,
    InvalidDecision,
    NotEligible,
    StaleDecision,
    get_interrupt,
    record_decision_ex,
)
from keelson_engine.core.runs import RunNotFound, emit_run_error, get_run, request_cancel
from keelson_engine.core.tokens import APPLY_SCOPE, apply_ready, get_token_broker

__all__ = ["APPLY_SCOPE", "router"]

router = APIRouter()


class DecisionBody(BaseModel):
    """``edits`` is the M1 free text or (M4) typed approach edits ``[{key, value_int | value_ids}]``
    (contract ``ApproachEdit``); ``removed_checks`` are approach-card task ids (M4). Both typed
    fields are only valid with ``decision: "edit"`` (checked against ``ResumePayload``)."""

    decision: Decision
    plan_version: int | None = Field(default=None, ge=1)
    change_set_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    edits: Annotated[str, Field(max_length=8000)] | Annotated[
        list[ApproachEdit], Field(max_length=MAX_PAYLOAD_KEYS)] | None = None
    removed_checks: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default=[], max_length=MAX_PAYLOAD_KEYS)
    reason: Annotated[str, Field(min_length=1, max_length=MAX_DECISION_REASON_CHARS)] | None = None
    """M5: the decider's note on a ``reject``/``revise`` (shown on the approval card)."""


def _check_typed_edit(interrupt_id: str, body: DecisionBody) -> None:
    """Typed edits / removed checks must form a valid ``ResumePayload`` edit (only with
    ``edit``, unique keys and check ids) → else 422 ``invalid_decision``."""
    if not isinstance(body.edits, list) and not body.removed_checks:
        return
    try:
        ResumePayload(interrupt_id=interrupt_id, decision=body.decision, edits=body.edits,
                      removed_checks=body.removed_checks)
    except ValidationError as exc:
        problems = "; ".join(str(e.get("msg", "")).removeprefix("Value error, ") for e in exc.errors()[:3])
        raise ApiError(422, "invalid_decision", problems or "the edit is not valid") from exc


@router.post("/api/interrupts/{interrupt_id}/decision", status_code=202)
async def decide(interrupt_id: str, body: DecisionBody, sess: MutatingSession) -> dict[str, Any]:
    # Anyone with a case role may reach the quorum check below (M5: compliance co-approves four-eyes
    # change sets without being a planner/approver); the interrupt's quorum roles decide.
    tenant = require_any_role(sess, READ_ROLES, "deciding")
    if len(interrupt_id) > 200:
        raise ApiError(404, "not_found", "interrupt not found")
    _check_typed_edit(interrupt_id, body)
    intr = await get_interrupt(tenant, interrupt_id)
    if intr is None or intr["tenant"] != tenant:
        raise ApiError(404, "not_found", "interrupt not found")
    if intr["status"] in ("SUPERSEDED", "EXPIRED"):
        raise ApiError(
            409, "superseded", f"this request is {intr['status'].lower()}", interrupt_status=intr["status"]
        )
    quorum_roles = [str(r) for r in (intr["quorum_spec"] or {}).get("roles") or []]
    if quorum_roles and not sess.has_any(*quorum_roles):
        raise ApiError(403, "forbidden", "you do not hold a role that can decide this", required_roles=quorum_roles)

    if intr["kind"] == "approval" and body.decision == "approve" and intr["status"] == "PENDING":
        if body.change_set_hash != intr["change_set_hash"] or body.plan_version != intr["plan_version"]:
            raise ApiError(409, "stale", "the plan or change set changed since this card was shown",
                           plan_version=intr["plan_version"], change_set_hash=intr["change_set_hash"])
    if body.reason is not None and body.decision not in ("reject", "revise"):
        raise ApiError(422, "invalid_decision", "a reason is only allowed with reject or revise")
    # Approve pre-flight (M5): only the quorum member who will EXECUTE (counted under 'approver')
    # needs an active Merxa link with changes:apply — checked for the decider when that is them,
    # and for the final executor when this approval completes the quorum. Co-approvers (e.g.
    # compliance) never need a link. The source of truth is that person's own link (never the
    # approval card's connect.status). Same rule as GET /api/connectors/{slug}/link → apply_ready.
    connector = str((intr["payload"] or {}).get("connector") or "merxa")
    broker = get_token_broker()

    async def executor_check(executor: dict[str, Any]) -> None:
        link = await broker.link_status(tenant=tenant, connector=connector, keelson_sub=executor["sub"])
        if apply_ready(link):
            return
        if executor["is_decider"]:
            raise ApiError(
                412, "connect_required", "connect your Merxa account to approve",
                connect_url=broker.connect_url(connector, return_to=f"/cases/{intr['case_id']}"),
                link_status=link["status"],
            )
        raise ApiError(
            412, "executor_connect_required",
            f"{executor['name'] or executor['username']} applies this change and must connect their Merxa account "
            "first",
            executor={"username": executor["username"], "name": executor["name"], "role": executor["role"]},
            link_status=link["status"],
        )

    try:
        outcome = await record_decision_ex(
            tenant=tenant,
            interrupt_id=interrupt_id,
            decider=sess.actor(),
            decision=body.decision,
            plan_version=body.plan_version,
            change_set_hash=body.change_set_hash,
            edits=body.edits,
            removed_checks=body.removed_checks,
            auth_time=sess.auth_time,
            reason=body.reason,
            executor_check=executor_check,
        )
    except InterruptNotFound as exc:
        raise ApiError(404, "not_found", "interrupt not found") from exc
    except InterruptClosed as exc:
        raise ApiError(409, "superseded", str(exc), interrupt_status=exc.status) from exc
    except StaleDecision as exc:
        raise ApiError(409, "stale", str(exc)) from exc
    except NotEligible as exc:
        raise ApiError(403, "forbidden", str(exc), **exc.details) from exc
    except InvalidDecision as exc:
        raise ApiError(422, "invalid_decision", str(exc)) from exc
    await repo.admin_audit(
        sess.actor(), f"interrupt.{body.decision}", f"interrupt:{interrupt_id}", None,
        {"kind": intr["kind"], "plan_version": body.plan_version, "change_set_hash": body.change_set_hash,
         "outcome": outcome.status, "reason": body.reason,
         "quorum_remaining": outcome.quorum.remaining if outcome.quorum is not None else None},
        tenant=tenant,
    )
    out: dict[str, Any] = ({"status": "resumed", "run_id": outcome.run_id} if outcome.status == "resolved"
                           else {"status": "recorded"})
    if outcome.quorum is not None:
        out["quorum"] = outcome.quorum.model_dump(mode="json")
    return out


@router.post("/api/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, sess: MutatingSession) -> dict[str, Any]:
    tenant = require_any_role(sess, WRITE_ROLES, "cancelling runs")
    if len(run_id) > 64:
        raise ApiError(404, "not_found", "run not found")
    run = await get_run(tenant, run_id)
    if run is None or run.tenant != tenant:
        raise ApiError(404, "not_found", "run not found")
    try:
        previous, status = await request_cancel(tenant, run_id)
    except RunNotFound as exc:
        raise ApiError(404, "not_found", "run not found") from exc
    if previous == "queued" and status == "cancelled":
        await emit_run_error(tenant, run.case_id, run_id, message="Run cancelled.", code="CANCELLED")
    return {"status": status, "cancel_requested": status in ("running", "cancelled")}
