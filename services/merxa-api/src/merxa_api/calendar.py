"""ISO-week helpers and the demo calendar (today = 2026-10-05, ISO W41)."""

from __future__ import annotations

import re
from datetime import date, timedelta

DEMO_TODAY = date(2026, 10, 5)
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_monday(week: str) -> date:
    m = _WEEK_RE.match(week)
    if not m:
        raise ValueError(f"not an ISO week: {week!r}")
    return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)


def week_range(first: str, last: str) -> list[str]:
    start, end = week_monday(first), week_monday(last)
    if end < start:
        raise ValueError("last week before first week")
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(iso_week(cur))
        cur += timedelta(days=7)
    return out


def is_iso_week(value: str) -> bool:
    m = _WEEK_RE.match(value)
    if not m:
        return False
    try:
        date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return False
    return True
