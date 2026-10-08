"""The ``llm_replay`` table (migration 0006) behind :class:`PostgresReplayStore` against a real,
migrated database (RLS FORCE, a non-superuser role without BYPASSRLS, exactly like production):
round trip of a recorded call, tenant isolation, size check, housekeeping, re-runnable SQL.

Skipped without a throwaway Postgres (``SCN_TEST_ADMIN_DSN`` / ``E1_TEST_ADMIN_DSN``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fakes.scn_fixture import ADMIN_DSN
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_replay import OTHER, TENANT, _citing_turn, _ev, _request, _run

from keelson_engine.runtime.context import bind_run
from keelson_engine.runtime.llm import FakeLlm, FakeScript
from keelson_engine.runtime.replay import PostgresReplayStore, ReplayLlm, ReplayMiss, ReplayRecord, replay_stats

pytestmark = pytest.mark.asyncio(loop_scope="module")

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ROLE = "keelson_rb_replay_test"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db() -> AsyncIterator[None]:
    import asyncpg

    try:
        admin = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no throwaway Postgres ({type(exc).__name__})")
    name = f"rb_replay_{uuid.uuid4().hex[:8]}"
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await admin.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{name}" OWNER {ROLE}')
    finally:
        await admin.close()
    base = ADMIN_DSN.split("@", 1)[1].rsplit("/", 1)[0]
    ext = await asyncpg.connect(ADMIN_DSN.rsplit("/", 1)[0] + "/" + name, timeout=5)
    try:
        await ext.execute("CREATE EXTENSION IF NOT EXISTS vector")  # 0005 needs it (superuser-only)
    except asyncpg.PostgresError as exc:
        await ext.close()
        pytest.skip(f"pgvector not available ({type(exc).__name__})")
    await ext.close()

    from keelson_engine.config import Settings, set_settings
    from keelson_engine.core.db import close_db, init_db, keelson_engine, system_session
    from keelson_engine.core.migrate import migrate

    set_settings(Settings(keelson_database_url=f"postgresql+asyncpg://{ROLE}:x@{base}/{name}",
                          migrations_dir=MIGRATIONS, log_json=False))
    await init_db()
    try:
        await migrate(keelson_engine())
        async with system_session() as s:
            for slug in (TENANT, OTHER):
                await s.execute(text("INSERT INTO tenant (slug, name) VALUES (:s, :s) ON CONFLICT DO NOTHING"),
                                {"s": slug})
        yield None
    finally:
        await close_db()
        set_settings(None)
        admin = await asyncpg.connect(ADMIN_DSN, timeout=5)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _record(key: str, *, size: int = 10) -> ReplayRecord:
    return ReplayRecord(key=key, alias="fast-default", agent="critic", spec_version="default", model="gemini-x",
                        responses=[{"content": {"role": "model", "parts": [{"text": "⟦case⟧ ok"}]}}],
                        usage={"total_token_count": 7}, bytes=size)


async def test_table_has_forced_rls_and_the_migration_is_rerunnable(db: None) -> None:
    from keelson_engine.core.db import keelson_engine, system_session

    async with system_session() as s:
        flags = (await s.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                      "WHERE relname = 'llm_replay'"))).one()
    assert tuple(flags) == (True, True)
    sql = (MIGRATIONS / "0006_llm_replay.sql").read_text(encoding="utf-8")
    async with keelson_engine().connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        async with raw.transaction():
            await raw.execute(sql)


async def test_round_trip_and_tenant_isolation(db: None) -> None:
    store = PostgresReplayStore()
    key = uuid.uuid4().hex * 2
    await store.put(tenant=TENANT, record=_record(key))
    got = await store.get(tenant=TENANT, key=key)
    assert got is not None and got.responses[0]["content"]["parts"][0]["text"] == "⟦case⟧ ok"
    assert (got.alias, got.agent, got.model, got.usage, got.bytes) == ("fast-default", "critic", "gemini-x",
                                                                      {"total_token_count": 7}, 10)
    assert isinstance(got.created_at, dt.datetime)
    # the other tenant neither sees it nor overwrites it (same key, own row)
    assert await store.get(tenant=OTHER, key=key) is None
    await store.put(tenant=OTHER, record=_record(key, size=11))
    assert (await store.get(tenant=TENANT, key=key)).bytes == 10  # type: ignore[union-attr]
    # upsert within a tenant (record mode re-records)
    await store.put(tenant=TENANT, record=_record(key, size=12))
    assert (await store.get(tenant=TENANT, key=key)).bytes == 12  # type: ignore[union-attr]

    from keelson_engine.core import repo
    from keelson_engine.core.db import tenant_session

    # a tenant session cannot write a row for another tenant (WITH CHECK)
    with pytest.raises(DBAPIError):
        async with tenant_session(TENANT) as s:
            await s.execute(text("INSERT INTO llm_replay (tenant, key, alias, model, responses, bytes) "
                                 "VALUES (:t, :k, 'a', 'm', '[]'::jsonb, 1)"), {"t": OTHER, "k": "e" * 64})
    assert await repo.delete_llm_replays(tenant=OTHER) == 1
    assert await store.get(tenant=TENANT, key=key) is not None
    assert await repo.delete_llm_replays(older_than=dt.datetime(2000, 1, 1, tzinfo=dt.UTC), tenant=TENANT) == 0


async def test_size_and_key_checks(db: None) -> None:
    store = PostgresReplayStore()
    with pytest.raises(DBAPIError):
        await store.put(tenant=TENANT, record=_record("f" * 64, size=8 * 1024 * 1024 + 1))
    with pytest.raises(DBAPIError):
        await store.put(tenant=TENANT, record=_record("not-a-sha"))


async def test_record_then_replay_through_postgres(db: None) -> None:
    store = PostgresReplayStore()
    run1 = _run()
    script = FakeScript({"inv": [_citing_turn]})
    with bind_run(run1):
        model = ReplayLlm(inner=FakeLlm(agent="inv", script=script), alias="fast-default", mode="record_or_replay",
                          store=store)
        [live] = [r async for r in model.generate_content_async(_request(run1, [_ev(), _ev()]))]
    assert replay_stats(run1.run_id).recorded == 1

    run2 = _run()
    empty = FakeScript()
    ev2 = [_ev(), _ev()]
    with bind_run(run2):
        model = ReplayLlm(inner=FakeLlm(agent="inv", script=empty), alias="fast-default", mode="replay", store=store)
        [again] = [r async for r in model.generate_content_async(_request(run2, ev2))]
    assert empty.requests == {}
    text_part = next(p.text for p in again.content.parts if p.text and not p.thought)
    assert ev2[0] in text_part and run2.case_id in text_part
    assert again.content.parts[0].thought and again.usage_metadata.total_token_count == 0
    assert live.usage_metadata.total_token_count == 1234

    # the same request in another tenant: fail closed
    run3 = _run(OTHER)
    with bind_run(run3), pytest.raises(ReplayMiss):
        model = ReplayLlm(inner=FakeLlm(agent="inv", script=empty), alias="fast-default", mode="replay", store=store)
        _: Any = [r async for r in model.generate_content_async(_request(run3, [_ev(), _ev()]))]
