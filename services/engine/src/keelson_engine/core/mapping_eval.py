"""Evaluator for tier-A connector mappings (``docs/contracts/110-m7-backoffice-generic.md`` §4).

``keelson_contracts.mapping`` **parses** the small mapping languages into a frozen AST; this
module **walks** that AST over plain JSON values. It is a hand-written interpreter:

* no ``eval`` / ``exec`` / ``compile``, no ``getattr``, no format strings over user input;
* values are only ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` (what
  a JSON decoder produces): a path indexes **dict keys** and **list positions** and nothing
  else — an object attribute is unreachable, so ``$.__class__`` (refused by the parser anyway)
  or ``$.items`` on a Python object would just be "missing" (``None``);
* every function is a fixed table entry (:data:`_FUNCTIONS`); the parser already pinned the arity.

This interpreter is the **normative semantics** of the expression language: the M8 DuckDB mapping
compiler (:mod:`keelson_engine.core.mapping_compiler`) must agree with it on every value, ``null``
and error (differential tests), and it produces every error message the compiler reports.

Three users:

* the **crawl** (:mod:`keelson_engine.core.scn_crawl`): :func:`evaluate` per ``fields`` column of
  each crawled item (``lookups`` / record tables resolved beforehand into :class:`Lookups`);
* **action rendering** (per-op commit + admin dry-run): :func:`render_template` fills
  ``{{payload.*}}`` / ``{{target_ref}}`` / ``{{expected_version}}`` / ``{{request_id}}`` /
  ``{{op}}`` placeholders and ``{"$json": …}`` wrappers (:class:`ActionContext`);
* the **read-back** (:func:`readback_items`, :func:`readback_match`).

Errors are :class:`MappingEvalError` (message only: never the value of a credential, values are
shortened to 60 characters).
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from keelson_contracts.common import MAX_SAFE_INTEGER, weeks_in_year
from keelson_contracts.mapping import (
    CallExpr,
    ConcatExpr,
    ConstExpr,
    Expr,
    PathExpr,
    parse_expression,
    parse_path,
)

__all__ = [
    "ActionContext",
    "Lookups",
    "MappingEvalError",
    "aggregate",
    "canonical_json",
    "evaluate",
    "evaluate_text",
    "get_path",
    "key_text",
    "readback_items",
    "readback_match",
    "render_template",
    "values_equal",
]

INT_PATTERN: Final = r"[+-]?[0-9]{1,16}"
ISO_DATE_PATTERN: Final = r"([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ][0-9:.]+(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?"
ISO_WEEK_TEXT_PATTERN: Final = r"([0-9]{4})-?W([0-9]{2})"
DECIMAL_PATTERN: Final = r"([+-]?)([0-9]{1,16})(?:\.([0-9]{1,16}))?"
ASCII_SPACE: Final = " \t\n\r\x0b\x0c"
"""What ``trim()`` strips (ASCII whitespace only)."""
_INT_RE: Final = re.compile(INT_PATTERN)
_ISO_DATE_RE: Final = re.compile(ISO_DATE_PATTERN)
_ISO_WEEK_RE: Final = re.compile(ISO_WEEK_TEXT_PATTERN)
_DECIMAL_RE: Final = re.compile(DECIMAL_PATTERN)
_TOKEN_RE: Final = re.compile(r"YYYY|MM|DD")
_UPPER: Final = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
MAX_TEXT: Final = 100_000
"""Upper bound of a concatenated text value (a mapping can only grow it a little per node)."""
_MAX_TEXT: Final = MAX_TEXT


class MappingEvalError(ValueError):
    """An expression / template could not be evaluated for this input."""


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ───────────────────────────────── paths ────────────────────────────────────


def _step(node: Any, segment: str | int) -> Any:
    """One path step: dict key (``str``) or list position (``int``); anything else → ``None``.
    ``type(...) is`` checks: only real JSON containers are indexed (a ``dict`` subclass with a
    custom ``__getitem__`` / a ``str`` index on a list never reaches Python's item protocol)."""
    if isinstance(segment, int) and not isinstance(segment, bool):
        if type(node) is list and 0 <= segment < len(node):
            return node[segment]
        return None
    if type(node) is dict:
        return node.get(segment)
    return None


def get_path(value: Any, path: PathExpr | str) -> Any:
    """Read a JSONPath-lite path (``$``, ``$.a.b[0]``, ``$['x y']``) from a JSON value;
    missing keys / out-of-range indexes / non-containers yield ``None``."""
    expr = parse_path(path) if isinstance(path, str) else path
    node = value
    for seg in expr.segments:
        node = _step(node, seg)
        if node is None:
            return None
    return node


# ───────────────────────────────── values ───────────────────────────────────


def _text(value: Any, what: str) -> str | None:
    """A ``+`` operand / ``str()`` argument as text (``None`` stays ``None``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise MappingEvalError(f"{what}: cannot turn {type(value).__name__} {_short(value)} into text")


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise MappingEvalError(f"int(): a boolean is not an integer ({_short(value)})")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise MappingEvalError("int(): integer out of range")
        return value
    if isinstance(value, str) and _INT_RE.fullmatch(value):
        out = int(value)
        if abs(out) > MAX_SAFE_INTEGER:
            raise MappingEvalError("int(): integer out of range")
        return out
    raise MappingEvalError(f"int(): not an integer: {_short(value)}")


def _iso_day(value: str, fn: str) -> dt.date:
    m = _ISO_DATE_RE.fullmatch(value.strip())
    if m is None:
        raise MappingEvalError(f"{fn}: not an ISO date: {_short(value)}")
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d)
    except ValueError:
        raise MappingEvalError(f"{fn}: not a calendar date: {_short(value)}") from None


def _to_date(value: Any, fmt: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MappingEvalError(f"date(): not a date string: {_short(value)}")
    if fmt is None:
        return _iso_day(value, "date()").isoformat()
    else:
        sep = fmt[4] if fmt.startswith("YYYY") else fmt[2]
        tokens = _TOKEN_RE.findall(fmt)
        parts = value.strip().split(sep)
        widths = {"YYYY": 4, "MM": 2, "DD": 2}
        if len(parts) != 3 or any(len(p) != widths[t] or not p.isascii() or not p.isdigit()
                                  for p, t in zip(parts, tokens, strict=True)):
            raise MappingEvalError(f"date(): {_short(value)} does not match {fmt!r}")
        by = dict(zip(tokens, (int(p) for p in parts), strict=True))
        y, mo, d = by["YYYY"], by["MM"], by["DD"]
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        raise MappingEvalError(f"date(): not a calendar date: {_short(value)}") from None


def _to_cents(value: Any) -> int | None:
    """An exact decimal amount → integer minor units (digit arithmetic, never binary floats: a JSON
    number is read through its shortest ``repr``, so ``12.99`` → ``1299`` and ``0.1 + 0.2`` → error)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise MappingEvalError(f"cents(): a boolean is not an amount ({_short(value)})")
    if isinstance(value, int):
        out = value * 100
    elif isinstance(value, (str, float)):
        m = _DECIMAL_RE.fullmatch(value if isinstance(value, str) else repr(value))
        if m is None:
            raise MappingEvalError(f"cents(): not a decimal amount: {_short(value)}")
        sign, whole, frac = m.group(1), m.group(2), m.group(3) or ""
        if frac[2:].strip("0"):
            raise MappingEvalError(f"cents(): more than 2 decimals: {_short(value)}")
        out = int(whole) * 100 + int((frac + "00")[:2])
        if sign == "-":
            out = -out
    else:
        raise MappingEvalError(f"cents(): not an amount: {_short(value)}")
    if abs(out) > MAX_SAFE_INTEGER:
        raise MappingEvalError("cents(): amount out of range")
    return out


def _to_iso_week(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MappingEvalError(f"iso_week(): not a week or date string: {_short(value)}")
    m = _ISO_WEEK_RE.fullmatch(value.strip())
    if m is not None:
        year, week = int(m.group(1)), int(m.group(2))
        if year < 1 or not 1 <= week <= weeks_in_year(year):
            raise MappingEvalError(f"iso_week(): not an ISO week: {_short(value)}")
        return f"{year:04d}-W{week:02d}"
    iso = _iso_day(value, "iso_week()").isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _text_only(value: Any, fn: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MappingEvalError(f"{fn}: not a string: {_short(value)}")
    return value


def key_text(value: Any, what: str) -> str | None:
    """A lookup / record / group key as text (the ``+`` operand rule; ``None`` stays ``None``;
    floats and containers raise :class:`MappingEvalError`)."""
    return _text(value, what)


def aggregate(fn: str, current: Any, kind: type | None, value: Any) -> tuple[Any, type | None]:
    """One item of a ``sum`` / ``min`` / ``max`` aggregate (M8 ``group_by``): ``(current, kind)`` →
    the new state. ``null`` values are ignored; ``sum`` takes integers within ±(2^53-1) and keeps
    its running total in that range; ``min`` / ``max`` take integers (same range) or text, never
    both in one group."""
    if value is None:
        return current, kind
    if fn == "sum":
        if isinstance(value, bool) or not isinstance(value, int):
            raise MappingEvalError(f"sum(): not an integer: {_short(value)}")
        if abs(value) > MAX_SAFE_INTEGER:
            raise MappingEvalError("sum(): integer out of range")
        total = value if current is None else current + value
        if abs(total) > MAX_SAFE_INTEGER:
            raise MappingEvalError("sum(): total out of range")
        return total, int
    if fn not in ("min", "max"):
        raise MappingEvalError(f"unknown aggregate {fn!r}")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MappingEvalError(f"{fn}(): only integers or text: {_short(value)}")
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        raise MappingEvalError(f"{fn}(): integer out of range")
    k: type = int if isinstance(value, int) else str
    if kind is not None and kind is not k:
        raise MappingEvalError(f"{fn}(): mixes integers and text")
    if current is None or (value < current if fn == "min" else value > current):
        current = value
    return current, k


# ───────────────────────────────── lookups ──────────────────────────────────


@dataclass(slots=True)
class Lookups:
    """Resolved lookup tables: name → {key text → scalar}; record tables (M8, ``field()``):
    name → {key text → source item}."""

    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, table: str, key: Any, default: Any = None) -> Any:
        values = self.tables.get(table)
        if values is None:
            raise MappingEvalError(f"lookup(): unknown table {table!r}")
        text = _text(key, "lookup() key")
        if text is None:
            return default
        return values.get(text, default)

    def record(self, table: str, key: Any) -> Any:
        """The item of record table ``table`` under ``key`` (``None`` for a ``null`` key / a miss)."""
        items = self.records.get(table)
        if items is None:
            raise MappingEvalError(f"field(): unknown record table {table!r}")
        text = _text(key, "field() key")
        if text is None:
            return None
        return items.get(text)


_EMPTY_LOOKUPS: Final = Lookups()


# ───────────────────────────────── evaluator ────────────────────────────────


def evaluate(expr: Expr, item: Any, lookups: Lookups | None = None) -> Any:
    """Evaluate a parsed mapping expression over one source item (see the contract module
    docstring for the semantics)."""
    tables = lookups or _EMPTY_LOOKUPS
    if isinstance(expr, PathExpr):
        return get_path(item, expr)
    if isinstance(expr, ConstExpr):
        return expr.value
    if isinstance(expr, ConcatExpr):
        out: list[str] = []
        size = 0
        for part in expr.parts:
            text = _text(evaluate(part, item, tables), "'+'")
            if text is None:
                return None
            size += len(text)
            if size > _MAX_TEXT:
                raise MappingEvalError("'+': text too long")
            out.append(text)
        return "".join(out)
    if isinstance(expr, CallExpr):
        fn = _FUNCTIONS.get(expr.fn)
        if fn is None:  # the parser only produces known names; defence in depth
            raise MappingEvalError(f"unknown function {expr.fn!r}")
        return fn(expr.args, item, tables)
    raise MappingEvalError(f"not an expression node: {type(expr).__name__}")


def _fn_int(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    return _to_int(evaluate(args[0], item, tables))


def _fn_str(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    return _text(evaluate(args[0], item, tables), "str()")


def _fn_date(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    fmt = None
    if len(args) == 2:
        const = args[1]
        if not (isinstance(const, ConstExpr) and isinstance(const.value, str)):
            raise MappingEvalError("date(): the format must be a constant")
        fmt = const.value
    return _to_date(evaluate(args[0], item, tables), fmt)


def _fn_lookup(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    table = args[0]
    if not (isinstance(table, ConstExpr) and isinstance(table.value, str)):
        raise MappingEvalError("lookup(): the table must be a constant")
    default = None
    if len(args) == 3:
        d = args[2]
        if not isinstance(d, ConstExpr):
            raise MappingEvalError("lookup(): the default must be a constant")
        default = d.value
    return tables.get(table.value, evaluate(args[1], item, tables), default)


def _fn_coalesce(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    for a in args:
        value = evaluate(a, item, tables)
        if value is not None:
            return value
    return None


def _fn_if_present(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    probe = evaluate(args[0], item, tables)
    chosen = args[1] if probe is not None and probe != "" else args[2]
    return evaluate(chosen, item, tables)


@functools.lru_cache(maxsize=1024)
def _const_path(text: str) -> PathExpr:
    return parse_path(text)


def _fn_field(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    table, path = args[0], args[2]
    if not (isinstance(table, ConstExpr) and isinstance(table.value, str)
            and isinstance(path, ConstExpr) and isinstance(path.value, str)):
        raise MappingEvalError("field(): the table and the path must be constants")
    record = tables.record(table.value, evaluate(args[1], item, tables))
    return None if record is None else get_path(record, _const_path(path.value))


def _fn_cents(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    return _to_cents(evaluate(args[0], item, tables))


def _fn_iso_week(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    return _to_iso_week(evaluate(args[0], item, tables))


def _fn_trim(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    text = _text_only(evaluate(args[0], item, tables), "trim()")
    return None if text is None else text.strip(ASCII_SPACE)


def _fn_upper(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    text = _text_only(evaluate(args[0], item, tables), "upper()")
    return None if text is None else text.translate(_UPPER)


def _fn_lower(args: Sequence[Expr], item: Any, tables: Lookups) -> Any:
    text = _text_only(evaluate(args[0], item, tables), "lower()")
    return None if text is None else text.translate(_LOWER)


_FUNCTIONS: Final = {
    "int": _fn_int,
    "str": _fn_str,
    "date": _fn_date,
    "lookup": _fn_lookup,
    "coalesce": _fn_coalesce,
    "if_present": _fn_if_present,
    # M8
    "field": _fn_field,
    "cents": _fn_cents,
    "iso_week": _fn_iso_week,
    "trim": _fn_trim,
    "upper": _fn_upper,
    "lower": _fn_lower,
}
"""Aggregates (``first`` …) are not functions: the crawl's group pass applies them."""


def evaluate_text(expression: str, item: Any, lookups: Lookups | None = None) -> Any:
    """Parse + evaluate (tests and one-off use; the crawl parses each expression once)."""
    return evaluate(parse_expression(expression), item, lookups)


# ───────────────────────────────── templates ────────────────────────────────


def canonical_json(value: Any) -> str:
    """Compact, key-sorted JSON (the ``{"$json": …}`` wrapper and request fingerprints)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True, slots=True)
class ActionContext:
    """The values an action / read-back template may reference."""

    op: str
    payload: Mapping[str, Any]
    target_ref: str | None = None
    expected_version: int | None = None
    request_id: str | None = None

    def resolve(self, ref: str) -> Any:
        parts = ref.split(".")
        root = parts[0]
        if root == "payload":
            node: Any = dict(self.payload)
            for i, seg in enumerate(parts[1:], start=1):
                key: str | int = int(seg) if seg.isdigit() else seg
                if isinstance(key, int) and type(node) is list:
                    if key >= len(node):
                        raise MappingEvalError(f"{{{{{ref}}}}}: payload.{'.'.join(parts[1:i + 1])} is out of range")
                    node = node[key]
                elif isinstance(key, str) and type(node) is dict:
                    if key not in node:
                        raise MappingEvalError(f"{{{{{ref}}}}}: the payload has no {'.'.join(parts[1:i + 1])}")
                    node = node[key]
                else:
                    raise MappingEvalError(f"{{{{{ref}}}}}: cannot index {type(node).__name__} with {seg!r}")
            return node
        if len(parts) != 1:
            raise MappingEvalError(f"{{{{{ref}}}}}: {root} has no fields")
        if root == "target_ref":
            return self.target_ref
        if root == "expected_version":
            return self.expected_version
        if root == "request_id":
            return self.request_id
        if root == "op":
            return self.op
        raise MappingEvalError(f"unknown placeholder root {root!r}")


_PLACEHOLDER_RE: Final = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def _render_string(text: str, ctx: ActionContext | None) -> Any:
    matches = list(_PLACEHOLDER_RE.finditer(text))
    if not matches:
        return text
    if ctx is None:
        raise MappingEvalError("placeholders are not allowed here")
    whole = matches[0]
    if len(matches) == 1 and whole.start() == 0 and whole.end() == len(text):
        return ctx.resolve(whole.group(1).strip())
    out: list[str] = []
    last = 0
    for m in matches:
        ref = m.group(1).strip()
        value = ctx.resolve(ref)
        rendered = _text(value, f"{{{{{ref}}}}}") if not isinstance(value, (dict, list)) else None
        if rendered is None:
            if value is None:
                raise MappingEvalError(f"{{{{{ref}}}}} is empty but is part of a longer text")
            raise MappingEvalError(f"{{{{{ref}}}}} is a {type(value).__name__}; only text, numbers and booleans "
                                   "can be part of a longer text")
        out.append(text[last:m.start()])
        out.append(rendered)
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def render_template(template: Any, ctx: ActionContext | None = None, *, _depth: int = 0) -> Any:
    """Render an argument template (validated by ``keelson_contracts.mapping.template_refs``):
    placeholders from ``ctx`` (``None`` = none allowed, e.g. crawl source args) and
    ``{"$json": <template>}`` → canonical JSON text."""
    if _depth > 32:
        raise MappingEvalError("template nested too deeply")
    if template is None or isinstance(template, (bool, int)):
        return template
    if isinstance(template, float):
        raise MappingEvalError("floats are not allowed in templates")
    if isinstance(template, str):
        return _render_string(template, ctx)
    if isinstance(template, list):
        return [render_template(v, ctx, _depth=_depth + 1) for v in template]
    if isinstance(template, Mapping):
        if "$json" in template:
            if len(template) != 1:
                raise MappingEvalError('a {"$json": ...} object has exactly one key')
            return canonical_json(render_template(template["$json"], ctx, _depth=_depth + 1))
        return {str(k): render_template(v, ctx, _depth=_depth + 1) for k, v in template.items()}
    raise MappingEvalError(f"not a JSON value: {type(template).__name__}")


# ───────────────────────────────── read-back ────────────────────────────────


def readback_items(result: Any, items_path: str | None) -> list[Any]:
    """The candidate items of a read-back result: ``None`` path = the result itself; a path to a
    list = its items; to anything else = that single value (``[]`` when missing)."""
    selected = result if items_path is None else get_path(result, items_path)
    if selected is None:
        return []
    if type(selected) is list:
        return list(selected)
    return [selected]


def values_equal(actual: Any, expected: Any) -> bool:
    """Read-back equality: JSON equality, plus integer/text equivalence for scalars
    (``12`` equals ``"12"``; booleans only equal booleans)."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if actual == expected:
        return True
    scalars = (str, int)
    if isinstance(actual, scalars) and isinstance(expected, scalars):
        return str(actual) == str(expected)
    return False


def readback_match(items: Sequence[Any], match: Mapping[str, Any], ctx: ActionContext) -> bool:
    """Whether some item has every ``match`` path equal to the rendered expected value."""
    expected = {path: render_template(value, ctx) for path, value in match.items()}
    for item in items:
        if all(values_equal(get_path(item, path), want) for path, want in expected.items()):
            return True
    return False
