"""M8 Evals UI (``docs/contracts/128-m8-evals-ui.md``): eval runs started in the back-office or
imported from the CLI, their case results, thresholds (targets, never gates), run comparison and
the results file format.

Units: ratios are basis points (``*_bp``, 10000 = 1.0), latencies milliseconds (``*_ms``), cost
integer micro-USD (``cost_microusd``: LiteLLM prices in USD; never mixed with the gateway's EUR
budgets), tokens integers. Floats appear only in the **results file** written by the CLI
(``evals/run_live.py``): ``keelson-evals/1`` (M4–M7: ratios 0–1, seconds) and ``keelson-evals/2``
(M8: a superset of 1 that adds integer fields, usage, trajectory, entities and latency targets).
:func:`case_result_from_doc` converts one case of either into an :class:`EvalCaseResult`
(half-up rounding); :func:`case_result_to_doc` is its inverse for exports, and
``case_result_from_doc(case_result_to_doc(r)) == r``.

Imported documents and runner reports are **untrusted**: the converter reads only known fields,
strips control and bidi-override characters, truncates strings and caps every list
(:data:`MAX_DOC_CASES` cases, :data:`MAX_TURNS` turns, :data:`MAX_CHECKS` checks per turn); the
result is validated by the strict models below and is never fetched, linked or sent to a model.

A results document's identity is :func:`results_doc_sha256` over its *normalised* form (header +
converted cases), so a CLI file, its re-serialisation and the engine's export of the imported run
all dedupe to the same run.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, ValidationError, model_validator

from .common import ContractModel, NonNegInt, PositiveInt, Sha256Hex, TenantSlug, canonical_json, sha256_hex

__all__ = [
    "CONFIRM_LIVE_TEXT",
    "DEFAULT_EVAL_THRESHOLDS",
    "EVALS_SCHEMAS",
    "EVALS_SCHEMA_V1",
    "EVALS_SCHEMA_V2",
    "EVAL_CASE_STATUSES",
    "EVAL_EVENT_TYPES",
    "EVAL_LIVE_MODES",
    "EVAL_METRICS",
    "EVAL_MODES",
    "EVAL_RUN_STATUSES",
    "EVAL_STOP_CODES",
    "LATENCY_STAGES",
    "MAX_CASE_RESULT_BYTES",
    "MAX_CHECKS",
    "MAX_DETAIL_CHARS",
    "MAX_DOC_CASES",
    "MAX_RUN_CASES",
    "MAX_TRAJECTORY_CALLS",
    "MAX_TURNS",
    "METRIC_OPS",
    "EvalActor",
    "EvalBudget",
    "EvalCaseCompare",
    "EvalCaseResult",
    "EvalCaseRow",
    "EvalCaseStatus",
    "EvalCheck",
    "EvalCompare",
    "EvalCounters",
    "EvalDocError",
    "EvalDocHeader",
    "EvalEngineAccounting",
    "EvalEntities",
    "EvalEventType",
    "EvalLatency",
    "EvalLatencyStat",
    "EvalLatencySummary",
    "EvalMetric",
    "EvalMetricDelta",
    "EvalMode",
    "EvalModeAvailability",
    "EvalProgress",
    "EvalRecordings",
    "EvalRun",
    "EvalRunCreate",
    "EvalRunDetail",
    "EvalRunEvent",
    "EvalRunEventIn",
    "EvalRunMode",
    "EvalRunStatus",
    "EvalRunSummary",
    "EvalRunnerInfo",
    "EvalRunnerJob",
    "EvalSource",
    "EvalSuiteCase",
    "EvalSuiteInfo",
    "EvalThreshold",
    "EvalThresholdResult",
    "EvalTrajectory",
    "EvalTrajectoryCall",
    "EvalTrendCase",
    "EvalTrendRun",
    "EvalTrends",
    "EvalTurnResult",
    "EvalUnavailableReason",
    "EvalUsage",
    "EvalUsageRow",
    "EvalsStatus",
    "breach_count",
    "case_result_from_doc",
    "case_result_to_doc",
    "clean_text",
    "eval_run_create_error",
    "evaluate_thresholds",
    "metric_value",
    "ratio_to_bp",
    "results_doc_header",
    "results_doc_sha256",
    "results_doc_to_case_results",
    "seconds_to_ms",
]

# ───────────────────────────── vocabulary & caps ─────────────────────────────

EVALS_SCHEMA_V1: Final = "keelson-evals/1"
EVALS_SCHEMA_V2: Final = "keelson-evals/2"
EVALS_SCHEMAS: Final = (EVALS_SCHEMA_V1, EVALS_SCHEMA_V2)

MAX_DOC_CASES: Final = 100
"""Cases per results document (import / export)."""
MAX_RUN_CASES: Final = 50
"""Cases one in-app run may select."""
MAX_TURNS: Final = 20
MAX_CHECKS: Final = 300
"""Checks per turn and per case (case-level checks)."""
MAX_DETAIL_CHARS: Final = 2000
MAX_TRAJECTORY_CALLS: Final = 100
MAX_CASE_RESULT_BYTES: Final = 1024 * 1024
"""Canonical JSON size of one :class:`EvalCaseResult` (the ``eval_case_result.result`` check)."""
CONFIRM_LIVE_TEXT: Final = "RUN LIVE"
"""What an admin types to start a live (model-calling) run."""

EvalMode = Literal["replay", "record_or_replay", "record"]
"""``replay``: recorded model answers only, a miss fails closed (no provider call); the two live
modes call the provider for every request without a recording (and record it)."""
EVAL_MODES: Final[tuple[str, ...]] = get_args(EvalMode)
EVAL_LIVE_MODES: Final = ("record_or_replay", "record")
EvalRunMode = Literal["replay", "record_or_replay", "record", "unknown"]
"""A run's mode; imported CLI files that do not say how they ran are ``unknown``."""
EvalSource = Literal["in_app", "import"]
EvalRunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
"""``succeeded`` = the runner got through every case (whatever their verdicts); ``failed`` = the
runner stopped (``runner_lost``, an error); ``cancelled`` = an admin cancelled it."""
EVAL_RUN_STATUSES: Final[tuple[str, ...]] = get_args(EvalRunStatus)
EvalCaseStatus = Literal["pass", "fail", "error", "skipped", "cancelled"]
EVAL_CASE_STATUSES: Final[tuple[str, ...]] = get_args(EvalCaseStatus)
EvalEventType = Literal["run_started", "case_started", "turn_started", "turn_finished", "case_finished", "note",
                        "run_finished"]
EVAL_EVENT_TYPES: Final[tuple[str, ...]] = get_args(EvalEventType)
EvalUnavailableReason = Literal["runner_not_configured", "runner_offline", "demo_mode_off", "live_disabled",
                                "replay_only", "gateway_budget_low"]
