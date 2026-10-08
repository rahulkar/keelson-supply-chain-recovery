"""Tier C change feed: the ``journal`` table and ``scn.changes_since`` (contract 122 §4.3).

* Every applied / rolled-back row change appends one ``change`` row per ``scn/1.0`` entity its
  table ``affects`` (``{entity, op upsert|delete, key = source_id, version, change_set_code}``),
  in the same state transaction as the change set's own rows.
* Source drift the sidecar did not cause (a CSV file edited, a SQL table whose fingerprint
  moved, a REST endpoint answering differently) appends ``external`` rows; a demo reset appends a
  ``reset`` row. Both make ``full_resync_required`` true for any watermark before them.
* Watermark ``j:<seq>`` (``seq`` never repeats: ``AUTOINCREMENT``). A watermark older than the
  retained rows, or newer than anything this state store has seen (state wiped), also asks for a
  full resync — exactly what ``keelson_contracts.scn.ChangesSince.requires_sync`` reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from adapter_sidecar.backends import Stmt
from adapter_sidecar.config import SidecarConfig
from adapter_sidecar.errors import Problem
from adapter_sidecar.templates import render

WATERMARK_RE = re.compile(r"^j:(\d{1,18})$")
MAX_CHANGES = 2000

_INSERT = ("INSERT INTO journal (at, kind, entity, op, key, version, change_set_code, detail) "
           "VALUES (:at, :kind, :entity, :op, :key, :version, :cs, :detail)")


def format_watermark(seq: int) -> str:
    return f"j:{seq}"


def parse_watermark(value: str | None) -> int:
    m = WATERMARK_RE.match((value or "").strip())
    if not m:
        raise Problem(400, "invalid_watermark", "watermark must look like 'j:<number>' (from scn.snapshot_open)")
    return int(m.group(1))


async def high_water(conn: AsyncConnection) -> int:
    """The last journal ``seq`` ever assigned (also when old rows were pruned)."""
    row = (await conn.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'journal'"))).first()
    return int(row[0]) if row is not None else 0


def change_statements(cfg: SidecarConfig, effects: Iterable[Mapping[str, Any]], *, change_set_code: str,
                      at: str) -> list[Stmt]:
    """Journal rows for applied effects (``{kind, table, before, after}``)."""
    out: list[Stmt] = []
    for eff in effects:
        table = cfg.tables.get(str(eff["table"]))
        if table is None:
            continue
        deleted = eff["kind"] == "delete"
        row = eff["before"] if deleted else eff["after"]
        if not isinstance(row, Mapping):
            continue
        version = row.get(table.version_col) if table.version_col else None
        for entity, template in table.affects.items():
            key = render(template, row)
            if key is None or key == "":
                continue
            out.append((_INSERT, {"at": at, "kind": "change", "entity": entity, "op": "delete" if deleted else "upsert",
                                  "key": str(key), "version": int(version) if isinstance(version, int) else None,
                                  "cs": change_set_code, "detail": None}))
    return out


def drift_statements(cfg: SidecarConfig, tables: Sequence[str], *, at: str, kind: str = "external",
                     detail: str | None = None) -> list[Stmt]:
    """One ``external`` (or ``reset``) row per affected entity of each table (one row when none)."""
    out: list[Stmt] = []
    for name in tables:
        table = cfg.tables.get(name)
        entities = list(table.affects) if table is not None and table.affects else [None]
        for entity in entities:
            out.append((_INSERT, {"at": at, "kind": kind, "entity": entity, "op": None, "key": None, "version": None,
                                  "cs": None, "detail": detail or f"{name} changed outside the sidecar"}))
    if not tables and kind == "reset":
        out.append((_INSERT, {"at": at, "kind": "reset", "entity": None, "op": None, "key": None, "version": None,
                              "cs": None, "detail": detail or "demo reset"}))
    return out


async def changes_since(conn: AsyncConnection, watermark: str) -> dict[str, Any]:
    after = parse_watermark(watermark)
    hi = await high_water(conn)
    if after > hi:
        return {"changes": [], "next_watermark": format_watermark(hi), "has_more": False,
                "full_resync_required": True}
    lowest = (await conn.execute(text("SELECT min(seq) FROM journal"))).scalar_one_or_none()
    resync = lowest is not None and after < int(lowest) - 1
    rows = (await conn.execute(
        text("SELECT seq, at, kind, entity, op, key, version FROM journal WHERE seq > :n ORDER BY seq LIMIT :lim"),
        {"n": after, "lim": MAX_CHANGES + 1},
    )).all()
    more = len(rows) > MAX_CHANGES
    rows = rows[:MAX_CHANGES]
    changes: list[dict[str, Any]] = []
    for r in rows:
        if r.kind == "change":
            changes.append({"entity": r.entity, "op": r.op, "key": r.key, "version": r.version, "at": r.at})
        else:
            resync = True
    out: dict[str, Any] = {
        "changes": changes,
        "next_watermark": format_watermark(int(rows[-1].seq) if rows else max(after, 0)),
        "has_more": more,
    }
    if resync:
        out["full_resync_required"] = True
    return out


async def prune(conn: AsyncConnection, retention: int) -> int:
    hi = await high_water(conn)
    res = await conn.execute(text("DELETE FROM journal WHERE seq <= :cut"), {"cut": hi - retention})
    return int(res.rowcount or 0)
