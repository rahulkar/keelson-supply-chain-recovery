"""Rule-based mapping proposer (``docs/contracts/121-m8-mapping-compiler.md`` §3.3): source
profiles + the approved tool catalogue → read / action mapping **drafts**, each field with a
confidence (basis points), the origin ``heuristic`` and a deterministic rationale. Pure (no I/O, no
model, no clock): the same profiles always give the same proposal, so it works with LLM credits at
zero and is what the LLM proposer refines.

Read rules (in order, per sampled source):

1. **Entity.** The source's variant (``list_items(type=dc)`` → ``dc``) or tool name is matched
   against synonym tables: ``dc`` / ``warehouse`` / ``store`` / ``cluster`` / ``factory`` … →
   ``location`` (that kind), ``sku`` / ``style`` / ``product`` … → ``item``, ``po_line``,
   ``inventory`` / ``stock`` → ``inventory_position``, ``forecast`` → ``demand_forecast`` …
2. **Columns.** Each ``scn/1.0`` column takes the best item path by name (exact, synonym, then
   without ``_code`` / ``_id`` suffixes) whose sampled *shape* fits the column type, which also
   picks the wrapper: ``int($.x)`` for counts, ``$.x`` for an integer version, ``cents($.x)`` for
   decimal money in ``*_cents``, ``date($.x)`` / ``date($.x, 'DD/MM/YYYY')``, ``$.x`` or
   ``iso_week($.x)`` for weeks, ``str($.x)`` for integer codes. ``kind`` columns are constants from
   the variant; other enum columns take a path whose sampled values all are options; a
   ``location_kind`` / ``location_code`` pair over ``store_code`` / ``cluster_code`` paths becomes
   ``if_present`` / ``coalesce``; a style's ``style_code`` is its own code; a location's
   ``parent_code`` is the path of its parent kind (store → cluster → dc, factory → supplier).
3. **Record joins.** A column still missing while the item has ``<x>_code`` and another sampled
   source ``x`` (``po`` = ``purchase_order``) is keyed by a unique code and has that column becomes
   a ``record`` lookup + ``field('x', $.x_code, '$.column')`` (sample overlap of the codes raises
   the confidence).
4. **Derived parents.** A parent kind with no source of its own (store → ``cluster_code``) becomes a
   ``group_by`` location entity with ``parent_code: first($.<parent>_code)`` (low confidence).
5. **source_id.** ``'<kind>:' + code`` for locations / items (the Merxa tier-B convention), the
   unique ``code`` otherwise, else the ``scn/1.0`` natural key joined with ``#`` / ``/`` / ``|``
   (``po_code#line_no``, ``location_kind/location_code/sku_code``, ``style|location|week``).

Action rules (per canonical op): a **specific** write tool whose name tokens contain the op's
(``create_transfer_order`` ↔ ``transfer_order.create``) with arguments matched by name
(``*_json`` text parameters get a ``{"$json": …}`` wrapper, ``request_id`` / ``idempotency_key``
becomes ``request_id_arg``); for field-setting ops (``allocation.set_qty``, ``po_line.reschedule``,
``po_line.change_qty``) a **generic update tool** ``(type, key, fields_json, expected_version)``
is used at low confidence and flagged (``generic_update_tool``). Idempotency is the admin-confirmed
classification of the tool (never higher). The read-back is a get-by-key read tool (whose result
layout was probed) or a list tool filtered on the payload fields.
"""

from __future__ import annotations

import datetime as dt
import re
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal, Union, get_args, get_origin

from keelson_contracts.changeset import OP_PAYLOAD_MODELS
from keelson_contracts.connectors import Idempotency
from keelson_contracts.mapping import (
    LOOKUP_NAME_PATTERN,
    ActionOpMapping,
    CallExpr,
    ConstExpr,
    Expr,
    PathExpr,
    RecordLookup,
    expression_paths,
    op_takes,
    parse_expression,
    parse_path,
    path_text,
    required_scn_columns,
)
from keelson_contracts.mapping_proposal import (
    PathProfile,
    ProposedAction,
    ProposedEntity,
    ProposedField,
    ProposedLookup,
    ProposedOp,
    ProposedRead,
    SourceProfile,
)
from keelson_contracts.scn import SCN_COLUMNS, SCN_ENTITIES, SCN_ROW_MODELS
from keelson_engine.core.mapping_profile import KEY_PARAMS, CatalogueParam, CatalogueTool, Notes, Probe, norm

__all__ = [
    "ColType",
    "HeuristicResult",
    "col_type",
    "entity_of",
    "expression_type_problem",
    "probe_variants",
    "propose",
]

# ───────────────────────────── vocabularies ─────────────────────────────

LOCATION_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "dc": ("dc", "warehouse", "distribution_center", "distribution_centre", "depot"),
    "store": ("store", "shop", "outlet", "branch"),
    "cluster": ("cluster", "store_cluster"),
    "port": ("port", "seaport"),
    "factory": ("factory", "plant", "mill"),
    "supplier": ("supplier", "vendor"),
    "airport": ("airport",),
}
ITEM_KINDS: Final[Mapping[str, tuple[str, ...]]] = {
    "sku": ("sku", "variant", "article"),
    "style": ("style", "product", "model"),
    "component": ("component", "material", "trim"),
}
ENTITY_SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "po_line": ("po_line", "purchase_order_line", "po_item", "purchase_order_item"),
    "inventory_position": ("inventory", "stock", "inventory_position", "stock_level", "stock_position"),
    "demand_forecast": ("forecast", "demand", "demand_forecast", "sales_forecast"),
    "allocation": ("allocation",),
    "transfer_order_line": ("transfer_order_line", "transfer_line", "to_line"),
    "sailing": ("sailing", "voyage"),
    "lane": ("lane", "route"),
    "lane_rate": ("lane_rate", "freight_rate", "rate"),
    "promotion": ("promotion", "promo"),
    "bom_line": ("bom_line", "bom"),
    "production_order": ("production_order", "work_order"),
    "size_curve": ("size_curve",),
    "online_order_line": ("online_order_line", "ecom_order_line", "web_order_line"),
}
ABBREVIATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "purchase_order": ("po",), "transfer_order": ("to",), "production_order": ("prod_order",),
}
PARENT_KIND: Final[Mapping[str, str]] = {"store": "cluster", "cluster": "dc", "factory": "supplier"}
"""``scn/1.0`` location hierarchy (``LocationRow.parent_code``)."""

