"""DuckDB mapping compiler (``docs/contracts/121-m8-mapping-compiler.md`` §2): a tier-A read
mapping → SQL over the crawled items. **Pure**: no I/O, no DuckDB import; the session
(:mod:`keelson_engine.core.duckdb_session`) and the execution (:mod:`keelson_engine.core.mapping_exec`)
live elsewhere.

The M7 interpreter (:mod:`keelson_engine.core.mapping_eval`) stays the **normative semantics**;
this compiler must agree with it on every value, ``null`` and error (differential tests). How:

* **Sources once.** Entity mappings and lookups with equal :class:`~keelson_contracts.mapping.ReadSource`
  share one paged read (:func:`~keelson_contracts.mapping.source_key`); ``foreign-erp-lite`` pages
  its styles and stores once instead of twice.
* **Exact paths.** Python projects every item onto the paths the mapping reads with
  :func:`~keelson_engine.core.mapping_eval.get_path` itself (:meth:`CompiledSource.flatten`), so the
  JSONPath dialect never diverges and DuckDB's JSON extension is not needed. Each path becomes a
  *variant* pair of columns: a tag ``p<i>_t`` (0 null/missing, 1 text, 2 integer, 3 boolean,
  4 float, 5 object, 6 list) and a text ``p<i>_v`` (text as-is, integers in decimal, booleans
  ``true``/``false``, floats as Python ``repr``, containers as JSON). The raw item is kept as JSON
  text (``raw``) for error messages.
* **Values with errors.** Every AST node compiles to three columns — tag, text and an error code
  (``NULL`` = no error) — each defined once, in the first of a chain of CTE stages after everything
  it reads (never a lateral alias: DuckDB expands those by copying, exponentially in the nesting);
  ``CASE`` chains, never a function that can raise (``TRY_CAST``, guarded ``make_date``). Errors are
  values, so the lazy semantics of ``coalesce`` / ``if_present`` / ``+`` (an argument that is not
  evaluated never fails) are reproduced exactly by choosing whose error propagates. Messages are
  not generated in SQL: the execution re-evaluates the failing item with the interpreter, which
  raises the exact M7 message.
* **Joins.** ``lookup()`` / ``field()`` call sites are ``LEFT JOIN`` s onto per-table key → value
  (record) tables built first (first item wins). A call whose key needs another join's result is
  placed one CTE stage later.
* **Grouping.** ``group_by`` mappings compile to a per-item stage (key, aggregate argument and
  field columns) plus a ``GROUP BY`` over the key texts; per-item errors (key errors, aggregate
  type rules, the running ``sum`` range) use window functions in crawl order.
* **Safe SQL.** Identifiers are generated (``src_<key>``, ``lk_<name>`` from the contract's
  ``[a-z][a-z0-9_]*`` table names, ``p3_t``, ``n12_v``); every constant goes through
  :func:`sql_str` (quotes doubled, NUL refused). No user text reaches SQL otherwise.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, get_args, get_origin

from keelson_contracts import scn
from keelson_contracts.common import MAX_SAFE_INTEGER
from keelson_contracts.mapping import (
    AGGREGATE_FUNCTIONS,
    CallExpr,
    ConcatExpr,
    ConstExpr,
    EntityMapping,
    Expr,
    MappingPlan,
    PathExpr,
    ReadMapping,
    ReadSource,
    RecordLookup,
    SourceLookup,
    StaticLookup,
    expression_paths,
    expression_record_paths,
    is_aggregate,
    mapping_hash,
    parse_expression,
    parse_path,
    path_text,
    required_scn_columns,
    source_key,
)
from keelson_engine.core.mapping_eval import (
    ASCII_SPACE,
    DECIMAL_PATTERN,
    INT_PATTERN,
    ISO_DATE_PATTERN,
    ISO_WEEK_TEXT_PATTERN,
    MAX_TEXT,
    get_path,
)

__all__ = [
    "CompileError",
    "CompiledEntity",
    "CompiledLookup",
    "CompiledMapping",
    "CompiledSource",
    "PY_WHITESPACE",
    "TAG_BOOL",
    "TAG_FLOAT",
    "TAG_INT",
    "TAG_LIST",
    "TAG_NULL",
    "TAG_OBJECT",
    "TAG_STR",
    "compile_read_mapping",
    "decode",
    "encode",
    "explain",
    "sql_str",
]

TAG_NULL, TAG_STR, TAG_INT, TAG_BOOL, TAG_FLOAT, TAG_OBJECT, TAG_LIST = range(7)
_MAX: Final = MAX_SAFE_INTEGER

PY_WHITESPACE: Final = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0           "
    "     　"
)
"""What Python's ``str.strip()`` removes (``date()`` / ``iso_week()`` strip their input with it)."""


class CompileError(ValueError):
    """The mapping cannot be compiled (never for a mapping the contract accepts)."""


def sql_str(text: str) -> str:
    """The only way a constant reaches SQL: a single-quoted literal (quotes doubled). DuckDB
    string literals take backslashes literally; a NUL character is refused."""
    if "\x00" in text:
        raise CompileError("a constant contains a NUL character")
    return "'" + text.replace("'", "''") + "'"


def _re_class(chars: str) -> str:
    return "[" + "".join(f"\\x{{{ord(c):x}}}" for c in chars) + "]"


