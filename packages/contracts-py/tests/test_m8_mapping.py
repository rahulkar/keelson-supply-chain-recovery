"""M8 mapping contract extensions (``docs/contracts/121-m8-mapping-compiler.md`` §1): the new
functions (``field`` / ``cents`` / ``iso_week`` / ``trim`` / ``upper`` / ``lower``), record lookups,
``group_by`` with aggregates, the catalogue check of record sources, ``SCN_FOREIGN_KEYS``, the
explain / preview models and the source identity. Every M7 mapping keeps validating unchanged."""

from __future__ import annotations

import copy
from typing import Any, Literal, get_args, get_origin

import pytest
from pydantic import ValidationError

from keelson_contracts.connectors import ToolClassification
from keelson_contracts.mapping import (
    AGGREGATE_FUNCTIONS,
    EntityMapping,
    ExpressionError,
    MappingPlan,
    ReadMapping,
    ReadPreview,
    ReadPreviewRequest,
    ReadSource,
    expression_record_paths,
    expression_records,
    is_aggregate,
    mapping_hash,
    op_takes,
    parse_expression,
    parse_path,
    path_text,
    read_mapping_violations,
    source_key,
)
from keelson_contracts.scn import SCN_COLUMNS, SCN_FOREIGN_KEYS, SCN_ROW_MODELS

SRC: dict[str, Any] = {"tool": "list_items", "args": {"type": "store"}, "page_param": "page",
                       "page_size_param": "page_size", "page_size": 200, "items_path": "$.items", "cursor_path": None,
                       "max_pages": 50}
READ = {"access": "read", "content_trust": "trusted_data", "egress": "internal", "idempotency": "enforced"}


def _src(**kw: Any) -> dict[str, Any]:
    return {**copy.deepcopy(SRC), **kw}


def _cluster(**kw: Any) -> dict[str, Any]:
    em = {"entity": "location", "source": _src(), "group_by": ["$.cluster_code"], "fields": {
        "source_id": "'cluster:' + $.cluster_code", "kind": "'cluster'", "code": "$.cluster_code",
        "parent_code": "first($.home_dc_code)", "src_version": "count()"}}
    em.update(kw)
    return em


def _sku(**fields: str) -> dict[str, Any]:
    base = {"source_id": "'sku:' + $.code", "kind": "'sku'", "code": "$.code", "style_code": "$.style_code",
            "src_version": "$.version"}
    return {"entity": "item", "source": _src(args={"type": "sku"}), "fields": {**base, **fields}}


def test_new_functions_parse_and_check_their_constants() -> None:
    for text in ("field('style', $.style_code, '$.family')", "cents($.price)", "iso_week($.ship_date)",
                 "trim($.x)", "upper(lower($.x))", "int(field('po', $.po_code, '$.lines[0].qty'))",
                 "field('style', $.k, \"$['odd key']\")"):
        parse_expression(text)
    for text, part in (("field($.t, $.k, '$.x')", "table name constant"), ("field('Style', $.k, '$.x')", "table"),
                       ("field('s', $.k, $.x)", "path must be a constant"), ("field('s', $.k, 'x')", "JSONPath"),
                       ("field('s', $.k, '$.__class__')", "JSONPath"), ("cents()", "takes 1"),
                       ("iso_week($.a, $.b)", "takes 1"), ("first($.x)", "aggregate"), ("count()", "aggregate")):
        with pytest.raises(ExpressionError, match=part):
            parse_expression(text)
    ast = parse_expression("coalesce(field('style', $.style_code, '$.family'), field('sku', $.code, '$.f.g'))")
    assert expression_records(ast) == {"style", "sku"}
    assert [(t, path_text(p)) for t, p in expression_record_paths(ast)] == [("style", "$.family"), ("sku", "$.f.g")]


