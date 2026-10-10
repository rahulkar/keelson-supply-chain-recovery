"""Signal Hub client (``docs/contracts/100-m6-signals.md`` §1, §5–§7) — the engine's only way in.

Every call carries a short-lived **service JWT** (:func:`mint_hub_token`): HS256 with
``SIGNAL_HUB_SHARED_SECRET``, ``{iss: keelson-engine, aud: signal-hub, sub: keelson-engine, tenant,
iat, exp, jti}``. The ``tenant`` claim scopes every tenant read/write on the hub; the engine only
ever mints it for the tenant of the request it serves (session tenant, webhook path tenant,
demo tenant, the tenant whose snapshot was synced). M8 (``docs/contracts/123``): ``platform=True``
adds ``scope: "platform"`` — minted only after the engine's own platform-admin check, and honoured
by the hub only on its platform clustering routes (``PUT /v1/clustering/config``,
``POST /v1/clustering/run``).

Traffic goes over the internal network (``internal_http``, no egress proxy): the hub is a
first-party service with no public route. Ids that end up in URL paths are validated first
(canonical lowercase UUIDs, pack ids, tenant slugs), so a path can never be steered elsewhere.

Endpoints used (hub side, all with the service JWT):

==========================================  =================================================
``GET  /v1/alerts?status=&limit=``          tenant alerts (``Alert`` list, or ``{items: [...]}``)
``GET  /v1/alerts/{id}``                    ``AlertDetail`` (event, member signals, exposures)
``POST /v1/alerts/{id}/status``             ``AlertStatusUpdate`` → the updated ``Alert``
``GET  /v1/events/{id}``                    ``DisruptionEvent``
``PUT  /v1/tenants/{tenant}/network``       ``TenantNetwork`` (after every scn sync)
``POST /v1/ingest/connector-event``         forwarded connector webhooks (see :func:`ingest_connector_event`)
``POST /v1/demo/packs/{pack}/{action}``     ``start | advance | reset`` (DEMO_MODE, presenter proxy)
``POST /v1/demo/reset``                     the demo reset step: packs + the tenant's alerts
==========================================  =================================================

Responses are validated against the ``keelson_contracts.signals`` models; rows of another tenant
are dropped (defence in depth: the hub scopes by the token's tenant claim already).
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Final

import httpx
import jwt
from pydantic import ValidationError

from keelson_contracts.signals import (
    PACK_ID_PATTERN,
    UUID_PATTERN,
    Alert,
    AlertDetail,
    AlertRule,
    AlertStatusUpdate,
    DisruptionEvent,
    TenantNetwork,
)
from keelson_engine.config import get_settings
from keelson_engine.core.db import check_tenant_slug
from keelson_engine.core.logs import get_logger

__all__ = [
    "DEMO_PACK_ACTIONS",
    "HUB_AUDIENCE",
    "HUB_ISSUER",
    "HubError",
    "HubNotConfigured",
    "HubNotFound",
    "HubRejected",
    "HubUnavailable",
    "INTERNAL_UNTRUSTED_TOOLS",
    "demo_pack",
    "demo_reset",
    "get_alert",
    "get_event",
    "ingest_connector_event",
    "internal_connector_spec",
    "is_configured",
    "is_hub_id",
    "is_pack_id",
    "list_alerts",
    "mint_hub_token",
    "put_alert_rules",
    "put_network",
    "register_internal_connector",
    "request_json",
    "set_alert_status",
]

log = get_logger(__name__)

HUB_ISSUER: Final = "keelson-engine"
HUB_AUDIENCE: Final = "signal-hub"
DEMO_PACK_ACTIONS: Final = ("start", "advance", "reset")
_PACK_RE: Final = re.compile(PACK_ID_PATTERN)
_UUID_RE: Final = re.compile(UUID_PATTERN)
MAX_LIST_LIMIT: Final = 500
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
INTERNAL_UNTRUSTED_TOOLS: Final = ("search_signals", "get_event", "hazards_near_node")
"""Hub MCP tools whose results carry free text (news titles/summaries, ``raw_text``): classified
``untrusted_text`` (the runtime quarantines them). ``assess_exposure`` returns structured exposure only."""


class HubError(Exception):
    """A Signal Hub call failed. ``status`` = the hub's HTTP status (``None`` = no answer)."""

    code = "signal_hub_error"

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HubNotConfigured(HubError):
    code = "signal_hub_not_configured"


