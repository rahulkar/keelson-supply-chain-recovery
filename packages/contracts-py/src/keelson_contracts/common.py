"""Shared primitives: provenance, money/units, ISO weeks, canonical hashing, ids.

Everything in this module is dependency-free apart from Pydantic and is safe to
import from any Keelson service (engine, optimizer) or tool script.

Canonical hashing follows ``docs/contracts/00-overview.md`` exactly::

    canonical(obj) = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    H              = sha256(canonical(obj).encode("utf-8")).hexdigest()

Hashed payloads must not contain floats (money is integer cents, ratios are
basis points). :func:`canonical_json` rejects floats, non-string dict keys,
integers outside the IEEE-754 safe range and strings that are not valid Unicode
(lone surrogates). For such payloads the output equals RFC 8785 (JCS) whenever
object keys are BMP characters; JCS orders keys by UTF-16 code units while
``sort_keys`` orders by code point, so keys containing characters above U+FFFF
may sort differently. The contract (and Merxa) define the hash with
``json.dumps``, so that ordering is authoritative.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

__all__ = [
    "BLOCK_ID_PATTERN",
    "BP_ONE",
    "BasisPoints",
    "BlockId",
    "CanonicalJsonError",
    "CaseMode",
    "Cents",
    "ContractModel",
    "EVIDENCE_ID_PATTERN",
    "EntityRef",
    "EntityRefType",
    "EVENT_TAXONOMY_VERSION",
    "EVENT_TYPE_KEYS",
    "EventFamily",
    "EventTypeKey",
    "EvidenceId",
    "ISO_DATE_PATTERN",
    "ISO_WEEK_PATTERN",
    "IsoDate",
    "IsoWeek",
    "MAX_SAFE_INTEGER",
    "NonNegInt",
    "PositiveInt",
    "Provenance",
    "ProvenanceStamp",
    "PROVENANCE_LABELS",
    "SHA256_HEX_PATTERN",
    "Sha256Hex",
    "TENANT_SLUG_PATTERN",
    "TenantSlug",
    "add_weeks",
    "apply_bp",
    "canonical_json",
    "committed",
    "eur_to_cents",
    "event_family_of",
    "factor_bp_from_pct_change",
    "format_eur_cents",
    "is_block_id",
    "is_evidence_id",
    "is_grounded",
    "iso_week_of",
    "monday_of",
    "needs_confirmation",
    "new_block_id",
    "new_evidence_id",
    "new_run_id",
    "parse_iso_week",
    "pct_to_bp",
    "ratio_bp",
    "sha256_hex",
    "stable_block_id",
    "to_canonical_data",
    "uuid7",
    "week_range",
    "weeks_in_year",
]


class ContractModel(BaseModel):
    """Base class for every contract model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ───────────────────────────── shared literals ─────────────────────────────

CaseMode = Literal["actual", "what_if"]
"""A case is either acting on reality or exploring a hypothetical (never commits)."""

EventFamily = Literal["SUP", "LOG", "NAT", "CYB", "REG", "DEM", "OTHER"]
"""Disruption taxonomy families (``data/taxonomy``)."""

EventTypeKey = Literal[
    "SUP.delay",
    "SUP.capacity_loss",
    "LOG.chokepoint_closure",
    "LOG.port_closure",
    "LOG.strike",
    "NAT.storm",
    "NAT.flood",
    "NAT.earthquake",
    "DEM.spike",
    "DEM.drop",
    "OTHER",
]
"""Event types of the disruption taxonomy (``data/taxonomy/events.yaml``, ``90`` §1).

The single source of truth for the intake enum (``llm.DisruptionExtract.event_type``);
the engine checks at startup that the YAML keys equal :data:`EVENT_TYPE_KEYS`.
``NAT.earthquake`` was added in M6 (``100`` §6, taxonomy version 2)."""

EVENT_TYPE_KEYS: Final[tuple[str, ...]] = get_args(EventTypeKey)
"""Every taxonomy event type key, in declaration order."""

EVENT_TAXONOMY_VERSION: Final = 2
"""Version of the taxonomy key set above (bump when keys are added or removed)."""


def event_family_of(event_type: str) -> EventFamily:
    """The family of a taxonomy key (``"LOG.strike"`` → ``"LOG"``; ``"OTHER"`` → ``"OTHER"``).

    Raises ``ValueError`` for keys outside :data:`EVENT_TYPE_KEYS`.
    """
    if event_type not in EVENT_TYPE_KEYS:
        raise ValueError(f"unknown taxonomy event type {event_type!r}; known: {', '.join(EVENT_TYPE_KEYS)}")
    family = event_type.split(".", 1)[0]
    assert family in get_args(EventFamily)
    return family  # type: ignore[return-value]


