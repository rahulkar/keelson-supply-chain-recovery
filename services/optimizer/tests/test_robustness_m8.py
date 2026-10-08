"""Robustness ``mc/1`` in ``/v1/optimize`` (126 §5): nominal-equal bands without uncertainty,
ordered bands, determinism (in process, across processes and hash seeds, shuffled inputs), common
random numbers across the options of a plan, the deterministic sample cap, window-band
validation (422) and the exact golden of the full story (seed 7, 64 samples)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse, RobustnessSpec

from keelson_opt.robust.evaluate import CELL_BUDGET, effective_samples
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_optimize

from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS
from .fixtures.m8_inputs import m8_request, robustness
from .synthetic import synthetic_request

GOLDEN = Path(__file__).parent / "fixtures" / "robust_full.golden.json"
PLANS = ["A", "B", "C", "D", "do-nothing"]


def _solve(raw: dict[str, Any]) -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(raw))


def _levers(plan: str) -> list[dict[str, Any]]:
    return [] if plan == "do-nothing" else OPTIONS[plan]


@pytest.fixture(scope="module")
def story() -> dict[str, OptimizeResponse]:
    """Options A-D + do-nothing on the full story: trims delay (band 5 / 7 / 14) and the 10-day
    VN closure (duration band 7 / 10 / 14), default noise, seed 7, 64 samples."""
    return {p: _solve(m8_request(_levers(p), plan_id=f"opt-{p}", robust=robustness())) for p in PLANS}


def _summary(resp: OptimizeResponse) -> dict[str, Any]:
    sc = resp.scorecard
    assert sc is not None and sc.bands is not None and resp.robustness is not None and resp.emissions is not None
    rob = resp.robustness

    def digest(values: list[int]) -> str:
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    return {
        "nominal": {k: getattr(sc, k) for k in ("fill_rate_bp", "lost_units", "lost_margin_cents", "net_benefit_cents",
                                                 "shortage_days", "incremental_cost_cents")},
        "bands": sc.bands.model_dump(mode="json"),
        "co2e": {"incremental_co2e_kg": sc.incremental_co2e_kg,
                 "breakdown": sc.co2e_breakdown.model_dump(mode="json") if sc.co2e_breakdown else None},
        "emissions": resp.emissions.model_dump(mode="json"),
        "robustness": {"samples": rob.samples, "requested_samples": rob.requested_samples, "sampled": list(rob.sampled),
                       "seed": rob.seed, "net_benefit_sha256": digest(rob.per_sample_net_benefit_cents),
                       "lost_margin_sha256": digest(rob.per_sample_lost_margin_cents)},
    }


def test_golden(story: dict[str, OptimizeResponse]) -> None:
    """Exact regression of the robust story. Regenerate after an intentional change with
    ``KEELSON_UPDATE_GOLDEN=1 pytest tests/test_robustness_m8.py -k golden``."""
    got = {p: _summary(story[p]) for p in PLANS}
    if os.environ.get("KEELSON_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_bands_are_ordered_and_sampled_everything(story: dict[str, OptimizeResponse]) -> None:
    for resp in story.values():
        sc = resp.scorecard
        assert sc is not None and sc.bands is not None and resp.robustness is not None
        assert resp.robustness.sampled == ["disruption_severity", "disruption_duration", "lead_time", "demand"]
        assert resp.robustness.samples == 64 == sc.bands.samples
        for name in ("fill_rate_bp", "lost_units", "lost_margin_cents", "net_benefit_cents", "shortage_days"):
            b = getattr(sc.bands, name)
            assert b.p10 <= b.p50 <= b.p90
    nothing = story["do-nothing"].scorecard
    assert nothing is not None and nothing.bands is not None
    assert nothing.bands.net_benefit_cents.model_dump() == {"p10": 0, "p50": 0, "p90": 0}
    for p in "ABCD":
        sc = story[p].scorecard
        assert sc is not None and sc.bands is not None
        # an improving plan is at least as good as doing nothing in the pessimistic case too
        assert sc.bands.fill_rate_bp.p10 >= nothing.bands.fill_rate_bp.p10
        assert sc.bands.net_benefit_cents.p50 > 0


def test_common_random_numbers_across_options(story: dict[str, OptimizeResponse]) -> None:
    """Every option's baseline batch is the do-nothing batch: same futures, same keys."""
    base = story["do-nothing"].robustness
    assert base is not None
    for p in "ABCD":
        resp = story[p]
        rob, sc = resp.robustness, resp.scorecard
        assert rob is not None and sc is not None
        assert rob.crn_key == base.crn_key and rob.seed == base.seed and rob.samples == base.samples
        recovered = [n + sc.incremental_cost_cents for n in rob.per_sample_net_benefit_cents]
        baseline = [r + lm for r, lm in zip(recovered, rob.per_sample_lost_margin_cents, strict=True)]
        assert baseline == base.per_sample_lost_margin_cents


