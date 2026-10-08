"""M8 plan options: CO₂e, robustness bands and weight profiles in the engine
(``docs/contracts/126-m8-co2-robustness.md`` §4–§6).

* :class:`OptimizeInputs` builds every ``/v1/optimize`` request of a plan version (``simulate``
  and the M5 re-plan share it): the ``ProblemData`` enriched for CO₂e (:mod:`.emissions`, computed
  once per plan), ``emissions`` (the tenant's factor set) and ``robustness``
  (:func:`robustness_spec`; one seed for every option, so the options are compared in the same
  sampled futures). With both features off the request is exactly the M7 one.
* :func:`robustness_spec` samples the perturbations' own p10 / p90, the taxonomy's duration band of
  a windowed event whose duration is an assumption (``window_bands``: the Scenario contract and
  the planner prompt stay unchanged), transit noise by mode and a demand factor per (cluster,
  style).
* :func:`default_option_score` / :func:`rank_key` rank options by the tenant's default weight
  profile (``planning.scoring``); under the built-in ``net-benefit`` profile that is the net
  benefit, i.e. the M3 rule.
* :func:`win_rates` — share of the sampled futures in which each option has the highest net
  benefit (do nothing wins a future in which no option gains; ties go to the earlier candidate),
  only when every option was evaluated in the same futures (equal ``crn_key``).
* :func:`decorate_compare`, :func:`band_kpi_items`, :func:`co2_item` — the ``scenario_compare@1``
  profiles / scores / win rates / notes and the KPI bands and CO₂e tile. Numbers only come from the
  optimizer responses (no LLM ever sees them: the Strategist brief and its KPI catalog are
  unchanged, so recorded replays stay valid).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from keelson_contracts.blocks import (
    CompareProfile,
    EmissionsNote,
    KpiBand,
    KpiItem,
    ProfileScore,
    ScenarioCompareProps,
    UncertaintyNote,
)
from keelson_contracts.common import Provenance
from keelson_contracts.optimizer import (
    Band,
    OptimizeRequest,
    PlanSpec,
    PlanWeights,
    ProblemData,
    RobustnessSpec,
    Scorecard,
    SolverOptions,
    WindowBand,
)
from keelson_contracts.scoring import ScoringConfig, profile_score, rank_options

from .emissions import FactorSet, enrich_for_emissions, factor_set
from .geo import GeoCatalog

__all__ = [
    "CO2_KEY",
    "CO2_LABEL",
    "OptimizeInputs",
    "band_kpi_items",
    "co2_item",
    "decorate_compare",
    "default_option_score",
    "plan_weights",
    "rank_key",
    "robustness_spec",
    "win_rates",
]

log = structlog.get_logger(__name__)

CO2_KEY = "incremental_co2e_kg"
CO2_LABEL = "Extra CO₂e of the plan"
MAX_WINDOW_DAYS = 366
_DO_NOTHING = "do-nothing"
_WEAK = ("assumption", "inference")


# ───────────────────────────── requests ─────────────────────────────


def _prov(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _duration_assumed(scenario: Any) -> bool:
    return any(a.key == "duration_days" and _prov(a.prov) == "assumption"
               for a in getattr(scenario, "assumptions", None) or ())


def _duration_prior(scenario: Any) -> Any:
    from .nodes.taxonomy import TaxonomyError, get_taxonomy  # noqa: PLC0415 - nodes import this module

    try:
        return get_taxonomy().template(str(getattr(scenario, "event_type", ""))).duration_days
    except TaxonomyError:
        return None


def robustness_spec(planning: Any, scenario: Any, perturbations: Sequence[Any]) -> RobustnessSpec | None:
    """The request's ``robustness`` (``None`` when off). ``perturbations`` are the ones the request
    sends (window bands point at their indexes)."""
    cfg = planning.robustness
    if not cfg.enabled:
        return None
    windows: list[WindowBand] = []
    prior = _duration_prior(scenario) if cfg.duration_bands and _duration_assumed(scenario) else None
    if prior is not None:
        for i, p in enumerate(perturbations):
            window = getattr(p, "window", None)
            if window is None or window.end is None or _prov(getattr(p, "prov", "")) not in _WEAK:
                continue
            length = (window.end - window.start).days + 1
            if length > MAX_WINDOW_DAYS:
                continue
            windows.append(WindowBand(perturbation=i, duration_days=Band(
                p10=min(prior.p10, length), p50=length, p90=min(MAX_WINDOW_DAYS, max(prior.p90, length)))))
    return RobustnessSpec(
        samples=cfg.samples,
        seed=cfg.seed if cfg.seed is not None else planning.solver_seed,
        value_bands=cfg.value_bands,
        window_bands=windows,
        lead_time_noise=list(cfg.lead_time_noise),
        demand_noise_bp=cfg.demand_noise_bp,
    )


def plan_weights(planning: Any) -> PlanWeights:
    """``PlanSpec.weights`` from the default weight profile (10000 / 10000 = the M3 objective)."""
    default = planning.scoring.default
    return PlanWeights(lost_margin=default.lost_margin_bp, transfer_cost=default.cost_bp)


@dataclass(frozen=True)
class OptimizeInputs:
    """Everything the optimize requests of one plan version share."""

    problem: ProblemData
    perturbations: tuple[Any, ...]
    solver: SolverOptions
    factors: FactorSet | None
    robustness: RobustnessSpec | None

    @classmethod
    def build(cls, planning: Any, scenario: Any, problem: ProblemData, perturbations: Sequence[Any],
              geo: GeoCatalog) -> OptimizeInputs:
        enriched = enrich_for_emissions(problem, planning, geo)
        data, factors = (problem, None) if enriched is None else enriched
        return cls(
            problem=data,
            perturbations=tuple(perturbations),
            solver=SolverOptions(time_limit_s=planning.solver_time_limit_s, seed=planning.solver_seed),
            factors=factors,
            robustness=robustness_spec(planning, scenario, perturbations),
        )

    def request(self, spec: PlanSpec) -> OptimizeRequest:
        extra: dict[str, Any] = {}
        if self.factors is not None:
            extra["emissions"] = self.factors.spec()
        if self.robustness is not None:
            extra["robustness"] = self.robustness
        return OptimizeRequest(data=self.problem, perturbations=list(self.perturbations), plan_spec=spec,
                               solver=self.solver, **extra)


# ───────────────────────────── scoring / win rates ─────────────────────────────


def _card(option: Mapping[str, Any]) -> Scorecard | None:
    raw = option.get("scorecard")
    return Scorecard.model_validate(raw) if raw else None


def default_option_score(option: Mapping[str, Any], scoring: ScoringConfig) -> int:
    """The option's score under the default profile (the net benefit when it has no scorecard)."""
    card = _card(option)
    if card is None:
        return int((option.get("kpi_delta") or {}).get("net_benefit_cents") or 0)
    return profile_score(card, scoring.default)