EntityRefType = Literal[
    "supplier",
    "po",
    "po_line",
    "sku",
    "style",
    "dc",
    "cluster",
    "store",
    "factory",
    "component",
    "port",
    "lane",
    "chokepoint",
    "region",
    "other",
]
"""Entity kinds that can be referenced once resolved against a connector
(``chokepoint``/``region`` added in M4: resolved from ``data/geo``)."""


class EntityRef(ContractModel):
    """A resolved business entity (``{type, code, label}``).

    Strict-compatible (all fields required, no defaults) so it can be used inside
    LLM output schemas as well as render blocks.
    """

    type: EntityRefType
    code: str
    label: str


# ─────────────────────────────── provenance ────────────────────────────────


class Provenance(StrEnum):
    """Where a number or statement comes from (plan §3.3)."""

    FACT = "fact"
    """ERP or tool read, with an evidence id."""
    INFERENCE = "inference"
    """LLM claim with evidence pointers."""
    ASSUMPTION = "assumption"
    """Tenant prior / template default."""
    SIMULATED = "simulated"
    """Solver output."""
    DECISION = "decision"
    """Human confirmation, approval, or a user-stated value."""
    COMMITTED = "committed"
    """Applied in the target system; carries a ``verified`` flag after read-back."""


PROVENANCE_LABELS: Final[Mapping[Provenance, str]] = {
    Provenance.FACT: "Fact",
    Provenance.INFERENCE: "Inference",
    Provenance.ASSUMPTION: "Assumption",
    Provenance.SIMULATED: "Simulated",
    Provenance.DECISION: "Decision",
    Provenance.COMMITTED: "Committed",
}

_GROUNDED: Final = frozenset({Provenance.FACT, Provenance.DECISION})
_NEEDS_CONFIRMATION: Final = frozenset({Provenance.INFERENCE, Provenance.ASSUMPTION})


def is_grounded(prov: Provenance | str) -> bool:
    """True for ``fact`` and ``decision`` — values that may drive auto-confirmation."""
    return Provenance(prov) in _GROUNDED


def needs_confirmation(prov: Provenance | str) -> bool:
    """True for ``inference`` and ``assumption`` — nothing depending on these may commit."""
    return Provenance(prov) in _NEEDS_CONFIRMATION


class ProvenanceStamp(ContractModel):
    """A provenance label plus the read-back ``verified`` flag.

    ``verified`` is only meaningful (and only allowed) for ``committed``.
    """

    kind: Provenance
    verified: bool = False

    @model_validator(mode="after")
    def _verified_only_for_committed(self) -> ProvenanceStamp:
        if self.verified and self.kind is not Provenance.COMMITTED:
            raise ValueError("verified=True is only allowed for provenance 'committed'")
        return self

    @property
    def is_verified_commit(self) -> bool:
        return self.kind is Provenance.COMMITTED and self.verified


def committed(*, verified: bool = False) -> ProvenanceStamp:
    """Stamp for a value applied in the target system (``verified`` after read-back)."""
    return ProvenanceStamp(kind=Provenance.COMMITTED, verified=verified)


# ───────────────────────────── money and units ─────────────────────────────

MAX_SAFE_INTEGER: Final = 2**53 - 1
"""Largest integer a JavaScript consumer can represent exactly."""

Cents = Annotated[StrictInt, Field(description="Money in integer EUR minor units (cents).")]
BasisPoints = Annotated[StrictInt, Field(description="Basis points; 10000 = 100 % = x1.0.")]
NonNegInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]

BP_ONE: Final = 10_000
"""Basis points representing x1.0 (or 100 %)."""