_STRIP_RE: Final = f"^{_re_class(PY_WHITESPACE)}+|{_re_class(PY_WHITESPACE)}+$"
_ALIAS_RE: Final = re.compile(r"\b(n\d+_[a-z]+)\b")
_JOIN_RE: Final = re.compile(r"\b(j\d+)\.")
_COL_RE: Final = re.compile(r"\b(ord|raw|p\d+_[tv]|n\d+_[a-z]+)\b")
_ASCII_TRIM: Final = " || ".join(f"chr({ord(c)})" for c in ASCII_SPACE)
_NULL_E: Final = "NULL::VARCHAR"
_UPPER_FROM: Final = sql_str("abcdefghijklmnopqrstuvwxyz")
_UPPER_TO: Final = sql_str("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


# ───────────────────────────── variants ─────────────────────────────


def encode(value: Any) -> tuple[int, str]:
    """A JSON value → (tag, text)."""
    if value is None:
        return TAG_NULL, ""
    if isinstance(value, bool):
        return TAG_BOOL, "true" if value else "false"
    if isinstance(value, int):
        return TAG_INT, str(value)
    if isinstance(value, float):
        return TAG_FLOAT, repr(value)
    if isinstance(value, str):
        return TAG_STR, value
    if type(value) is dict:
        return TAG_OBJECT, json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if type(value) is list:
        return TAG_LIST, json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    raise CompileError(f"not a JSON value: {type(value).__name__}")


def decode(tag: int, text: str | None) -> Any:
    """(tag, text) → the JSON value (inverse of :func:`encode`)."""
    if tag == TAG_NULL:
        return None
    if tag == TAG_STR:
        return text if text is not None else ""
    if tag == TAG_INT:
        return int(text or "0")
    if tag == TAG_BOOL:
        return text == "true"
    if tag == TAG_FLOAT:
        return float(text or "nan")
    return json.loads(text or "null")


# ───────────────────────────── compiled shapes ─────────────────────────────


@dataclass(slots=True)
class CompiledSource:
    """One distinct paged read and the item paths the mapping reads from it."""

    key: str
    source: ReadSource
    paths: list[PathExpr] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)
    _index: dict[tuple[str | int, ...], int] = field(default_factory=dict)

    @property
    def table(self) -> str:
        return f"src_{self.key}"

    def col(self, path: PathExpr) -> int:
        i = self._index.get(path.segments)
        if i is None:
            i = self._index[path.segments] = len(self.paths)
            self.paths.append(path)
        return i

    def ddl(self) -> str:
        cols = ", ".join(f"p{i}_t UTINYINT, p{i}_v VARCHAR" for i in range(len(self.paths)))
        return f"CREATE TABLE {self.table} (ord BIGINT, raw VARCHAR{', ' + cols if cols else ''})"

    def flatten(self, items: Sequence[Any], start: int) -> Any:
        """An Arrow record batch of ``items`` (``ord`` from ``start``): the raw JSON and every path
        read with the interpreter's own :func:`get_path`."""
        import pyarrow as pa

        tags: list[list[int]] = [[] for _ in self.paths]
        texts: list[list[str]] = [[] for _ in self.paths]
        raws: list[str] = []
        for item in items:
            raws.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            for i, path in enumerate(self.paths):
                t, v = encode(get_path(item, path))
                tags[i].append(t)
                texts[i].append(v)
        arrays: dict[str, Any] = {"ord": pa.array(range(start, start + len(items)), pa.int64()),
                                  "raw": pa.array(raws, pa.string())}
        for i in range(len(self.paths)):
            arrays[f"p{i}_t"] = pa.array(tags[i], pa.uint8())
            arrays[f"p{i}_v"] = pa.array(texts[i], pa.string())
        return pa.record_batch(arrays)


@dataclass(slots=True)
class CompiledLookup:
    name: str
    kind: Literal["static", "source", "record"]
    table: StaticLookup | SourceLookup | RecordLookup
    source: CompiledSource | None
    sql: str | None = None
    """``source`` / ``record``: the per-item build query (key / value columns + ``ord``)."""
    record_paths: list[PathExpr] = field(default_factory=list)
    _rindex: dict[tuple[str | int, ...], int] = field(default_factory=dict)

    @property
    def db_table(self) -> str:
        return f"rec_{self.name}" if self.kind == "record" else f"lk_{self.name}"

    @property
    def where(self) -> str:
        return f"lookups.{self.name}"

    def rcol(self, path: PathExpr) -> int:
        i = self._rindex.get(path.segments)
        if i is None:
            i = self._rindex[path.segments] = len(self.record_paths)
            self.record_paths.append(path)
        return i


@dataclass(slots=True)
class CompiledEntity:
    index: int
    where: str
    em: EntityMapping
    source: CompiledSource
    columns: list[tuple[str, Expr]]
    """Mapped columns in contract order with their ASTs."""
    group_by: list[Expr]
    sql: str
    """Per item: ``ord`` + per column ``c<j>_t/_v/_e`` (grouped: ``k<j>``, ``a<j>`` / ``c<j>``) + ``err``."""
    column_sql: dict[str, str]
    joins: Counter[str]
    group_errors_sql: str | None = None
    """Grouped: per item ``ord, err`` over the per-item table (key errors, aggregate rules)."""
    group_rows_sql: str | None = None
    """Grouped: one row per group (``g_ord`` + ``c<j>_t/_v``), in first-item order."""

    @property
    def grouped(self) -> bool:
        return bool(self.group_by)


@dataclass(slots=True)
class CompiledMapping:
    mapping: ReadMapping
    hash: str
    sources: dict[str, CompiledSource]
    lookups: list[CompiledLookup]
    entities: list[CompiledEntity]

    def lookup(self, name: str) -> CompiledLookup:
        return next(lk for lk in self.lookups if lk.name == name)

    @property
    def mapped_entities(self) -> list[str]:
        return self.mapping.mapped_entities


# ───────────────────────────── code generation ─────────────────────────────


@dataclass(frozen=True, slots=True)
class _Val:
    t: str
    v: str
    e: str
    level: int


def _ekey(expr: Expr) -> tuple[Any, ...]:
    """A memo key that keeps ``true`` and ``1`` apart (dataclass equality would not)."""
    if isinstance(expr, ConstExpr):
        return ("c", type(expr.value).__name__, expr.value)
    if isinstance(expr, PathExpr):
        return ("p", expr.segments)
    if isinstance(expr, ConcatExpr):
        return ("+", tuple(_ekey(p) for p in expr.parts))
    return ("f", expr.fn, tuple(_ekey(a) for a in expr.args))


