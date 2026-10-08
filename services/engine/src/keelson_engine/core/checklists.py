"""Guide-mode checklists with verified progress (``docs/contracts/95-m5-remediation.md`` §2).

Tables ``checklist`` (one per guided connector + change set) and ``checklist_step`` (``seq``,
title, instructions, deep link, ``expected {entity_type, entity_ref|null, match}``, ``status``
pending|done|verified|skipped, ``verified_at``, ``evidence_id``, ``matched_ref``).

* The runtime creates a checklist (:func:`create_checklist`) for the guide lines of a change set
  and renders it as a ``remediation_checklist@1`` block (:func:`checklist_props`); passing the
  block id (or :func:`attach_block`) lets the platform refresh the block in place. The runtime's
  ``ChecklistsPort`` maps onto :func:`create_checklist`, :func:`get_checklist`,
  :func:`list_open_checklists`, :func:`set_checklist_step` and :func:`set_checklist_status`.
* People tick steps ``done`` / ``skipped`` (:func:`set_step_status`; ``POST
  /api/cases/{id}/checklists/{cid}/steps/{seq}``; audited). ``verified`` is set only by read-back.
* :func:`verify_checklists` (the worker calls it on connector webhooks and on a poll, see
  :func:`poll_checklists`) reads the expected entities back through the connector's MCP **read
  tools as the reader principal** and marks matching pending/done steps ``verified`` with an
  evidence row (``prov: fact``). For a step that **creates** something (``entity_ref`` null) an
  entity verifies at most one step per checklist and never counts when it already existed: it is in
  the runtime's ``meta.baseline`` for the step, its ``created_at`` is older than the checklist, or
  it was created by an applied change set (``created_via: change_set:...``), not by a person. A step
  that **edits** a named entity (``entity_ref``) verifies when that entity matches, however old.
* When every step is verified or skipped the checklist is ``completed`` ("Guide completed").

Read-back planning (:func:`plan_readback`) is generic: a :data:`READBACK_HINTS` entry, else
``get_entity {type, code}`` when the step names an ``entity_ref`` and ``get_entity`` accepts that type
(its input schema's ``type`` enum), else ``list_<entity_type>s`` with the scalar ``match`` fields its
input schema accepts as filters (and, for a named entity, only the item whose ``code``/``id``/``ref``
is the ``entity_ref``). A listed item that lacks some ``match`` field is read in full through
``get_<entity_type> {code}`` when the connector has it. Matching (:func:`entity_matches`) compares
JSON: scalars equal, objects recursively, every expected list element must match a distinct element
of the actual list (subset), a missing field may be present under a :data:`FIELD_ALIASES` name
(``qty`` / ``qty_ordered``), and an entity with ``lines`` also matches when its own scalar fields
plus one line match (a shipment line of a PO line). Event payloads are never trusted: an event only
triggers a read-back.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text

from keelson_contracts.blocks import (
    ChecklistExpected,
    ChecklistStep,
    RemediationChecklistProps,
    is_safe_deep_link,
)
from keelson_engine.config import get_settings
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps

__all__ = [
    "READBACK_HINTS",
    "ChecklistError",
    "ReadBack",
    "VerifyResult",
    "attach_block",
    "checklist_props",
    "create_checklist",
    "get_checklist",
    "list_checklists",
    "list_open_checklists",
    "entity_matches",
    "matches",
    "plan_readback",
    "poll_checklists",
    "set_checklist_status",
    "set_checklist_step",
    "set_step_status",
    "set_tool_caller",
    "verify_checklists",
]

log = get_logger(__name__)

MANUAL_STATUSES = ("done", "skipped")
MAX_PAGES = 5
MAX_DETAIL_READS = 20
"""Full reads (``get_<entity_type>``) per read-back, for listed items that lack a ``match`` field."""
_CREATED_SLACK = dt.timedelta(minutes=2)
FIELD_ALIASES: dict[str, tuple[str, ...]] = {"qty": ("qty_ordered", "qty_units")}
"""Names a connector may use for an expected field the entity does not carry under its own name."""


class ChecklistError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ReadbackHint:
    """How to read one entity type back when there is no ``list_<type>s`` / ``get_entity``."""

    tool: str
    args: Callable[[str | None, Mapping[str, Any]], dict[str, Any] | None]
    items_key: str | None = None
    ref_field: str | None = None
    """Field of each item that must equal the part of ``entity_ref`` after ``#`` (PO lines)."""


def _po_args(ref: str | None, _match: Mapping[str, Any]) -> dict[str, Any] | None:
    if not ref or "#" not in ref:
        return None
    return {"code": ref.rsplit("#", 1)[0]}


READBACK_HINTS: dict[str, ReadbackHint] = {
    "po_line": ReadbackHint("get_purchase_order", _po_args, items_key="lines", ref_field="line_no"),
}


