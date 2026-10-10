"""M7 contracts (``docs/contracts/110-m7-backoffice-generic.md``): four-eyes change requests,
users, custom specialists + playground, tier-A read/action mappings and the safe expression
grammar (incl. a fuzz), snapshot consistency + advisory blocks, per-op commit ledger,
connector capability summary."""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
from typing import Any

import pytest
from pydantic import ValidationError

from examples import good_props, provenance
from keelson_contracts import agui
from keelson_contracts.admin import (
    CHANGE_REQUEST_TTL,
    GUARD_REASONS_BY_KIND,
    PRODUCT_ROLES,
    AdminChangeRequest,
    AdminUser,
    AdminUserCreate,
    AdminUserCreated,
    AdminUserPatch,
    ChangeDiffEntry,
    ChangeRequestDecision,
    PasswordSetup,
    change_request_decision_error,
    grantable_roles,
    user_change_violations,
)
from keelson_contracts.agents import (
    RESERVED_SPECIALIST_SLUGS,
    CustomSpecialistSpec,
    PlaygroundRequest,
    PlaygroundResult,
    custom_specialist_violations,
    custom_specialists_violations,
    is_custom_specialist_slug,
    is_pro_model,
    quarantined_tool_refs,
)
from keelson_contracts.blocks import (
    Advisory,
    NarrativeProps,
    RecommendedOptionProps,
    ScenarioCompareProps,
    advisory_for,
    block_to_wire,
    make_block,
    parse_block,
)
from keelson_contracts.commit import (
    CommitLedgerEntry,
    atomic_group_bulk_allowed,
    ledger_next_action,
)
from keelson_contracts.connectors import (
    ConnectorCapabilities,
    ConnectorTier,
    ContentTrust,
    Egress,
    Idempotency,
    ToolAccess,
    ToolClassification,
    summarize_capabilities,
)
from keelson_contracts.mapping import (
    EXPRESSION_FUNCTIONS,
    ActionDryRunRequest,
    ActionDryRunResult,
    ActionMapping,
    CallExpr,
    ConcatExpr,
    ConstExpr,
    DryRunReadback,
    EntityMapping,
    ExpressionError,
    PathExpr,
    ReadMapping,
    ReadSource,
    ScnEntityName,
    TemplateError,
    action_mapping_violations,
    expression_lookups,
    expression_paths,
    parse_expression,
    parse_path,
    required_scn_columns,
    template_refs,
    unmapped_ops,
)
from keelson_contracts.scn import CRAWLED_ADVISORY_TEXT, SCN_ENTITIES, AsOfRange, SnapshotLabel

T0 = dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.UTC)
UUID_A = "0199c1a2-7b3c-7d4e-8f50-0123456789ab"
UUID_B = "0199c1a2-7b3c-7d4e-8f50-0123456789ac"
KB_1 = "0199c1a2-7b3c-7d4e-8f50-00000000000b"

# ──────────────────────────────── expressions ───────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$", PathExpr(())),
        ("$.code", PathExpr(("code",))),
        ("$.a.b[0]['x y']", PathExpr(("a", "b", 0, "x y"))),
        ('$["key"]', PathExpr(("key",))),
        ("'store:' + $.code", ConcatExpr((ConstExpr("store:"), PathExpr(("code",))))),
        ("int($.qty)", CallExpr("int", (PathExpr(("qty",)),))),
        ("str( $.line_no )", CallExpr("str", (PathExpr(("line_no",)),))),
        ("date($.eta, 'DD/MM/YYYY')", CallExpr("date", (PathExpr(("eta",)), ConstExpr("DD/MM/YYYY")))),
        (
            "lookup('dc_country', $.dc, 'XX')",
            CallExpr("lookup", (ConstExpr("dc_country"), PathExpr(("dc",)), ConstExpr("XX"))),
        ),
        ("coalesce($.a, $.b, null)", CallExpr("coalesce", (PathExpr(("a",)), PathExpr(("b",)), ConstExpr(None)))),
        (
            "if_present($.store_code, 'store', 'cluster')",
            CallExpr("if_present", (PathExpr(("store_code",)), ConstExpr("store"), ConstExpr("cluster"))),
        ),
        ("-5", ConstExpr(-5)),
        ("0", ConstExpr(0)),
        ("true", ConstExpr(True)),
        ("false", ConstExpr(False)),
        ('"a\\"b"', ConstExpr('a"b')),
        ("'it\\'s'", ConstExpr("it's")),
        ("($.a)", PathExpr(("a",))),
        (" ( $.po_code + '#' + str($.line_no) ) ",
         ConcatExpr((PathExpr(("po_code",)), ConstExpr("#"), CallExpr("str", (PathExpr(("line_no",)),))))),
        ("9007199254740991", ConstExpr(9007199254740991)),
    ],
)
def test_expression_parses(text: str, expected: Any) -> None:
    assert parse_expression(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "eval('1')",
        "exec('x')",
        "open('/etc/passwd')",
        "getattr($, 'x')",
        "$.__class__",
        "$.a.__dict__",
        "$['__globals__']",
        "$.a()",
        "x",
        "lambda: 1",
        "1.5",
        "1e3",
        "01",
        "-",
        "$.a +",
        "+ $.a",
        "int()",
        "int(1, 2)",
        "coalesce($.a)",
        "if_present($.a, 1)",
        "lookup($.t, $.k)",
        "lookup('T', $.k)",
        "lookup('t', $.k, $.d)",
        "date($.d, 'YYYY-MM')",
        "date($.d, 'YYYY-YYYY-MM')",
        "date($.d, $.fmt)",
        "'abc",
        "'a\\nb'",
        "$.a[-1]",
        "$ .a",
        "$[1.5]",
        "$[]",
        "$.",
        "$.a; $.b",
        "$.a or $.b",
        "$.a * 2",
        "{}",
        "[1]",
        "''.join",
        "int",
        "int ($.a",
        "9007199254740992",
        "$.a\x00",
        "é",
        "٣",
        "'\ud800'",
        "",
        "   ",
        ")",
    ],
)
def test_expression_rejects(text: str) -> None:
    with pytest.raises(ExpressionError):
        parse_expression(text)


def test_expression_limits() -> None:
    with pytest.raises(ExpressionError, match="longer than 500"):
        parse_expression("'" + "a" * 499 + "'" + "")  # 501 chars
    parse_expression("$" + ".a" * 16)
    with pytest.raises(ExpressionError, match="16 segments"):
        parse_expression("$" + ".a" * 17)
    with pytest.raises(ExpressionError, match="nested deeper"):
        parse_expression("(" * 17 + "$" + ")" * 17)
    with pytest.raises(ExpressionError, match="nested deeper"):
        parse_expression("int(" * 17 + "$" + ")" * 17)
    with pytest.raises(ExpressionError, match="64 nodes"):
        parse_expression(" + ".join(["$.a"] * 65))
    with pytest.raises(ExpressionError, match="200 characters"):
        parse_expression("'" + "a" * 201 + "'")


def test_expression_helpers() -> None:
    ast = parse_expression("lookup('a', $.x) + coalesce(lookup('b', $.y.z), '')")
    assert expression_lookups(ast) == {"a", "b"}
    assert expression_paths(ast) == [PathExpr(("x",)), PathExpr(("y", "z"))]
    assert set(EXPRESSION_FUNCTIONS) == {"int", "str", "date", "lookup", "coalesce", "if_present",
                                         "field", "cents", "iso_week", "trim", "upper", "lower"}  # M8 additions


