"""M8 mapping compiler vs the normative interpreter, differential (no database): seeded random
expressions over a matrix of item values agree on value / ``null`` / error, and whole read mappings
(the ``foreign-erp-lite`` pack, record lookups, ``group_by`` aggregates, mutated item sets) give
identical rows in identical order or the identical ``CrawlError`` code **and message**
(docs/contracts/121-m8-mapping-compiler.md §2)."""

from __future__ import annotations

import copy
import random
import string
from typing import Any

import pytest

from e1_env import HAVE_PACKS, REPO_PACKS
from e1_lite_server import LITE_DATA
from e1_mapping_fixtures import collect, disagreements, outcome
from keelson_contracts.mapping import (
    ExpressionError,
    ReadMapping,
    expression_lookups,
    expression_records,
    parse_expression,
)
from keelson_engine.core.packs import load_pack, load_pack_mappings

aio = pytest.mark.asyncio(loop_scope="session")
needs_packs = pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")

ITEMS: list[Any] = [
    {"code": "PO-1", "n": 12, "s": "12", "f": 12.99, "b": True, "d": "2026-11-24", "w": "2026-W05", "m": "12.990",
     "o": {"a": [1, {"b": "deep"}]}, "l": ["x"], "x y": "sp", "e": ""},
    {"code": "2", "n": -7, "s": "+007", "f": 0.1, "b": False, "d": " 2026-02-29 ", "w": "2026W53", "m": "12.999",
     "o": {}, "l": [], "e": "x"},
    {"code": 12, "n": 2**60, "s": "1.5", "f": 1e20, "b": None, "d": "24/11/2026", "w": "2020-W53", "m": "-0.5",
     "o": [1], "l": "x"},
    {"code": True, "n": 9007199254740991, "s": "ABC ß", "f": -0.0, "d": "2026-11-24T10:00:00Z", "w": "2021-W53",
     "m": 7, "o": None},
    {"code": None, "s": "", "d": "0000-01-01", "w": "2026-13-01", "m": "abc", "n": 0},
    {"code": "x", "s": "　 2026-01-01 ", "d": "2024-02-29", "w": "2024-12-30", "m": "1e5", "n": 9007199254740992},
    {"code": "true", "s": " x\t", "d": "31/02/2026", "w": "  2027-01-03 ", "m": 12.5, "n": -9007199254740991},
    {},
    "a string item",
    [1, 2],
    {"code": {"a": 1}, "s": "9999999999999999", "d": "9999-12-31", "w": "0001-W01", "m": "99999999999999.99"},
    {"code": 1.5, "s": "-0", "d": "2026-1-1", "w": " 2026-W01 ", "m": ".5", "b": True, "f": float("nan")},
    {"code": "PO-1", "s": "12345678901234567", "d": "2026-11-24 23:59", "w": "2026-W00", "m": "+3.10", "n": 2},
    {"code": "ß", "s": "aBcé", "d": "11.24.2026", "w": "2026-W52", "m": "-12", "e": None},
]

PATHS = ["$.code", "$.n", "$.s", "$.f", "$.b", "$.d", "$.w", "$.m", "$.o", "$.l", "$.e", "$.missing", "$.o.a[1].b",
         "$.l[0]", "$['x y']", "$"]
CONSTS = ["'PO-1'", "'12'", "''", "'a\\'b'", "'2026-11-24'", "'24/11/2026'", "'2026-W05'", "' x '", "'true'", "'x'",
          "'12.5'", "'2'", "0", "12", "-7", "9007199254740991", "true", "false", "null"]
FORMATS = ["'DD/MM/YYYY'", "'YYYY-MM-DD'", "'MM.DD.YYYY'", "'YYYY MM DD'"]
RECORD_PATHS = ["'$.family'", "'$.price'", "'$.nested.a[1].b'", "'$.flag'", "'$.nested'", "'$.missing'"]


