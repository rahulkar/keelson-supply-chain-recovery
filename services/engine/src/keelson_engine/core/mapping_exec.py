"""Running a compiled read mapping (``docs/contracts/121-m8-mapping-compiler.md`` §2): fetch →
stage → project into DuckDB → lookups → entities → checks → load.

:class:`CompiledRun` drives one :class:`~keelson_engine.core.mapping_compiler.CompiledMapping` in
one hardened DuckDB session (:mod:`keelson_engine.core.duckdb_session`). Its methods are
synchronous (DuckDB and pydantic are CPU-bound); the async entry points run them in a worker thread
(:func:`_blocking`: a cancelled caller interrupts the query and waits for the thread, so the session
is never closed under a running statement).

Error parity with the interpreter: SQL only *locates* the first failing item (``min(ord)`` over the
error columns, per entity mapping in mapping order, per lookup group in lookup order); rows before
it are decoded and validated with the ``scn/1.0`` model in crawl order (the authority, unchanged);
the failing item itself is **re-evaluated by the interpreter** (:func:`~keelson_engine.core.scn_crawl.map_item`,
:class:`~keelson_engine.core.scn_crawl.GroupPass`, :func:`~keelson_engine.core.scn_crawl.lookup_entry`)
against DuckDB-backed lookup tables (:class:`_DuckLookups`), which raises the exact M7 message. Should
the interpreter not fail on that item, the engines disagree: the crawl fails ``mapping_failed`` with
a "report this" message and an error log (never silently loads).

Validated rows go to typed per-entity tables (``out_<entity>``, one Arrow batch per 5,000 rows),
where duplicate ``source_id`` s and :data:`~keelson_contracts.scn.SCN_FOREIGN_KEYS` are checked
**before** anything is written to Postgres; the load then streams them out in mapping order.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import types
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import suppress
from typing import Annotated, Any, Final, Literal, TypeVar, Union, get_args, get_origin

from pydantic import ValidationError

from keelson_contracts import scn
from keelson_contracts.mapping import ReadMapping, ReadPreview, SourceLookup, StaticLookup, parse_expression
from keelson_contracts.common import ratio_bp
from keelson_engine.core.duckdb_session import DuckSession, open_duckdb
from keelson_engine.core.logs import get_logger
from keelson_engine.core.mapping_compiler import (
    CompiledEntity,
    CompiledLookup,
    CompiledMapping,
    CompileError,
    compile_read_mapping,
    decode,
    encode,
    sql_str,
)
from keelson_engine.core.mapping_eval import Lookups, MappingEvalError, evaluate, key_text
from keelson_engine.core.scn_crawl import (
    FLUSH_ROWS,
    MAX_LOOKUP_ROWS,
    CrawlError,
    CrawlStats,
    GroupPass,
    ToolCaller,
    _flush,
    _pages,
    compile_entity,
    lookup_entry,
    map_item,
    row_limit,
    session_caller,
    validate_row,
)

__all__ = ["CompiledRun", "compiled_rows", "crawl", "preview"]

log = get_logger(__name__)

T = TypeVar("T")

_DISAGREE: Final = "the DuckDB compiler and the interpreter disagree on this item (please report it)"


# ───────────────────────────── typed output tables ─────────────────────────────


def _scalar(ann: Any) -> str:
    origin = get_origin(ann)
    if origin is Annotated:
        return _scalar(get_args(ann)[0])
    if origin is Literal:
        return "str"
    if origin is list:
        return "list"
    if origin in (Union, types.UnionType):
        kinds = {_scalar(a) for a in get_args(ann) if a is not type(None)}
        if kinds <= {"int", "float"} and "float" in kinds:
            return "float"
        if len(kinds) == 1:
            return kinds.pop()
        raise CompileError(f"unsupported column type {ann!r}")
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if ann is dt.date:
        return "date"
    raise CompileError(f"unsupported column type {ann!r}")


_DUCK_TYPES: Final = {"str": "VARCHAR", "int": "BIGINT", "float": "DOUBLE", "date": "DATE", "list": "VARCHAR[]",
                      "bool": "BOOLEAN"}


def column_kinds(entity: str) -> list[tuple[str, str]]:
    """``(column, kind)`` of a ``scn/1.0`` entity (``str|int|float|date|list|bool``)."""
    model = scn.SCN_ROW_MODELS[entity]
    return [(c, _scalar(model.model_fields[c].annotation)) for c in scn.SCN_COLUMNS[entity]]


def _arrow_type(kind: str) -> Any:
    import pyarrow as pa

    return {"str": pa.string(), "int": pa.int64(), "float": pa.float64(), "date": pa.date32(),
            "list": pa.list_(pa.string()), "bool": pa.bool_()}[kind]


# ───────────────────────────── lookups backed by DuckDB ─────────────────────────────


class _DuckLookups(Lookups):
    """The interpreter's :class:`~keelson_engine.core.mapping_eval.Lookups` answered from the run's
    lookup / record tables (used only to re-evaluate a failing item)."""

    def __init__(self, run: CompiledRun) -> None:
        super().__init__()
        self._run = run

    def get(self, table: str, key: Any, default: Any = None) -> Any:
        lk = self._run.lookup_or_none(table)
        if lk is None or lk.kind == "record":
            raise MappingEvalError(f"lookup(): unknown table {table!r}")
        text = key_text(key, "lookup() key")
        if text is None:
            return default
        row = self._run.s.execute(f"SELECT lt, lv FROM {lk.db_table} WHERE k = ?", [text]).fetchone()
        return default if row is None else decode(int(row[0]), row[1])

    def record(self, table: str, key: Any) -> Any:
        lk = self._run.lookup_or_none(table)
        if lk is None or lk.kind != "record":
            raise MappingEvalError(f"field(): unknown record table {table!r}")
        text = key_text(key, "field() key")
        if text is None:
            return None
        row = self._run.s.execute(f"SELECT raw FROM {lk.db_table} WHERE k = ?", [text]).fetchone()
        return None if row is None else json.loads(row[0])


# ───────────────────────────── the run ─────────────────────────────


class CompiledRun:
    """One compiled mapping over one DuckDB session (see the module docstring)."""

    def __init__(self, compiled: CompiledMapping, session: DuckSession, stats: CrawlStats, *,
                 row_limit: int | None = None, max_bytes: int | None = None) -> None:
        self.c = compiled
        self.s = session
        self.stats = stats
        self.row_limit = row_limit
        self.max_bytes = max_bytes
        self.items: dict[str, int] = {k: 0 for k in compiled.sources}
        self.bytes = 0
        self.rows = 0
        self.kinds = {e: column_kinds(e) for e in scn.SCN_ENTITIES}
        self.lookups_proxy = _DuckLookups(self)
        for cs in compiled.sources.values():
            session.execute(cs.ddl())
        for entity, kinds in self.kinds.items():
            cols = ", ".join(f"{c} {_DUCK_TYPES[k]}" for c, k in kinds)
            session.execute(f"CREATE TABLE out_{entity} (m INTEGER, ord BIGINT, {cols})")

    def lookup_or_none(self, name: str) -> CompiledLookup | None:
        return next((lk for lk in self.c.lookups if lk.name == name), None)

    # ── ingest ──
    def ingest(self, key: str, items: Sequence[Any]) -> str:
        """Project one page of ``key`` into DuckDB; returns the page as JSON text (for staging)."""
        cs = self.c.sources[key]
        try:
            batch = cs.flatten(items, self.items[key])
        except (CompileError, TypeError, ValueError) as exc:
            raise CrawlError("items_invalid", f"{cs.used_by[0]}: an item is not JSON ({type(exc).__name__})") from None
        raws = batch.column("raw").to_pylist()
        page = "[" + ",".join(raws) + "]"
        self.bytes += len(page.encode("utf-8"))
        if self.max_bytes is not None and self.bytes > self.max_bytes:
            raise CrawlError("too_many_rows", f"the crawl fetched more than {self.max_bytes} bytes (SCN_RAW_MAX_BYTES)")
        self.s.insert_batch(cs.table, batch)
        self.items[key] += len(items)
        return page

    def raw_item(self, key: str, ord_: int) -> Any:
        row = self.s.execute(f"SELECT raw FROM src_{key} WHERE ord = ?", [ord_]).fetchone()
        return None if row is None else json.loads(row[0])

    # ── lookups ──
    def build_lookups(self) -> None:
        """Static, source and record tables (first item wins per key text); the first failing item
        of each source group (mapping order) raises the interpreter's error."""
        import pyarrow as pa

        groups: dict[str, list[CompiledLookup]] = {}
        for lk in self.c.lookups:
            if isinstance(lk.table, StaticLookup):
                self.s.execute(f"CREATE TABLE {lk.db_table} (k VARCHAR, lt INTEGER, lv VARCHAR)")
                rows = [(k, *encode(v)) for k, v in lk.table.values.items()]
                if rows:
                    batch = pa.record_batch({"k": pa.array([r[0] for r in rows], pa.string()),
                                             "lt": pa.array([r[1] for r in rows], pa.int32()),
                                             "lv": pa.array([r[2] for r in rows], pa.string())})
                    self.s.insert_batch(lk.db_table, batch)
            else:
                assert lk.source is not None
                groups.setdefault(lk.source.key, []).append(lk)
        for key, members in groups.items():
            failures: list[tuple[int, int, CompiledLookup]] = []
            for pos, lk in enumerate(members):
                assert lk.sql is not None
                self.s.execute(f"CREATE TABLE lkb_{lk.name} AS {lk.sql}")
                cond = "key_e IS NOT NULL OR (key_t <> 0 AND key_t >= 4)"
                if lk.kind == "source":
                    cond += " OR val_e IS NOT NULL OR (key_t <> 0 AND val_t >= 5)"
                first = self.s.execute(f"SELECT min(ord) FROM lkb_{lk.name} WHERE {cond}").fetchone()[0]
                if first is not None:
                    failures.append((int(first), pos, lk))
            if failures:
                ord_, _pos, lk = min(failures, key=lambda f: (f[0], f[1]))
                table = lk.table
                assert not isinstance(table, StaticLookup)
                value_ast = parse_expression(table.value) if isinstance(table, SourceLookup) else None
                lookup_entry(lk.where, parse_expression(table.key), value_ast, self.raw_item(key, ord_))
                log.error("mapping compiler disagreement", where=lk.where, item=ord_)
                raise CrawlError("mapping_failed", f"{lk.where}: item {ord_}: {_DISAGREE}")
            for lk in members:
                first_wins = "QUALIFY row_number() OVER (PARTITION BY key_v ORDER BY ord) = 1"
                if lk.kind == "source":
                    cols = "key_v AS k, val_t AS lt, val_v AS lv"
                else:
                    cols = ", ".join(["key_v AS k", "raw", *(f"r{q}_t, r{q}_v" for q in range(len(lk.record_paths)))])
                self.s.execute(f"CREATE TABLE {lk.db_table} AS SELECT {cols} FROM lkb_{lk.name} WHERE key_t <> 0 "
                               f"{first_wins}")
                self.s.execute(f"DROP TABLE lkb_{lk.name}")
                n = int(self.s.execute(f"SELECT count(*) FROM {lk.db_table}").fetchone()[0])
                if n > MAX_LOOKUP_ROWS:
                    raise CrawlError("too_many_rows", f"{lk.where}: more than {MAX_LOOKUP_ROWS} entries")
                self.stats.lookups[lk.name] = n

    # ── entities ──
    def _out(self, ce: CompiledEntity, rows: list[tuple[int, tuple[Any, ...]]]) -> None:
        import pyarrow as pa

        if not rows:
            return
        self.rows += len(rows)
        if self.row_limit is not None and self.rows > self.row_limit:
            raise CrawlError("too_many_rows", f"the crawl exceeds {self.row_limit} rows (SCN_CRAWL_MAX_ROWS)")
        kinds = self.kinds[ce.em.entity]
        arrays: dict[str, Any] = {"m": pa.array([ce.index] * len(rows), pa.int32()),
                                  "ord": pa.array([o for o, _r in rows], pa.int64())}
        for j, (column, kind) in enumerate(kinds):
            values = [r[j] for _o, r in rows]
            if kind == "float":
                values = [None if v is None else float(v) for v in values]
            arrays[column] = pa.array(values, _arrow_type(kind))
        self.s.insert_batch(f"out_{ce.em.entity}", pa.record_batch(arrays))

    def _stream(self, sql: str, params: list[Any] | None = None) -> Iterator[list[tuple[Any, ...]]]:
        cur = self.s.con.cursor()
        try:
            cur.execute(sql, params) if params is not None else cur.execute(sql)
            while chunk := cur.fetchmany(FLUSH_ROWS):
                yield chunk
        finally:
            cur.close()

    @staticmethod
    def _decoded(ce: CompiledEntity, r: Sequence[Any], offset: int) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for j, (column, _ast) in enumerate(ce.columns):
            value = decode(int(r[offset + 2 * j]), r[offset + 2 * j + 1])
            if value is not None:
                row[column] = value
        return row

    def _tv(self, ce: CompiledEntity) -> str:
        return ", ".join(f"c{j}_t, c{j}_v" for j in range(len(ce.columns)))

    def _reraise_item(self, ce: CompiledEntity, ord_: int) -> None:
        c = compile_entity(ce.index, ce.em)
        map_item(c, self.raw_item(ce.source.key, ord_), self.lookups_proxy, ord_)
        log.error("mapping compiler disagreement", where=ce.where, item=ord_)
        raise CrawlError("mapping_failed", f"{ce.where} item {ord_}: {_DISAGREE}")

    def _reraise_group(self, ce: CompiledEntity, ord_: int) -> None:
        n = len(ce.group_by)
        keys = self.s.execute(f"SELECT {', '.join(f'k{j}_t, k{j}_v' for j in range(n))} FROM ev_{ce.index} "
                              "WHERE ord = ?", [ord_]).fetchone()
        texts = [keys[2 * j + 1] if int(keys[2 * j]) in (1, 2, 3) else None for j in range(n)]
        if any(t is None for t in texts):
            ords = [ord_]
        else:
            cond = " AND ".join(f"k{j}_t IN (1, 2, 3) AND k{j}_v = ?" for j in range(n))
            ords = [int(r[0]) for r in self.s.execute(
                f"SELECT ord FROM ev_{ce.index} WHERE ord <= ? AND {cond} ORDER BY ord", [ord_, *texts]).fetchall()]
            if ord_ not in ords:
                ords.append(ord_)
        gp = GroupPass(compile_entity(ce.index, ce.em), self.lookups_proxy)
        for o in ords:
            gp.add(o, self.raw_item(ce.source.key, o))
        log.error("mapping compiler disagreement", where=ce.where, item=ord_)
        raise CrawlError("mapping_failed", f"{ce.where} item {ord_}: {_DISAGREE}")

    def _first_error(self, ce: CompiledEntity) -> int | None:
        if ce.grouped:
            assert ce.group_errors_sql is not None
            self.s.execute(f"CREATE TABLE ge_{ce.index} AS {ce.group_errors_sql.replace('{items}', f'ev_{ce.index}')}")
            first = self.s.execute(f"SELECT min(ord) FROM ge_{ce.index} WHERE err").fetchone()[0]
        else:
            first = self.s.execute(f"SELECT min(ord) FROM ev_{ce.index} WHERE err").fetchone()[0]
        return None if first is None else int(first)

    def _skipped(self, ce: CompiledEntity) -> int:
        n = len(ce.group_by)
        ok = " AND ".join(f"k{j}_e IS NULL AND k{j}_t < 4" for j in range(n))
        null = " OR ".join(f"k{j}_t = 0" for j in range(n))
        return int(self.s.execute(f"SELECT count(*) FROM ev_{ce.index} WHERE {ok} AND ({null})").fetchone()[0])

    def map_entities(self) -> None:
        """Crawl mode: every entity mapping in order; the first problem (in the interpreter's order)
        raises. Valid rows land in ``out_<entity>``."""
        c_cache = {ce.index: compile_entity(ce.index, ce.em) for ce in self.c.entities}
        for ce in self.c.entities:
            self.stats.rows_seen += self.items[ce.source.key]
            self.s.execute(f"CREATE TABLE ev_{ce.index} AS {ce.sql}")
            first = self._first_error(ce)
            c = c_cache[ce.index]
            if ce.grouped:
                if first is not None:
                    self._reraise_group(ce, first)
                skipped = self._skipped(ce)
                if skipped:
                    self.stats.skipped[ce.where] = skipped
                sql = (ce.group_rows_sql or "").replace("{items}", f"ev_{ce.index}")
                n = 0
                for chunk in self._stream(sql):
                    batch = []
                    for r in chunk:
                        batch.append((int(r[0]), validate_row(c, self._decoded(ce, r, 2), f"group {n}")))
                        n += 1
                    self._out(ce, batch)
                continue
            where = "WHERE ord < ?" if first is not None else ""
            sql = f"SELECT ord, {self._tv(ce)} FROM ev_{ce.index} {where} ORDER BY ord"
            seen: set[str] = set()
            for chunk in self._stream(sql, [first] if first is not None else None):
                batch = []
                for r in chunk:
                    o = int(r[0])
                    row = validate_row(c, self._decoded(ce, r, 1), f"item {o}")
                    if ce.em.dedupe:
                        sid = str(row[0])
                        if sid in seen:
                            continue
                        seen.add(sid)
                    batch.append((o, row))
                self._out(ce, batch)
            if first is not None:
                self._reraise_item(ce, first)

    # ── checks ──
    def checks(self, *, fail: bool) -> list[dict[str, Any]]:
        """Duplicate ``source_id`` per entity, then :data:`~keelson_contracts.scn.SCN_FOREIGN_KEYS`
        (restricted to the mapped entities; warnings only when both ends are mapped). With ``fail``
        the first error raises (``data_invalid`` / ``fk_violation``); returns the checks with
        violations."""
        mapped = set(self.c.mapped_entities)
        found: list[dict[str, Any]] = []
        for entity in scn.SCN_ENTITIES:
            if entity not in mapped:
                continue
            base = f"SELECT source_id AS x FROM out_{entity} GROUP BY source_id HAVING count(*) > 1"
            n = int(self.s.execute(f"SELECT count(*) FROM ({base})").fetchone()[0])
            if not n:
                continue
            examples = [str(r[0]) for r in self.s.execute(f"{base} ORDER BY 1 LIMIT 5").fetchall()]
            if fail:
                raise CrawlError("data_invalid", f"{entity}: duplicate source_id {', '.join(examples)} "
                                                 "(mark derived rows dedupe)")
            found.append({"code": "duplicate_source_id", "label": f"{entity}: unique source_id", "entity": entity,
                          "severity": "error", "violations": n, "examples": examples})
        hint = f" (the crawl was truncated: {', '.join(self.stats.truncated)})" if self.stats.truncated else ""
        for fk in scn.SCN_FOREIGN_KEYS:
            if fk.entity not in mapped or (fk.severity == "warning" and fk.target_entity not in mapped):
                continue
            cond = [f"s.{c} IS NOT NULL" for c in fk.columns]
            if fk.kinds:
                cond.append(f"s.kind IN ({', '.join(sql_str(k) for k in fk.kinds)})")
            match = [f"t.{tc} = s.{c}" for c, tc in zip(fk.columns, fk.target_columns, strict=True)]
            if fk.target_kinds:
                match.append(f"t.kind IN ({', '.join(sql_str(k) for k in fk.target_kinds)})")
            label = "concat_ws(':', " + ", ".join(f"CAST(s.{c} AS VARCHAR)" for c in fk.columns) + ")"
            base = (f"SELECT DISTINCT {label} AS x FROM out_{fk.entity} s WHERE {' AND '.join(cond)} AND NOT EXISTS "
                    f"(SELECT 1 FROM out_{fk.target_entity} t WHERE {' AND '.join(match)})")
            n = int(self.s.execute(f"SELECT count(*) FROM ({base})").fetchone()[0])
            if not n:
                continue
            examples = [str(r[0]) for r in self.s.execute(f"{base} ORDER BY 1 LIMIT 5").fetchall()]
            if fail and fk.severity == "error":
                raise CrawlError("fk_violation", f"{fk.label}: {', '.join(examples)}{hint}")
            found.append({"code": "fk", "label": fk.label, "entity": fk.entity, "severity": fk.severity,
                          "violations": n, "examples": examples})
        return found

    # ── output ──
    def out_batches(self, entity: str) -> Iterator[list[tuple[Any, ...]]]:
        cols = ", ".join(scn.SCN_COLUMNS[entity])
        yield from self._stream(f"SELECT {cols} FROM out_{entity} ORDER BY m, ord")

    # ── preview ──
    def preview_entities(self, sample: int) -> list[dict[str, Any]]:
        """Preview mode: per entity mapping the rows that validate, failures (all of them), the
        first problem as the crawl would report it, per-column statistics and sample rows."""
        out: list[dict[str, Any]] = []
        for ce in self.c.entities:
            self.stats.rows_seen += self.items[ce.source.key]
            out.append(self._preview_entity(ce, sample))
        return out

    def _first_problem(self, fn: Callable[[], None]) -> str | None:
        try:
            fn()
        except CrawlError as exc:
            return f"{exc.code}: {exc.detail}"[:500]
        return None

    def _column_failure(self, ce: CompiledEntity, column: str, ord_: int) -> str:
        ast = dict(ce.columns)[column]
        target = ast.args[0] if ce.grouped and getattr(ast, "fn", None) in ("first", "min", "max", "sum") else ast
        try:
            evaluate(target, self.raw_item(ce.source.key, ord_), self.lookups_proxy)  # type: ignore[arg-type]
        except MappingEvalError as exc:
            return f"item {ord_}: {exc}"[:500]
        return f"item {ord_}: could not be evaluated"

    def _preview_entity(self, ce: CompiledEntity, sample: int) -> dict[str, Any]:
        c = compile_entity(ce.index, ce.em)
        ev = f"ev_{ce.index}"
        self.s.execute(f"CREATE TABLE {ev} AS {ce.sql}")
        first = self._first_error(ce)
        entry: dict[str, Any] = {"entity": ce.em.entity, "where": ce.where, "rows": 0, "failed": 0, "skipped": 0,
                                 "sample": [], "error": None, "columns": []}
        fail_cols: dict[str, tuple[int, int | None]] = {}
        for j, (column, ast) in enumerate(ce.columns):
            prefix = "a" if ce.grouped and getattr(ast, "fn", None) in ("first", "min", "max", "sum") else "c"
            if ce.grouped and getattr(ast, "fn", None) == "count":
                fail_cols[column] = (0, None)
                continue
            n, o = self.s.execute(f"SELECT count(*) FILTER (WHERE {prefix}{j}_e IS NOT NULL), "
                                  f"min(ord) FILTER (WHERE {prefix}{j}_e IS NOT NULL) FROM {ev}").fetchone()
            fail_cols[column] = (int(n), None if o is None else int(o))
        problems: list[tuple[int, str]] = []
        if first is not None:
            msg = self._first_problem(lambda: self._reraise_group(ce, first) if ce.grouped
                                      else self._reraise_item(ce, first))
            problems.append((first, msg or "mapping_failed"))
        rows: list[tuple[Any, ...]] = []
        if ce.grouped:
            entry["skipped"] = self._skipped(ce)
            entry["failed"] = int(self.s.execute(f"SELECT count(*) FROM ge_{ce.index} WHERE err").fetchone()[0])
            items = f"(SELECT * FROM {ev} WHERE ord NOT IN (SELECT ord FROM ge_{ce.index} WHERE err))"
            sql = (ce.group_rows_sql or "").replace("{items}", items)
            n = 0
            for chunk in self._stream(sql):
                batch = []
                for r in chunk:
                    try:
                        batch.append((int(r[0]), validate_row(c, self._decoded(ce, r, 2), f"group {n}")))
                    except CrawlError as exc:
                        entry["failed"] += 1
                        problems.append((int(r[0]), f"{exc.code}: {exc.detail}"[:500]))
                    n += 1
                rows += [row for _o, row in batch]
                self._out(ce, batch)
        else:
            entry["failed"] = int(self.s.execute(f"SELECT count(*) FROM {ev} WHERE err").fetchone()[0])
            seen: set[str] = set()
            for chunk in self._stream(f"SELECT ord, {self._tv(ce)} FROM {ev} WHERE NOT err ORDER BY ord"):
                batch = []
                for r in chunk:
                    o = int(r[0])
                    try:
                        row = validate_row(c, self._decoded(ce, r, 1), f"item {o}")
                    except CrawlError as exc:
                        entry["failed"] += 1
                        problems.append((o, f"{exc.code}: {exc.detail}"[:500]))
                        continue
                    if ce.em.dedupe:
                        if str(row[0]) in seen:
                            continue
                        seen.add(str(row[0]))
                    batch.append((o, row))
                rows += [row for _o, row in batch]
                self._out(ce, batch)
        entry["rows"] = len(rows)
        if ce.grouped and first is not None:
            entry["error"] = problems[0][1]  # item errors stop a grouped crawl before any group is validated
        else:
            entry["error"] = min(problems)[1] if problems else None
        cols = scn.SCN_COLUMNS[ce.em.entity]
        entry["sample"] = [{col: (v.isoformat() if isinstance(v, dt.date) else v) for col, v in zip(cols, row, strict=True)}
                           for row in rows[:sample]]
        for column, _ast in ce.columns:
            k = cols.index(column)
            values = [row[k] for row in rows]
            nulls = sum(1 for v in values if v is None)
            distinct = len({json.dumps(v, sort_keys=True, default=str) for v in values if v is not None})
            n_fail, o = fail_cols[column]
            entry["columns"].append({
                "column": column, "null_bp": ratio_bp(nulls, len(values)) if values else 0, "distinct": distinct,
                "failures": n_fail, "first_failure": self._column_failure(ce, column, o) if o is not None else None,
            })
        return entry


