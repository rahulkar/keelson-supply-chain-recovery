"""Refcounted LRU of ``(App, Runner)`` per ``(tenant, spec_version)``.

* ``App(name=adk_app_name(tenant), root_agent=build_workflow(spec), resumability_config=
  ResumabilityConfig(is_resumable=True), plugins=[Policy, Budget, Audit, Evidence])``; the app
  name never changes with the spec version, so sessions survive spec upgrades.
* One shared ``DatabaseSessionService(ADK_DATABASE_URL)`` for every runner.
* Eviction closes a Runner only once nobody holds a lease on it.
* **Tenant LLM key rotation** (``POST /api/admin/gateway/tenant/rotate-key`` runs in engine-api,
  a different process): a runner's models hold the tenant's LiteLLM virtual key from build time,
  so every :meth:`RunnerCache.get` first reads the tenant's *key version*
  (``ConnectorsPort.tenant_llm_key_version`` → ``core.connectors.get_tenant_llm_key_version``,
  i.e. the ``tenant_llm_key.key_version`` column that ``store_tenant_llm_key`` bumps on every key
  change; on a pre-M2 core, a fingerprint of the stored ``tenant_llm_key`` row — its ``key_enc``
  ciphertext is new on every store). One small DB read per run. When it differs from the version a cached
  runner was built with, all of that tenant's runners are evicted (closed once their leases are
  released), the in-process key copy is dropped, and the runner is rebuilt with the new key.
  A failing version read keeps the cached runner (logged). Every build also drops core's
  in-process key copy first, so a runner is never built with a pre-rotation key.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from keelson_contracts.runs import adk_app_name

from .deps import get_deps
from .llm import LiteLlmModelFactory, install_sentinel_default
from .plugins import default_plugins
from .spec import DEFAULT_SPEC_VERSION, AgentSpec, default_spec
from .workflow import WorkflowBindings, build_workflow

__all__ = ["RunnerCache", "RunnerLease", "get_runner", "get_runner_cache", "set_runner_cache"]

log = structlog.get_logger(__name__)

SpecLoader = Callable[[str, str], Awaitable[AgentSpec]]
BindingsFactory = Callable[[str, AgentSpec], Awaitable[WorkflowBindings]]
KeyVersion = Callable[[str], Awaitable[str | None]]
"""``tenant -> opaque LLM key version`` (``None`` = unknown / no key)."""
KeyForget = Callable[[str], None]

_UNCHECKED = object()


@dataclass
class _Entry:
    key: tuple[str, str]
    app: App
    runner: Runner
    spec: AgentSpec
    key_version: str | None = None
    refs: int = 0
    evicted: bool = False


@dataclass
class RunnerLease:
    runner: Runner
    app: App
    spec: AgentSpec
    _release: Callable[[], Awaitable[None]] = field(repr=False)
    _released: bool = False

    async def release(self) -> None:
        if not self._released:
            self._released = True
            await self._release()

    async def __aenter__(self) -> RunnerLease:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.release()


async def default_spec_loader(tenant: str, version: str) -> AgentSpec:
    from .settings import engine_settings  # noqa: PLC0415

    vendor = engine_settings().brand_vendor_name
    if version and version != DEFAULT_SPEC_VERSION:
        row = await get_deps().repo.get_agent_spec(tenant=tenant, version=version)
        if row is not None:
            return AgentSpec.from_row(tenant, version, row)
        log.warning("agent spec not found; using default", tenant=tenant, version=version)
    return default_spec(tenant, vendor_name=vendor)


async def default_bindings(tenant: str, spec: AgentSpec) -> WorkflowBindings:
    from .settings import engine_settings  # noqa: PLC0415

    key = await get_deps().connectors.get_tenant_llm_key(tenant=tenant)
    settings = engine_settings()
    return WorkflowBindings(
        models=LiteLlmModelFactory(
            api_base=settings.litellm_base_url, api_key=key,
            timeout_s=float(getattr(settings, "llm_request_timeout_s", 600.0)),
        )
    )


async def default_key_version(tenant: str) -> str | None:
    fn = getattr(get_deps().connectors, "tenant_llm_key_version", None)
    return await fn(tenant=tenant) if fn is not None else None


def default_key_forget(tenant: str) -> None:
    fn = getattr(get_deps().connectors, "forget_tenant_llm_key", None)
    if fn is not None:
        fn(tenant=tenant)


class RunnerCache:
    def __init__(
        self,
        *,
        session_service: BaseSessionService,
        spec_loader: SpecLoader = default_spec_loader,
        bindings: BindingsFactory = default_bindings,
        max_size: int = 8,
        key_version: KeyVersion | None = default_key_version,
        key_forget: KeyForget | None = default_key_forget,
    ) -> None:
        """``key_version=None`` disables rotation detection (tests with fixed bindings)."""
        install_sentinel_default()
        self.session_service = session_service
        self._spec_loader = spec_loader
        self._bindings = bindings
        self._max = max(1, max_size)
        self._key_version = key_version
        self._key_forget = key_forget
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self.invalidations = 0

    async def _build(self, tenant: str, version: str, key_version: str | None) -> _Entry:
        if self._key_forget is not None and self._key_version is not None:
            # core keeps the decrypted key for 10 s: a runner built right after a rotation (e.g.
            # for a new spec version, with no cached entry to invalidate) would otherwise get the
            # old key while being tagged with the new version, and never be rebuilt.
            self._key_forget(tenant)
        spec = await self._spec_loader(tenant, version)
        bindings = await self._bindings(tenant, spec)
        app = App(
            name=adk_app_name(tenant),
            root_agent=build_workflow(spec, bindings),
            plugins=default_plugins(),
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
        runner = Runner(app=app, session_service=self.session_service)
        log.info("runner built", tenant=tenant, spec_version=version, key_version=key_version)
        return _Entry(key=(tenant, version), app=app, runner=runner, spec=spec, key_version=key_version)

    async def _current_key_version(self, tenant: str) -> Any:
        if self._key_version is None:
            return _UNCHECKED
        try:
            return await self._key_version(tenant)
        except Exception as exc:  # a DB hiccup must not fail the run: keep what is cached
            log.warning("tenant LLM key version unavailable", tenant=tenant, error=type(exc).__name__)
            return _UNCHECKED

    async def invalidate(self, tenant: str, *, reason: str = "requested") -> int:
        """Evict every runner of ``tenant`` (closed once released); returns how many."""
        async with self._lock:
            return await self._invalidate_locked(tenant, reason=reason)

    async def _invalidate_locked(self, tenant: str, *, reason: str) -> int:
        victims = [e for k, e in self._entries.items() if k[0] == tenant]
        for entry in victims:
            del self._entries[entry.key]
            entry.evicted = True
            if entry.refs <= 0:
                await self._close(entry)
        if self._key_forget is not None:
            self._key_forget(tenant)
        if victims:
            self.invalidations += 1
            log.info("runners invalidated", tenant=tenant, reason=reason, count=len(victims))
        return len(victims)

    async def get(self, tenant: str, spec_version: str | None = None) -> RunnerLease:
        version = spec_version or DEFAULT_SPEC_VERSION
        key = (tenant, version)
        current = await self._current_key_version(tenant)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and current is not _UNCHECKED and entry.key_version != current:
                await self._invalidate_locked(tenant, reason="llm_key_rotated")
                entry = None
            if entry is None:
                entry = await self._build(tenant, version, None if current is _UNCHECKED else current)
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.refs += 1
            await self._evict_locked()

        async def release() -> None:
            async with self._lock:
                entry.refs -= 1
                if entry.evicted and entry.refs <= 0:
                    await self._close(entry)

        return RunnerLease(runner=entry.runner, app=entry.app, spec=entry.spec, _release=release)

    async def _evict_locked(self) -> None:
        while len(self._entries) > self._max:
            victim = next((e for e in self._entries.values() if e.refs <= 0), None)
            if victim is None:
                victim = next(iter(self._entries.values()))
                victim.evicted = True
                del self._entries[victim.key]
                continue
            del self._entries[victim.key]
            victim.evicted = True
            await self._close(victim)

    async def _close(self, entry: _Entry) -> None:
        log.info("runner closed", tenant=entry.key[0], spec_version=entry.key[1])
        await entry.runner.close()

    @property
    def size(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        async with self._lock:
            for entry in list(self._entries.values()):
                entry.evicted = True
                if entry.refs <= 0:
                    await self._close(entry)
            self._entries.clear()


_cache: RunnerCache | None = None


def get_runner_cache() -> RunnerCache:
    global _cache
    if _cache is None:
        from google.adk.sessions import DatabaseSessionService  # noqa: PLC0415

        from .settings import WorkerSettings, engine_settings  # noqa: PLC0415

        _cache = RunnerCache(
            session_service=DatabaseSessionService(db_url=engine_settings().adk_database_url),
            max_size=WorkerSettings().runner_cache_size,
        )
    return _cache


def set_runner_cache(cache: RunnerCache | None) -> None:
    global _cache
    _cache = cache


async def get_runner(tenant: str, spec_version: str | None) -> RunnerLease:
    return await get_runner_cache().get(tenant, spec_version)