def _expr(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.3:
        return rng.choice(PATHS) if rng.random() < 0.6 else rng.choice(CONSTS)
    kind = rng.choice(["int", "str", "date", "date2", "lookup", "lookup3", "coalesce", "if_present", "concat", "field",
                       "cents", "iso_week", "trim", "upper", "lower"])
    sub = lambda: _expr(rng, depth - 1)  # noqa: E731
    if kind == "date2":
        return f"date({sub()}, {rng.choice(FORMATS)})"
    if kind == "lookup":
        return f"lookup('t', {sub()})"
    if kind == "lookup3":
        return f"lookup('t', {sub()}, {rng.choice(CONSTS)})"
    if kind == "coalesce":
        return f"coalesce({', '.join(sub() for _ in range(rng.randint(2, 3)))})"
    if kind == "if_present":
        return f"if_present({sub()}, {sub()}, {sub()})"
    if kind == "concat":
        return " + ".join(f"({sub()})" for _ in range(rng.randint(2, 3)))
    if kind == "field":
        return f"field('r', {sub()}, {rng.choice(RECORD_PATHS)})"
    return f"{kind}({sub()})"


def _valid(texts: list[str]) -> list[str]:
    """Expressions the contract accepts in a mapping whose only tables are the static ``t`` and
    the record ``r`` (a mapping naming another table never validates, so it never compiles)."""
    out = []
    for t in texts:
        try:
            ast = parse_expression(t)
        except ExpressionError:
            continue
        if expression_lookups(ast) - {"t"} or expression_records(ast) - {"r"}:
            continue
        out.append(t)
    return out


@pytest.mark.parametrize("chunk", range(12))
def test_random_expressions_agree(chunk: int) -> None:
    """6,000 seeded random expressions (12 chunks of 500) over 14 items of every value shape."""
    rng = random.Random(1210 + chunk)
    exprs = _valid([_expr(rng, rng.randint(1, 4)) for _ in range(500)])
    assert len(exprs) > 450
    for start in range(0, len(exprs), 25):
        bad = disagreements(exprs[start:start + 25], ITEMS)
        assert not bad, bad[:5]


def test_m7_fuzz_soup_agrees() -> None:
    """The M7 parser fuzz (token soups and mutations): whatever parses, both engines agree on."""
    rng = random.Random(110)
    tokens = ["$", ".", "[", "]", "(", ")", "+", ",", "'", '"', "\\", "$.code", "$.nested", "$.a[0]", "int", "str",
              "date", "lookup", "coalesce", "if_present", "null", "true", "false", "'x'", "12", "-3", " ", "__",
              "$['k']", "'DD/MM/YYYY'", "'t'", "é", "0", "cents", "iso_week", "trim", "upper", "lower", "field",
              "'r'", "'$.family'"]
    seeds = ["$.code + '#' + $.n", "coalesce($.missing, int($.s))", "date($.d, 'DD/MM/YYYY')",
             "if_present($.code, lookup('t', $.code, 'd'), 'n')", "$.o.a[1].b", "field('r', $.code, '$.family')",
             "cents($.m)", "iso_week($.w)", "upper(trim($.s))"]
    alphabet = string.printable + "éß€ "
    texts: list[str] = []
    for i in range(6000):
        mode = i % 3
        if mode == 0:
            texts.append("".join(rng.choice(tokens) for _ in range(rng.randint(1, 14))))
        elif mode == 1:
            s = list(rng.choice(seeds))
            for _ in range(rng.randint(1, 4)):
                pos = rng.randrange(len(s) + 1)
                if rng.random() < 0.5 and s:
                    del s[min(pos, len(s) - 1)]
                else:
                    s.insert(pos, rng.choice(alphabet))
            texts.append("".join(s))
        else:
            texts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))))
    exprs = sorted(set(_valid(texts)))
    assert len(exprs) > 150
    for start in range(0, len(exprs), 25):
        bad = disagreements(exprs[start:start + 25], ITEMS)
        assert not bad, bad[:5]


# ───────────────────────────── whole mappings ─────────────────────────────


def _pack_read() -> ReadMapping:
    return ReadMapping.model_validate(load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))["read"])


@needs_packs
@aio
async def test_pack_mapping_rows_identical_and_sources_fetched_once() -> None:
    read = _pack_read()
    want, istats, ilog = await collect("interpreter", read, LITE_DATA)
    got, cstats, clog = await collect("duckdb", read, LITE_DATA)
    assert got == want  # identical tuples in identical order
    assert {k: len(v) for k, v in got.items()} == {"location": 7, "item": 8, "po_line": 3, "inventory_position": 3,
                                                   "demand_forecast": 2, "allocation": 2}
    types = [a["type"] for _t, a in clog]
    assert types.count("style") == 1 and types.count("store") == 1 and types.count("purchase_order") == 1
    assert cstats.pages < istats.pages and cstats.rows_seen == istats.rows_seen
    assert cstats.lookups == istats.lookups and cstats.checks == []
    assert len(cstats.sources) == 9 and {s["tool"] for s in cstats.sources} == {"list_items"}


