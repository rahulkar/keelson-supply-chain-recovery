"""Migrations are idempotent; RLS isolates tenants; system_session sees everything."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from e1_env import TENANT_A, TENANT_B
from keelson_engine.core import repo
from keelson_engine.core.db import keelson_engine, system_session, tenant_session
from keelson_engine.core.migrate import Migration, MigrationError, discover, migrate

pytestmark = pytest.mark.asyncio(loop_scope="session")

TENANT_TABLES = {
    "tenant_llm_key", "admin_audit", "case", "case_message", "run", "run_event", "interrupt", "approval_decision",
    "resume_request", "scenario", "plan", "investigation_task", "finding", "evidence", "calc", "block",
    "change_set", "commit_ledger", "connector", "connector_tool", "connector_credential",
    "connector_identity_link", "agent_spec", "connector_event",
    # M3 canonical store (0004_scn)
    "scn_snapshot", "scn_current", "scn_pin", "scn_sync_state", "scn_location", "scn_item", "scn_bom_line",
    "scn_lane", "scn_lane_rate", "scn_sailing", "scn_po_line", "scn_production_order", "scn_inventory_position",
    "scn_demand_forecast", "scn_size_curve", "scn_allocation", "scn_transfer_order_line", "scn_online_order_line",
    "scn_promotion",
    # M4 knowledge base (0005_kb; the kb_embedding_<profile> tables are checked in test_kb.py)
    "kb", "kb_document", "kb_chunk", "kb_reembed_job",
    # M6 Signal Hub integration (0009_m6_signals)
    "alert_case",
}


async def test_migrations_are_idempotent(platform: None) -> None:
    assert await migrate(keelson_engine()) == []
    assert await migrate(keelson_engine()) == []
    async with system_session() as s:
        rows = (await s.execute(text("SELECT version FROM schema_migrations ORDER BY version"))).all()
    assert [r[0] for r in rows] == [m.version for m in discover()]


async def test_migration_sql_itself_is_rerunnable(platform: None) -> None:
    """Second line of defence: re-executing every migration on a migrated DB does not fail."""
    for migration in discover():
        async with keelson_engine().connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            async with raw.transaction():
                await raw.execute(migration.sql)


async def test_changed_migration_is_rejected(platform: None, tmp_path) -> None:  # type: ignore[no-untyped-def]
    for m in discover():
        (tmp_path / f"{m.version}_{m.name}.sql").write_text(m.sql + "\n-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="changed after being applied"):
        await migrate(keelson_engine(), tmp_path)


async def test_rls_forced_on_every_tenant_table(platform: None) -> None:
    async with system_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
                )
            )
        ).all()
    flags = {r[0]: (r[1], r[2]) for r in rows}
    for table in TENANT_TABLES:
        assert flags[table] == (True, True), table
    # the connecting role is the owner and has no BYPASSRLS / superuser
    async with system_session() as s:
        role = (await s.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
    assert tuple(role) == (False, False)


async def test_tenant_cannot_read_other_tenant_rows(platform: None) -> None:
    a = await repo.create_case("A case", created_by={"sub": "u-a", "name": "A"}, tenant=TENANT_A)
    b = await repo.create_case("B case", created_by={"sub": "u-b", "name": "B"}, tenant=TENANT_B)
    await repo.add_message(a, "user", "hello A", tenant=TENANT_A)

    assert await repo.get_case(b, tenant=TENANT_A) is None
    assert await repo.get_case(a, tenant=TENANT_B) is None
    assert all(c["tenant"] == TENANT_A for c in await repo.list_cases(200, tenant=TENANT_A))
    assert await repo.list_messages(a, tenant=TENANT_B) == []

    async with tenant_session(TENANT_B) as s:
        count = (await s.execute(text('SELECT count(*) FROM "case" WHERE id = :a'), {"a": a})).scalar_one()
        assert count == 0
        # updates/deletes of foreign rows silently affect nothing
        res = await s.execute(text('UPDATE "case" SET title = \'pwned\' WHERE id = :a RETURNING id'), {"a": a})
        assert res.first() is None

    async with system_session() as s:
        seen = {r[0] for r in (await s.execute(text('SELECT tenant FROM "case" WHERE id IN (:a, :b)'),
                                                  {"a": a, "b": b})).all()}
    assert seen == {TENANT_A, TENANT_B}
    assert (await repo.get_case(a, tenant=TENANT_A))["title"] == "A case"  # type: ignore[index]


async def test_tenant_cannot_write_rows_for_other_tenant(platform: None) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(TENANT_A) as s:
            await s.execute(
                text('INSERT INTO "case" (tenant, title, created_by) VALUES (:t, :ti, :c)'),
                {"t": TENANT_B, "ti": "smuggled", "c": "x"},
            )


async def test_no_tenant_context_sees_nothing(platform: None) -> None:
    await repo.create_case("visible only with context", created_by="u", tenant=TENANT_A)
    async with keelson_engine().connect() as conn:
        n = (await conn.execute(text('SELECT count(*) FROM "case"'))).scalar_one()
    assert n == 0


async def test_system_flag_does_not_leak_to_pooled_connections(platform: None) -> None:
    async with system_session() as s:
        assert (await s.execute(text('SELECT count(*) FROM "case"'))).scalar_one() > 0
    for _ in range(5):  # any pooled connection: the GUC was transaction-local
        async with keelson_engine().connect() as conn:
            flag = (await conn.execute(text("SELECT coalesce(current_setting('app.system', true), '')"))).scalar_one()
            assert flag == ""


async def test_admin_audit_is_append_only(platform: None) -> None:
    await repo.admin_audit({"sub": "s", "username": "u"}, "test.action", "t", None, {"x": 1}, tenant=TENANT_A)
    with pytest.raises(DBAPIError, match="append-only"):
        async with system_session() as s:
            await s.execute(text("UPDATE admin_audit SET action = 'x'"))
    with pytest.raises(DBAPIError, match="append-only"):
        async with system_session() as s:
            await s.execute(text("DELETE FROM admin_audit"))


async def test_invalid_tenant_slug_rejected(platform: None) -> None:
    with pytest.raises(ValueError):
        async with tenant_session("x'; DROP TABLE run; --"):
            pass


async def test_migration_checksum_stable() -> None:
    m = Migration("0001", "init", "SELECT 1;")
    assert m.checksum == Migration("0001", "init", "SELECT 1;").checksum


async def test_child_rows_cannot_reference_another_tenants_case(platform: None) -> None:
    """Regression (0002): FK checks bypass RLS, so composite (tenant, ref) keys must stop a
    tenant-A session from attaching rows to tenant B's case / interrupt."""
    b_case = await repo.create_case("B only", created_by={"sub": "u-b", "name": "B"}, tenant=TENANT_B)
    for sql in (
        "INSERT INTO case_message (tenant, case_id, role, text) VALUES (:t, :c, 'user', 'smuggled')",
        "INSERT INTO run_event (tenant, case_id, type, payload) VALUES (:t, :c, 'CUSTOM', '{}'::jsonb)",
        "INSERT INTO block (id, tenant, case_id, kind, envelope) VALUES ('blk_x1', :t, :c, 'narrative', '{}'::jsonb)",
    ):
        with pytest.raises(DBAPIError, match="foreign key"):
            async with tenant_session(TENANT_A) as s:
                await s.execute(text(sql), {"t": TENANT_A, "c": b_case})
    async with system_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM pg_constraint WHERE conname LIKE '%_tenant_%_fk' "
                                  "AND convalidated"))).scalar_one()
    assert n == 16