@dataclass(frozen=True, slots=True)
class ReadBack:
    tool: str
    args: dict[str, Any]
    items_key: str | None = None
    ref_field: str | None = None
    ref_value: str | None = None
    match_ref: str | None = None
    """Listed items are kept only when their ``code``/``id``/``ref`` equals this (a named entity)."""
    detail_tool: str | None = None
    """``get_<entity_type> {code}``: full read of a listed item that lacks a ``match`` field."""


ToolCaller = Callable[[str, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""``(tenant, connector, tool, args) -> structuredContent`` as the reader principal."""


async def _mcp_call(tenant: str, connector: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    from mcp import types as mcp_types

    from keelson_engine.core.connectors import auth_headers, get_active_connector
    from keelson_engine.core.mcp import mcp_client

    conn = await get_active_connector(tenant, connector)
    headers = await auth_headers(tenant, connector, "reader")
    async with mcp_client(conn.url, headers, timeout=30.0) as client:
        result = await client.call_tool_mcp(tool, args)
    if result.is_error:
        msg = " ".join(b.text for b in result.content if isinstance(b, mcp_types.TextContent))
        raise RuntimeError(f"{tool} failed: {msg[:200]}")
    data = result.structured_content
    if not isinstance(data, dict):
        raise RuntimeError(f"{tool} returned no structured content")
    return data


_tool_caller: ToolCaller = _mcp_call


def set_tool_caller(caller: ToolCaller | None) -> None:
    """Tests: replace the MCP read-back call (``None`` restores the default)."""
    global _tool_caller
    _tool_caller = caller or _mcp_call


# ───────────────────────────── create / read ─────────────────────────────


def _step_model(raw: Mapping[str, Any] | ChecklistStep, seq: int) -> ChecklistStep:
    data = raw.model_dump(mode="json") if isinstance(raw, ChecklistStep) else dict(raw)
    data = {"status": "pending", "verified_at": None, "evidence_id": None, "deep_link": None, **data, "seq": seq}
    try:
        return ChecklistStep.model_validate(data)
    except ValidationError as exc:
        raise ChecklistError(422, "checklist_invalid", f"step {seq}: {exc.errors()[0].get('msg', 'invalid')}") from exc


async def create_checklist(
    tenant: str,
    case_id: str,
    connector: str,
    steps: Sequence[Mapping[str, Any] | ChecklistStep],
    *,
    checklist_id: str | None = None,
    change_set_id: str | None = None,
    plan_version: int | None = None,
    option_id: str | None = None,
    block_id: str | None = None,
    created_by: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """Store a checklist with its steps (``remediation_checklist@1`` step JSON, renumbered 1..n in
    the given order; ``status`` defaults to ``pending``) and the runtime's ``meta`` (JSON).
    Idempotent on ``checklist_id`` (a crash-retried node gets the same id back). This is also the
    runtime ``ChecklistsPort.create_checklist`` (keyword call)."""
    if not steps:
        raise ChecklistError(422, "checklist_invalid", "a checklist needs at least one step")
    models = [_step_model(st, i) for i, st in enumerate(steps, start=1)]
    async with tenant_session(tenant) as s:
        if checklist_id is not None:
            res = await s.execute(text("SELECT id FROM checklist WHERE id = :id"), {"id": checklist_id})
            if res.scalar_one_or_none() is not None:
                return checklist_id
        res = await s.execute(
            text(
                """
                INSERT INTO checklist (id, tenant, case_id, connector, change_set_id, plan_version, option_id,
                                       block_id, created_by, meta)
                VALUES (coalesce(:id, uuidv7()::text), :t, :c, :conn, :cs, :pv, :opt, :blk, :by,
                        CAST(:meta AS jsonb))
                RETURNING id
                """
            ),
            {"id": checklist_id, "t": tenant, "c": case_id, "conn": connector, "cs": change_set_id,
             "pv": plan_version, "opt": option_id, "blk": block_id, "by": created_by, "meta": dumps(dict(meta or {}))},
        )
        cid = str(res.scalar_one())
        for st in models:
            await s.execute(
                text(
                    """
                    INSERT INTO checklist_step (tenant, checklist_id, seq, title, instructions_md, deep_link, expected,
                                                status, verified_at, evidence_id)
                    VALUES (:t, :cid, :seq, :title, :ins, :link, CAST(:exp AS jsonb), :st, :vat, :ev)
                    """
                ),
                {"t": tenant, "cid": cid, "seq": st.seq, "title": st.title, "ins": st.instructions_md,
                 "link": st.deep_link, "exp": dumps(st.expected.model_dump(mode="json")), "st": st.status,
                 "vat": st.verified_at, "ev": st.evidence_id},
            )
        await _maybe_complete(s, cid)
    return cid


_CL_COLS = ("id, tenant, case_id, connector, change_set_id, plan_version, option_id, block_id, status, meta, "
            "created_by, created_at, updated_at, completed_at, last_checked_at, last_event_id")
_STEP_COLS = ("seq, title, instructions_md, deep_link, expected, status, verified_at, evidence_id, matched_ref, "
              "updated_by, updated_at")


async def _load(s: Any, checklist_id: str) -> dict[str, Any] | None:
    res = await s.execute(text(f"SELECT {_CL_COLS} FROM checklist WHERE id = :id"), {"id": checklist_id})
    m = res.mappings().first()
    if m is None:
        return None
    out = dict(m)
    res = await s.execute(text(f"SELECT {_STEP_COLS} FROM checklist_step WHERE checklist_id = :id ORDER BY seq"),
                          {"id": checklist_id})
    out["steps"] = [dict(r) for r in res.mappings().all()]
    return out


def _public(cl: Mapping[str, Any]) -> dict[str, Any]:
    """Port shape: checklist columns + ``steps`` as exact ``remediation_checklist@1`` step JSON."""
    out = {k: cl[k] for k in ("id", "tenant", "case_id", "connector", "change_set_id", "plan_version", "option_id",
                              "block_id", "status", "created_by", "created_at", "updated_at", "completed_at")}
    out["meta"] = dict(cl.get("meta") or {})
    out["steps"] = [st.model_dump(mode="json") for st in props_of(cl).steps]
    return out


async def get_checklist(tenant: str, checklist_id: str) -> dict[str, Any] | None:
    """The checklist (port shape, see :func:`_public`) or ``None``."""
    if not checklist_id or len(checklist_id) > 200:
        return None
    async with tenant_session(tenant) as s:
        cl = await _load(s, checklist_id)
    return _public(cl) if cl is not None else None


async def list_open_checklists(*, tenant: str | None = None) -> list[dict[str, Any]]:
    """Open checklists of ``tenant`` (all tenants when ``None``: a worker verify loop)."""
    sql = text("SELECT tenant, id FROM checklist WHERE status = 'open' ORDER BY created_at")
    if tenant is None:
        async with system_session() as s:
            pairs = [(str(r[0]), str(r[1])) for r in (await s.execute(sql)).all()]
    else:
        async with tenant_session(tenant) as s:
            pairs = [(str(r[0]), str(r[1])) for r in (await s.execute(sql)).all()]
    out: list[dict[str, Any]] = []
    for t, cid in pairs:
        cl = await get_checklist(t, cid)
        if cl is not None:
            out.append(cl)
    return out


def _parse_ts(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str) and value:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC)


async def set_checklist_step(*, tenant: str, checklist_id: str, seq: int, status: str,
                             verified_at: str | dt.datetime | None = None, evidence_id: str | None = None,
                             actor: str | None = None) -> dict[str, Any] | None:
    """Runtime port: set one step's status under the row lock (``verified`` carries
    ``verified_at``, default now, and ``evidence_id``; a verified step never changes back).
    Completes the checklist when every step is verified/skipped. Returns the checklist (port
    shape) or ``None`` for an unknown checklist/step."""
    if status not in ("pending", "done", "verified", "skipped"):
        raise ChecklistError(422, "invalid_status", f"unknown step status {status!r}")
    async with tenant_session(tenant) as s:
        head = (await s.execute(text("SELECT status FROM checklist WHERE id = :id FOR UPDATE"),
                                {"id": checklist_id})).first()
        if head is None:
            return None
        before = (await s.execute(
            text("SELECT status FROM checklist_step WHERE checklist_id = :id AND seq = :seq FOR UPDATE"),
            {"id": checklist_id, "seq": seq})).scalar_one_or_none()
        if before is None:
            return None
        if before != "verified" and before != status:
            if status == "verified":
                await s.execute(
                    text("UPDATE checklist_step SET status = 'verified', verified_at = :at, evidence_id = :ev, "
                         "updated_by = :by, updated_at = now() WHERE checklist_id = :id AND seq = :seq"),
                    {"at": _parse_ts(verified_at), "ev": evidence_id, "by": actor or "read-back", "id": checklist_id,
                     "seq": seq},
                )
            else:
                await s.execute(
                    text("UPDATE checklist_step SET status = :st, verified_at = NULL, evidence_id = NULL, "
                         "updated_by = :by, updated_at = now() WHERE checklist_id = :id AND seq = :seq"),
                    {"st": status, "by": actor, "id": checklist_id, "seq": seq},
                )
            await s.execute(text("UPDATE checklist SET updated_at = now() WHERE id = :id"), {"id": checklist_id})
            await _maybe_reopen(s, checklist_id)
            await _maybe_complete(s, checklist_id)
        cl = await _load(s, checklist_id)
    return _public(cl) if cl is not None else None


async def set_checklist_status(*, tenant: str, checklist_id: str, status: str) -> None:
    """Runtime port: ``open`` | ``completed`` | ``cancelled``."""
    if status not in ("open", "completed", "cancelled"):
        raise ChecklistError(422, "invalid_status", f"unknown checklist status {status!r}")
    async with tenant_session(tenant) as s:
        await s.execute(
            text("UPDATE checklist SET status = :st, updated_at = now(), completed_at = CASE WHEN :st = "
                 "'completed' THEN coalesce(completed_at, now()) ELSE completed_at END WHERE id = :id"),
            {"st": status, "id": checklist_id},
        )


async def list_checklists(tenant: str, case_id: str) -> list[dict[str, Any]]:
    """The case's checklists (API view: status, completed, ``props`` = ``remediation_checklist@1``)."""
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT id FROM checklist WHERE case_id = :c ORDER BY created_at, id"),
                              {"c": case_id})
        ids = [str(r[0]) for r in res.all()]
        loaded = [c for c in [await _load(s, i) for i in ids] if c is not None]
    return [_view(c) for c in loaded]


