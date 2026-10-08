# 40 · Optimizer service contract (M1: impact + reallocate_inventory)

Service `optimizer` (FastAPI, port 8200, internal only on network `app`). Stateless. Auth: `Authorization: Bearer <service JWT>` signed by engine (HS256 with `OPTIMIZER_SHARED_SECRET` in M1; ES256 later) with claims `{iss:"keelson-engine", aud:"keelson-optimizer", tenant, exp}`; 401 otherwise. Pydantic models live in `packages/contracts-py` (`keelson_contracts.optimizer`) and are imported by both engine and optimizer. OR-Tools 9.15.6755 (GLOP via `ortools.linear_solver.python.model_builder`; CP-SAT later). Never import OR-Tools in the engine.

All quantities int, money int cents, dates ISO. Time buckets = ISO weeks (`YYYY-Www`) in M1.

## Shared input: `ProblemData`
```json
{"tenant":"demo-retail","as_of":"2026-10-05","horizon":{"start_week":"2026-W41","weeks":9},
 "dcs":[{"code":"UK1","outbound_cap_units_wk":50000}],
 "clusters":[{"code":"UK1-COLD-A","dc_code":"UK1"}],
 "skus":[{"code":"PUF-W-BLK-M","style_code":"PUF-W-BLK","retail_price_cents":2500,"unit_cost_cents":900}],
 "inventory":[{"location_type":"dc","location_code":"UK1","sku":"PUF-W-BLK-M","on_hand":1200,"allocated":0,"in_transit":0}],
 "cluster_inventory":[{"cluster_code":"UK1-COLD-A","sku":"…","on_hand":80}],
 "receipts":[{"ref":"PO-26-0101#1","dc_code":"UK1","sku":"…","qty":3000,"eta":"2026-11-02"}],
 "demand":[{"cluster_code":"UK1-COLD-A","sku":"…","iso_week":"2026-W45","units":140}],
 "lanes":[{"from_dc":"DE1","to_dc":"NL1","transit_days":2,"cost_per_unit_cents":35,"max_units_wk":20000}],
 "protected_min":[{"cluster_code":"…","sku":"…","units":10}]}
```
`Perturbation` (subset of the generic union; others accepted and ignored with a warning in M1):
```json
{"op":"lead_time_shift","target":{"kinds":["receipt"],"ids":["PO-26-0101#1"]},"days":7,"prov":"decision"}
{"op":"demand_scale","target":{"kinds":["cluster"],"ids":["UK1-COLD-A"]},"factor_bp":11800,"weeks":["2026-W45"],"prov":"assumption"}
{"op":"capacity_scale","target":{"kinds":["lane"],"ids":["DE1>NL1"]},"factor_bp":3000,"prov":"fact"}
```
(`factor_bp` = basis points, 10000 = ×1.0.)

## `POST /v1/impact`
Request `{data: ProblemData, perturbations: [Perturbation]}` → 
```json
{"kpis":{"units_at_risk":int,"revenue_at_risk_cents":int,"margin_at_risk_cents":int,"shortage_cluster_weeks":int,
         "first_breach":[{"dc_code":"UK1","cluster_code":"…","style_code":"…","week":"2026-W45","date":"2026-11-02"}]},
 "by_dc":[{"dc_code","units_at_risk","revenue_at_risk_cents"}],
 "timeline":[{"cluster_code","style_code","week","projected_available","demand","shortage"}],
 "baseline_kpis":{…same keys, without perturbations…},
 "provenance":{"engine":"keelson-opt","version":"0.1.0","inputs_hash":"sha256…","computed_at":"…"}}
```
Method: per cluster×sku×week, projected available balance from cluster on-hand + DC supply allocated to the cluster (DC stock + receipts by arrival week shared to its clusters pro-rata to demand) − demand; shortages counted where PAB < protected_min (or < 0). `first_breach` = earliest week with shortage per (dc, cluster, style). Deterministic (no randomness).

## `POST /v1/optimize`
Request:
```json
{"data":ProblemData,"perturbations":[…],
 "plan_spec":{"id":"opt-A","title":"Rebalance between DCs","levers":[{"code":"reallocate_inventory","params":{"max_units_total":20000,"allowed_lanes":["DE1>NL1","DE1>UK1","PL1>NL1"]}}],
              "weights":{"lost_margin":10000,"transfer_cost":10000}},
 "solver":{"time_limit_s":5,"seed":7}}
```
Response:
```json
{"status":"OPTIMAL|FEASIBLE|INFEASIBLE","plan_spec_id":"opt-A",
 "kpis_before":{…impact kpis…},"kpis_after":{…},"kpi_delta":{"units_recovered":int,"margin_recovered_cents":int,"transfer_cost_cents":int,"net_benefit_cents":int,"transfers":int},
 "decisions":[{"lever":"reallocate_inventory","from_dc":"DE1","to_dc":"NL1","sku":"…","qty":int,"ship_week":"2026-W43","arrive_week":"2026-W44"}],
 "actions":[{"seq":1,"op":"transfer_order.create","target_ref":null,"expected_version":null,
             "payload":{"from_dc":"DE1","to_dc":"NL1","ship_date":"2026-10-19","lines":[{"sku":"…","qty":400}]}}],
 "explain":{"binding_constraints":[{"name":"lane DE1>NL1 cap W43","shadow_price_cents":int}]},
 "solver":{"name":"GLOP","ortools":"9.15.6755","params":{…},"seed":7,"inputs_hash":"sha256…","solve_ms":int}}
```
LP (M1): variables `x[lane, sku, ship_week] ≥ 0` (transfers; arrive after `ceil(transit_days/7)` weeks), `s[cluster, sku, week] ≥ 0` (served), `l[cluster, sku, week] ≥ 0` (lost), DC stock balance per (dc, sku, week) ≥ 0 incl. perturbed receipts and outbound transfers, served+lost = demand, lane caps (`max_units_wk` × capacity_scale), total ≤ `max_units_total`. Objective `min Σ margin·l·w_lost + Σ cost·x·w_transfer`. Round transfer quantities down to integers (case-pack later), recompute KPIs from the rounded solution (KPIs never from the objective). Group actions: one `transfer_order.create` per (from_dc, to_dc, ship_week) with lines per sku; `ship_date` = Monday of ship_week. `seq` from 1 in deterministic order.

## `GET /healthz`, `GET /readyz`, `GET /v1/levers` → lever catalogue `[{code, title, params_schema, ops:["transfer_order.create"]}]`.

Tests: golden fixture `tests/fixtures/small_problem.json` (built from the Merxa `small` seed shape) asserting OPTIMAL, conservation, non-negativity, KPI recomputation, determinism (same inputs → identical response except `solve_ms`).