"""Why a mode cannot start now. ``gateway_budget_low`` (live modes): the eval tenant's AI gateway
team budget is used up (spend ≥ budget, compared in the gateway's own units)."""

EVAL_STOP_CODES: Final[Mapping[str, str]] = {
    "LLM_REPLAY_MISS": "replay_miss",
    "EVAL_BUDGET_EXHAUSTED": "budget_exhausted",
    "BUDGET_EXCEEDED": "budget_exhausted",
    "EVAL_CANCELLED": "cancelled",
}
"""``RUN_ERROR`` codes that end an eval case early → the case's ``error_code``."""

LATENCY_STAGES: Final = ("first_stage", "exposure", "options", "total")

_CASE_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
_ERROR_CODE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_UUID_PATTERN: Final = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_REF_PATTERN: Final = r"^[A-Za-z0-9_-]{1,64}$"

EvalCaseId = Annotated[str, Field(pattern=_CASE_ID_PATTERN)]
ErrorCode = Annotated[str, Field(pattern=_ERROR_CODE_PATTERN)]
Bp = Annotated[StrictInt, Field(ge=0, le=10_000)]
Label = Annotated[str, Field(min_length=1, max_length=80)]
PatternText = Annotated[str, Field(min_length=1, max_length=210)]
"""An ``agent:tool`` trajectory pattern (``*`` wildcards)."""
ShortText = Annotated[str, Field(max_length=300)]


def _uniq(values: Iterable[str]) -> bool:
    items = list(values)
    return len(items) == len(set(items))


# ───────────────────────────── untrusted text ─────────────────────────────

_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")
"""C0/C1 controls (newline, CR and tab kept), zero-width marks, line/paragraph separators and
bidi overrides/isolates (``Trojan Source``): never shown, never stored."""


def clean_text(value: Any, limit: int) -> str:
    """``value`` as display-safe text: control/bidi characters removed, at most ``limit``
    characters (``…`` marks a cut)."""
    text = _CONTROL_RE.sub("", str(value)).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


# ───────────────────────────── case results ─────────────────────────────


class EvalCheck(ContractModel):
    """One assertion of a turn or of the case (``<turn>.<check>`` / ``case.<check>``)."""

    name: Annotated[str, Field(min_length=1, max_length=160)]
    ok: StrictBool
    soft: StrictBool = False
    """Reported but never fails the case (metrics still being calibrated)."""
    detail: Annotated[str, Field(max_length=MAX_DETAIL_CHARS)] = ""
    """Plain text (may quote LLM output: shown as text, never rendered as markdown or links)."""


class EvalUsageRow(ContractModel):
    """LLM usage of one agent (or the total): the runs' ``metadata.usage``."""

    calls: NonNegInt = 0
    replayed: NonNegInt = 0
    """Calls answered from a recording (zero tokens, no cost)."""
    prompt_tokens: NonNegInt = 0
    completion_tokens: NonNegInt = 0
    reasoning_tokens: NonNegInt = 0
    total_tokens: NonNegInt = 0
    latency_ms: NonNegInt = 0
    cost_microusd: NonNegInt | None = None
    """``None`` = the proxy reported no cost."""


class EvalUsage(ContractModel):
    """Exact usage of a turn / case: the sum of ``metadata.usage`` over its runs."""

    runs: NonNegInt
    """Runs whose final event carried ``metadata.usage``."""
    total: EvalUsageRow
    agents: dict[str, EvalUsageRow] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _caps(self) -> EvalUsage:
        if len(self.agents) > 64 or any(not k or len(k) > 80 for k in self.agents):
            raise ValueError("agents: at most 64 names of 1-80 characters")
        return self


class EvalTrajectoryCall(ContractModel):
    agent: Annotated[str, Field(max_length=80)]
    tool: Annotated[str, Field(min_length=1, max_length=128)]
    n: PositiveInt


class EvalTrajectory(ContractModel):
    """Tool trajectory of a turn, in any order (plan §20): ``agent:tool`` patterns (``*`` =
    any agent / any tool) that must appear, patterns that must not, and the calls made."""

    required: Annotated[list[PatternText], Field(max_length=50)]
    matched: Annotated[list[PatternText], Field(max_length=50)]
    missing: Annotated[list[PatternText], Field(max_length=50)]
    forbidden: Annotated[list[PatternText], Field(max_length=50)]
    forbidden_hits: Annotated[list[Annotated[str, Field(max_length=210)]], Field(max_length=100)]
    """``agent:tool`` calls that matched a forbidden pattern."""
    calls: Annotated[list[EvalTrajectoryCall], Field(max_length=MAX_TRAJECTORY_CALLS)]
    """Calls per ``(agent, tool)``, most frequent first (capped)."""
    total_calls: NonNegInt
    max_calls: NonNegInt | None = None
    recall_bp: Bp | None
    """Share of ``required`` patterns matched; ``None`` = nothing required."""
    min_recall_bp: Bp = 10_000


class EvalEntities(ContractModel):
    """Affected-entity recall of a turn against its golden list."""

    golden: Annotated[list[Label], Field(max_length=200)]
    missing: Annotated[list[Label], Field(max_length=200)]
    recall_bp: Bp
    min_recall_bp: Bp


class EvalLatency(ContractModel):
    """Milliseconds from posting the turn to its first stage, first exposure strip, recommended
    option and end (``None`` = not reached / no budget)."""

    first_stage_ms: NonNegInt | None = None
    exposure_ms: NonNegInt | None = None
    options_ms: NonNegInt | None = None
    total_ms: NonNegInt | None = None


ExtraValue = StrictInt | StrictBool | ShortText | Annotated[list[Label], Field(max_length=50)] | None