async def test_0003_converts_m1_rows(platform: None, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """M1 data → M2 registry: approval_gate tools → approved (+ split classification columns),
    M1 connector statuses → active, Merxa rows → oauth_platform_merxa, webhook ref from config,
    agent spec versions get a seq."""
    import uuid

    import asyncpg
    from sqlalchemy.ext.asyncio import create_async_engine

    from conftest import ADMIN_DSN, APP_ROLE, APP_ROLE_PASSWORD, _db_url, create_extensions

    name = f"keelson_mig_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(ADMIN_DSN)
    await admin.execute(f'CREATE DATABASE "{name}" OWNER {APP_ROLE}')
    await admin.close()
    await create_extensions(name)
    engine = create_async_engine(_db_url(ADMIN_DSN, name, APP_ROLE, APP_ROLE_PASSWORD))
    try:
        m1 = tmp_path / "m1"
        m1.mkdir()
        for m in discover():
            if m.version in ("0001", "0002"):
                (m1 / f"{m.version}_{m.name}.sql").write_text(m.sql, encoding="utf-8")
        assert await migrate(engine, m1) == ["0001", "0002"]
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.system', 'on', true)"))
            await conn.execute(text("INSERT INTO tenant (slug, name) VALUES ('t1', 'T1')"))
            await conn.execute(text(
                "INSERT INTO connector (tenant, slug, name, mcp_url, issuer, trust_level, auth, config, status) "
                "VALUES ('t1', 'merxa', 'Merxa', 'http://merxa.localhost/mcp', 'iss', 'first_party', "
                "'{\"reader_client\": \"keelson-reader\"}', '{\"webhook_secret_env\": \"MERXA_WEBHOOK_SECRET\"}', "
                "'needs_review')"))
            for tool, status, access in (("get_inventory", "approved", "read"), ("apply_change_set", "approval_gate",
                                                                                   "destructive")):
                await conn.execute(text(
                    "INSERT INTO connector_tool (tenant, connector, name, classification, definition_hash, status) "
                    "VALUES ('t1', 'merxa', :n, CAST(:c AS jsonb), 'h', :s)"),
                    {"n": tool, "s": status, "c": '{"access": "%s", "content_trust": "trusted_data", '
                                                  '"egress": "internal", "idempotency": "enforced"}' % access})
            for v in ("a", "b"):
                await conn.execute(text("INSERT INTO agent_spec (tenant, version, spec) VALUES ('t1', :v, '{}')"),
                                   {"v": v})
        assert await migrate(engine) == [m.version for m in discover() if m.version not in ("0001", "0002")]
        async with engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.system', 'on', true)"))
            c = (await conn.execute(text(
                "SELECT status, auth_kind, webhook_secret_ref FROM connector"))).one()
            tools = {r[0]: r[1:] for r in (await conn.execute(text(
                "SELECT name, status, access, content_trust, idempotency, suggested IS NOT NULL "
                "FROM connector_tool"))).all()}
            seqs = sorted(r[0] for r in (await conn.execute(text("SELECT seq FROM agent_spec"))).all())
        assert c == ("active", "oauth_platform_merxa", "MERXA_WEBHOOK_SECRET")
        assert tools["apply_change_set"] == ("approved", "destructive", "trusted_data", "enforced", True)
        assert tools["get_inventory"][:2] == ("approved", "read")
        assert seqs == [1, 2]
        with pytest.raises(DBAPIError):  # the M1-only status is gone
            async with engine.begin() as conn:
                await conn.execute(text("SELECT set_config('app.system', 'on', true)"))
                await conn.execute(text("UPDATE connector_tool SET status = 'approval_gate'"))
    finally:
        await engine.dispose()
        admin = await asyncpg.connect(ADMIN_DSN)
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
