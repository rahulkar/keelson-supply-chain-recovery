"""Spike S4 budgets for ``integer_stage="cpsat"`` (M8): the full profile with every lever, the
planner's worst case (resequence moves PO lines, so the LP levers are solved and completed with
and without it) and a 400-order line. ``cpsat`` is opt-in and runs up to two more GLOP solves
plus up to 0.6 deterministic CP-SAT seconds per request, so its budget is 10 s - twice S4's
(``repair`` keeps the 5 s, ``tests/test_perf.py``); the deterministic CP-SAT time per stage is
printed for the README's det/wall calibration."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse

from keelson_opt.service import run_optimize

from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, VN_PORT_CLOSURE, full_request
from .test_resequence_m8 import long_line_problem

BUDGET_S = 10.0
"""See the module doc (125 §9: measured 3.8-7.9 s on the shared 6-CPU test host under load)."""


def _cpsat_line(resp: OptimizeResponse) -> str:
    runs = [s for s in resp.solver.stages if s.solver == "CP-SAT" and s.det_time_ms is not None]
    det = sum(s.det_time_ms or 0 for s in runs)
    return f"CP-SAT runs={len(runs)} det={det} ms"


@pytest.mark.perf
@pytest.mark.parametrize("story", ["bd", "vn", "bd+vn"])
def test_s4_full_profile_cpsat_under_budget(story: str, capsys: pytest.CaptureFixture[str]) -> None:
    perts = {"bd": [BD_TRIMS_DELAY], "vn": [VN_PORT_CLOSURE], "bd+vn": [BD_TRIMS_DELAY, VN_PORT_CLOSURE]}[story]
    raw = full_request(OPTIONS["D"], perts, plan_id="opt-D")
    raw["solver"]["integer_stage"] = "cpsat"
    t0 = time.perf_counter()
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    elapsed = time.perf_counter() - t0
    assert resp.status == "OPTIMAL" and resp.scorecard is not None
    with capsys.disabled():
        print(
            f"\n[S4/M8] full profile, all levers, cpsat, {story}: total={elapsed:.2f}s "
            f"(solver {resp.solver.name}, {resp.solver.solve_ms} ms, {_cpsat_line(resp)}, "
            f"integer={resp.solver.params.get('integer_method')}, "
            f"resequence_dropped={resp.solver.params.get('resequence_dropped', False)})"
        )
    assert elapsed < BUDGET_S


@pytest.mark.perf
def test_s4_full_profile_cpsat_over_http(
    client: TestClient, auth: Callable[..., dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    raw = full_request(OPTIONS["D"], [BD_TRIMS_DELAY, VN_PORT_CLOSURE], plan_id="opt-D")
    raw["solver"]["integer_stage"] = "cpsat"
    body = json.dumps(raw).encode()
    t0 = time.perf_counter()
    r = client.post("/v1/optimize", content=body, headers={**auth(), "content-type": "application/json"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:500]
    assert r.json()["solver"]["stages"]
    with capsys.disabled():
        print(f"\n[S4/M8] HTTP /v1/optimize all levers, cpsat, bd+vn {len(body) / 1e6:.1f} MB: {elapsed:.2f}s")
    assert elapsed < BUDGET_S


@pytest.mark.perf
def test_s4_400_order_line_cpsat_under_budget(capsys: pytest.CaptureFixture[str]) -> None:
    raw = long_line_problem(400, 9)
    raw["solver"]["integer_stage"] = "cpsat"
    t0 = time.perf_counter()
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    elapsed = time.perf_counter() - t0
    assert resp.status == "OPTIMAL"
    with capsys.disabled():
        print(
            f"\n[S4/M8] 400-order line, cpsat: total={elapsed:.2f}s "
            f"({resp.solver.params.get('resequence_methods')}, {_cpsat_line(resp)})"
        )
    assert elapsed < BUDGET_S