def _between(sql: str) -> str:
    return f"({sql} BETWEEN {-_MAX} AND {_MAX})"


class _Gen:
    """SQL for the expressions over one source's items: node aliases per CTE stage (a stage per
    join depth) and the joins of each stage."""

    def __init__(self, source: CompiledSource, lookups: Mapping[str, CompiledLookup], *, raw: bool = False) -> None:
        self.source = source
        self.lookups = lookups
        self.raw = raw
        self.defs: list[list[str]] = [[]]
        self.joins: list[list[str]] = [[]]
        self.memo: dict[tuple[Any, ...], _Val] = {}
        self.n = 0
        self.calls: Counter[str] = Counter()
        self.stage_of: dict[str, int] = {}
        self.join_stage: dict[str, int] = {}

    # ── helpers ──
    def _stage(self, level: int) -> None:
        while len(self.defs) <= level:
            self.defs.append([])
            self.joins.append([])

    def _name(self) -> str:
        self.n += 1
        return f"n{self.n}"

    def _stage_for(self, *sqls: str) -> int:
        """The first CTE stage where ``sqls`` can be evaluated: after every alias they read (an alias
        is never read in its own stage — DuckDB expands lateral aliases by copying, exponentially),
        and in the stage of every join whose columns they read."""
        stage = 1
        for sql in sqls:
            for alias in _ALIAS_RE.findall(sql):
                stage = max(stage, self.stage_of.get(alias, 0) + 1)
            for join in _JOIN_RE.findall(sql):
                stage = max(stage, self.join_stage[join])
        return stage

    def _helper(self, level: int, name: str, suffix: str, sql: str) -> str:
        del level  # the stage follows from what the SQL reads
        alias = f"{name}_{suffix}"
        stage = self._stage_for(sql)
        self._stage(stage)
        self.defs[stage].append(f"{sql} AS {alias}")
        self.stage_of[alias] = stage
        return alias

    def _define(self, level: int, name: str, t: str, v: str, e: str) -> _Val:
        del level
        stage = self._stage_for(t, v, e)
        self._stage(stage)
        self.defs[stage] += [f"{t} AS {name}_t", f"{v} AS {name}_v", f"{e} AS {name}_e"]
        for suffix in ("t", "v", "e"):
            self.stage_of[f"{name}_{suffix}"] = stage
        return _Val(f"{name}_t", f"{name}_v", f"{name}_e", stage)

    # ── nodes ──
    def val(self, expr: Expr) -> _Val:
        key = _ekey(expr)
        hit = self.memo.get(key)
        if hit is None:
            hit = self.memo[key] = self._val(expr)
        return hit

    def _val(self, expr: Expr) -> _Val:
        if isinstance(expr, PathExpr):
            i = self.source.col(expr)
            return _Val(f"p{i}_t", f"p{i}_v", _NULL_E, 0)
        if isinstance(expr, ConstExpr):
            t, v = encode(expr.value)
            return _Val(str(t), sql_str(v), _NULL_E, 0)
        if isinstance(expr, ConcatExpr):
            return self._concat([self.val(p) for p in expr.parts])
        if is_aggregate(expr):
            raise CompileError(f"{expr.fn}() outside a grouped field")
        fn = getattr(self, f"_fn_{expr.fn}", None)
        if fn is None:
            raise CompileError(f"no SQL for {expr.fn}()")
        result: _Val = fn(expr)
        return result

    def _concat(self, parts: list[_Val]) -> _Val:
        level = max(p.level for p in parts)
        name = self._name()
        whens: list[str] = []
        for k, p in enumerate(parts):
            size = " + ".join(f"length({q.v})" for q in parts[: k + 1])
            whens += [f"WHEN {p.e} IS NOT NULL THEN {p.e}", f"WHEN {p.t} >= 4 THEN 'concat'",
                      f"WHEN {p.t} = 0 THEN NULL", f"WHEN {size} > {MAX_TEXT} THEN 'concat'"]
        e = f"CASE {' '.join(whens)} END"
        t = f"CASE WHEN {' OR '.join(f'{p.t} = 0' for p in parts)} THEN 0 ELSE 1 END"
        v = " || ".join(p.v for p in parts)
        return self._define(level, name, t, v, e)

    def _unary(self, expr: CallExpr) -> tuple[_Val, int, str]:
        c = self.val(expr.args[0])
        return c, c.level, self._name()

    def _fn_int(self, expr: CallExpr) -> _Val:
        c, level, name = self._unary(expr)
        e = (f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} = 0 THEN NULL "
             f"WHEN {c.t} = 2 THEN (CASE WHEN length({c.v}) <= 17 AND {_between(f'TRY_CAST({c.v} AS HUGEINT)')} "
             f"THEN NULL ELSE 'int' END) "
             f"WHEN {c.t} = 1 THEN (CASE WHEN regexp_full_match({c.v}, {sql_str(INT_PATTERN)}) "
             f"AND {_between(f'TRY_CAST({c.v} AS BIGINT)')} THEN NULL ELSE 'int' END) ELSE 'int' END")
        t = f"CASE WHEN {c.t} = 0 THEN 0 ELSE 2 END"
        v = (f"CASE WHEN {c.t} IN (1, 2) AND length({c.v}) <= 17 "
             f"THEN coalesce(CAST(TRY_CAST({c.v} AS HUGEINT) AS VARCHAR), '') ELSE '' END")
        return self._define(level, name, t, v, e)

    def _fn_str(self, expr: CallExpr) -> _Val:
        c, level, name = self._unary(expr)
        e = f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} >= 4 THEN 'str' END"
        return self._define(level, name, f"CASE WHEN {c.t} = 0 THEN 0 ELSE 1 END", c.v, e)

    def _text_fn(self, expr: CallExpr, value_sql: str) -> _Val:
        c, level, name = self._unary(expr)
        e = (f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} = 0 THEN NULL "
             f"WHEN {c.t} <> 1 THEN {sql_str(expr.fn)} END")
        v = f"CASE WHEN {c.t} = 1 THEN {value_sql.format(v=c.v)} ELSE '' END"
        return self._define(level, name, f"CASE WHEN {c.t} = 0 THEN 0 ELSE 1 END", v, e)

    def _fn_trim(self, expr: CallExpr) -> _Val:
        return self._text_fn(expr, "trim({v}, " + _ASCII_TRIM + ")")

    def _fn_upper(self, expr: CallExpr) -> _Val:
        return self._text_fn(expr, f"translate({{v}}, {_UPPER_FROM}, {_UPPER_TO})")

    def _fn_lower(self, expr: CallExpr) -> _Val:
        return self._text_fn(expr, f"translate({{v}}, {_UPPER_TO}, {_UPPER_FROM})")

    def _strip(self, level: int, name: str, c: _Val) -> str:
        return self._helper(level, name, "s", f"CASE WHEN {c.t} = 1 THEN regexp_replace({c.v}, {sql_str(_STRIP_RE)}, '', 'g') END")

    def _calendar(self, level: int, name: str, ok: str, y: str, m: str, d: str) -> str:
        last = f"day(last_day(make_date(greatest({y}, 1), least(greatest({m}, 1), 12), 1)))"
        return self._helper(level, name, "valid",
                            f"coalesce({ok} AND {y} >= 1 AND {m} BETWEEN 1 AND 12 AND {d} >= 1 AND {d} <= {last}, false)")

    def _iso_ymd(self, level: int, name: str, s: str) -> tuple[str, str, str, str]:
        ok = self._helper(level, name, "ok", f"coalesce(regexp_full_match({s}, {sql_str(ISO_DATE_PATTERN)}), false)")
        y = self._helper(level, name, "y", f"CASE WHEN {ok} THEN TRY_CAST(substr({s}, 1, 4) AS INTEGER) END")
        m = self._helper(level, name, "m", f"CASE WHEN {ok} THEN TRY_CAST(substr({s}, 6, 2) AS INTEGER) END")
        d = self._helper(level, name, "d", f"CASE WHEN {ok} THEN TRY_CAST(substr({s}, 9, 2) AS INTEGER) END")
        return self._calendar(level, name, ok, y, m, d), y, m, d

    @staticmethod
    def _ymd_text(y: str, m: str, d: str) -> str:
        return (f"lpad(CAST({y} AS VARCHAR), 4, '0') || '-' || lpad(CAST({m} AS VARCHAR), 2, '0') || '-' || "
                f"lpad(CAST({d} AS VARCHAR), 2, '0')")

    def _fn_date(self, expr: CallExpr) -> _Val:
        c, level, name = self._unary(expr)
        s = self._strip(level, name, c)
        if len(expr.args) == 1:
            valid, y, m, d = self._iso_ymd(level, name, s)
        else:
            fmt = expr.args[1]
            assert isinstance(fmt, ConstExpr) and isinstance(fmt.value, str)
            sep = fmt.value[4] if fmt.value.startswith("YYYY") else fmt.value[2]
            tokens = [tok for tok in fmt.value.split(sep)]
            widths = {"YYYY": 4, "MM": 2, "DD": 2}
            parts = self._helper(level, name, "p", f"string_split({s}, {sql_str(sep)})")
            checks = " AND ".join(f"regexp_full_match({parts}[{i + 1}], '[0-9]{{{widths[tok]}}}')"
                                  for i, tok in enumerate(tokens))
            ok = self._helper(level, name, "ok", f"coalesce(len({parts}) = 3 AND {checks}, false)")
            pos = {tok: i + 1 for i, tok in enumerate(tokens)}
            y = self._helper(level, name, "y", f"CASE WHEN {ok} THEN TRY_CAST({parts}[{pos['YYYY']}] AS INTEGER) END")
            m = self._helper(level, name, "m", f"CASE WHEN {ok} THEN TRY_CAST({parts}[{pos['MM']}] AS INTEGER) END")
            d = self._helper(level, name, "d", f"CASE WHEN {ok} THEN TRY_CAST({parts}[{pos['DD']}] AS INTEGER) END")
            valid = self._calendar(level, name, ok, y, m, d)
        e = (f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} = 0 THEN NULL WHEN {c.t} <> 1 THEN 'date' "
             f"WHEN NOT {valid} THEN 'date' END")
        v = f"CASE WHEN {valid} THEN {self._ymd_text(y, m, d)} ELSE '' END"
        return self._define(level, name, f"CASE WHEN {c.t} = 0 THEN 0 ELSE 1 END", v, e)

    def _fn_iso_week(self, expr: CallExpr) -> _Val:
        c, level, name = self._unary(expr)
        s = self._strip(level, name, c)
        wk = self._helper(level, name, "wk", f"coalesce(regexp_full_match({s}, {sql_str(ISO_WEEK_TEXT_PATTERN)}), false)")
        wy = self._helper(level, name, "wy", f"CASE WHEN {wk} THEN TRY_CAST(substr({s}, 1, 4) AS INTEGER) END")
        ww = self._helper(level, name, "ww", f"CASE WHEN {wk} THEN TRY_CAST(right({s}, 2) AS INTEGER) END")
        wok = self._helper(level, name, "wok",
                           f"coalesce({wk} AND {wy} >= 1 AND {ww} >= 1 AND {ww} <= week(make_date(greatest({wy}, 1), 12, 28)), false)")
        valid, y, m, d = self._iso_ymd(level, name, s)
        dd = self._helper(level, name, "dd", f"CASE WHEN NOT {wk} AND {valid} THEN make_date({y}, {m}, {d}) END")
        v = (f"CASE WHEN {wok} THEN lpad(CAST({wy} AS VARCHAR), 4, '0') || '-W' || lpad(CAST({ww} AS VARCHAR), 2, '0') "
             f"WHEN {dd} IS NOT NULL THEN lpad(CAST(isoyear({dd}) AS VARCHAR), 4, '0') || '-W' || "
             f"lpad(CAST(week({dd}) AS VARCHAR), 2, '0') ELSE '' END")
        e = (f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} = 0 THEN NULL WHEN {c.t} <> 1 THEN 'iso_week' "
             f"WHEN {wk} THEN (CASE WHEN {wok} THEN NULL ELSE 'iso_week' END) WHEN NOT {valid} THEN 'iso_week' END")
        return self._define(level, name, f"CASE WHEN {c.t} = 0 THEN 0 ELSE 1 END", v, e)

    def _fn_cents(self, expr: CallExpr) -> _Val:
        c, level, name = self._unary(expr)
        dec = self._helper(level, name, "ok",
                           f"coalesce({c.t} IN (1, 4) AND regexp_full_match({c.v}, {sql_str(DECIMAL_PATTERN)}), false)")
        frac = self._helper(level, name, "f", f"CASE WHEN {dec} THEN regexp_extract({c.v}, '\\.([0-9]+)$', 1) ELSE '' END")
        whole = self._helper(level, name, "w", f"CASE WHEN {dec} THEN regexp_extract({c.v}, '^[+-]?([0-9]+)', 1) END")
        amount = self._helper(
            level, name, "c",
            f"CASE WHEN {c.t} = 2 AND length({c.v}) <= 17 THEN TRY_CAST({c.v} AS HUGEINT) * 100 "
            f"WHEN {dec} THEN (CASE WHEN left({c.v}, 1) = '-' THEN -1 ELSE 1 END) * "
            f"(TRY_CAST({whole} AS HUGEINT) * 100 + TRY_CAST(rpad(left({frac}, 2), 2, '0') AS HUGEINT)) END")
        e = (f"CASE WHEN {c.e} IS NOT NULL THEN {c.e} WHEN {c.t} = 0 THEN NULL "
             f"WHEN {c.t} = 2 THEN (CASE WHEN {_between(amount)} THEN NULL ELSE 'cents' END) "
             f"WHEN {dec} THEN (CASE WHEN regexp_full_match(substr({frac}, 3), '0*') AND {_between(amount)} "
             f"THEN NULL ELSE 'cents' END) ELSE 'cents' END")
        v = f"coalesce(CAST({amount} AS VARCHAR), '')"
        return self._define(level, name, f"CASE WHEN {c.t} = 0 THEN 0 ELSE 2 END", v, e)

    def _fn_coalesce(self, expr: CallExpr) -> _Val:
        args = [self.val(a) for a in expr.args]
        level = max(a.level for a in args)
        name = self._name()
        e = "CASE " + " ".join(f"WHEN {a.e} IS NOT NULL THEN {a.e} WHEN {a.t} <> 0 THEN NULL" for a in args) + " END"
        t = "CASE " + " ".join(f"WHEN {a.t} <> 0 THEN {a.t}" for a in args) + " ELSE 0 END"
        v = "CASE " + " ".join(f"WHEN {a.t} <> 0 THEN {a.v}" for a in args) + " ELSE '' END"
        return self._define(level, name, t, v, e)

    def _fn_if_present(self, expr: CallExpr) -> _Val:
        probe, then, other = (self.val(a) for a in expr.args)
        level = max(probe.level, then.level, other.level)
        name = self._name()
        present = self._helper(level, name, "present", f"({probe.t} <> 0 AND NOT ({probe.t} = 1 AND {probe.v} = ''))")
        e = f"CASE WHEN {probe.e} IS NOT NULL THEN {probe.e} WHEN {present} THEN {then.e} ELSE {other.e} END"
        t = f"CASE WHEN {present} THEN {then.t} ELSE {other.t} END"
        v = f"CASE WHEN {present} THEN {then.v} ELSE {other.v} END"
        return self._define(level, name, t, v, e)

    def _join(self, table: str, key: _Val) -> tuple[str, int]:
        condition = f"(CASE WHEN {key.t} IN (1, 2, 3) THEN {key.v} END)"
        stage = self._stage_for(condition)
        self._stage(stage)
        self.n += 1
        alias = f"j{self.n}"
        self.join_stage[alias] = stage
        self.joins[stage].append(f"LEFT JOIN {table} AS {alias} ON {alias}.k = {condition}")
        return alias, stage

    def _fn_lookup(self, expr: CallExpr) -> _Val:
        table = expr.args[0]
        assert isinstance(table, ConstExpr) and isinstance(table.value, str)
        lk = self.lookups.get(table.value)
        if lk is None or lk.kind == "record":
            raise CompileError(f"lookup() on unknown or record table {table.value!r}")
        key = self.val(expr.args[1])
        dt, dv = encode(expr.args[2].value if len(expr.args) == 3 and isinstance(expr.args[2], ConstExpr) else None)
        alias, level = self._join(lk.db_table, key)
        self.calls[lk.name] += 1
        name = self._name()
        hit = f"({key.t} <> 0 AND {alias}.k IS NOT NULL)"
        e = f"CASE WHEN {key.e} IS NOT NULL THEN {key.e} WHEN {key.t} >= 4 THEN 'lookup' END"
        return self._define(level, name, f"CASE WHEN {hit} THEN {alias}.lt ELSE {dt} END",
                            f"CASE WHEN {hit} THEN {alias}.lv ELSE {sql_str(dv)} END", e)

    def _fn_field(self, expr: CallExpr) -> _Val:
        table, path = expr.args[0], expr.args[2]
        assert isinstance(table, ConstExpr) and isinstance(table.value, str)
        assert isinstance(path, ConstExpr) and isinstance(path.value, str)
        lk = self.lookups.get(table.value)
        if lk is None or lk.kind != "record":
            raise CompileError(f"field() on unknown or value table {table.value!r}")
        q = lk.rcol(parse_path(path.value))
        key = self.val(expr.args[1])
        alias, level = self._join(lk.db_table, key)
        self.calls[lk.name] += 1
        name = self._name()
        hit = f"({key.t} <> 0 AND {alias}.k IS NOT NULL)"
        e = f"CASE WHEN {key.e} IS NOT NULL THEN {key.e} WHEN {key.t} >= 4 THEN 'field' END"
        return self._define(level, name, f"CASE WHEN {hit} THEN {alias}.r{q}_t ELSE 0 END",
                            f"CASE WHEN {hit} THEN {alias}.r{q}_v ELSE '' END", e)

    # ── query ──
    def query(self, outputs: Sequence[str]) -> str:
        """``WITH s0 … sN SELECT <outputs> FROM sN`` (``outputs`` read the stages' columns)."""
        base = ["ord", *(["raw"] if self.raw else []),
                *(c for i in range(len(self.source.paths)) for c in (f"p{i}_t", f"p{i}_v"))]
        last = len(self.defs) - 1
        defined = [[d.rsplit(" AS ", 1)[1] for d in self.defs[k]] for k in range(last + 1)]
        order = {c: i for i, c in enumerate([*base, *(a for names in defined for a in names)])}
        # liveness: each stage passes on only what a later stage or the outputs read (a wide
        # pass-through of every column through every stage costs a vector per column and stage)
        out = {c for c in _COL_RE.findall(" ".join(outputs)) if c in order} | {"ord"}
        carry: list[list[str]] = [[] for _ in range(last + 1)]
        for k in range(last, 0, -1):
            own = set(defined[k])
            exprs = [d.rsplit(" AS ", 1)[0] for d in self.defs[k]]
            reads = {c for c in _COL_RE.findall(" ".join([*exprs, *self.joins[k]])) if c in order}
            carry[k] = sorted((out - own) & set(order), key=order.__getitem__)
            out = (out - own) | reads
        first = [c for c in base if c in out]
        stages = [f"s0 AS (SELECT {', '.join(first)} FROM {self.source.table})"]
        for k in range(1, last + 1):
            cols = ", ".join([*(f"s{k - 1}.{c}" for c in carry[k]), *self.defs[k]])
            stages.append(f"s{k} AS (SELECT {cols} FROM s{k - 1} {' '.join(self.joins[k])})")
        return f"WITH {', '.join(stages)} SELECT {', '.join(outputs)} FROM s{last}"

    def node_sql(self, val: _Val) -> str:
        """The definitions of ``val``'s own columns (for the explain view)."""
        if not (val.t.startswith("n") and val.t.endswith("_t")):
            return f"{val.t} AS tag, {val.v} AS text"
        marker = f" AS {val.t[:-1]}"
        return "\n".join(d for d in self.defs[val.level] if marker in d)