def rank_key(scoring: ScoringConfig | None) -> Any:
    """Sort key of an improving option for the deterministic recommendation (best first)."""
    def key(option: Mapping[str, Any]) -> int:
        if scoring is None:
            return -int(option["kpi_delta"]["net_benefit_cents"])
        return -default_option_score(option, scoring)

    return key


def _largest_remainder(counts: Mapping[str, int], total: int, order: Sequence[str]) -> dict[str, int]:
    raw = {k: counts.get(k, 0) * 10_000 for k in order}
    out = {k: v // total for k, v in raw.items()}
    rest = 10_000 - sum(out.values())
    for k in sorted(order, key=lambda k: (-(raw[k] % total), order.index(k)))[:rest]:
        out[k] += 1
    return out


def win_rates(options: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    """Option id → win rate (bp, summing to 10000); ``None`` when the options were not all
    evaluated in the same sampled futures."""
    robs = [o.get("robustness") for o in options]
    if not options or any(not r for r in robs):
        return None
    keys = {(r["crn_key"], int(r["samples"])) for r in robs}  # type: ignore[index]
    if len(keys) != 1:
        return None
    n = next(iter(keys))[1]
    ids = [str(o["id"]) for o in options]
    vectors = {str(o["id"]): [int(v) for v in o["robustness"]["per_sample_net_benefit_cents"]] for o in options}
    nothing = _DO_NOTHING if _DO_NOTHING in vectors else None
    counts: dict[str, int] = dict.fromkeys(ids, 0)
    for s in range(n):
        best_id, best = nothing, 0
        for oid in ids:
            if oid == _DO_NOTHING:
                continue
            if vectors[oid][s] > best:
                best_id, best = oid, vectors[oid][s]
        if best_id is None:
            return None  # no "Do nothing" to credit a future in which nothing gains
        counts[best_id] += 1
    return _largest_remainder(counts, n, ids)


# ───────────────────────────── blocks ─────────────────────────────


def _emissions_note(options: Sequence[Mapping[str, Any]], cards: Sequence[Scorecard]) -> EmissionsNote | None:
    summaries = [o.get("emissions") for o in options if o.get("emissions")]
    if not summaries or any(c.incremental_co2e_kg is None for c in cards):
        return None
    source = str(summaries[0]["source_id"])
    fs = factor_set(source)
    return EmissionsNote(
        source_label=fs.label if fs is not None else source,
        basis=summaries[0].get("basis") or "WTW",
        indicative=True if fs is None else not fs.verified,
        coverage_bp=min(int(s["coverage_bp"]) for s in summaries),
    )


def _uncertainty_note(options: Sequence[Mapping[str, Any]]) -> UncertaintyNote | None:
    robs = [o.get("robustness") for o in options if o.get("robustness")]
    if not robs:
        return None
    sampled = [s for s in ("disruption_duration", "disruption_severity", "lead_time", "demand")
               if any(s in (r.get("sampled") or ()) for r in robs)]
    if not sampled:
        return None
    return UncertaintyNote(method="mc/1", samples=min(int(r["samples"]) for r in robs), sampled=sampled)  # type: ignore[arg-type]


def decorate_compare(props: ScenarioCompareProps, options: Sequence[Mapping[str, Any]],
                     scoring: ScoringConfig) -> ScenarioCompareProps:
    """``scenario_compare@1`` + the weight profiles, each option's scores / ranks, the win rates
    and the uncertainty / emissions notes (``options`` = the plan options of ``props``, same ids)."""
    by_id = {str(o["id"]): o for o in options}
    shown = [by_id[o.id] for o in props.options if o.id in by_id]
    cards = [(o.id, o.scorecard) for o in props.options]
    ranked = {p.id: rank_options(cards, p) for p in scoring.profiles}
    wins = win_rates(shown) if len(shown) == len(props.options) else None
    data = props.model_dump(mode="json")
    for o in data["options"]:
        o["scores"] = [{"profile_id": pid, "score_cents": r[o["id"]][0], "rank": r[o["id"]][1]}
                       for pid, r in ranked.items()]
        if wins is not None:
            o["win_rate_bp"] = wins[o["id"]]
    data["profiles"] = [
        CompareProfile(id=p.id, label=p.label, default=p.id == scoring.default_profile, lost_margin_bp=p.lost_margin_bp,
                       cost_bp=p.cost_bp, carbon_price_cents_per_t=p.carbon_price_cents_per_t,
                       risk_aversion_bp=p.risk_aversion_bp).model_dump(mode="json")
        for p in scoring.profiles
    ]
    note = _uncertainty_note(shown)
    if note is not None:
        data["uncertainty"] = note.model_dump(mode="json")
    emissions = _emissions_note(shown, [o.scorecard for o in props.options])
    if emissions is not None:
        data["emissions"] = emissions.model_dump(mode="json")
    return ScenarioCompareProps.model_validate(data)


_BAND_OF: dict[str, tuple[str, int]] = {
    "fill_rate_bp": ("fill_rate_bp", 1),
    "net_benefit_cents": ("net_benefit_cents", 1),
    "units_at_risk": ("lost_units", 1),
    "margin_at_risk_cents": ("lost_margin_cents", 1),
    "shortage_days": ("shortage_days", 1),
    "shortage_cluster_weeks": ("shortage_days", 7),
}
"""KPI key → (scorecard band, divisor): short cluster-weeks are shortage days / 7."""


def _band(card: Mapping[str, Any] | None, key: str) -> KpiBand | None:
    bands = (card or {}).get("bands")
    hit = _BAND_OF.get(key)
    if not bands or hit is None:
        return None
    name, div = hit
    b = bands.get(name)
    if not b:
        return None
    return KpiBand(p10=int(b["p10"]) // div, p90=int(b["p90"]) // div, samples=int(bands["samples"]))


def band_kpi_items(items: Sequence[KpiItem], option: Mapping[str, Any], *,
                   exposure: Mapping[str, Any] | None = None,
                   exposure_keys: frozenset[str] = frozenset()) -> list[KpiItem]:
    """``items`` with p10–p90 bands: the ``exposure_keys`` from ``exposure`` (the do-nothing option:
    what happens if nothing is done), every other banded key from ``option``."""
    out: list[KpiItem] = []
    for item in items:
        source = exposure if item.key in exposure_keys else option
        band = _band((source or {}).get("scorecard"), item.key) if source is not None else None
        out.append(item.model_copy(update={"band": band}) if band is not None else item)
    return out


def co2_item(option: Mapping[str, Any]) -> KpiItem | None:
    """The "Extra CO₂e of the plan" tile (``None`` when the option carries no CO₂e)."""
    value = (option.get("scorecard") or {}).get(CO2_KEY)
    if value is None:
        return None
    return KpiItem(key=CO2_KEY, label=CO2_LABEL, value=int(value), unit="kg_co2e", prov=Provenance.SIMULATED,
                   preliminary=False)