class EvalTurnResult(ContractModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Literal["message", "action", "signal", "unknown"]
    run_ids: Annotated[list[Annotated[str, Field(pattern=_REF_PATTERN)]], Field(max_length=20)] = Field(
        default_factory=list)
    timings: EvalLatency = Field(default_factory=EvalLatency)
    budgets: EvalLatency | None = None
    """The turn's latency budgets (``latency_s`` expectation) when it had any."""
    plan_version: NonNegInt | None = None
    skipped: ShortText | None = None
    checks: Annotated[list[EvalCheck], Field(max_length=MAX_CHECKS)] = Field(default_factory=list)
    entity_recall_bp: Bp | None = None
    numeric_grounding_bp: Bp | None = None
    numeric_grounding_min_bp: Bp | None = None
    """The turn's grounding target (``numeric_grounding.min``)."""
    narrative_numbers: NonNegInt | None = None
    trajectory_recall_bp: Bp | None = None
    tool_calls: NonNegInt | None = None
    agents_started: Annotated[list[Label], Field(max_length=50)] = Field(default_factory=list)
    trajectory: EvalTrajectory | None = None
    entities: EvalEntities | None = None
    usage: EvalUsage | None = None
    final_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")] | None = None
    """``RUN_ERROR`` code of the turn's last run (``LLM_REPLAY_MISS``, ``INTERNAL_ERROR``, …)."""
    extra: dict[str, ExtraValue] = Field(default_factory=dict)
    """Other metrics of the turn (option ids, exposure summary, …) as short scalars / text."""

    @model_validator(mode="after")
    def _caps(self) -> EvalTurnResult:
        if len(self.extra) > 32 or any(not k or len(k) > 64 for k in self.extra):
            raise ValueError("extra: at most 32 keys of 1-64 characters")
        return self


class EvalEngineAccounting(ContractModel):
    """What the engine metered for the linked Keelson case (in-app runs; authoritative)."""

    llm_calls_live: NonNegInt
    llm_calls_replayed: NonNegInt
    tokens_live: NonNegInt
    replay_misses: NonNegInt
    cost_microusd: NonNegInt


class EvalCaseResult(ContractModel):
    """One eval case's outcome (``eval_case_result.result``; ``GET …/runs/{id}/cases/{case}``)."""

    case_id: EvalCaseId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=20)]
    status: EvalCaseStatus
    keelson_case_id: Annotated[str, Field(pattern=_REF_PATTERN)] | None
    """The Keelson case the runner created (a demo reset may have deleted it since)."""
    error: Annotated[str, Field(max_length=MAX_DETAIL_CHARS)] | None
    error_code: ErrorCode | None
    """``replay_miss`` | ``budget_exhausted`` | ``cancelled`` | ``needs_live_embeddings`` | …"""
    duration_ms: NonNegInt
    turns: Annotated[list[EvalTurnResult], Field(max_length=MAX_TURNS)]
    checks: Annotated[list[EvalCheck], Field(max_length=MAX_CHECKS)]
    """Case-level checks (``case.*``: safety, quarantine, narrative leaks, setup)."""
    entity_recall_bp: Bp | None
    """The first measured recall of the case (its main turn)."""
    numeric_grounding_bp: Bp | None
    """The minimum grounding over the case's turns."""
    trajectory_recall_bp: Bp | None
    """The minimum trajectory recall over the case's turns."""
    guard_flagged: NonNegInt | None
    tool_calls: NonNegInt | None
    usage: EvalUsage | None
    """Exact usage from the runs' ``metadata.usage`` (``keelson-evals/2``)."""
    tokens_gateway: NonNegInt | None
    """``keelson-evals/1``: the LiteLLM usage delta around the case (approximate: counts other
    traffic of the tenant too)."""
    engine: EvalEngineAccounting | None = None

    def all_checks(self) -> list[EvalCheck]:
        return [c for t in self.turns for c in t.checks] + list(self.checks)

    @property
    def tokens(self) -> int | None:
        """Live tokens of the case: exact usage when known, else the gateway delta."""
        if self.usage is not None:
            return self.usage.total.total_tokens
        return self.tokens_gateway


# ───────────────────────────── run summary & thresholds ─────────────────────────────


class EvalLatencyStat(ContractModel):
    n: NonNegInt
    p50_ms: NonNegInt | None
    max_ms: NonNegInt | None


class EvalLatencySummary(ContractModel):
    first_stage: EvalLatencyStat
    exposure: EvalLatencyStat
    options: EvalLatencyStat
    total: EvalLatencyStat


class EvalRunSummary(ContractModel):
    """Run-level metrics, computed by the engine from the stored case results (authoritative)."""

    cases: NonNegInt
    passed: NonNegInt
    failed: NonNegInt
    errors: NonNegInt
    skipped: NonNegInt
    cancelled: NonNegInt
    checks: NonNegInt
    checks_failed: NonNegInt
    soft_failed: NonNegInt
    pass_rate_bp: Bp | None
    """Passed / evaluated cases (skipped and cancelled cases are not evaluated)."""
    adversarial_cases: NonNegInt
    adversarial_pass_rate_bp: Bp | None
    entity_recall_mean_bp: Bp | None
    numeric_grounding_mean_bp: Bp | None
    numeric_grounding_min_bp: Bp | None
    trajectory_recall_mean_bp: Bp | None
    latency: EvalLatencySummary
    tokens_total: NonNegInt | None
    tokens_per_case_mean: NonNegInt | None
    tokens_source: Literal["engine", "gateway", "mixed"] | None
    """``engine`` = exact ``metadata.usage``; ``gateway`` = approximate LiteLLM deltas (v1 files)."""
    cost_microusd_total: NonNegInt | None
    cost_per_case_mean_microusd: NonNegInt | None
    llm_calls_live: NonNegInt
    llm_calls_replayed: NonNegInt
    replay_misses: NonNegInt
    """Cases that stopped at a replay miss."""


EvalMetric = Literal[
    "pass_rate_bp",
    "adversarial_pass_rate_bp",
    "entity_recall_mean_bp",
    "numeric_grounding_min_bp",
    "trajectory_recall_mean_bp",
    "first_stage_p50_ms",
    "exposure_max_ms",
    "options_max_ms",
    "tokens_per_case_mean",
    "cost_per_case_mean_microusd",
]
EVAL_METRICS: Final[tuple[str, ...]] = get_args(EvalMetric)
ThresholdOp = Literal["min", "max"]
METRIC_OPS: Final[Mapping[str, str]] = {
    "pass_rate_bp": "min",
    "adversarial_pass_rate_bp": "min",
    "entity_recall_mean_bp": "min",
    "numeric_grounding_min_bp": "min",
    "trajectory_recall_mean_bp": "min",
    "first_stage_p50_ms": "max",
    "exposure_max_ms": "max",
    "options_max_ms": "max",
    "tokens_per_case_mean": "max",
    "cost_per_case_mean_microusd": "max",
}
"""Which direction is better for each metric (``min``: higher is better)."""


def metric_value(summary: EvalRunSummary, metric: str) -> int | None:
    """The value of ``metric`` in ``summary`` (``None`` = not measured)."""
    lat = summary.latency
    values: dict[str, int | None] = {
        "pass_rate_bp": summary.pass_rate_bp,
        "adversarial_pass_rate_bp": summary.adversarial_pass_rate_bp,
        "entity_recall_mean_bp": summary.entity_recall_mean_bp,
        "numeric_grounding_min_bp": summary.numeric_grounding_min_bp,
        "trajectory_recall_mean_bp": summary.trajectory_recall_mean_bp,
        "first_stage_p50_ms": lat.first_stage.p50_ms,
        "exposure_max_ms": lat.exposure.max_ms,
        "options_max_ms": lat.options.max_ms,
        "tokens_per_case_mean": summary.tokens_per_case_mean,
        "cost_per_case_mean_microusd": summary.cost_per_case_mean_microusd,
    }
    if metric not in values:
        raise ValueError(f"unknown eval metric {metric!r}")
    return values[metric]


