# 126 · M8 — Optimizer: CO₂e KPI and p10 / p50 / p90 robustness, weight profiles

Plan §9 ("KPIs … robustness at p50, p10/p90 is a stretch goal"; "Scoring. The PlanSpec carries a weight
profile. Results always show the Pareto view plus a scorecard"), §17 (Policies: KPI weight profiles) and
§19 M8 ("CP-SAT (MOQ and resequencing), CO₂, p10/p90 robustness"). Workstream 6 of
`120-m8-overview.md` (branch `m8/optimizer-co2-robustness`). Builds on `40-optimizer.md`,
`80-m3-deterministic-core.md` (scorecard, `scenario_compare@1`, KPI strip), `90-m4-investigation.md`
(taxonomy priors), `95-m5-remediation.md` (re-plan, capacity bookings) and `110-m7-backoffice-generic.md`
(guarded changes, admin).

Everything here is deterministic and LLM-free: no model is called, no recorded replay is invalidated.

## 1. Scope

| Item | Status |
|---|---|
| Emission factor data `data/emissions/factors.yaml` (+ README citation, `verified` flag) and the engine loader | done (factors **indicative**, `verified: false`, see §11) |
| Engine enrichment: `Sku.unit_weight_g`, `Lane.distance_km`, `InboundLane.legs` from the map catalogue | done |
| Optimizer CO₂e: `keelson_opt/emissions.py`, `Scorecard.incremental_co2e_kg` + `co2e_breakdown`, `OptimizeResponse.emissions` | done |
| Weight profiles (`keelson_contracts.scoring`), engine ranking / scores, `PlanSpec.weights` from the default profile, CO₂e Pareto axis | done |
| Robustness `mc/1`: keyed integer draws, piecewise-linear quantiles, value bands, duration window bands, lead-time noise, demand noise | done |
| Batch PAB simulator (`robust/batch.py`) incl. firm overrides, bit-exact with `impact.simulate` | done |
| Robustness in `/v1/optimize`: `Scorecard.bands`, `OptimizeResponse.robustness`, deterministic sample cap | done |
| Engine integration: shared request builder (simulate + M5 re-plan), win rates (CRN), band KPI items, CO₂e tile, `kg_co2e` unit + CO₂ words, replay-safe brief | done |
| Contracts-ts regeneration + `VALUE_UNITS` `kg_co2e` | done |
| Viz: band captions, whiskers, cost ⇄ CO₂e axis with client Pareto, profile rank chips, "How sure are we?" range chart + table, KPI band line, `kg_co2e` formatting | done |
| Admin "Option scoring" section on the Policies page via `PUT /api/admin/agents/spec` | done |
| Offline e2e `e2e/api_m8_robustness_offline.py` + Playwright `m8-co2-robustness.spec.ts` (screenshots 41–43) | written, syntax/type-checked; run by the integrator |
| Carbon price in the LP objective; TTR sampling; sensitivity drivers; CEL `cs.incremental_co2e_kg`; narrative citing bands / CO₂e; scn weight column, tenant factors, robustness on `/v1/impact`, cost uncertainty | **Deferred** (§12) |

## 2. CO₂e: data and engine inputs

**Factor sets** — `data/emissions/factors.yaml` (`version: 1`, `sets[]`): `id` (slug), `label`, `basis`
(`WTW` | `TTW`), `unit` (`g CO2e/tkm`), `verified`, `source {title, publisher, url, table, retrieved}`,
`factors {sea|air|road|rail: positive int}`, `distance {road_circuity_bp (10000–30000, default 12000),
air_uplift_km (0–500, default 95)}`. Platform data, not tenant-editable (a tenant cannot zero its CO₂e).
The shipped set `glec-v3-indicative` uses mid-points of the published GLEC Framework v3 default ranges
(sea 16, air 850, road 100, rail 25 g CO₂e/tkm, WTW) and is `verified: false` (§11). Loader:
`runtime/emissions.py` `load_factor_sets()` (cached; `$EMISSIONS_DIR`, `/app/emissions`, the source tree's
`data/emissions`; strict validation: unique ids, int factors ≥ 1, known modes, a source); a missing file or
unknown `factors_id` logs a warning and plans carry no CO₂e (never fails a case). The engine image copies
`data/emissions` to `/app/emissions` (runtime) and `/src/data/emissions` (test stage).

**Enrichment** — `enrich_for_emissions(problem, planning, geo)` → `(ProblemData, FactorSet) | None` builds
the request's data only (the stored `problem_data` evidence is never changed); deterministic and
idempotent; `None` when `planning.emissions.enabled` is false or no set is available:

* `Sku.unit_weight_g`: the longest `planning.emissions.unit_weights[].style_prefix` that the style code
  starts with (plain prefix, `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`, never a pattern), else
  `default_unit_weight_g`, else `planning.grams_per_unit` (800 g — the weight the per-kg rate cards
  already assume, so cost and CO₂e share one assumption). Pack `value-fashion-eu` ships illustrative
  weights (PUF 1100, PRK 1500, GIL 600, FLC 500, KNT 450, CRD 550, HDY 600 g).
* `Lane.distance_km` (DC transfers, road): great circle between the DCs × `road_circuity_bp / 10000`.
* `InboundLane.legs [{mode, distance_km}]` from the network map's own stop resolution
  (`visuals._lane_stops` / `_locate`, imported unchanged) and `runtime/geo.py`: road = great circle ×
  circuity; air = great circle + uplift; sea-air = the catalogued sea leg into the hub
  (`geo.sea_air_legs`) + air from the hub (great circle + uplift); sea = a road feeder when the origin is a
  factory, the catalogued route between the port stops (Cape / Suez from `via`; short-sea to the nearest
  port when the lane names no destination port) and a road leg from the last port to the DC. Polylines are
  summed haversine segments; every leg rounds half up to integer km (floats never enter the hashed
  request); legs of 0 km are dropped; a lane without coordinates gets no legs (reported as uncovered).

## 3. CO₂e in the optimizer (`keelson_opt/emissions.py`)

Request `OptimizeRequest.emissions = EmissionsSpec {factors[{mode, g_co2e_per_tkm}] (1–8, unique modes),
basis, source_id}`. Lane intensity `I = Σ legs km × factor` (integer, g per tonne); one unit of `w` grams
emits `w × I / 10⁹` kg. Incremental versus doing nothing, per component (integer numerators, each total
rounded half up to kg):

| component | per unit |
|---|---|
| `transfer_kg` | `+ w × distance_km × road factor` |
| `expedite_kg` | `w × (I(split lane) − I(the PO line's booked lane))` |
| `nearshore_kg` | `w × (I(alternate source lane) − I(booked lane))` — negative when near-shore road beats deep sea |
| `rebook_kg` | (M5) for every decidable booking: `(kept by the plan − kept by the FCFS baseline) × qty × w × (I(booked fast lane) − I(its sea fallback))`; units without a sea route are lost either way (0) |

Resequences and allocations move no goods (0). `Scorecard.incremental_co2e_kg` = the sum of the rounded
components; `co2e_breakdown` is present exactly when it is and sums to it (validator).
`OptimizeResponse.emissions = EmissionsSummary {source_id, basis, coverage_bp, baseline_inbound_kg,
uncovered[≤ 20]}`: `coverage_bp` = moved units whose lanes and weight are known ÷ moved units (10000 when
nothing moves); `uncovered` lists `lane:<code>` / `sku:<code>`; `baseline_inbound_kg` = every open PO line
on its booked lane (context). Scope: transport only, per the factor set's basis; production, packaging,
warehousing and the last mile are out of scope.

## 4. Weight profiles (`keelson_contracts.scoring`)

`WeightProfile {id (^[a-z][a-z0-9-]{0,31}$), label (1–40), lost_margin_bp 0–50000 = 10000, cost_bp 0–50000
= 10000, carbon_price_cents_per_t 0–1,000,000 = 0, risk_aversion_bp 0–10000 = 0}`;
`ScoringConfig {default_profile = "net-benefit", profiles (1–8, unique ids, default among them)}`.
Score (integer cents, every division rounded half up, higher is better):

```
recovered = net_benefit + incremental_cost
robust    = bands.net_benefit_cents.p10 + incremental_cost        (= recovered without bands)
blended   = ((10000 − risk_aversion_bp) × recovered + risk_aversion_bp × robust) / 10000
score     = blended × lost_margin_bp / 10000 − incremental_cost × cost_bp / 10000
          − incremental_co2e_kg × carbon_price_cents_per_t / 1000     (missing CO₂e counts as 0: co2_missing())
```

Built-in profiles: `net-benefit` 10000 / 10000 / 0 / 0 (score ≡ net benefit), `cost-first` 10000 / 15000,
`service-first` 15000 / 7500 / 0 / 5000, `low-carbon` 10000 / 10000 / 20,000 c/t (€200/t) / 2500.
`rank_options(cards, profile)` → `{id: (score, rank)}`, ties keep the candidate order.

Engine (`AgentSpec.planning.scoring`): the default profile's `lost_margin_bp` / `cost_bp` become
`PlanSpec.weights` (`candidates.build_candidates`, the M5 re-plan; 10000 / 10000 by default, i.e. the M3
LP unchanged); `default_recommendation(options, scoring)` ranks the improving options by the default
profile's score (same filters and tie-breaks as M3: fewer changes, then candidate order) — under
`net-benefit` it is exactly the M3 rule. Carbon price inside the LP objective is deferred (§12).

## 5. Robustness `mc/1`

Request `OptimizeRequest.robustness = RobustnessSpec {method "mc/1", samples 8–256 = 64, seed ≥ 0,
value_bands = true, window_bands[{perturbation, duration_days: Band}] (≤ 64, unique indexes, 1 ≤ p10,
p90 ≤ 366), lead_time_noise[{mode: sea|sea_air|air|road|default, days: Band}] (unique, −60..60),
demand_noise_bp: Band|null (0..100000)}`. The request's own rounded plan is **re-simulated, never
re-optimised**, together with the do-nothing baseline, in the same sampled futures.

* **Draws** (`robust/draws.py`): sample `n` of `(seed, stream, key)` = word `n % 8` of
  `blake2b("mc/1|{seed}|{stream}|{key}|{n // 8}", digest_size=64)` (little-endian u64), uniform basis point
  `u = ((word >> 32) × 10000) >> 32`. Streams: `value:{i}` (perturbation i's value), `window:{i}` (its
  duration), `lt` (transit noise, keyed by the arrival: `{ref}|{dc}|{sku}|{eta}`, `{ref}|revert`,
  `{ref}@{lane}`, `{factory}@{week}:{ref}`), `dem` (`{cluster}|{style}`). Independent of iteration order,
  `PYTHONHASHSEED` and platform; the same seed gives every option of a plan the same futures (common
  random numbers).
* **Quantile**: integer piecewise-linear through `(1000, p10)`, `(5000, p50)`, `(9000, p90)`; the tails
  continue at the adjacent slope; `p50 + floor((2·Δ + 4000) / 8000)` with `Δ = slope × (u − 5000)` (half up);
  factors clamp at 0, durations at 1.
* **Futures** (`robust/sample.py` `Futures`): a `lead_time_shift` / `capacity_scale` / `demand_scale` with
  `p10`/`p90` samples its value (`disruption_severity`); a window band keeps the window's start and
  samples its length (`disruption_duration`; 422 when the index has no bounded window or `p50` ≠ the
  window's length, or the op is not sampled); transit noise by the arrival lane's mode (`lead_time`); a
  demand factor per (cluster, style), constant across sizes and weeks (`demand`). Readiness deltas are
  matched with `scn`'s own rules (`_target`, `_network_attrs`, `covers_day`; `scn.py` is unchanged);
  closures are rebuilt per sample from the sampled capacity perturbations (factor 0 → closure over the
  sampled window; > 0 → no closure: a partial scale only limits new bookings).
* **Dated arrivals** (`plan_arrivals`): mirrors `plan.plan_lines` exactly (bookings kept / cancelled,
  resequence moves, reductions from the latest arrival first, then expedite splits and alternate buys;
  `tests/test_batch_sim_m8.py` asserts equality). Per sample: a receipt (`line`) or a booking's sea
  fallback (`revert`) is ready `Δ` days later and goes through `scn.closure_delay` again where its lane
  has a closure in that sample (later, earlier or lost); a resequenced line moves by its PO line's `Δ`;
  an expedite split by `max(0, Δ)` (a booked departure cannot leave earlier; capacity assumed); an
  alternate buy not at all; then transit noise. Lines lost nominally stay lost. The per-sample bucket is
  `max(0, week(arrival)) + (ledger week − max(0, week(nominal date)))`, so with nothing sampled every
  arrival lands in its ledger week.
* **Batch simulator** (`robust/batch.py`): int64 tensors per DC (samples × SKUs × the DC's clusters), weeks
  looped; vectorised largest-remainder rationing (ties by cluster order — padding every DC to all its
  clusters changes nothing: a zero request never has a remainder) and firm-then-fair overrides; demand
  computed once per DC and shared by both batches. Equal to `impact.simulate` per key when nothing is
  sampled.
* **Evaluation** (`robust/evaluate.py`): `samples = min(requested, max(8, 2,500,000 // (SKUs × clusters ×
  weeks)))` (deterministic cell budget, never wall clock; 64 on the full profile, 38 for 180 SKUs × 40
  clusters × 9 weeks); per sample `net_benefit = (baseline lost margin − plan lost margin) −
  incremental cost`; nearest-rank quantiles (index `ceil(q·n/100) − 1`). Results:
  `Scorecard.bands = ScorecardBands {samples, fill_rate_bp, lost_units, lost_margin_cents,
  net_benefit_cents, shortage_days: Band, stockout_chance_bp}` (`stockout_chance_bp` = share of futures in
  which the plan loses some demand) and `OptimizeResponse.robustness = RobustnessResult {method, seed,
  samples, requested_samples, sampled[], crn_key, per_sample_net_benefit_cents[],
  per_sample_lost_margin_cents[]}` (`crn_key` = sha256 of the canonical data + perturbations + robustness
  spec: equal keys ⇒ comparable vectors).
* Not sampled (documented approximations): costs and rates, time-to-survive and first-breach shift,
  capacity of already decided bookings and of expedite splits, closures of resequenced lines, the
  recovery tail after a disruption (TTR).
* The nominal scorecard stays the deterministic run; a nominal value may lie outside its p10–p90 (the LP
  plans to nominal demand exactly, so upside futures lose sales while downside ones cannot gain them).

**Engine** (`runtime/robustness.py`): `robustness_spec(planning, scenario, perturbations)` — `seed =
planning.robustness.seed ?? solver_seed` (one seed per plan version); window bands for every perturbation
with a bounded window whose `prov` is `assumption`/`inference`, only when the scenario's `duration_days`
assumption is an `assumption`: the event template's duration prior re-centred on the window's length
(`p10 = min(prior p10, length)`, `p90 = max(prior p90, length)` capped at 366). `OptimizeInputs` (enriched
problem, perturbations, solver options, factor set, robustness spec) builds the requests of `simulate`
and the M5 re-plan. `win_rates(options)` = share of futures in which each option has the highest net
benefit (> 0; ties → earlier candidate; "Do nothing" wins a future in which no option gains), as bp with
largest-remainder rounding summing to 10000 — only when every option carries a `RobustnessResult` with
one `crn_key` and one sample count and "Do nothing" is among them (else none: e.g. M4 merged / M5 re-plan
mixes).

Performance (512 MB / 1.5 CPU, full profile 180 SKUs × 18 clusters × 9 weeks, all five levers, trims delay
with its band, 64 samples, default noise): ~2.9–3.1 s per call in process, ~2.9 s over HTTP (plain S4
~2.5 s), peak RSS ~250 MB (fresh process). The robustness part is ~0.5 s per call (two batches + sampling).

## 6. APIs and contracts (all additive, omitted while unset)

**contracts-py `optimizer`** (shared with workstream 5: additive only): `TransportMode`, `EmissionsBasis`,
`LaneLeg`, `Band`, `EmissionFactor`, `EmissionsSpec`, `LeadTimeNoise`, `WindowBand`, `RobustnessSpec`,
`Co2eBreakdown`, `ScorecardBands`, `EmissionsSummary`, `RobustnessResult`, `SampledStream`,
`ROBUSTNESS_MIN/MAX_SAMPLES`; fields `Sku.unit_weight_g`, `Lane.distance_km`, `InboundLane.legs`,
`Scorecard.incremental_co2e_kg / co2e_breakdown / bands`, `OptimizeRequest.emissions / robustness`
(+ a validator: window-band indexes in range), `OptimizeResponse.emissions / robustness`;
`ScorecardMetric += "incremental_co2e_kg"` (optional on a scorecard). `wire_m5` inherits everything.

**contracts-py `blocks`**: `KpiUnit += "kg_co2e"`; `KpiBand {p10 ≤ p90, samples ≥ 1}`, `KpiItem.band`;
`ProfileScore {profile_id, score_cents, rank ≥ 1}`, `CompareOption.win_rate_bp` (0–10000; on every
option or none, summing to 10000) and `scores` (unique profile ids); `CompareProfile {id, label, default,
lost_margin_bp, cost_bp, carbon_price_cents_per_t, risk_aversion_bp}`, `UncertaintyNote {method "mc/1",
samples, sampled[disruption_duration|disruption_severity|lead_time|demand] (≥ 1, unique)}`,
`EmissionsNote {source_label, basis, indicative, coverage_bp}`; `ScenarioCompareProps.profiles` (≤ 8, unique,
at most one default; every score references a known profile), `uncertainty`, `emissions`; an
`incremental_co2e_kg` axis requires CO₂e on every option.

**contracts-py `scoring`** (new, owned by this workstream): §4.

**contracts-ts**: regenerated schemas / Zod (`block.scenario_compare.v1.props`, `block.kpi_strip.v1.props`,
`block.recommended_option.v1.props`, `block.envelope`) and `VALUE_UNITS` + `kg_co2e`. The JSON Schema
carries types, ranges and patterns; cross-field rules are checked by contracts-py.

**HTTP**: no new route. `/v1/optimize` accepts and answers the new blocks (422 for invalid specs);
`/v1/impact` is unchanged. The admin section uses `GET`/`PUT /api/admin/agents/spec` and `POST
…/restore` (M2).

## 7. Engine blocks and UI

* `kpi_strip@1` (exposure + recommended plan): `units_at_risk`, `margin_at_risk_cents`,
  `shortage_cluster_weeks` get do-nothing's bands (what happens if nothing is done; cluster-weeks =
  shortage days / 7), `net_benefit_cents` / `fill_rate_bp` the recommended option's; a tile
  `incremental_co2e_kg` "Extra CO₂e of the plan" (`kg_co2e`). `recommended_option@1`'s KPIs get the
  option's bands and the CO₂e tile (within the card's 8 KPIs).
* `scenario_compare@1`: the props from `visuals.scenario_compare_props` decorated by `decorate_compare`
  (profiles, each option's scores / ranks, win rates, `uncertainty`, `emissions` with the factor set's
  label and `indicative = not verified`, the minimum coverage); the engine keeps the default axes.
* `compose`: `Unit += kg_co2e` ("26,811 kg CO₂e"); the numbers rule accepts `CO2`, `CO₂`, `CO2e`, `CO₂e`,
  `tCO2e`, `tCO₂e`, `kgCO2e`, `kgCO₂e` as words (`CO2_TOKENS`). **Replay safety**: `option_kpis`,
  `kpi_placeholders`, `kpi_meanings` and the Strategist brief are unchanged (no CO₂e / band keys reach a
  model); tested byte-for-byte with and without M8 data.
* Viz (`@keelson/viz`): §5 of its README — "Compare against" chips (server x axis | Extra CO₂e; disabled
  with the options it is missing for), the frontier recomputed client-side per axis (direction-aware,
  comparisons only), p10–p90 whiskers on the y metric, "likely A–B" captions (sr-only meaning), an Extra
  CO₂e column, "Rank by" profile chips with a rank column and "Top under …" (ranking only — the
  recommendation follows the default profile), the **How sure are we?** panel (Lost margin | Fill rate |
  Net benefit; bar p10–p90, median tick, ◆ planned; View as table: p10 / median / p90 / planned / chance of
  a stock-out / best in % of futures), the CO₂e footnote; KPI tiles "Likely A–B". Axe clean; animations
  follow `prefers-reduced-motion`. Fixtures `scenario_compare.robust`, `kpi_strip.robust`.
* Admin **Policies → Option scoring** (`ScoringSection.tsx`, `lib/admin/scoring.ts`): profiles (name, id,
  weights in %, carbon price in €/t, caution %), the default profile, sampled futures (16–256); the factor
  set and unit weights read-only; saved with a merge patch `{planning: {scoring, robustness: {samples}}}`
  (profiles replace whole); 409 → reload; 422 → the engine's `errors[].loc` mapped to the fields.

## 8. Data, migrations, configuration

* **Migrations**: none (120 §2: optimizer none in M8; no engine table). State lives in the versioned
  AgentSpec (`planning.emissions / robustness / scoring`), the plan row's options JSON (`scorecard` with
  CO₂e and bands, `emissions`, `robustness` incl. the per-sample vectors — 2 × 64 integers per option) and
  block props.
