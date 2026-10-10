"""The dedicated LiteLLM virtual key for news extraction (``100`` §3, ``110`` §5).

* **Provisioning** (:func:`provision_signals_key`) runs only in the one-shot ``signal-hub-bootstrap``
  container, the only place that gets ``LITELLM_MASTER_KEY``. It upserts one virtual key with alias
  ``SIGNALS_LLM_KEY_ALIAS`` (``keelson-signal-hub``) restricted to the extraction aliases
  (``extract-fast`` + ``guard-default``), ``key_type: llm_api`` (no management routes) and a small
  ``max_budget`` (``SIGNALS_LLM_MAX_BUDGET``, ``budget_duration`` ``30d``), then stores it encrypted
  in ``hub_secret`` (:mod:`signal_hub.secretbox`). A stored key that is still active is reused and
  its restrictions re-synced; a missing/blocked/undecryptable one is replaced (old keys with the
  alias are deleted first).
* **Use** (:func:`resolve_llm_key`): the API (and ``poller --once --adapter extraction``) reads the
  stored key at startup; ``LITELLM_SIGNALS_KEY`` in the environment always wins. No key → news
  stays ``pending_extraction``; a 402 (budget/credits) also keeps it pending (quarantine).

Endpoints verified against the installed LiteLLM proxy source (``ghcr.io/berriai/litellm-non_root:1.104.0``,
``proxy/management_endpoints/key_management_endpoints.py``): ``GET /key/info?key=<sha256>`` (the
hash is accepted, ``info.status`` active|expired|revoked|deleted), ``POST /key/update`` (``key`` may
be the hash; merge patch), ``POST /key/generate`` (``key_alias``, ``models``, ``max_budget``,
``budget_duration``, ``metadata``, ``key_type``), ``POST /key/delete {key_aliases}``.

The raw key is never logged, never put in a URL and never included in an exception message.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import re
import time
from typing import Any, Final

import asyncpg
import httpx

from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.logs import get_logger
from signal_hub.secretbox import Kek, SecretBoxError, derive_kek, load_secret, store_secret

__all__ = [
    "SECRET_NAME",
    "LiteLlmAdmin",
    "LiteLlmAdminError",
    "key_restrictions",
    "load_stored_key",
    "provision_signals_key",
    "redact",
    "resolve_llm_key",
]

log = get_logger(__name__)
SECRET_NAME: Final = "litellm_signals_key"
LLM_API_ROUTES: Final = ("llm_api_routes",)
"""The route preset LiteLLM stores for key_type: llm_api (no management/info routes)."""
_KEYISH = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")


def redact(text: str, *secrets: str | None) -> str:
    """``text`` with the given secrets and anything shaped like a LiteLLM key removed."""
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return _KEYISH.sub("sk-[redacted]", text)


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class LiteLlmAdminError(RuntimeError):
    def __init__(self, what: str, status: int, body: str = "") -> None:
        super().__init__(f"LiteLLM {what} failed: HTTP {status} {redact(body)[:300]}")
        self.status = status


class LiteLlmAdmin:
    """The few management calls the bootstrap needs (master key; one-shot container only)."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, master_key: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._master = master_key
        self._headers = {"Authorization": f"Bearer {master_key}"}

    def __repr__(self) -> str:
        return f"LiteLlmAdmin(base_url={self._base!r})"

    async def _req(self, method: str, path: str, *, what: str, ok: tuple[int, ...] = (200,), **kw: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, f"{self._base}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise LiteLlmAdminError(what, 0, type(exc).__name__) from None
        if resp.status_code not in ok:
            raise LiteLlmAdminError(what, resp.status_code, redact(resp.text, self._master))
        return resp

    async def ready(self) -> bool:
        try:
            resp = await self._http.get(f"{self._base}/health/readiness", timeout=5.0)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def wait_ready(self, timeout_s: float, *, interval_s: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if await self.ready():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(interval_s)

    async def key_info(self, key: str) -> dict[str, Any] | None:
        """The key's ``info`` row, ``None`` when LiteLLM does not know it (looked up by sha256)."""
        resp = await self._http.get(f"{self._base}/key/info", params={"key": _digest(key)}, headers=self._headers)
        if resp.status_code == 200:
            info = resp.json().get("info")
            return dict(info) if isinstance(info, dict) else {}
        if resp.status_code in (400, 404) or (resp.status_code == 500 and "not found" in resp.text.lower()):
            return None
        raise LiteLlmAdminError("key/info", resp.status_code, redact(resp.text, key, self._master))

    async def update_key(self, key: str, body: dict[str, Any]) -> None:
        await self._req("POST", "/key/update", what="key/update", json={"key": _digest(key), **body})

    async def generate_key(self, body: dict[str, Any]) -> str:
        resp = await self._req("POST", "/key/generate", what="key/generate", json=body)
        key = resp.json().get("key")
        if not isinstance(key, str) or not key:
            raise LiteLlmAdminError("key/generate (no key in the response)", resp.status_code)
        return key

    async def delete_alias(self, alias: str) -> None:
        resp = await self._http.post(f"{self._base}/key/delete", json={"key_aliases": [alias]}, headers=self._headers)
        if resp.status_code not in (200, 400, 404):
            raise LiteLlmAdminError("key/delete", resp.status_code, redact(resp.text, self._master))


def key_restrictions(settings: Settings) -> dict[str, Any]:
    """What the extraction key may do: the two extraction aliases (+ the embedding alias when the
    opt-in remote embedding profile is on, ``SIGNALS_EMBED_PROVIDER=litellm``), a small budget."""
    models = {settings.extract_alias, settings.guard_alias}
    if settings.embed_provider == "litellm":
        models.add(settings.embed_alias)
    return {
        "models": sorted(models),
        "max_budget": float(settings.llm_key_max_budget),
        "budget_duration": settings.llm_key_budget_duration,
    }


def _active(info: dict[str, Any]) -> bool:
    status = info.get("status")
    if status is not None:
        return status == "active"
    return not info.get("blocked")


def _kek(settings: Settings) -> Kek | None:
    return derive_kek(data_key=settings.data_key, shared_secret=settings.shared_secret)


async def provision_signals_key(settings: Settings, pool_or_conn: asyncpg.Pool | asyncpg.Connection,
                                admin: LiteLlmAdmin) -> str:
    """Upsert the extraction key; returns ``unchanged`` / ``updated`` / ``generated``.

    Raises :class:`LiteLlmAdminError` (LiteLLM refused) or :class:`SecretBoxError` (no secret to
    encrypt with). Logs carry the alias and the outcome only."""
    kek = _kek(settings)
    if kek is None:
        raise SecretBoxError("neither SIGNALS_DATA_KEY nor SIGNAL_HUB_SHARED_SECRET is set: cannot store the key")
    wanted = key_restrictions(settings)
    alias = settings.llm_key_alias
    stored: str | None = None
    try:
        stored = await _read(pool_or_conn, kek)
    except SecretBoxError as exc:
        log.warning("stored extraction key unreadable; generating a new one", extra={"reason": str(exc)[:200]})
    if stored:
        info = await admin.key_info(stored)
        if info is not None and _active(info):
            patch: dict[str, Any] = {}
            if sorted(str(m) for m in info.get("models") or []) != wanted["models"]:
                patch["models"] = wanted["models"]
            if info.get("max_budget") is None or float(info["max_budget"]) != wanted["max_budget"]:
                patch["max_budget"] = wanted["max_budget"]
            if info.get("budget_duration") != wanted["budget_duration"]:
                patch["budget_duration"] = wanted["budget_duration"]
            if info.get("key_alias") != alias:
                patch["key_alias"] = alias
            if info.get("allowed_routes") != list(LLM_API_ROUTES):
                patch["allowed_routes"] = list(LLM_API_ROUTES)  # LLM routes only (what key_type llm_api set)
            if not patch:
                return "unchanged"
            await admin.update_key(stored, patch)
            await _write(pool_or_conn, kek, stored, alias, wanted)
            return "updated"
    # Missing, revoked/blocked/deleted or undecryptable: replace. Stale keys with our alias (e.g.
    # sealed under a rotated secret) are deleted first so exactly one extraction key exists.
    await admin.delete_alias(alias)
    key = await admin.generate_key({
        "key_alias": alias,
        **wanted,
        "key_type": "llm_api",
        "metadata": {"service": "signal-hub", "purpose": "news extraction (quarantined, no tools)"},
    })
    await _write(pool_or_conn, kek, key, alias, wanted)
    return "generated"


async def _read(pool_or_conn: asyncpg.Pool | asyncpg.Connection, kek: Kek) -> str | None:
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with tx(pool_or_conn, None) as conn:
            return await load_secret(conn, kek, SECRET_NAME)
    await pool_or_conn.execute("SELECT set_config('app.tenant_id', '', false)")
    return await load_secret(pool_or_conn, kek, SECRET_NAME)


async def _write(pool_or_conn: asyncpg.Pool | asyncpg.Connection, kek: Kek, key: str, alias: str,
                 wanted: dict[str, Any]) -> None:
    meta = {"key_alias": alias, **wanted}
    if isinstance(pool_or_conn, asyncpg.Pool):
        async with tx(pool_or_conn, None) as conn:
            await store_secret(conn, kek, SECRET_NAME, key, meta)
        return
    await pool_or_conn.execute("SELECT set_config('app.tenant_id', '', false)")
    await store_secret(pool_or_conn, kek, SECRET_NAME, key, meta)


async def load_stored_key(settings: Settings, pool: asyncpg.Pool) -> str | None:
    """The provisioned key from ``hub_secret`` (``None`` when absent, undecryptable or no secret)."""
    kek = _kek(settings)
    if kek is None:
        return None
    try:
        return await _read(pool, kek)
    except SecretBoxError as exc:
        log.warning("stored extraction key unreadable (secret rotated? re-run signal-hub-bootstrap)",
                    extra={"reason": str(exc)[:200]})
        return None
    except asyncpg.UndefinedTableError:
        return None


async def resolve_llm_key(settings: Settings, pool: asyncpg.Pool) -> Settings:
    """``settings`` with ``litellm_key`` resolved: the environment key wins, else the stored one."""
    if settings.litellm_key:
        return settings if settings.llm_key_source else dataclasses.replace(settings, llm_key_source="env")
    key = await load_stored_key(settings, pool)
    if key:
        log.info("using the provisioned extraction key", extra={"key_alias": settings.llm_key_alias})
        return dataclasses.replace(settings, litellm_key=key, llm_key_source="db")
    log.info("no extraction key (LITELLM_SIGNALS_KEY unset and none provisioned): news stays pending")
    return dataclasses.replace(settings, litellm_key=None, llm_key_source=None)
