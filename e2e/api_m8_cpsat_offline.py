"""M8 CP-SAT integer stage on the real stack (docs/contracts/125-m8-optimizer-cpsat.md), offline.

Runs inside the engine image with the engine's own environment, like the M5-M7 offline scripts
(``docker compose run --rm --no-deps -T -v <repo>/e2e:/e2e:ro engine-api python -u
/e2e/api_m8_cpsat_offline.py``). Needs pg-core, the optimizer and merxa-api up and a current
demo-retail scn snapshot (a sync is requested through the admin API when there is none). No chat is
posted, no run is queued and no model is called: the engine's own runtime functions build the
ProblemData and call the real optimizer through the engine's optimizer client.

Steps:
1. ProblemData from the current demo-retail snapshot (the tenant's active planning defaults with
   the M8 sourcing terms ``order_multiple_units=6``, ``moq_basis="style"`` and
   ``share_factory_capacity=true``) and the TRIM-BD-01 +7 day delay.
2. Candidates opt-C (near-shore + resequence) and opt-D (every lever) with the tenant's lever
   params, each in ``repair`` and ``cpsat`` mode, through the real optimizer:
   * status OPTIMAL / FEASIBLE;
   * every ``po.create``: each line a multiple of 6, each style-colour at least the effective MOQ
     (``ceil(moq / 6) * 6``), the order within its source's weekly capacity; per factory and arrival
     date at most the group capacity; one ``po_line`` op per PO line;
   * ``cpsat`` net benefit >= ``repair`` net benefit for the same option (weights 1.0);
   * ``repair`` responses carry no ``solver.stages`` and no ``integer_stage`` param;
   * ``cpsat`` responses carry stages with the planning seed and one CP-SAT worker, and the integer
     stage's method / fallback in the params;
   * a second ``cpsat`` call is identical except ``solver.solve_ms``;
   * each call within the time budget (``repair`` 5 s, ``cpsat`` 10 s: up to two more LP solves
     and 0.6 deterministic CP-SAT seconds).

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_thin_slice import E2EFailure, expect, heading, say  # noqa: E402

TENANT = "demo-retail"
CONNECTOR = "merxa"
MULTIPLE = 6
OPTIONS = ("opt-C", "opt-D")
BUDGET_S = {"repair": 5.0, "cpsat": 10.0}
TRIM_DELAY = {
    "op": "lead_time_shift",
    "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]},
    "days": 7,
    "prov": "decision",
}

_LOOP = asyncio.new_event_loop()


def run(coro: Any) -> Any:
    return _LOOP.run_until_complete(coro)


def core_init() -> None:
    try:
        from keelson_engine.core.db import init_db
    except ImportError as exc:  # not in the engine image
        print(f"ENV: run this inside the engine image ({exc})", file=sys.stderr)
        raise SystemExit(2) from exc
    run(init_db())


# ───────────────────────────────── 1. ProblemData ─────────────────────────────────


async def _planning() -> Any:
    """The tenant's active planning defaults plus the M8 sourcing terms."""
    from keelson_engine.core import agent_specs
    from keelson_engine.runtime.spec import AgentSpec

    env = await agent_specs.latest(TENANT)
    spec = AgentSpec.model_validate({**env["spec"], "tenant": TENANT})
    planning = spec.planning
    alt = planning.alt_sourcing.model_copy(
        update={"order_multiple_units": MULTIPLE, "moq_basis": "style", "share_factory_capacity": True}
    )
    return planning.model_copy(update={"alt_sourcing": alt})


async def _problem(planning: Any) -> Any:
    from keelson_engine.core.connectors import get_connector
    from keelson_engine.runtime.problem_scn import build_problem_from_snapshot
    from keelson_engine.runtime.scn_store import CoreScnAdapter

    store = CoreScnAdapter()
    snapshot = await store.current_snapshot(tenant=TENANT, connector=CONNECTOR)
    if snapshot is None:
        return None
    conn = await get_connector(TENANT, CONNECTOR)
    as_of = planning.as_of or dt.date.today()
    async with store.reader(TENANT) as reader:
        bundle = await build_problem_from_snapshot(
            TENANT, reader, snapshot=snapshot, lanes=conn.lanes, as_of=as_of,
            horizon_weeks=planning.horizon_weeks, planning=planning, connector_config=conn.config or {},
        )  # fmt: skip
    say(f"problem : snapshot {snapshot.id[:8]}..., {len(bundle.data.skus)} SKUs, {len(bundle.data.po_lines)} PO lines, "
        f"{len(bundle.data.alt_sources)} alternate sources, {len(bundle.data.production)} factory lines")  # fmt: skip
    return bundle.data


def build_problem(args: argparse.Namespace) -> tuple[Any, Any]:
    heading("1. ProblemData from the current demo-retail snapshot (M8 sourcing terms, TRIM-BD-01 +7 d)")
    planning = run(_planning())
    data = run(_problem(planning))
    if data is None:
        say("scn     : no current snapshot; requesting a sync")
        ts.keelson_resync("no snapshot for the M8 CP-SAT checks", timeout_s=args.sync_timeout)
        data = run(_problem(planning))
    expect(data is not None, "no current demo-retail snapshot after the sync")
    expect(any(s.usable for s in data.alt_sources), "the snapshot derives no usable alternate source")
    expect(all(s.order_multiple_units == MULTIPLE and s.moq_basis == "style" for s in data.alt_sources),
           "the derived alternate sources do not carry the M8 terms")  # fmt: skip
    expect(all(s.capacity_group == s.factory_code for s in data.alt_sources), "share_factory_capacity not applied")
    return planning, data


# ───────────────────────────────── 2. optimizer calls ─────────────────────────────────


