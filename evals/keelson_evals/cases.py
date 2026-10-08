"""Eval case files (``evals/cases/*.yaml``): schema, loading and offline validation.

A case is data: a setup (Merxa reset / demo injects / temporary knowledge-base documents),
a list of turns (a chat message or a case action) each with its expectations, and case-level
expectations checked after the last turn. The runner (``run_live.py``) knows no scenario; it
only interprets these fields. ``python evals/run_live.py --dry-run`` validates every file
offline (schema, unique ids, referenced files exist, regexes compile, soft names known).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "ActionSpec",
    "Case",
    "CaseError",
    "CaseExpect",
    "Setup",
    "Turn",
    "TurnExpect",
    "check_names",
    "load_case",
    "load_cases",
    "validate_cases",
]

Id = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
NonEmpty = Annotated[str, Field(min_length=1)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Seconds = Annotated[float, Field(gt=0)]
IsoWeek = Annotated[str, Field(pattern=r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")]


class CaseError(Exception):
    """A case file is invalid (message names the file and the problem)."""


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ─────────────────────────────────── setup ───────────────────────────────────


class Inject(_M):
    """Merxa demo-director inject (``POST /v1/admin/scenario:inject``, ``merxa.admin``)."""

    kind: Literal["supplier_message", "trim_delay", "po_eta_change", "inventory_adjust", "port_closure", "typhoon",
                  "lane_capacity"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _supplier_message_fields(self) -> Inject:
        if self.kind == "supplier_message":
            missing = [k for k in ("supplier_code", "subject", "body") if not str(self.payload.get(k) or "").strip()]
            if missing:
                raise ValueError(f"supplier_message payload needs {missing}")
        if self.kind == "lane_capacity":
            lane, factor, weeks = (self.payload.get(k) for k in ("lane_code", "factor_bp", "weeks"))
            if not isinstance(lane, str) or not lane.strip():
                raise ValueError("lane_capacity payload needs lane_code")
            for name, value in (("factor_bp", factor), ("weeks", weeks)):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"lane_capacity payload needs an integer {name}")
        return self


class KbUpload(_M):
    """Documents uploaded through the admin API into a (temporary) knowledge base before the
    case and deleted afterwards. ``files`` are paths relative to the repository root."""

    kb_name: NonEmpty
    kb_description: str = "Temporary knowledge base created by the live evals"
    files: Annotated[list[NonEmpty], Field(min_length=1)]


class Setup(_M):
    reset_merxa: bool = True
    """Re-seed Merxa from its golden seed before the case (``merxa.admin``)."""
    inject: list[Inject] = Field(default_factory=list)
    resync: bool = True
    """Queue a canonical-store sync (tenant admin) after the reset / injects and wait for it."""
    kb_upload: KbUpload | None = None


class Teardown(_M):
    reset_merxa: bool = False
    """Re-seed Merxa after the case (e.g. to remove an injected adversarial message)."""


# ─────────────────────────────────── turns ───────────────────────────────────


class ApproachExpect(_M):
    required: bool = False
    """The turn must pause at the approach card."""
    mode: Literal["actual", "what_if"] | None = None


class OptionsExpect(_M):
    include: list[NonEmpty] = Field(default_factory=list)
    min_count: Annotated[int, Field(ge=0)] = 0


class LostUnitsExpect(_M):
    """The recommended option loses "0 to few" units: at most ``max`` units and/or at most
    ``max_ratio`` of the ``baseline`` option's lost units."""

    max: Annotated[int, Field(ge=0)] | None = None
    max_ratio: Ratio | None = None
    baseline: NonEmpty = "do-nothing"

    @model_validator(mode="after")
    def _some_bound(self) -> LostUnitsExpect:
        if self.max is None and self.max_ratio is None:
            raise ValueError("give max and/or max_ratio")
        return self


class EntitiesExpect(_M):
    golden: Annotated[list[NonEmpty], Field(min_length=1)]
    min_recall: Ratio = 1.0


