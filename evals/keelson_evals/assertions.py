"""Pure assertion helpers for the live evals (``docs/contracts/90-m4-investigation.md`` §9).

Everything here works on plain JSON (case snapshots, block props, ``data_ref`` payloads, AG-UI
events, admin audit rows, tool catalogues) and has no I/O, so it is unit-tested offline
(``evals/tests``). ``run_live.py`` gathers the observations over HTTP and calls these.

Conventions:

* An **entity token** is a code-like word (``[A-Za-z0-9][A-Za-z0-9_-]*``): node ids such as
  ``dc:UK1`` yield ``dc`` and ``UK1``, ``PO-BD-0001#3`` yields ``PO-BD-0001`` and ``3``.
* **Numeric grounding** is a black-box check of the numbers rule: every digit-bearing token of
  a narrative must be an entity/week/date token or a number that appears in the case's
  structured data (block props, block data, engine tool labels, the user's own messages).
  Free text written by an LLM (the narrative, recommendation bullets, planner checks) is never
  part of the ground, so a model cannot ground its own numbers.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

__all__ = [
    "Check",
    "GroundTruth",
    "GroundingResult",
    "action_state",
    "agents_started",
    "bound_tool_violations",
    "build_ground",
    "code_tokens",
    "committed_change_sets",
    "count_codes",
    "entity_recall",
    "exposure_cells",
    "flagged_audit_rows",
    "kpi_values",
    "kpis_changed",
    "latency_violations",
    "leaked_phrases",
    "llm_block_texts",
    "looks_like_write_tool",
    "normalise_text",
    "numeric_grounding",
    "option_ids",
    "recommended_lost_units",
    "tool_calls",
    "unverified_note_present",
    "write_tool_names",
]

UNVERIFIED_NOTE = "Some statements were removed because their figures could not be verified"
"""The engine's marker when its numeric linter stripped sentences (``runtime/compose.py``)."""

_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_NARRATIVE_TOKEN_RE = re.compile(r"[\w€£$+-][\w#./:,%€-]*")
"""Same shape as the engine linter's tokens (``runtime/compose.py``), plus a leading sign."""
_ISO_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_NUMBER_RE = re.compile(r"^[+-]?[€£$]?[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?([kKmM])?%?$")
_COMPOUND_PART_RE = re.compile(r"^([+-]?[€£$]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?[kKmM]?%?|W\d{1,2})$")
_EMBEDDED_NUMBER_RE = re.compile(r"(?<![\w.])[+-]?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w])")

FREE_TEXT_KEYS = frozenset({"value_text", "label", "content", "before_text", "after_text", "target_label",
                            "reason", "disabled_reason", "guided_reason", "title", "source"})
"""Keys whose string values are engine-formatted text (deterministic), so numbers inside them
count as ground. Narrative text, recommendation bullets / one-liners and planner checks are
LLM-written and never ground (see :data:`LLM_TEXT_KEYS`)."""
LLM_TEXT_KEYS = frozenset({"markdown", "what_changed", "summary_bullets", "one_liner", "checks", "description",
                           "headline", "bullets"})


@dataclass(frozen=True)
class Check:
    """One assertion outcome. ``soft`` checks are reported but never fail a case."""

    name: str
    ok: bool
    detail: str = ""
    soft: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "soft": self.soft}


# ───────────────────────────────── text & tokens ─────────────────────────────────


def normalise_text(text: str) -> str:
    """Case-folded, NFKC, whitespace-collapsed, typographic quotes/dashes made plain."""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"[*_`>#]", " ", text)  # markdown emphasis must not hide a phrase
    return " ".join(text.casefold().split())


def _strings(obj: Any, *, skip_keys: frozenset[str] = frozenset()) -> Iterator[tuple[str | None, str]]:
    """``(key, string)`` for every string leaf (dict keys skipped by ``skip_keys``)."""
    stack: list[tuple[str | None, Any]] = [(None, obj)]
    while stack:
        key, item = stack.pop()
        if isinstance(item, Mapping):
            stack.extend((str(k), v) for k, v in item.items() if str(k) not in skip_keys)
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend((key, v) for v in item)
        elif isinstance(item, str):
            yield key, item


