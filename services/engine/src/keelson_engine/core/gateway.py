"""AI Gateway facade over the LiteLLM management API (``docs/contracts/70-backoffice-m2.md`` §2).

The master key stays server-side; tenant-admins only see their own team (``keelson-<tenant>``,
pinned from the session), platform-admins manage the global aliases.

Alias rules (:func:`normalize_alias`): ``^[a-z0-9][a-z0-9-]*$`` or ``anthropic/<that>``; the
``anthropic/`` prefix is added automatically when the provider model is a Claude backend
(``anthropic/…``, ``bedrock/…claude…``, ``vertex_ai/claude…``), which must also set
``reasoning_effort`` and ``max_tokens >= 16000``; never ``gemini/…`` / ``openai/…``.

Capability badges from the provider family: gemini ✓✓✓, anthropic ✓✓✓, openai ✓✓✗
(``structured_output``, ``reasoning_stream``, ``tool_loop_signatures``); embeddings ✗✗✗.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
from typing import Any

from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core import connectors as reg
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session
from keelson_engine.core.http import internal_http
from keelson_engine.core.litellm_admin import LiteLLMAdmin, LiteLLMError
from keelson_engine.core.logs import get_logger
from keelson_engine.core.repo import admin_audit
from keelson_engine.core.sql import dumps

__all__ = [
    "TEAM_PREFIX",
    "capabilities",
    "delete_model",
    "get_admin",
    "list_models",
    "normalize_alias",
    "patch_tenant",
    "provider_family",
    "rotate_key",
    "set_admin",
    "team_id",
    "tenant_models",
    "tenant_view",
    "upsert_model",
    "usage",
]

log = get_logger(__name__)

TEAM_PREFIX = "keelson-"
ALIAS_RE = re.compile(r"^(anthropic/)?[a-z0-9][a-z0-9-]*$")
MODES = ("chat", "embedding", "responses", "completion")
_SECRET_PARAM_KEYS = ("api_key", "aws_secret_access_key", "aws_access_key_id", "aws_session_token",
                      "azure_ad_token", "client_secret", "vertex_credentials", "api_base")

_admin: LiteLLMAdmin | None = None


def get_admin() -> LiteLLMAdmin:
    global _admin
    if _admin is None:
        s = get_settings()
        return LiteLLMAdmin(s.litellm_base_url, s.litellm_master_key.get_secret_value(), internal_http())
    return _admin


def set_admin(admin: LiteLLMAdmin | None) -> None:
    global _admin
    _admin = admin


def team_id(tenant: str) -> str:
    return f"{TEAM_PREFIX}{tenant}"


def _unavailable(exc: Exception) -> AdminError:
    log.warning("litellm management call failed", error=str(exc)[:300])
    return AdminError(502, "gateway_unavailable", "The AI gateway could not complete the request.")


# ───────────────────────────── alias rules / capabilities ─────────────────────────────


def _is_claude(provider_model: str) -> bool:
    pm = provider_model.lower()
    return (
        pm.startswith("anthropic/")
        or (pm.startswith("bedrock/") and "claude" in pm)
        or pm.startswith("vertex_ai/claude")
    )


def provider_family(provider_model: str | None, alias: str = "") -> str:
    pm = (provider_model or "").lower()
    if alias.startswith("anthropic/") or _is_claude(pm):
        return "anthropic"
    if pm.startswith("gemini/") or (pm.startswith("vertex_ai/") and "gemini" in pm):
        return "gemini"
    if pm.startswith(("openai/", "azure/")) or pm.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "other"


def capabilities(alias: str, provider_model: str | None, mode: str | None) -> dict[str, bool]:
    if (mode or "chat") == "embedding":
        return {"structured_output": False, "reasoning_stream": False, "tool_loop_signatures": False}
    family = provider_family(provider_model, alias)
    if family in ("gemini", "anthropic"):
        return {"structured_output": True, "reasoning_stream": True, "tool_loop_signatures": True}
    if family == "openai":
        return {"structured_output": True, "reasoning_stream": True, "tool_loop_signatures": False}
    return {"structured_output": True, "reasoning_stream": False, "tool_loop_signatures": False}


def normalize_alias(alias: str, provider_model: str) -> str:
    alias = (alias or "").strip()
    if alias.startswith(("gemini/", "openai/")):
        raise AdminError(422, "invalid_alias", "aliases never start with gemini/ or openai/")
    if _is_claude(provider_model) and not alias.startswith("anthropic/"):
        alias = f"anthropic/{alias}"
    if not ALIAS_RE.match(alias) or len(alias) > 80:
        raise AdminError(422, "invalid_alias", "alias must match ^[a-z0-9][a-z0-9-]*$ (or anthropic/<that>)")
    if alias.startswith("anthropic/") and not _is_claude(provider_model):
        raise AdminError(422, "invalid_alias", "the anthropic/ prefix is reserved for Claude backends")
    return alias


def _model_rows(models: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """alias → {provider_model, mode, params, ids} (first deployment wins for params)."""
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        alias = m.get("model_name")
        if not alias:
            continue
        info = m.get("model_info") or {}
        params = dict(m.get("litellm_params") or {})
        entry = out.setdefault(str(alias), {
            "provider_model": params.get("model"),
            "mode": info.get("mode") or "chat",
            "params": {k: v for k, v in params.items() if k not in ("model", *_SECRET_PARAM_KEYS)},
            "ids": [],
        })
        if info.get("id"):
            entry["ids"].append(str(info["id"]))
    return out


def _health(checks: dict[str, Any] | None, alias: str, ids: list[str]) -> dict[str, Any] | None:
    if not checks:
        return None
    for key in (*ids, alias):
        c = checks.get(key)
        if isinstance(c, dict):
            return {"status": c.get("status"), "checked_at": c.get("checked_at"),
                    "response_time_ms": c.get("response_time_ms")}
    return None


async def _team(tenant: str) -> dict[str, Any]:
    try:
        info = await get_admin().team_info(team_id(tenant))
    except (LiteLLMError, Exception) as exc:  # noqa: BLE001 - transport or API failure
        raise _unavailable(exc) from exc
    if info is None:
        raise AdminError(404, "team_not_found", "this tenant has no AI gateway team yet")
    return info


async def tenant_models(tenant: str) -> dict[str, dict[str, Any]]:
    """Aliases usable by the tenant team: alias → {provider_model, mode, params, ids, capabilities}."""
    team = await _team(tenant)
    try:
        rows = _model_rows(await get_admin().models())
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    allowed = [str(m) for m in team.get("models") or []]
    names = sorted(rows) if not allowed or "all-proxy-models" in allowed else [a for a in allowed if a in rows]
    out = {}
    for alias in names:
        r = rows[alias]
        out[alias] = {**r, "capabilities": capabilities(alias, r["provider_model"], r["mode"])}
    return out


async def list_models(tenant: str, *, platform_view: bool) -> list[dict[str, Any]]:
    if platform_view:
        try:
            rows = _model_rows(await get_admin().models())
        except Exception as exc:  # noqa: BLE001
            raise _unavailable(exc) from exc
        team = await _team(tenant)
        allowed = set(str(m) for m in team.get("models") or [])
        models = {a: {**r, "capabilities": capabilities(a, r["provider_model"], r["mode"])} for a, r in rows.items()}
    else:
        models = await tenant_models(tenant)
        allowed = set(models)
    checks = await get_admin().health_latest()
    items = []
    for alias in sorted(models):
        r = models[alias]
        item: dict[str, Any] = {
            "alias": alias,
            "mode": r["mode"],
            "capabilities": r["capabilities"],
            "health": _health(checks, alias, r["ids"]),
        }
        if platform_view:
            item.update({"provider_model": r["provider_model"], "params": r["params"],
                         "tenant_access": alias in allowed or not allowed})
        items.append(item)
    return items


# ───────────────────────────── platform: aliases ─────────────────────────────


async def _all_tenants() -> list[str]:
    async with system_session() as s:
        return [str(r[0]) for r in (await s.execute(text("SELECT slug FROM tenant ORDER BY slug"))).all()]


async def _grant(tenant: str, alias: str, *, revoke: bool = False) -> None:
    admin = get_admin()
    team = await admin.team_info(team_id(tenant))
    if team is None:
        return
    models = [str(m) for m in team.get("models") or []]
    if not models or "all-proxy-models" in models:
        return  # an unrestricted team already reaches every alias; a grant must not restrict it
    new = [m for m in models if m != alias] if revoke else sorted({*models, alias})
    if new != models:
        await admin.update_team(team_id(tenant), {"models": new})
    try:
        key = await reg.get_tenant_llm_key(tenant)
    except reg.LlmKeyMissing:
        return
    await admin.update_key(key, {"models": new})


async def upsert_model(alias: str, body: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
    provider_model = str(body.get("provider_model") or "").strip()
    if not provider_model or len(provider_model) > 200 or " " in provider_model:
        raise AdminError(422, "invalid_model", "provider_model is required")
    mode = str(body.get("mode") or "chat")
    if mode not in MODES:
        raise AdminError(422, "invalid_mode", f"mode must be one of {', '.join(MODES)}")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise AdminError(422, "invalid_params", "params must be an object")
    params = dict(params)
    params.pop("model", None)
    for k in _SECRET_PARAM_KEYS:
        v = params.get(k)
        if v is not None and not (isinstance(v, str) and v.startswith("os.environ/")):
            raise AdminError(422, "invalid_params", f"{k} may only reference an environment variable (os.environ/…)")
    alias = normalize_alias(alias, provider_model)
    if params.get("max_tokens") is not None and (
            isinstance(params["max_tokens"], bool) or not isinstance(params["max_tokens"], int)
            or params["max_tokens"] < 1):
        raise AdminError(422, "invalid_params", "max_tokens must be a positive integer")
    if _is_claude(provider_model) and mode == "chat":
        if not params.get("reasoning_effort") or int(params.get("max_tokens") or 0) < 16000:
            raise AdminError(422, "invalid_params", "Claude aliases must set reasoning_effort and max_tokens >= 16000")
    litellm_params: dict[str, Any] = {"model": provider_model, **params}
    if provider_model.startswith("gemini/"):
        litellm_params.setdefault("api_key", "os.environ/GEMINI_API_KEY")
    info = {"mode": mode, "description": "keelson back-office alias"}
    admin = get_admin()
    try:
        before = _model_rows(await admin.models()).get(alias)
        result = await admin.upsert_model(alias, litellm_params, info)
        granted: list[str] = []
        if body.get("grant_all_tenants") is True:
            for tenant in await _all_tenants():
                await _grant(tenant, alias)
                granted.append(tenant)
    except AdminError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    await admin_audit(actor, "gateway.model.upsert", f"model:{alias}",
                      {"provider_model": before["provider_model"], "mode": before["mode"], "params": before["params"]}
                      if before else None,
                      {"provider_model": provider_model, "mode": mode,
                       "params": {k: v for k, v in params.items() if k not in _SECRET_PARAM_KEYS},
                       "granted": granted}, platform=True)
    return {"alias": alias, "result": result, "granted_tenants": granted,
            "capabilities": capabilities(alias, provider_model, mode)}


async def delete_model(alias: str, actor: dict[str, Any]) -> dict[str, Any]:
    from keelson_engine.core.agent_specs import aliases_in_use

    users = await aliases_in_use()
    referencing = sorted(t for t, aliases in users.items() if alias in aliases)
    if referencing:
        raise AdminError(409, "alias_in_use", "the alias is used by an agent spec", tenants=referencing)
    admin = get_admin()
    try:
        deleted = await admin.delete_alias(alias)
        if deleted == 0:
            raise AdminError(404, "not_found", "alias not found")
        for tenant in await _all_tenants():
            await _grant(tenant, alias, revoke=True)
    except AdminError:
        raise
    except LiteLLMError as exc:
        if exc.status == 409:
            raise AdminError(409, "alias_not_managed", "this alias is defined in the gateway config file") from exc
        raise _unavailable(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    await admin_audit(actor, "gateway.model.delete", f"model:{alias}", {"deployments": deleted}, None, platform=True)
    return {"alias": alias, "deleted": deleted}


# ───────────────────────────── tenant team ─────────────────────────────


def key_prefix(key: str) -> str:
    return f"{key[:7]}…" if key else ""


async def tenant_view(tenant: str) -> dict[str, Any]:
    team = await _team(tenant)
    try:
        key = await reg.get_tenant_llm_key(tenant)
    except reg.LlmKeyMissing:
        key = ""
    row = await reg.read_tenant_llm_key_row(tenant)
    return {
        "team_id": team_id(tenant),
        "team_alias": team.get("team_alias"),
        "budget_eur": team.get("max_budget"),
        "spend_eur": round(float(team.get("spend") or 0.0), 6),
        "budget_duration": team.get("budget_duration"),
        "rpm_limit": team.get("rpm_limit"),
        "tpm_limit": team.get("tpm_limit"),
        "models": sorted(str(m) for m in team.get("models") or []),
        "key_prefix": key_prefix(key),
        "key_version": int(row["key_version"]) if row else 0,
        "rotated_at": row["rotated_at"].isoformat() if row and row.get("rotated_at") else None,
    }


_DURATION_RE = re.compile(r"^[1-9][0-9]{0,3}[smhd]$|^[1-9][0-9]{0,2}mo$")


async def patch_tenant(tenant: str, body: dict[str, Any], actor: dict[str, Any], *,
                       platform_admin: bool) -> dict[str, Any]:
    settings = get_settings()
    update: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    for field_name, cap in (("rpm_limit", settings.platform_max_rpm_limit),
                            ("tpm_limit", settings.platform_max_tpm_limit)):
        if body.get(field_name) is not None:
            value = body[field_name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise AdminError(422, "invalid_limit", f"{field_name} must be a positive integer")
            if value > cap and not platform_admin:
                raise AdminError(403, "guarded_change", f"{field_name} above the platform maximum ({cap}) "
                                 "requires a platform admin")
            update[field_name] = value
            overrides[field_name] = value
    if body.get("budget_eur") is not None or body.get("budget_duration") is not None:
        if not platform_admin:
            raise AdminError(403, "guarded_change", "changing the budget requires a platform admin")
        if body.get("budget_eur") is not None:
            budget = body["budget_eur"]
            if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0 or budget > 1_000_000:
                raise AdminError(422, "invalid_budget", "budget_eur must be between 0 and 1,000,000")
            update["max_budget"] = float(budget)
            overrides["budget_eur"] = float(budget)
        if body.get("budget_duration") is not None:
            if not isinstance(body["budget_duration"], str) or not _DURATION_RE.match(body["budget_duration"]):
                raise AdminError(422, "invalid_budget", "budget_duration must look like 30d, 24h or 1mo")
            update["budget_duration"] = body["budget_duration"]
            overrides["budget_duration"] = body["budget_duration"]
    if not update:
        raise AdminError(422, "empty_patch", "nothing to change")
    before = await tenant_view(tenant)
    try:
        await get_admin().update_team(team_id(tenant), update)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    async with system_session() as s:
        await s.execute(
            text("UPDATE tenant SET admin_overrides = admin_overrides || CAST(:o AS jsonb), updated_at = now() "
                 "WHERE slug = :t"),
            {"o": dumps(overrides), "t": tenant},
        )
    await admin_audit(actor, "gateway.tenant.update", f"team:{team_id(tenant)}",
                      {k: before.get(k) for k in ("budget_eur", "budget_duration", "rpm_limit", "tpm_limit")},
                      overrides, tenant=tenant)
    return await tenant_view(tenant)


async def rotate_key(tenant: str, actor: dict[str, Any]) -> dict[str, Any]:
    """New virtual key (same team + model allow-list) → stored encrypted (``key_version`` + 1,
    published to Valkey for the runtime) → old key deleted in LiteLLM."""
    # One rotation per tenant at a time (transaction-scoped advisory lock held until we are done):
    # two concurrent rotations would both read the same "old" key, and the key generated by the
    # first would be overwritten in the DB yet stay valid in LiteLLM (an orphaned live credential).
    async with system_session() as lock:
        await lock.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"llm_key_rotate|{tenant}"})
        return await _rotate_key_locked(tenant, actor)


async def _rotate_key_locked(tenant: str, actor: dict[str, Any]) -> dict[str, Any]:
    admin = get_admin()
    team = await _team(tenant)
    row = await reg.read_tenant_llm_key_row(tenant)
    try:
        old = await reg.get_tenant_llm_key(tenant)
    except reg.LlmKeyMissing:
        old = ""
    except Exception:  # noqa: BLE001 - undecryptable old key: still rotate
        old = ""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    # LiteLLM requires unique key aliases: a second-resolution stamp alone made a second rotation
    # within the same second fail (400 "Unique key aliases … are required" → 502)
    alias = f"keelson-{tenant}-{stamp}-{secrets.token_hex(3)}"
    try:
        new_key = await admin.generate_key({
            "team_id": team_id(tenant),
            "key_alias": alias,
            "models": [str(m) for m in team.get("models") or []],
            "metadata": {"keelson_tenant": tenant, "purpose": "engine runtime", "rotated_by": actor.get("username")},
        })
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    version = await reg.store_tenant_llm_key(tenant, new_key, key_alias=alias, team_id=team_id(tenant),
                                             alias_prefix=(row or {}).get("alias_prefix") or "", rotated=True)
    old_deleted = False
    if old and old != new_key:
        try:
            await admin.delete_key(old)
            old_deleted = True
        except Exception as exc:  # noqa: BLE001 - the new key is live; a stale old key is only logged
            log.warning("old LiteLLM key not deleted", tenant=tenant, error=str(exc)[:200])
    reg.clear_llm_key_cache()
    await admin_audit(actor, "gateway.key.rotate", f"team:{team_id(tenant)}",
                      {"key_sha256_prefix": hashlib.sha256(old.encode()).hexdigest()[:12] if old else None},
                      {"key_alias": alias, "key_version": version, "old_deleted": old_deleted}, tenant=tenant)
    return {"key_prefix": key_prefix(new_key), "key_version": version, "old_key_deleted": old_deleted}


async def usage(tenant: str, days: int = 7) -> dict[str, Any]:
    days = max(1, min(int(days), 90))
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    try:
        rows = await get_admin().team_daily_activity(team_id(tenant), start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    per: dict[tuple[str, str], dict[str, Any]] = {}
    daily: dict[str, dict[str, Any]] = {
        (start + dt.timedelta(days=i)).isoformat(): {"date": (start + dt.timedelta(days=i)).isoformat(),
                                                     "spend_eur": 0.0, "tokens_in": 0, "tokens_out": 0,
                                                     "requests": 0}
        for i in range(days)
    }
    for r in rows:
        date = str(r.get("date") or "")[:10]
        breakdown = r.get("breakdown") or {}
        groups = breakdown.get("model_groups") or breakdown.get("models") or {}
        for alias, entry in groups.items():
            m = (entry or {}).get("metrics") or {}
            item = per.setdefault((date, str(alias)), {"date": date, "alias": str(alias), "spend_eur": 0.0,
                                                       "tokens_in": 0, "tokens_out": 0, "requests": 0})
            item["spend_eur"] += float(m.get("spend") or 0.0)
            item["tokens_in"] += int(m.get("prompt_tokens") or 0)
            item["tokens_out"] += int(m.get("completion_tokens") or 0)
            item["requests"] += int(m.get("api_requests") or 0)
        m = r.get("metrics") or {}
        if date in daily:
            d = daily[date]
            d["spend_eur"] += float(m.get("spend") or 0.0)
            d["tokens_in"] += int(m.get("prompt_tokens") or 0)
            d["tokens_out"] += int(m.get("completion_tokens") or 0)
            d["requests"] += int(m.get("api_requests") or 0)
    items = sorted(per.values(), key=lambda i: (i["date"], i["alias"]))
    for coll in (items, list(daily.values())):
        for i in coll:
            i["spend_eur"] = round(i["spend_eur"], 6)
    return {"days": days, "from": start.isoformat(), "to": end.isoformat(), "items": items,
            "daily": list(daily.values())}


async def tenant_overrides(tenant: str) -> dict[str, Any]:
    async with system_session() as s:
        res = await s.execute(text("SELECT admin_overrides FROM tenant WHERE slug = :t"), {"t": tenant})
        value = res.scalar_one_or_none()
    return dict(value or {})