def _aware(v: Any) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=dt.UTC)
    return None


def props_of(checklist: Mapping[str, Any]) -> RemediationChecklistProps:
    steps = [
        ChecklistStep(
            seq=int(st["seq"]), title=st["title"], instructions_md=st["instructions_md"],
            deep_link=st["deep_link"] if st["deep_link"] and is_safe_deep_link(st["deep_link"]) else None,
            expected=ChecklistExpected.model_validate(st["expected"]), status=st["status"],
            verified_at=_aware(st["verified_at"]) if st["status"] == "verified" else None,
            evidence_id=st["evidence_id"] if st["status"] == "verified" else None,
        )
        for st in checklist["steps"]
    ]
    return RemediationChecklistProps(checklist_id=str(checklist["id"]), connector=str(checklist["connector"]),
                                     steps=steps, progress=RemediationChecklistProps.progress_of(steps))


async def checklist_props(tenant: str, checklist_id: str) -> RemediationChecklistProps:
    """``remediation_checklist@1`` props of the stored checklist (the runtime renders the block)."""
    cl = await get_checklist(tenant, checklist_id)
    if cl is None:
        raise ChecklistError(404, "not_found", "checklist not found")
    return props_of(cl)


async def attach_block(tenant: str, checklist_id: str, block_id: str) -> None:
    """Record the ``remediation_checklist`` block that renders this checklist."""
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE checklist SET block_id = :b, updated_at = now() WHERE id = :id"),
                        {"b": block_id, "id": checklist_id})


