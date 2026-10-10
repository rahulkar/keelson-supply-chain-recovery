"""Near-duplicate news items (``docs/contracts/123`` §2): the SimHash index lookup at queue time
and the extraction reuse for cross-source copies.

**Which items.** Trade-press news only (``origin news``, or an ``rss.*`` / ``demo.rss.*``
connector): templated free text of connected systems (supplier messages that differ by a PO number)
must never be folded together.

**Lookup** (:func:`find_original`, in the item's own transaction and RLS scope, serialised per
scope by an advisory lock): the band index (GIN ``&&``) narrows the candidates, ``bit_count`` gives
the exact Hamming distance, and only *originals* qualify (not copies, not duplicates) within
``window_hours`` and the same scope rules as clustering:

* a global item matches global items only;
* a tenant item matches global items and the tenant's own (never another tenant's: RLS);
* a demo-pack item matches items of the same tenant and pack only.

**Outcomes** (:func:`classify`):

* the **same source** with an **identical** fingerprint (0 bits) → ``duplicate`` (a re-post under a
  new id: never extracted, never a signal). The same source with 1..k bits is an edited update and
  stays a normal item (corroboration already counts a source once);
* **another source** within ``max_hamming_bits`` → a near-duplicate copy (``near_duplicate_of``,
  ``near_duplicate_bits``): it still becomes a signal, joins its original's event and adds no
  source to its corroboration.

**Extraction reuse** (:func:`reuse_outcome`, ``reuse_extraction``): a copy without a pre-baked
extraction first runs the deterministic pattern screen on its **own** text (a hit flags it); then

* original ``extracted`` and **every place / port / chokepoint / company the original's extraction
  names also appears in the copy's text** → the stored extraction is re-validated and reused
  (``extracted_by`` = ``simhash:<original's>``): no guard or extractor call, the copy's text never
  reaches an LLM. A copy that no longer names one of them (a templated story about another port
  differs by a few bits only) runs its own quarantine instead;
* original ``pending_extraction`` → the copy waits (no attempt and no bad output is counted);
* original ``flagged`` → the copy is flagged too (``{by: near_duplicate, of: <id>}``);
* original ``rejected`` / ``failed`` → the copy goes through the normal quarantine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final

import asyncpg
from pydantic import ValidationError

from keelson_contracts.llm import NewsExtraction, news_extraction_violations
from keelson_contracts.signals import SimHashSettings
from signal_hub.gazetteer import normalise
from signal_hub.quarantine import ExtractionOutcome, pattern_flags
from signal_hub.simhash import Fingerprint, hamming

__all__ = ["REUSE_PREFIX", "Defer", "Match", "applies", "classify", "find_original", "names_every_entity",
           "reuse_outcome"]

REUSE_PREFIX: Final = "simhash:"
MAX_CANDIDATES: Final = 20
DEFER_AFTER_ORIGINAL: Final = dt.timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Match:
    """The closest original of an item: its id, source and Hamming distance."""

    id: str
    connector_id: str
    bits: int


@dataclass(frozen=True, slots=True)
class Defer:
    """Wait for the original's extraction (retry at ``until``)."""

    until: dt.datetime


def applies(connector_id: str, origin: str) -> bool:
    """SimHash runs on trade-press news only (see the module docstring)."""
    return origin == "news" or connector_id.startswith(("rss.", "demo.rss."))


async def find_original(conn: asyncpg.Connection, *, item_id: str, tenant: str | None, pack_id: str | None,
                        fp: Fingerprint, connector_id: str, settings: SimHashSettings,
                        now: dt.datetime) -> tuple[Match | None, Match | None]:
    """``(identical item of the same source, closest original of another source)`` within the
    window (either may be ``None``). ``conn`` is in the item's RLS scope."""
    scope_key = f"{tenant or ''}/{pack_id or ''}"
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"simhash:{scope_key}")
    if pack_id is not None:
        scope_sql, args = "tenant = $6 AND pack_id = $7", [tenant, pack_id]
    elif tenant is not None:
        scope_sql, args = "(tenant IS NULL OR tenant = $6) AND pack_id IS NULL", [tenant]
    else:
        scope_sql, args = "tenant IS NULL AND pack_id IS NULL", []
    rows = await conn.fetch(
        f"""SELECT id, connector_id, simhash, coalesce(published_at, retrieved_at) AS at
            FROM news_item
            WHERE simhash_bands && $1::int[] AND simhash IS NOT NULL AND id <> $2
              AND near_duplicate_of IS NULL AND status <> 'duplicate'
              AND retrieved_at >= $3::timestamptz - make_interval(hours => $4::int) AND {scope_sql}
            ORDER BY bit_count((simhash # $5::bigint)::bit(64)), coalesce(published_at, retrieved_at), id
            LIMIT {MAX_CANDIDATES}""",
        list(fp.bands), item_id, now, settings.window_hours, fp.value, *args,
    )
    return classify(rows, fp=fp, connector_id=connector_id, max_bits=settings.max_hamming_bits)


def classify(rows: list[Any], *, fp: Fingerprint, connector_id: str, max_bits: int) -> tuple[Match | None, Match | None]:
    """Pure part of :func:`find_original` over ``{id, connector_id, simhash, at}`` rows."""
    same: Match | None = None
    other: Match | None = None
    ordered = sorted(rows, key=lambda r: (hamming(int(r["simhash"]), fp.value), r["at"], str(r["id"])))
    for r in ordered:
        bits = hamming(int(r["simhash"]), fp.value)
        if bits > max_bits:
            break
        if r["connector_id"] == connector_id:
            if bits == 0 and same is None:
                same = Match(str(r["id"]), r["connector_id"], 0)
        elif other is None:
            other = Match(str(r["id"]), r["connector_id"], bits)
    return same, other


def names_every_entity(text: str, extraction: NewsExtraction) -> bool:
    """Whether every place, port, chokepoint and company of ``extraction`` occurs in ``text``
    (whole words, case / accents / punctuation ignored)."""
    haystack = f" {normalise(text)} "
    for mention in (*extraction.places, *extraction.ports, *extraction.chokepoints, *extraction.companies):
        needle = normalise(mention)
        if needle and f" {needle} " not in haystack:
            return False
    return True


def reuse_outcome(text: str, original: dict[str, Any], now: dt.datetime) -> ExtractionOutcome | Defer | None:
    """What a cross-source copy does instead of its own quarantine (see the module docstring);
    ``None`` = run the normal quarantine."""
    flags = pattern_flags(text)
    if flags:
        return ExtractionOutcome("flagged", guard={"by": "pattern", "patterns": flags[:5]})
    status = original.get("status")
    if status == "flagged":
        return ExtractionOutcome("flagged", guard={"by": "near_duplicate", "of": str(original["id"])})
    if status == "pending_extraction":
        nxt = original.get("next_attempt_at")
        return Defer(max(now, nxt if isinstance(nxt, dt.datetime) else now) + DEFER_AFTER_ORIGINAL)
    if status != "extracted" or not isinstance(original.get("extraction"), dict):
        return None
    try:
        extraction = NewsExtraction.model_validate(original["extraction"])
    except ValidationError:
        return None
    if extraction.injection_suspected or news_extraction_violations(extraction):
        return None
    if not names_every_entity(text, extraction):
        return None
    by = f"{REUSE_PREFIX}{original.get('extracted_by') or 'unknown'}"[:120]
    return ExtractionOutcome("extracted", extraction=extraction, extracted_by=by,
                             guard={"by": "near_duplicate", "of": str(original["id"])})
