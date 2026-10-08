"""REST backend (read-only in M8): a JSON API crawled into staging tables.

Each table names an ``endpoint`` (``path``, fixed ``params``, ``items_path``, pagination
``page`` / ``offset`` / ``cursor`` / ``link``) and per column a JSONPath-lite ``path`` into one
item (``keelson_contracts.mapping.parse_path``; default ``$.<column>``). A crawl pages through every
endpoint, types the values, and replaces the staging tables in one transaction — so the entity SQL
and the ``scn/1.0`` export work exactly as for the other backends.

A crawl is not a point-in-time read: snapshots are labelled ``consistency: crawled`` with an
``as_of_range`` (first request … last response), which Keelson treats like a tier-A crawl (plans
on it are advisory). ``scn.changes_since`` re-crawls at most every ``min_crawl_interval_s`` and
reports changed tables as external drift.

Outbound safety: ``httpx`` with ``follow_redirects=False`` and ``trust_env=False`` (optional
``OUTBOUND_PROXY``); every next-page URL must stay on the ``base_url`` origin
(``keelson_contracts.scn.part_uri_allowed``); per-response byte cap and ``max_pages`` cap (a crawl
that hits a cap fails — a truncated table is never served).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

import httpx
from keelson_contracts.mapping import parse_path
from keelson_contracts.scn import part_uri_allowed
from sqlalchemy.ext.asyncio import AsyncEngine

from adapter_sidecar.backends import Backend
from adapter_sidecar.backends import staging
from adapter_sidecar.config import Endpoint, RestBackend as RestOpts, TableSpec
from adapter_sidecar.db import begin_write
from adapter_sidecar.errors import ConfigError, SourceDataError
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.I)


def eval_path(path: str, node: Any) -> Any:
    """JSONPath-lite over plain JSON (missing → ``None``)."""
    for seg in parse_path(path).segments:
        if isinstance(seg, int):
            node = node[seg] if isinstance(node, list) and 0 <= seg < len(node) else None
        else:
            node = node.get(seg) if isinstance(node, Mapping) else None
        if node is None:
            return None
    return node


class RestError(Exception):
    """A crawl failed (``code`` is client-safe; URLs carry no secrets: auth is in headers)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RestBackend(Backend):
    kind = "rest"
    shared_state = True

    def __init__(self, *args: Any, client: httpx.AsyncClient | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        opts = self.cfg.backend.rest
        assert opts is not None
        self.opts: RestOpts = opts
        self._client = client
        self._token: tuple[str, float] | None = None
        self._last_crawl = float("-inf")
        auth = opts.auth
        for name in (auth.token_env, auth.client_id_env, auth.client_secret_env):
            if name and not self.settings.secret(name):
                raise ConfigError(f"{name} must be set for the rest backend's auth")

    @property
    def engine(self) -> AsyncEngine:
        return self.state

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, proxy=self.settings.outbound_proxy,
                timeout=httpx.Timeout(float(self.opts.timeout_s)),
            )
        return self._client

    async def start(self) -> None:
        async with begin_write(self.state) as conn:
            await staging.ensure_tables(conn, self.cfg.tables)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> None:
        if not self.cfg.tables:
            return
        name, table = next(iter(self.cfg.tables.items()))
        assert table.endpoint is not None
        await self._get(self.opts.base_url + table.endpoint.path, dict(table.endpoint.params))

    # ───────────── auth ─────────────

    async def _headers(self) -> dict[str, str]:
        auth = self.opts.auth
        headers = {"Accept": "application/json"}
        if auth.kind == "header" and auth.header and auth.token_env:
            headers[auth.header] = self.settings.secret(auth.token_env) or ""
        elif auth.kind == "oauth_client_credentials":
            headers["Authorization"] = f"Bearer {await self._client_credentials()}"
        return headers

    async def _client_credentials(self) -> str:
        if self._token is not None and self._token[1] > time.monotonic() + 30:
            return self._token[0]
        auth = self.opts.auth
        assert auth.token_url and auth.client_id_env and auth.client_secret_env
        form = {"grant_type": "client_credentials"}
        if auth.scope:
            form["scope"] = auth.scope
        resp = await self._http().post(
            auth.token_url, data=form,
            auth=(self.settings.secret(auth.client_id_env) or "", self.settings.secret(auth.client_secret_env) or ""),
        )
        if resp.status_code != 200:
            raise RestError("auth_failed", f"token endpoint answered HTTP {resp.status_code}")
        body = resp.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise RestError("auth_failed", "token endpoint returned no access_token")
        ttl = body.get("expires_in") if isinstance(body.get("expires_in"), int) else 300
        self._token = (token, time.monotonic() + ttl)
        return token

    # ───────────── fetch ─────────────

    async def _get(self, url: str, params: dict[str, Any] | None) -> tuple[Any, httpx.Headers]:
        if not part_uri_allowed(url, [self.opts.base_url]):
            raise RestError("foreign_host", f"{url.split('?')[0][:120]} is not on the base_url origin")
        cap = self.opts.max_response_bytes
        try:
            async with self._http().stream("GET", url, params=params, headers=await self._headers()) as resp:
                if resp.status_code != 200:
                    raise RestError("http_error", f"HTTP {resp.status_code} from {resp.url.path}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        raise RestError("response_too_large", f"{resp.url.path} exceeds {cap} bytes")
                    chunks.append(chunk)
                headers = resp.headers
        except httpx.HTTPError as exc:
            raise RestError("unreachable", f"{type(exc).__name__}") from None
        try:
            return json.loads(b"".join(chunks)), headers
        except ValueError:
            raise RestError("invalid_json", "the response is not JSON") from None

    async def _crawl_table(self, name: str, table: TableSpec) -> list[list[Any]]:
        ep: Endpoint = table.endpoint  # type: ignore[assignment]
        pag = ep.pagination
        url = self.opts.base_url + ep.path
        base_params: dict[str, Any] = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in ep.params.items()}
        if pag is not None and pag.size_param:
            base_params[pag.size_param] = pag.size
        items: list[tuple[int, Any]] = []
        cursor: Any = None
        next_url: str | None = None
        for page in range(self.opts.max_pages + 1):
            if page == self.opts.max_pages:
                raise RestError("max_pages", f"{name}: more than {self.opts.max_pages} pages")
            params = dict(base_params)
            target = url
            if pag is not None:
                if pag.kind == "page":
                    params[pag.param] = pag.start + page  # type: ignore[index]
                elif pag.kind == "offset":
                    params[pag.param] = page * pag.size  # type: ignore[index]
                elif pag.kind == "cursor" and cursor is not None:
                    params[pag.param] = cursor  # type: ignore[index]
                elif pag.kind == "link" and next_url is not None:
                    target, params = next_url, {}
            body, headers = await self._get(target, params)
            found = eval_path(ep.items_path, body)
            if found is None:
                found = []
            if not isinstance(found, list):
                raise RestError("invalid_items", f"{name}: items_path does not point to a list")
            base = len(items)
            items.extend((base + i + 1, it) for i, it in enumerate(found))
            if pag is None:
                break
            if pag.kind in ("page", "offset"):
                if len(found) < pag.size:
                    break
            elif pag.kind == "cursor":
                cursor = eval_path(pag.cursor_path or "$", body)
                if cursor in (None, "") or not found:
                    break
            else:
                nxt = eval_path(pag.next_path, body) if pag.next_path else _link_next(headers)
                if not nxt:
                    break
                nxt = urljoin(target, str(nxt))
                if not part_uri_allowed(nxt, [self.opts.base_url]):
                    raise RestError("foreign_next_link", f"{name}: a next-page link leaves the base_url origin")
                next_url = nxt
        return self._typed(name, table, items)

    def _typed(self, name: str, table: TableSpec, items: list[tuple[int, Any]]) -> list[list[Any]]:
        types = table.column_types
        out: list[list[Any]] = []
        seen: set[tuple[Any, ...]] = set()
        key_idx = [list(types).index(k) for k in table.key]
        for n, item in items:
            if not isinstance(item, Mapping):
                raise SourceDataError(name, "item is not an object", row=n)
            row: list[Any] = []
            for col, kind in types.items():
                try:
                    row.append(staging.coerce_json(kind, eval_path(table.column_path(col), item)))
                except ValueError as exc:
                    raise SourceDataError(name, str(exc), row=n, column=col) from None
            key = tuple(row[i] for i in key_idx)
            if any(v is None for v in key):
                raise SourceDataError(name, f"empty key {table.key}", row=n)
            if key in seen:
                raise SourceDataError(name, f"duplicate key {list(key)}", row=n)
            seen.add(key)
            out.append(row)
        return out

    async def refresh(self, *, force: bool = False) -> list[str]:
        if not force and time.monotonic() - self._last_crawl < self.opts.min_crawl_interval_s:
            return []
        started = dt.datetime.now(dt.UTC)
        crawled: dict[str, list[list[Any]]] = {}
        for name, table in self.cfg.tables.items():
            crawled[name] = await self._crawl_table(name, table)
            await asyncio.sleep(0)
        finished = dt.datetime.now(dt.UTC)
        external: list[str] = []
        async with begin_write(self.state) as conn:
            for name, rows in crawled.items():
                sha = staging.rows_fingerprint(rows)
                prev = await self._store_fingerprint(conn, f"rest:{name}", sha)
                if prev == sha:
                    continue
                await staging.replace_rows(conn, name, self.cfg.tables[name], rows)
                if prev is not None:
                    external.append(name)
        self._last_crawl = time.monotonic()
        self.as_of_range = (started, finished)
        log.info("rest crawl finished", extra={"tables": {n: len(r) for n, r in crawled.items()},
                                               "changed": external})
        return external


def _link_next(headers: httpx.Headers) -> str | None:
    for value in headers.get_list("link"):
        m = _LINK_NEXT_RE.search(value)
        if m:
            return m.group(1)
    return None
