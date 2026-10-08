"""Same request -> identical response (except ``solver.solve_ms`` / ``computed_at``),
including across processes with different string-hash seeds."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from keelson_contracts.optimizer import ImpactRequest, OptimizeRequest

from keelson_opt.service import run_impact, run_optimize

FIXTURE = Path(__file__).parent / "fixtures" / "small_problem.json"

_CHILD = """
import json, sys
from keelson_contracts.optimizer import OptimizeRequest
from keelson_opt.service import run_optimize
raw = json.loads(open(sys.argv[1], encoding="utf-8").read())
out = run_optimize(OptimizeRequest.model_validate(raw)).model_dump(mode="json")
out["solver"].pop("solve_ms")
sys.stdout.write(json.dumps(out, sort_keys=True))
"""


def _strip(resp: dict[str, Any]) -> dict[str, Any]:
    resp = json.loads(json.dumps(resp))
    resp["solver"].pop("solve_ms")
    return resp


def test_optimize_repeatable_in_process(small_raw: dict[str, Any]) -> None:
    a = run_optimize(OptimizeRequest.model_validate(small_raw)).model_dump(mode="json")
    b = run_optimize(OptimizeRequest.model_validate(small_raw)).model_dump(mode="json")
    assert _strip(a) == _strip(b)


def test_optimize_independent_of_input_order(small_raw: dict[str, Any]) -> None:
    shuffled = json.loads(json.dumps(small_raw))
    for key in ("demand", "inventory", "receipts", "cluster_inventory", "lanes", "skus", "clusters", "dcs"):
        shuffled["data"][key].reverse()
    a = _strip(run_optimize(OptimizeRequest.model_validate(small_raw)).model_dump(mode="json"))
    b = _strip(run_optimize(OptimizeRequest.model_validate(shuffled)).model_dump(mode="json"))
    # inputs_hash covers the literal request (order of rows included); everything else matches.
    assert a["solver"].pop("inputs_hash") != b["solver"].pop("inputs_hash")
    assert a == b


def test_optimize_identical_across_processes_and_hash_seeds() -> None:
    outputs = []
    for seed in ("1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "LOG_LEVEL": "WARNING"}
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, str(FIXTURE)], capture_output=True, text=True, env=env, check=True
        )
        outputs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert outputs[0] == outputs[1]


def test_impact_repeatable(small_raw: dict[str, Any]) -> None:
    req = ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"])
    a = run_impact(req).model_dump(mode="json", exclude={"provenance": {"computed_at"}})
    b = run_impact(req).model_dump(mode="json", exclude={"provenance": {"computed_at"}})
    assert a == b
