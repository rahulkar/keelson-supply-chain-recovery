"""Source sampling and shape-only profiles for proposed tier-A mappings
(``docs/contracts/121-m8-mapping-compiler.md`` §3.1–§3.2). This module is the **untrusted-text
boundary** of the mapping proposer: what leaves it (a :class:`CatalogueTool` list and
:class:`~keelson_contracts.mapping_proposal.SourceProfile` s) is all the rule-based proposer and the
LLM ever see of the vendor.

* **Catalogue** (:func:`catalogue`): the connector's *approved* tools. Read candidates are
  ``read`` + ``trusted_data`` tools; ``untrusted_text`` read tools are never sampled nor described.
  Write tools are listed by name and parameters (action proposals; they are **never called**).
  Tool and parameter descriptions are vendor text: control / format characters are dropped, they
  are cut to 300 characters and screened with the deterministic injection patterns of
  :func:`~keelson_engine.runtime.quarantine.looks_like_injection`; a flagged description is dropped
  (note ``description_flagged`` + audit ``untrusted_text.flagged`` by the caller).
* **Sampling** (:func:`sample`): one first page (``MAPPING_PROPOSE_SAMPLE_SIZE`` items) per read
  tool and *variant*, through the caller's reader-principal session (egress proxy). Variants are
  the values of a string parameter named ``type`` / ``kind`` / ``entity`` / ``resource`` /
  ``table`` / ``collection`` taken from its ``enum`` or from the tokens after "one of" in its
  description (≤ 32); admin-named ``sources`` are sampled too. Paging is detected from the input
  schema (``page`` + ``page_size`` / ``per_page`` / ``limit`` / ``size``, honouring the schema
  maximum; ``cursor`` / ``page_token`` / ``after``), the item list, cursor and total from the first
  result. At most ``MAPPING_PROPOSE_MAX_CALLS`` calls (``sampling_truncated`` beyond). For action
  proposals, a get-by-key read tool is probed once per variant with a sampled key to learn where its
  result holds the record (the read-back ``items_path``).
* **Profiles** (:func:`profile_items`): per item path (keys ``^[A-Za-z_][A-Za-z0-9_]{0,63}$`` up to
  depth 4, ≤ 200 paths) a *shape* and counts. Examples are kept only for shape-safe values (codes
  without whitespace, integers, decimals, dates, ISO weeks, booleans) that also pass the injection
  screen; free text (names, cities with spaces, descriptions) is reduced to the shape ``text`` and
  its maximum length (note ``sample_value_withheld``). Raw samples never leave this module and
  are never stored.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from keelson_contracts.common import sha256_hex
from keelson_contracts.connectors import ContentTrust, ToolAccess, ToolClassification
from keelson_contracts.mapping import MAX_PAGE_SIZE, MAX_PAGES_LIMIT, ReadSource, parse_path
from keelson_contracts.mapping_proposal import (
    MAX_EXAMPLES,
    MAX_PROFILE_PATHS,
    MappingProposeRequest,
    PathProfile,
    ProposalNote,
    ShapeCount,
    SourceProfile,
)
from keelson_engine.core.logs import get_logger
from keelson_engine.core.mapping_eval import canonical_json

__all__ = [
    "CatalogueParam",
    "CatalogueTool",
    "Notes",
    "Probe",
    "Sampling",
    "catalogue",
    "clean_text",
    "inputs_hash",
    "norm",
    "profile_items",
    "sample",
]

log = get_logger(__name__)

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]

MAX_DESCRIPTION: Final = 300
MAX_PARAM_DESCRIPTION: Final = 200
MAX_VARIANTS: Final = 32
MAX_DEPTH: Final = 4
DEFAULT_PAGE_SIZE: Final = 100
DEFAULT_MAX_PAGES: Final = 1000
MIN_MAX_PAGES: Final = 10
SAMPLE_SIZE: Final = 20
"""Items per sampled page (120 §3.5: a code constant; ``MAPPING_PROPOSE_SAMPLE_SIZE`` may only lower it,
recorded as a ``sampling_truncated`` note)."""
MAX_CALLS: Final = 24
"""Read calls one proposal may make (``MAPPING_PROPOSE_MAX_CALLS`` may only lower it)."""

VARIANT_PARAMS: Final = frozenset({"type", "kind", "entity", "resource", "table", "collection", "record_type",
                                   "entity_type", "object_type"})
PAGE_PARAMS: Final = ("page", "page_no", "page_number", "page_index")
SIZE_PARAMS: Final = ("page_size", "per_page", "limit", "size", "max_results")
CURSOR_PARAMS: Final = ("cursor", "page_token", "after", "next_token", "continuation", "starting_after")
KEY_PARAMS: Final = ("key", "id", "code", "ref", "identifier")
ITEMS_KEYS: Final = ("items", "data", "results", "records", "rows", "entries", "values", "objects")
CURSOR_KEYS: Final = ("next_cursor", "next", "next_page_token", "cursor", "continuation", "next_token")
TOTAL_KEYS: Final = ("total", "total_count", "count", "total_items", "total_results")

_KEY_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TOKEN_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,40}")
_ONE_OF_RE: Final = re.compile(r"\bone\s+of\b\s*:?\s*(.+)", re.IGNORECASE | re.DOTALL)
_CODE_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#|-]{0,39}$")
_DATE_RE: Final = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
_DATETIME_RE: Final = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?(Z|[+-][0-9]{2}:?[0-9]{2})?$")
_WEEK_RE: Final = re.compile(r"^([0-9]{4})(-?)W([0-9]{2})$")
_INT_TEXT_RE: Final = re.compile(r"^[+-]?[0-9]{1,16}$")
_DECIMAL_TEXT_RE: Final = re.compile(r"^[+-]?[0-9]{1,16}\.[0-9]{1,16}$")
_DMY_FORMATS: Final = (("DD/MM/YYYY", re.compile(r"^([0-9]{2})/([0-9]{2})/([0-9]{4})$"), (3, 2, 1)),
                       ("MM/DD/YYYY", re.compile(r"^([0-9]{2})/([0-9]{2})/([0-9]{4})$"), (3, 1, 2)),
                       ("DD.MM.YYYY", re.compile(r"^([0-9]{2})\.([0-9]{2})\.([0-9]{4})$"), (3, 2, 1)),
                       ("DD-MM-YYYY", re.compile(r"^([0-9]{2})-([0-9]{2})-([0-9]{4})$"), (3, 2, 1)),
                       ("YYYY/MM/DD", re.compile(r"^([0-9]{4})/([0-9]{2})/([0-9]{2})$"), (1, 2, 3)))
_SAFE_SHAPES: Final = frozenset({"code", "int", "int_text", "decimal_text", "date", "datetime", "iso_week", "bool",
                                 "float"})
_SPECIFIC: Final = ("date", "datetime", "iso_week", "int_text", "decimal_text")


def norm(name: str) -> str:
    """``fooBar-baz`` → ``foo_bar_baz`` (names compared by the proposers)."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def clean_text(text: Any, limit: int = MAX_DESCRIPTION) -> str:
    """Vendor text for display / the model: NFKC, no control or format characters, whitespace
    collapsed, at most ``limit`` characters."""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in unicodedata.normalize("NFKC", text):
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Co", "Cs"):
            continue
        out.append(" " if cat == "Cc" or ch.isspace() else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()[:limit]


def _flagged(text: str) -> bool:
    from keelson_engine.runtime.quarantine import looks_like_injection

    return bool(text) and looks_like_injection(text) is not None


# ───────────────────────────── notes ─────────────────────────────


class Notes:
    """Collects proposal notes (one per ``(code, where)``) and flagged vendor texts (for the audit)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str | None], ProposalNote] = {}
        self.flagged: list[str] = []

    def add(self, code: str, where: str | None, message: str, *, severity: str = "info") -> None:
        key = (code, where)
        if key not in self._items:
            self._items[key] = ProposalNote.model_validate({
                "code": code, "severity": severity, "where": where[:200] if where else None,
                "message": clean_text(message, 300) or code})

    def extend(self, notes: Iterable[ProposalNote]) -> None:
        for n in notes:
            self._items.setdefault((n.code, n.where), n)

    def has(self, code: str) -> bool:
        return any(c == code for c, _w in self._items)

    @property
    def items(self) -> list[ProposalNote]:
        return list(self._items.values())


# ───────────────────────────── the catalogue ─────────────────────────────


@dataclass(frozen=True, slots=True)
class CatalogueParam:
    name: str
    types: tuple[str, ...]
    """JSON Schema types (``string``, ``integer`` …; ``null`` for optional ones)."""
    required: bool
    enum: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    default: Any = None
    description: str | None = None
    """Sanitised (``None`` = absent or flagged)."""

    @property
    def is_string(self) -> bool:
        return "string" in self.types

    @property
    def is_integer(self) -> bool:
        return "integer" in self.types or "number" in self.types


@dataclass(frozen=True, slots=True)
class CatalogueTool:
    name: str
    classification: ToolClassification
    description: str | None
    params: tuple[CatalogueParam, ...]
    definition_hash: str
    variants: tuple[str, ...] = ()
    """Discovered values of the variant parameter (``sku``, ``store`` …)."""
    variant_param: str | None = None

    @property
    def readable(self) -> bool:
        """An approved trusted read tool (may be sampled and crawled)."""
        cls = self.classification
        return cls.access is ToolAccess.READ and cls.content_trust is ContentTrust.TRUSTED_DATA

    @property
    def writable(self) -> bool:
        return self.classification.access is not ToolAccess.READ

    def param(self, name: str) -> CatalogueParam | None:
        return next((p for p in self.params if p.name == name), None)

    def model_view(self) -> dict[str, Any]:
        """What the LLM is told about the tool (sanitised; descriptions are fenced by the caller)."""
        cls = self.classification
        return {
            "name": self.name, "access": cls.access.value, "idempotency": cls.idempotency.value,
            "description": self.description,
            "params": [{"name": p.name, "types": list(p.types), "required": p.required, "enum": list(p.enum[:32]),
                        "minimum": p.minimum, "maximum": p.maximum, "description": p.description}
                       for p in self.params[:40]],
            "variants": list(self.variants),
        }


def _schema_types(schema: Mapping[str, Any]) -> tuple[set[str], list[Mapping[str, Any]]]:
    types: set[str] = set()
    branches: list[Mapping[str, Any]] = [schema]
    for key in ("anyOf", "oneOf"):
        for b in schema.get(key) or ():
            if isinstance(b, Mapping):
                branches.append(b)
    for b in branches:
        t = b.get("type")
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(x for x in t if isinstance(x, str))
    return types, branches


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return int(value)
    return None


def _param(name: str, schema: Any, required: bool, notes: Notes, tool: str) -> CatalogueParam:
    schema = schema if isinstance(schema, Mapping) else {}
    types, branches = _schema_types(schema)
    enum: list[str] = []
    minimum = maximum = None
    flagged_enum = False
    for b in branches:
        for v in b.get("enum") or ():
            # enum values are vendor text the model sees: like sampled examples, only shape-safe codes
            # that pass the injection screen are kept (free text is dropped, instructions are flagged)
            if not isinstance(v, str) or v in enum:
                continue
            if _flagged(clean_text(v, MAX_PARAM_DESCRIPTION)):
                flagged_enum = True
            elif _CODE_RE.fullmatch(v):
                enum.append(v)
        minimum = minimum if minimum is not None else _int_or_none(b.get("minimum"))
        maximum = maximum if maximum is not None else _int_or_none(b.get("maximum"))
    desc = clean_text(schema.get("description"), MAX_PARAM_DESCRIPTION) or None
    flagged_desc = bool(desc and _flagged(desc))
    if flagged_desc or flagged_enum:
        what = " and ".join(w for w, f in (("description", flagged_desc), ("allowed values", flagged_enum)) if f)
        notes.add("description_flagged", f"{tool}.{name}"[:200],
                  f"text that looks like instructions was dropped from the {what} of parameter {name} of {tool}",
                  severity="warning")
        notes.flagged.append(f"{tool}.{name}")
    if flagged_desc:
        desc = None
    default = schema.get("default")
    if not isinstance(default, (str, int, bool)) or isinstance(default, float):
        default = None
    return CatalogueParam(name=name, types=tuple(sorted(types)) or ("any",), required=required,
                          enum=tuple(enum[:200]), minimum=minimum, maximum=maximum, default=default,
                          description=desc)


def _variants(params: Sequence[CatalogueParam]) -> tuple[str | None, tuple[str, ...]]:
    """The variant parameter (a string parameter with a record-type name) and its discovered
    values (``()`` when the schema names none: a get tool's ``type`` takes the list tool's values)."""
    for p in params:
        if norm(p.name) not in VARIANT_PARAMS or not p.is_string:
            continue
        found = [v for v in p.enum if _TOKEN_RE.fullmatch(v)]
        if not found and p.description:
            m = _ONE_OF_RE.search(p.description)
            if m:
                head = re.split(r"[.;:()\[\]]", m.group(1), maxsplit=1)[0]
                found = [t for t in re.split(r"[\s,|/]+", head)
                         if t and t not in ("or", "and", "etc") and _TOKEN_RE.fullmatch(t)]
        return p.name, tuple(dict.fromkeys(found))[:MAX_VARIANTS]
    return None, ()


def catalogue(tools: Sequence[Any], notes: Notes) -> list[CatalogueTool]:
    """The approved tools (``connectors.ToolRow`` s) as sanitised :class:`CatalogueTool` s.
    ``untrusted_text`` read tools are left out entirely (never sampled nor described)."""
    out: list[CatalogueTool] = []
    for t in tools:
        if getattr(t, "status", "approved") != "approved":
            continue
        cls: ToolClassification = t.classification
        if cls.access is ToolAccess.READ and cls.content_trust is ContentTrust.UNTRUSTED_TEXT:
            continue
        desc = clean_text(t.description) or None
        if cls.content_trust is ContentTrust.UNTRUSTED_TEXT:
            desc = None
        elif desc and _flagged(desc):
            notes.add("description_flagged", t.name, f"the description of {t.name} looks like instructions; it was "
                      "dropped", severity="warning")
            notes.flagged.append(t.name)
            desc = None
        schema = t.input_schema if isinstance(t.input_schema, Mapping) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = {r for r in schema.get("required") or () if isinstance(r, str)}
        params = tuple(_param(name, s, name in required, notes, t.name)
                       for name, s in list(props.items())[:60] if isinstance(name, str) and _KEY_RE.fullmatch(name))
        vparam, variants = _variants(params)
        out.append(CatalogueTool(name=t.name, classification=cls, description=desc, params=params,
                                 definition_hash=str(t.definition_hash), variants=variants, variant_param=vparam))
    return out


# ───────────────────────────── paging and results ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Paging:
    page_param: str | None
    size_param: str | None
    cursor: bool
    first_page: int
    max_size: int | None

    def page_size(self) -> int:
        return min(self.max_size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)


def _find(params: Sequence[CatalogueParam], names: Sequence[str], *, integer: bool) -> CatalogueParam | None:
    by_norm = {norm(p.name): p for p in params}
    for n in names:
        p = by_norm.get(n)
        if p is not None and (p.is_integer if integer else p.is_string):
            return p
    return None


def paging(tool: CatalogueTool) -> Paging:
    page = _find(tool.params, PAGE_PARAMS, integer=True)
    size = _find(tool.params, SIZE_PARAMS, integer=True)
    if page is not None:
        first = page.minimum if page.minimum in (0, 1) else (page.default if page.default in (0, 1) else 1)
        return Paging(page.name, size.name if size else None, False, int(first), size.maximum if size else None)
    cursor = _find(tool.params, CURSOR_PARAMS, integer=False)
    if cursor is not None:
        return Paging(cursor.name, size.name if size else None, True, 1, size.maximum if size else None)
    return Paging(None, None, False, 1, None)


def _items(result: Any) -> tuple[str, list[Any]] | None:
    """Where the result holds its item list: ``$`` (a list), ``$.items`` / ``$.data`` / … or the
    first top-level list of objects (one nested object level is searched too)."""
    if isinstance(result, list):
        return "$", result
    if not isinstance(result, Mapping):
        return None

    def lists(obj: Mapping[str, Any], prefix: str) -> tuple[str, list[Any]] | None:
        keyed = {norm(str(k)): k for k in obj if isinstance(k, str) and _KEY_RE.fullmatch(k)}
        for name in ITEMS_KEYS:
            k = keyed.get(name)
            if k is not None and isinstance(obj[k], list):
                return f"{prefix}.{k}", obj[k]
        for k in keyed.values():
            v = obj[k]
            if isinstance(v, list) and v and all(isinstance(x, Mapping) for x in v):
                return f"{prefix}.{k}", v
        return None

    found = lists(result, "$")
    if found is not None:
        return found
    for k, v in result.items():
        if isinstance(k, str) and _KEY_RE.fullmatch(k) and isinstance(v, Mapping):
            found = lists(v, f"$.{k}")
            if found is not None:
                return found
    return None


def _record(result: Any) -> str | None:
    """Where a get-by-key result holds the record: the result itself or its first object value."""
    if not isinstance(result, Mapping):
        return None
    for k, v in result.items():
        if isinstance(k, str) and _KEY_RE.fullmatch(k) and isinstance(v, Mapping) and v:
            return f"$.{k}"
    return "$"


def _top_key(result: Any, names: Sequence[str]) -> str | None:
    if not isinstance(result, Mapping):
        return None
    keyed = {norm(str(k)): k for k in result if isinstance(k, str) and _KEY_RE.fullmatch(k)}
    return next((str(keyed[n]) for n in names if n in keyed), None)


# ───────────────────────────── shapes and profiles ─────────────────────────────


def _date_ok(y: int, m: int, d: int) -> bool:
    try:
        return y >= 1 and dt.date(y, m, d) is not None
    except ValueError:
        return False


def _week_ok(y: int, w: int) -> bool:
    from keelson_contracts.common import weeks_in_year

    return 1 <= y <= 9999 and 1 <= w <= weeks_in_year(y)


def str_shape(s: str) -> str:
    """The shape of one text value."""
    if m := _DATE_RE.fullmatch(s):
        if _date_ok(int(m[1]), int(m[2]), int(m[3])):
            return "date"
    if m := _DATETIME_RE.fullmatch(s):
        if _date_ok(int(m[1]), int(m[2]), int(m[3])):
            return "datetime"
    if m := _WEEK_RE.fullmatch(s):
        if _week_ok(int(m[1]), int(m[3])):
            return "iso_week"
    if _INT_TEXT_RE.fullmatch(s):
        return "int_text"
    if _DECIMAL_TEXT_RE.fullmatch(s):
        return "decimal_text"
    if _CODE_RE.fullmatch(s):
        return "code"
    return "text"


def _combine_text(shapes: set[str], values: Sequence[str]) -> str:
    if len(shapes) == 1:
        return next(iter(shapes))
    if shapes == {"date", "datetime"}:
        return "datetime"
    if shapes == {"int_text", "decimal_text"}:
        return "decimal_text"
    if "text" not in shapes and all(_CODE_RE.fullmatch(v) for v in values):
        return "code"
    return "text"


def _date_format(values: Sequence[str]) -> str | None:
    """A ``date()`` format every value parses with (``None`` when none, or ambiguous)."""
    fits: list[str] = []
    for name, rx, (yi, mi, di) in _DMY_FORMATS:
        ok = True
        for v in values:
            m = rx.fullmatch(v)
            if m is None or not _date_ok(int(m[yi]), int(m[mi]), int(m[di])):
                ok = False
                break
        if ok:
            fits.append(name)
    return fits[0] if len(fits) == 1 else None


_TYPE_OF: Final = {bool: "bool", int: "int", float: "float", str: "str", dict: "object", list: "list"}


def _type(v: Any) -> str:
    if v is None:
        return "null"
    return _TYPE_OF.get(type(v), "object" if isinstance(v, Mapping) else "list" if isinstance(v, list) else "str")


def _shape(values: Sequence[Any]) -> tuple[str, str | None]:
    """``(shape, date format)`` of the non-null values of one path."""
    if not values:
        return "empty", None
    kinds = {_type(v) for v in values}
    if kinds == {"str"}:
        texts = [v for v in values if isinstance(v, str)]
        shapes = {str_shape(s) for s in texts}
        shape = _combine_text(shapes, texts)
        if shape == "code":
            fmt = _date_format(texts)
            if fmt is not None:
                return "date", fmt
        return shape, None
    if kinds == {"int"}:
        return "int", None
    if kinds == {"bool"}:
        return "bool", None
    if kinds <= {"int", "float"}:
        return "float", None
    if kinds == {"int", "str"} and all(isinstance(v, int) or _INT_TEXT_RE.fullmatch(v) for v in values):
        return "int_text", None
    if kinds == {"object"}:
        return "object", None
    if kinds == {"list"}:
        return "list", None
    return "text", None


def _example(v: Any, shape: str) -> str | None:
    if shape not in _SAFE_SHAPES:
        return None
    if isinstance(v, bool):
        text = "true" if v else "false"
    elif isinstance(v, int):
        text = str(v)
    elif isinstance(v, float):
        if not math.isfinite(v):
            return None
        text = repr(v)
    elif isinstance(v, str):
        text = v
        if str_shape(v) == "text" or not (_CODE_RE.fullmatch(v) or _DATETIME_RE.fullmatch(v)):
            return None
    else:
        return None
    if not text or len(text) > 80 or _flagged(text):
        return None
    return text


def _walk(value: Mapping[str, Any], segs: tuple[str, ...], out: dict[tuple[str, ...], list[Any]],
          dropped: set[str]) -> None:
    for k, v in value.items():
        if not (isinstance(k, str) and _KEY_RE.fullmatch(k)) or k.startswith("__"):
            dropped.add(clean_text(str(k), 40) or "?")
            continue
        path = (*segs, k)
        if isinstance(v, Mapping) and len(path) < MAX_DEPTH:
            _walk(v, path, out, dropped)
            out.setdefault(path, [])
            continue
        out.setdefault(path, []).append(v)


def profile_items(items: Sequence[Any], *, where: str, notes: Notes) -> list[PathProfile]:
    """Shape statistics of every item path (the items themselves are not kept)."""
    seen: dict[tuple[str, ...], list[Any]] = {}
    dropped: set[str] = set()
    objects = [it for it in items if isinstance(it, Mapping)]
    for it in objects:
        _walk(it, (), seen, dropped)
    if dropped:
        notes.add("key_dropped", where, f"{len(dropped)} item key(s) that are not plain names were left out")
    paths = [p for p in seen if seen[p] or not any(q[: len(p)] == p and q != p for q in seen)]
    if len(paths) > MAX_PROFILE_PATHS:
        notes.add("key_dropped", where, f"only the first {MAX_PROFILE_PATHS} item paths were profiled")
        paths = paths[:MAX_PROFILE_PATHS]
    out: list[PathProfile] = []
    withheld = False
    for segs in paths:
        values = [v for v in seen[segs] if v is not None]
        shape, fmt = _shape(values)
        counts: dict[str, int] = {}
        for v in seen[segs]:
            counts[_type(v)] = counts.get(_type(v), 0) + 1
        missing = len(objects) - len(seen[segs])
        if missing > 0:
            counts["null"] = counts.get("null", 0) + missing
        distinct = len({canonical_json(v) for v in values})
        examples: list[str] = []
        for v in values:
            if len(examples) >= MAX_EXAMPLES:
                break
            ex = _example(v, shape)
            if ex is not None and ex not in examples:
                examples.append(ex)
        if shape in ("text", "object", "list") and values:
            withheld = True
        try:
            text = "$." + ".".join(segs)
            parse_path(text)
        except ValueError:
            continue
        out.append(PathProfile.model_validate({
            "path": text, "shape": shape, "date_format": fmt,
            "types": [ShapeCount(type=t, count=n) for t, n in sorted(counts.items())],  # type: ignore[arg-type]
            "null_count": len(objects) - len(values), "distinct_count": distinct,
            "unique": bool(values) and distinct == len(values) and len(values) == len(objects),
            "max_len": max((len(v) for v in values if isinstance(v, str)), default=0),
            "examples": examples,
        }))
    if withheld:
        notes.add("sample_value_withheld", None, "free-text values (names, descriptions) are summarised as their "
                  "shape and length; they are never shown or sent to a model")
    return out


def _schema_paths(tool: Any) -> list[PathProfile]:
    """Schema-only: the item properties the tool's output schema declares (no values)."""
    schema = getattr(tool, "output_schema", None)
    if not isinstance(schema, Mapping):
        return []
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    item_props: Mapping[str, Any] | None = None
    for name in ITEMS_KEYS:
        s = props.get(name)
        if isinstance(s, Mapping) and s.get("type") == "array" and isinstance(s.get("items"), Mapping):
            inner = s["items"].get("properties")
            if isinstance(inner, Mapping):
                item_props = inner
                break
    if item_props is None:
        return []
    out: list[PathProfile] = []
    for name, s in list(item_props.items())[:MAX_PROFILE_PATHS]:
        if not (isinstance(name, str) and _KEY_RE.fullmatch(name)) or name.startswith("__") or not isinstance(s, Mapping):
            continue
        types, branches = _schema_types(s)
        fmt = next((b.get("format") for b in branches if isinstance(b.get("format"), str)), None)
        if "integer" in types:
            shape = "int"
        elif "number" in types:
            shape = "float"
        elif "boolean" in types:
            shape = "bool"
        elif "string" in types:
            shape = {"date": "date", "date-time": "datetime"}.get(str(fmt), "text")
        elif "array" in types:
            shape = "list"
        elif "object" in types:
            shape = "object"
        else:
            continue
        out.append(PathProfile(path=f"$.{name}", shape=shape, types=[], null_count=0, distinct_count=0,  # type: ignore[arg-type]
                               unique=False, max_len=0, examples=[]))
    return out


# ───────────────────────────── sampling ─────────────────────────────


@dataclass(frozen=True, slots=True)
class Probe:
    """Where a get-by-key read tool holds the record (learned from one call with a sampled key)."""

    tool: str
    variant: str | None
    """The variant the probe used (the result layout is assumed the same for every variant)."""
    variant_param: str | None
    key_param: str
    items_path: str


@dataclass(slots=True)
class Sampling:
    profiles: list[SourceProfile] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    calls: int = 0


@dataclass(frozen=True, slots=True)
class _Plan:
    tool: CatalogueTool
    variant: str | None
    args: dict[str, Any]


def _fillable(tool: CatalogueTool, args: Mapping[str, Any], pg: Paging) -> list[str]:
    """Required parameters the sampler cannot fill (``[]`` = the tool can be sampled)."""
    filled = set(args) | {n for n in (pg.page_param, pg.size_param) if n}
    return [p.name for p in tool.params if p.required and p.name not in filled and p.default is None]


def _plans(tools: Sequence[CatalogueTool], request: MappingProposeRequest, notes: Notes) -> list[_Plan]:
    plans: list[_Plan] = []
    readable = {t.name: t for t in tools if t.readable}
    for t in readable.values():
        pg = paging(t)
        if t.variant_param and t.variants:
            for v in t.variants:
                if missing := _fillable(t, {t.variant_param: v}, pg):
                    notes.add("source_unused", t.name, f"{t.name} needs arguments the sampler cannot choose "
                              f"({', '.join(missing[:4])}); name them in sources")
                    break
                plans.append(_Plan(t, v, {t.variant_param: v}))
            continue
        if missing := _fillable(t, {}, pg):
            if not any(p.name in KEY_PARAMS for p in t.params if p.required):  # get-by-key tools are probed instead
                notes.add("source_unused", t.name, f"{t.name} needs arguments the sampler cannot choose "
                          f"({', '.join(missing[:4])}); name them in sources")
            continue
        plans.append(_Plan(t, None, {}))
    for i, src in enumerate(request.sources):
        t = readable.get(src.tool)
        where = f"sources[{i}]"
        if t is None:
            notes.add("sample_failed", where, f"{src.tool} is not an approved trusted read tool of this connector",
                      severity="warning")
            continue
        pg = paging(t)
        args = {k: v for k, v in src.args.items() if k not in (pg.page_param, pg.size_param)}
        variant = args.get(t.variant_param) if t.variant_param else None
        if missing := _fillable(t, args, pg):
            notes.add("sample_failed", where, f"{src.tool} still needs {', '.join(missing[:4])}", severity="warning")
            continue
        plan = _Plan(t, variant if isinstance(variant, str) else None, args)
        if all(p.tool.name != t.name or p.args != args for p in plans):
            plans.append(plan)
    return plans


def _read_source(plan: _Plan, pg: Paging, items_path: str, cursor_key: str | None,
                 total: int | None) -> ReadSource:
    size = pg.page_size()
    if pg.page_param is None:
        max_pages = 1
    elif total is not None:
        max_pages = min(MAX_PAGES_LIMIT, max(MIN_MAX_PAGES, math.ceil(total / size) * 2))
    else:
        max_pages = DEFAULT_MAX_PAGES
    return ReadSource.model_validate({
        "tool": plan.tool.name, "args": dict(plan.args), "page_param": pg.page_param,
        "page_size_param": pg.size_param, "page_size": size, "items_path": items_path,
        "cursor_path": f"$.{cursor_key}" if pg.cursor and cursor_key else None, "max_pages": max_pages,
        "first_page": pg.first_page,
    })


async def sample(call: ToolCaller | None, tools: Sequence[CatalogueTool], request: MappingProposeRequest, *,
                 max_calls: int, sample_size: int, notes: Notes, raw_tools: Mapping[str, Any] | None = None,
                 probe: Sequence[str] | None = None) -> Sampling:
    """Profile the readable tools (``call=None``: schema-only, from declared output schemas).
    ``probe`` (action proposals): probe each get-by-key read tool once, preferring a key of these
    variants."""
    from keelson_engine.core.scn_crawl import CrawlError

    out = Sampling()
    plans = _plans(tools, request, notes)
    keys: dict[str | None, str] = {}
    for plan in plans:
        ref = f"S{len(out.profiles) + 1}"
        where = f"{plan.tool.name}({plan.variant})" if plan.variant else plan.tool.name
        pg = paging(plan.tool)
        if call is None:
            paths = _schema_paths((raw_tools or {}).get(plan.tool.name))
            if not paths:
                continue
            src = _read_source(plan, pg, "$.items", None, None)
            out.profiles.append(SourceProfile(ref=ref, source=src, variant=plan.variant, items_sampled=0,
                                              total_hint=None, paths=paths))
            continue
        if out.calls >= max_calls:
            notes.add("sampling_truncated", None, f"sampling stopped after {max_calls} calls "
                      "(MAPPING_PROPOSE_MAX_CALLS); name the reads you need in sources", severity="warning")
            break
        args = dict(plan.args)
        size = min(sample_size, pg.max_size or sample_size, MAX_PAGE_SIZE)
        if pg.page_param and not pg.cursor:
            args[pg.page_param] = pg.first_page
        if pg.size_param:
            args[pg.size_param] = size
        out.calls += 1
        try:
            result = await call(plan.tool.name, args)
        except CrawlError as exc:
            notes.add("sample_failed", where, f"{where} could not be sampled ({exc.code})", severity="warning")
            continue
        except (asyncio.CancelledError, TimeoutError):
            raise
        found = _items(result)
        if found is None:
            notes.add("sample_failed", where, f"{where} returned no list of items", severity="warning")
            continue
        items_path, items = found
        total = None
        tkey = _top_key(result, TOTAL_KEYS)
        if tkey is not None and isinstance(result, Mapping):
            total = _int_or_none(result.get(tkey))
            total = total if total is not None and total >= 0 else None
        cursor_key = _top_key(result, CURSOR_KEYS) if pg.cursor else None
        if pg.cursor and cursor_key is None:
            pg = Paging(None, None, False, 1, None)  # no cursor in the result: one call
        src = _read_source(plan, pg, items_path, cursor_key, total)
        paths = profile_items(items[:sample_size], where=where, notes=notes)
        out.profiles.append(SourceProfile(ref=ref, source=src, variant=plan.variant,
                                          items_sampled=min(len(items), sample_size), total_hint=total,
                                          paths=paths))
        key_path = _sample_key(items)
        if key_path is not None:
            keys.setdefault(plan.variant, key_path)
    if probe is not None and call is not None:
        await _probe(call, tools, keys, list(probe), out, max_calls=max_calls, notes=notes)
    return out


def _sample_key(items: Sequence[Any]) -> str | None:
    """A key value of the first item (``key`` / ``code`` / ``id``) usable to probe a get tool."""
    for it in items[:1]:
        if not isinstance(it, Mapping):
            continue
        for k in ("key", "code", "id"):
            v = it.get(k)
            if isinstance(v, str) and 0 < len(v) <= 200 or isinstance(v, int) and not isinstance(v, bool):
                return str(v)
    return None


async def _probe(call: ToolCaller, tools: Sequence[CatalogueTool], keys: Mapping[str | None, str],
                 prefer: Sequence[str], out: Sampling, *, max_calls: int, notes: Notes) -> None:
    """One call per get-by-key read tool (with a sampled key): where its result holds the record."""
    from keelson_engine.core.scn_crawl import CrawlError

    for t in tools:
        if not t.readable:
            continue
        key_param = next((p.name for p in t.params if norm(p.name) in KEY_PARAMS and p.required), None)
        others = [p.name for p in t.params if p.required and p.name not in (key_param, t.variant_param)]
        if key_param is None or others:
            continue
        if t.variant_param:
            candidates = [v for v in prefer if v in keys] + [v for v in keys if v is not None and v not in prefer]
            if t.variants:
                candidates = [v for v in candidates if v in t.variants]
        else:
            candidates = [None] if None in keys else []
        if not candidates:
            continue
        if out.calls >= max_calls:
            notes.add("sampling_truncated", None, f"sampling stopped after {max_calls} calls "
                      "(MAPPING_PROPOSE_MAX_CALLS)", severity="warning")
            return
        v = candidates[0]
        args: dict[str, Any] = {key_param: keys[v]}
        if t.variant_param and v is not None:
            args[t.variant_param] = v
        out.calls += 1
        try:
            result = await call(t.name, args)
        except CrawlError:
            continue
        path = _record(result)
        if path is not None:
            out.probes.append(Probe(tool=t.name, variant=v, variant_param=t.variant_param, key_param=key_param,
                                    items_path=path))


def inputs_hash(profiles: Sequence[SourceProfile], tools: Sequence[CatalogueTool], probes: Sequence[Probe],
                request: MappingProposeRequest) -> str:
    """sha256 of what a proposal is computed from (profiles, approved tool definitions, probes, request)."""
    return sha256_hex({
        "profiles": [p.model_dump(mode="json") for p in profiles],
        "tools": [[t.name, t.definition_hash, t.classification.model_dump(mode="json")] for t in tools],
        "probes": [[p.tool, p.variant, p.items_path] for p in probes],
        "request": request.model_dump(mode="json"),
    })
