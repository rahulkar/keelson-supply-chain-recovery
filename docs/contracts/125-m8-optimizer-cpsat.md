# 125 · M8 — Optimizer: CP-SAT for MOQ and resequencing

Workstream 5 of M8 (`120-m8-overview.md`). Builds on 40 (optimizer), 80 (M3 levers), 95 (M5
re-plan), 110 (four-eyes). Authoritative where it differs from 40/80 for `integer_stage="cpsat"`.

**One sentence:** an opt-in `SolverOptions.integer_stage="cpsat"` turns the LP rounding and repair
of `resource_alt_supplier` (and the expedite splits competing for the same PO lines) into a
whole-unit CP-SAT stage with real sourcing terms (MOQ per order or per style-colour, case packs,
shared factory capacity, order and split limits), replaces the weighted-completion proxy of
`resequence_production` by a CP-SAT model that prices completion days by the simulator's lost
margin per arrival week (closure-aware, due dates, per-order pinning), and records every solve;
`"repair"` stays the default and is byte-identical to M7.

## 1. Scope

| Item | Status |
|---|---|
| `SolverOptions.integer_stage` opt-in switch; `repair` byte-identical to M7 | done |
| Contract additions (omitted at defaults): `AltSource.order_multiple_units/moq_basis/capacity_group/max_orders`, `ResourceAltSupplierParams.max_buys_per_line`, `ResequenceProductionParams.frozen_days/lateness_cents_per_unit_day`, `SolverStage` + `SolverInfo.stages`, `CompareOption.integer`, guard reason `compliance_block_removed` | done |
| `keelson_opt/cpsat.py` shared runner (workers, seed, deterministic limits, wall guard, status mapping, overflow guard, stage hash, request budget) | done |
| Whole-unit CP-SAT stage for `resource_alt_supplier` + `expedite_mode_shift` (`levers/alt_integer.py`) | done |
| Repair path honours the new sourcing terms in both modes | done |
| Resequence v2 (`levers/resequence_cpsat.py`) + `frozen_days` in both modes | done |
| Per-solve metadata and flat params in `cpsat` mode | done |
| Golden (`full_problem.cpsat.golden.json`) and performance coverage | done (budget deviation §9) |
| Engine: spec fields, `_alt_sources` terms, solver options for planning and re-plan, option summary, `CompareOption.integer`, stub stages | done |
| UI: whole-unit caption in ScenarioCompare; Admin → Agents "Planning solver" card | done |
| Guard: removing a blocked factory is a four-eyes change | done |
| Offline real-stack e2e `e2e/api_m8_cpsat_offline.py` | written, syntax-checked (run by the integrator) |
| Cross-line assignment, line calendars, warm-start re-plan, demo-retail default flip | **Deferred** (§10) |

## 2. Principles

1. **Opt-in, request-scoped.** `SolverOptions.integer_stage: "repair" | "cpsat"`, default `repair`
   and omitted from dumps while `repair`, so every M1–M7 request, `inputs_hash`, golden and LLM
   replay key is unchanged (`tests/test_full_options.py` goldens and `test_cpsat_integer_m8.py::
   test_repair_is_the_default_and_byte_identical`). The response is a pure function of the request.
2. **Integer and deterministic.** One CP-SAT worker (`CPSAT_NUM_WORKERS`, default 1),
   `random_seed` = `SolverOptions.seed`, every solve stops on `max_deterministic_time` (code
   constants), models are built in sorted order, objectives are exact integers checked against
   int64 overflow. A wall-clock stop is never used as a result.
3. **Same plan ledger.** Every stage writes the M3 `Plan` (`AltBuy`, `Expedite`, `EtaMove`,
   `LineSequence`); KPIs, scorecard, decisions and actions are recomputed by the simulator and keep
   their shape; `_keep_improving` (never worse than doing nothing) still runs last.
4. **Never worse than the repair** for the integer stage: its units are used only when the
   simulator rates the completed plan higher (§4.4); any failure falls back to the repair plan.

## 3. Shared runner (`keelson_opt/cpsat.py`)