def _mutate(path: tuple[str, int, str], value: Any) -> dict[str, list[dict[str, Any]]]:
    data = copy.deepcopy(LITE_DATA)
    if value is _DROP:
        del data[path[0]][path[1]][path[2]]
    else:
        data[path[0]][path[1]][path[2]] = value
    return data


_DROP = object()


@needs_packs
@aio
@pytest.mark.parametrize(("path", "value"), [
    (("po_line", 1, "qty_ordered"), "1.5"),          # int() error
    (("po_line", 2, "eta_dc"), "2026-02-30"),        # date() calendar error
    (("inventory", 0, "on_hand"), -5),               # scn/1.0 refuses the row (data_invalid)
    (("sku", 3, "version"), _DROP),                  # missing key → required src_version missing
    (("style", 0, "code"), {"a": 1}),                # container lookup key → source lookup error
    (("purchase_order", 1, "ex_factory_date"), 5),   # date() of an integer through a lookup
    (("forecast", 1, "iso_week"), "2026-W60"),       # not an ISO week (row model refuses it)
    (("store", 0, "cluster_code"), 12),              # integer in a concat (cluster name)
    (("dc", 1, "version"), True),                    # boolean src_version
    (("allocation", 0, "qty"), 9007199254740992),    # int() out of range
])
async def test_mutated_items_fail_identically(path: tuple[str, int, str], value: Any) -> None:
    read = _pack_read()
    data = _mutate(path, value)
    want = await outcome("interpreter", read, data)
    got = await outcome("duckdb", read, data)
    assert got == want
    assert isinstance(want, tuple), want  # every mutation is a failure


def _clusters(**extra: Any) -> dict[str, Any]:
    src = {"tool": "list_items", "args": {"type": "store"}, "page_param": "page", "page_size_param": "page_size",
           "page_size": 2, "items_path": "$.items", "cursor_path": None, "max_pages": 50}
    return {"entity": "location", "source": src, "group_by": ["$.cluster_code"], "fields": {
        "source_id": "'cluster:' + $.cluster_code", "kind": "'cluster'", "code": "$.cluster_code",
        "parent_code": "first($.home_dc_code)", "name": "min($.name)", "src_version": "count()", **extra}}


def _inventory_by_sku(**fields: str) -> dict[str, Any]:
    src = {"tool": "list_items", "args": {"type": "inventory"}, "page_param": "page", "page_size_param": "page_size",
           "page_size": 2, "items_path": "$.items", "cursor_path": None, "max_pages": 50}
    return {"entity": "inventory_position", "source": src, "group_by": ["$.sku_code", "$.location_type"], "fields": {
        "source_id": "$.location_type + '/ALL/' + $.sku_code", "location_kind": "$.location_type",
        "location_code": "first($.location_code)", "sku_code": "$.sku_code", "on_hand": "sum($.on_hand)",
        "allocated": "max($.allocated)", "reserved": "min($.reserved)", "in_transit": "sum(int($.in_transit))",
        "src_version": "count()", **fields}}


def _sites() -> list[dict[str, Any]]:
    """DC and store locations (the inventory rows reference them: an error-level FK)."""
    out = []
    for kind in ("dc", "store"):
        src = {"tool": "list_items", "args": {"type": kind}, "page_param": "page", "page_size_param": "page_size",
               "page_size": 50, "items_path": "$.items", "cursor_path": None, "max_pages": 5}
        out.append({"entity": "location", "source": src, "fields": {
            "source_id": f"'{kind}:' + $.code", "kind": f"'{kind}'", "code": "$.code", "src_version": "$.version"}})
    return out


def _grouped() -> ReadMapping:
    return ReadMapping.model_validate({"entities": [_clusters(), _inventory_by_sku(), *_sites()]})


@aio
async def test_group_by_aggregates_agree() -> None:
    mapping = _grouped()
    data = copy.deepcopy(LITE_DATA)
    data["store"].append({"code": "S-XX-001", "name": "No cluster", "market_code": "XX", "city": "Nowhere",
                          "grade": "C", "cluster_code": None, "home_dc_code": "DE1", "version": 1})
    want, istats, _ = await collect("interpreter", mapping, data)
    got, cstats, _ = await collect("duckdb", mapping, data)
    assert got == want
    loc = {r[0]: r for r in got["location"] if r[1] == "cluster"}
    assert set(loc) == {"cluster:DE-URB-A", "cluster:NL-URB-B"}
    assert loc["cluster:DE-URB-A"][8] == "DE1" and loc["cluster:DE-URB-A"][-1] == 2  # parent, count()
    assert istats.skipped == cstats.skipped == {"entities[0] (location)": 1}
    inv = {r[0]: r for r in got["inventory_position"]}
    assert inv["dc/ALL/PUF-W-BLK-M"][4] == 335 and inv["dc/ALL/PUF-W-BLK-M"][7] == 60  # on_hand, in_transit sums


