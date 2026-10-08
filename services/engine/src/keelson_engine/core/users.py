"""Tenant users through the Keycloak admin API (``docs/contracts/110-m7-backoffice-generic.md`` §2).

Realm ``keelson`` (``Settings.keelson_realm``). A tenant's users are the users whose multi-valued
``tenant`` attribute holds the tenant slug (emitted as the ``tenants`` claim). Only
:data:`~keelson_contracts.admin.PRODUCT_ROLES` are read or written (Keycloak's own roles such as
``offline_access`` / ``default-roles-keelson`` are never shown nor touched).

* :func:`list_users` — ``GET /api/admin/users``;
* :func:`create_user` — ``POST /api/admin/users``: user with the tenant attribute, a random
  **temporary** password nobody sees + required action ``UPDATE_PASSWORD``; Keycloak e-mails a
  one-time "set your password" link when it can (``execute-actions-email``). The password is never
  returned, logged or stored (:class:`~keelson_contracts.admin.PasswordSetup` says what to do);
* :func:`patch_user` — ``PATCH /api/admin/users/{id} {roles?, enabled?}`` (full new product-role set);
* :func:`disable_user` — ``DELETE /api/admin/users/{id}`` (disable, never delete: audit history).

Authorisation (contracts ``user_change_violations``): only tenant-admins / platform-admins; a
tenant-admin never grants or revokes ``platform-admin``, never touches a platform-admin's account and
never changes a user who also belongs to a tenant the admin does not (realm roles are global). A
tenant-admin granting ``tenant-admin`` (new admin, added role, re-enabled admin) is a four-eyes
change request (kind ``user_roles``, ``user_change_guard_reasons``): approval re-runs both checks
with the requester's roles and the user's current state, then applies. Role changes and disabling
end the user's Keelson sessions (BFF rows) and Keycloak sessions, so they take effect at once.
Every change is audited (no password, no setup link in the audit row).
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from collections.abc import Collection, Mapping
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import text

from keelson_contracts.admin import (
    PRODUCT_ROLES,
    AdminUser,
    AdminUserCreate,
    AdminUserCreated,
    AdminUserPatch,
    PasswordSetup,
    user_change_guard_reasons,
    user_change_violations,
)
from keelson_engine.config import get_settings
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session
from keelson_engine.core.keycloak_admin import KeycloakAdmin, KeycloakAdminError, KeycloakConflict
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit

__all__ = [
    "SETUP_LINK_LIFESPAN_S",
    "apply_change_request",
    "create_user",
    "disable_user",
    "list_users",
    "patch_user",
    "set_keycloak_admin",
]

log = get_logger(__name__)

SETUP_LINK_LIFESPAN_S: Final = 12 * 3600
_KC_ID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TENANT_ATTR: Final = "tenant"

_kc: KeycloakAdmin | None = None


def set_keycloak_admin(kc: KeycloakAdmin | None) -> None:
    """Tests: use another admin client (default: ``KeycloakAdmin()`` from the settings)."""
    global _kc
    _kc = kc


def _keycloak() -> KeycloakAdmin:
    return _kc or KeycloakAdmin()


def _realm() -> str:
    return get_settings().keelson_realm


def _unavailable(exc: Exception) -> AdminError:
    log.warning("keycloak admin API failed", error=str(exc)[:200])
    return AdminError(502, "identity_provider_unavailable",
                      "The identity provider could not be reached; try again.")


def _tenants_of(user: Mapping[str, Any]) -> list[str]:
    return [str(v) for v in ((user.get("attributes") or {}).get(_TENANT_ATTR) or [])]


def _name(user: Mapping[str, Any]) -> str:
    return " ".join(x for x in (user.get("firstName"), user.get("lastName")) if x).strip()[:120]


def _created(user: Mapping[str, Any]) -> str | None:
    ts = user.get("createdTimestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return dt.datetime.fromtimestamp(ts / 1000, dt.UTC).isoformat()
    return None


def _view(user: Mapping[str, Any], roles: Collection[str]) -> dict[str, Any]:
    product = sorted({r for r in roles if r in PRODUCT_ROLES}, key=PRODUCT_ROLES.index)
    email = user.get("email") or None
    try:
        return AdminUser.model_validate({
            "id": str(user.get("id")), "username": str(user.get("username")), "email": email,
            "name": _name(user), "enabled": bool(user.get("enabled", True)), "roles": product,
            "tenants": _tenants_of(user), "created_at": _created(user),
        }).model_dump(mode="json")
    except ValidationError:
        # a Keycloak-side value outside the contract (e.g. an odd e-mail): shown, but never invented
        return AdminUser.model_validate({
            "id": str(user.get("id")), "username": str(user.get("username")).lower()[:63],
            "email": None, "name": _name(user), "enabled": bool(user.get("enabled", True)), "roles": product,
            "tenants": [t for t in _tenants_of(user) if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", t)],
            "created_at": _created(user),
        }).model_dump(mode="json")


# ───────────────────────────── reads ─────────────────────────────


async def list_users(tenant: str) -> list[dict[str, Any]]:
    kc, realm = _keycloak(), _realm()
    try:
        users = await kc.search_users_by_attribute(realm, _TENANT_ATTR, tenant)
        out = []
        for u in users:
            if not _KC_ID_RE.fullmatch(str(u.get("id") or "")):
                continue
            out.append(_view(u, await kc.realm_roles_of(realm, str(u["id"]))))
    except KeycloakAdminError as exc:
        raise _unavailable(exc) from None
    return sorted(out, key=lambda u: u["username"])


async def _load(tenant: str, user_id: str) -> tuple[dict[str, Any], list[str], list[str]]:
    """The user (must hold the tenant attribute, else 404) + its directly assigned realm roles +
    its effective product roles (incl. roles held through groups / composites: the authorization
    checks use these, so a platform-admin by group membership is still protected)."""
    if not _KC_ID_RE.fullmatch(user_id or ""):
        raise AdminError(404, "not_found", "user not found")
    kc, realm = _keycloak(), _realm()
    try:
        user = await kc.get_user(realm, user_id)
        if user is None or tenant not in _tenants_of(user):
            raise AdminError(404, "not_found", "user not found")
        direct = await kc.realm_roles_of(realm, user_id)
        effective = await kc.effective_realm_roles(realm, user_id)
        return user, direct, [r for r in PRODUCT_ROLES if r in {*direct, *effective}]
    except KeycloakAdminError as exc:
        raise _unavailable(exc) from None


# ───────────────────────────── writes ─────────────────────────────


def _actor_auth(actor: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    return [str(r) for r in actor.get("roles") or []], [str(t) for t in actor.get("tenants") or []]


def _violations(problems: list[str]) -> None:
    if problems:
        raise AdminError(403, "forbidden", problems[0], problems=problems)


async def _end_sessions(user_id: str, username: str) -> int:
    """Delete the user's Keelson BFF sessions (roles are read at login) + Keycloak sessions."""
    async with system_session() as s:
        n = (await s.execute(text("DELETE FROM bff_session WHERE sub = :sub"), {"sub": user_id})).rowcount or 0
    try:
        await _keycloak().logout_user(_realm(), user_id)
    except KeycloakAdminError as exc:
        log.warning("keycloak logout failed", username=username, error=str(exc)[:200])
    return int(n)