class HubUnavailable(HubError):
    """Transport error, timeout, 5xx/429 or an answer that does not match the contract."""

    code = "signal_hub_unavailable"


class HubNotFound(HubError):
    code = "not_found"


class HubRejected(HubError):
    """A 4xx other than 404/429 (the request itself was refused, e.g. 409/422)."""

    code = "signal_hub_rejected"


def is_configured() -> bool:
    return get_settings().signal_hub_enabled


def is_pack_id(value: object) -> bool:
    return isinstance(value, str) and bool(_PACK_RE.fullmatch(value))


def is_hub_id(value: object) -> bool:
    """A canonical lowercase UUID — ``fullmatch``: unlike ``re.match`` with a ``$`` pattern (and
    ``keelson_contracts.signals.is_uuid``) a trailing newline is rejected, since ids go into paths."""
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def mint_hub_token(tenant: str, *, ttl_s: int | None = None, now: int | None = None, platform: bool = False) -> str:
    """Service JWT for one tenant (see module docstring); ``platform`` adds ``scope: "platform"``
    (callers check the platform-admin role first). Raises :class:`HubNotConfigured`."""
    st = get_settings()
    secret = st.signal_hub_shared_secret.get_secret_value()
    if not secret:
        raise HubNotConfigured("SIGNAL_HUB_SHARED_SECRET is not configured")
    check_tenant_slug(tenant)
    issued = int(time.time()) if now is None else now
    ttl = st.signal_hub_token_ttl_s if ttl_s is None else ttl_s
    claims = {"iss": HUB_ISSUER, "aud": HUB_AUDIENCE, "sub": HUB_ISSUER, "tenant": tenant, "iat": issued,
              "exp": issued + max(5, min(int(ttl), 600)), "jti": uuid.uuid4().hex}
    if platform:
        claims["scope"] = "platform"
    return jwt.encode(claims, secret, algorithm="HS256")


def _base() -> str:
    st = get_settings()
    if not st.signal_hub_enabled:
        raise HubNotConfigured("the Signal Hub is not configured (SIGNAL_HUB_URL / SIGNAL_HUB_SHARED_SECRET)")
    return st.signal_hub_url


def _uuid_or_not_found(value: str, what: str) -> str:
    if not is_hub_id(value):
        raise HubNotFound(f"{what} not found", status=404)
    return value


async def _request(method: str, path: str, *, tenant: str, json_body: Any = None,
                   params: dict[str, Any] | None = None, timeout: float | None = None,
                   allow_empty: bool = False, platform: bool = False) -> Any:
    from keelson_engine.core.http import internal_http

    url = _base() + path
    headers = {"Authorization": f"Bearer {mint_hub_token(tenant, platform=platform)}", "Accept": "application/json"}
    t = get_settings().signal_hub_timeout_s if timeout is None else timeout
    try:
        resp = await internal_http().request(method, url, headers=headers, json=json_body, params=params,
                                             timeout=httpx.Timeout(t, connect=min(t, 5.0)))
    except httpx.HTTPError as exc:
        raise HubUnavailable(f"Signal Hub unreachable ({type(exc).__name__})") from exc
    status = resp.status_code
    body: Any = None
    if resp.content:
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise HubUnavailable("Signal Hub answer too large", status=status)
        try:
            body = resp.json()
        except ValueError:
            body = None
    if status == 404:
        raise HubNotFound("not found on the Signal Hub", status=status, body=body)
    if 300 <= status < 400:  # redirects are never followed (nor treated as an answer)
        raise HubUnavailable(f"Signal Hub answered HTTP {status} (redirect)", status=status)
    if status == 429 or status >= 500:
        raise HubUnavailable(f"Signal Hub answered HTTP {status}", status=status, body=body)
    if status >= 400:
        raise HubRejected(f"Signal Hub refused the request (HTTP {status})", status=status, body=body)
    if body is None and not allow_empty and status != 204:
        raise HubUnavailable("Signal Hub answered without a JSON body", status=status)
    return body


