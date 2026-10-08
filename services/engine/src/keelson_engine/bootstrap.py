"""One-shot bootstrap job: ``python -m keelson_engine.bootstrap`` (idempotent; exit 1 on failure).

1. wait for Postgres and LiteLLM;
2. apply migrations;
3. upsert tenants from ``/app/tenants.yaml``;
4. LiteLLM: upsert every alias from ``/app/models.lock`` (``GET /model/info`` → ``PATCH
   /model/{id}/update`` else ``POST /model/new``), one team per tenant (budget/rpm/tpm from
   tenants.yaml, falling back to the pack's budgets, overridden by back-office changes in
   ``tenant.admin_overrides``) and one virtual key per tenant (reused while valid, its model
   allow-list re-synced; else generated) stored AES-GCM encrypted;
5. upsert connector rows (incl. lanes) from tenants.yaml;
6. apply each tenant's template pack (``pack`` key, ``PACKS_DIR``): connectors of the pack the
   tenant does not have yet, and agent spec ``v1`` when the tenant has no spec version; then
   built-in specialists a release added that the tenant's spec history never had are added as a
   new version (``agent_specs.upgrade_builtins``);
7. M6: the Signal Hub as an internal first-party connector of every tenant (``service_jwt``) when
   ``SIGNAL_HUB_URL`` / ``SIGNAL_HUB_SHARED_SECRET`` are set (:func:`register_signal_hub`);
8. knowledge base (contract 90 §7): the embedding profile from models.lock ``KB_EMBED_ALIAS``
   (``embed-default``, ``params.dimensions``) — created on first run, re-embedded (shadow + swap)
   when the model/dims changed — then ``KB_SEED_DIR/<tenant>/*.md`` is ingested into a tenant whose
   knowledge base is empty. Embedding failures are logged, never fail the bootstrap (the next run
   retries the failed documents).

The Merxa tool snapshot is **not** taken here: Keycloak authenticates Keelson's
``private_key_jwt`` clients against engine-api's JWKS endpoint, which is not serving yet.
The worker calls ``core.connectors.refresh_tool_snapshot`` at startup; admins can use
``POST /api/connectors/{slug}/refresh-tools`` or the back-office connection test.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text

from keelson_engine.config import Settings, get_settings
from keelson_engine.core import agent_specs
from keelson_engine.core import connectors as connectors_mod
from keelson_engine.core.db import close_db, init_db, keelson_engine, system_session
from keelson_engine.core.http import close_http, internal_http
from keelson_engine.core.litellm_admin import LiteLLMAdmin
from keelson_engine.core.logs import configure_logging, get_logger
from keelson_engine.core.migrate import migrate
from keelson_engine.core.packs import PackError, load_pack
from keelson_engine.core.sql import dumps
from keelson_engine.core.tenants import apply_pack, effective_limits, provision_litellm

__all__ = ["BootstrapError", "apply_packs", "load_models_lock", "load_tenants", "main", "register_signal_hub",
           "run_bootstrap", "seed_knowledge"]

log = get_logger("keelson_engine.bootstrap")



class BootstrapError(RuntimeError):
    pass


def load_tenants(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tenants = data.get("tenants")
    if not isinstance(tenants, list) or not tenants:
        raise BootstrapError(f"{path}: 'tenants' must be a non-empty list")
    for t in tenants:
        if not isinstance(t, dict) or not t.get("slug") or not t.get("name"):
            raise BootstrapError(f"{path}: every tenant needs slug and name")
    return tenants


def load_models_lock(path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    aliases = data.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        raise BootstrapError(f"{path}: 'aliases' must be a non-empty mapping")
    for name, spec in aliases.items():
        if not isinstance(spec, dict) or not spec.get("model"):
            raise BootstrapError(f"{path}: alias {name!r} needs a model")
    return aliases


async def _wait_for(what: str, probe: Any, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if await probe():
                log.info("dependency ready", dependency=what)
                return
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = f"{exc.__class__.__name__}: {exc}"
        await asyncio.sleep(2)
    raise BootstrapError(f"{what} not ready after {timeout_s:.0f}s ({last_error})")


async def _db_probe() -> bool:
    async with system_session() as s:
        await s.execute(text("SELECT 1"))
    return True


async def upsert_tenants(tenants: list[dict[str, Any]]) -> None:
    async with system_session() as s:
        for t in tenants:
            cfg = {k: v for k, v in t.items() if k not in {"slug", "name", "connectors"}}
            await s.execute(
                text(
                    """
                    INSERT INTO tenant (slug, name, config) VALUES (:slug, :name, CAST(:cfg AS jsonb))
                    ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, config = EXCLUDED.config,
                      updated_at = now()
                    """
                ),
                {"slug": t["slug"], "name": t["name"], "cfg": dumps(cfg)},
            )
    log.info("tenants upserted", count=len(tenants))


def _model_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    params: dict[str, Any] = {"model": spec["model"]}
    params.update(spec.get("params") or {})
    if str(spec["model"]).startswith("gemini/"):
        params.setdefault("api_key", "os.environ/GEMINI_API_KEY")
    info = {"mode": spec.get("mode") or "chat", "description": "keelson models.lock alias"}
    return params, info


async def _overrides(tenant: str) -> dict[str, Any]:
    async with system_session() as s:
        res = await s.execute(text("SELECT admin_overrides FROM tenant WHERE slug = :t"), {"t": tenant})
        return dict(res.scalar_one_or_none() or {})


def _pack_budgets(t: dict[str, Any]) -> dict[str, Any]:
    if not t.get("pack"):
        return {}
    try:
        return load_pack(str(t["pack"])).budgets
    except PackError:
        return {}


async def upsert_litellm(settings: Settings, tenants: list[dict[str, Any]], aliases: dict[str, Any],
                         admin: LiteLLMAdmin) -> None:
    for alias, spec in aliases.items():
        params, info = _model_spec(spec)
        result = await admin.upsert_model(alias, params, info)
        log.info("litellm alias upserted", alias=alias, model=spec["model"], result=result)
    alias_names = sorted(aliases)
    for t in tenants:
        tenant = t["slug"]
        limits = effective_limits(_pack_budgets(t), t, await _overrides(tenant))
        provisioned = await provision_litellm(admin, tenant, t["name"], limits, alias_names)
        log.info("litellm tenant provisioned", tenant=tenant, team=provisioned["team"], key=provisioned["key"])


async def upsert_connectors(tenants: list[dict[str, Any]]) -> None:
    for t in tenants:
        for spec in t.get("connectors") or []:
            if not spec.get("slug") or not spec.get("mcp_url") or not spec.get("issuer"):
                raise BootstrapError(f"tenant {t['slug']}: connector needs slug, mcp_url, issuer")
            await connectors_mod.upsert_connector(t["slug"], spec)
            log.info("connector upserted", tenant=t["slug"], connector=spec["slug"])


async def apply_packs(tenants: list[dict[str, Any]]) -> None:
    """Template pack per tenant (``pack`` key): idempotent; tenants.yaml connectors win."""
    for t in tenants:
        if not t.get("pack"):
            continue
        try:
            pack = load_pack(str(t["pack"]))
        except PackError as exc:
            raise BootstrapError(f"tenant {t['slug']}: {exc}") from exc
        own = {str(c.get("slug")) for c in t.get("connectors") or []}
        result = await apply_pack(t["slug"], pack, actor="bootstrap", skip_connectors=own)
        log.info("pack applied", tenant=t["slug"], pack=pack.name, **result)
        upgraded = await agent_specs.upgrade_builtins(t["slug"], actor="bootstrap")
        if upgraded:
            log.info("agent spec upgraded with new built-in specialists", tenant=t["slug"], version=upgraded)


async def register_signal_hub(settings: Settings, tenants: list[dict[str, Any]]) -> None:
    """M6: the Signal Hub as each tenant's internal first-party connector (``service_jwt``), when
    ``SIGNAL_HUB_URL`` + ``SIGNAL_HUB_SHARED_SECRET`` are set. Its tool snapshot is taken by
    engine-api's alert watcher once the hub answers (the hub may start after the bootstrap)."""
    if not settings.signal_hub_enabled:
        log.info("signal hub not configured; internal connector not registered")
        return
    from keelson_engine.core.signal_hub import register_internal_connector

    for t in tenants:
        await register_internal_connector(t["slug"])


