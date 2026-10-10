"""M8 Evals UI: pure run-level computations (``docs/contracts/128-m8-evals-ui.md`` §3).

No I/O: the summary of a run from its stored case results (the engine's authoritative numbers,
whatever the runner or an imported file claimed), the summary row of a case, the comparison of two
runs and the trend series. Integer units only (``_bp``, ``_ms``, micro-USD); means and medians
round half-up.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from keelson_contracts.evals import (
    EVAL_METRICS,
    LATENCY_STAGES,
    METRIC_OPS,
    EvalCaseCompare,
    EvalCaseResult,
    EvalCompare,
    EvalLatencyStat,
    EvalLatencySummary,
    EvalMetricDelta,
    EvalRun,
    EvalRunSummary,
    EvalThreshold,
    EvalTrendCase,
    EvalTrendRun,
    EvalTrends,
    metric_value,
)

__all__ = ["CASE_DELTA_METRICS", "case_columns", "compare_runs", "metric_delta", "summarise", "trends"]

CASE_DELTA_METRICS = ("entity_recall_bp", "numeric_grounding_bp", "trajectory_recall_bp", "duration_ms", "tokens",
                      "checks_failed")
"""Per-case values the comparison reports (``better`` direction: ratios up, the rest down)."""
_HIGHER_IS_BETTER = frozenset({"entity_recall_bp", "numeric_grounding_bp", "trajectory_recall_bp"})
MAX_FAILED_NAMES = 20
MAX_CHECK_DELTAS = 50


def _half_up(numerator: int, denominator: int) -> int:
    q, r = divmod(numerator, denominator)
    return q + (1 if 2 * r >= denominator else 0)


def _mean(values: Sequence[int]) -> int | None:
    return _half_up(sum(values), len(values)) if values else None


def _median(values: Sequence[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else _half_up(ordered[mid - 1] + ordered[mid], 2)


def _rate(part: int, whole: int) -> int | None:
    return _half_up(part * 10_000, whole) if whole else None


def summarise(cases: Sequence[EvalCaseResult]) -> EvalRunSummary:
    """Run metrics from the case results. ``pass_rate`` counts evaluated cases only (skipped and
    cancelled cases were never judged); tokens come from the exact usage when every case has it
    (``engine``), from the gateway deltas of ``keelson-evals/1`` files (``gateway``) or both."""
    statuses = [c.status for c in cases]
    checks = [k for c in cases for k in c.all_checks()]
    evaluated = [c for c in cases if c.status not in ("skipped", "cancelled")]
    adversarial = [c for c in evaluated if "adversarial" in c.tags]
    recall = [c.entity_recall_bp for c in cases if c.entity_recall_bp is not None]
    grounding = [c.numeric_grounding_bp for c in cases if c.numeric_grounding_bp is not None]
    trajectory = [c.trajectory_recall_bp for c in cases if c.trajectory_recall_bp is not None]
    latency: dict[str, EvalLatencyStat] = {}
    for stage in LATENCY_STAGES:
        values = [v for c in cases for t in c.turns if (v := getattr(t.timings, f"{stage}_ms")) is not None]
        latency[stage] = EvalLatencyStat(n=len(values), p50_ms=_median(values), max_ms=max(values) if values else None)
    exact = [c for c in cases if c.usage is not None]
    gateway = [c for c in cases if c.usage is None and c.tokens_gateway is not None]
    tokens = [int(c.tokens) for c in (*exact, *gateway) if c.tokens is not None]
    source = ("mixed" if exact and gateway else "engine" if exact else "gateway" if gateway else None)
    costs = [c.usage.total.cost_microusd for c in exact
             if c.usage is not None and c.usage.total.cost_microusd is not None]
    return EvalRunSummary(
        cases=len(cases),
        passed=statuses.count("pass"),
        failed=statuses.count("fail"),
        errors=statuses.count("error"),
        skipped=statuses.count("skipped"),
        cancelled=statuses.count("cancelled"),
        checks=len(checks),
        checks_failed=sum(1 for k in checks if not k.ok and not k.soft),
        soft_failed=sum(1 for k in checks if not k.ok and k.soft),
        pass_rate_bp=_rate(sum(1 for c in evaluated if c.status == "pass"), len(evaluated)),
        adversarial_cases=len(adversarial),
        adversarial_pass_rate_bp=_rate(sum(1 for c in adversarial if c.status == "pass"), len(adversarial)),
        entity_recall_mean_bp=_mean(recall),
        numeric_grounding_mean_bp=_mean(grounding),
        numeric_grounding_min_bp=min(grounding) if grounding else None,
        trajectory_recall_mean_bp=_mean(trajectory),
        latency=EvalLatencySummary(**latency),
        tokens_total=sum(tokens) if tokens else None,
        tokens_per_case_mean=_mean(tokens),
        tokens_source=source,  # type: ignore[arg-type]
        cost_microusd_total=sum(costs) if costs else None,
        cost_per_case_mean_microusd=_mean(costs),
        # untrusted reports: never a negative count (replayed calls are a subset of the calls)
        llm_calls_live=sum(max(0, c.usage.total.calls - c.usage.total.replayed) for c in exact if c.usage is not None),
        llm_calls_replayed=sum(min(c.usage.total.replayed, c.usage.total.calls) for c in exact if c.usage is not None),
        replay_misses=sum(1 for c in cases if c.error_code == "replay_miss"),
    )


def case_columns(result: EvalCaseResult) -> dict[str, Any]:
    """The ``eval_case_result`` summary columns of a case (listings never load ``result``)."""
    checks = result.all_checks()
    first = next((t for t in result.turns if t.timings.first_stage_ms is not None), None)
    options = next((t for t in result.turns if t.timings.options_ms is not None), None)
    failed = [k.name for k in checks if not k.ok and not k.soft]
    cost = result.usage.total.cost_microusd if result.usage is not None else None
    return {
        "title": result.title,
        "tags": list(result.tags),
        "status": result.status,
        "error_code": result.error_code,
        "error": result.error,
        "keelson_case_id": result.keelson_case_id,
        "duration_ms": result.duration_ms,
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "soft_failed": sum(1 for k in checks if not k.ok and k.soft),
        "entity_recall_bp": result.entity_recall_bp,
        "numeric_grounding_bp": result.numeric_grounding_bp,
        "trajectory_recall_bp": result.trajectory_recall_bp,
        "first_stage_ms": first.timings.first_stage_ms if first is not None else None,
        "options_ms": options.timings.options_ms if options is not None else None,
        "tokens": result.tokens,
        "cost_microusd": cost,
        "failed_checks": failed[:MAX_FAILED_NAMES],
    }


def metric_delta(base: int | None, head: int | None, *, higher_is_better: bool) -> EvalMetricDelta:
    if base is None or head is None:
        return EvalMetricDelta(base=base, head=head, delta=None, better=None)
    delta = head - base
    better = None if delta == 0 else (delta > 0) == higher_is_better
    return EvalMetricDelta(base=base, head=head, delta=delta, better=better)


def _case_values(c: EvalCaseResult) -> dict[str, int | None]:
    cols = case_columns(c)
    return {k: cols[k] for k in CASE_DELTA_METRICS}


def _failed_names(c: EvalCaseResult) -> set[str]:
    return {k.name for k in c.all_checks() if not k.ok and not k.soft}


def _change(base: str | None, head: str | None) -> str:
    if base is None:
        return "new"
    if head is None:
        return "removed"
    if base == head:
        return "unchanged"
    if head == "pass" and base in ("fail", "error"):
        return "fixed"
    if base == "pass" and head in ("fail", "error"):
        return "regressed"
    return "changed"


def compare_runs(base: EvalRun, base_cases: Sequence[EvalCaseResult], head: EvalRun,
                 head_cases: Sequence[EvalCaseResult]) -> EvalCompare:
    """Head against base: summary metric deltas (``better`` by each metric's direction) and every
    case of either run with its status change, metric deltas and the hard checks that newly fail /
    pass. Cases keep the head's order, then the cases only the base had."""
    deltas: dict[str, EvalMetricDelta] = {}
    for metric in EVAL_METRICS:
        b = metric_value(base.summary, metric) if base.summary is not None else None
        h = metric_value(head.summary, metric) if head.summary is not None else None
        deltas[metric] = metric_delta(b, h, higher_is_better=METRIC_OPS[metric] == "min")
    by_base = {c.case_id: c for c in base_cases}
    by_head = {c.case_id: c for c in head_cases}
    order = [c.case_id for c in head_cases] + [c.case_id for c in base_cases if c.case_id not in by_head]
    out: list[EvalCaseCompare] = []
    for case_id in order:
        b, h = by_base.get(case_id), by_head.get(case_id)
        title = (h or b).title  # type: ignore[union-attr]
        metric_deltas: dict[str, EvalMetricDelta] = {}
        newly_failed: list[str] = []
        newly_passed: list[str] = []
        if b is not None and h is not None:
            bv, hv = _case_values(b), _case_values(h)
            for key in CASE_DELTA_METRICS:
                metric_deltas[key] = metric_delta(bv[key], hv[key], higher_is_better=key in _HIGHER_IS_BETTER)
            bf, hf = _failed_names(b), _failed_names(h)
            newly_failed = sorted(hf - bf)[:MAX_CHECK_DELTAS]
            newly_passed = sorted(bf - hf)[:MAX_CHECK_DELTAS]
        out.append(EvalCaseCompare(
            case_id=case_id, title=title, base_status=b.status if b else None, head_status=h.status if h else None,
            change=_change(b.status if b else None, h.status if h else None),  # type: ignore[arg-type]
            metric_deltas=metric_deltas, checks_newly_failed=newly_failed, checks_newly_passed=newly_passed,
        ))
    return EvalCompare(base=base, head=head, summary_deltas=deltas, cases=out)


def trends(runs: Sequence[EvalRun], case_rows: Mapping[str, Iterable[tuple[str, str, str]]],
           thresholds: Sequence[EvalThreshold]) -> EvalTrends:
    """The trend series: ``runs`` oldest first with their metric values and breach counts, and a
    status strip per case (``case_rows``: run id → ``(case_id, title, status)``)."""
    points: list[EvalTrendRun] = []
    for run in runs:
        metrics = {m: (metric_value(run.summary, m) if run.summary is not None else None) for m in EVAL_METRICS}
        points.append(EvalTrendRun(
            id=run.id, at=run.started_at or run.created_at, mode=run.mode, source=run.source, status=run.status,
            metrics=metrics, breaches=run.breaches,
            tokens_source=run.summary.tokens_source if run.summary is not None else None,
        ))
    titles: dict[str, str] = {}
    status: dict[str, dict[str, str]] = {}
    for run in runs:
        for case_id, title, st in case_rows.get(run.id, ()):
            titles[case_id] = title
            status.setdefault(case_id, {})[run.id] = st
    cases = [EvalTrendCase(case_id=cid, title=titles[cid],
                           statuses=[status[cid].get(r.id) for r in runs])  # type: ignore[misc]
             for cid in sorted(titles)]
    return EvalTrends(runs=points, cases=cases[:200], thresholds=list(thresholds))