def _div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero (deterministic, sign-symmetric)."""
    if denominator == 0:
        raise ZeroDivisionError("division by zero")
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    q, r = divmod(abs(numerator), abs(denominator))
    if 2 * r >= abs(denominator):
        q += 1
    return sign * q


def apply_bp(value: int, bp: int) -> int:
    """Scale an integer by basis points with half-up rounding: ``apply_bp(3000, 11800) == 3540``."""
    _require_int(value, "value")
    _require_int(bp, "bp")
    return _div_round_half_up(value * bp, BP_ONE)


def ratio_bp(part: int, whole: int) -> int:
    """``part / whole`` in basis points (half-up). Raises ``ValueError`` if ``whole == 0``."""
    _require_int(part, "part")
    _require_int(whole, "whole")
    if whole == 0:
        raise ValueError("ratio_bp: whole must be non-zero")
    return _div_round_half_up(part * BP_ONE, whole)


def pct_to_bp(pct: int) -> int:
    """Whole percent to basis points: ``18 -> 1800``."""
    _require_int(pct, "pct")
    return pct * 100


def factor_bp_from_pct_change(pct_change: int) -> int:
    """A ``+18 %`` change as a multiplicative factor in basis points: ``18 -> 11800``."""
    return BP_ONE + pct_to_bp(pct_change)


def eur_to_cents(amount: int | str | Decimal) -> int:
    """Convert a EUR amount (``"12.40"``, ``Decimal("12.4")``, ``12``) to integer cents.

    Floats are rejected on purpose; amounts with more than two decimals raise.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError("eur_to_cents: pass int, str or Decimal, never float")
    try:
        d = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except InvalidOperation as exc:
        raise ValueError(f"eur_to_cents: not a decimal amount: {amount!r}") from exc
    if not d.is_finite():
        raise ValueError("eur_to_cents: amount must be finite")
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"eur_to_cents: more than two decimals: {amount!r}")
    return int(cents.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_eur_cents(cents: int, *, show_cents: bool = False) -> str:
    """Human formatting used by narratives: ``1240000 -> "€12,400"`` (half-up to whole euros)."""
    _require_int(cents, "cents")
    sign = "-" if cents < 0 else ""
    if show_cents:
        euros, rem = divmod(abs(cents), 100)
        return f"{sign}€{euros:,}.{rem:02d}"
    return f"{sign}€{_div_round_half_up(abs(cents), 100):,}"


def _require_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")


# ─────────────────────────────── ISO weeks ─────────────────────────────────

ISO_WEEK_PATTERN: Final = r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$"
_ISO_WEEK_RE: Final = re.compile(ISO_WEEK_PATTERN)


def weeks_in_year(year: int) -> int:
    """Number of ISO weeks (52 or 53) in ``year``."""
    return dt.date(year, 12, 28).isocalendar().week


def parse_iso_week(week: str) -> tuple[int, int]:
    """Parse ``"2026-W41"`` into ``(2026, 41)``; validates that the week exists."""
    if not isinstance(week, str) or not _ISO_WEEK_RE.match(week):
        raise ValueError(f"not an ISO week (YYYY-Www): {week!r}")
    year, wk = int(week[:4]), int(week[6:])
    if wk > weeks_in_year(year):
        raise ValueError(f"ISO year {year} has no week {wk}")
    return year, wk


def iso_week_of(day: dt.date) -> str:
    """ISO week label of a date: ``date(2026, 10, 5) -> "2026-W41"``."""
    iso = day.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def monday_of(week: str) -> dt.date:
    """Monday of an ISO week: ``"2026-W45" -> date(2026, 11, 2)``."""
    year, wk = parse_iso_week(week)
    return dt.date.fromisocalendar(year, wk, 1)


def add_weeks(week: str, n: int) -> str:
    """Shift an ISO week by ``n`` weeks (may cross year boundaries)."""
    _require_int(n, "n")
    return iso_week_of(monday_of(week) + dt.timedelta(weeks=n))


def week_range(start_week: str, weeks: int) -> list[str]:
    """``weeks`` consecutive ISO weeks starting at ``start_week`` (inclusive)."""
    _require_int(weeks, "weeks")
    if weeks < 0:
        raise ValueError("weeks must be >= 0")
    first = monday_of(start_week)
    return [iso_week_of(first + dt.timedelta(weeks=i)) for i in range(weeks)]


def _validate_iso_week(value: str) -> str:
    parse_iso_week(value)
    return value


IsoWeek = Annotated[str, Field(pattern=ISO_WEEK_PATTERN), AfterValidator(_validate_iso_week)]
"""``YYYY-Www`` string validated against the real ISO calendar."""

ISO_DATE_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}$"
_ISO_DATE_RE: Final = re.compile(ISO_DATE_PATTERN)


def _iso_date_input(value: object) -> object:
    """Only accept ``YYYY-MM-DD`` strings or real ``date`` objects for contract dates.

    Pydantic's lax ``date`` also accepts Unix timestamps (``1760832000`` becomes
    2025-10-19) and datetime strings (``"2026-10-19T23:00:00Z"``), silently changing
    what a change-set item means; contracts say dates are ISO ``YYYY-MM-DD``.
    """
    if isinstance(value, dt.datetime):
        raise ValueError("expected a date (YYYY-MM-DD), got a datetime")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.match(value):
        return value
    raise ValueError(f"expected an ISO date string YYYY-MM-DD, got {value!r}")