def _request(planning: Any, data: Any, option: str, stage: str) -> Any:
    from keelson_contracts.optimizer import OptimizeRequest, PlanSpec

    from keelson_engine.runtime.candidates import lever_specs
    from keelson_engine.runtime.nodes.planning import solver_options

    cand = next(c for c in planning.candidates if c.id == option)
    codes = list(planning.enabled_levers) if cand.all_enabled else [c for c in cand.levers if c in planning.enabled_levers]
    spec = PlanSpec.model_validate({"id": option, "title": cand.title, "levers": lever_specs(codes, planning)})
    solver = solver_options(planning.model_copy(update={"solver_integer_stage": stage}))
    return OptimizeRequest(data=data, perturbations=[TRIM_DELAY], plan_spec=spec, solver=solver)


def optimize(req: Any) -> tuple[Any, float]:
    from keelson_engine.runtime.optimizer_client import get_optimizer

    t0 = time.perf_counter()
    resp = run(get_optimizer().optimize(req))
    return resp, time.perf_counter() - t0


def check_invariants(data: Any, resp: Any, label: str) -> None:
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    expect(len(refs) == len(set(refs)), f"{label}: more than one op on a PO line")
    lanes = {il.code: il for il in data.inbound_lanes}
    style = {s.code: s.style_code for s in data.skus}
    group_cap: dict[str, int] = {}
    for s in data.alt_sources:
        if s.capacity_group is not None:
            group_cap[s.capacity_group] = min(group_cap.get(s.capacity_group, s.weekly_capacity_units),
                                              s.weekly_capacity_units)  # fmt: skip
    per_group: dict[tuple[str, str], int] = defaultdict(int)
    creates = 0
    for a in resp.actions:
        if a.op != "po.create":
            continue
        creates += 1
        p = a.payload
        src = next(s for s in data.alt_sources if s.factory_code == p.factory_code and lanes[s.lane_code].dest_dc == p.dest_dc)
        total = sum(line.qty for line in p.lines)
        expect(all(line.qty % MULTIPLE == 0 for line in p.lines), f"{label}: a po.create line is not a multiple of {MULTIPLE}")
        by_style: dict[str, int] = defaultdict(int)
        for line in p.lines:
            by_style[style[line.sku]] += line.qty
        expect(all(q >= src.moq_effective for q in by_style.values()),
               f"{label}: a style below the MOQ {src.moq_effective} in {dict(by_style)}")  # fmt: skip
        expect(total <= src.weekly_capacity_units, f"{label}: po.create {total} over the weekly capacity")
        if src.capacity_group is not None:
            per_group[(src.capacity_group, p.eta_dc.isoformat())] += total
    for (group, eta), qty in per_group.items():
        expect(qty <= group_cap[group], f"{label}: factory {group} gets {qty} units for {eta} (capacity {group_cap[group]})")
    say(f"{label:16s}: invariants hold ({creates} po.create)")


def _body(resp: Any) -> dict[str, Any]:
    out = resp.model_dump(mode="json")
    out["solver"].pop("solve_ms")
    return out


def check_option(planning: Any, data: Any, option: str) -> None:
    results: dict[str, Any] = {}
    for stage in ("repair", "cpsat"):
        req = _request(planning, data, option, stage)
        resp, elapsed = optimize(req)
        label = f"{option} {stage}"
        expect(resp.status in ("OPTIMAL", "FEASIBLE"), f"{label}: status {resp.status} ({resp.solver.status_detail})")
        expect(resp.scorecard is not None, f"{label}: no scorecard")
        check_invariants(data, resp, label)
        p = resp.solver.params
        if stage == "repair":
            expect(not resp.solver.stages, f"{label}: repair responses carry no stages")
            expect("integer_stage" not in p, f"{label}: repair responses carry no integer_stage")
        else:
            stages = resp.solver.stages
            expect(stages, f"{label}: no solver stages")
            expect(all(s.seed == planning.solver_seed for s in stages), f"{label}: a stage without the planning seed")
            expect(all(s.num_workers in (None, 1) for s in stages), f"{label}: a CP-SAT stage with several workers")
            expect(p.get("integer_stage") == "cpsat" and p.get("cpsat_deterministic") is True,
                   f"{label}: params {json.dumps({k: p.get(k) for k in ('integer_stage', 'cpsat_deterministic')})}")  # fmt: skip
            expect("integer_method" in p, f"{label}: no integer_method (the option re-sources)")
            again, _ = optimize(req)
            expect(_body(again) == _body(resp), f"{label}: a second identical request differs (beyond solve_ms)")
        expect(elapsed < BUDGET_S[stage], f"{label}: {elapsed:.2f}s over the {BUDGET_S[stage]:.0f}s budget")
        sc = resp.scorecard
        say(f"{label:16s}: net {sc.net_benefit_cents / 100:,.2f} EUR, lost {sc.lost_units} units, {sc.changes} changes, "
            f"{elapsed:.2f}s, solver {resp.solver.name}"
            + (f", integer {p.get('integer_method')}/{p.get('integer_fallback', '-')}" if stage == "cpsat" else ""))  # fmt: skip
        results[stage] = resp
    rep, cps = results["repair"].scorecard, results["cpsat"].scorecard
    expect(cps.net_benefit_cents >= rep.net_benefit_cents,
           f"{option}: cpsat net {cps.net_benefit_cents} below repair {rep.net_benefit_cents}")  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sync-timeout", type=float, default=300.0, help="seconds to wait for one scn sync")
    args = parser.parse_args()
    core_init()
    try:
        planning, data = build_problem(args)
        heading("2. opt-C / opt-D in repair and cpsat mode through the real optimizer")
        for option in OPTIONS:
            check_option(planning, data, option)
    except E2EFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    say("PASS: M8 CP-SAT integer stage (offline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
