"""The authenticated caller, built from a merxa-realm access token or a UI session."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

READ_SCOPES: tuple[str, ...] = (
    "catalog:read",
    "inventory:read",
    "po:read",
    "orders:read",
    "snapshot:read",
)
SCOPE_PREVIEW = "changes:preview"
SCOPE_APPLY = "changes:apply"
SCOPE_APPLY_AUTO = "changes:apply:auto"
ALL_SCOPES: tuple[str, ...] = (*READ_SCOPES, SCOPE_PREVIEW, SCOPE_APPLY, SCOPE_APPLY_AUTO)

ROLE_PLANNER = "merch-planner"
ROLE_OPS_DIRECTOR = "ops-director"
ROLE_INTEGRATION_WRITER = "integration-writer"
ROLE_ADMIN = "merxa-admin"

AUTOCOMMIT_CLIENT = "keelson-autocommit"


def roles_from_claims(claims: Mapping[str, Any]) -> frozenset[str]:
    """Realm roles: Keycloak's ``realm_access.roles`` plus a flat ``roles`` claim if mapped."""
    roles: set[str] = set()
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, Mapping):
        value = realm_access.get("roles")
        if isinstance(value, list):
            roles.update(str(r) for r in value)
    flat = claims.get("roles")
    if isinstance(flat, str):
        roles.add(flat)
    elif isinstance(flat, list):
        roles.update(str(r) for r in flat)
    return frozenset(roles)


def scopes_from_claims(claims: Mapping[str, Any]) -> frozenset[str]:
    for name in ("scope", "scp"):
        value = claims.get(name)
        if isinstance(value, str):
            return frozenset(value.split())
        if isinstance(value, list):
            return frozenset(str(v) for v in value)
    return frozenset()


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. ``kind`` is ``bearer`` (OAuth access token), ``session`` (Merxa UI) or
    ``api_key`` (Merxa-issued API key; its scopes are the grant, it has no roles)."""

    sub: str
    kind: str
    username: str | None = None
    display: str | None = None
    email: str | None = None
    client_id: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    claims: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_claims(cls, claims: Mapping[str, Any]) -> Principal:
        sub = str(claims.get("sub") or "")
        return cls(
            sub=sub,
            kind="bearer",
            username=claims.get("preferred_username"),
            display=claims.get("name"),
            email=claims.get("email"),
            client_id=claims.get("azp") or claims.get("client_id"),
            scopes=scopes_from_claims(claims),
            roles=roles_from_claims(claims),
            claims=dict(claims),
        )

    @classmethod
    def from_session(
        cls,
        *,
        sub: str,
        username: str,
        name: str | None,
        email: str | None,
        roles: Iterable[str],
        client_id: str,
    ) -> Principal:
        role_set = frozenset(roles)
        return cls(
            sub=sub,
            kind="session",
            username=username,
            display=name,
            email=email,
            client_id=client_id,
            scopes=session_scopes(role_set),
            roles=role_set,
            claims={},
        )

    # ── helpers ──
    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_any_scope(self, scopes: Iterable[str]) -> bool:
        return any(s in self.scopes for s in scopes)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    @property
    def via(self) -> str:
        """Attribution channel: ``Keelson`` for keelson-* OAuth clients."""
        cid = self.client_id or ""
        if cid.startswith("keelson-"):
            return "Keelson"
        if self.kind == "session":
            return "Merxa UI"
        if self.kind == "api_key":
            return "API key"
        return "API"

    @property
    def actor_display(self) -> str:
        return self.display or self.username or self.sub


def session_scopes(roles: frozenset[str]) -> frozenset[str]:
    """Scopes granted to an interactive Merxa UI session, derived from realm roles."""
    scopes: set[str] = set(READ_SCOPES)
    if roles & {ROLE_PLANNER, ROLE_OPS_DIRECTOR, ROLE_ADMIN}:
        scopes.add(SCOPE_PREVIEW)
    if ROLE_OPS_DIRECTOR in roles:
        scopes.add(SCOPE_APPLY)
    return frozenset(scopes)