class EvalThreshold(ContractModel):
    """A target (never a gate): ``op: min`` = actual ≥ value, ``max`` = actual ≤ value."""

    metric: EvalMetric
    op: ThresholdOp
    value: NonNegInt
    modes: Annotated[list[EvalRunMode], Field(min_length=1, max_length=4)]
    """Run modes the target applies to (latency, tokens and cost only mean something live)."""

    @model_validator(mode="after")
    def _consistent(self) -> EvalThreshold:
        if self.op != METRIC_OPS[self.metric]:
            raise ValueError(f"{self.metric} is a {METRIC_OPS[self.metric]} target")
        if not _uniq(self.modes):
            raise ValueError("modes must be unique")
        return self


ThresholdOutcome = Literal["ok", "breach", "not_measured", "not_applicable"]


class EvalThresholdResult(ContractModel):
    metric: EvalMetric
    op: ThresholdOp
    value: NonNegInt
    actual: NonNegInt | None
    outcome: ThresholdOutcome


_ALL_MODES: Final[list[str]] = ["replay", "record_or_replay", "record", "unknown"]
_LIVE_OR_UNKNOWN: Final[list[str]] = ["record_or_replay", "record", "unknown"]
"""Imported CLI files without a mode label were live runs (the CLI's default)."""

DEFAULT_EVAL_THRESHOLDS: Final[tuple[EvalThreshold, ...]] = tuple(
    EvalThreshold(metric=m, op=METRIC_OPS[m], value=v, modes=modes)  # type: ignore[arg-type]
    for m, v, modes in (
        ("pass_rate_bp", 8000, _ALL_MODES),
        ("adversarial_pass_rate_bp", 10_000, _ALL_MODES),
        ("entity_recall_mean_bp", 8000, _ALL_MODES),
        ("numeric_grounding_min_bp", 10_000, _ALL_MODES),
        ("trajectory_recall_mean_bp", 8000, _ALL_MODES),
        ("first_stage_p50_ms", 2000, _LIVE_OR_UNKNOWN),
        ("exposure_max_ms", 30_000, _LIVE_OR_UNKNOWN),
        ("options_max_ms", 180_000, _LIVE_OR_UNKNOWN),
        ("tokens_per_case_mean", 400_000, _LIVE_OR_UNKNOWN),
        ("cost_per_case_mean_microusd", 250_000, _LIVE_OR_UNKNOWN),
    )
)
"""Starting targets (``90`` §9 latency budgets, plan §20). Uncalibrated: no live run was possible
when they were set; recalibrate after the first live runs (they are data, snapshotted per run)."""


def evaluate_thresholds(summary: EvalRunSummary, thresholds: Sequence[EvalThreshold], mode: str
                        ) -> list[EvalThresholdResult]:
    """Each threshold against the run (pure): ``not_applicable`` when the run's mode is not one
    of the threshold's, ``not_measured`` when the summary has no value."""
    out: list[EvalThresholdResult] = []
    for t in thresholds:
        actual = metric_value(summary, t.metric)
        if mode not in t.modes:
            outcome: ThresholdOutcome = "not_applicable"
        elif actual is None:
            outcome = "not_measured"
        elif t.op == "min":
            outcome = "ok" if actual >= t.value else "breach"
        else:
            outcome = "ok" if actual <= t.value else "breach"
        out.append(EvalThresholdResult(metric=t.metric, op=t.op, value=t.value, actual=actual, outcome=outcome))
    return out


def breach_count(results: Sequence[EvalThresholdResult]) -> int:
    return sum(1 for r in results if r.outcome == "breach")


# ───────────────────────────── API: requests ─────────────────────────────


class EvalBudget(ContractModel):
    """Caps of one live run (both required): engine-enforced per run (BudgetPlugin) and per eval
    run (pre-check before every run); never above the installation's ``EVALS_LIVE_MAX_*``."""

    max_tokens: Annotated[StrictInt, Field(ge=1, le=50_000_000)]
    max_cost_microusd: Annotated[StrictInt, Field(ge=1, le=100_000_000)]


class EvalRunCreate(ContractModel):
    """``POST /api/admin/evals/runs``: start an in-app run (platform admins)."""

    mode: EvalMode = "replay"
    case_ids: Annotated[list[EvalCaseId], Field(min_length=1, max_length=MAX_RUN_CASES)] | None = None
    """``None`` = every case of the runner's suite."""
    acknowledge_reset: Literal[True]
    """Each case re-seeds Merxa's demo data: the admin must say they know."""
    confirm_live: Annotated[str, Field(max_length=20)] | None = None
    """Live modes: exactly :data:`CONFIRM_LIVE_TEXT`."""
    budget: EvalBudget | None = None
    """Live modes: required. Replay runs make no provider calls and ignore it."""
    note: Annotated[str, Field(max_length=200)] | None = None

    @model_validator(mode="after")
    def _unique(self) -> EvalRunCreate:
        if self.case_ids is not None and not _uniq(self.case_ids):
            raise ValueError("case_ids must be unique")
        return self


def eval_run_create_error(req: EvalRunCreate, live_caps: EvalBudget) -> tuple[str, str] | None:
    """``(code, message)`` when a live request lacks its confirmation or budget, or asks for more
    than the installation's caps (the API answers 422 with that code); ``None`` = fine."""
    if req.mode not in EVAL_LIVE_MODES:
        return None
    if req.confirm_live != CONFIRM_LIVE_TEXT:
        return "confirm_required", f"type {CONFIRM_LIVE_TEXT} to start a run that calls the model provider"
    if req.budget is None:
        return "budget_required", "a live run needs a budget (max tokens and max cost)"
    if req.budget.max_tokens > live_caps.max_tokens or req.budget.max_cost_microusd > live_caps.max_cost_microusd:
        return "budget_too_high", (f"the budget exceeds this installation's caps ({live_caps.max_tokens} tokens, "
                                   f"{live_caps.max_cost_microusd} micro-USD)")
    return None