class GroundingExpect(_M):
    min: Ratio = 1.0
    """Share of digit-bearing narrative tokens traceable to the case's structured data."""
    forbid_unverified_note: bool = False
    """Fail when the engine had to strip unverifiable sentences (its numeric linter)."""
    include_recommendation: bool = True
    """Also check the recommendation's LLM-written text (``summary_bullets``, alternatives'
    ``one_liner``): their numbers come from placeholders too (``90`` §9)."""


class LatencyExpect(_M):
    """Budgets in seconds from posting the turn (``90`` §9: 2 s / 30 s / 3 min)."""

    first_stage: Seconds | None = None
    exposure: Seconds | None = None
    options: Seconds | None = None
    total: Seconds | None = None


class ActionStateExpect(_M):
    type: Literal["apply", "guide", "confirm_assumptions", "promote"]
    offered: bool = True
    enabled: bool | None = None
    reason_contains: str | None = None


class ExposureExpect(_M):
    """Short cells in the ``stockout_timeline`` for every listed DC (``each_dc``) or any."""

    dc_codes: Annotated[list[NonEmpty], Field(min_length=1)]
    style_codes: list[NonEmpty] = Field(default_factory=list)
    style_prefixes: list[NonEmpty] = Field(default_factory=list)
    weeks: list[IsoWeek] = Field(default_factory=list)
    each_dc: bool = True
    min_weeks_short: Annotated[int, Field(ge=1)] = 1
    """Distinct short weeks required per DC (``each_dc``) or overall."""


class AnswerExpect(_M):
    min_evidence_items: Annotated[int, Field(ge=0)] = 1
    no_plan: bool = True
    """No plan blocks (options, approach card, plan diff, recommended option)."""


class TurnExpect(_M):
    run: Literal["success", "interrupt"] | None = "success"
    interrupt: Literal["approach", "approval"] | None = None
    approach: ApproachExpect | None = None
    blocks_present: list[NonEmpty] = Field(default_factory=list)
    blocks_absent: list[NonEmpty] = Field(default_factory=list)
    case_mode: Literal["actual", "what_if"] | None = None
    options: OptionsExpect | None = None
    recommended_lost_units: LostUnitsExpect | None = None
    affected_entities: EntitiesExpect | None = None
    numeric_grounding: GroundingExpect | None = None
    no_placeholders: bool = True
    latency_s: LatencyExpect | None = None
    actions: list[ActionStateExpect] = Field(default_factory=list)
    exposure: ExposureExpect | None = None
    answer: AnswerExpect | None = None
    plan_version_delta: int | None = None
    """Plan version of this turn minus that of the previous turn (``1`` = plan v+1)."""
    kpis_changed: bool | None = None
    """The final ``kpi_strip`` values differ from the previous turn's."""
    what_changed: bool | None = None
    """The narrative carries a "What changed" line."""
    agents_started: list[NonEmpty] = Field(default_factory=list)
    agents_not_started: list[NonEmpty] = Field(default_factory=list)
    replan_banner: bool | None = None
    """(M5) The turn's narrative carries the re-plan banner ("New information: … plan updated (vN)")."""
    never_auto: bool | None = None
    """(M5, ``95`` §1 rule 2) Nothing the turn compiled was approved by policy: no commit tracker,
    every approval card asks for people, and no Merxa change set newly reached a committed status
    while the turn ran."""


class SignalTurn(_M):
    """(M5) A turn started by the connector, not the user: a Merxa demo inject whose webhook the
    engine routes to this case as a ``signal`` run (e.g. ``lane_capacity`` → re-plan)."""

    inject: Inject
    start_timeout_s: Annotated[int, Field(ge=5, le=900)] = 120
    """How long to wait for the routed run to start (webhook delivery + routing)."""


