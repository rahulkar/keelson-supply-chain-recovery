"""``impact`` → ``simulate`` → (``recommend`` LLM) → ``compose``.

``impact`` runs ``/v1/impact`` with ``include_graph`` on the confirmed scenario and keeps the
response (graph, timeline, TTS) as a ``keelson/impact`` evidence row for ``compose``.

``simulate`` evaluates the tenant's deterministic candidate plans (``AgentSpec.planning.candidates``,
default A–D, restricted to the enabled levers, ``80-m3-deterministic-core.md`` §4) plus "Do nothing"
with the optimizer. Each option carries its levers, scorecard and the connector capability check
(``guided_only`` + reason when the connector cannot apply its ops). It persists plan v{n}, pins the
snapshot the ProblemData came from (releasing the pin of the plan version it supersedes), and hands the Strategist a brief that names options and KPI
*placeholders* but contains no numbers.

``compose`` resolves placeholders, enforces the numbers rule (one repair turn, then strip and
mark unverified) and emits the review blocks — inline: ``kpi_strip`` (the exposure strip, now with
the recommended plan's effect; same block id, so it replaces) and ``recommended_option``; canvas:
``scenario_compare``, ``network_map``, ``impact_graph``, ``stockout_timeline`` (large payloads via
``data_ref``), ``plan_diff`` and ``evidence_pack`` — and the only user-visible text of the turn.

M8 (``126-m8-co2-robustness.md``): every optimize request of a plan version carries the CO₂e inputs
and the robustness spec (:class:`~keelson_engine.runtime.robustness.OptimizeInputs`); the default
recommendation ranks by the tenant's default weight profile; the KPI strip, the recommended option and
``scenario_compare`` show p10–p90 bands, the plan's extra CO₂e, profile scores and win rates. The
Strategist brief and its KPI catalog are unchanged (recorded replays stay valid).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from typing import Any

import structlog
from google.adk.agents.context import Context
from google.adk.workflow import FunctionNode
from keelson_contracts.blocks import (
    Advisory,
    AlternativeOption,
    ChangeTotals,
    EvidenceItem,
    EvidencePackProps,
    KpiItem,
    KpiStripProps,
    NarrativeProps,
    OptionAction,
    OptionSummary,
    PlanDiffProps,
    PlanDiffRow,
    RecommendedOptionProps,
    RemediationMix,
    UnconfirmedValue,
)
from keelson_contracts.common import Provenance
from keelson_contracts.llm import Findings, Recommendation
from keelson_contracts.optimizer import (
    LEVER_OPS,
    LEVERS,
    ImpactKpis,
    OptimizeResponse,
    PlanSpec,
    ProblemData,
    SolverOptions,
)

from ..advisory import ADVISORY_SENTENCE, advisory_from_kpis
from ..agents import KeelsonLlmAgent, fill_nullable
from ..candidates import DO_NOTHING_ID, build_candidates, capability_table
from ..robustness import OptimizeInputs, band_kpi_items, co2_item, decorate_compare, rank_key
from ..spec_m8 import ScoringConfig
from ..remediation import plan_dependencies, remediation_mix
from ..geo import get_geo
from ..visuals import (
    horizon_end,
    impact_graph,
    network_map,
    option_scorecard,
    outcome_kpi_items,
    scenario_compare_props,
    stockout_timeline,
    total_demand,
)
from ..compose import (
    UNVERIFIED_NOTE,
    ComposeContext,
    KpiValue,
    Unit,
    allowed_tokens_from,
    check_text,
    format_value,
    strip_unverified,
)
from ..context import RunContext, current_run
from ..errors import RuntimeConfigError
from ..mcp_client import get_gateway
from ..optimizer_client import get_optimizer
from ..quarantine import display_results
from ..spec import PlanningDefaults
from .case_state import ensure_planning_state
from .common import ENGINE_COMPOSE, ENGINE_OPT, block_event, input_dict, say
from .investigate import user_request
from .framing import (
    DATA_SOURCE_STATE_KEY,
    EVIDENCE_STATE_KEY,
    PROBLEM_STATE_KEY,
    SNAPSHOT_ERRORS,
    SNAPSHOT_LABEL_STATE_KEY,
    SNAPSHOT_STATE_KEY,
    impact_kpi_items,
    load_problem,
    run_impact,
)

__all__ = [
    "DO_NOTHING_ID",
    "FINDINGS_STATE_KEY",
    "REBALANCE_ID",
    "IMPACT_EVIDENCE_KEY",
    "action_units",
    "default_recommendation",
    "impact",
    "improves",
    "make_compose",
    "option_from",
    "cited_kpi_keys",
    "kpi_catalog",
    "kpi_meanings",
    "kpi_placeholders",
    "levers_used",
    "option_kpis",
    "plan_diff_props",
    "plan_summary",
    "repair_needed",
    "option_items_m8",
    "simulate",
    "strip_items_m8",
    "what_changed_line",
]

log = structlog.get_logger(__name__)

REBALANCE_ID = "opt-A"
FINDINGS_STATE_KEY = "findings"
IMPACT_STATE_KEY = "impact"
IMPACT_EVIDENCE_KEY = "impact_evidence_id"
SIMULATION_OVERRIDES_KEY = "simulation_overrides"
"""``ctx.state``: follow-up constraints for ``simulate`` (``followup.SimulationOverrides``)."""
BANNED_OPTIONS_KEY = "banned_options"
"""``ctx.state``: options over the follow-up's cost cap (compared, never recommended)."""
WHAT_CHANGED_KEY = "what_changed"
"""``ctx.state``: the follow-up / action's change list (``what_changed`` of its payload, ``90`` §6)."""
PREVIOUS_PLAN_KEY = "previous_plan"
"""``ctx.state``: summary of the plan version ``simulate`` superseded (for the "What changed" line)."""
"""``keelson/impact`` evidence row holding the full impact response (graph, timeline, TTS)."""

