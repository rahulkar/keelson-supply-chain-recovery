"""Full-profile fixture in ``integer_stage="cpsat"`` mode (M8, ``docs/contracts/125-m8-optimizer-cpsat.md``):
plan invariants with the M8 sourcing terms, never worse than ``repair`` on the story options,
determinism across processes and an exact golden summary (options A-D on the BD story, C and D
on the VN and BD+VN stories)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse

from keelson_opt.service import run_optimize

from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, VN_PORT_CLOSURE, full_request
from .network_m8 import check_invariants

GOLDEN = Path(__file__).parent / "fixtures" / "full_problem.cpsat.golden.json"
STORIES: dict[str, list[dict[str, Any]]] = {
    "bd": [BD_TRIMS_DELAY],
    "vn": [VN_PORT_CLOSURE],
    "bd+vn": [BD_TRIMS_DELAY, VN_PORT_CLOSURE],
}
CASES = [("bd", "A"), ("bd", "B"), ("bd", "C"), ("bd", "D"), ("vn", "C"), ("vn", "D"), ("bd+vn", "C"), ("bd+vn", "D")]


def raw_request(story: str, option: str, stage: str = "cpsat") -> dict[str, Any]:
    raw = full_request(OPTIONS[option], STORIES[story], plan_id=f"opt-{option}")
    raw["solver"]["integer_stage"] = stage
    return raw


@cache
def solved(story: str, option: str, stage: str = "cpsat") -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(raw_request(story, option, stage)))


def _digest(resp: OptimizeResponse) -> str:
    body = resp.model_dump(mode="json")
    body["solver"].pop("solve_ms")
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize(("story", "option"), CASES)
def test_cpsat_never_worse_than_repair_and_invariants_hold(story: str, option: str) -> None:
    rep, cps = solved(story, option, "repair"), solved(story, option)
    assert cps.status == rep.status == "OPTIMAL"
    assert rep.scorecard is not None and cps.scorecard is not None
    assert cps.scorecard.net_benefit_cents >= rep.scorecard.net_benefit_cents
    check_invariants(OptimizeRequest.model_validate(raw_request(story, option)), cps)
    assert cps.solver.stages and all(s.seed == 7 for s in cps.solver.stages)
    assert all(s.num_workers in (None, 1) for s in cps.solver.stages)


@pytest.mark.parametrize(("story", "option"), [c for c in CASES if c[1] in ("C", "D")])
def test_resequence_stages_are_used_only_when_the_plan_keeps_the_line(story: str, option: str) -> None:
    """Review fix: a v2 sequence that compare mode dropped (``resequence_dropped``) kept
    ``used=true`` on its stage although the returned plan has no resequence."""
    resp = solved(story, option)
    lines = {a.payload.line_code for a in resp.actions if a.op == "production_order.resequence"}
    for s in resp.solver.stages:
        if s.name.startswith("resequence:"):
            line = s.name.removeprefix("resequence:").removesuffix(":tiebreak")
            assert s.used == (line in lines and s.fallback_reason is None), s
    if resp.solver.params.get("resequence_dropped"):
        assert not lines and not [s for s in resp.solver.stages if s.name.startswith("resequence:") and s.used]


def test_option_c_gains_from_whole_unit_buys() -> None:
    rep, cps = solved("bd", "C", "repair"), solved("bd", "C")
    assert cps.solver.params["integer_method"] in ("cpsat_optimal", "cpsat_feasible")
    assert cps.scorecard is not None and rep.scorecard is not None
    assert cps.scorecard.net_benefit_cents > rep.scorecard.net_benefit_cents


@pytest.mark.parametrize("story", ["bd", "bd+vn"])
def test_m8_sourcing_terms_on_the_full_profile(story: str) -> None:
    """Case packs of 6, MOQ per style-colour and one capacity per factory (both lanes)."""
    raw = raw_request(story, "D")
    for src in raw["data"]["alt_sources"]:
        src.update(order_multiple_units=6, moq_basis="style", capacity_group=src["factory_code"])
    alt = next(lv for lv in raw["plan_spec"]["levers"] if lv["code"] == "resource_alt_supplier")
    alt["params"] = {"max_buys_per_line": 2}
    req = OptimizeRequest.model_validate(raw)
    for stage in ("repair", "cpsat"):
        r = copy.deepcopy(raw)
        r["solver"]["integer_stage"] = stage
        resp = run_optimize(OptimizeRequest.model_validate(r))
        assert resp.status == "OPTIMAL"
        check_invariants(req, resp)


def _summary(story: str, option: str) -> dict[str, Any]:
    resp = solved(story, option)
    sc = resp.scorecard
    assert sc is not None
    return {
        "status": resp.status,
        "solver": resp.solver.name,
        "inputs_hash": resp.solver.inputs_hash,
        "units": [resp.kpis_before.units_at_risk, resp.kpis_after.units_at_risk],
        "net_benefit_cents": sc.net_benefit_cents,
        "integer": [resp.solver.params.get("integer_method"), resp.solver.params.get("integer_fallback")],
        "resequence": resp.solver.params.get("resequence_methods"),
        "stages": [[s.name, s.status, s.used, s.objective, s.fallback_reason] for s in resp.solver.stages],
        "response_sha256": _digest(resp),
    }


def test_golden_summary_cpsat() -> None:
    """Exact regression of the cpsat mode (everything but ``solve_ms`` is in
    ``response_sha256``). Regenerate after an intentional change with
    ``KEELSON_UPDATE_GOLDEN=1 pytest tests/test_full_options_m8.py -k golden``."""
    got = {f"{story}:{option}": _summary(story, option) for story, option in CASES}
    if os.environ.get("KEELSON_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


_CHILD = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from keelson_contracts.optimizer import OptimizeRequest
from keelson_opt.service import run_optimize
from tests.fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, VN_PORT_CLOSURE, full_request
raw = full_request(OPTIONS["D"], [BD_TRIMS_DELAY, VN_PORT_CLOSURE], plan_id="opt-D")
raw["solver"]["integer_stage"] = "cpsat"
if sys.argv[2] == "reversed":
    for key in ("demand", "po_lines", "receipts", "alt_sources", "inbound_lanes", "allocations"):
        if key in raw["data"]:
            raw["data"][key] = raw["data"][key][::-1]
r = run_optimize(OptimizeRequest.model_validate(raw)).model_dump(mode="json")
r["solver"].pop("solve_ms")
r["solver"].pop("inputs_hash")
for s in r["solver"].get("stages", []):
    s.pop("model_hash", None)
sys.stdout.write(hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
"""


def test_cpsat_option_d_identical_across_processes_hash_seeds_and_row_order() -> None:
    """bd+vn option D (resequence v2, compare mode, integer stage) in fresh processes with
    different hash seeds, and with the input rows reversed (only the hashes of the request may
    differ then)."""
    root = str(Path(__file__).resolve().parents[1])
    digests = set()
    for seed, order in (("1", "as-is"), ("31337", "as-is"), ("7", "reversed")):
        env = {**os.environ, "PYTHONHASHSEED": seed, "LOG_LEVEL": "WARNING"}
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, root, order], capture_output=True, text=True, env=env, check=True
        )
        digests.add(proc.stdout.strip().splitlines()[-1])
    assert len(digests) == 1
