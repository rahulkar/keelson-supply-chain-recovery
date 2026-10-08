"""M7 tier-A mapping evaluator + crawl paging, pure (no database): the safe expression language
(semantics, no eval / attribute access, fuzzed), action templates, read-back matching, the crawl
paging rules (page numbers, cursor, max_pages truncation, repeated cursor, item errors) and the
``foreign-erp-lite`` pack mappings against real-shape ``/mcp-lite`` responses
(docs/contracts/110-m7-backoffice-generic.md §4)."""

from __future__ import annotations

import ast
import json
import random
import string
from pathlib import Path
from typing import Any

import pytest

from e1_env import HAVE_PACKS, REPO_PACKS
from e1_lite_server import COLUMNS, LITE_DATA, key_of
from keelson_contracts import scn
from keelson_contracts.mapping import (
    ActionMapping,
    ExpressionError,
    ReadMapping,
    parse_expression,
    read_mapping_violations,
)
from keelson_engine.core import mapping_eval as me
from keelson_engine.core import scn_crawl
from keelson_engine.core.packs import PackError, load_pack, load_pack_mappings
from keelson_engine.core.scn_crawl import CrawlError, CrawlStats, crawl_rows

aio = pytest.mark.asyncio(loop_scope="session")

ITEM = {"code": "PO-1", "line_no": 2, "qty": "12", "eta": "2026-11-24T00:00:00Z", "eu": "24/11/2026",
        "flag": True, "nested": {"a": [10, {"b": "deep"}]}, "empty": "", "none": None, "x y": "spaced",
        "big": 2**60, "float": 1.5}


def ev(expr: str, item: Any = ITEM, lookups: me.Lookups | None = None) -> Any:
    return me.evaluate_text(expr, item, lookups)


# ───────────────────────────── expression semantics ─────────────────────────────


@pytest.mark.parametrize(("expr", "want"), [
    ("$.code", "PO-1"),
    ("$.nested.a[0]", 10),
    ("$.nested.a[1].b", "deep"),
    ("$['x y']", "spaced"),
    ("$.missing", None),
    ("$.nested.a[9]", None),
    ("$.code.deeper", None),  # indexing a string: missing, never an attribute
    ("$.code + '#' + $.line_no", "PO-1#2"),
    ("'dc:' + $.flag", "dc:true"),
    ("$.code + $.missing", None),  # a null operand nulls the concatenation
    ("int($.qty)", 12),
    ("int($.line_no)", 2),
    ("int($.none)", None),
    ("str($.line_no)", "2"),
    ("date($.eta)", "2026-11-24"),
    ("date($.eu, 'DD/MM/YYYY')", "2026-11-24"),
    ("date($.none)", None),
    ("coalesce($.none, $.missing, 'fallback')", "fallback"),
    ("if_present($.empty, 'yes', 'no')", "no"),
    ("if_present($.code, 'yes', 'no')", "yes"),
    ("42", 42),
    ("-7", -7),
    ("null", None),
    ("true", True),
    ("('a' + 'b') + 'c'", "abc"),
    ("  $.code  +  'x' ", "PO-1x"),
])
def test_expression_semantics(expr: str, want: Any) -> None:
    assert ev(expr) == want


@pytest.mark.parametrize("expr", [
    "int($.float)", "int('1.5')", "int($.flag)", "int($.big)", "date('2026-13-01')", "date('soon')",
    "date('24-11-2026', 'DD/MM/YYYY')", "$.nested + 'x'", "str($.nested)", "lookup('nope', $.code)",
])
def test_expression_runtime_errors(expr: str) -> None:
    with pytest.raises(me.MappingEvalError):
        ev(expr, lookups=me.Lookups({}))


def test_lookups() -> None:
    tables = me.Lookups({"region": {"PO-1": "EU", "2": "two"}})
    assert ev("lookup('region', $.code)", lookups=tables) == "EU"
    assert ev("lookup('region', $.line_no)", lookups=tables) == "two"  # keys compared as text
    assert ev("lookup('region', $.missing)", lookups=tables) is None
    assert ev("lookup('region', 'zz', 'default')", lookups=tables) == "default"


