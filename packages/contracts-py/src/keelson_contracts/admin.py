"""M7 back-office shapes (``110-m7-backoffice-generic.md`` §1-§2): four-eyes change requests on
guarded admin changes, and tenant users with product roles.

**Four-eyes.** A guarded change (plan §17: raising approval thresholds / auto caps, removing or
weakening refuse / approval rules, reclassifying a write tool as read or untrusted text as
trusted, binding ``auto`` on a write tool, enabling an external-egress connector, adding a
first-party trust level, changing the active embedding profile, deleting a connector or a
knowledge base; M7 addition: a tenant-admin granting an admin role, kind ``user_roles``) is not
refused with 403 any more: the PUT/PATCH answers ``202`` with an
:class:`AdminChangeRequest` (``pending``, expires after :data:`CHANGE_REQUEST_TTL`). A
*different* tenant-admin of the tenant, or any platform-admin (co-sign), approves via
``POST /api/admin/change-requests/{id}/approve`` (:class:`ChangeRequestDecision`); approval
re-validates the change against the current state and applies it in the same transaction
(→ ``applied``; ``409`` and still ``pending`` when the base version moved). ``reject`` needs a
reason; the requester may reject (withdraw) their own request but never approve it. Rules in one
place: :func:`change_request_decision_error`.

**Users.** ``GET/POST/PATCH/DELETE /api/admin/users`` manage Keycloak users (realm ``keelson``)
holding the tenant attribute. Only :data:`PRODUCT_ROLES` are ever read or written; only a
platform-admin grants or revokes ``platform-admin``, touches a platform-admin's account or
changes a user who also belongs to another tenant (realm roles are global;
:func:`user_change_violations`). A tenant-admin granting ``tenant-admin`` is itself a guarded
change (:func:`user_change_guard_reasons`), so nobody approves their own change with a second
account they created. Passwords are never returned (:class:`PasswordSetup`). Change requests
never carry credentials.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Collection, Mapping
from typing import Annotated, Any, Final, Literal, get_args
from urllib.parse import urlsplit

from pydantic import AfterValidator, AwareDatetime, Field, StrictBool, model_validator

from .common import ContractModel, TenantSlug

__all__ = [
    "CHANGE_REQUEST_KINDS",
    "CHANGE_REQUEST_STATUSES",
    "CHANGE_REQUEST_TTL",
    "GUARD_REASONS_BY_KIND",
    "GUARD_REASON_CODES",
    "PRODUCT_ROLES",
    "VERSIONED_CHANGE_KINDS",
    "AdminActor",
    "AdminChangeRequest",
    "AdminUser",
    "AdminUserCreate",
    "AdminUserCreated",
    "AdminUserPatch",
    "ChangeApprover",
    "ChangeDiffEntry",
    "ChangeRequestDecision",
    "ChangeRequestKind",
    "ChangeRequestStatus",
    "GuardReason",
    "GuardReasonCode",
    "PasswordSetup",
    "ProductRole",
    "change_request_decision_error",
    "grantable_roles",
    "user_change_guard_reasons",
    "user_change_violations",
]

# ───────────────────────────────── roles ────────────────────────────────────

ProductRole = Literal["planner", "approver", "compliance", "tenant-admin", "platform-admin", "demo-presenter"]
"""Keelson realm roles (``10-auth.md``); Keycloak's own roles (``offline_access``,
``default-roles-keelson`` …) are never shown or written."""
PRODUCT_ROLES: Final[tuple[str, ...]] = get_args(ProductRole)
_ADMIN_ROLES: Final = frozenset({"tenant-admin", "platform-admin"})


def grantable_roles(actor_roles: Collection[str]) -> tuple[str, ...]:
    """Product roles the actor may grant or revoke (none for non-admins)."""
    if "platform-admin" in actor_roles:
        return PRODUCT_ROLES
    if "tenant-admin" in actor_roles:
        return tuple(r for r in PRODUCT_ROLES if r != "platform-admin")
    return ()


def user_change_violations(
    actor_roles: Collection[str],
    *,
    actor_tenants: Collection[str],
    target_roles: Collection[str],
    target_tenants: Collection[str],
    new_roles: Collection[str] | None = None,
    enabled: bool | None = None,
) -> list[str]:
    """Why the actor may not make this user change (empty = allowed). ``target_roles`` = the
    user's current product roles; ``new_roles`` = the full new set (``None`` = unchanged);
    ``actor_tenants`` / ``target_tenants`` = the tenant attribute of the actor / the user (for a
    new user: the tenant it is created in).

    Realm roles are **global** in Keycloak (a user's ``tenant-admin`` role applies in every
    tenant the user belongs to), so a tenant-admin may only change users whose tenants are all
    tenants the actor belongs to: otherwise granting ``tenant-admin`` (or disabling the account)
    would reach into another tenant. Users of other tenants are a platform-admin's job."""
    problems: list[str] = []
    allowed = set(grantable_roles(actor_roles))
    if not allowed:
        return ["only tenant-admins and platform-admins manage users"]
    is_platform = "platform-admin" in actor_roles
    if not is_platform:
        if not target_tenants:
            problems.append("the user belongs to no tenant of yours: only a platform-admin changes it")
        elif outside := sorted(set(target_tenants) - set(actor_tenants)):
            problems.append(f"the user also belongs to tenants {outside}: only a platform-admin changes it")
    if "platform-admin" in target_roles and not is_platform and (new_roles is not None or enabled is not None):
        problems.append("only a platform-admin changes a platform-admin's account")
    if new_roles is not None:
        if unknown := sorted(set(new_roles) - set(PRODUCT_ROLES)):
            problems.append(f"not product roles: {unknown}")
        changed = set(new_roles) ^ set(target_roles)
        if forbidden := sorted((changed - allowed) & set(PRODUCT_ROLES)):
            problems.append(f"cannot grant or revoke {forbidden}")
    return problems