KPI_UNITS: dict[str, Unit] = {
    "units_recovered": "units",
    "margin_recovered_cents": "eur_cents",
    "transfer_cost_cents": "eur_cents",
    "net_benefit_cents": "eur_cents",
    "transfers": "count",
    "units_at_risk": "units",
    "revenue_at_risk_cents": "eur_cents",
    "margin_at_risk_cents": "eur_cents",
    "shortage_cluster_weeks": "count",
    "units_moved": "units",
    "lines": "count",
    "exposure_units_at_risk": "units",
    "exposure_revenue_at_risk_cents": "eur_cents",
    "exposure_margin_at_risk_cents": "eur_cents",
    "incremental_cost_cents": "eur_cents",
    "expedite_premium_cents": "eur_cents",
    "nearshore_premium_cents": "eur_cents",
    "fill_rate_bp": "pct_bp",
    "shortage_days": "days",
    "first_breach_shift_days": "days",
    "changes": "count",
}
KPI_LABELS = {
    "units_recovered": "Units recovered",
    "margin_recovered_cents": "Margin recovered",
    "transfer_cost_cents": "Transfer cost",
    "net_benefit_cents": "Net benefit",
    "transfers": "Transfer orders",
    "units_at_risk": "Units still at risk",
    "revenue_at_risk_cents": "Revenue still at risk",
    "margin_at_risk_cents": "Margin still at risk",
    "shortage_cluster_weeks": "Short cluster-weeks",
    "units_moved": "Units moved",
    "lines": "Change lines",
    "exposure_units_at_risk": "Units at risk if nothing is done",
    "exposure_revenue_at_risk_cents": "Revenue at risk if nothing is done",
    "exposure_margin_at_risk_cents": "Margin at risk if nothing is done",
    "incremental_cost_cents": "Plan cost",
    "expedite_premium_cents": "Expedite premium",
    "nearshore_premium_cents": "Near-shore premium",
    "fill_rate_bp": "Fill rate",
    "shortage_days": "Shortage days",
    "first_breach_shift_days": "First stock-out moves by",
    "changes": "Change lines",
}
KPI_MEANINGS = {
    "units_recovered": "units of lost sales this option avoids",
    "margin_recovered_cents": "margin this option protects (money)",
    "transfer_cost_cents": "cost of this option's transfers (money)",
    "net_benefit_cents": "margin protected minus transfer cost (money)",
    "transfers": "number of transfer orders; rendered as a bare number, so write the noun after it",
    "units_at_risk": "units still short AFTER this option",
    "revenue_at_risk_cents": "revenue still at risk AFTER this option (money)",
    "margin_at_risk_cents": "margin still at risk AFTER this option (money)",
    "shortage_cluster_weeks": "cluster-weeks still short AFTER this option; bare number, write the noun after it",
    "units_moved": "units moved between DCs by this option",
    "lines": "number of change lines sent to the ERP; bare number, write the noun after it",
    "exposure_units_at_risk": "units short if NOTHING is done (the measured exposure, same for every option)",
    "exposure_revenue_at_risk_cents": "revenue at risk if NOTHING is done (money, the measured exposure)",
    "exposure_margin_at_risk_cents": "margin at risk if NOTHING is done (money, the measured exposure)",
    "incremental_cost_cents": "total extra cost of this option: transfers + expedite premium + near-shore premium (money)",
    "expedite_premium_cents": "extra freight cost of moving units to sea-air (money)",
    "nearshore_premium_cents": "extra unit cost of buying from the alternate source (money)",
    "fill_rate_bp": "share of demand served with this option (a percentage)",
    "shortage_days": "days of shortage across clusters with this option (a number of days)",
    "first_breach_shift_days": "days by which the first stock-out moves later with this option (a number of days)",
    "changes": "number of change lines; bare number, write the noun after it",
}
"""What each KPI placeholder means (the Strategist sees these, never the values)."""


def action_units(actions: Iterable[Mapping[str, Any]]) -> int:
    total = 0
    for action in actions:
        payload = action.get("payload") or {}
        if "lines" in payload:
            total += sum(int(line["qty"]) for line in payload["lines"])
        elif "qty" in payload:
            total += int(payload["qty"])
    return total


def option_kpis(option: Mapping[str, Any]) -> dict[str, int]:
    kpis = {k: int(v) for k, v in (option.get("kpi_delta") or {}).items()}
    after = option.get("kpis_after") or {}
    for key in ("units_at_risk", "revenue_at_risk_cents", "margin_at_risk_cents", "shortage_cluster_weeks"):
        if key in after:
            kpis[key] = int(after[key])
    before = option.get("kpis_before") or {}
    for key in ("units_at_risk", "revenue_at_risk_cents", "margin_at_risk_cents"):
        if key in before:
            kpis[f"exposure_{key}"] = int(before[key])
    actions = option.get("actions") or []
    kpis["units_moved"] = action_units(actions)
    kpis["lines"] = len(actions)
    card = option.get("scorecard") or {}
    for key in ("incremental_cost_cents", "fill_rate_bp", "shortage_days", "first_breach_shift_days", "changes"):
        if key in card:
            kpis[key] = int(card[key])
    breakdown = card.get("cost_breakdown") or {}
    if "expedite_premium" in breakdown:
        kpis["expedite_premium_cents"] = int(breakdown["expedite_premium"])
    if "nearshore_premium" in breakdown:
        kpis["nearshore_premium_cents"] = int(breakdown["nearshore_premium"])
    return kpis


def kpi_catalog(option: Mapping[str, Any]) -> list[dict[str, str]]:
    """``[{placeholder, meaning}]`` for the Strategist (names and meanings, never values)."""
    return [
        {"placeholder": f"{{{{kpi:{option['id']}.{key}}}}}", "meaning": KPI_MEANINGS.get(key, key.replace("_", " "))}
        for key in sorted(option_kpis(option))
    ]


def _option_from(
    resp: OptimizeResponse,
    title: str,
    *,
    levers: Sequence[str] = (),
    guided_only: bool = False,
    guided_reason: str | None = None,
    problem: ProblemData | None = None,
) -> dict[str, Any]:
    fallback = str(resp.solver.params.get("plan_fallback", "")) == "do_nothing"
    option: dict[str, Any] = {
        "id": resp.plan_spec_id,
        "title": title,
        "status": resp.status,
        "levers": list(levers),
        "guided_only": guided_only,
        "guided_reason": guided_reason if guided_only else None,
        "kpis_before": resp.kpis_before.model_dump(mode="json"),
        "kpis_after": resp.kpis_after.model_dump(mode="json"),
        "kpi_delta": resp.kpi_delta.model_dump(mode="json"),
        "actions": [a.model_dump(mode="json") for a in resp.actions],
        "decisions": [d.model_dump(mode="json") for d in resp.decisions],
        "warnings": list(resp.warnings),
        "fallback": fallback,
        "inputs_hash": resp.solver.inputs_hash,
    }
    if resp.emissions is not None:  # M8 (126): factor set + coverage of the scorecard's CO2e
        option["emissions"] = resp.emissions.model_dump(mode="json")
    if resp.robustness is not None:  # M8: per-sample vectors (win rates), sampled inputs
        option["robustness"] = resp.robustness.model_dump(mode="json")
    if resp.scorecard is not None:
        option["scorecard"] = resp.scorecard.model_dump(mode="json")
    elif problem is not None:
        option["scorecard"] = option_scorecard(
            option, total_demand=total_demand(problem), horizon_end=horizon_end(problem)
        ).model_dump(mode="json")
    if resp.solver.stages:
        option["solver"] = solver_summary(resp)
    return option


def solver_summary(resp: OptimizeResponse) -> dict[str, Any]:
    """M8 (``integer_stage="cpsat"`` responses only, so ``repair`` options are unchanged): how the
    option's whole units were found (``integer_method`` is ``None`` when no integer stage ran,
    i.e. the option has no ``resource_alt_supplier``)."""
    params = resp.solver.params
    method = params.get("integer_method")
    gap = params.get("integer_gap_bp")
    fallback = params.get("integer_fallback")
    return {
        "integer_stage": "cpsat",
        "integer_method": str(method) if method is not None else None,
        "gap_bp": int(gap) if isinstance(gap, int) and not isinstance(gap, bool) else None,
        "fallback_reason": str(fallback) if fallback is not None else None,
        "resequence_methods": str(params["resequence_methods"]) if "resequence_methods" in params else None,
    }