# ───────────────────────────── async drivers ─────────────────────────────


async def _blocking(session: DuckSession, fn: Callable[..., T], *args: Any) -> T:
    """``fn(*args)`` in a worker thread. A cancelled caller interrupts DuckDB and waits for the
    thread before re-raising (the session is never closed under a running statement)."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            session.con.interrupt()
        with suppress(BaseException):
            await task
        raise


async def _fetch(run: CompiledRun, call: ToolCaller, *, max_pages: int | None, writer: Any = None) -> None:
    """Page every distinct source once (lookup sources first, then entity sources)."""
    from keelson_engine.core.scn_staging import fetch_key

    for cs in run.c.sources.values():
        before = len(run.stats.truncated)
        page_no = 0
        async for items in _pages(call, cs.source, cs.used_by[0], run.stats, max_pages=max_pages,
                                  truncate_as=cs.used_by):
            page = await _blocking(run.s, run.ingest, cs.key, items)
            if writer is not None:
                await writer.add(cs.key, page_no, len(items), page)
            page_no += 1
        run.stats.sources.append({"key": cs.key, "fetch_key": fetch_key(cs.source), "tool": cs.source.tool,
                                  "pages": page_no, "items": run.items[cs.key],
                                  "truncated": len(run.stats.truncated) > before})


def _settings() -> Any:
    from keelson_engine.config import get_settings

    return get_settings()


async def crawl(conn: Any, sid: Any, mapping: ReadMapping, stats: CrawlStats, *, mapping_version: int | None) -> dict[str, int]:
    """The DuckDB engine of :func:`~keelson_engine.core.scn_crawl.run_crawl`: fetch (+ stage) every
    distinct source, map, check, then load the rows under snapshot ``sid``; returns row counts."""
    from keelson_engine.core import scn_staging

    st = _settings()
    compiled = compile_read_mapping(mapping)
    stats.engine = "duckdb"
    tenant, connector = conn.tenant, conn.slug
    writer = await scn_staging.begin(tenant, connector, sid, mapping_version=mapping_version,
                                     mapping_hash=compiled.hash, enabled=st.scn_raw_staging)
    counts: dict[str, int] = {e: 0 for e in scn.SCN_ENTITIES}
    with open_duckdb(st.duckdb_memory_limit_mb, st.duckdb_threads) as session:
        run = CompiledRun(compiled, session, stats, row_limit=row_limit(), max_bytes=st.scn_raw_max_bytes)
        try:
            async with session_caller(conn) as call:
                await _fetch(run, call, max_pages=None, writer=writer)
        except BaseException:
            if writer is not None:
                with suppress(Exception):
                    await asyncio.shield(writer.finish("failed", sources=stats.sources, fetched_from=stats.first_at,
                                                       fetched_to=stats.last_at))
            raise
        if writer is not None:
            await writer.finish("complete", sources=stats.sources, fetched_from=stats.first_at, fetched_to=stats.last_at)
        await _blocking(session, run.build_lookups)
        await _blocking(session, run.map_entities)
        stats.checks = await _blocking(session, lambda: run.checks(fail=True))
        for entity in scn.SCN_ENTITIES:
            batches = run.out_batches(entity)
            while (rows := await _blocking(session, next, batches, None)) is not None:
                counts[entity] += await _flush(tenant, sid, entity, rows)
    return counts


async def compiled_rows(call: ToolCaller, mapping: ReadMapping, *, stats: CrawlStats,
                        max_pages: int | None = None) -> AsyncIterator[tuple[str, list[tuple[Any, ...]]]]:
    """``(entity, rows)`` batches of the DuckDB engine without staging or loading (tests,
    differential comparisons): rows of each entity in mapping order, after every check."""
    st = _settings()
    compiled = compile_read_mapping(mapping)
    stats.engine = "duckdb"
    with open_duckdb(st.duckdb_memory_limit_mb, st.duckdb_threads) as session:
        run = CompiledRun(compiled, session, stats, row_limit=row_limit(), max_bytes=st.scn_raw_max_bytes)
        await _fetch(run, call, max_pages=max_pages)
        await _blocking(session, run.build_lookups)
        await _blocking(session, run.map_entities)
        stats.checks = await _blocking(session, lambda: run.checks(fail=True))
        order = [e.em.entity for e in compiled.entities]
        for entity in dict.fromkeys(order):
            batches = run.out_batches(entity)
            while (rows := await _blocking(session, next, batches, None)) is not None:
                yield entity, rows


async def preview(tenant: str, mapping: ReadMapping, *, source: Literal["live", "staged"], conn: Any = None,
                  max_pages: int = 1, max_items: int | None = None, sample: int = 5,
                  memory_mb: int | None = None) -> dict[str, Any]:
    """A read preview (``ReadPreview`` JSON): ``live`` pages at most ``max_pages`` page(s) per
    distinct source through ``conn`` (reader principal, egress proxy); ``staged`` maps the newest
    complete raw set of the connector (no vendor call), at most ``max_items`` items per source.
    Nothing is stored. Raises ``LookupError`` when ``staged`` has no raw set."""
    from keelson_engine.core import scn_staging

    st = _settings()
    compiled = compile_read_mapping(mapping)
    stats = CrawlStats(engine="duckdb")
    staged: dict[str, Any] | None = None
    capped = False
    limit = max_items or st.mapping_preview_max_items
    with open_duckdb(memory_mb or st.duckdb_memory_limit_mb, st.duckdb_threads) as session:
        run = CompiledRun(compiled, session, stats, max_bytes=st.scn_raw_max_bytes)
        try:
            if source == "live":
                async with session_caller(conn) as call:
                    await _fetch(run, call, max_pages=max_pages)
            else:
                staged = await scn_staging.latest_set(tenant, conn.slug)
                if staged is None:
                    raise LookupError("no staged crawl data")
                capped = await _ingest_staged(run, tenant, staged, limit)
            await _blocking(session, run.build_lookups)
        except CrawlError as exc:
            return _preview_json(stats, source, staged, capped, [], [], f"{exc.code}: {exc.detail}"[:500])
        entities = await _blocking(session, run.preview_entities, sample)
        checks = await _blocking(session, lambda: run.checks(fail=False))
    return _preview_json(stats, source, staged, capped, entities, checks, None)


async def _ingest_staged(run: CompiledRun, tenant: str, staged: dict[str, Any], limit: int) -> bool:
    from keelson_engine.core.scn_staging import fetch_key, read_pages

    by_key = {s.get("key"): s for s in staged.get("sources") or []}
    by_fetch = {s.get("fetch_key"): s for s in staged.get("sources") or []}
    capped = False
    for cs in run.c.sources.values():
        found = by_key.get(cs.key) or by_fetch.get(fetch_key(cs.source))
        if found is None:
            raise CrawlError("not_staged", f"{cs.used_by[0]}: the last crawl did not read {cs.source.tool} with these "
                                           "arguments; preview it live or crawl once")
        pages = 0
        async for items in read_pages(tenant, staged["snapshot_id"], str(found["key"]), max_pages=cs.source.max_pages):
            room = limit - run.items[cs.key]
            if room <= 0:
                capped = True
                break
            if len(items) > room:
                items, capped = items[:room], True
            await _blocking(run.s, run.ingest, cs.key, items)
            pages += 1
        run.stats.pages += pages
        if found.get("truncated"):
            run.stats.truncated.extend(cs.used_by)
    return capped


def _preview_json(stats: CrawlStats, source: str, staged: dict[str, Any] | None, capped: bool,
                  entities: list[dict[str, Any]], checks: list[dict[str, Any]], error: str | None) -> dict[str, Any]:
    full = source == "staged" and not capped
    blocking = [c for c in checks if c["severity"] == "error"] if full else []
    ok = error is None and not any(e["error"] for e in entities) and not blocking
    out = {
        "ok": ok, "error": error, "engine": "duckdb", "source": source,
        "staged": None if staged is None else {
            "snapshot_id": staged["snapshot_id"],
            "status": "complete" if staged.get("snapshot_status") != "failed" else "failed",
            "fetched_from": staged.get("fetched_from"), "fetched_to": staged.get("fetched_to")},
        "capped": capped, "pages": stats.pages, "rows_seen": stats.rows_seen, "truncated": list(stats.truncated),
        "lookups": dict(stats.lookups), "entities": entities, "checks": checks,
    }
    return ReadPreview.model_validate(out).model_dump(mode="json")
