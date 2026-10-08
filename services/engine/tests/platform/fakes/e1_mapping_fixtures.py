"""Shared fixtures of the M8 mapping compiler tests (no database, no network): an in-process
``/mcp-lite`` caller over :data:`e1_lite_server.LITE_DATA`, collectors for both crawl engines and a
white-box harness that evaluates many expressions through the compiler's SQL at once."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

from e1_lite_server import COLUMNS, key_of
from keelson_contracts.mapping import (
    ReadMapping,
    ReadSource,
    RecordLookup,
    StaticLookup,
    parse_expression,
)
from keelson_engine.core import mapping_eval as me
from keelson_engine.core.duckdb_session import open_duckdb
from keelson_engine.core.mapping_compiler import (
    CompiledLookup,
    CompiledSource,
    _Gen,
    decode,
    encode,
)
from keelson_engine.core.scn_crawl import CrawlError, CrawlStats, compiled_rows, crawl_rows

ToolLog = list[tuple[str, dict[str, Any]]]


def lite_caller(data: dict[str, list[dict[str, Any]]], log: ToolLog,
                fail: dict[tuple[str, int], str] | None = None) -> Any:
    """``list_items`` / ``list_cursor`` exactly as /mcp-lite answers them (structured content)."""

    async def call(tool: str, args: dict[str, Any]) -> Any:
        log.append((tool, dict(args)))
        type_ = args["type"]
        rows = sorted(data[type_], key=lambda r: key_of(type_, r))
        if fail and (type_, int(args.get("page") or 0)) in fail:
            raise CrawlError("tool_error", fail[(type_, int(args.get("page") or 0))])
        if tool == "list_items":
            page, size = int(args.get("page", 1)), int(args.get("page_size", 50))
            chunk = rows[(page - 1) * size: page * size]
            return {"type": type_, "page": page, "page_size": size, "total": len(rows),
                    "items": [{"key": key_of(type_, r), **{c: r.get(c) for c in COLUMNS[type_]}} for r in chunk]}
        start = int(args.get("cursor") or 0)
        chunk = rows[start: start + 2]
        return {"items": chunk, "next_cursor": str(start + 2) if start + 2 < len(rows) else None}

    return call


async def collect(engine: str, mapping: ReadMapping, data: dict[str, list[dict[str, Any]]], *,
                  max_pages: int | None = None) -> tuple[dict[str, list[tuple[Any, ...]]], CrawlStats, ToolLog]:
    """Rows per entity of one engine (``interpreter`` | ``duckdb``)."""
    log: ToolLog = []
    stats = CrawlStats()
    fn = crawl_rows if engine == "interpreter" else compiled_rows
    out: dict[str, list[tuple[Any, ...]]] = {}
    async for entity, rows in fn(lite_caller(data, log), mapping, stats=stats, max_pages=max_pages):
        out.setdefault(entity, []).extend(rows)
    return out, stats, log


async def outcome(engine: str, mapping: ReadMapping, data: dict[str, list[dict[str, Any]]]) -> Any:
    """The rows, or ``(code, detail)`` of the crawl error."""
    try:
        rows, _stats, _log = await collect(engine, mapping, data)
    except CrawlError as exc:
        return exc.code, exc.detail
    return rows


# ───────────────────────────── expression harness ─────────────────────────────

STATIC: dict[str, Any] = {"PO-1": "hit", "12": 12, "true": True, "x": None, "2": "two"}
RECORDS: list[dict[str, Any]] = [
    {"code": "PO-1", "family": "outerwear", "price": "12.99", "nested": {"a": [1, {"b": "deep"}]}, "flag": True},
    {"code": "2", "family": None, "price": 7, "nested": [], "flag": False},
    {"code": "PO-1", "family": "second wins? never", "price": "1"},
]
_SRC = ReadSource(tool="items", page_param=None, page_size=1, items_path="$", cursor_path=None, max_pages=1)
_RSRC = ReadSource(tool="records", page_param=None, page_size=1, items_path="$", cursor_path=None, max_pages=1)


def interpreter_lookups() -> me.Lookups:
    records: dict[str, Any] = {}
    for r in RECORDS:
        records.setdefault(str(r["code"]), r)
    return me.Lookups({"t": dict(STATIC)}, {"r": records})


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or repr(a) == repr(b)
    if isinstance(a, (dict, list)) and type(a) is type(b):  # NaN inside: compare the JSON texts
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    return type(a) is type(b) and a == b


def interpret(expr: str, item: Any) -> tuple[str, Any]:
    try:
        return "ok", me.evaluate(parse_expression(expr), item, interpreter_lookups())
    except me.MappingEvalError:
        return "error", None


def compile_many(exprs: Sequence[str], items: Sequence[Any]) -> list[list[tuple[str, Any]]]:
    """Evaluate every expression over every item through the compiler's generated SQL (one query):
    ``result[item][expr]`` = ``("ok", value)`` or ``("error", None)``."""
    import pyarrow as pa

    cs = CompiledSource("0" * 16, _SRC)
    rs = CompiledSource("1" * 16, _RSRC)
    lookups = {"t": CompiledLookup("t", "static", StaticLookup(kind="static", values=STATIC), None),
               "r": CompiledLookup("r", "record", RecordLookup(kind="record", source=_RSRC, key="$.code"), rs)}
    gen = _Gen(cs, lookups)
    outputs = ["ord"]
    for k, text in enumerate(exprs):
        v = gen.val(parse_expression(text))
        outputs += [f"{v.t} AS o{k}_t", f"{v.v} AS o{k}_v", f"{v.e} AS o{k}_e"]
    sql = gen.query(outputs) + " ORDER BY ord"
    rec = lookups["r"]
    with open_duckdb(256, 1) as s:
        s.execute(cs.ddl())
        batch = cs.flatten(list(items), 0)
        s.insert_batch(cs.table, batch)
        s.execute("CREATE TABLE lk_t (k VARCHAR, lt INTEGER, lv VARCHAR)")
        rows = [(k, *encode(v)) for k, v in STATIC.items()]
        s.insert_batch("lk_t", pa.record_batch({"k": [r[0] for r in rows], "lt": pa.array([r[1] for r in rows], pa.int32()),
                                                "lv": [r[2] for r in rows]}))
        cols = ", ".join(["k VARCHAR", "raw VARCHAR", *(f"r{q}_t UTINYINT, r{q}_v VARCHAR"
                                                         for q in range(len(rec.record_paths)))])
        s.execute(f"CREATE TABLE rec_r ({cols})")
        seen: set[str] = set()
        for r in RECORDS:
            key = str(r["code"])
            if key in seen:
                continue
            seen.add(key)
            vals: list[Any] = [key, "{}"]
            for path in rec.record_paths:
                vals += list(encode(me.get_path(r, path)))
            s.execute(f"INSERT INTO rec_r VALUES ({', '.join('?' for _ in vals)})", vals)
        result = s.execute(sql).fetchall()
    out: list[list[tuple[str, Any]]] = []
    for row in result:
        per: list[tuple[str, Any]] = []
        for k in range(len(exprs)):
            t, v, e = row[1 + 3 * k], row[2 + 3 * k], row[3 + 3 * k]
            per.append(("error", None) if e is not None else ("ok", decode(int(t), v)))
        out.append(per)
    return out


def disagreements(exprs: Sequence[str], items: Sequence[Any]) -> list[tuple[str, Any, Any, Any]]:
    """``(expr, item, interpreter, compiler)`` wherever the two engines differ."""
    compiled = compile_many(exprs, items)
    bad: list[tuple[str, Any, Any, Any]] = []
    for i, item in enumerate(items):
        for k, expr in enumerate(exprs):
            want = interpret(expr, item)
            got = compiled[i][k]
            if want[0] != got[0] or (want[0] == "ok" and not _same(want[1], got[1])):
                bad.append((expr, item, want, got))
    return bad
