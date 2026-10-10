"""M8 robustness, CO₂e and weight profiles in the engine (``126-m8-co2-robustness.md`` §4–§6):
the robustness spec (window bands only for an assumed duration, one seed for every option), the
shared optimize requests (M7-identical when both features are off), LP weights from the default
profile, win rates over common random numbers, ranking (the default profile reproduces the M3
recommendation), the compare / KPI decorations, replay safety of the Strategist brief, the numbers
rule for CO₂ words, and the whole planning flow with an optimizer that answers bands and CO₂e."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from typing import Any

import pytest
from fakes.harness import Harness
from fakes.scripts import chat_flow_script
from keelson_contracts.blocks import KpiItem, ScenarioCompareProps, parse_block
from keelson_contracts.common import Provenance
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse, PlanSpec, PlanWeights, Scorecard
from keelson_contracts.scenario import ComplianceBlock, CostScale, DisruptionScenario
from keelson_contracts.scoring import DEFAULT_SCORING, ScoringConfig, WeightProfile
from test_m3_visuals import options as m3_options
from test_m3_visuals import problem

from keelson_engine.runtime.candidates import build_candidates
from keelson_engine.runtime.compose import ComposeContext, check_text, format_value, numeric_lint
from keelson_engine.runtime.geo import get_geo
from keelson_engine.runtime.nodes import planning as pl
from keelson_engine.runtime.robustness import (
    CO2_KEY,
    OptimizeInputs,
    band_kpi_items,
    co2_item,
    decorate_compare,
    robustness_spec,
    win_rates,
)
from keelson_engine.runtime.spec import AgentSpec, PlanningDefaults
from keelson_engine.runtime.visuals import scenario_compare_props

WINDOW = {"start": "2026-10-06", "end": "2026-10-12"}  # 7 days


def closure_scenario(*, duration_prov: str = "assumption", perturbation_prov: str = "assumption",
                     event_type: str = "LOG.port_closure") -> DisruptionScenario:
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": "actual", "event_family": event_type.split(".")[0],
        "event_type": event_type, "description": "Port closed",
        "perturbations": [
            {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-1#1"]}, "days": 7, "p10": 5,
             "p90": 14, "prov": "assumption"},
            {"op": "capacity_scale", "target": {"kinds": ["port"], "ids": ["VNSGN"]}, "factor_bp": 0,
             "window": WINDOW, "prov": perturbation_prov},
            {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["SA-UK"]}, "factor_bp": 3000,
             "window": WINDOW, "prov": "fact"},
        ],
        "matched": [],
        "assumptions": [{"key": "duration_days", "label": "Duration", "value_text": "7 days", "prov": duration_prov}],
        "config_version": "default/p-1",
    })


# ───────────────────────────── robustness spec / requests ─────────────────────────────


def test_robustness_spec_bands_an_assumed_duration_only() -> None:
    planning = PlanningDefaults()
    scn = closure_scenario()
    spec = robustness_spec(planning, scn, scn.perturbations)
    assert spec is not None
    assert spec.seed == planning.solver_seed == 7 and spec.samples == 64 and spec.value_bands
    # LOG.port_closure duration prior 3 / 7 / 14 around the 7-day window; the fact perturbation keeps its window
    assert [(w.perturbation, w.duration_days.model_dump()) for w in spec.window_bands] == [
        (1, {"p10": 3, "p50": 7, "p90": 14})]
    assert [n.mode for n in spec.lead_time_noise] == ["sea", "sea_air", "air", "road", "default"]
    assert spec.demand_noise_bp is not None and spec.demand_noise_bp.p50 == 10_000
    stated = closure_scenario(duration_prov="decision", perturbation_prov="decision")
    assert robustness_spec(planning, stated, stated.perturbations).window_bands == []  # type: ignore[union-attr]
    no_prior = closure_scenario(event_type="SUP.delay")
    assert robustness_spec(planning, no_prior, no_prior.perturbations).window_bands == []  # type: ignore[union-attr]


def test_window_band_recentred_on_a_longer_window() -> None:
    scn = closure_scenario()
    p = scn.perturbations[1].model_copy(update={"window": scn.perturbations[1].window.model_copy(  # type: ignore[union-attr]
        update={"end": dt.date(2026, 11, 4)})})  # 30 days
    spec = robustness_spec(PlanningDefaults(), scn, [scn.perturbations[0], p])
    assert spec is not None and [w.duration_days.model_dump() for w in spec.window_bands] == [
        {"p10": 3, "p50": 30, "p90": 30}]


def test_window_bands_only_for_ops_the_optimizer_samples() -> None:
    """Review fix: the optimizer answers 422 for a window band on an op it does not sample (e.g. a
    windowed compliance block or cost scale), which would fail every option of the plan."""
    scn = closure_scenario()
    block = ComplianceBlock.model_validate({
        "op": "compliance_block", "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]}, "reason": "audit",
        "window": WINDOW, "prov": "assumption"})
    cost = CostScale.model_validate({**scn.perturbations[1].model_dump(mode="json"), "op": "cost_scale",
                                     "factor_bp": 12_000})
    spec = robustness_spec(PlanningDefaults(), scn, [block, cost, *scn.perturbations])
    assert spec is not None and [w.perturbation for w in spec.window_bands] == [3]  # the VN closure only


def test_window_bands_are_capped_at_the_contract_limit() -> None:
    """Review fix: ``RobustnessSpec.window_bands`` holds at most 64 entries; a scenario with more
    windowed assumptions (an unbounded list) must not fail the whole plan — the first 64 are sampled."""
    scn = closure_scenario()
    closure = scn.perturbations[1]
    many = [closure.model_copy(update={"target": closure.target.model_copy(update={"ids": [f"PORT{i:02d}"]})})
            for i in range(70)]
    spec = robustness_spec(PlanningDefaults(), scn, many)
    assert spec is not None and [w.perturbation for w in spec.window_bands] == list(range(64))


def test_robustness_settings() -> None:
    off = PlanningDefaults(robustness={"enabled": False})
    assert robustness_spec(off, closure_scenario(), closure_scenario().perturbations) is None
    custom = PlanningDefaults(robustness={"samples": 128, "seed": 42, "duration_bands": False, "demand_noise_bp": None,
                                          "lead_time_noise": []})
    spec = robustness_spec(custom, closure_scenario(), closure_scenario().perturbations)
    assert spec is not None and (spec.samples, spec.seed, spec.window_bands, spec.demand_noise_bp) == (128, 42, [], None)
    for bad in ({"samples": 8}, {"samples": 257}, {"seed": -1},
                {"lead_time_noise": [{"mode": "sea", "days": {"p10": 0, "p50": 0, "p90": 1}}] * 2},
                {"demand_noise_bp": {"p10": -5, "p50": 10000, "p90": 11000}}):
        with pytest.raises(ValueError):
            PlanningDefaults(robustness=bad)


def test_requests_carry_co2e_inputs_and_one_seed_for_every_option() -> None:
    p, scn = problem(), closure_scenario()
    inputs = OptimizeInputs.build(PlanningDefaults(), scn, p, scn.perturbations, get_geo())
    reqs = [inputs.request(PlanSpec(id=i, title=i, levers=[])) for i in ("opt-A", "do-nothing")]
    for req in reqs:
        assert req.emissions is not None and req.emissions.source_id == "glec-v3-indicative"
        assert req.robustness is not None and req.robustness.seed == 7
        assert all(s.unit_weight_g for s in req.data.skus) and all(il.legs for il in req.data.inbound_lanes)
    assert reqs[0].model_dump(exclude={"plan_spec"}) == reqs[1].model_dump(exclude={"plan_spec"})
    # round trip through the wire model the optimizer validates
    OptimizeRequest.model_validate(reqs[0].model_dump(mode="json"))


def test_requests_without_m8_features_are_the_m7_requests() -> None:
    p, scn = problem(), closure_scenario()
    planning = PlanningDefaults(emissions={"enabled": False}, robustness={"enabled": False})
    inputs = OptimizeInputs.build(planning, scn, p, scn.perturbations, get_geo())
    spec = PlanSpec(id="opt-A", title="A", levers=[])
    m7 = OptimizeRequest(data=p, perturbations=list(scn.perturbations), plan_spec=spec,
                         solver={"time_limit_s": planning.solver_time_limit_s, "seed": planning.solver_seed})
    assert json.dumps(inputs.request(spec).model_dump(mode="json"), sort_keys=True) == json.dumps(
        m7.model_dump(mode="json"), sort_keys=True)


def test_solver_options_flow_through_the_shared_request_builder() -> None:
    """Integration seam of 125 + 126: planning and re-plan build every request with OptimizeInputs, so the
    tenant's integer stage must reach it next to the CO2e inputs and the robustness spec."""
    p, scn = problem(), closure_scenario()
    spec = PlanSpec(id="opt-A", title="A", levers=[])
    cpsat = PlanningDefaults(solver_integer_stage="cpsat")
    req = OptimizeInputs.build(cpsat, scn, p, scn.perturbations, get_geo(), solver=pl.solver_options(cpsat)).request(spec)
    assert req.solver.integer_stage == "cpsat"
    assert req.emissions is not None and req.robustness is not None
    default = PlanningDefaults()
    req = OptimizeInputs.build(default, scn, p, scn.perturbations, get_geo(), solver=pl.solver_options(default)).request(spec)
    assert "integer_stage" not in req.solver.model_dump(mode="json")  # repair: omitted, as in M7