* `run_cpsat(model, *, seed, det_limit, time_limit_s, presolve=True) -> CpRun`: `num_workers =
  cpsat_workers()` (moved here from `resequence.py`, re-imported there), `random_seed`,
  `max_deterministic_time = det_limit`, `max_time_in_seconds` = wall guard,
  `linearization_level=2`, `cp_model_probing_level=0`, `cut_level=0` (calibrated on the full
  profile: level 1 stalled at a 40 % gap; probing and generic cuts cost budget without tightening
  these assignment/flow models). `CpRun` carries status, the **exact** integer objective (recomputed
  from the solution, CP-SAT reports a double), best bound, deterministic limit/time (ms), workers,
  sizes and `guard_hit` (= `cpsat.stopped_by_wall_clock`: an incomplete answer, FEASIBLE or UNKNOWN,
  before the deterministic limit - only the wall-clock cap stops a solve there; decided on the
  deterministic time because CP-SAT can stop on the cap with a reported wall time just short of it,
  review fix: machine-dependent, discarded → `wall_clock_guard`).
* `wall_guard_ms(time_limit_s)`: env `OPTIMIZER_CPSAT_WALL_GUARD_MS` (default 3000; invalid or
  negative → default), never above the request's `time_limit_s`. A safety cap only (120 §3.5).
* `objective_fits(terms, offset)`: Σ|coef|·max|value| + |offset| < 2^61, else the stage falls back
  with `objective_overflow` (never a `MODEL_INVALID`).
* `Budget`: deterministic seconds per request — **0.6** for all CP-SAT stages together, of which
  resequencing may use **0.3**; a stage that would get less than 0.005 falls back with
  `budget_exhausted`.
* `stage_hash(inputs_hash, stage, payload)` = sha256 of canonical JSON `{inputs_hash, stage,
  payload}` where `payload` is the stage's own inputs (locked refs, booking load, moves, fixed
  decisions, transfers, repair decisions, pinned refs, hint). Never a hash of the solver's proto.
* `StageLog`: the `SolverInfo.stages` of a `cpsat` request (inert in `repair`: no records, no
  hashes).

## 4. Whole units for `resource_alt_supplier` (+ expedite) — `levers/alt_integer.py`

### 4.1 Sourcing terms (contracts; both modes)

| Field | Meaning |
|---|---|
| `AltSource.order_multiple_units` (≥ 1, default 1) | case pack: every `po.create` line quantity is a multiple; effective MOQ `moq_effective = ceil(moq_units / m) · m` |
| `AltSource.moq_basis` (`po` \| `style`, default `po`) | `style`: the MOQ applies to each style-colour within a purchase order (the KB rule "MOQ per style-colour") |
| `AltSource.capacity_group` (code, default none) | rows with the same group share one weekly capacity = the smallest `weekly_capacity_units` of the group's rows (one factory shipping on two lanes) |
| `AltSource.max_orders` (≥ 1, default none) | at most this many purchase orders (distinct arrival weeks) from the row over the horizon |
| `ResourceAltSupplierParams.max_buys_per_line` (≥ 1, default none) | at most this many buys (source × arrival week) replace one PO line |

A purchase order is (source row, arrival week); `build_plan_actions` groups its buys per SKU into
`po.create` lines (multiples apply per line). The LP gets one extra row per capacity group and
arrival week (`alt group <g> cap <week>`); the other terms are integer rules.

### 4.2 Repair with the terms (both modes)

When any term is set (`has_terms`), `planner._alt_decisions` calls `repair_terms` instead of the M7
`_moq_repair` (which stays byte-identical without terms): (1) buys per line — keep the largest
(ties: earliest week, then source); (2) floor every (source, week, SKU) total to the case pack,
taking the remainder from the highest PO-line refs; (3) orders per source — keep the largest POs
(ties: earliest week); (4) group capacity — trim whole cases from the last (source, SKU) blocks;
(5) MOQ per basis — top up in whole cases (headroom of the unit's candidate lines, largest current
buy first, within source and group capacity, open quantities, buys per line and budgets) or drop,
whichever has the higher recomputed weighted net benefit on the affected pairs (ties: drop; a
dropped line is valued with the split the expedite lever would likely make, as in M7).

### 4.3 CP-SAT model (`solve_integer`, `cpsat` only, when the plan re-sources)

Over **every** phase-1 alternate candidate (not only those the LP used) and the expedite candidates
of the same PO lines; the repair's splits of other lines stay fixed (their arrivals, lane load and
budget use enter as constants):

* `n[a] ∈ [0, ub]`, `e[c] ∈ [0, ub]`; `r[ref]`, `sel[ref, lane]` bools; `Σ sel + r ≤ 1` per PO line
  (one op per line, one split lane — replaces the M7 "larger lever wins" heuristic);
  `Σ n ≤ open qty` per line.