def test_option_b_flies_more_co2e_than_option_a(story: dict[str, OptimizeResponse]) -> None:
    a, b = story["A"].scorecard, story["B"].scorecard
    assert a is not None and b is not None
    assert b.incremental_co2e_kg > a.incremental_co2e_kg > 0  # type: ignore[operator]


def test_without_uncertainty_every_band_is_the_nominal_value() -> None:
    quiet = robustness(window=None, noise=False, demand=False, value_bands=False, samples=8)
    for plan in ("A", "D", "do-nothing"):
        resp = _solve(m8_request(_levers(plan), plan_id=f"opt-{plan}", robust=quiet))
        sc, rob = resp.scorecard, resp.robustness
        assert sc is not None and sc.bands is not None and rob is not None
        assert rob.sampled == []
        for name in ("fill_rate_bp", "lost_units", "lost_margin_cents", "net_benefit_cents", "shortage_days"):
            nominal = getattr(sc, name)
            assert getattr(sc.bands, name).model_dump() == {"p10": nominal, "p50": nominal, "p90": nominal}, name
        assert sc.bands.stockout_chance_bp == (10_000 if sc.lost_units else 0)
        assert set(rob.per_sample_net_benefit_cents) == {sc.net_benefit_cents}


def test_deterministic_in_process_and_for_shuffled_inputs() -> None:
    raw = m8_request(OPTIONS["B"], plan_id="opt-B", robust=robustness(samples=16))
    shuffled = copy.deepcopy(raw)
    for key in ("receipts", "demand", "skus", "po_lines", "inventory", "cluster_inventory", "inbound_lanes", "lanes"):
        shuffled["data"][key] = list(reversed(shuffled["data"][key]))
    shuffled["robustness"]["lead_time_noise"] = list(reversed(shuffled["robustness"]["lead_time_noise"]))
    a, b, c = _solve(raw), _solve(copy.deepcopy(raw)), _solve(shuffled)
    assert a.model_dump(mode="json", exclude={"solver"}) == b.model_dump(mode="json", exclude={"solver"})
    assert a.robustness is not None and c.robustness is not None
    assert a.scorecard == c.scorecard
    assert a.robustness.model_dump(exclude={"crn_key"}) == c.robustness.model_dump(exclude={"crn_key"})