def embedding_spec(aliases: dict[str, Any], alias: str) -> tuple[str, int] | None:
    """``(provider_model, dims)`` of the models.lock embedding alias, or None when it is absent."""
    spec = aliases.get(alias)
    if not spec:
        return None
    if (spec.get("mode") or "chat") != "embedding":
        raise BootstrapError(f"models.lock alias {alias!r} is not an embedding model (mode: embedding)")
    try:
        dims = int((spec.get("params") or {})["dimensions"])
    except (KeyError, TypeError, ValueError):
        raise BootstrapError(f"models.lock alias {alias!r} needs params.dimensions") from None
    return str(spec["model"]), dims


async def seed_knowledge(settings: Settings, tenants: list[dict[str, Any]], aliases: dict[str, Any]) -> None:
    from keelson_engine.core import kb

    spec = embedding_spec(aliases, settings.kb_embed_alias)
    if spec is None:
        log.warning("no embedding alias in models.lock; knowledge base disabled", alias=settings.kb_embed_alias)
        return
    profile = await kb.ensure_profile(settings.kb_embed_alias, spec[0], spec[1])
    log.info("embedding profile ready", **profile.to_json())
    root = kb.seed_dir()
    if root is None:
        log.info("no knowledge base seed directory")
        return
    for t in tenants:
        directory = root / t["slug"]
        if not directory.is_dir():
            continue
        try:
            result = await kb.seed_tenant(t["slug"], directory)
        except kb.KbError as exc:  # e.g. no LiteLLM key yet; retried by the next bootstrap
            log.warning("knowledge base seed failed", tenant=t["slug"], code=exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - the KB is optional; never fail the bootstrap for it
            log.error("knowledge base seed failed", tenant=t["slug"], error=f"{type(exc).__name__}: {exc}")
            continue
        log.info("knowledge base seeded", tenant=t["slug"], **result)


async def run_bootstrap(settings: Settings | None = None, *, admin: LiteLLMAdmin | None = None,
                        wait_timeout_s: float = 180.0) -> None:
    settings = settings or get_settings()
    tenants = load_tenants(Path(settings.tenants_file))
    aliases = load_models_lock(Path(settings.models_lock))
    await init_db(settings)
    await _wait_for("postgres", _db_probe, wait_timeout_s)
    applied = await migrate(keelson_engine())
    log.info("migrations done", applied=applied)
    await upsert_tenants(tenants)
    admin = admin or LiteLLMAdmin(settings.litellm_base_url, settings.litellm_master_key.get_secret_value(),
                                  internal_http())
    await _wait_for("litellm", admin.ready, wait_timeout_s)
    await upsert_litellm(settings, tenants, aliases, admin)
    await upsert_connectors(tenants)
    await apply_packs(tenants)
    await register_signal_hub(settings, tenants)
    await seed_knowledge(settings, tenants, aliases)
    log.info("bootstrap complete")


async def _main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    try:
        await run_bootstrap(settings)
        return 0
    except Exception as exc:  # noqa: BLE001 - reported + non-zero exit
        log.error("bootstrap failed", error=f"{exc.__class__.__name__}: {exc}")
        return 1
    finally:
        await close_http()
        await close_db()


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
