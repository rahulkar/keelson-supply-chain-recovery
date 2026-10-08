"""ISO-week calendar helpers (time buckets are ISO weeks ``YYYY-Www`` in M1)."""

from __future__ import annotations

import datetime as dt


def parse_iso_week(label: str) -> dt.date:
    """Return the Monday of ISO week ``YYYY-Www``."""
    year_s, week_s = label.split("-W")
    return dt.date.fromisocalendar(int(year_s), int(week_s), 1)


def iso_week_label(day: dt.date) -> str:
    """ISO week label (``YYYY-Www``) of the week containing ``day``."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def horizon_weeks(start_week: str, weeks: int) -> tuple[list[str], list[dt.date]]:
    """Labels and Mondays of ``weeks`` consecutive ISO weeks starting at ``start_week``."""
    start = parse_iso_week(start_week)
    mondays = [start + dt.timedelta(weeks=i) for i in range(weeks)]
    return [iso_week_label(m) for m in mondays], mondays


def week_index(day: dt.date, start_monday: dt.date) -> int:
    """Bucket index of ``day`` relative to the horizon start (may be < 0 or >= horizon)."""
    return (monday_of(day) - start_monday).days // 7


def short_week(label: str) -> str:
    """``2026-W43`` -> ``W43`` (display form used in constraint names)."""
    return label.split("-", 1)[1]