@pytest.mark.parametrize("expr", [
    "$.__class__", "$['__globals__']", "$.code.__len__", "__import__('os')", "eval('1')", "exec('x')",
    "getattr($, 'x')", "open('/etc/passwd')", "$.code()", "$.items()", "lambda: 1", "1 if 1 else 2",
    "$.a | $.b", "$.a - 1", "1.5", "0x10", "01", "'unterminated", "'a\\nb'", "f'{1}'", "$..a", "$[-1]",
    "$.a[*]", "int()", "int(1, 2)", "lookup($.t, $.k)", "date($.d, $.f)", "date($.d, 'YYYY')", "x" * 501,
    "(" * 20 + "1" + ")" * 20, "+".join(["'a'"] * 70), "$" + ".a" * 17, "\x00", "１２", "$.ｃｏｄｅ",
])
def test_parser_refuses_everything_outside_the_grammar(expr: str) -> None:
    with pytest.raises(ExpressionError):
        parse_expression(expr)


def test_evaluator_only_indexes_real_json_containers() -> None:
    class Sneaky(dict):  # a dict subclass with a custom item protocol never reaches __getitem__
        def get(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("called")

        def __getitem__(self, _k: Any) -> Any:
            raise AssertionError("called")

    assert me.get_path(Sneaky(code="x"), "$.code") is None
    assert me.get_path(["a"], "$['0']") is None  # a text key on a list
    assert me.get_path({"a": ["x"]}, "$.a[0]") == "x"


def test_no_eval_in_the_engine_modules() -> None:
    """The evaluator, the crawl and the mapping service never call eval / exec / compile /
    getattr / __import__ (static check of the source)."""
    root = Path(me.__file__).parent
    for name in ("mapping_eval.py", "scn_crawl.py", "connector_mappings.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"eval", "exec", "compile", "__import__", "setattr"}, name
        if name == "mapping_eval.py":  # the interpreter itself never reflects on objects
            assert not calls & {"getattr", "hasattr", "vars"}, name


def test_fuzz_expressions_never_escape() -> None:
    """Random token soups and mutations of valid expressions: the parser either refuses them with
    ExpressionError or the evaluator returns a JSON scalar / raises MappingEvalError — nothing else
    (no TypeError / AttributeError / RecursionError, no Python object)."""
    rng = random.Random(110)
    tokens = ["$", ".", "[", "]", "(", ")", "+", ",", "'", '"', "\\", "$.code", "$.nested", "$.a[0]", "int", "str",
              "date", "lookup", "coalesce", "if_present", "null", "true", "false", "'x'", "12", "-3", " ", "__",
              "__class__", "$['k']", "'DD/MM/YYYY'", "'t'", "\n", "\t", "é", "0", "999999999999999999999"]
    seeds = ["$.code + '#' + $.line_no", "coalesce($.none, int($.qty))", "date($.eu, 'DD/MM/YYYY')",
             "if_present($.code, lookup('t', $.code, 'd'), 'n')", "$.nested.a[1].b"]
    tables = me.Lookups({"t": {"PO-1": "hit"}})
    alphabet = string.printable + "éß€ "
    parsed = 0
    for i in range(6000):
        mode = i % 3
        if mode == 0:
            text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 14)))
        elif mode == 1:
            s = list(rng.choice(seeds))
            for _ in range(rng.randint(1, 4)):
                pos = rng.randrange(len(s) + 1)
                if rng.random() < 0.5 and s:
                    del s[min(pos, len(s) - 1)]
                else:
                    s.insert(pos, rng.choice(alphabet))
            text = "".join(s)
        else:
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        try:
            node = parse_expression(text)
        except ExpressionError:
            continue
        parsed += 1
        try:
            value = me.evaluate(node, ITEM, tables)
        except me.MappingEvalError:
            continue
        assert value is None or isinstance(value, (str, int, bool, dict, list)), (text, value)
        if isinstance(value, (dict, list)):  # only a bare path may select a container
            assert text.strip().startswith("$") or text.strip().startswith("(") or "coalesce" in text \
                or "if_present" in text, text
    assert parsed > 200  # the fuzzer did reach the evaluator


# ───────────────────────────── templates & read-back ─────────────────────────────