def _view(cl: Mapping[str, Any]) -> dict[str, Any]:
    props = props_of(cl)
    return {
        "id": cl["id"], "case_id": cl["case_id"], "connector": cl["connector"], "status": cl["status"],
        "change_set_id": cl["change_set_id"], "plan_version": cl["plan_version"], "option_id": cl["option_id"],
        "block_id": cl["block_id"], "completed": props.completed,
        "completed_at": cl["completed_at"].isoformat() if cl.get("completed_at") else None,
        "props": props.model_dump(mode="json"),
    }


async def _refresh_block(tenant: str, cl: Mapping[str, Any], *, label: str, run_id: str | None = None) -> None:
    """Re-render the checklist's block in place + ``ACTIVITY_SNAPSHOT`` (best effort)."""
    block_id = cl.get("block_id")
    if not block_id:
        return
    try:
        from keelson_contracts.agui import ScMetadata, Stage, activity_snapshot
        from keelson_contracts.blocks import block_to_wire, parse_block
        from keelson_engine.core.events import get_event_bus

        async with tenant_session(tenant) as s:
            res = await s.execute(text("SELECT envelope, run_id FROM block WHERE id = :id"), {"id": block_id})
            row = res.first()
        if row is None:
            return
        envelope = dict(row[0])
        wire = block_to_wire(parse_block({**envelope, "props": props_of(cl).model_dump(mode="json")}))
        async with tenant_session(tenant) as s:
            await s.execute(text("UPDATE block SET envelope = CAST(:e AS jsonb), updated_at = now() WHERE id = :id"),
                            {"e": dumps(wire), "id": block_id})
        rid = run_id or (str(row[1]) if row[1] else None)
        sc = ScMetadata(tenant_id=tenant, case_id=str(cl["case_id"]), run_id=rid, stage=Stage.APPLYING,
                        prov="committed" if "verified" in label.lower() else "decision", label=label)
        await get_event_bus().append(tenant=tenant, case_id=str(cl["case_id"]), run_id=rid,
                                     event=activity_snapshot(sc, block=wire))
    except Exception as exc:  # noqa: BLE001 - the step state is stored; the live block is cosmetic
        log.warning("checklist block refresh skipped", checklist_id=cl.get("id"), error=type(exc).__name__)