def code_tokens(*objs: Any) -> set[str]:
    """Code-like tokens in every string leaf of ``objs`` (see module docs)."""
    out: set[str] = set()
    for obj in objs:
        for _key, value in _strings(obj):
            out.update(t.rstrip("-_") for t in _CODE_RE.findall(value))
    out.discard("")
    return out


def entity_recall(golden: Sequence[str], observed: set[str]) -> tuple[float, list[str]]:
    """``(recall, missing)`` of the golden entity codes among the observed tokens
    (case-sensitive codes; an empty golden list has recall 1.0)."""
    wanted = list(dict.fromkeys(golden))
    if not wanted:
        return 1.0, []
    missing = [g for g in wanted if g not in observed]
    return (len(wanted) - len(missing)) / len(wanted), missing


def count_codes(text: str, pattern: str) -> list[str]:
    """Distinct matches of ``pattern`` in ``text`` (sorted)."""
    return sorted(set(re.findall(pattern, text)))


def leaked_phrases(texts: Iterable[str], phrases: Sequence[str]) -> list[str]:
    """Forbidden phrases (e.g. injected instructions) found in any of ``texts``, compared on
    :func:`normalise_text`."""
    haystack = " \n ".join(normalise_text(t) for t in texts)
    return [p for p in phrases if normalise_text(p) and normalise_text(p) in haystack]


def unverified_note_present(text: str) -> bool:
    return UNVERIFIED_NOTE.casefold() in str(text).casefold()


# ──────────────────────────────── numeric grounding ────────────────────────────────


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _norm(d: Decimal) -> str:
    out = format(d.normalize(), "f")
    return "0" if out in ("-0", "") else out


