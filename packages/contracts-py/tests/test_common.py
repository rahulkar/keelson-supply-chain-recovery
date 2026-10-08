"""Canonical hashing, money/units, ISO weeks, provenance and id helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from keelson_contracts import common
from keelson_contracts.common import (
    CanonicalJsonError,
    Provenance,
    ProvenanceStamp,
    add_weeks,
    apply_bp,
    canonical_json,
    eur_to_cents,
    factor_bp_from_pct_change,
    format_eur_cents,
    is_grounded,
    iso_week_of,
    monday_of,
    needs_confirmation,
    parse_iso_week,
    ratio_bp,
    sha256_hex,
    week_range,
)

# Literal canonical text written by hand, and its sha256 computed with hashlib alone.
VECTOR_TEXT = '{"city":"Poznań","n":-7,"ok":true,"z":null}'
VECTOR_HASH = "a885e25fa3ffdb6ba2da2606e82f0cc9d556f9b766c7463cc3a1bbc25510acd0"


class TestCanonicalJson:
    def test_hand_computed_vector(self) -> None:
        obj = {"z": None, "ok": True, "n": -7, "city": "Poznań"}
        assert canonical_json(obj) == VECTOR_TEXT
        assert hashlib.sha256(VECTOR_TEXT.encode("utf-8")).hexdigest() == VECTOR_HASH
        assert sha256_hex(obj) == VECTOR_HASH

    def test_matches_contract_definition(self) -> None:
        obj = {"b": [3, {"y": "ü", "x": 1}], "a": "€"}
        expected = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert canonical_json(obj) == expected
        assert sha256_hex(obj) == hashlib.sha256(expected.encode("utf-8")).hexdigest()

    def test_dict_key_order_insensitive(self) -> None:
        a = {"seq": 1, "payload": {"qty": 5, "sku": "X"}, "op": "o"}
        b = {"op": "o", "payload": {"sku": "X", "qty": 5}, "seq": 1}
        assert sha256_hex(a) == sha256_hex(b)

    def test_list_order_sensitive(self) -> None:
        assert sha256_hex([1, 2]) != sha256_hex([2, 1])

    @pytest.mark.parametrize(
        "bad",
        [1.5, {"a": 0.0}, [1, {"b": [2.25]}], {"nan": float("nan")}, {"inf": float("inf")}],
    )
    def test_floats_rejected(self, bad: object) -> None:
        with pytest.raises(CanonicalJsonError, match="float"):
            canonical_json(bad)

    def test_float_error_has_pointer(self) -> None:
        with pytest.raises(CanonicalJsonError, match=r"/items/0/price"):
            sha256_hex({"items": [{"price": 9.99}]})

    def test_non_string_keys_rejected(self) -> None:
        with pytest.raises(CanonicalJsonError, match="non-string key"):
            canonical_json({1: "a"})

    def test_unsafe_integers_rejected(self) -> None:
        canonical_json(2**53 - 1)
        with pytest.raises(CanonicalJsonError, match="2\\^53"):
            canonical_json(2**53)

    def test_non_json_types_rejected(self) -> None:
        with pytest.raises(CanonicalJsonError, match="date"):
            canonical_json({"d": dt.date(2026, 10, 5)})
        with pytest.raises(CanonicalJsonError):
            canonical_json(b"bytes")

    def test_models_and_tuples_are_normalised(self) -> None:
        class M(BaseModel):
            day: dt.date
            n: int

        assert canonical_json((M(day=dt.date(2026, 10, 5), n=1),)) == '[{"day":"2026-10-05","n":1}]'

    def test_bool_is_not_treated_as_int(self) -> None:
        assert canonical_json([True, 1]) == "[true,1]"


class TestMoney:
    def test_apply_bp_half_up(self) -> None:
        assert apply_bp(3000, 11800) == 3540
        assert apply_bp(1, 5000) == 1  # 0.5 rounds up
        assert apply_bp(-1, 5000) == -1  # symmetric
        assert apply_bp(3, 3333) == 1

    def test_ratio_bp(self) -> None:
        assert ratio_bp(1, 3) == 3333
        assert ratio_bp(2, 3) == 6667
        with pytest.raises(ValueError):
            ratio_bp(1, 0)

    def test_factor_from_pct(self) -> None:
        assert factor_bp_from_pct_change(18) == 11800
        assert factor_bp_from_pct_change(-100) == 0

    def test_eur_to_cents(self) -> None:
        assert eur_to_cents("12.40") == 1240
        assert eur_to_cents(Decimal("0.05")) == 5
        assert eur_to_cents(25) == 2500
        with pytest.raises(TypeError):
            eur_to_cents(12.4)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            eur_to_cents("1.234")

    def test_format(self) -> None:
        assert format_eur_cents(1_240_000) == "€12,400"
        assert format_eur_cents(-150) == "-€2"
        assert format_eur_cents(123456, show_cents=True) == "€1,234.56"

    def test_rejects_float_inputs(self) -> None:
        with pytest.raises(TypeError):
            apply_bp(1.0, 10000)  # type: ignore[arg-type]


class TestIsoWeeks:
    def test_demo_calendar(self) -> None:
        assert iso_week_of(dt.date(2026, 10, 5)) == "2026-W41"
        assert monday_of("2026-W45") == dt.date(2026, 11, 2)
        assert iso_week_of(dt.date(2026, 11, 27)) == "2026-W48"  # Black Friday

    def test_add_weeks_crosses_years(self) -> None:
        assert add_weeks("2026-W52", 1) == "2026-W53"  # 2026 has 53 ISO weeks
        assert add_weeks("2026-W53", 1) == "2027-W01"
        assert add_weeks("2027-W01", -1) == "2026-W53"

    def test_week_range(self) -> None:
        assert week_range("2026-W41", 3) == ["2026-W41", "2026-W42", "2026-W43"]
        assert week_range("2026-W41", 0) == []

    @pytest.mark.parametrize("bad", ["2026-W54", "2025-W53", "2026-41", "2026-W1", "W41"])
    def test_invalid_weeks(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_iso_week(bad)

    def test_isoweek_type(self) -> None:
        class M(BaseModel):
            w: common.IsoWeek

        assert M(w="2026-W53").w == "2026-W53"
        with pytest.raises(ValidationError):
            M(w="2025-W53")


class TestProvenance:
    def test_groups(self) -> None:
        assert is_grounded("fact") and is_grounded(Provenance.DECISION)
        assert not is_grounded("simulated")
        assert needs_confirmation("inference") and needs_confirmation("assumption")
        assert not needs_confirmation("committed")

    def test_verified_only_for_committed(self) -> None:
        assert common.committed(verified=True).is_verified_commit
        assert not common.committed().is_verified_commit
        with pytest.raises(ValidationError):
            ProvenanceStamp(kind=Provenance.FACT, verified=True)


class TestIds:
    def test_formats(self) -> None:
        ev, blk, run = common.new_evidence_id(), common.new_block_id(), common.new_run_id()
        assert re.fullmatch(r"ev_[0-9a-f]{32}", ev) and common.is_evidence_id(ev)
        assert re.fullmatch(r"blk_[0-9a-f]{32}", blk) and common.is_block_id(blk)
        assert common.uuid7().version == 7
        assert str(common.uuid7()) != run

    def test_time_ordered(self) -> None:
        ids = [common.new_evidence_id() for _ in range(50)]
        assert len(set(ids)) == 50
        # The first 12 hex chars are the millisecond timestamp: non-decreasing.
        prefixes = [i[3:15] for i in ids]
        assert prefixes == sorted(prefixes)

    def test_stable_block_id(self) -> None:
        a = common.stable_block_id("case-1", "kpi_strip", 2)
        assert a == common.stable_block_id("case-1", "kpi_strip", "2")
        assert a != common.stable_block_id("case-1", "kpi_strip", 3)
        assert common.is_block_id(a)

    def test_fallback_uuid7(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(common.uuid, "uuid7", raising=False)
        u = common.uuid7()
        assert u.version == 7 and u.variant == common.uuid.RFC_4122
