"""One-shot bootstrap: ``python -m signal_hub.bootstrap`` (idempotent; exit 1 on failure).

1. As the pg-core **superuser** (``SIGNALS_ADMIN_DSN``) — only for what needs it:
   ``CREATE ROLE signals`` (or re-sync its password), ``CREATE DATABASE signals OWNER signals``,
   ``CREATE EXTENSION postgis`` inside it, and ``CREATE EXTENSION vector`` (pinned by
   ``SIGNALS_VECTOR_VERSION``) **when the server offers it** — optional, a missing pgvector only
   turns the opt-in remote embedding profile off (``docs/contracts/123`` §6).
2. As role **signals** (``SIGNALS_DATABASE_URL``): the numbered migrations (tables, RLS FORCE), then
   the active remote embedding profile when ``SIGNALS_EMBED_PROVIDER=litellm``
   (:func:`signal_hub.vectors.ensure_profile`; never fatal).
3. With ``LITELLM_MASTER_KEY`` (given to this one-shot container only): upsert the dedicated
   extraction virtual key and store it encrypted in ``hub_secret`` (:mod:`signal_hub.llm_key`).
   Optional: without the master key, or when LiteLLM is not ready within ``SIGNALS_LITELLM_WAIT_S``
   or refuses, the step is skipped with a warning (news stays ``pending_extraction``) and the
   bootstrap still succeeds, so the hub never depends on the gateway to start.

The superuser connection is closed before step 2; nothing else ever uses it. Neither the master
key nor the virtual key is ever logged.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx

from signal_hub.config import Settings, load_settings, read_secret
from signal_hub.llm_key import LiteLlmAdmin, provision_signals_key, redact
from signal_hub.logs import configure_logging, get_logger
from signal_hub.migrate import migrate
from signal_hub.timeutil import utc_now
from signal_hub.vectors import ensure_profile

__all__ = ["BootstrapError", "check_rls_role", "ensure_database", "main", "provision_llm_key", "run_bootstrap"]

log = get_logger("signal_hub.bootstrap")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}(\.[0-9]{1,3})?$")


class BootstrapError(RuntimeError):
    pass


def _with_database(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def _connect_retry(dsn: str, what: str, timeout_s: float = 120.0) -> asyncpg.Connection:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            return await asyncpg.connect(dsn, timeout=10)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(2)
    raise BootstrapError(f"{what} not reachable after {timeout_s:.0f}s ({last})")


async def ensure_database(admin_dsn: str, *, role: str, password: str, database: str,
                          postgis_version: str | None = None, vector_version: str | None = None) -> dict[str, bool]:
    """Superuser/admin part (idempotent). Returns what was created.

    Works with a real superuser (compose pg-core) and with Cloud SQL's ``postgres`` (CREATEROLE +
    CREATEDB, not superuser): since PG16 the admin needs SET/INHERIT membership in the role to create
    a database owned by it, so it grants itself that membership (like ``keelson-db-init``)."""
    if not _IDENT_RE.match(role) or not _IDENT_RE.match(database):
        raise BootstrapError("role and database names must be simple lowercase identifiers")
    if postgis_version is not None and not _VERSION_RE.match(postgis_version):
        raise BootstrapError("SIGNALS_POSTGIS_VERSION must look like 3.6.0")
    if vector_version is not None and not _VERSION_RE.match(vector_version):
        raise BootstrapError("SIGNALS_VECTOR_VERSION must look like 0.8.5")
    created = {"role": False, "database": False, "postgis": False, "vector": False}
    conn = await _connect_retry(admin_dsn, "pg-core (admin)")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if not exists:
            # NOSUPERUSER NOBYPASSRLS: the service role is always subject to RLS.
            await conn.execute(await conn.fetchval(
                "SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', $1::text)",
                role))
        await conn.execute(await conn.fetchval(
            "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', $1::text, $2::text)", role, password))
        created["role"] = not exists
        member = await conn.fetchval(
            """SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
               WHERE r.rolname = $1 AND m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
                 AND m.inherit_option AND m.set_option""", role)
        is_super = await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if not member and not is_super:
            await conn.execute(await conn.fetchval(
                "SELECT format('GRANT %I TO CURRENT_USER WITH INHERIT TRUE, SET TRUE', $1::text)", role))
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await conn.execute(await conn.fetchval(
                "SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', $1::text, $2::text, 'UTF8')",
                database, role))
            created["database"] = True
        await conn.execute(await conn.fetchval("SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', $1::text)", database))
        await conn.execute(await conn.fetchval(
            "SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', $1::text, $2::text)", database, role))
    finally:
        await conn.close()
    conn = await _connect_retry(_with_database(admin_dsn, database), "signals database (admin)")
    try:
        have = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
        if have is None:
            if postgis_version:
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS postgis VERSION '{postgis_version}'")
            else:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            created["postgis"] = True
        elif postgis_version and have != postgis_version:
            log.warning("postgis version differs from the pin", extra={"have": have, "pinned": postgis_version})
        created["vector"] = await _ensure_vector(conn, vector_version)
        # The owner role creates its own objects in schema public.
        await conn.execute(await conn.fetchval("SELECT format('GRANT ALL ON SCHEMA public TO %I', $1::text)", role))
    finally:
        await conn.close()
    return created


async def _ensure_vector(conn: asyncpg.Connection, version: str | None) -> bool:
    """``CREATE EXTENSION vector`` when the server offers it (optional; never fatal). True = created."""
    have = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    if have is not None:
        if version and have != version:
            log.warning("pgvector version differs from the pin", extra={"have": have, "pinned": version})
        return False
    if not await conn.fetchval("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"):
        log.warning("pgvector is not available on this server: the remote embedding profile stays off")
        return False
    try:
        if version:
            await conn.execute(f"CREATE EXTENSION IF NOT EXISTS vector VERSION '{version}'")
        else:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except asyncpg.PostgresError as exc:
        log.warning("pgvector not created: the remote embedding profile stays off",
                    extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        return False
    return True


async def check_rls_role(conn: asyncpg.Connection) -> None:
    """Refuse a service role that row-level security does not apply to.

    ``FORCE ROW LEVEL SECURITY`` binds the table owner, but never a superuser or a ``BYPASSRLS``
    role. A pre-existing ``signals`` role with either attribute (the bootstrap does not ALTER an
    existing role: Cloud SQL's admin may not) would silently disable tenant isolation."""
    row = await conn.fetchrow("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    if row is None or row["rolsuper"] or row["rolbypassrls"]:
        name = row["rolname"] if row is not None else "?"
        raise BootstrapError(
            f"role {name!r} is a superuser or has BYPASSRLS: row-level security would not isolate tenants "
            "(ALTER ROLE ... NOSUPERUSER NOBYPASSRLS, or use a dedicated role)")


async def provision_llm_key(settings: Settings, conn: asyncpg.Connection, master_key: str | None, *,
                            http: httpx.AsyncClient | None = None) -> str:
    """Step 3 (never fatal): ``skipped`` / ``unavailable`` / ``failed`` / the provisioning result."""
    if not master_key:
        log.info("LITELLM_MASTER_KEY not set: extraction key not provisioned")
        return "skipped"
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), trust_env=False)
    try:
        admin = LiteLlmAdmin(client, base_url=settings.litellm_base_url, master_key=master_key)
        if not await admin.wait_ready(settings.litellm_wait_s):
            log.warning("LiteLLM not ready: extraction key not provisioned (news stays pending; re-run the bootstrap)",
                        extra={"waited_s": settings.litellm_wait_s})
            return "unavailable"
        try:
            result = await provision_signals_key(settings, conn, admin)
        except Exception as exc:
            # Never fatal: the hub must start without the gateway
            # (LiteLLM refusal, transport error, malformed answer, DB write…). Redacted again in case
            # an unexpected error text carries a key; CancelledError (BaseException) still propagates.
            text = redact(f"{type(exc).__name__}: {exc}", master_key)[:300]
            log.warning("extraction key not provisioned", extra={"error": text})
            return "failed"
        log.info("extraction key provisioned", extra={"key_alias": settings.llm_key_alias, "result": result})
        return result
    finally:
        if http is None:
            await client.aclose()


