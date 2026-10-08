"""Regression tests from the independent M3 contracts review: data_ref confinement of the
canvas visuals, snapshot part URL shape + host allow-list (SSRF), the changes_since resync
flag, and transfer actions that would not compile into a change set."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from examples import envelope, good_props, provenance
from test_m3_optimizer import V2
from test_optimizer_models import problem
from keelson_contracts import blocks, scn
from keelson_contracts import optimizer as opt
from keelson_contracts.blocks import block_data_ref, make_block, parse_block
from pydantic import ValidationError

DATA_KEYS = ("network_map@1", "impact_graph@1", "stockout_timeline@1")

# ───────────────────────────── data_ref ─────────────────────────────


@pytest.mark.parametrize("key", DATA_KEYS)
@pytest.mark.parametrize(
    "data_ref",
    [
        "https://evil.example/api/blocks/x/data",
        "//evil.example/api/blocks/x/data",
        "/api/blocks/blk_other/data",  # another block's payload
        "/api/cases/1",
        "javascript:alert(1)",
    ],
)
def test_m3_data_ref_must_be_own_endpoint(key: str, data_ref: str) -> None:
    with pytest.raises(ValidationError, match="data_ref must be"):
        parse_block({**envelope(key), "data_ref": data_ref})


@pytest.mark.parametrize("key", DATA_KEYS)
def test_m3_data_ref_own_endpoint_accepted(key: str) -> None:
    env = envelope(key)
    block = parse_block(env)
    assert block.data_ref == block_data_ref(env["id"]) == f"/api/blocks/{env['id']}/data"


def test_scenario_compare_data_ref_optional_but_confined() -> None:
    env = envelope("scenario_compare@1")
    assert parse_block({**env, "data_ref": block_data_ref(env["id"])}).data_ref is not None
    with pytest.raises(ValidationError, match="data_ref must be"):
        parse_block({**env, "data_ref": "https://evil.example/x"})


def test_make_block_defaults_data_ref_to_given_id() -> None:
    block = make_block("network_map", good_props("network_map@1"), title="Map", provenance=provenance(),
                       block_id="blk_case1_map")
    assert block.data_ref == "/api/blocks/blk_case1_map/data"
    with pytest.raises(ValidationError, match="data_ref must be"):
        make_block("network_map", good_props("network_map@1"), title="Map", provenance=provenance(),
                   block_id="blk_case1_map", data_ref="/api/blocks/blk_case2_map/data")


def test_m1_blocks_data_ref_unchanged() -> None:
    """M1 kinds keep their free-form (nullable) data_ref."""
    block = make_block("plan_diff", good_props("plan_diff@1"), title="Plan", provenance=provenance())
    assert block.data_ref is None
    assert isinstance(parse_block({**envelope("plan_diff@1"), "data_ref": "/api/blocks/blk_plan_diff/data"}),
                      blocks.PlanDiffBlock)


# ───────────────────────────── snapshot part URLs ─────────────────────────────

GOOD_URI = "http://merxa.localhost/v1/snapshots/abc/po_line/part-0001.parquet?exp=1&sig=ab"


def _part(uri: str) -> dict[str, Any]:
    return {"uri": uri, "sha256": "c" * 64, "rows": 1, "bytes": 10}


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "ftp://merxa.localhost/x",
        "gopher://merxa.localhost/x",
        "merxa.localhost/v1/snapshots/x",
        "/v1/snapshots/x",
        "http:///v1/snapshots/x",
        "http://merxa.localhost@evil.example/x",
        "http://user:pw@merxa.localhost/x",
        "http://evil.example\\@merxa.localhost/x",
        "http://merxa.localhost/x y",
        "http://merxa.localhost/x\n",
        "http://merxa.localhost/x#frag",
        "http://merxa.localhost:99999/x",
        "http://merxa.localhost:abc/x",
    ],
)
def test_snapshot_part_rejects_unsafe_uris(uri: str) -> None:
    with pytest.raises(ValidationError):
        scn.SnapshotPart.model_validate(_part(uri))
    assert not scn.part_uri_allowed(uri, ["http://merxa.localhost"])


def test_snapshot_part_accepts_signed_http_urls() -> None:
    assert scn.SnapshotPart.model_validate(_part(GOOD_URI)).uri == GOOD_URI
    assert scn.SnapshotPart.model_validate(_part("https://erp.example.com:8443/p?sig=1")).uri


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        (GOOD_URI, True),
        ("http://MERXA.localhost/v1/snapshots/x", True),
        ("http://merxa.localhost./v1/snapshots/x", True),
        ("http://merxa.localhost:80/v1/snapshots/x", True),
        ("http://merxa.localhost:8080/v1/snapshots/x", False),
        ("https://merxa.localhost/v1/snapshots/x", False),
        ("http://evil.example/v1/snapshots/x", False),
        ("http://merxa.localhost.evil.example/x", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("http://merxa.localhost@evil.example/x", False),
    ],
)
def test_part_uri_allowed_compares_origin(uri: str, allowed: bool) -> None:
    assert scn.part_uri_allowed(uri, ["http://merxa.localhost"]) is allowed


def test_part_uri_allowed_needs_an_allow_list() -> None:
    assert not scn.part_uri_allowed(GOOD_URI, [])
    assert not scn.part_uri_allowed(GOOD_URI, ["not a url"])
    assert scn.part_uri_allowed(GOOD_URI, ["https://other", "http://merxa.localhost/"])


def test_snapshot_export_echoes_request() -> None:
    export = scn.SnapshotExport.model_validate(
        {"snapshot_id": "s1", "entity": "po_line", "rows": 1, "canonical_model": "scn/1.0",
         "parts": [_part(GOOD_URI)]}
    )
    assert (export.snapshot_id, export.entity, export.rows) == ("s1", "po_line", 1)


# ───────────────────────────── changes_since ─────────────────────────────


def test_changes_since_keeps_full_resync_flag() -> None:
    """Merxa sets full_resync_required (scenario reset) possibly with no change rows; dropping
    it would leave the engine on a stale snapshot."""
    reset = scn.ChangesSince.model_validate(
        {"changes": [], "next_watermark": "ox:9", "has_more": False, "full_resync_required": True}
    )
    assert reset.full_resync_required and reset.requires_sync
    quiet = scn.ChangesSince.model_validate({"changes": [], "next_watermark": "ox:9"})
    assert not quiet.requires_sync
    busy = scn.ChangesSince.model_validate(
        {"changes": [{"entity": "po_line", "op": "upsert", "key": "PO-1#1"}], "next_watermark": "ox:10"}
    )
    assert busy.requires_sync and not busy.full_resync_required
    with pytest.raises(ValidationError):
        scn.ChangesSince.model_validate({"changes": [], "next_watermark": "w", "full_resync_required": "yes"})


# ───────────────────────────── ProblemData v2 integrity ─────────────────────────────


def _v2() -> dict[str, Any]:
    return {**problem(), **copy.deepcopy(V2)}


def test_v2_fixture_valid() -> None:
    assert opt.ProblemData.model_validate(_v2()).alt_sources


def test_duplicate_alt_source_rejected() -> None:
    """A duplicated (supplier, factory, lane) row would double the source's capacity/MOQ."""
    data = _v2()
    data["alt_sources"].append(copy.deepcopy(data["alt_sources"][0]))
    with pytest.raises(ValidationError, match="duplicate alt_source"):
        opt.ProblemData.model_validate(data)


def test_alt_lane_cannot_be_itself() -> None:
    data = _v2()
    lane = next(il for il in data["inbound_lanes"] if il.get("alt_lane_code"))
    lane["alt_lane_code"] = lane["code"]
    with pytest.raises(ValidationError, match="alt_lane_code is the lane itself"):
        opt.ProblemData.model_validate(data)


# ───────────────────────────── actions compile ─────────────────────────────

TRANSFER: dict[str, Any] = {
    "from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-12", "lines": [{"sku": "PUF-W-BLK-M", "qty": 5}],
}


def test_transfer_action_still_valid() -> None:
    action = opt.Action(seq=1, op="transfer_order.create", payload=TRANSFER)
    assert action.change_set_item()["payload"] == TRANSFER


@pytest.mark.parametrize(
    "over",
    [
        {"payload": {**TRANSFER, "to_dc": "DE1"}},
        {"payload": {**TRANSFER, "lines": [{"sku": "A", "qty": 1}, {"sku": "A", "qty": 2}]}},
        {"target_ref": "TO-1"},
        {"expected_version": 1},
    ],
)
def test_transfer_action_must_compile(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.Action.model_validate({"seq": 1, "op": "transfer_order.create", "payload": TRANSFER, **over})