def _setup(sent: bool, email: str) -> PasswordSetup:
    if sent:
        hours = SETUP_LINK_LIFESPAN_S // 3600
        text_ = (f"Keycloak e-mailed {email} a one-time link to set a password (valid {hours} hours). "
                 "The account cannot be used until the password is set.")
    else:
        text_ = ("No e-mail could be sent (no mail server is configured). The account has a random temporary "
                 "password that nobody knows and must change it at first sign-in: a platform admin sets a "
                 "temporary password in the identity provider's admin console to hand over access.")
    return PasswordSetup(link=None, link_expires_at=None, instructions=text_[:500])


async def create_user(tenant: str, body: AdminUserCreate, actor: dict[str, Any], *,
                      cosigned: bool = False) -> tuple[int, dict[str, Any]]:
    """→ ``(201, AdminUserCreated)`` or ``(202, AdminChangeRequest)`` (tenant-admin creating an admin)."""
    roles, tenants = _actor_auth(actor)
    kc, realm = _keycloak(), _realm()
    _violations(user_change_violations(roles, actor_tenants=tenants, target_roles=[], target_tenants=[tenant],
                                       new_roles=list(body.roles)))
    try:
        if await kc.find_user(realm, body.username) is not None:
            raise AdminError(409, "user_exists", f"the username {body.username!r} is taken")
    except KeycloakAdminError as exc:
        raise _unavailable(exc) from None
    reasons = [] if cosigned else user_change_guard_reasons(roles, user=body.username, target_roles=[],
                                                            new_roles=list(body.roles))
    if reasons:
        from keelson_engine.core.change_requests import GuardedChange, create_request

        cr = await create_request(GuardedChange(
            tenant=tenant, actor=actor, kind="user_roles", target=f"user:{body.username}",
            reasons=[r.model_dump(mode="json") for r in reasons],
            payload={"action": "create", "user": body.model_dump(mode="json")},
            diff=[{"op": "add", "path": f"/users/{body.username}", "after": body.model_dump(mode="json")}],
            message="creating an admin needs a second admin"), actor_roles=roles, actor_tenants=tenants)
        return 202, cr
    first, _, last = body.name.partition(" ")
    representation = {
        "username": body.username, "email": body.email, "emailVerified": False, "enabled": True,
        "firstName": first, "lastName": last or first, "attributes": {_TENANT_ATTR: [tenant]},
        "requiredActions": ["UPDATE_PASSWORD"],
        "credentials": [{"type": "password", "value": secrets.token_urlsafe(24), "temporary": True}],
    }
    try:
        user_id = await kc.create_user(realm, representation)
        await kc.set_realm_roles(realm, user_id, add=list(body.roles), remove=[])
        sent = await kc.send_actions_email(realm, user_id, ["UPDATE_PASSWORD"], lifespan_s=SETUP_LINK_LIFESPAN_S,
                                           client_id=get_settings().oidc_client_id,
                                           redirect_uri=get_settings().public_url)
        user = await kc.get_user(realm, user_id) or {**representation, "id": user_id}
        granted = await kc.realm_roles_of(realm, user_id)
    except KeycloakConflict:
        raise AdminError(409, "user_exists", "a user with this username or e-mail exists") from None
    except KeycloakAdminError as exc:
        raise _unavailable(exc) from None
    view = _view(user, granted)
    setup = _setup(sent, body.email)
    await admin_audit(actor, "user.create", f"user:{body.username}", None,
                      {"id": user_id, "roles": view["roles"], "tenant": tenant, "setup_email_sent": sent,
                       "co_signed": cosigned}, tenant=tenant)
    return 201, AdminUserCreated.model_validate({"user": view, "setup": setup.model_dump(mode="json")}).model_dump(
        mode="json")


