"""Tenant provisioning (``docs/contracts/70-backoffice-m2.md`` §4): tenant row, LiteLLM team +
virtual key, connectors from the template pack, agent spec ``v1`` and the ``tenant`` attribute of
the named Keycloak users. Every step is idempotent, so a failed creation can simply be retried
with the same pack (a different pack for an existing slug is 409).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core import agent_specs
from keelson_engine.core import connectors as reg
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session
from keelson_engine.core.keycloak_admin import KeycloakAdmin, KeycloakAdminError
from keelson_engine.core.litellm_admin import LiteLLMAdmin
from keelson_engine.core.logs import get_logger
from keelson_engine.core.packs import Pack, PackError, load_pack
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "LIMIT_KEYS",
    "apply_pack",
    "create_tenant",
    "effective_limits",
    "forget_suspension_cache",
    "is_suspended",
    "list_tenants",
    "provision_litellm",
    "set_keycloak_admin",
    "set_status",
    "suspended_tenants",
]

log = get_logger(__name__)

TEAM_PREFIX = "keelson-"
LIMIT_KEYS = ("budget_eur", "budget_duration", "rpm_limit", "tpm_limit")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

_kc: KeycloakAdmin | None = None


def set_keycloak_admin(kc: KeycloakAdmin | None) -> None:
    global _kc
    _kc = kc


def _keycloak() -> KeycloakAdmin:
    return _kc or KeycloakAdmin()


def effective_limits(*layers: dict[str, Any] | None) -> dict[str, Any]:
    """Later layers win: e.g. ``(pack.budgets, tenants.yaml entry, tenant.admin_overrides)``."""
    out: dict[str, Any] = {}
    for layer in layers:
        for k in LIMIT_KEYS:
            if layer and layer.get(k) is not None:
                out[k] = layer[k]
    return out


async def provision_litellm(admin: LiteLLMAdmin, tenant: str, name: str, limits: dict[str, Any],
                            alias_names: list[str]) -> dict[str, str]:
    """Team ``keelson-<tenant>`` (budget/rpm/tpm) + one virtual key (reused while valid, its
    model allow-list re-synced; else generated) stored encrypted."""
    team_id = f"{TEAM_PREFIX}{tenant}"
    existing_team = await admin.team_info(team_id)
    current = [str(m) for m in (existing_team or {}).get("models") or []]
    if current and "all-proxy-models" not in current:
        # keep aliases granted in the back-office (PUT /gateway/models {grant_all_tenants}); a
        # bootstrap re-run must not revoke them (aliases are revoked by DELETE /gateway/models)
        alias_names = sorted({*alias_names, *current})
    team_body: dict[str, Any] = {
        "team_alias": f"Keelson {name}"[:120],
        "models": alias_names,
        "metadata": {"keelson_tenant": tenant},
    }
    if limits.get("budget_eur") is not None:
        team_body["max_budget"] = float(limits["budget_eur"])
        team_body["budget_duration"] = str(limits.get("budget_duration") or "30d")
    if limits.get("rpm_limit") is not None:
        team_body["rpm_limit"] = int(limits["rpm_limit"])
    if limits.get("tpm_limit") is not None:
        team_body["tpm_limit"] = int(limits["tpm_limit"])
    team_result = await admin.upsert_team(team_id, team_body)

    key_alias = f"keelson-{tenant}"
    row = await reg.read_tenant_llm_key_row(tenant)
    if row is not None:
        try:
            existing = await reg.get_tenant_llm_key(tenant)
        except Exception:  # noqa: BLE001 - unreadable (e.g. data key rotated) → regenerate
            existing = ""
        if existing and await admin.key_valid(existing):
            await admin.update_key(existing, {"team_id": team_id, "models": alias_names})
            return {"team": team_result, "key": "reused"}
        if row.get("litellm_key_alias") and row["litellm_key_alias"] != key_alias:
            await admin.delete_keys_by_alias(str(row["litellm_key_alias"]))
    await admin.delete_keys_by_alias(key_alias)
    key = await admin.generate_key({
        "team_id": team_id,
        "key_alias": key_alias,
        "models": alias_names,
        "metadata": {"keelson_tenant": tenant, "purpose": "engine runtime"},
    })
    await reg.store_tenant_llm_key(tenant, key, key_alias=key_alias, team_id=team_id)
    return {"team": team_result, "key": "generated"}


async def apply_pack(tenant: str, pack: Pack, *, actor: dict[str, Any] | str,
                     skip_connectors: set[str] | None = None) -> dict[str, Any]:
    """Connectors of the pack that the tenant does not have yet (platform → ``active``, else
    ``draft``) and spec ``v1`` when the tenant has no spec version. Idempotent."""
    created_connectors: list[str] = []
    for spec in pack.connectors:
        if skip_connectors and spec["slug"] in skip_connectors:
            continue
        if await reg.upsert_connector(tenant, spec, overwrite=False):
            created_connectors.append(spec["slug"])
    version = await agent_specs.create_initial(tenant, pack.spec_overrides, actor=actor,
                                               summary=f"initial (pack {pack.name})")
    return {"connectors_created": created_connectors, "spec_version": version}


def _alias_names() -> list[str] | None:
    path = Path(get_settings().models_lock)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return None
    aliases = data.get("aliases")
    return sorted(aliases) if isinstance(aliases, dict) and aliases else None


async def _tenant_row(slug: str) -> dict[str, Any] | None:
    async with system_session() as s:
        m = (await s.execute(text("SELECT slug, name, config, admin_overrides, created_at FROM tenant WHERE slug = :s"),
                             {"s": slug})).mappings().first()
    return dict(m) if m is not None else None


async def create_tenant(body: dict[str, Any], actor: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    from keelson_engine.core import gateway

    slug = str(body.get("slug") or "")
    name = str(body.get("name") or "").strip()
    pack_name = str(body.get("pack") or "")
    usernames = body.get("admin_usernames") or []
    if not SLUG_RE.match(slug):
        raise AdminError(422, "invalid_slug", "slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
    if not name or len(name) > 200:
        raise AdminError(422, "invalid_name", "name is required (at most 200 characters)")
    if not isinstance(usernames, list) or len(usernames) > 20 or not all(
            isinstance(u, str) and USERNAME_RE.match(u) for u in usernames):
        raise AdminError(422, "invalid_admin_usernames", "admin_usernames must be a list of usernames")
    try:
        pack = load_pack(pack_name)
    except PackError as exc:
        raise AdminError(422, "unknown_pack", str(exc)) from exc

    existing = await _tenant_row(slug)
    created = existing is None
    if existing is not None:
        if (existing.get("config") or {}).get("pack") != pack.name:
            raise AdminError(409, "tenant_exists", f"tenant {slug!r} already exists with another pack")
        name = existing["name"]
    else:
        limits = effective_limits(pack.budgets)
        async with system_session() as s:
            await s.execute(
                text("INSERT INTO tenant (slug, name, config) VALUES (:s, :n, CAST(:c AS jsonb)) "
                     "ON CONFLICT (slug) DO NOTHING"),
                {"s": slug, "n": name, "c": dumps({"pack": pack.name, **limits, "created_by": actor.get("username")})},
            )
    overrides = dict((existing or {}).get("admin_overrides") or {})
    limits = effective_limits(pack.budgets, (existing or {}).get("config"), overrides)
    admin = gateway.get_admin()
    try:
        aliases = _alias_names() or sorted({str(m.get("model_name")) for m in await admin.models()
                                            if m.get("model_name")})
        litellm = await provision_litellm(admin, slug, name, limits, aliases)
    except AdminError:
        raise
    except Exception as exc:  # noqa: BLE001 - LiteLLM down: the row exists; a retry completes it
        log.warning("tenant provisioning: litellm failed", tenant=slug, error=str(exc)[:300])
        raise AdminError(502, "gateway_unavailable",
                         "The AI gateway could not provision the tenant; retry with the same request.") from exc
    applied = await apply_pack(slug, pack, actor=actor)
    admins: list[dict[str, str]] = []
    realm = get_settings().keelson_realm
    for username in usernames:
        try:
            status = await _keycloak().add_attribute_value(realm, username, "tenant", slug)
        except KeycloakAdminError as exc:
            log.warning("tenant provisioning: keycloak failed", tenant=slug, username=username, error=str(exc))
            status = "failed"
        admins.append({"username": username, "status": status})
    connectors_now = [{"slug": c.slug, "status": c.status} for c in await reg.list_connectors(slug)]
    result = {
        "slug": slug,
        "name": name,
        "pack": pack.name,
        "created": created,
        "team_id": f"{TEAM_PREFIX}{slug}",
        "litellm": litellm,
        "connectors": connectors_now,
        "spec_version": await agent_specs.latest_version_name(slug),
        "admins": admins,
    }
    await admin_audit(actor, "tenant.create" if created else "tenant.reprovision", f"tenant:{slug}", None,
                      {"pack": pack.name, "connectors_created": applied["connectors_created"],
                       "spec_version": result["spec_version"], "admins": admins}, tenant=slug)
    return result, created


async def list_tenants() -> list[dict[str, Any]]:
    async with system_session() as s:
        rows = (await s.execute(text(
            """
            SELECT t.slug, t.name, t.config, t.created_at, t.status, t.suspended_at, t.suspended_by,
                   (SELECT count(*) FROM connector c WHERE c.tenant = t.slug) AS connectors,
                   (SELECT a.version FROM agent_spec a WHERE a.tenant = t.slug
                     ORDER BY a.seq DESC NULLS LAST, a.created_at DESC LIMIT 1) AS spec_version
            FROM tenant t ORDER BY t.slug
            """
        ))).mappings().all()
    return [
        {"slug": r["slug"], "name": r["name"], "pack": (r["config"] or {}).get("pack"),
         "connectors": int(r["connectors"]), "spec_version": r["spec_version"] or agent_specs.DEFAULT_VERSION,
         "created_at": r["created_at"].isoformat() if r["created_at"] else None,
         "status": r["status"], "suspended_at": r["suspended_at"].isoformat() if r["suspended_at"] else None,
         "suspended_by": r["suspended_by"]}
        for r in rows
    ]


# ───────────────────────────── M7: suspension (110 §2) ─────────────────────────────

_SUSPENDED_TTL_S = 5.0
_suspended_cache: dict[str, tuple[float, bool]] = {}


def forget_suspension_cache() -> None:
    _suspended_cache.clear()


async def is_suspended(slug: str) -> bool:
    """Whether the tenant is suspended (cached 5 s per process; a suspension takes effect on every
    replica within that time, and at once in the process that made it)."""
    import time

    now = time.monotonic()
    hit = _suspended_cache.get(slug)
    if hit is not None and now - hit[0] < _SUSPENDED_TTL_S:
        return hit[1]
    async with system_session() as s:
        row = (await s.execute(text("SELECT status FROM tenant WHERE slug = :s"), {"s": slug})).first()
    suspended = row is not None and row[0] == "suspended"
    _suspended_cache[slug] = (now, suspended)
    return suspended


async def suspended_tenants(slugs: list[str]) -> set[str]:
    if not slugs:
        return set()
    async with system_session() as s:
        rows = (await s.execute(text("SELECT slug FROM tenant WHERE slug = ANY(:s) AND status = 'suspended'"),
                                {"s": list(slugs)})).all()
    return {r[0] for r in rows}


async def set_status(slug: str, status: str, actor: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    """Platform-admin: suspend / reactivate a tenant. A suspended tenant's users cannot sign in to it
    (the BFF drops it from new sessions) and every tenant-scoped API call answers 403
    ``tenant_suspended``; its Keelson sessions are ended at once (platform-admins keep theirs)."""
    if status not in ("active", "suspended"):
        raise AdminError(422, "invalid_status", "status must be active or suspended")
    row = await _tenant_row(slug)
    if row is None:
        raise AdminError(404, "not_found", "tenant not found")
    async with system_session() as s:
        before = (await s.execute(text("SELECT status FROM tenant WHERE slug = :s FOR UPDATE"), {"s": slug})).scalar_one()
        if before == status:
            return {"slug": slug, "status": status, "unchanged": True}
        await s.execute(
            text("UPDATE tenant SET status = :st, suspended_at = CASE WHEN :st = 'suspended' THEN now() END, "
                 "suspended_by = CASE WHEN :st = 'suspended' THEN :by END, updated_at = now() WHERE slug = :s"),
            {"st": status, "by": actor.get("username"), "s": slug},
        )
        ended = 0
        if status == "suspended":
            # end the sessions whose active tenant is the suspended one (platform-admins keep theirs)
            res = await s.execute(
                text("DELETE FROM bff_session WHERE active_tenant = :s "
                     "AND NOT (roles @> CAST(:pa AS jsonb))"),
                {"s": slug, "pa": '["platform-admin"]'})
            ended = int(res.rowcount or 0)
    _suspended_cache.pop(slug, None)
    await admin_audit(actor, "tenant.suspend" if status == "suspended" else "tenant.reactivate", f"tenant:{slug}",
                      {"status": before}, {"status": status, "reason": (reason or "")[:500] or None,
                                           "sessions_ended": ended}, tenant=slug)
    return {"slug": slug, "status": status, "sessions_ended": ended}