def test_path_parser() -> None:
    assert parse_path("$.items") == PathExpr(("items",))
    assert parse_path("$") == PathExpr(())
    for bad in (" $.items", "$.items ", "items", "$.a + $.b", "int($.a)", "$..a", "$[*]", "$.a[?(@.x)]"):
        with pytest.raises(ExpressionError):
            parse_path(bad)


_FUZZ_ALPHABET = list("$.[]()'\",+-_ \t\n0123456789abcdefgijklnoprstuxyz") + [
    "int(", "str(", "date(", "lookup(", "coalesce(", "if_present(", "__", "eval(", "import", "\\", "\x00", "é",
    "null", "true", "{{", "}}", ";", ":", "*", "@",
]


def _check_ast(node: Any) -> None:
    if isinstance(node, PathExpr):
        for seg in node.segments:
            assert isinstance(seg, int) or not seg.startswith("__")
    elif isinstance(node, CallExpr):
        assert node.fn in EXPRESSION_FUNCTIONS
        lo, hi = EXPRESSION_FUNCTIONS[node.fn]
        assert lo <= len(node.args) <= hi
        for a in node.args:
            _check_ast(a)
    elif isinstance(node, ConcatExpr):
        assert len(node.parts) >= 2
        for p in node.parts:
            _check_ast(p)
    else:
        assert isinstance(node, ConstExpr)
        assert node.value is None or isinstance(node.value, (str, int, bool))
        assert not isinstance(node.value, float)


def test_expression_fuzz_never_crashes_or_escapes() -> None:
    """Random token soup and mutations of valid expressions either parse into a safe AST (only
    known functions, no dunder segments, no floats) or raise ExpressionError - never anything
    else (no eval / attribute access is reachable)."""
    rng = random.Random(20261008)
    seeds = [
        "'store:' + $.code",
        "lookup('dc_country', $.dc, 'XX')",
        "date($.eta, 'DD/MM/YYYY')",
        "if_present($.store_code, 'store', 'cluster')",
        "coalesce(int($.a), -1)",
        "$.a.b[0]['k']",
    ]
    parsed = 0
    for i in range(6000):
        if i % 2:
            text = "".join(rng.choice(_FUZZ_ALPHABET) for _ in range(rng.randint(1, 30)))
        else:
            chars = list(rng.choice(seeds))
            for _ in range(rng.randint(1, 4)):
                op = rng.randint(0, 2)
                pos = rng.randint(0, len(chars))
                if op == 0:
                    chars.insert(pos, rng.choice(_FUZZ_ALPHABET))
                elif op == 1 and chars:
                    del chars[min(pos, len(chars) - 1)]
                elif chars:
                    chars[min(pos, len(chars) - 1)] = rng.choice(_FUZZ_ALPHABET)
            text = "".join(chars)
        try:
            ast = parse_expression(text)
        except ExpressionError:
            continue
        parsed += 1
        _check_ast(ast)
    assert parsed > 100  # the fuzz reaches the success path too


# ───────────────────────────────── templates ────────────────────────────────

ROOTS = frozenset({"payload", "target_ref", "expected_version", "request_id", "op"})


def test_template_refs() -> None:
    tpl = {
        "type": "allocation",
        "key": "{{target_ref}}",
        "fields_json": {"$json": {"qty": "{{ payload.qty }}"}},
        "expected_version": "{{expected_version}}",
        "note": "set {{payload.qty}} on {{target_ref}}",
        "lines": [{"sku": "{{payload.lines.0.sku}}"}],
    }
    refs = template_refs(tpl, roots=ROOTS)
    assert ("target_ref",) in refs and ("payload", "qty") in refs and ("payload", "lines", "0", "sku") in refs


@pytest.mark.parametrize(
    ("tpl", "match"),
    [
        ({"x": 1.5}, "floats"),
        ({"x": "{{tenant}}"}, "unknown placeholder root"),
        ({"x": "{{payload.__class__}}"}, "__"),
        ({"x": "{{payload.qty"}, "unterminated"),
        ({"x": "a }} b"}, "without"),
        ({"x": "{{ {{payload}} }}"}, "nested"),
        ({"x": "{{payload..qty}}"}, "invalid placeholder"),
        ({"x": "{{payload.qty()}}"}, "invalid placeholder"),
        ({"x": "{{target_ref.code}}"}, "has no fields"),
        ({"{{payload.k}}": 1}, "literal"),
        ({"$eval": 1}, "reserved"),
        ({"x": {"$json": 1, "y": 2}}, "exactly one key"),
        ({"x": 2**60}, "out of range"),
        ({"x": "a" * 2001}, "2000"),
        ({"x": object()}, "not a JSON value"),
    ],
)
def test_template_rejects(tpl: Any, match: str) -> None:
    with pytest.raises(TemplateError, match=match):
        template_refs(tpl, roots=ROOTS)


def test_template_depth_and_static_args() -> None:
    deep: Any = "x"
    for _ in range(9):
        deep = [deep]
    with pytest.raises(TemplateError, match="deeper"):
        template_refs({"a": deep}, roots=ROOTS)
    with pytest.raises(TemplateError, match="none here"):
        template_refs({"x": "{{payload.qty}}"}, roots=())


# ──────────────────────────────── read mapping ──────────────────────────────


def _src(type_: str, **over: Any) -> dict[str, Any]:
    src = {
        "tool": "list_items",
        "args": {"type": type_},
        "page_param": "page",
        "page_size_param": "page_size",
        "page_size": 200,
        "items_path": "$.items",
        "cursor_path": None,
        "max_pages": 50,
    }
    src.update(over)
    return src