def test_lp_weights_follow_the_default_profile() -> None:
    from test_m3_candidates import caps

    default = build_candidates(PlanningDefaults(), caps())
    assert default and all(c.plan_spec.weights == PlanWeights() for c in default)
    scoring = ScoringConfig(default_profile="cost-first", profiles=DEFAULT_SCORING.profiles)
    cost_first = build_candidates(PlanningDefaults(scoring=scoring), caps())
    assert all(c.plan_spec.weights == PlanWeights(lost_margin=10_000, transfer_cost=15_000) for c in cost_first)


# ───────────────────────────── win rates / ranking ─────────────────────────────


def _rob(vector: list[int], key: str = "a" * 64) -> dict[str, Any]:
    return {"method": "mc/1", "seed": 7, "samples": len(vector), "requested_samples": len(vector), "sampled": ["demand"],
            "crn_key": key, "per_sample_net_benefit_cents": vector, "per_sample_lost_margin_cents": [0] * len(vector)}


def test_win_rates_sum_to_10000_with_tie_rules() -> None:
    opts = [
        {"id": "opt-A", "robustness": _rob([5, 1, 0, 3, -1, 2, 2])},
        {"id": "opt-B", "robustness": _rob([4, 2, 0, 3, -2, 2, 1])},
        {"id": "do-nothing", "robustness": _rob([0] * 7)},
    ]
    rates = win_rates(opts)
    # A wins s0, s3 (tie, earlier candidate), s5 (tie), s6; B wins s1; do nothing wins s2 and s4
    assert rates == {"opt-A": 5714, "opt-B": 1429, "do-nothing": 2857}
    assert sum(rates.values()) == 10_000  # type: ignore[union-attr]
    assert win_rates([*opts[:2], {"id": "do-nothing", "robustness": _rob([0] * 7, key="b" * 64)}]) is None
    assert win_rates([opts[0], {"id": "do-nothing"}]) is None
    assert win_rates(opts[:2]) is None  # no "Do nothing" to credit the futures nobody wins
    # review fix (126 §5): without "Do nothing" there are no win rates, even when every future has a winner
    assert win_rates([{"id": "opt-A", "robustness": _rob([5, 1])}, {"id": "opt-B", "robustness": _rob([4, 2])}]) is None


