"""Four-eyes change requests for guarded admin changes (``docs/contracts/110-m7-backoffice-generic.md`` §1).

**The mechanism (generic).** A core admin function that finds a guarded change (plan §17) made by
an actor who may not apply it alone raises :class:`GuardedChange` instead of refusing with 403::

    raise GuardedChange(tenant=t, actor=actor, kind="connector_tool", target="connector:x/tool:y",
                        reasons=[GuardReason(...)], payload={...}, diff=[...], base_version=None)

The API turns it into ``202`` + an :class:`~keelson_contracts.admin.AdminChangeRequest` (stored here,
status ``pending``, expires after 72 h). Nothing is applied. A *different* tenant-admin of the
tenant, or any platform-admin (co-sign), approves via ``POST /api/admin/change-requests/{id}/approve``:
the request's **handler** (:data:`HANDLERS`, one per kind) re-runs the same core function with
``cosigned=True`` against the **current** state (so every validation runs again), and the request
becomes ``applied`` (``approved`` for kinds applied asynchronously: the embedding-profile switch
starts a re-embed job). A versioned target that moved since the request (agent spec / policy set:
``base_version``) answers ``409 stale_base`` and the request stays ``pending`` (the approver
rejects it; the requester submits a new one). Any other refusal of the re-validation is returned
as-is and the request stays pending as well.

Rules (``keelson_contracts.admin.change_request_decision_error``): the requester can never approve
(they may reject = withdraw); only tenant-admins of the request's tenant or platform-admins decide;
kinds in :data:`PLATFORM_KINDS` (platform-wide effects) need a platform-admin; a reject needs a
reason; a pending request past ``expires_at`` becomes ``expired`` (lazily, on any read or decision).
Every step is written to ``admin_audit`` (``change_request.created|approved|applied|rejected|expired``
+ the applied change's own audit row).

**Atomicity.** The request row is locked (``FOR UPDATE``) for the whole decision, so two approvers
never apply it twice; the handler's own transaction commits the change, then the request flips to
``applied`` in the locking transaction. A crash between the two leaves the request ``pending`` with
the change applied: approving again then finds a moved base (409) or re-applies an idempotent change
(tool classification / binding / trust / activation are set-to-value; deletions answer 404).

Payloads and diffs never carry credentials (keys that look like secrets are stripped before the
contract model, which refuses them, sees them).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import text

from keelson_contracts.admin import (
    CHANGE_REQUEST_TTL,
    AdminChangeRequest,
    GuardReason,
    change_request_decision_error,
)
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "HANDLERS",
    "PLATFORM_KINDS",
    "GuardedChange",
    "approve",
    "create_request",
    "expire_overdue",
    "get_request",
    "json_diff",
    "list_requests",
    "pending_count",
    "reject",
    "strip_secrets",
]

log = get_logger(__name__)

PLATFORM_KINDS: Final = frozenset({"embedding_profile"})
"""Kinds with platform-wide effects: only a platform-admin approves them (a second tenant-admin of
the requesting tenant cannot decide for every tenant)."""
ASYNC_KINDS: Final = frozenset({"embedding_profile"})
"""Kinds whose handler only starts the change (status ``approved``, not ``applied``)."""
MAX_DIFF: Final = 200
MAX_PENDING: Final = 100
"""Pending requests per tenant (429 beyond): bounds the storage a tenant-admin can fill and keeps
the "Pending changes" queue reviewable."""

_SECRET_KEYS: Final = frozenset({
    "secret", "password", "passwd", "api_key", "apikey", "token", "access_token", "refresh_token", "id_token",
    "authorization", "private_key", "secret_enc", "credentials", "cookie", "client_secret",
})


def _secret_key(key: str) -> bool:
    k = key.strip().lower().replace("-", "_")
    return k in _SECRET_KEYS or k.endswith(("_secret", "_password", "_api_key", "_private_key"))


def strip_secrets(value: Any, depth: int = 0) -> Any:
    """A copy of ``value`` without credential-like keys (same rule as the contract model)."""
    if depth > 64:
        return None
    if isinstance(value, Mapping):
        return {k: strip_secrets(v, depth + 1) for k, v in value.items()
                if not (isinstance(k, str) and _secret_key(k))}
    if isinstance(value, list):
        return [strip_secrets(v, depth + 1) for v in value]
    return value


def _pointer(parts: Sequence[str]) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def json_diff(before: Any, after: Any, *, prefix: Sequence[str] = (), depth: int = 6) -> list[dict[str, Any]]:
    """RFC 6902-style differences (objects recurse up to ``depth``; lists and scalars are replaced
    whole). At most :data:`MAX_DIFF` entries; credential-like keys are skipped."""
    out: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, path: list[str], d: int) -> None:
        if len(out) >= MAX_DIFF or a == b:
            return
        if path and _secret_key(path[-1]):
            return
        if isinstance(a, Mapping) and isinstance(b, Mapping) and d > 0:
            for k in list(dict.fromkeys([*a.keys(), *b.keys()])):
                key = str(k)
                if k not in a:
                    if not _secret_key(key):
                        out.append({"op": "add", "path": _pointer([*path, key]), "after": strip_secrets(b[k])})
                elif k not in b:
                    if not _secret_key(key):
                        out.append({"op": "remove", "path": _pointer([*path, key]), "before": strip_secrets(a[k])})
                else:
                    walk(a[k], b[k], [*path, key], d - 1)
                if len(out) >= MAX_DIFF:
                    return
            return
        if a is None:
            out.append({"op": "add", "path": _pointer(path), "after": strip_secrets(b)})
        elif b is None:
            out.append({"op": "remove", "path": _pointer(path), "before": strip_secrets(a)})
        else:
            out.append({"op": "replace", "path": _pointer(path), "before": strip_secrets(a), "after": strip_secrets(b)})

    walk(before, after, list(prefix), depth)
    return out


# ───────────────────────────── the guard exception ─────────────────────────────


class GuardedChange(Exception):
    """Raised by a core admin function for a guarded change the actor may not apply alone."""

    def __init__(self, *, tenant: str, actor: dict[str, Any], kind: str, target: str,
                 reasons: Sequence[GuardReason | Mapping[str, Any]], payload: dict[str, Any],
                 diff: list[dict[str, Any]], base_version: str | None = None,
                 message: str = "this change requires a second admin") -> None:
        super().__init__(message)
        self.tenant = tenant
        self.actor = actor
        self.kind = kind
        self.target = target
        self.reasons = list(reasons)
        self.payload = payload
        self.diff = diff
        self.base_version = base_version
        self.message = message


# ───────────────────────────── storage ─────────────────────────────

_COLS = ("id, tenant, kind, target, payload, base_version, diff, reasons, requested_by, requested_sub, "
         "requester_auth, requested_at, status, approver, decided_at, decision_reason, expires_at, result")


def _model(m: Mapping[str, Any]) -> AdminChangeRequest:
    return AdminChangeRequest.model_validate({
        "id": str(m["id"]), "tenant": m["tenant"], "kind": m["kind"], "target": m["target"],
        "payload": m["payload"], "base_version": m["base_version"], "diff": m["diff"], "reasons": m["reasons"],
        "requested_by": m["requested_by"], "requested_at": m["requested_at"], "status": m["status"],
        "approver": m["approver"], "decided_at": m["decided_at"], "decision_reason": m["decision_reason"],
        "expires_at": m["expires_at"],
    })


def _json(m: Mapping[str, Any]) -> dict[str, Any]:
    out = _model(m).model_dump(mode="json")
    if m.get("result") is not None:
        out["result"] = m["result"]
    return out


def _actor_view(actor: Mapping[str, Any]) -> dict[str, str]:
    sub = str(actor.get("sub") or actor.get("username") or "unknown")[:200]
    return {"sub": sub, "username": str(actor.get("username") or sub)[:200], "name": str(actor.get("name") or "")[:200]}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


async def create_request(change: GuardedChange, *, actor_roles: Collection[str] | None = None,
                         actor_tenants: Collection[str] | None = None) -> dict[str, Any]:
    """Store a guarded change as a ``pending`` request; returns the ``AdminChangeRequest`` JSON."""
    now = _now()
    payload = strip_secrets(change.payload)
    diff = [d for d in change.diff if d][:MAX_DIFF] or [
        {"op": "replace", "path": "", "before": None, "after": {"target": change.target}}]
    reasons = [r if isinstance(r, GuardReason) else GuardReason.model_validate(r) for r in change.reasons]
    # dedupe (code, field): the contract model requires unique reasons
    seen: set[tuple[str, str]] = set()
    unique: list[GuardReason] = []
    for r in reasons:
        if (r.code, r.field) not in seen:
            seen.add((r.code, r.field))
            unique.append(r)
    roles = list(actor_roles if actor_roles is not None else change.actor.get("roles") or [])
    tenants = list(actor_tenants if actor_tenants is not None else change.actor.get("tenants") or [change.tenant])
    rid = str(uuid.uuid7())
    try:
        model = AdminChangeRequest.model_validate({
            "id": rid, "tenant": change.tenant, "kind": change.kind, "target": change.target[:200],
            "payload": payload, "base_version": change.base_version, "diff": diff,
            "reasons": [r.model_dump(mode="json") for r in unique[:50]], "requested_by": _actor_view(change.actor),
            "requested_at": now, "status": "pending", "approver": None, "decided_at": None,
            "decision_reason": None, "expires_at": now + CHANGE_REQUEST_TTL,
        })
    except ValidationError as exc:  # a programming error in a guard: never silently apply instead
        log.error("change request invalid", kind=change.kind, error=str(exc)[:500])
        raise AdminError(500, "change_request_invalid", "the guarded change could not be recorded") from exc
    data = model.model_dump(mode="json")
    await expire_overdue(change.tenant)
    async with tenant_session(change.tenant) as s:
        # one transaction per tenant at a time (the cap below is exact under concurrency)
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"change_request|{change.tenant}"})
        pending = int((await s.execute(text("SELECT count(*) FROM admin_change_request WHERE tenant = :t "
                                            "AND status = 'pending'"), {"t": change.tenant})).scalar_one())
        if pending >= MAX_PENDING:
            raise AdminError(429, "too_many_pending_changes",
                             f"this tenant already has {pending} pending changes; decide or withdraw some first")
        await s.execute(
            text(
                """
                INSERT INTO admin_change_request (id, tenant, kind, target, payload, base_version, diff, reasons,
                  requested_by, requested_sub, requester_auth, requested_at, status, expires_at)
                VALUES (CAST(:id AS uuid), :t, :k, :target, CAST(:p AS jsonb), :base, CAST(:d AS jsonb),
                  CAST(:r AS jsonb), CAST(:by AS jsonb), :sub, CAST(:auth AS jsonb), :at, 'pending', :exp)
                """
            ),
            {"id": rid, "t": change.tenant, "k": change.kind, "target": model.target, "p": dumps(data["payload"]),
             "base": change.base_version, "d": dumps(data["diff"]), "r": dumps(data["reasons"]),
             "by": dumps(data["requested_by"]), "sub": model.requested_by.sub,
             "auth": dumps({"roles": roles, "tenants": tenants}), "at": now, "exp": model.expires_at},
        )
    await admin_audit(change.actor, "change_request.created", f"change_request:{rid}", None,
                      {"kind": change.kind, "target": model.target, "reasons": [r.code for r in model.reasons],
                       "base_version": change.base_version}, tenant=change.tenant)
    log.info("change request created", tenant=change.tenant, kind=change.kind, id=rid)
    return data


async def expire_overdue(tenant: str | None = None) -> int:
    """Flip pending requests past ``expires_at`` to ``expired`` (audited). ``None`` = every tenant."""
    sql = text(
        "UPDATE admin_change_request SET status = 'expired' WHERE status = 'pending' AND expires_at <= now() "
        + ("AND tenant = :t " if tenant else "") + "RETURNING id, tenant, kind, target"
    )
    session = tenant_session(tenant) if tenant else system_session()
    async with session as s:
        rows = (await s.execute(sql, {"t": tenant} if tenant else {})).all()
    for r in rows:
        await admin_audit("system:four-eyes", "change_request.expired", f"change_request:{r[0]}", None,
                          {"kind": r[2], "target": r[3]}, tenant=r[1])
    return len(rows)


def _check_id(request_id: str) -> str:
    try:
        return str(uuid.UUID(str(request_id)))
    except (ValueError, TypeError):
        raise AdminError(404, "not_found", "change request not found") from None


async def list_requests(tenant: str, *, status: str | None = None, kind: str | None = None,
                        limit: int = 50) -> list[dict[str, Any]]:
    await expire_overdue(tenant)
    where = ["tenant = :t"]
    params: dict[str, Any] = {"t": tenant, "n": max(1, min(int(limit), 200))}
    if status:
        where.append("status = :st")
        params["st"] = status
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    async with tenant_session(tenant) as s:
        rows = (await s.execute(text(f"SELECT {_COLS} FROM admin_change_request WHERE {' AND '.join(where)} "
                                     "ORDER BY requested_at DESC, id DESC LIMIT :n"), params)).mappings().all()
    return [_json(r) for r in rows]


async def pending_count(tenant: str) -> int:
    await expire_overdue(tenant)
    async with tenant_session(tenant) as s:
        return int((await s.execute(text("SELECT count(*) FROM admin_change_request WHERE tenant = :t "
                                         "AND status = 'pending'"), {"t": tenant})).scalar_one())


async def get_request(tenant: str, request_id: str) -> dict[str, Any]:
    rid = _check_id(request_id)
    await expire_overdue(tenant)
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(f"SELECT {_COLS} FROM admin_change_request WHERE tenant = :t AND id = CAST(:i AS uuid)"),
                             {"t": tenant, "i": rid})).mappings().first()
    if m is None:
        raise AdminError(404, "not_found", "change request not found")
    return _json(m)


# ───────────────────────────── decisions ─────────────────────────────

Handler = Callable[[str, AdminChangeRequest, dict[str, Any], dict[str, Any]], Awaitable[Any]]
"""``(tenant, request, requester actor (+ roles/tenants at request time), approver actor) -> result``."""

HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


_DECISION_STATUS: Final = {"forbidden": 403, "not_pending": 409, "expired": 409, "self_approval": 403,
                           "reason_required": 422}
_DECISION_MESSAGE: Final = {
    "forbidden": "only a tenant admin of this tenant or a platform admin decides change requests",
    "not_pending": "this change request was already decided",
    "expired": "this change request expired (72 hours); submit the change again",
    "self_approval": "a different admin must approve your change (four-eyes)",
    "reason_required": "a rejection needs a reason",
}


def _approver(actor: Mapping[str, Any], roles: Collection[str], tenants: Collection[str],
              tenant: str) -> dict[str, Any]:
    role = "tenant-admin" if "tenant-admin" in roles and tenant in tenants else "platform-admin"
    return {**_actor_view(actor), "role": role}


async def _decide(tenant: str, request_id: str, decision: str, *, actor: dict[str, Any], actor_roles: Collection[str],
                  actor_tenants: Collection[str], reason: str | None) -> dict[str, Any]:
    rid = _check_id(request_id)
    if not str(actor.get("sub") or "").strip():
        # self-approval is detected by subject: an actor without one can never be told apart
        raise AdminError(403, "forbidden", _DECISION_MESSAGE["forbidden"])
    await expire_overdue(tenant)
    now = _now()
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(f"SELECT {_COLS} FROM admin_change_request WHERE tenant = :t "
                                  "AND id = CAST(:i AS uuid) FOR UPDATE"), {"t": tenant, "i": rid})).mappings().first()
        if m is None:
            raise AdminError(404, "not_found", "change request not found")
        cr = _model(m)
        error = change_request_decision_error(cr, decision=decision, actor_sub=str(actor.get("sub") or ""),  # type: ignore[arg-type]
                                              actor_roles=actor_roles, actor_tenants=actor_tenants, reason=reason,
                                              now=now)
        if error is None and decision == "approve" and cr.kind in PLATFORM_KINDS and "platform-admin" not in actor_roles:
            raise AdminError(403, "platform_admin_required",
                             "this change affects every tenant: a platform admin approves it")
        if error is not None:
            raise AdminError(_DECISION_STATUS[error], error, _DECISION_MESSAGE[error], status_now=cr.status)
        approver = _approver(actor, actor_roles, actor_tenants, tenant)
        result: Any = None
        if decision == "approve":
            fn = HANDLERS.get(cr.kind)
            if fn is None:
                raise AdminError(501, "not_implemented", f"no handler applies {cr.kind} changes")
            auth = m["requester_auth"] or {}
            requester = {**cr.requested_by.model_dump(mode="json"), "roles": list(auth.get("roles") or []),
                         "tenants": list(auth.get("tenants") or [])}
            try:
                result = await fn(tenant, cr, requester, {**dict(actor), "approver_role": approver["role"]})
            except AdminError as exc:
                if exc.status == 409 and exc.code in ("version_conflict", "stale_base"):
                    raise AdminError(409, "stale_base",
                                     "the target changed since this request was made; reject it and submit the "
                                     "change again", latest_version=exc.extra.get("latest_version"),
                                     base_version=cr.base_version) from None
                raise
            status = "approved" if cr.kind in ASYNC_KINDS else "applied"
        else:
            status = "rejected"
        await s.execute(
            text("UPDATE admin_change_request SET status = :st, approver = CAST(:ap AS jsonb), decided_at = :at, "
                 "decision_reason = :why, result = CAST(:res AS jsonb) WHERE tenant = :t AND id = CAST(:i AS uuid)"),
            {"st": status, "ap": dumps(approver), "at": now, "why": (reason or "").strip()[:500] or None,
             "res": dumps(_summary(result)), "t": tenant, "i": rid},
        )
        updated = (await s.execute(text(f"SELECT {_COLS} FROM admin_change_request WHERE tenant = :t "
                                        "AND id = CAST(:i AS uuid)"), {"t": tenant, "i": rid})).mappings().one()
        out = _json(updated)
    action = {"approved": "change_request.approved", "applied": "change_request.applied",
              "rejected": "change_request.rejected"}[status]
    await admin_audit(actor, action, f"change_request:{rid}", {"status": "pending"},
                      {"status": status, "kind": cr.kind, "target": cr.target, "requested_by": cr.requested_by.username,
                       "approver_role": approver["role"], "reason": (reason or "").strip()[:500] or None},
                      tenant=tenant)
    return out


def _summary(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, Mapping):
        return strip_secrets({k: v for k, v in result.items() if k in ("version", "status", "slug", "name", "id",
                                                                       "job_id", "deleted", "tool", "binding",
                                                                       "trust_level", "user", "username")})
    return None


async def approve(tenant: str, request_id: str, *, actor: dict[str, Any], actor_roles: Collection[str],
                  actor_tenants: Collection[str], reason: str | None = None) -> dict[str, Any]:
    return await _decide(tenant, request_id, "approve", actor=actor, actor_roles=actor_roles,
                         actor_tenants=actor_tenants, reason=reason)


async def reject(tenant: str, request_id: str, *, actor: dict[str, Any], actor_roles: Collection[str],
                 actor_tenants: Collection[str], reason: str | None) -> dict[str, Any]:
    return await _decide(tenant, request_id, "reject", actor=actor, actor_roles=actor_roles,
                         actor_tenants=actor_tenants, reason=reason)


# ───────────────────────────── handlers (re-validate + apply) ─────────────────────────────


@handler("agent_spec")
async def _apply_agent_spec(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import agent_specs

    assert cr.base_version is not None
    if "restore_version" in cr.payload:
        return await agent_specs.restore(tenant, str(cr.payload["restore_version"]), requester, platform_admin=False,
                                         cosigned=True, base_version=cr.base_version)
    envelope, _created = await agent_specs.put_spec(tenant, cr.base_version, dict(cr.payload.get("spec_patch") or {}),
                                                    requester, platform_admin=False, cosigned=True)
    return envelope


@handler("policy_set")
async def _apply_policy_set(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import policies

    assert cr.base_version is not None
    result, _created = await policies.put_policies(tenant, cr.base_version, list(cr.payload.get("rules") or []),
                                                   requester, platform_admin=False, cosigned=True,
                                                   version=cr.payload.get("version"))
    return {"version": result.get("spec_version")}


@handler("connector_tool")
async def _apply_tool(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import connector_admin

    p = cr.payload
    return await connector_admin.update_tool(tenant, str(p["connector"]), str(p["tool"]), dict(p.get("patch") or {}),
                                             requester, platform_admin=False, cosigned=True,
                                             base_version=cr.base_version)


@handler("tool_binding")
async def _apply_binding(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import connector_admin

    p = cr.payload
    return await connector_admin.set_binding(tenant, str(p["connector"]), str(p["tool"]), str(p["binding"]),
                                             requester, platform_admin=False, cosigned=True,
                                             base_version=cr.base_version)


@handler("connector_activate")
async def _apply_activate(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import connector_admin

    return await connector_admin.set_status(tenant, str(cr.payload["connector"]), "activate", requester,
                                            platform_admin=False, cosigned=True, base_version=cr.base_version)


@handler("connector_trust")
async def _apply_trust(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import connector_admin

    return await connector_admin.set_trust(tenant, str(cr.payload["connector"]), str(cr.payload["trust_level"]),
                                           requester, platform_admin=False, cosigned=True,
                                           base_version=cr.base_version)


@handler("connector_delete")
async def _apply_connector_delete(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any],
                                  _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import connector_admin

    slug = str(cr.payload["connector"])
    await connector_admin.delete_connector(tenant, slug, requester, platform_admin=False, cosigned=True,
                                           base_version=cr.base_version)
    return {"slug": slug, "deleted": True}


@handler("kb_delete")
async def _apply_kb_delete(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import kb

    try:
        return await kb.delete_kb(tenant, str(cr.payload["kb_id"]), requester, platform_admin=False, cosigned=True,
                                  base_version=cr.base_version)
    except kb.KbError as exc:
        raise AdminError(exc.status, exc.code, exc.message, **exc.extra) from None


@handler("embedding_profile")
async def _apply_embedding(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], approver: dict[str, Any]) -> Any:
    from keelson_engine.core import kb

    p = cr.payload
    try:
        job = await kb.start_reembed("platform", tenant, str(approver.get("username") or "admin"),
                                     alias=p.get("alias"), dims=p.get("dims"))
    except kb.KbError as exc:
        raise AdminError(exc.status, exc.code, exc.message, **exc.extra) from None
    await admin_audit(requester, "kb.reembed", "kb:reembed", None,
                      {"scope": "platform", "job_id": job.get("id"), "co_signed_by": approver.get("username")},
                      tenant=tenant)
    return {"job_id": job.get("id"), "status": job.get("status")}


@handler("connector_mapping")
async def _apply_connector_mapping(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any],
                                   _approver: dict[str, Any]) -> Any:
    """M8: an action mapping saved from a proposed draft (``121`` §3.6): re-validated against the
    current catalogue; a mapping version that moved since the request answers 409 ``stale_base``."""
    from keelson_engine.core import connector_mappings

    p = cr.payload
    base = int(cr.base_version or 0)
    row, _created = await connector_mappings.put_mapping(
        tenant, str(p["connector"]), str(p.get("kind") or "action"), dict(p.get("mapping") or {}),
        base_version=base or None, actor=requester, proposal_id=p.get("proposal_id"), cosigned=True)
    return {"version": row["version"]}


@handler("user_roles")
async def _apply_user_roles(tenant: str, cr: AdminChangeRequest, requester: dict[str, Any], _approver: dict[str, Any]) -> Any:
    from keelson_engine.core import users

    return await users.apply_change_request(tenant, dict(cr.payload), requester)