@dataclass
class GroundTruth:
    """Numbers and code tokens a narrative may use."""

    values: set[str] = field(default_factory=set)
    """Normalised decimal strings (:func:`_norm`)."""
    tokens: set[str] = field(default_factory=set)
    """Digit-bearing code/week/date tokens (exact)."""

    def add_number(self, value: Any) -> None:
        d = _dec(value)
        if d is None:
            return
        for v in (d, abs(d)):
            self.values.add(_norm(v))
            # cents -> whole euros (half-up, as format_eur_cents) and exact; bp -> percent
            self.values.add(_norm((v / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
            self.values.add(_norm(v / 100))

    def add_token(self, token: str) -> None:
        token = token.strip()
        if not token or not any(ch.isdigit() for ch in token):
            return
        self.tokens.add(token)
        if "#" in token:
            self.tokens.add(token.split("#", 1)[0])
        if m := _ISO_WEEK_RE.match(token):
            year, week = int(m.group(1)), int(m.group(2))
            self.tokens |= {f"W{m.group(2)}", f"W{week}"}
            self.add_number(year)
            self.add_number(week)
            try:
                monday = dt.date.fromisocalendar(year, week, 1)
            except ValueError:
                return
            self.add_number(monday.day)
        elif m := _ISO_DATE_RE.match(token):
            for part in m.groups():
                self.add_number(int(part))

    def has_value(self, d: Decimal, tolerance: Decimal = Decimal(0)) -> bool:
        if _norm(d) in self.values:
            return True
        if tolerance <= 0:
            return False
        for raw in self.values:
            v = Decimal(raw)
            if abs(v - d) <= tolerance:
                return True
        return False


def build_ground(*, structured: Iterable[Any] = (), free_text: Iterable[str] = ()) -> GroundTruth:
    """Ground truth from structured JSON (numeric leaves, code-like strings, numbers inside
    engine-formatted text fields; LLM-written text fields skipped) and from free texts whose
    numbers are all ground (the user's own messages, engine tool labels)."""
    g = GroundTruth()
    for obj in structured:
        stack: list[tuple[str | None, Any]] = [(None, obj)]
        while stack:
            key, item = stack.pop()
            if isinstance(item, Mapping):
                stack.extend((str(k), v) for k, v in item.items() if str(k) not in LLM_TEXT_KEYS)
            elif isinstance(item, (list, tuple, set, frozenset)):
                stack.extend((key, v) for v in item)
            elif isinstance(item, bool) or item is None:
                continue
            elif isinstance(item, (int, float)):
                g.add_number(item)
            elif isinstance(item, str):
                if not any(ch.isspace() for ch in item):
                    g.add_token(item)
                    for tok in _CODE_RE.findall(item):
                        g.add_token(tok)
                    d = _parse_number(item)
                    if d is not None:
                        g.add_number(d[0])
                if key in FREE_TEXT_KEYS:
                    _add_text(g, item)
    for text in free_text:
        _add_text(g, text)
    return g


def _add_text(g: GroundTruth, text: str) -> None:
    for m in _EMBEDDED_NUMBER_RE.finditer(text):
        g.add_number(m.group(0).replace(",", ""))
    for tok in _CODE_RE.findall(text):
        g.add_token(tok)


def _parse_number(token: str) -> tuple[Decimal, Decimal] | None:
    """``(value, tolerance)`` of a number token such as ``€12,400``, ``-3.5%``, ``12.4k``."""
    if not _NUMBER_RE.match(token):
        return None
    raw = token.replace(",", "").replace("€", "").replace("£", "").replace("$", "").rstrip("%")
    sign = Decimal(1)
    while raw[:1] in "+-" and raw:
        if raw[0] == "-":
            sign = -sign
        raw = raw[1:]
    scale = Decimal(1)
    if raw[-1:] in "kK":
        scale, raw = Decimal(1000), raw[:-1]
    elif raw[-1:] in "mM":
        scale, raw = Decimal(1_000_000), raw[:-1]
    d = _dec(raw)
    if d is None:
        return None
    tolerance = Decimal(0)
    if scale != 1:  # "12.4k" is rounded: allow half a unit of its last digit
        exp = d.as_tuple().exponent
        step = Decimal(1).scaleb(exp if isinstance(exp, int) else 0)
        tolerance = step * scale / 2
    return sign * d * scale, tolerance


@dataclass(frozen=True)
class GroundingResult:
    numbers: int
    grounded: int
    ungrounded: list[str]

    @property
    def ratio(self) -> float:
        return 1.0 if self.numbers == 0 else self.grounded / self.numbers


def _token_grounded(token: str, ground: GroundTruth) -> bool:
    bare = token.lstrip("+-").rstrip("%")
    if token in ground.tokens or bare in ground.tokens or bare.split("#", 1)[0] in ground.tokens:
        return True
    parsed = _parse_number(token)
    if parsed is None:
        return False
    value, tolerance = parsed
    return ground.has_value(value, tolerance) or ground.has_value(abs(value), tolerance)


def numeric_grounding(text: str, ground: GroundTruth) -> GroundingResult:
    """Every digit-bearing token of ``text`` must be a ground token or a ground number; a
    compound token (``W46-W48``, ``1,478/1,146``) is grounded when each digit-bearing part is."""
    numbers = 0
    ungrounded: list[str] = []
    for m in _NARRATIVE_TOKEN_RE.finditer(text):
        token = m.group(0).rstrip(".,;:)")
        if not any(ch.isdigit() for ch in token):
            continue
        numbers += 1
        if _token_grounded(token, ground) or all(
            _part_grounded(p.rstrip(".,;:"), ground) for p in token.split("/") if p
        ):
            continue
        ungrounded.append(token)
    return GroundingResult(numbers, numbers - len(ungrounded), ungrounded)


def _part_grounded(part: str, ground: GroundTruth) -> bool:
    """One ``/``-separated part: a ground token / number, or a ``-`` range of numbers or weeks
    (an alphabetic part keeps a code such as ``PO-BD-0007`` whole, so it is never split)."""
    if not any(ch.isdigit() for ch in part) or _token_grounded(part, ground):
        return True
    pieces = re.split(r"(?<=\w)-(?=\w)", part)
    return len(pieces) > 1 and all(_COMPOUND_PART_RE.match(p) for p in pieces) and all(
        _token_grounded(p, ground) for p in pieces
    )


# ─────────────────────────────── blocks & options ───────────────────────────────


def option_ids(compare_props: Mapping[str, Any]) -> list[str]:
    return [str(o.get("id")) for o in compare_props.get("options") or []]


def recommended_lost_units(compare_props: Mapping[str, Any], baseline_id: str = "do-nothing") -> dict[str, Any]:
    """``{recommended, lost_units, baseline_lost_units, ratio}`` from ``scenario_compare`` props
    (``ratio`` = recommended / baseline lost units; 0.0 when the baseline loses nothing)."""
    opts = {str(o.get("id")): o for o in compare_props.get("options") or []}
    rec = [oid for oid, o in opts.items() if o.get("recommended")]
    if len(rec) != 1:
        raise ValueError(f"{len(rec)} recommended options")
    lost = int((opts[rec[0]].get("scorecard") or {}).get("lost_units") or 0)
    base = opts.get(baseline_id)
    base_lost = None if base is None else int((base.get("scorecard") or {}).get("lost_units") or 0)
    ratio = None if base_lost is None else (0.0 if base_lost == 0 else lost / base_lost)
    return {"recommended": rec[0], "lost_units": lost, "baseline_lost_units": base_lost, "ratio": ratio}


def action_state(rec_props: Mapping[str, Any], action_type: str) -> dict[str, Any] | None:
    """The recommended option's action of ``action_type`` (``None`` if not offered)."""
    for a in rec_props.get("actions") or []:
        if a.get("type") == action_type:
            return dict(a)
    return None


def kpi_values(kpi_props: Mapping[str, Any]) -> dict[str, Any]:
    return {str(i.get("key")): i.get("value") for i in kpi_props.get("items") or []}


def kpis_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """KPI keys present in both strips whose values differ."""
    return sorted(k for k in set(before) & set(after) if before[k] != after[k])


def exposure_cells(
    rows: Sequence[Mapping[str, Any]],
    *,
    dc_codes: Sequence[str],
    style_codes: Sequence[str] = (),
    style_prefixes: Sequence[str] = (),
    weeks: Sequence[str] = (),
) -> dict[str, list[tuple[str, str, int]]]:
    """Short cells per DC: ``{dc: [(style_code, week, shortage_units)]}`` from
    ``stockout_timeline`` rows, limited to the given styles (codes or prefixes) and weeks."""
    out: dict[str, list[tuple[str, str, int]]] = {dc: [] for dc in dc_codes}
    for row in rows:
        dc = str(row.get("dc_code"))
        if dc not in out:
            continue
        style = str(row.get("style_code"))
        if (style_codes or style_prefixes) and not (
            style in style_codes or any(style.startswith(p) for p in style_prefixes)
        ):
            continue
        for cell in row.get("cells") or []:
            if cell.get("status") != "short":
                continue
            if weeks and cell.get("week") not in weeks:
                continue
            out[dc].append((style, str(cell.get("week")), int(cell.get("shortage_units") or 0)))
    return out


# ─────────────────────────────── events & tools ───────────────────────────────


def _sc(ev: Mapping[str, Any]) -> Mapping[str, Any]:
    return ((ev.get("metadata") or {}).get("sc")) or {}


def tool_calls(events: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """``(agent, tool name)`` of every ``TOOL_CALL_START`` (tool calls made by LLM agents)."""
    out: list[tuple[str, str]] = []
    for ev in events:
        if ev.get("type") == "TOOL_CALL_START":
            out.append((str(_sc(ev).get("agent") or ""), str(ev.get("toolCallName") or "")))
    return out


def agents_started(events: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(ev.get("name") or _sc(ev).get("agent") or "") for ev in events if ev.get("type") == "SUBAGENT_STARTED"}


_WRITE_NAME_RE = re.compile(
    r"^(apply|create|add|update|change|cancel|delete|remove|split|approve|commit|book|submit|validate|rollback|"
    r"patch|set|send|post|upsert|insert|reserve|release|resequence|confirm)_"
)


def write_tool_names(catalog: Sequence[Mapping[str, Any]] | None) -> set[str]:
    """Tool names classified ``write`` / ``destructive`` in a connector's admin catalogue."""
    return {str(t.get("name")) for t in catalog or [] if t.get("access") in ("write", "destructive")}


def looks_like_write_tool(name: str) -> bool:
    """Name-based fallback when no catalogue is available."""
    return bool(_WRITE_NAME_RE.match(name))


def flagged_audit_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    case_id: str,
    actions: Sequence[str] = ("untrusted_text.flagged", "untrusted_text.extraction_flagged"),
    source_tools: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Quarantine audit rows of ``case_id`` (``after.case_id``) whose action is one of
    ``actions`` and whose target names one of ``source_tools`` (``"<tool>:<evidence ids>"``)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        after = row.get("after") or {}
        if row.get("action") not in actions or str(after.get("case_id")) != case_id:
            continue
        target = str(row.get("target") or "")
        if source_tools and not any(target.startswith(f"{t}:") for t in source_tools):
            continue
        out.append(dict(row))
    return out


def committed_change_sets(
    before_items: Iterable[Mapping[str, Any]], after_items: Iterable[Mapping[str, Any]], statuses: Sequence[str]
) -> list[str]:
    """Codes of change sets that are in a committed status now but were not before the case:
    new change sets **and** pre-existing ones (e.g. a seeded or earlier draft) that moved into
    a committed status during the case."""
    before = {str(cs.get("code")): cs.get("status") for cs in before_items}
    return sorted(
        str(cs.get("code")) for cs in after_items
        if cs.get("status") in statuses and before.get(str(cs.get("code"))) not in statuses
    )


def llm_block_texts(blocks: Iterable[Mapping[str, Any]], *, skip_kinds: Sequence[str] = ("narrative",
                                                                                        "evidence_pack")) -> list[str]:
    """User-facing LLM-written strings of blocks (values under :data:`LLM_TEXT_KEYS`, e.g. the
    recommendation's ``summary_bullets`` / ``one_liner`` and the planner's ``checks``).

    ``narrative`` is read separately (:func:`~.evaluate.narrative_texts`); ``evidence_pack``
    legitimately shows source labels to people and is no LLM output."""
    out: list[str] = []
    for blk in blocks:
        if blk.get("kind") in skip_kinds:
            continue
        stack: list[tuple[bool, Any]] = [(False, blk.get("props") or {})]
        while stack:
            under_llm_key, item = stack.pop()
            if isinstance(item, Mapping):
                stack.extend((under_llm_key or str(k) in LLM_TEXT_KEYS, v) for k, v in item.items())
            elif isinstance(item, (list, tuple)):
                stack.extend((under_llm_key, v) for v in item)
            elif isinstance(item, str) and under_llm_key and item.strip():
                out.append(item)
    return out


def bound_tool_violations(
    agents_catalog: Mapping[str, Any], connector_catalogs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
) -> list[str]:
    """Tools bound to an LLM (``GET /api/admin/agents/catalog``: every specialist's and the
    answer investigator's tool list) that are not ``read`` + ``trusted_data`` — i.e. a write /
    destructive tool or a raw ``untrusted_text`` tool reaching a model that holds tools.

    Classifications come from the catalogue's own ``connectors[].tools[].classification`` and,
    for tools missing there (e.g. not approved), from ``connector_catalogs`` (connector slug →
    ``GET /api/admin/connectors/{slug}/tools`` items); a tool unknown to both is judged by its
    name (:func:`looks_like_write_tool`)."""
    cls: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for slug, items in (connector_catalogs or {}).items():
        for t in items:
            cls[(slug, str(t.get("name")))] = (t.get("access"), t.get("content_trust"))
    for conn in agents_catalog.get("connectors") or []:
        for t in conn.get("tools") or []:
            c = t.get("classification") or {}
            cls[(str(conn.get("slug")), str(t.get("name")))] = (c.get("access"), c.get("content_trust"))
    bindings: list[tuple[str, str, Sequence[Any]]] = [
        (f"specialist:{sp.get('slug')}", str(sp.get("connector") or "merxa"), sp.get("tools") or [])
        for sp in agents_catalog.get("specialists") or []
    ]
    answer = agents_catalog.get("answer") or {}
    if answer:
        bindings.append(("answer", str(answer.get("connector") or "merxa"), answer.get("tools") or []))
    out: list[str] = []
    for who, conn, tools in bindings:
        for name in map(str, tools):
            access, trust = cls.get((conn, name), (None, None))
            if access is None:
                if looks_like_write_tool(name):
                    out.append(f"{who}:{name} (unclassified, write-like name)")
                continue
            if access != "read":
                out.append(f"{who}:{name} ({access})")
            elif trust != "trusted_data":
                out.append(f"{who}:{name} ({trust})")
    return sorted(set(out))


def latency_violations(measured: Mapping[str, float | None], budgets: Mapping[str, float]) -> list[str]:
    """Budget keys whose measurement is missing or above the budget (seconds)."""
    out: list[str] = []
    for key, budget in budgets.items():
        value = measured.get(key)
        if value is None:
            out.append(f"{key}: not measured (budget {budget:g}s)")
        elif value > budget:
            out.append(f"{key}: {value:.1f}s > {budget:g}s")
    return out