def mcp_lite_read_mapping() -> dict[str, Any]:
    """The generic-proof read mapping for Merxa /mcp-lite (110 §4; pack ``foreign-erp-lite``)."""
    return {
        "kind": "read",
        "lookups": {
            "po_supplier": {"kind": "source", "source": _src("purchase_order"), "key": "$.code",
                            "value": "$.supplier_code"},
            "po_dest_dc": {"kind": "source", "source": _src("purchase_order"), "key": "$.code",
                           "value": "$.dest_dc_code"},
            "po_factory": {"kind": "source", "source": _src("purchase_order"), "key": "$.code",
                           "value": "$.factory_code"},
            "currency": {"kind": "static", "values": {"default": "EUR"}},
        },
        "entities": [
            {"entity": "item", "source": _src("sku"), "fields": {
                "source_id": "'sku:' + $.code", "kind": "'sku'", "code": "$.code", "style_code": "$.style_code",
                "size": "$.size", "size_seq": "int($.size_seq)", "src_version": "int($.version)"}},
            {"entity": "item", "source": _src("style"), "fields": {
                "source_id": "'style:' + $.code", "kind": "'style'", "code": "$.code", "family": "$.family",
                "colour": "$.colour", "retail_price_cents": "int($.retail_price_cents)",
                "unit_cost_cents": "int($.unit_cost_cents)", "currency": "lookup('currency', 'default')",
                "src_version": "int($.version)"}},
            {"entity": "location", "source": _src("dc"), "fields": {
                "source_id": "'dc:' + $.code", "kind": "'dc'", "code": "$.code", "name": "$.name",
                "country": "$.country", "city": "$.city", "src_version": "int($.version)"}},
            {"entity": "location", "source": _src("store"), "fields": {
                "source_id": "'store:' + $.code", "kind": "'store'", "code": "$.code", "name": "$.name",
                "city": "$.city", "grade": "$.grade", "parent_code": "$.cluster_code",
                "src_version": "int($.version)"}},
            {"entity": "location", "source": _src("store"), "dedupe": True, "fields": {
                "source_id": "'cluster:' + $.cluster_code", "kind": "'cluster'", "code": "$.cluster_code",
                "parent_code": "$.home_dc_code", "src_version": "0"}},
            {"entity": "po_line", "source": _src("po_line"), "fields": {
                "source_id": "$.key", "po_code": "$.po_code", "line_no": "int($.line_no)",
                "supplier_code": "lookup('po_supplier', $.po_code)", "factory_code": "lookup('po_factory', $.po_code)",
                "dest_dc_code": "lookup('po_dest_dc', $.po_code)", "sku_code": "$.sku_code",
                "qty_ordered": "int($.qty_ordered)", "qty_received": "int($.qty_received)",
                "eta_dc": "date($.eta_dc)", "status": "$.status", "src_version": "int($.version)"}},
            {"entity": "inventory_position", "source": _src("inventory"), "fields": {
                "source_id": "$.key", "location_kind": "$.location_type", "location_code": "$.location_code",
                "sku_code": "$.sku_code", "on_hand": "int($.on_hand)", "allocated": "int($.allocated)",
                "reserved": "int($.reserved)", "in_transit": "int($.in_transit)", "src_version": "int($.version)"}},
            {"entity": "demand_forecast", "source": _src("forecast"), "fields": {
                "source_id": "$.key", "style_code": "$.style_code",
                "location_kind": "if_present($.store_code, 'store', 'cluster')",
                "location_code": "coalesce($.store_code, $.cluster_code)", "iso_week": "$.iso_week",
                "units": "int($.units)", "forecast_version": "int($.version)"}},
            {"entity": "allocation", "source": _src("allocation"), "fields": {
                "source_id": "$.code", "code": "$.code", "sku_code": "$.sku_code", "dc_code": "$.dc_code",
                "store_code": "$.store_code", "ship_week": "$.ship_week", "qty": "int($.qty)",
                "status": "$.status", "src_version": "int($.version)"}},
        ],
    }


def test_read_mapping_valid() -> None:
    m = ReadMapping.model_validate(mcp_lite_read_mapping())
    assert m.mapped_entities == ["location", "item", "po_line", "inventory_position", "demand_forecast", "allocation"]
    assert m.entities[4].dedupe is True
    assert list(m.entities[0].parsed_fields())[:3] == ["source_id", "kind", "code"]
    assert json.loads(m.model_dump_json())["lookups"]["currency"] == {"kind": "static", "values": {"default": "EUR"}}
    assert ReadMapping.model_validate(m.model_dump(mode="json")) == m


def test_scn_entity_literal_matches() -> None:
    assert ScnEntityName.__args__ == SCN_ENTITIES  # type: ignore[attr-defined]
    assert required_scn_columns("inventory_position") == (
        "source_id", "location_kind", "location_code", "sku_code", "on_hand", "allocated", "reserved",
        "in_transit", "src_version")