def _err_any(names: Iterable[str]) -> str:
    items = [f"{n} IS NOT NULL" for n in names]
    return "(" + " OR ".join(items) + ")" if items else "false"


# ───────────────────────────── compile ─────────────────────────────


def compile_read_mapping(mapping: ReadMapping) -> CompiledMapping:
    """Compile a (contract-valid) read mapping. Deterministic: the same mapping always yields the
    same SQL and the same :func:`explain`."""
    sources: dict[str, CompiledSource] = {}

    def src(s: ReadSource, where: str) -> CompiledSource:
        k = source_key(s)
        cs = sources.get(k)
        if cs is None:
            cs = sources[k] = CompiledSource(k, s)
        if where not in cs.used_by:
            cs.used_by.append(where)
        return cs

    lookups: list[CompiledLookup] = []
    for name, table in mapping.lookups.items():
        if isinstance(table, StaticLookup):
            lookups.append(CompiledLookup(name, "static", table, None))
        elif isinstance(table, SourceLookup):
            lookups.append(CompiledLookup(name, "source", table, src(table.source, f"lookups.{name}")))
        else:
            lookups.append(CompiledLookup(name, "record", table, src(table.source, f"lookups.{name}")))
    by_name = {lk.name: lk for lk in lookups}
    entities: list[CompiledEntity] = []
    for i, em in enumerate(mapping.entities):
        where = f"entities[{i}] ({em.entity})"
        entities.append(_compile_entity(i, where, em, src(em.source, where), by_name))
    # record paths are known once every field() call compiled: build the lookup queries last
    for lk in lookups:
        if lk.source is not None:
            lk.sql = _lookup_sql(lk, by_name)
    return CompiledMapping(mapping, mapping_hash(mapping), sources, lookups, entities)


