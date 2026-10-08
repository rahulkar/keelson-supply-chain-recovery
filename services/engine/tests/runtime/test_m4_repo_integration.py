"""The M4 repo port methods of the E1 adapter against a real, migrated database (RLS FORCE, a
non-superuser role without BYPASSRLS, exactly like production): ``latest_evidence``,
``get_evidence`` (now with ``case_id``), ``save_findings(task_input=…)`` and
``list_investigation_tasks`` (latest finding per task row, ``<task>-r<rev>`` ids).

Skipped without a throwaway Postgres with pgvector (``SCN_TEST_ADMIN_DSN`` / ``E1_TEST_ADMIN_DSN``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fakes.scn_fixture import ADMIN_DSN
from sqlalchemy import text

pytestmark = pytest.mark.asyncio(loop_scope="module")

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ROLE = "keelson_rb_m4_test"
TENANT, OTHER = "demo-retail", "northwind-test"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def cases() -> AsyncIterator[dict[str, str]]:
    import asyncpg

    try:
        admin = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"no throwaway Postgres ({type(exc).__name__})")
    name = f"rb_m4_{uuid.uuid4().hex[:8]}"
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", ROLE):
            await admin.execute(f"CREATE ROLE {ROLE} LOGIN PASSWORD 'x' NOSUPERUSER NOCREATEDB NOBYPASSRLS")
        await admin.execute(f'CREATE DATABASE "{name}" OWNER {ROLE}')
    finally:
        await admin.close()
    base = ADMIN_DSN.split("@", 1)[1].rsplit("/", 1)[0]
    ext = await asyncpg.connect(ADMIN_DSN.rsplit("/", 1)[0] + "/" + name, timeout=5)
    try:
        await ext.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
        ids: dict[str, str] = {}
        async with system_session() as s:
            for slug in (TENANT, OTHER):
                await s.execute(text("INSERT INTO tenant (slug, name) VALUES (:s, :s) ON CONFLICT DO NOTHING"),
                                {"s": slug})
                row = await s.execute(
                    text("INSERT INTO \"case\" (tenant, title, mode, status, created_by) "
                         "VALUES (:t, 'x', 'actual', 'open', 'u') RETURNING id"), {"t": slug})
                ids[slug] = str(row.scalar_one())
        yield ids
    finally:
        await close_db()
        set_settings(None)
        admin = await asyncpg.connect(ADMIN_DSN, timeout=5)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _repo() -> Any:
    from keelson_engine.runtime.deps import CoreAdapter

    return CoreAdapter().repo


async def test_latest_evidence_by_tool_is_tenant_scoped(cases: dict[str, str]) -> None:
    repo = _repo()
    case = cases[TENANT]
    first = await repo.save_evidence(tenant=TENANT, case_id=case, run_id="r1", connector="keelson",
                                     tool="investigation_plan", args={"source": "planner"},
                                     result={"plan": {"tasks": []}}, prov="inference")
    second = await repo.save_evidence(tenant=TENANT, case_id=case, run_id="r2", connector="keelson",
                                      tool="investigation_plan", args={"source": "edited"},
                                      result={"plan": {"tasks": [{"id": "t1"}]}}, prov="inference")
    latest = await repo.latest_evidence(tenant=TENANT, case_id=case, tool="investigation_plan")
    assert latest is not None and latest["id"] == second != first
    assert latest["args"] == {"source": "edited"} and latest["prov"] == "inference"
    assert await repo.latest_evidence(tenant=TENANT, case_id=case, tool="problem_data") is None
    # the other tenant cannot read it, even naming the case
    assert await repo.latest_evidence(tenant=OTHER, case_id=case, tool="investigation_plan") is None
    [row] = await repo.get_evidence(tenant=TENANT, ids=[second])
    assert row["case_id"] == case and row["run_id"] == "r2"


async def test_findings_with_task_input_and_task_listing(cases: dict[str, str]) -> None:
    repo = _repo()
    case = cases[TENANT]
    base = {"status": "ok", "claims": [], "affected": [], "gaps": [], "scenario_updates": []}
    for rev in (1, 2):
        await repo.save_findings(
            tenant=TENANT, case_id=case, task_id=f"{case}:t1:r{rev}", specialist="inventory_demand", run_id=f"r{rev}",
            findings={**base, "task_id": "t1", "gaps": [f"rev {rev}"]}, status="done",
            task_input={"task_id": "t1", "rev": rev, "depends_on": [], "assumption_keys": ["delay_days"]},
        )
    await repo.save_findings(
        tenant=TENANT, case_id=case, task_id=f"{case}:t3:r1", specialist="playbook", run_id="r1",
        findings={**base, "task_id": "t3", "status": "failed"}, status="failed",
        task_input={"task_id": "t3", "rev": 1, "depends_on": ["t1"], "assumption_keys": ["event_type"]},
    )
    rows = await repo.list_investigation_tasks(tenant=TENANT, case_id=case)
    assert [r["task_id"] for r in rows] == ["t1-r1", "t1-r2", "t3-r1"]
    t1r2 = rows[1]
    assert t1r2["input"]["assumption_keys"] == ["delay_days"] and t1r2["findings"]["gaps"] == ["rev 2"]
    assert rows[2]["status"] == "failed" and rows[2]["input"]["depends_on"] == ["t1"]
    assert await repo.list_investigation_tasks(tenant=OTHER, case_id=case) == []

    from keelson_engine.runtime.nodes.followup import TaskRecord, dirty_task_ids

    records = [TaskRecord.from_row(r) for r in rows]
    # t3 failed → dirty; t1's latest revision depends on delay_days
    assert dirty_task_ids(records, {"delay_days"}) == ["t1-r2", "t3-r1"]