def user_change_guard_reasons(
    actor_roles: Collection[str],
    *,
    user: str,
    target_roles: Collection[str],
    new_roles: Collection[str] | None = None,
    enabled: bool | None = None,
    target_enabled: bool | None = None,
) -> list[GuardReason]:
    """Guard reasons of a user change made by a **tenant-admin** (empty = apply directly).

    Granting ``tenant-admin`` / ``platform-admin`` (creating an admin, adding the role, or
    re-enabling a disabled admin) is a guarded change (kind ``user_roles``): otherwise one
    tenant-admin could create a second admin account and approve their own change requests with
    it, which defeats four-eyes. A platform-admin's user changes are not guarded (a platform-admin
    is the co-signing authority; this also lets them bootstrap a new tenant's first admin).
    ``user`` = the username (``target`` of the change request); ``target_enabled`` = the user's
    current enabled flag (``None`` for a new user)."""
    if "platform-admin" in actor_roles:
        return []
    before = set(target_roles) & _ADMIN_ROLES
    after = set(new_roles) & _ADMIN_ROLES if new_roles is not None else before
    reasons: list[GuardReason] = []
    for role in sorted(after - before):
        reasons.append(GuardReason(code="admin_role_granted", field=f"user:{user}/role:{role}",
                                   reason=f"grants the {role} role"))
    if enabled is True and target_enabled is False and (held := sorted(after & before)):
        for role in held:
            reasons.append(GuardReason(code="admin_role_granted", field=f"user:{user}/role:{role}",
                                       reason=f"re-enables an account holding the {role} role"))
    return reasons


def _unique_sorted_roles(roles: list[str]) -> list[str]:
    if len(set(roles)) != len(roles):
        raise ValueError("roles must be unique")
    return roles


Roles = Annotated[list[ProductRole], AfterValidator(_unique_sorted_roles)]

USERNAME_PATTERN: Final = r"^[a-z0-9][a-z0-9._-]{1,62}$"
EMAIL_PATTERN: Final = r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
_KC_ID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _display_name(value: str) -> str:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value) or value != value.strip():
        raise ValueError("a single trimmed line without control characters")
    return value


Username = Annotated[str, Field(pattern=USERNAME_PATTERN)]
Email = Annotated[str, Field(max_length=254, pattern=EMAIL_PATTERN)]
DisplayName = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(_display_name)]


class AdminUser(ContractModel):
    """A user holding the tenant attribute (``GET /api/admin/users``)."""

    id: Annotated[str, Field(pattern=_KC_ID_PATTERN)]
    username: Username
    email: Email | None
    name: Annotated[str, Field(max_length=120)]
    enabled: StrictBool
    roles: Roles
    tenants: list[TenantSlug]
    created_at: AwareDatetime | None


