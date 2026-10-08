"""Connector registry administration (``docs/contracts/70-backoffice-m2.md`` §3).

* :func:`check_url` — URL policy: ``http(s)`` only, no userinfo; the host must be one of
  ``CONNECTOR_DEV_HOSTS`` or resolve **only** to public addresses. All traffic then goes through
  the egress proxy (which enforces the same rule on the resolved destination).
* :func:`create_connector` — ``draft`` row; secrets AES-GCM encrypted (``secret_enc``).
* :func:`test_connection` — step log ``url_policy → reach → discover → auth → tools_list →
  tier_detect → classify``; a passing test moves ``draft`` → ``tested``.
* :func:`update_tool` — admin classification/review (guarded: making a tool ``read`` or
  ``trusted_data`` against its current/suggested classification needs platform-admin).
* :func:`set_status` / :func:`delete_connector` — lifecycle.

Raises :class:`AdminError` (``status``, ``code``, ``message``) for every refusal; the API maps it
1:1 to ``{code, message}``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text

from keelson_contracts.connectors import ToolClassification, detect_tiers
from keelson_engine.config import get_settings
from keelson_engine.core import connectors as reg
from keelson_engine.core.crypto import encrypt_str
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "BINDINGS",
    "AdminError",
    "check_url",
    "connector_admin_json",
    "create_connector",
    "delete_connector",
    "guard_base",
    "list_events",
    "set_binding",
    "set_status",
    "set_trust",
    "test_connection",
    "update_tool",
]

log = get_logger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
WEBHOOK_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}_WEBHOOK_SECRET$")
FORBIDDEN_HEADERS = frozenset({
    "host", "cookie", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
    "proxy-authorization", "mcp-session-id", "mcp-protocol-version", "accept", "te", "trailer",
})
SECRET_MAX = 4096
HEADER_VALUE_RE = re.compile(r"^[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?$")
"""A static header value: visible ASCII (inner spaces allowed). Control characters (CR/LF/NUL …)
would be header injection or make every request fail inside the HTTP client; non-ASCII cannot be
sent as a header value at all."""


class AdminError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# ───────────────────────────── URL policy ─────────────────────────────

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5.0)
    return sorted({str(info[4][0]) for info in infos})


_resolver: Resolver = _system_resolver


def set_resolver(resolver: Resolver | None) -> None:
    """Tests: replace DNS resolution of :func:`check_url`."""
    global _resolver
    _resolver = resolver or _system_resolver


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not addr.is_multicast and not addr.is_reserved


async def check_url(url: str) -> str:
    """Validate a connector / token URL; returns a short detail or raises ``AdminError(422,
    "url_policy")``."""
    if not isinstance(url, str) or not url or len(url) > 2048 or any(c.isspace() for c in url):
        raise AdminError(422, "url_policy", "the URL is empty, too long or contains spaces")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AdminError(422, "url_policy", "only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise AdminError(422, "url_policy", "credentials in the URL are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise AdminError(422, "url_policy", "the URL has no host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise AdminError(422, "url_policy", "the URL has an invalid port") from exc
    if host in get_settings().dev_hosts:
        return f"{host} is an allowed development host"
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await _resolver(host, port)
        except (OSError, TimeoutError) as exc:
            raise AdminError(422, "url_policy", f"{host} does not resolve") from exc
    if not addresses:
        raise AdminError(422, "url_policy", f"{host} does not resolve")
    private = [a for a in addresses if not _public(a)]
    if private:
        raise AdminError(422, "url_policy", f"{host} resolves to a non-public address")
    return f"{host} resolves to public address(es) only"


# ───────────────────────────── create ─────────────────────────────


def _clean_auth(auth: dict[str, Any], *, mcp_url: str) -> tuple[str, dict[str, Any], str | None]:
    """→ ``(kind, non-secret auth_config, secret)``; raises 422 on invalid input."""
    kind = str(auth.get("kind") or "none")
    if kind in ("oauth_platform_merxa", reg.INTERNAL_AUTH_KIND):
        raise AdminError(422, "invalid_auth", "platform connectors are provisioned by Keelson, not registered")
    if kind not in reg.AUTH_KINDS:
        raise AdminError(422, "invalid_auth", f"unknown auth kind {kind!r}")
    if kind == "none":
        return kind, {}, None
    if kind == "static_header":
        header = str(auth.get("header_name") or "")
        if not HEADER_RE.match(header) or header.lower() in FORBIDDEN_HEADERS:
            raise AdminError(422, "invalid_auth", "header_name must be a valid, non-reserved HTTP header name")
        secret = auth.get("secret")
        if not isinstance(secret, str) or not secret.strip() or len(secret) > SECRET_MAX:
            raise AdminError(422, "invalid_auth", "secret (the header value) is required")
        if not HEADER_VALUE_RE.match(secret.strip()):
            raise AdminError(422, "invalid_auth", "the header value may only contain visible ASCII characters")
        return kind, {"header_name": header}, secret.strip()
    # oauth_client_credentials
    token_url = str(auth.get("token_url") or "")
    client_id = str(auth.get("client_id") or "")
    if not token_url or not client_id or len(client_id) > 256:
        raise AdminError(422, "invalid_auth", "token_url and client_id are required")
    secret = auth.get("client_secret")
    client_auth = str(auth.get("client_auth") or ("client_secret" if secret else "private_key_jwt"))
    if client_auth not in ("client_secret", "private_key_jwt"):
        raise AdminError(422, "invalid_auth", "client_auth must be client_secret or private_key_jwt")
    if client_auth == "client_secret":
        if not isinstance(secret, str) or not secret or len(secret) > SECRET_MAX:
            raise AdminError(422, "invalid_auth", "client_secret is required for client_secret authentication")
    else:
        secret = None
    scopes = auth.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or not all(isinstance(s, str) and s and " " not in s for s in scopes):
        raise AdminError(422, "invalid_auth", "scopes must be a list of scope names")
    resource = auth.get("resource")
    if resource is not None and (not isinstance(resource, str) or len(resource) > 2048):
        raise AdminError(422, "invalid_auth", "resource must be a URL")
    cfg: dict[str, Any] = {"token_url": token_url, "client_id": client_id, "client_auth": client_auth,
                           "scopes": scopes}
    if resource:
        cfg["resource"] = resource
    if client_auth == "private_key_jwt" and auth.get("audience"):
        audience = str(auth["audience"])
        if len(audience) > 2048 or not reg.same_origin(audience, token_url):
            # the assertion is only ever sent to token_url: an audience of another origin would make
            # Keelson mint a credential that is valid at a *different* IdP (confused deputy)
            raise AdminError(422, "invalid_auth", "audience must be on the same origin as token_url")
        cfg["audience"] = audience
    return kind, cfg, secret


async def create_connector(tenant: str, body: dict[str, Any], actor: dict[str, Any], *,
                           platform_admin: bool) -> dict[str, Any]:
    slug = str(body.get("slug") or "")
    if not SLUG_RE.match(slug):
        raise AdminError(422, "invalid_slug", "slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if slug == get_settings().signal_hub_connector_slug:
        # reserved for the internal Signal Hub connector (bootstrap): a look-alike registered first
        # would otherwise keep its own tool classifications once bootstrap adopts the slug
        raise AdminError(422, "reserved_slug", f"the slug {slug!r} is reserved for an internal Keelson service")
    name = str(body.get("name") or "").strip() or slug
    mcp_url = str(body.get("mcp_url") or "")
    trust = str(body.get("trust_level") or "third_party")
    if trust not in ("first_party", "third_party"):
        raise AdminError(422, "invalid_trust_level", "trust_level must be first_party or third_party")
    trust_requested = trust
    if trust == "first_party" and not platform_admin:
        # M7 four-eyes: registered as third_party; raising the trust level is a change request
        trust = "third_party"
    webhook_ref = body.get("webhook_secret_ref")
    if webhook_ref is not None:
        if not isinstance(webhook_ref, str) or not WEBHOOK_REF_RE.match(webhook_ref):
            raise AdminError(422, "invalid_webhook_secret_ref", "webhook_secret_ref must name a *_WEBHOOK_SECRET variable")
        if not platform_admin:
            raise AdminError(403, "guarded_change", "only a platform admin may bind a webhook secret")
    await check_url(mcp_url)
    kind, auth_config, secret = _clean_auth(dict(body.get("auth") or {}), mcp_url=mcp_url)
    if auth_config.get("client_auth") == "private_key_jwt" and not platform_admin:
        # private_key_jwt signs with Keelson's own platform key (ca-1, the key Merxa trusts for the
        # keelson-* clients): only a platform admin may point that identity at an IdP
        raise AdminError(403, "guarded_change", "private_key_jwt client authentication requires a platform admin")
    if kind == "oauth_client_credentials":
        await check_url(auth_config["token_url"])
    enc = encrypt_str(secret, reg.secret_aad(tenant, slug)) if secret else None
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                """
                INSERT INTO connector (tenant, slug, name, mcp_url, issuer, trust_level, auth, lanes, config, status,
                                       auth_kind, auth_config, secret_enc, webhook_secret_ref, created_by)
                VALUES (:t, :slug, :name, :url, '', :trust, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, 'draft',
                        :kind, CAST(:cfg AS jsonb), :enc, :wh, :by)
                ON CONFLICT (tenant, slug) DO NOTHING
                RETURNING slug
                """
            ),
            {"t": tenant, "slug": slug, "name": name[:200], "url": mcp_url, "trust": trust, "kind": kind,
             "cfg": dumps(auth_config), "enc": enc, "wh": webhook_ref, "by": actor.get("username")},
        )
        if res.first() is None:
            raise AdminError(409, "connector_exists", f"a connector named {slug!r} already exists")
    reg.clear_token_cache(tenant, slug)
    await admin_audit(actor, "connector.create", f"connector:{slug}", None,
                      {"name": name, "mcp_url": mcp_url, "trust_level": trust, "auth_kind": kind,
                       "auth_config": auth_config, "has_secret": bool(secret), "webhook_secret_ref": webhook_ref},
                      tenant=tenant)
    out: dict[str, Any] = {"slug": slug, "status": "draft", "trust_level": trust}
    if trust_requested != trust:
        from keelson_engine.core.change_requests import create_request

        out["change_request"] = await create_request(_trust_guard(tenant, slug, trust, trust_requested, actor,
                                                                  await guard_base(tenant, slug)))
    return out


# ───────────────────────────── views ─────────────────────────────


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


async def _tool_counts(tenant: str) -> dict[str, dict[str, int]]:
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT connector, status, count(*) FROM connector_tool GROUP BY 1, 2"))
        out: dict[str, dict[str, int]] = {}
        for conn, status, n in res.all():
            out.setdefault(conn, {"approved": 0, "needs_review": 0, "disabled": 0})
            if status in out[conn]:
                out[conn][status] = int(n)
    return out


def connector_admin_json(conn: reg.ConnectorRow, counts: dict[str, int] | None = None, *,
                         detail: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slug": conn.slug,
        "name": conn.name,
        "url": conn.url,
        "auth_kind": conn.auth_kind,
        "tier": conn.tiers,
        "status": conn.status,
        "trust_level": conn.trust,
        "tools": counts or {"approved": 0, "needs_review": 0, "disabled": 0},
        "last_test_at": _iso(conn.last_test_at),
        "last_test_ok": (conn.last_test or {}).get("ok") if conn.last_test else None,
        "webhook": bool(conn.webhook_secret_ref),
    }
    if detail:
        cfg = dict(conn.auth_config)
        out.update({
            "auth": {"kind": conn.auth_kind, **cfg, "has_secret": conn.has_secret},
            "last_test": conn.last_test,
            "webhook_secret_ref": conn.webhook_secret_ref,
            "webhook_path": f"/api/webhooks/{conn.tenant}/{conn.slug}" if conn.webhook_secret_ref else None,
            "created_by": conn.created_by,
            "lanes": conn.lanes,
        })
    return out


async def list_admin(tenant: str) -> list[dict[str, Any]]:
    counts = await _tool_counts(tenant)
    return [connector_admin_json(c, counts.get(c.slug)) for c in await reg.list_connectors(tenant)]


async def detail_admin(tenant: str, slug: str) -> dict[str, Any]:
    conn = await _get(tenant, slug)
    counts = await _tool_counts(tenant)
    return connector_admin_json(conn, counts.get(slug), detail=True)


async def _get(tenant: str, slug: str) -> reg.ConnectorRow:
    try:
        return await reg.get_connector(tenant, slug)
    except reg.ConnectorNotFound as exc:
        raise AdminError(404, "not_found", "connector not found") from exc


# ───────────────────────────── four-eyes base state ─────────────────────────────


def _connector_core(conn: reg.ConnectorRow) -> dict[str, Any]:
    """What identifies the server a connector talks to (never the secret itself)."""
    return {"url": conn.url, "issuer": conn.issuer, "auth_kind": conn.auth_kind,
            "auth_config": {k: v for k, v in (conn.auth_config or {}).items() if "secret" not in str(k).lower()},
            "has_secret": conn.has_secret}


def _tool_state(tool: reg.ToolRow) -> dict[str, Any]:
    return {"name": tool.name, "definition_hash": tool.definition_hash,
            "classification": tool.classification.model_dump(mode="json"), "status": tool.status,
            "binding": tool.binding}


def _fingerprint(state: Any) -> str:
    import hashlib
    import json

    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "state:" + hashlib.sha256(raw).hexdigest()[:24]


async def guard_base(tenant: str, slug: str, *, tool: str | None = None, external_tools: bool = False) -> str:
    """``base_version`` of a connector-targeted change request (M7 four-eyes, ``110`` §1): a
    fingerprint of the state the requester saw — the server identity (URL, issuer, auth kind and
    settings), plus the tool's definition hash / classification / status / binding for tool kinds,
    or the approved external-egress tools for an activation. Approval recomputes it and answers
    ``409 stale_base`` when it moved (a re-pointed connector, a tool whose definition drifted or
    was reclassified meanwhile): the approver never applies a change to something they did not see."""
    conn = await _get(tenant, slug)
    state: dict[str, Any] = {"connector": _connector_core(conn)}
    if tool is not None:
        row = await reg.get_tool(tenant, slug, tool)
        state["tool"] = _tool_state(row) if row is not None else None
    if external_tools:
        state["external"] = [_tool_state(t) for t in await reg.list_tools(tenant, slug, include_removed=False)
                             if t.status == "approved" and t.classification.egress.value == "external"]
    return _fingerprint(state)


async def _check_base(tenant: str, slug: str, base_version: str | None, **kw: Any) -> None:
    """Approval of a request that recorded a base state: 409 ``stale_base`` when it moved."""
    if base_version is None:
        return
    if await guard_base(tenant, slug, **kw) != base_version:
        raise AdminError(409, "stale_base", "the connector or tool changed since this request was made",
                         base_version=base_version)


# ───────────────────────────── test connection ─────────────────────────────


@dataclass
class _Log:
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, step: str, ok: bool, started: float, detail: str) -> bool:
        self.steps.append({"step": step, "ok": ok, "ms": int((time.monotonic() - started) * 1000),
                           "detail": detail[:500]})
        return ok


def _http_status(exc: BaseException) -> int | None:
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    texts: list[str] = []
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        code = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int):
            return code
        texts.append(str(e))
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        for nxt in (e.__cause__, e.__context__):
            if nxt is not None:
                stack.append(nxt)
    for t in texts:
        m = re.search(r"\b(401|403|404|405|406|407|502|503)\b", t)
        if m:
            return int(m.group(1))
    return None


def _describe(exc: BaseException) -> str:
    code = _http_status(exc)
    if code is not None:
        return f"HTTP {code}"
    root: BaseException = exc
    while isinstance(root, BaseExceptionGroup) and root.exceptions:
        root = root.exceptions[0]
    while root.__cause__ is not None:
        root = root.__cause__
    return root.__class__.__name__


async def _probe_status(url: str, headers: dict[str, str], timeout: float, *, direct: bool = False) -> int | None:
    """HTTP status of a bare JSON-RPC ``initialize`` POST (the MCP client hides it on failure)."""
    from keelson_engine.core.mcp import http_client_factory

    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "keelson-connection-test", "version": "1"}}}
    proxy = None if direct else get_settings().connector_proxy_url
    async with http_client_factory(proxy, call_timeout=timeout, connect_timeout=timeout)() as client:
        try:
            resp = await client.post(url, json=body, headers={
                **headers, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
        except Exception:  # noqa: BLE001 - no status available
            return None
    return resp.status_code


async def _reach(url: str, timeout: float, *, direct: bool = False) -> tuple[bool, str]:
    from keelson_engine.core.mcp import http_client_factory

    proxy = None if direct else get_settings().connector_proxy_url
    async with http_client_factory(proxy, call_timeout=timeout, connect_timeout=timeout)() as client:
        try:
            resp = await client.get(url, headers={"Accept": "application/json, text/event-stream"})
        except Exception as exc:  # noqa: BLE001 - any transport failure is the step result
            return False, f"not reachable ({exc.__class__.__name__})"
    if resp.headers.get("x-squid-error"):
        return False, f"blocked by the egress proxy ({resp.headers['x-squid-error'].split()[0]})"
    via = " via egress" if proxy else ""
    return True, f"HTTP {resp.status_code}{via}"


async def test_connection(tenant: str, slug: str, actor: dict[str, Any]) -> dict[str, Any]:
    from keelson_engine.core.mcp import list_tools_paged, mcp_client
    from keelson_engine.core.tokens import ConnectRequired, TokenError

    conn = await _get(tenant, slug)
    timeout = get_settings().connector_test_timeout_seconds
    run = _Log()
    tools: list[dict[str, Any]] = []
    tiers: list[str] = []
    summary: dict[str, Any] = {}
    ok = await _test_steps(conn, run, timeout, mcp_client, list_tools_paged, ConnectRequired, TokenError, actor,
                           tools, tiers, summary)
    at = time.time()
    result_log = {"ok": ok, "tier": tiers, "steps": run.steps, "at": int(at)}
    async with tenant_session(tenant) as s:
        await s.execute(
            text(
                """
                UPDATE connector SET last_test = CAST(:lt AS jsonb), last_test_at = now(), updated_at = now(),
                  status = CASE
                    WHEN :ok AND status = 'draft' THEN 'tested'
                    WHEN NOT :ok AND status = 'tested' THEN 'draft'
                    ELSE status END
                WHERE slug = :s
                """
            ),
            {"lt": dumps(result_log), "ok": ok, "s": slug},
        )
    await admin_audit(actor, "connector.test", f"connector:{slug}", None,
                      {"ok": ok, "tier": tiers, "steps": [(st["step"], st["ok"]) for st in run.steps]},
                      tenant=tenant)
    by_name = {t.name: t for t in await reg.list_tools(tenant, slug)} if summary else {}
    out_tools = []
    for d in summary.get("definitions", []):
        row = by_name.get(d["name"])
        out_tools.append({**d, "status": row.status if row else None, "drift": row.drift if row else False})
    return {"ok": ok, "tier": tiers, "steps": run.steps, "tools": out_tools}


async def _test_steps(conn: reg.ConnectorRow, run: _Log, timeout: float, mcp_client: Any, list_tools_paged: Any,
                      connect_required: type[Exception], token_error: type[Exception], actor: dict[str, Any],
                      tools: list[dict[str, Any]], tiers: list[str], summary: dict[str, Any]) -> bool:
    t0 = time.monotonic()
    try:
        if conn.is_internal:
            # internal first-party service (M6 Signal Hub): only ever the configured internal origin
            from keelson_engine.config import get_settings

            hub = get_settings().signal_hub_url
            if not hub or not reg.same_origin(conn.url, hub):
                raise AdminError(422, "url_policy", "an internal connector must use the configured Signal Hub URL")
            detail = "internal Keelson service (SIGNAL_HUB_URL)"
        else:
            detail = await check_url(conn.url)
        if conn.auth_kind == "oauth_client_credentials":
            await check_url(str(conn.auth_config.get("token_url") or ""))
    except AdminError as exc:
        return run.add("url_policy", False, t0, exc.message)
    run.add("url_policy", True, t0, detail)

    t0 = time.monotonic()
    direct = conn.is_internal  # internal services: the internal network, never the egress proxy
    mcp_extra: dict[str, Any] = {"proxy": ""} if direct else {}
    reached, detail = await _reach(conn.url, timeout, direct=direct)
    if not run.add("reach", reached, t0, detail):
        return False

    # discover: server/discover (modern) falling back to initialize — first without credentials
    t0 = time.monotonic()
    discovered = False
    try:
        async with mcp_client(conn.url, {}, timeout=timeout, **mcp_extra) as client:
            via = "initialize" if client.initialize_result is not None else "server/discover"
            info = client.server_info
            discovered = True
            run.add("discover", True, t0,
                    f"{via} · protocol {client.protocol_version} · server {getattr(info, 'name', '') or '?'}")
    except Exception as exc:  # noqa: BLE001 - every failure mode is a step result
        if not discovered:  # (an error while closing a successful probe session is irrelevant)
            code = _http_status(exc) or await _probe_status(conn.url, {}, timeout, direct=direct)
            if code in (401, 403) and conn.auth_kind != "none":
                run.add("discover", True, t0, f"server requires authentication (HTTP {code})")
            else:
                why = f"HTTP {code}" if code and code >= 400 else _describe(exc)
                return run.add("discover", False, t0, f"MCP handshake failed ({why})")

    # auth: obtain the credential and open an authenticated session; tools_list reuses it
    t0 = time.monotonic()
    try:
        headers = await reg.headers_for(conn, "reader")
    except connect_required:
        return run.add("auth", False, t0, "a delegated sign-in is required")
    except token_error as exc:
        return run.add("auth", False, t0, f"credential refused: {exc}")
    except ValueError as exc:
        return run.add("auth", False, t0, f"credential unavailable ({exc.__class__.__name__})")
    what = {
        "none": "no credentials",
        "static_header": f"header {conn.auth_config.get('header_name')}",
        "oauth_client_credentials": f"client-credentials token ({conn.auth_config.get('client_auth')})",
        "oauth_platform_merxa": "platform reader token",
        "service_jwt": "internal service token",
    }.get(conn.auth_kind, conn.auth_kind)
    listed: list[dict[str, Any]] | None = None
    pages = 0
    list_error: BaseException | None = None
    authenticated = False
    t_list = time.monotonic()
    try:
        async with mcp_client(conn.url, headers, timeout=timeout, **mcp_extra) as client:
            authenticated = True
            run.add("auth", True, t0, f"{what} accepted")
            t_list = time.monotonic()
            try:
                listed, pages = await list_tools_paged(client)
            except Exception as exc:  # noqa: BLE001 - reported as the tools_list step
                list_error = exc
    except Exception as exc:  # noqa: BLE001
        if not authenticated:
            code = _http_status(exc) or await _probe_status(conn.url, headers, timeout, direct=direct)
            why = f"HTTP {code}" if code and code >= 400 else _describe(exc)
            return run.add("auth", False, t0, f"{what} rejected ({why})")
        if listed is None and list_error is None:
            list_error = exc  # (an error while closing after a successful listing is ignored)
    if listed is None:
        detail = _describe(list_error) if list_error is not None else "no result"
        return run.add("tools_list", False, t_list, f"tools/list failed ({detail})")
    tools.extend(listed)
    if not run.add("tools_list", bool(tools), t_list,
                   f"{len(tools)} tool(s) in {pages} page(s)" if tools else "the server offers no tools"):
        return False

    t0 = time.monotonic()
    detected = [t.value for t in detect_tiers([str(t.get("name") or "") for t in tools],
                                              has_webhooks=bool(conn.webhook_secret_ref))]
    tiers.extend(detected)
    run.add("tier_detect", True, t0, ", ".join(detected) or "none")

    t0 = time.monotonic()
    approve = conn.trust == "first_party"
    result = await reg.upsert_tool_snapshot(
        conn.tenant, conn.slug, tools, auto_approve=approve,
        untrusted=conn.untrusted_tools if conn.is_provisioned else (),
        has_webhooks=bool(conn.webhook_secret_ref), actor=actor, read_only=conn.is_internal,
    )
    summary.update(result)
    added = len(result["added"]) if not result["first_snapshot"] else result["tools"]
    run.add("classify", True, t0,
            f"{added} new · {len(result['changed'])} changed · {len(result['removed'])} removed · "
            f"{len(result['needs_review'])} need review")
    return True


# ───────────────────────────── tool review ─────────────────────────────

_CLASS_FIELDS = ("access", "content_trust", "egress", "idempotency")


async def update_tool(tenant: str, slug: str, name: str, patch: dict[str, Any], actor: dict[str, Any], *,
                      platform_admin: bool, cosigned: bool = False, base_version: str | None = None) -> dict[str, Any]:
    """Admin classification / review of one tool. Guarded (M7: a change request for tenant-admins,
    ``cosigned`` = an approved request re-applying it): classifying a write/destructive tool as
    ``read`` and untrusted free text as ``trusted_data`` (against the current or suggested
    classification). Reclassifying a tool bound ``auto`` as write/destructive resets the binding to
    ``approval_gate`` (``auto`` on a write tool is itself guarded). ``base_version`` (approval) =
    the request's :func:`guard_base`: 409 ``stale_base`` when the tool drifted meanwhile."""
    if "binding" in patch:
        if set(patch) - {"binding"}:
            raise AdminError(422, "binding_separate", "change the binding on its own (PATCH {binding})")
        return await set_binding(tenant, slug, name, str(patch["binding"]), actor, platform_admin=platform_admin,
                                 cosigned=cosigned, base_version=base_version)
    await _get(tenant, slug)
    tool = await reg.get_tool(tenant, slug, name)
    if tool is None:
        raise AdminError(404, "not_found", "tool not found")
    if tool.status == "removed":
        raise AdminError(409, "tool_removed", "the connector no longer offers this tool")
    if cosigned:
        await _check_base(tenant, slug, base_version, tool=name)
    current = tool.classification.model_dump(mode="json")
    updates = {k: patch[k] for k in _CLASS_FIELDS if patch.get(k) is not None}
    new_status = patch.get("status")
    if new_status is not None and new_status not in ("approved", "needs_review", "disabled"):
        raise AdminError(422, "invalid_status", "status must be approved, needs_review or disabled")
    try:
        new_cls = ToolClassification.model_validate({**current, **updates})
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        raise AdminError(422, "invalid_classification", "invalid classification value") from exc
    suggested = tool.suggested or tool.classification
    approving = new_status == "approved"
    touches_access = "access" in updates or approving
    reasons: list[dict[str, str]] = []
    target = f"connector:{slug}/tool:{name}"
    if (touches_access and new_cls.access.value == "read"
            and (tool.classification.access.value != "read" or suggested.access.value != "read")):
        reasons.append({"code": "tool_reclassified_read", "field": target,
                        "reason": "classifies a write or destructive tool as read"})
    touches_trust = "content_trust" in updates or approving
    if (touches_trust and new_cls.content_trust.value == "trusted_data"
            and (tool.classification.content_trust.value != "trusted_data"
                 or suggested.content_trust.value != "trusted_data")):
        reasons.append({"code": "tool_reclassified_trusted", "field": target,
                        "reason": "marks untrusted free text as trusted data"})
    if reasons and not (platform_admin or cosigned):
        from keelson_engine.core.change_requests import GuardedChange, json_diff

        raise GuardedChange(
            tenant=tenant, actor=actor, kind="connector_tool", target=target, reasons=reasons,
            payload={"connector": slug, "tool": name,
                     "patch": {k: v for k, v in patch.items() if k in (*_CLASS_FIELDS, "status")}},
            diff=json_diff({**current, "status": tool.status},
                           {**new_cls.model_dump(mode="json"), "status": new_status or tool.status}),
            base_version=await guard_base(tenant, slug, tool=name),
            message="classifying a write tool as read or untrusted text as trusted needs a second admin")
    status = new_status or tool.status
    reset_binding = tool.binding == "auto" and new_cls.access.value != "read"
    async with tenant_session(tenant) as s:
        await s.execute(
            text(
                """
                UPDATE connector_tool SET access = :access, content_trust = :trust, egress = :egress,
                  idempotency = :idem, classification = CAST(:cls AS jsonb), status = :st,
                  drift = CASE WHEN :approving THEN false ELSE drift END,
                  reviewed_by = CASE WHEN :approving THEN :by ELSE reviewed_by END,
                  reviewed_at = CASE WHEN :approving THEN now() ELSE reviewed_at END,
                  binding = CASE WHEN :reset THEN 'approval_gate' ELSE binding END,
                  updated_at = now()
                WHERE connector = :c AND name = :n
                """
            ),
            {"access": new_cls.access.value, "trust": new_cls.content_trust.value, "egress": new_cls.egress.value,
             "idem": new_cls.idempotency.value, "cls": dumps(new_cls.model_dump(mode="json")), "st": status,
             "approving": approving, "by": actor.get("username"), "c": slug, "n": name, "reset": reset_binding},
        )
    await admin_audit(actor, "connector.tool.update", f"connector:{slug}/tool:{name}",
                      {**current, "status": tool.status}, {**new_cls.model_dump(mode="json"), "status": status},
                      tenant=tenant)
    updated = await reg.get_tool(tenant, slug, name)
    assert updated is not None
    return updated.admin_json()


