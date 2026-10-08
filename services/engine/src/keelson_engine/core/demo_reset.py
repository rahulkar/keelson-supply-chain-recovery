"""Full demo reset (``POST /api/demo/reset``, ``docs/contracts/95-m5-remediation.md`` §6).

:func:`reset_demo` runs ``keelson_contracts.demo.DEMO_RESET_STEPS`` in order for the demo tenant
(``DEMO_TENANTS``, first entry) and returns the ``DemoResetResult`` step log. A failing step is
logged as ``failed`` with a short detail (never a secret) and the reset continues, so one call
always leaves as much as possible reset; ``ok`` is false when any step failed.

1. ``merxa_scenario_reset`` — Merxa REST as ``DEMO_MERXA_ADMIN`` (``merxa.admin``): demo
   ``login-as`` (Merxa session cookie, ``Origin: MERXA_PUBLIC_URL``, ``X-Demo-Access`` on hosted
   demos), then ``POST /v1/admin/scenario:reset`` (golden seed; this also restores Merxa's sim
   clock) and logout.
2. ``keelson_rows`` — delete the tenant's cases (cascading to messages, runs, run events,
   interrupts, decisions, scenarios, plans + dependencies, change sets + ledger, checklists,
   evidence, blocks, signals) and its connector events received before the reset, and marks every
   scn snapshot that is not current for GC (``scn_sync.mark_noncurrent``: the worker deletes their
   rows in the background, so the reset never waits for ~300k-row deletes). Kept: connectors, tools, knowledge bases, LLM recordings
   (``llm_replay``, so demos replay), the append-only audit.
3. ``adk_sessions`` — ADK ``sessions``/``events``/``app_states``/``user_states`` rows of the app
   ``t_<tenant>`` (``ADK_DATABASE_URL``).
4. ``valkey_keys`` — the deleted cases' live streams ``keelson:case:<id>`` and the tenant's
   webhook rate-limit keys.
5. ``merxa_consents`` — Keycloak admin API (realm of each platform connector's issuer): revoke
   the consents + offline tokens of the users linked through the Token Broker and
   ``DEMO_MERXA_USERS`` (skipped with ``keep_consents``).
6. ``token_broker`` — delete the tenant's delegated credentials + identity links (skipped with
   ``keep_consents``).
7. ``agent_spec`` — back to the pack default (``agent_specs.reset_to_default``).
8. ``sim_clock`` — restored by step 1 (the golden seed carries Merxa's sim clock); failed when 1
   failed.
9. ``scn_resync`` — request a sync of every sync-capable connector now (the worker runs it).

M6: with the Signal Hub configured, a ``signal_hub`` step runs right after step 1 — the hub's
``POST /v1/demo/reset`` for the demo tenant (scenario packs back to not-started, the tenant's alerts
and tenant-origin signals deleted) — *before* the Keelson rows are deleted, so the alert watcher
(paused while the reset lock is held) can never re-open a case for an old alert. ``keelson_rows``
also deletes the alert → case mappings (``alert_case``, with the cases) and ``valkey_keys`` the
tenant's alert channel keys (stream, state; the first poll after the reset primes a fresh state).
The tenant network is pushed again by the re-sync (``core.network``).
"""

from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from keelson_contracts.demo import DemoResetResult, DemoResetStep
from keelson_contracts.runs import adk_app_name, case_stream_key
from keelson_engine.config import get_settings
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger

__all__ = ["DemoResetBusy", "StepFailed", "demo_tenant", "reset_demo", "set_adk_url"]

log = get_logger(__name__)

LOCK_TTL_S = 120
_adk_url_override: str | None = None


class StepFailed(Exception):
    """A step failed with a user-safe ``detail``."""


class DemoResetBusy(Exception):
    """Another reset of the same tenant is running."""


def set_adk_url(url: str | None) -> None:
    """Tests: point the ADK step at another database (``None`` = ``ADK_DATABASE_URL``)."""
    global _adk_url_override
    _adk_url_override = url


def demo_tenant() -> str:
    tenants = [t.strip() for t in get_settings().demo_tenants.split(",") if t.strip()]
    if not tenants:
        raise StepFailed("no demo tenant configured")
    return tenants[0]


def _counts(raw: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).lower().replace(" ", "_")[:64]
            if re.match(r"^[a-z][a-z0-9_.-]*$", key) and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[key] = v
    return out


# ───────────────────────────── steps ─────────────────────────────