def _mutate(path: list[Any], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(mcp_lite_read_mapping())
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    if value is _DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        (["entities", 0, "fields", "code"], _DELETE, "misses required columns"),
        (["entities", 0, "fields", "colour_name"], "$.x", "no columns"),
        (["entities", 0, "fields", "code"], "$.__class__", "__"),
        (["entities", 0, "fields", "code"], "eval($.code)", "unknown function"),
        (["entities", 5, "fields", "supplier_code"], "lookup('nope', $.po_code)", "unknown lookup tables"),
        (["entities", 0, "entity"], "sku", "literal_error|Input should be"),
        (["entities", 0, "source"], {**_src("sku"), "page_param": None, "cursor_path": "$.next"},
         "cursor paging needs page_param"),
        (["entities", 0, "source", "page_param"], None, "max_pages must be 1"),
        (["entities", 0, "source", "args"], {"type": "sku", "page": 1}, "paging parameters"),
        (["entities", 0, "source", "args"], {"type": "{{payload.x}}"}, "placeholder"),
        (["entities", 0, "source", "args"], {"price": 1.5}, "floats"),
        (["entities", 0, "source", "page_size"], 0, "greater than or equal"),
        (["entities", 0, "source", "items_path"], "items", "should match pattern|path"),
        (["entities", 0, "source", "tool"], "list items", "should match pattern"),
        (["lookups", "po_supplier", "value"], "lookup('currency', 'x')", "may not call lookup"),
        (["lookups", "Bad"], {"kind": "static", "values": {}}, "should match pattern"),
        (["lookups", "currency", "values", "x"], 1.5, "Input should be"),
        (["entities"], [], "at least 1"),
        (["kind"], "action", "Input should be 'read'"),
    ],
)
def test_read_mapping_rejects(path: list[Any], value: Any, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        ReadMapping.model_validate(_mutate(path, value))


def test_read_source_cursor_mode() -> None:
    src = ReadSource.model_validate(_src("x", cursor_path="$.next_cursor", page_param="cursor", page_size_param=None))
    assert src.cursor_path == "$.next_cursor"
    single = ReadSource.model_validate(_src("x", page_param=None, page_size_param=None, max_pages=1))
    assert single.page_param is None
    with pytest.raises(ValidationError, match="must differ"):
        ReadSource.model_validate(_src("x", page_param="p", page_size_param="p"))


def test_entity_mapping_alone() -> None:
    em = EntityMapping.model_validate(mcp_lite_read_mapping()["entities"][2])
    assert em.entity == "location"


# ─────────────────────────────── action mapping ─────────────────────────────


def mcp_lite_action_mapping() -> dict[str, Any]:
    return {
        "kind": "action",
        "ops": [
            {
                "op": "transfer_order.create",
                "tool": "create_transfer_order",
                "args": {
                    "from_dc": "{{payload.from_dc}}",
                    "to_dc": "{{payload.to_dc}}",
                    "ship_date": "{{payload.ship_date}}",
                    "lines_json": {"$json": "{{payload.lines}}"},
                },
                "idempotency": "enforced",
                "request_id_arg": "request_id",
                "readback": {
                    "tool": "list_items",
                    "args": {"type": "transfer_order", "filter_json": {"$json": {
                        "from_dc": "{{payload.from_dc}}", "to_dc": "{{payload.to_dc}}",
                        "ship_date": "{{payload.ship_date}}"}}},
                    "items_path": "$.items",
                    "match": {"$.from_dc": "{{payload.from_dc}}", "$.to_dc": "{{payload.to_dc}}"},
                },
            },
            {
                "op": "allocation.set_qty",
                "tool": "update_item",
                "args": {
                    "type": "allocation",
                    "key": "{{target_ref}}",
                    "fields_json": {"$json": {"qty": "{{payload.qty}}"}},
                    "expected_version": "{{expected_version}}",
                },
                "idempotency": "enforced",
                "request_id_arg": "request_id",
                "readback": {
                    "tool": "get_item",
                    "args": {"type": "allocation", "key": "{{target_ref}}"},
                    "items_path": "$.item",
                    "match": {"$.qty": "{{payload.qty}}"},
                },
            },
        ],
    }


def test_action_mapping_valid() -> None:
    m = ActionMapping.model_validate(mcp_lite_action_mapping())
    assert m.mapped_ops == ["allocation.set_qty", "transfer_order.create"]
    assert m.for_op("allocation.set_qty") is not None and m.for_op("po.create") is None
    assert unmapped_ops(m, ["po_line.reschedule", "allocation.set_qty", "po.create"]) == ["po.create", "po_line.reschedule"]
    assert unmapped_ops(None, ["a.b"]) == ["a.b"]


def _amut(i: int, key: str, value: Any) -> dict[str, Any]:
    data = copy.deepcopy(mcp_lite_action_mapping())
    if value is _DELETE:
        del data["ops"][i][key]
    else:
        data["ops"][i][key] = value
    return data


@pytest.mark.parametrize(
    ("i", "key", "value", "match"),
    [
        (0, "request_id_arg", None, "needs request_id_arg"),
        (1, "idempotency", "best_effort", None),  # still has a readback: valid
        (1, "readback", None, None),  # enforced without readback: valid
        (0, "args", {"from_dc": "{{target_ref}}"}, "has no target_ref"),
        (0, "args", {"x": "{{expected_version}}"}, "has no expected_version"),
        (1, "args", {"q": "{{payload.quantity}}"}, "payload has no field 'quantity'"),
        (1, "args", {"request_id": "x"}, "must not set request_id_arg"),
        (1, "args", {"q": "{{tenant}}"}, "unknown placeholder root"),
        (1, "op", "Allocation.SetQty", "should match pattern"),
        (1, "tool", "", "should match pattern"),
    ],
)
def test_action_mapping_rules(i: int, key: str, value: Any, match: str | None) -> None:
    data = _amut(i, key, value)
    if match is None:
        ActionMapping.model_validate(data)
        return
    with pytest.raises(ValidationError, match=match):
        ActionMapping.model_validate(data)


def test_action_mapping_readback_required_unless_enforced() -> None:
    data = _amut(1, "readback", None)
    data["ops"][1]["idempotency"] = "best_effort"
    with pytest.raises(ValidationError, match="needs a readback"):
        ActionMapping.model_validate(data)
    data["ops"][1]["idempotency"] = "none"
    with pytest.raises(ValidationError, match="needs a readback"):
        ActionMapping.model_validate(data)


def test_action_mapping_duplicate_op_and_generic_ops() -> None:
    data = mcp_lite_action_mapping()
    data["ops"].append(copy.deepcopy(data["ops"][1]))
    with pytest.raises(ValidationError, match="at most once"):
        ActionMapping.model_validate(data)
    generic = {"kind": "action", "ops": [{
        "op": "vendor.custom_thing", "tool": "do_it", "args": {"x": "{{payload.anything.0}}", "r": "{{target_ref}}"},
        "idempotency": "none", "request_id_arg": None,
        "readback": {"tool": "get_item", "args": {}, "items_path": None, "match": {"$.x": "{{payload.anything}}"}},
    }]}
    assert ActionMapping.model_validate(generic).mapped_ops == ["vendor.custom_thing"]


def test_readback_match_paths_are_validated() -> None:
    data = mcp_lite_action_mapping()
    data["ops"][1]["readback"]["match"] = {"qty": 1}
    with pytest.raises(ValidationError):
        ActionMapping.model_validate(data)
    data["ops"][1]["readback"]["match"] = {"$.qty": "{{payload.qty.__x}}"}
    with pytest.raises(ValidationError, match="__"):
        ActionMapping.model_validate(data)


def _cls(access: str = "write", trust: str = "trusted_data", egress: str = "internal", idem: str = "enforced") -> ToolClassification:
    return ToolClassification(access=ToolAccess(access), content_trust=ContentTrust(trust), egress=Egress(egress),
                              idempotency=Idempotency(idem))


def test_action_mapping_violations() -> None:
    m = ActionMapping.model_validate(mcp_lite_action_mapping())
    tools = {
        "create_transfer_order": _cls(idem="enforced"),
        "update_item": _cls(idem="best_effort"),
        "list_items": _cls(access="read", idem="none"),
    }
    problems = action_mapping_violations(m, tools=tools)
    assert any("update_item" in p and "confirmed 'best_effort'" in p for p in problems)
    assert any("get_item" in p and "approved read tool" in p for p in problems)
    tools["update_item"] = _cls(idem="enforced")
    tools["get_item"] = _cls(access="read")
    assert action_mapping_violations(m, tools=tools) == []
    tools["create_transfer_order"] = _cls(access="read")
    assert any("is a read tool" in p for p in action_mapping_violations(m, tools=tools))
    del tools["create_transfer_order"]
    assert any("not an approved tool" in p for p in action_mapping_violations(m, tools=tools))


def test_dry_run_models() -> None:
    req = ActionDryRunRequest(op="allocation.set_qty", target_ref="AL-1", expected_version=3, payload={"qty": 12})
    assert req.run_readback is True
    with pytest.raises(ValidationError, match="floats"):
        ActionDryRunRequest(op="allocation.set_qty", payload={"qty": 1.5})
    ok = ActionDryRunResult.model_validate({
        "op": "allocation.set_qty", "mapped": True,
        "call": {"tool": "update_item", "args": {"type": "allocation", "key": "AL-1", "fields_json": '{"qty":12}',
                                                 "expected_version": 3, "request_id": "rid"}},
        "idempotency": "enforced", "request_id_arg": "request_id",
        "readback": {"call": {"tool": "get_item", "args": {"type": "allocation", "key": "AL-1"}},
                     "executed": True, "ok": True, "items_found": 1, "matched": False},
        "guided_reason": None,
    })
    assert ok.executed is False
    guided = ActionDryRunResult.model_validate({
        "op": "po.create", "mapped": False, "call": None, "idempotency": None, "request_id_arg": None,
        "readback": None, "guided_reason": "No action mapping for po.create: a guided step is generated."})
    assert guided.mapped is False
    with pytest.raises(ValidationError, match="only a guided_reason"):
        ActionDryRunResult.model_validate({**guided.model_dump(), "idempotency": "none"})
    with pytest.raises(ValidationError, match="request id"):
        ActionDryRunResult.model_validate({**ok.model_dump(), "call": {"tool": "update_item", "args": {}}})
    with pytest.raises(ValidationError):
        ActionDryRunResult.model_validate({**ok.model_dump(), "executed": True})
    with pytest.raises(ValidationError, match="did not run"):
        DryRunReadback.model_validate({"call": {"tool": "t", "args": {}}, "executed": False, "ok": True,
                                       "items_found": None, "matched": None})


# ─────────────────────── snapshot consistency + advisory ────────────────────


def test_as_of_range_and_label() -> None:
    r = AsOfRange.model_validate({"from": "2026-10-08T09:00:00Z", "to": "2026-10-08T09:02:00Z"})
    assert r.from_ == T0
    assert r.model_dump(mode="json") == {"from": "2026-10-08T09:00:00Z", "to": "2026-10-08T09:02:00Z"}
    assert AsOfRange(from_=T0, to=T0).to == T0
    with pytest.raises(ValidationError, match="before"):
        AsOfRange.model_validate({"from": "2026-10-08T09:00:00Z", "to": "2026-10-08T08:00:00Z"})
    with pytest.raises(ValidationError):
        AsOfRange.model_validate({"from": "2026-10-08T09:00:00", "to": "2026-10-08T09:00:00"})  # naive
    crawled = SnapshotLabel.model_validate({"consistency": "crawled", "as_of_range": r.model_dump(mode="json")})
    assert crawled.advisory
    consistent = SnapshotLabel(consistency="consistent", as_of_range=None)
    assert not consistent.advisory
    with pytest.raises(ValidationError, match="exactly for crawled"):
        SnapshotLabel(consistency="crawled", as_of_range=None)
    with pytest.raises(ValidationError, match="exactly for crawled"):
        SnapshotLabel(consistency="consistent", as_of_range=r)
    assert advisory_for(consistent) is None
    adv = advisory_for(crawled)
    assert adv is not None and adv.text == CRAWLED_ADVISORY_TEXT == "Data gathered by paging, not a consistent snapshot"


def _advisory_wire() -> dict[str, Any]:
    return {"reason": "crawled_snapshot", "text": CRAWLED_ADVISORY_TEXT,
            "as_of_range": {"from": "2026-10-08T09:00:00Z", "to": "2026-10-08T09:02:00Z"}}


@pytest.mark.parametrize("key", ["recommended_option@1", "scenario_compare@1", "narrative@1"])
def test_advisory_on_blocks(key: str) -> None:
    kind = key.split("@")[0]
    plain = good_props(key)
    block = make_block(kind, plain, title="t", provenance=provenance())
    assert "advisory" not in block_to_wire(block)["props"]  # pre-M7 wire unchanged
    props = {**plain, "advisory": _advisory_wire()}
    block = make_block(kind, props, title="t", provenance=provenance())
    wire = block_to_wire(block)
    assert wire["props"]["advisory"] == _advisory_wire()
    assert block_to_wire(parse_block(json.dumps(wire))) == wire
    with pytest.raises(ValidationError):
        make_block(kind, {**plain, "advisory": {**_advisory_wire(), "reason": "other"}}, title="t",
                   provenance=provenance())


def test_advisory_recommended_option_still_applies() -> None:
    props = RecommendedOptionProps.model_validate({**good_props("recommended_option@1"), "advisory": _advisory_wire()})
    assert any(a.type == "apply" and a.enabled for a in props.actions)
    assert isinstance(props.advisory, Advisory)
    assert NarrativeProps.model_validate({"markdown": "x", "advisory": _advisory_wire()}).advisory is not None
    assert ScenarioCompareProps.model_validate(
        {**good_props("scenario_compare@1"), "advisory": _advisory_wire()}).advisory is not None


# ─────────────────────────────── commit ledger ──────────────────────────────


def _entry(**over: Any) -> dict[str, Any]:
    data = {
        "change_set_id": "cs-1", "line_seq": 1, "op": "allocation.set_qty", "connector": "merxa-lite",
        "tool": "update_item", "idempotency": "enforced", "state": "attempted", "attempts": 1,
        "request_id": "rid-1", "ext_ref": None, "readback": None, "error": None,
        "updated_at": "2026-10-08T09:00:00Z",
    }
    data.update(over)
    return data


RB_FOUND = {"at": "2026-10-08T09:01:00Z", "ok": True, "found": True}
RB_MISSING = {"at": "2026-10-08T09:01:00Z", "ok": True, "found": False}
RB_ERROR = {"at": "2026-10-08T09:01:00Z", "ok": False, "found": False, "detail": "timeout"}


@pytest.mark.parametrize(
    ("over", "action"),
    [
        ({"state": "succeeded"}, "none"),
        ({"state": "verified", "readback": RB_FOUND}, "none"),
        ({"state": "failed", "error": "conflict: version moved"}, "guide"),
        ({"state": "in_doubt"}, "retry"),
        ({"state": "attempted"}, "retry"),
        ({"state": "in_doubt", "attempts": 3}, "guide"),
        ({"state": "in_doubt", "idempotency": "best_effort"}, "readback"),
        ({"state": "in_doubt", "idempotency": "best_effort", "readback": RB_ERROR}, "readback"),
        ({"state": "in_doubt", "idempotency": "best_effort", "readback": RB_MISSING}, "retry"),
        ({"state": "in_doubt", "idempotency": "best_effort", "readback": RB_MISSING, "attempts": 3}, "guide"),
        ({"state": "in_doubt", "idempotency": "none", "request_id": None}, "readback"),
        ({"state": "in_doubt", "idempotency": "none", "request_id": None, "readback": RB_MISSING}, "guide"),
    ],
)
def test_ledger_next_action(over: dict[str, Any], action: str) -> None:
    assert ledger_next_action(CommitLedgerEntry.model_validate(_entry(**over))) == action


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"request_id": None}, "carries its request_id"),
        ({"idempotency": "none", "attempts": 2, "readback": RB_MISSING}, "never auto-retried"),
        ({"idempotency": "best_effort", "attempts": 2}, "read back before any retry"),
        ({"idempotency": "best_effort", "attempts": 2, "readback": RB_ERROR}, "read back before any retry"),
        ({"state": "failed"}, "says why"),
        ({"state": "verified"}, "found the effect"),
        ({"state": "in_doubt", "readback": RB_FOUND}, "settles the line"),
        ({"attempts": 4}, "less than or equal"),
        ({"state": "unknown"}, "Input should be"),
        ({"readback": {"at": "2026-10-08T09:01:00Z", "ok": False, "found": True}}, "cannot have found"),
    ],
)
def test_ledger_entry_rules(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CommitLedgerEntry.model_validate(_entry(**over))


def test_atomic_groups_need_enforced_members() -> None:
    assert atomic_group_bulk_allowed(["enforced", Idempotency.ENFORCED])
    assert not atomic_group_bulk_allowed(["enforced", "best_effort"])
    assert not atomic_group_bulk_allowed([])


# ─────────────────────────────── capabilities ───────────────────────────────


def test_capability_summary() -> None:
    merxa = summarize_capabilities("merxa", ["W", "C", "B", "A"])
    assert (merxa.sync, merxa.consistency, merxa.commit, merxa.advisory) == ("snapshot", "consistent", "change_set", False)
    assert merxa.tiers == [ConnectorTier.A, ConnectorTier.B, ConnectorTier.C, ConnectorTier.W]
    lite = summarize_capabilities("merxa-lite", ["A"], read_mapping_version=2, action_mapping_version=1,
                                  mapped_ops=["transfer_order.create", "allocation.set_qty"])
    assert (lite.sync, lite.consistency, lite.commit, lite.advisory) == ("crawl", "crawled", "per_op", True)
    assert lite.mapped_ops == ["allocation.set_qty", "transfer_order.create"]
    bare = summarize_capabilities("bare", ["A"])
    assert (bare.sync, bare.consistency, bare.commit, bare.advisory) == ("none", None, "guide", False)
    assert summarize_capabilities("x", ["A"], action_mapping_version=1).commit == "guide"  # no ops mapped
    wire = lite.model_dump(mode="json")
    assert ConnectorCapabilities.model_validate(wire) == lite
    for field, value in (("advisory", False), ("commit", "change_set"), ("sync", "snapshot"), ("consistency", None)):
        with pytest.raises(ValidationError, match=f"{field} must be"):
            ConnectorCapabilities.model_validate({**wire, field: value})
    with pytest.raises(ValidationError, match="order"):
        ConnectorCapabilities.model_validate({**merxa.model_dump(mode="json"), "tiers": ["B", "A", "C", "W"]})
    with pytest.raises(ValidationError, match="sorted"):
        ConnectorCapabilities.model_validate({**wire, "mapped_ops": ["transfer_order.create", "allocation.set_qty"]})


# ─────────────────────────────── change requests ────────────────────────────

REQUESTER = {"sub": "sub-tina", "username": "tina", "name": "Tina Admin"}
APPROVER = {"sub": "sub-omar", "username": "omar", "name": "Omar Admin", "role": "tenant-admin"}


def _cr(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": UUID_A,
        "tenant": "northwind-test",
        "kind": "agent_spec",
        "target": "agent_spec",
        "payload": {"base_version": "v3", "spec_patch": {"policy": {"auto_max_lines": 20}}},
        "base_version": "v3",
        "diff": [{"op": "replace", "path": "/policy/auto_max_lines", "before": 5, "after": 20}],
        "reasons": [{"code": "auto_cap_raised", "field": "policy.auto_max_lines",
                     "reason": "raises the automatic-approval caps"}],
        "requested_by": REQUESTER,
        "requested_at": T0.isoformat(),
        "status": "pending",
        "approver": None,
        "decided_at": None,
        "decision_reason": None,
        "expires_at": (T0 + CHANGE_REQUEST_TTL).isoformat(),
    }
    data.update(over)
    return data


DECIDED = (T0 + dt.timedelta(hours=1)).isoformat()


def test_change_request_lifecycle_shapes() -> None:
    pending = AdminChangeRequest.model_validate(_cr())
    assert not pending.is_expired(T0 + dt.timedelta(hours=71))
    assert pending.is_expired(T0 + dt.timedelta(hours=72))
    applied = AdminChangeRequest.model_validate(_cr(status="applied", approver=APPROVER, decided_at=DECIDED))
    assert applied.approver is not None and applied.approver.role == "tenant-admin"
    cosign = {**APPROVER, "sub": "sub-ava", "role": "platform-admin"}
    assert AdminChangeRequest.model_validate(_cr(status="applied", approver=cosign, decided_at=DECIDED))
    # the requester may withdraw (reject) their own request
    withdrawn = _cr(status="rejected", approver={**REQUESTER, "role": "tenant-admin"}, decided_at=DECIDED,
                    decision_reason="withdrawn")
    assert AdminChangeRequest.model_validate(withdrawn).status == "rejected"
    assert AdminChangeRequest.model_validate(_cr(status="expired")).status == "expired"


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"expires_at": (T0 + dt.timedelta(hours=24)).isoformat()}, "72 h"),
        ({"status": "applied", "approver": {**REQUESTER, "role": "tenant-admin"}, "decided_at": DECIDED},
         "self-approval"),
        ({"status": "approved", "approver": {**REQUESTER, "role": "platform-admin"}, "decided_at": DECIDED},
         "self-approval"),
        ({"status": "rejected", "approver": APPROVER, "decided_at": DECIDED}, "carries its reason"),
        ({"status": "applied", "approver": None, "decided_at": DECIDED}, "names its approver"),
        ({"status": "applied", "approver": APPROVER, "decided_at": None}, "names its approver"),
        ({"approver": APPROVER}, "has no approver"),
        ({"status": "expired", "decided_at": DECIDED}, "has no approver"),
        ({"status": "applied", "approver": APPROVER, "decided_at": (T0 + dt.timedelta(hours=73)).isoformat()},
         "between"),
        ({"status": "applied", "approver": APPROVER, "decided_at": (T0 - dt.timedelta(hours=1)).isoformat()},
         "between"),
        ({"kind": "kb_delete"}, "do not apply"),
        ({"reasons": []}, "at least 1"),
        ({"diff": []}, "at least 1"),
        ({"reasons": [_cr()["reasons"][0]] * 2}, "unique"),
        ({"payload": {"x": float("nan")}}, "finite JSON"),
        ({"id": "not-a-uuid"}, "should match pattern"),
        ({"tenant": "Bad Tenant"}, "should match pattern"),
        ({"status": "applied", "approver": {**APPROVER, "role": "planner"}, "decided_at": DECIDED}, "Input should be"),
    ],
)
def test_change_request_rules(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        AdminChangeRequest.model_validate(_cr(**over))


def test_guard_reason_kinds_cover_contract() -> None:
    # 110 §1 list: thresholds/auto caps, refuse/never-auto rules, reclassification (read / trusted), auto on
    # write, external egress, first-party trust, embedding profile, connector delete, KB delete
    assert GUARD_REASONS_BY_KIND["connector_tool"] == {"tool_reclassified_read", "tool_reclassified_trusted"}
    assert "auto_binding_on_write" in GUARD_REASONS_BY_KIND["tool_binding"]
    assert "external_egress_enabled" in GUARD_REASONS_BY_KIND["connector_activate"]
    assert "first_party_trust_added" in GUARD_REASONS_BY_KIND["connector_trust"]
    assert "embedding_profile_changed" in GUARD_REASONS_BY_KIND["embedding_profile"]
    assert "policy_rule_weakened" in GUARD_REASONS_BY_KIND["policy_set"]


def test_change_request_decisions() -> None:
    cr = AdminChangeRequest.model_validate(_cr())
    now = T0 + dt.timedelta(hours=1)
    kw: dict[str, Any] = {"actor_tenants": ["northwind-test"], "now": now}
    ta, pa = ["tenant-admin"], ["platform-admin"]
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-omar", actor_roles=ta, reason=None, **kw) is None
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-tina", actor_roles=ta, reason=None,
                                         **kw) == "self_approval"
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-tina", actor_roles=pa, reason=None,
                                         **kw) == "self_approval"
    assert change_request_decision_error(cr, decision="reject", actor_sub="sub-tina", actor_roles=ta,
                                         reason="withdrawn", **kw) is None
    assert change_request_decision_error(cr, decision="reject", actor_sub="sub-omar", actor_roles=ta, reason="  ",
                                         **kw) == "reason_required"
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-omar", actor_roles=["planner"],
                                         reason=None, **kw) == "forbidden"
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-omar", actor_roles=ta, reason=None,
                                         actor_tenants=["value-fashion-eu"], now=now) == "forbidden"
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-ava", actor_roles=pa, reason=None,
                                         actor_tenants=[], now=now) is None  # platform co-sign, any tenant
    assert change_request_decision_error(cr, decision="approve", actor_sub="sub-omar", actor_roles=ta, reason=None,
                                         actor_tenants=["northwind-test"], now=T0 + CHANGE_REQUEST_TTL) == "expired"
    applied = AdminChangeRequest.model_validate(_cr(status="applied", approver=APPROVER, decided_at=DECIDED))
    assert change_request_decision_error(applied, decision="approve", actor_sub="sub-x", actor_roles=ta, reason=None,
                                         **kw) == "not_pending"
    assert ChangeRequestDecision().reason is None
    with pytest.raises(ValidationError):
        ChangeRequestDecision(reason="x" * 501)