# ───────────────────────────── lifecycle ─────────────────────────────


BINDINGS = ("auto", "approval_gate", "deny")


async def set_binding(tenant: str, slug: str, name: str, binding: str, actor: dict[str, Any], *,
                      platform_admin: bool, cosigned: bool = False, base_version: str | None = None) -> dict[str, Any]:
    """Agent tool binding (plan §10 ``agent_tool_binding``): ``auto`` | ``approval_gate`` | ``deny``.
    ``auto`` on a write / destructive tool is guarded (change request ``tool_binding``)."""
    if binding not in BINDINGS:
        raise AdminError(422, "invalid_binding", "binding must be auto, approval_gate or deny")
    await _get(tenant, slug)
    tool = await reg.get_tool(tenant, slug, name)
    if tool is None:
        raise AdminError(404, "not_found", "tool not found")
    if tool.status == "removed":
        raise AdminError(409, "tool_removed", "the connector no longer offers this tool")
    if tool.binding == binding:
        return tool.admin_json()
    if cosigned:
        await _check_base(tenant, slug, base_version, tool=name)
    target = f"connector:{slug}/tool:{name}"
    if binding == "auto" and tool.classification.access.value != "read" and not (platform_admin or cosigned):
        from keelson_engine.core.change_requests import GuardedChange

        raise GuardedChange(
            tenant=tenant, actor=actor, kind="tool_binding", target=target,
            reasons=[{"code": "auto_binding_on_write", "field": target,
                      "reason": f"lets agents call the {tool.classification.access.value} tool without approval"}],
            payload={"connector": slug, "tool": name, "binding": binding},
            diff=[{"op": "replace", "path": "/binding", "before": tool.binding, "after": binding}],
            base_version=await guard_base(tenant, slug, tool=name),
            message="binding auto on a write tool needs a second admin")
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE connector_tool SET binding = :b, updated_at = now() WHERE connector = :c "
                             "AND name = :n"), {"b": binding, "c": slug, "n": name})
    await admin_audit(actor, "connector.tool.binding", target, {"binding": tool.binding}, {"binding": binding},
                      tenant=tenant)
    updated = await reg.get_tool(tenant, slug, name)
    assert updated is not None
    return updated.admin_json()


