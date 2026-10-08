"""Human-in-the-loop interrupts (approach + approval cards).

* :func:`create_interrupt` — called by the runtime when a node yields ``RequestInput``.
* :func:`record_decision` — called by the API: checks freshness (``change_set_hash`` +
  ``plan_version`` for approvals), eligibility (quorum roles, ``exclude_initiator``) and
  records one ``approval_decision`` per person. When the quorum is met it flips the
  interrupt to ``RESOLVED`` and inserts the ``resume_request`` + the ``kind='resume'`` run
  in the **same transaction** (the run reuses the paused run's ADK ``session_id`` and the
  interrupt's ``invocation_id``).
* :func:`supersede_open` — a new plan version / dirtied case invalidates open interrupts.

Quorum spec (``quorum_spec`` jsonb): ``{"roles": ["approver"], "count": 1,
"exclude_initiator": false}``. Only ``approve`` accumulates towards ``count``; any other
decision by an eligible person resolves the interrupt at once (a single reject/revise/
cancel stops the plan).

M5 (``95-m5-remediation.md`` §1): the quorum spec is a ``keelson_contracts.policy.Requirement``
(the matched policy rule's requirement, e.g. four-eyes ``{roles: [approver, compliance], count: 2,
exclude_initiator: true}``). ``approval_decision.roles`` keeps the roles each person held, and
approvals are counted with ``quorum_assign`` (distinct people, one of each role when ``count >=
len(roles)``, the excluded initiator never counts). :class:`DecisionOutcome` carries the
:class:`~keelson_contracts.policy.QuorumView` (API + approval card progress). The executor is the
member counted under ``approver`` (:func:`quorum_executor`), listed first in ``decided_by``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_contracts.agui import approach_response_schema, approval_response_schema
from keelson_contracts.policy import QuorumApproval, QuorumCandidate, QuorumView, Requirement, quorum_assign
from keelson_contracts.runs import APPROACH_DECISIONS, APPROVAL_DECISIONS, ApproachEdit, ResumePayload
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.runs import insert_run, wake_workers
from keelson_engine.core.sql import dumps

__all__ = [
    "DEFAULT_APPROACH_QUORUM",
    "DEFAULT_APPROVAL_QUORUM",
    "DecisionOutcome",
    "EXECUTOR_ROLE",
    "ExecutorCheck",
    "InterruptClosed",
    "InterruptError",
    "InterruptNotFound",
    "InvalidDecision",
    "NotEligible",
    "StaleDecision",
    "build_quorum_view",
    "create_interrupt",
    "get_interrupt",
    "list_open_interrupts",
    "quorum_executor",
    "quorum_progress",
    "quorum_requirement",
    "record_decision",
    "record_decision_ex",
    "supersede_open",
]

log = get_logger(__name__)

DEFAULT_APPROACH_QUORUM: dict[str, Any] = {
    "roles": ["planner", "approver", "tenant-admin"],
    "count": 1,
    "exclude_initiator": False,
}
DEFAULT_APPROVAL_QUORUM: dict[str, Any] = {"roles": ["approver"], "count": 1, "exclude_initiator": False}
EXECUTOR_ROLE = "approver"
"""The quorum role whose holder's delegated token applies the change set (``95`` §1)."""
_REQUIREMENT_KEYS = frozenset({"roles", "count", "exclude_initiator"})

_DEFAULT_MESSAGES = {
    "approach": "Confirm how I should approach this disruption.",
    "approval": "Approve these changes before they are applied in Merxa.",
}


class InterruptError(Exception):
    code = "interrupt_error"


class InterruptNotFound(InterruptError):
    code = "not_found"


class InterruptClosed(InterruptError):
    """Not PENDING any more (resolved, superseded or expired) → HTTP 409."""

    code = "superseded"

    def __init__(self, status: str) -> None:
        super().__init__(f"interrupt is {status}")
        self.status = status


