"""M8 proposed-mapping contracts (``docs/contracts/121-m8-mapping-compiler.md`` §3): the strict LLM
output schema, the deterministic checks the engine runs before merging an LLM proposal, the drafts
(complete entities only) and the request."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.connectors import ToolClassification
from keelson_contracts.llm import strict_schema_violations
from keelson_contracts.mapping_proposal import (
    CONFIDENCE_BP,
    MappingProposal,
    MappingProposalLlm,
    MappingProposeRequest,
    ProposedEntity,
    SourceProfile,
    llm_args,
    mapping_proposal_llm_violations,
)

SRC: dict[str, Any] = {"tool": "list_items", "args": {"type": "allocation"}, "page_param": "page",
                       "page_size_param": "page_size", "page_size": 200, "items_path": "$.items", "cursor_path": None,
                       "max_pages": 10}
READ = {"access": "read", "content_trust": "trusted_data", "egress": "internal", "idempotency": "enforced"}
WRITE = {"access": "write", "content_trust": "trusted_data", "egress": "internal", "idempotency": "best_effort"}
TOOLS = {"list_items": ToolClassification.model_validate(READ), "get_item": ToolClassification.model_validate(READ),
         "update_item": ToolClassification.model_validate(WRITE),
         "list_notes": ToolClassification.model_validate({**READ, "content_trust": "untrusted_text"})}
PROFILE = SourceProfile.model_validate({"ref": "S1", "source": SRC, "variant": "allocation", "items_sampled": 2,
                                        "total_hint": 2, "paths": [
                                            {"path": "$.qty", "shape": "int", "types": [{"type": "int", "count": 2}],
                                             "null_count": 0, "distinct_count": 2, "unique": True, "max_len": 0,
                                             "examples": ["40", "25"]}]})
ALLOC_FIELDS = {"source_id": "$.code", "code": "$.code", "sku_code": "$.sku_code", "dc_code": "$.dc_code",
                "ship_week": "$.ship_week", "qty": "int($.qty)", "status": "$.status", "src_version": "$.version"}


def _arg(name: str, text: str | None = None, kind: str = "template", **kw: Any) -> dict[str, Any]:
    return {"name": name, "kind": kind, "text": text, "integer": None, "boolean": None, **kw}


def _op(**kw: Any) -> dict[str, Any]:
    op = {"op": "allocation.set_qty", "tool": "update_item",
          "args": [_arg("type", "allocation"), _arg("key", "{{target_ref}}"),
                   _arg("fields_json", '{"qty": "{{payload.qty}}"}', "json_template"),
                   _arg("expected_version", "{{expected_version}}")],
          "request_id_arg": "request_id", "readback_tool": "get_item",
          "readback_args": [_arg("type", "allocation"), _arg("key", "{{target_ref}}")],
          "readback_items_path": "$.item", "readback_match": [{"path": "$.qty", "template": "{{payload.qty}}"}],
          "confidence": "high", "rationale": "r"}
    op.update(kw)
    return op


def _llm(**kw: Any) -> MappingProposalLlm:
    body: dict[str, Any] = {"entities": [], "lookups": [], "ops": [], "injection_suspected": False}
    body.update(kw)
    return MappingProposalLlm.model_validate(body)


def _entity(fields: dict[str, str], **kw: Any) -> dict[str, Any]:
    return {"entity": "allocation", "source_ref": "S1", "dedupe": False, "group_by": [], "rationale": "r",
            "fields": [{"column": c, "expression": e, "confidence": "high", "rationale": "r"} for c, e in fields.items()],
            **kw}


def test_llm_output_is_strict_structured_output() -> None:
    assert strict_schema_violations(MappingProposalLlm) == []
    assert CONFIDENCE_BP == {"high": 8500, "medium": 6000, "low": 3000}


def test_llm_violations_list_what_the_engine_drops() -> None:
    assert mapping_proposal_llm_violations(_llm(entities=[_entity(ALLOC_FIELDS)], ops=[_op()]), profiles=[PROFILE],
                                           tools=TOOLS) == []
    out = _llm(entities=[_entity({"qty": "int($.qty)"}, source_ref="S9"),
                         _entity({"colour": "$.c", "qty": "1.5", "code": "$.__class__", "status": "lookup($.x)"}),
                         _entity({"qty": "first($.qty)"}),
                         _entity({"qty": "first($.qty)"}, group_by=["$.code"], dedupe=True)],
               ops=[_op(tool="nope"), _op(tool="list_items"), _op(op="foo.bar"), _op(readback_tool="update_item"),
                    _op(readback_tool="list_notes"), _op(args=[_arg("fields_json", '{"qty": 1.5}', "json_template")]),
                    _op(readback_match=[{"path": "qty", "template": "{{payload.qty}}"}]),
                    _op(args=[_arg("key", "{{payload.nope}}")])],
               lookups=[{"name": "Bad", "kind": "static", "source_ref": None, "key": None, "value": None, "values": []},
                        {"name": "rec", "kind": "record", "source_ref": "S1", "key": None, "value": None,
                         "values": []},
                        {"name": "src", "kind": "source", "source_ref": "S1", "key": "$.code",
                         "value": "lookup('x', $.y)", "values": []}])
    found = mapping_proposal_llm_violations(out, profiles=[PROFILE], tools=TOOLS)
    where = [w for w, _p in found]
    assert "entities[0]" in where  # unknown source_ref
    assert {"entities[1].fields.colour", "entities[1].fields.qty", "entities[1].fields.code",
            "entities[1].fields.status", "entities[2].fields.qty"} <= set(where)
    assert any(w == "entities[3]" and "exclude" in p for w, p in found)
    assert {f"ops[{i}]" for i in range(8)} <= set(where)
    assert {"lookups[0]", "lookups[1]", "lookups[2]"} <= set(where)


def test_llm_args() -> None:
    args = llm_args(MappingProposalLlm.model_validate({"entities": [], "lookups": [], "ops": [_op()],
                                                       "injection_suspected": False}).ops[0].args)
    assert args == {"type": "allocation", "key": "{{target_ref}}", "fields_json": {"$json": {"qty": "{{payload.qty}}"}},
                    "expected_version": "{{expected_version}}"}
    from keelson_contracts.mapping_proposal import LlmArg

    lit = [LlmArg.model_validate(_arg("n", None, "integer", integer=3)),
           LlmArg.model_validate(_arg("b", None, "boolean", boolean=True)), LlmArg.model_validate(_arg("z", None, "null"))]
    assert llm_args(lit) == {"n": 3, "b": True, "z": None}
    for bad in ([_arg("a", "x"), _arg("a", "y")], [_arg("a", None)], [_arg("a", "{bad", "json_template")],
                [_arg("a", None, "integer")], [_arg("a", "{{nope.x}}")]):
        with pytest.raises(ValueError):
            llm_args([LlmArg.model_validate(a) for a in bad])


def _proposal(entities: list[dict[str, Any]], lookups: list[dict[str, Any]] | None = None,
              ops: list[dict[str, Any]] | None = None) -> MappingProposal:
    return MappingProposal.model_validate({
        "id": "p1", "connector": "erp", "status": "ready", "proposer": "heuristic", "model_alias": None,
        "created_by": "nadia", "created_at": "2026-10-08T10:00:00Z", "inputs_hash": "0" * 64, "base_versions": {},
        "profiles": [PROFILE.model_dump(mode="json")], "read": {"entities": entities, "lookups": lookups or []},
        "action": {"ops": ops or []}})


def _proposed(fields: dict[str, str], missing: list[str] | None = None) -> dict[str, Any]:
    return {"entity": "allocation", "source_ref": "S1", "source": SRC, "confidence_bp": 7000, "origin": "heuristic",
            "rationale": "r", "missing_required": missing or [],
            "fields": [{"column": c, "expression": e, "confidence_bp": 7000, "origin": "heuristic", "rationale": "r"}
                       for c, e in fields.items()]}


def test_drafts_hold_complete_entities_and_used_lookups_only() -> None:
    incomplete = {k: v for k, v in ALLOC_FIELDS.items() if k != "qty"}
    stat = {"name": "st", "table": {"kind": "static", "values": {"P": "planned"}}, "source_ref": None,
            "confidence_bp": 6000, "origin": "llm", "rationale": "r"}
    unused = {**copy.deepcopy(stat), "name": "unused"}
    fields = {**ALLOC_FIELDS, "status": "lookup('st', $.status, 'planned')"}
    p = _proposal([_proposed(fields), _proposed(incomplete, ["qty"])], lookups=[stat, unused])
    draft = p.read_draft()
    assert draft is not None and len(draft.entities) == 1 and set(draft.lookups) == {"st"}
    assert _proposal([_proposed(incomplete, ["qty"])]).read_draft() is None
    assert p.action_draft() is None
    op = {"mapping": {"op": "allocation.set_qty", "tool": "update_item",
                      "args": {"type": "allocation", "key": "{{target_ref}}", "fields_json": {"$json": {
                          "qty": "{{payload.qty}}"}}, "expected_version": "{{expected_version}}"},
                      "idempotency": "best_effort", "request_id_arg": "request_id",
                      "readback": {"tool": "get_item", "args": {"type": "allocation", "key": "{{target_ref}}"},
                                   "items_path": "$.item", "match": {"$.qty": "{{payload.qty}}"}}},
          "confidence_bp": 3000, "origin": "heuristic", "rationale": "r", "generic": True}
    action = _proposal([], ops=[op]).action_draft()
    assert action is not None and action.mapped_ops == ["allocation.set_qty"]
    with pytest.raises(ValidationError, match="no columns"):
        ProposedEntity.model_validate(_proposed({**ALLOC_FIELDS, "colour": "$.c"}))
    dup = _proposed(ALLOC_FIELDS)
    dup["fields"].append(copy.deepcopy(dup["fields"][0]))
    with pytest.raises(ValidationError, match="once"):
        ProposedEntity.model_validate(dup)


def test_propose_request() -> None:
    req = MappingProposeRequest.model_validate({"kinds": ["read"]})
    assert (req.proposer, req.sample, req.entities, req.ops, req.sources) == ("auto", True, None, None, [])
    for bad in ({"kinds": []}, {"kinds": ["read", "read"]}, {"kinds": ["write"]}, {"kinds": ["read"], "proposer": "llm"},
                {"kinds": ["read"], "entities": ["warehouse"]},
                {"kinds": ["read"], "sources": [{"tool": "t", "args": {}}] * 17}):
        with pytest.raises(ValidationError):
            MappingProposeRequest.model_validate(bad)
