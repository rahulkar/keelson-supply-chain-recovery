"""M8 contracts (``docs/contracts/126-m8-co2-robustness.md``): CO₂e and robustness on the optimizer
models, the block additions (``kg_co2e``, KPI bands, profiles / scores / win rates / notes on
``scenario_compare@1``), weight profiles (``scoring``). Every addition is optional and omitted
from dumps while unset, so M1-M7 payloads and their hashes are unchanged."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from examples import good_props, provenance
from keelson_contracts import blocks
from keelson_contracts import optimizer as opt
from keelson_contracts.common import sha256_hex
from keelson_contracts.scoring import (
    DEFAULT_PROFILES,
    DEFAULT_SCORING,
    ScoringConfig,
    WeightProfile,
    co2_missing,
    div_half_up,
    profile_score,
    rank_options,
)

CARD: dict[str, Any] = {
    "fill_rate_bp": 9650,
    "lost_units": 420,
    "lost_margin_cents": 840_000,
    "incremental_cost_cents": 183_000,
    "cost_breakdown": {"transfer": 63_000, "expedite_premium": 120_000, "nearshore_premium": 0},
    "net_benefit_cents": 1_517_000,
    "shortage_days": 12,
    "first_breach_shift_days": 7,
    "tts_gain_days": 5,
    "changes": 6,
}
BAND = {"p10": 1, "p50": 2, "p90": 3}
BANDS = {
    "samples": 64,
    "fill_rate_bp": {"p10": 9100, "p50": 9500, "p90": 9800},
    "lost_units": {"p10": 200, "p50": 500, "p90": 1400},
    "lost_margin_cents": {"p10": 400_000, "p50": 1_000_000, "p90": 2_800_000},
    "net_benefit_cents": {"p10": 900_000, "p50": 1_400_000, "p90": 1_700_000},
    "shortage_days": {"p10": 7, "p50": 14, "p90": 35},
    "stockout_chance_bp": 9_100,
}
CO2 = {"incremental_co2e_kg": 26_811,
       "co2e_breakdown": {"transfer_kg": 926, "expedite_kg": 25_885, "nearshore_kg": 0, "rebook_kg": 0}}


def m8_card(**over: Any) -> dict[str, Any]:
    return {**copy.deepcopy(CARD), **copy.deepcopy(CO2), "bands": copy.deepcopy(BANDS), **over}


# ───────────────────────────── optimizer models ─────────────────────────────


def _request(**extra: Any) -> dict[str, Any]:
    return {
        "data": {
            "tenant": "demo-retail",
            "as_of": "2026-10-05",
            "horizon": {"start_week": "2026-W41", "weeks": 2},
            "dcs": [{"code": "UK1"}, {"code": "DE1"}],
            "clusters": [{"code": "UK1-A", "dc_code": "UK1"}],
            "skus": [{"code": "PUF-M", "style_code": "PUF", "retail_price_cents": 3000, "unit_cost_cents": 1000}],
            "lanes": [{"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60,
                       "max_units_wk": 100}],
            "inbound_lanes": [{"code": "SEA-UK", "origin": "PORT-A", "dest_dc": "UK1", "mode": "sea",
                               "transit_days": 30, "cost_per_unit_cents": 20, "weekly_capacity_units": 100}],
        },
        "perturbations": [],
        "plan_spec": {"id": "opt-A", "title": "A", "levers": []},
        **extra,
    }


def test_new_fields_are_omitted_so_old_hashes_hold() -> None:
    raw = _request()
    req = opt.OptimizeRequest.model_validate(raw)
    dumped = req.model_dump(mode="json")
    assert "emissions" not in dumped and "robustness" not in dumped
    assert "unit_weight_g" not in dumped["data"]["skus"][0]
    assert "distance_km" not in dumped["data"]["lanes"][0]
    assert "legs" not in dumped["data"]["inbound_lanes"][0]
    before = sha256_hex(dumped)
    assert sha256_hex(opt.OptimizeRequest.model_validate(copy.deepcopy(raw)).model_dump(mode="json")) == before
    card = opt.Scorecard.model_validate(CARD).model_dump(mode="json")
    assert set(card) == set(CARD)


def test_m8_inputs_round_trip() -> None:
    raw = _request(
        emissions={"factors": [{"mode": "sea", "g_co2e_per_tkm": 16}, {"mode": "road", "g_co2e_per_tkm": 100}],
                   "source_id": "glec-v3-indicative"},
        robustness={"samples": 32, "seed": 7, "window_bands": [], "lead_time_noise": [
            {"mode": "sea", "days": {"p10": -2, "p50": 0, "p90": 5}}], "demand_noise_bp": {"p10": 9000, "p50": 10000,
                                                                                         "p90": 11000}},
    )
    raw["data"]["skus"][0]["unit_weight_g"] = 1100
    raw["data"]["lanes"][0]["distance_km"] = 1088
    raw["data"]["inbound_lanes"][0]["legs"] = [{"mode": "sea", "distance_km": 21_040}, {"mode": "road", "distance_km": 96}]
    req = opt.OptimizeRequest.model_validate(raw)
    assert req.emissions is not None and req.emissions.basis == "WTW"
    assert req.robustness is not None and req.robustness.method == "mc/1"
    again = opt.OptimizeRequest.model_validate(req.model_dump(mode="json"))
    assert again == req


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("emissions", "factors"), [{"mode": "sea", "g_co2e_per_tkm": 16}, {"mode": "sea", "g_co2e_per_tkm": 17}]),
        (("emissions", "factors"), [{"mode": "sea", "g_co2e_per_tkm": 16.5}]),
        (("emissions", "factors"), [{"mode": "barge", "g_co2e_per_tkm": 16}]),
        (("emissions", "factors"), []),
        (("robustness", "samples"), 7),
        (("robustness", "samples"), 257),
        (("robustness", "lead_time_noise"), [{"mode": "sea", "days": {"p10": 1, "p50": 0, "p90": 2}}]),
        (("robustness", "lead_time_noise"), [{"mode": "sea", "days": {"p10": -90, "p50": 0, "p90": 2}}]),
        (("robustness", "window_bands"), [{"perturbation": 0, "duration_days": {"p10": 0, "p50": 1, "p90": 2}}]),
        (("robustness", "window_bands"), [{"perturbation": 3, "duration_days": {"p10": 1, "p50": 2, "p90": 3}}]),
        (("robustness", "demand_noise_bp"), {"p10": -1, "p50": 10000, "p90": 11000}),
    ],
)
def test_invalid_m8_requests(path: tuple[str, str], value: Any) -> None:
    raw = _request(emissions={"factors": [{"mode": "sea", "g_co2e_per_tkm": 16}], "source_id": "s"},
                   robustness={"samples": 16})
    raw[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        opt.OptimizeRequest.model_validate(raw)


def test_band_and_lane_leg_validation() -> None:
    assert opt.Band.model_validate(BAND).p50 == 2
    for bad in ({"p10": 3, "p50": 2, "p90": 4}, {"p10": 1, "p50": 5, "p90": 4}, {"p10": 1.0, "p50": 2, "p90": 3}):
        with pytest.raises(ValidationError):
            opt.Band.model_validate(bad)
    with pytest.raises(ValidationError):
        opt.LaneLeg.model_validate({"mode": "sea", "distance_km": 0})


def test_scorecard_co2_breakdown_and_bands() -> None:
    sc = opt.Scorecard.model_validate(m8_card())
    assert sc.incremental_co2e_kg == 26_811 and sc.bands is not None and sc.bands.samples == 64
    with pytest.raises(ValidationError, match="sums to"):
        opt.Scorecard.model_validate(m8_card(incremental_co2e_kg=1))
    with pytest.raises(ValidationError, match="exactly when"):
        opt.Scorecard.model_validate({**CARD, "incremental_co2e_kg": 0})
    with pytest.raises(ValidationError, match="exactly when"):
        opt.Scorecard.model_validate({**CARD, "co2e_breakdown": CO2["co2e_breakdown"]})
    negative = m8_card(incremental_co2e_kg=-2_235,
                       co2e_breakdown={"transfer_kg": 0, "expedite_kg": 0, "nearshore_kg": -2_235, "rebook_kg": 0})
    assert opt.Scorecard.model_validate(negative).incremental_co2e_kg == -2_235
    bad_bands = m8_card(bands={**BANDS, "stockout_chance_bp": 10_001})
    with pytest.raises(ValidationError):
        opt.Scorecard.model_validate(bad_bands)


def test_robustness_result_lengths() -> None:
    good = {"method": "mc/1", "seed": 7, "samples": 2, "requested_samples": 64, "sampled": ["lead_time"],
            "crn_key": "a" * 64, "per_sample_net_benefit_cents": [1, 2], "per_sample_lost_margin_cents": [3, 4]}
    assert opt.RobustnessResult.model_validate(good).samples == 2
    for over in ({"per_sample_net_benefit_cents": [1]}, {"samples": 65}, {"sampled": ["lead_time", "lead_time"]},
                 {"crn_key": "x"}, {"sampled": ["weather"]}):
        with pytest.raises(ValidationError):
            opt.RobustnessResult.model_validate({**good, **over})


def test_response_summaries() -> None:
    summary = opt.EmissionsSummary.model_validate(
        {"source_id": "glec-v3-indicative", "basis": "WTW", "coverage_bp": 9_500, "baseline_inbound_kg": 134_811,
         "uncovered": ["lane:SEA-UK"]})
    assert summary.uncovered == ["lane:SEA-UK"]
    with pytest.raises(ValidationError):
        opt.EmissionsSummary.model_validate({**summary.model_dump(), "uncovered": [f"sku:{i}" for i in range(21)]})


# ───────────────────────────── blocks ─────────────────────────────


def _compare(**over: Any) -> dict[str, Any]:
    props = good_props("scenario_compare@1")
    props["options"][0]["scorecard"] = {**props["options"][0]["scorecard"], **{
        "incremental_co2e_kg": 0, "co2e_breakdown": {"transfer_kg": 0, "expedite_kg": 0, "nearshore_kg": 0,
                                                     "rebook_kg": 0}}}
    props["options"][1]["scorecard"] = m8_card()
    props["profiles"] = [
        {"id": p.id, "label": p.label, "default": p.id == "net-benefit", "lost_margin_bp": p.lost_margin_bp,
         "cost_bp": p.cost_bp, "carbon_price_cents_per_t": p.carbon_price_cents_per_t,
         "risk_aversion_bp": p.risk_aversion_bp}
        for p in DEFAULT_PROFILES
    ]
    for i, o in enumerate(props["options"]):
        o["scores"] = [{"profile_id": p.id, "score_cents": 100 - i, "rank": i + 1} for p in DEFAULT_PROFILES]
        o["win_rate_bp"] = [2_500, 7_500][i]
    props["uncertainty"] = {"method": "mc/1", "samples": 64, "sampled": ["disruption_duration", "lead_time"]}
    props["emissions"] = {"source_label": "GLEC Framework v3 default intensities", "basis": "WTW",
                          "indicative": True, "coverage_bp": 10_000}
    props.update(over)
    return props


def test_scenario_compare_with_profiles_scores_win_rates_and_notes() -> None:
    props = blocks.ScenarioCompareProps.model_validate(_compare())
    assert props.profiles[0].default and props.options[1].win_rate_bp == 7_500
    co2_axis = blocks.ScenarioCompareProps.model_validate(_compare(axes={"x": "incremental_co2e_kg", "y": "lost_units"}))
    assert co2_axis.axes.x == "incremental_co2e_kg"
    block = blocks.make_block(kind="scenario_compare", title="Options", placement="canvas",
                              provenance=blocks.BlockProvenance.model_validate(provenance()), props=props)
    assert blocks.parse_block(blocks.block_to_wire(block)).props == props  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["profiles"].__setitem__(1, {**p["profiles"][1], "default": True}),
        lambda p: p["profiles"].append(copy.deepcopy(p["profiles"][0])),
        lambda p: p["options"][0]["scores"].__setitem__(0, {"profile_id": "unknown", "score_cents": 1, "rank": 1}),
        lambda p: p["options"][0].__setitem__("win_rate_bp", None),
        lambda p: p["options"][0].__setitem__("win_rate_bp", 2_400),
        lambda p: p["options"][0]["scorecard"].pop("incremental_co2e_kg") and p["options"][0]["scorecard"].pop(
            "co2e_breakdown") and p.__setitem__("axes", {"x": "incremental_co2e_kg", "y": "lost_units"}),
        lambda p: p["uncertainty"].__setitem__("sampled", []),
        lambda p: p["emissions"].__setitem__("coverage_bp", 10_001),
        lambda p: p["profiles"][0].__setitem__("label", ""),
        lambda p: p["profiles"][0].__setitem__("id", "Bad Id"),
        lambda p: p["options"][0]["scores"].__setitem__(0, {"profile_id": "net-benefit", "score_cents": 1, "rank": 0}),
    ],
)
def test_invalid_scenario_compare(mutate: Any) -> None:
    props = _compare()
    mutate(props)
    with pytest.raises(ValidationError):
        blocks.ScenarioCompareProps.model_validate(props)


def test_pre_m8_compare_and_kpi_blocks_serialize_as_before() -> None:
    props = blocks.ScenarioCompareProps.model_validate(good_props("scenario_compare@1"))
    dumped = props.model_dump(mode="json")
    assert not {"profiles", "uncertainty", "emissions"} & set(dumped)
    assert not {"scores", "win_rate_bp"} & set(dumped["options"][0])
    strip = blocks.KpiStripProps.model_validate(good_props("kpi_strip@1")).model_dump(mode="json")
    assert all("band" not in item for item in strip["items"])


def test_kpi_band_and_co2_unit() -> None:
    item = blocks.KpiItem.model_validate({"key": "incremental_co2e_kg", "label": "Extra CO₂e of the plan",
                                          "value": 26_811, "unit": "kg_co2e", "prov": "simulated",
                                          "preliminary": False, "band": {"p10": 20_000, "p90": 30_000, "samples": 64}})
    assert item.unit == "kg_co2e" and item.band is not None and item.band.samples == 64
    for bad in ({"p10": 2, "p90": 1, "samples": 64}, {"p10": 1, "p90": 2, "samples": 0}):
        with pytest.raises(ValidationError):
            blocks.KpiItem.model_validate({**item.model_dump(), "band": bad})


# ───────────────────────────── scoring ─────────────────────────────


def test_default_profile_score_is_the_net_benefit() -> None:
    for card in (CARD, m8_card(), {**CARD, "net_benefit_cents": -5_000}):
        sc = opt.Scorecard.model_validate(card)
        assert profile_score(sc, DEFAULT_SCORING.default) == sc.net_benefit_cents


def test_profile_score_integer_math() -> None:
    sc = opt.Scorecard.model_validate(m8_card())
    recovered = 1_517_000 + 183_000
    cost_first = next(p for p in DEFAULT_PROFILES if p.id == "cost-first")
    assert profile_score(sc, cost_first) == recovered - 183_000 * 3 // 2
    service = next(p for p in DEFAULT_PROFILES if p.id == "service-first")
    robust = 900_000 + 183_000
    blended = div_half_up(5_000 * recovered + 5_000 * robust, 10_000)
    assert profile_score(sc, service) == div_half_up(blended * 15_000, 10_000) - div_half_up(183_000 * 7_500, 10_000)
    low = next(p for p in DEFAULT_PROFILES if p.id == "low-carbon")
    blended_low = div_half_up(7_500 * recovered + 2_500 * robust, 10_000)
    # 26,811 kg at €200 / t = €5,362.20
    assert profile_score(sc, low) == blended_low - 183_000 - 536_220
    plain = opt.Scorecard.model_validate(CARD)
    assert co2_missing(plain) and not co2_missing(sc)
    assert profile_score(plain, low) == div_half_up(recovered * 10_000, 10_000) - 183_000  # no bands, no CO2


def test_div_half_up() -> None:
    assert [div_half_up(n, 10) for n in (4, 5, 15, -5, -6, -15)] == [0, 1, 2, 0, -1, -1]


def test_rank_options_ties_keep_candidate_order() -> None:
    a = opt.Scorecard.model_validate(CARD)
    b = opt.Scorecard.model_validate({**CARD, "net_benefit_cents": 2_000_000})
    ranks = rank_options([("A", a), ("B", b), ("C", a)], DEFAULT_SCORING.default)
    assert ranks == {"B": (2_000_000, 1), "A": (1_517_000, 2), "C": (1_517_000, 3)}
    assert rank_options({"X": a}, DEFAULT_SCORING.default) == {"X": (1_517_000, 1)}


def test_scoring_config_validation() -> None:
    assert DEFAULT_SCORING.default.id == "net-benefit" and len(DEFAULT_SCORING.profiles) == 4
    one = WeightProfile(id="custom", label="Custom")
    assert ScoringConfig(default_profile="custom", profiles=(one,)).default == one
    for bad in (
        {"default_profile": "missing", "profiles": [one.model_dump()]},
        {"default_profile": "custom", "profiles": [one.model_dump(), one.model_dump()]},
        {"default_profile": "custom", "profiles": []},
        {"default_profile": "custom", "profiles": [one.model_dump()] + [
            {"id": f"p{i}", "label": "P"} for i in range(8)]},
        {"profiles": [{"id": "net-benefit", "label": "x", "cost_bp": 50_001}]},
        {"profiles": [{"id": "net-benefit", "label": "x", "risk_aversion_bp": 10_001}]},
        {"profiles": [{"id": "net-benefit", "label": "x" * 41}]},
        {"profiles": [{"id": "Net", "label": "x"}], "default_profile": "Net"},
    ):
        with pytest.raises(ValidationError):
            ScoringConfig.model_validate(bad)