_PATH_RE: Final = re.compile(r"/v1(/[a-z0-9][a-z0-9_.:-]*)+")


async def request_json(method: str, path: str, *, tenant: str, json_body: Any = None,
                       params: dict[str, Any] | None = None, allow_empty: bool = False,
                       platform: bool = False) -> Any:
    """A hub call for the engine's admin proxies (M7 Signals admin): ``path`` must be a plain
    ``/v1/...`` path of lowercase segments (no ``..``, no query); the ``tenant`` claim scopes it.
    ``platform`` mints the platform-scoped token (M8 clustering settings; platform admins only)."""
    if method not in ("GET", "PUT", "POST") or not _PATH_RE.fullmatch(path) or ".." in path:
        raise ValueError(f"invalid Signal Hub path {path[:80]!r}")
    return await _request(method, path, tenant=tenant, json_body=json_body, params=params, allow_empty=allow_empty,
                          platform=platform)


def _items(body: Any) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("items", "alerts"):
            if isinstance(body.get(key), list):
                return list(body[key])
    raise HubUnavailable("unexpected Signal Hub alert list shape")


# ───────────────────────────── alerts / events ─────────────────────────────


async def list_alerts(tenant: str, *, status: str | None = None, limit: int = 50) -> list[Alert]:
    """The tenant's alerts (hub order, newest first). Invalid rows / rows of another tenant are
    dropped and logged, never passed on."""
    params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_LIST_LIMIT))}
    if status:
        params["status"] = status
    body = await _request("GET", "/v1/alerts", tenant=tenant, params=params)
    out: list[Alert] = []
    dropped = 0
    for raw in _items(body):
        try:
            alert = Alert.model_validate(raw)
        except ValidationError:
            dropped += 1
            continue
        if alert.tenant != tenant:
            dropped += 1
            continue
        out.append(alert)
    if dropped:
        log.warning("signal hub alerts dropped (invalid or foreign)", tenant=tenant, dropped=dropped)
    return out


async def get_alert(tenant: str, alert_id: str) -> AlertDetail:
    _uuid_or_not_found(alert_id, "alert")
    body = await _request("GET", f"/v1/alerts/{alert_id}", tenant=tenant)
    try:
        detail = AlertDetail.model_validate(body)
    except ValidationError as exc:
        raise HubUnavailable("Signal Hub alert detail does not match the contract") from exc
    if detail.alert.tenant != tenant or detail.alert.id != alert_id:
        raise HubNotFound("alert not found", status=404)
    return detail


async def set_alert_status(tenant: str, alert_id: str, update: AlertStatusUpdate) -> Alert | None:
    """Returns the updated alert when the hub sends it back (``None`` for an empty answer)."""
    _uuid_or_not_found(alert_id, "alert")
    body = await _request("POST", f"/v1/alerts/{alert_id}/status", tenant=tenant,
                          json_body=update.model_dump(mode="json", exclude_none=True), allow_empty=True)
    if body is None:
        return None
    raw = body.get("alert", body) if isinstance(body, dict) else body
    try:
        alert = Alert.model_validate(raw)
    except ValidationError:
        return None
    return alert if alert.tenant == tenant and alert.id == alert_id else None


async def get_event(tenant: str, event_id: str) -> DisruptionEvent:
    _uuid_or_not_found(event_id, "event")
    body = await _request("GET", f"/v1/events/{event_id}", tenant=tenant)
    try:
        event = DisruptionEvent.model_validate(body.get("event", body) if isinstance(body, dict) else body)
    except ValidationError as exc:
        raise HubUnavailable("Signal Hub event does not match the contract") from exc
    if event.tenant not in (None, tenant) or event.id != event_id:
        raise HubNotFound("event not found", status=404)
    return event


# ───────────────────────────── tenant network / ingest ─────────────────────────────