def test_diff_entries() -> None:
    assert ChangeDiffEntry(op="add", path="/rules/3", after={"id": "r"}).before is None
    assert ChangeDiffEntry(op="replace", path="", before=1, after=2)
    with pytest.raises(ValidationError, match="add has no before"):
        ChangeDiffEntry(op="add", path="/a", before=1, after=2)
    with pytest.raises(ValidationError, match="remove has no after"):
        ChangeDiffEntry(op="remove", path="/a", before=1, after=2)
    with pytest.raises(ValidationError, match="pattern"):
        ChangeDiffEntry(op="remove", path="a/b", before=1)
    with pytest.raises(ValidationError, match="pattern"):
        ChangeDiffEntry(op="remove", path="/a~2", before=1)


# ─────────────────────────────────── users ──────────────────────────────────


def test_users_models() -> None:
    created = AdminUserCreate(username="nora", email="nora@northwind.test", name="Nora Planner",
                              roles=["planner", "approver"])
    assert created.roles == ["planner", "approver"]
    for bad in ({"roles": []}, {"roles": ["planner", "planner"]}, {"roles": ["merxa-admin"]},
                {"email": "nora"}, {"username": "Nora"}, {"name": "Nora\nX"}, {"name": " Nora"}):
        with pytest.raises(ValidationError):
            AdminUserCreate.model_validate({**created.model_dump(), **bad})
    user = AdminUser(id=UUID_B, username="nora", email=None, name="Nora", enabled=True, roles=["planner"],
                     tenants=["northwind-test"], created_at=None)
    setup = PasswordSetup(link="https://auth.localhost/realms/keelson/login-actions/action-token?key=abc",
                          link_expires_at=T0, instructions="Open the link once to set a password.")
    assert AdminUserCreated(user=user, setup=setup).setup.required_action == "UPDATE_PASSWORD"
    assert PasswordSetup(link=None, link_expires_at=None, instructions="Ask the user to sign in and set a password.")
    for link in ("javascript:alert(1)", "https://evil@auth.localhost/x", "https://auth.localhost/a b", "/relative"):
        with pytest.raises(ValidationError):
            PasswordSetup(link=link, link_expires_at=T0, instructions="x")
    with pytest.raises(ValidationError, match="exactly when"):
        PasswordSetup(link=None, link_expires_at=T0, instructions="x")
    assert "password" not in json.dumps(PasswordSetup.model_json_schema()["properties"])
    assert AdminUserPatch(enabled=False).roles is None
    with pytest.raises(ValidationError, match="set roles"):
        AdminUserPatch()