* **AgentSpec** (`runtime/spec_m8.py`, embedded in `PlanningDefaults`): `emissions {enabled = true,
  factors_id = "glec-v3-indicative", unit_weights[{style_prefix, grams 1–100000}] (≤ 200, unique),
  default_unit_weight_g = null}`; `robustness {enabled = true, samples 16–256 = 64, seed = null,
  value_bands = true, duration_bands = true, lead_time_noise = sea −2/0/+5, sea_air −1/0/+2, air 0/0/+2,
  road −1/0/+2, default −1/0/+3 days, demand_noise_bp = 9000/10000/11000}`; `scoring = DEFAULT_SCORING`.
  Settable through pack `spec_overrides` or the admin section. **Sample counts and seeds are spec settings,
  never env** (120 §3.5).
* **Env**: `EMISSIONS_DIR` (engine, optional lookup override like `GEO_DIR`; prefix allocated to this
  workstream). No optimizer env; no compose or `.env.example` change.
* **Images**: engine Dockerfile copies `data/emissions` (test stage + runtime). Optimizer: `numpy==2.5.3`
  is now a direct dependency (it was already locked and installed through OR-Tools; `uv.lock` only records
  the direct requirement — no new wheels).
* **Pack**: `value-fashion-eu` `spec_overrides.planning.emissions.unit_weights`.