def test_render_template_types_and_json_wrapper() -> None:
    ctx = me.ActionContext(op="transfer_order.create", payload={"from_dc": "DE1", "qty": 12,
                                                               "lines": [{"sku": "A", "qty": 3}]},
                           target_ref="AL-1", expected_version=3, request_id="rq-1")
    tpl = {"from": "{{payload.from_dc}}", "qty": "{{ payload.qty }}", "label": "to {{payload.from_dc}} x{{payload.qty}}",
           "lines_json": {"$json": "{{payload.lines}}"}, "sku": "{{payload.lines.0.sku}}", "key": "{{target_ref}}",
           "v": "{{expected_version}}", "rid": "{{request_id}}", "op": "{{op}}", "const": [1, True, None, "x"],
           "obj": {"$json": {"qty": "{{payload.qty}}", "b": "z"}}}
    out = me.render_template(tpl, ctx)
    assert out == {"from": "DE1", "qty": 12, "label": "to DE1 x12", "lines_json": '[{"qty":3,"sku":"A"}]',
                   "sku": "A", "key": "AL-1", "v": 3, "rid": "rq-1", "op": "transfer_order.create",
                   "const": [1, True, None, "x"], "obj": '{"b":"z","qty":12}'}
    for bad in ({"a": "{{payload.nope}}"}, {"a": "{{payload.lines.9}}"}, {"a": "x{{payload.lines}}"},
                {"a": "{{secrets}}"}, {"a": 1.5}, {"$json": 1, "b": 2}, {"a": "{{target_ref.x}}"}):
        with pytest.raises(me.MappingEvalError):
            me.render_template(bad, ctx)
    with pytest.raises(me.MappingEvalError):  # crawl args take no placeholders
        me.render_template({"a": "{{payload.qty}}"}, None)


def test_readback_match() -> None:
    ctx = me.ActionContext(op="allocation.set_qty", payload={"qty": 30}, target_ref="AL-2001")
    items = me.readback_items({"item": {"code": "AL-2001", "qty": "30"}}, "$.item")
    assert me.readback_match(items, {"$.qty": "{{payload.qty}}", "$.code": "{{target_ref}}"}, ctx)  # "30" == 30
    assert not me.readback_match(items, {"$.qty": 31}, ctx)
    assert me.readback_items({"items": [1, 2]}, "$.items") == [1, 2]
    assert me.readback_items({}, "$.items") == []
    assert not me.values_equal(True, "true") and not me.values_equal(1, True)


# ───────────────────────────── crawl paging (in-process caller) ─────────────────────────────


def _lite_caller(data: dict[str, list[dict[str, Any]]], log: list[tuple[str, dict[str, Any]]],
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


def _sku_mapping(**source: Any) -> ReadMapping:
    src = {"tool": "list_items", "args": {"type": "sku"}, "page_param": "page", "page_size_param": "page_size",
           "page_size": 2, "items_path": "$.items", "cursor_path": None, "max_pages": 10, **source}
    return ReadMapping.model_validate({"entities": [{"entity": "item", "source": src, "fields": {
        "source_id": "'sku:' + $.code", "kind": "'sku'", "code": "$.code", "style_code": "$.style_code",
        "size": "$.size", "size_seq": "int($.size_seq)", "src_version": "$.version"}}]})


async def _collect(call: Any, mapping: ReadMapping, **kw: Any) -> tuple[list[tuple[Any, ...]], CrawlStats]:
    stats = CrawlStats()
    rows: list[tuple[Any, ...]] = []
    async for _entity, batch in crawl_rows(call, mapping, stats=stats, **kw):
        rows += batch
    return rows, stats


@aio
async def test_page_number_paging_stops_at_a_short_or_empty_page() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    rows, stats = await _collect(_lite_caller(LITE_DATA, log), _sku_mapping())
    assert len(rows) == 6 and stats.rows_seen == 6 and not stats.truncated
    # 6 rows at 2 per page: pages 1-3 are full, page 4 is empty and ends the paging
    assert [a["page"] for _t, a in log] == [1, 2, 3, 4] and all(a["page_size"] == 2 for _t, a in log)
    assert stats.pages == 4 and stats.first_at is not None and stats.last_at >= stats.first_at
    log.clear()
    rows, _ = await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(page_size=4))
    assert len(rows) == 6 and [a["page"] for _t, a in log] == [1, 2]  # page 2 is short (2 < 4)


@aio
async def test_max_pages_truncation_is_recorded() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    rows, stats = await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(max_pages=2))
    assert len(rows) == 4 and len(log) == 2
    assert stats.truncated == ["entities[0] (item)"]