class AdminUserCreate(ContractModel):
    """``POST /api/admin/users``: the engine creates the user with the active tenant's attribute,
    a temporary password and the required action ``UPDATE_PASSWORD``."""

    username: Username
    email: Email
    name: DisplayName
    roles: Annotated[Roles, Field(min_length=1)]


SETUP_LINK_PATTERN: Final = r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^\s]*)?$"
"""Absolute http(s) URL with a plain host (also in the JSON Schema, so the web rejects
``javascript:`` / userinfo links before rendering an ``href``)."""


def _setup_link(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        raise ValueError("link must be an http(s) URL with a host and no userinfo")
    if any(c.isspace() or ord(c) < 0x20 for c in value):
        raise ValueError("link must not contain whitespace or control characters")
    return value


class PasswordSetup(ContractModel):
    """How the new user sets a password. The temporary password itself is never returned or
    stored: ``link`` is a one-time action link (Keycloak execute-actions) when available;
    ``instructions`` say what to do either way."""

    required_action: Literal["UPDATE_PASSWORD"] = "UPDATE_PASSWORD"
    link: Annotated[str, Field(max_length=2000, pattern=SETUP_LINK_PATTERN), AfterValidator(_setup_link)] | None = (
        Field(repr=False)
    )
    """A bearer credential (whoever opens it sets the password): kept out of ``repr`` so it never
    lands in logs; returned once to the creating admin and never stored."""
    link_expires_at: AwareDatetime | None
    instructions: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def _expiry_iff_link(self) -> PasswordSetup:
        if (self.link is None) != (self.link_expires_at is None):
            raise ValueError("link_expires_at is set exactly when a link is")
        return self


class AdminUserCreated(ContractModel):
    user: AdminUser
    setup: PasswordSetup


class AdminUserPatch(ContractModel):
    """``PATCH /api/admin/users/{id}``: the full new product-role set and/or enabled flag."""

    roles: Roles | None = None
    enabled: StrictBool | None = None

    @model_validator(mode="after")
    def _something(self) -> AdminUserPatch:
        if self.roles is None and self.enabled is None:
            raise ValueError("set roles and/or enabled")
        return self


# ────────────────────────────── change requests ─────────────────────────────

CHANGE_REQUEST_TTL: Final = dt.timedelta(hours=72)

ChangeRequestKind = Literal[
    "agent_spec",
    "policy_set",
    "connector_tool",
    "tool_binding",
    "connector_activate",
    "connector_trust",
    "embedding_profile",
    "connector_delete",
    "kb_delete",
    "user_roles",
    "connector_mapping",
]
"""Which admin mutation the request applies when approved (its handler re-validates)."""
CHANGE_REQUEST_KINDS: Final[tuple[str, ...]] = get_args(ChangeRequestKind)

ChangeRequestStatus = Literal["pending", "approved", "rejected", "applied", "expired"]
"""``applied``: approved and applied in one transaction (the normal outcome of approve);
``approved``: approved, applied asynchronously (e.g. an embedding-profile switch that starts a
re-embed job)."""
CHANGE_REQUEST_STATUSES: Final[tuple[str, ...]] = get_args(ChangeRequestStatus)

GuardReasonCode = Literal[
    "auto_cap_raised",
    "approval_threshold_raised",
    "approver_changed",
    "quorum_lowered",
    "planning_connector_changed",
    "compliance_block_removed",
    "auto_rule_added",
    "policy_rule_removed",
    "policy_rule_weakened",
    "tool_reclassified_read",
    "tool_reclassified_trusted",
    "auto_binding_on_write",
    "external_egress_enabled",
    "first_party_trust_added",
    "embedding_profile_changed",
    "connector_deleted",
    "kb_deleted",
    "admin_role_granted",
    "ai_proposed_action_mapping",
]
GUARD_REASON_CODES: Final[tuple[str, ...]] = get_args(GuardReasonCode)

_POLICY_REASONS: Final = frozenset(
    {
        "auto_cap_raised",
        "approval_threshold_raised",
        "approver_changed",
        "quorum_lowered",
        "auto_rule_added",
        "policy_rule_removed",
        "policy_rule_weakened",
    }
)
GUARD_REASONS_BY_KIND: Final[Mapping[str, frozenset[str]]] = {
    "agent_spec": _POLICY_REASONS | {"planning_connector_changed", "compliance_block_removed"},
    "policy_set": _POLICY_REASONS,
    "connector_tool": frozenset({"tool_reclassified_read", "tool_reclassified_trusted"}),
    "tool_binding": frozenset({"auto_binding_on_write"}),
    "connector_activate": frozenset({"external_egress_enabled"}),
    "connector_trust": frozenset({"first_party_trust_added"}),
    "embedding_profile": frozenset({"embedding_profile_changed"}),
    "connector_delete": frozenset({"connector_deleted"}),
    "kb_delete": frozenset({"kb_deleted"}),
    "user_roles": frozenset({"admin_role_granted"}),
    "connector_mapping": frozenset({"ai_proposed_action_mapping"}),
}
"""Reason codes each kind may carry."""
assert set(GUARD_REASONS_BY_KIND) == set(CHANGE_REQUEST_KINDS)
assert set().union(*GUARD_REASONS_BY_KIND.values()) == set(GUARD_REASON_CODES)

_UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
JSON_POINTER_PATTERN: Final = r"^(/([^~/]|~[01])*)*$"
_MAX_JSON_CHARS: Final = 256 * 1024


def _json_value(value: Any, what: str) -> None:
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be finite JSON: {exc}") from exc
    if len(text) > _MAX_JSON_CHARS:
        raise ValueError(f"{what} is larger than {_MAX_JSON_CHARS // 1024} KiB")


VERSIONED_CHANGE_KINDS: Final = frozenset({"agent_spec", "policy_set", "connector_mapping"})
"""Kinds whose target is versioned (AgentSpec, policy set, M8: connector mapping): ``base_version``
is required so approval can refuse a stale apply (``409``)."""

_SECRET_KEYS: Final = frozenset(
    {
        "secret", "password", "passwd", "api_key", "apikey", "token", "access_token", "refresh_token",
        "id_token", "authorization", "private_key", "secret_enc", "credentials", "cookie",
    }
)


def _secret_key(key: str) -> bool:
    k = key.strip().lower().replace("-", "_")
    return k in _SECRET_KEYS or k.endswith(("_secret", "_password", "_api_key", "_private_key"))


def _no_secrets(value: Any, what: str, depth: int = 0) -> None:
    """Change requests are shown to other admins and audited: they must never carry credentials
    (the engine strips them from the stored payload; a connector's secret is set by its own,
    unguarded endpoint)."""
    if depth > 64:
        return
    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(k, str) and _secret_key(k):
                raise ValueError(f"{what} must not carry credentials (key {k[:40]!r})")
            _no_secrets(v, what, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _no_secrets(v, what, depth + 1)


class AdminActor(ContractModel):
    sub: Annotated[str, Field(min_length=1, max_length=200)]
    username: Annotated[str, Field(min_length=1, max_length=200)]
    name: Annotated[str, Field(max_length=200)]


class ChangeApprover(AdminActor):
    """Who decided; ``role`` = the admin role the decision was made under (``platform-admin`` =
    co-sign from outside the tenant)."""

    role: Literal["tenant-admin", "platform-admin"]


class GuardReason(ContractModel):
    """Why a change is guarded (one entry per guarded field / rule)."""

    code: GuardReasonCode
    field: Annotated[str, Field(min_length=1, max_length=200)]
    """The admin field or object (``policy.auto_max_lines``, ``policy.rules.auto_small``,
    ``connector:merxa-lite/tool:update_item`` …)."""
    reason: Annotated[str, Field(min_length=1, max_length=300)]
    """Human-readable ("raises the automatic-approval caps")."""


class ChangeDiffEntry(ContractModel):
    """One RFC 6902-style difference between the current and the requested state (for the
    "Pending changes" diff view). ``add`` has no ``before``, ``remove`` no ``after``."""

    op: Literal["add", "remove", "replace"]
    path: Annotated[str, Field(max_length=500, pattern=JSON_POINTER_PATTERN)]
    before: Any = None
    after: Any = None

    @model_validator(mode="after")
    def _shape(self) -> ChangeDiffEntry:
        if self.op == "add" and self.before is not None:
            raise ValueError("an add has no before value")
        if self.op == "remove" and self.after is not None:
            raise ValueError("a remove has no after value")
        _json_value(self.before, "before")
        _json_value(self.after, "after")
        return self


class AdminChangeRequest(ContractModel):
    """``admin_change_request`` row / API shape (``202`` body of a guarded PUT/PATCH,
    ``GET /api/admin/change-requests``)."""

    id: Annotated[str, Field(pattern=_UUID_PATTERN)]
    tenant: TenantSlug
    kind: ChangeRequestKind
    target: Annotated[str, Field(min_length=1, max_length=200)]
    payload: dict[str, Any]
    """The original request body; re-validated when applied."""
    base_version: Annotated[str, Field(min_length=1, max_length=64)] | None
    """The version the change was made against (``409`` on approve when it moved)."""
    diff: Annotated[list[ChangeDiffEntry], Field(min_length=1, max_length=200)]
    reasons: Annotated[list[GuardReason], Field(min_length=1, max_length=50)]
    requested_by: AdminActor
    requested_at: AwareDatetime
    status: ChangeRequestStatus
    approver: ChangeApprover | None
    decided_at: AwareDatetime | None
    decision_reason: Annotated[str, Field(min_length=1, max_length=500)] | None
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _rules(self) -> AdminChangeRequest:
        _json_value(self.payload, "payload")
        _no_secrets(self.payload, "payload")
        for i, entry in enumerate(self.diff):
            segments = [p.replace("~1", "/").replace("~0", "~") for p in entry.path.split("/")[1:]]
            if any(_secret_key(p) for p in segments):
                raise ValueError(f"diff/{i} must not carry credentials (path {entry.path[:60]!r})")
            _no_secrets([entry.before, entry.after], f"diff/{i}")
        if self.kind in VERSIONED_CHANGE_KINDS and self.base_version is None:
            raise ValueError(f"a {self.kind} change names its base_version (approve answers 409 when it moved)")
        if self.expires_at - self.requested_at != CHANGE_REQUEST_TTL:
            raise ValueError("expires_at is requested_at + 72 h")
        allowed = GUARD_REASONS_BY_KIND[self.kind]
        if bad := sorted({r.code for r in self.reasons} - allowed):
            raise ValueError(f"reason codes {bad} do not apply to a {self.kind} change")
        keys = [(r.code, r.field) for r in self.reasons]
        if len(set(keys)) != len(keys):
            raise ValueError("reasons must be unique per (code, field)")
        decided = self.status in ("approved", "rejected", "applied")
        if not decided:
            if self.approver is not None or self.decided_at is not None or self.decision_reason is not None:
                raise ValueError(f"a {self.status} request has no approver / decided_at / decision_reason")
            return self
        if self.approver is None or self.decided_at is None:
            raise ValueError(f"a {self.status} request names its approver and decided_at")
        if not self.requested_at <= self.decided_at <= self.expires_at:
            raise ValueError("decided_at lies between requested_at and expires_at")
        if self.status != "rejected" and self.approver.sub == self.requested_by.sub:
            raise ValueError("self-approval is impossible: a different admin approves")
        if self.status == "rejected" and self.decision_reason is None:
            raise ValueError("a rejection carries its reason")
        return self

    def is_expired(self, now: dt.datetime) -> bool:
        """A pending request past its expiry (the engine flips it to ``expired``)."""
        return self.status == "pending" and now >= self.expires_at


class ChangeRequestDecision(ContractModel):
    """Body of ``…/approve`` and ``…/reject`` (a reject needs ``reason``)."""

    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None


DecisionError = Literal["not_pending", "expired", "self_approval", "forbidden", "reason_required"]


def change_request_decision_error(
    cr: AdminChangeRequest,
    *,
    decision: Literal["approve", "reject"],
    actor_sub: str,
    actor_roles: Collection[str],
    actor_tenants: Collection[str],
    reason: str | None,
    now: dt.datetime,
) -> DecisionError | None:
    """Why the actor cannot decide the request (``None`` = may decide). Order: ``forbidden``
    (not a tenant-admin of the request's tenant nor a platform-admin) → ``not_pending`` →
    ``expired`` → ``self_approval`` (the requester may reject = withdraw, never approve) →
    ``reason_required`` (reject without reason)."""
    platform = "platform-admin" in actor_roles
    tenant_admin = "tenant-admin" in actor_roles and cr.tenant in actor_tenants
    if not (platform or tenant_admin):
        return "forbidden"
    if cr.status != "pending":
        return "not_pending"
    if cr.is_expired(now):
        return "expired"
    if decision == "approve" and actor_sub == cr.requested_by.sub:
        return "self_approval"
    if decision == "reject" and not (reason and reason.strip()):
        return "reason_required"
    return None
