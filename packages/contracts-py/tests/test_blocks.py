"""Render block envelope, props models, registry and make_block."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from examples import BAD_PROPS, GOOD_PROPS, bad_props, envelope, good_props, provenance
from keelson_contracts import blocks
from keelson_contracts.blocks import (
    BLOCK_KEYS,
    BLOCK_PROPS_REGISTRY,
    M1_BLOCK_KEYS,
    M3_BLOCK_KEYS,
    M5_BLOCK_KEYS,
    M6_BLOCK_KEYS,
    KpiStripBlock,
    KpiStripProps,
    NarrativeProps,
    block_to_wire,
    make_block,
    parse_block,
)

M1_KEYS = {
    "kpi_strip@1",
    "approach_card@1",
    "recommended_option@1",
    "plan_diff@1",
    "approval_card@1",
    "commit_tracker@1",
    "narrative@1",
    "evidence_pack@1",
}
M3_KEYS = {"network_map@1", "impact_graph@1", "stockout_timeline@1", "scenario_compare@1"}
M5_KEYS = {"remediation_checklist@1"}
M6_KEYS = {"alert_origin@1"}
EXPECTED_KEYS = M1_KEYS | M3_KEYS | M5_KEYS | M6_KEYS


def test_registry_covers_every_known_kind() -> None:
    assert set(M1_BLOCK_KEYS) == M1_KEYS
    assert set(M3_BLOCK_KEYS) == M3_KEYS
    assert set(M5_BLOCK_KEYS) == M5_KEYS
    assert set(M6_BLOCK_KEYS) == M6_KEYS
    assert set(BLOCK_KEYS) == set(BLOCK_PROPS_REGISTRY) == EXPECTED_KEYS
    assert set(GOOD_PROPS) == EXPECTED_KEYS
    assert set(BAD_PROPS) == EXPECTED_KEYS


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
def test_props_accept_good_examples(key: str) -> None:
    model = BLOCK_PROPS_REGISTRY[key].model_validate(good_props(key))
    # JSON round trip is lossless.
    again = BLOCK_PROPS_REGISTRY[key].model_validate_json(model.model_dump_json())
    assert again == model


@pytest.mark.parametrize(
    ("key", "why", "mutation"),
    [(key, why, mutation) for key, cases in sorted(BAD_PROPS.items()) for why, mutation in cases],
)
def test_props_reject_bad_examples(key: str, why: str, mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BLOCK_PROPS_REGISTRY[key].model_validate(bad_props(key, mutation))


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
def test_envelope_union_discriminates_on_kind_and_version(key: str) -> None:
    block = parse_block(envelope(key))
    assert type(block) is blocks.BLOCK_ENVELOPE_REGISTRY[key]
    wire = block_to_wire(block)
    assert wire["data_ref"] == envelope(key)["data_ref"] and wire["provenance"]["as_of"] == "2026-10-05"
    assert parse_block(json.dumps(wire)) == block


def test_envelope_rejects_unknown_kind_or_version() -> None:
    data = envelope("kpi_strip@1")
    with pytest.raises(ValidationError, match="unknown block"):
        parse_block({**data, "v": 2})
    with pytest.raises(ValidationError, match="unknown block"):
        parse_block({**data, "kind": "agent_trace_graph"})
    with pytest.raises(ValidationError):
        parse_block({**data, "v": "1"})


def test_envelope_rejects_props_of_another_kind() -> None:
    data = envelope("kpi_strip@1")
    data["props"] = good_props("narrative@1")
    with pytest.raises(ValidationError):
        parse_block(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"id": "kpi-1"},
        {"placement": "sidebar"},
        {"provenance": {**provenance(), "kind": "rumour"}},
        {"provenance": {k: v for k, v in provenance().items() if k != "engine"}},
        {"extra": True},
    ],
)
def test_envelope_rejects_bad_fields(mutation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_block({**envelope("kpi_strip@1"), **mutation})


def test_evidence_pack_is_canvas_only() -> None:
    with pytest.raises(ValidationError, match="canvas"):
        parse_block(envelope("evidence_pack@1", placement="inline"))


class TestMakeBlock:
    def test_from_model(self) -> None:
        props = KpiStripProps.model_validate(good_props("kpi_strip@1"))
        block = make_block("kpi_strip", props, title="Exposure", provenance=provenance())
        assert isinstance(block, KpiStripBlock)
        assert block.id.startswith("blk_") and block.v == 1 and block.placement == "inline"
        assert block.props == props

    def test_from_mapping_with_stable_id(self) -> None:
        block = make_block(
            "evidence_pack",
            good_props("evidence_pack@1"),
            title="Evidence",
            placement="canvas",
            provenance=provenance("fact"),
            block_id="blk_case1_evidence",
            data_ref="/api/blocks/blk_case1_evidence/data",
        )
        assert block.id == "blk_case1_evidence"
        assert block_to_wire(block)["data_ref"] == "/api/blocks/blk_case1_evidence/data"

    def test_validates_props(self) -> None:
        with pytest.raises(ValidationError):
            make_block("kpi_strip", {"items": []}, title="x", provenance=provenance())

    def test_rejects_wrong_props_model(self) -> None:
        with pytest.raises(TypeError, match="KpiStripProps"):
            make_block("kpi_strip", NarrativeProps(markdown="hi"), title="x", provenance=provenance())

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(KeyError, match="agent_trace_graph@1"):
            make_block("agent_trace_graph", {}, title="x", provenance=provenance())
        with pytest.raises(KeyError):
            make_block("kpi_strip", good_props("kpi_strip@1"), title="x", provenance=provenance(), v=2)


def test_envelope_json_schema_is_a_closed_union() -> None:
    schema = blocks.BLOCK_ENVELOPE_ADAPTER.json_schema()
    assert len(schema["oneOf"]) == len(EXPECTED_KEYS)
    for name, definition in schema["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name
    kpi = schema["$defs"]["KpiStripBlock"]
    assert kpi["properties"]["kind"]["const"] == "kpi_strip"
    assert kpi["properties"]["v"]["const"] == 1
