"""Full-profile fixture (180 SKUs x 18 clusters x 9 weeks, 12 POs): the Merxa story invariants,
the candidate option shapes A-D of 80 §4 composed in ONE model each, plan invariants
(capacity, MOQ, one op per PO line, atomic groups, change-set compile), determinism and an
exact golden summary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from keelson_contracts.changeset import ChangeSet
from keelson_contracts.optimizer import LEVER_OPS, ImpactRequest, OptimizeRequest, OptimizeResponse

from keelson_opt.service import run_impact, run_optimize

from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, VN_PORT_CLOSURE, full_data, full_request

GOLDEN = Path(__file__).parent / "fixtures" / "full_problem.golden.json"


@cache
def solved(option: str) -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(full_request(OPTIONS[option], plan_id=f"opt-{option}")))


def ops_of(resp: OptimizeResponse) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for a in resp.actions:
        out[a.op] += 1
    return dict(sorted(out.items()))


def test_story_invariants() -> None:
    data = full_data()
    assert len(data["skus"]) == 180 and len(data["clusters"]) == 18 and data["horizon"]["weeks"] == 9
    assert len({pl["po_code"] for pl in data["po_lines"]}) == 12
    assert {p["line_code"] for p in data["production"]} == {"FL-BD01-A", "FL-BD02-A", "FL-VN01-A"}
    base = run_impact(ImpactRequest(data=data))
    assert base.kpis.units_at_risk == 0
    bd = run_impact(ImpactRequest(data=data, perturbations=[BD_TRIMS_DELAY]))
    risk = {r.dc_code: r.units_at_risk for r in bd.by_dc}
    assert risk["UK1"] > 0 and risk["NL1"] > 0 and risk["DE1"] == risk["PL1"] == 0
    assert {(f.dc_code, f.week) for f in bd.kpis.first_breach} == {
        ("UK1", "2026-W45"),
        ("UK1", "2026-W46"),
        ("NL1", "2026-W45"),
        ("NL1", "2026-W46"),
    }
    vn = run_impact(ImpactRequest(data=data, perturbations=[VN_PORT_CLOSURE]))
    risk = {r.dc_code: r.units_at_risk for r in vn.by_dc}
    assert risk["DE1"] > 0 and risk["PL1"] > 0 and risk["UK1"] == risk["NL1"] == 0
    assert min(f.week for f in vn.kpis.first_breach) == "2026-W46"
    assert {f.style_code[:3] for f in vn.kpis.first_breach} <= {"FLC", "KNT", "CRD", "HDY"}


@pytest.mark.parametrize("option", ["A", "B", "C", "D"])
def test_option_shapes(option: str) -> None:
    resp = solved(option)
    assert resp.status == "OPTIMAL"
    allowed = {op for lv in OPTIONS[option] for op in LEVER_OPS[lv["code"]]}
    assert set(ops_of(resp)) <= allowed
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    sc = resp.scorecard
    assert sc is not None and sc.net_benefit_cents > 0 and sc.changes == len(resp.actions)
    assert sc.net_benefit_cents == resp.kpi_delta.net_benefit_cents
    assert sc.lost_units == resp.kpis_after.units_at_risk


def test_options_rank_as_the_story_says() -> None:
    a, b, c, d = (solved(o).scorecard for o in "ABCD")
    assert a and b and c and d
    # A recovers most but not all (tight sizes); B adds sea-air for the rest.
    assert 0 < a.lost_units and b.lost_units < a.lost_units and b.cost_breakdown.expedite_premium > 0
    assert c.cost_breakdown.nearshore_premium > 0
    # D may use every lever, so it is never worse than any of the narrower options.
    assert d.net_benefit_cents >= max(a.net_benefit_cents, b.net_benefit_cents, c.net_benefit_cents)


@pytest.mark.parametrize("option", ["A", "B", "C", "D"])
def test_plan_invariants(option: str) -> None:
    resp = solved(option)
    req = OptimizeRequest.model_validate(full_request(OPTIONS[option]))
    data = req.data
    # compiles into a typed change set; one op per PO line (Merxa rule); groups contiguous
    ChangeSet.model_validate(
        {
            "id": "cs",
            "tenant": "demo-retail",
            "connector": "merxa",
            "revision": 1,
            "items": [a.change_set_item() for a in resp.actions],
        }
    ) if resp.actions else None
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs))
    closed: set[str] = set()
    prev = None
    for a in resp.actions:
        if a.group != prev and prev is not None:
            closed.add(prev)
        assert a.group is None or a.group not in closed
        prev = a.group
    # expedite capacity per lane and booking week, split < open qty
    pol = {p.ref: p for p in data.po_lines}
    lanes = {il.code: il for il in data.inbound_lanes}
    booked: dict[tuple[str, str], int] = defaultdict(int)
    for d in resp.decisions:
        if d.lever == "expedite_mode_shift":
            booked[(d.lane_code, d.book_week)] += d.qty
            assert d.qty < pol[d.po_line_ref].qty and lanes[d.lane_code].mode == "sea_air"
            assert lanes[d.lane_code].dest_dc == pol[d.po_line_ref].dest_dc
    for (lane, _week), qty in booked.items():
        assert qty <= lanes[lane].weekly_capacity_units
    # alternate sources: usable only, MOQ, weekly capacity, same quantity cut from the line
    src = {(s.factory_code, s.lane_code): s for s in data.alt_sources}
    po_totals: dict[tuple[str, str, str], int] = defaultdict(int)
    cut: dict[str, int] = defaultdict(int)
    for d in resp.decisions:
        if d.lever == "resource_alt_supplier":
            cut[d.replaces_po_line_ref] += d.qty
            lane = next(c for (f, c) in src if f == d.factory_code and lanes[c].dest_dc == d.dest_dc)
            assert src[(d.factory_code, lane)].usable
            po_totals[(d.factory_code, d.dest_dc, d.eta.isoformat())] += d.qty
    for a in resp.actions:
        if a.op == "po.create":
            s = next(
                s
                for s in data.alt_sources
                if s.factory_code == a.payload.factory_code and lanes[s.lane_code].dest_dc == a.payload.dest_dc
            )
            assert s.moq_units <= a.payload.total_qty <= s.weekly_capacity_units
            assert s.supplier_code == a.payload.supplier_code
        if a.op == "po_line.change_qty":
            assert a.payload.qty == pol[a.target_ref].qty - cut[a.target_ref]
        if a.op == "po_line.cancel":
            assert cut[a.target_ref] == pol[a.target_ref].qty
    for d in resp.decisions:
        if d.lever == "resource_alt_supplier":
            assert any(
                a.op == "po.create" and a.group == g
                for g in {x.group for x in resp.actions if x.target_ref == d.replaces_po_line_ref}
                for a in resp.actions
            )


@cache
def vn_solved(levers: tuple[str, ...]) -> OptimizeResponse:
    raw = full_request([{"code": c} for c in levers], [VN_PORT_CLOSURE], plan_id="opt-vn")
    return run_optimize(OptimizeRequest.model_validate(raw))


def test_vn_port_closure_resequences_the_vn_line() -> None:
    resp = vn_solved(("resequence_production",))
    ops = ops_of(resp)
    assert ops.get("production_order.resequence") == 1 and ops.get("po_line.reschedule", 0) >= 1
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    (reseq,) = [a for a in resp.actions if a.op == "production_order.resequence"]
    assert reseq.payload.line_code == "FL-VN01-A"
    raw = full_request([], [VN_PORT_CLOSURE])
    line = next(p for p in OptimizeRequest.model_validate(raw).data.production if p.line_code == "FL-VN01-A")
    assert sorted(reseq.payload.order_codes) == sorted(o.code for o in line.orders if o.movable)
    assert all(a.group == reseq.group for a in resp.actions if a.op == "po_line.reschedule")


def test_vn_port_closure_sea_air_departs_after_the_port_reopens() -> None:
    """VNSGN is closed 2026-10-06..10-15 (its sea-air lanes too): an expedite split departs
    once the port is open again and the booking week has capacity (W42 overlaps the
    closure, so W43), and still lands weeks before the delayed sea receipts."""
    resp = vn_solved(("reallocate_inventory", "expedite_mode_shift"))
    splits = [d for d in resp.decisions if d.lever == "expedite_mode_shift"]
    assert splits and {d.book_week for d in splits} == {"2026-W43"}
    assert {d.lane_code for d in splits} <= {"SGN-DE1-SA", "SGN-PL1-SA"}
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk


def test_vn_port_closure_option_c_is_never_worse_than_its_levers_alone() -> None:
    """Resequenced PO lines are locked for re-sourcing (one op per PO line): option C keeps
    the resequence only when it beats re-sourcing those lines instead."""
    c = vn_solved(("resource_alt_supplier", "resequence_production"))
    alone = [vn_solved(("resource_alt_supplier",)), vn_solved(("resequence_production",))]
    assert c.scorecard is not None and all(r.scorecard is not None for r in alone)
    assert c.scorecard.net_benefit_cents >= max(r.scorecard.net_benefit_cents for r in alone)  # type: ignore[union-attr]
    assert ops_of(c).get("po.create", 0) >= 1
    if "production_order.resequence" not in ops_of(c):
        assert c.solver.params.get("resequence_dropped") is True
        assert any("resequence_production: dropped" in w for w in c.warnings)
    resched = {a.target_ref for a in c.actions if a.op == "po_line.reschedule"}
    altered = {a.target_ref for a in c.actions if a.op in ("po_line.change_qty", "po_line.cancel")}
    assert not resched & altered
    b = vn_solved(("reallocate_inventory", "expedite_mode_shift"))
    d = vn_solved(tuple(lv["code"] for lv in OPTIONS["D"]))
    assert d.scorecard is not None and b.scorecard is not None
    assert d.scorecard.net_benefit_cents >= max(b.scorecard.net_benefit_cents, c.scorecard.net_benefit_cents)


def test_nothing_at_risk_means_nothing_to_do() -> None:
    raw = full_request(OPTIONS["D"], [])
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    assert resp.kpis_before.units_at_risk == 0 and resp.actions == []
    assert resp.scorecard is not None and resp.scorecard.incremental_cost_cents == 0


def _digest(resp: OptimizeResponse) -> str:
    body = resp.model_dump(mode="json")
    body["solver"].pop("solve_ms")
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _summary(option: str) -> dict[str, Any]:
    resp = solved(option)
    sc = resp.scorecard
    assert sc is not None
    return {
        "status": resp.status,
        "solver": resp.solver.name,
        "inputs_hash": resp.solver.inputs_hash,
        "units": [resp.kpis_before.units_at_risk, resp.kpis_after.units_at_risk],
        "scorecard": sc.model_dump(mode="json"),
        "ops": ops_of(resp),
        "decisions": len(resp.decisions),
        "response_sha256": _digest(resp),
    }


def test_golden_summary() -> None:
    """Exact regression of options A-D on the full fixture (everything but ``solve_ms`` is
    in ``response_sha256``). Regenerate after an intentional change with
    ``KEELSON_UPDATE_GOLDEN=1 pytest tests/test_full_options.py -k golden``."""
    got = {o: _summary(o) for o in "ABCD"}
    if os.environ.get("KEELSON_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


_CHILD = """
import hashlib, json, sys
sys.path.insert(0, sys.argv[1])
from keelson_contracts.optimizer import OptimizeRequest
from keelson_opt.service import run_optimize
from tests.fixtures.full_problem import OPTIONS, full_request
r = run_optimize(OptimizeRequest.model_validate(full_request(OPTIONS["D"], plan_id="opt-D"))).model_dump(mode="json")
r["solver"].pop("solve_ms")
sys.stdout.write(hashlib.sha256(json.dumps(r, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
"""


def test_option_d_identical_across_processes_and_hash_seeds() -> None:
    root = str(Path(__file__).resolve().parents[1])
    digests = set()
    for seed in ("1", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "LOG_LEVEL": "WARNING"}
        proc = subprocess.run([sys.executable, "-c", _CHILD, root], capture_output=True, text=True, env=env, check=True)
        digests.add(proc.stdout.strip().splitlines()[-1])
    assert digests == {_digest(solved("D"))}