def test_role_rules() -> None:
    assert PRODUCT_ROLES == ("planner", "approver", "compliance", "tenant-admin", "platform-admin", "demo-presenter")
    assert "platform-admin" not in grantable_roles(["tenant-admin"])
    assert grantable_roles(["platform-admin"]) == PRODUCT_ROLES
    assert grantable_roles(["planner"]) == ()
    ta = ["tenant-admin"]
    nw = {"actor_tenants": ["northwind-test"], "target_tenants": ["northwind-test"]}
    assert user_change_violations(ta, target_roles=["planner"], new_roles=["planner", "approver"], **nw) == []
    assert user_change_violations(ta, target_roles=["planner"], new_roles=["platform-admin"], **nw)
    assert user_change_violations(ta, target_roles=["platform-admin"], enabled=False, **nw)
    assert user_change_violations(ta, target_roles=["platform-admin", "planner"],
                                  new_roles=["platform-admin"], **nw)  # touching a platform-admin
    assert user_change_violations(["platform-admin"], target_roles=["planner"], new_roles=["platform-admin"],
                                  actor_tenants=[], target_tenants=["northwind-test"]) == []
    assert user_change_violations(["planner"], target_roles=[], new_roles=["planner"], **nw)
    assert user_change_violations(["platform-admin"], target_roles=[], new_roles=["merxa-admin"], **nw)


