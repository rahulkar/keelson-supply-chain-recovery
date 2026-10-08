"""M8 CO₂e and robustness on the real stack, offline (docs/contracts/126-m8-co2-robustness.md).

Runs inside the engine image with the engine's own environment, like the M5–M7 offline scripts
(``docker compose run --rm --no-deps -T -v D:/primark/e2e:/e2e:ro engine-api python -u
/e2e/api_m8_robustness_offline.py``): the engine core reads the tenant's current ``scn/1.0``
snapshot, its agent spec and connector, then calls the real optimizer through the engine's own
``OptimizerClient`` exactly as ``simulate`` does (``runtime.robustness.OptimizeInputs``). No chat is
posted and no run is queued, so no model is ever called.

Steps:
1. Demo reset (``ava``): every step ok.
2. ProblemData from demo-retail's current snapshot (engine core, the ``build_problem_from_snapshot``
   path of ``frame_case``) and the trims-delay scenario compiled deterministically from the taxonomy
   (``SUP.delay``: the TRIM-BD-01 supplier's lines late by the prior's p50, band p10 / p90; an
   assumption).
3. ``/v1/optimize`` for the tenant's candidates A–D plus "Do nothing" with CO₂e and robustness — twice:
   identical scorecards, bands, CO₂e and per-sample vectors; every call < 5 s; the request carries
   the enriched data (unit weights, lane legs, transfer distances) and one seed for every option.
4. Expectations: p10 ≤ p50 ≤ p90; an option that flies (sea-air splits) adds more CO₂e than the
   rebalance; doing nothing has the lowest pessimistic fill rate; every option was evaluated in
   the same futures (one ``crn_key``) and the win rates sum to 100 %; the ``scenario_compare@1``
   props the engine builds (profiles, scores, win rates, uncertainty and emissions notes) validate,
   also with the CO₂e x axis; the default recommendation equals the M3 net-benefit rule.
5. Option scoring admin (``sam`` tenant-admin): a profile change and more samples save as a new
   spec version (201, no change request); invalid profiles are 422 ``spec_invalid``; a stale base is
   409; the earlier version is restored.

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402

TENANT = "demo-retail"
CONNECTOR = "merxa"
SPEC = f"{KEELSON}/api/admin/agents/spec"
CALL_BUDGET_S = 5.0

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


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# ───────────────────────────────── 1. demo reset ─────────────────────────────────


def demo_reset() -> None:
    heading("1. Demo reset")
    ava = ts.keelson_login("ava")
    r = ava.client.post(f"{KEELSON}/api/demo/reset", json={}, headers=ava.headers(), timeout=180.0)
    expect(r.status_code == 200, f"demo reset: HTTP {r.status_code} {r.text[:300]}")
    log = r.json()
    failed = [s["step"] for s in log["steps"] if s["status"] == "failed"]
    expect(log["ok"] is True, f"demo reset failed steps: {failed}")
    say(f"demo reset ok ({len(log['steps'])} steps)")


# ───────────────────────────── 2. problem + scenario ─────────────────────────────


async def _problem_and_spec() -> tuple[Any, Any]:
    from keelson_engine.core import agent_specs
    from keelson_engine.core import connectors as core_connectors
    from keelson_engine.runtime.deps import ConnectorInfo
    from keelson_engine.runtime.problem_scn import build_problem_from_snapshot
    from keelson_engine.runtime.scn_store import CoreScnAdapter
    from keelson_engine.runtime.spec import AgentSpec

    latest = await agent_specs.latest(TENANT)
    spec = AgentSpec.from_row(TENANT, latest["version"], latest["spec"])
    info = ConnectorInfo.from_row(await _maybe(core_connectors.get_connector(TENANT, CONNECTOR)))
    scn = CoreScnAdapter()
    snapshot = await scn.current_snapshot(tenant=TENANT, connector=CONNECTOR)
    if snapshot is None:
        raise E2EFailure("no current scn snapshot for demo-retail (the demo reset syncs one)")
    async with scn.reader(TENANT) as reader:
        bundle = await build_problem_from_snapshot(
            TENANT, reader, snapshot=snapshot, lanes=info.lanes, as_of=spec.planning.as_of or dt.date.today(),
            horizon_weeks=spec.planning.horizon_weeks, planning=spec.planning, connector_config=info.config or {},
        )
    return bundle.data, spec


def trims_delay_scenario() -> Any:
    from keelson_contracts.scenario import DisruptionScenario
    from keelson_engine.runtime.nodes.taxonomy import get_taxonomy

    prior = get_taxonomy().template("SUP.delay").perturbations[0].value.prior
    expect(prior is not None and (prior.p10, prior.p50, prior.p90) == (5, 7, 14), f"SUP.delay prior: {prior}")
    return DisruptionScenario.model_validate({
        "id": "scn-e2e-m8", "tenant": TENANT, "mode": "actual", "event_family": "SUP", "event_type": "SUP.delay",
        "description": "Trims from Meghna are late (offline e2e)",
        "perturbations": [{"op": "lead_time_shift", "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]},
                           "days": prior.p50, "p10": prior.p10, "p90": prior.p90, "prov": "assumption"}],
        "matched": [],
        "assumptions": [{"key": "delay_days", "label": "Supplier delay", "value_text": f"{prior.p50} days",
                         "prov": "assumption"}],
        "config_version": "e2e/m8",
    })


# ───────────────────────────── 3. optimize twice ─────────────────────────────


async def _optimize_all(problem: Any, spec: Any, scenario: Any) -> tuple[list[dict[str, Any]], list[float], list[Any]]:
    from keelson_contracts.optimizer import LEVER_OPS, PlanSpec
    from keelson_engine.runtime.candidates import DO_NOTHING_ID, ConnectorCapabilities, build_candidates
    from keelson_engine.runtime.geo import get_geo
    from keelson_engine.runtime.nodes.planning import option_from
    from keelson_engine.runtime.optimizer_client import get_optimizer
    from keelson_engine.runtime.robustness import OptimizeInputs

    planning = spec.planning
    ops = frozenset(op for codes in LEVER_OPS.values() for op in codes)
    candidates = build_candidates(planning, ConnectorCapabilities(CONNECTOR, "Merxa", (), ops, "config"))
    inputs = OptimizeInputs.build(planning, scenario, problem, scenario.perturbations, get_geo())
    plans = [(c.plan_spec, c.levers) for c in candidates] + [(PlanSpec(id=DO_NOTHING_ID, title="Do nothing", levers=[]), ())]
    options, seconds, requests = [], [], []
    for plan_spec, levers in plans:
        req = inputs.request(plan_spec)
        requests.append(req)
        t0 = time.monotonic()
        resp = await get_optimizer().optimize(req)
        seconds.append(time.monotonic() - t0)
        options.append(option_from(resp, plan_spec.title, levers=levers, problem=problem))
    return options, seconds, requests


def optimize_twice(problem: Any, spec: Any, scenario: Any) -> list[dict[str, Any]]:
    heading("3. /v1/optimize A–D + do nothing with CO2e and robustness, twice")
    first, t1, reqs = run(_optimize_all(problem, spec, scenario))
    second, t2, _ = run(_optimize_all(problem, spec, scenario))
    for opt, s in zip(first, t1, strict=True):
        say(f"  {opt['id']:<11} {s:5.2f} s  CO2e {opt['scorecard'].get('incremental_co2e_kg')} kg  "
            f"net p10/p50/p90 {[opt['scorecard']['bands']['net_benefit_cents'][k] for k in ('p10', 'p50', 'p90')]}")
    expect(max(t1 + t2) < CALL_BUDGET_S, f"an optimize call took {max(t1 + t2):.2f} s (budget {CALL_BUDGET_S} s)")
    for a, b in zip(first, second, strict=True):
        expect(a["scorecard"] == b["scorecard"], f"{a['id']}: scorecards differ between two identical runs")
        expect(a["robustness"] == b["robustness"] and a["emissions"] == b["emissions"],
               f"{a['id']}: robustness / emissions differ between two identical runs")
    req0 = reqs[0]
    expect(req0.emissions is not None and req0.robustness is not None, "requests carry emissions + robustness")
    expect(all(s.unit_weight_g for s in req0.data.skus), "every SKU has a unit weight")
    legs = sum(1 for il in req0.data.inbound_lanes if il.legs)
    expect(legs > 0, "no inbound lane got legs from the map catalogue")
    expect({r.robustness.seed for r in reqs} == {spec.planning.solver_seed}, "one seed (solver_seed) for every option")
    say(f"identical twice; {legs}/{len(req0.data.inbound_lanes)} inbound lanes with legs; seed {req0.robustness.seed}")
    return first


# ───────────────────────────── 4. expectations ─────────────────────────────


def check_options(options: list[dict[str, Any]], spec: Any) -> None:
    from keelson_contracts.blocks import ScenarioCompareProps
    from keelson_contracts.scoring import ScoringConfig
    from keelson_engine.runtime.nodes.planning import default_recommendation
    from keelson_engine.runtime.robustness import decorate_compare, win_rates
    from keelson_engine.runtime.visuals import scenario_compare_props

    heading("4. Bands, CO2e, win rates and the compare block")
    by_id = {str(o["id"]): o for o in options}
    for o in options:
        bands = o["scorecard"]["bands"]
        for name in ("fill_rate_bp", "lost_units", "lost_margin_cents", "net_benefit_cents", "shortage_days"):
            b = bands[name]
            expect(b["p10"] <= b["p50"] <= b["p90"], f"{o['id']} {name} band out of order: {b}")
    nothing = by_id["do-nothing"]["scorecard"]
    expect(nothing["incremental_co2e_kg"] == 0, f"do nothing adds CO2e: {nothing['incremental_co2e_kg']}")
    for o in options:
        if o["id"] != "do-nothing" and int(o["kpi_delta"]["net_benefit_cents"]) > 0:
            expect(o["scorecard"]["bands"]["fill_rate_bp"]["p10"] >= nothing["bands"]["fill_rate_bp"]["p10"],
                   f"{o['id']} is pessimistically worse than doing nothing")
    a, b = by_id.get("opt-A"), by_id.get("opt-B")
    if a is not None and b is not None and any(d.get("lever") == "expedite_mode_shift" for d in b["decisions"]):
        expect(b["scorecard"]["incremental_co2e_kg"] > a["scorecard"]["incremental_co2e_kg"],
               "sea-air option B should add more CO2e than the rebalance A")
        say(f"B flies: {b['scorecard']['incremental_co2e_kg']} kg CO2e > A {a['scorecard']['incremental_co2e_kg']} kg")
    expect(len({o["robustness"]["crn_key"] for o in options}) == 1, "options were not evaluated in the same futures")
    rates = win_rates(options)
    expect(rates is not None and sum(rates.values()) == 10_000, f"win rates: {rates}")
    scoring: ScoringConfig = spec.planning.scoring
    chosen = default_recommendation(options, scoring)
    if scoring.default_profile == "net-benefit":
        expect(chosen == default_recommendation(options), "the default profile changed the M3 recommendation")
    props = decorate_compare(scenario_compare_props(options, recommended_id=chosen), options, scoring)
    expect(props.uncertainty is not None and props.emissions is not None, "uncertainty / emissions notes missing")
    expect(len(props.profiles) == len(scoring.profiles), "profiles missing on the compare block")
    ScenarioCompareProps.model_validate({**props.model_dump(mode="json"),
                                         "axes": {"x": "incremental_co2e_kg", "y": "lost_units"}})
    say(f"recommended {chosen}; win rates {rates}; sampled {props.uncertainty.sampled}; "
        f"factors {props.emissions.source_label} (indicative={props.emissions.indicative})")


# ───────────────────────────── 5. option scoring admin ─────────────────────────────


def scoring_admin() -> None:
    heading("5. Option scoring admin (sam, tenant admin)")
    sam = ts.keelson_login("sam")
    cur = sam.client.get(SPEC)
    expect(cur.status_code == 200, f"GET spec: {cur.status_code}")
    base = cur.json()
    planning = base["spec"]["planning"]
    profiles = planning["scoring"]["profiles"]
    patch = {"planning": {"scoring": {"default_profile": "cost-first"}, "robustness": {"samples": 96}}}
    r = sam.client.put(SPEC, json={"base_version": base["version"], "spec_patch": patch}, headers=sam.headers())
    expect(r.status_code == 201, f"scoring save: HTTP {r.status_code} {r.text[:300]} (not a guarded change)")
    saved = r.json()
    expect(saved["spec"]["planning"]["scoring"]["default_profile"] == "cost-first", "default profile not saved")
    expect(saved["spec"]["planning"]["robustness"]["samples"] == 96, "samples not saved")
    bad = sam.client.put(SPEC, json={"base_version": saved["version"], "spec_patch": {"planning": {"scoring": {
        "profiles": [{**profiles[0], "cost_bp": 99_999}]}}}}, headers=sam.headers())
    expect(bad.status_code == 422 and bad.json().get("code") == "spec_invalid", f"invalid profile: {bad.status_code}")
    stale = sam.client.put(SPEC, json={"base_version": base["version"], "spec_patch": patch}, headers=sam.headers())
    expect(stale.status_code == 409, f"stale base: {stale.status_code}")
    restored = sam.client.post(f"{SPEC}/restore", json={"version": base["version"]}, headers=sam.headers())
    expect(restored.status_code == 201, f"restore: HTTP {restored.status_code} {restored.text[:200]}")
    back = restored.json()["spec"]["planning"]
    expect(back["scoring"] == planning["scoring"] and back["robustness"]["samples"] == planning["robustness"]["samples"],
           "restore did not bring the scoring back")
    say(f"saved {saved['version']}, 422 + 409 refused, restored as {restored.json()['version']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-reset", action="store_true", help="use the current demo data")
    args = parser.parse_args()
    core_init()
    try:
        if not args.skip_reset:
            demo_reset()
        heading("2. ProblemData from the current snapshot + the trims-delay scenario")
        problem, spec = run(_problem_and_spec())
        scenario = trims_delay_scenario()
        say(f"{len(problem.skus)} SKUs, {len(problem.clusters)} clusters, {len(problem.inbound_lanes)} inbound lanes; "
            f"spec {spec.version}")
        options = optimize_twice(problem, spec, scenario)
        check_options(options, spec)
        scoring_admin()
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    print(json.dumps({"ok": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
