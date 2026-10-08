"""RFC 6901 JSON pointers (read-only) over evidence payloads.

Besides plain pointers, a few *virtual* pointers resolve the list summaries that
``plugins.compact_for_model`` shows a model beside a cut top-level list (the stored evidence keeps
the whole list and no summary key, so the summary is recomputed from it here — one source of truth,
:func:`list_summary`):

* ``/_summary/<field>/total`` — ``len(<field>)``;
* ``/_summary/<field>/sums/<column>`` — the sum of an integer column over **all** rows;
* ``/_summary/<field>/counts/<column>/<value>`` — how many rows have that value;
* ``/_summary/<field>/range/<column>/min|max`` — the earliest / latest ISO date of a date column.

``<field>`` may also be the path of a list *inside* the result (``lines/0/orders``, ``po/lines``):
``compact_for_model`` cuts such nested lists to a prefix and shows their row count under that path,
so ``/_summary/lines/0/orders/total`` resolves to ``len(doc["lines"][0]["orders"])``.

``_truncated`` (the M4 key) and the frequent model spellings ``summary`` / ``truncated`` mean the
same thing. A document that really has such a key is resolved literally.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "SUMMARY_KEYS",
    "TRUNCATION_KEYS",
    "JsonPointerError",
    "list_summary",
    "resolve_pointer",
]

SUMMARY_KEYS: Final = ("_summary", "summary", "_truncated", "truncated")
"""Virtual summary keys (``compact_for_model`` writes ``_summary``; ``_truncated`` is the M4 key)."""
TRUNCATION_KEYS: Final = SUMMARY_KEYS
"""Back-compat alias (M4 name)."""

MAX_SUM_COLUMNS: Final = 8
MAX_COUNT_COLUMNS: Final = 6
MAX_COUNT_VALUES: Final = 8
"""A string column is counted only when it has at most this many distinct values (a status, a DC,
a mode — not a SKU or a free text)."""
MAX_COUNT_VALUE_CHARS: Final = 40
"""Longer strings (descriptions, notes) are never counted."""
MAX_RANGE_COLUMNS: Final = 4
_ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}")
_ID_LIKE: Final = re.compile(r"(^|_)(id|no|seq|version|line|index|idx|page|cursor)$")
"""Integer columns whose sum means nothing (line numbers, versions, sequence numbers)."""


class JsonPointerError(LookupError):
    pass


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """Resolve ``pointer`` (``""``, ``"/items/0/qty"``; a leading ``#`` is accepted) in ``doc``."""
    if pointer.startswith("#"):
        pointer = pointer[1:]
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise JsonPointerError(f"JSON pointer must start with '/': {pointer!r}")
    virtual = _summary_value(doc, pointer)
    if virtual is not None:
        return virtual
    current = doc
    for raw in pointer[1:].split("/"):
        token = _unescape(raw)
        if isinstance(current, dict):
            if token not in current:
                raise JsonPointerError(f"no key {token!r} in pointer {pointer!r}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise JsonPointerError(f"invalid array index {token!r} in pointer {pointer!r}")
            index = int(token)
            if index >= len(current):
                raise JsonPointerError(f"index {index} out of range in pointer {pointer!r}")
            current = current[index]
        else:
            raise JsonPointerError(f"cannot descend into a scalar at {token!r} in {pointer!r}")
    return current


# ─────────────────────────────── list summaries ───────────────────────────────


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _columns(rows: Sequence[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        if isinstance(row, Mapping):
            for key in row:
                seen.setdefault(str(key), None)
    return list(seen)


def _sum_columns(rows: Sequence[Any]) -> list[str]:
    """Integer columns worth a total: present in at least half of the rows, every non-null value an
    integer, not an id-like column."""
    out: list[str] = []
    dicts = [r for r in rows if isinstance(r, Mapping)]
    for col in _columns(dicts):
        if _ID_LIKE.search(col):
            continue
        values = [r.get(col) for r in dicts if r.get(col) is not None]
        if values and len(values) * 2 >= len(dicts) and all(_is_int(v) for v in values):
            out.append(col)
    return out[:MAX_SUM_COLUMNS]


def _count_columns(rows: Sequence[Any]) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {}
    dicts = [r for r in rows if isinstance(r, Mapping)]
    for col in _columns(dicts):
        values = [r.get(col) for r in dicts if col in r]
        if (not values or not all(isinstance(v, str) and len(v) <= MAX_COUNT_VALUE_CHARS for v in values)
                or _ISO_DATE_RE.match(str(values[0]))):
            continue
        counts = Counter(str(v) for v in values)
        if 1 < len(counts) <= MAX_COUNT_VALUES or (len(counts) == 1 and len(values) > 1):
            out[col] = counts
        if len(out) >= MAX_COUNT_COLUMNS:
            break
    return out


def _range_columns(rows: Sequence[Any]) -> list[str]:
    dicts = [r for r in rows if isinstance(r, Mapping)]
    out = []
    for col in _columns(dicts):
        values = [r.get(col) for r in dicts if r.get(col) is not None]
        if values and all(isinstance(v, str) and _ISO_DATE_RE.match(v) for v in values):
            out.append(col)
    return out[:MAX_RANGE_COLUMNS]


def list_summary(rows: Sequence[Any], *, shown: int) -> dict[str, Any]:
    """What a model sees instead of the cut part of a list: ``{total, shown, sums, counts, range}``
    over **all** rows (every value resolvable through the virtual pointers above)."""
    out: dict[str, Any] = {"total": len(rows), "shown": shown}
    sums = {col: sum(r.get(col) or 0 for r in rows if isinstance(r, Mapping)) for col in _sum_columns(rows)}
    if sums:
        out["sums"] = sums
    counts = {col: dict(c.most_common()) for col, c in _count_columns(rows).items()}
    if counts:
        out["counts"] = counts
    ranges = {}
    for col in _range_columns(rows):
        values = [str(r.get(col)) for r in rows if isinstance(r, Mapping) and r.get(col) is not None]
        ranges[col] = {"min": min(values), "max": max(values)}
    if ranges:
        out["range"] = ranges
    return out


_SUMMARY_PARTS: Final = frozenset({"total", "sums", "counts", "range"})


def _list_at(doc: Any, path: Sequence[str]) -> list[Any] | None:
    """The list at ``path`` (keys / indices) inside ``doc``, or ``None``."""
    current = doc
    for token in path:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and (len(token) == 1 or not token.startswith("0")):
            index = int(token)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current if isinstance(current, list) else None


def _summary_of(rows: list[Any], what: Sequence[str]) -> Any:
    head = what[0]
    if head == "total" and len(what) == 1:
        return len(rows)
    if head == "sums" and len(what) == 2 and what[1] in _sum_columns(rows):
        return sum(r.get(what[1]) or 0 for r in rows if isinstance(r, Mapping))
    if head == "counts" and len(what) == 3:
        counts = _count_columns(rows).get(what[1])
        return counts.get(what[2]) if counts is not None and what[2] in counts else None
    if head == "range" and len(what) == 3 and what[2] in ("min", "max") and what[1] in _range_columns(rows):
        values = [str(r.get(what[1])) for r in rows if isinstance(r, Mapping) and r.get(what[1]) is not None]
        return min(values) if what[2] == "min" else max(values)
    return None


def _summary_value(doc: Any, pointer: str) -> Any:
    """The value of a virtual summary pointer, or ``None`` when ``pointer`` is not one (or does not
    resolve: the caller then raises the usual "no key" error). The list path may be nested
    (``/_summary/lines/0/orders/total``): every split of the pointer into ``<path>/<what>`` whose
    path names a list is tried, longest path first."""
    parts = [_unescape(p) for p in pointer[1:].split("/")]
    if len(parts) < 3 or parts[0] not in SUMMARY_KEYS or not isinstance(doc, dict) or parts[0] in doc:
        return None
    for split in range(len(parts) - 1, 1, -1):
        what = parts[split:]
        if what[0] not in _SUMMARY_PARTS:
            continue
        path = parts[1:split]
        rows = _list_at(doc, path)
        if rows is None and len(path) == 1 and "/" in path[0]:
            rows = _list_at(doc, path[0].split("/"))  # an escaped path key ("lines~10~1orders")
        if rows is None:
            continue
        value = _summary_of(rows, what)
        if value is not None:
            return value
    return None