def test_aggregates_only_whole_and_only_with_group_by() -> None:
    assert set(AGGREGATE_FUNCTIONS) == {"first", "min", "max", "sum", "count"}
    for text in ("first($.x)", "min(int($.q))", "max($.d)", "sum(int($.q))", "count()"):
        assert is_aggregate(parse_expression(text, aggregates=True))
    for text in ("int(first($.x))", "first(first($.x))", "'a' + count()", "coalesce(min($.a), 1)"):
        with pytest.raises(ExpressionError, match="never nested"):
            parse_expression(text, aggregates=True)
    with pytest.raises(ExpressionError, match="takes 0"):
        parse_expression("count($.x)", aggregates=True)
    em = EntityMapping.model_validate(_cluster())
    assert em.parsed_group_by() and em.model_dump(mode="json")["group_by"] == ["$.cluster_code"]
    with pytest.raises(ValidationError, match="need group_by"):
        EntityMapping.model_validate({**_cluster(), "group_by": None})
    with pytest.raises(ValidationError, match="exclude each other"):
        EntityMapping.model_validate(_cluster(dedupe=True))
    bad = _cluster()
    bad["fields"]["name"] = "$.name"
    with pytest.raises(ValidationError, match=r"reads \$\.name, which is not in group_by"):
        EntityMapping.model_validate(bad)
    with pytest.raises(ValidationError):
        EntityMapping.model_validate(_cluster(group_by=["$.a", "$.b", "$.c", "$.d", "$.e"]))
    with pytest.raises(ValidationError):
        EntityMapping.model_validate(_cluster(group_by=["first($.a)"]))  # group keys are plain expressions


def test_m7_mappings_dump_unchanged() -> None:
    em = EntityMapping.model_validate(_sku())
    assert "group_by" not in em.model_dump(mode="json")
    m = ReadMapping.model_validate({"entities": [_sku(family="lookup('fam', $.style_code)")],
                                    "lookups": {"fam": {"kind": "static", "values": {"S1": "tops"}}}})
    assert set(m.model_dump(mode="json")) == {"kind", "entities", "lookups"}


def test_record_lookups_and_table_kinds() -> None:
    lookups = {"style": {"kind": "record", "source": _src(args={"type": "style"}), "key": "$.code"},
               "fam": {"kind": "source", "source": _src(args={"type": "style"}), "key": "$.code", "value": "$.family"}}
    ok = ReadMapping.model_validate({"entities": [_sku(family="field('style', $.style_code, '$.family')",
                                                       colour="lookup('fam', $.style_code)")], "lookups": lookups})
    assert ok.lookups["style"].kind == "record"
    for fields, part in (({"family": "lookup('style', $.style_code)"}, "lookup\\(\\) on record tables"),
                         ({"family": "field('fam', $.style_code, '$.family')"}, "field\\(\\) on value tables"),
                         ({"family": "field('nope', $.style_code, '$.family')"}, "unknown record tables")):
        with pytest.raises(ValidationError, match=part):
            ReadMapping.model_validate({"entities": [_sku(**fields)], "lookups": lookups})
    chained = copy.deepcopy(lookups)
    chained["style"]["key"] = "lookup('fam', $.code)"
    with pytest.raises(ValidationError, match="may not call lookup"):
        ReadMapping.model_validate({"entities": [_sku()], "lookups": chained})


def test_read_mapping_violations_cover_record_sources() -> None:
    lookups = {"style": {"kind": "record", "source": _src(tool="get_styles"), "key": "$.code"}}
    m = ReadMapping.model_validate({"entities": [_sku(family="field('style', $.style_code, '$.family')")],
                                    "lookups": lookups})
    tools = {"list_items": ToolClassification.model_validate(READ)}
    assert any("lookups.style" in p for p in read_mapping_violations(m, tools=tools))
    for cls in ({**READ, "access": "write"}, {**READ, "content_trust": "untrusted_text"}):
        tools["get_styles"] = ToolClassification.model_validate(cls)
        assert read_mapping_violations(m, tools=tools), cls
    tools["get_styles"] = ToolClassification.model_validate(READ)
    assert read_mapping_violations(m, tools=tools) == []


def _literal_options(entity: str, column: str) -> set[str]:
    ann = SCN_ROW_MODELS[entity].model_fields[column].annotation
    return {str(a) for a in get_args(ann)} if get_origin(ann) is Literal else set()


