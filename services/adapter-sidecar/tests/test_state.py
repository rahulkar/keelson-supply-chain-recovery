"""State store: migrations (idempotent, immutable once applied), the append-only audit log."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from adapter_sidecar import state
from adapter_sidecar.db import begin_write, create_engine


async def test_migrations_are_idempotent_and_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        assert await state.migrate(engine) == ["0001"]
        assert await state.migrate(engine) == []
        changed = [state.Migration(m.version, m.name, m.sql + "\n-- edited\n") for m in state.discover()]
        monkeypatch.setattr(state, "discover", lambda: changed)
        with pytest.raises(state.MigrationError, match="changed after being applied"):
            await state.migrate(engine)
    finally:
        await engine.dispose()


async def test_audit_log_is_append_only(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        await state.migrate(engine)
        async with begin_write(engine) as conn:
            await conn.execute(text("INSERT INTO audit_log (at, entity_type, entity_ref, action) "
                                    "VALUES ('x', 'change_set', 'CS-0001', 'create')"))
        for stmt in ("UPDATE audit_log SET action = 'forged'", "DELETE FROM audit_log"):
            with pytest.raises(DBAPIError, match="append-only"):
                async with begin_write(engine) as conn:
                    await conn.execute(text(stmt))
    finally:
        await engine.dispose()


def test_statement_splitter_keeps_trigger_bodies() -> None:
    stmts = state.split_statements(
        "-- c\nCREATE TABLE a (x INT);\nCREATE TRIGGER t BEFORE DELETE ON a\nBEGIN\n  SELECT RAISE(ABORT, 'no');\nEND;\n")
    assert len(stmts) == 2 and stmts[1].endswith("END;") and "RAISE" in stmts[1]


async def test_sequences(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    try:
        await state.migrate(engine)
        async with begin_write(engine) as conn:
            assert await state.next_value(conn, "x") == 1
            assert await state.next_value(conn, "x", at_least=10) == 11
            assert await state.peek_value(conn, "x") == 11
    finally:
        await engine.dispose()