async def _external_tools(tenant: str, slug: str) -> list[str]:
    return [t.name for t in await reg.list_tools(tenant, slug, include_removed=False)
            if t.status == "approved" and t.classification.egress.value == "external"]


async def set_status(tenant: str, slug: str, action: str, actor: dict[str, Any], *,
                     platform_admin: bool = False, cosigned: bool = False,
                     base_version: str | None = None) -> dict[str, Any]:
    """Activate / disable. Activating a connector with approved **external-egress** tools is
    guarded (M7 change request ``connector_activate``) unless ``platform_admin`` / ``cosigned``."""
    conn = await _get(tenant, slug)
    if action == "activate":
        if not (conn.last_test or {}).get("ok") and not conn.is_provisioned:
            raise AdminError(409, "test_required", "run a passing connection test before activating")
        if cosigned:
            await _check_base(tenant, slug, base_version, external_tools=True)
        external = [] if conn.status == "active" else await _external_tools(tenant, slug)
        if external and not (platform_admin or cosigned):
            from keelson_engine.core.change_requests import GuardedChange

            raise GuardedChange(
                tenant=tenant, actor=actor, kind="connector_activate", target=f"connector:{slug}",
                reasons=[{"code": "external_egress_enabled", "field": f"connector:{slug}",
                          "reason": ("enables a connector whose tools send data outside (external egress): "
                                     + ", ".join(external[:10]))[:300]}],
                payload={"connector": slug},
                diff=[{"op": "replace", "path": "/status", "before": conn.status, "after": "active"}],
                base_version=await guard_base(tenant, slug, external_tools=True),
                message="enabling an external-egress connector needs a second admin")
        new = "active"
    elif action == "disable":
        new = "disabled"
    else:  # pragma: no cover - routes only pass the two actions
        raise AdminError(400, "invalid_action", action)
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE connector SET status = :st, updated_at = now() WHERE slug = :s"),
                        {"st": new, "s": slug})
    reg.clear_token_cache(tenant, slug)
    await admin_audit(actor, f"connector.{action}", f"connector:{slug}", {"status": conn.status}, {"status": new},
                      tenant=tenant)
    return {"slug": slug, "status": new}