def _cookie_header(resp: Any) -> str:
    pairs = []
    for raw in resp.headers.get_list("set-cookie"):
        first = raw.split(";", 1)[0].strip()
        if "=" in first:
            pairs.append(first)
    return "; ".join(pairs)


async def _merxa_reset(tenant: str) -> tuple[dict[str, int], str]:
    import httpx

    from keelson_engine.core.http import internal_http

    st = get_settings()
    base, origin = st.merxa_api_url, st.merxa_public_url
    headers = {"Origin": origin}
    secret = st.demo_access_secret.get_secret_value()
    if not st.is_local_demo and secret:
        headers["X-Demo-Access"] = secret
    http = internal_http()
    try:
        login = await http.post(f"{base}/v1/auth/demo/login-as", json={"username": st.demo_merxa_admin},
                                headers=headers, timeout=10.0)
        if login.status_code != 200:
            raise StepFailed(f"Merxa refused the {st.demo_merxa_admin} sign-in (HTTP {login.status_code})")
        cookie = _cookie_header(login)
        if not cookie:
            raise StepFailed("Merxa sign-in returned no session")
        auth = {**headers, "Cookie": cookie}
        resp = await http.post(f"{base}/v1/admin/scenario:reset", headers={**auth, "Idempotency-Key": str(uuid.uuid4())},
                               timeout=st.demo_reset_merxa_timeout_s)
        try:
            await http.post(f"{base}/v1/auth/logout", headers=auth, timeout=5.0)
        except httpx.HTTPError:
            pass
    except httpx.HTTPError as exc:
        raise StepFailed(f"Merxa unreachable ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise StepFailed(f"Merxa scenario:reset failed (HTTP {resp.status_code})")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    counts = _counts(body.get("counts")) if isinstance(body, dict) else {}
    profile = str(body.get("profile") or "") if isinstance(body, dict) else ""
    return counts, f"golden seed restored{f' ({profile[:40]})' if profile else ''}"


_CASCADED = ("case_message", "run", "run_event", "interrupt", "approval_decision", "scenario", "plan",
             "plan_dependency", "change_set", "commit_ledger", "checklist", "checklist_step", "evidence", "block",
             "case_signal", "finding", "calc", "investigation_task", "resume_request", "alert_case")


async def _keelson_rows(tenant: str, started: dt.datetime, case_ids: list[str]) -> tuple[dict[str, int], str]:
    from keelson_engine.core import scn_sync

    counts: dict[str, int] = {}
    async with tenant_session(tenant) as s:
        rows = await s.execute(text('SELECT id FROM "case"'))
        case_ids.extend(str(r[0]) for r in rows.all())
        for table in _CASCADED:
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            if n:
                counts[table] = int(n)
        res = await s.execute(text('DELETE FROM "case" RETURNING id'))
        counts["case"] = len(res.all())
        res = await s.execute(text("DELETE FROM connector_event WHERE received_at < :t RETURNING id"), {"t": started})
        counts["connector_event"] = len(res.all())
        # M7 four-eyes queue: requests were made against the state the reset discards (admin_audit
        # keeps their history)
        res = await s.execute(text("DELETE FROM admin_change_request RETURNING id"))
        counts["admin_change_request"] = len(res.all())
    # only marked here (registry rows): the worker deletes the ~300k rows per snapshot in the background
    counts["scn_snapshot_gc"] = await scn_sync.mark_noncurrent(tenant)
    return counts, f"{counts['case']} cases and their rows deleted"


async def _adk_sessions(tenant: str) -> tuple[dict[str, int], str]:
    url = _adk_url_override or get_settings().adk_database_url
    app = adk_app_name(tenant)
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    counts: dict[str, int] = {}
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(
                "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND column_name = 'app_name' AND table_name = ANY(:t)"),
                {"t": ["events", "sessions", "app_states", "user_states"]})
            present = {str(r[0]) for r in res.all()}
            for table in ("events", "sessions", "app_states", "user_states"):  # children first
                if table in present:
                    res = await conn.execute(text(f"DELETE FROM {table} WHERE app_name = :a"), {"a": app})
                    counts[table] = int(res.rowcount or 0)
    finally:
        await engine.dispose()
    if not counts:
        return counts, "no ADK session tables yet"
    return counts, f"ADK sessions of {app} deleted"


