"""Shared helpers for the service layer: JSON conversion and keyset cursor pagination."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, tuple_

from merxa_api.errors import Problem

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def jsonable(value: Any) -> Any:
    """Convert DB values to JSON-safe primitives (RFC 3339 UTC timestamps, ISO dates)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def row_dict(row: Any, *, exclude: Sequence[str] = ()) -> dict[str, Any]:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return {k: jsonable(v) for k, v in mapping.items() if k not in exclude}


def encode_cursor(values: Sequence[Any]) -> str:
    raw = json.dumps([jsonable(v) for v in values], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, arity: int) -> list[Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise Problem(400, "invalid_cursor", "cursor is malformed") from exc
    if not isinstance(values, list) or len(values) != arity:
        raise Problem(400, "invalid_cursor", "cursor does not match this listing")
    return values


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise Problem(400, "invalid_limit", "limit must be >= 1")
    return min(limit, MAX_LIMIT)


async def paginate(
    conn: Any,
    query: Select[Any],
    key_cols: Sequence[ColumnElement[Any]],
    *,
    limit: int,
    cursor: str | None,
    key_types: Sequence[type] | None = None,
) -> tuple[list[Any], str | None]:
    """Keyset pagination over ``key_cols`` (ascending, must be unique together).

    Fetches ``limit + 1`` rows to know whether a next page exists. ``key_types`` lets
    callers coerce cursor values (e.g. ``int`` ids, ``date``) before comparison.
    """
    if cursor:
        values = decode_cursor(cursor, len(key_cols))
        # Cursors are client-controlled: values that do not match the key columns' types
        # must be a 400, never a driver error (asyncpg rejects e.g. an int for a text key).
        types = key_types or [str] * len(key_cols)
        values = [_coerce(v, ty) for v, ty in zip(values, types, strict=True)]
        if len(key_cols) == 1:
            query = query.where(key_cols[0] > values[0])
        else:
            query = query.where(tuple_(*key_cols) > tuple_(*values))
    query = query.order_by(*key_cols).limit(limit + 1)
    rows = list(await conn.execute(query))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]._mapping
        next_cursor = encode_cursor([last[c.key] for c in key_cols])
    return rows, next_cursor


_PG_BIGINT_MAX = 2**63 - 1


def _coerce(value: Any, ty: type) -> Any:
    try:
        if ty is str:
            if not isinstance(value, str) or "\x00" in value:
                raise TypeError("cursor value must be a string")
            return value
        if ty is int:
            if isinstance(value, bool) or not -_PG_BIGINT_MAX <= int(value) <= _PG_BIGINT_MAX:
                raise ValueError("cursor value out of range")
            return int(value)
        if ty is date:
            return date.fromisoformat(value)
        if ty is datetime:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ty is uuid.UUID:
            return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise Problem(400, "invalid_cursor", "cursor is malformed") from exc
    return value


def parse_date(value: str | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise Problem(400, "invalid_parameter", f"{field} must be an ISO date (YYYY-MM-DD)") from exc


def parse_timestamp(value: str | None, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Problem(400, "invalid_parameter", f"{field} must be an RFC 3339 timestamp") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
