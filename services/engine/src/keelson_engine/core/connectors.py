"""Connector registry (per tenant, RLS): connector rows, the admin-reviewed tool snapshot,
connector credentials for the runtime (:func:`auth_headers`) and tenant LiteLLM virtual keys.

Connector lifecycle (``docs/contracts/70-backoffice-m2.md`` §3): ``draft`` → (passing test)
``tested`` → ``active`` ⇄ ``disabled``. Platform connectors (``auth_kind =
oauth_platform_merxa``, from ``infra/tenants.yaml`` / packs) start ``active``.

Auth kinds:

* ``none`` — no credentials;
* ``static_header`` — one header (``auth_config.header_name``) whose value is the encrypted
  ``secret_enc`` (e.g. ``X-Merxa-Api-Key``);
* ``oauth_client_credentials`` — client credentials at ``auth_config.token_url`` with
  ``client_secret`` (encrypted in ``secret_enc``) or ``private_key_jwt`` (Keelson's ``ca-1``
  key), optional ``scopes`` / ``resource``; tokens cached in-process until 30 s before expiry;
* ``oauth_platform_merxa`` — Keelson's own Merxa clients (reader / preparer / autocommit
  service principals via :class:`~keelson_engine.core.tokens.TokenBroker`, delegated users);
* ``service_jwt`` (M6) — Keelson's internal first-party services (the Signal Hub): a short-lived
  HS256 service JWT with the tenant claim (:func:`keelson_engine.core.signal_hub.mint_hub_token`),
  minted **only** for a connector URL on the ``SIGNAL_HUB_URL`` origin. Provisioned by bootstrap
  (never registrable through the back-office); its read-only tools are auto-approved, every
  non-read tool needs a review.

Tool snapshot: every tool gets a ``definition_hash`` = sha256 of its canonical ``{name, title,
description, inputSchema, outputSchema, annotations}`` and a **suggested** classification
(:func:`suggest_classification`). New or changed tools become ``needs_review`` (``drift`` =
changed since the last review) unless the connector is ``first_party`` (or the caller passes
``auto_approve=True``), in which case the suggestion is approved and audited (a classification
a human reviewed is never loosened: per field the stricter of the two wins). Tools that
disappeared become ``removed``. Only ``approved`` tools of an ``active`` connector are listed
by :func:`list_approved_tools`; LLM agents additionally need ``access=read`` +
``content_trust=trusted_data`` (``ToolClassification.investigator_bindable``).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from keelson_contracts.connectors import (
    ContentTrust,
    Egress,
    Idempotency,
    ToolAccess,
    ToolClassification,
    detect_tiers,
)
from keelson_engine.core.crypto import DATA_KEY_ID, decrypt_str, encrypt_str, sign_client_assertion
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps

__all__ = [
    "AUTH_KINDS",
    "CONNECTOR_STATUSES",
    "ConnectorDisabled",
    "ConnectorNotFound",
    "ConnectorRow",
    "DEFAULT_UNTRUSTED_TOOLS",
    "LlmKeyMissing",
    "TOOL_STATUSES",
    "ToolRow",
    "auth_headers",
    "classify_tool",
    "clear_llm_key_cache",
    "clear_token_cache",
    "connector_secret",
    "definition_hash",
    "forget_tenant_llm_key",
    "get_action_mapping",
    "get_active_connector",
    "get_capabilities",
    "get_connector",
    "get_read_mapping",
    "get_tenant_llm_key",
    "get_tenant_llm_key_version",
    "headers_for",
    "list_approved_tools",
    "list_connectors",
    "list_tenants_with_connector",
    "list_tools",
    "llm_key_version_key",
    "refresh_tool_snapshot",
    "same_origin",
    "set_tool_lister",
    "store_tenant_llm_key",
    "suggest_classification",
    "upsert_connector",
    "upsert_tool_snapshot",
]

log = get_logger(__name__)

DEFAULT_UNTRUSTED_TOOLS = ("list_supplier_messages", "list_events")
"""Free-text sources (supplier messages, the events feed): content_trust untrusted_text (contract 90 §4)."""
AUTH_KINDS = ("none", "static_header", "oauth_client_credentials", "oauth_platform_merxa", "service_jwt")
INTERNAL_AUTH_KIND = "service_jwt"
"""Auth kind of Keelson's internal first-party services (bootstrap-provisioned, never admin-registered)."""
CONNECTOR_STATUSES = ("draft", "tested", "active", "disabled")
TOOL_STATUSES = ("needs_review", "approved", "disabled", "removed")
SERVICE_PRINCIPALS = ("reader", "preparer", "autocommit")
_TIER_ORDER = {"A": 0, "B": 1, "C": 2, "W": 3}
_SKEW_S = 30


class ConnectorNotFound(LookupError):
    pass


class ConnectorDisabled(LookupError):
    """The connector exists but is disabled (or not yet activated) — never call it."""


class LlmKeyMissing(LookupError):
    pass


@dataclass(slots=True)
class ConnectorRow:
    tenant: str
    slug: str
    name: str
    url: str
    """MCP endpoint = RFC 8707 resource / token audience."""
    issuer: str
    clients: dict[str, str]
    """Platform connectors: ``{"reader": client_id, "preparer": …, "autocommit": …, "connect": …}``."""
    lanes: list[dict[str, Any]]
    config: dict[str, Any]
    tier: str | None
    tiers: list[str]
    trust: str
    status: str
    auth_kind: str = "none"
    auth_config: dict[str, Any] = field(default_factory=dict)
    has_secret: bool = False
    webhook_secret_ref: str | None = None
    last_test: dict[str, Any] | None = None
    last_test_at: Any = None
    created_by: str | None = None

    @property
    def resource(self) -> str:
        return self.url

    @property
    def untrusted_tools(self) -> list[str]:
        return list(self.config.get("untrusted_tools") or DEFAULT_UNTRUSTED_TOOLS)

    @property
    def is_platform(self) -> bool:
        return self.auth_kind == "oauth_platform_merxa"

    @property
    def is_internal(self) -> bool:
        """An internal first-party service connector (M6 Signal Hub, ``service_jwt``)."""
        return self.auth_kind == INTERNAL_AUTH_KIND

    @property
    def is_provisioned(self) -> bool:
        """Provisioned by Keelson (platform or internal): guarded like a platform connector."""
        return self.is_platform or self.is_internal

    @property
    def usable(self) -> bool:
        """The runtime may call this connector (``active``)."""
        return self.status == "active"

    def client_id(self, principal: str) -> str:
        cid = self.clients.get(principal)
        if not cid:
            raise ConnectorNotFound(f"connector {self.slug} has no {principal} client")
        return cid