@aio
@pytest.mark.parametrize(("table", "index", "column", "value"), [
    ("inventory", 1, "on_hand", "15"),     # sum() of text
    ("inventory", 1, "allocated", "0"),    # max() mixing integers and text
    ("inventory", 0, "reserved", True),    # min() of a boolean
    ("inventory", 1, "on_hand", 9007199254740991),  # sum() total out of range
    ("inventory", 0, "sku_code", 1.5),     # float group key
    ("store", 1, "home_dc_code", {"x": 1}),  # first() keeps the first item: a later container is fine…
])
async def test_group_by_errors_agree(table: str, index: int, column: str, value: Any) -> None:
    mapping = _grouped()
    data = copy.deepcopy(LITE_DATA)
    data[table][index][column] = value
    assert await outcome("duckdb", mapping, data) == await outcome("interpreter", mapping, data)


@needs_packs
@aio
async def test_record_lookups_equal_the_pack_source_lookups() -> None:
    """The deferred pack rewrite, proven here: two record lookups + field() give exactly the rows
    of the pack's eight source lookups (both engines)."""
    raw = load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))["read"]
    pack = ReadMapping.model_validate(raw)
    styles = raw["lookups"]["style_family"]["source"]
    pos = raw["lookups"]["po_supplier"]["source"]
    rewritten = copy.deepcopy(raw)
    rewritten["lookups"] = {"style": {"kind": "record", "source": styles, "key": "$.code"},
                            "purchase_order": {"kind": "record", "source": pos, "key": "$.code"}}
    sku = next(e for e in rewritten["entities"] if e["source"]["args"] == {"type": "sku"})
    for col, attr in (("family", "family"), ("colour", "colour"), ("retail_price_cents", "retail_price_cents"),
                      ("unit_cost_cents", "unit_cost_cents")):
        sku["fields"][col] = f"field('style', $.style_code, '$.{attr}')"
    po = next(e for e in rewritten["entities"] if e["entity"] == "po_line")
    for col, attr in (("supplier_code", "supplier_code"), ("factory_code", "factory_code"),
                      ("dest_dc_code", "dest_dc_code")):
        po["fields"][col] = f"field('purchase_order', $.po_code, '$.{attr}')"
    po["fields"]["ex_factory_date"] = "date(field('purchase_order', $.po_code, '$.ex_factory_date'))"
    record = ReadMapping.model_validate(rewritten)
    want, _, _ = await collect("interpreter", pack, LITE_DATA)
    for engine in ("interpreter", "duckdb"):
        got, stats, _ = await collect(engine, record, LITE_DATA)
        assert got == want, engine
        assert stats.lookups == {"style": 2, "purchase_order": 2}


@aio
async def test_boolean_lookup_keys_match_now() -> None:
    """M8 key fix: a source lookup keyed by a boolean is found by lookup() (both sides 'true')."""
    src = {"tool": "list_items", "args": {"type": "dc"}, "page_param": "page", "page_size_param": "page_size",
           "page_size": 50, "items_path": "$.items", "cursor_path": None, "max_pages": 5}
    sku_src = {**src, "args": {"type": "sku"}}
    mapping = ReadMapping.model_validate({
        "lookups": {"flag": {"kind": "source", "source": src, "key": "$.flag", "value": "$.code"}},
        "entities": [{"entity": "item", "source": sku_src, "fields": {
            "source_id": "'sku:' + $.code", "kind": "'sku'", "code": "$.code", "family": "lookup('flag', true)",
            "src_version": "$.version"}}]})
    data = copy.deepcopy(LITE_DATA)
    data["dc"][0]["flag"] = True
    from e1_lite_server import COLUMNS

    COLUMNS["dc"] = (*COLUMNS["dc"], "flag")
    try:
        for engine in ("interpreter", "duckdb"):
            rows, _, _ = await collect(engine, mapping, data)
            assert {r[4] for r in rows["item"]} == {"DE1"}, engine
    finally:
        COLUMNS["dc"] = tuple(c for c in COLUMNS["dc"] if c != "flag")
