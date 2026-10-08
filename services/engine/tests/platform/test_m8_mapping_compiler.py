"""M8 DuckDB mapping compiler, unit (no database): the new functions and aggregates (semantics,
lazy errors), the hardened DuckDB session, constants that stay inert literals, the duplicate and
foreign-key checks before load, the explain plan and the static no-eval check of every new module
(docs/contracts/121-m8-mapping-compiler.md §2)."""

from __future__ import annotations

import ast
import copy
import os
import re
from pathlib import Path
from typing import Any

import pytest

from e1_env import HAVE_PACKS, REPO_PACKS
from e1_lite_server import LITE_DATA
from e1_mapping_fixtures import collect, compile_many, disagreements, interpret
from keelson_contracts import scn
from keelson_contracts.mapping import MappingPlan, ReadMapping
from keelson_engine.core import mapping_compiler as mc
from keelson_engine.core import mapping_eval as me
from keelson_engine.core.duckdb_session import open_duckdb
from keelson_engine.core.packs import load_pack, load_pack_mappings
from keelson_engine.core.scn_crawl import CrawlError

aio = pytest.mark.asyncio(loop_scope="session")
needs_packs = pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not available")

ITEM = {"code": "PO-1", "qty": "12", "price": "12.99", "eu": "24/11/2026", "wk": "2026W05", "day": "2026-11-24",
        "pad": "  Ab c\t", "flag": True, "n": 7, "f": 12.5, "none": None, "nested": {"a": 1}, "list": [1]}


@pytest.mark.parametrize(("expr", "want"), [
    ("cents($.price)", 1299), ("cents('12')", 1200), ("cents($.n)", 700), ("cents($.f)", 1250), ("cents('-0.5')", -50),
    ("cents('12.990')", 1299), ("cents('+3.1')", 310), ("cents($.none)", None),
    ("iso_week($.day)", "2026-W48"), ("iso_week($.wk)", "2026-W05"), ("iso_week('2020-W53')", "2020-W53"),
    ("iso_week('2021-01-01')", "2020-W53"), ("iso_week(' 2026-11-24T10:00:00Z ')", "2026-W48"),
    ("trim($.pad)", "Ab c"), ("upper($.pad)", "  AB C\t"), ("lower('ÀBC')", "Àbc"), ("upper('straße')", "STRAßE"),
    ("field('r', $.code, '$.family')", "outerwear"), ("field('r', $.missing, '$.family')", None),
    ("field('r', 'nope', '$.family')", None), ("field('r', $.code, '$.nested.a[1].b')", "deep"),
    ("field('r', 2, '$.price')", 7),
])
def test_new_functions(expr: str, want: Any) -> None:
    assert interpret(expr, ITEM) == ("ok", want)
    assert compile_many([expr], [ITEM])[0][0] == ("ok", want)


@pytest.mark.parametrize("expr", [
    "cents('12.999')", "cents('1e5')", "cents($.flag)", "cents($.nested)", "cents('12,5')", "cents(9007199254740991)",
    "iso_week('2021-W53')", "iso_week('2026-W00')", "iso_week($.n)", "iso_week('2026-02-30')", "trim($.n)",
    "upper($.flag)", "lower($.list)", "field('r', $.f, '$.family')", "field('r', $.nested, '$.family')",
])
def test_new_function_errors(expr: str) -> None:
    assert interpret(expr, ITEM)[0] == "error"
    assert compile_many([expr], [ITEM])[0][0][0] == "error"


@pytest.mark.parametrize("expr", [
    "coalesce('x', int('a'))", "if_present($.code, 'y', int('bad'))", "if_present($.none, int('bad'), 'n')",
    "$.missing + int('a')", "coalesce($.code, cents('x'))", "date(coalesce($.day, $.nested))",
])
def test_lazy_errors_never_surface(expr: str) -> None:
    """An argument that is not evaluated never fails — in both engines."""
    assert interpret(expr, ITEM)[0] == "ok"
    assert not disagreements([expr], [ITEM])


def test_python_whitespace_constant_is_complete() -> None:
    assert set(mc.PY_WHITESPACE) == {chr(c) for c in range(0x110000) if chr(c).isspace()}