async def put_network(tenant: str, network: TenantNetwork) -> Any:
    check_tenant_slug(tenant)
    return await _request("PUT", f"/v1/tenants/{tenant}/network", tenant=tenant,
                          json_body=network.model_dump(mode="json"), allow_empty=True,
                          timeout=max(get_settings().signal_hub_timeout_s, 30.0))


async def put_alert_rules(tenant: str, rules: list[AlertRule]) -> Any:
    """``PUT /v1/tenants/{tenant}/alert-rules {rules}`` (hub extension; ``[]`` = the hub's defaults).
    Keeps the hub's alert decisions and the engine's auto-open decision on the same rule set."""
    check_tenant_slug(tenant)
    return await _request("PUT", f"/v1/tenants/{tenant}/alert-rules", tenant=tenant,
                          json_body={"rules": [r.model_dump(mode="json") for r in rules]}, allow_empty=True)


async def ingest_connector_event(tenant: str, *, connector: str, event_id: str, event_type: str,
                                 received_at: str | None, payload: dict[str, Any],
                                 timeout: float | None = None) -> Any:
    """Forward one stored connector webhook as the hub's ``ConnectorEvent`` — the delivery's
    envelope ``{connector_id, type, id, timestamp, data}`` (Merxa ``22-merxa-m2.md`` §5; the hub
    reads exactly these fields) plus ``event_id`` (the ``webhook-id``) and ``received_at`` for
    tracing. ``id`` is the envelope's own id, else the ``webhook-id``; ``timestamp`` the envelope's
    when it is an ISO time with an offset, else ``received_at``. The tenant is the token's claim,
    taken from the webhook path. Structured fields are facts on the hub, free text goes to its
    quarantined extractor."""
    native = payload.get("id")
    stamp = payload.get("timestamp")
    data = payload.get("data")
    body = {
        "connector_id": connector,
        "type": event_type,
        "id": native if isinstance(native, str) and 0 < len(native) <= 200 else event_id,
        "timestamp": stamp if _aware_iso(stamp) else received_at,
        "data": data if isinstance(data, dict) else {},
        "event_id": event_id,
        "received_at": received_at,
    }
    return await _request("POST", "/v1/ingest/connector-event", tenant=tenant, json_body=body,
                          allow_empty=True, timeout=timeout)


def _aware_iso(value: object) -> bool:
    """An ISO-8601 date-time string with a UTC offset (what the hub's ``AwareDatetime`` accepts)."""
    import datetime as dt

    if not isinstance(value, str) or not 10 <= len(value) <= 64:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


# ───────────────────────────── demo (DEMO_MODE) ─────────────────────────────