_CHILD = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from keelson_contracts.optimizer import OptimizeRequest
from keelson_opt.service import run_optimize
from tests.fixtures.full_problem import OPTIONS
from tests.fixtures.m8_inputs import m8_request, robustness
r = run_optimize(OptimizeRequest.model_validate(m8_request(OPTIONS["A"], plan_id="opt-A", robust=robustness(samples=16))))
d = r.model_dump(mode="json")
d["solver"].pop("solve_ms")
sys.stdout.write(hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
"""


def test_identical_across_processes_and_hash_seeds() -> None:
    root = str(Path(__file__).resolve().parents[1])
    digests = set()
    for seed in ("1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "LOG_LEVEL": "WARNING"}
        proc = subprocess.run([sys.executable, "-c", _CHILD, root], capture_output=True, text=True, env=env, check=True)
        digests.add(proc.stdout.strip().splitlines()[-1])
    assert len(digests) == 1


def test_sample_cap_is_deterministic_and_bounded() -> None:
    raw = synthetic_request(n_styles=30, clusters_per_dc=10)
    req = OptimizeRequest.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    cells = len(scn.skus) * len(scn.clusters) * scn.horizon
    capped = effective_samples(scn, 256)
    assert capped == max(8, CELL_BUDGET // cells) < 256
    assert effective_samples(scn, 8) == 8
    raw["robustness"] = {"samples": 256, "seed": 3}
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    assert resp.robustness is not None and resp.robustness.samples == capped
    assert resp.robustness.requested_samples == 256
    assert len(resp.robustness.per_sample_net_benefit_cents) == capped


def test_capacity_band_can_reopen_a_closed_lane() -> None:
    """A closure whose factor band reaches above 0 is partly open in some futures (no closure
    there); a partial capacity only limits new bookings, so those futures lose less."""
    vn = {"op": "capacity_scale", "target": {"kinds": ["port"], "ids": ["VNSGN"]}, "factor_bp": 0, "p90": 6000,
          "window": {"start": "2026-10-06", "end": "2026-10-15"}, "prov": "assumption"}
    spec = robustness(window=None, noise=False, demand=False, samples=32)
    closed = _solve(m8_request([], plan_id="do-nothing", perturbations=[vn], robust=spec))
    sc = closed.scorecard
    assert sc is not None and sc.bands is not None and closed.robustness is not None
    assert closed.robustness.sampled == ["disruption_severity"]
    assert sc.bands.lost_units.p10 < sc.lost_units == sc.bands.lost_units.p90


@pytest.mark.parametrize(
    ("mutate", "status", "message"),
    [
        (lambda r: r["robustness"]["window_bands"].__setitem__(0, {"perturbation": 5, "duration_days":
                                                                 {"p10": 7, "p50": 10, "p90": 14}}), 422, "out of range"),
        (lambda r: r["robustness"]["window_bands"].__setitem__(0, {"perturbation": 0, "duration_days":
                                                                 {"p10": 7, "p50": 10, "p90": 14}}), 422,
         "no bounded window"),
        (lambda r: r["robustness"]["window_bands"].__setitem__(0, {"perturbation": 1, "duration_days":
                                                                 {"p10": 7, "p50": 9, "p90": 14}}), 422, "p50 is 9"),
        (lambda r: r["robustness"].__setitem__("samples", 4), 422, None),
        (lambda r: r["robustness"].__setitem__("samples", 257), 422, None),
        (lambda r: r["robustness"]["lead_time_noise"].append({"mode": "sea", "days": {"p10": 0, "p50": 0, "p90": 1}}),
         422, None),
    ],
)
def test_invalid_robustness_specs_are_422(
    client: TestClient, auth: Callable[..., dict[str, str]], mutate: Callable[[dict[str, Any]], None], status: int,
    message: str | None,
) -> None:
    raw = m8_request(OPTIONS["A"], plan_id="opt-A", robust=robustness(samples=8))
    mutate(raw)
    r = client.post("/v1/optimize", json=raw, headers=auth())
    assert r.status_code == status, r.text[:300]
    if message:
        assert message in r.text


def test_robustness_spec_defaults() -> None:
    spec = RobustnessSpec()
    assert (spec.method, spec.samples, spec.seed, spec.value_bands) == ("mc/1", 64, 0, True)
    assert spec.window_bands == [] and spec.lead_time_noise == [] and spec.demand_noise_bp is None


def test_trims_delay_only_story_runs_without_window_bands() -> None:
    raw = m8_request(OPTIONS["A"], plan_id="opt-A", perturbations=[{**BD_TRIMS_DELAY, "p10": 5, "p90": 14}],
                     robust=robustness(window=None, samples=16))
    resp = _solve(raw)
    assert resp.robustness is not None and "disruption_duration" not in resp.robustness.sampled