def _trust_guard(tenant: str, slug: str, before: str, after: str, actor: dict[str, Any],
                 base_version: str | None = None) -> Any:
    from keelson_engine.core.change_requests import GuardedChange

    return GuardedChange(
        tenant=tenant, actor=actor, kind="connector_trust", target=f"connector:{slug}",
        reasons=[{"code": "first_party_trust_added", "field": f"connector:{slug}/trust_level",
                  "reason": "makes the connector first-party (its tool classifications are auto-approved)"}],
        payload={"connector": slug, "trust_level": after},
        diff=[{"op": "replace", "path": "/trust_level", "before": before, "after": after}],
        base_version=base_version,
        message="adding a first-party trust level needs a second admin")


async def set_trust(tenant: str, slug: str, trust_level: str, actor: dict[str, Any], *,
                    platform_admin: bool, cosigned: bool = False, base_version: str | None = None) -> dict[str, Any]:
    """``PATCH /api/admin/connectors/{slug} {trust_level}``: raising to ``first_party`` is guarded
    (change request ``connector_trust``); lowering to ``third_party`` applies directly. Platform
    connectors (provisioned by Keelson) keep their trust level."""
    if trust_level not in ("first_party", "third_party"):
        raise AdminError(422, "invalid_trust_level", "trust_level must be first_party or third_party")
    conn = await _get(tenant, slug)
    if conn.is_provisioned:
        raise AdminError(409, "provisioned_connector", "a platform connector keeps its trust level")
    if conn.trust == trust_level:
        return {"slug": slug, "trust_level": trust_level, "unchanged": True}
    if cosigned:
        await _check_base(tenant, slug, base_version)
    if trust_level == "first_party" and not (platform_admin or cosigned):
        raise _trust_guard(tenant, slug, conn.trust, trust_level, actor, await guard_base(tenant, slug))
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE connector SET trust_level = :tr, updated_at = now() WHERE slug = :s"),
                        {"tr": trust_level, "s": slug})
    await admin_audit(actor, "connector.trust", f"connector:{slug}", {"trust_level": conn.trust},
                      {"trust_level": trust_level}, tenant=tenant)
    return {"slug": slug, "trust_level": trust_level}


