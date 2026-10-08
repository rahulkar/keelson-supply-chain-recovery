"""Typed staging tables (CSV and REST backends stage their tables in the SQLite state database).

Every configured table becomes a ``STRICT`` SQLite table with the configured columns and the key
as primary key, so the uniform pipeline (entity SQL → scn rows, write primitives with bound
parameters) runs on real SQL whatever the source is. Storage types: ``text``/``date``/``json``
→ TEXT (dates ISO ``YYYY-MM-DD``, JSON canonical text), ``int``/``bool`` → INTEGER, ``real`` →
REAL. Row order is the source order (``rowid``); write-back keeps it, new rows go last.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from adapter_sidecar.config import ColumnType, TableSpec
from adapter_sidecar.db import quote_ident
from adapter_sidecar.errors import SourceDataError

_SQLITE_TYPES: dict[str, str] = {"text": "TEXT", "int": "INTEGER", "real": "REAL", "date": "TEXT", "bool": "INTEGER",
                                 "json": "TEXT"}
_INT_RE = re.compile(r"^[+-]?\d{1,19}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSERT_CHUNK = 500
MAX_INT = 2**63 - 1


def schema_signature(table: TableSpec) -> str:
    """Changes whenever the staging DDL would change (then the staging table is rebuilt)."""
    material = {"columns": table.column_types, "key": table.key}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def create_sql(conn: AsyncConnection, name: str, table: TableSpec) -> str:
    cols = [f"{quote_ident(conn, c)} {_SQLITE_TYPES[t]}" + (" NOT NULL" if c in table.key else "")
            for c, t in table.column_types.items()]
    pk = ", ".join(quote_ident(conn, k) for k in table.key)
    return f"CREATE TABLE {quote_ident(conn, name)} ({', '.join(cols)}, PRIMARY KEY ({pk})) STRICT"


async def ensure_tables(conn: AsyncConnection, tables: Mapping[str, TableSpec]) -> list[str]:
    """Create (or rebuild after a column change) the staging tables; returns the rebuilt names
    (their data must be reloaded)."""
    rebuilt: list[str] = []
    for name, table in tables.items():
        sig = schema_signature(table)
        source = f"schema:{name}"
        row = (await conn.execute(text("SELECT sha256 FROM source_fingerprint WHERE source = :s"), {"s": source})).first()
        exists = (await conn.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"),
                                     {"n": name})).first() is not None
        if exists and row is not None and row[0] == sig:
            continue
        if exists:
            await conn.exec_driver_sql(f"DROP TABLE {quote_ident(conn, name)}")
        await conn.exec_driver_sql(create_sql(conn, name, table))
        # the staged data is gone: forget what was loaded (schema / csv / rest fingerprints)
        await conn.execute(text("DELETE FROM source_fingerprint WHERE source IN (:a, :b, :c)"),
                           {"a": source, "b": f"csv:{name}", "c": f"rest:{name}"})
        await conn.execute(
            text("INSERT INTO source_fingerprint (source, sha256, observed_at) VALUES (:s, :h, datetime('now'))"),
            {"s": source, "h": sig},
        )
        rebuilt.append(name)
    return rebuilt


# ───────────────────────────── typing ─────────────────────────────


def parse_cell(kind: ColumnType, raw: str) -> Any:
    """CSV text → staging value (empty string = NULL). Raises ``ValueError`` with a reason."""
    if raw == "":
        return None
    if kind == "text":
        return raw
    if kind == "int":
        if not _INT_RE.match(raw) or abs(int(raw)) > MAX_INT:
            raise ValueError(f"{raw[:40]!r} is not an integer")
        return int(raw)
    if kind == "real":
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{raw[:40]!r} is not a number") from None
        if not math.isfinite(value):
            raise ValueError("number must be finite")
        return value
    if kind == "date":
        return check_date(raw)
    if kind == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return 1
        if low in ("false", "0", "no"):
            return 0
        raise ValueError(f"{raw[:40]!r} is not a boolean")
    try:
        return canonical_json(json.loads(raw))
    except ValueError:
        raise ValueError("invalid JSON") from None


def check_date(value: str) -> str:
    if not _DATE_RE.match(value):
        raise ValueError(f"{value[:40]!r} is not a YYYY-MM-DD date")
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a calendar date") from None
    return value


def coerce_json(kind: ColumnType, value: Any) -> Any:
    """A JSON value (REST item field) → staging value."""
    if value is None:
        return None
    if kind == "text":
        if isinstance(value, bool) or not isinstance(value, str | int):
            raise ValueError(f"expected text, got {type(value).__name__}")
        return str(value)
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str) and _INT_RE.match(value):
                return int(value)
            raise ValueError(f"expected an integer, got {value!r:.40}")
        return value
    if kind == "real":
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError(f"expected a number, got {value!r:.40}")
        return float(value)
    if kind == "date":
        if not isinstance(value, str):
            raise ValueError("expected a YYYY-MM-DD string")
        return check_date(value[:10] if "T" in value else value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected true/false")
        return 1 if value else 0
    return canonical_json(value)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def to_json_value(kind: ColumnType, value: Any) -> Any:
    """Stored value (SQLite or Postgres) → JSON-able image value (dates ISO, bools bool, JSON parsed)."""
    if value is None:
        return None
    if kind == "date":
        return value.isoformat() if isinstance(value, dt.date) else str(value)
    if kind == "bool":
        return bool(value)
    if kind == "json":
        return json.loads(value) if isinstance(value, str | bytes) else value
    if kind == "real":
        return float(value)
    if kind == "int":
        return int(value)
    return value


def format_cell(kind: ColumnType, value: Any) -> str:
    """JSON image value → CSV text (deterministic: ints decimal, ISO dates, ``true``/``false``,
    shortest round-trip reals, canonical JSON; NULL = empty)."""
    if value is None:
        return ""
    if kind == "bool":
        return "true" if value else "false"
    if kind == "real":
        return repr(float(value))
    if kind == "json":
        return canonical_json(value)
    return str(value)


# ───────────────────────────── load / dump ─────────────────────────────


async def replace_rows(conn: AsyncConnection, name: str, table: TableSpec, rows: Sequence[Sequence[Any]]) -> None:
    """Replace the staging table's content (rows in column order, staging-typed values)."""
    cols = list(table.column_types)
    await conn.exec_driver_sql(f"DELETE FROM {quote_ident(conn, name)}")
    collist = ", ".join(quote_ident(conn, c) for c in cols)
    params = ", ".join(f":c{i}" for i in range(len(cols)))
    stmt = text(f"INSERT INTO {quote_ident(conn, name)} ({collist}) VALUES ({params})")
    for i in range(0, len(rows), INSERT_CHUNK):
        chunk = [{f"c{j}": v for j, v in enumerate(r)} for r in rows[i:i + INSERT_CHUNK]]
        if chunk:
            await conn.execute(stmt, chunk)


