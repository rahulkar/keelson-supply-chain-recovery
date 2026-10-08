"""keelson_contracts.scn: scn/1.0 row models (docs/contracts/25-scn-1.0.md) and snapshot tools."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
from keelson_contracts import scn
from pydantic import ValidationError

DOC = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "25-scn-1.0.md"

ROWS: dict[str, dict[str, Any]] = {
    "location": {"source_id": "dc:UK1", "kind": "dc", "code": "UK1", "name": "Daventry", "country": "GB",
                 "city": "Daventry", "lat": 52.257, "lon": -1.163, "parent_code": None, "grade": None,
                 "climate": None, "unlocode": None, "src_version": 1},
    "item": {"source_id": "sku:PUF-W-BLK-M", "kind": "sku", "code": "PUF-W-BLK-M", "style_code": "PUF-W-BLK",
             "family": "outerwear", "colour": "BLK", "size": "M", "size_seq": 3, "retail_price_cents": 2500,
             "unit_cost_cents": 900, "currency": "EUR", "src_version": 1},
    "bom_line": {"source_id": "PUF-W-BLK|ZIP-5|*", "style_code": "PUF-W-BLK", "component_code": "ZIP-5",
                 "supplier_code": "TRIM-BD-01", "factory_code": None, "qty_per_unit": 2, "src_version": 1},
    "lane": {"source_id": "BDCGP-GBFXT-CAPE", "code": "BDCGP-GBFXT-CAPE", "origin_code": "BDCGP",
             "dest_code": "UK1", "mode": "sea", "carrier_code": "MSK", "transit_days": 38, "via": ["CAPE"],
             "alt_lane_code": None, "src_version": 1},
    "lane_rate": {"source_id": "L|2026-01-01", "lane_code": "L", "valid_from": dt.date(2026, 1, 1),
                  "valid_to": "2026-12-31", "basis": "per_unit", "rate_cents": 42, "weekly_capacity": 60000,
                  "src_version": 1},
    "sailing": {"source_id": "S1", "code": "S1", "lane_code": "L", "cutoff": "2026-10-01", "etd": "2026-10-03",
                "eta": "2026-11-02", "capacity_teu": 400, "status": "scheduled", "src_version": 2},
    "po_line": {"source_id": "PO-26-0101#1", "po_code": "PO-26-0101", "line_no": 1, "supplier_code": "SUP-BD-01",
                "factory_code": "F-BD-01", "dest_dc_code": "UK1", "sku_code": "PUF-W-BLK-M", "qty_ordered": 3000,
                "qty_received": 0, "ex_factory_date": "2026-10-01", "eta_dc": "2026-11-02", "status": "open",
                "shipment_code": None, "sailing_code": None, "src_version": 4},
    "production_order": {"source_id": "MO-1", "code": "MO-1", "po_line_ref": "PO-26-0101#1",
                         "line_code": "F-BD-01-L1", "seq": 1, "planned_start": "2026-10-06",
                         "planned_end": "2026-10-09", "status": "planned", "hold_reason": None, "src_version": 1},
    "inventory_position": {"source_id": "dc/UK1/PUF-W-BLK-M", "location_kind": "dc", "location_code": "UK1",
                           "sku_code": "PUF-W-BLK-M", "on_hand": 1200, "allocated": 0, "reserved": 0,
                           "in_transit": 0, "src_version": 7},
    "demand_forecast": {"source_id": "PUF-W-BLK|UK1-COLD-A|2026-W45", "style_code": "PUF-W-BLK",
                        "location_kind": "cluster", "location_code": "UK1-COLD-A", "iso_week": "2026-W45",
                        "units": 140, "forecast_version": 1},
    "size_curve": {"source_id": "outerwear|UK1-COLD-A|M", "family": "outerwear", "cluster_code": "UK1-COLD-A",
                   "size": "M", "share_bp": 2500},
    "allocation": {"source_id": "AL-0042", "code": "AL-0042", "sku_code": "PUF-W-BLK-M", "dc_code": "UK1",
                   "store_code": "GB-0001", "ship_week": "2026-W44", "qty": 12, "status": "planned",
                   "src_version": 3},
    "transfer_order_line": {"source_id": "TO-1#1", "to_code": "TO-1", "from_dc": "DE1", "to_dc": "NL1",
                            "ship_date": "2026-10-19", "eta_date": None, "sku_code": "PUF-W-BLK-M", "qty": 400,
                            "status": "open", "src_version": 1},
    "online_order_line": {"source_id": "OO-1#1", "order_code": "OO-1", "market": "GB", "channel": "online",
                          "store_code": None, "sku_code": "PUF-W-BLK-M", "qty": 1, "reserved_qty": 0,
                          "promise_date": "2026-11-20", "status": "open", "src_version": 1},
    "promotion": {"source_id": "BF-26", "code": "BF-26", "markets": ["GB", "DE"], "grades": None,
                  "style_codes": ["PUF-W-BLK"], "period_start": "2026-11-23", "period_end": "2026-11-29",
                  "discount_bp": 3000, "uplift_bp": 18000, "src_version": 1},
}


def _top_level_split(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += ch in "({<" and 1 or (ch in ")}>" and -1 or 0)
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [*parts, cur]


def doc_columns() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| `([a-z_]+)` \| (.+) \|$", line)
        if m:
            cols = [re.match(r"[a-z_0-9]+", c.strip()) for c in _top_level_split(m.group(2))]
            out[m.group(1)] = [c.group(0) for c in cols if c]
    return out


@pytest.mark.skipif(not DOC.exists(), reason="docs/contracts not available")
def test_columns_match_the_contract_table() -> None:
    columns = doc_columns()
    assert list(columns) == list(scn.SCN_ENTITIES)
    for entity, cols in columns.items():
        assert list(scn.SCN_COLUMNS[entity]) == cols, entity


def test_registry() -> None:
    assert scn.CANONICAL_MODEL == "scn/1.0"
    assert set(ROWS) == set(scn.SCN_ROW_MODELS)


@pytest.mark.parametrize("entity", sorted(ROWS))
def test_rows_parse(entity: str) -> None:
    row = scn.parse_row(entity, ROWS[entity])
    assert type(row) is scn.SCN_ROW_MODELS[entity]
    assert set(row.model_dump()) == set(scn.SCN_COLUMNS[entity])


def test_row_helpers() -> None:
    loc = scn.parse_row("location", ROWS["location"])
    assert isinstance(loc, scn.LocationRow)
    assert (loc.lat_e6, loc.lon_e6) == (52_257_000, -1_163_000)
    promo = scn.parse_row("promotion", ROWS["promotion"])
    assert isinstance(promo, scn.PromotionRow) and promo.grades == []  # null list -> []
    line = scn.parse_row("po_line", {**ROWS["po_line"], "qty_received": 1000})
    assert isinstance(line, scn.PoLineRow) and line.open_qty == 2000
    cluster = scn.parse_row("location", {**ROWS["location"], "kind": "cluster", "lat": None, "lon": None})
    assert isinstance(cluster, scn.LocationRow) and cluster.lat_e6 is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(51.5072, 51_507_200), (-0.1276, -127_600), (0, 0), (90, 90_000_000), (-180.0, -180_000_000),
     (22.3300005, 22_330_001), (-22.3300005, -22_330_001), (None, None)],
)
def test_to_e6(value: float | None, expected: int | None) -> None:
    assert scn.to_e6(value) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "51.5"])
def test_to_e6_rejects(bad: Any) -> None:
    with pytest.raises(ValueError):
        scn.to_e6(bad)


@pytest.mark.parametrize(
    ("entity", "over"),
    [
        ("location", {"kind": "warehouse"}),
        ("location", {"lat": 91.0}),
        ("location", {"lon": float("nan")}),
        ("location", {"surprise": 1}),
        ("item", {"retail_price_cents": 25.0}),
        ("item", {"src_version": -1}),
        ("lane", {"mode": "rail"}),
        ("lane_rate", {"valid_to": "2025-12-31"}),
        ("po_line", {"qty_ordered": "3000"}),
        ("po_line", {"line_no": 0}),
        ("po_line", {"eta_dc": 1793577600}),
        ("inventory_position", {"location_kind": "cluster"}),
        ("demand_forecast", {"iso_week": "2026-W54"}),
        ("demand_forecast", {"units": 1.5}),
        ("size_curve", {"share_bp": 10001}),
        ("allocation", {"ship_week": "W44"}),
        ("promotion", {"period_end": "2026-11-01"}),
        ("online_order_line", {"qty": True}),
    ],
)
def test_rows_reject(entity: str, over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scn.parse_row(entity, {**ROWS[entity], **over})


def test_unknown_entity() -> None:
    with pytest.raises(KeyError, match="shipment"):
        scn.parse_row("shipment", {})


MANIFEST = {
    "snapshot_id": "0b2f2f0e-8f7a-4f7e-9d42-1a2b3c4d5e6f",
    "as_of": "2026-10-05T09:00:00+00:00",
    "watermark": "ox:1234",
    "wal_lsn": "0/16B3748",
    "canonical_model": "scn/1.0",
    "expires_at": "2026-10-05T11:00:00+00:00",
    "entities": {"po_line": {"rows": 36, "parts": 1, "sha256": ["a" * 64]},
                 "location": {"rows": 0, "parts": 1, "sha256": ["b" * 64]}},
}


def test_snapshot_manifest_matches_merxa_shape() -> None:
    m = scn.SnapshotManifest.model_validate(MANIFEST)  # extra keys (wal_lsn) ignored
    assert m.entities["po_line"].rows == 36
    with pytest.raises(ValidationError):
        scn.SnapshotManifest.model_validate({**MANIFEST, "canonical_model": "scn/2.0"})
    with pytest.raises(ValidationError, match="unknown scn/1.0 entities"):
        scn.SnapshotManifest.model_validate({**MANIFEST, "entities": {"shipment": {"rows": 1, "parts": 1}}})
    with pytest.raises(ValidationError, match="one hash per part"):
        scn.SnapshotManifest.model_validate(
            {**MANIFEST, "entities": {"po_line": {"rows": 1, "parts": 2, "sha256": ["a" * 64]}}})


def test_snapshot_export_and_changes() -> None:
    export = scn.SnapshotExport.model_validate({
        "snapshot_id": MANIFEST["snapshot_id"], "entity": "po_line", "canonical_model": "scn/1.0", "rows": 36,
        "parts": [{"uri": "http://merxa.localhost/v1/snapshots/x/po_line/part-0001.parquet?exp=1&sig=ab",
                   "sha256": "c" * 64, "rows": 36, "bytes": 4096}],
    })
    assert export.rows == 36
    with pytest.raises(ValidationError):
        scn.SnapshotPart.model_validate({"uri": "u", "sha256": "nothex", "rows": 1, "bytes": 1})
    changes = scn.ChangesSince.model_validate({
        "changes": [{"entity": "po_line", "op": "upsert", "key": "PO-26-0101#1", "version": 5,
                     "at": "2026-10-05T09:01:00+00:00", "event_seq": 9}],
        "next_watermark": "ox:1235", "has_more": False,
    })
    assert changes.changes[0].op == "upsert"
