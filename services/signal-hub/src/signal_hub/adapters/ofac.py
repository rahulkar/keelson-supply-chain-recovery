"""OFAC SDN list delta adapter (``124`` §2): the legacy CSV trio → one ``regulatory`` signal per
publication that adds or changes organisations, matched to tenants by exact organisation name.

Files (Sanctions List Service exports, checked 2026-10-08; the export URL answers 302 to a
presigned S3 object that carries ETag / Last-Modified): ``SDN.CSV`` (``ent_num, SDN_Name, SDN_Type,
Program, Title, Call_Sign, Vess_type, Tonnage, GRT, Vess_flag, Vess_owner, Remarks``), ``ALT.CSV``
(``ent_num, alt_num, alt_type, alt_name, alt_remarks``) and ``ADD.CSV`` (``ent_num, add_num,
address, city/state/postal, country, add_remarks``). Quoted CSV, no header, ``-0-`` (often with
trailing blanks) means null, CRLF line ends and a trailing ``\\x1a`` (DOS end-of-file); UTF-8 with a
cp1252 fallback. Programs are ``"IRAN] [SDGT"``.

Only **organisations** count: ``SDN_Type`` null (individuals, vessels and aircraft are skipped).
Each entity's hash covers its name, programs, aliases and countries; the poller keeps the last
snapshot in ``source_snapshot`` and diffs every new publication against it (the first one is a
silent baseline). The publication signal carries the :func:`signal_hub.names.match_key` of every
name and alias of the new and changed entities (``match_keys``, at most :data:`MAX_MATCH_KEYS`),
no geometry and no ``location_refs`` — nothing attaches to it by place; a tenant is exposed only
when one of its supplier/factory names matches (``entity_match``, :mod:`signal_hub.exposure`).

This is awareness, not sanctions screening: exact normalised names, deltas only, SDN list only.
Untrusted text: entity names never appear in the title or summary.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from keelson_contracts.common import canonical_json
from signal_hub import names, scoring
from signal_hub.models import SignalDraft, SourceContext, clip

__all__ = [
    "ATTRIBUTION",
    "CONNECTOR_ID",
    "LICENSE",
    "MAX_MATCH_KEYS",
    "SOURCE_NAME",
    "Delta",
    "Entity",
    "OfacError",
    "diff",
    "hashes",
    "parse_entities",
    "parse_rows",
    "publication_draft",
]

CONNECTOR_ID: Final = "ofac"
SOURCE_NAME: Final = "U.S. Treasury OFAC SDN list"
LICENSE: Final = "Public domain (U.S. Treasury OFAC)"
ATTRIBUTION: Final = "Source: U.S. Department of the Treasury, Office of Foreign Assets Control"
SOURCE_URL: Final = "https://ofac.treasury.gov/sanctions-list-service"
MAX_ROWS: Final = 200_000
MAX_FIELD_CHARS: Final = 20_000
MAX_MATCH_KEYS: Final = 20_000
ALIAS_TYPES: Final = frozenset({"aka", "fka", "nka"})
_NULL: Final = "-0-"


class OfacError(ValueError):
    """An SDN/ALT/ADD file the adapter cannot read."""


@dataclass(slots=True)
class Entity:
    ent_num: str
    name: str
    programs: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)

    def item_hash(self) -> str:
        payload = {"name": self.name, "programs": sorted(set(self.programs)), "aliases": sorted(set(self.aliases)),
                   "countries": sorted(set(self.countries))}
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


@dataclass(frozen=True, slots=True)
class Delta:
    new: list[str]
    changed: list[str]
    removed: list[str]

    @property
    def any(self) -> bool:
        return bool(self.new or self.changed)


def _decode(body: bytes) -> str:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252", errors="replace")
    return text.rstrip("\x1a\r\n\t ")


def _cell(value: str) -> str | None:
    value = value.strip()
    return None if not value or value == _NULL else value


def parse_rows(body: bytes, *, min_columns: int) -> list[list[str | None]]:
    """CSV rows with ``-0-`` → None (blank lines skipped; rows shorter than ``min_columns`` refused)."""
    rows: list[list[str | None]] = []
    reader = csv.reader(io.StringIO(_decode(body), newline=""), strict=True)
    try:
        for raw in reader:
            if not raw or (len(raw) == 1 and not raw[0].strip()):
                continue
            if len(rows) >= MAX_ROWS:
                raise OfacError(f"more than {MAX_ROWS} rows")
            if len(raw) < min_columns:
                raise OfacError(f"row {reader.line_num}: {len(raw)} columns (expected at least {min_columns})")
            if any(len(v) > MAX_FIELD_CHARS for v in raw):
                raise OfacError(f"row {reader.line_num}: a field is longer than {MAX_FIELD_CHARS} characters")
            rows.append([_cell(v) for v in raw])
    except csv.Error as exc:
        raise OfacError(f"malformed CSV ({exc})"[:200]) from None
    return rows


def parse_entities(sdn: bytes, alt: bytes, add: bytes) -> dict[str, Entity]:
    """Organisations of one publication by ``ent_num`` (with aliases and address countries)."""
    entities: dict[str, Entity] = {}
    for row in parse_rows(sdn, min_columns=4):
        ent, name, sdn_type, program = row[0], row[1], row[2], row[3]
        if ent is None or not ent.isdigit() or name is None or sdn_type is not None:
            continue  # individuals, vessels and aircraft carry an SDN_Type; organisations do not
        programs = [p.strip(" []") for p in (program or "").split("] [") if p.strip(" []")]
        entities[ent] = Entity(ent, name, programs)
    for row in parse_rows(alt, min_columns=4):
        ent, alt_type, alt_name = row[0], (row[2] or "").lower(), row[3]
        if ent in entities and alt_type in ALIAS_TYPES and alt_name:
            entities[ent].aliases.append(alt_name)
    for row in parse_rows(add, min_columns=5):
        ent, country = row[0], row[4]
        if ent in entities and country and country not in entities[ent].countries:
            entities[ent].countries.append(country)
    return entities


def diff(current: Mapping[str, str], previous: Mapping[str, str]) -> Delta:
    """``item_key → item_hash`` maps → new / changed / removed keys (sorted)."""
    return Delta(
        new=sorted(k for k in current if k not in previous),
        changed=sorted(k for k in current if k in previous and previous[k] != current[k]),
        removed=sorted(k for k in previous if k not in current),
    )


def publication_draft(
    entities: Mapping[str, Entity],
    delta: Delta,
    ctx: SourceContext,
    *,
    published_at: dt.datetime,
    ttl_days: int = 30,
) -> SignalDraft:
    """The publication signal of a delta with new or changed organisations."""
    touched = [entities[k] for k in [*delta.new, *delta.changed] if k in entities]
    keys = names.match_keys((n for e in touched for n in e.all_names()), limit=MAX_MATCH_KEYS)
    programs = sorted({p for e in touched for p in e.programs})
    stamp = published_at.astimezone(dt.UTC)
    native = {
        "scheme": "ofac", "list": "SDN", "new": len(delta.new), "changed": len(delta.changed),
        "removed": len(delta.removed), "match_keys": len(keys), "programs": ", ".join(programs)[:200] or None,
    }
    title = (f"OFAC SDN list update {stamp:%Y-%m-%d}: {len(delta.new)} new and {len(delta.changed)} changed "
             "organisation entries")
    summary = (f"U.S. Treasury OFAC Specially Designated Nationals list publication of {stamp:%Y-%m-%d %H:%M} UTC"
               + (f" (programs: {', '.join(programs[:12])}{' …' if len(programs) > 12 else ''})" if programs else "")
               + ". Awareness only: exact organisation-name matches against your suppliers and factories; "
                 "not a sanctions-screening result.")
    return SignalDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(CONNECTOR_ID),
        native_key=ctx.key(f"sdn:{stamp:%Y%m%dT%H%M%SZ}"),
        origin=ctx.origin("feed"),
        kind="regulatory",
        subtype="sanctions_designation",
        title=clip(title, 300) or "OFAC SDN list update",
        summary=clip(summary, 2000),
        raw_text=None,
        geometry=None,
        location_refs=[],
        starts_at=stamp,
        ends_at=None,
        observed_at=min(stamp, ctx.retrieved_at),
        expires_at=stamp + dt.timedelta(days=max(1, ttl_days)),
        severity_bp=scoring.OFAC_SEVERITY_BP,
        severity_native=native,
        provenance=ctx.provenance("fact"),
        extracted_by=None,
        source_name=ctx.source_name(SOURCE_NAME),
        source_url=SOURCE_URL,
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["ofac"],
        retrieved_at=ctx.retrieved_at,
        native_id=f"SDN:{stamp:%Y-%m-%dT%H:%M:%SZ}",
        authoritative=True,
        pack_id=ctx.pack_id,
        match_keys=keys,
    )


def hashes(entities: Mapping[str, Entity]) -> dict[str, str]:
    """``ent_num → item_hash`` of a publication (the ``source_snapshot`` rows)."""
    return {k: e.item_hash() for k, e in entities.items()}
