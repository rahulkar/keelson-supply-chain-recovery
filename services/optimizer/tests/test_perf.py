"""Spike S4: full-size problems must solve end-to-end in under 5 s inside the 512 MB container
(M0 preview: synthetic 180 SKUs x 40 clusters, reallocation only; M3: the full-profile
fixture with all five levers in one request, in-process and over HTTP; M8: the same with CO₂e and
robustness bands, ``126``)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import OptimizeRequest

from keelson_opt.service import run_optimize

from .synthetic import synthetic_request

BUDGET_S = 5.0


@pytest.mark.perf
def test_full_size_optimize_under_budget(capsys: pytest.CaptureFixture[str]) -> None:
    raw = synthetic_request(n_styles=30, clusters_per_dc=10)
    assert len(raw["data"]["skus"]) == 180 and len(raw["data"]["clusters"]) == 40
    t0 = time.perf_counter()
    req = OptimizeRequest.model_validate(raw)
    t1 = time.perf_counter()
    resp = run_optimize(req)
    t2 = time.perf_counter()
    assert resp.status == "OPTIMAL"
    assert resp.kpi_delta.margin_recovered_cents > 0
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    total = t2 - t0
    with capsys.disabled():
        print(
            f"\n[S4] 180 SKUs x 40 clusters x 9 weeks: vars={resp.solver.params['num_variables']} "
            f"rows={resp.solver.params['num_constraints']} validate={t1 - t0:.2f}s "
            f"optimize={t2 - t1:.2f}s (LP build+solve {resp.solver.solve_ms} ms) total={total:.2f}s"
        )
    assert total < BUDGET_S


@pytest.mark.perf
def test_full_size_over_http_under_budget(
    client: TestClient, auth: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.dumps(synthetic_request()).encode()
    t0 = time.perf_counter()
    r = client.post("/v1/optimize", content=body, headers={**auth(), "content-type": "application/json"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:500]
    with capsys.disabled():
        print(f"\n[S4] HTTP /v1/optimize {len(body) / 1e6:.1f} MB request: {elapsed:.2f}s")
    assert elapsed < BUDGET_S


@pytest.mark.perf
def test_s4_full_profile_all_levers_under_budget(capsys: pytest.CaptureFixture[str]) -> None:
    """80 §3 S4: full profile (180 SKUs x 18 clusters x 9 weeks, 12 POs, 3 factory lines),
    every v1 lever in ONE request, concurrency 1, < 5 s."""
    from .fixtures.full_problem import OPTIONS, full_request

    raw = full_request(OPTIONS["D"], plan_id="opt-D")
    t0 = time.perf_counter()
    req = OptimizeRequest.model_validate(raw)
    t1 = time.perf_counter()
    resp = run_optimize(req)
    t2 = time.perf_counter()
    assert resp.status == "OPTIMAL" and resp.scorecard is not None
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    with capsys.disabled():
        print(
            f"\n[S4] full profile, all levers: validate={t1 - t0:.2f}s optimize={t2 - t1:.2f}s "
            f"(solver {resp.solver.name}, {resp.solver.solve_ms} ms, vars={resp.solver.params.get('num_variables')}) "
            f"total={t2 - t0:.2f}s actions={len(resp.actions)}"
        )
    assert t2 - t0 < BUDGET_S


@pytest.mark.perf
def test_s4_full_profile_all_levers_over_http(
    client: TestClient, auth: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    from .fixtures.full_problem import OPTIONS, full_request

    body = json.dumps(full_request(OPTIONS["D"], plan_id="opt-D")).encode()
    t0 = time.perf_counter()
    r = client.post("/v1/optimize", content=body, headers={**auth(), "content-type": "application/json"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:500]
    t1 = time.perf_counter()
    imp = client.post(
        "/v1/impact",
        content=json.dumps(
            {**{k: v for k, v in json.loads(body).items() if k in ("data", "perturbations")}, "include_graph": True}
        ).encode(),
        headers={**auth(), "content-type": "application/json"},
    )
    impact_s = time.perf_counter() - t1
    assert imp.status_code == 200 and imp.json()["graph"]["nodes"]
    with capsys.disabled():
        print(
            f"\n[S4] HTTP /v1/optimize all levers {len(body) / 1e6:.1f} MB: {elapsed:.2f}s; "
            f"/v1/impact include_graph: {impact_s:.2f}s"
        )
    assert elapsed < BUDGET_S
    assert impact_s < BUDGET_S


@pytest.mark.perf
@pytest.mark.parametrize("story", ["vn", "bd+vn"])
def test_s4_full_profile_all_levers_with_resequence_alternative(story: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Worst case of the planner: the resequence moves PO lines, so the LP levers are solved
    twice (with the resequence and without it, each with its MOQ phase 2)."""
    from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, VN_PORT_CLOSURE, full_request

    perts = [VN_PORT_CLOSURE] if story == "vn" else [BD_TRIMS_DELAY, VN_PORT_CLOSURE]
    raw = full_request(OPTIONS["D"], perts, plan_id="opt-D")
    t0 = time.perf_counter()
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    elapsed = time.perf_counter() - t0
    assert resp.status == "OPTIMAL" and resp.scorecard is not None
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    with capsys.disabled():
        print(
            f"\n[S4] full profile, all levers, {story}: total={elapsed:.2f}s (solver {resp.solver.name}, "
            f"{resp.solver.solve_ms} ms, resequence_dropped={resp.solver.params.get('resequence_dropped', False)}) "
            f"actions={len(resp.actions)}"
        )
    assert elapsed < BUDGET_S