## 9. Security

No new route, table, egress or secret. Factor files and geometry are local platform data; tenants cannot
edit factors. No untrusted text enters any computation (profile labels are length-limited and rendered as
text). Unit-weight rules are plain prefixes (no regex, no ReDoS). DoS bounds: samples ≤ 256 (contract) and
the cell budget, ≤ 8 profiles with bounded weights, ≤ 64 window bands, the existing 32 MiB body limit and
concurrency 1. Scoring is not a guarded change (it does not change who approves; spec versions are audited
and restorable); approval floors, four-eyes and RLS are unchanged. Fully deterministic, LLM-free.

## 10. Tests

| Suite | Command (Docker harness) | Result |
|---|---|---|
| optimizer (all) | `M8_SLOT=13 m8-pytest.sh <wt> optimizer -q` | see the delivery report |
| optimizer perf at 512 MB / 1.5 CPU | `docker run --memory 512m --cpus 1.5 … m8-test-optimizer pytest tests/test_perf.py` | 8 passed (S4 + CO₂e + robustness 3.1 s, 253 MB) |
| contracts-py | `m8-pytest.sh <wt> contracts-py -q` | see the delivery report |
| contracts chain | `m8-contracts.sh <wt> optimizer-co2-robustness --check` | clean |
| engine runtime / platform | `m8-pytest.sh <wt> engine tests/runtime` / `tests/platform` | see the delivery report |
| viz, contracts-ts, web | `m8-js.sh … 'pnpm --filter … typecheck && lint && test'` | see the delivery report |