* Expedite capacity per (lane, booking week) net of the bookings that fly and the kept splits;
  expedite / alternate `max_units_total` and `max_premium_cents`.
* Per purchase order `y[s,w]`: `Σ n ≤ cap · y`; MOQ `Σ n ≥ moq_eff · y` (`po`) or per style
  `y_st ≤ y`, `n ≤ ub · y_st`, `Σ_style n ≥ moq_eff · y_st`; cases `Σ_{(s,w,sku)} n = m · k`;
  group rows `Σ ≤ group cap`; `Σ_w y[s,·] ≤ max_orders`; buys per line through per-buy bools.
* **Recourse = the LP** on the DC × SKU pairs any candidate touches, transfers fixed at the floored
  phase-1 values: `inv[t] = inv[t-1] + fixed supply + arrivals − removals − served sales − served
  buffer`, `inv ≥ 0`, requirements from `reallocate._requirements` summed over the DC's clusters
  (one pool, one SKU margin: exact). Objective = `OBJ_SCALE = 40 000` × the phase-1 objective
  restricted to those pairs, constants dropped: `Σ 4·m·W_lost·(lost sales) + m·W_lost·(lost buffer)
  + Σ (4·W_cost·premium + 400)·units` (weights in bp; `composed.unit_cost_scaled` is the one place
  that maps the LP's per-unit cost `unit_cost(w, premium) = w·premium + 0.01` to integers, so a
  later per-unit cost term — e.g. CO₂ — is added in one place for both solvers).
* Hint: the repair decisions plus a greedy recourse (complete); its objective `H` bounds acceptance.
* Limits: 0.15 deterministic seconds (from the request budget); > 60 000 estimated variables →
  `model_too_large`.

### 4.4 Acceptance (simulator guard) and completion

1. CP-SAT returns OPTIMAL/FEASIBLE without hitting the guard (else `time_limit_no_solution`,
   `infeasible`, `model_invalid`, `wall_clock_guard`).
2. Its units differ from the repair's and its objective is ≤ `H` (else `no_improvement`).
3. On the pairs either set touches, with the phase-1 transfers, its weighted net is ≥ the repair's
   (`simulator_not_better` when lower; equal and not a lower objective → `no_improvement`).
4. **Screen:** the CP-SAT units on the transfers of the repair's own phase 2 (feasible for any buys
   and splits: transfers are reserved at the source) against that repair completion, both after
   `prioritize_allocation` ran on copies, on the pairs either touches (`planner._lookahead`); not
   strictly better → `simulator_not_better` / `no_improvement` without another LP solve.
5. **Completion:** phase 2 with the CP-SAT buys **and** splits fixed (`Fixed.exp`: only those split
   variables exist, `lb = ub = q`; only the transfers are re-optimised) against the repair
   completion, compared the same way. The CP-SAT completion is used only when strictly better; a
   phase 2 that does not reproduce the fixed units → `solver_error`.

Otherwise the repair plan is returned exactly as in `repair` mode. Exceptions inside the stage are
logged and become `solver_error`. Technical fallbacks add the warning `resource_alt_supplier:
whole-unit CP-SAT stage fell back to LP rounding (<reason>)`; `no_improvement` /
`simulator_not_better` / `no_candidates` are normal outcomes (params and stages only).

**Compare mode** (resequence moved PO lines and expedite / re-sourcing is requested): in `cpsat`
mode both candidates (with and without the resequence) are completed by the repair's phase 2 and
compared through `_lookahead`; the integer stage then runs once, for the winner, reusing its
completion. (M7 compares bare phase-1 plans; with the v2 sequence that kept the resequence on the
full-profile D options although the completed plan without it was better — §9.)

## 5. Resequencing v2 (`levers/resequence_cpsat.py`; `frozen_days` in both modes)

* Same line model as M3 (`resequence._line_model`: durations, readiness releases, the on-time +
  own-delay reference, the anchor, the fluid single machine).
* **Frozen zone** (`frozen_days`, both modes): the movable orders planned to start before `as_of +
  frozen_days` (the longest such prefix in sequence order) stay first in their current order; the
  rest is resequenced (`order_codes` still lists every movable order). With at most one order left
  the line is unchanged.