def solver_options(planning: PlanningDefaults) -> SolverOptions:
    """The optimizer's solver options from the tenant's planning defaults (planning and re-plan);
    M8: ``integer_stage`` is omitted from the request while ``repair``."""
    return SolverOptions(
        time_limit_s=planning.solver_time_limit_s,
        seed=planning.solver_seed,
        integer_stage=planning.solver_integer_stage,
    )


option_from = _option_from
"""Public name of the optimizer response → plan option conversion (M5 re-plan)."""


def improves(option: Mapping[str, Any]) -> bool:
    """The option changes something and beats doing nothing (net benefit > 0)."""
    return (
        option["id"] != DO_NOTHING_ID
        and bool(option.get("actions"))
        and not option.get("fallback")
        and int((option.get("kpi_delta") or {}).get("net_benefit_cents") or 0) > 0
    )


def default_recommendation(options: Sequence[Mapping[str, Any]], scoring: ScoringConfig | None = None) -> str:
    """Deterministic default: the applicable improving option with the best net benefit (fewer
    changes, then candidate order on ties); guided-only options only when nothing applicable
    improves; else "Do nothing". M8 (126 §4): with ``scoring`` the improving options rank by the
    tenant's default weight profile (the built-in ``net-benefit`` profile is the net benefit)."""
    order = {str(o["id"]): i for i, o in enumerate(options)}
    score = rank_key(scoring)

    def best(pool: Sequence[Mapping[str, Any]]) -> str | None:
        ranked = sorted(
            (o for o in pool if improves(o)),
            key=lambda o: (score(o), len(o.get("actions") or []), order[str(o["id"])]),
        )
        return str(ranked[0]["id"]) if ranked else None

    return best([o for o in options if not o.get("guided_only")]) or best(options) or DO_NOTHING_ID


async def impact(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    """Final exposure on the confirmed scenario (replaces the preliminary strip)."""
    run = current_run()
    data = input_dict(node_input) if node_input is not None else {}
    if "findings" in data:
        ctx.state[FINDINGS_STATE_KEY] = list(data.get("findings") or [])
    ctx.state[WHAT_CHANGED_KEY] = [str(c) for c in data.get("what_changed") or [] if str(c).strip()][:6]
    await ensure_planning_state(ctx, run)  # later runs of the case (actions, follow-ups) start empty
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    problem_id = str(ctx.state.get(PROBLEM_STATE_KEY) or "")
    problem = await load_problem(run, problem_id)
    result = await run_impact(run, problem, record.scenario, include_graph=True)
    impact_id = await run.deps.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool="impact",
        args={"scenario_version": record.version, "problem_evidence_id": problem_id, "include_graph": True},
        result=result.model_dump(mode="json"), prov="simulated",
    )
    ctx.state[IMPACT_EVIDENCE_KEY] = impact_id
    ctx.state[IMPACT_STATE_KEY] = {
        "kpis": result.kpis.model_dump(mode="json"),
        "baseline_kpis": result.baseline_kpis.model_dump(mode="json"),
        "inputs_hash": result.provenance.inputs_hash,
    }
    yield await block_event(
        run,
        "kpi_strip",
        KpiStripProps(items=impact_kpi_items(result.kpis, result.baseline_kpis, preliminary=False)),
        title="Exposure",
        prov=Provenance.SIMULATED,
        engine=ENGINE_OPT,
        key=("exposure", problem_id),
        inputs_hash=result.provenance.inputs_hash,
    )
    yield {"scenario_version": record.version, "mode": record.scenario.mode}


