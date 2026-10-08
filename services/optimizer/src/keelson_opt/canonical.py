"""Canonical JSON + input hashing (rule from ``docs/contracts/00-overview.md``).

``canonical(obj) = json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)``
and ``H = sha256(canonical(obj).encode("utf-8"))`` as lowercase hex.

Implemented locally on purpose: every integer field of the optimizer contract models is
*strict* (floats are rejected at validation), so the extra recursive float check done by
``keelson_contracts.common.canonical_json`` is redundant here and costs ~0.5 s on a
full-size (5 MB) request. ``tests/test_isoweeks_canonical.py`` asserts byte equality with the
shared implementation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def model_hash(model: BaseModel) -> str:
    """Hash of a request model: canonical JSON of its JSON-mode dump (defaults included)."""
    return sha256_hex(model.model_dump(mode="json"))