async def _maybe_reopen(s: Any, checklist_id: str) -> bool:
    """A completed checklist whose step was set back to pending/done (e.g. a skip undone) is open
    again: "Guide completed" must never show while a step is outstanding."""
    res = await s.execute(
        text(
            """
            UPDATE checklist SET status = 'open', completed_at = NULL, updated_at = now()
            WHERE id = :id AND status = 'completed'
              AND EXISTS (SELECT 1 FROM checklist_step WHERE checklist_id = :id
                          AND status NOT IN ('verified', 'skipped'))
            RETURNING id
            """
        ),
        {"id": checklist_id},
    )
    return res.first() is not None


async def _maybe_complete(s: Any, checklist_id: str) -> bool:
    res = await s.execute(
        text(
            """
            UPDATE checklist SET status = 'completed', completed_at = now(), updated_at = now()
            WHERE id = :id AND status = 'open'
              AND NOT EXISTS (SELECT 1 FROM checklist_step WHERE checklist_id = :id
                              AND status NOT IN ('verified', 'skipped'))
            RETURNING id
            """
        ),
        {"id": checklist_id},
    )
    return res.first() is not None


async def set_step_status(tenant: str, case_id: str, checklist_id: str, seq: int, status: str,
                          actor: dict[str, Any]) -> dict[str, Any]:
    """A person ticks a step ``done`` or ``skipped`` (audited). A verified step is final (409)."""
    from keelson_engine.core.repo import admin_audit

    if status not in MANUAL_STATUSES:
        raise ChecklistError(422, "invalid_status", "a step can only be marked done or skipped by hand")
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT case_id, status FROM checklist WHERE id = :id FOR UPDATE"),
                              {"id": checklist_id})
        head = res.first()
        if head is None or str(head[0]) != case_id:
            raise ChecklistError(404, "not_found", "checklist not found")
        if head[1] == "cancelled":
            raise ChecklistError(409, "checklist_closed", "this checklist was cancelled")
        res = await s.execute(
            text("SELECT status FROM checklist_step WHERE checklist_id = :id AND seq = :seq FOR UPDATE"),
            {"id": checklist_id, "seq": seq},
        )
        before = res.scalar_one_or_none()
        if before is None:
            raise ChecklistError(404, "not_found", "step not found")
        if before == "verified":
            raise ChecklistError(409, "already_verified", "this step was already verified in the system")
        changed = before != status
        if changed:
            await s.execute(
                text("UPDATE checklist_step SET status = :st, updated_by = :by, updated_at = now() "
                     "WHERE checklist_id = :id AND seq = :seq"),
                {"st": status, "by": str(actor.get("username") or actor.get("sub") or ""), "id": checklist_id,
                 "seq": seq},
            )
            await s.execute(text("UPDATE checklist SET updated_at = now() WHERE id = :id"), {"id": checklist_id})
            await _maybe_reopen(s, checklist_id)
            await _maybe_complete(s, checklist_id)
        cl = await _load(s, checklist_id)
    assert cl is not None
    if changed:
        await admin_audit(actor, f"checklist.step_{status}", f"checklist:{checklist_id}#{seq}",
                          {"status": before}, {"status": status, "case_id": case_id}, tenant=tenant)
        await _refresh_block(tenant, cl, label=f"Step {seq} marked {status}")
    step = next(st for st in cl["steps"] if int(st["seq"]) == seq)
    return {"step": {"seq": seq, "status": step["status"], "updated_by": step["updated_by"]}, "changed": changed,
            "checklist": _view(cl)}


# ───────────────────────────── read-back ─────────────────────────────


def _schema_props(schema: Mapping[str, Any] | None) -> set[str]:
    props = (schema or {}).get("properties")
    return set(props) if isinstance(props, Mapping) else set()


def _accepts_value(schema: Mapping[str, Any] | None, prop: str, value: str) -> bool:
    """``schema`` accepts ``value`` for ``prop``: the property exists and, when it declares an
    ``enum``/``const`` (inline, through ``$ref`` to ``$defs`` or in ``anyOf``/``oneOf``), lists it."""
    props = (schema or {}).get("properties")
    if not isinstance(props, Mapping) or prop not in props:
        return False
    defs = (schema or {}).get("$defs") or (schema or {}).get("definitions") or {}

    def enums(node: Any, depth: int = 0) -> list[list[Any]]:
        if not isinstance(node, Mapping) or depth > 5:
            return []
        out = [list(node["enum"])] if isinstance(node.get("enum"), list) else []
        if "const" in node:
            out.append([node["const"]])
        ref = node.get("$ref")
        if isinstance(ref, str) and isinstance(defs, Mapping):
            out += enums(defs.get(ref.rsplit("/", 1)[-1]), depth + 1)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in node.get(key) or []:
                out += enums(sub, depth + 1)
        return out

    found = enums(props[prop])
    return not found or any(value in e for e in found)


