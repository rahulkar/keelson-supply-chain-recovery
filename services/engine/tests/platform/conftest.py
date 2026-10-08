"""Platform (E1) test fixtures.

Needs a throwaway Postgres 18 (superuser DSN ``E1_TEST_ADMIN_DSN``) and Valkey
(``E1_TEST_VALKEY_URL``); see README › Tests. The session creates a login role without
BYPASSRLS + a fresh database owned by it (so RLS applies exactly as in production, where
the engine connects as the non-superuser owner ``keelson``) and drops the database at the end.

All tests here run on one session-scoped event loop (the DB engine, Valkey client and
HTTP clients are process-wide singletons, as in the service).
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).parent / "fakes"))

from e1_browser import Browser, login_as  # noqa: E402
from e1_env import (  # noqa: E402
    HAVE_PACKS, KEELSON_ISS, LITELLM, MASTER_KEY, MERXA_ISS, MERXA_MCP, PUBLIC, REPO_PACKS, TENANT_A, TENANT_B,
)
from e1_idp import FakeRealm  # noqa: E402
from e1_embeddings import FakeEmbeddings  # noqa: E402
from e1_litellm import FakeLiteLLM  # noqa: E402
from keelson_engine.config import Settings, set_settings  # noqa: E402
from keelson_engine.core import crypto  # noqa: E402
from keelson_engine.core.db import close_db, init_db, keelson_engine, system_session  # noqa: E402
from keelson_engine.core.events import set_event_bus  # noqa: E402
from keelson_engine.core.http import close_http, set_http_clients  # noqa: E402
from keelson_engine.core.migrate import migrate  # noqa: E402
from keelson_engine.core.tokens import set_token_broker  # noqa: E402
from keelson_engine.core.valkey import close_valkey, get_valkey  # noqa: E402

ADMIN_DSN = os.environ.get("E1_TEST_ADMIN_DSN", "postgresql://postgres:test@e1-test-pg:5432/postgres")
VALKEY_URL = os.environ.get("E1_TEST_VALKEY_URL", "redis://:test@e1-test-valkey:6379/0")
APP_ROLE = "keelson_e1_test"
APP_ROLE_PASSWORD = "keelson-e1-test"

# ───────────────────────────── database ─────────────────────────────


def _db_url(dsn: str, name: str, user: str, password: str) -> str:
    rest = dsn.split("@", 1)[1].rsplit("/", 1)[0]
    return f"postgresql+asyncpg://{user}:{password}@{rest}/{name}"


async def create_extensions(db_name: str) -> None:
    """Like pg-core's init script: pgvector is not a trusted extension, so the superuser creates it
    (migration 0005 then only finds it with ``CREATE EXTENSION IF NOT EXISTS``)."""
    conn = await asyncpg.connect(ADMIN_DSN.rsplit("/", 1)[0] + "/" + db_name)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def database() -> AsyncIterator[str]:
    name = f"keelson_t_{uuid.uuid4().hex[:10]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE)
        if not exists:
            await conn.execute(
                f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_ROLE_PASSWORD}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
            )
        await conn.execute(f'CREATE DATABASE "{name}" OWNER {APP_ROLE}')
    finally:
        await conn.close()
    await create_extensions(name)
    try:
        yield _db_url(ADMIN_DSN, name, APP_ROLE, APP_ROLE_PASSWORD)
    finally:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()


@pytest.fixture(scope="session")
def secrets_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("secrets")
    for name in ("keelson_client_assertion_key", "keelson_receipt_key"):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        (d / name).write_bytes(pem)
    (d / "keelson_data_key").write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
    return d


@pytest.fixture(scope="session")
def tenants_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo_file = Path(__file__).resolve().parents[4] / "infra" / "tenants.yaml"
    d = tmp_path_factory.mktemp("cfg")
    target = d / "tenants.yaml"
    content = repo_file.read_text(encoding="utf-8")
    if not HAVE_PACKS:  # test image without data/packs: bootstrap without pack application
        import yaml

        data = yaml.safe_load(content)
        for t in data["tenants"]:
            t.pop("pack", None)
        content = yaml.safe_dump(data)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture(scope="session")
def models_lock(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo_file = Path(__file__).resolve().parents[4] / "models.lock"
    d = tmp_path_factory.mktemp("models")
    target = d / "models.lock"
    target.write_text(repo_file.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture(scope="session")
def settings(database: str, secrets_dir: Path, tenants_file: Path, models_lock: Path) -> Settings:
    s = Settings(
        keelson_database_url=database,
        valkey_url=VALKEY_URL,
        oidc_issuer=KEELSON_ISS,
        keelson_bff_client_secret="bff-secret",
        keelson_demo_client_secret="demo-secret",
        demo_user_password="pw",
        merxa_issuer=MERXA_ISS,
        merxa_mcp_url=MERXA_MCP,
        public_url=PUBLIC,
        public_base_domain="localhost",
        session_secret="test-session-secret",
        demo_access_secret="demo-access",
        demo_mode=True,
        connector_proxy_url=None,
        litellm_base_url=LITELLM,
        litellm_master_key=MASTER_KEY,
        kc_admin_password="",
        secrets_dir=secrets_dir,
        tenants_file=tenants_file,
        models_lock=models_lock,
        packs_dir=REPO_PACKS if HAVE_PACKS else secrets_dir / "no-packs",
        migrations_dir=Path(__file__).resolve().parents[2] / "migrations",
        kb_seed_dir=secrets_dir / "no-kb-seed",  # bootstrap seeds nothing unless a test points it elsewhere
        log_json=False,
    )
    set_settings(s)
    crypto.reset_keys()
    return s


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db(settings: Settings) -> AsyncIterator[None]:
    await init_db(settings)
    await migrate(keelson_engine())
    async with system_session() as s:
        for slug, name in ((TENANT_A, "Demo Retail"), (TENANT_B, "Northwind test")):
            await s.execute(
                text("INSERT INTO tenant (slug, name) VALUES (:s, :n) ON CONFLICT (slug) DO NOTHING"),
                {"s": slug, "n": name},
            )
    await get_valkey().flushdb()
    try:
        yield
    finally:
        await close_valkey()
        await close_http()
        await close_db()
        set_settings(None)


# ───────────────────────────── identity providers ─────────────────────────────


@pytest.fixture(scope="session")
def keelson_realm() -> FakeRealm:
    realm = FakeRealm(KEELSON_ISS)
    realm.add_client("keelson-bff", secret="bff-secret")
    realm.add_client("keelson-demo", secret="demo-secret", extra_claims={"demo": True})
    people = [
        ("priya", "Priya Shah", ["planner"], [TENANT_A]),
        ("marco", "Marco Rossi", ["planner", "approver"], [TENANT_A]),
        ("lena", "Lena Vogel", ["compliance"], [TENANT_A]),
        ("sam", "Sam Okafor", ["tenant-admin", "planner"], [TENANT_A]),
        ("ava", "Ava Lindqvist", ["platform-admin"], [TENANT_A]),
        ("nora", "Nora Test", ["planner"], [TENANT_B]),
    ]
    for username, name, roles, tenants in people:
        realm.add_user(username, roles=roles, name=name, tenants=tenants)
    return realm


@pytest.fixture(scope="session")
def merxa_realm() -> FakeRealm:
    realm = FakeRealm(MERXA_ISS, jwks_provider=crypto.jwks)
    reads = "catalog:read inventory:read po:read orders:read snapshot:read"
    realm.add_client("keelson-reader", private_key_jwt=True, default_scope=reads,
                     service_roles=["integration-writer"], audience=MERXA_MCP)
    realm.add_client("keelson-preparer", private_key_jwt=True, default_scope=f"changes:preview {reads}",
                     service_roles=["integration-writer"], audience=MERXA_MCP)
    realm.add_client("keelson-autocommit", private_key_jwt=True, default_scope="changes:apply:auto",
                     service_roles=["integration-writer"], audience=MERXA_MCP)
    realm.add_client("keelson-connect", private_key_jwt=True, audience=MERXA_MCP)
    realm.add_user("marco.rossi", roles=["ops-director", "offline_access"], name="Marco Rossi")
    realm.add_user("priya.shah", roles=["merch-planner"], name="Priya Shah")
    return realm


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def http_mock(
    settings: Settings, keelson_realm: FakeRealm, merxa_realm: FakeRealm
) -> AsyncIterator[respx.Router]:
    router = respx.Router(assert_all_called=False, assert_all_mocked=True)
    keelson_realm.install(router)
    merxa_realm.install(router)
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))
    connector_client = httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler))
    set_http_clients(internal=client, connector=connector_client)
    yield router


@pytest_asyncio.fixture(loop_scope="session")
async def platform(db: None, http_mock: respx.Router) -> AsyncIterator[None]:
    """DB + Valkey + mocked IdPs; fresh EventBus/TokenBroker singletons per test."""
    set_event_bus(None)
    set_token_broker(None)
    from keelson_engine.api import routes_auth, routes_system

    routes_auth.reset_keelson_oidc()
    routes_system._ready_cache = None
    yield


@pytest.fixture
def litellm_fake(http_mock: respx.Router) -> FakeLiteLLM:
    fake = FakeLiteLLM(LITELLM, MASTER_KEY)
    fake.install(http_mock)
    return fake


@pytest.fixture
def embeddings(http_mock: respx.Router) -> FakeEmbeddings:
    """Fake LiteLLM ``POST /embeddings`` (deterministic hashed bag-of-words vectors)."""
    fake = FakeEmbeddings(LITELLM)
    fake.install(http_mock)
    return fake


# ───────────────────────────── API client ─────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def api(platform: None) -> AsyncIterator[httpx.AsyncClient]:
    from keelson_engine.api.main import create_app

    app = create_app(manage_resources=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as client:
        yield client


@pytest.fixture
def browser_factory(api: httpx.AsyncClient) -> Any:
    async def make(username: str) -> Browser:
        return await login_as(api, username)

    return make


# ───────────────────────────── back-office (M2) ─────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def gateway_ready(platform: None, settings: Settings, litellm_fake: FakeLiteLLM) -> FakeLiteLLM:
    """Fresh fake LiteLLM with the models.lock aliases, one team + key per test tenant."""
    from keelson_engine.bootstrap import _model_spec, load_models_lock
    from keelson_engine.core import connectors, gateway
    from keelson_engine.core.tenants import provision_litellm

    lock = load_models_lock(Path(settings.models_lock))
    admin = gateway.get_admin()
    for alias, spec in lock.items():
        params, info = _model_spec(spec)
        await admin.upsert_model(alias, params, info)
    for tenant, name in ((TENANT_A, "Demo Retail"), (TENANT_B, "Northwind test")):
        await provision_litellm(admin, tenant, name, {"budget_eur": 50, "rpm_limit": 120, "tpm_limit": 400000},
                                sorted(lock))
    connectors.clear_llm_key_cache()
    return litellm_fake


@pytest.fixture
def session_browser(api: httpx.AsyncClient) -> Any:
    """A browser for a user that is not a demo persona (e.g. a tenant-admin of northwind-test)."""
    from keelson_engine.api.sessions import create_session

    async def make(username: str, roles: list[str], tenants: list[str]) -> Browser:
        cookie, sess = await create_session(
            sub=f"sub-{username}", username=username, name=username.title(), email=None, roles=roles,
            tenants=tenants, active_tenant=tenants[0] if tenants else None, tokens={}, demo=True,
            auth_time=None, merxa_username=None,
        )
        return Browser(api, cookie, sess.csrf_token, {"user": sess.user_json()})

    return make