async def demo_pack(tenant: str, pack: str, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if action not in DEMO_PACK_ACTIONS:
        raise HubNotFound("unknown demo pack action", status=404)
    if not is_pack_id(pack):
        raise HubNotFound("demo pack not found", status=404)
    out = await _request("POST", f"/v1/demo/packs/{pack}/{action}", tenant=tenant, json_body=body or {},
                         allow_empty=True, timeout=max(get_settings().signal_hub_timeout_s, 30.0))
    return out if isinstance(out, dict) else {}


async def list_demo_packs(tenant: str) -> list[dict[str, Any]]:
    """The presenter's pack list: ``GET /v1/demo/packs`` plus each pack's replay state for this
    tenant (``GET /v1/demo/packs/{id}``). Only structured fields pass (id, title, description, step
    counts, status); packs the hub could not load are skipped."""
    timeout = max(get_settings().signal_hub_timeout_s, 10.0)
    listing = await _request("GET", "/v1/demo/packs", tenant=tenant, timeout=timeout)
    raw_packs = listing.get("packs") if isinstance(listing, dict) else listing
    if not isinstance(raw_packs, list):
        raise HubUnavailable("unexpected Signal Hub pack list shape")

    def text(value: Any, limit: int) -> str | None:
        return value[:limit] if isinstance(value, str) and value.strip() else None

    def count(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    out: list[dict[str, Any]] = []
    for entry in raw_packs[:50]:
        if not isinstance(entry, dict) or entry.get("error") or not is_pack_id(entry.get("id")):
            continue
        pack = str(entry["id"])
        try:
            state = await _request("GET", f"/v1/demo/packs/{pack}", tenant=tenant, timeout=timeout)
        except HubNotFound:
            continue
        state = state if isinstance(state, dict) else {}
        out.append({
            "id": pack,
            "title": text(entry.get("title"), 200) or pack,
            "description": text(entry.get("description"), 2000),
            "status": text(state.get("status"), 40),
            "steps_applied": count(state.get("steps_applied")),
            "steps_total": count(state.get("steps_total")) or count(entry.get("steps")),
        })
    return out


async def demo_reset(tenant: str) -> dict[str, Any]:
    """The demo reset's hub step: ``POST /v1/demo/reset`` (all packs + the tenant's alerts). A hub
    without that endpoint (404) is reset pack by pack instead: ``GET /v1/demo/packs`` then
    ``POST /v1/demo/packs/{id}/reset`` for each (that deletes each pack's events, signals and —
    cascading — their alerts). Returns ``{counts: {...}}``."""
    timeout = max(get_settings().signal_hub_timeout_s, 30.0)
    try:
        out = await _request("POST", "/v1/demo/reset", tenant=tenant, json_body={}, allow_empty=True,
                             timeout=timeout)
        return out if isinstance(out, dict) else {}
    except HubNotFound:
        pass
    listing = await _request("GET", "/v1/demo/packs", tenant=tenant, timeout=timeout)
    raw_packs = listing.get("packs") if isinstance(listing, dict) else listing
    if not isinstance(raw_packs, list):
        raise HubUnavailable("unexpected Signal Hub pack list shape")
    counts: dict[str, int] = {"packs": 0}
    for entry in raw_packs:
        pack = entry.get("id") if isinstance(entry, dict) else entry
        if not is_pack_id(pack) or (isinstance(entry, dict) and entry.get("error")):
            continue
        res = await _request("POST", f"/v1/demo/packs/{pack}/reset", tenant=tenant, json_body={},
                             allow_empty=True, timeout=timeout)
        counts["packs"] += 1
        deleted = res.get("deleted") if isinstance(res, dict) else None
        if isinstance(deleted, dict):
            for key, value in deleted.items():
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[key] = counts.get(key, 0) + value
    return {"counts": counts}


# ───────────────────────────── internal connector ─────────────────────────────


def internal_connector_spec() -> dict[str, Any]:
    """The Signal Hub as an internal first-party connector (``auth: service_jwt``): its MCP endpoint
    ``<SIGNAL_HUB_URL>/mcp``; read-only tools auto-approved; text-bearing tools ``untrusted_text``."""
    st = get_settings()
    return {
        "slug": st.signal_hub_connector_slug,
        "name": "Signal Hub",
        "mcp_url": f"{_base()}/mcp",
        "issuer": "",
        "trust_level": "first_party",
        "auth": {"kind": "service_jwt"},
        "untrusted_tools": list(INTERNAL_UNTRUSTED_TOOLS),
        "vendor_name": "Signal Hub",
        "internal": True,
    }


async def register_internal_connector(tenant: str) -> bool:
    """Upsert the tenant's internal Signal Hub connector (bootstrap). Returns True when inserted."""
    from keelson_engine.core import connectors

    spec = internal_connector_spec()
    try:
        existing = await connectors.get_connector(tenant, spec["slug"])
    except connectors.ConnectorNotFound:
        existing = None
    if existing is not None and not existing.is_internal:
        # never adopt a tenant-registered connector of the same slug (its URL, secret and reviewed
        # tool classifications are not the hub's); an operator must rename or delete it first
        log.error("signal hub connector slug taken by a tenant connector; not registered", tenant=tenant,
                  slug=spec["slug"], auth_kind=existing.auth_kind)
        return False
    inserted = await connectors.upsert_connector(tenant, spec)
    log.info("signal hub connector registered", tenant=tenant, inserted=inserted)
    return inserted