class ActionSpec(_M):
    type: Literal["apply", "guide", "confirm_assumptions", "promote"]
    option: NonEmpty = "recommended"
    """Option id, or ``recommended`` (the latest ``recommended_option``)."""
    if_enabled: bool = False
    """Skip the turn (not a failure) when the action is not offered or disabled."""


class Turn(_M):
    id: Id
    message: NonEmpty | None = None
    action: ActionSpec | None = None
    signal: SignalTurn | None = None
    approach: Literal["confirm", "what_if", "none", "any"] = "confirm"
    """How to answer an approach card: confirm / what_if decision; ``none`` = the turn must not
    pause at one; ``any`` = confirm if it does."""
    expect: TurnExpect = Field(default_factory=TurnExpect)

    @model_validator(mode="after")
    def _one_kind(self) -> Turn:
        if sum(x is not None for x in (self.message, self.action, self.signal)) != 1:
            raise ValueError("a turn has exactly one of message / action / signal")
        return self

    @property
    def kind(self) -> str:
        return "message" if self.message is not None else "action" if self.action is not None else "signal"


# ─────────────────────────────── case-level expect ───────────────────────────────


class GuardExpect(_M):
    min_flagged: Annotated[int, Field(ge=1)] = 1
    source_tools: list[NonEmpty] = Field(default_factory=list)
    """Audit target prefixes, e.g. ``list_supplier_messages`` or ``kb.search``."""


class CodeLimit(_M):
    pattern: NonEmpty
    max: Annotated[int, Field(ge=0)]

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from None
        return v


class CaseExpect(_M):
    guard_flagged: GuardExpect | None = None
    no_write_tool_calls: bool = True
    """No LLM agent called a write / destructive tool (catalogue classification + name heuristic)."""
    no_write_tools_bound: bool = True
    """No LLM's tool allowlist (admin agents catalogue: specialists + answer investigator) holds a
    write / destructive or raw ``untrusted_text`` tool — "no write tool reaches an LLM" (``90`` §9)."""
    no_untrusted_tool_calls: bool = True
    """No LLM agent called a raw ``untrusted_text`` tool (only quarantine wrappers)."""
    no_commit_without_human: bool = True
    """No commit tracker, case not committed, no new Merxa change set in a committed status."""
    committed_statuses: list[NonEmpty] = Field(
        default_factory=lambda: ["applied", "applying", "conflict", "rolled_back"])
    """Merxa change-set statuses that mean an apply reached Merxa (``conflict`` = an apply was
    attempted and refused by Merxa) — any change set newly in one of them fails the case."""
    narrative_excludes: list[NonEmpty] = Field(default_factory=list)
    narrative_code_limit: list[CodeLimit] = Field(default_factory=list)


# ─────────────────────────────────── case ───────────────────────────────────


class Case(_M):
    id: Id
    title: NonEmpty
    description: str = ""
    tags: list[NonEmpty] = Field(default_factory=list)
    setup: Setup = Field(default_factory=Setup)
    turns: Annotated[list[Turn], Field(min_length=1)]
    expect: CaseExpect = Field(default_factory=CaseExpect)
    teardown: Teardown = Field(default_factory=Teardown)
    soft: list[NonEmpty] = Field(default_factory=list)
    """Check names (``<turn>.<check>`` or ``case.<check>``, ``*`` suffix = prefix) that are
    reported but never fail the case — for metrics still being calibrated."""

    @model_validator(mode="after")
    def _consistent(self) -> Case:
        ids = [t.id for t in self.turns]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate turn ids: {ids}")
        if self.turns[0].message is None:
            raise ValueError("the first turn must be a message (an action or a signal needs a case)")
        for i, t in enumerate(self.turns):
            if i == 0 and (t.expect.plan_version_delta is not None or t.expect.kpis_changed is not None):
                raise ValueError(f"turn {t.id}: plan_version_delta / kpis_changed need a previous turn")
        return self