async def simulate(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    await ensure_planning_state(ctx, run)
    record = await run.deps.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
    if record is None:
        raise LookupError("no scenario saved for the case")
    problem = await load_problem(run, str(ctx.state.get(PROBLEM_STATE_KEY) or ""))
    overrides = _simulation_overrides(ctx)
    planning = run.spec.planning if overrides is None else overrides.apply(run.spec.planning, problem)
    table = await capability_table(
        run.deps, tenant=run.tenant, connectors=run.spec.write_connectors(), vendor=run.spec.vendor_name
    )
    caps = table.primary
    candidates = build_candidates(planning, table)
    inputs = OptimizeInputs.build(planning, record.scenario, problem, record.scenario.perturbations, get_geo(),
                                  solver=solver_options(planning))
    options: list[dict[str, Any]] = []
    plans = [(c.plan_spec, c) for c in candidates] + [(PlanSpec(id=DO_NOTHING_ID, title="Do nothing", levers=[]), None)]
    for spec, cand in plans:  # the optimizer runs one computation at a time
        resp = await get_optimizer().optimize(inputs.request(spec))
        options.append(
            _option_from(
                resp, spec.title, levers=cand.levers if cand else (), guided_only=bool(cand and cand.guided_only),
                guided_reason=cand.guided_reason if cand else None, problem=problem,
            )
        )
    for option in options:  # M5: how the option's lines split into apply / guide lines
        if option.get("actions") and not option.get("fallback"):
            mix = ({"apply_lines": 0, "guide_lines": len(option["actions"])} if option.get("guided_only")
                   else remediation_mix(option["actions"], table))
            option["remediation"] = mix
            if not option.get("guided_only") and mix["apply_lines"] == 0:
                # every line is guided (e.g. an atomic group with an op no connector applies)
                ops = sorted({str(a["op"]) for a in option["actions"] if table.owner(str(a["op"])) is None})
                option["guided_only"] = True
                option["guided_reason"] = (
                    f"{run.spec.vendor_name} cannot apply {', '.join(ops) or 'these changes'} through its change "
                    "sets, so this option can only be followed as guided steps."
                )
    repo = run.deps.repo
    latest = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
    ctx.state[PREVIOUS_PLAN_KEY] = plan_summary(latest) if latest is not None else None
    banned: set[str] = set()
    if overrides is not None:
        if overrides.keep_previous and latest is not None:
            options = overrides.merge(list(latest.get("options") or []), options)
        banned = set(overrides.over_budget(options))
    ctx.state[BANNED_OPTIONS_KEY] = sorted(banned)
    default_id = default_recommendation([o for o in options if str(o["id"]) not in banned], planning.scoring)
    version = int((latest or {}).get("version") or 0) + 1
    snapshot_id = ctx.state.get(SNAPSHOT_STATE_KEY)
    dependencies = plan_dependencies(options, problem, run.spec.write_connectors())
    plan_id = await repo.save_plan(
        tenant=run.tenant,
        case_id=run.case_id,
        version=version,
        options=options,
        recommended_option_id=default_id,
        kpis={
            "before": options[0]["kpis_before"],
            "scenario_version": record.version,
            "snapshot_id": snapshot_id,
            "data_source": ctx.state.get(DATA_SOURCE_STATE_KEY) or "mcp",
            # M7 (110 §4): a crawled (tier-A) snapshot makes the plan advisory
            "snapshot_label": ctx.state.get(SNAPSHOT_LABEL_STATE_KEY) if snapshot_id else None,
            "capabilities": {"ops_source": caps.ops_source, "missing_tools": list(caps.missing_tools)},
            "dependencies": dependencies,
        },
    )
    if run.deps.plans is not None:
        try:
            await run.deps.plans.save_plan_dependencies(tenant=run.tenant, case_id=run.case_id, plan_version=version,
                                                        dependencies=dependencies)
        except Exception as exc:  # best effort; the plan row keeps the fingerprint (kpis.dependencies)
            log.warning("plan dependency index not updated", error=type(exc).__name__)
    if snapshot_id and run.deps.scn is not None and plan_id is not None:
        # Plans pin their snapshot (scn_pin → plan.id): never garbage-collected while the plan exists.
        try:
            await run.deps.scn.pin_snapshot(tenant=run.tenant, snapshot_id=str(snapshot_id), plan_id=int(plan_id))
        except SNAPSHOT_ERRORS as exc:
            log.warning("could not pin the plan's snapshot", snapshot_id=snapshot_id, error=type(exc).__name__)
    previous_id = (latest or {}).get("id")
    if run.deps.scn is not None and previous_id is not None and plan_id is not None and previous_id != plan_id:
        # The superseded version can no longer be applied (compile only takes the latest plan):
        # release its pin so the snapshot GC can reclaim the snapshot it was built from.
        try:
            await run.deps.scn.unpin_plan(tenant=run.tenant, plan_id=int(previous_id))
        except SNAPSHOT_ERRORS as exc:
            log.warning("could not release the superseded plan's snapshot pin", plan_id=previous_id,
                        error=type(exc).__name__)
    await run.deps.interrupts.supersede_open(tenant=run.tenant, case_id=run.case_id, reason=f"plan v{version}")
    yield _recommender_brief(record.scenario, options, ctx.state.get(FINDINGS_STATE_KEY) or [], banned=banned,
                             request=user_request(run))


def plan_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """``{version, units_at_risk, recommended: {id, title, kpis}}`` of a saved plan (What changed)."""
    options = {str(o["id"]): o for o in plan.get("options") or []}
    rec = options.get(str(plan.get("recommended_option_id")))
    before = (plan.get("kpis") or {}).get("before") or {}
    return {
        "version": int(plan.get("version") or 0),
        "units_at_risk": before.get("units_at_risk"),
        "recommended": None if rec is None else {"id": str(rec["id"]), "title": str(rec.get("title") or rec["id"]),
                                                 "kpis": option_kpis(rec)},
    }


def what_changed_line(previous: Mapping[str, Any] | None, current: Mapping[str, Any],
                      changes: Sequence[str] = ()) -> str | None:
    """One deterministic "What changed" line (``narrative@1.what_changed``, ``90`` §6): the stated
    changes, then the exposure and recommendation against the superseded plan version. Numbers come
    from the plans (never from an LLM). None for a case's first plan."""
    if not previous:
        return None
    parts = [str(c).strip().rstrip(".") for c in changes if str(c).strip()]
    head = f"Plan v{previous.get('version')} → v{current.get('version')}"
    before_risk, now_risk = previous.get("units_at_risk"), current.get("units_at_risk")
    if before_risk is not None and now_risk is not None:
        if int(before_risk) == int(now_risk):
            parts.append(f"units at risk unchanged at {format_value(int(now_risk), 'units')}")
        else:
            parts.append(f"units at risk {format_value(int(before_risk), 'units')} → {format_value(int(now_risk), 'units')}")
    prev_rec, rec = previous.get("recommended") or {}, current.get("recommended") or {}
    if rec:
        if prev_rec and prev_rec.get("id") != rec.get("id"):
            parts.append(f"recommendation {prev_rec.get('title')} → {rec.get('title')}")
        else:
            parts.append(f"recommendation still {rec.get('title')}")
        old_k, new_k = (prev_rec or {}).get("kpis") or {}, rec.get("kpis") or {}
        if "net_benefit_cents" in new_k and old_k.get("net_benefit_cents") != new_k["net_benefit_cents"]:
            if "net_benefit_cents" in old_k and prev_rec.get("id") == rec.get("id"):
                parts.append(f"net benefit {format_value(int(old_k['net_benefit_cents']), 'eur_cents')} → "
                             f"{format_value(int(new_k['net_benefit_cents']), 'eur_cents')}")
            else:
                parts.append(f"net benefit {format_value(int(new_k['net_benefit_cents']), 'eur_cents')}")
    return (head + ": " + "; ".join(parts) + ".")[:600]


class _Overrides:
    """``ctx.state["simulation_overrides"]`` of a follow-up (``nodes/followup.py``; ``M4_NODES.md``)."""

    def __init__(self, mod: Any, data: Mapping[str, Any]) -> None:
        self._mod = mod
        self.value = mod.SimulationOverrides.from_dict(data)

    @property
    def keep_previous(self) -> bool:
        return bool(self.value.keep_previous)

    def apply(self, planning: Any, problem: ProblemData) -> Any:
        return self._mod.planning_with_overrides(planning, self.value, problem)

    def merge(self, previous: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(self._mod.merge_options(previous, new))

    def over_budget(self, options: Sequence[Mapping[str, Any]]) -> list[str]:
        return list(self._mod.over_budget(options, self.value.max_cost_cents))


def _simulation_overrides(ctx: Context) -> _Overrides | None:
    data = ctx.state.get(SIMULATION_OVERRIDES_KEY)
    if not data:
        return None
    from . import followup  # noqa: PLC0415 - followup imports this module

    overrides = _Overrides(followup, data)
    return None if overrides.value.is_empty else overrides


def unconfirmed_values(scenario: Any, vendor: str) -> list[UnconfirmedValue]:
    """Assumptions flagged ``unconfirmed`` (``90`` §5, from ``refine_scenario``) as the recommended
    option shows them ("Confirm before applying: …"); Apply stays disabled while any exist."""
    out: list[UnconfirmedValue] = []
    for a in scenario.pending_confirmation:
        source = "Supplier or event information (unverified)" if a.source_evidence_ids else "Analyst inference"
        out.append(UnconfirmedValue(key=a.key, label=a.label or a.key, value_text=a.value_text or "unknown",
                                    source=source))
    return out


def _assessment(option: Mapping[str, Any]) -> str:
    if option["id"] == DO_NOTHING_ID:
        return "baseline: keeps today's plan, the exposure stays as measured"
    if option["fallback"]:
        text = "no plan with these levers improves the outcome; the optimizer fell back to doing nothing"
    elif improves(option):
        text = "improves the outcome versus doing nothing (protects more margin than it costs)"
    else:
        text = "does not improve the outcome versus doing nothing"
    if option.get("guided_only"):
        text += "; it cannot be applied automatically (guided steps only)"
    return text


def levers_used(option: Mapping[str, Any]) -> list[str]:
    """The option's levers whose ops its actions actually contain (catalogue order). A candidate
    configured with every lever (D "Hybrid") may end up using only some of them: the optimizer
    drops levers that do not pay (e.g. a resequence or near-shore buy beaten by sea-air)."""
    ops = {str(a.get("op")) for a in option.get("actions") or []}
    return [code for code in (option.get("levers") or []) if ops & set(LEVER_OPS.get(code, ()))]


def lever_title(code: str) -> str:
    """The lever's plain name ("Expedite by sea-air / air"); unknown codes in words."""
    d = LEVERS.get(str(code))
    return d.title if d is not None else str(code).replace("_", " ")


def _changes_key(option: Mapping[str, Any]) -> str:
    return json.dumps(option.get("actions") or [], sort_keys=True, separators=(",", ":"), default=str)


def _recommender_brief(
    scenario: Any, options: Sequence[Mapping[str, Any]], findings: Sequence[Mapping[str, Any]],
    *, banned: Iterable[str] = (), request: str | None = None,
) -> str:
    first_with: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    over = set(banned)
    for opt in options:
        assessment = _assessment(opt)
        if str(opt["id"]) in over:
            assessment += "; it exceeds the cost cap the user set, so it must not be recommended"
        if opt.get("actions") and not opt.get("fallback"):
            twin = first_with.setdefault(_changes_key(opt), str(opt["id"]))
            if twin != opt["id"]:
                # e.g. "Hybrid" that ends up with exactly the sea-air plan: say so instead of letting
                # the narrative credit it with levers it does not use
                assessment += f"; it makes exactly the same changes as {twin}"
        # plain lever names (never codes: the Strategist's prose quotes what it reads)
        used = [lever_title(c) for c in levers_used(opt)]
        considered = [lever_title(c) for c in opt.get("levers") or []]
        entry: dict[str, Any] = {
            "id": opt["id"],
            "title": opt["title"],
            "levers": used,
            "assessment": assessment,
            "kpi_placeholders": kpi_placeholders(opt),
        }
        if considered != used:
            entry["levers_considered"] = considered
        entries.append(entry)
    claims = list(dict.fromkeys(
        str(claim["text"])[:MAX_BRIEF_CLAIM_CHARS]
        for finding in findings
        if finding.get("status") == "ok"
        for claim in finding.get("claims") or []
    ))
    brief: dict[str, Any] = {
        "scenario": {"event_type": scenario.event_type, "description": scenario.description, "mode": scenario.mode},
        "options": entries,
        "kpi_meanings": kpi_meanings(options),
        "findings": claims[:MAX_BRIEF_CLAIMS],
    }
    if request:
        brief["user_request"] = request
    return json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


MAX_BRIEF_CLAIMS = 16
MAX_BRIEF_CLAIM_CHARS = 280


def kpi_placeholders(option: Mapping[str, Any]) -> list[str]:
    """The option's KPI placeholders (names only, never values; meanings in :func:`kpi_meanings`)."""
    return [f"{{{{kpi:{option['id']}.{key}}}}}" for key in sorted(option_kpis(option))]


def kpi_meanings(options: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """What each KPI key of the options means — once, not once per option (token diet)."""
    keys = sorted({key for opt in options for key in option_kpis(opt)})
    return {key: KPI_MEANINGS.get(key, key.replace("_", " ")) for key in keys}


# ─────────────────────────────────── compose ─────────────────────────────────


def _kpi_context(options: Sequence[Mapping[str, Any]], default: str) -> ComposeContext:
    kpis = {
        str(opt["id"]): {key: KpiValue(value, KPI_UNITS.get(key, "number")) for key, value in option_kpis(opt).items()}
        for opt in options
    }
    return ComposeContext(kpis=kpis, default_option=default)


def _rec_texts(rec: Recommendation) -> list[str]:
    return [rec.headline, *rec.bullets, *(r.reason for r in rec.why_not_others), *rec.risks, *rec.follow_ups]


def _ev_refs(texts: Iterable[str]) -> list[str]:
    ids: set[str] = set()
    for text in texts:
        ids |= set(re.findall(r"\{\{\s*ev\s*:\s*(ev_[0-9A-Za-z_-]+)#", text))
    return sorted(ids)


def _allowed_tokens(run: RunContext, scenario: Any, options: Sequence[Mapping[str, Any]], extra: Iterable[Any]) -> set[str]:
    values: list[Any] = [scenario.model_dump(mode="json")]
    for opt in options:
        values.append(opt.get("actions") or [])
        values.append(opt.get("decisions") or [])
    values.extend(extra)
    return allowed_tokens_from(values)


def plan_diff_props(
    *,
    plan_version: int,
    actions: Sequence[Mapping[str, Any]],
    vendor: str,
    problem: ProblemData | None = None,
    validated: Sequence[Mapping[str, Any]] | None = None,
    totals: Mapping[str, int] | None = None,
) -> PlanDiffProps:
    costs = {s.code: s.unit_cost_cents for s in problem.skus} if problem is not None else {}
    po_lines = {pl.ref: pl for pl in problem.po_lines} if problem is not None else {}
    allocations = {a.code: a for a in problem.allocations} if problem is not None else {}
    orders = {
        line.line_code: [o.code for o in sorted(line.orders, key=lambda o: o.seq) if o.movable]
        for line in (problem.production if problem is not None else [])
    }
    by_seq = {int(v["seq"]): v for v in validated or []}
    rows: list[PlanDiffRow] = []
    value_cents = 0
    units_total = 0
    for seq, action in enumerate(sorted(actions, key=lambda a: int(a.get("seq") or 0)), start=1):
        payload = action.get("payload") or {}
        op = str(action["op"])
        ref = str(action.get("target_ref") or "")
        pl = po_lines.get(ref)
        if op in ("transfer_order.create", "po.create"):
            lines = payload.get("lines") or []
            qty = sum(int(line["qty"]) for line in lines)
            value_cents += sum(int(line["qty"]) * costs.get(line["sku"], 0) for line in lines)
            after = ", ".join(f"{line['sku']} × {int(line['qty']):,}" for line in lines)
            if op == "transfer_order.create":
                target = f"Transfer {payload['from_dc']} → {payload['to_dc']}, ships {payload['ship_date']}"
                before = "No transfer order"
            else:
                target = (f"New purchase order {payload['supplier_code']} / {payload['factory_code']} → "
                          f"{payload['dest_dc']}, ETA {payload['eta_dc']}")
                before = "No purchase order"
        elif op == "po_line.split":
            qty = int(payload["qty"])
            target = f"PO line {ref}: move {qty:,} units to {payload['mode'].replace('_', '-')}"
            before = f"{pl.qty:,} units by {pl.mode.replace('_', '-')}, ETA {pl.eta}" if pl else "Current shipment"
            after = f"{qty:,} units on lane {payload['lane_code']}"
        elif op == "capacity_booking.cancel":
            qty = 0
            target = f"Release capacity booking {ref}"
            before = "Booked"
            after = "Released"
        elif op == "capacity_booking.create":
            qty = 0  # a capacity hold moves no stock (the split carries the units)
            target = f"Book capacity on {payload['lane_code']} in {payload['iso_week']}"
            before = "No booking"
            after = f"{int(payload['qty_units']):,} units held"
        elif op in ("po_line.change_qty", "po_line.cancel"):
            new = 0 if op == "po_line.cancel" else int(payload["qty"])
            old = pl.qty if pl else None
            qty = abs(old - new) if old is not None else new
            value_cents += qty * costs.get(pl.sku, 0) if pl else 0
            target = f"PO line {ref}" + (f" ({pl.sku})" if pl else "")
            before = f"{old:,} units open" if old is not None else "Current quantity"
            after = "Cancelled" if op == "po_line.cancel" else f"{new:,} units"
        elif op == "allocation.set_qty":
            new = int(payload["qty"])
            alloc = allocations.get(ref)
            qty = abs(alloc.qty - new) if alloc else new
            target = (f"Allocation {ref} ({alloc.sku}, {alloc.cluster_code}, {alloc.ship_week})" if alloc
                      else f"Allocation {ref}")
            before = f"{alloc.qty:,} units" if alloc else "Current quantity"
            after = f"{new:,} units"
        elif op == "production_order.resequence":
            codes = list(payload.get("order_codes") or [])
            qty = 0
            target = f"Factory line {payload['line_code']}: new order of {len(codes)} production orders"
            before = " → ".join(orders.get(str(payload["line_code"]), [])[:6]) or "Current sequence"
            after = " → ".join(codes[:6]) + (" …" if len(codes) > 6 else "")
        elif op == "po_line.reschedule":
            qty = pl.qty if pl else 0
            target = f"PO line {ref}" + (f" ({pl.sku})" if pl else "")
            before = f"ETA {pl.eta}" if pl else "Current ETA"
            after = f"ETA {payload['eta_dc']}"
        else:
            qty = int(payload.get("qty") or 0)
            target = f"{op} {ref}".strip()
            before = "Current"
            after = json.dumps(payload, sort_keys=True)
        validated_row = by_seq.get(seq)
        if validated_row is not None and validated_row.get("errors"):
            after = f"{after} — {'; '.join(str(e) for e in validated_row['errors'])}"
        units_total += qty
        if action.get("group"):
            target = f"{target} · applied together ({action['group']})"
        rows.append(
            PlanDiffRow(
                seq=seq, op=op, target_label=target, before_text=before, after_text=after,
                qty=qty, system=vendor,
            )
        )
    summary = ChangeTotals(
        lines=int((totals or {}).get("lines", len(rows))),
        units=int((totals or {}).get("units_moved", units_total)),
        value_cents=int((totals or {}).get("value_cents", value_cents)),
    )
    return PlanDiffProps(plan_version=plan_version, rows=rows, totals=summary)


def _narrative(rec: Recommendation, texts: Mapping[str, str], option_titles: Mapping[str, str], stripped: int) -> str:
    parts = [f"**{texts['headline']}**"] if texts.get("headline") else []
    bullets = [texts[f"bullet{i}"] for i in range(len(rec.bullets)) if texts.get(f"bullet{i}")]
    if bullets:
        parts.append("\n".join(f"- {b}" for b in bullets))
    why = [
        f"- {option_titles.get(r.option_id, r.option_id)}: {texts[f'why{i}']}"
        for i, r in enumerate(rec.why_not_others)
        if texts.get(f"why{i}")
    ]
    if why:
        parts.append("**Why not the alternatives**\n" + "\n".join(why))
    risks = [texts[f"risk{i}"] for i in range(len(rec.risks)) if texts.get(f"risk{i}")]
    if risks:
        parts.append("**Risks**\n" + "\n".join(f"- {r}" for r in risks))
    follow = [texts[f"follow{i}"] for i in range(len(rec.follow_ups)) if texts.get(f"follow{i}")]
    if follow:
        parts.append("**Next steps**\n" + "\n".join(f"- {f}" for f in follow))
    if stripped:
        parts.append(UNVERIFIED_NOTE)
    return "\n\n".join(parts) or "I evaluated the options; see the recommended option below."


MAX_OPTION_KPIS = 8
"""Most KPIs on the recommended option's card (the four defaults + the ones its texts cite)."""
_KPI_REF_RE = re.compile(r"\{\{\s*kpi\s*:\s*([^{}]+?)\s*\}\}")


def cited_kpi_keys(texts: Iterable[str], option_id: str) -> list[str]:
    """KPI keys of ``option_id`` cited by ``{{kpi:…}}`` placeholders in ``texts`` (a bare
    ``{{kpi:key}}`` is the default, i.e. the chosen option), first citation first."""
    keys: list[str] = []
    for text in texts:
        for ref in _KPI_REF_RE.findall(text or ""):
            option, _, key = ref.rpartition(".")
            if (option or option_id) == option_id and key and key not in keys:
                keys.append(key)
    return keys


def repair_needed(violations_by_key: Mapping[str, Sequence[str]]) -> bool:
    """Token diet: the Strategist's repair turn runs only for a *real* problem — a numbers-rule
    violation in the headline or a bullet (the core answer the user reads first; stripping it would
    leave a hole). Violations confined to why-not reasons, risks and next steps are stripped
    deterministically (and marked unverified) without another model call. Schema problems never get
    here: the structured output is validated (one retry) before ``compose``, and a recommended option
    that is not allowed falls back to the deterministic default."""
    return any(key == "headline" or key.startswith("bullet") for key in violations_by_key)


def _keyed_texts(rec: Recommendation) -> dict[str, str]:
    texts = {"headline": rec.headline}
    texts |= {f"bullet{i}": b for i, b in enumerate(rec.bullets)}
    texts |= {f"why{i}": r.reason for i, r in enumerate(rec.why_not_others)}
    texts |= {f"risk{i}": r for i, r in enumerate(rec.risks)}
    texts |= {f"follow{i}": f for i, f in enumerate(rec.follow_ups)}
    return texts


async def evidence_items(run: RunContext, ids: Sequence[str], *, limit: int = 60) -> list[EvidenceItem]:
    if not ids:
        return []
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=list(dict.fromkeys(ids))[:limit])
    items: list[EvidenceItem] = []
    sources: dict[str, str] = {"keelson": "Keelson", run.spec.connector: run.spec.vendor_name}
    for row in rows:
        connector = str(row.get("connector") or "keelson")
        if connector not in sources:
            try:
                info = await get_gateway().connector_info(tenant=run.tenant, connector=connector)
                sources[connector] = info.label
            except (LookupError, RuntimeConfigError):
                sources[connector] = connector
        created = row.get("created_at") or dt.datetime.now(dt.UTC)
        if isinstance(created, str):
            created = dt.datetime.fromisoformat(created)
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)
        tool = str(row.get("tool") or "tool")
        source = sources[connector]
        args = row.get("args") or {}
        arg_text = ", ".join(f"{k}={v}" for k, v in sorted(args.items()) if v not in (None, "", [])) if isinstance(args, Mapping) else ""
        items.append(
            EvidenceItem(
                id=str(row["id"]),
                source=source,
                label=f"{tool.replace('_', ' ')}" + (f" ({arg_text[:120]})" if arg_text else ""),
                prov=Provenance(str(row.get("prov") or "fact")),
                retrieved_at=created,
            )
        )
    return items


async def _impact_result(run: RunContext, ctx: Context) -> dict[str, Any] | None:
    evidence_id = ctx.state.get(IMPACT_EVIDENCE_KEY)
    if not evidence_id:
        return None
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=[str(evidence_id)])
    return dict(rows[0]["result"]) if rows else None