New tests: optimizer `test_robust_draws_m8.py` (pinned draws for seed 7, anchors, tails, half-up rounding,
uniformity, identical across processes and hash seeds), `test_batch_sim_m8.py` (batch == scalar per key /
lost / margin / short cluster-weeks / demand for the small and full fixtures × stories × options A–D, the
M5 booking re-plans, N = 1 and 8; `plan_arrivals` == `plan_lines`; vector `ration` == scalar; sampled demand
== scenario demand; shuffled inputs), `test_emissions_m8.py` (hand-computed transfer / expedite /
near-shore / rebook kg, signs, rounding, coverage and uncovered, missing factors, input order, requests
without M8 blocks unchanged), `test_robustness_m8.py` (golden `robust_full.golden.json`, band order, CRN
identity, nominal bands without uncertainty, determinism in process / shuffled / across processes, sample
cap, a capacity band reopening a closure, 422 cases), `test_perf.py` (+2); contracts-py
`test_m8_co2_robustness.py` (+ `test_m3_optimizer` metric set); engine `test_m8_emissions.py`,
`test_m8_robustness.py` (incl. the planning flow with an M8-answering optimizer stub and with the features
off = M7 requests, replay safety), platform `test_m8_scoring_admin.py`; viz `tests/m8.test.tsx`,
`format.test.ts`; contracts-ts `m8-co2-robustness.test.ts`; web `lib/admin/scoring.test.ts`. e2e:
`e2e/api_m8_robustness_offline.py` (py_compile), `e2e/playwright/tests/m8-co2-robustness.spec.ts`
(`tsc --noEmit` of the Playwright project).

