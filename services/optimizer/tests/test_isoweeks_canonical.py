from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pytest
from keelson_contracts.optimizer import ImpactRequest, OptimizeRequest

from keelson_opt.canonical import canonical_json, model_hash
from keelson_opt.isoweeks import horizon_weeks, iso_week_label, parse_iso_week, short_week, week_index


def test_horizon_weeks_crosses_iso_year_boundary() -> None:
    labels, mondays = horizon_weeks("2026-W52", 3)
    assert labels == ["2026-W52", "2026-W53", "2027-W01"]
    assert mondays[0] == dt.date(2026, 12, 21)
    assert all(m.weekday() == 0 for m in mondays)


def test_demo_calendar() -> None:
    assert parse_iso_week("2026-W41") == dt.date(2026, 10, 5)  # demo "today"
    assert parse_iso_week("2026-W45") == dt.date(2026, 11, 2)  # peak start
    assert iso_week_label(dt.date(2026, 11, 27)) == "2026-W48"  # Black Friday
    assert short_week("2026-W43") == "W43"


@pytest.mark.parametrize(
    ("day", "expected"),
    [(dt.date(2026, 10, 4), -1), (dt.date(2026, 10, 5), 0), (dt.date(2026, 10, 11), 0), (dt.date(2026, 11, 2), 4)],
)
def test_week_index(day: dt.date, expected: int) -> None:
    assert week_index(day, dt.date(2026, 10, 5)) == expected


def test_canonical_json_rule() -> None:
    obj = {"b": 1, "a": [{"z": "é", "y": None}], "c": True}
    assert canonical_json(obj) == '{"a":[{"y":null,"z":"é"}],"b":1,"c":true}'


def test_canonical_matches_shared_contracts_implementation(small_raw: dict[str, Any]) -> None:
    common = pytest.importorskip("keelson_contracts.common")
    req = OptimizeRequest.model_validate(small_raw)
    dumped = req.model_dump(mode="json")
    assert canonical_json(dumped) == common.canonical_json(dumped)
    assert model_hash(req) == common.sha256_hex(dumped)


def test_inputs_hash_is_sha256_of_canonical_request(small_raw: dict[str, Any]) -> None:
    req = ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"])
    expected = hashlib.sha256(
        json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert model_hash(req) == expected
    # Insensitive to key order of the incoming JSON, sensitive to content.
    shuffled = json.loads(json.dumps(small_raw["data"], sort_keys=True))
    assert model_hash(ImpactRequest(data=shuffled, perturbations=small_raw["perturbations"])) == expected
    assert model_hash(ImpactRequest(data=small_raw["data"], perturbations=[])) != expected