def plan_readback(expected: ChecklistExpected, tools: Mapping[str, Mapping[str, Any]]) -> ReadBack | None:
    """Which read tool (and filter arguments) reads ``expected`` back; ``tools`` = approved read
    tools ``{name: input_schema}``. ``None`` when the connector offers no way to read it."""
    etype, ref = expected.entity_type, expected.entity_ref
    hint = READBACK_HINTS.get(etype)
    if hint is not None and hint.tool in tools:
        args = hint.args(ref, expected.match)
        if args is not None:
            ref_value = ref.rsplit("#", 1)[1] if (ref and hint.ref_field and "#" in ref) else None
            return ReadBack(hint.tool, args, hint.items_key, hint.ref_field, ref_value)
    if ref and "get_entity" in tools and _accepts_value(tools["get_entity"], "type", etype):
        return ReadBack("get_entity", {"type": etype, "code": ref})
    detail = f"get_{etype}"
    detail_tool = detail if detail in tools and "code" in _schema_props(tools[detail]) else None
    for name in (f"list_{etype}s", f"list_{etype}es", f"list_{etype}"):
        if name in tools:
            accepted = _schema_props(tools[name])
            args = {k: v for k, v in sorted(expected.match.items())
                    if k in accepted and isinstance(v, (str, int, bool)) and not isinstance(v, float)}
            return ReadBack(name, args, match_ref=ref, detail_tool=detail_tool)
    return None


def _scalar_eq(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, int) and isinstance(actual, str) and actual.strip().lstrip("-").isdigit():
        return expected == int(actual)
    if isinstance(expected, str) and isinstance(actual, int) and expected.strip().lstrip("-").isdigit():
        return int(expected) == actual
    return bool(expected == actual)