async def delete_connector(tenant: str, slug: str, actor: dict[str, Any], *, platform_admin: bool,
                           cosigned: bool = False, base_version: str | None = None) -> None:
    """Delete a connector (credentials, links, canonical snapshots, mappings, tools). Guarded (M7
    change request ``connector_delete``) for tenant-admins; platform connectors are platform-admin
    only (403)."""
    from keelson_engine.core import scn_sync
    from keelson_engine.core.agent_specs import bound_connectors

    conn = await _get(tenant, slug)
    if slug in await bound_connectors(tenant):
        raise AdminError(409, "connector_bound", "the latest agent spec still uses this connector")
    if conn.is_provisioned and not platform_admin:
        raise AdminError(403, "guarded_change", "only a platform admin may delete a platform connector")
    if cosigned:
        await _check_base(tenant, slug, base_version)
    if not (platform_admin or cosigned):
        from keelson_engine.core.change_requests import GuardedChange

        raise GuardedChange(
            tenant=tenant, actor=actor, kind="connector_delete", target=f"connector:{slug}",
            reasons=[{"code": "connector_deleted", "field": f"connector:{slug}",
                      "reason": "deletes the connector, its credentials, mappings and canonical snapshots"}],
            payload={"connector": slug},
            diff=[{"op": "remove", "path": f"/connectors/{slug}",
                   "before": {"name": conn.name, "url": conn.url, "status": conn.status}}],
            base_version=await guard_base(tenant, slug),
            message="deleting a connector needs a second admin")
    async with tenant_session(tenant) as s:
        for table in ("connector_credential", "connector_identity_link"):
            await s.execute(text(f"DELETE FROM {table} WHERE connector = :s"), {"s": slug})
        await scn_sync.purge_connector(s, tenant, slug)  # canonical snapshots of this connector
        await s.execute(text("DELETE FROM connector WHERE slug = :s"), {"s": slug})
    reg.clear_token_cache(tenant, slug)
    await admin_audit(actor, "connector.delete", f"connector:{slug}",
                      {"name": conn.name, "url": conn.url, "auth_kind": conn.auth_kind, "status": conn.status}, None,
                      tenant=tenant)