@dataclass(slots=True)
class ToolRow:
    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    classification: ToolClassification
    definition_hash: str
    status: str
    connector: str = ""
    suggested: ToolClassification | None = None
    drift: bool = False
    reviewed_by: str | None = None
    reviewed_at: Any = None
    updated_at: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    binding: str = "approval_gate"
    """M7 agent tool binding: ``auto`` | ``approval_gate`` | ``deny`` (``auto`` on a write tool is a
    guarded change, ``connector_admin.set_binding``)."""

    @property
    def access(self) -> str:
        return self.classification.access.value

    @property
    def content_trust(self) -> str:
        return self.classification.content_trust.value

    def admin_json(self) -> dict[str, Any]:
        cls = self.classification
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "access": cls.access.value,
            "content_trust": cls.content_trust.value,
            "egress": cls.egress.value,
            "idempotency": cls.idempotency.value,
            "bindable": self.status == "approved" and cls.investigator_bindable,
            "binding": self.binding,
            "status": self.status,
            "drift": self.drift,
            "suggested": self.suggested.model_dump(mode="json") if self.suggested else None,
            "annotations": self.annotations,
            "input_schema": self.input_schema,
            "definition_hash": self.definition_hash,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if hasattr(self.reviewed_at, "isoformat") else None,
            "updated_at": self.updated_at.isoformat() if hasattr(self.updated_at, "isoformat") else None,
        }


# ───────────────────────────── connectors ─────────────────────────────


def _connector_row(d: dict[str, Any]) -> ConnectorRow:
    auth = d.get("auth") or {}
    clients = {
        "reader": auth.get("reader_client", ""),
        "preparer": auth.get("preparer_client", ""),
        "autocommit": auth.get("autocommit_client", ""),
        "connect": auth.get("connect_client", ""),
    }
    tiers = [str(t) for t in d.get("tiers") or []]
    tier = max(tiers, key=lambda t: _TIER_ORDER.get(t, -1)) if tiers else None
    return ConnectorRow(
        tenant=d["tenant"],
        slug=d["slug"],
        name=d["name"],
        url=d["mcp_url"],
        issuer=d.get("issuer") or "",
        clients={k: v for k, v in clients.items() if v},
        lanes=list(d.get("lanes") or []),
        config=dict(d.get("config") or {}),
        tier=tier,
        tiers=tiers,
        trust=d.get("trust_level") or "third_party",
        status=d.get("status") or "draft",
        auth_kind=d.get("auth_kind") or "none",
        auth_config=dict(d.get("auth_config") or {}),
        has_secret=bool(d.get("secret_enc")),
        webhook_secret_ref=d.get("webhook_secret_ref"),
        last_test=d.get("last_test"),
        last_test_at=d.get("last_test_at"),
        created_by=d.get("created_by"),
    )


_CONNECTOR_COLS = (
    "tenant, slug, name, mcp_url, issuer, trust_level, auth, lanes, config, status, tiers, snapshot_at, "
    "auth_kind, auth_config, secret_enc, webhook_secret_ref, last_test, last_test_at, created_by"
)


async def get_connector(tenant: str, slug: str) -> ConnectorRow:
    """Any connector row of ``tenant`` (whatever its status; check :attr:`ConnectorRow.usable`)."""
    async with tenant_session(tenant) as s:
        res = await s.execute(text(f"SELECT {_CONNECTOR_COLS} FROM connector WHERE slug = :s"), {"s": slug})
        m = res.mappings().first()
    if m is None:
        raise ConnectorNotFound(f"connector {slug!r} not configured for tenant {tenant!r}")
    return _connector_row(dict(m))


async def get_active_connector(tenant: str, slug: str) -> ConnectorRow:
    """The connector if the runtime may use it, else :class:`ConnectorDisabled`."""
    conn = await get_connector(tenant, slug)
    if not conn.usable:
        raise ConnectorDisabled(f"connector {slug!r} is {conn.status}")
    return conn


async def list_connectors(tenant: str) -> list[ConnectorRow]:
    async with tenant_session(tenant) as s:
        res = await s.execute(text(f"SELECT {_CONNECTOR_COLS} FROM connector ORDER BY slug"))
        return [_connector_row(dict(m)) for m in res.mappings().all()]


async def list_tenants_with_connector(connector: str) -> list[str]:
    """Tenants that have an **active** ``connector`` (worker start-up snapshot refresh)."""
    async with system_session() as s:
        res = await s.execute(
            text("SELECT tenant FROM connector WHERE slug = :c AND status = 'active' ORDER BY tenant"),
            {"c": connector},
        )
        return [str(r[0]) for r in res.all()]


def _platform_spec(spec: dict[str, Any]) -> bool:
    auth = spec.get("auth") or {}
    return bool(auth.get("reader_client")) or auth.get("kind") == "oauth_platform_merxa" or bool(spec.get("platform"))