async def patch_user(tenant: str, user_id: str, patch: AdminUserPatch, actor: dict[str, Any], *,
                     cosigned: bool = False, action: str = "user.update") -> tuple[int, dict[str, Any]]:
    """→ ``(200, AdminUser)`` or ``(202, AdminChangeRequest)``."""
    roles, tenants = _actor_auth(actor)
    user, current_roles, effective = await _load(tenant, user_id)
    current = [r for r in current_roles if r in PRODUCT_ROLES]
    new_roles = list(patch.roles) if patch.roles is not None else None
    # authorization on the effective roles; a role only inherited (group) is never "revoked" by
    # leaving it out of new_roles, so the role diff for the check is taken against the direct set
    _violations(user_change_violations(roles, actor_tenants=tenants, target_roles=current,
                                       target_tenants=_tenants_of(user), new_roles=new_roles, enabled=patch.enabled))
    if "platform-admin" in effective and "platform-admin" not in roles:
        raise AdminError(403, "forbidden", "only a platform-admin changes a platform-admin's account")
    username = str(user.get("username"))
    reasons = [] if cosigned else user_change_guard_reasons(
        roles, user=username, target_roles=effective, new_roles=new_roles, enabled=patch.enabled,
        target_enabled=bool(user.get("enabled", True)))
    if reasons:
        from keelson_engine.core.change_requests import GuardedChange, create_request

        before = {"roles": current, "enabled": bool(user.get("enabled", True))}
        after = {"roles": new_roles if new_roles is not None else current,
                 "enabled": patch.enabled if patch.enabled is not None else before["enabled"]}
        from keelson_engine.core.change_requests import json_diff

        cr = await create_request(GuardedChange(
            tenant=tenant, actor=actor, kind="user_roles", target=f"user:{username}",
            reasons=[r.model_dump(mode="json") for r in reasons],
            payload={"action": "patch", "user_id": user_id, "username": username,
                     **patch.model_dump(mode="json", exclude_none=True)},
            diff=json_diff(before, after, prefix=("users", username)),
            message="granting an admin role needs a second admin"), actor_roles=roles, actor_tenants=tenants)
        return 202, cr
    kc, realm = _keycloak(), _realm()
    try:
        if new_roles is not None:
            add = sorted(set(new_roles) - set(current))
            remove = sorted(set(current) - set(new_roles))
            await kc.set_realm_roles(realm, user_id, add=add, remove=remove)
        if patch.enabled is not None and patch.enabled != bool(user.get("enabled", True)):
            await kc.update_user(realm, user_id, {"enabled": patch.enabled})
        fresh = await kc.get_user(realm, user_id) or user
        granted = await kc.realm_roles_of(realm, user_id)
    except KeycloakAdminError as exc:
        raise _unavailable(exc) from None
    view = _view(fresh, granted)
    changed_roles = new_roles is not None and set(new_roles) != set(current)
    ended = 0
    if changed_roles or patch.enabled is False:
        ended = await _end_sessions(user_id, username)
    await admin_audit(actor, action, f"user:{username}",
                      {"roles": current, "enabled": bool(user.get("enabled", True))},
                      {"roles": view["roles"], "enabled": view["enabled"], "sessions_ended": ended,
                       "co_signed": cosigned}, tenant=tenant)
    return 200, view