def _lookup_sql(lk: CompiledLookup, by_name: Mapping[str, CompiledLookup]) -> str:
    assert lk.source is not None and not isinstance(lk.table, StaticLookup)
    gen = _Gen(lk.source, by_name, raw=lk.kind == "record")
    key = gen.val(parse_expression(lk.table.key))
    outputs = ["ord", f"{key.t} AS key_t", f"{key.v} AS key_v", f"{key.e} AS key_e"]
    if isinstance(lk.table, SourceLookup):
        value = gen.val(parse_expression(lk.table.value))
        outputs += [f"{value.t} AS val_t", f"{value.v} AS val_v", f"{value.e} AS val_e"]
    else:
        outputs.append("raw")
        for q, path in enumerate(lk.record_paths):
            i = lk.source.col(path)
            outputs += [f"p{i}_t AS r{q}_t", f"p{i}_v AS r{q}_v"]
    return gen.query(outputs)


def _compile_entity(index: int, where: str, em: EntityMapping, source: CompiledSource,
                    lookups: Mapping[str, CompiledLookup]) -> CompiledEntity:
    gen = _Gen(source, lookups)
    columns = list(em.parsed_fields().items())
    groups = em.parsed_group_by()
    outputs = ["ord"]
    column_sql: dict[str, str] = {}
    errs: list[str] = []
    if not groups:
        for j, (column, ast) in enumerate(columns):
            val = gen.val(ast)
            outputs += [f"{val.t} AS c{j}_t", f"{val.v} AS c{j}_v", f"{val.e} AS c{j}_e"]
            errs.append(val.e)
            column_sql[column] = gen.node_sql(val)
        outputs.append(f"{_err_any(errs)} AS err")
        return CompiledEntity(index, where, em, source, columns, [], gen.query(outputs), column_sql, gen.calls)
    for j, g in enumerate(groups):
        val = gen.val(g)
        outputs += [f"{val.t} AS k{j}_t", f"{val.v} AS k{j}_v", f"{val.e} AS k{j}_e"]
    for j, (column, ast) in enumerate(columns):
        if isinstance(ast, CallExpr) and ast.fn in AGGREGATE_FUNCTIONS:
            if ast.args:
                val = gen.val(ast.args[0])
                outputs += [f"{val.t} AS a{j}_t", f"{val.v} AS a{j}_v", f"{val.e} AS a{j}_e"]
                column_sql[column] = f"{ast.fn}(…) over\n{gen.node_sql(val)}"
            else:
                column_sql[column] = "count(*)"
        else:
            val = gen.val(ast)
            outputs += [f"{val.t} AS c{j}_t", f"{val.v} AS c{j}_v", f"{val.e} AS c{j}_e"]
            column_sql[column] = f"first item:\n{gen.node_sql(val)}"
    item_sql = gen.query(outputs)
    errors_sql, rows_sql = _group_sql(len(groups), columns)
    return CompiledEntity(index, where, em, source, columns, groups, item_sql, column_sql, gen.calls, errors_sql,
                          rows_sql)


