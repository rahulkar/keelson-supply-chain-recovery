#!/usr/bin/env python3
"""Smoke-verify the Keelson local infrastructure from inside the compose networks.

Stdlib only (runs in ``python:3.14-slim-trixie``). Started by ``scripts/verify-infra.ps1``
/ ``scripts/verify-infra.sh``, which attach the container to ``keelson_app`` + ``keelson_ai``
and pass ``.env`` as environment (values are never printed).

Subcommands
-----------
``all [--skip-llm]``  OIDC discovery, demo password grants + claims, Keycloak admin API
                      checks, public admin blocking, forward_auth on the admin consoles,
                      LiteLLM readiness + temporary-model smoke call, Squid egress policy.
``realms``            Realm-only subset (discovery, password grants, admin API checks,
                      built-ins); used against a throwaway Keycloak by the realm test.
``demo-disabled``     Expect the demo password grants to FAIL (DEMO_ENABLED=false).
``dns``               Resolve the public hostnames on the network(s) this container is
                      attached to and compare with the ``caddy`` service address.

Exit status is the number of failed checks (0 = all good). Output: one JSON line per check.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Overridable for the throwaway-Keycloak realm test (scripts/tests/keycloak/run.sh).
AUTH = os.environ.get("AUTH_BASE_URL", "http://auth.localhost")
KC_INTERNAL = os.environ.get("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
LITELLM = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
EGRESS = os.environ.get("CONNECTOR_PROXY_URL", "http://egress:3128")
KEELSON_JWKS = "http://keelson.localhost/api/.well-known/jwks.json"
MERXA_MCP = "http://merxa.localhost/mcp"
SMOKE_MODEL = "infra-smoke"
SMOKE_UPSTREAM = os.environ.get("SMOKE_UPSTREAM_MODEL", "gemini/gemini-3.8-flash")


# ── tiny HTTP helper ───────────────────────────────────────────────────────────


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int

    def json(self) -> Any:
        return json.loads(self.body or b"null")


def http(
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
) -> Resp:
    """Perform a request; never raises for HTTP error statuses."""
    hdrs = dict(headers or {})
    payload: bytes | None = None
    if data is not None:
        payload = urllib.parse.urlencode(data).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        payload = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=payload, method=method, headers=hdrs)
    start = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
            status, rh = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        body, status, rh = e.read(), e.code, dict(e.headers)
    return Resp(status, {k.lower(): v for k, v in rh.items()}, body, int((time.perf_counter() - start) * 1000))


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode (without verifying) the payload of a compact JWS."""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# ── check runner ───────────────────────────────────────────────────────────────


@dataclass
class Runner:
    failures: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def check(self, name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        try:
            detail = fn()
            ok = bool(detail.pop("ok", True))
        except Exception as exc:  # noqa: BLE001 - report any failure as a failed check
            ok, detail = False, {"error": f"{type(exc).__name__}: {exc}"}
        self.failures += 0 if ok else 1
        line = {"check": name, "ok": ok, **detail}
        self.results.append(line)
        print(json.dumps(line), flush=True)
        return detail if ok else None


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"env {name} is not set (run with --env-file .env)")
    return value


# ── checks ─────────────────────────────────────────────────────────────────────


def check_discovery(realm: str) -> dict[str, Any]:
    r = http("GET", f"{AUTH}/realms/{realm}/.well-known/openid-configuration")
    d = r.json()
    expected = f"{AUTH}/realms/{realm}"
    return {
        "ok": r.status == 200 and d.get("issuer") == expected,
        "status": r.status,
        "issuer": d.get("issuer"),
        "ms": r.elapsed_ms,
    }


def password_grant(realm: str, client_id: str, secret_env: str, username: str) -> dict[str, Any]:
    r = http(
        "POST",
        f"{AUTH}/realms/{realm}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": env(secret_env),
            "username": username,
            "password": env("DEMO_USER_PASSWORD"),
            "scope": "openid",
        },
    )
    if r.status != 200:
        raise RuntimeError(f"token endpoint HTTP {r.status}: {r.body[:300]!r}")
    return jwt_claims(r.json()["access_token"])


def check_keelson_demo_priya() -> dict[str, Any]:
    c = password_grant("keelson", "keelson-demo", "KEELSON_DEMO_CLIENT_SECRET", "priya")
    ok = (
        c.get("roles") == ["planner"]
        and c.get("tenants") == ["demo-retail"]
        and c.get("demo") is True
        and c.get("iss") == f"{AUTH}/realms/keelson"
        and c.get("azp") == "keelson-demo"
        and bool(c.get("sub"))
    )
    return {
        "ok": ok,
        "iss": c.get("iss"),
        "azp": c.get("azp"),
        "preferred_username": c.get("preferred_username"),
        "name": c.get("name"),
        "roles": c.get("roles"),
        "tenants": c.get("tenants"),
        "demo": c.get("demo"),
        "has_sub": bool(c.get("sub")),
        "exp_minus_iat": c.get("exp", 0) - c.get("iat", 0),
    }