* **Value tables**: per PO line on the line and per completion-day change its orders can see, the
  receipt's new eta through the lane-closure rules (`resequence._moved_eta`); days that lose the
  receipt are removed; a **pinned** (capacity-booked) PO line may only take days that keep its
  current eta, so the rest of the line can still move (M3 rejects the whole line). Each distinct
  arrival week is priced once: `w_lost_bp × lost margin` of the pair with only this PO line moved
  (single-pair simulation).
* **Model** (time-indexed): `x[o,d] = 1` when order `o` completes on day `d`; exactly one day per
  order; cumulative fluid capacity per day and release threshold (≤ 8 thresholds); a single-order
  PO line's cost sits on its `x`, a multi-order line prices `max(day − reference day)` through
  `add_max_equality` + `add_element`. Solve **A**: Σ table cost + `10 000 ×
  lateness_cents_per_unit_day × units × days late` (late = completion after the PO line's
  ex-factory relative to the anchor, Merxa's `late_vs_ex_factory`). Solve **B** (only when A is
  proven optimal, with the budget A left, hinted with A): keep A's optimum, minimise units × days
  late first, then the total day deviation from the current schedule (stability).
* Budget per line `min(0.15, 0.02 + 0.0007 · orders)`, at most 0.3 per request; `presolve=False`
  (the presolved time-indexed model found no solution in the line's budget). A is not hinted with
  the M3 sequence (the hinted search stayed at the hint's cost on the VN line).
* **Choice**: the orders are sequenced by their CP-SAT day (ties: the M3 order) and applied back to
  back as Merxa re-dates them; the result is kept only when the simulator rates it strictly better
  than the M3 result on the PO lines either moves (ties: M3). `resequence_methods` shows
  `<line>:cpsat_v2_optimal|cpsat_v2_feasible` or the M3 method. Lines over 400 orders, 400 days or
  60 000 order-days use the M3 path (`model_too_large`).

## 6. Per-solve records and params (`cpsat` only)

`SolverInfo.stages: [SolverStage]` in solve order: `resequence:<line>` (+
`resequence:<line>:tiebreak`) per line, `booking_decision` (M5 LP), `lp_phase1`, then either (one
pass) `integer`, `lp_phase2` (the repair's completion), `lp_phase2:cpsat` (the CP-SAT units'
completion, only when the screen passes) or (compare mode) `lp_phase1:no_resequence`, `lp_phase2`,
`lp_phase2:no_resequence`, `integer`, `lp_phase2:cpsat`. The integer stage is recorded when it is
solved and its `used` / `fallback_reason` settled once its completions decided (review fix: it was
appended after them). A stage name is at most 160 characters (`resequence:` + a 128-character line
code + `:tiebreak`).

```json
{"name": "integer", "solver": "CP-SAT", "status": "OPTIMAL", "used": true, "seed": 7,
 "model_hash": "<64 hex>", "objective": 152170142000, "bound": 152170142000,
 "det_time_limit_ms": 150, "det_time_ms": 89, "num_workers": 1,
 "num_variables": 5288, "num_constraints": 6120, "fallback_reason": null}
```

`status` ∈ OPTIMAL/FEASIBLE/INFEASIBLE/UNKNOWN/MODEL_INVALID/SKIPPED (SKIPPED: not solved, see
`fallback_reason` ∈ `no_candidates, model_too_large, objective_overflow, time_limit_no_solution,
infeasible, model_invalid, wall_clock_guard, budget_exhausted, no_improvement,
simulator_not_better, solver_error`). `used` marks the solves whose result is in the returned plan:
the resequence stages of a line whose sequence the returned plan does not keep (compare mode
dropped it, the simulator check or the never-worse pruning rejected it) and every stage of a
do-nothing fallback are reported unused; when the never-worse pruning (`_keep_improving`) drops a
whole LP lever afterwards, the LP / integer stages that produced it keep `used`.
No wall times: `solve_ms` stays the only volatile field. Flat params: `integer_stage`,
`integer_method` (`cpsat_optimal|cpsat_feasible|lp_rounding`), `integer_gap_bp`,
`integer_fallback`, `cpsat_num_workers`, `cpsat_deterministic`; `solver.name` gains `CP-SAT` when
CP-SAT units or a v2 sequence are used.

## 7. Engine, web, contracts chain

* **Spec** (`runtime/spec.py`, omitted from dumps at their defaults): `PlanningDefaults.
  solver_integer_stage` (`repair`); `AltSourcingDefaults.order_multiple_units` (1, ≤ 100 000),
  `moq_basis` (`po`), `share_factory_capacity` (false), `max_orders_per_source` (none, 1..52).
* `nodes.planning.solver_options(planning)` builds `SolverOptions` for planning **and** re-plan.
* `problem_scn._alt_sources` passes the terms to every derived source (`capacity_group` = the
  factory when shared — fixes the per-lane double counting of derived capacity); explicit connector
  `alt_sources` already pass through `AltSource.model_validate`.
* Option dicts of `cpsat` responses get `option["solver"] = {integer_stage, integer_method, gap_bp,
  fallback_reason, resequence_methods}`; `visuals.compare_integer` maps it to `CompareOption.integer`
  (`CompareInteger {method, gap_bp ≤ 10 000 (CP-SAT methods only), fallback_reason (lp_rounding
  only)}`) for options with an integer stage. `repair` option dicts and block props are unchanged.
* **Guard**: removing a factory from `planning.alt_sourcing.blocked_factories` relaxes compliance →
  guarded agent-spec change (`compliance_block_removed`, field
  `planning.alt_sourcing.blocked_factories`, also in the catalog's `guarded_fields`): 202 + change
  request for a tenant-admin, applied for a platform-admin. Adding a factory, the integer stage and
  the commercial terms are not guarded.
* **Web**: ScenarioCompare shows a muted caption under the levers ("Whole units: CP-SAT, proven
  optimal" / "… best found (gap 0.4 %)" / "Whole units: rounded from the LP (CP-SAT fallback: time
  limit)"); Admin → Agents "Planning solver" card (integer stage select, MOQ, MOQ basis, case pack,
  orders per source, shared capacity checkbox, block list with the second-admin note).
* Contract chain regenerated (`block.scenario_compare.v1.props`, `admin.change-request`,
  `block.envelope` schemas and Zod). The engine parses responses with the shared models
  (`extra=forbid`), so contracts-py, the optimizer and the engine must ship together.

## 8. Data, config, security

* **Migrations:** none (the optimizer is stateless; spec and plan options are existing JSONB).
* **Env:** `OPTIMIZER_CPSAT_WALL_GUARD_MS` (optional, default 3000) — safety cap only, falls back
  with a recorded reason. `CPSAT_NUM_WORKERS` (existing, compose and Cloud Run set 1); > 1 is allowed
  but recorded `cpsat_deterministic=false` + a warning. No compose, Cloud Run or `.env.example`
  change. All budgets that affect results are code constants (120 §3.5).
* **Security:** no new untrusted input (every CP-SAT input is a strict int or code validated by
  ProblemData); the optimizer stays internal without egress; request concurrency 1 and the body
  limit unchanged; size guards (variables, orders, days, order-days), deterministic budgets and the
  wall guard bound CPU and memory per request; the overflow guard prevents invalid models;
  exceptions in a stage are caught, logged and become fallbacks (never a 500). Relaxing the
  compliance block list needs a second admin. `po.create` / `production_order.resequence` keep their
  four-eyes approval policy.

## 9. Tests, measurements and deviations

**Tests** (all offline, no model): optimizer `tests/test_cpsat_integer_m8.py` (switch byte-identity,
stages in solve order, `used` under a do-nothing fallback, determinism, one op per line, MOQ per PO
/ style, cases, capacity groups, split limits, budgets, **30 random networks: cpsat net ≥ repair net
and every invariant in both modes**, **a tiny instance: the CP-SAT optimum equals an exhaustive
search over the integer model, and for every feasible whole-unit decision the CP-SAT objective is
`OBJ_SCALE` × the phase-2 GLOP objective** (LP consistency), every fallback returns the repair plan,
workers > 1, runner helpers, HTTP 200/422 and the lever catalogue), `tests/test_resequence_m8.py`
(v2 beats Smith's rule on the VN closure story, ties keep M3, nothing to gain, pinned line, frozen
prefix in both modes, lateness tie-break, **value tables = a direct simulation of the moved PO line**
(reseq story and the closure-hit VN line), 200-order line deterministic and never worse than M3, size
fallback, solver error, a 128-character line code), `tests/test_full_options_m8.py` (8 full-profile
cases cpsat ≥ repair + invariants, resequence `used` against the returned plan, M8 terms on the full
profile, golden, option D bd+vn identical across processes, hash seeds and reversed input rows),
`tests/test_perf_m8.py`; contracts `tests/test_m8_optimizer_cpsat.py` + contracts-ts
`tests/m8-cpsat.test.ts`; engine `tests/runtime/test_m8_cpsat_planning.py` (incl. a lane-signal
re-plan flow carrying the integer stage), `tests/platform/test_m8_cpsat_admin.py`; viz
`tests/m8-cpsat.test.tsx`; web `src/lib/admin/planning.test.ts`, `test/admin-planning.test.tsx`.

**Full profile, net benefit `cpsat` vs `repair`** (golden): C/BD 16.13 M vs 13.85 M cents,
C/VN 6.35 M vs 5.51 M, C/BD+VN 22.48 M vs 19.57 M, D/VN 8.01 M vs 7.98 M; A, B, D/BD and D/BD+VN
equal (the D integer stage is rejected by the simulator guard). Resequence only, VN closure: 6.12 M
(v2) vs 2.29 M (Smith's rule).

**Deviations from 120 / the brief**

1. *Budget:* the brief asked for < 5 s in `cpsat` mode for the full profile. The `cpsat` path runs
   up to two more GLOP solves (compare mode: the second repair completion; the CP-SAT completion when
   the screen passes) and up to
   0.6 deterministic CP-SAT seconds (5–8× in wall time). Measured on the shared 6-CPU test host at
   load ~6 (2026-10-09, all levers): 3.8 s (BD), 5.6 s (VN), 6.9 s (BD+VN), 7.9 s over HTTP, against
   2.2 / 2.7 / 2.9 s for `repair` in the same run (the unchanged M7 perf tests themselves exceeded
   5 s in another run under higher load). `tests/test_perf_m8.py` and the e2e use **10 s** (twice
   S4); `repair` keeps the 5 s. `cpsat` is opt-in and not the demo default (§10). Review re-run on
   the same shared host (2026-10-10, three runs): `cpsat` 4.3-5.2 s (BD), 7.4-8.5 s (VN),
   9.2-10.8 s (BD+VN) and 10.8-15.7 s over HTTP, against 3.4-5.5 s for `repair` BD+VN (base commit
   and branch alike) - so the 10 s `cpsat` perf tests failed there too (BD+VN and HTTP). Profile of
   one BD+VN request (cProfile): 4 GLOP solves (2 more than `repair`), ~1.3 s of `_lookahead`
   simulations, the integer stage ~0.85 s wall for 0.15 det-s, resequence v2 ~1.1 s wall for 0.16
   det-s plus ~0.7 s of value-table simulations. Meeting 5 s needs a design change (e.g. one shared
   completion in compare mode, cheaper value tables), not a tuning of the budgets.
2. *Compare mode completes both candidates* (one more GLOP solve) instead of deciding on bare
   phase-1 plans: with the phase-1 decision the `cpsat` D options ended below `repair`
   (VN 7.56 M vs 7.98 M, BD+VN 26.94 M vs 27.36 M).
3. *Whole-plan completion check*: the brief accepted CP-SAT units after the partial simulator
   check; this also compares against the repair's phase-2 completion - screened without an LP solve
   (the CP-SAT units on the repair completion's transfers), then with the CP-SAT units' own phase 2
   (one more GLOP solve, only when the screen passes) - so the integer stage is never worse than the
   repair on the returned plan.
4. *Budgets*: integer 0.15 (brief 0.2), request 0.6 (brief "≤ 0.6 per request" for resequencing
   only), resequencing `min(0.15, 0.02 + 0.0007·n)` per line and 0.3 per request (brief
   `min(0.2, 0.03 + 0.001·n)`, 0.6) — calibrated on the full profile; wall guard default 3000 ms
   (brief 1500: the BD+VN integer model needs ~1.3 s wall under load).
5. *Resequence v2 model*: time-indexed over completion days with cumulative fluid capacity instead
   of intervals + `add_division_equality` + `add_element` per order (much faster to a first
   solution at these sizes); lines are also limited to 60 000 order-days, so a 400-order line with
   ~180 days uses the M3 path (`model_too_large`).
6. *Extra fallback reason* `budget_exhausted` (the per-request CP-SAT budget).
7. *Spec doc number* 125 (the brief said `120-m8-cpsat.md`; 120 allocates 125).
8. *Engine spec fields* use `exclude_if` like the contracts, so default spec dumps (admin API,
   stored versions) are unchanged; the web drops an M8 setting again when it is set back to its
   default.
9. *Brute-force and objective-consistency unit tests*: added in the review
   (`test_cpsat_matches_brute_force_and_the_lp_objective`, one tiny instance with three alternate
   candidates and one split; every feasible decision, not only the floored LP one, is checked
   against GLOP).
10. *Re-plan path*: the shared `solver_options()` unit test plus, added in the review, a lane-signal
    re-plan flow with the engine stubs (`test_the_replan_flow_carries_the_integer_stage`).
11. *400-order resequence*: the brief's 400-order synthetic line exceeds the 60 000 order-day cap,
    so `test_perf_m8.py` only shows that it falls back to the M3 path quickly; v2 itself is
    exercised up to 200 orders.

## 10. Deferred

* **Cross-line assignment** of production orders (optional intervals per eligible line): needs a
  Merxa op `production_order.move {to_line_code}`, a scn/1.0 `factory_line` entity with
  capabilities, an engine migration (not allocated in 120 §2), a problem_scn mapping and a
  `LEVER_OPS` update with the capability check.
* **Line calendars** (holidays, reduced Saturdays) in sequencing: Merxa `line_calendar` → a scn
  entity → `ProductionLine.calendar` → cumulative-capacity tables.
* **Warm-start re-plan**: `SolverOptions.hint {alt, expedite, sequences}` from the previous plan
  version plus a stickiness penalty (plan §9, 95).
* **Case-pack multiples on `po_line.split` and transfer lines.**
* **Flipping demo-retail / value-fashion-eu to `cpsat` by default** (needs LLM replay re-recording
  with Gemini credits, and the budget question of §9.1).

## 11. Known issues

* Timing under host load: the `cpsat` perf tests (10 s) and the M7 perf tests (5 s) are wall-clock
  checks; on an overloaded host they can fail without a functional regression. On the shared M8
  test host the `cpsat` BD+VN and HTTP cases failed the 10 s in the review's runs (§9.1); rerun them
  on a quiet host (the integrator) - the brief's 5 s is not met by design (§9.1).
* Wall time per deterministic second grows with the time-indexed model: the 180-order VN line
  (~4 900 variables) runs at 5-8×, a 200-order synthetic line (~34 000 variables) at ~20× (0.149
  det-s ≈ 2.7-3.0 s on the shared host), right at the 3000 ms default cap, and lines up to the
  60 000 order-day limit can exceed it. When the cap fires the line falls back to the M3 sequence
  with `wall_clock_guard` recorded - correct but machine-dependent. Calibrate the cap (or lower
  `RESEQ_V2_MAX_CELLS`) on the reference container before `cpsat` becomes a default (§10).
  `tests/test_resequence_m8.py::test_long_line_is_deterministic_and_never_worse_than_m3` raises the
  cap to the request's 5 s for that reason.
* The M3 path's own CP-SAT (`resequence._cpsat`, lines of 8-40 orders with releases), which `cpsat`
  mode still runs as the comparator of v2, is not recorded in `SolverInfo.stages` and does not draw
  on the request budget (it keeps its fixed 0.05 det-s, one worker and the request seed, so it stays
  deterministic).
* `exact_objective` adds the model's objective offset, which CP-SAT stores as a double: above
  2^53 (only with extreme weights, below the 2^61 overflow guard) a stage's `objective` can be off by
  a few units. Only the acceptance bound (objective ≤ the hint's) and the `no_improvement` tie rule
  read it; the simulator guards that follow decide.
* The CP-SAT recourse is the LP's DC-level fair-pool model; the simulator rations per cluster with
  protected minimums, so CP-SAT optimality does not imply simulator optimality — the guards (§4.4)
  keep the repair plan when they disagree (the full-profile D options).
* Value tables price one PO line at a time; orders sharing a DC × SKU interact. The final simulator
  comparison against the M3 sequence protects against regressions.
* `cpsat_v2_*` may be reported in `resequence_methods` for a resequence that compare mode then
  drops (`resequence_dropped=true`), exactly as M7 reports the M3 method in that case.
* Merxa does not validate MOQ or case packs on `po.create`: the terms come from tenant
  configuration and are enforced by Keelson only (four-eyes approval of `po.create` remains).
