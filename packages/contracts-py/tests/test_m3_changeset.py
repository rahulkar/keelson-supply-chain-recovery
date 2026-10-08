"""Typed M3 change-set ops (v1 lever ops) and hash invariance (docs/contracts/80 §3,
22-merxa-m2 §3). Typing an op must never change the change-set hash (= Merxa preview_hash)."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
from keelson_contracts import changeset as cs
from keelson_contracts.changeset import CHANGE_SET_ITEM_ADAPTER, ChangeSet, change_set_hash
from pydantic import ValidationError
from test_changeset import CANONICAL_HASH, ITEMS

# Hand-written canonical text (keys sorted, no whitespace) and its sha256 computed with
# hashlib only: what Merxa stores as preview_hash for M3_ITEMS.
M3_CANONICAL_TEXT = (
    '[{"expected_version":null,"op":"po.create","payload":{"dest_dc":"UK1","eta_dc":"2026-11-02",'
    '"factory_code":"F-MA-01","lines":[{"qty":600,"sku":"PUF-W-BLK-M"}],"supplier_code":"SUP-MA-01"},'
    '"seq":1,"target_ref":null},'
    '{"expected_version":4,"op":"po_line.change_qty","payload":{"qty":2400},"seq":2,"target_ref":"PO-26-0101#1"},'
    '{"expected_version":2,"op":"po_line.split","payload":{"lane_code":"BDCGP-AEJEA-UK1","mode":"sea_air",'
    '"qty":200},"seq":3,"target_ref":"PO-26-0101#2"},'
    '{"expected_version":null,"op":"capacity_booking.create","payload":{"iso_week":"2026-W42",'
    '"lane_code":"BDCGP-AEJEA-UK1","qty_units":200},"seq":4,"target_ref":null},'
    '{"expected_version":1,"op":"po_line.cancel","payload":{},"seq":5,"target_ref":"PO-26-0102#1"},'
    '{"expected_version":null,"op":"production_order.resequence","payload":{"line_code":"F-BD-01-L1",'
    '"order_codes":["MO-2","MO-1"]},"seq":6,"target_ref":null}]'
)
M3_CANONICAL_HASH = "d634650ed770e3223fb93b16389566b36996fadc70b08f592bd94e9c066d17cc"

M3_ITEMS: list[dict[str, Any]] = [
    {"seq": 1, "op": "po.create", "target_ref": None, "expected_version": None,
     "payload": {"supplier_code": "SUP-MA-01", "factory_code": "F-MA-01", "dest_dc": "UK1", "eta_dc": "2026-11-02",
                 "lines": [{"sku": "PUF-W-BLK-M", "qty": 600}]}},
    {"seq": 2, "op": "po_line.change_qty", "target_ref": "PO-26-0101#1", "expected_version": 4,
     "payload": {"qty": 2400}},
    {"seq": 3, "op": "po_line.split", "target_ref": "PO-26-0101#2", "expected_version": 2,
     "payload": {"mode": "sea_air", "qty": 200, "lane_code": "BDCGP-AEJEA-UK1"}},
    {"seq": 4, "op": "capacity_booking.create", "target_ref": None, "expected_version": None,
     "payload": {"lane_code": "BDCGP-AEJEA-UK1", "iso_week": "2026-W42", "qty_units": 200}},
    {"seq": 5, "op": "po_line.cancel", "target_ref": "PO-26-0102#1", "expected_version": 1, "payload": {}},
    {"seq": 6, "op": "production_order.resequence", "target_ref": None, "expected_version": None,
     "payload": {"line_code": "F-BD-01-L1", "order_codes": ["MO-2", "MO-1"]}},
]

TYPED = {
    "po.create": cs.PoCreateItem,
    "po_line.change_qty": cs.PoLineChangeQtyItem,
    "po_line.split": cs.PoLineSplitItem,
    "capacity_booking.create": cs.CapacityBookingCreateItem,
    "po_line.cancel": cs.PoLineCancelItem,
    "production_order.resequence": cs.ProductionOrderResequenceItem,
}


def test_op_registries() -> None:
    assert cs.M1_OPS == ("transfer_order.create", "allocation.set_qty", "po_line.reschedule", "po_line.change_qty")
    assert set(cs.M3_OPS) == {"po_line.split", "capacity_booking.create", "po.create", "po_line.cancel",
                              "production_order.resequence"}
    assert set(cs.OP_PAYLOAD_MODELS) == set(cs.TYPED_OPS) == set(cs.M1_OPS) | set(cs.M3_OPS)


class TestHashInvariance:
    def test_old_ops_hash_unchanged(self) -> None:
        """The M1 pinned vector still holds (typed M1 items and wire dicts)."""
        assert change_set_hash(ITEMS) == CANONICAL_HASH
        typed = [CHANGE_SET_ITEM_ADAPTER.validate_python(i) for i in ITEMS]
        assert change_set_hash(typed) == CANONICAL_HASH

    def test_m3_vector(self) -> None:
        assert hashlib.sha256(M3_CANONICAL_TEXT.encode()).hexdigest() == M3_CANONICAL_HASH
        assert change_set_hash(M3_ITEMS) == M3_CANONICAL_HASH

    def test_typed_items_hash_like_wire_dicts(self) -> None:
        typed = [CHANGE_SET_ITEM_ADAPTER.validate_python(i) for i in M3_ITEMS]
        assert [type(t) for t in typed] == [TYPED[i["op"]] for i in M3_ITEMS]
        assert [t.hash_view() for t in typed] == M3_ITEMS
        assert change_set_hash(typed) == M3_CANONICAL_HASH
        change_set = ChangeSet(id="cs_m3", tenant="demo-retail", connector="merxa", revision=1, items=typed)
        assert change_set.hash == M3_CANONICAL_HASH
        assert [t["op"] for t in change_set.tool_items()] == [i["op"] for i in M3_ITEMS]

    def test_hash_same_as_when_these_ops_were_generic(self) -> None:
        """Before M3 these ops were GenericOpItem; their hash view is byte-identical."""
        for raw in M3_ITEMS:
            typed = CHANGE_SET_ITEM_ADAPTER.validate_python(raw)
            generic = cs.GenericOpItem.model_construct(**copy.deepcopy(raw))
            assert typed.hash_view() == generic.hash_view() == raw

    @pytest.mark.parametrize("payload", [None, "missing"])
    def test_cancel_missing_or_null_payload_hashes_as_empty(self, payload: Any) -> None:
        raw = {k: v for k, v in M3_ITEMS[4].items() if k != "payload"}
        if payload is None:
            raw["payload"] = None
        typed = CHANGE_SET_ITEM_ADAPTER.validate_python(raw)
        assert isinstance(typed, cs.PoLineCancelItem)
        assert change_set_hash([typed]) == change_set_hash([raw]) == change_set_hash([M3_ITEMS[4]])

    def test_untyped_ops_stay_generic(self) -> None:
        item = CHANGE_SET_ITEM_ADAPTER.validate_python(
            {"seq": 1, "op": "promotion.reschedule", "target_ref": "BF-26", "expected_version": 2,
             "payload": {"period_start": "2026-11-23", "period_end": "2026-11-30"}})
        assert isinstance(item, cs.GenericOpItem)
        with pytest.raises(ValidationError, match="typed model"):
            cs.GenericOpItem(seq=1, op="po.create", payload={})


def _with(index: int, **over: Any) -> dict[str, Any]:
    item = copy.deepcopy(M3_ITEMS[index])
    for key, value in over.items():
        if key.startswith("p_"):
            item["payload"][key[2:]] = value
        else:
            item[key] = value
    return item


@pytest.mark.parametrize(
    ("why", "item"),
    [
        ("po.create with target_ref", _with(0, target_ref="PO-1#1")),
        ("po.create with version", _with(0, expected_version=1)),
        ("po.create no lines", _with(0, p_lines=[])),
        ("po.create duplicate skus", _with(0, p_lines=[{"sku": "A", "qty": 1}, {"sku": "A", "qty": 2}])),
        ("po.create qty 0", _with(0, p_lines=[{"sku": "A", "qty": 0}])),
        ("po.create float qty", _with(0, p_lines=[{"sku": "A", "qty": 1.0}])),
        ("po.create too many lines", _with(0, p_lines=[{"sku": f"S{i}", "qty": 1} for i in range(501)])),
        ("po.create timestamp eta", _with(0, p_eta_dc=1793577600)),
        ("po.create unknown key", _with(0, p_rush=True)),
        ("split without line ref", _with(2, target_ref="PO-26-0101")),
        ("split without version", _with(2, expected_version=None)),
        ("split mode sea", _with(2, p_mode="sea")),
        ("split qty 0", _with(2, p_qty=0)),
        ("split empty lane", _with(2, p_lane_code="")),
        ("booking with target", _with(3, target_ref="X")),
        ("booking bad week", _with(3, p_iso_week="2025-W53")),
        ("booking qty string", _with(3, p_qty_units="200")),
        ("cancel with payload key", _with(4, p_reason="late")),
        ("cancel without version", _with(4, expected_version=None)),
        ("resequence target mismatch", _with(5, target_ref="OTHER")),
        ("resequence empty", _with(5, p_order_codes=[])),
        ("resequence duplicates", _with(5, p_order_codes=["MO-1", "MO-1"])),
    ],
)
def test_invalid_m3_items(why: str, item: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CHANGE_SET_ITEM_ADAPTER.validate_python(item)


def test_valid_variants() -> None:
    air = CHANGE_SET_ITEM_ADAPTER.validate_python(_with(2, p_mode="air"))
    assert air.payload.mode == "air"
    same_line = CHANGE_SET_ITEM_ADAPTER.validate_python(_with(5, target_ref="F-BD-01-L1", expected_version=3))
    assert same_line.target_ref == same_line.payload.line_code
    po = CHANGE_SET_ITEM_ADAPTER.validate_python(M3_ITEMS[0])
    assert po.payload.total_qty == 600