IsoDate = Annotated[dt.date, BeforeValidator(_iso_date_input)]
"""A calendar date given as ``YYYY-MM-DD`` (or a ``date``); timestamps and datetimes are rejected."""

TENANT_SLUG_PATTERN: Final = r"^[a-z0-9][a-z0-9-]{0,62}$"
TenantSlug = Annotated[str, Field(pattern=TENANT_SLUG_PATTERN)]

SHA256_HEX_PATTERN: Final = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, Field(pattern=SHA256_HEX_PATTERN)]


# ─────────────────────────── canonical hashing ─────────────────────────────


class CanonicalJsonError(ValueError):
    """Raised when a value cannot be hashed canonically (float, bad key, huge int, …)."""


def _normalize(obj: Any, path: str) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        if abs(obj) > MAX_SAFE_INTEGER:
            raise CanonicalJsonError(
                f"integer at {path} exceeds 2^53-1 ({obj}); not representable by JSON clients"
            )
        return obj
    if isinstance(obj, float):
        raise CanonicalJsonError(
            f"float at {path} ({obj!r}) is not allowed in hashed payloads; "
            "use integer minor units (cents), integer quantities or basis points"
        )
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="json"), path)
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(
                    f"non-string key {key!r} at {path}; canonical JSON objects need string keys"
                )
            out[key] = _normalize(value, f"{path}/{_escape_pointer(key)}")
        return out
    if isinstance(obj, Sequence) and not isinstance(obj, (bytes, bytearray)):
        return [_normalize(v, f"{path}/{i}") for i, v in enumerate(obj)]
    raise CanonicalJsonError(
        f"unsupported type {type(obj).__name__} at {path}; convert to JSON types first"
    )


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def to_canonical_data(obj: Any) -> Any:
    """Validate and convert ``obj`` (JSON types, tuples, Pydantic models) into plain JSON data.

    Pydantic models are dumped with ``mode="json"`` (dates become ISO strings, enums
    their values). Raises :class:`CanonicalJsonError` with a JSON-pointer path on
    floats, non-string keys, unsafe integers or non-JSON types.
    """
    return _normalize(obj, "")


def canonical_json(obj: Any) -> str:
    """Canonical JSON text per ``00-overview.md`` (sorted keys, no spaces, UTF-8 kept)."""
    text = json.dumps(
        to_canonical_data(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalJsonError(
            f"string contains a lone surrogate ({exc.object[exc.start:exc.end]!r}); "
            "hashed payloads must be valid Unicode"
        ) from exc
    return text


def sha256_hex(obj: Any) -> str:
    """``sha256(canonical_json(obj).encode("utf-8"))`` as lowercase hex."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


# ──────────────────────────────────── ids ──────────────────────────────────

EVIDENCE_ID_PATTERN: Final = r"^ev_[0-9A-Za-z_-]{1,64}$"
BLOCK_ID_PATTERN: Final = r"^blk_[0-9A-Za-z_-]{1,64}$"
_EVIDENCE_ID_RE: Final = re.compile(EVIDENCE_ID_PATTERN)
_BLOCK_ID_RE: Final = re.compile(BLOCK_ID_PATTERN)

EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID_PATTERN)]
BlockId = Annotated[str, Field(pattern=BLOCK_ID_PATTERN)]


def uuid7() -> uuid.UUID:
    """Time-ordered UUIDv7 (RFC 9562). Uses :func:`uuid.uuid7` when available (3.14+)."""
    native = getattr(uuid, "uuid7", None)
    if native is not None:
        value: uuid.UUID = native()
        return value
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    bits = ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=bits)


def new_evidence_id() -> str:
    """New evidence id ``ev_<32 hex>`` (time-sortable)."""
    return f"ev_{uuid7().hex}"


def new_block_id() -> str:
    """New render-block id ``blk_<32 hex>`` (time-sortable)."""
    return f"blk_{uuid7().hex}"


def stable_block_id(*parts: str | int) -> str:
    """Deterministic block id from identifying parts, e.g. ``("case-1", "kpi_strip", "final")``.

    Lets a node re-emit (``replace: true``) the same block on retries/re-runs.
    """
    if not parts:
        raise ValueError("stable_block_id needs at least one part")
    return f"blk_{sha256_hex([str(p) for p in parts])[:32]}"


def new_run_id() -> str:
    """New run id: a canonical UUIDv7 string (also used as the ADK session id)."""
    return str(uuid7())


def is_evidence_id(value: str) -> bool:
    return isinstance(value, str) and bool(_EVIDENCE_ID_RE.match(value))


def is_block_id(value: str) -> bool:
    return isinstance(value, str) and bool(_BLOCK_ID_RE.match(value))