async def disable_user(tenant: str, user_id: str, actor: dict[str, Any]) -> dict[str, Any]:
    status, out = await patch_user(tenant, user_id, AdminUserPatch(enabled=False), actor, action="user.disable")
    assert status == 200  # disabling is never guarded
    return out


async def apply_change_request(tenant: str, payload: dict[str, Any], requester: dict[str, Any]) -> dict[str, Any]:
    """Approved ``user_roles`` change request: re-validate with the requester's roles + the user's
    current state, then apply (``cosigned``)."""
    action = payload.get("action")
    try:
        if action == "create":
            body = AdminUserCreate.model_validate(payload.get("user") or {})
            _status, out = await create_user(tenant, body, requester, cosigned=True)
            return {"user": out.get("user", {}).get("username"), "id": out.get("user", {}).get("id"),
                    "setup": out.get("setup")}
        if action == "patch":
            patch = AdminUserPatch.model_validate({k: payload[k] for k in ("roles", "enabled") if k in payload})
            _status, out = await patch_user(tenant, str(payload.get("user_id") or ""), patch, requester,
                                            cosigned=True)
            return {"user": out.get("username"), "id": out.get("id")}
    except ValidationError as exc:
        raise AdminError(422, "invalid_request", "the stored change is no longer valid",
                         detail=str(exc)[:300]) from None
    raise AdminError(422, "invalid_request", f"unknown user change {action!r}")