async def _valkey_keys(tenant: str, case_ids: list[str]) -> tuple[dict[str, int], str]:
    from keelson_engine.core.alerts import alert_valkey_keys
    from keelson_engine.core.valkey import get_valkey

    v = get_valkey()
    deleted = 0
    keys = [case_stream_key(c) for c in case_ids]
    for i in range(0, len(keys), 500):
        deleted += int(await v.delete(*keys[i:i + 500]) or 0)
    rate = 0
    async for key in v.scan_iter(match=f"keelson:wh:rl:{tenant}:*", count=500):
        rate += int(await v.delete(key) or 0)
    alert_keys = int(await v.delete(*alert_valkey_keys(tenant)) or 0)
    return ({"case_streams": deleted, "rate_limit_keys": rate, "alert_keys": alert_keys},
            f"{deleted + rate + alert_keys} keys deleted")


async def _signal_hub(tenant: str) -> tuple[dict[str, int], str]:
    from keelson_engine.core import signal_hub

    try:
        out = await signal_hub.demo_reset(tenant)
    except signal_hub.HubError as exc:
        raise StepFailed(f"Signal Hub demo reset failed ({exc.code}"
                         f"{f', HTTP {exc.status}' if exc.status else ''})") from exc
    counts = _counts(out.get("counts")) if isinstance(out, dict) else {}
    return counts, "scenario packs and tenant alerts reset"


async def _linked_usernames(tenant: str) -> list[str]:
    async with tenant_session(tenant) as s:
        res = await s.execute(text(
            "SELECT DISTINCT ext_username FROM connector_identity_link WHERE ext_username IS NOT NULL"))
        return [str(r[0]) for r in res.all() if r[0]]


async def _merxa_consents(tenant: str, usernames: list[str]) -> tuple[dict[str, int], str]:
    from keelson_engine.core import connectors
    from keelson_engine.core.keycloak_admin import KeycloakAdmin, KeycloakAdminError

    st = get_settings()
    users = list(dict.fromkeys([*usernames, *(u.strip() for u in st.demo_merxa_users.split(",") if u.strip())]))
    by_realm: dict[str, list[str]] = {}
    for conn in await connectors.list_connectors(tenant):
        if conn.is_platform and conn.issuer and conn.clients.get("connect"):
            realm = conn.issuer.rstrip("/").rsplit("/", 1)[-1]
            by_realm.setdefault(realm, []).append(conn.clients["connect"])
    if not by_realm or not users:
        return {}, "no platform connector or demo users"
    admin = KeycloakAdmin()
    totals: dict[str, int] = {"users": 0, "users_missing": 0, "revoked": 0}
    for realm, clients in sorted(by_realm.items()):
        try:
            got = await admin.revoke_user_grants(realm, users, sorted(set(clients)))
        except KeycloakAdminError as exc:
            raise StepFailed(f"Keycloak admin API: {exc}") from exc
        for k, v in got.items():
            totals[k] = totals.get(k, 0) + v
    return totals, f"offline sessions/consents revoked for {totals['users']} users"


async def _token_broker(tenant: str) -> tuple[dict[str, int], str]:
    from keelson_engine.core.tokens import get_token_broker

    async with tenant_session(tenant) as s:
        creds = await s.execute(text("DELETE FROM connector_credential WHERE principal_type = 'user' RETURNING 1"))
        links = await s.execute(text("DELETE FROM connector_identity_link RETURNING 1"))
        counts = {"connector_credential": len(creds.all()), "connector_identity_link": len(links.all())}
    get_token_broker().clear_cache()
    return counts, "delegated links deleted"


async def _agent_spec(tenant: str, actor: dict[str, Any]) -> tuple[dict[str, int], str]:
    from keelson_engine.core import agent_specs
    from keelson_engine.core.db import system_session
    from keelson_engine.core.packs import PackError, load_pack

    async with system_session() as s:
        cfg = (await s.execute(text("SELECT config FROM tenant WHERE slug = :t"), {"t": tenant})).scalar_one_or_none()
    pack_name = (cfg or {}).get("pack") if isinstance(cfg, dict) else None
    overrides: dict[str, Any] = {}
    if pack_name:
        try:
            overrides = load_pack(str(pack_name)).spec_overrides
        except PackError as exc:
            raise StepFailed(f"pack {pack_name!r} unavailable") from exc
    out = await agent_specs.reset_to_default(tenant, overrides, actor=actor,
                                             summary=f"demo reset to {pack_name or 'built-in'} default")
    return {}, f"{out['version']} ({out['action']})"