class StaleDecision(InterruptError):
    """``change_set_hash`` / ``plan_version`` differ from the current values → HTTP 409."""

    code = "stale"


class NotEligible(InterruptError):
    """Decider lacks a quorum role, is the excluded initiator, or their approval cannot advance
    the quorum → HTTP 403 (``details``: ``required_roles`` / ``missing_roles``)."""

    code = "forbidden"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class InvalidDecision(InterruptError):
    """Decision not valid for this interrupt kind / missing fields → HTTP 422."""

    code = "invalid_decision"


Edits = str | Sequence[ApproachEdit | Mapping[str, Any]] | None
"""M1 free text, or M4 typed approach edits (``ApproachEdit`` or its JSON form)."""


def _typed_edits(edits: Edits) -> str | list[ApproachEdit] | None:
    if edits is None or isinstance(edits, str):
        return edits
    try:
        return [e if isinstance(e, ApproachEdit) else ApproachEdit.model_validate(e) for e in edits]
    except ValidationError as exc:
        raise InvalidDecision(f"invalid typed edit: {exc.errors()[0].get('msg', 'invalid')}") from exc


def _stored_edits(edits: str | list[ApproachEdit] | None, removed_checks: list[str]) -> str | None:
    """``approval_decision.edits`` (text): free text as is; typed edits / removed checks as JSON."""
    if not isinstance(edits, list) and not removed_checks:
        return edits
    return dumps({
        "edits": [e.model_dump(mode="json", exclude_none=True) for e in edits] if isinstance(edits, list) else edits,
        "removed_checks": removed_checks,
    })


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    status: Literal["recorded", "resolved"]
    run_id: str | None = None
    quorum: QuorumView | None = field(default=None, compare=False)
    """Quorum progress after this decision (approval interrupts)."""