@aio
async def test_cursor_paging() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    mapping = _sku_mapping(tool="list_cursor", page_param="cursor", page_size_param=None, cursor_path="$.next_cursor")
    rows, stats = await _collect(_lite_caller(LITE_DATA, log), mapping)
    assert len(rows) == 6 and [a.get("cursor") for _t, a in log] == [None, "2", "4"] and not stats.truncated
    log.clear()
    rows, stats = await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(
        tool="list_cursor", page_param="cursor", page_size_param=None, cursor_path="$.next_cursor", max_pages=2))
    assert len(rows) == 4 and stats.truncated == ["entities[0] (item)"]

    async def repeating(tool: str, args: dict[str, Any]) -> Any:
        log.append((tool, args))
        return {"items": [{"code": f"S-{len(log)}", "style_code": "X", "version": 1}], "next_cursor": "same"}

    log.clear()
    rows, stats = await _collect(repeating, mapping)
    assert len(rows) == 2 and not stats.truncated  # a repeated cursor ends the paging (no loop)


@aio
async def test_preview_max_pages_never_counts_as_truncation() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    rows, stats = await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(), max_pages=1)
    assert len(rows) == 2 and not stats.truncated and len(log) == 1


@aio
async def test_crawl_item_errors() -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    with pytest.raises(CrawlError) as err:
        await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(items_path="$.nope"))
    assert err.value.code == "items_invalid"
    with pytest.raises(CrawlError) as err:
        await _collect(_lite_caller(LITE_DATA, log), _sku_mapping(items_path="$.total"))
    assert err.value.code == "items_invalid"
    bad = {**LITE_DATA, "sku": [*LITE_DATA["sku"], {"code": "Z-1", "style_code": "Z", "size": "S", "size_seq": "x",
                                                     "version": 1}]}
    with pytest.raises(CrawlError) as err:
        await _collect(_lite_caller(bad, log), _sku_mapping())
    assert err.value.code == "mapping_failed" and "size_seq" in err.value.detail
    bad = {**LITE_DATA, "sku": [{"code": "Z-1", "style_code": "Z", "size": "S", "size_seq": 1, "version": -1}]}
    with pytest.raises(CrawlError) as err:
        await _collect(_lite_caller(bad, log), _sku_mapping())
    assert err.value.code == "data_invalid" and "src_version" in err.value.detail
    with pytest.raises(CrawlError) as err:
        await _collect(_lite_caller(LITE_DATA, log, fail={("sku", 2): "boom"}), _sku_mapping())
    assert err.value.code == "tool_error"


# ───────────────────────────── the foreign-erp-lite pack ─────────────────────────────


needs_packs = pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")


def _pack_mappings() -> tuple[ReadMapping, ActionMapping]:
    docs = load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))
    return ReadMapping.model_validate(docs["read"]), ActionMapping.model_validate(docs["action"])


@needs_packs
def test_pack_mappings_validate() -> None:
    pack = load_pack("foreign-erp-lite", REPO_PACKS)
    assert pack.mappings == {"connector": "merxa-lite", "read": "mappings/read.yaml",
                             "action": "mappings/action.yaml"}
    assert pack.summary()["mappings"] == ["read", "action"] and pack.connectors == []
    read, action = _pack_mappings()
    assert read.mapped_entities == ["location", "item", "po_line", "inventory_position", "demand_forecast",
                                    "allocation"]
    assert "transfer_order_line" not in read.mapped_entities  # /mcp-lite has no transfer-order lines
    assert action.mapped_ops == ["allocation.set_qty", "transfer_order.create"]
    from keelson_contracts.connectors import ToolClassification

    def cls(access: str, trust: str = "trusted_data", idem: str = "none") -> ToolClassification:
        return ToolClassification.model_validate({"access": access, "content_trust": trust, "egress": "internal",
                                                  "idempotency": idem})

    tools = {"list_items": cls("read"), "get_item": cls("read"), "update_item": cls("write", idem="best_effort"),
             "create_transfer_order": cls("write", idem="best_effort")}
    assert read_mapping_violations(read, tools=tools) == []
    from keelson_contracts.mapping import action_mapping_violations

    assert action_mapping_violations(action, tools=tools) == []
    # a crawl never calls a write tool, even if an admin edits the mapping to do so
    assert read_mapping_violations(read, tools={**tools, "list_items": cls("write")})