def check_merxa_demo_marco() -> dict[str, Any]:
    c = password_grant("merxa", "merxa-demo", "MERXA_DEMO_CLIENT_SECRET", "marco.rossi")
    roles = c.get("roles") or []
    realm_roles = (c.get("realm_access") or {}).get("roles") or []
    return {
        "ok": "ops-director" in roles and "ops-director" in realm_roles and c.get("iss") == f"{AUTH}/realms/merxa",
        "iss": c.get("iss"),
        "preferred_username": c.get("preferred_username"),
        "name": c.get("name"),
        "roles": roles,
    }


def check_grant_refused(realm: str, client_id: str, secret_env: str, username: str) -> dict[str, Any]:
    """With DEMO_ENABLED=false the demo client and personas are disabled: no token."""
    try:
        password_grant(realm, client_id, secret_env, username)
    except RuntimeError as exc:
        return {"ok": True, "refused": str(exc)[:80]}
    return {"ok": False, "refused": False}


def kc_admin_token() -> str:
    r = http(
        "POST",
        f"{KC_INTERNAL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": env("KC_ADMIN_PASSWORD"),
        },
    )
    if r.status != 200:
        raise RuntimeError(f"admin token HTTP {r.status}")
    return str(r.json()["access_token"])


def kc_get(token: str, path: str) -> Any:
    r = http("GET", f"{KC_INTERNAL}/admin/realms/{path}", headers={"Authorization": f"Bearer {token}"})
    if r.status != 200:
        raise RuntimeError(f"GET {path} -> HTTP {r.status}")
    return r.json()