async def rotate_secret(tenant: str, slug: str, secret: str, actor: dict[str, Any]) -> dict[str, Any]:
    """Replace the stored credential of a ``static_header`` connector (the header value, e.g. a
    vendor API key) or an ``oauth_client_credentials`` connector with ``client_secret``
    authentication. Nothing else changes (tool classifications, mappings, bindings, status); the
    secret is encrypted like at registration and never echoed or audited. Run *Test* afterwards."""
    conn = await _get(tenant, slug)
    if conn.is_provisioned or conn.auth_kind not in ("static_header", "oauth_client_credentials"):
        raise AdminError(422, "no_rotatable_secret", "this connector has no stored secret to replace")
    if conn.auth_kind == "oauth_client_credentials" and conn.auth_config.get("client_auth") != "client_secret":
        raise AdminError(422, "no_rotatable_secret", "this connector signs with Keelson's key; it has no client secret")
    value = secret.strip() if isinstance(secret, str) else ""
    if not value or len(value) > SECRET_MAX:
        raise AdminError(422, "invalid_auth", "the new secret is required")
    if conn.auth_kind == "static_header" and not HEADER_VALUE_RE.match(value):
        raise AdminError(422, "invalid_auth", "the header value may only contain visible ASCII characters")
    enc = encrypt_str(value, reg.secret_aad(tenant, slug))
    async with tenant_session(tenant) as s:
        await s.execute(text("UPDATE connector SET secret_enc = :enc, updated_at = now() WHERE slug = :s"),
                        {"enc": enc, "s": slug})
    reg.clear_token_cache(tenant, slug)
    await admin_audit(actor, "connector.secret_rotated", f"connector:{slug}", {"auth_kind": conn.auth_kind},
                      {"auth_kind": conn.auth_kind, "has_secret": True}, tenant=tenant)
    return {"slug": slug, "status": conn.status, "auth_kind": conn.auth_kind, "rotated": True}


async def list_events(tenant: str, slug: str, limit: int = 50) -> list[dict[str, Any]]:
    await _get(tenant, slug)
    limit = max(1, min(int(limit), 500))
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                "SELECT id, event_id, type, payload, received_at, processed_at FROM connector_event "
                "WHERE connector = :c ORDER BY received_at DESC, id DESC LIMIT :n"
            ),
            {"c": slug, "n": limit},
        )
        rows = res.mappings().all()
    return [
        {"id": int(r["id"]), "event_id": r["event_id"], "type": r["type"], "payload": r["payload"],
         "received_at": _iso(r["received_at"]), "processed_at": _iso(r["processed_at"])}
        for r in rows
    ]
