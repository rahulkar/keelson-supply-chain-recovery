"""Change-set ops, hashing (must equal Merxa's preview_hash) and idempotency keys."""

from __future__ import annotations

import hashlib
import random
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.changeset import (
    CHANGE_SET_ITEM_ADAPTER,
    AllocationSetQtyItem,
    ChangeSet,
    GenericOpItem,
    PoLineRescheduleItem,
    TransferOrderCreateItem,
    change_set_hash,
    hash_items,
    idempotency_key,
    parse_po_line_ref,
    po_line_ref,
)

# Hand-written canonical text of ITEMS (keys sorted, no whitespace) and its sha256
# computed with hashlib alone — what Merxa stores as preview_hash for these items.
CANONICAL_TEXT = (
    '[{"expected_version":null,"op":"transfer_order.create","payload":{"from_dc":"DE1",'
    '"lines":[{"qty":400,"sku":"PUF-W-BLK-M"},{"qty":120,"sku":"PUF-W-BLK-S"}],'
    '"ship_date":"2026-10-19","to_dc":"NL1"},"seq":1,"target_ref":null},'
    '{"expected_version":3,"op":"allocation.set_qty","payload":{"qty":0},"seq":2,'
    '"target_ref":"AL-0042"}]'
)
CANONICAL_HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc"

ITEMS: list[dict[str, Any]] = [
    {
        "seq": 1,
        "op": "transfer_order.create",
        "target_ref": None,
        "expected_version": None,
        "payload": {
            "from_dc": "DE1",
            "to_dc": "NL1",
            "ship_date": "2026-10-19",
            "lines": [{"sku": "PUF-W-BLK-M", "qty": 400}, {"sku": "PUF-W-BLK-S", "qty": 120}],
        },
    },
    {
        "seq": 2,
        "op": "allocation.set_qty",
        "target_ref": "AL-0042",
        "expected_version": 3,
        "payload": {"qty": 0},
    },
]


def make_cs(items: list[dict[str, Any]] | None = None, **kw: Any) -> ChangeSet:
    data: dict[str, Any] = {
        "id": "cs_01",
        "tenant": "demo-retail",
        "connector": "merxa",
        "revision": 1,
        "items": items if items is not None else ITEMS,
    }
    data.update(kw)
    return ChangeSet.model_validate(data)


class TestHash:
    def test_hand_computed_vector(self) -> None:
        assert hashlib.sha256(CANONICAL_TEXT.encode()).hexdigest() == CANONICAL_HASH
        assert change_set_hash(ITEMS) == CANONICAL_HASH

    def test_typed_items_hash_identically_to_wire_dicts(self) -> None:
        cs = make_cs()
        assert isinstance(cs.items[0], TransferOrderCreateItem)
        assert isinstance(cs.items[1], AllocationSetQtyItem)
        assert cs.hash == CANONICAL_HASH

    def test_dict_key_order_insensitive(self) -> None:
        shuffled = [dict(reversed(list(i.items()))) for i in ITEMS]
        shuffled[0]["payload"] = dict(reversed(list(shuffled[0]["payload"].items())))
        assert change_set_hash(shuffled) == CANONICAL_HASH

    def test_list_order_irrelevant_but_seq_matters(self) -> None:
        assert change_set_hash(list(reversed(ITEMS))) == CANONICAL_HASH
        swapped = [{**ITEMS[0], "seq": 2}, {**ITEMS[1], "seq": 1}]
        assert change_set_hash(swapped) != CANONICAL_HASH

    def test_line_order_inside_payload_matters(self) -> None:
        item = {**ITEMS[0], "payload": {**ITEMS[0]["payload"]}}
        item["payload"]["lines"] = list(reversed(item["payload"]["lines"]))
        assert change_set_hash([item, ITEMS[1]]) != CANONICAL_HASH

    def test_missing_optional_keys_count_as_null(self) -> None:
        sparse = {k: v for k, v in ITEMS[0].items() if k not in ("target_ref", "expected_version")}
        assert change_set_hash([sparse, ITEMS[1]]) == CANONICAL_HASH

    def test_extra_keys_ignored_like_merxa(self) -> None:
        noisy = [{**i, "status": "ok", "before": {"x": 1}} for i in ITEMS]
        assert change_set_hash(noisy) == CANONICAL_HASH

    def test_duplicate_seq_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            hash_items([ITEMS[0], {**ITEMS[1], "seq": 1}])

    def test_float_in_payload_rejected(self) -> None:
        bad = {**ITEMS[1], "payload": {"qty": 1.0}}
        with pytest.raises(ValueError, match="float"):
            change_set_hash([bad])

    def test_deterministic_under_random_order(self) -> None:
        rng = random.Random(7)
        for _ in range(20):
            items = list(ITEMS)
            rng.shuffle(items)
            assert change_set_hash(items) == CANONICAL_HASH