def test_default_profile_keeps_the_m3_recommendation() -> None:
    opts = m3_options()
    for o in opts:
        o["scorecard"] = {**o["scorecard"], "net_benefit_cents": o["kpi_delta"]["net_benefit_cents"]}
    assert pl.default_recommendation(opts) == pl.default_recommendation(opts, DEFAULT_SCORING) == "opt-A"
    # a cost-heavy profile prefers the cheaper of two close options
    pricey = copy.deepcopy(opts)
    for o in pricey:
        if o["id"] == "opt-A":
            o["kpi_delta"]["net_benefit_cents"] = o["scorecard"]["net_benefit_cents"] = 300_000
            o["scorecard"]["incremental_cost_cents"] = o["scorecard"]["cost_breakdown"]["transfer"] = 200_000
        if o["id"] == "opt-C":
            o["kpi_delta"]["net_benefit_cents"] = o["scorecard"]["net_benefit_cents"] = 290_000
    assert pl.default_recommendation(pricey, DEFAULT_SCORING) == "opt-A"
    stingy = ScoringConfig(default_profile="stingy", profiles=(WeightProfile(id="stingy", label="Stingy", cost_bp=30_000),))
    assert pl.default_recommendation(pricey, stingy) == "opt-C"


# ───────────────────────────── blocks ─────────────────────────────


