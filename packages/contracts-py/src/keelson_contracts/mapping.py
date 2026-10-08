"""Tier-A connector mappings (``110-m7-backoffice-generic.md`` §4): the read mapping (crawl a
plain MCP server into ``scn/1.0``), the action mapping (canonical op → vendor tool call) and
the two small languages they use.

This module **parses and validates only**. Nothing here evaluates an expression or renders a
template: the engine's crawler / commit service walk the AST returned by
:func:`parse_expression` / the refs returned by :func:`template_refs`. There is no ``eval``,
no attribute access and no Python object in reach: values are plain JSON (dict / list / str /
int / bool / null) and a path can only index dict keys and list positions.

Expression language (``fields: {scn_column: expression}``)
-----------------------------------------------------------
::

    expr     := primary ( "+" primary )*                  -- "+" = string concatenation
    primary  := path | string | integer | "null" | "true" | "false" | call | "(" expr ")"
    path     := "$" segment*                              -- JSONPath-lite over the source item
    segment  := "." ident | "[" index "]" | "[" string "]"
    call     := fname "(" [ expr ( "," expr )* ] ")"
    fname    := "int" | "str" | "date" | "lookup" | "coalesce" | "if_present"
    ident    := [A-Za-z_][A-Za-z0-9_]*  (never starting with "__")
    index    := [0-9]+                                    -- list position (no negatives)
    integer  := "-"? [0-9]+   (no leading zeros, |n| <= 2^53-1; floats do not exist)
    string   := '...' | "..."  (escapes \\ \' \" only; no control characters; <= 200 chars)

Whitespace (space, tab, CR, LF) may separate tokens but never splits a path. Limits: 500
characters, nesting depth 16, 64 nodes, 16 path segments. Semantics (engine):

* ``$.a.b[0]`` reads dict keys / list positions of the source item; a missing key, an index out
  of range or indexing a non-container yields ``null`` (never an error, never an attribute);
* ``a + b`` concatenates text: strings as-is, integers in decimal, booleans ``true``/``false``;
  a ``null`` operand makes the result ``null``; dicts/lists are an error;
* ``int(x)``: integer as-is, a string of an optional sign and digits → integer, ``null`` → ``null``,
  anything else (floats, ``"1.5"``, booleans) is an error;
* ``str(x)``: like one ``+`` operand (``null`` stays ``null``);
* ``date(x)`` / ``date(x, 'DD/MM/YYYY')``: an ISO date (or the date part of an ISO date-time) →
  ``YYYY-MM-DD``; the optional format is a constant made of ``YYYY``, ``MM``, ``DD`` once each,
  joined by one of ``-`` ``/`` ``.`` or a space;
* ``lookup('table', key[, default])``: the table is a string constant naming one of the mapping's
  ``lookups``; the key is compared as text (``str(key)``); a miss yields ``default`` (a constant)
  or ``null``;
* ``coalesce(a, b, …)`` (2..8 arguments): the first non-null value;
* ``if_present(x, then, else)``: ``then`` when ``x`` is neither ``null`` nor ``""``, else ``else``.

Argument templates (``args`` of read sources, actions and read-backs)
---------------------------------------------------------------------
A template is a JSON value (no floats, depth <= 8, <= 500 nodes, strings <= 2000 chars).
Inside strings, ``{{ ref }}`` placeholders name values of the action being rendered::

    ref := root ( "." ( ident | index ) )*      -- e.g. payload.lines, payload.lines.0.sku

Roots: ``payload``, ``target_ref``, ``expected_version``, ``request_id``, ``op`` (actions and
read-backs; :data:`ACTION_TEMPLATE_ROOTS`). Read-source arguments take **no** placeholders (the
crawler adds the page / cursor / page size under the configured parameter names). A string that
is exactly one placeholder takes the referenced value with its JSON type (``"{{payload.qty}}"`` →
``12``); otherwise each placeholder is replaced by its text. ``{"$json": <template>}`` renders the
inner template and passes it as **canonical JSON text** (vendor tools that take JSON-encoded
string arguments, e.g. Merxa ``/mcp-lite`` ``fields_json`` / ``lines_json``). Object keys are
literal (no placeholders); other ``$``-keys are reserved.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, TypeAlias, get_args

from pydantic import AfterValidator, BaseModel, Field, StrictBool, StrictInt, StrictStr, model_validator

from .changeset import (
    OP_NAME_PATTERN,
    OP_PAYLOAD_MODELS,
    AllocationSetQtyItem,
    CapacityBookingCreateItem,
    PoCreateItem,
    PoLineCancelItem,
    PoLineChangeQtyItem,
    PoLineRescheduleItem,
    PoLineSplitItem,
    ProductionOrderResequenceItem,
    TransferOrderCreateItem,
)
from .common import MAX_SAFE_INTEGER, ContractModel, NonNegInt, PositiveInt
from .connectors import ContentTrust, Idempotency, ToolAccess, ToolClassification
from .scn import SCN_COLUMNS, SCN_ENTITIES, SCN_ROW_MODELS

__all__ = [
    "ACTION_TEMPLATE_ROOTS",
    "ARG_NAME_PATTERN",
    "ActionDryRunRequest",
    "ActionDryRunResult",
    "ActionMapping",
    "ActionOpMapping",
    "CallExpr",
    "ConcatExpr",
    "ConstExpr",
    "DryRunReadback",
    "EXPRESSION_FUNCTIONS",
    "EntityMapping",
    "Expr",
    "Expression",
    "ExpressionError",
    "JsonPathLite",
    "LOOKUP_NAME_PATTERN",
    "LookupTable",
    "MAX_EXPRESSION_CHARS",
    "MAX_EXPRESSION_DEPTH",
    "MAX_EXPRESSION_NODES",
    "MAX_PATH_SEGMENTS",
    "PathExpr",
    "ReadMapping",
    "ReadSource",
    "Readback",
    "RenderedCall",
    "ScnEntityName",
    "SourceLookup",
    "StaticLookup",
    "TOOL_NAME_PATTERN",
    "TemplateError",
    "action_mapping_violations",
    "expression_lookups",
    "expression_paths",
    "parse_expression",
    "parse_path",
    "read_mapping_violations",
    "required_scn_columns",
    "template_refs",
    "unmapped_ops",
]

# ─────────────────────────────── limits & names ─────────────────────────────

MAX_EXPRESSION_CHARS: Final = 500
MAX_EXPRESSION_DEPTH: Final = 16
MAX_EXPRESSION_NODES: Final = 64
MAX_PATH_SEGMENTS: Final = 16
MAX_PATH_CHARS: Final = 200
MAX_STRING_LITERAL_CHARS: Final = 200
MAX_TEMPLATE_DEPTH: Final = 8
MAX_TEMPLATE_NODES: Final = 500
MAX_TEMPLATE_STRING_CHARS: Final = 2000
MAX_LOOKUP_ENTRIES: Final = 10_000
MAX_ENTITY_MAPPINGS: Final = 64
MAX_LOOKUP_TABLES: Final = 32
MAX_ACTION_OPS: Final = 64
MAX_PAGES_LIMIT: Final = 10_000
MAX_PAGE_SIZE: Final = 1000

EXPRESSION_FUNCTIONS: Final[Mapping[str, tuple[int, int]]] = {
    "int": (1, 1),
    "str": (1, 1),
    "date": (1, 2),
    "lookup": (2, 3),
    "coalesce": (2, 8),
    "if_present": (3, 3),
}
"""Function name → (min, max) arity. Nothing else is callable."""

ACTION_TEMPLATE_ROOTS: Final = frozenset({"payload", "target_ref", "expected_version", "request_id", "op"})
"""Placeholder roots of action and read-back templates."""

TOOL_NAME_PATTERN: Final = r"^[A-Za-z0-9_.-]{1,128}$"
"""MCP tool names (``tools/list``: 1-128 of ``A-Z a-z 0-9 _ - .``)."""
ARG_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
LOOKUP_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{0,39}$"
DATE_FORMAT_PATTERN: Final = r"^(YYYY|MM|DD)([-/. ])(YYYY|MM|DD)\2(YYYY|MM|DD)$"

_IDENT_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOOKUP_NAME_RE: Final = re.compile(LOOKUP_NAME_PATTERN)
_DATE_FORMAT_RE: Final = re.compile(DATE_FORMAT_PATTERN)
_REF_SEGMENT_RE: Final = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]{0,8})$")
_WHITESPACE: Final = frozenset(" \t\r\n")

ToolName = Annotated[str, Field(pattern=TOOL_NAME_PATTERN)]
ArgName = Annotated[str, Field(pattern=ARG_NAME_PATTERN)]
LookupName = Annotated[str, Field(pattern=LOOKUP_NAME_PATTERN)]
OpName = Annotated[str, Field(max_length=64, pattern=OP_NAME_PATTERN)]

ScnEntityName = Literal[
    "location",
    "item",
    "bom_line",
    "lane",
    "lane_rate",
    "sailing",
    "po_line",
    "production_order",
    "inventory_position",
    "demand_forecast",
    "size_curve",
    "allocation",
    "transfer_order_line",
    "online_order_line",
    "promotion",
]
"""``scn/1.0`` entity names (equal to :data:`~keelson_contracts.scn.SCN_ENTITIES`)."""

assert get_args(ScnEntityName) == SCN_ENTITIES, "ScnEntityName must list SCN_ENTITIES in order"


def required_scn_columns(entity: str) -> tuple[str, ...]:
    """Columns of ``entity`` a read mapping must fill (no default in the row model)."""
    model = SCN_ROW_MODELS[entity]
    return tuple(name for name, f in model.model_fields.items() if f.is_required())


# ─────────────────────────────────── AST ────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PathExpr:
    """``$`` + segments: dict keys (``str``) and list positions (``int``)."""

    segments: tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class ConstExpr:
    value: str | int | bool | None


@dataclass(frozen=True, slots=True)
class CallExpr:
    fn: str
    """One of :data:`EXPRESSION_FUNCTIONS`."""
    args: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class ConcatExpr:
    parts: tuple[Expr, ...]
    """Two or more operands of ``+``."""


Expr: TypeAlias = PathExpr | ConstExpr | CallExpr | ConcatExpr


class ExpressionError(ValueError):
    """A malformed or disallowed mapping expression / path (``pos`` = character offset)."""

    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"{message} (at {pos})")
        self.pos = pos


# ─────────────────────────────────── parser ─────────────────────────────────


class _Parser:
    def __init__(self, text: str, *, limit: int) -> None:
        if not isinstance(text, str):
            raise ExpressionError("expression must be a string", 0)
        if len(text) > limit:
            raise ExpressionError(f"longer than {limit} characters", limit)
        for i, ch in enumerate(text):
            if (ord(ch) < 0x20 and ch not in _WHITESPACE) or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF:
                raise ExpressionError("control or surrogate character", i)
        self.text = text
        self.pos = 0
        self.depth = 0
        self.nodes = 0

    # helpers
    def _error(self, message: str) -> ExpressionError:
        return ExpressionError(message, self.pos)

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in _WHITESPACE:
            self.pos += 1

    def _expect(self, ch: str) -> None:
        self._skip_ws()
        if self._peek() != ch:
            found = self._peek() or "end of input"
            raise self._error(f"expected {ch!r}, found {found!r}")
        self.pos += 1

    def _node(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_EXPRESSION_NODES:
            raise self._error(f"more than {MAX_EXPRESSION_NODES} nodes")

    # grammar
    def parse(self) -> Expr:
        expr = self._expr()
        self._skip_ws()
        if self.pos != len(self.text):
            raise self._error(f"unexpected {self._peek()!r}")
        return expr

    def _expr(self) -> Expr:
        self.depth += 1
        if self.depth > MAX_EXPRESSION_DEPTH:
            raise self._error(f"nested deeper than {MAX_EXPRESSION_DEPTH}")
        parts = [self._primary()]
        while True:
            self._skip_ws()
            if self._peek() != "+":
                break
            self.pos += 1
            parts.append(self._primary())
        self.depth -= 1
        if len(parts) == 1:
            return parts[0]
        self._node()
        return ConcatExpr(tuple(parts))

    def _primary(self) -> Expr:
        self._skip_ws()
        ch = self._peek()
        if ch == "$":
            return self.path()
        if ch in ("'", '"'):
            self._node()
            return ConstExpr(self._string())
        if ch == "-" or ch.isdigit():
            self._node()
            return ConstExpr(self._integer(signed=True))
        if ch == "(":
            self.pos += 1
            inner = self._expr()
            self._expect(")")
            return inner
        if ch and (ch.isalpha() or ch == "_"):
            start = self.pos
            m = _IDENT_RE.match(self.text, self.pos)
            if m is None:
                raise self._error(f"unexpected {ch!r} (names are ASCII)")
            name = m.group(0)
            self.pos = m.end()
            if name in ("null", "true", "false"):
                self._node()
                return ConstExpr(None if name == "null" else name == "true")
            if name not in EXPRESSION_FUNCTIONS:
                raise ExpressionError(f"unknown function or name {name[:40]!r}", start)
            self._skip_ws()
            if self._peek() != "(":
                raise self._error(f"{name} must be called: {name}(…)")
            return self._call(name, start)
        if not ch:
            raise self._error("unexpected end of input")
        raise self._error(f"unexpected {ch!r}")

    def _call(self, name: str, start: int) -> Expr:
        self.pos += 1  # "("
        self._node()
        args: list[Expr] = []
        self._skip_ws()
        if self._peek() == ")":
            self.pos += 1
        else:
            while True:
                args.append(self._expr())
                self._skip_ws()
                if self._peek() == ",":
                    self.pos += 1
                    continue
                self._expect(")")
                break
        lo, hi = EXPRESSION_FUNCTIONS[name]
        if not lo <= len(args) <= hi:
            want = str(lo) if lo == hi else f"{lo}..{hi}"
            raise ExpressionError(f"{name}() takes {want} arguments, got {len(args)}", start)
        _check_call(name, args, start)
        return CallExpr(name, tuple(args))

    def path(self) -> PathExpr:
        self._node()
        start = self.pos
        if self._peek() != "$":
            raise self._error("a path starts with '$'")
        self.pos += 1
        segments: list[str | int] = []
        while True:
            ch = self._peek()
            if ch == ".":
                self.pos += 1
                m = _IDENT_RE.match(self.text, self.pos)
                if m is None:
                    raise self._error("expected a field name after '.'")
                segments.append(_segment_name(m.group(0), self.pos))
                self.pos = m.end()
            elif ch == "[":
                self.pos += 1
                if self._peek() in ("'", '"'):
                    segments.append(_segment_name(self._string(), self.pos))
                elif self._peek().isdigit():
                    segments.append(self._integer(signed=False))
                else:
                    raise self._error("expected a list index or a quoted key inside [ ]")
                if self._peek() != "]":
                    raise self._error("expected ']'")
                self.pos += 1
            else:
                break
            if len(segments) > MAX_PATH_SEGMENTS:
                raise ExpressionError(f"a path has at most {MAX_PATH_SEGMENTS} segments", start)
        return PathExpr(tuple(segments))

    def _string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise self._error("unterminated string")
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                break
            if ch in "\r\n\t":
                raise self._error("control character inside a string")
            if ch == "\\":
                nxt = self.text[self.pos + 1] if self.pos + 1 < len(self.text) else ""
                if nxt not in ("\\", "'", '"'):
                    raise self._error("only \\\\, \\' and \\\" escapes are allowed")
                out.append(nxt)
                self.pos += 2
            else:
                out.append(ch)
                self.pos += 1
            if len(out) > MAX_STRING_LITERAL_CHARS:
                raise self._error(f"string literal longer than {MAX_STRING_LITERAL_CHARS} characters")
        return "".join(out)

    def _integer(self, *, signed: bool) -> int:
        start = self.pos
        if signed and self._peek() == "-":
            self.pos += 1
        digits_start = self.pos
        while self._peek().isdigit() and self._peek().isascii():
            self.pos += 1
        digits = self.text[digits_start:self.pos]
        if not digits:
            raise ExpressionError("expected digits", start)
        if len(digits) > 1 and digits[0] == "0":
            raise ExpressionError("integers have no leading zeros", start)
        if len(digits) > 16:
            raise ExpressionError("integer out of range", start)
        nxt = self._peek()
        if nxt == "." or (nxt and (nxt.isalpha() or nxt == "_")):
            raise ExpressionError("only integers are allowed (no floats, no exponents)", start)
        value = int(self.text[start:self.pos])
        if abs(value) > MAX_SAFE_INTEGER:
            raise ExpressionError("integer out of range", start)
        return value


def _segment_name(name: str, pos: int) -> str:
    if name.startswith("__"):
        raise ExpressionError("path segments may not start with '__'", pos)
    if not name or len(name) > 128:
        raise ExpressionError("path segments are 1..128 characters", pos)
    return name


def _check_call(name: str, args: list[Expr], pos: int) -> None:
    if name == "lookup":
        table = args[0]
        table_name = table.value if isinstance(table, ConstExpr) else None
        if not (isinstance(table_name, str) and _LOOKUP_NAME_RE.fullmatch(table_name)):
            raise ExpressionError(f"lookup() needs a table name constant matching {LOOKUP_NAME_PATTERN}", pos)
        if len(args) == 3 and not isinstance(args[2], ConstExpr):
            raise ExpressionError("lookup()'s default must be a constant", pos)
    elif name == "date" and len(args) == 2:
        fmt = args[1]
        if not (isinstance(fmt, ConstExpr) and isinstance(fmt.value, str) and _date_format_ok(fmt.value)):
            raise ExpressionError("date()'s format must be a constant like 'DD/MM/YYYY'", pos)


def _date_format_ok(fmt: str) -> bool:
    m = _DATE_FORMAT_RE.fullmatch(fmt)
    return m is not None and sorted((m.group(1), m.group(3), m.group(4))) == ["DD", "MM", "YYYY"]


def parse_expression(text: str) -> Expr:
    """Parse a mapping expression into its AST (raises :class:`ExpressionError`)."""
    return _Parser(text, limit=MAX_EXPRESSION_CHARS).parse()


def parse_path(text: str) -> PathExpr:
    """Parse a JSONPath-lite path (``$``, ``$.items``, ``$.data[0]['next cursor']``)."""
    parser = _Parser(text, limit=MAX_PATH_CHARS)
    if text != text.strip():
        raise ExpressionError("a path has no surrounding whitespace", 0)
    path = parser.path()
    if parser.pos != len(text):
        raise ExpressionError(f"unexpected {text[parser.pos]!r} in path", parser.pos)
    return path


def _walk(expr: Expr) -> Iterator[Expr]:
    yield expr
    if isinstance(expr, CallExpr):
        for a in expr.args:
            yield from _walk(a)
    elif isinstance(expr, ConcatExpr):
        for p in expr.parts:
            yield from _walk(p)


def expression_lookups(expr: Expr) -> set[str]:
    """Lookup tables an expression reads."""
    return {
        str(e.args[0].value)
        for e in _walk(expr)
        if isinstance(e, CallExpr) and e.fn == "lookup" and isinstance(e.args[0], ConstExpr)
    }


def expression_paths(expr: Expr) -> list[PathExpr]:
    """Every source path an expression reads (in order)."""
    return [e for e in _walk(expr) if isinstance(e, PathExpr)]


def _check_expression(text: str) -> str:
    parse_expression(text)
    return text


def _check_path(text: str) -> str:
    parse_path(text)
    return text


Expression = Annotated[
    str,
    Field(min_length=1, max_length=MAX_EXPRESSION_CHARS),
    AfterValidator(_check_expression),
]
"""A mapping expression (validated by :func:`parse_expression`)."""

JsonPathLite = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PATH_CHARS, pattern=r"^\$"),
    AfterValidator(_check_path),
]
"""A JSONPath-lite path (validated by :func:`parse_path`)."""


# ───────────────────────────────── templates ────────────────────────────────


class TemplateError(ValueError):
    """A malformed argument template (``path`` = where in the template)."""

    def __init__(self, message: str, path: str) -> None:
        super().__init__(f"{path or '/'}: {message}")
        self.path = path


def _placeholders(text: str, path: str) -> list[str]:
    """The inner refs of every ``{{ … }}`` in ``text`` (stray braces are errors)."""
    refs: list[str] = []
    i = 0
    while True:
        open_at = text.find("{{", i)
        close_at = text.find("}}", i)
        if open_at < 0:
            if close_at >= 0:
                raise TemplateError("'}}' without '{{'", path)
            return refs
        if 0 <= close_at < open_at:
            raise TemplateError("'}}' without '{{'", path)
        end = text.find("}}", open_at + 2)
        if end < 0:
            raise TemplateError("unterminated '{{'", path)
        inner = text[open_at + 2:end]
        if "{" in inner or "}" in inner:
            raise TemplateError("nested braces in a placeholder", path)
        refs.append(inner.strip())
        i = end + 2


def _check_ref(ref: str, roots: Collection[str], path: str) -> tuple[str, ...]:
    parts = ref.split(".")
    if not ref or not all(_REF_SEGMENT_RE.fullmatch(p) for p in parts):
        raise TemplateError(f"invalid placeholder {{{{{ref[:60]}}}}}", path)
    if any(p.startswith("__") for p in parts):
        raise TemplateError("placeholder segments may not start with '__'", path)
    if parts[0] not in roots:
        allowed = ", ".join(sorted(roots)) or "none here"
        raise TemplateError(f"unknown placeholder root {parts[0]!r} (allowed: {allowed})", path)
    if parts[0] != "payload" and len(parts) > 1:
        raise TemplateError(f"{parts[0]} has no fields", path)
    return tuple(parts)


def template_refs(template: Any, *, roots: Collection[str]) -> list[tuple[str, ...]]:
    """Validate an argument template and return its placeholder refs (``("payload", "qty")``…).

    Raises :class:`TemplateError` for floats, non-string keys, ``{{`` in keys, reserved ``$`` keys,
    a ``$json`` object with other keys, unknown roots, and the size / depth limits."""
    refs: list[tuple[str, ...]] = []
    nodes = 0

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_TEMPLATE_NODES:
            raise TemplateError(f"more than {MAX_TEMPLATE_NODES} nodes", path)
        if depth > MAX_TEMPLATE_DEPTH:
            raise TemplateError(f"nested deeper than {MAX_TEMPLATE_DEPTH}", path)
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, int):
            if abs(node) > MAX_SAFE_INTEGER:
                raise TemplateError("integer out of range", path)
            return
        if isinstance(node, float):
            raise TemplateError("floats are not allowed (money is cents, ratios are basis points)", path)
        if isinstance(node, str):
            if len(node) > MAX_TEMPLATE_STRING_CHARS:
                raise TemplateError(f"string longer than {MAX_TEMPLATE_STRING_CHARS} characters", path)
            if any(0xD800 <= ord(c) <= 0xDFFF for c in node):
                raise TemplateError("lone surrogate in string", path)
            refs.extend(_check_ref(r, roots, path) for r in _placeholders(node, path))
            return
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}/{i}", depth + 1)
            return
        if isinstance(node, Mapping):
            if "$json" in node and len(node) != 1:
                raise TemplateError("a {\"$json\": …} object has exactly one key", path)
            for key, value in node.items():
                if not isinstance(key, str):
                    raise TemplateError("object keys must be strings", path)
                if "{{" in key or "}}" in key:
                    raise TemplateError("object keys are literal (no placeholders)", path)
                if key.startswith("$") and key != "$json":
                    raise TemplateError(f"reserved key {key[:40]!r}", path)
                walk(value, f"{path}/{key.replace('~', '~0').replace('/', '~1')}", depth + 1)
            return
        raise TemplateError(f"not a JSON value: {type(node).__name__}", path)

    walk(template, "", 0)
    return refs


def _check_static_template(value: dict[str, Any]) -> dict[str, Any]:
    template_refs(value, roots=())
    return value


StaticArgs = Annotated[dict[str, Any], AfterValidator(_check_static_template)]
"""Read-source arguments: a template without placeholders (``$json`` allowed)."""


# ─────────────────────────────── read mapping ───────────────────────────────


class ReadSource(ContractModel):
    """One paged read-tool call sequence.

    Paging: with ``cursor_path`` null the crawler sends page numbers ``first_page, first_page+1, …``
    under ``page_param`` and stops at an empty or short page (fewer than ``page_size`` items);
    with ``cursor_path`` set it sends no cursor first, then the cursor read from the previous
    result under ``page_param``, and stops when the cursor is missing, ``null``, ``""`` or
    repeats. It never fetches more than ``max_pages`` (hitting the cap is reported as truncation).
    ``page_size`` is sent under ``page_size_param`` when that is set. Without ``page_param`` the
    tool is called once (``max_pages`` must be 1). ``items_path`` selects the result's list of
    items (``$`` = the result itself)."""

    tool: ToolName
    args: StaticArgs = Field(default_factory=dict)
    page_param: ArgName | None
    page_size_param: ArgName | None = None
    page_size: Annotated[StrictInt, Field(ge=1, le=MAX_PAGE_SIZE)]
    items_path: JsonPathLite
    cursor_path: JsonPathLite | None
    max_pages: Annotated[StrictInt, Field(ge=1, le=MAX_PAGES_LIMIT)]
    first_page: Annotated[StrictInt, Field(ge=0, le=1)] = 1

    @model_validator(mode="after")
    def _paging(self) -> ReadSource:
        names = [n for n in (self.page_param, self.page_size_param) if n is not None]
        if len(set(names)) != len(names):
            raise ValueError("page_param and page_size_param must differ")
        if clash := sorted(set(names) & set(self.args)):
            raise ValueError(f"args must not set the paging parameters {clash}")
        if self.cursor_path is not None and self.page_param is None:
            raise ValueError("cursor paging needs page_param (the argument that carries the cursor)")
        if self.page_param is None and self.max_pages != 1:
            raise ValueError("without page_param the tool is called once: max_pages must be 1")
        return self


class EntityMapping(ContractModel):
    """Rows of one ``scn/1.0`` entity from one source. Several mappings may fill the same entity
    (e.g. ``location`` from DCs and from stores). ``dedupe``: rows with the same ``source_id``
    collapse into the first one (derived rows such as store clusters)."""

    entity: ScnEntityName
    source: ReadSource
    fields: Annotated[dict[str, Expression], Field(min_length=1)]
    dedupe: StrictBool = False

    @model_validator(mode="after")
    def _columns(self) -> EntityMapping:
        known = SCN_COLUMNS[self.entity]
        if unknown := sorted(set(self.fields) - set(known)):
            raise ValueError(f"{self.entity} has no columns {unknown}")
        if missing := [c for c in required_scn_columns(self.entity) if c not in self.fields]:
            raise ValueError(f"{self.entity} mapping misses required columns {missing}")
        return self

    def parsed_fields(self) -> dict[str, Expr]:
        """Column → AST (contract column order)."""
        return {c: parse_expression(self.fields[c]) for c in SCN_COLUMNS[self.entity] if c in self.fields}


LookupScalar = StrictStr | StrictInt | StrictBool | None


class StaticLookup(ContractModel):
    """A fixed key → value table (keys compared as text)."""

    kind: Literal["static"]
    values: Annotated[dict[Annotated[str, Field(max_length=200)], LookupScalar], Field(max_length=MAX_LOOKUP_ENTRIES)]

    @model_validator(mode="after")
    def _safe_ints(self) -> StaticLookup:
        for v in self.values.values():
            if isinstance(v, int) and not isinstance(v, bool) and abs(v) > MAX_SAFE_INTEGER:
                raise ValueError("lookup integers must be within +/-(2^53-1)")
            if isinstance(v, str) and len(v) > 2000:
                raise ValueError("lookup values are at most 2000 characters")
        return self


class SourceLookup(ContractModel):
    """A table crawled from a read tool before the entities: ``key`` → ``value`` per item. Its
    expressions may not call ``lookup()`` (no chains, no cycles)."""

    kind: Literal["source"]
    source: ReadSource
    key: Expression
    value: Expression

    @model_validator(mode="after")
    def _no_chain(self) -> SourceLookup:
        if expression_lookups(parse_expression(self.key)) or expression_lookups(parse_expression(self.value)):
            raise ValueError("a source lookup's key/value may not call lookup()")
        return self


LookupTable = Annotated[StaticLookup | SourceLookup, Field(discriminator="kind")]


class ReadMapping(ContractModel):
    """``connector_mapping`` kind ``read`` (versioned by the engine): how the crawl sync turns a
    tier-A connector's read tools into ``scn/1.0`` rows. The resulting snapshot is
    ``consistency: crawled`` (plans built on it are advisory)."""

    kind: Literal["read"] = "read"
    entities: Annotated[list[EntityMapping], Field(min_length=1, max_length=MAX_ENTITY_MAPPINGS)]
    lookups: Annotated[dict[LookupName, LookupTable], Field(max_length=MAX_LOOKUP_TABLES)] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _lookups_exist(self) -> ReadMapping:
        for i, em in enumerate(self.entities):
            for column, ast in em.parsed_fields().items():
                if missing := sorted(expression_lookups(ast) - set(self.lookups)):
                    raise ValueError(f"entities[{i}] ({em.entity}).{column} uses unknown lookup tables {missing}")
        return self

    @property
    def mapped_entities(self) -> list[str]:
        """Entities the mapping fills (contract order)."""
        present = {e.entity for e in self.entities}
        return [e for e in SCN_ENTITIES if e in present]


def read_mapping_violations(
    mapping: ReadMapping,
    *,
    tools: Mapping[str, ToolClassification],
) -> list[str]:
    """Cross-checks against the connector's approved tool catalogue (``tools``: tool name →
    classification). The crawl calls every source tool up to ``max_pages`` times without an
    approval gate, so each one (entity sources and ``source`` lookups) must be an approved
    ``read`` tool; ``untrusted_text`` tools are refused too, because crawled rows become
    ``scn/1.0`` data that investigators read as trusted."""
    problems: list[str] = []
    sources: list[tuple[str, ReadSource]] = [
        (f"entities[{i}] ({em.entity})", em.source) for i, em in enumerate(mapping.entities)
    ]
    sources += [(f"lookups.{name}", t.source) for name, t in mapping.lookups.items() if isinstance(t, SourceLookup)]
    for where, src in sources:
        cls = tools.get(src.tool)
        if cls is None:
            problems.append(f"{where}: tool {src.tool!r} is not an approved tool of this connector")
            continue
        if cls.access is not ToolAccess.READ:
            problems.append(f"{where}: tool {src.tool!r} is a {cls.access.value} tool (a crawl only calls read tools)")
        if cls.content_trust is ContentTrust.UNTRUSTED_TEXT:
            problems.append(f"{where}: tool {src.tool!r} returns untrusted text (not crawlable into scn/1.0)")
    return problems


# ────────────────────────────── action mapping ──────────────────────────────

_ITEM_MODELS: Final[Mapping[str, type[BaseModel]]] = {
    "transfer_order.create": TransferOrderCreateItem,
    "allocation.set_qty": AllocationSetQtyItem,
    "po_line.reschedule": PoLineRescheduleItem,
    "po_line.change_qty": PoLineChangeQtyItem,
    "po_line.split": PoLineSplitItem,
    "capacity_booking.create": CapacityBookingCreateItem,
    "po.create": PoCreateItem,
    "po_line.cancel": PoLineCancelItem,
    "production_order.resequence": ProductionOrderResequenceItem,
}
assert set(_ITEM_MODELS) == set(OP_PAYLOAD_MODELS)


def _item_has(op: str, field: str) -> bool:
    """Whether a typed op's item carries ``target_ref`` / ``expected_version`` (unknown ops: yes)."""
    model = _ITEM_MODELS.get(op)
    if model is None:
        return True
    return model.model_fields[field].annotation is not type(None)


def _args_template(value: dict[str, Any]) -> dict[str, Any]:
    template_refs(value, roots=ACTION_TEMPLATE_ROOTS)
    return value


ActionArgs = Annotated[dict[str, Any], AfterValidator(_args_template)]


class Readback(ContractModel):
    """How to confirm an op took effect without repeating it: call ``tool`` with ``args`` (a
    read tool; same placeholders as the action), take the items at ``items_path`` (``null`` = the
    result is the single item; a path to an object = that single item; to a list = its items) and
    succeed when some item has every ``match`` path (JSONPath-lite relative to the item) equal to
    the rendered value."""

    tool: ToolName
    args: ActionArgs = Field(default_factory=dict)
    items_path: JsonPathLite | None = None
    match: Annotated[dict[str, Any], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def _match_values(self) -> Readback:
        for key in self.match:
            try:
                parse_path(key)
            except ExpressionError as exc:
                raise ValueError(f"match key {key[:60]!r} is not a JSONPath-lite path: {exc}") from exc
        self.match_refs()
        return self

    def match_refs(self) -> list[tuple[str, ...]]:
        """Placeholder refs of the ``match`` values (the keys are item paths, not templates)."""
        return template_refs(list(self.match.values()), roots=ACTION_TEMPLATE_ROOTS)


class ActionOpMapping(ContractModel):
    """One canonical op → one vendor tool call. ``idempotency`` is the admin-confirmed guarantee
    of that tool: ``enforced`` needs ``request_id_arg`` (the per-op idempotency key is sent
    there); ``best_effort`` / ``none`` need a ``readback`` (read back before any retry; ``none``
    ops are never auto-retried)."""

    op: OpName
    tool: ToolName
    args: ActionArgs
    idempotency: Idempotency
    request_id_arg: ArgName | None
    readback: Readback | None

    @model_validator(mode="after")
    def _check(self) -> ActionOpMapping:
        if self.request_id_arg is not None and self.request_id_arg in self.args:
            raise ValueError("args must not set request_id_arg (the engine fills it)")
        if self.idempotency is Idempotency.ENFORCED and self.request_id_arg is None:
            raise ValueError("idempotency 'enforced' needs request_id_arg")
        if self.idempotency is not Idempotency.ENFORCED and self.readback is None:
            raise ValueError(f"idempotency {self.idempotency.value!r} needs a readback (read back before any retry)")
        refs = template_refs(self.args, roots=ACTION_TEMPLATE_ROOTS)
        if self.readback is not None:
            refs += template_refs(self.readback.args, roots=ACTION_TEMPLATE_ROOTS)
            refs += self.readback.match_refs()
        for problem in _ref_problems(self.op, refs):
            raise ValueError(problem)
        return self


def _ref_problems(op: str, refs: list[tuple[str, ...]]) -> list[str]:
    problems: list[str] = []
    payload_model = OP_PAYLOAD_MODELS.get(op)
    for ref in refs:
        root = ref[0]
        if root in ("target_ref", "expected_version") and not _item_has(op, root):
            problems.append(f"{op} has no {root}")
        if root == "payload" and payload_model is not None and len(ref) > 1:
            if ref[1] not in payload_model.model_fields:
                problems.append(f"{op} payload has no field {ref[1]!r} (fields: {sorted(payload_model.model_fields)})")
    return problems


class ActionMapping(ContractModel):
    """``connector_mapping`` kind ``action`` (versioned by the engine): the ops a tier-A connector
    can apply per-op. Ops not listed are guided (``95`` §2)."""

    kind: Literal["action"] = "action"
    ops: Annotated[list[ActionOpMapping], Field(min_length=1, max_length=MAX_ACTION_OPS)]

    @model_validator(mode="after")
    def _unique(self) -> ActionMapping:
        ops = [o.op for o in self.ops]
        if len(set(ops)) != len(ops):
            raise ValueError("each op is mapped at most once")
        return self

    def for_op(self, op: str) -> ActionOpMapping | None:
        return next((o for o in self.ops if o.op == op), None)

    @property
    def mapped_ops(self) -> list[str]:
        return sorted(o.op for o in self.ops)


def unmapped_ops(mapping: ActionMapping | None, ops: Collection[str]) -> list[str]:
    """Ops of a plan the mapping cannot apply (they become guided steps), sorted."""
    mapped = set(mapping.mapped_ops) if mapping is not None else set()
    return sorted(set(ops) - mapped)


def action_mapping_violations(
    mapping: ActionMapping,
    *,
    tools: Mapping[str, ToolClassification],
) -> list[str]:
    """Cross-checks against the connector's approved tool catalogue (``tools``: tool name →
    :class:`~keelson_contracts.connectors.ToolClassification`): every mapped tool must exist, the
    action tool must be a write tool whose confirmed idempotency is at least the mapping's
    claim, and every read-back tool must be a ``read`` tool."""
    rank = {Idempotency.NONE: 0, Idempotency.BEST_EFFORT: 1, Idempotency.ENFORCED: 2}
    problems: list[str] = []
    for m in mapping.ops:
        cls = tools.get(m.tool)
        if cls is None:
            problems.append(f"{m.op}: tool {m.tool!r} is not an approved tool of this connector")
        else:
            if cls.access is ToolAccess.READ:
                problems.append(f"{m.op}: tool {m.tool!r} is a read tool")
            if rank[cls.idempotency] < rank[m.idempotency]:
                problems.append(
                    f"{m.op}: mapping claims idempotency {m.idempotency.value!r} but {m.tool!r} is confirmed "
                    f"{cls.idempotency.value!r}"
                )
        if m.readback is not None:
            rb = tools.get(m.readback.tool)
            if rb is None or rb.access is not ToolAccess.READ:
                problems.append(f"{m.op}: readback tool {m.readback.tool!r} must be an approved read tool")
    return problems


# ───────────────────────────────── dry-run ──────────────────────────────────


class ActionDryRunRequest(ContractModel):
    """``POST /api/admin/connectors/{slug}/mappings/action/dry-run``: render the tool call for a
    sample action (never executed); with ``run_readback`` the read-back query runs (read-only)."""

    op: OpName
    target_ref: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    expected_version: PositiveInt | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    run_readback: StrictBool = True

    @model_validator(mode="after")
    def _json_payload(self) -> ActionDryRunRequest:
        template_refs(self.payload, roots=())
        return self


class RenderedCall(ContractModel):
    tool: ToolName
    args: dict[str, Any]

    @model_validator(mode="after")
    def _finite(self) -> RenderedCall:
        _require_finite(self.args)
        return self


def _require_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("rendered arguments must be finite JSON")
    if isinstance(value, list):
        for v in value:
            _require_finite(v)
    elif isinstance(value, Mapping):
        for v in value.values():
            _require_finite(v)


class DryRunReadback(ContractModel):
    """The rendered read-back and, when it ran, what it found."""

    call: RenderedCall
    executed: StrictBool
    ok: StrictBool | None
    """``None`` when not executed; ``False`` when the tool call failed."""
    items_found: NonNegInt | None
    matched: StrictBool | None
    detail: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DryRunReadback:
        if not self.executed and (self.ok is not None or self.items_found is not None or self.matched is not None):
            raise ValueError("a read-back that did not run has no ok / items_found / matched")
        if self.executed and self.ok is None:
            raise ValueError("an executed read-back says whether it succeeded")
        if self.ok is False and (self.items_found is not None or self.matched is not None):
            raise ValueError("a failed read-back has no items_found / matched")
        return self


class ActionDryRunResult(ContractModel):
    """Dry-run answer: the exact tool call the commit service would make (``executed`` is always
    false), or why the op is guided."""

    op: OpName
    mapped: StrictBool
    call: RenderedCall | None
    idempotency: Idempotency | None
    request_id_arg: ArgName | None
    readback: DryRunReadback | None
    guided_reason: Annotated[str, Field(min_length=1, max_length=500)] | None
    executed: Literal[False] = False

    @model_validator(mode="after")
    def _consistent(self) -> ActionDryRunResult:
        if self.mapped:
            if self.call is None or self.idempotency is None or self.guided_reason is not None:
                raise ValueError("a mapped op has a call and idempotency and no guided_reason")
            if self.request_id_arg is not None and self.request_id_arg not in self.call.args:
                raise ValueError("the rendered call carries the request id under request_id_arg")
        elif (self.call is not None or self.idempotency is not None or self.readback is not None
              or self.request_id_arg is not None or self.guided_reason is None):
            raise ValueError("an unmapped op has only a guided_reason")
        return self