def check_names(case: Case) -> set[str]:
    """Every check name the runner can produce for ``case`` (for ``soft`` validation)."""
    names: set[str] = set()
    for t in case.turns:
        e = t.expect
        p = f"{t.id}."
        names |= {p + "run", p + "follow"}
        if t.action is not None:
            names.add(p + "action")
        if t.signal is not None:
            names.add(p + "signal")
        if e.interrupt:
            names.add(p + "interrupt")
        if e.approach or t.approach == "none":
            names.add(p + "approach")
        names |= {p + f"block:{k}" for k in e.blocks_present} | {p + f"no_block:{k}" for k in e.blocks_absent}
        for attr in ("case_mode", "options", "recommended_lost_units", "affected_entities", "numeric_grounding",
                     "latency_s", "exposure", "answer", "plan_version_delta", "kpis_changed", "what_changed",
                     "replan_banner", "never_auto"):
            if getattr(e, attr) is not None:
                names.add(p + attr)
        if e.numeric_grounding is not None:
            names.add(p + "unverified_note")
        if e.no_placeholders:
            names.add(p + "no_placeholders")
        names |= {p + f"action:{a.type}" for a in e.actions}
        names |= {p + f"agent:{a}" for a in e.agents_started} | {p + f"no_agent:{a}" for a in e.agents_not_started}
    c = case.expect
    for attr in ("guard_flagged", "no_write_tool_calls", "no_write_tools_bound", "no_untrusted_tool_calls",
                 "no_commit_without_human",
                 "narrative_excludes", "narrative_code_limit"):
        if getattr(c, attr):
            names.add(f"case.{attr}")
    if case.setup.kb_upload is not None:
        names |= {"case.kb_upload", "case.kb_cleanup"}
    if case.setup.inject:
        names.add("case.inject")
    return names


def is_soft(case: Case, name: str) -> bool:
    for pattern in case.soft:
        if pattern.endswith("*") and name.startswith(pattern[:-1]):
            return True
        if pattern == name:
            return True
    return False


def load_case(path: Path) -> Case:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CaseError(f"{path.name}: cannot read: {exc}") from None
    if not isinstance(raw, dict):
        raise CaseError(f"{path.name}: the top level must be a mapping")
    try:
        case = Case.model_validate(raw)
    except ValidationError as exc:
        raise CaseError(f"{path.name}: {exc}") from None
    if case.id != path.stem:
        raise CaseError(f"{path.name}: id {case.id!r} must equal the file name stem {path.stem!r}")
    return case


def load_cases(cases_dir: Path, only: list[str] | None = None) -> list[Case]:
    files = sorted(cases_dir.glob("*.yaml"))
    if not files:
        raise CaseError(f"no case files in {cases_dir}")
    cases = [load_case(f) for f in files]
    if only:
        unknown = sorted(set(only) - {c.id for c in cases})
        if unknown:
            raise CaseError(f"unknown case ids: {unknown}")
        cases = [c for c in cases if c.id in only]
    return cases


def validate_cases(cases: list[Case], repo_root: Path) -> list[str]:
    """Offline problems beyond the schema (empty = valid): referenced files exist and are
    readable, ``soft`` names match a check of the case."""
    problems: list[str] = []
    for case in cases:
        if case.setup.kb_upload is not None:
            for rel in case.setup.kb_upload.files:
                p = (repo_root / rel).resolve()
                if not p.is_file():
                    problems.append(f"{case.id}: kb_upload file {rel} not found under {repo_root}")
                elif p.suffix.lower() not in (".md", ".txt", ".pdf"):
                    problems.append(f"{case.id}: kb_upload file {rel} is not .md/.txt/.pdf")
        known = check_names(case)
        for pattern in case.soft:
            hit = any(n.startswith(pattern[:-1]) for n in known) if pattern.endswith("*") else pattern in known
            if not hit:
                problems.append(f"{case.id}: soft entry {pattern!r} matches no check of this case")
    return problems
