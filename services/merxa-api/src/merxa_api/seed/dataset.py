"""Compact, column-oriented container for generated seed data.

The ``full`` profile generates ~0.5 M rows; holding them as one dict per row would cost
several hundred MB, so tables are ``(columns, [tuple, ...])``. The golden dataset is kept
as **CSV bytes per table** (what ``COPY … FORMAT csv`` ingests), which is also the input of
the dataset fingerprint — compact (≈ 20 MB for ``full``) and fast to reload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class DateRange:
    """A half-open ``[lower, upper)`` date range (Postgres ``daterange`` canonical form)."""

    lower: date
    upper: date

    def literal(self) -> str:
        return f"[{self.lower.isoformat()},{self.upper.isoformat()})"


@dataclass
class TableData:
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]] = field(default_factory=list)


@dataclass
class Dataset:
    profile: str
    tables: dict[str, TableData] = field(default_factory=dict)

    # ── building ──
    def add(self, table: str, row: Mapping[str, Any]) -> None:
        """Append one row given as a mapping (column order fixed by the first row)."""
        td = self.tables.get(table)
        if td is None:
            td = self.tables[table] = TableData(tuple(row))
        if len(row) != len(td.columns):
            raise ValueError(f"{table}: row has columns {sorted(row)}, expected {list(td.columns)}")
        try:
            self.tables[table].rows.append(tuple(row[c] for c in td.columns))
        except KeyError as exc:
            raise ValueError(f"{table}: row has columns {sorted(row)}, expected {list(td.columns)}") from exc

    def add_many(self, table: str, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            self.add(table, row)

    def put(self, table: str, columns: Sequence[str], rows: list[tuple[Any, ...]]) -> None:
        """Set a whole table from tuples (fast path for the big tables)."""
        self.tables[table] = TableData(tuple(columns), rows)

    def ensure(self, table: str, columns: Sequence[str]) -> None:
        self.tables.setdefault(table, TableData(tuple(columns)))

    # ── reading ──
    def count(self, table: str) -> int:
        td = self.tables.get(table)
        return len(td.rows) if td else 0

    def dicts(self, table: str) -> list[dict[str, Any]]:
        td = self.tables.get(table)
        if td is None:
            return []
        cols = td.columns
        return [dict(zip(cols, r, strict=True)) for r in td.rows]

    def iter_dicts(self, table: str) -> Iterator[dict[str, Any]]:
        td = self.tables.get(table)
        if td is None:
            return
        cols = td.columns
        for r in td.rows:
            yield dict(zip(cols, r, strict=True))


# ───────────────────────── CSV encoding (COPY … FORMAT csv) ─────────────────────────
def _quote(text: str) -> str:
    if "\x00" in text:
        raise ValueError("NUL characters cannot be stored")
    return '"' + text.replace('"', '""') + '"'


def _array_literal(values: Sequence[Any]) -> str:
    parts = []
    for v in values:
        if v is None:
            parts.append("NULL")
            continue
        s = str(v)
        parts.append('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "{" + ",".join(parts) + "}"


def encode_value(value: Any) -> str:
    """One CSV field. NULL is an unquoted empty field; every string is quoted, so an empty
    string stays distinguishable from NULL."""
    if value is None:
        return ""
    if value is True:
        return "t"
    if value is False:
        return "f"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, DateRange):
        return _quote(value.literal())
    if isinstance(value, (list, tuple)):
        return _quote(_array_literal(value))
    if isinstance(value, Mapping):
        return _quote(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    raise TypeError(f"cannot encode {type(value).__name__} for COPY")


def encode_csv(td: TableData) -> bytes:
    out = []
    for row in td.rows:
        out.append(",".join(encode_value(v) for v in row))
    if not out:
        return b""
    return ("\n".join(out) + "\n").encode("utf-8")


@dataclass(frozen=True)
class EncodedTable:
    name: str
    columns: tuple[str, ...]
    data: bytes
    rows: int


def encode_dataset(ds: Dataset, order: Sequence[str]) -> list[EncodedTable]:
    return [
        EncodedTable(name, ds.tables[name].columns, encode_csv(ds.tables[name]), len(ds.tables[name].rows))
        for name in order
        if name in ds.tables
    ]


def fingerprint(tables: Sequence[EncodedTable]) -> str:
    """sha256 over ``name \\n columns \\n csv`` of every table in load order."""
    h = hashlib.sha256()
    for t in tables:
        h.update(t.name.encode("utf-8") + b"\n" + ",".join(t.columns).encode("utf-8") + b"\n")
        h.update(t.data)
    return h.hexdigest()