async def upsert_connector(tenant: str, spec: dict[str, Any], *, overwrite: bool = True) -> bool:
    """Create/update a connector from a tenants.yaml / pack entry (snapshot + status untouched on
    update). Platform connectors start ``active``, others ``draft``. ``overwrite=False`` leaves
    an existing row alone. Returns True when a row was inserted."""
    auth = dict(spec.get("auth") or {})
    platform = _platform_spec(spec)
    auth_kind = "oauth_platform_merxa" if platform else str(auth.get("kind") or "none")
    if auth_kind not in AUTH_KINDS:
        raise ValueError(f"connector {spec.get('slug')!r}: unknown auth kind {auth_kind!r}")
    reserved = {"slug", "name", "mcp_url", "issuer", "trust_level", "auth", "lanes", "platform",
                "webhook_secret_env", "webhook_secret_ref"}
    config = {k: v for k, v in spec.items() if k not in reserved}
    webhook_ref = spec.get("webhook_secret_ref") or spec.get("webhook_secret_env")
    auth_config = {} if platform else {k: v for k, v in auth.items() if k not in {"kind", "secret", "client_secret"}}
    conflict = (
        """
        DO UPDATE SET
          name = EXCLUDED.name, mcp_url = EXCLUDED.mcp_url, issuer = EXCLUDED.issuer,
          trust_level = EXCLUDED.trust_level, auth = EXCLUDED.auth, lanes = EXCLUDED.lanes,
          config = EXCLUDED.config, auth_kind = EXCLUDED.auth_kind, auth_config = EXCLUDED.auth_config,
          webhook_secret_ref = EXCLUDED.webhook_secret_ref, updated_at = now()
        """
        if overwrite
        else "DO NOTHING"
    )
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                f"""
                INSERT INTO connector (tenant, slug, name, mcp_url, issuer, trust_level, auth, lanes, config,
                                       status, auth_kind, auth_config, webhook_secret_ref, created_by)
                VALUES (:t, :slug, :name, :url, :iss, :trust, CAST(:auth AS jsonb), CAST(:lanes AS jsonb),
                        CAST(:config AS jsonb), :status, :kind, CAST(:auth_config AS jsonb), :wh, 'bootstrap')
                ON CONFLICT (tenant, slug) {conflict}
                RETURNING (xmax = 0) AS inserted
                """
            ),
            {
                "t": tenant,
                "slug": spec["slug"],
                "name": spec.get("name") or spec["slug"],
                "url": spec["mcp_url"],
                "iss": spec.get("issuer") or "",
                "trust": spec.get("trust_level") or ("first_party" if platform else "third_party"),
                "auth": dumps(auth if platform else {}),
                "lanes": dumps(list(spec.get("lanes") or [])),
                "config": dumps(config),
                # platform + internal (bootstrap-provisioned) connectors start active; on update the
                # status is kept (an admin's "disable" sticks)
                "status": "active" if platform or auth_kind == INTERNAL_AUTH_KIND else "draft",
                "kind": auth_kind,
                "auth_config": dumps(auth_config),
                "wh": webhook_ref,
            },
        )
        row = res.first()
    clear_token_cache(tenant, spec["slug"])
    return bool(row and row[0])


# ───────────────────────────── credentials (runtime) ─────────────────────────────


def secret_aad(tenant: str, slug: str) -> str:
    return f"connector|{tenant}|{slug}|secret"


async def connector_secret(tenant: str, slug: str) -> str | None:
    """Decrypted ``secret_enc`` (static header value / client secret) or None."""
    async with tenant_session(tenant) as s:
        res = await s.execute(text("SELECT secret_enc FROM connector WHERE slug = :s"), {"s": slug})
        enc = res.scalar_one_or_none()
    return decrypt_str(enc, secret_aad(tenant, slug)) if enc else None


@dataclass(slots=True)
class _CachedToken:
    token: str
    expires_at: float


_token_cache: dict[tuple[str, str, str], _CachedToken] = {}


def clear_token_cache(tenant: str | None = None, slug: str | None = None) -> None:
    if tenant is None:
        _token_cache.clear()
        return
    for key in [k for k in _token_cache if k[0] == tenant and (slug is None or k[1] == slug)]:
        _token_cache.pop(key, None)


def same_origin(a: str, b: str) -> bool:
    """``scheme://host:port`` of both URLs is equal (default ports normalised)."""
    from urllib.parse import urlsplit

    def origin(u: str) -> tuple[str, str, int] | None:
        try:
            p = urlsplit(u)
            port = p.port or (443 if p.scheme == "https" else 80)
        except ValueError:
            return None
        host = (p.hostname or "").lower().rstrip(".")
        if p.scheme not in ("http", "https") or not host:
            return None
        return p.scheme, host, port

    oa, ob = origin(a), origin(b)
    return oa is not None and oa == ob