COLUMN_SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "name": ("title", "label", "display_name"),
    "country": ("country_code", "market_code", "market"),
    "city": ("town",),
    "lat": ("latitude",),
    "lon": ("lng", "longitude"),
    "src_version": ("version", "row_version", "rev", "revision"),
    "forecast_version": ("version", "rev", "revision"),
    "qty_ordered": ("qty", "quantity", "ordered_qty", "order_qty"),
    "qty_received": ("received_qty", "received"),
    "on_hand": ("onhand", "stock", "on_hand_qty", "qty_on_hand"),
    "allocated": ("allocated_qty",),
    "reserved": ("reserved_qty",),
    "in_transit": ("intransit", "in_transit_qty"),
    "units": ("qty", "quantity", "forecast_units"),
    "qty": ("quantity", "units"),
    "iso_week": ("week", "year_week", "yearweek"),
    "ship_week": ("week", "delivery_week"),
    "retail_price_cents": ("price_cents", "retail_price", "price"),
    "unit_cost_cents": ("cost_cents", "unit_cost", "cost"),
    "eta_dc": ("eta", "eta_date", "arrival_date"),
    "eta_date": ("eta",),
    "ex_factory_date": ("ex_factory", "exfactory_date"),
    "dest_dc_code": ("dest_dc", "destination_dc", "dc_code"),
    "location_kind": ("location_type", "loc_type", "loc_kind"),
    "location_code": ("loc_code", "location"),
    "size_seq": ("size_order", "size_sort", "size_index"),
    "colour": ("color",),
    "family": ("category", "product_family"),
    "line_no": ("line", "line_number", "line_num"),
    "status": ("state",),
    "grade": ("tier", "store_grade"),
    "currency": ("currency_code",),
    "ship_date": ("shipping_date", "dispatch_date"),
    "to_code": ("transfer_order_code",),
    "capacity_teu": ("teu", "capacity"),
    "transit_days": ("transit_time", "lead_time_days"),
}
NATURAL_KEYS: Final[Mapping[str, tuple[tuple[str, ...], str]]] = {
    "po_line": (("po_code", "line_no"), "#"),
    "inventory_position": (("location_kind", "location_code", "sku_code"), "/"),
    "demand_forecast": (("style_code", "location_code", "iso_week"), "|"),
    "transfer_order_line": (("to_code", "sku_code"), "#"),
    "online_order_line": (("order_code", "sku_code"), "#"),
    "lane_rate": (("lane_code", "valid_from"), "|"),
    "bom_line": (("style_code", "component_code", "supplier_code"), "|"),
    "size_curve": (("family", "cluster_code", "size"), "|"),
}
"""The ``scn/1.0`` natural key of entities without a ``code`` (Merxa's key formats)."""
VERSION_COLUMNS: Final = frozenset({"src_version", "forecast_version"})

REQUEST_ID_PARAMS: Final = ("request_id", "idempotency_key", "client_request_id", "dedupe_key", "idempotency_token")
TARGET_PARAMS: Final = KEY_PARAMS + ("target", "target_ref", "record_key")
VERSION_PARAMS: Final = ("expected_version", "version", "if_match", "etag", "base_version")
FIELDS_PARAMS: Final = ("fields_json", "fields", "patch", "changes", "values", "updates", "data", "set")
FILTER_PARAMS: Final = ("filter_json", "filter", "filters", "where", "query")
ARG_SYNONYMS: Final[Mapping[str, tuple[str, ...]]] = {
    "dest_dc": ("dest_dc_code", "destination_dc"), "eta_dc": ("eta", "eta_date"), "qty": ("quantity",),
    "sku": ("sku_code",), "from_dc": ("from_dc_code", "source_dc"), "to_dc": ("to_dc_code", "destination_dc"),
    "supplier_code": ("supplier",), "factory_code": ("factory",), "lines": ("items", "line_items"),
}
GENERIC_UPDATE_OPS: Final[Mapping[str, tuple[str, Mapping[str, tuple[str, ...]]]]] = {
    "allocation.set_qty": ("allocation", {"qty": ("qty", "quantity")}),
    "po_line.reschedule": ("po_line", {"eta_dc": ("eta_dc", "eta", "eta_date")}),
    "po_line.change_qty": ("po_line", {"qty": ("qty_ordered", "qty", "quantity")}),
}
"""Ops that only set fields of one record (safe to express as a generic ``update`` call)."""

_LOOKUP_NAME_RE: Final = re.compile(LOOKUP_NAME_PATTERN)
_CANON_WEEK_RE: Final = re.compile(r"^[0-9]{4}-W[0-9]{2}$")
SHAPE_WORDS: Final[Mapping[str, str]] = {
    "code": "codes", "int": "integers", "int_text": "integer texts", "decimal_text": "decimal texts",
    "date": "dates", "datetime": "date-times", "iso_week": "ISO weeks", "bool": "booleans", "float": "numbers",
    "text": "texts", "object": "objects", "list": "lists", "empty": "empty",
}

