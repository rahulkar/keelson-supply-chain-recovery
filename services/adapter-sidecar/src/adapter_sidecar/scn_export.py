"""``scn/1.0`` rows → Parquet parts (contract 25; the shape Keelson's sync reads, ``core/scn_sync.py``).

* :data:`SCN_ARROW_TYPES` — the explicit Arrow type of every ``SCN_COLUMNS`` column (strings,
  ``int64``, ``float64`` lat/lon, ``date32``, ``list<string>``); a test checks it against the
  ``keelson_contracts.scn`` row models.
* :func:`convert_row` — one entity-SQL row (SQLite or Postgres values) → contract values: ISO
  date text → ``date``, JSON-array text / Postgres arrays → ``list[str]``, integral numbers →
  ``int``; then :func:`keelson_contracts.scn.parse_row` validates it (closed row models: a schema
  drift fails the export loudly, before Keelson ever sees it).
* :func:`write_part` — zstd Parquet via a temp file, ``chmod 0444``, sha256 of the bytes.
* :func:`fk_problems` — the two foreign-key checks Keelson's sync runs after loading
  (``scn_sync.FK_CHECKS``), so ``python -m adapter_sidecar check`` catches them locally.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from keelson_contracts.scn import SCN_COLUMNS, SCN_ENTITIES, parse_row

S, I64, F64, D32 = pa.string(), pa.int64(), pa.float64(), pa.date32()
LS = pa.list_(pa.string())

FLOAT_COLUMNS = frozenset({"lat", "lon"})
DATE_COLUMNS = frozenset({
    "valid_from", "valid_to", "cutoff", "etd", "eta", "ex_factory_date", "eta_dc", "planned_start", "planned_end",
    "ship_date", "eta_date", "promise_date", "period_start", "period_end",
})
LIST_COLUMNS = frozenset({"via", "markets", "grades", "style_codes"})
INT_COLUMNS = frozenset({
    "src_version", "size_seq", "retail_price_cents", "unit_cost_cents", "qty_per_unit", "transit_days", "rate_cents",
    "weekly_capacity", "capacity_teu", "line_no", "qty_ordered", "qty_received", "seq", "on_hand", "allocated",
    "reserved", "in_transit", "units", "forecast_version", "share_bp", "qty", "reserved_qty", "discount_bp",
    "uplift_bp",
})


def _arrow_type(column: str) -> pa.DataType:
    if column in FLOAT_COLUMNS:
        return F64
    if column in DATE_COLUMNS:
        return D32
    if column in LIST_COLUMNS:
        return LS
    if column in INT_COLUMNS:
        return I64
    return S


SCN_ARROW_TYPES: dict[str, dict[str, pa.DataType]] = {
    entity: {col: _arrow_type(col) for col in SCN_COLUMNS[entity]} for entity in SCN_ENTITIES
}
"""Entity → column → Arrow type, in ``SCN_COLUMNS`` order."""


def arrow_schema(entity: str) -> pa.Schema:
    return pa.schema(list(SCN_ARROW_TYPES[entity].items()))


class RowError(ValueError):
    """An entity row the contract refuses (``entity``, ``source_id`` and the reason)."""

    def __init__(self, entity: str, source_id: Any, detail: str) -> None:
        super().__init__(f"{entity} {str(source_id)[:80]!r}: {detail}")
        self.entity = entity
        self.source_id = source_id
        self.detail = detail


def _convert(column: str, kind: pa.DataType, value: Any) -> Any:
    if value is None:
        return None
    if kind == S:
        if isinstance(value, str):
            return value
        raise ValueError(f"{column} must be text, got {type(value).__name__}")
    if kind == I64:
        if isinstance(value, bool):
            raise ValueError(f"{column} must be an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if hasattr(value, "as_integer_ratio") and not isinstance(value, float):  # Decimal from Postgres numeric
            num, den = value.as_integer_ratio()
            if den == 1:
                return int(num)
        raise ValueError(f"{column} must be an integer, got {value!r:.40}")
    if kind == F64:
        if isinstance(value, bool) or not isinstance(value, int | float):
            if hasattr(value, "as_integer_ratio"):
                return float(value)
            raise ValueError(f"{column} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{column} must be finite")
        return float(value)
    if kind == D32:
        if isinstance(value, dt.datetime):
            raise ValueError(f"{column} must be a date, got a timestamp")
        if isinstance(value, dt.date):
            return value
        if isinstance(value, str):
            try:
                return dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{column} must be YYYY-MM-DD, got {value[:40]!r}") from None
        raise ValueError(f"{column} must be a date")
    # list<string>: Postgres arrays come as lists; SQLite stores JSON array text
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"{column} must be a JSON array of strings") from None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{column} must be a list of strings")
    return value


def convert_row(entity: str, columns: Sequence[str], values: Sequence[Any]) -> dict[str, Any]:
    """One SQL result row → validated contract values (``RowError`` on any problem)."""
    kinds = SCN_ARROW_TYPES[entity]
    row = dict(zip(columns, values, strict=True))
    out: dict[str, Any] = {}
    for col in SCN_COLUMNS[entity]:
        try:
            out[col] = _convert(col, kinds[col], row.get(col))
        except ValueError as exc:
            raise RowError(entity, row.get("source_id"), str(exc)) from None
    try:
        parse_row(entity, out)
    except ValidationError as exc:
        errs = exc.errors()[:3]
        detail = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg')}" for e in errs)
        raise RowError(entity, row.get("source_id"), detail) from None
    return out


def write_part(path: Path, entity: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Write one Parquet part (zstd); returns ``{rows, bytes, sha256}`` of the file's bytes."""
    schema = arrow_schema(entity)
    arrays = [pa.array([r[f.name] for r in rows], type=f.type) for f in schema]
    table = pa.Table.from_arrays(arrays, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    data = tmp.read_bytes()
    tmp.replace(path)
    path.chmod(0o444)
    return {"rows": len(rows), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def fk_problems(rows: Mapping[str, Iterable[Mapping[str, Any]]]) -> list[str]:
    """Keelson's post-load FK checks: every ``po_line.sku_code`` is an ``item`` of kind ``sku``;
    every ``inventory_position`` location exists in ``location`` with its kind."""
    items = list(rows.get("item", ()))
    locations = list(rows.get("location", ()))
    skus = {r["code"] for r in items if r.get("kind") == "sku"}
    locs = {(r["kind"], r["code"]) for r in locations}
    problems: list[str] = []
    bad_skus = sorted({r["sku_code"] for r in rows.get("po_line", ()) if r["sku_code"] not in skus})
    if bad_skus:
        problems.append(f"po_line.sku_code → item(sku): {', '.join(bad_skus[:5])}")
    bad_locs = sorted({f"{r['location_kind']}:{r['location_code']}" for r in rows.get("inventory_position", ())
                       if (r["location_kind"], r["location_code"]) not in locs})
    if bad_locs:
        problems.append(f"inventory_position.location → location: {', '.join(bad_locs[:5])}")
    return problems