## 11. Deviations (from `120-m8-overview.md` and the design brief)

1. **On by default.** Both features are enabled in the built-in spec (`planning.emissions.enabled`,
   `planning.robustness.enabled`): they add outputs but change no plan, action or recommendation (the
   default profile is the M3 rule), so the M7 path's *results* stay the default; optimize requests (and
   their `inputs_hash`) gain the enriched data and the two blocks. Set both flags to `false` to send the M7
   requests byte-identically (tested). Goldens of the optimizer are unchanged (no M8 blocks in them).
2. Spec doc number **126** (120 §1) instead of the brief's 125; fixtures `scenario_compare.robust.json` /
   `kpi_strip.robust.json` (dot variants like the M7 ones) instead of `scenario_compare_robust.json`.
3. Draw scheme: `u = ((word >> 32) × 10000) >> 32` (vectorisable in numpy) instead of `(u·10000) >> 64`.
4. Transit noise is added **after** the closure re-evaluation (it is transit-time variability), not to the
   readiness date; closures are memoised per (closure pattern, lane, ready date).
5. No new `Scenario` fields in `scn.py` (`line_shift_hits` etc.): the robust module re-derives matches with
   `scn`'s own helpers, so `scn.py` is untouched (less overlap with workstream 5); a test asserts the
   sampled demand and the dated arrivals reproduce the scenario exactly.