EXPOSURE_BAND_KEYS = frozenset({"units_at_risk", "margin_at_risk_cents", "shortage_cluster_weeks"})
"""Exposure tiles of the KPI strip: their bands are do-nothing's (what happens if nothing is done)."""


def strip_items_m8(items: Sequence[KpiItem], option: Mapping[str, Any],
                   options: Sequence[Mapping[str, Any]]) -> list[KpiItem]:
    """M8 (126 §5): the exposure + plan strip with p10–p90 bands (exposure from "Do nothing", the
    plan tiles from the recommended option) and the plan's extra CO₂e tile."""
    nothing = next((o for o in options if str(o["id"]) == DO_NOTHING_ID), None)
    out = band_kpi_items(items, option, exposure=nothing, exposure_keys=EXPOSURE_BAND_KEYS)
    co2 = co2_item(option)
    if co2 is not None and all(i.key != co2.key for i in out):
        out.append(co2)
    return out


def option_items_m8(items: Sequence[KpiItem], option: Mapping[str, Any]) -> list[KpiItem]:
    """M8: the recommended option's KPI tiles with bands, plus its extra CO₂e (within the card's limit)."""
    out = band_kpi_items(items, option)
    co2 = co2_item(option)
    if co2 is not None and len(out) < MAX_OPTION_KPIS and all(i.key != co2.key for i in out):
        out.append(co2)
    return out