async def dump_rows(conn: AsyncConnection, name: str, table: TableSpec) -> list[dict[str, Any]]:
    """Rows as JSON image dicts in source order (``rowid``)."""
    types = table.column_types
    cols = list(types)
    collist = ", ".join(quote_ident(conn, c) for c in cols)
    res = await conn.execute(text(f"SELECT {collist} FROM {quote_ident(conn, name)} ORDER BY rowid"))
    return [{c: to_json_value(types[c], v) for c, v in zip(cols, r, strict=True)} for r in res.all()]


def typed_rows(source: str, table: TableSpec, records: Iterable[tuple[int, Mapping[str, str]]]) -> list[list[Any]]:
    """CSV records (``(line number, {column: text})``) → staging rows; duplicate keys and bad cells
    raise :class:`SourceDataError` naming the row and column."""
    types = table.column_types
    out: list[list[Any]] = []
    seen: dict[tuple[Any, ...], int] = {}
    key_idx = [list(types).index(k) for k in table.key]
    for line, rec in records:
        row: list[Any] = []
        for col, kind in types.items():
            raw = rec.get(col)
            if raw is None:
                raise SourceDataError(source, "missing value", row=line, column=col)
            try:
                row.append(parse_cell(kind, raw))
            except ValueError as exc:
                raise SourceDataError(source, str(exc), row=line, column=col) from None
        key = tuple(row[i] for i in key_idx)
        if any(v is None for v in key):
            raise SourceDataError(source, f"empty key {list(table.key)}", row=line)
        if key in seen:
            raise SourceDataError(source, f"duplicate key {list(key)} (first on row {seen[key]})", row=line)
        seen[key] = line
        out.append(row)
    return out


def rows_fingerprint(rows: Sequence[Sequence[Any]]) -> str:
    return hashlib.sha256(canonical_json([list(r) for r in rows]).encode("utf-8")).hexdigest()