def _field(actual: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    if key in actual:
        return True, actual[key]
    for alias in FIELD_ALIASES.get(key, ()):
        if alias in actual:
            return True, actual[alias]
    return False, None


def matches(actual: Any, expected: Any) -> bool:
    """JSON subset match (see module docstring)."""
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        for k, v in expected.items():
            present, value = _field(actual, str(k))
            if not present or not matches(value, v):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        used: set[int] = set()
        for e in expected:
            hit = next((i for i, a in enumerate(actual) if i not in used and matches(a, e)), None)
            if hit is None:
                return False
            used.add(hit)
        return True
    return _scalar_eq(expected, actual)


def entity_matches(entity: Mapping[str, Any], match: Mapping[str, Any]) -> bool:
    """:func:`matches`, or the entity's own scalar fields plus one of its ``lines`` match (e.g. a
    shipment that carries the expected PO line)."""
    if matches(entity, match):
        return True
    lines = entity.get("lines")
    if not isinstance(lines, list):
        return False
    head = {k: v for k, v in entity.items() if not isinstance(v, (list, Mapping))}
    return any(matches({**head, **line}, match) for line in lines if isinstance(line, Mapping))


def _ref_of(item: Mapping[str, Any]) -> str | None:
    for key in ("code", "id", "ref"):
        if item.get(key) not in (None, ""):
            return str(item[key])
    return None


def _items(data: Mapping[str, Any], rb: ReadBack) -> list[dict[str, Any]]:
    if rb.match_ref is not None:
        return [i for i in _items_of(data, rb) if _ref_of(i) == rb.match_ref]
    return _items_of(data, rb)


def _items_of(data: Mapping[str, Any], rb: ReadBack) -> list[dict[str, Any]]:
    if rb.items_key is not None:
        raw = data.get(rb.items_key)
        items = [dict(i) for i in raw if isinstance(i, Mapping)] if isinstance(raw, list) else []
        if rb.ref_field and rb.ref_value is not None:
            items = [i for i in items if str(i.get(rb.ref_field)) == rb.ref_value]
        return items
    if isinstance(data.get("items"), list):
        return [dict(i) for i in data["items"] if isinstance(i, Mapping)]
    if isinstance(data.get("data"), Mapping):  # get_entity {type, code, data, version}
        return [{**dict(data["data"]), **{k: data[k] for k in ("code", "version") if k in data}}]
    return [dict(data)]


def _entity_ref(item: Mapping[str, Any], rb: ReadBack) -> str:
    for key in ("code", "id", "ref"):
        if item.get(key) not in (None, ""):
            ref = str(item[key])
            return f"{rb.args.get('code')}#{ref}" if rb.ref_field and key != "code" and rb.args.get("code") else ref
    if rb.ref_field and rb.ref_value is not None:
        return f"{rb.args.get('code')}#{rb.ref_value}"
    return dumps(item) or ""


def _made_by_change_set(item: Mapping[str, Any]) -> bool:
    """Created by an applied change set (Keelson's own apply), not by a person following the guide."""
    via = item.get("created_via")
    return isinstance(via, str) and via.startswith("change_set")


def _created_before(item: Mapping[str, Any], since: dt.datetime | None) -> bool:
    if since is None:
        return False
    raw = item.get("created_at")
    if not isinstance(raw, str):
        return False
    try:
        created = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return created < since - _CREATED_SLACK


@dataclass(slots=True)
class VerifyResult:
    tenant: str
    connector: str
    checked_steps: int = 0
    verified: list[tuple[str, int]] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    unreadable: list[tuple[str, int]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"tenant": self.tenant, "connector": self.connector, "checked_steps": self.checked_steps,
                "verified": [list(v) for v in self.verified], "completed": self.completed,
                "unreadable": [list(v) for v in self.unreadable], "errors": self.errors}


async def _read_tools(tenant: str, connector: str) -> dict[str, dict[str, Any]]:
    from keelson_engine.core.connectors import list_approved_tools

    return {t.name: dict(t.input_schema or {}) for t in await list_approved_tools(tenant, connector)
            if t.access == "read"}


async def verify_checklists(tenant: str, connector: str, event: Mapping[str, Any] | None = None) -> VerifyResult:
    """Read back the expected entities of every open checklist of ``(tenant, connector)`` and mark
    matching pending/done steps ``verified`` (evidence row, block refresh). ``event`` (a stored
    connector event, optional) only triggers the check; its content is not trusted."""
    from keelson_engine.core.repo import save_evidence

    result = VerifyResult(tenant, connector)
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT id FROM checklist WHERE connector = :c AND status = 'open' ORDER BY created_at"),
            {"c": connector},
        )
        ids = [str(r[0]) for r in res.all()]
        checklists = [c for c in [await _load(s, i) for i in ids] if c is not None]
    event_id = event.get("id") if isinstance(event, Mapping) else None
    if not checklists:
        return result
    try:
        tools = await _read_tools(tenant, connector)
    except Exception as exc:  # noqa: BLE001 - connector gone/disabled: nothing to verify now
        result.errors.append(f"tools unavailable ({type(exc).__name__})")
        return result
    cache: dict[str, tuple[ReadBack, list[dict[str, Any]]] | None] = {}
    details: dict[tuple[str, str], dict[str, Any] | None] = {}
    for cl in checklists:
        since = _aware(cl["created_at"])
        used = {str(st["matched_ref"]) for st in cl["steps"] if st.get("matched_ref")}
        raw_baseline = (cl.get("meta") or {}).get("baseline")
        baseline = raw_baseline if isinstance(raw_baseline, Mapping) else {}
        changed = False
        for st in cl["steps"]:
            if st["status"] not in ("pending", "done"):
                continue
            result.checked_steps += 1
            expected = ChecklistExpected.model_validate(st["expected"])
            rb = plan_readback(expected, tools)
            if rb is None:
                result.unreadable.append((str(cl["id"]), int(st["seq"])))
                continue
            key = dumps({"tool": rb.tool, "args": rb.args, "k": rb.items_key, "r": rb.ref_value,
                         "m": rb.match_ref}) or ""
            if key not in cache:
                try:
                    items = await _read_all(tenant, connector, rb, tools.get(rb.tool) or {})
                    cache[key] = (rb, items)
                except Exception as exc:  # noqa: BLE001 - try again on the next event/poll
                    cache[key] = None
                    result.errors.append(f"{rb.tool}: {type(exc).__name__}")
            hit = cache[key]
            if hit is None:
                continue
            creates = expected.entity_ref is None
            existing = baseline.get(str(st["seq"])) if creates else None
            existing_refs = {str(r) for r in existing} if isinstance(existing, list) else set()
            reads = 0
            for listed in hit[1]:
                ref = _entity_ref(listed, rb)
                if creates and (ref in used or ref in existing_refs or _created_before(listed, since)
                                or _made_by_change_set(listed)):
                    continue  # a create step needs an entity a person made after the guide was issued
                item = listed
                code = _ref_of(listed)
                if rb.detail_tool and code is not None and any(k not in listed for k in expected.match):
                    dkey = (rb.detail_tool, code)
                    if dkey not in details and reads < MAX_DETAIL_READS:
                        reads += 1
                        try:
                            details[dkey] = await _tool_caller(tenant, connector, rb.detail_tool, {"code": code})
                        except Exception as exc:  # noqa: BLE001 - try again on the next event/poll
                            details[dkey] = None
                            result.errors.append(f"{rb.detail_tool}: {type(exc).__name__}")
                    full = details.get(dkey)
                    if isinstance(full, Mapping):
                        item = {**listed, **dict(full)}
                        if creates and (_created_before(item, since) or _made_by_change_set(item)):
                            continue
                if not entity_matches(item, expected.match):
                    continue
                evidence_id = await save_evidence(
                    str(cl["case_id"]), None, connector, rb.tool, rb.args,
                    {"entity_type": expected.entity_type, "entity_ref": ref, "entity": item,
                     "checklist_id": cl["id"], "seq": int(st["seq"]), "event_id": event_id},
                    "fact", tenant=tenant,
                )
                async with tenant_session(tenant) as s:
                    res = await s.execute(
                        text(
                            """
                            UPDATE checklist_step SET status = 'verified', verified_at = now(), evidence_id = :ev,
                                   matched_ref = :ref, updated_by = 'read-back', updated_at = now()
                            WHERE checklist_id = :id AND seq = :seq AND status IN ('pending', 'done')
                            RETURNING seq
                            """
                        ),
                        {"ev": evidence_id, "ref": ref[:500], "id": cl["id"], "seq": int(st["seq"])},
                    )
                    if res.first() is not None:
                        changed = True
                        used.add(ref)
                        result.verified.append((str(cl["id"]), int(st["seq"])))
                        if await _maybe_complete(s, str(cl["id"])):
                            result.completed.append(str(cl["id"]))
                break
        async with tenant_session(tenant) as s:
            await s.execute(text("UPDATE checklist SET last_checked_at = now() WHERE id = :id"), {"id": cl["id"]})
            fresh = await _load(s, str(cl["id"])) if changed else None
        if fresh is not None:
            done = sum(1 for st in fresh["steps"] if st["status"] == "verified")
            label = ("Guide completed — every step is verified or skipped" if fresh["status"] == "completed"
                     else f"Verified in {connector}: {done} of {len(fresh['steps'])} steps")
            await _refresh_block(tenant, fresh, label=label)
    if result.verified:
        log.info("checklist steps verified", tenant=tenant, connector=connector, verified=len(result.verified),
                 completed=len(result.completed))
    return result