@pytest.mark.perf
def test_s4_full_profile_with_emissions_and_robustness_over_http(
    client: TestClient, auth: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    from .fixtures.full_problem import OPTIONS
    from .fixtures.m8_inputs import BD_DELAY_BANDED, m8_request, robustness

    raw = m8_request(OPTIONS["D"], plan_id="opt-D", perturbations=[BD_DELAY_BANDED], robust=robustness(window=None))
    body = json.dumps(raw).encode()
    t0 = time.perf_counter()
    r = client.post("/v1/optimize", content=body, headers={**auth(), "content-type": "application/json"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:500]
    assert r.json()["robustness"]["samples"] == 64
    with capsys.disabled():
        print(f"\n[S4] HTTP /v1/optimize all levers + CO2e + robustness(64) {len(body) / 1e6:.1f} MB: {elapsed:.2f}s")
    assert elapsed < BUDGET_S


_M8_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from keelson_contracts.optimizer import OptimizeRequest
from keelson_opt.service import run_optimize
from tests.fixtures.full_problem import OPTIONS
from tests.fixtures.m8_inputs import BD_DELAY_BANDED, m8_request, robustness
raw = m8_request(OPTIONS["D"], plan_id="opt-D", perturbations=[BD_DELAY_BANDED], robust=robustness(window=None))
t0 = time.perf_counter()
resp = run_optimize(OptimizeRequest.model_validate(raw))
elapsed = time.perf_counter() - t0
assert resp.scorecard.bands is not None and resp.robustness.samples == 64
assert resp.scorecard.incremental_co2e_kg is not None
hwm = next(int(line.split()[1]) for line in open("/proc/self/status") if line.startswith("VmHWM:"))
print(f"{elapsed:.3f} {hwm}")
"""


@pytest.mark.perf
def test_s4_full_profile_with_emissions_and_robustness(capsys: pytest.CaptureFixture[str]) -> None:
    """126 §5: S4 (all levers, the trims delay with its 5 / 7 / 14 band) plus CO₂e and robustness
    (64 samples, default transit and demand noise): < 5 s at 512 MB / 1.5 CPU and a peak RSS
    (``VmHWM``) below 400 MB, measured in a fresh process so earlier tests do not count. Kept last
    in the module: the child process runs next to this one."""
    root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run([sys.executable, "-c", _M8_CHILD, root], capture_output=True, text=True, check=True,
                          env={**os.environ, "LOG_LEVEL": "WARNING"})
    elapsed_s, rss_kb = proc.stdout.strip().splitlines()[-1].split()
    elapsed, rss_mb = float(elapsed_s), int(rss_kb) / 1024
    with capsys.disabled():
        print(f"\n[S4] full profile, all levers + CO2e + robustness(64): total={elapsed:.2f}s peak RSS={rss_mb:.0f} MB")
    assert elapsed < BUDGET_S
    assert rss_mb < 400