async def _visual_blocks(
    run: RunContext,
    *,
    problem: ProblemData,
    problem_id: str,
    impact_data: Mapping[str, Any] | None,
    scenario: Any,
    options: Sequence[Mapping[str, Any]],
    chosen: str,
    version: int,
    recommended: RecommendedOptionProps,
    option: Mapping[str, Any],
    advisory: Advisory | None = None,
) -> list[Any]:
    """Inline: the exposure strip with the plan's effect (replaces the impact strip — same id) and
    the recommended option. Canvas: scenario compare, network map, impact graph, stockout timeline
    (map / graph / timeline payloads behind ``data_ref``)."""
    events: list[Any] = []
    inputs_hash = str(option.get("inputs_hash") or "") or None
    if impact_data is not None:
        kpis = ImpactKpis.model_validate(impact_data["kpis"])
        baseline = ImpactKpis.model_validate(impact_data["baseline_kpis"])
        card = option_scorecard(option, total_demand=total_demand(problem), horizon_end=horizon_end(problem))
        items = outcome_kpi_items(impact_kpi_items(kpis, baseline, preliminary=False), option, card)
        events.append(
            await block_event(
                run, "kpi_strip",
                KpiStripProps(items=strip_items_m8(items, option, options)),
                title="Exposure and recommended plan", prov=Provenance.SIMULATED, engine=ENGINE_OPT,
                key=("exposure", problem_id), inputs_hash=str(impact_data["provenance"]["inputs_hash"]),
            )
        )
    events.append(
        await block_event(
            run, "recommended_option", recommended, title="Recommended option", prov=Provenance.SIMULATED,
            engine=ENGINE_COMPOSE, key=("recommended", version), inputs_hash=inputs_hash,
        )
    )
    scored = [o for o in options if o.get("scorecard")]
    if scored:
        events.append(
            await block_event(
                run, "scenario_compare",
                decorate_compare(scenario_compare_props(scored, recommended_id=chosen), scored,
                                 run.spec.planning.scoring).model_copy(update={"advisory": advisory}),
                title="Options compared", prov=Provenance.SIMULATED, engine=ENGINE_OPT, key=("compare", version),
                placement="canvas",
            )
        )
    if impact_data is None:
        return events
    impact_hash = str(impact_data["provenance"]["inputs_hash"])
    map_props, map_data = network_map(
        problem=problem, scenario=scenario, impact=impact_data, options=options, geo=get_geo(),
        title=f"{run.spec.vendor_name} supply network",
    )
    if map_data.nodes:  # nothing to place without coordinates (e.g. a connector that publishes none)
        events.append(
            await block_event(
                run, "network_map", map_props, title="Network map", prov=Provenance.SIMULATED, engine=ENGINE_OPT,
                key=("map", version), inputs_hash=impact_hash, placement="canvas", data=map_data,
            )
        )
    graph = impact_graph(impact_data)
    if graph is not None:
        events.append(
            await block_event(
                run, "impact_graph", graph[0], title="Impact graph", prov=Provenance.SIMULATED, engine=ENGINE_OPT,
                key=("graph", version), inputs_hash=impact_hash, placement="canvas", data=graph[1],
            )
        )
    timeline = stockout_timeline(problem=problem, impact=impact_data, options=options)
    if timeline is not None:
        events.append(
            await block_event(
                run, "stockout_timeline", timeline[0], title="Stock-out timeline", prov=Provenance.SIMULATED,
                engine=ENGINE_OPT, key=("timeline", version), inputs_hash=impact_hash, placement="canvas",
                data=timeline[1],
            )
        )
    return events