@needs_packs
def test_pack_mapping_files_stay_inside_the_pack(tmp_path: Path) -> None:
    pack_dir = tmp_path / "evil"
    pack_dir.mkdir()
    (tmp_path / "secret.yaml").write_text("kind: read\n", encoding="utf-8")
    for rel in ("../secret.yaml", "/etc/passwd.yaml", "mappings/../../secret.yaml", "x.py"):
        (pack_dir / "pack.yaml").write_text(json.dumps({"name": "evil", "mappings": {"read": rel}}), encoding="utf-8")
        with pytest.raises(PackError):
            load_pack_mappings(load_pack("evil", tmp_path))
    (pack_dir / "pack.yaml").write_text(json.dumps({"name": "evil", "mappings": {"read": "m.yaml"}}), encoding="utf-8")
    (pack_dir / "m.yaml").write_text("kind: action\nops: []\n", encoding="utf-8")
    with pytest.raises(PackError):  # wrong kind
        load_pack_mappings(load_pack("evil", tmp_path))
    (pack_dir / "pack.yaml").write_text(json.dumps({"name": "evil", "mappings": {"bogus": "m.yaml"}}),
                                        encoding="utf-8")
    with pytest.raises(PackError):
        load_pack("evil", tmp_path)


@needs_packs
@aio
async def test_pack_read_mapping_maps_real_shape_lite_rows() -> None:
    """The pack's read mapping over real-shape /mcp-lite answers: every row validates against
    scn/1.0, with the Merxa tier-B source ids, lookups resolved and clusters derived once."""
    read, _ = _pack_mappings()
    log: list[tuple[str, dict[str, Any]]] = []
    stats = CrawlStats()
    out: dict[str, list[dict[str, Any]]] = {}
    async for entity, batch in crawl_rows(_lite_caller(LITE_DATA, log), read, stats=stats):
        out.setdefault(entity, []).extend(dict(zip(scn.SCN_COLUMNS[entity], row, strict=True)) for row in batch)
    loc = {r["source_id"]: r for r in out["location"]}
    assert set(loc) == {"dc:DE1", "dc:NL1", "store:S-DE-001", "store:S-DE-002", "store:S-NL-001",
                        "cluster:DE-URB-A", "cluster:NL-URB-B"}
    assert loc["cluster:DE-URB-A"]["parent_code"] == "DE1" and loc["store:S-NL-001"]["parent_code"] == "NL-URB-B"
    assert loc["store:S-DE-001"]["country"] == "DE" and loc["dc:DE1"]["kind"] == "dc"
    items = {r["source_id"]: r for r in out["item"]}
    assert items["sku:PUF-W-BLK-M"]["family"] == "outerwear" and items["sku:PUF-W-BLK-M"]["retail_price_cents"] == 4999
    assert items["style:TEE-M-WHT"]["unit_cost_cents"] == 210 and len(items) == 8
    po = {r["source_id"]: r for r in out["po_line"]}
    assert set(po) == {"PO-1001#1", "PO-1001#2", "PO-1002#1"}
    assert po["PO-1001#2"]["supplier_code"] == "SUP-VN-01" and po["PO-1001#2"]["dest_dc_code"] == "DE1"
    assert str(po["PO-1001#2"]["ex_factory_date"]) == "2026-10-20" and po["PO-1002#1"]["factory_code"] is None
    assert po["PO-1002#1"]["eta_dc"] is None and po["PO-1002#1"]["qty_received"] == 400
    inv = {r["source_id"]: r for r in out["inventory_position"]}
    assert inv["dc/DE1/PUF-W-BLK-M"]["on_hand"] == 320 and inv["store/S-DE-001/PUF-W-BLK-M"]["location_kind"] == "store"
    fc = {r["source_id"]: r for r in out["demand_forecast"]}
    assert fc["PUF-W-BLK|S-DE-001|2026-W46"]["location_kind"] == "store"
    assert fc["PUF-W-BLK|NL-URB-B|2026-W46"]["location_kind"] == "cluster"
    al = {r["source_id"]: r for r in out["allocation"]}
    assert al["AL-2001"]["qty"] == 40 and al["AL-2002"]["store_code"] is None
    # lookups over the same source are crawled once: 1 style + 1 purchase_order lookup run
    lookup_calls = [a["type"] for _t, a in log]
    assert lookup_calls.count("purchase_order") == 1 and lookup_calls.count("style") == 2  # lookup + entity
    assert lookup_calls.count("store") == 2  # stores + derived clusters
    assert stats.lookups["po_supplier"] == 2 and stats.lookups["style_family"] == 2


def test_crawl_module_constants() -> None:
    assert scn_crawl.FLUSH_ROWS > 0 and scn_crawl.MAX_ROWS >= 2_000_000