# ───────────────────────────── custom specialists ───────────────────────────


def _spec(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "slug": "supplier_risk",
        "display_name": "Supplier risk analyst",
        "description": "Checks open purchase orders and supplier delivery history in the foreign ERP.",
        "role_prompt": "You review supplier reliability.\nCite every number.",
        "model_alias": "fast-default",
        "tools": [{"connector": "merxa-lite", "tool": "list_items"}, {"connector": "merxa-lite", "tool": "get_item"}],
        "kb_ids": [KB_1],
    }
    data.update(over)
    return data


def test_custom_specialist_valid() -> None:
    spec = CustomSpecialistSpec.model_validate(_spec())
    assert (spec.max_tool_calls, spec.max_turns, spec.enabled) == (8, 6, True)
    assert spec.connectors == ["merxa-lite"]
    assert CustomSpecialistSpec.model_validate(_spec(tools=[])).kb_ids == [KB_1]
    assert CustomSpecialistSpec.model_validate(_spec(model_alias="anthropic/claude-fast"))
    assert CustomSpecialistSpec.model_validate(_spec(role_prompt="x" * 4000))


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"slug": "sourcing"}, "built-in"),
        ({"slug": "external_intel"}, "built-in"),
        ({"slug": "answer_investigator"}, "built-in"),
        ({"slug": "risk_investigator"}, "_investigator"),
        ({"slug": "a__b"}, "__"),
        ({"slug": "Supplier"}, "pattern"),
        ({"slug": "ab"}, "pattern"),
        ({"slug": "supplier-risk"}, "pattern"),
        ({"model_alias": "gemini-2-5-pro"}, "Pro models"),
        ({"model_alias": "pro-default"}, "Pro models"),
        ({"model_alias": "gemini/flash"}, "pattern"),
        ({"model_alias": "Fast"}, "pattern"),
        ({"role_prompt": "x" * 4001}, "at most 4000"),
        ({"role_prompt": "a\x00b"}, "control"),
        ({"role_prompt": "   \n"}, "blank"),
        ({"display_name": "A\nB"}, "single line"),
        ({"display_name": "x" * 61}, "at most 60"),
        ({"description": "x" * 301}, "at most 300"),
        ({"tools": [], "kb_ids": []}, "at least one tool"),
        ({"tools": [{"connector": "c", "tool": "t"}] * 2}, "unique"),
        ({"kb_ids": [KB_1, KB_1]}, "unique"),
        ({"kb_ids": ["kb-1"]}, "pattern"),
        ({"tools": [{"connector": f"c{i}", "tool": "t"} for i in range(4)]}, "at most 3 connectors"),
        ({"tools": [{"connector": "c", "tool": f"t{i}"} for i in range(16)]}, "at most 15"),
        ({"max_tool_calls": 16}, "less than or equal"),
        ({"max_turns": 0}, "greater than or equal"),
        ({"max_turns": True}, "valid integer"),
        ({"extra": 1}, "Extra inputs"),
    ],
)
def test_custom_specialist_rejects(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        CustomSpecialistSpec.model_validate(_spec(**over))


def test_custom_specialist_violations() -> None:
    spec = CustomSpecialistSpec.model_validate(_spec())
    read = _cls(access="read")
    catalogue = {("merxa-lite", "list_items"): read, ("merxa-lite", "get_item"): read}
    kw: dict[str, Any] = {"aliases": ["fast-default"], "kb_ids": [KB_1]}
    assert custom_specialist_violations(spec, catalogue=catalogue, **kw) == []
    assert quarantined_tool_refs(spec, catalogue) == []
    p = custom_specialist_violations(spec, catalogue={("merxa-lite", "list_items"): read}, **kw)
    assert p == ["merxa-lite/get_item: not an approved tool"]
    p = custom_specialist_violations(spec, catalogue={**catalogue, ("merxa-lite", "get_item"): _cls()}, **kw)
    assert p == ["merxa-lite/get_item: only read tools may be bound (write)"]
    mixed = {("merxa-lite", "list_items"): _cls(access="read", trust="untrusted_text"),
             ("merxa-lite", "get_item"): _cls(access="read", egress="external")}
    p = custom_specialist_violations(spec, catalogue=mixed, **kw)
    assert p == ["untrusted_text tools cannot be combined with external-egress tools"]
    untrusted = {**catalogue, ("merxa-lite", "list_items"): _cls(access="read", trust="untrusted_text")}
    assert custom_specialist_violations(spec, catalogue=untrusted, **kw) == []
    assert [t.tool for t in quarantined_tool_refs(spec, untrusted)] == ["list_items"]
    assert custom_specialist_violations(spec, catalogue=catalogue, aliases=[], kb_ids=[KB_1]) == [
        "model alias 'fast-default' is not available to this tenant"]
    assert custom_specialist_violations(spec, catalogue=catalogue, alias_models={"fast-default": "gemini/gemini-2.5-pro"},
                                        **kw) == ["model alias 'fast-default' points at a Pro model"]
    assert custom_specialist_violations(spec, catalogue=catalogue, alias_models={"fast-default": "gemini/gemini-2.5-flash"},
                                        **kw) == []
    assert custom_specialist_violations(spec, catalogue=catalogue, aliases=["fast-default"], kb_ids=[]) == [
        f"unknown knowledge bases ['{KB_1}']"]
    p = custom_specialist_violations(spec, catalogue=catalogue, active_connectors=["merxa"], **kw)
    assert len(p) == 2 and all("not active" in x for x in p)


def test_custom_specialists_list_rules() -> None:
    a = CustomSpecialistSpec.model_validate(_spec())
    b = CustomSpecialistSpec.model_validate(_spec(slug="dc_capacity", display_name="DC capacity analyst"))
    assert custom_specialists_violations([a, b]) == []
    assert "slugs must be unique" in custom_specialists_violations([a, a])[0]
    c = CustomSpecialistSpec.model_validate(_spec(slug="other", display_name="supplier RISK analyst"))
    assert custom_specialists_violations([a, c]) == ["custom specialist display names must be unique"]
    many = [CustomSpecialistSpec.model_validate(_spec(slug=f"spec_{i}", display_name=f"S{i}")) for i in range(9)]
    assert custom_specialists_violations(many) == ["at most 8 custom specialists"]


@pytest.mark.parametrize(
    ("name", "pro"),
    [
        ("gemini-2.5-pro", True), ("gemini/gemini-3-pro-preview", True), ("vertex_ai/gemini-pro", True),
        ("pro-default", True), ("PRO", True), ("gemini-2.5-flash", False), ("prod-fast", False),
        ("fast-default", False), ("reasoning-default", False), ("improve", False), ("gemini-flash-lite", False),
    ],
)
def test_is_pro_model(name: str, pro: bool) -> None:
    assert is_pro_model(name) is pro


def test_reserved_slugs() -> None:
    assert set(agui.AGENT_DISPLAY_NAMES) <= RESERVED_SPECIALIST_SLUGS
    assert not is_custom_specialist_slug("planner")
    assert is_custom_specialist_slug("supplier_risk")


# ──────────────────────────────── playground ────────────────────────────────

FINDINGS = {"task_id": "t1", "status": "ok", "claims": [], "affected": [], "gaps": [], "scenario_updates": []}


def _result(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "playground_id": UUID_A, "specialist": "supplier_risk", "display_name": "Supplier risk analyst",
        "status": "completed", "findings": FINDINGS,
        "tool_calls": [{"connector": "merxa-lite", "tool": "list_items", "ok": True, "duration_ms": 41,
                        "quarantined": False, "evidence_id": "ev_abc", "error": None}],
        "usage": {"llm_calls": 2, "tool_calls": 1, "tokens_in": 900, "tokens_out": 120},
        "error": None, "started_at": T0.isoformat(), "duration_ms": 1200,
    }
    data.update(over)
    return data


def test_playground_models() -> None:
    assert PlaygroundRequest(specialist="supplier_risk", question="Which POs are late?").specialist == "supplier_risk"
    draft = PlaygroundRequest.model_validate({"specialist": _spec(), "question": "Which POs are late?"})
    assert isinstance(draft.specialist, CustomSpecialistSpec)
    for bad in ({"specialist": "sourcing", "question": "q"}, {"specialist": "supplier_risk", "question": ""},
                {"specialist": "supplier_risk", "question": "x" * 2001}):
        with pytest.raises(ValidationError):
            PlaygroundRequest.model_validate(bad)
    res = PlaygroundResult.model_validate(_result())
    assert res.committed is False and res.findings is not None
    budget = _result(status="budget_exceeded", findings=None,
                     error={"code": "budget_exceeded", "message": "Stopped after 8 tool calls."})
    assert PlaygroundResult.model_validate(budget).status == "budget_exceeded"
    for over, match in (
        ({"findings": None}, "has findings"),
        ({"status": "failed", "findings": None}, "says why"),
        ({"status": "budget_exceeded", "findings": None, "error": {"code": "other", "message": "x"}}, "budget_exceeded"),
        ({"usage": {"llm_calls": 2, "tool_calls": 2, "tokens_in": 1, "tokens_out": 1}}, "usage.tool_calls"),
        ({"committed": True}, "Input should be False"),
        ({"tool_calls": [{"connector": "c", "tool": "t", "ok": False, "duration_ms": 1, "quarantined": False,
                          "evidence_id": None, "error": None}]}, "error exactly"),
        ({"tool_calls": [{"connector": "c", "tool": "t", "ok": False, "duration_ms": 1, "quarantined": False,
                          "evidence_id": "ev_x", "error": "boom"}]}, "no evidence"),
    ):
        with pytest.raises(ValidationError, match=match):
            PlaygroundResult.model_validate(_result(**over))


# ─────────────────────────────── AG-UI subagents ────────────────────────────


def test_subagent_started_custom_specialist() -> None:
    sc = agui.ScMetadata(tenant_id="northwind-test", case_id="c1", stage="checking_systems", prov="inference",
                         label="Supplier risk analyst started")
    ev = agui.subagent_started(sc, subagent_run_id="r1", name="supplier_risk", description="Supplier risk analyst",
                               custom=True)
    assert ev["name"] == "supplier_risk" and ev["description"] == "Supplier risk analyst"
    with pytest.raises(ValueError, match="custom specialist slug"):
        agui.subagent_started(sc, subagent_run_id="r1", name="sourcing", description="X", custom=True)
    with pytest.raises(ValueError, match="display name"):
        agui.subagent_started(sc, subagent_run_id="r1", name="supplier_risk", custom=True)
    with pytest.raises(ValueError, match="unknown agent slug"):
        agui.subagent_started(sc, subagent_run_id="r1", name="supplier_risk", description="Supplier risk analyst")
    assert agui.subagent_started(sc, subagent_run_id="r2", name="sourcing")["description"] == "Sourcing analyst"
