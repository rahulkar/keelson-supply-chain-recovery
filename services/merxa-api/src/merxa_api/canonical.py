"""Canonical JSON + hashing exactly as specified in docs/contracts/00-overview.md."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def canonical_json(obj: Any) -> str:
    """``json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)``."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def preview_hash(items: Iterable[Mapping[str, Any]]) -> str:
    """H = sha256(canonical([{seq, op, target_ref, expected_version, payload}] ordered by seq))."""
    rows = sorted(items, key=lambda it: int(it["seq"]))
    return sha256_hex(
        [
            {
                "seq": int(it["seq"]),
                "op": it["op"],
                "target_ref": it.get("target_ref"),
                "expected_version": it.get("expected_version"),
                "payload": it.get("payload") or {},
            }
            for it in rows
        ]
    )
