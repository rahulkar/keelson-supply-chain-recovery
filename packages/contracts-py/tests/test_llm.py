"""LLM output schemas are strict-structured-output compatible (ADK + LiteLLM)."""

from __future__ import annotations

import copy
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from keelson_contracts.llm import (
    LLM_OUTPUT_MODELS,
    Claim,
    Findings,
    IntakeResult,
    Recommendation,
    strict_schema_violations,
)


def adk_enforce_strict_openai_schema(schema: dict[str, Any]) -> None:
    """Verbatim behaviour of ``google.adk.models.lite_llm._enforce_strict_openai_schema``
    (google-adk 2.11.0), copied so this package does not depend on ADK."""
    if not isinstance(schema, dict):
        return
    if "$ref" in schema:
        for key in list(schema.keys()):
            if key != "$ref":
                del schema[key]
        return
    if schema.get("type") == "object" and "properties" in schema:
        schema["additionalProperties"] = False
        schema["required"] = sorted(schema["properties"].keys())
    for defn in schema.get("$defs", {}).values():
        adk_enforce_strict_openai_schema(defn)
    for prop in schema.get("properties", {}).values():
        adk_enforce_strict_openai_schema(prop)
    for key in ("anyOf", "oneOf", "allOf"):
        for item in schema.get(key, []):
            adk_enforce_strict_openai_schema(item)
    if "items" in schema and isinstance(schema["items"], dict):
        adk_enforce_strict_openai_schema(schema["items"])


def adk_transformed(model: type[BaseModel]) -> dict[str, Any]:
    schema = copy.deepcopy(model.model_json_schema())
    adk_enforce_strict_openai_schema(schema)
    return schema


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_strict_compatible_as_authored(model: type[BaseModel]) -> None:
    assert strict_schema_violations(model) == []


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_adk_transformation_is_a_no_op(model: type[BaseModel]) -> None:
    """ADK forces required=all + additionalProperties=false; our schemas already are.

    If the transformation changed anything, the provider would enforce a schema
    different from the one Pydantic validates (e.g. a defaulted field becoming
    required), so we require them to be identical modulo stripped $ref siblings.
    """
    original = model.model_json_schema()
    transformed = adk_transformed(model)
    assert strict_schema_violations(transformed) == []

    def strip_ref_siblings(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return {"$ref": node["$ref"]}
            out = {k: strip_ref_siblings(v) for k, v in node.items()}
            if isinstance(out.get("required"), list):
                out["required"] = sorted(out["required"])  # ADK sorts; order is irrelevant
            return out
        if isinstance(node, list):
            return [strip_ref_siblings(v) for v in node]
        return node

    assert strip_ref_siblings(original) == transformed


@pytest.mark.parametrize("model", LLM_OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_nullable_fields_are_required(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        assert field.is_required(), f"{model.__name__}.{name} has a default"


# ─────────────── the checker itself must catch non-strict models ───────────────


class _Open(BaseModel):
    x: int


class _FreeDict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, str]


class _AnyField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any


class _Optional(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = None


class _ExtraAllow(BaseModel):
    model_config = ConfigDict(extra="allow")
    x: int


class _Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner: list[_Open]


@pytest.mark.parametrize(
    ("model", "needle"),
    [
        (_Open, "additionalProperties must be false"),
        (_FreeDict, "free-form dict"),
        (_AnyField, "untyped schema"),
        (_Optional, "not required"),
        (_ExtraAllow, "additionalProperties must be false (got True)"),
        (_Nested, "$defs/_Open: additionalProperties"),
    ],
)
def test_checker_flags_non_strict_models(model: type[BaseModel], needle: str) -> None:
    problems = strict_schema_violations(model)
    assert any(needle in p for p in problems), problems


def test_checker_flags_constraints() -> None:
    from pydantic import Field

    class _Constrained(BaseModel):
        model_config = ConfigDict(extra="forbid")
        code: str = Field(pattern="^A")

    assert any("pattern" in p for p in strict_schema_violations(_Constrained))


def test_free_dict_survives_adk_transformation_and_is_still_caught() -> None:
    assert any("free-form" in p for p in strict_schema_violations(adk_transformed(_FreeDict)))


# ─────────────────────────── behaviour of the schemas ──────────────────────────


def test_intake_roundtrip() -> None:
    raw = {
        "kind": "NEW_DISRUPTION",
        "summary": "Trim supplier delay",
        "disruption": {
            "event_family": "SUP",
            "event_type": "SUP.delay",
            "description": "Meghna Trims delayed 7 days",
            "entity_mentions": [{"type": "supplier", "text": "Meghna Trims"}],
            "delay_days": 7,
            "demand_change_pct": None,
            "start_date": None,
            "duration_days": None,
            "is_hypothetical": False,
        },
    }
    parsed = IntakeResult.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw


def test_nullable_fields_must_be_present() -> None:
    with pytest.raises(ValidationError, match="disruption"):
        IntakeResult.model_validate({"kind": "SMALLTALK", "summary": "hi"})


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "CHITCHAT", "summary": "x", "disruption": None},
        {"kind": "SMALLTALK", "summary": "x", "disruption": None, "extra": 1},
    ],
)
def test_intake_rejects_bad(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntakeResult.model_validate(bad)


def test_findings_and_claims() -> None:
    claim = {
        "text": "UK1 is short in {{ev:ev_1#/items/0/atp}}",
        "kind": "fact",
        "evidence_id": "ev_1",
        "json_pointer": "/items/0/atp",
        "calc_id": None,
        "confidence": "high",
    }
    findings = Findings.model_validate(
        {
            "task_id": "task-inventory_demand-r1",
            "status": "ok",
            "claims": [claim],
            "affected": [{"type": "dc", "code": "UK1", "label": "Thurrock"}],
            "gaps": [],
        }
    )
    assert findings.claims[0].is_cited
    uncited = Claim.model_validate({**claim, "evidence_id": None, "json_pointer": None})
    assert not uncited.is_cited


def test_recommendation() -> None:
    rec = Recommendation.model_validate(
        {
            "headline": "Rebalance between DCs",
            "bullets": ["Recovers {{kpi:opt-A.units_recovered}}"],
            "recommended_option_id": "opt-A",
            "why_not_others": [{"option_id": "opt-0", "reason": "Leaves stores short at peak"}],
            "risks": [],
            "follow_ups": [],
        }
    )
    assert rec.why_not_others[0].option_id == "opt-0"


def test_literal_enums_render_as_json_enum() -> None:
    schema = IntakeResult.model_json_schema()
    assert schema["properties"]["kind"]["enum"] == ["QUESTION", "NEW_DISRUPTION", "FOLLOWUP", "SMALLTALK"]
    kinds: Literal["fact", "inference"] = "fact"
    assert kinds in Claim.model_json_schema()["properties"]["kind"]["enum"]