async def _scn_resync(tenant: str) -> tuple[dict[str, int], str]:
    from keelson_engine.core import connectors, scn_sync

    requested = 0
    for conn in await connectors.list_connectors(tenant):
        try:
            await scn_sync.request_sync(tenant, conn.slug, reason="demo_reset")
            requested += 1
        except scn_sync.SyncUnavailable:
            continue
    return {"connectors": requested}, f"re-sync requested for {requested} connector(s)"


# ───────────────────────────── orchestration ─────────────────────────────


async def _lock(tenant: str) -> str | None:
    from keelson_engine.core.valkey import get_valkey

    key = f"keelson:demo_reset:{tenant}"
    try:
        if not await get_valkey().set(key, "1", nx=True, ex=LOCK_TTL_S):
            raise DemoResetBusy(tenant)
    except DemoResetBusy:
        raise
    except Exception as exc:  # noqa: BLE001 - Valkey down: run unlocked
        log.warning("demo reset lock unavailable", error=type(exc).__name__)
        return None
    return key


async def _unlock(key: str | None) -> None:
    if key is None:
        return
    from keelson_engine.core.valkey import get_valkey

    try:
        await get_valkey().delete(key)
    except Exception:  # noqa: BLE001 - expires anyway
        pass


async def reset_demo(*, keep_consents: bool, actor: dict[str, Any], tenant: str | None = None) -> DemoResetResult:
    """Run the reset (see module docstring). Raises :class:`DemoResetBusy` while another runs."""
    from keelson_engine.core.repo import admin_audit

    t = tenant or demo_tenant()
    key = await _lock(t)
    started = dt.datetime.now(dt.UTC)
    t0 = time.perf_counter()
    steps: list[DemoResetStep] = []
    case_ids: list[str] = []
    usernames: list[str] = []
    merxa_ok = False

    async def run(name: str, fn: Callable[[], Awaitable[tuple[dict[str, int], str]]], *,
                  skip: str | None = None) -> bool:
        s0 = time.perf_counter()
        if skip is not None:
            steps.append(DemoResetStep(step=name, status="skipped", duration_ms=0, detail=skip[:500]))
            return False
        try:
            counts, detail = await fn()
            status, ok = "ok", True
        except StepFailed as exc:
            counts, detail, status, ok = {}, str(exc), "failed", False
        except Exception as exc:  # noqa: BLE001 - report and continue (class name only: no secrets)
            log.error("demo reset step failed", step=name, tenant=t, error=f"{type(exc).__name__}: {exc}")
            counts, detail, status, ok = {}, f"{name} failed ({type(exc).__name__})", "failed", False
        steps.append(DemoResetStep(step=name, status=status, duration_ms=round((time.perf_counter() - s0) * 1000),
                                   detail=(detail or None) and detail[:500], counts=_counts(counts)))
        return ok

    try:
        merxa_ok = await run("merxa_scenario_reset", lambda: _merxa_reset(t))
        from keelson_engine.core import signal_hub

        if signal_hub.is_configured():
            await run("signal_hub", lambda: _signal_hub(t))
        await run("keelson_rows", lambda: _keelson_rows(t, started, case_ids))
        await run("adk_sessions", lambda: _adk_sessions(t))
        await run("valkey_keys", lambda: _valkey_keys(t, case_ids))
        consent_skip = "rehearsal mode (keep_consents)" if keep_consents else None

        async def consents() -> tuple[dict[str, int], str]:
            usernames.extend(await _linked_usernames(t))
            return await _merxa_consents(t, usernames)

        await run("merxa_consents", consents, skip=consent_skip)
        await run("token_broker", lambda: _token_broker(t), skip=consent_skip)
        await run("agent_spec", lambda: _agent_spec(t, actor))

        async def clock() -> tuple[dict[str, int], str]:
            if not merxa_ok:
                raise StepFailed("not reset: the Merxa scenario reset failed")
            return {}, "restored with the Merxa golden seed"

        await run("sim_clock", clock)
        await run("scn_resync", lambda: _scn_resync(t))
    finally:
        await _unlock(key)
    duration = round((time.perf_counter() - t0) * 1000)
    result = DemoResetResult(ok=all(s.status != "failed" for s in steps), keep_consents=keep_consents,
                             started_at=started, duration_ms=duration, steps=steps)
    await admin_audit(actor, "demo.reset", f"tenant:{t}", None,
                      {"ok": result.ok, "keep_consents": keep_consents, "duration_ms": duration,
                       "failed": result.failed_steps}, tenant=t)
    log.info("demo reset", tenant=t, ok=result.ok, duration_ms=duration, failed=result.failed_steps)
    return result
