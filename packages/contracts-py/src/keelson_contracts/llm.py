"""LLM output schemas (``50-engine-workflow.md``, ``90-m4-investigation.md``) and a strict-schema checker.

Every model here is used as an ADK ``output_schema`` and must be accepted by
OpenAI-style strict structured outputs (which ADK's LiteLLM adapter enables via
``_enforce_strict_openai_schema``):

* no ``Any`` and no free-form dicts (``additionalProperties`` is always ``false``);
* every property is required — nullable fields are *required and nullable*
  (``x: int | None`` with no default);
* enumerations are ``Literal`` (rendered as JSON ``enum``);
* no defaults and no validation keywords the provider may ignore (patterns,
  min/max) — semantic checks happen in deterministic engine code instead
  (:func:`plan_violations`, :meth:`ScenarioUpdate.shape_error`,
  :func:`untrusted_extraction_violations`).

:func:`strict_schema_violations` verifies those rules on any Pydantic model.

M4 (``90``) adds the planner (:class:`InvestigationPlan`), critic (:class:`Critique`),
follow-up classifier (:class:`FollowupClassification`), prompt-injection guard
(:class:`GuardVerdict`) and quarantined extractor (:class:`UntrustedExtraction`)
outputs, ``Findings.scenario_updates`` and the taxonomy enum for
``DisruptionExtract.event_type`` (:data:`~keelson_contracts.common.EVENT_TYPE_KEYS`).

M6 (``100`` §3) adds :class:`NewsExtraction`, the Signal Hub's quarantined news extractor output
(checked with :func:`news_extraction_violations`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, model_validator

from .changeset import PO_LINE_REF_PATTERN
from .common import (
    EVENT_TYPE_KEYS,
    EntityRef,
    EntityRefType,
    EventFamily,
    EventTypeKey,
    event_family_of,
    is_evidence_id,
)
from .optimizer import LeverCode
from .signals import SignalKind

__all__ = [
    "Claim",
    "ConstraintDelta",
    "Critique",
    "CritiqueIssue",
    "DisruptionExtract",
    "EVENT_TYPE_KEYS",
    "EntityMention",
    "EntityMentionType",
    "EntityRef",
    "EntityRefType",
    "EventTypeKey",
    "FOLLOWUP_KINDS",
    "Findings",
    "FollowupClassification",
    "FollowupKind",
    "GuardVerdict",
    "IntakeResult",
    "InvestigationPlan",
    "InvestigationTask",
    "LLM_OUTPUT_MODELS",
    "LlmModel",
    "MAX_MENTION_CHARS",
    "MAX_NEWS_MENTIONS",
    "NewsExtraction",
    "news_extraction_violations",
    "MAX_OBJECTIVE_CHARS",
    "MAX_PLAN_PARALLEL",
    "MAX_PLAN_TASKS",
    "MAX_QUOTE_CHARS",
    "OptionReason",
    "PlanBudget",
    "Recommendation",
    "SCENARIO_UPDATE_ID_KEYS",
    "SCENARIO_UPDATE_INT_KEYS",
    "SCENARIO_UPDATE_KEYS",
    "ScenarioUpdate",
    "ScenarioUpdateKey",
    "UNTRUSTED_ITEM_KINDS",
    "UntrustedExtraction",
    "UntrustedItem",
    "UntrustedItemKind",
    "constraint_delta_violations",
    "critique_violations",
    "plan_violations",
    "plan_waves",
    "strict_schema_violations",
    "untrusted_extraction_violations",
    "usable_untrusted_items",
]


class LlmModel(BaseModel):
    """Base for LLM output schemas: closed objects, strict-compatible."""

    model_config = ConfigDict(extra="forbid")


EntityMentionType = Literal[
    "supplier",
    "po",
    "sku",
    "style",
    "dc",
    "store",
    "factory",
    "component",
    "port",
    "lane",
    "chokepoint",
    "region",
    "other",
]
"""What an entity mention names (``chokepoint``/``region`` added in M4; resolved
deterministically against ``data/geo`` and the connector)."""


class EntityMention(LlmModel):
    """An entity named in user/supplier text, before deterministic resolution."""

    type: EntityMentionType
    text: str


class DisruptionExtract(LlmModel):
    """Structured reading of a disruption; user-stated numbers become ``decision`` values.

    ``event_type`` is a taxonomy key (:data:`~keelson_contracts.common.EVENT_TYPE_KEYS`,
    ``data/taxonomy/events.yaml``); ``OTHER`` makes the case investigation-only.
    """

    event_family: EventFamily
    event_type: EventTypeKey
    description: str
    entity_mentions: list[EntityMention]
    delay_days: int | None
    demand_change_pct: int | None
    start_date: str | None
    duration_days: int | None
    is_hypothetical: bool

    @property
    def taxonomy_family(self) -> EventFamily:
        """The family implied by ``event_type`` (authoritative over ``event_family``)."""
        return event_family_of(self.event_type)


class IntakeResult(LlmModel):
    """Output of the ``intake`` agent; routing is deterministic on ``kind``."""

    kind: Literal["QUESTION", "NEW_DISRUPTION", "FOLLOWUP", "SMALLTALK"]
    summary: str
    disruption: DisruptionExtract | None


class Claim(LlmModel):
    """A statement with pointers to evidence or a deterministic calc."""

    text: str
    kind: Literal["fact", "inference"]
    evidence_id: str | None
    json_pointer: str | None
    calc_id: str | None
    confidence: Literal["high", "medium", "low"]

    @property
    def is_cited(self) -> bool:
        """True if the claim points at evidence or a calc (facts must be cited)."""
        return self.evidence_id is not None or self.calc_id is not None


ScenarioUpdateKey = Literal[
    "delay_days", "duration_days", "demand_change_pct", "add_receipts", "remove_receipts"
]
"""Scenario values a specialist may propose to change (``90`` §5, ``refine_scenario``)."""

SCENARIO_UPDATE_KEYS: Final[tuple[str, ...]] = get_args(ScenarioUpdateKey)
SCENARIO_UPDATE_INT_KEYS: Final = frozenset({"delay_days", "duration_days", "demand_change_pct"})
"""Keys carried in ``value_int`` (``value_ids`` must be null)."""
SCENARIO_UPDATE_ID_KEYS: Final = frozenset({"add_receipts", "remove_receipts"})
"""Keys carried in ``value_ids`` (receipt ids ``<po_code>#<line_no>``; ``value_int`` must be null)."""
_SCENARIO_UPDATE_INT_MIN: Final[Mapping[str, int]] = {
    "delay_days": 0,
    "duration_days": 1,
    "demand_change_pct": -100,
}
_PO_LINE_REF_RE: Final = re.compile(PO_LINE_REF_PATTERN)


class ScenarioUpdate(LlmModel):
    """A proposed scenario change backed by evidence (``Findings.scenario_updates``).

    ``refine_scenario`` applies fact-backed updates and turns inference-backed ones into
    ``Assumption(unconfirmed=True)`` entries. The value shape is checked deterministically
    with :meth:`shape_error` (a malformed update is dropped, not the whole ``Findings``).
    """

    key: ScenarioUpdateKey
    value_int: int | None
    value_ids: list[str] | None
    evidence_id: str
    json_pointer: str

    def shape_error(self) -> str | None:
        """Why the value does not fit ``key`` (``None`` = well formed).

        Checks the value shape per key, value bounds (``delay_days >= 0``,
        ``duration_days >= 1``, ``demand_change_pct >= -100``), receipt ids
        (``<po_code>#<line_no>``), the evidence id format (``ev_…``) and the JSON pointer.

        A well-formed update is not yet *fact-backed*: ``refine_scenario`` must still
        resolve ``evidence_id`` + ``json_pointer`` and treat the update as ``inference``
        unless that evidence is a trusted (non-``untrusted_text``) read whose value at the
        pointer supports ``value_int`` / ``value_ids``.
        """
        if self.key in SCENARIO_UPDATE_INT_KEYS:
            if self.value_int is None or self.value_ids is not None:
                return f"{self.key} needs value_int (and value_ids null)"
            low = _SCENARIO_UPDATE_INT_MIN[self.key]
            if self.value_int < low:
                return f"{self.key} must be >= {low} (got {self.value_int})"
        elif not self.value_ids or self.value_int is not None:
            return f"{self.key} needs a non-empty value_ids (and value_int null)"
        elif any(not i.strip() for i in self.value_ids) or len(set(self.value_ids)) != len(self.value_ids):
            return f"{self.key} value_ids must be unique and non-empty"
        elif bad := [i for i in self.value_ids if not _PO_LINE_REF_RE.match(i)]:
            return f"{self.key} value_ids must be receipt ids <po_code>#<line_no> (got {bad[:3]})"
        if not self.evidence_id.strip():
            return "evidence_id is required"
        if not is_evidence_id(self.evidence_id):
            return f"evidence_id must be an evidence id ev_… (got {self.evidence_id[:80]!r})"
        if not self.json_pointer.startswith("/") and self.json_pointer != "":
            return "json_pointer must be an RFC 6901 pointer"
        return None


class Findings(LlmModel):
    """Structured output of a specialist (investigator → structurer).

    ``scenario_updates`` (M4) is required in the schema (strict mode); payloads stored
    before M4 that lack the key still validate (it is read as ``[]``).
    """

    task_id: str
    status: Literal["ok", "failed"]
    claims: list[Claim]
    affected: list[EntityRef]
    gaps: list[str]
    scenario_updates: list[ScenarioUpdate]

    @model_validator(mode="before")
    @classmethod
    def _pre_m4_payloads(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "scenario_updates" not in data:
            return {**data, "scenario_updates": []}
        return data


class OptionReason(LlmModel):
    """Why an option was not recommended."""

    option_id: str
    reason: str


class Recommendation(LlmModel):
    """Output of the ``recommender`` (Strategist). Numbers only via placeholders."""

    headline: str
    bullets: list[str]
    recommended_option_id: str
    why_not_others: list[OptionReason]
    risks: list[str]
    follow_ups: list[str]


# ─────────────────────────────── planner (90 §2) ─────────────────────────────

MAX_PLAN_PARALLEL: Final = 4
MAX_PLAN_TASKS: Final = 6
MAX_OBJECTIVE_CHARS: Final = 200


class PlanBudget(LlmModel):
    """Concurrency/size budget of a plan (``max_parallel <= 4``, ``max_tasks <= 6``)."""

    max_parallel: int
    max_tasks: int


class InvestigationTask(LlmModel):
    """One specialist task. ``tools`` must be a subset of that specialist's allowlist."""

    id: str
    specialist: str
    objective: str
    tools: list[str]
    depends_on: list[str]


class InvestigationPlan(LlmModel):
    """Output of the ``planner`` (tool-free). Validate with :func:`plan_violations`."""

    rationale: str
    tasks: list[InvestigationTask]
    budget: PlanBudget


def plan_violations(
    plan: InvestigationPlan, catalogue: Mapping[str, Collection[str]]
) -> list[str]:
    """Every reason ``plan`` is not executable (empty = OK).

    ``catalogue`` maps specialist name → its tool allowlist (from the AgentSpec). Checks:
    budget bounds, task count, unique non-empty ids, objective length, known specialists,
    tools within the allowlist, dependencies on known other tasks and no cycles.
    """
    problems: list[str] = []
    b = plan.budget
    if not 1 <= b.max_parallel <= MAX_PLAN_PARALLEL:
        problems.append(f"budget.max_parallel must be 1..{MAX_PLAN_PARALLEL} (got {b.max_parallel})")
    if not 1 <= b.max_tasks <= MAX_PLAN_TASKS:
        problems.append(f"budget.max_tasks must be 1..{MAX_PLAN_TASKS} (got {b.max_tasks})")
    if not plan.tasks:
        problems.append("plan has no tasks")
    limit = min(max(b.max_tasks, 0), MAX_PLAN_TASKS)
    if len(plan.tasks) > limit:
        problems.append(f"plan has {len(plan.tasks)} tasks; budget allows {limit}")
    ids = [t.id for t in plan.tasks]
    if any(not i.strip() for i in ids):
        problems.append("task ids must be non-empty")
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"duplicate task ids: {dupes}")
    known = set(ids)
    for t in plan.tasks:
        if len(t.objective) > MAX_OBJECTIVE_CHARS:
            problems.append(f"task {t.id}: objective longer than {MAX_OBJECTIVE_CHARS} chars")
        if not t.objective.strip():
            problems.append(f"task {t.id}: empty objective")
        allowed = catalogue.get(t.specialist)
        if allowed is None:
            problems.append(f"task {t.id}: unknown specialist {t.specialist!r}")
        else:
            extra = sorted(set(t.tools) - set(allowed))
            if extra:
                problems.append(f"task {t.id}: tools not allowed for {t.specialist}: {extra}")
        if t.id in t.depends_on:
            problems.append(f"task {t.id}: depends on itself")
        unknown = sorted(set(t.depends_on) - known)
        if unknown:
            problems.append(f"task {t.id}: depends on unknown tasks {unknown}")
    if not problems:
        try:
            plan_waves(plan)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


def plan_waves(plan: InvestigationPlan) -> list[list[str]]:
    """Task ids grouped in dependency waves (each wave depends only on earlier ones).

    Order inside a wave follows the plan. Raises ``ValueError`` on unknown dependencies or
    a cycle. The engine runs each wave with at most ``budget.max_parallel`` tasks at once.
    """
    deps = {t.id: set(t.depends_on) for t in plan.tasks}
    for tid, d in deps.items():
        if unknown := d - deps.keys():
            raise ValueError(f"task {tid}: depends on unknown tasks {sorted(unknown)}")
    done: set[str] = set()
    waves: list[list[str]] = []
    remaining = [t.id for t in plan.tasks]
    while remaining:
        wave = [tid for tid in remaining if deps[tid] <= done]
        if not wave:
            raise ValueError(f"dependency cycle among tasks {sorted(remaining)}")
        waves.append(wave)
        done.update(wave)
        remaining = [tid for tid in remaining if tid not in done]
    return waves


# ─────────────────────────────── critic (90 §5) ──────────────────────────────


class CritiqueIssue(LlmModel):
    """One issue the critic found; ``task_id`` null = about the investigation as a whole."""

    task_id: str | None
    issue: str
    severity: Literal["low", "medium", "high"]


class Critique(LlmModel):
    """Output of the ``critic`` (tool-free). ``REVISE`` re-runs ``revise_task_ids`` once."""

    verdict: Literal["PASS", "REVISE"]
    issues: list[CritiqueIssue]
    revise_task_ids: list[str]


def critique_violations(critique: Critique, plan: InvestigationPlan) -> list[str]:
    """Problems with a critique of ``plan`` (empty = usable as is).

    ``REVISE`` must name at least one task to re-run and ``PASS`` none; every
    ``revise_task_ids`` entry and every non-null issue ``task_id`` must be a plan task;
    ``revise_task_ids`` are unique. The engine treats a critique with violations as
    ``PASS`` (the shared REVISE budget is never spent on an unusable critique).
    """
    known = {t.id for t in plan.tasks}
    problems: list[str] = []
    if critique.verdict == "REVISE" and not critique.revise_task_ids:
        problems.append("REVISE must name revise_task_ids")
    if critique.verdict == "PASS" and critique.revise_task_ids:
        problems.append("PASS must not name revise_task_ids")
    if len(set(critique.revise_task_ids)) != len(critique.revise_task_ids):
        problems.append("revise_task_ids must be unique")
    if unknown := sorted(set(critique.revise_task_ids) - known):
        problems.append(f"revise_task_ids not in the plan: {unknown}")
    for i, issue in enumerate(critique.issues):
        if issue.task_id is not None and issue.task_id not in known:
            problems.append(f"issues/{i}: unknown task_id {issue.task_id!r}")
    return problems


# ─────────────────────────────── follow-ups (90 §6) ──────────────────────────

FollowupKind = Literal["EXPLAIN", "RESIMULATE", "REINVESTIGATE"]
FOLLOWUP_KINDS: Final[tuple[str, ...]] = get_args(FollowupKind)


class ConstraintDelta(LlmModel):
    """User-stated changes to the simulation; null/empty = unchanged.

    ``max_cost_cents`` is integer euro cents (``"cap extra cost at €5k"`` → ``500000``).
    """

    delay_days: int | None
    duration_days: int | None
    exclude_levers: list[LeverCode]
    sku_filter: list[str]
    max_cost_cents: int | None

    @property
    def is_empty(self) -> bool:
        return (
            self.delay_days is None
            and self.duration_days is None
            and not self.exclude_levers
            and not self.sku_filter
            and self.max_cost_cents is None
        )


def constraint_delta_violations(delta: ConstraintDelta) -> list[str]:
    """Problems with a follow-up constraint delta (empty = OK): ``delay_days >= 0``,
    ``duration_days >= 1``, ``max_cost_cents >= 0``, unique ``exclude_levers`` and
    unique non-empty ``sku_filter`` codes."""
    problems: list[str] = []
    if delta.delay_days is not None and delta.delay_days < 0:
        problems.append(f"delay_days must be >= 0 (got {delta.delay_days})")
    if delta.duration_days is not None and delta.duration_days < 1:
        problems.append(f"duration_days must be >= 1 (got {delta.duration_days})")
    if delta.max_cost_cents is not None and delta.max_cost_cents < 0:
        problems.append(f"max_cost_cents must be >= 0 (got {delta.max_cost_cents})")
    if len(set(delta.exclude_levers)) != len(delta.exclude_levers):
        problems.append("exclude_levers must be unique")
    if any(not s.strip() for s in delta.sku_filter) or len(set(delta.sku_filter)) != len(delta.sku_filter):
        problems.append("sku_filter codes must be unique and non-empty")
    return problems


class FollowupClassification(LlmModel):
    """Output of ``followup_delta``: what kind of follow-up and which constraints changed."""

    kind: FollowupKind
    constraint_delta: ConstraintDelta
    question: str


# ──────────────────────────── quarantine (90 §4) ─────────────────────────────


class GuardVerdict(LlmModel):
    """Output of the prompt-injection screen (``guard-default``) for one untrusted text."""

    injection_suspected: bool
    reason: str


UntrustedItemKind = Literal[
    "delay_notice", "capacity_notice", "quality_issue", "price_change", "guidance", "other"
]
UNTRUSTED_ITEM_KINDS: Final[tuple[str, ...]] = get_args(UntrustedItemKind)
MAX_QUOTE_CHARS: Final = 200
MAX_MENTION_CHARS: Final = 80
"""Longest entity-mention text accepted from untrusted extraction (names/codes only)."""


class UntrustedItem(LlmModel):
    """One structured fact read from untrusted text; always labelled ``inference``."""

    source_evidence_id: str
    kind: UntrustedItemKind
    entity_mentions: list[EntityMention]
    delay_days: int | None
    quote: str


class UntrustedExtraction(LlmModel):
    """Output of the quarantined extractor (no tools, ``include_contents='none'``)."""

    items: list[UntrustedItem]
    injection_suspected: bool


def _item_violations(item: UntrustedItem, allowed: Collection[str]) -> list[str]:
    problems: list[str] = []
    if item.source_evidence_id not in allowed:
        problems.append(f"unknown source_evidence_id {item.source_evidence_id[:80]!r}")
    if len(item.quote) > MAX_QUOTE_CHARS:
        problems.append(f"quote longer than {MAX_QUOTE_CHARS} chars")
    for j, mention in enumerate(item.entity_mentions):
        if not mention.text.strip() or len(mention.text) > MAX_MENTION_CHARS:
            problems.append(f"entity_mentions/{j}: text must be 1..{MAX_MENTION_CHARS} chars")
    if item.delay_days is not None and item.delay_days < 0:
        problems.append(f"delay_days must be >= 0 (got {item.delay_days})")
    return problems


def untrusted_extraction_violations(
    extraction: UntrustedExtraction, evidence_ids: Collection[str]
) -> list[str]:
    """Problems with an extraction (empty = OK).

    ``injection_suspected`` on the extraction itself is a violation of the whole
    extraction (reported as ``"extraction: …"``; nothing from it may be used). Per item
    (``"items/<i>: …"``): it must cite one of ``evidence_ids`` (the raw evidence the
    wrapper stored), the quote is at most :data:`MAX_QUOTE_CHARS` characters, entity
    mention texts are 1..:data:`MAX_MENTION_CHARS` characters and ``delay_days >= 0``.
    Use :func:`usable_untrusted_items` to get what may be passed on.
    """
    allowed = set(evidence_ids)
    problems: list[str] = []
    if extraction.injection_suspected:
        problems.append("extraction: injection_suspected (drop the whole extraction)")
    for i, item in enumerate(extraction.items):
        problems.extend(f"items/{i}: {p}" for p in _item_violations(item, allowed))
    return problems


def usable_untrusted_items(
    extraction: UntrustedExtraction, evidence_ids: Collection[str]
) -> list[UntrustedItem]:
    """The items the quarantine wrapper may hand to an investigator (always as ``inference``).

    Nothing when the extractor flagged ``injection_suspected``; otherwise every item
    without a violation (see :func:`untrusted_extraction_violations`), in order.
    """
    if extraction.injection_suspected:
        return []
    allowed = set(evidence_ids)
    return [item for item in extraction.items if not _item_violations(item, allowed)]


# ───────────────────── M6 news extraction (100 §3, quarantined) ─────────────

MAX_NEWS_MENTIONS: Final = 20
"""Most entries per list of a :class:`NewsExtraction`."""
_SUBTYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class NewsExtraction(LlmModel):
    """Output of the Signal Hub's quarantined news extractor (LiteLLM ``extract-fast`` behind the
    ``guard-default`` screen; no tools). Input is a headline + snippet only. Everything here is
    ``inference``; places/ports/chokepoints are resolved by the gazetteer, never trusted as codes.
    Check with :func:`news_extraction_violations` before use (``injection_suspected`` → drop)."""

    kind: SignalKind
    subtype: str
    places: list[str]
    companies: list[str]
    ports: list[str]
    chokepoints: list[str]
    dates_text: list[str]
    injection_suspected: bool


def news_extraction_violations(extraction: NewsExtraction) -> list[str]:
    """Problems with a news extraction (empty = usable).

    ``injection_suspected`` makes the whole extraction unusable. ``subtype`` must be snake_case
    (<= 64 chars); each list holds at most :data:`MAX_NEWS_MENTIONS` entries of
    1..:data:`MAX_MENTION_CHARS` characters.
    """
    problems: list[str] = []
    if extraction.injection_suspected:
        problems.append("extraction: injection_suspected (drop the whole extraction)")
    if not _SUBTYPE_RE.match(extraction.subtype):
        problems.append("subtype must be snake_case, 1..64 chars")
    for name in ("places", "companies", "ports", "chokepoints", "dates_text"):
        values: list[str] = getattr(extraction, name)
        if len(values) > MAX_NEWS_MENTIONS:
            problems.append(f"{name}: more than {MAX_NEWS_MENTIONS} entries")
        for j, text in enumerate(values):
            if not text.strip() or len(text) > MAX_MENTION_CHARS:
                problems.append(f"{name}/{j}: text must be 1..{MAX_MENTION_CHARS} chars")
    return problems


LLM_OUTPUT_MODELS: Final[tuple[type[BaseModel], ...]] = (
    EntityMention,
    EntityRef,
    DisruptionExtract,
    IntakeResult,
    Claim,
    ScenarioUpdate,
    Findings,
    OptionReason,
    Recommendation,
    PlanBudget,
    InvestigationTask,
    InvestigationPlan,
    CritiqueIssue,
    Critique,
    ConstraintDelta,
    FollowupClassification,
    GuardVerdict,
    UntrustedItem,
    UntrustedExtraction,
    NewsExtraction,
)

# ─────────────────────────────── strict checker ─────────────────────────────

_TYPE_KEYS: Final = ("type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const")
_DISALLOWED_KEYWORDS: Final = (
    "default",
    "pattern",
    "patternProperties",
    "format",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "not",
    "if",
    "then",
    "else",
)


def _walk(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if not isinstance(node, dict):
        return
    yield path, node
    for name, sub in node.get("$defs", {}).items():
        yield from _walk(sub, f"{path}/$defs/{name}")
    for name, sub in node.get("properties", {}).items():
        yield from _walk(sub, f"{path}/properties/{name}")
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for i, sub in enumerate(node.get(key, [])):
            yield from _walk(sub, f"{path}/{key}/{i}")
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            yield from _walk(node[key], f"{path}/{key}")


def strict_schema_violations(schema_or_model: type[BaseModel] | dict[str, Any]) -> list[str]:
    """Return every reason a schema is not strict-structured-output compatible (empty = OK).

    Checks, for every node (including ``$defs``): each schema is typed (no ``{}``
    = ``Any``); objects declare ``properties``, set ``additionalProperties: false``
    and list *every* property in ``required``; no free-form dicts; ``$ref`` has no
    sibling keywords other than ``description``/``title`` (ADK strips them); and no
    keywords strict mode may reject or silently ignore (defaults, patterns, bounds).
    """
    schema = (
        schema_or_model.model_json_schema()
        if isinstance(schema_or_model, type)
        else schema_or_model
    )
    problems: list[str] = []
    for path, node in _walk(schema, "#"):
        where = path or "#"
        if not any(k in node for k in _TYPE_KEYS):
            problems.append(f"{where}: untyped schema (Any)")
            continue
        if "$ref" in node:
            extra = set(node) - {"$ref", "description", "title"}
            if extra:
                problems.append(f"{where}: $ref with sibling keywords {sorted(extra)}")
        for kw in _DISALLOWED_KEYWORDS:
            if kw in node:
                problems.append(f"{where}: keyword {kw!r} not allowed in strict schemas")
        types = node.get("type")
        is_object = types == "object" or (isinstance(types, list) and "object" in types)
        if is_object:
            props = node.get("properties")
            if not props:
                problems.append(f"{where}: object without properties (free-form dict)")
            ap = node.get("additionalProperties")
            if ap is not False:
                problems.append(
                    f"{where}: additionalProperties must be false (got {ap!r})"
                )
            if props:
                missing = sorted(set(props) - set(node.get("required", [])))
                if missing:
                    problems.append(f"{where}: properties not required: {missing}")
        if types == "array" and not isinstance(node.get("items"), dict):
            problems.append(f"{where}: array without typed items")
    return problems