# ───────────────────────────── hardening & literals ─────────────────────────────


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv('/etc/passwd')", "COPY (SELECT 1) TO '/tmp/x.csv'", "INSTALL httpfs", "LOAD httpfs",
    "SET enable_external_access = true", "SET memory_limit = '4GB'", "ATTACH '/tmp/x.db'", "SELECT * FROM glob('/*')",
    "SELECT * FROM read_text('/proc/self/environ')", "SET lock_configuration = false",
])
def test_duckdb_session_is_locked(sql: str) -> None:
    with open_duckdb(64, 1) as s, pytest.raises(Exception, match=r"(?i)permission|disabled|configuration|lock"):
        s.execute(sql).fetchall()


def test_duckdb_session_spills_and_cleans_up() -> None:
    """External access off does not stop spilling to the private temp dir, which is removed after."""
    with open_duckdb(32, 1) as s:
        tmp = s.tmp
        n = s.execute("SELECT count(*) FROM (SELECT md5(CAST(i AS VARCHAR)) AS h FROM range(1500000) t(i) "
                      "ORDER BY h)").fetchone()[0]
        assert n == 1_500_000 and os.path.isdir(tmp)
    assert not os.path.exists(tmp)


def _strip_literals(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def test_constants_stay_inert_literals() -> None:
    evil = ["'x\\'); DROP TABLE src_0000000000000000; --'", "'\\\\'", "'a\\\"b'", "\"'''\"", "'%s {0} $1 ?'"]
    out = compile_many(evil, [ITEM])
    assert [v for _ok, v in out[0]] == ["x'); DROP TABLE src_0000000000000000; --", "\\", 'a"b', "'''", "%s {0} $1 ?"]
    mapping = ReadMapping.model_validate({"entities": [{"entity": "location", "source": {
        "tool": "t", "page_param": None, "page_size": 1, "items_path": "$", "cursor_path": None, "max_pages": 1},
        "fields": {"source_id": "'x\\'); DROP TABLE out_location; --' + $.code", "kind": "'dc'", "code": "$.code",
                   "src_version": "0"}}]})
    sql = mc.compile_read_mapping(mapping).entities[0].sql
    assert "DROP TABLE" in sql and "DROP TABLE" not in _strip_literals(sql)
    with pytest.raises(mc.CompileError):
        mc.sql_str("a\x00b")


@needs_packs
def test_item_keys_and_table_names_never_reach_sql_as_text() -> None:
    """Item paths become generated columns (``p3_v``) and lookup tables generated names
    (``lk_<name>``): outside literals no vendor key or bare table name appears."""
    read = _pack()
    compiled = mc.compile_read_mapping(read)
    for sql in [*(e.sql for e in compiled.entities), *(lk.sql or "" for lk in compiled.lookups)]:
        bare = _strip_literals(sql)
        for key in ("cluster_code", "home_dc_code", "style_code", "po_code", "retail_price_cents", "items"):
            assert not re.search(rf"\b{key}\b", bare), key
        for name in read.lookups:
            assert not re.search(rf"\b{name}\b", bare), name


# ───────────────────────────── checks before load ─────────────────────────────


def _pack() -> ReadMapping:
    return ReadMapping.model_validate(load_pack_mappings(load_pack("foreign-erp-lite", REPO_PACKS))["read"])


@needs_packs
@aio
async def test_fk_error_and_warning_classification() -> None:
    data = copy.deepcopy(LITE_DATA)
    data["dc"] = [d for d in data["dc"] if d["code"] != "NL1"]
    with pytest.raises(CrawlError) as err:
        await collect("duckdb", _pack(), data)
    assert err.value.code == "fk_violation"
    assert err.value.detail == "inventory_position.location → location: dc:NL1"
    data = copy.deepcopy(LITE_DATA)
    data["store"] = [s for s in data["store"] if s["code"] != "S-DE-001"]
    data["inventory"] = [i for i in data["inventory"] if i["location_code"] != "S-DE-001"]  # an error FK
    rows, stats, _ = await collect("duckdb", _pack(), data)
    labels = {c["label"]: c for c in stats.checks}
    assert labels["allocation.store_code → location(store)"]["severity"] == "warning"
    assert labels["allocation.store_code → location(store)"]["examples"] == ["S-DE-001"]
    assert "demand_forecast.location → location" in labels
    assert len(rows["location"]) == 6


@aio
async def test_duplicate_source_id_fails_before_load() -> None:
    src = {"tool": "list_items", "args": {"type": "store"}, "page_param": "page", "page_size_param": "page_size",
           "page_size": 50, "items_path": "$.items", "cursor_path": None, "max_pages": 5}
    mapping = ReadMapping.model_validate({"entities": [{"entity": "location", "source": src, "fields": {
        "source_id": "'cluster:' + $.cluster_code", "kind": "'cluster'", "code": "$.cluster_code",
        "src_version": "1"}}]})
    with pytest.raises(CrawlError) as err:
        await collect("duckdb", mapping, LITE_DATA)
    assert err.value.code == "data_invalid"
    assert err.value.detail == "location: duplicate source_id cluster:DE-URB-A (mark derived rows dedupe)"


@aio
async def test_too_many_rows_and_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_engine.core import scn_crawl

    if not HAVE_PACKS:
        pytest.skip("data/packs not available")
    monkeypatch.setattr(scn_crawl, "row_limit", lambda: 10)
    from keelson_engine.core import mapping_exec

    monkeypatch.setattr(mapping_exec, "row_limit", lambda: 10)
    with pytest.raises(CrawlError) as err:
        await collect("duckdb", _pack(), LITE_DATA)
    assert err.value.code == "too_many_rows"


# ───────────────────────────── explain ─────────────────────────────


@needs_packs
def test_explain_foreign_erp_lite() -> None:
    plan = mc.explain(mc.compile_read_mapping(_pack()))
    assert isinstance(plan, MappingPlan) and len(plan.mapping_hash) == 64
    assert len(plan.sources) == 9  # 2 lookup sources (+ shared with entities) instead of 11 paging runs
    styles = next(s for s in plan.sources if s.source.args == {"type": "style"})
    assert styles.used_by == ["lookups.style_family", "lookups.style_colour", "lookups.style_price", "lookups.style_cost",
                              "entities[3] (item)"]
    stores = next(s for s in plan.sources if s.source.args == {"type": "store"})
    assert stores.used_by == ["entities[1] (location)", "entities[2] (location)"]
    assert "$.cluster_code" in stores.paths and "$.home_dc_code" in stores.paths
    sku = plan.entities[4]
    assert {j.table: j.calls for j in sku.joins} == {"style_colour": 1, "style_cost": 1, "style_family": 1,
                                                    "style_price": 1}
    cols = {c.column: c for c in sku.columns}
    assert cols["size_seq"].target_type == "integer" and cols["kind"].target_type.startswith("one of sku")
    assert cols["source_id"].required and not cols["family"].required and "CASE" in cols["family"].sql
    po = next(e for e in plan.entities if e.entity == "po_line")
    assert {c.column: c.target_type for c in po.columns}["ex_factory_date"] == "date"
    checks = {(c.code, c.label): c.severity for c in plan.checks}
    assert checks[("fk", "po_line.sku_code → item(sku)")] == "error"
    assert checks[("fk", "allocation.dc_code → location(dc)")] == "warning"
    assert ("fk", "lane_rate.lane_code → lane") not in checks
    assert all(sql.sql for sql in plan.entities)
    assert plan.warnings == []
    again = mc.explain(mc.compile_read_mapping(_pack()))
    assert again.model_dump() == plan.model_dump()  # deterministic


def test_every_scn_column_has_a_target_type() -> None:
    from keelson_engine.core.mapping_exec import column_kinds

    for entity in scn.SCN_ENTITIES:
        assert [c for c, _k in column_kinds(entity)] == list(scn.SCN_COLUMNS[entity])
        for column in scn.SCN_COLUMNS[entity]:
            assert mc._target_type(entity, column)
    assert dict(column_kinds("location"))["lat"] == "float" and dict(column_kinds("lane"))["via"] == "list"
    assert dict(column_kinds("po_line"))["eta_dc"] == "date"


# ───────────────────────────── static check ─────────────────────────────


def test_no_eval_in_the_m8_modules() -> None:
    core = Path(mc.__file__).parent
    names = [core / n for n in ("mapping_compiler.py", "mapping_exec.py", "duckdb_session.py", "scn_staging.py",
                                "mapping_profile.py", "mapping_heuristics.py", "mapping_proposals.py")]
    names.append(core.parent / "runtime" / "mapping_proposer.py")
    names.append(core.parent / "api" / "routes_admin_mappings.py")
    for path in names:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not calls & {"eval", "exec", "compile", "__import__", "setattr"}, path.name


def test_aggregate_rules() -> None:
    assert me.aggregate("sum", None, None, 3) == (3, int)
    assert me.aggregate("min", "b", str, "a") == ("a", str)
    for fn, cur, kind, value in (("sum", 1, int, "2"), ("min", 1, int, "x"), ("max", None, None, True),
                                 ("sum", 9007199254740991, int, 1), ("max", None, None, 1.5)):
        with pytest.raises(me.MappingEvalError):
            me.aggregate(fn, cur, kind, value)