class EvalRunEventIn(ContractModel):
    """One progress event the runner reports (the engine assigns ``seq`` and ``at``)."""

    type: EvalEventType
    case_id: EvalCaseId | None = None
    turn_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    data: dict[str, StrictInt | StrictBool | ShortText | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _caps(self) -> EvalRunEventIn:
        if len(self.data) > 20 or any(not k or len(k) > 40 for k in self.data):
            raise ValueError("data: at most 20 keys of 1-40 characters")
        return self


# ───────────────────────────── API: views ─────────────────────────────


class EvalActor(ContractModel):
    sub: Annotated[str, Field(min_length=1, max_length=200)]
    username: Annotated[str, Field(min_length=1, max_length=200)]
    name: Annotated[str, Field(max_length=200)] | None


class EvalCounters(ContractModel):
    """Engine-side accounting of an in-app run (every linked case's runs; the live budget's base)."""

    tokens_live: NonNegInt
    tokens_replayed: NonNegInt
    llm_calls_live: NonNegInt
    llm_calls_replayed: NonNegInt
    replay_misses: NonNegInt
    cost_microusd: NonNegInt


class EvalProgress(ContractModel):
    cases_done: NonNegInt
    cases_total: NonNegInt
    current_case: EvalCaseId | None
    current_turn: Annotated[str, Field(max_length=64)] | None


class EvalRun(ContractModel):
    """An eval run (``GET /api/admin/evals/runs[/{id}]``)."""

    id: Annotated[str, Field(pattern=_UUID_PATTERN)]
    tenant: TenantSlug
    source: EvalSource
    mode: EvalRunMode
    status: EvalRunStatus
    case_ids: Annotated[list[EvalCaseId], Field(max_length=MAX_DOC_CASES)]
    requested_by: EvalActor | None
    note: Annotated[str, Field(max_length=200)] | None
    budget: EvalBudget | None
    counters: EvalCounters
    cancel_requested: StrictBool
    suite_hash: Sha256Hex | None
    summary: EvalRunSummary | None
    thresholds: Annotated[list[EvalThreshold], Field(max_length=20)]
    threshold_results: Annotated[list[EvalThresholdResult], Field(max_length=20)]
    breaches: NonNegInt
    doc_schema: Literal["keelson-evals/1", "keelson-evals/2"] | None
    file_name: Annotated[str, Field(max_length=120)] | None
    target: Annotated[str, Field(max_length=200)] | None
    """The stack the run measured (an imported file's ``keelson_url``): a label, never a link."""
    error: Annotated[str, Field(max_length=MAX_DETAIL_CHARS)] | None
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    progress: EvalProgress


class EvalCaseRow(ContractModel):
    """A case of a run, without its turns and checks (run detail table)."""

    case_id: EvalCaseId
    ordinal: NonNegInt
    title: Annotated[str, Field(min_length=1, max_length=200)]
    tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=20)]
    status: EvalCaseStatus
    error_code: ErrorCode | None
    error: Annotated[str, Field(max_length=MAX_DETAIL_CHARS)] | None
    keelson_case_id: Annotated[str, Field(pattern=_REF_PATTERN)] | None
    duration_ms: NonNegInt
    checks_total: NonNegInt
    checks_failed: NonNegInt
    soft_failed: NonNegInt
    entity_recall_bp: Bp | None
    numeric_grounding_bp: Bp | None
    trajectory_recall_bp: Bp | None
    first_stage_ms: NonNegInt | None
    options_ms: NonNegInt | None
    tokens: NonNegInt | None
    cost_microusd: NonNegInt | None
    failed_checks: Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=20)]
    """Names of the first failed hard checks (the adversarial matrix, quick triage)."""


class EvalRunDetail(ContractModel):
    run: EvalRun
    cases: Annotated[list[EvalCaseRow], Field(max_length=MAX_DOC_CASES)]


class EvalRunEvent(ContractModel):
    """A progress event (``GET …/runs/{id}/events?after=``)."""

    seq: PositiveInt
    type: EvalEventType
    case_id: EvalCaseId | None
    turn_id: Annotated[str, Field(max_length=64)] | None
    data: dict[str, StrictInt | StrictBool | ShortText | None]
    at: AwareDatetime


class EvalMetricDelta(ContractModel):
    base: StrictInt | None
    head: StrictInt | None
    delta: StrictInt | None
    better: StrictBool | None
    """``True`` = head improved on base (direction of :data:`METRIC_OPS`); ``None`` = equal or unknown."""


CaseChange = Literal["fixed", "regressed", "changed", "unchanged", "new", "removed"]


class EvalCaseCompare(ContractModel):
    case_id: EvalCaseId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    base_status: EvalCaseStatus | None
    head_status: EvalCaseStatus | None
    change: CaseChange
    metric_deltas: dict[str, EvalMetricDelta]
    checks_newly_failed: Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=50)]
    checks_newly_passed: Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=50)]


class EvalCompare(ContractModel):
    """``GET /api/admin/evals/compare?base=&head=``."""

    base: EvalRun
    head: EvalRun
    summary_deltas: dict[str, EvalMetricDelta]
    """Per :data:`EVAL_METRICS` key."""
    cases: Annotated[list[EvalCaseCompare], Field(max_length=2 * MAX_DOC_CASES)]


class EvalTrendRun(ContractModel):
    id: Annotated[str, Field(pattern=_UUID_PATTERN)]
    at: AwareDatetime
    """``started_at`` (else ``created_at``)."""
    mode: EvalRunMode
    source: EvalSource
    status: EvalRunStatus
    metrics: dict[str, StrictInt | None]
    """Per :data:`EVAL_METRICS` key."""
    breaches: NonNegInt
    tokens_source: Literal["engine", "gateway", "mixed"] | None


class EvalTrendCase(ContractModel):
    case_id: EvalCaseId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    statuses: list[EvalCaseStatus | None]
    """One entry per trend run (same order; ``None`` = not in that run)."""


class EvalTrends(ContractModel):
    """``GET /api/admin/evals/trends``: finished runs, oldest first."""

    runs: Annotated[list[EvalTrendRun], Field(max_length=100)]
    cases: Annotated[list[EvalTrendCase], Field(max_length=200)]
    thresholds: Annotated[list[EvalThreshold], Field(max_length=20)]


class EvalSuiteCase(ContractModel):
    id: EvalCaseId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=20)]
    turns: Annotated[StrictInt, Field(ge=1, le=MAX_TURNS)]
    turn_kinds: Annotated[list[Literal["message", "action", "signal"]], Field(min_length=1, max_length=MAX_TURNS)]
    needs_live_embeddings: StrictBool
    """The case uploads knowledge-base documents (embeddings are not replayed: skipped in replay)."""
    injects: StrictBool
    """The case injects data into Merxa (it always resets Merxa afterwards)."""


