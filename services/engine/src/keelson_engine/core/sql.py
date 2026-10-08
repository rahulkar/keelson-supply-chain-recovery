"""Tiny helpers for parameterised SQL with ``jsonb`` columns (``CAST(:p AS jsonb)``)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

__all__ = ["dumps", "jsonable", "row_dict"]


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=False)
    return value


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def dumps(value: Any) -> str | None:
    """JSON text for a ``CAST(:x AS jsonb)`` parameter (``None`` stays SQL NULL).

    ``allow_nan=False``: NaN/Infinity are not JSON and would break JS clients.
    """
    if value is None:
        return None
    return json.dumps(jsonable(value), ensure_ascii=False, separators=(",", ":"), default=_default, allow_nan=False)


def row_dict(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