def _group_sql(n_keys: int, columns: Sequence[tuple[str, Expr]]) -> tuple[str, str]:
    """Over the per-item table ``{items}``: (per-item errors, one row per group)."""
    keys = [f"gk{j}" for j in range(n_keys)]
    key_cols = ", ".join(f"CASE WHEN k{j}_t IN (1, 2, 3) THEN k{j}_v END AS gk{j}" for j in range(n_keys))
    key_err = " OR ".join(f"k{j}_e IS NOT NULL OR k{j}_t >= 4" for j in range(n_keys))
    part = ", ".join(keys)
    frame = f"OVER (PARTITION BY {part} ORDER BY ord ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    windows: list[str] = []
    item_errs: list[str] = []
    for j, (_column, ast) in enumerate(columns):
        if not (isinstance(ast, CallExpr) and ast.fn in AGGREGATE_FUNCTIONS):
            item_errs.append(f"(rn = 1 AND c{j}_e IS NOT NULL)")
            continue
        fn = ast.fn
        if fn == "count":
            continue
        rule = [f"a{j}_e IS NOT NULL"]
        in_range = f"(length(a{j}_v) <= 17 AND {_between(f'TRY_CAST(a{j}_v AS HUGEINT)')})"
        if fn == "sum":
            windows.append(f"sum(CASE WHEN a{j}_t = 2 AND {in_range} THEN TRY_CAST(a{j}_v AS HUGEINT) END) "
                           f"{frame} AS rs{j}")
            rule += [f"a{j}_t NOT IN (0, 2)", f"(a{j}_t = 2 AND NOT {in_range})", f"NOT {_between(f'coalesce(rs{j}, 0)')}"]
        elif fn in ("min", "max"):
            windows.append(f"first_value(CASE WHEN a{j}_t IN (1, 2) THEN a{j}_t END IGNORE NULLS) {frame} AS mt{j}")
            rule += [f"a{j}_t NOT IN (0, 1, 2)", f"(a{j}_t = 2 AND NOT {in_range})",
                     f"(a{j}_t IN (1, 2) AND a{j}_t <> mt{j})"]
        item_errs.append("(" + " OR ".join(rule) + ")")
    valid = " AND ".join(f"gk{j} IS NOT NULL" for j in range(n_keys))
    base = (f"WITH x AS (SELECT *, {key_cols}, ({key_err}) AS key_err FROM {{items}}), "
            f"w AS (SELECT *, (NOT key_err AND {valid}) AS valid FROM x)")
    win_cols = ", ".join([f"row_number() OVER (PARTITION BY {part} ORDER BY ord) AS rn", *windows])
    errors_sql = (f"{base}, v AS (SELECT *, {win_cols} FROM w WHERE valid) "
                  f"SELECT ord, ({' OR '.join(item_errs) or 'false'}) AS err FROM v "
                  f"UNION ALL SELECT ord, key_err AS err FROM w WHERE NOT valid")
    outs = ["min(ord) AS g_ord", "count(*) AS g_items"]
    for j, (_column, ast) in enumerate(columns):
        fn = ast.fn if isinstance(ast, CallExpr) and ast.fn in AGGREGATE_FUNCTIONS else None
        if fn is None:
            outs += [f"arg_min(c{j}_t, ord) AS c{j}_t", f"arg_min(c{j}_v, ord) AS c{j}_v"]
        elif fn == "first":
            outs += [f"arg_min(a{j}_t, ord) AS c{j}_t", f"arg_min(a{j}_v, ord) AS c{j}_v"]
        elif fn == "count":
            outs += [f"2 AS c{j}_t", f"CAST(count(*) AS VARCHAR) AS c{j}_v"]
        elif fn == "sum":
            outs += [f"CASE WHEN count(*) FILTER (WHERE a{j}_t = 2) > 0 THEN 2 ELSE 0 END AS c{j}_t",
                     f"coalesce(CAST(sum(TRY_CAST(a{j}_v AS HUGEINT)) FILTER (WHERE a{j}_t = 2) AS VARCHAR), '') "
                     f"AS c{j}_v"]
        else:
            typ = f"max(a{j}_t) FILTER (WHERE a{j}_t IN (1, 2))"
            outs += [f"coalesce({typ}, 0) AS c{j}_t",
                     f"CASE WHEN {typ} = 2 THEN CAST({fn}(TRY_CAST(a{j}_v AS HUGEINT)) FILTER (WHERE a{j}_t = 2) "
                     f"AS VARCHAR) WHEN {typ} = 1 THEN {fn}(a{j}_v) FILTER (WHERE a{j}_t = 1) ELSE '' END AS c{j}_v"]
    rows_sql = f"{base} SELECT {', '.join(outs)} FROM w WHERE valid GROUP BY {part} ORDER BY g_ord"
    return errors_sql, rows_sql


