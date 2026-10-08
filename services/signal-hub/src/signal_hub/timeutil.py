"""Timestamp parsing for feed data (naive feed timestamps are UTC)."""

from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime

__all__ = ["epoch_ms", "not_after", "parse_ts", "utc_now"]


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_ts(value: object) -> dt.datetime | None:
    """ISO 8601 (``Z``/offset/naive = UTC) or RFC 822 (RSS ``pubDate``) → aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed_opt: dt.datetime | None = None
        try:
            parsed_opt = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            try:
                parsed_opt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError):
                return None
        if parsed_opt is None:
            return None
        parsed = parsed_opt
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if not (1970 <= parsed.year <= 2200):
        return None
    return parsed.astimezone(dt.UTC)


def epoch_ms(value: object) -> dt.datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return dt.datetime.fromtimestamp(value / 1000.0, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def not_after(value: dt.datetime | None, limit: dt.datetime) -> dt.datetime | None:
    """``value`` capped at ``limit`` (a publication/receipt time cannot lie after its retrieval:
    a future-dated item would otherwise never age out of recency and expiry)."""
    if value is None:
        return None
    return min(value, limit)