def _config_fingerprint(conn: ConnectorRow, secret: str | None) -> str:
    material = json.dumps({"cfg": conn.auth_config, "url": conn.url, "s": secret or ""}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def client_credentials_token(conn: ConnectorRow, secret: str | None) -> str:
    """Client-credentials access token (``client_secret_post`` or ``private_key_jwt``) through
    the egress proxy; cached per connector configuration until 30 s before expiry."""
    from keelson_engine.core.http import connector_http
    from keelson_engine.core.tokens import TokenError

    cfg = conn.auth_config
    key = (conn.tenant, conn.slug, _config_fingerprint(conn, secret))
    cached = _token_cache.get(key)
    if cached and cached.expires_at - _SKEW_S > time.time():
        return cached.token
    token_url = str(cfg.get("token_url") or "")
    client_id = str(cfg.get("client_id") or "")
    if not token_url or not client_id:
        raise TokenError(f"connector {conn.slug}: token_url and client_id are required")
    form: dict[str, str] = {"grant_type": "client_credentials", "client_id": client_id}
    scopes = cfg.get("scopes")
    if scopes:
        form["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
    if cfg.get("resource"):
        form["resource"] = str(cfg["resource"])
    if cfg.get("client_auth") == "private_key_jwt":
        audience = str(cfg.get("audience") or token_url)
        if not same_origin(audience, token_url):  # never mint an assertion for another IdP
            raise TokenError(f"connector {conn.slug}: assertion audience must be on the token_url origin")
        form["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        form["client_assertion"] = sign_client_assertion(client_id, audience)
    else:
        if not secret:
            raise TokenError(f"connector {conn.slug}: no client secret stored")
        form["client_secret"] = secret
    try:
        resp = await connector_http().post(token_url, data=form, headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 - transport errors → TokenError (no secrets in the message)
        raise TokenError(f"connector {conn.slug}: token endpoint unreachable ({exc.__class__.__name__})") from exc
    if resp.status_code != 200:
        try:
            data = resp.json()
            err = str(data.get("error") or "")[:100] if isinstance(data, dict) else ""
        except ValueError:
            err = ""
        raise TokenError(f"connector {conn.slug}: token endpoint answered HTTP {resp.status_code} {err}".strip())
    try:
        body = resp.json()
        token = str(body["access_token"]) if isinstance(body, dict) else ""
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenError(f"connector {conn.slug}: token response has no access_token") from exc
    if not token:
        raise TokenError(f"connector {conn.slug}: token response has no access_token")
    _token_cache[key] = _CachedToken(token, time.time() + _expires_in(body.get("expires_in")))
    return token


def _expires_in(value: Any) -> int:
    """``expires_in`` of a token response (a foreign IdP's JSON): seconds, clamped to [0, 1 day];
    missing / malformed → 60 s."""
    if isinstance(value, bool):
        return 60
    try:
        seconds = int(value) if value is not None else 60
    except (TypeError, ValueError, OverflowError):
        return 60
    return max(0, min(seconds, 86_400))


async def headers_for(conn: ConnectorRow, principal: str | None = "reader", *, secret: str | None = None,
                      secret_loaded: bool = False) -> dict[str, str]:
    """HTTP headers that authenticate ``principal`` at ``conn`` (no status check)."""
    from keelson_engine.core.tokens import get_token_broker

    kind = conn.auth_kind
    if kind == "none":
        return {}
    if kind in ("static_header", "oauth_client_credentials") and not secret_loaded:
        secret = await connector_secret(conn.tenant, conn.slug)
    if kind == "static_header":
        header = str(conn.auth_config.get("header_name") or "Authorization")
        if not secret:
            from keelson_engine.core.tokens import TokenError

            raise TokenError(f"connector {conn.slug}: no header value stored")
        return {header: secret}
    if kind == "oauth_client_credentials":
        return {"Authorization": f"Bearer {await client_credentials_token(conn, secret)}"}
    if kind == INTERNAL_AUTH_KIND:
        return {"Authorization": f"Bearer {internal_service_token(conn)}"}
    # oauth_platform_merxa
    broker = get_token_broker()
    p = principal or "reader"
    if p.startswith("user:"):
        token = await broker.user_token(tenant=conn.tenant, connector=conn.slug, keelson_sub=p[5:])
    elif p in SERVICE_PRINCIPALS:
        token = await broker.service_token(tenant=conn.tenant, connector=conn.slug, principal=p)  # type: ignore[arg-type]
    else:
        raise ValueError(f"unknown principal {principal!r}")
    return {"Authorization": f"Bearer {token}"}


def internal_service_token(conn: ConnectorRow) -> str:
    """Service JWT for an internal (``service_jwt``) connector of ``conn.tenant``. Refuses (``TokenError``)
    unless the connector URL is on the configured ``SIGNAL_HUB_URL`` origin: a row pointing elsewhere
    (tampered, or a stale URL) never receives a token signed with the platform's shared secret."""
    from keelson_engine.config import get_settings
    from keelson_engine.core.signal_hub import HubNotConfigured, mint_hub_token
    from keelson_engine.core.tokens import TokenError

    hub = get_settings().signal_hub_url
    if not hub or not same_origin(conn.url, hub):
        raise TokenError(f"connector {conn.slug}: service_jwt is only issued for the Signal Hub origin")
    try:
        return mint_hub_token(conn.tenant)
    except HubNotConfigured as exc:
        raise TokenError(f"connector {conn.slug}: the Signal Hub secret is not configured") from exc


async def auth_headers(tenant: str, connector_slug: str, principal: str | None = "reader") -> dict[str, str]:
    """Headers for one MCP request of ``principal`` to an **active** connector (runtime helper).

    ``principal``: ``"reader"`` | ``"preparer"`` | ``"autocommit"`` (platform service clients) or
    ``"user:<keelson_sub>"`` (delegated link). ``static_header`` / ``oauth_client_credentials`` /
    ``service_jwt`` connectors have one credential for every principal; ``none`` → ``{}``.
    Raises :class:`ConnectorNotFound`, :class:`ConnectorDisabled`,
    :class:`~keelson_engine.core.tokens.TokenError` (IdP refused) or
    :class:`~keelson_engine.core.tokens.ConnectRequired` (delegated link missing/expired).
    Never log the result."""
    conn = await get_active_connector(tenant, connector_slug)
    return await headers_for(conn, principal)


# ───────────────────────────── tools ─────────────────────────────


def definition_hash(tool: dict[str, Any]) -> str:
    """sha256 over the canonical tool definition (key order irrelevant; floats allowed)."""
    material = {
        "name": tool.get("name"),
        "title": tool.get("title"),
        "description": tool.get("description") or "",
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": tool.get("outputSchema"),
        "annotations": tool.get("annotations") or {},
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_UNTRUSTED_WORDS = re.compile(r"(?<![a-z])(messages?|e-?mails?|events?|news|comments?|notes?)(?![a-z])", re.I)


def _mentions_untrusted(tool: dict[str, Any]) -> bool:
    name = str(tool.get("name") or "").replace("_", " ").replace(".", " ").replace("-", " ")
    title = str(tool.get("title") or "")
    description = str(tool.get("description") or "")
    return any(_UNTRUSTED_WORDS.search(part) for part in (name, title, description))


def _idempotency(tool: dict[str, Any]) -> Idempotency:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if "idempotency_key" in props:
        return Idempotency.ENFORCED if "idempotency_key" in required else Idempotency.BEST_EFFORT
    if "request_id" in props:
        return Idempotency.ENFORCED if "request_id" in required else Idempotency.BEST_EFFORT
    return Idempotency.NONE


def suggest_classification(tool: dict[str, Any], *, untrusted: Iterable[str] = ()) -> ToolClassification:
    """Contract 70 §3 suggestion: readOnlyHint → read; destructiveHint → destructive; else write;
    names/descriptions mentioning message/email/event/news/comment/note (or listed in
    ``untrusted``) → untrusted_text; openWorldHint → external egress; idempotency from an
    ``idempotency_key`` (required → enforced) / ``request_id`` (optional → best_effort) param."""
    ann = tool.get("annotations") or {}
    if ann.get("readOnlyHint") is True:
        access = ToolAccess.READ
    elif ann.get("destructiveHint") is True:
        access = ToolAccess.DESTRUCTIVE
    else:
        access = ToolAccess.WRITE
    untrusted_names = set(untrusted)
    trust = (
        ContentTrust.UNTRUSTED_TEXT
        if tool.get("name") in untrusted_names or _mentions_untrusted(tool)
        else ContentTrust.TRUSTED_DATA
    )
    egress = Egress.EXTERNAL if ann.get("openWorldHint") is True else Egress.INTERNAL
    return ToolClassification(access=access, content_trust=trust, egress=egress, idempotency=_idempotency(tool))


def classify_tool(tool: dict[str, Any], *, untrusted: tuple[str, ...] | list[str] = DEFAULT_UNTRUSTED_TOOLS,
                  egress: Egress | None = None) -> tuple[ToolClassification, str]:
    """M1 helper kept for callers: ``(suggested classification, "approved")``."""
    cls = suggest_classification(tool, untrusted=untrusted)
    if egress is not None:
        cls = cls.model_copy(update={"egress": egress})
    return cls, "approved"


_ACCESS_RANK = {"read": 0, "write": 1, "destructive": 2}


def _stricter(suggested: ToolClassification, access: str | None, trust: str | None,
              egress: str | None) -> ToolClassification:
    """Per field the more restrictive of ``suggested`` and an admin's classification."""
    acc = suggested.access.value
    if access in _ACCESS_RANK and _ACCESS_RANK[access] > _ACCESS_RANK[acc]:
        acc = access
    tr = "untrusted_text" if "untrusted_text" in (trust, suggested.content_trust.value) else "trusted_data"
    eg = "external" if "external" in (egress, suggested.egress.value) else "internal"
    return suggested.model_copy(update={"access": ToolAccess(acc), "content_trust": ContentTrust(tr),
                                        "egress": Egress(eg)})


def _cls_from_row(d: dict[str, Any]) -> ToolClassification:
    raw = d.get("classification") or {}
    return ToolClassification(
        access=d.get("access") or raw.get("access") or "write",
        content_trust=d.get("content_trust") or raw.get("content_trust") or "untrusted_text",
        egress=d.get("egress") or raw.get("egress") or "external",
        idempotency=d.get("idempotency") or raw.get("idempotency") or "none",
    )


def _tool_row(d: dict[str, Any]) -> ToolRow:
    suggested = d.get("suggested")
    return ToolRow(
        name=d["name"],
        title=d.get("title"),
        description=d.get("description") or "",
        input_schema=d.get("input_schema") or {},
        output_schema=d.get("output_schema"),
        annotations=d.get("annotations") or {},
        classification=_cls_from_row(d),
        definition_hash=d["definition_hash"],
        status=d["status"],
        connector=d.get("connector", ""),
        suggested=ToolClassification.model_validate(suggested) if suggested else None,
        drift=bool(d.get("drift")),
        reviewed_by=d.get("reviewed_by"),
        reviewed_at=d.get("reviewed_at"),
        updated_at=d.get("updated_at"),
        binding=str(d.get("binding") or "approval_gate"),
    )


_TOOL_COLS = (
    "connector, name, title, description, input_schema, output_schema, annotations, classification, "
    "definition_hash, status, access, content_trust, egress, idempotency, suggested, drift, reviewed_by, "
    "reviewed_at, updated_at, binding"
)


async def list_tools(tenant: str, connector: str, *, include_removed: bool = True) -> list[ToolRow]:
    where = "connector = :c" + ("" if include_removed else " AND status <> 'removed'")
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(f"SELECT {_TOOL_COLS} FROM connector_tool WHERE {where} ORDER BY position, name"),
            {"c": connector},
        )
        return [_tool_row(dict(m)) for m in res.mappings().all()]


async def get_tool(tenant: str, connector: str, name: str) -> ToolRow | None:
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(f"SELECT {_TOOL_COLS} FROM connector_tool WHERE connector = :c AND name = :n"),
            {"c": connector, "n": name},
        )
        m = res.mappings().first()
    return _tool_row(dict(m)) if m is not None else None


async def list_approved_tools(tenant: str, connector: str) -> list[ToolRow]:
    """``approved`` tools of an **active** connector, in server order (empty for draft/tested/
    disabled connectors). LLM agents must additionally filter on
    ``classification.investigator_bindable`` (read + trusted_data)."""
    try:
        conn = await get_connector(tenant, connector)
    except ConnectorNotFound:
        return []
    if not conn.usable:
        return []
    return [t for t in await list_tools(tenant, connector) if t.status == "approved"]


ToolLister = Callable[[str, str], Awaitable[list[dict[str, Any]]]]
_tool_lister: ToolLister | None = None


def set_tool_lister(lister: ToolLister | None) -> None:
    """Override ``tools/list`` (``(url, bearer) -> [tool dict]``) — tests."""
    global _tool_lister
    _tool_lister = lister


async def _default_lister(url: str, bearer: str) -> list[dict[str, Any]]:
    from keelson_engine.core.mcp import list_remote_tools

    return await list_remote_tools(url, bearer)


async def upsert_tool_snapshot(
    tenant: str,
    connector: str,
    tools: list[dict[str, Any]],
    *,
    auto_approve: bool,
    untrusted: Iterable[str] = (),
    has_webhooks: bool = False,
    actor: dict[str, Any] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """Merge a ``tools/list`` result into ``connector_tool`` (see module docstring) and set the
    connector's tiers. Returns ``{tools, added, changed, removed, needs_review, tiers,
    first_snapshot, definitions}``.

    ``read_only`` (internal connectors, M6): only tools suggested ``access=read`` are auto-approved;
    any other tool takes the review path (``needs_review``) even with ``auto_approve``."""
    from keelson_engine.core.repo import admin_audit

    untrusted_names = tuple(untrusted)
    added: list[str] = []
    changed: list[str] = []
    definitions: list[dict[str, Any]] = []
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT name, definition_hash, status, access, content_trust, egress, reviewed_by "
                 "FROM connector_tool WHERE connector = :c"),
            {"c": connector},
        )
        rows = res.all()
        existing = {r[0]: (r[1], r[2]) for r in rows}
        reviewed = {r[0]: (r[3], r[4], r[5], str(r[6])) for r in rows
                    if r[6] and not str(r[6]).startswith("auto:") and r[2] != "removed"}
        first_snapshot = not existing
        seen: set[str] = set()
        for position, tool in enumerate(tools):
            name = str(tool.get("name") or "")
            if not name or name in seen or len(name) > 128:
                continue
            seen.add(name)
            h = definition_hash(tool)
            suggested = suggest_classification(tool, untrusted=untrusted_names)
            definitions.append({
                "name": name, "title": tool.get("title"), "suggested": suggested.model_dump(mode="json"),
                "annotations": tool.get("annotations") or {}, "definition_hash": h,
            })
            prev = existing.get(name)
            if prev is not None and prev[0] == h and prev[1] != "removed":
                await s.execute(
                    text("UPDATE connector_tool SET position = :p WHERE connector = :c AND name = :n"),
                    {"p": position, "c": connector, "n": name},
                )
                continue
            is_new = prev is None or prev[1] == "removed"
            (added if is_new else changed).append(name)
            params = {
                "t": tenant, "c": connector, "n": name, "title": tool.get("title"),
                "d": str(tool.get("description") or "")[:8000],
                "i": dumps(tool.get("inputSchema") or {}), "o": dumps(tool.get("outputSchema")),
                "a": dumps(tool.get("annotations") or {}), "h": h, "p": position,
                "sug": dumps(suggested.model_dump(mode="json")),
                "access": suggested.access.value, "trust": suggested.content_trust.value,
                "egress": suggested.egress.value, "idem": suggested.idempotency.value,
            }
            if auto_approve and not (read_only and suggested.access != ToolAccess.READ):
                params["cls"] = params["sug"]
                # first-party: the suggestion is the classification; approved at once (audited).
                # A classification a human set is never loosened by a vendor update: per field the
                # stricter of (admin's, suggested) wins.
                params.update({"st": "approved", "drift": False, "reviewer": "auto:first_party"})
                if name in reviewed:
                    acc, trust, egress, reviewer = reviewed[name]
                    merged = _stricter(suggested, acc, trust, egress)
                    params.update({"access": merged.access.value, "trust": merged.content_trust.value,
                                   "egress": merged.egress.value, "reviewer": reviewer,
                                   "cls": dumps(merged.model_dump(mode="json"))})
                await s.execute(
                    text(
                        """
                        INSERT INTO connector_tool (tenant, connector, name, title, description, input_schema,
                          output_schema, annotations, classification, definition_hash, status, position,
                          access, content_trust, egress, idempotency, suggested, drift, reviewed_by, reviewed_at)
                        VALUES (:t, :c, :n, :title, :d, CAST(:i AS jsonb), CAST(:o AS jsonb), CAST(:a AS jsonb),
                                CAST(:cls AS jsonb), :h, :st, :p, :access, :trust, :egress, :idem,
                                CAST(:sug AS jsonb), :drift, :reviewer, now())
                        ON CONFLICT (tenant, connector, name) DO UPDATE SET
                          title = EXCLUDED.title, description = EXCLUDED.description,
                          input_schema = EXCLUDED.input_schema, output_schema = EXCLUDED.output_schema,
                          annotations = EXCLUDED.annotations, classification = EXCLUDED.classification,
                          definition_hash = EXCLUDED.definition_hash,
                          status = CASE WHEN connector_tool.status = 'disabled' THEN 'disabled' ELSE 'approved' END,
                          position = EXCLUDED.position, access = EXCLUDED.access,
                          content_trust = EXCLUDED.content_trust, egress = EXCLUDED.egress,
                          idempotency = EXCLUDED.idempotency, suggested = EXCLUDED.suggested, drift = false,
                          reviewed_by = EXCLUDED.reviewed_by, reviewed_at = now(), updated_at = now()
                        """
                    ),
                    params,
                )
            else:
                # third-party: new tools take the suggestion; changed tools keep the admin's
                # classification but need a new review (drift); disabled stays disabled.
                await s.execute(
                    text(
                        """
                        INSERT INTO connector_tool (tenant, connector, name, title, description, input_schema,
                          output_schema, annotations, classification, definition_hash, status, position,
                          access, content_trust, egress, idempotency, suggested, drift)
                        VALUES (:t, :c, :n, :title, :d, CAST(:i AS jsonb), CAST(:o AS jsonb), CAST(:a AS jsonb),
                                CAST(:sug AS jsonb), :h, 'needs_review', :p, :access, :trust, :egress, :idem,
                                CAST(:sug AS jsonb), false)
                        ON CONFLICT (tenant, connector, name) DO UPDATE SET
                          title = EXCLUDED.title, description = EXCLUDED.description,
                          input_schema = EXCLUDED.input_schema, output_schema = EXCLUDED.output_schema,
                          annotations = EXCLUDED.annotations, definition_hash = EXCLUDED.definition_hash,
                          position = EXCLUDED.position, suggested = EXCLUDED.suggested,
                          access = CASE WHEN connector_tool.status = 'removed' THEN EXCLUDED.access
                                        ELSE connector_tool.access END,
                          content_trust = CASE WHEN connector_tool.status = 'removed' THEN EXCLUDED.content_trust
                                               ELSE connector_tool.content_trust END,
                          egress = CASE WHEN connector_tool.status = 'removed' THEN EXCLUDED.egress
                                        ELSE connector_tool.egress END,
                          idempotency = CASE WHEN connector_tool.status = 'removed' THEN EXCLUDED.idempotency
                                             ELSE connector_tool.idempotency END,
                          classification = CASE WHEN connector_tool.status = 'removed' THEN EXCLUDED.classification
                                                ELSE connector_tool.classification END,
                          drift = connector_tool.status <> 'removed',
                          status = CASE WHEN connector_tool.status = 'disabled' THEN 'disabled'
                                        ELSE 'needs_review' END,
                          reviewed_by = NULL, reviewed_at = NULL, updated_at = now()
                        """
                    ),
                    params,
                )
        removed = [n for n, (_h, st) in existing.items() if n not in seen and st != "removed"]
        if removed:
            await s.execute(
                text(
                    "UPDATE connector_tool SET status = 'removed', updated_at = now() "
                    "WHERE connector = :c AND name = ANY(:names)"
                ),
                {"c": connector, "names": removed},
            )
        res = await s.execute(
            text("SELECT name, status FROM connector_tool WHERE connector = :c AND status <> 'removed'"),
            {"c": connector},
        )
        live = res.all()
        tiers = [t.value for t in detect_tiers([r[0] for r in live], has_webhooks=has_webhooks)]
        needs_review = sorted(r[0] for r in live if r[1] == "needs_review")
        await s.execute(
            text("UPDATE connector SET tiers = CAST(:tiers AS jsonb), snapshot_at = now(), updated_at = now() "
                 "WHERE slug = :c"),
            {"tiers": dumps(tiers), "c": connector},
        )
    summary = {
        "tools": len(seen),
        "added": added if not first_snapshot else [],
        "changed": changed,
        "removed": removed,
        "needs_review": needs_review,
        "tiers": tiers,
        "first_snapshot": first_snapshot,
        "definitions": definitions,
    }
    who = actor or {"sub": "system", "username": "system"}
    if first_snapshot:
        await admin_audit(who, "connector.tools.snapshot", f"connector:{connector}", None,
                          {"tools": len(seen), "tiers": tiers, "auto_approved": auto_approve}, tenant=tenant)
    elif added or changed or removed:
        await admin_audit(who, "connector.tools.drift", f"connector:{connector}", None,
                          {"added": added, "changed": changed, "removed": removed, "auto_approved": auto_approve},
                          tenant=tenant)
    return summary


async def refresh_tool_snapshot(
    tenant: str,
    connector: str,
    *,
    actor: dict[str, Any] | None = None,
    auto_approve: bool | None = None,
) -> dict[str, Any]:
    """Snapshot the connector's ``tools/list`` into ``connector_tool`` (connector credentials,
    reader principal for platform connectors, via egress). Returns the summary of
    :func:`upsert_tool_snapshot` (without ``definitions``).

    Called by the worker at startup (E2) and by ``POST /api/connectors/{slug}/refresh-tools``."""
    conn = await get_connector(tenant, connector)
    approve = conn.trust == "first_party" if auto_approve is None else auto_approve
    if conn.is_internal and not (conn.trust == "first_party" or auto_approve):
        approve = False
    lister = _tool_lister
    if lister is not None and conn.is_platform:
        from keelson_engine.core.tokens import get_token_broker

        token = await get_token_broker().service_token(tenant=tenant, connector=connector, principal="reader")
        tools = await lister(conn.url, token)
    else:
        from keelson_engine.core.mcp import list_remote_tools_with_headers

        headers = await headers_for(conn, "reader")
        if lister is not None:
            tools = await lister(conn.url, headers.get("Authorization", "").removeprefix("Bearer "))
        else:
            # internal services are reached directly on the internal network ("" = no egress proxy)
            tools = await list_remote_tools_with_headers(conn.url, headers, proxy="" if conn.is_internal else None)
    summary = await upsert_tool_snapshot(
        tenant, connector, tools, auto_approve=approve,
        untrusted=conn.untrusted_tools if conn.is_provisioned else (),
        has_webhooks=bool(conn.webhook_secret_ref), actor=actor, read_only=conn.is_internal,
    )
    summary.pop("definitions", None)
    log.info("tool snapshot refreshed", tenant=tenant, connector=connector, tools=summary["tools"],
             tiers=summary["tiers"])
    return summary


# ───────────────────────────── tenant LLM keys ─────────────────────────────

_LLM_KEY_TTL_S = 10.0
_llm_key_cache: dict[str, tuple[str, int, float]] = {}


def _llm_aad(tenant: str, alias_prefix: str) -> str:
    return f"llmkey|{tenant}|{alias_prefix}"


def llm_key_version_key(tenant: str) -> str:
    """Valkey key holding the tenant's current LLM key version (runner-cache invalidation)."""
    return f"keelson:tenant:{tenant}:llm_key_version"


async def store_tenant_llm_key(
    tenant: str, key: str, *, alias_prefix: str = "", key_alias: str | None = None, team_id: str | None = None,
    rotated: bool = False,
) -> int:
    """Store (encrypted) the tenant's virtual key; a different key bumps ``key_version``.
    Returns the version and publishes it to Valkey (best effort)."""
    enc = encrypt_str(key, _llm_aad(tenant, alias_prefix))
    previous = None
    try:
        previous = await _read_key_plain(tenant)
    except Exception:  # noqa: BLE001 - unreadable old key (data key rotated) counts as different
        previous = None
    async with system_session() as s:
        res = await s.execute(
            text(
                """
                INSERT INTO tenant_llm_key (tenant, alias_prefix, key_enc, key_id, litellm_key_alias, team_id,
                                            key_version, rotated_at)
                VALUES (:t, :p, :k, :kid, :alias, :team, 1, CASE WHEN :rot THEN now() END)
                ON CONFLICT (tenant) DO UPDATE SET alias_prefix = EXCLUDED.alias_prefix, key_enc = EXCLUDED.key_enc,
                  key_id = EXCLUDED.key_id, litellm_key_alias = EXCLUDED.litellm_key_alias,
                  team_id = EXCLUDED.team_id,
                  key_version = tenant_llm_key.key_version + CASE WHEN :changed THEN 1 ELSE 0 END,
                  rotated_at = CASE WHEN :rot THEN now() ELSE tenant_llm_key.rotated_at END,
                  updated_at = now()
                RETURNING key_version
                """
            ),
            {"t": tenant, "p": alias_prefix, "k": enc, "kid": DATA_KEY_ID, "alias": key_alias, "team": team_id,
             "rot": rotated, "changed": previous != key},
        )
        version = int(res.scalar_one())
    _llm_key_cache.pop(tenant, None)
    await _publish_key_version(tenant, version)
    return version


async def _publish_key_version(tenant: str, version: int) -> None:
    try:
        from keelson_engine.core.valkey import get_valkey

        await get_valkey().set(llm_key_version_key(tenant), str(version))
    except Exception as exc:  # noqa: BLE001 - the DB column stays the source of truth
        log.warning("llm key version not published", tenant=tenant, error=exc.__class__.__name__)


async def read_tenant_llm_key_row(tenant: str) -> dict[str, Any] | None:
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT alias_prefix, key_enc, key_id, litellm_key_alias, team_id, key_version, rotated_at "
                 "FROM tenant_llm_key")
        )
        m = res.mappings().first()
    return dict(m) if m is not None else None