async def run_bootstrap(settings: Settings, *, master_key: str | None = None,
                        http: httpx.AsyncClient | None = None) -> list[str]:
    if not settings.database_url:
        raise BootstrapError("SIGNALS_DATABASE_URL is not set")
    if settings.admin_dsn:
        if not settings.signals_db_password:
            raise BootstrapError("SIGNALS_DB_PASSWORD is not set")
        created = await ensure_database(
            settings.admin_dsn,
            role=settings.signals_role,
            password=settings.signals_db_password,
            database=settings.signals_db_name,
            postgis_version=settings.postgis_version,
            vector_version=settings.vector_version,
        )
        log.info("database ensured", extra=created)
    else:
        log.info("SIGNALS_ADMIN_DSN not set: assuming role/database/PostGIS exist (managed database)")
    conn = await _connect_retry(settings.database_url, "signals database (role signals)")
    try:
        await check_rls_role(conn)
        applied = await migrate(conn)
        log.info("migrations ready", extra={"applied": applied})
        try:
            await ensure_profile(conn, settings, now=utc_now())
        except (asyncpg.PostgresError, ValueError) as exc:  # never fatal: local clustering needs no profile
            log.warning("embedding profile not ensured", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
        await provision_llm_key(settings, conn, master_key, http=http)
    finally:
        await conn.close()
    return applied


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run_bootstrap(settings, master_key=read_secret("LITELLM_MASTER_KEY")))
    except Exception:
        log.exception("bootstrap failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