async def create_interrupt(
    *,
    tenant: str,
    case_id: str,
    run_id: str,
    kind: Literal["approach", "approval"],
    interrupt_id: str,
    payload: dict[str, Any],
    quorum_spec: dict[str, Any],
    plan_version: int | None,
    change_set_hash: str | None,
    invocation_id: str,
    session_id: str | None = None,
    message: str | None = None,
) -> None:
    """Persist a PENDING interrupt (idempotent on ``interrupt_id``: a crash-retried node that
    re-yields the same request does not create a second one).

    ``session_id`` defaults to the run's ADK session id; ``message`` defaults to
    ``payload['message']`` or a generic text.
    """
    if kind not in ("approach", "approval"):
        raise ValueError("kind must be approach|approval")
    if not interrupt_id.startswith(f"{kind}-"):
        raise ValueError(f"interrupt_id must start with '{kind}-'")
    if kind == "approval" and (plan_version is None or not change_set_hash):
        raise ValueError("approval interrupts need plan_version and change_set_hash")
    quorum = {**(DEFAULT_APPROVAL_QUORUM if kind == "approval" else DEFAULT_APPROACH_QUORUM), **(quorum_spec or {})}
    schema = approval_response_schema() if kind == "approval" else approach_response_schema()
    text_message = message or str(payload.get("message") or _DEFAULT_MESSAGES[kind])
    async with tenant_session(tenant) as s:
        if session_id is None:
            res = await s.execute(text("SELECT session_id, created_by FROM run WHERE id = :r"), {"r": run_id})
            row = res.first()
            if row is None:
                raise ValueError(f"unknown run {run_id}")
            session_id = str(row[0])
            initiator = str(row[1])
        else:
            res = await s.execute(text("SELECT created_by FROM run WHERE id = :r"), {"r": run_id})
            initiator = str(res.scalar_one_or_none() or "")
        await s.execute(
            text(
                """
                INSERT INTO interrupt (id, tenant, case_id, run_id, kind, status, quorum_spec, plan_version,
                                       change_set_hash, payload, message, response_schema, invocation_id,
                                       session_id, initiator_sub)
                VALUES (:id, :tenant, :case_id, :run_id, :kind, 'PENDING', CAST(:quorum AS jsonb), :pv, :h,
                        CAST(:payload AS jsonb), :message, CAST(:schema AS jsonb), :inv, :sid, :initiator)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": interrupt_id,
                "tenant": tenant,
                "case_id": case_id,
                "run_id": run_id,
                "kind": kind,
                "quorum": dumps(quorum),
                "pv": plan_version,
                "h": change_set_hash,
                "payload": dumps(payload),
                "message": text_message,
                "schema": dumps(schema),
                "inv": invocation_id,
                "sid": session_id,
                "initiator": initiator or None,
            },
        )


_INTERRUPT_COLUMNS = (
    "id, tenant, case_id, run_id, kind, status, quorum_spec, plan_version, change_set_hash, payload, message, "
    "response_schema, invocation_id, session_id, initiator_sub, superseded_reason, created_at, resolved_at"
)


async def get_interrupt(tenant: str, interrupt_id: str) -> dict[str, Any] | None:
    async with tenant_session(tenant) as s:
        res = await s.execute(text(f"SELECT {_INTERRUPT_COLUMNS} FROM interrupt WHERE id = :id"), {"id": interrupt_id})
        m = res.mappings().first()
        return dict(m) if m is not None else None


async def list_open_interrupts(tenant: str, case_id: str) -> list[dict[str, Any]]:
    async with tenant_session(tenant) as s:
        return await open_interrupts_in(s, case_id)


async def open_interrupts_in(session: AsyncSession, case_id: str) -> list[dict[str, Any]]:
    """``PENDING`` interrupts of the case visible to ``session`` (oldest first)."""
    res = await session.execute(
        text(
            f"SELECT {_INTERRUPT_COLUMNS} FROM interrupt WHERE case_id = :c AND status = 'PENDING' "
            "ORDER BY created_at"
        ),
        {"c": case_id},
    )
    return [dict(m) for m in res.mappings().all()]


def quorum_requirement(quorum_spec: Mapping[str, Any] | None, kind: str = "approval") -> Requirement:
    """``interrupt.quorum_spec`` → :class:`Requirement` (unknown keys ignored; an unusable stored
    spec falls back to the kind's default quorum, never to "no quorum")."""
    default = DEFAULT_APPROVAL_QUORUM if kind == "approval" else DEFAULT_APPROACH_QUORUM
    spec = {**default, **{k: v for k, v in dict(quorum_spec or {}).items() if k in _REQUIREMENT_KEYS}}
    try:
        return Requirement.model_validate(spec)
    except ValidationError:
        log.warning("invalid quorum_spec; using the default quorum", kind=kind)
        return Requirement.model_validate(default)


def _held_required(required: Requirement, roles: Sequence[str]) -> list[str]:
    held = set(roles)
    return [r for r in required.roles if r in held]


async def _decision_rows(s: AsyncSession, interrupt_id: str) -> list[dict[str, Any]]:
    res = await s.execute(
        text(
            "SELECT approver_sub, approver_username, approver_name, role, roles, decision, reason, created_at "
            "FROM approval_decision WHERE interrupt_id = :i ORDER BY created_at, id"
        ),
        {"i": interrupt_id},
    )
    return [dict(m) for m in res.mappings().all()]


def _row_roles(row: Mapping[str, Any]) -> list[str]:
    roles = [str(r) for r in (row.get("roles") or [])]
    if not roles and row.get("role"):
        roles = [str(row["role"])]  # pre-M5 rows only stored the counted role
    return roles


def _candidates(rows: Sequence[Mapping[str, Any]]) -> list[QuorumCandidate]:
    return [
        QuorumCandidate(sub=str(r["approver_sub"]), name=str(r.get("approver_name") or r["approver_username"]),
                        roles=_row_roles(r), at=_aware(r["created_at"]))
        for r in rows
        if r["decision"] == "approve"
    ]


def _aware(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC)


def quorum_executor(view: QuorumView) -> QuorumApproval | None:
    """The quorum member whose delegated token applies the change set: the approval counted under
    :data:`EXECUTOR_ROLE` (``approver``) whatever the approval order (``None`` while nobody holds
    it yet); for a quorum without that role, the first approval."""
    if EXECUTOR_ROLE in view.required.roles:
        return next((a for a in view.approvals if a.role == EXECUTOR_ROLE), None)
    return view.approvals[0] if view.approvals else None


def build_quorum_view(intr: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> QuorumView:
    """Quorum progress of an interrupt from its ``approval_decision`` rows."""
    required = quorum_requirement(intr.get("quorum_spec"), str(intr.get("kind") or "approval"))
    view, _ = quorum_assign(required, _candidates(rows), initiator_sub=intr.get("initiator_sub"))
    return view


async def quorum_progress(tenant: str, interrupt_ids: Sequence[str]) -> dict[str, QuorumView]:
    """``{interrupt_id: QuorumView}`` for approval interrupts (API card progress)."""
    if not interrupt_ids:
        return {}
    out: dict[str, QuorumView] = {}
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(f"SELECT {_INTERRUPT_COLUMNS} FROM interrupt WHERE id = ANY(:ids) AND kind = 'approval'"),
            {"ids": list(interrupt_ids)},
        )
        intrs = [dict(m) for m in res.mappings().all()]
        for intr in intrs:
            out[intr["id"]] = build_quorum_view(intr, await _decision_rows(s, intr["id"]))
    return out


async def _check_current(s: AsyncSession, intr: dict[str, Any], plan_version: int, change_set_hash: str) -> None:
    """``approve`` must match the case's *current* plan and change set, not only the values
    the card was created with (defence in depth if a newer plan was saved without the
    open approval being superseded, or the change set row was revised)."""
    res = await s.execute(text("SELECT max(version) FROM plan WHERE case_id = :c"), {"c": intr["case_id"]})
    current_plan = res.scalar_one_or_none()
    if current_plan is not None and int(current_plan) != plan_version:
        raise StaleDecision("a newer plan exists for this case; review it before approving")
    change_set_id = (intr.get("payload") or {}).get("change_set_id")
    if change_set_id:
        res = await s.execute(text("SELECT hash FROM change_set WHERE id = :id"), {"id": str(change_set_id)})
        current_hash = res.scalar_one_or_none()
        if current_hash is None or current_hash != change_set_hash:
            raise StaleDecision("the change set changed since this card was shown")


ExecutorCheck = Callable[[dict[str, Any]], Awaitable[None]]
"""Approve pre-flight for the quorum member who will execute (``{sub, username, name, role,
is_decider}``); raises to refuse the decision (the transaction rolls back)."""


async def record_decision_ex(
    *,
    tenant: str,
    interrupt_id: str,
    decider: dict[str, Any],
    decision: str,
    plan_version: int | None = None,
    change_set_hash: str | None = None,
    edits: Edits = None,
    removed_checks: Sequence[str] = (),
    auth_time: dt.datetime | None = None,
    reason: str | None = None,
    executor_check: ExecutorCheck | None = None,
) -> DecisionOutcome:
    """Record one person's decision; resolve + enqueue the resume run when the quorum is met.

    ``decider`` = ``{sub, username, name, roles}`` of the authenticated user. ``edits`` is the
    M1 free text or (M4) typed :class:`ApproachEdit` values; ``removed_checks`` are approach-card
    task ids. Typed edits and removed checks are only valid with ``edit`` and travel to the
    worker in :class:`ResumePayload`; an invalid combination raises :class:`InvalidDecision`.

    M5 quorum (approval ``approve``): the person must hold a required role and not be the
    excluded initiator; approvals are counted by ``keelson_contracts.policy.quorum_assign`` over
    the roles each approver held (stored in ``approval_decision.roles``), so the same person never
    counts twice and an approval that cannot advance the quorum (e.g. a second approver while
    compliance is missing) is refused with :class:`NotEligible`. ``executor_check`` runs for the
    member counted under ``approver`` (the executor) — for the decider when that is them, and for
    the final executor when this approval completes the quorum. Any ``reject``/``revise`` by an
    eligible person resolves at once (``reason`` = their note).
    """
    typed = _typed_edits(edits)
    removed = [str(c) for c in removed_checks or ()]
    if decision != "edit" and (isinstance(typed, list) or removed):
        raise InvalidDecision("typed edits and removed_checks are only allowed with decision 'edit'")
    note = (reason or "").strip() or None
    if note is not None and decision not in ("reject", "revise"):
        raise InvalidDecision("a reason is only allowed with reject or revise")
    roles = [str(r) for r in decider.get("roles") or []]
    sub = str(decider.get("sub") or "")
    if not sub:
        raise NotEligible("decider has no subject")
    username = str(decider.get("username") or sub)
    name = str(decider.get("name") or "")
    resume_run: str | None = None
    view: QuorumView | None = None
    rejection: dict[str, Any] | None = None
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(f"SELECT {_INTERRUPT_COLUMNS} FROM interrupt WHERE id = :id FOR UPDATE"), {"id": interrupt_id}
        )
        m = res.mappings().first()
        if m is None:
            raise InterruptNotFound(interrupt_id)
        intr = dict(m)
        kind = intr["kind"]
        valid = APPROVAL_DECISIONS if kind == "approval" else APPROACH_DECISIONS
        if decision not in valid:
            raise InvalidDecision(f"decision {decision!r} is not valid for an {kind} interrupt")
        rows = await _decision_rows(s, interrupt_id)
        prior = next((r for r in rows if r["approver_sub"] == sub), None)
        if intr["status"] != "PENDING":
            # An identical repeat of a decision that resolved the interrupt is acknowledged.
            if intr["status"] == "RESOLVED" and prior is not None and prior["decision"] == decision:
                rr = await s.execute(
                    text("SELECT run_id FROM resume_request WHERE interrupt_id = :i"), {"i": interrupt_id}
                )
                return DecisionOutcome("resolved", rr.scalar_one_or_none(),
                                       build_quorum_view(intr, rows) if kind == "approval" else None)
            raise InterruptClosed(str(intr["status"]).lower())
        required = quorum_requirement(intr["quorum_spec"], kind)
        held = _held_required(required, roles)
        if not held:
            raise NotEligible("you do not hold a role that can decide this", required_roles=list(required.roles))
        if required.exclude_initiator and intr.get("initiator_sub") == sub:
            raise NotEligible("the initiator cannot approve their own change")
        if kind == "approval" and decision == "approve":
            if plan_version is None or not change_set_hash:
                raise InvalidDecision("approve requires plan_version and change_set_hash")
            if change_set_hash != intr["change_set_hash"] or plan_version != intr["plan_version"]:
                raise StaleDecision("the plan or change set changed since this card was shown")
            await _check_current(s, intr, plan_version, change_set_hash)
        if decision == "edit" and not (typed if isinstance(typed, list) else (typed or "").strip()) and not removed:
            raise InvalidDecision("edit requires edits text, typed edits or removed_checks")
        if prior is not None:
            if prior["decision"] != decision:
                raise InterruptClosed("decided")
            return DecisionOutcome("recorded", None, build_quorum_view(intr, rows) if kind == "approval" else None)

        now = dt.datetime.now(dt.UTC)
        role = held[0]
        decided_by: list[dict[str, Any]]
        if kind == "approval" and decision == "approve":
            candidates = [*_candidates(rows), QuorumCandidate(sub=sub, name=name or username, roles=roles, at=now)]
            view, not_counted = quorum_assign(required, candidates, initiator_sub=intr.get("initiator_sub"))
            if sub in not_counted:
                missing = list(view.missing_roles)
                waiting = ", ".join(missing) if missing else "other approvers"
                raise NotEligible(f"this approval is waiting for {waiting}; yours cannot count towards it",
                                  missing_roles=missing, required_roles=list(required.roles))
            role = next(a.role for a in view.approvals if a.sub == sub)
            executor = quorum_executor(view)
            if executor_check is not None and executor is not None and executor.sub == sub:
                await executor_check({"sub": sub, "username": username, "name": name, "role": executor.role,
                                      "is_decider": True})
        await s.execute(
            text(
                """
                INSERT INTO approval_decision (tenant, interrupt_id, approver_sub, approver_username, approver_name,
                                               role, roles, decision, change_set_hash, plan_version, edits,
                                               auth_time, reason, created_at)
                VALUES (:t, :i, :sub, :username, :name, :role, CAST(:roles AS jsonb), :decision, :h, :pv, :edits,
                        :auth_time, :reason, :now)
                """
            ),
            {
                "t": tenant,
                "i": interrupt_id,
                "sub": sub,
                "username": username,
                "name": name,
                "role": role,
                "roles": dumps(roles),
                "decision": decision,
                "h": change_set_hash,
                "pv": plan_version,
                "edits": _stored_edits(typed, removed),
                "auth_time": auth_time,
                "reason": note,
                "now": now,
            },
        )
        if view is not None:
            if not view.met:
                outcome = DecisionOutcome("recorded", None, view)
                resume_run = None
            else:
                executor = quorum_executor(view)
                usernames = {str(r["approver_sub"]): str(r["approver_username"]) for r in rows}
                usernames[sub] = username
                if executor_check is not None and executor is not None and executor.sub != sub:
                    await executor_check({"sub": executor.sub, "username": usernames.get(executor.sub, executor.sub),
                                          "name": executor.name, "role": executor.role, "is_decider": False})
                ordered = sorted(view.approvals, key=lambda a: 0 if executor is not None and a.sub == executor.sub
                                 else 1)
                decided_by = [{"sub": a.sub, "username": usernames.get(a.sub, a.sub), "name": a.name, "role": a.role}
                              for a in ordered]
        else:
            decided_by = [{"sub": sub, "username": username, "name": name, "role": role}]
            if kind == "approval" and decision in ("reject", "revise"):
                rejection = {"sub": sub, "name": name or username, "role": role, "at": now.isoformat(),
                             "reason": note, "decision": decision}
        if view is None or view.met:
            try:
                resume = ResumePayload(
                    interrupt_id=interrupt_id,
                    decision=decision,
                    plan_version=plan_version if kind == "approval" else None,
                    change_set_hash=change_set_hash if kind == "approval" else None,
                    edits=typed if decision == "edit" else None,
                    removed_checks=removed if decision == "edit" else [],
                    decided_by=decided_by,
                    reason=note,
                )
            except ValidationError as exc:  # rolls the decision row back with the transaction
                raise InvalidDecision(str(exc.errors()[0].get("msg", "invalid decision"))) from exc
            actor = {"sub": sub, "username": username, "name": name, "roles": roles}
            resume_run, _ = await insert_run(
                s,
                tenant=tenant,
                case_id=intr["case_id"],
                kind="resume",
                payload=resume.model_dump(mode="json"),
                actor=actor,
                idempotency_key=f"resume:{interrupt_id}",
                session_id=intr["session_id"],
                invocation_id=intr["invocation_id"],
            )
            await s.execute(
                text("UPDATE interrupt SET status = 'RESOLVED', resolved_at = now() WHERE id = :i"),
                {"i": interrupt_id},
            )
            await s.execute(
                text("INSERT INTO resume_request (tenant, interrupt_id, run_id, decision) VALUES (:t, :i, :r, :d)"),
                {"t": tenant, "i": interrupt_id, "r": resume_run, "d": decision},
            )
            outcome = DecisionOutcome("resolved", resume_run, view)
    if kind == "approval":
        await _refresh_approval_card(tenant, intr, view=view, rejection=rejection)
    if resume_run is not None:
        await wake_workers(resume_run, tenant)
    return outcome


async def record_decision(
    *,
    tenant: str,
    interrupt_id: str,
    decider: dict[str, Any],
    decision: str,
    plan_version: int | None = None,
    change_set_hash: str | None = None,
    edits: Edits = None,
    removed_checks: Sequence[str] = (),
    auth_time: dt.datetime | None = None,
    reason: str | None = None,
) -> Literal["recorded", "resolved"]:
    """Contract signature (``55-engine-internals.md``); see :func:`record_decision_ex`."""
    outcome = await record_decision_ex(
        tenant=tenant, interrupt_id=interrupt_id, decider=decider, decision=decision, plan_version=plan_version,
        change_set_hash=change_set_hash, edits=edits, removed_checks=removed_checks, auth_time=auth_time,
        reason=reason,
    )
    return outcome.status


async def _refresh_approval_card(tenant: str, intr: Mapping[str, Any], *, view: QuorumView | None,
                                 rejection: dict[str, Any] | None) -> None:
    """Live quorum progress / rejection on the case's ``approval_card@1`` block for this interrupt
    (updated in place + ``ACTIVITY_SNAPSHOT``). Best effort: a card that does not accept the
    update (e.g. its ``required_roles`` differ from the quorum's) is left as it is."""
    if view is None and rejection is None:
        return
    try:
        from keelson_contracts.agui import ScMetadata, Stage, activity_snapshot
        from keelson_contracts.blocks import block_to_wire, parse_block
        from keelson_engine.core.events import get_event_bus

        async with tenant_session(tenant) as s:
            res = await s.execute(
                text("SELECT id, envelope FROM block WHERE case_id = :c AND kind = 'approval_card' "
                     "AND envelope->'props'->>'interrupt_id' = :i ORDER BY created_at DESC LIMIT 1"),
                {"c": intr["case_id"], "i": intr["id"]},
            )
            row = res.first()
        if row is None:
            return
        envelope = dict(row[1])
        props = dict(envelope.get("props") or {})
        if rejection is not None:
            props.pop("quorum", None)
            props["rejection"] = {k: v for k, v in rejection.items() if k != "decision"}
            label = f"{rejection['name']} chose {rejection['decision']}"
        else:
            assert view is not None
            props["quorum"] = view.model_dump(mode="json")
            done = len(view.approvals)
            total = done + view.remaining
            label = f"{done} of {total} approvals"
        model = parse_block({**envelope, "props": props})
        wire = block_to_wire(model)
        async with tenant_session(tenant) as s:
            await s.execute(text("UPDATE block SET envelope = CAST(:e AS jsonb), updated_at = now() WHERE id = :id"),
                            {"e": dumps(wire), "id": row[0]})
        sc = ScMetadata(tenant_id=tenant, case_id=str(intr["case_id"]), run_id=str(intr["run_id"]),
                        stage=Stage.AWAITING_APPROVAL, prov="decision", label=label)
        await get_event_bus().append(tenant=tenant, case_id=str(intr["case_id"]), run_id=str(intr["run_id"]),
                                     event=activity_snapshot(sc, block=wire))
    except Exception as exc:  # noqa: BLE001 - the decision is committed; the card refresh is cosmetic
        log.warning("approval card refresh skipped", interrupt_id=intr.get("id"), error=f"{type(exc).__name__}")


async def supersede_open(tenant: str, case_id: str, reason: str) -> int:
    """Mark every PENDING interrupt of the case SUPERSEDED; returns how many."""
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                "UPDATE interrupt SET status = 'SUPERSEDED', superseded_reason = :r, resolved_at = now() "
                "WHERE case_id = :c AND status = 'PENDING' RETURNING id"
            ),
            {"c": case_id, "r": reason[:500]},
        )
        count = len(res.all())
    if count:
        log.info("interrupts superseded", case_id=case_id, count=count, reason=reason)
    return count
