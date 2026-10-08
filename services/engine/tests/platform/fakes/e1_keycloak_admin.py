"""In-memory Keycloak admin REST API (respx): master-realm admin token + users of one realm."""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any
from urllib.parse import parse_qs

import httpx
import respx


class FakeKeycloakAdmin:
    def __init__(self, base: str, password: str, realm: str = "keelson") -> None:
        self.base = base.rstrip("/")
        self.password = password
        self.realm = realm
        self.users: dict[str, dict[str, Any]] = {}
        self.tokens: set[str] = set()
        self.puts: list[dict[str, Any]] = []
        self.down = False
        self.consents: dict[str, set[str]] = {}
        """user id → client ids the user consented to (M5 demo reset)."""
        self.offline: dict[str, set[str]] = {}
        """user id → client ids holding an offline session for the user."""
        self.revoked: list[tuple[str, str]] = []

    def add_user(self, username: str, **attributes: list[str]) -> str:
        uid = str(uuid.uuid4())
        self.users[uid] = {"id": uid, "username": username, "enabled": True, "firstName": username.title(),
                           "attributes": {k: list(v) for k, v in attributes.items()}}
        return uid

    def by_username(self, username: str) -> dict[str, Any]:
        return next(u for u in self.users.values() if u["username"] == username)

    def install(self, router: respx.Router) -> None:
        b = self.base
        router.post(f"{b}/realms/master/protocol/openid-connect/token", name="kc-admin-token").mock(
            side_effect=self._token)
        router.get(url__regex=rf"^{b}/admin/realms/[^/]+/users(\?.*)?$", name="kc-admin-users").mock(
            side_effect=self._users)
        router.route(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+$", name="kc-admin-user").mock(
            side_effect=self._user)
        router.get(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/consents$", name="kc-admin-consents").mock(
            side_effect=self._consents)
        router.delete(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/consents/[^/?]+$",
                      name="kc-admin-consent").mock(side_effect=self._revoke)

    def _user_of(self, req: httpx.Request) -> str | None:
        parts = req.url.path.split("/")
        if parts[3] != self.realm:
            return None
        uid = parts[5]
        return uid if uid in self.users else None

    def _consents(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = self._user_of(req)
        if uid is None:
            return httpx.Response(404)
        return httpx.Response(200, json=[{"clientId": c, "grantedClientScopes": ["changes:apply"]}
                                         for c in sorted(self.consents.get(uid, set()))])

    def _revoke(self, req: httpx.Request) -> httpx.Response:
        """Keycloak: revoke the consent AND the offline tokens of that client; 404 when neither."""
        if not self._authorized(req):
            return httpx.Response(401)
        uid = self._user_of(req)
        if uid is None:
            return httpx.Response(404)
        client = req.url.path.rsplit("/", 1)[1]
        had = client in self.consents.get(uid, set()) or client in self.offline.get(uid, set())
        self.consents.get(uid, set()).discard(client)
        self.offline.get(uid, set()).discard(client)
        if not had:
            return httpx.Response(404, json={"error": "Consent nor offline token not found"})
        self.revoked.append((self.users[uid]["username"], client))
        return httpx.Response(204)

    def _authorized(self, req: httpx.Request) -> bool:
        auth = req.headers.get("authorization", "")
        return auth.startswith("Bearer ") and auth[7:] in self.tokens

    def _token(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if form.get("client_id") != "admin-cli" or form.get("username") != "admin" or \
                form.get("password") != self.password:
            return httpx.Response(401, json={"error": "invalid_grant"})
        token = secrets.token_urlsafe(16)
        self.tokens.add(token)
        return httpx.Response(200, json={"access_token": token, "expires_in": 60})

    def _users(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        if req.url.path.split("/")[3] != self.realm:
            return httpx.Response(404)
        username = req.url.params.get("username", "")
        exact = req.url.params.get("exact") == "true"
        found = [
            {k: v for k, v in u.items() if k != "attributes"} for u in self.users.values()
            if (u["username"] == username if exact else username in u["username"])
        ]
        return httpx.Response(200, json=found)

    def _user(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = req.url.path.rsplit("/", 1)[1]
        if uid not in self.users:
            return httpx.Response(404)
        if req.method == "GET":
            return httpx.Response(200, json=json.loads(json.dumps(self.users[uid])))
        if req.method == "PUT":
            body = json.loads(req.content)
            self.puts.append(body)
            self.users[uid]["attributes"] = body.get("attributes") or {}
            return httpx.Response(204)
        return httpx.Response(405)


# ───────────────────────────── M7 users API (110 §2) ─────────────────────────────

REALM_ROLES = ("planner", "approver", "compliance", "tenant-admin", "platform-admin", "offline_access",
               "default-roles-keelson", "uma_authorization")


class FakeKeycloakUsers(FakeKeycloakAdmin):
    """:class:`FakeKeycloakAdmin` + the endpoints of the M7 users API: attribute search
    (``q=tenant:x``), create (``201`` + ``Location``; ``409`` duplicate), realm role lookup and
    role mappings, ``reset-password``, ``execute-actions-email`` (``smtp`` off → 500 like Keycloak
    without a mail server) and ``logout``. Credentials are kept (to assert they are temporary) but
    never returned."""

    def __init__(self, base: str, password: str, realm: str = "keelson") -> None:
        super().__init__(base, password, realm)
        self.roles: dict[str, set[str]] = {}
        self.credentials: dict[str, list[dict[str, Any]]] = {}
        self.smtp = False
        self.emails: list[tuple[str, list[str], dict[str, str]]] = []
        self.logouts: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.group_roles: dict[str, set[str]] = {}
        """Roles a user holds through a group (effective, never directly assigned)."""

    def add_user(self, username: str, **attributes: list[str]) -> str:
        roles = attributes.pop("roles", [])
        uid = super().add_user(username, **attributes)
        self.users[uid].update({"email": f"{username}@example.test", "createdTimestamp": 1_760_000_000_000,
                                "lastName": "Test"})
        self.roles[uid] = {*roles, "default-roles-keelson", "offline_access"}
        return uid

    def install(self, router: respx.Router) -> None:
        super().install(router)
        b = self.base
        router.post(url__regex=rf"^{b}/admin/realms/[^/]+/users$", name="kc-admin-users-create").mock(
            side_effect=self._create)
        router.get(url__regex=rf"^{b}/admin/realms/[^/]+/roles/[^/?]+$", name="kc-admin-role").mock(
            side_effect=self._role)
        router.route(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/role-mappings/realm$",
                     name="kc-admin-role-mappings").mock(side_effect=self._mappings)
        router.get(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/role-mappings/realm/composite$",
                   name="kc-admin-role-mappings-composite").mock(side_effect=self._effective)
        router.put(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/reset-password$",
                   name="kc-admin-reset-password").mock(side_effect=self._reset)
        router.put(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/execute-actions-email(\?.*)?$",
                   name="kc-admin-actions-email").mock(side_effect=self._email)
        router.post(url__regex=rf"^{b}/admin/realms/[^/]+/users/[^/?]+/logout$", name="kc-admin-logout").mock(
            side_effect=self._logout)

    def _users(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params.get("q")
        if q is None:
            return super()._users(req)
        if not self._authorized(req):
            return httpx.Response(401)
        attr, _, value = q.partition(":")
        brief = req.url.params.get("briefRepresentation", "true") != "false"
        found = [u for u in self.users.values() if value in (u.get("attributes") or {}).get(attr, [])]
        return httpx.Response(200, json=[{k: v for k, v in u.items() if not (brief and k == "attributes")}
                                         for u in found])

    def _create(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        body = json.loads(req.content)
        if any(u["username"] == body["username"] or (body.get("email") and u.get("email") == body.get("email"))
               for u in self.users.values()):
            return httpx.Response(409, json={"errorMessage": "User exists with same username"})
        uid = str(uuid.uuid4())
        self.credentials[uid] = list(body.pop("credentials", []) or [])
        self.users[uid] = {"id": uid, "createdTimestamp": 1_760_000_000_000, **body}
        self.roles[uid] = {"default-roles-keelson", "offline_access"}
        self.created.append({"id": uid, **body})
        return httpx.Response(201, headers={"Location": f"{self.base}/admin/realms/{self.realm}/users/{uid}"})

    def _role(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        name = req.url.path.rsplit("/", 1)[1]
        if name not in REALM_ROLES:
            return httpx.Response(404)
        return httpx.Response(200, json={"id": f"role-{name}", "name": name, "composite": False})

    def _mappings(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = req.url.path.split("/")[5]
        if uid not in self.users:
            return httpx.Response(404)
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": f"role-{r}", "name": r} for r in sorted(self.roles[uid])])
        reps = json.loads(req.content)
        for rep in reps:
            if rep.get("name") not in REALM_ROLES or rep.get("id") != f"role-{rep.get('name')}":
                return httpx.Response(404)
        names = {r["name"] for r in reps}
        if req.method == "POST":
            self.roles[uid] |= names
        elif req.method == "DELETE":
            self.roles[uid] -= names
        else:
            return httpx.Response(405)
        return httpx.Response(204)

    def _effective(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = req.url.path.split("/")[5]
        if uid not in self.users:
            return httpx.Response(404)
        names = sorted(self.roles[uid] | self.group_roles.get(uid, set()))
        return httpx.Response(200, json=[{"id": f"role-{r}", "name": r} for r in names])

    def _reset(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = req.url.path.split("/")[5]
        self.credentials[uid] = [json.loads(req.content)]
        return httpx.Response(204)

    def _email(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        uid = req.url.path.split("/")[5]
        if not self.smtp or not self.users.get(uid, {}).get("email"):
            return httpx.Response(500, json={"errorMessage": "Failed to send execute actions email"})
        self.emails.append((uid, json.loads(req.content), dict(req.url.params)))
        return httpx.Response(204)

    def _logout(self, req: httpx.Request) -> httpx.Response:
        if not self._authorized(req):
            return httpx.Response(401)
        self.logouts.append(req.url.path.split("/")[5])
        return httpx.Response(204)

    def _user(self, req: httpx.Request) -> httpx.Response:
        if req.method == "PUT" and self._authorized(req):
            uid = req.url.path.rsplit("/", 1)[1]
            if uid in self.users:
                body = json.loads(req.content)
                self.puts.append(body)
                self.users[uid].update({k: v for k, v in body.items() if k not in ("id", "credentials")})
                return httpx.Response(204)
        return super()._user(req)