def test_scn_foreign_keys_name_real_columns() -> None:
    assert {fk.label for fk in SCN_FOREIGN_KEYS if fk.severity == "error"} == {
        "po_line.sku_code → item(sku)", "inventory_position.location → location"}  # the M3–M7 FK_CHECKS
    assert [fk.severity for fk in SCN_FOREIGN_KEYS] == sorted((fk.severity for fk in SCN_FOREIGN_KEYS),
                                                              key=lambda s: s != "error")
    for fk in SCN_FOREIGN_KEYS:
        assert len(fk.columns) == len(fk.target_columns) >= 1
        assert set(fk.columns) <= set(SCN_COLUMNS[fk.entity]), fk.label
        assert set(fk.target_columns) <= set(SCN_COLUMNS[fk.target_entity]), fk.label
        if fk.kinds:
            assert set(fk.kinds) <= _literal_options(fk.entity, "kind"), fk.label
        if fk.target_kinds:
            assert set(fk.target_kinds) <= _literal_options(fk.target_entity, "kind"), fk.label


def test_source_identity_and_hashes() -> None:
    a, b = ReadSource.model_validate(_src()), ReadSource.model_validate(_src(max_pages=51))
    assert len(source_key(a)) == 16 and source_key(a) == source_key(ReadSource.model_validate(_src()))
    assert source_key(a) != source_key(b)
    m = ReadMapping.model_validate({"entities": [_sku()]})
    assert len(mapping_hash(m)) == 64 and mapping_hash(m) == mapping_hash(ReadMapping.model_validate(m.model_dump()))
    assert path_text(parse_path("$['a'].b[2]['x y']")) == "$.a.b[2]['x y']"
    assert op_takes("allocation.set_qty", "target_ref") and not op_takes("transfer_order.create", "target_ref")


def test_plan_and_preview_models() -> None:
    key = "0123456789abcdef"
    plan = MappingPlan.model_validate({
        "mapping_hash": "a" * 64, "sources": [{"key": key, "source": SRC, "paths": ["$.code"], "used_by": ["x"]}],
        "lookups": [{"name": "style", "kind": "record", "source_key": key, "entries": None, "key": "$.code",
                     "value": None, "paths": ["$.family"]}],
        "entities": [{"where": "entities[0] (item)", "entity": "item", "source_key": key, "dedupe": False,
                      "group_by": None, "joins": [{"table": "style", "kind": "record", "calls": 2}],
                      "columns": [{"column": "code", "expression": "$.code", "aggregate": None, "target_type": "text",
                                   "required": True, "sql": "SELECT 1"}], "sql": "SELECT 1"}],
        "checks": [{"code": "fk", "label": "x", "entity": "item", "severity": "warning"}], "warnings": []})
    assert MappingPlan.model_validate(plan.model_dump(mode="json")) == plan and plan.engine == "duckdb"
    with pytest.raises(ValidationError):
        MappingPlan.model_validate({**plan.model_dump(mode="json"), "sources": [{**plan.sources[0].model_dump(
            mode="json"), "key": "XYZ"}]})
    req = ReadPreviewRequest()
    assert (req.source, req.max_pages, req.max_items_per_source, req.sample) == ("live", 1, 20_000, 5)
    for bad in ({"max_pages": 4}, {"max_items_per_source": 50_001}, {"sample": 21}, {"source": "cache"}):
        with pytest.raises(ValidationError):
            ReadPreviewRequest.model_validate(bad)
    preview = ReadPreview.model_validate({
        "ok": False, "error": None, "source": "staged",
        "staged": {"snapshot_id": "s1", "status": "failed", "fetched_from": None, "fetched_to": None},
        "entities": [{"entity": "item", "where": "entities[0] (item)", "rows": 1, "sample": [{"code": "A"}],
                      "error": "mapping_failed: x", "columns": [{"column": "code", "null_bp": 0, "distinct": 1,
                                                                 "failures": 2, "first_failure": "item 3: x"}]}],
        "checks": [{"code": "duplicate_source_id", "label": "item: unique source_id", "entity": "item",
                    "severity": "error", "violations": 1, "examples": ["sku:A"]}]})
    assert ReadPreview.model_validate(preview.model_dump(mode="json")) == preview
    # the M7 read-test answer is a valid preview (a superset of its fields)
    m7 = {"ok": True, "error": None, "entities": [{"entity": "item", "where": "entities[0] (item)", "rows": 2,
                                                   "sample": [], "error": None}]}
    assert ReadPreview.model_validate(m7).engine == "duckdb"