# ───────────────────────────── explain ─────────────────────────────


def _target_type(entity: str, column: str) -> str:
    info = scn.SCN_ROW_MODELS[entity].model_fields[column]
    ann = info.annotation
    text = _type_text(ann)
    meta = [m for m in info.metadata if hasattr(m, "ge") and getattr(m, "ge", None) is not None]
    if meta and text == "integer":
        text = f"integer >= {meta[0].ge}"
    return text


def _type_text(ann: Any) -> str:
    origin = get_origin(ann)
    args = [a for a in get_args(ann) if a is not type(None)]
    if origin is Literal:
        return "one of " + "|".join(str(a) for a in get_args(ann))
    if origin is not None and str(origin).endswith("Annotated"):
        return _type_text(args[0])
    if args and origin is not list:
        if all(a in (int, float) for a in args):
            return "number"
        return _type_text(args[0])
    if origin is list:
        return "list of text"
    if ann is int:
        return "integer"
    if ann is float:
        return "number"
    if ann is bool:
        return "boolean"
    name = getattr(ann, "__name__", str(ann))
    return {"date": "date", "str": "text"}.get(name, name)


def explain(compiled: CompiledMapping) -> MappingPlan:
    """The :class:`~keelson_contracts.mapping.MappingPlan` of a compiled mapping."""
    mapping = compiled.mapping
    lookups = []
    for lk in compiled.lookups:
        table = lk.table
        lookups.append({
            "name": lk.name, "kind": lk.kind, "source_key": lk.source.key if lk.source else None,
            "entries": len(table.values) if isinstance(table, StaticLookup) else None,
            "key": None if isinstance(table, StaticLookup) else table.key,
            "value": table.value if isinstance(table, SourceLookup) else None,
            "paths": [path_text(p) for p in lk.record_paths],
        })
    entities = []
    for ce in compiled.entities:
        required = set(required_scn_columns(ce.em.entity))
        cols = []
        for column, ast in ce.columns:
            agg = ast.fn if isinstance(ast, CallExpr) and ast.fn in AGGREGATE_FUNCTIONS else None
            cols.append({"column": column, "expression": ce.em.fields[column], "aggregate": agg,
                         "target_type": _target_type(ce.em.entity, column), "required": column in required,
                         "sql": ce.column_sql.get(column, "")[:100_000]})
        sql = ce.sql
        if ce.grouped and ce.group_rows_sql:
            sql += f";\n-- one row per group over the per-item rows (ev_{ce.index}):\n"
            sql += ce.group_rows_sql.replace("{items}", f"ev_{ce.index}")
        entities.append({
            "where": ce.where, "entity": ce.em.entity, "source_key": ce.source.key, "dedupe": ce.em.dedupe,
            "group_by": list(ce.em.group_by) if ce.em.group_by else None,
            "joins": [{"table": t, "kind": compiled.lookup(t).kind, "calls": n} for t, n in sorted(ce.joins.items())],
            "columns": cols, "sql": sql[:1_000_000],
        })
    mapped = set(mapping.mapped_entities)
    checks = [{"code": "duplicate_source_id", "label": f"{e}: unique source_id", "entity": e, "severity": "error"}
              for e in mapping.mapped_entities]
    for fk in scn.SCN_FOREIGN_KEYS:
        if fk.entity in mapped and (fk.severity == "error" or fk.target_entity in mapped):
            checks.append({"code": "fk", "label": fk.label, "entity": fk.entity, "severity": fk.severity})
    warnings = []
    plain = Counter((ce.source.key, ce.em.entity) for ce in compiled.entities if not (ce.grouped or ce.em.dedupe))
    for ce in compiled.entities:
        if not (ce.grouped or ce.em.dedupe) and plain[(ce.source.key, ce.em.entity)] > 1:
            warnings.append(f"{ce.where} reads the same source as another {ce.em.entity} mapping: their source_ids "
                            "must differ (or mark derived rows dedupe / group_by)")
    for fk in scn.SCN_FOREIGN_KEYS:
        if fk.severity == "error" and fk.entity in mapped and fk.target_entity not in mapped:
            warnings.append(f"{fk.label}: {fk.target_entity} is not mapped, so every {fk.entity} row fails this check")
    return MappingPlan.model_validate({
        "mapping_hash": compiled.hash,
        "sources": [{"key": s.key, "source": s.source.model_dump(mode="json"),
                     "paths": [path_text(p) for p in s.paths], "used_by": list(s.used_by)}
                    for s in compiled.sources.values()],
        "lookups": lookups, "entities": entities, "checks": checks, "warnings": warnings[:50],
    })


def entity_paths(em: EntityMapping) -> list[PathExpr]:
    """Item paths an entity mapping reads (fields and group keys)."""
    out: list[PathExpr] = []
    for ast in [*em.parsed_fields().values(), *em.parsed_group_by()]:
        out += expression_paths(ast)
    return out


def record_refs(em: EntityMapping) -> list[tuple[str, PathExpr]]:
    """``(record table, path)`` read through ``field()`` by an entity mapping."""
    out: list[tuple[str, PathExpr]] = []
    for ast in [*em.parsed_fields().values(), *em.parsed_group_by()]:
        out += expression_record_paths(ast)
    return out
