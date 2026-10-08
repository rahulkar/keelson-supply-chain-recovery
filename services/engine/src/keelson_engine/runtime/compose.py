"""Numbers rule (``50-engine-workflow.md``): LLM narrative carries no free numbers.

Placeholder grammar::

    {{kpi:<option_id>.<key>}}      a plan option KPI (also {{kpi:<key>}} = recommended option)
    {{ev:<evidence_id>#<json_pointer>}}   a value inside a stored tool result
    {{calc:<calc_id>}}             a deterministic calc result

:func:`fill_placeholders` resolves them with unit-aware formatting (:func:`format_value`);
:func:`numeric_lint` checks that every remaining digit sequence belongs to an allowed token
(entity codes, ISO weeks/dates present in evidence or the scenario). ``compose`` gives the
recommender one repair turn; whatever still violates is stripped and marked unverified
(:func:`strip_unverified`).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from keelson_contracts.common import format_eur_cents

from .jsonptr import JsonPointerError, resolve_pointer

__all__ = [
    "ComposeContext",
    "KpiValue",
    "Unit",
    "CO2_TOKENS",
    "UNVERIFIED_NOTE",
    "allowed_tokens_from",
    "fill_placeholders",
    "format_value",
    "numeric_lint",
    "split_sentences",
    "strip_unverified",
    "check_text",
]

Unit = Literal["units", "eur_cents", "days", "pct_bp", "count", "kg_co2e", "date", "week", "number", "text"]

PLACEHOLDER_RE = re.compile(r"\{\{\s*(kpi|ev|calc)\s*:\s*([^{}]+?)\s*\}\}")
_ANY_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")
"""Any ``{{…}}`` left after resolution (unknown kind or key) is a violation, digits or not."""
_TOKEN_RE = re.compile(r"[\w€£$][\w#./:,%€-]*")
"""Word-like tokens; the word class is Unicode-aware, so non-ASCII digits (``１２``, ``١٢``) are linted too."""
_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNVERIFIED_NOTE = "_Some statements were removed because their figures could not be verified._"
CO2_TOKENS = frozenset({"CO2", "CO₂", "CO2e", "CO₂e", "tCO2e", "tCO₂e", "kgCO2e", "kgCO₂e"})
"""M8 (126): the names of CO₂ are words, not free numbers (the subscript two is a numeric character)."""


@dataclass(frozen=True)
class KpiValue:
    value: int
    unit: Unit


@dataclass
class ComposeContext:
    kpis: dict[str, dict[str, KpiValue]] = field(default_factory=dict)
    """option_id → key → value."""
    default_option: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    """evidence_id → stored result JSON."""
    calcs: dict[str, int] = field(default_factory=dict)


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def format_value(value: Any, unit: Unit) -> str:
    """Unit-aware formatting: ``eur_cents`` → "€12,400", ``units`` → "3,200 units", ...."""
    if unit == "text":
        return str(value)
    if unit == "date":
        day = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
        return f"{day.day} {day.strftime('%b')} {day.year}"
    if unit == "week":
        m = _ISO_WEEK_RE.match(str(value))
        if not m:
            raise ValueError(f"not an ISO week: {value!r}")
        monday = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        return f"W{m.group(2)} (w/c {monday.day} {monday.strftime('%b')})"
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{unit} values must be integers, got {value!r}")
    if unit == "eur_cents":
        return format_eur_cents(value)
    if unit == "units":
        return f"{_fmt_int(value)} unit" if abs(value) == 1 else f"{_fmt_int(value)} units"
    if unit == "days":
        return f"{value} day" if abs(value) == 1 else f"{_fmt_int(value)} days"
    if unit == "pct_bp":
        whole, rem = divmod(abs(value), 100)
        sign = "-" if value < 0 else ""
        return f"{sign}{whole}%" if rem == 0 else f"{sign}{whole}.{rem:02d}".rstrip("0") + "%"
    if unit == "kg_co2e":  # M8 (126): kilograms of CO2-equivalent
        return f"{_fmt_int(value)} kg CO₂e"
    return _fmt_int(value)


def _infer_unit(value: Any) -> Unit:
    if isinstance(value, str):
        if _ISO_DATE_RE.match(value):
            return "date"
        if _ISO_WEEK_RE.match(value):
            return "week"
        return "text"
    return "number"


def _resolve(kind: str, ref: str, ctx: ComposeContext) -> str | None:
    if kind == "kpi":
        option, _, key = ref.rpartition(".")
        option = option or (ctx.default_option or "")
        kpi = ctx.kpis.get(option, {}).get(key)
        return None if kpi is None else format_value(kpi.value, kpi.unit)
    if kind == "calc":
        value = ctx.calcs.get(ref)
        return None if value is None else format_value(value, "number")
    evidence_id, sep, pointer = ref.partition("#")
    if not sep or evidence_id not in ctx.evidence:
        return None
    try:
        value = resolve_pointer(ctx.evidence[evidence_id], pointer)
    except JsonPointerError:
        return None
    if isinstance(value, (dict, list)) or value is None or isinstance(value, float):
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return format_value(value, _infer_unit(value))


def fill_placeholders(text: str, ctx: ComposeContext) -> tuple[str, list[str]]:
    """Replace every resolvable placeholder; unknown ones stay in the text and are reported."""
    violations: list[str] = []

    def sub(m: re.Match[str]) -> str:
        resolved = _resolve(m.group(1), m.group(2), ctx)
        if resolved is None:
            violations.append(f"unknown placeholder {m.group(0)}")
            return m.group(0)
        return resolved

    return _dedupe_units(PLACEHOLDER_RE.sub(sub, text)), violations


_DUP_UNITS = (
    re.compile(r"\b(units?)\s+units?\b", re.I),
    re.compile(r"\b(days?)\s+days?\b", re.I),
)


def _dedupe_units(text: str) -> str:
    """Placeholders render with their unit; drop a unit word the model repeated after one
    (``"{{kpi:x.units_recovered}} units"`` -> ``"280 units"``, not ``"280 units units"``)."""
    for pattern in _DUP_UNITS:
        text = pattern.sub(lambda m: m.group(1), text)
    return text


def _week_aliases(token: str) -> set[str]:
    m = _ISO_WEEK_RE.match(token)
    return {f"W{m.group(2)}", f"W{int(m.group(2))}"} if m else set()


def allowed_tokens_from(values: Iterable[Any]) -> set[str]:
    """Collect allowed digit-bearing tokens (codes, ISO weeks/dates) from nested JSON values."""
    allowed: set[str] = set()
    stack = list(values)
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set)):
            stack.extend(item)
        elif isinstance(item, str) and any(ch.isdigit() for ch in item) and len(item) <= 64:
            allowed.add(item)
            allowed |= _week_aliases(item)
            if "#" in item:  # "PO-BD-0001#3" also allows the PO code itself
                allowed.add(item.split("#", 1)[0])
    return allowed


def numeric_lint(text: str, allowed_tokens: set[str]) -> list[str]:
    """Digit sequences outside placeholders that are not part of an allowed token."""
    stripped = PLACEHOLDER_RE.sub(" ", text)
    violations: list[str] = [f"unknown placeholder {m.group(0)}" for m in _ANY_PLACEHOLDER_RE.finditer(stripped)]
    stripped = _ANY_PLACEHOLDER_RE.sub(" ", stripped)
    for m in _TOKEN_RE.finditer(stripped):
        token = m.group(0).rstrip(".,;:%")
        if not any(ch.isnumeric() for ch in token):
            continue
        if token in allowed_tokens or token in CO2_TOKENS or token.split("#", 1)[0] in allowed_tokens:
            continue
        violations.append(f"free number {token!r}")
    return violations


def check_text(raw: str, ctx: ComposeContext, allowed_tokens: set[str]) -> tuple[str, list[str]]:
    """Lint the raw LLM text, then fill placeholders. Returns (filled, violations)."""
    violations = numeric_lint(raw, allowed_tokens)
    filled, unknown = fill_placeholders(raw, ctx)
    return filled, violations + unknown


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9_*{(\[])")


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_RE.split(text.strip()) if s.strip()]


def strip_unverified(raw: str, ctx: ComposeContext, allowed_tokens: set[str]) -> tuple[str, int]:
    """Drop every sentence that still violates the numbers rule; returns (filled text, dropped)."""
    kept: list[str] = []
    dropped = 0
    for sentence in split_sentences(raw):
        filled, violations = check_text(sentence, ctx, allowed_tokens)
        if violations:
            dropped += 1
            continue
        kept.append(filled)
    return " ".join(kept), dropped