async def _read_key_plain(tenant: str) -> str | None:
    row = await read_tenant_llm_key_row(tenant)
    if row is None:
        return None
    return decrypt_str(row["key_enc"], _llm_aad(tenant, row["alias_prefix"]))


async def get_tenant_llm_key(tenant: str) -> str:
    """The tenant's LiteLLM virtual key (decrypted; cached in-process for 10 s so a rotation
    reaches every process quickly)."""
    cached = _llm_key_cache.get(tenant)
    if cached and cached[2] > time.monotonic():
        return cached[0]
    row = await read_tenant_llm_key_row(tenant)
    if row is None:
        raise LlmKeyMissing(f"no LiteLLM key for tenant {tenant!r} (run bootstrap)")
    key = decrypt_str(row["key_enc"], _llm_aad(tenant, row["alias_prefix"]))
    _llm_key_cache[tenant] = (key, int(row.get("key_version") or 1), time.monotonic() + _LLM_KEY_TTL_S)
    return key


async def get_tenant_llm_key_version(tenant: str) -> int:
    """Current ``tenant_llm_key.key_version`` (0 when no key). The runtime compares it with the
    version its cached runner was built with and rebuilds on change. Reading a version that differs
    from the in-process key cache drops that cache entry, so the next :func:`get_tenant_llm_key`
    returns the rotated key (the old one is deleted in LiteLLM right after a rotation)."""
    row = await read_tenant_llm_key_row(tenant)
    version = int(row["key_version"]) if row is not None else 0
    cached = _llm_key_cache.get(tenant)
    if cached is not None and cached[1] != version:
        _llm_key_cache.pop(tenant, None)
    return version