async def _read_all(tenant: str, connector: str, rb: ReadBack, schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    args = dict(rb.args)
    items: list[dict[str, Any]] = []
    pages = 0
    seen: set[str] = set()
    while True:
        data = await _tool_caller(tenant, connector, rb.tool, args)
        pages += 1
        items.extend(_items(data, rb))
        cursor = data.get("next_cursor") if rb.items_key is None else None
        if not cursor or "cursor" not in _schema_props(schema) or pages >= MAX_PAGES or str(cursor) in seen:
            return items
        seen.add(str(cursor))
        args = {**args, "cursor": cursor}


async def poll_checklists(*, interval_s: float | None = None, limit: int = 20) -> list[VerifyResult]:
    """Worker hook (call it every few seconds): verify every ``(tenant, connector)`` with an open
    checklist that was not checked for ``interval_s`` (``CHECKLIST_POLL_SECONDS``, 60 s) **or**
    that received a connector event (webhook) since its last check. Cheap when nothing is open."""
    interval = get_settings().checklist_poll_seconds if interval_s is None else interval_s
    async with system_session() as s:
        res = await s.execute(
            text(
                """
                SELECT c.tenant, c.connector, max(e.id) AS event_id
                FROM checklist c
                LEFT JOIN connector_event e ON e.tenant = c.tenant AND e.connector = c.connector
                     AND e.id > c.last_event_id
                WHERE c.status = 'open'
                GROUP BY c.tenant, c.connector
                HAVING max(e.id) IS NOT NULL
                    OR bool_or(c.last_checked_at IS NULL
                               OR c.last_checked_at < now() - make_interval(secs => :interval))
                ORDER BY min(coalesce(c.last_checked_at, '-infinity'::timestamptz))
                LIMIT :limit
                """
            ),
            {"interval": float(interval), "limit": limit},
        )
        due = [(str(r[0]), str(r[1]), int(r[2]) if r[2] is not None else None) for r in res.all()]
    out: list[VerifyResult] = []
    for tenant, connector, event_seq in due:
        event: dict[str, Any] | None = None
        if event_seq is not None:
            async with tenant_session(tenant) as s:
                row = (await s.execute(text("SELECT event_id, type FROM connector_event WHERE id = :i"),
                                       {"i": event_seq})).first()
            event = {"id": str(row[0]), "type": str(row[1])} if row is not None else None
        try:
            out.append(await verify_checklists(tenant, connector, event))
        except Exception as exc:  # noqa: BLE001 - one tenant's failure never stops the others
            log.warning("checklist verification failed", tenant=tenant, connector=connector,
                        error=type(exc).__name__)
        finally:
            async with tenant_session(tenant) as s:
                await s.execute(
                    text("UPDATE checklist SET last_checked_at = now(), "
                         "last_event_id = greatest(last_event_id, coalesce(:e, 0)) "
                         "WHERE connector = :c AND status = 'open'"),
                    {"e": event_seq, "c": connector},
                )
    return out