def make_compose(repair_agent: KeelsonLlmAgent) -> FunctionNode:
    async def compose(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
        run = current_run()
        repo = run.deps.repo
        plan = await repo.latest_plan(tenant=run.tenant, case_id=run.case_id)
        record = await repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)
        if plan is None or record is None:
            raise LookupError("compose needs a saved plan and scenario")
        options: list[dict[str, Any]] = list(plan["options"] or [])
        ids = {str(o["id"]) for o in options}
        rec = Recommendation.model_validate(fill_nullable(Recommendation, input_dict(node_input)))
        banned = set(ctx.state.get(BANNED_OPTIONS_KEY) or [])
        allowed_ids = ids - banned
        chosen = rec.recommended_option_id if rec.recommended_option_id in allowed_ids else str(plan["recommended_option_id"])
        ctx_kpi = _kpi_context(options, chosen)
        findings = [Findings.model_validate(fill_nullable(Findings, f)) for f in ctx.state.get(FINDINGS_STATE_KEY) or []]

        async def check(candidate: Recommendation) -> tuple[ComposeContext, set[str], dict[str, list[str]]]:
            texts = _rec_texts(candidate)
            evidence_rows = await repo.get_evidence(tenant=run.tenant, ids=_ev_refs(texts))
            # this case's rows only; untrusted text rows resolve to their numbers only (never text)
            shown = display_results(evidence_rows, case_id=run.case_id)
            compose_ctx = ComposeContext(kpis=ctx_kpi.kpis, default_option=chosen, evidence=shown)
            allowed = _allowed_tokens(
                run, record.scenario, options, list(shown.values()) + [f.model_dump() for f in findings]
            )
            by_key: dict[str, list[str]] = {}
            for key, text in _keyed_texts(candidate).items():
                found = check_text(text, compose_ctx, allowed)[1]
                if found:
                    by_key[key] = found
            return compose_ctx, allowed, by_key

        compose_ctx, allowed, by_key = await check(rec)
        violations = [v for found in by_key.values() for v in found]
        if repair_needed(by_key):
            log.info("numbers rule violations; one repair turn", violations=violations[:10])
            repair_input = json.dumps(
                {
                    "previous_answer": rec.model_dump(mode="json"),
                    "violations": violations[:20],
                    "instruction": "Rewrite the answer without these violations. Use only the placeholders "
                    "listed for each option; never write digits.",
                    "options": [
                        {"id": o["id"], "title": o["title"], "kpi_placeholders": kpi_placeholders(o)}
                        for o in options if str(o["id"]) in allowed_ids
                    ],
                    "kpi_meanings": kpi_meanings(options),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            repaired = await ctx.run_node(repair_agent, node_input=repair_input, run_id="repair")
            rec = Recommendation.model_validate(fill_nullable(Recommendation, repaired if isinstance(repaired, Mapping) else {}))
            if rec.recommended_option_id in allowed_ids:
                chosen = rec.recommended_option_id
                ctx_kpi = _kpi_context(options, chosen)
            compose_ctx, allowed, by_key = await check(rec)
            violations = [v for found in by_key.values() for v in found]
            if violations:
                log.warning("numbers rule violations after repair; stripping", violations=violations[:10])
        elif violations:
            # token diet: a slip in a secondary text (why not / risks / next steps) is stripped
            # deterministically; a repair turn (~20-30 s of reasoning) is kept for the core answer
            log.info("numbers rule violations in secondary texts; stripping without a repair turn",
                     violations=violations[:10], keys=sorted(by_key))

        filled: dict[str, str] = {}
        stripped = 0
        for key, raw in _keyed_texts(rec).items():
            text, dropped = strip_unverified(raw, compose_ctx, allowed)
            stripped += dropped
            if text:
                filled[key] = text
        titles = {str(o["id"]): str(o["title"]) for o in options}
        markdown = _narrative(rec, filled, titles, stripped)
        advisory = advisory_from_kpis(plan.get("kpis"))
        if advisory is not None:
            markdown = f"{markdown}\n\n{ADVISORY_SENTENCE}" if markdown else ADVISORY_SENTENCE
        version = int(plan["version"])
        option = next(o for o in options if str(o["id"]) == chosen)
        kpi_values = option_kpis(option)
        cost_key = "incremental_cost_cents" if "incremental_cost_cents" in kpi_values else "transfer_cost_cents"
        # every KPI of the chosen option the kept texts cite is shown on its card too, so each number
        # of the narrative can be traced on screen (the exposure is already on the KPI strip)
        cited = cited_kpi_keys([_keyed_texts(rec)[k] for k in filled], chosen)
        kpi_keys = tuple(dict.fromkeys(
            ("units_recovered", "net_benefit_cents", cost_key, "units_at_risk",
             *(k for k in cited if k in KPI_LABELS and not k.startswith("exposure_")))
        ))[:MAX_OPTION_KPIS]
        actionable = bool(option.get("actions")) and not option.get("fallback")
        what_if = record.scenario.mode == "what_if" or run.case_mode == "what_if"
        unconfirmed = unconfirmed_values(record.scenario, run.spec.vendor_name)
        apply_reason = None
        if what_if:
            apply_reason = "What-if case: promote it to an actual case first."
        elif unconfirmed:
            apply_reason = "Confirm before applying: " + ", ".join(u.label for u in unconfirmed) + "."
        elif not actionable:
            apply_reason = "This option makes no changes."
        elif option.get("guided_only"):
            apply_reason = str(option.get("guided_reason") or "This option can only be followed as guided steps.")
        mix = option.get("remediation") if actionable else None
        apply_lines = int((mix or {}).get("apply_lines", len(option.get("actions") or [])))
        guide_lines = int((mix or {}).get("guide_lines", 0))
        if apply_reason is None and apply_lines == 0:
            apply_reason = "None of these changes can be applied automatically; use the guided steps."
        guide_reason = None
        if what_if:
            guide_reason = "What-if case: promote it to an actual case first."
        elif unconfirmed:
            guide_reason = "Confirm before acting: " + ", ".join(u.label for u in unconfirmed) + "."
        elif not actionable:
            guide_reason = "This option makes no changes."
        reasons = {r.option_id: filled.get(f"why{i}") for i, r in enumerate(rec.why_not_others)}
        props = RecommendedOptionProps(
            plan_version=version,
            option=OptionSummary(
                id=chosen,
                title=titles[chosen],
                summary_bullets=[filled[f"bullet{i}"] for i in range(len(rec.bullets)) if f"bullet{i}" in filled],
                kpis=option_items_m8([
                    KpiItem(key=k, label=KPI_LABELS[k], value=kpi_values[k], unit=KPI_UNITS[k],  # type: ignore[arg-type]
                            prov=Provenance.SIMULATED, preliminary=False)
                    for k in kpi_keys
                    if k in kpi_values
                ], option),
            ),
            alternatives=[
                AlternativeOption(
                    id=str(o["id"]),
                    title=str(o["title"]),
                    one_liner=reasons.get(str(o["id"])) or ("No changes; the exposure remains." if o["id"] == DO_NOTHING_ID else ""),
                )
                for o in options
                if str(o["id"]) != chosen
            ],
            actions=[
                OptionAction(type="apply",
                             label=(f"Apply {apply_lines} change{'s' if apply_lines != 1 else ''} for me"
                                    if guide_lines and apply_lines else "Apply changes for me"),
                             enabled=apply_reason is None, disabled_reason=apply_reason),
                OptionAction(type="guide",
                             label=(f"Guide me through {guide_lines}" if guide_lines and apply_lines else "Guide me"),
                             enabled=guide_reason is None, disabled_reason=guide_reason),
                *([OptionAction(type="promote", label="Promote to actual case", enabled=True)] if what_if else []),
                *([OptionAction(type="confirm_assumptions", label="Confirm these values", enabled=True)]
                  if unconfirmed else []),
            ],
            unconfirmed=unconfirmed,
            what_if=what_if,
            remediation_mix=RemediationMix(apply_lines=apply_lines, guide_lines=guide_lines)
            if actionable and (apply_lines + guide_lines) > 0 else None,
            advisory=advisory,
        )
        problem_id = str(ctx.state.get(PROBLEM_STATE_KEY) or "")
        problem = await load_problem(run, problem_id)
        impact_data = await _impact_result(run, ctx)
        for event in await _visual_blocks(
            run, problem=problem, problem_id=problem_id, impact_data=impact_data, scenario=record.scenario,
            options=options, chosen=chosen, version=version, recommended=props, option=option, advisory=advisory,
        ):
            yield event
        if option.get("actions"):
            yield await block_event(
                run, "plan_diff",
                plan_diff_props(plan_version=version, actions=option["actions"], vendor=run.spec.vendor_name, problem=problem),
                title=f"Changes in {run.spec.vendor_name}", prov=Provenance.SIMULATED, engine=ENGINE_COMPOSE,
                key=("plan_diff", version, chosen), placement="canvas",
            )
        evidence_ids = list(ctx.state.get(EVIDENCE_STATE_KEY) or [])
        evidence_ids += [c.evidence_id for f in findings for c in f.claims if c.evidence_id]
        items = await evidence_items(run, evidence_ids)
        if items:
            yield await block_event(
                run, "evidence_pack", EvidencePackProps(items=items), title="Evidence", prov=Provenance.FACT,
                engine=ENGINE_COMPOSE, key=("evidence", version), placement="canvas",
            )
        changed = what_changed_line(ctx.state.get(PREVIOUS_PLAN_KEY), plan_summary(plan),
                                    list(ctx.state.get(WHAT_CHANGED_KEY) or []))
        yield await block_event(
            run, "narrative", NarrativeProps(markdown=markdown, what_changed=changed, advisory=advisory),
            title="Recommendation (advisory)" if advisory is not None else "Recommendation",
            prov=Provenance.INFERENCE, engine=ENGINE_COMPOSE, key=("narrative", run.run_id, "compose"),
        )
        yield await say(run, "compose", markdown)
        yield {"plan_version": version, "recommended_option_id": chosen, "unverified": stripped}

    return FunctionNode(func=compose, name="compose", rerun_on_resume=True)