def check_merxa_jwt_clients(token: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ok = True
    expected_scopes = {
        "keelson-reader": {"catalog:read", "inventory:read", "po:read", "orders:read", "snapshot:read"},
        "keelson-preparer": {"changes:preview", "catalog:read", "inventory:read", "po:read", "orders:read", "snapshot:read"},
        "keelson-autocommit": {"changes:apply:auto"},
        "keelson-connect": set(),
    }
    for cid, scopes in expected_scopes.items():
        (client,) = kc_get(token, f"merxa/clients?clientId={cid}")
        attrs = client.get("attributes", {})
        defaults = {s["name"] for s in kc_get(token, f"merxa/clients/{client['id']}/default-client-scopes")}
        optional = {s["name"] for s in kc_get(token, f"merxa/clients/{client['id']}/optional-client-scopes")}
        info: dict[str, Any] = {
            "authenticator": client.get("clientAuthenticatorType"),
            "use.jwks.url": attrs.get("use.jwks.url"),
            "jwks.url": attrs.get("jwks.url"),
            "alg": attrs.get("token.endpoint.auth.signing.alg"),
        }
        c_ok = (
            info["authenticator"] == "client-jwt"
            and info["use.jwks.url"] == "true"
            and info["jwks.url"] == KEELSON_JWKS
            and info["alg"] == "ES256"
            and scopes <= defaults
        )
        if cid == "keelson-connect":
            info["pkce"] = attrs.get("pkce.code.challenge.method")
            info["optional"] = sorted(optional)
            c_ok = c_ok and info["pkce"] == "S256" and {"changes:apply", "offline_access"} <= optional
        else:
            sa = kc_get(token, f"merxa/clients/{client['id']}/service-account-user")
            sa_roles = {r["name"] for r in kc_get(token, f"merxa/users/{sa['id']}/role-mappings/realm")}
            info["sa_has_integration_writer"] = "integration-writer" in sa_roles
            # Example token as the service account: audience + scope from the client scopes.
            ex = kc_get(
                token,
                f"merxa/clients/{client['id']}/evaluate-scopes/generate-example-access-token"
                f"?scope=openid&userId={sa['id']}",
            )
            aud = ex.get("aud")
            aud_list = aud if isinstance(aud, list) else [aud]
            info["aud_has_mcp"] = MERXA_MCP in aud_list
            info["scope"] = ex.get("scope")
            c_ok = c_ok and info["sa_has_integration_writer"] and info["aud_has_mcp"]
        info["default_scopes"] = sorted(defaults)
        ok = ok and c_ok
        out[cid] = info
    return {"ok": ok, **out}


def check_builtins_preserved(token: str) -> dict[str, Any]:
    builtin_clients = {"account", "account-console", "admin-cli", "broker", "realm-management", "security-admin-console"}
    builtin_scopes = {"basic", "profile", "email", "roles", "web-origins", "acr", "offline_access", "address", "phone"}
    missing: dict[str, list[str]] = {}
    for realm in ("keelson", "merxa"):
        clients = {c["clientId"] for c in kc_get(token, f"{realm}/clients?max=200")}
        scopes = {s["name"] for s in kc_get(token, f"{realm}/client-scopes")}
        miss = sorted((builtin_clients - clients) | (builtin_scopes - scopes))
        if miss:
            missing[realm] = miss
    return {"ok": not missing, "missing": missing}


def check_public_admin_blocked() -> dict[str, Any]:
    admin = http("GET", f"{AUTH}/admin/master/console/")
    master = http("GET", f"{AUTH}/realms/master/.well-known/openid-configuration")
    return {"ok": admin.status == 403 and master.status == 403, "admin": admin.status, "master_realm": master.status}


def check_admin_consoles_guarded() -> dict[str, Any]:
    """gateway./traces.localhost sit behind forward_auth: never open without a session.

    Sent to Caddy by service name with an explicit Host header, so the check works from
    networks where those hostnames have no alias. Expected: 401/403 from engine-api, or
    502 while engine-api is not running (fail closed).
    """
    out: dict[str, Any] = {}
    for host in ("gateway.localhost", "traces.localhost"):
        r = http("GET", "http://caddy/", headers={"Host": host})
        out[host] = r.status
    return {"ok": all(v in {401, 403, 502} for v in out.values()), **out}


def check_litellm_ready() -> dict[str, Any]:
    r = http("GET", f"{LITELLM}/health/readiness")
    d = r.json()
    return {"ok": r.status == 200, "status": r.status, "db": d.get("db"), "litellm_version": d.get("litellm_version")}


def check_litellm_smoke() -> dict[str, Any]:
    """Create a temporary model, make one tiny call through the proxy, delete the model."""
    auth = {"Authorization": f"Bearer {env('LITELLM_MASTER_KEY')}"}
    created = http(
        "POST",
        f"{LITELLM}/model/new",
        headers=auth,
        json_body={
            "model_name": SMOKE_MODEL,
            "litellm_params": {"model": SMOKE_UPSTREAM, "api_key": "os.environ/GEMINI_API_KEY"},
            "model_info": {"description": "temporary infra smoke test model"},
        },
    )
    if created.status != 200:
        raise RuntimeError(f"/model/new HTTP {created.status}: {created.body[:300]!r}")
    model_id = created.json().get("model_id") or created.json().get("model_info", {}).get("id")
    result: dict[str, Any] = {"upstream": SMOKE_UPSTREAM, "model_id_present": bool(model_id)}
    try:
        chat = http(
            "POST",
            f"{LITELLM}/chat/completions",
            headers=auth,
            json_body={
                "model": SMOKE_MODEL,
                "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                "max_tokens": 512,
                "reasoning_effort": "low",
            },
            timeout=120,
        )
        result["chat_status"] = chat.status
        result["chat_ms"] = chat.elapsed_ms
        if chat.status == 200:
            body = chat.json()
            result["reply"] = (body["choices"][0]["message"].get("content") or "")[:40]
            result["usage"] = body.get("usage", {}).get("total_tokens")
            result["served_model"] = body.get("model")
        else:
            result["error"] = chat.body[:300].decode(errors="replace")
    finally:
        if model_id:
            deleted = http("POST", f"{LITELLM}/model/delete", headers=auth, json_body={"id": model_id})
            result["delete_status"] = deleted.status
    listed = http("GET", f"{LITELLM}/v1/models", headers=auth).json()
    result["model_gone"] = SMOKE_MODEL not in {m.get("id") for m in listed.get("data", [])}
    result["ok"] = result.get("chat_status") == 200 and result.get("delete_status") == 200 and result["model_gone"]
    return result


def _via_proxy(url: str) -> Resp:
    return http("GET", url, proxy=EGRESS, timeout=30)


def check_egress() -> dict[str, Any]:
    def denied(r: Resp) -> bool:
        return r.status == 403 and "ERR_ACCESS_DENIED" in r.headers.get("x-squid-error", "")

    merxa = _via_proxy("http://merxa.localhost/")
    merxa_other_port = _via_proxy("http://merxa.localhost:443/")  # dev exception is port 80 only
    # M8 adapter sidecar demo (profile adapters): Caddy answers (502 while the profile is off)
    fernhill = _via_proxy("http://fernhill.localhost/healthz")
    fernhill_other_port = _via_proxy("http://fernhill.localhost:443/")
    fernhill_direct = _via_proxy("http://fernhill-sidecar:8400/healthz")  # service names stay denied
    merxa_as = _via_proxy(f"{AUTH}/realms/merxa/.well-known/openid-configuration")
    keelson_as = _via_proxy(f"{AUTH}/realms/keelson/.well-known/openid-configuration")
    pg = _via_proxy("http://pg-core:5432/")
    kc = _via_proxy("http://keycloak:8080/")
    meta = _via_proxy("http://169.254.169.254/latest/meta-data/")
    loop = _via_proxy("http://127.0.0.1/")
    public = _via_proxy("https://example.com/")
    details = {
        "merxa.localhost": [merxa.status, merxa.headers.get("x-squid-error", "")],
        "merxa.localhost:443": [merxa_other_port.status, merxa_other_port.headers.get("x-squid-error", "")],
        "fernhill.localhost": [fernhill.status, fernhill.headers.get("x-squid-error", "")],
        "fernhill.localhost:443": [fernhill_other_port.status, fernhill_other_port.headers.get("x-squid-error", "")],
        "fernhill-sidecar:8400": [fernhill_direct.status, fernhill_direct.headers.get("x-squid-error", "")],
        "auth.localhost/realms/merxa": merxa_as.status,
        "auth.localhost/realms/keelson": [keelson_as.status, keelson_as.headers.get("x-squid-error", "")],
        "pg-core:5432": [pg.status, pg.headers.get("x-squid-error", "")],
        "keycloak:8080": [kc.status, kc.headers.get("x-squid-error", "")],
        "169.254.169.254": [meta.status, meta.headers.get("x-squid-error", "")],
        "127.0.0.1": [loop.status, loop.headers.get("x-squid-error", "")],
        "https://example.com": [public.status, public.elapsed_ms],
    }
    ok = (
        "x-squid-error" not in merxa.headers  # answered by Caddy (any status)
        and denied(merxa_other_port)
        and "x-squid-error" not in fernhill.headers  # answered by Caddy (any status)
        and denied(fernhill_other_port)
        and denied(fernhill_direct)
        and merxa_as.status == 200
        and denied(keelson_as)
        and denied(pg)
        and denied(kc)
        and denied(meta)
        and denied(loop)
        and public.status == 200
    )
    return {"ok": ok, **details}


def check_dns() -> dict[str, Any]:
    def addrs(host: str) -> list[str]:
        try:
            return sorted({a[4][0] for a in socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)})
        except socket.gaierror as exc:
            return [f"unresolved ({exc.errno})"]

    caddy = set(addrs("caddy"))
    hosts = ["auth.localhost", "keelson.localhost", "merxa.localhost", "gateway.localhost", "traces.localhost"]
    resolved = {h: addrs(h) for h in hosts}
    to_caddy = {h: bool(set(v) & caddy) for h, v in resolved.items()}
    return {
        "ok": to_caddy["auth.localhost"] and to_caddy["keelson.localhost"],
        "network": os.environ.get("VERIFY_NETWORK", "?"),
        "caddy": sorted(caddy),
        "resolves_to_caddy": to_caddy,
    }


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "all"
    run = Runner()
    if mode == "dns":
        run.check("dns", check_dns)
        return run.failures
    if mode == "demo-disabled":
        run.check(
            "demo_disabled_keelson",
            lambda: check_grant_refused("keelson", "keelson-demo", "KEELSON_DEMO_CLIENT_SECRET", "priya"),
        )
        run.check(
            "demo_disabled_merxa",
            lambda: check_grant_refused("merxa", "merxa-demo", "MERXA_DEMO_CLIENT_SECRET", "marco.rossi"),
        )
        return run.failures
    run.check("oidc_discovery_keelson", lambda: check_discovery("keelson"))
    run.check("oidc_discovery_merxa", lambda: check_discovery("merxa"))
    run.check("password_grant_keelson_demo_priya", check_keelson_demo_priya)
    run.check("password_grant_merxa_demo_marco", check_merxa_demo_marco)
    token_holder: dict[str, str] = {}

    def _admin_token() -> dict[str, Any]:
        token_holder["t"] = kc_admin_token()
        return {}

    if run.check("kc_admin_api_login_internal", _admin_token) is not None:
        run.check("merxa_private_key_jwt_clients", lambda: check_merxa_jwt_clients(token_holder["t"]))
        run.check("kc_builtins_preserved", lambda: check_builtins_preserved(token_holder["t"]))
    if mode == "realms":
        print(json.dumps({"summary": {"checks": len(run.results), "failed": run.failures}}))
        return run.failures
    run.check("kc_admin_blocked_publicly", check_public_admin_blocked)
    run.check("admin_consoles_forward_auth", check_admin_consoles_guarded)
    run.check("litellm_readiness", check_litellm_ready)
    if "--skip-llm" not in argv:
        run.check("litellm_temp_model_smoke", check_litellm_smoke)
    run.check("egress_policy", check_egress)
    print(json.dumps({"summary": {"checks": len(run.results), "failed": run.failures}}))
    return run.failures


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