6. `RobustnessResult.crn_key` added (comparability of per-sample vectors for win rates) instead of the
   engine guessing from seeds; `LeadTimeNoise.mode` has no `transfer` (DC transfers are not sampled).
7. `rebook_kg` is symmetric (plan vs the FCFS baseline), not only cancellations; distance rules (road
   circuity, air uplift) belong to the factor set (platform data), not to tenant planning settings.
8. `cite_in_narrative` flags were not added: citing CO₂e / bands in the narrative is deferred as a whole
   (needs live re-recording); new KPI keys are therefore not resolvable placeholders either.
9. The admin samples range is 16–256 (spec) while the contract allows 8–256 (tests use 8).
10. Profile ids are editable in the admin form (validated); profiles are shown in the compare block only
    when there is more than one.
11. Emission factors are **indicative, not verified** against the official GLEC v3 tables (no offline
    access to the tables in this session): `verified: false`, every screen says "indicative".

## 12. Deferred

* Carbon price inside the LP objective (`composed.py` coefficients + the planner's weighted check):
  overlaps workstream 5 (CP-SAT); ranking + the CO₂e axis deliver "CO₂ as an optional weight".
* TTR-prior sampling (recovery tail into the closure backlog) and TTS verdict probability bands.
* Sensitivity drivers (rank correlation of sampled inputs vs lost margin, tornado chart).
* Policy CEL context `cs.incremental_co2e_kg` (sustainability approval rules).
* Narrative citing bands / CO₂e (`cite_in_narrative`): needs a live model to re-record replays.
* `scn/1.0` item weight / volume column, tenant-editable emission factors, robustness on `/v1/impact`,
  cost / rate uncertainty.
* Verifying the factor set against the official GLEC tables (`verified: true`).
* Sampling the capacity of expedite splits and of already decided bookings; re-evaluating closures for
  resequenced lines per sample.

## 13. Known issues

* Factor values are indicative mid-points; distances follow hand-made illustrative sea polylines; unit
  weights are assumptions — CO₂e compares options, it is not an emissions inventory.
* Perf headroom: on the shared 9-builder harness a full suite run measured S4 + CO₂e + robustness at
  4.8 s (budget 5 s) while the isolated perf run is 2.9–3.1 s; like the existing S4 tests it is sensitive
  to CPU contention.
* Nominal values can sit outside their p10–p90 (see §5); the UI shows both ("planned" ◆ vs the bar).
* Merge overlap: workstream 5 edits `optimizer.py`, `service.py` (`run_optimize`), `planning.py`,
  `candidates.py`, `spec.py`, `pyproject.toml` / `uv.lock`, `README.md`, `test_perf.py`; workstreams 7 / 8
  edit `blocks.py`, the generated contracts-ts files, `fixtures.ts`, `e2e/README.md`. All edits here are
  additive; regenerate contracts-ts after merging (never hand-merge generated files).