# confidence (basis points)
C_CONST: Final = 9000
C_EXACT: Final = 8000
C_SYNONYM: Final = 7000
C_STRIPPED: Final = 6000
C_DERIVED: Final = 5000
C_JOIN: Final = 6000
C_LOW: Final = 3000


# ───────────────────────────── column types ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ColType:
    kind: Literal["str", "int", "float", "date", "week", "literal", "list", "bool"]
    options: tuple[str, ...] = ()
    version: bool = False


def _classify(ann: Any) -> ColType:
    origin = get_origin(ann)
    args = get_args(ann)
    if origin is Annotated:
        return _classify(args[0])
    if origin is Literal:
        return ColType("literal", tuple(str(a) for a in args))
    if origin in (Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        kinds = [_classify(a) for a in inner]
        if {k.kind for k in kinds} <= {"int", "float"} and len(kinds) > 1:
            return ColType("float")
        return kinds[0] if kinds else ColType("str")
    if origin is list:
        return ColType("list")
    if ann is bool:
        return ColType("bool")
    if ann is int:
        return ColType("int")
    if ann is float:
        return ColType("float")
    if ann is dt.date:
        return ColType("date")
    return ColType("str")


def col_type(entity: str, column: str) -> ColType:
    """The value type a column is validated against (weeks by name: ``iso_week`` / ``*_week``)."""
    t = _classify(SCN_ROW_MODELS[entity].model_fields[column].annotation)
    if t.kind == "str" and (column == "iso_week" or column.endswith("_week")):
        return ColType("week")
    if t.kind == "int" and column in VERSION_COLUMNS:
        return ColType("int", version=True)
    return t


def _singular(name: str) -> str:
    if name.endswith("ies") and len(name) > 4:
        return name[:-3] + "y"
    if name.endswith("s") and not name.endswith("ss") and len(name) > 3:
        return name[:-1]
    return name


def _stem(name: str) -> str:
    n = norm(name)
    for prefix in ("list_", "get_", "fetch_", "search_", "read_", "all_"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            break
    return _singular(n)


def entity_of(name: str) -> tuple[str, str | None] | None:
    """``(entity, kind)`` a source variant / tool name stands for (``None`` = none)."""
    n = _stem(name)
    for kind, names in LOCATION_KINDS.items():
        if n in names:
            return "location", kind
    for kind, names in ITEM_KINDS.items():
        if n in names:
            return "item", kind
    if n in ("location", "site"):
        return "location", None
    if n in ("item", "product_item"):
        return "item", None
    for entity, names in ENTITY_SYNONYMS.items():
        if n in names:
            return entity, None
    return None


def probe_variants(ops: Iterable[str]) -> list[str]:
    """Entity names a get-by-key probe should prefer (the generic-update ops' records)."""
    return [GENERIC_UPDATE_OPS[o][0] for o in ops if o in GENERIC_UPDATE_OPS]


# ───────────────────────────── sources ─────────────────────────────


@dataclass(slots=True)
class _Src:
    profile: SourceProfile
    name: str
    """Normalised variant (or tool stem): ``dc``, ``purchase_order`` …"""
    entity: str | None
    kind: str | None
    paths: list[PathProfile]
    by_norm: dict[str, PathProfile]

    @property
    def ref(self) -> str:
        return self.profile.ref

    @property
    def sampled(self) -> int:
        return self.profile.items_sampled

    def where(self) -> str:
        p = self.profile
        return f"{p.source.tool}({p.variant})" if p.variant else p.source.tool

    def path(self, name: str) -> PathProfile | None:
        return self.by_norm.get(name)

    def code_path(self) -> PathProfile | None:
        """A unique code-like path (``code`` / ``key`` / ``id``)."""
        for n in ("code", "key", "id"):
            p = self.by_norm.get(n)
            if p is not None and p.unique and p.shape in ("code", "int", "int_text"):
                return p
        return None

    def names(self) -> set[str]:
        out = {self.name, *ABBREVIATIONS.get(self.name, ())}
        return out


def _src(profile: SourceProfile) -> _Src:
    name = _stem(profile.variant or profile.source.tool)
    found = entity_of(name)
    by_norm: dict[str, PathProfile] = {}
    for p in sorted(profile.paths, key=lambda p: p.path.count(".")):
        last = norm(p.path.rsplit(".", 1)[-1])
        by_norm.setdefault(last, p)
    return _Src(profile=profile, name=name, entity=found[0] if found else None, kind=found[1] if found else None,
                paths=list(profile.paths), by_norm=by_norm)


def _overlap(a: PathProfile, b: PathProfile) -> int:
    return len(set(a.examples) & set(b.examples))


# ───────────────────────────── expressions ─────────────────────────────


@dataclass(slots=True)
class _Cand:
    expression: str
    confidence: int
    rationale: str
    paths: list[str] = field(default_factory=list)


def _words(p: PathProfile, sampled: int) -> str:
    if sampled <= 0:
        return f"declared as {SHAPE_WORDS.get(p.shape, p.shape)}"
    present = sampled - p.null_count
    return f"{present}/{sampled} sampled values are {SHAPE_WORDS.get(p.shape, p.shape)}"


def _value_expr(t: ColType, value: str, p: PathProfile, column: str) -> tuple[str, int] | None:
    """How ``value`` (a path or ``field(…)``) becomes the column's type: ``(expression, penalty)``."""
    s = p.shape
    if t.kind == "str":
        if s in ("code", "text", "int_text", "decimal_text", "date", "datetime", "iso_week"):
            return value, 0
        if s == "int":
            return f"str({value})", 500
        if s == "empty":
            return value, 2000
        return None
    if t.kind == "int":
        if s == "int":
            return (value, 0) if t.version else (f"int({value})", 0)
        if s == "int_text":
            return f"int({value})", 0
        if s in ("decimal_text", "float") and column.endswith("_cents"):
            return f"cents({value})", 500
        if s == "empty":
            return f"int({value})", 2000
        return None
    if t.kind == "date":
        if s == "date" and p.date_format:
            return f"date({value}, '{p.date_format}')", 500
        if s in ("date", "datetime"):
            return f"date({value})", 0
        if s == "empty":
            return f"date({value})", 2000
        return None
    if t.kind == "week":
        if s == "iso_week":
            canonical = bool(p.examples) and all(_CANON_WEEK_RE.fullmatch(e) for e in p.examples)
            return (value, 0) if canonical else (f"iso_week({value})", 0)
        if s in ("date", "datetime"):
            return f"iso_week({value})", 500
        if s == "empty":
            return f"iso_week({value})", 2000
        return None
    if t.kind == "literal":
        known = p.distinct_count <= len(p.examples) and p.distinct_count > 0
        if known and set(p.examples) <= set(t.options):
            return value, 0
        if known and {e.lower() for e in p.examples} <= set(t.options) and s in ("code", "text"):
            return f"lower({value})", 500
        return None
    if t.kind == "list":
        return (value, 0) if s == "list" else None
    if t.kind == "float":
        return (value, 0) if s in ("float", "int") else None
    return None


def expression_type_problem(entity: str, column: str, ast: Expr, paths: Mapping[str, PathProfile]) -> str | None:
    """Whether a proposed expression's outermost step contradicts the sampled shape of the path it
    reads (LLM post-validation): ``None`` when it fits or cannot be judged."""
    t = col_type(entity, column)
    inner = ast.args[0] if isinstance(ast, CallExpr) and ast.fn in ("first", "min", "max", "sum") and ast.args else ast
    wrapper = inner.fn if isinstance(inner, CallExpr) else None
    target = inner.args[0] if isinstance(inner, CallExpr) and inner.fn in ("int", "date", "cents", "iso_week",
                                                                           "str", "trim", "upper", "lower") else inner
    if not isinstance(target, PathExpr):
        return None
    p = paths.get(path_text(target))
    if p is None or p.shape == "empty":
        return None
    ok: Mapping[str | None, set[str]] = {
        "int": {"int", "int_text"}, "date": {"date", "datetime"}, "cents": {"int", "int_text", "decimal_text", "float"},
        "iso_week": {"iso_week", "date", "datetime"}, "str": {"code", "text", "int", "int_text", "decimal_text", "date",
                                                             "datetime", "iso_week", "bool"},
    }
    if wrapper in ok and p.shape not in ok[wrapper]:
        return f"{wrapper}() over {SHAPE_WORDS.get(p.shape, p.shape)}"
    if wrapper is None or wrapper in ("trim", "upper", "lower"):
        allowed = {"str": {"code", "text", "int_text", "decimal_text", "date", "datetime", "iso_week"},
                   "int": {"int"}, "date": {"date"}, "week": {"iso_week"}, "list": {"list"},
                   "float": {"float", "int"}, "literal": {"code", "text"}, "bool": {"bool"}}[t.kind]
        if p.shape not in allowed:
            return f"{SHAPE_WORDS.get(p.shape, p.shape)} for a {t.kind} column"
    return None


def _match(src: _Src, column: str) -> tuple[PathProfile, int, str] | None:
    """The best path for ``column`` by name: ``(path, confidence, why)``."""
    p = src.path(column)
    if p is not None:
        return p, C_EXACT, f"name matches {column}"
    for syn in COLUMN_SYNONYMS.get(column, ()):
        p = src.path(syn)
        if p is not None:
            return p, C_SYNONYM, f"{path_last(p)} is a usual name for {column}"
    stripped = re.sub(r"_(code|id|no|number)$", "", column)
    if stripped != column:
        p = src.path(stripped)
        if p is not None:
            return p, C_STRIPPED, f"{path_last(p)} matches {column} without its suffix"
    return None


def path_last(p: PathProfile) -> str:
    return p.path.rsplit(".", 1)[-1]


# ───────────────────────────── one entity mapping ─────────────────────────────


@dataclass(slots=True)
class _Draft:
    src: _Src
    entity: str
    kind: str | None
    fields: dict[str, _Cand] = field(default_factory=dict)
    group_by: list[str] | None = None
    joins: set[str] = field(default_factory=set)
    rationale: str = ""
    derived: bool = False


def _plain(expression: str) -> str | None:
    """The path text when ``expression`` is a plain path or a conversion of one (for keys)."""
    ast = parse_expression(expression)
    if isinstance(ast, PathExpr):
        return path_text(ast)
    if isinstance(ast, CallExpr) and ast.fn in ("int", "str", "trim") and isinstance(ast.args[0], PathExpr):
        return path_text(ast.args[0])
    return None


def _columns(d: _Draft, notes: Notes) -> None:
    src, entity = d.src, d.entity
    for column in SCN_COLUMNS[entity]:
        if column in ("source_id",) or column in d.fields:
            continue
        t = col_type(entity, column)
        if column == "kind" and d.kind is not None:
            d.fields[column] = _Cand(f"'{d.kind}'", C_CONST, f"the {src.name} records of {src.where()}")
            continue
        if entity == "item" and column == "style_code" and d.kind == "style" and "code" in d.fields:
            code = d.fields["code"]
            d.fields[column] = _Cand(code.expression, C_SYNONYM, "a style is its own style", list(code.paths))
            continue
        if entity == "location" and column == "parent_code" and d.kind in PARENT_KIND:
            parent = PARENT_KIND[d.kind]
            p = next((src.path(n) for n in (f"{parent}_code", parent, "parent_code", "parent") if src.path(n)), None)
            if p is not None and p.shape in ("code", "text", "int_text", "empty"):
                d.fields[column] = _Cand(p.path, C_SYNONYM, f"a {d.kind}'s parent is its {parent}; "
                                         f"{_words(p, src.sampled)}", [p.path])
            continue
        m = _match(src, column)
        if m is None:
            continue
        p, conf, why = m
        conv = _value_expr(t, p.path, p, column)
        if conv is None:
            if t.kind != "literal":
                notes.add("type_mismatch", f"{src.where()}.{column}",
                          f"{path_last(p)} looks like {column} but its sampled values are "
                          f"{SHAPE_WORDS.get(p.shape, p.shape)}")
            continue
        expression, penalty = conv
        d.fields[column] = _Cand(expression, max(C_LOW, conf - penalty), f"{why}; {_words(p, src.sampled)}", [p.path])
    _location_pair(d)


def _location_pair(d: _Draft) -> None:
    """``location_kind`` + ``location_code`` from per-kind code paths (``store_code`` …)."""
    entity, src = d.entity, d.src
    if "location_kind" not in SCN_COLUMNS[entity] or "location_kind" in d.fields:
        return
    options = col_type(entity, "location_kind").options
    found = [(o, src.path(f"{o}_code")) for o in options]
    # an all-null path (e.g. store codes absent from the sampled page) still says the field exists
    present = [(o, p) for o, p in found if p is not None and p.shape in ("code", "int_text", "text", "empty")]
    if not present:
        return
    if len(present) == 1:
        o, p = present[0]
        d.fields["location_kind"] = _Cand(f"'{o}'", C_DERIVED, f"only {o} codes ({path_last(p)}) are present")
        d.fields.setdefault("location_code", _Cand(p.path, C_DERIVED, f"the {o} code", [p.path]))
        return
    present.sort(key=lambda op: -op[1].null_count)  # sometimes-null (finer) codes first, the always-set one last
    kind_expr = f"'{present[-1][0]}'"
    for o, p in reversed(present[:-1]):
        kind_expr = f"if_present({p.path}, '{o}', {kind_expr})"
    code_expr = f"coalesce({', '.join(p.path for _o, p in present)})"
    names = ", ".join(path_last(p) for _o, p in present)
    d.fields["location_kind"] = _Cand(kind_expr, C_DERIVED, f"the most specific of {names} that is set")
    d.fields["location_code"] = _Cand(code_expr, C_DERIVED, f"the most specific of {names} that is set",
                                      [p.path for _o, p in present])


def _source_id(d: _Draft) -> None:
    entity = d.entity
    if entity in ("location", "item") and d.kind is not None and "code" in d.fields:
        code = d.fields["code"]
        if _plain(code.expression) is not None or d.group_by:
            d.fields["source_id"] = _Cand(f"'{d.kind}:' + {code.expression}", min(code.confidence, C_SYNONYM),
                                          f"'{d.kind}:' + code: the kind-prefixed code (Merxa's tier-B source ids)",
                                          list(code.paths))
            return
    if "code" in SCN_COLUMNS[entity] and "code" in d.fields:
        code = d.fields["code"]
        p = d.src.path(path_last_text(code.paths[0])) if code.paths else None
        if p is not None and p.unique:
            d.fields["source_id"] = _Cand(code.expression, min(code.confidence, C_SYNONYM),
                                          "the record's code (unique in the sample)", list(code.paths))
            return
    natural = NATURAL_KEYS.get(entity)
    if natural is not None:
        cols, sep = natural
        parts = []
        for c in cols:
            cand = d.fields.get(c)
            if cand is None:
                parts = []
                break
            plain = _plain(cand.expression)
            parts.append(plain if plain is not None else cand.expression)
        if parts:
            d.fields["source_id"] = _Cand(f" + '{sep}' + ".join(parts), C_SYNONYM - 1000,
                                          f"the natural key of {entity} ({', '.join(cols)}) joined with '{sep}'",
                                          [x for c in cols for x in d.fields[c].paths])
            return
    for n in ("key", "id", "code"):
        p = d.src.path(n)
        if p is not None and p.unique and p.shape in ("code", "int", "int_text"):
            expr = p.path if p.shape == "code" else f"str({p.path})"
            d.fields["source_id"] = _Cand(expr, C_SYNONYM - 1000, f"{path_last(p)} is unique in the sample", [p.path])
            return


def path_last_text(text: str) -> str:
    return norm(text.rsplit(".", 1)[-1])


def _joins(d: _Draft, srcs: Sequence[_Src], lookups: dict[str, ProposedLookup]) -> None:
    """Record lookups for columns the item lacks (``po_code`` → the purchase order's supplier)."""
    entity, src = d.entity, d.src
    missing = [c for c in SCN_COLUMNS[entity] if c not in d.fields and c not in ("source_id", "kind")]
    if not missing:
        return
    for target in srcs:
        if target is src or target.entity == entity and target.kind == d.kind:
            continue
        code = target.code_path()
        if code is None:
            continue
        join = next((src.path(f"{n}_code") or src.path(f"{n}_id") for n in sorted(target.names())
                     if src.path(f"{n}_code") or src.path(f"{n}_id")), None)
        if join is None:
            continue
        table = target.name if _LOOKUP_NAME_RE.fullmatch(target.name) else None
        if table is None:
            continue
        overlap = _overlap(join, code)
        used = False
        for column in list(missing):
            m = _match(target, column)
            if m is None:
                continue
            p, conf, why = m
            value = f"field('{table}', {join.path}, '{p.path}')"
            conv = _value_expr(col_type(entity, column), value, p, column)
            if conv is None:
                continue
            expression, penalty = conv
            confidence = min(conf, C_JOIN) - penalty + (1000 if overlap else 0)
            share = f"{overlap}/{len(join.examples)} sampled codes found" if join.examples else "no codes to compare"
            d.fields[column] = _Cand(expression, max(C_LOW, confidence),
                                     f"{table} record joined on {path_last(join)} ({share}); {why}",
                                     [join.path])
            missing.remove(column)
            used = True
        if used:
            d.joins.add(table)
            if table not in lookups:
                lookups[table] = ProposedLookup(
                    name=table, table=RecordLookup(kind="record", source=target.profile.source, key=code.path),
                    source_ref=target.ref, confidence_bp=C_JOIN + (1000 if overlap else 0), origin="heuristic",
                    rationale=f"{target.where()} keyed by its unique {path_last(code)}")


def _derived_parent(d: _Draft, have_kinds: set[str]) -> _Draft | None:
    """A ``group_by`` parent location entity for a parent kind without a source of its own."""
    if d.entity != "location" or d.kind not in PARENT_KIND:
        return None
    parent = PARENT_KIND[d.kind]
    if parent in have_kinds:
        return None
    src = d.src
    key = src.path(f"{parent}_code")
    if key is None or key.unique or key.shape not in ("code", "int_text"):
        return None
    out = _Draft(src=src, entity="location", kind=parent, group_by=[key.path], derived=True,
                 rationale=f"one {parent} per distinct {path_last(key)} of the {src.name} records")
    out.fields["kind"] = _Cand(f"'{parent}'", C_LOW, f"derived {parent} rows")
    out.fields["code"] = _Cand(key.path, C_LOW, f"the distinct {path_last(key)} values", [key.path])
    grand = PARENT_KIND.get(parent)
    if grand is not None:
        gp = next((p for p in src.paths if path_last_text(p.path) == f"{grand}_code"
                   or path_last_text(p.path).endswith(f"_{grand}_code")), None)
        if gp is not None:
            out.fields["parent_code"] = _Cand(f"first({gp.path})", C_LOW,
                                              f"a {parent}'s parent is a {grand}: the first record's "
                                              f"{path_last(gp)}", [gp.path])
    out.fields["src_version"] = _Cand("0", C_LOW, "derived rows carry no vendor version (0)")
    out.fields["source_id"] = _Cand(f"'{parent}:' + {key.path}", C_LOW,
                                    f"'{parent}:' + code: the kind-prefixed code", [key.path])
    return out


def _entity(d: _Draft) -> ProposedEntity:
    cols = SCN_COLUMNS[d.entity]
    fields = [ProposedField(column=c, expression=d.fields[c].expression, confidence_bp=d.fields[c].confidence,
                            origin="heuristic", rationale=d.fields[c].rationale[:300])
              for c in cols if c in d.fields]
    missing = [c for c in required_scn_columns(d.entity) if c not in d.fields]
    conf = round(sum(f.confidence_bp for f in fields) / len(fields)) if fields else 0
    if missing:
        conf = min(conf, 2000)
    if d.derived:
        conf = min(conf, C_LOW)
    rationale = d.rationale or (f"{d.src.where()} holds {d.src.name} records" +
                                (f" (kind {d.kind})" if d.kind else ""))
    return ProposedEntity(entity=d.entity, source_ref=d.src.ref, source=d.src.profile.source,  # type: ignore[arg-type]
                          group_by=d.group_by, dedupe=False, fields=fields, missing_required=missing,
                          confidence_bp=conf, origin="heuristic", rationale=rationale[:300])


# ───────────────────────────── read ─────────────────────────────


def _read(profiles: Sequence[SourceProfile], request_entities: Sequence[str] | None,
          notes: Notes) -> tuple[ProposedRead, list[str]]:
    srcs = [_src(p) for p in profiles]
    wanted = set(request_entities) if request_entities else set(SCN_ENTITIES)
    lookups: dict[str, ProposedLookup] = {}
    drafts: list[_Draft] = []
    for s in srcs:
        if s.entity is None or s.entity not in wanted or not s.paths:
            continue
        d = _Draft(src=s, entity=s.entity, kind=s.kind)
        _columns(d, notes)
        drafts.append(d)
    have_kinds = {d.kind for d in drafts if d.entity == "location" and d.kind}
    derived: list[_Draft] = []
    for d in drafts:
        extra = _derived_parent(d, have_kinds | {x.kind for x in derived if x.kind})
        if extra is not None and "location" in wanted:
            derived.append(extra)
    out: list[ProposedEntity] = []
    for d in [*drafts, *derived]:
        if not d.derived:
            _joins(d, srcs, lookups)
            _source_id(d)
        out.append(_entity(d))
    used_refs = {e.source_ref for e in out} | {lk.source_ref for lk in lookups.values()}
    for s in srcs:
        if s.ref not in used_refs:
            notes.add("source_unused", s.where(), f"{s.where()} fits no scn/1.0 entity; map it by hand if needed")
    for i, e in enumerate(out):
        if e.missing_required:
            notes.add("required_column_unmapped", f"entities[{i}] ({e.entity})",
                      f"no sampled field fits the required column(s) {', '.join(e.missing_required)}",
                      severity="warning")
    mapped = {e.entity for e in out if not e.missing_required}
    unmapped = [e for e in SCN_ENTITIES if e in wanted and e not in mapped]
    if request_entities:
        for e in unmapped:
            notes.add("entity_unmapped", e, f"no sampled source fits {e}", severity="warning")
    elif unmapped:
        notes.add("entity_unmapped", None, f"{len(unmapped)} scn/1.0 entities have no fitting source: "
                  f"{', '.join(unmapped)}")
    return ProposedRead(entities=out, lookups=list(lookups.values())), unmapped


# ───────────────────────────── action ─────────────────────────────


def _tokens(name: str) -> set[str]:
    return set(norm(name).split("_")) - {""}


def _op_token_sets(op: str) -> list[set[str]]:
    record, verb = op.split(".", 1)
    words = [record, *verb.split("_")]
    variants = [words]
    for full, abbrs in ABBREVIATIONS.items():
        if record in abbrs:
            variants.append([full, *verb.split("_")])
    return [set("_".join(w).split("_")) for w in variants]


def _payload_fields(op: str) -> dict[str, bool]:
    """Payload field → structured (list / object) for ``op``."""
    model = OP_PAYLOAD_MODELS[op]
    out: dict[str, bool] = {}
    for name, info in model.model_fields.items():
        t = _classify(info.annotation)
        out[name] = t.kind == "list" or (t.kind == "str" and _is_model(info.annotation))
    return out


def _is_model(ann: Any) -> bool:
    from pydantic import BaseModel

    return isinstance(ann, type) and issubclass(ann, BaseModel)


def _has(op: str, root: str) -> bool:
    return op_takes(op, root)


def _payload_arg(p: CatalogueParam, fields: Mapping[str, bool]) -> tuple[str, Any] | None:
    n = norm(p.name)
    stem = n[:-5] if n.endswith("_json") else n
    for f, structured in fields.items():
        if stem == f or stem in ARG_SYNONYMS.get(f, ()) or f in ARG_SYNONYMS.get(stem, ()):
            value = f"{{{{payload.{f}}}}}"
            if n.endswith("_json") or (structured and p.is_string):
                return f, {"$json": value}
            return f, value
    return None


@dataclass(slots=True)
class _OpDraft:
    op: str
    tool: CatalogueTool
    args: dict[str, Any]
    request_id_arg: str | None
    confidence: int
    rationale: str
    generic: bool = False
    fields: dict[str, str] = field(default_factory=dict)
    """payload field → item field (read-back match)."""
    variant: str | None = None


def _specific(op: str, tools: Sequence[CatalogueTool]) -> _OpDraft | None:
    fields = _payload_fields(op)
    best: tuple[int, CatalogueTool] | None = None
    for t in tools:
        if not t.writable:
            continue
        tt = _tokens(t.name)
        for want in _op_token_sets(op):
            if want <= tt and (best is None or len(tt - want) < best[0]):
                best = (len(tt - want), t)
    if best is None:
        return None
    tool = best[1]
    args: dict[str, Any] = {}
    rid: str | None = None
    mapped: dict[str, str] = {}
    for p in tool.params:
        n = norm(p.name)
        if n in REQUEST_ID_PARAMS:
            rid = p.name
            continue
        if _has(op, "target_ref") and n in TARGET_PARAMS:
            args[p.name] = "{{target_ref}}"
            continue
        if _has(op, "expected_version") and n in VERSION_PARAMS:
            args[p.name] = "{{expected_version}}"
            continue
        found = _payload_arg(p, fields)
        if found is not None:
            f, value = found
            args[p.name] = value
            if not fields[f]:
                mapped[f] = f
            continue
        if p.required:
            return None
    if not args:
        return None
    return _OpDraft(op=op, tool=tool, args=args, request_id_arg=rid, confidence=7000,
                    rationale=f"{tool.name} is named after {op}; arguments matched by name", fields=mapped)


def _variant_for(record: str, tool: CatalogueTool, srcs: Sequence[_Src]) -> str | None:
    pool = list(tool.variants) or [s.profile.variant for s in srcs if s.profile.variant]
    for v in pool:
        if v is None:
            continue
        found = entity_of(v)
        if (found and found[0] == record) or _stem(v) == record:
            return v
    return None


def _generic(op: str, tools: Sequence[CatalogueTool], srcs: Sequence[_Src]) -> _OpDraft | None:
    if op not in GENERIC_UPDATE_OPS:
        return None
    record, wanted = GENERIC_UPDATE_OPS[op]
    for tool in tools:
        if not tool.writable or tool.variant_param is None:
            continue
        key = next((p for p in tool.params if norm(p.name) in KEY_PARAMS), None)
        fparam = next((p for p in tool.params if norm(p.name) in FIELDS_PARAMS), None)
        if key is None or fparam is None:
            continue
        variant = _variant_for(record, tool, srcs)
        if variant is None:
            continue
        profile = next((s for s in srcs if s.profile.variant == variant), None)
        mapping: dict[str, str] = {}
        for f, names in wanted.items():
            item = next((n for n in names if profile is not None and profile.path(n) is not None), None)
            mapping[f] = item or names[0]
        patch = {item: f"{{{{payload.{f}}}}}" for f, item in mapping.items()}
        args: dict[str, Any] = {tool.variant_param: variant, key.name: "{{target_ref}}",
                                fparam.name: {"$json": patch} if fparam.is_string else patch}
        rid = None
        for p in tool.params:
            n = norm(p.name)
            if p.name in args:
                continue
            if n in REQUEST_ID_PARAMS:
                rid = p.name
            elif n in VERSION_PARAMS:
                args[p.name] = "{{expected_version}}"
            elif p.required:
                args = {}
                break
        if not args:
            continue
        seen = "sampled" if profile is not None else "not sampled"
        return _OpDraft(op=op, tool=tool, args=args, request_id_arg=rid, confidence=C_LOW, generic=True,
                        rationale=f"generic update of the {variant} record ({seen}): sets "
                                  f"{', '.join(patch)} by key with the expected version",
                        fields=mapping, variant=variant)
    return None


def _readback(d: _OpDraft, tools: Sequence[CatalogueTool], srcs: Sequence[_Src],
              probes: Sequence[Probe]) -> dict[str, Any] | None:
    if not d.fields:
        return None
    match = {f"$.{item}": f"{{{{payload.{f}}}}}" for f, item in d.fields.items()}
    if _has(d.op, "target_ref"):
        record = d.op.split(".", 1)[0]
        for probe in probes:
            tool = next((t for t in tools if t.name == probe.tool and t.readable), None)
            if tool is None:
                continue
            args: dict[str, Any] = {probe.key_param: "{{target_ref}}"}
            if probe.variant_param:
                variant = d.variant or _variant_for(record, tool, srcs)
                if variant is None:
                    continue
                args[probe.variant_param] = variant
            return {"tool": tool.name, "args": args, "items_path": None if probe.items_path == "$" else probe.items_path,
                    "match": match}
        return None
    record = d.op.split(".", 1)[0]
    for s in srcs:
        found = entity_of(s.name)
        if s.name != record and not (found and found[0] == record) and record not in ABBREVIATIONS.get(s.name, ()):
            continue
        tool = next((t for t in tools if t.name == s.profile.source.tool and t.readable), None)
        if tool is None:
            continue
        fparam = next((p for p in tool.params if norm(p.name) in FILTER_PARAMS), None)
        if fparam is None:
            continue
        filt = {item: f"{{{{payload.{f}}}}}" for f, item in d.fields.items() if s.path(item) is not None}
        if not filt:
            continue
        args = dict(s.profile.source.args)
        args[fparam.name] = {"$json": filt} if fparam.is_string else filt
        if s.profile.source.page_size_param:
            args[s.profile.source.page_size_param] = s.profile.source.page_size
        return {"tool": tool.name, "args": args, "items_path": s.profile.source.items_path,
                "match": {f"$.{item}": v for item, v in filt.items()}}
    return None


def _op(d: _OpDraft, tools: Sequence[CatalogueTool], srcs: Sequence[_Src], probes: Sequence[Probe],
        notes: Notes) -> ProposedOp | None:
    confirmed = d.tool.classification.idempotency
    idem = confirmed
    rid = d.request_id_arg
    if idem is Idempotency.ENFORCED and rid is None:
        idem = Idempotency.BEST_EFFORT
    readback = _readback(d, tools, srcs, probes)
    if idem is not Idempotency.ENFORCED and readback is None:
        notes.add("op_unmapped", d.op, f"{d.op}: {d.tool.name} fits, but no read-back tool was found (needed for "
                  f"{idem.value} idempotency)")
        return None
    try:
        mapping = ActionOpMapping.model_validate({"op": d.op, "tool": d.tool.name, "args": d.args,
                                                  "idempotency": idem.value, "request_id_arg": rid,
                                                  "readback": readback})
    except ValueError as exc:
        notes.add("op_unmapped", d.op, f"{d.op}: the drafted mapping is not valid ({str(exc)[:160]})")
        return None
    conf = d.confidence - (0 if readback is not None else 2000)
    rationale = d.rationale + (f"; read back with {readback['tool']}" if readback else "") + \
        f"; idempotency {idem.value} as confirmed for {d.tool.name}"
    if d.generic:
        notes.add("generic_update_tool", d.op, f"{d.op} uses the generic {d.tool.name}: check the vendor's rules "
                  "for this record before relying on it", severity="warning")
    return ProposedOp(mapping=mapping, confidence_bp=max(0, min(conf, 10_000)), origin="heuristic",
                      rationale=rationale[:300], generic=d.generic)


def _action(tools: Sequence[CatalogueTool], profiles: Sequence[SourceProfile], probes: Sequence[Probe],
            ops: Sequence[str] | None, notes: Notes) -> tuple[ProposedAction, list[str]]:
    srcs = [_src(p) for p in profiles]
    wanted = list(ops) if ops else list(OP_PAYLOAD_MODELS)
    out: list[ProposedOp] = []
    for op in wanted:
        if op not in OP_PAYLOAD_MODELS:
            continue
        d = _specific(op, tools) or _generic(op, tools, srcs)
        if d is None:
            continue
        proposed = _op(d, tools, srcs, probes, notes)
        if proposed is not None:
            out.append(proposed)
    mapped = {o.mapping.op for o in out}
    unmapped = [op for op in wanted if op not in mapped]
    if ops:
        for op in unmapped:
            notes.add("op_unmapped", op, f"no tool fits {op}: it stays a guided step")
    elif unmapped:
        notes.add("op_unmapped", None, f"{len(unmapped)} ops have no fitting tool and stay guided steps: "
                  f"{', '.join(unmapped)}")
    return ProposedAction(ops=out), unmapped


# ───────────────────────────── entry point ─────────────────────────────


@dataclass(slots=True)
class HeuristicResult:
    read: ProposedRead | None
    action: ProposedAction | None
    unmapped_entities: list[str]
    unmapped_ops: list[str]


def propose(profiles: Sequence[SourceProfile], tools: Sequence[CatalogueTool], *, kinds: Sequence[str],
            entities: Sequence[str] | None = None, ops: Sequence[str] | None = None,
            probes: Sequence[Probe] = (), notes: Notes) -> HeuristicResult:
    """The rule-based drafts (see the module docstring)."""
    read = action = None
    unmapped_entities: list[str] = []
    unmapped_ops: list[str] = []
    if "read" in kinds:
        read, unmapped_entities = _read(profiles, entities, notes)
    if "action" in kinds:
        action, unmapped_ops = _action(tools, profiles, probes, ops, notes)
    return HeuristicResult(read=read, action=action, unmapped_entities=unmapped_entities, unmapped_ops=unmapped_ops)


def paths_of(profile: SourceProfile) -> dict[str, PathProfile]:
    """Canonical path text → profile (post-validation of LLM expressions)."""
    return {path_text(parse_path(p.path)): p for p in profile.paths}


def expression_reads(expression: str, *, aggregates: bool = False) -> list[str]:
    """Canonical texts of the item paths an expression reads (``field()`` record paths excluded)."""
    return [path_text(p) for p in expression_paths(parse_expression(expression, aggregates=aggregates))]


def is_const(expression: str) -> bool:
    return isinstance(parse_expression(expression), ConstExpr)
