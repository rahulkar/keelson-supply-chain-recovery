"""Keycloak admin REST API (``KEYCLOAK_INTERNAL_URL`` + ``KC_ADMIN_PASSWORD``, master realm
``admin-cli`` password grant). Used to add a tenant to users' multi-valued ``tenant`` attribute
(realm ``keelson``; emitted as the ``tenants`` claim) when a tenant is created from a pack.

The admin token is requested per operation (short-lived) and never logged.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

import httpx

from keelson_engine.config import get_settings
from keelson_engine.core.http import internal_http
from keelson_engine.core.logs import get_logger

__all__ = ["KeycloakAdmin", "KeycloakAdminError", "KeycloakConflict"]

log = get_logger(__name__)

AttributeResult = Literal["added", "already", "not_found"]


class KeycloakAdminError(RuntimeError):
    pass


def _seg(value: str) -> str:
    """One URL path segment (realm, user id, client id): percent-encoded, never ``/`` / ``..``."""
    v = str(value)
    if not v or v in (".", ".."):
        raise KeycloakAdminError("invalid path segment")
    return quote(v, safe="")


class KeycloakAdmin:
    def __init__(self, base_url: str | None = None, password: str | None = None,
                 http: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self.base = (base_url or s.keycloak_internal_url).rstrip("/")
        self._password = password if password is not None else s.kc_admin_password.get_secret_value()
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http or internal_http()

    async def _token(self) -> str:
        if not self._password:
            raise KeycloakAdminError("KC_ADMIN_PASSWORD is not configured")
        try:
            resp = await self.http.post(
                f"{self.base}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli", "username": "admin",
                      "password": self._password},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(f"Keycloak unreachable ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise KeycloakAdminError(f"Keycloak admin login failed (HTTP {resp.status_code})")
        return str(resp.json()["access_token"])

    async def _req(self, method: str, path: str, token: str, **kw: Any) -> httpx.Response:
        try:
            return await self.http.request(method, f"{self.base}{path}",
                                           headers={"Authorization": f"Bearer {token}"}, timeout=10.0, **kw)
        except httpx.HTTPError as exc:
            raise KeycloakAdminError(f"Keycloak unreachable ({exc.__class__.__name__})") from exc

    async def add_attribute_value(self, realm: str, username: str, attribute: str, value: str) -> AttributeResult:
        """Append ``value`` to the user's multi-valued ``attribute`` (idempotent)."""
        token = await self._token()
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users", token,
                               params={"username": username, "exact": "true"})
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user lookup failed (HTTP {resp.status_code})")
        matches = [u for u in resp.json() if str(u.get("username", "")).lower() == username.lower()]
        if not matches:
            return "not_found"
        user_id = matches[0]["id"]
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}", token)
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user read failed (HTTP {resp.status_code})")
        user = resp.json()
        attributes = dict(user.get("attributes") or {})
        values = [str(v) for v in attributes.get(attribute) or []]
        if value in values:
            return "already"
        attributes[attribute] = [*values, value]
        user["attributes"] = attributes
        resp = await self._req("PUT", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}", token, json=user)
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(f"user update failed (HTTP {resp.status_code})")
        log.info("keycloak user attribute added", realm=realm, username=username, attribute=attribute)
        return "added"

    async def _user_id(self, realm: str, username: str, token: str) -> str | None:
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users", token,
                               params={"username": username, "exact": "true"})
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user lookup failed (HTTP {resp.status_code})")
        matches = [u for u in resp.json() if str(u.get("username", "")).lower() == username.lower()]
        return str(matches[0]["id"]) if matches else None

    async def revoke_user_grants(self, realm: str, usernames: list[str], client_ids: list[str]) -> dict[str, int]:
        """Demo reset (M5): for each user, revoke every consent **and the offline tokens** of each
        client they consented to plus ``client_ids`` (``DELETE …/users/{id}/consents/{clientId}``
        removes both; 404 = nothing granted). One admin token for the whole call.

        Returns counts ``{users, users_missing, revoked}``."""
        token = await self._token()
        users = missing = revoked = 0
        for username in usernames:
            user_id = await self._user_id(realm, username, token)
            if user_id is None:
                missing += 1
                continue
            users += 1
            resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/consents", token)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"consent listing failed (HTTP {resp.status_code})")
            clients = list(dict.fromkeys([*(str(c.get("clientId")) for c in resp.json() if c.get("clientId")),
                                          *client_ids]))
            for client_id in clients:
                path = f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/consents/{_seg(client_id)}"
                resp = await self._req("DELETE", path, token)
                if resp.status_code in (200, 204):
                    revoked += 1
                elif resp.status_code != 404:
                    raise KeycloakAdminError(f"consent revocation failed (HTTP {resp.status_code})")
        log.info("keycloak grants revoked", realm=realm, users=users, revoked=revoked)
        return {"users": users, "users_missing": missing, "revoked": revoked}

    # ───────────────────────────── M7 users API (110 §2) ─────────────────────────────

    async def search_users_by_attribute(self, realm: str, attribute: str, value: str, *,
                                        limit: int = 500) -> list[dict[str, Any]]:
        """Users whose multi-valued ``attribute`` contains ``value`` (``GET …/users?q=attr:value``,
        full representation incl. attributes); re-checked client-side (exact match)."""
        token = await self._token()
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users", token,
                               params={"q": f"{attribute}:{value}", "briefRepresentation": "false",
                                       "max": str(max(1, min(int(limit), 1000)))})
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user search failed (HTTP {resp.status_code})")
        out = []
        for u in resp.json():
            values = [str(v) for v in ((u.get("attributes") or {}).get(attribute) or [])]
            if value in values:
                out.append(u)
        return out

    async def get_user(self, realm: str, user_id: str) -> dict[str, Any] | None:
        token = await self._token()
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}", token)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user read failed (HTTP {resp.status_code})")
        return dict(resp.json())

    async def find_user(self, realm: str, username: str) -> dict[str, Any] | None:
        token = await self._token()
        uid = await self._user_id(realm, username, token)
        return await self.get_user(realm, uid) if uid else None

    async def realm_roles_of(self, realm: str, user_id: str) -> list[str]:
        """Directly assigned realm roles of a user (names)."""
        token = await self._token()
        resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/role-mappings/realm", token)
        if resp.status_code != 200:
            raise KeycloakAdminError(f"role mapping read failed (HTTP {resp.status_code})")
        return [str(r.get("name")) for r in resp.json() if r.get("name")]

    async def effective_realm_roles(self, realm: str, user_id: str) -> list[str]:
        """Effective realm roles (direct + composite + group-inherited) of a user — what its tokens
        carry, so authorization checks cannot be sidestepped by a role held through a group."""
        token = await self._token()
        resp = await self._req(
            "GET", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/role-mappings/realm/composite", token)
        if resp.status_code != 200:
            raise KeycloakAdminError(f"effective role read failed (HTTP {resp.status_code})")
        return [str(r.get("name")) for r in resp.json() if r.get("name")]

    async def create_user(self, realm: str, representation: dict[str, Any]) -> str:
        """``POST …/users`` → the new user's id (from ``Location``). 409 → :class:`KeycloakConflict`."""
        token = await self._token()
        resp = await self._req("POST", f"/admin/realms/{_seg(realm)}/users", token, json=representation)
        if resp.status_code == 409:
            raise KeycloakConflict("a user with this username or email exists")
        if resp.status_code != 201:
            raise KeycloakAdminError(f"user creation failed (HTTP {resp.status_code})")
        location = resp.headers.get("location", "")
        uid = location.rstrip("/").rsplit("/", 1)[-1] if location else ""
        if not uid:
            found = await self._user_id(realm, str(representation.get("username") or ""), token)
            if not found:
                raise KeycloakAdminError("user created but its id is unknown")
            uid = found
        return uid

    async def update_user(self, realm: str, user_id: str, changes: dict[str, Any]) -> None:
        """Read-modify-write of the user representation (``PUT …/users/{id}``)."""
        token = await self._token()
        path = f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}"
        resp = await self._req("GET", path, token)
        if resp.status_code != 200:
            raise KeycloakAdminError(f"user read failed (HTTP {resp.status_code})")
        user = {**resp.json(), **changes}
        resp = await self._req("PUT", path, token, json=user)
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(f"user update failed (HTTP {resp.status_code})")

    async def set_realm_roles(self, realm: str, user_id: str, add: list[str], remove: list[str]) -> None:
        """Add / remove directly assigned realm roles (role representations looked up by name)."""
        if not add and not remove:
            return
        token = await self._token()
        reps: dict[str, dict[str, Any]] = {}
        for name in {*add, *remove}:
            resp = await self._req("GET", f"/admin/realms/{_seg(realm)}/roles/{_seg(name)}", token)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"role {name!r} lookup failed (HTTP {resp.status_code})")
            body = resp.json()
            reps[name] = {"id": body.get("id"), "name": body.get("name")}
        path = f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/role-mappings/realm"
        if add:
            resp = await self._req("POST", path, token, json=[reps[n] for n in sorted(add)])
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(f"role grant failed (HTTP {resp.status_code})")
        if remove:
            resp = await self._req("DELETE", path, token, json=[reps[n] for n in sorted(remove)])
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(f"role revocation failed (HTTP {resp.status_code})")

    async def reset_password(self, realm: str, user_id: str, password: str, *, temporary: bool = True) -> None:
        token = await self._token()
        resp = await self._req("PUT", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/reset-password", token,
                               json={"type": "password", "value": password, "temporary": temporary})
        if resp.status_code not in (200, 204):
            raise KeycloakAdminError(f"password reset failed (HTTP {resp.status_code})")

    async def send_actions_email(self, realm: str, user_id: str, actions: list[str], *, lifespan_s: int,
                                 client_id: str | None = None, redirect_uri: str | None = None) -> bool:
        """``PUT …/execute-actions-email`` (Keycloak e-mails the user a one-time action link).
        Returns False when Keycloak could not send it (no SMTP server configured, no e-mail)."""
        token = await self._token()
        params: dict[str, str] = {"lifespan": str(int(lifespan_s))}
        if client_id:
            params["client_id"] = client_id
        if redirect_uri:
            params["redirect_uri"] = redirect_uri
        resp = await self._req("PUT", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/execute-actions-email",
                               token, params=params, json=actions)
        if resp.status_code in (200, 204):
            return True
        log.info("keycloak actions e-mail not sent", status=resp.status_code)
        return False

    async def logout_user(self, realm: str, user_id: str) -> None:
        """End the user's Keycloak sessions (``POST …/users/{id}/logout``); best effort."""
        token = await self._token()
        resp = await self._req("POST", f"/admin/realms/{_seg(realm)}/users/{_seg(user_id)}/logout", token)
        if resp.status_code not in (200, 204, 404):
            raise KeycloakAdminError(f"user logout failed (HTTP {resp.status_code})")


class KeycloakConflict(KeycloakAdminError):
    """409 from the admin API (duplicate username / e-mail)."""
