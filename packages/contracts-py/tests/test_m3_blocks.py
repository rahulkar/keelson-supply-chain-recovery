"""M3 canvas visuals (docs/contracts/80 §5): network_map@1, impact_graph@1, stockout_timeline@1,
scenario_compare@1 props + data_ref payloads, size limits, integer-only coordinates/money."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from examples import GOOD_DATA, SCORECARD, envelope, good_data, good_props, provenance
from keelson_contracts import blocks, llm
from keelson_contracts import optimizer as opt
from keelson_contracts.blocks import (
    BLOCK_DATA_REGISTRY,
    MAX_TIMELINE_ROWS,
    block_data_to_wire,
    make_block,
    parse_block,
    parse_block_data,
)
from keelson_contracts.llm import strict_schema_violations
from pydantic import ValidationError

M3_KEYS = ("network_map@1", "impact_graph@1", "stockout_timeline@1", "scenario_compare@1")


@pytest.mark.parametrize("key", sorted(GOOD_DATA))
def test_data_payloads_round_trip(key: str) -> None:
    parsed = parse_block_data(key, good_data(key))
    assert type(parsed) is BLOCK_DATA_REGISTRY[key]
    wire = block_data_to_wire(parsed)
    assert parse_block_data(key, json.dumps(wire)) == parsed
    assert wire == json.loads(json.dumps(wire))  # plain JSON


def test_network_map_keeps_wire_name_from() -> None:
    data = parse_block_data("network_map@1", good_data("network_map@1"))
    assert isinstance(data, blocks.NetworkMapData)
    link = data.links[0]
    assert link.from_ == "port:BDCGP" and link.path[0] == (91_800_000, 22_330_000)
    wire = block_data_to_wire(data)
    assert wire["links"][0]["from"] == "port:BDCGP" and "from_" not in wire["links"][0]
    assert data.model_dump(mode="json")["links"][0]["from"] == "port:BDCGP"  # serialize_by_alias
    assert wire["links"][0]["path"][0] == [91_800_000, 22_330_000]
    # Pythonic construction by field name also works.
    blocks.MapLink(id="x", kind="transfer", from_="dc:UK1", to="dc:UK1", path=[(0, 0), (1, 1)], status="ok")


def _mutated(key: str, path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    data = good_data(key)
    node: Any = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("key", "data", "why"),
    [
        ("network_map@1", _mutated("network_map@1", ("nodes", 0, "lat_e6"), 22.33), "float latitude"),
        ("network_map@1", _mutated("network_map@1", ("nodes", 0, "lat_e6"), "22330000"), "string latitude"),
        ("network_map@1", _mutated("network_map@1", ("nodes", 0, "lon_e6"), 180_000_001), "longitude range"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "path", 1), [18.47, -34.35]), "float path"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "path", 1), [1, 2, 3]), "3-tuple point"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "path"), [[1, 2]]), "single-point path"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "to"), "dc:XX1"), "unknown link node"),
        ("network_map@1", _mutated("network_map@1", ("links", 1, "id"), "BDCGP-UK1-CAPE"), "duplicate link id"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "kind"), "rail"), "link kind"),
        ("network_map@1", _mutated("network_map@1", ("links", 0, "units"), 1.5), "float units"),
        ("network_map@1", _mutated("network_map@1", ("nodes", 1, "kind"), "warehouse"), "node kind"),
        ("network_map@1",
         _mutated("network_map@1", ("footprint", "features", 0, "geometry", "coordinates", 0),
                  [[1, 1], [2, 2], [3, 1], [1, 2]]), "open ring"),
        ("network_map@1",
         _mutated("network_map@1", ("footprint", "features", 0, "geometry", "type"), "Circle"), "geometry type"),
        ("impact_graph@1", _mutated("impact_graph@1", ("nodes", 1, "units_at_risk"), 9000.0), "float units"),
        ("impact_graph@1", _mutated("impact_graph@1", ("edges", 0, "target"), "nope"), "unknown edge node"),
        ("stockout_timeline@1",
         _mutated("stockout_timeline@1", ("rows", 0, "cells", 2, "shortage_units"), 0), "short without units"),
        ("stockout_timeline@1",
         _mutated("stockout_timeline@1", ("rows", 0, "cells", 1, "shortage_units"), 5), "tight with units"),
        ("stockout_timeline@1",
         _mutated("stockout_timeline@1", ("rows", 0, "cells", 1, "week"), "2026-W44"), "duplicate week"),
        ("stockout_timeline@1",
         _mutated("stockout_timeline@1", ("rows", 0, "by_option"), {"opt-0": "2026-W99"}), "bad by_option week"),
        ("stockout_timeline@1",
         _mutated("stockout_timeline@1", ("rows", 0, "cells", 0, "status"), "fine"), "cell status"),
    ],
)
def test_data_payloads_reject(key: str, data: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError):
        parse_block_data(key, data)


def test_multipolygon_footprint() -> None:
    ring = [[1, 1], [2, 1], [2, 2], [1, 1]]
    data = _mutated("network_map@1", ("footprint", "features", 0, "geometry"),
                    {"type": "MultiPolygon", "coordinates": [[ring], [ring]]})
    parsed = parse_block_data("network_map@1", data)
    assert isinstance(parsed, blocks.NetworkMapData) and parsed.footprint is not None
    assert isinstance(parsed.footprint.features[0].geometry, blocks.FootprintMultiPolygon)


def test_graph_size_limit_in_block_data() -> None:
    node = {"kind": "cluster", "label": "c", "status": "ok", "units_at_risk": 0}
    ok = {"nodes": [{"id": f"n{i}", **node} for i in range(300)], "edges": []}
    assert len(parse_block_data("impact_graph@1", ok).nodes) == 300  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        parse_block_data("impact_graph@1", {"nodes": [{"id": f"n{i}", **node} for i in range(301)]})
    assert blocks.ImpactGraphData is opt.ImpactGraph


def test_timeline_row_limit() -> None:
    row = good_data("stockout_timeline@1")["rows"][0]
    rows = [{**copy.deepcopy(row), "id": f"r{i}"} for i in range(MAX_TIMELINE_ROWS)]
    assert len(parse_block_data("stockout_timeline@1", {"rows": rows}).rows) == 40  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        parse_block_data("stockout_timeline@1", {"rows": [*rows, {**row, "id": "r40"}]})
    with pytest.raises(ValidationError, match="row ids"):
        parse_block_data("stockout_timeline@1", {"rows": [row, row]})


def test_parse_block_data_unknown_key() -> None:
    with pytest.raises(KeyError, match="kpi_strip@1"):
        parse_block_data("kpi_strip@1", {})


@pytest.mark.parametrize("key", M3_KEYS)
def test_m3_blocks_are_canvas_only(key: str) -> None:
    with pytest.raises(ValidationError, match="canvas-only"):
        parse_block(envelope(key, placement="inline"))
    kind = key.split("@")[0]
    block = make_block(kind, good_props(key), title="x", provenance=provenance())
    assert block.placement == "canvas"
    # data-backed kinds get their own data endpoint by default; scenario_compare has none
    expected = f"/api/blocks/{block.id}/data" if key != "scenario_compare@1" else None
    assert block.data_ref == expected


@pytest.mark.parametrize("key", ["network_map@1", "impact_graph@1", "stockout_timeline@1"])
def test_data_backed_blocks_need_data_ref(key: str) -> None:
    with pytest.raises(ValidationError, match="data_ref"):
        parse_block({**envelope(key), "data_ref": None})


def test_scenario_compare_needs_no_data_ref() -> None:
    block = parse_block({**envelope("scenario_compare@1"), "data_ref": None})
    assert isinstance(block, blocks.ScenarioCompareBlock)
    option = block.props.options[1]
    assert option.scorecard == opt.Scorecard.model_validate(SCORECARD)  # same model as /v1/optimize
    assert block.props.axes.x == "incremental_cost_cents" and block.props.axes.y == "lost_units"
    defaults = blocks.ScenarioCompareProps.model_validate({"options": good_props("scenario_compare@1")["options"]})
    assert defaults.axes == blocks.CompareAxes()


def test_props_carry_only_summaries() -> None:
    """Large payloads stay out of the envelope: the props schemas have no node/link/row arrays."""
    for key in ("network_map@1", "impact_graph@1", "stockout_timeline@1"):
        props_fields = set(blocks.BLOCK_PROPS_REGISTRY[key].model_fields)
        assert not props_fields & {"nodes", "links", "edges", "rows"}, key


def test_llm_schemas_still_strict_after_m3_imports() -> None:
    """Importing the M3 models must not change the LLM structured-output schemas."""
    for model in llm.LLM_OUTPUT_MODELS:
        assert strict_schema_violations(model) == [], model.__name__