def forget_tenant_llm_key(tenant: str) -> None:
    """Drop the in-process cached key of one tenant (runner rebuild after a rotation)."""
    _llm_key_cache.pop(tenant, None)


def clear_llm_key_cache() -> None:
    _llm_key_cache.clear()


# ───────────────────────────── M7 tier-A mappings (runtime helpers) ─────────────────────────────


async def get_action_mapping(tenant: str, connector: str) -> Any:
    """The connector's active ``keelson_contracts.mapping.ActionMapping`` (``None`` = every op is
    guided). Thin wrapper over :mod:`keelson_engine.core.connector_mappings` for the runtime port."""
    from keelson_engine.core.connector_mappings import get_action_mapping as _get

    return await _get(tenant, connector)


async def get_read_mapping(tenant: str, connector: str) -> Any:
    """The connector's active ``ReadMapping`` (``None`` without one)."""
    from keelson_engine.core.connector_mappings import get_read_mapping as _get

    return await _get(tenant, connector)


async def get_capabilities(tenant: str, connector: str) -> dict[str, Any]:
    """``ConnectorCapabilities`` JSON (sync snapshot|crawl|none, commit change_set|per_op|guide, advisory)."""
    from keelson_engine.core.connector_mappings import capabilities

    return await capabilities(tenant, connector)
