"""Advisory labelling of plans built on crawled data (``docs/contracts/110-m7-backoffice-generic.md`` §4).

A tier-A connector's data is gathered by paging plain read tools over a time range (snapshot
``consistency: crawled`` + ``as_of_range``), not taken at one point in time. Plans built on it
stay applicable (per-op, with approval as usual) but are labelled **advisory**: the
``recommended_option@1``, ``scenario_compare@1`` and ``narrative@1`` blocks carry
:class:`~keelson_contracts.blocks.Advisory`, the narrative says so in words, and the scenario's
``data_source`` assumption names it (``nodes.framing.data_source_assumption``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

import structlog
from keelson_contracts.blocks import Advisory, advisory_for
from keelson_contracts.scn import CRAWLED_ADVISORY_TEXT, AsOfRange, SnapshotLabel
from pydantic import ValidationError

__all__ = ["ADVISORY_SENTENCE", "advisory_from_kpis", "advisory_from_label"]

log = structlog.get_logger(__name__)

ADVISORY_SENTENCE: Final = (
    f"Advisory: {CRAWLED_ADVISORY_TEXT[:1].lower()}{CRAWLED_ADVISORY_TEXT[1:]}, so check the figures in the "
    "source system before relying on them."
)
"""The narrative's advisory line (no numbers: it passes the numbers rule as it is)."""


def _parse(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def advisory_from_label(label: Mapping[str, Any] | None) -> Advisory | None:
    """``Advisory`` for a ``{consistency, as_of_range}`` label (``None`` unless ``crawled``). A
    malformed range never drops the label: it is repaired (ordered, or both ends = the one known)."""
    if not isinstance(label, Mapping) or label.get("consistency") != "crawled":
        return None
    raw = label.get("as_of_range")
    try:
        return advisory_for(SnapshotLabel.model_validate({"consistency": "crawled", "as_of_range": raw}))
    except ValidationError:
        pass
    ends = [p for p in (_parse((raw or {}).get("from")), _parse((raw or {}).get("to"))) if p is not None] \
        if isinstance(raw, Mapping) else []
    if not ends:
        ends = [dt.datetime.now(dt.UTC)]
    log.warning("crawled snapshot label repaired", label=dict(label))
    return Advisory(reason="crawled_snapshot", text=CRAWLED_ADVISORY_TEXT,
                    as_of_range=AsOfRange.model_validate({"from": min(ends), "to": max(ends)}))


def advisory_from_kpis(kpis: Mapping[str, Any] | None) -> Advisory | None:
    """The advisory label of a saved plan (``plan.kpis.snapshot_label``)."""
    return advisory_from_label((kpis or {}).get("snapshot_label") if isinstance(kpis, Mapping) else None)