class EvalSuiteInfo(ContractModel):
    """The runner's case catalogue (heartbeat)."""

    suite_hash: Sha256Hex
    cases: Annotated[list[EvalSuiteCase], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _unique(self) -> EvalSuiteInfo:
        if not _uniq(c.id for c in self.cases):
            raise ValueError("suite case ids must be unique")
        return self


class EvalRunnerJob(ContractModel):
    """``POST /api/internal/evals/runner/claim`` → the run to execute."""

    run_id: Annotated[str, Field(pattern=_UUID_PATTERN)]
    mode: EvalMode
    case_ids: Annotated[list[EvalCaseId], Field(min_length=1, max_length=200)]
    budget: EvalBudget | None
    lease_expires_at: AwareDatetime


class EvalRunnerInfo(ContractModel):
    configured: StrictBool
    """``EVALS_RUNNER_TOKEN`` is set (the internal runner API is on)."""
    online: StrictBool
    runner_id: Annotated[str, Field(max_length=80)] | None
    version: Annotated[str, Field(max_length=40)] | None
    last_seen_at: AwareDatetime | None
    suite_hash: Sha256Hex | None


class EvalModeAvailability(ContractModel):
    mode: EvalMode
    available: StrictBool
    reason: EvalUnavailableReason | None
    message: Annotated[str, Field(max_length=300)] | None


class EvalRecordings(ContractModel):
    """``llm_replay`` coverage of the eval tenant (what a replay run can answer from)."""

    rows: NonNegInt
    bytes: NonNegInt
    newest_at: AwareDatetime | None
    agents: dict[str, NonNegInt]


class EvalsStatus(ContractModel):
    """``GET /api/admin/evals/status``."""

    tenant: TenantSlug
    """The tenant in-app runs execute in (the demo tenant)."""
    runner: EvalRunnerInfo
    suite: EvalSuiteInfo | None
    modes: Annotated[list[EvalModeAvailability], Field(min_length=3, max_length=3)]
    process_replay_mode: Literal["off", "record", "replay", "record_or_replay", "invalid"]
    live_caps: EvalBudget
    active_run: EvalRun | None
    recordings: EvalRecordings


# ───────────────────────────── results file format ─────────────────────────────


class EvalDocError(ValueError):
    """A results document or a reported case is not usable (message is safe to show)."""


class EvalDocHeader(ContractModel):
    """The normalised header of a results document."""

    doc_schema: Literal["keelson-evals/1", "keelson-evals/2"]
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    mode: EvalRunMode
    runner: Literal["cli", "in_app", "unknown"]
    suite_hash: Sha256Hex | None
    target: Annotated[str, Field(max_length=200)] | None


_SECONDS_ROUND: Final = Decimal(1)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def ratio_to_bp(value: Any) -> int | None:
    """A 0–1 ratio (float) → basis points, half-up; out of range / not a number → ``None``."""
    d = _decimal(value)
    if d is None or d < 0 or d > 1:
        return None
    return int((d * 10_000).quantize(_SECONDS_ROUND, rounding=ROUND_HALF_UP))


def seconds_to_ms(value: Any) -> int | None:
    """Seconds (float) → milliseconds, half-up; negative / not a number → ``None``."""
    d = _decimal(value)
    if d is None or d < 0 or d > 10**9:
        return None
    return int((d * 1000).quantize(_SECONDS_ROUND, rounding=ROUND_HALF_UP))


def _bp_of(metrics: Mapping[str, Any], name: str) -> int | None:
    """``<name>_bp`` (an int, ``keelson-evals/2``) or ``<name>`` (a 0–1 float)."""
    raw = metrics.get(f"{name}_bp")
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 10_000:
        return raw
    return ratio_to_bp(metrics.get(name))


def _bp_target(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        return None
    return value


def _nonneg_int(value: Any, upper: int = 2**53 - 1) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, upper)


def _str_list(value: Any, *, limit: int, max_items: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = clean_text(item, limit)
            if text and text not in out:
                out.append(text)
        if len(out) >= max_items:
            break
    return out


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _checks(raw: Any, detail_limit: int) -> list[EvalCheck]:
    out: list[EvalCheck] = []
    for item in raw if isinstance(raw, list) else []:
        if len(out) >= MAX_CHECKS:
            break
        if not isinstance(item, Mapping) or not isinstance(item.get("ok"), bool):
            continue
        name = clean_text(item.get("name") or "", 160)
        if not name:
            continue
        soft = item.get("soft")
        out.append(EvalCheck(name=name, ok=item["ok"], soft=soft if isinstance(soft, bool) else False,
                             detail=clean_text(item.get("detail") or "", detail_limit)))
    return out


def _latency(raw_ms: Any, raw_s: Any) -> EvalLatency:
    ms, secs = _mapping(raw_ms), _mapping(raw_s)
    values: dict[str, int | None] = {}
    for stage in LATENCY_STAGES:
        exact = _nonneg_int(ms.get(stage))
        values[f"{stage}_ms"] = exact if exact is not None else seconds_to_ms(secs.get(stage))
    return EvalLatency(**values)


def _usage(raw: Any) -> EvalUsage | None:
    """``{runs, total, agents}`` (as written by ``keelson-evals/2``); ``cost_microusd`` or a
    ``cost_usd`` float."""
    data = _mapping(raw)
    if not data or not isinstance(data.get("total"), Mapping):
        return None

    def row(r: Any) -> EvalUsageRow:
        m = _mapping(r)
        fields = {k: _nonneg_int(m.get(k)) or 0 for k in ("calls", "replayed", "prompt_tokens", "completion_tokens",
                                                          "reasoning_tokens", "total_tokens", "latency_ms")}
        cost = _nonneg_int(m.get("cost_microusd"))
        if cost is None:
            d = _decimal(m.get("cost_usd"))
            cost = None if d is None or d < 0 else int((d * 1_000_000).quantize(_SECONDS_ROUND, rounding=ROUND_HALF_UP))
        return EvalUsageRow(**fields, cost_microusd=cost)

    agents: dict[str, EvalUsageRow] = {}
    for name, value in _mapping(data.get("agents")).items():
        key = clean_text(name, 80)
        if key and len(agents) < 64:
            agents[key] = row(value)
    return EvalUsage(runs=_nonneg_int(data.get("runs")) or 0, total=row(data["total"]), agents=agents)


def _model_or_none(model: Any, raw: Any) -> Any:
    if not isinstance(raw, Mapping):
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


_TURN_METRIC_KEYS: Final = frozenset({
    "entity_recall", "entity_recall_bp", "numeric_grounding", "numeric_grounding_bp", "narrative_numbers",
    "tool_calls", "agents_started", "trajectory_recall", "trajectory_recall_bp", "trajectory", "entities", "targets",
})


def _extra(metrics: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        name = clean_text(key, 64)
        if not name or name in _TURN_METRIC_KEYS or len(out) >= 32:
            continue
        if value is None or isinstance(value, bool) or (isinstance(value, int) and abs(value) < 2**53):
            out[name] = value
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[name] = _str_list(value, limit=80, max_items=50)
        elif isinstance(value, str):
            out[name] = clean_text(value, 300)
        else:  # floats, nested objects: compact text
            try:
                text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
            except (TypeError, ValueError):
                continue
            out[name] = clean_text(text, 300)
    return out


def _turn(raw: Mapping[str, Any], detail_limit: int) -> EvalTurnResult:
    metrics = _mapping(raw.get("metrics"))
    kind = raw.get("kind")
    targets = _mapping(raw.get("targets"))
    budgets = _latency(targets.get("latency_ms"), None) if isinstance(targets.get("latency_ms"), Mapping) else None
    final_code = raw.get("final_code")
    run_ids = [r for r in (raw.get("run_ids") or []) if isinstance(r, str) and re.fullmatch(_REF_PATTERN, r)][:20]
    return EvalTurnResult(
        id=clean_text(raw.get("id") or "turn", 64) or "turn",
        kind=kind if kind in ("message", "action", "signal") else "unknown",
        run_ids=run_ids,
        timings=_latency(raw.get("timings_ms"), raw.get("timings_s")),
        budgets=budgets,
        plan_version=_nonneg_int(raw.get("plan_version")),
        skipped=clean_text(raw["skipped"], 300) if isinstance(raw.get("skipped"), str) and raw["skipped"] else None,
        checks=_checks(raw.get("checks"), detail_limit),
        entity_recall_bp=_bp_of(metrics, "entity_recall"),
        numeric_grounding_bp=_bp_of(metrics, "numeric_grounding"),
        numeric_grounding_min_bp=_bp_target(targets.get("numeric_grounding_min_bp")),
        narrative_numbers=_nonneg_int(metrics.get("narrative_numbers")),
        trajectory_recall_bp=_bp_of(metrics, "trajectory_recall"),
        tool_calls=_nonneg_int(metrics.get("tool_calls")),
        agents_started=_str_list(metrics.get("agents_started"), limit=80, max_items=50),
        trajectory=_model_or_none(EvalTrajectory, raw.get("trajectory")),
        entities=_model_or_none(EvalEntities, raw.get("entities")),
        usage=_usage(raw.get("usage")),
        final_code=final_code if isinstance(final_code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", final_code)
        else None,
        extra=_extra(metrics),
    )


def _status(raw: Any, error: str | None, error_code: str | None, checks: list[EvalCheck]) -> str:
    aliases = {"passed": "pass", "failed": "fail"}
    status = aliases.get(raw, raw) if isinstance(raw, str) else None
    if status in EVAL_CASE_STATUSES:
        return str(status)
    if error_code == "cancelled":
        return "cancelled"
    if error:
        return "error"
    return "fail" if any(not c.ok and not c.soft for c in checks) else "pass"


def _min(values: Iterable[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def case_result_from_doc(raw: Any) -> EvalCaseResult:
    """One case of a ``keelson-evals/1|2`` document (or a runner report) → :class:`EvalCaseResult`.

    Raises :class:`EvalDocError` for a case without a usable id or one that stays above
    :data:`MAX_CASE_RESULT_BYTES` after its check details are shortened."""
    if not isinstance(raw, Mapping):
        raise EvalDocError("a case is not an object")
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not re.fullmatch(_CASE_ID_PATTERN, case_id):
        raise EvalDocError("a case has no valid id")
    for detail_limit in (MAX_DETAIL_CHARS, 300, 80):
        result = _case(raw, case_id, detail_limit)
        if len(canonical_json(result.model_dump(mode="json")).encode("utf-8")) <= MAX_CASE_RESULT_BYTES:
            return result
    raise EvalDocError(f"case {case_id}: the result is larger than {MAX_CASE_RESULT_BYTES} bytes")


def _case(raw: Mapping[str, Any], case_id: str, detail_limit: int) -> EvalCaseResult:
    metrics = _mapping(raw.get("metrics"))
    error = clean_text(raw["error"], MAX_DETAIL_CHARS) if isinstance(raw.get("error"), str) and raw["error"] else None
    code = raw.get("error_code")
    error_code = code if isinstance(code, str) and re.fullmatch(_ERROR_CODE_PATTERN, code) else None
    turns = [_turn(t, detail_limit) for t in (raw.get("turns") or []) if isinstance(t, Mapping)][:MAX_TURNS]
    checks = _checks(raw.get("checks"), detail_limit)
    all_checks = [c for t in turns for c in t.checks] + checks
    kc = raw.get("case_id") if raw.get("keelson_case_id") is None else raw.get("keelson_case_id")
    duration = _nonneg_int(raw.get("duration_ms"))
    if duration is None:
        duration = seconds_to_ms(raw.get("duration_s")) or 0
    recall = _bp_of(metrics, "entity_recall")
    if recall is None and "entity_recall" not in metrics:
        recall = next((t.entity_recall_bp for t in turns if t.entity_recall_bp is not None), None)
    grounding = _bp_of(metrics, "numeric_grounding")
    if grounding is None and "numeric_grounding" not in metrics:
        grounding = _min(t.numeric_grounding_bp for t in turns)
    trajectory = _bp_of(metrics, "trajectory_recall")
    if trajectory is None and "trajectory_recall" not in metrics:
        trajectory = _min(t.trajectory_recall_bp for t in turns)
    tokens_gateway = _nonneg_int(metrics.get("tokens_gateway"))
    if tokens_gateway is None:
        tokens_gateway = _nonneg_int(metrics.get("tokens"))
    tags = _str_list(raw.get("tags"), limit=40, max_items=20)
    return EvalCaseResult(
        case_id=case_id,
        title=clean_text(raw.get("title") or case_id, 200) or case_id,
        tags=tags,
        status=_status(raw.get("status"), error, error_code, all_checks),  # type: ignore[arg-type]
        keelson_case_id=kc if isinstance(kc, str) and re.fullmatch(_REF_PATTERN, kc) else None,
        error=error,
        error_code=error_code,
        duration_ms=duration,
        turns=turns,
        checks=checks,
        entity_recall_bp=recall,
        numeric_grounding_bp=grounding,
        trajectory_recall_bp=trajectory,
        guard_flagged=_nonneg_int(metrics.get("guard_flagged")),
        tool_calls=_nonneg_int(metrics.get("tool_calls")),
        usage=_usage(raw.get("usage")),
        tokens_gateway=tokens_gateway,
        engine=_model_or_none(EvalEngineAccounting, raw.get("engine")),
    )


def _ms_to_s(ms: int | None) -> float | None:
    return None if ms is None else ms / 1000


def _bp_to_ratio(bp: int | None) -> float | None:
    return None if bp is None else bp / 10_000


def _latency_doc(lat: EvalLatency, *, seconds: bool) -> dict[str, Any]:
    if seconds:
        return {s: _ms_to_s(getattr(lat, f"{s}_ms")) for s in LATENCY_STAGES}
    return {s: getattr(lat, f"{s}_ms") for s in LATENCY_STAGES}


def _usage_doc(usage: EvalUsage | None) -> dict[str, Any] | None:
    return None if usage is None else usage.model_dump(mode="json")


def case_result_to_doc(result: EvalCaseResult) -> dict[str, Any]:
    """The ``keelson-evals/2`` file form of a case (exports): v1 fields (0–1 ratios, seconds) plus
    the exact integer fields the converter prefers, so the export re-imports unchanged."""
    turns: list[dict[str, Any]] = []
    for t in result.turns:
        metrics: dict[str, Any] = dict(t.extra)
        for name in ("entity_recall", "numeric_grounding", "trajectory_recall"):
            bp = getattr(t, f"{name}_bp")
            if bp is not None:
                metrics[name] = _bp_to_ratio(bp)
                metrics[f"{name}_bp"] = bp
        if t.narrative_numbers is not None:
            metrics["narrative_numbers"] = t.narrative_numbers
        if t.tool_calls is not None:
            metrics["tool_calls"] = t.tool_calls
        if t.agents_started:
            metrics["agents_started"] = list(t.agents_started)
        turn: dict[str, Any] = {
            "id": t.id, "kind": t.kind, "run_ids": list(t.run_ids),
            "timings_s": _latency_doc(t.timings, seconds=True), "timings_ms": _latency_doc(t.timings, seconds=False),
            "plan_version": t.plan_version, "skipped": t.skipped,
            "checks": [c.model_dump(mode="json") for c in t.checks], "metrics": metrics,
            "trajectory": None if t.trajectory is None else t.trajectory.model_dump(mode="json"),
            "entities": None if t.entities is None else t.entities.model_dump(mode="json"),
            "usage": _usage_doc(t.usage), "final_code": t.final_code,
        }
        targets: dict[str, Any] = {}
        if t.budgets is not None:
            targets["latency_ms"] = _latency_doc(t.budgets, seconds=False)
        if t.numeric_grounding_min_bp is not None:
            targets["numeric_grounding_min_bp"] = t.numeric_grounding_min_bp
        if targets:
            turn["targets"] = targets
        turns.append(turn)
    case_metrics: dict[str, Any] = {}
    for name in ("entity_recall", "numeric_grounding", "trajectory_recall"):
        bp = getattr(result, f"{name}_bp")
        case_metrics[name] = _bp_to_ratio(bp)
        if bp is not None:
            case_metrics[f"{name}_bp"] = bp
    if result.guard_flagged is not None:
        case_metrics["guard_flagged"] = result.guard_flagged
    if result.tool_calls is not None:
        case_metrics["tool_calls"] = result.tool_calls
    if result.tokens_gateway is not None:
        case_metrics["tokens"] = result.tokens_gateway
    return {
        "id": result.case_id, "title": result.title, "tags": list(result.tags), "status": result.status,
        "case_id": result.keelson_case_id, "error": result.error, "error_code": result.error_code,
        "duration_s": result.duration_ms / 1000, "duration_ms": result.duration_ms,
        "turns": turns, "checks": [c.model_dump(mode="json") for c in result.checks], "metrics": case_metrics,
        "usage": _usage_doc(result.usage),
        "engine": None if result.engine is None else result.engine.model_dump(mode="json"),
    }


def _when(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and 10 <= len(value) <= 40:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC).replace(microsecond=0)


def results_doc_header(doc: Any) -> EvalDocHeader:
    """The normalised header of a results document (``EvalDocError`` when it is not one)."""
    if not isinstance(doc, Mapping):
        raise EvalDocError("the results document is not a JSON object")
    schema = doc.get("schema")
    if schema not in EVALS_SCHEMAS:
        raise EvalDocError(f"unsupported results schema (expected one of {', '.join(EVALS_SCHEMAS)})")
    started = _when(doc.get("started_at"))
    if started is None:
        raise EvalDocError("started_at is missing or not an ISO date-time with a time zone")
    mode = doc.get("mode")
    runner = doc.get("runner")
    suite_hash = doc.get("suite_hash")
    target = doc.get("keelson_url")
    return EvalDocHeader(
        doc_schema=schema,  # type: ignore[arg-type]
        started_at=started,
        finished_at=_when(doc.get("finished_at")),
        mode=mode if mode in EVAL_MODES else "unknown",  # type: ignore[arg-type]
        runner=runner if runner in ("cli", "in_app") else ("cli" if schema == EVALS_SCHEMA_V1 else "unknown"),
        suite_hash=suite_hash if isinstance(suite_hash, str) and re.fullmatch(r"[0-9a-f]{64}", suite_hash) else None,
        target=clean_text(target, 200) or None if isinstance(target, str) else None,
    )


def results_doc_to_case_results(doc: Any) -> tuple[EvalDocHeader, list[EvalCaseResult]]:
    """A whole ``keelson-evals/1|2`` document → header + case results (1–100 cases, unique ids)."""
    header = results_doc_header(doc)
    cases = doc.get("cases")
    if not isinstance(cases, list) or not cases:
        raise EvalDocError("the document has no cases")
    if len(cases) > MAX_DOC_CASES:
        raise EvalDocError(f"the document has more than {MAX_DOC_CASES} cases")
    results = [case_result_from_doc(c) for c in cases]
    if not _uniq(r.case_id for r in results):
        raise EvalDocError("case ids repeat in the document")
    return header, results


def results_doc_sha256(header: EvalDocHeader, results: Sequence[EvalCaseResult]) -> str:
    """Identity of a results document: sha256 of the canonical JSON of its normalised content
    (start/finish second, mode, suite hash, converted cases). The file's schema version, target
    label and byte layout do not count, so a file and the export of its import are one run. A
    missing ``finished_at`` counts as the start (an imported run stores it that way)."""
    finished = header.finished_at or header.started_at
    payload = {
        "started_at": header.started_at.astimezone(dt.UTC).replace(microsecond=0).isoformat(),
        "finished_at": finished.astimezone(dt.UTC).replace(microsecond=0).isoformat(),
        "mode": header.mode,
        "suite_hash": header.suite_hash,
        "cases": [r.model_dump(mode="json") for r in results],
    }
    return sha256_hex(payload)