def _bands(net: int, lost: int) -> dict[str, Any]:
    return {"samples": 64, "fill_rate_bp": {"p10": 9000, "p50": 9400, "p90": 9800},
            "lost_units": {"p10": lost // 2, "p50": lost, "p90": lost * 2},
            "lost_margin_cents": {"p10": lost * 1000, "p50": lost * 2000, "p90": lost * 4000},
            "net_benefit_cents": {"p10": net - 50_000, "p50": net, "p90": net + 20_000},
            "shortage_days": {"p10": 7, "p50": 14, "p90": 35}, "stockout_chance_bp": 8_000}


def m8_options() -> list[dict[str, Any]]:
    opts = m3_options()
    vectors = {"opt-A": [300_000, 250_000, -10, 310_000], "opt-B": [290_000, 400_000, -5, 320_000],
               "opt-C": [100_000, 50_000, -20, 90_000], "do-nothing": [0, 0, 0, 0]}
    for i, o in enumerate(opts):
        net = o["kpi_delta"]["net_benefit_cents"]
        co2 = [900, 26_000, -2_200, 0][i]
        o["scorecard"] = {**o["scorecard"], "net_benefit_cents": net, "bands": _bands(net, o["scorecard"]["lost_units"]),
                          "incremental_co2e_kg": co2,
                          "co2e_breakdown": {"transfer_kg": co2, "expedite_kg": 0, "nearshore_kg": 0, "rebook_kg": 0}}
        o["robustness"] = _rob(vectors[o["id"]])
        o["emissions"] = {"source_id": "glec-v3-indicative", "basis": "WTW", "coverage_bp": [10_000, 9_000, 10_000,
                                                                                            10_000][i],
                          "baseline_inbound_kg": 1000, "uncovered": []}
    return opts


def test_compare_props_carry_profiles_scores_win_rates_and_notes() -> None:
    opts = m8_options()
    props = decorate_compare(scenario_compare_props(opts, recommended_id="opt-A"), opts, DEFAULT_SCORING)
    assert [p.id for p in props.profiles] == ["net-benefit", "cost-first", "service-first", "low-carbon"]
    assert [p.default for p in props.profiles] == [True, False, False, False]
    for o in props.options:
        assert [s.profile_id for s in o.scores] == [p.id for p in props.profiles]
        net = next(s for s in o.scores if s.profile_id == "net-benefit")
        assert net.score_cents == o.scorecard.net_benefit_cents
    ranks = {o.id: next(s.rank for s in o.scores if s.profile_id == "net-benefit") for o in props.options}
    assert ranks == {"opt-B": 1, "opt-A": 2, "opt-C": 3, "do-nothing": 4}
    low = {o.id: next(s.rank for s in o.scores if s.profile_id == "low-carbon") for o in props.options}
    assert low["opt-B"] > ranks["opt-B"]  # flying costs it under the carbon price
    assert {o.id: o.win_rate_bp for o in props.options} == {"opt-A": 2500, "opt-B": 5000, "opt-C": 0,
                                                            "do-nothing": 2500}
    assert props.uncertainty is not None and props.uncertainty.samples == 4 and props.uncertainty.sampled == ["demand"]
    assert props.emissions is not None and props.emissions.indicative and props.emissions.coverage_bp == 9_000
    co2_axis = ScenarioCompareProps.model_validate({**props.model_dump(mode="json"),
                                                    "axes": {"x": "incremental_co2e_kg", "y": "lost_units"}})
    assert co2_axis.axes.x == "incremental_co2e_kg"


def test_compare_without_m8_data_only_adds_profiles_and_scores() -> None:
    opts = m3_options()
    props = decorate_compare(scenario_compare_props(opts, recommended_id="opt-A"), opts, DEFAULT_SCORING)
    assert props.uncertainty is None and props.emissions is None
    assert all(o.win_rate_bp is None for o in props.options)
    mixed = m8_options()
    mixed[1].pop("robustness")
    mixed[2]["scorecard"].pop("incremental_co2e_kg")
    mixed[2]["scorecard"].pop("co2e_breakdown")
    props = decorate_compare(scenario_compare_props(mixed, recommended_id="opt-A"), mixed, DEFAULT_SCORING)
    assert all(o.win_rate_bp is None for o in props.options) and props.emissions is None


def test_kpi_items_get_bands_and_the_co2e_tile() -> None:
    opts = m8_options()
    option, nothing = opts[0], opts[3]
    exposure = [KpiItem(key="units_at_risk", label="Units at risk", value=400, unit="units", prov=Provenance.SIMULATED,
                        preliminary=False),
                KpiItem(key="shortage_cluster_weeks", label="Short", value=10, unit="count", prov=Provenance.SIMULATED,
                        preliminary=False),
                KpiItem(key="net_benefit_cents", label="Net benefit", value=300_000, unit="eur_cents",
                        prov=Provenance.SIMULATED, preliminary=False),
                KpiItem(key="units_recovered", label="Recovered", value=200, unit="units", prov=Provenance.SIMULATED,
                        preliminary=False)]
    items = pl.strip_items_m8(exposure, option, opts)
    by_key = {i.key: i for i in items}
    assert by_key["units_at_risk"].band.model_dump() == {"p10": 200, "p90": 800, "samples": 64}  # do nothing's
    assert by_key["shortage_cluster_weeks"].band.model_dump() == {"p10": 1, "p90": 5, "samples": 64}
    assert by_key["net_benefit_cents"].band.model_dump() == {"p10": 250_000, "p90": 320_000, "samples": 64}
    assert by_key["units_recovered"].band is None
    co2 = by_key[CO2_KEY]
    assert (co2.value, co2.unit, co2.label) == (900, "kg_co2e", "Extra CO₂e of the plan")
    card = pl.option_items_m8([i for i in exposure if i.key != "shortage_cluster_weeks"], option)
    assert {i.key: i.band for i in card}["units_at_risk"].model_dump() == {"p10": 100, "p90": 400, "samples": 64}
    assert card[-1].key == CO2_KEY
    assert co2_item(m3_options()[0]) is None
    assert band_kpi_items(exposure, m3_options()[0]) == exposure
    full = [exposure[0].model_copy(update={"key": f"k{i}"}) for i in range(pl.MAX_OPTION_KPIS)]
    assert len(pl.option_items_m8(full, option)) == pl.MAX_OPTION_KPIS


# ───────────────────────────── replay safety / numbers rule ─────────────────────────────


def test_strategist_brief_and_kpi_catalog_are_unchanged_by_m8_data() -> None:
    """Recorded replays key on the exact prompt: bands and CO2e must never reach it."""
    scn = closure_scenario()
    plain, rich = m3_options(), m8_options()
    for o, r in zip(plain, rich, strict=True):
        o["scorecard"]["net_benefit_cents"] = r["scorecard"]["net_benefit_cents"]
        o["fallback"] = r["fallback"] = False
    assert pl._recommender_brief(scn, plain, []) == pl._recommender_brief(scn, rich, [])
    for o, r in zip(plain, rich, strict=True):
        assert pl.kpi_catalog(o) == pl.kpi_catalog(r)
        assert pl.option_kpis(o) == pl.option_kpis(r)
    assert pl.kpi_meanings(plain) == pl.kpi_meanings(rich)
    assert "co2" not in pl._recommender_brief(scn, rich, []).lower()


def test_numbers_rule_allows_co2_words_and_formats_kg() -> None:
    assert numeric_lint("Flying adds CO₂e; the CO2 and kgCO2e totals and tCO₂e figures are shown.", set()) == []
    assert numeric_lint("It adds 26 tonnes of CO2.", set()) == ["free number '26'"]
    assert format_value(26_811, "kg_co2e") == "26,811 kg CO₂e"
    assert format_value(-2_235, "kg_co2e") == "-2,235 kg CO₂e"
    filled, violations = check_text("Extra CO₂e: {{kpi:opt-A.x}}", ComposeContext(kpis={}), set())
    assert violations == ["unknown placeholder {{kpi:opt-A.x}}"] and "CO₂e" in filled


# ───────────────────────────── the planning flow ─────────────────────────────


def _m8_optimizer(h: Harness) -> None:
    """The stub answers like the M8 optimizer: CO2e by transferred unit, bands, per-sample vectors."""
    original = h.optimizer.optimize

    def optimize(req: OptimizeRequest) -> OptimizeResponse:
        resp = original(req)
        if resp.scorecard is None:
            return resp
        data = resp.model_dump(mode="json")
        card = data["scorecard"]
        if req.emissions is not None:
            co2 = 3 * sum(int(line["qty"]) for a in data["actions"] for line in a["payload"].get("lines") or [])
            card["incremental_co2e_kg"] = co2
            card["co2e_breakdown"] = {"transfer_kg": co2, "expedite_kg": 0, "nearshore_kg": 0, "rebook_kg": 0}
            data["emissions"] = {"source_id": req.emissions.source_id, "basis": "WTW", "coverage_bp": 10_000,
                                 "baseline_inbound_kg": 5_000, "uncovered": []}
        if req.robustness is not None:
            n = 16
            net = card["net_benefit_cents"]
            vector = [net + (s % 4 - 2) * 1000 if net else 0 for s in range(n)]
            ordered = sorted(vector)
            card["bands"] = {"samples": n, "fill_rate_bp": {"p10": card["fill_rate_bp"], "p50": card["fill_rate_bp"],
                                                             "p90": card["fill_rate_bp"]},
                             "lost_units": {"p10": card["lost_units"], "p50": card["lost_units"],
                                            "p90": card["lost_units"] + 10},
                             "lost_margin_cents": {"p10": card["lost_margin_cents"], "p50": card["lost_margin_cents"],
                                                   "p90": card["lost_margin_cents"] + 10_000},
                             "net_benefit_cents": {"p10": ordered[1], "p50": ordered[7], "p90": ordered[14]},
                             "shortage_days": {"p10": card["shortage_days"], "p50": card["shortage_days"],
                                               "p90": card["shortage_days"] + 7},
                             "stockout_chance_bp": 5_000}
            key = hashlib.sha256(json.dumps(req.model_dump(mode="json", include={"data", "perturbations", "robustness"}),
                                            sort_keys=True).encode()).hexdigest()
            data["robustness"] = {"method": "mc/1", "seed": req.robustness.seed, "samples": n,
                                  "requested_samples": req.robustness.samples, "sampled": ["lead_time", "demand"],
                                  "crn_key": key, "per_sample_net_benefit_cents": vector,
                                  "per_sample_lost_margin_cents": [0] * n}
        return OptimizeResponse.model_validate(data)

    h.optimizer.optimize = optimize  # type: ignore[method-assign]


async def _plan(h: Harness) -> None:
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"


async def test_planning_flow_with_bands_co2e_and_profiles(harness: Harness) -> None:
    h = harness
    _m8_optimizer(h)
    await _plan(h)
    bodies = [body for path, body in h.optimizer.requests if path == "/v1/optimize"]
    assert len(bodies) >= 2
    for body in bodies:
        assert body["robustness"]["seed"] == 7 and body["robustness"]["samples"] == 64
        assert body["emissions"]["source_id"] == "glec-v3-indicative"
        assert all("unit_weight_g" in s for s in body["data"]["skus"])
    assert len({json.dumps({k: b[k] for k in ("data", "perturbations", "robustness", "emissions")}, sort_keys=True)
                for b in bodies}) == 1  # every option sees the same futures

    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    assert all(o.get("robustness") and o.get("emissions") for o in plan["options"])
    compare = parse_block(h.core.repo.blocks_of("scenario_compare")[-1]).props
    assert [p.id for p in compare.profiles] == ["net-benefit", "cost-first", "service-first", "low-carbon"]
    assert sum(o.win_rate_bp or 0 for o in compare.options) == 10_000
    assert all(len(o.scores) == 4 for o in compare.options)
    assert compare.uncertainty is not None and compare.uncertainty.sampled == ["lead_time", "demand"]
    assert compare.emissions is not None and compare.emissions.indicative
    strip = parse_block(h.core.repo.blocks_of("kpi_strip")[-1]).props
    keys = {i.key: i for i in strip.items}
    assert keys["units_at_risk"].band is not None and keys["net_benefit_cents"].band is not None
    assert keys[CO2_KEY].unit == "kg_co2e"
    rec = parse_block(h.core.repo.blocks_of("recommended_option")[-1]).props
    rec_keys = {i.key: i for i in rec.option.kpis}
    assert rec_keys["net_benefit_cents"].band is not None and CO2_KEY in rec_keys
    assert plan["recommended_option_id"] == rec.option.id


async def test_planning_flow_with_m8_off_sends_m7_requests(harness: Harness) -> None:
    h = harness
    h.spec = lambda tenant: AgentSpec(  # type: ignore[method-assign]
        tenant=tenant, planning=PlanningDefaults(as_of=dt.date(2026, 10, 5), emissions={"enabled": False},
                                                 robustness={"enabled": False}))
    await _plan(h)
    bodies = [body for path, body in h.optimizer.requests if path == "/v1/optimize"]
    assert bodies and all("robustness" not in b and "emissions" not in b for b in bodies)
    assert all("unit_weight_g" not in s for b in bodies for s in b["data"]["skus"])
    compare = parse_block(h.core.repo.blocks_of("scenario_compare")[-1]).props
    assert compare.uncertainty is None and compare.emissions is None
    strip = parse_block(h.core.repo.blocks_of("kpi_strip")[-1]).props
    assert all(i.band is None and i.key != CO2_KEY for i in strip.items)


def test_scorecards_of_the_stub_validate() -> None:
    assert Scorecard.model_validate(m8_options()[0]["scorecard"]).bands is not None