class TestOps:
    def test_discriminates_known_ops(self) -> None:
        item = CHANGE_SET_ITEM_ADAPTER.validate_python(
            {
                "seq": 1,
                "op": "po_line.reschedule",
                "target_ref": "PO-26-0101#1",
                "expected_version": 2,
                "payload": {"eta_dc": "2026-11-09"},
            }
        )
        assert isinstance(item, PoLineRescheduleItem)
        assert item.tool_item() == {
            "op": "po_line.reschedule",
            "target_ref": "PO-26-0101#1",
            "expected_version": 2,
            "payload": {"eta_dc": "2026-11-09"},
        }

    def test_generic_fallback(self) -> None:
        item = CHANGE_SET_ITEM_ADAPTER.validate_python(
            {
                "seq": 1,
                "op": "online_order.repromise",  # still untyped in M3 (po.create is typed now)
                "target_ref": "OO-1",
                "payload": {"promise_date": "2026-11-20", "notify": True, "lines": [{"sku": "A", "qty": 5}]},
            }
        )
        assert isinstance(item, GenericOpItem)
        assert item.target_ref == "OO-1" and item.expected_version is None

    @pytest.mark.parametrize(
        "payload",
        [{"qty": 1.5}, {"lines": [{"cost": 0.1}]}, {"x": {1, 2}}],
    )
    def test_generic_payload_rejects_non_json_or_float(self, payload: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CHANGE_SET_ITEM_ADAPTER.validate_python({"seq": 1, "op": "promotion.reschedule", "payload": payload})

    def test_generic_cannot_impersonate_typed_op(self) -> None:
        with pytest.raises(ValidationError, match="typed model"):
            GenericOpItem(seq=1, op="allocation.set_qty", payload={"qty": 1})

    @pytest.mark.parametrize(
        "item",
        [
            # transfer: same DC
            {**ITEMS[0], "payload": {**ITEMS[0]["payload"], "to_dc": "DE1"}},
            # transfer: target_ref must be null
            {**ITEMS[0], "target_ref": "X"},
            # transfer: qty must be positive
            {**ITEMS[0], "payload": {**ITEMS[0]["payload"], "lines": [{"sku": "A", "qty": 0}]}},
            # transfer: duplicate skus
            {
                **ITEMS[0],
                "payload": {**ITEMS[0]["payload"], "lines": [{"sku": "A", "qty": 1}, {"sku": "A", "qty": 2}]},
            },
            # allocation: expected_version required
            {**ITEMS[1], "expected_version": None},
            # allocation: qty is an int, not a numeric string
            {**ITEMS[1], "payload": {"qty": "5"}},
            # po_line: bad reference
            {"seq": 1, "op": "po_line.change_qty", "target_ref": "PO-1", "expected_version": 1, "payload": {"qty": 1}},
            # unknown payload key
            {**ITEMS[1], "payload": {"qty": 1, "note": "x"}},
            # bad op name for generic
            {"seq": 1, "op": "Drop Table", "payload": {}},
        ],
    )
    def test_invalid_items(self, item: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CHANGE_SET_ITEM_ADAPTER.validate_python(item)

    def test_po_line_ref_helpers(self) -> None:
        assert po_line_ref("PO-26-0101", 3) == "PO-26-0101#3"
        assert parse_po_line_ref("PO-26-0101#3") == ("PO-26-0101", 3)
        with pytest.raises(ValueError):
            parse_po_line_ref("PO-26-0101#0")


class TestChangeSet:
    def test_seq_must_be_contiguous(self) -> None:
        with pytest.raises(ValidationError, match="seq 1..n"):
            make_cs([ITEMS[1]])  # seq 2 only
        with pytest.raises(ValidationError):
            make_cs([ITEMS[1], ITEMS[0]])  # out of order

    def test_tool_items_and_ops(self) -> None:
        cs = make_cs()
        tool_items = cs.tool_items()
        assert [t["op"] for t in tool_items] == ["transfer_order.create", "allocation.set_qty"]
        assert all("seq" not in t for t in tool_items)
        assert tool_items[0]["payload"]["ship_date"] == "2026-10-19"
        assert cs.ops == {"transfer_order.create", "allocation.set_qty"}

    def test_idempotency_key(self) -> None:
        expected = "9f6457bd8773353c1f01fb5d8c91d49188cb791be7e653156fcc01074c0e1a7d"
        assert idempotency_key("demo-retail", "cs_1", 2) == expected
        assert hashlib.sha256(b"demo-retail|cs_1|2").hexdigest() == expected
        cs = make_cs(id="cs_1", revision=2)
        assert cs.idempotency_key == expected
        assert make_cs(id="cs_1", revision=3).idempotency_key != expected
