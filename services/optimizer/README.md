# keelson-optimizer

Stateless, deterministic optimizer for Keelson (contracts: `docs/contracts/40-optimizer.md`,
M3: `docs/contracts/80-m3-deterministic-core.md` §2-§3). FastAPI on `PORT` (default **8200**),
internal only (compose network `app`). Python 3.14, OR-Tools **9.15.6755** (GLOP through
`ortools.linear_solver.python.model_builder`; CP-SAT for factory-line sequencing).
Request/response models live in `packages/contracts-py` → `keelson_contracts.optimizer`; the
M5 re-planning additions (capacity bookings, `capacity_booking.cancel`) are served as additive
subclasses in `keelson_opt.wire_m5` until the contracts package takes them over (see below).

| Route | Auth | Purpose |
|---|---|---|
| `GET /healthz` | – | process up → `{"ok": true}` |
| `GET /readyz` | – | `200 {"ok":true,"checks":{"shared_secret":true,"glop":true}}`, else `503` |
| `GET /v1/levers` | service JWT | the five v1 levers `[{code,title,params_schema,ops}]` (`lever_catalogue()`) |
| `POST /v1/impact` | service JWT | `ImpactRequest` → `ImpactResponse` (+ `graph`, `tts`, `bottlenecks` with `include_graph`) |
| `POST /v1/optimize` | service JWT | `OptimizeRequest` → `OptimizeResponse` (+ `scorecard`, typed `decisions`, grouped `actions`) |

Modules: `scn` (normalise + perturb), `impact` (PAB simulator), `tts` (TTS LP, TTR,
bottlenecks), `graph` (impact graph), `plan` (a plan's physical ledger), `planner` (one pass
over all levers), `levers/composed` (the LP), `levers/reallocate` (M1 formulation + solver
helpers), `levers/resequence` (factory-line sequencing), `service` (responses, scorecard,
actions), `wire_m5` (M5 request/response extensions).

## Auth (M1)
`Authorization: Bearer <JWT>`, HS256 signed with `OPTIMIZER_SHARED_SECRET` (env or
`/run/secrets/optimizer_shared_secret`). Required claims: `iss="keelson-engine"`,
`aud="keelson-optimizer"`, `exp`, non-empty string `tenant` (5 s leeway,
`OPTIMIZER_JWT_LEEWAY_S`). Anything else → **401**. `data.tenant` ≠ token `tenant` → **403**.
No secret configured → every `/v1` call is 401 and `/readyz` is 503. The token is checked in
the HTTP middleware **before the body is read**, so unauthenticated callers never make the
service parse JSON. Validation errors → 422 with a bounded body
`{"detail":[{"type","loc","msg"}…]}` (at most 50 errors; the request `input` is never echoed).
Bodies over `OPTIMIZER_MAX_BODY_BYTES` (32 MiB) → 413. `POST /v1/*` computations run at most
`OPTIMIZER_MAX_CONCURRENCY` (1) at a time; further requests wait before their body is read
(a full-size request peaks at ≈250 MB RSS, the container has 512 MB).

Engine-side minting (PyJWT):
```python
jwt.encode({"iss": "keelson-engine", "aud": "keelson-optimizer", "tenant": tenant,
            "exp": int(time.time()) + 60}, OPTIMIZER_SHARED_SECRET, algorithm="HS256")
```

## Impact model (`keelson_opt.impact`)
Weekly buckets = ISO weeks of the horizon. Per DC × SKU × week:
1. **DC pool** = (on_hand - reserved) + in_transit at t=0, minus all outbound transfers of the plan
   (reserved at creation, like Merxa); receipts (after perturbations and the plan's splits,
   buys and resequencing) and inbound transfers are added in their arrival week.
2. **Fair-share rule**: each cluster requests `max(0, demand + protected_min − cluster_stock)`.
   If the pool covers all requests they are filled; otherwise the pool is rationed
   **pro-rata to the requests** (floor, then the leftover units one each to the largest
   fractional remainders, ties by cluster code). Unallocated pool carries over. A plan's
   firm allocations (`prioritize_allocation`) replace the share of the clusters they name.
3. Cluster: `available = stock + alloc`, `PAB = available − demand`, `lost = max(0, −PAB)`
   (lost sales, no backorders), carried stock = `max(PAB, 0)`.

A cluster × SKU × week is **short** when `PAB < protected_min` (default 0 → `PAB < 0`).
KPIs: `units_at_risk` = Σ lost; `revenue_at_risk_cents` = Σ lost × retail;
`margin_at_risk_cents` = Σ lost × (retail − unit cost); `shortage_cluster_weeks` = distinct
(cluster, week) pairs with any short SKU; `first_breach` = earliest short week per
(dc, cluster, style), dated to the Monday. Timeline rows are cluster × style × week:
`projected_available` = Σ signed PAB over the style's sizes, `demand` = Σ demand,
`shortage` = Σ `max(0, protected_min − PAB)`. `baseline_kpis` = same without perturbations.
`provenance.inputs_hash` = sha256 of the canonical JSON (00-overview rule) of the full request
(`include_graph=false` and empty M3 lists are omitted from the dump, so M1 hashes are unchanged).

Perturbations (`keelson_opt.scn`; generic `keelson_contracts.scenario` union or local M1 subset;
target kinds per op = `keelson_contracts.optimizer.TARGET_KINDS_BY_OP`; details in the module
docstring):
* `lead_time_shift` - kinds `receipt`/`po_line`/`po`/`dc`/`sku`/`style` and, through `po_lines`
  + `bom`: `factory`, `supplier` (the line's own supplier and every BOM supplier of the style
  *at that factory*; a BOM row with `factory_code=null` = every factory) and `component`.
  Moves the eta by `days` (re-bucketed; shifts add up). It delays *readiness* (ex-factory), so
  a faster lane can only save transit time.
* `demand_scale` - kinds `cluster`/`dc`/`sku`/`style`; optional `weeks`; round half up.
* `capacity_scale` - kinds `lane` (a DC lane `FROM>TO` or an inbound lane code) and
  `port`/`chokepoint` (every inbound lane whose origin, via or destination includes it);
  rounded down. `factor_bp=0` **closes** the lane: a receipt whose transit overlaps the closure
  is delayed by the closure's length (port backlog; needs an end date) or re-routed onto the
  lane's `alt_lane_code` when that lane is open and **not a faster (premium) mode** - moving to
  sea-air is the expedite lever's decision - with `max(0, alt transit − transit)` extra days,
  whichever is earlier; otherwise the receipt is **lost**. A partial scale only limits new
  bookings (expedite splits); booked receipts keep their dates.
* `compliance_block` - kinds `supplier`/`factory`: those alternate sources are unusable (a
  block whose `window` ended before `as_of` or starts after the horizon is ignored).

Optional `window` restricts each (receipts with eta inside / overlapping weeks / transit
overlapping the closure). `p10`/`p90` ignored. Other ops/kinds are ignored and listed in `warnings`.

## Impact graph, TTS, bottlenecks (`include_graph: true`; `keelson_opt.graph`, `keelson_opt.tts`)
* **Graph** (≤ 300 nodes; stores never appear, PO lines are grouped to POs, SKUs to styles):
  `supplier -supplies-> component -supplies-> factory`, `supplier -supplies-> factory`,
  `factory -produces-> po -ships-> lane -ships-> dc` (or `po -ships-> dc` without a lane),
  `port/chokepoint -ships-> lane`, `dc -stocks-> style`, `dc -serves-> cluster`,
  `style -serves-> cluster`. Status: `disrupted` (directly targeted) > `impacted` (loses sales,
  or upstream of a disrupted receipt whose DC × style loses sales) > `at_risk` (on a disrupted
  path, or a protected minimum breached, without lost sales) > `ok`. `units_at_risk` = lost
  units (DC / cluster / style) or disrupted receipt units carried (upstream). Labels come from
  `locations`. Over the cap, nodes are kept by severity, units, kind, id (with a warning).
* **TTS** per DC (Simchi-Levi LP, GLOP): `y[t] ∈ [0,1]` = share of week t's demand served for
  every SKU, `y[t] ≤ y[t-1]`, `Σ_{τ≤t} y[τ]·D[k,τ] ≤ S[k,t]` where S = DC supply + its clusters'
  stock + **on-time** arrivals (disrupted receipts excluded). `TTS = floor(7·Σy)` days from
  the horizon start (the full horizon when it never runs short). The objective is scaled by
  the DC's daily margin value, so row duals are in cents: the **bottlenecks** are the binding
  SKU-week rows (top 25). **TTR** = days from the horizon start to the first perturbed arrival
  of the DC's disrupted receipts (`null` when it has none, or all are lost). Verdict: survives
  the horizon or `TTR ≤ TTS` → `ok`; `TTS < TTR ≤ TTS + 7` (one weekly bucket) → `expedite`;
  otherwise → `loss`.

## `reallocate_inventory` LP (M1, `keelson_opt.levers.reallocate`)
Variables `x[lane,sku,ship_week]` (arrive after `ceil(transit_days/7)` weeks, ship weeks from
the first Monday ≥ `as_of`), `s`/`l` (served/lost per cluster × SKU × week), `I[dc,sku,week] ≥ 0`.
The per-week requirement (the simulator's request under ample supply) is split into a
**sales** part and the protected-minimum **buffer** build-up; an unfilled buffer is not a lost
sale, so it is charged only 25 % of the margin (`BUFFER_LOST_FACTOR`, chosen on a randomized
sweep; charging the full margin produced plans with negative recomputed net benefit).
Constraints: `s + l = requirement part`, DC balance (receipts, arrivals, served, outbound
reserved at t=0), **ATP** (Σ outbound ≤ on_hand − allocated − reserved: Merxa validates this
on `transfer_order.create`), lane caps per ship week (× `capacity_scale`), `max_units_total`.
Objective `min Σ margin·w_lost·(l_sales + 0.25·l_buffer) + Σ lane_cost·w_transfer·x + 0.001·t·x`
(weights in bp; the tiny term prefers earlier shipping). Actions: one `transfer_order.create`
per (ship_week, from_dc, to_dc), lines sorted by SKU, `ship_date` = Monday of the ship week.
`explain.binding_constraints` = tight `<=` rows of the plan LP (lane caps, ATP,
`max_units_total`, expedite lane caps, alternate-source caps, budgets) with shadow price =
−dual (weighted cents per unit), top 25; only for `OPTIMAL` solves (a time-limited `FEASIBLE`
stop keeps its plan but reports no duals). A plan spec without levers is the "do nothing"
baseline (no model, `solver.name="none"`).

## The v1 levers in one request (`keelson_opt.planner`)
Any combination of the five levers is solved in one pass:
1. **`resequence_production`** (`levers/resequence`): per factory line the movable orders run
   back to back from the anchor at `daily_capacity_units` (a fluid single machine). The
   reference is the current sequence with every material on time; an order whose PO line has
   a readiness delay `d` cannot start before its on-time start + `d` (the others have their
   materials), and its scenario completion is its on-time completion + `d` (the scenario does
   not cascade a delay onto the orders behind it). Minimise `Σ w·C` with `w = margin ×
   min(qty, lost units of its DC × SKU)` by Smith's rule (exact without release dates), exact
   enumeration (≤ 7 orders), CP-SAT (`num_workers=1`, the request seed, *deterministic* time
   limit 0.05; ≤ 40 orders) or the release-aware greedy order (larger lines; O(n log n)). A new
   sequence is kept only when its objective beats the reference and the simulator (lost
   margin on the affected pairs) improves. Goods are ready earlier/later by the
   completion-day change against the reference and then pass the lane-closure rules again (a
   temporary port closure may now be hit or missed; a sequence that would make a receipt miss
   its lane is not used). Moved PO lines are locked for the other levers. Actions: group
   `resequence-n` = `production_order.resequence` (`order_codes` = every movable order) + one
   `po_line.reschedule` per changed eta.
2. **ONE LP** (`levers/composed`, GLOP) = the M1 model (built row for row identically when only
   `reallocate_inventory` is chosen) + `expedite_mode_shift` split variables (a sea-air/air lane
   to the line's DC from the same origin; Merxa departure `max(as_of + 1, ex_factory + delay +
   1)`, postponed past a temporary closure of the lane to the first booking week with
   capacity - an open-ended closure rules the lane out; capacity per lane and booking week ×
   `capacity_scale`; split < open qty; premium = lane cost − current lane cost; budgets) +
   `resource_alt_supplier` buy variables (approved ∧ compliant ∧ not blocked sources; eta =
   `as_of + lead_time (+ the wait for a temporary closure of its lane) + transit` or a later Monday;
   weekly capacity; the replaced line loses the same units; premium = alt unit cost + lane
   cost − SKU cost − current lane cost; budgets). Candidates are the PO lines with a disrupted
   receipt (or the refs / styles listed in the params).
3. **Integer repair** (deterministic): floor everything; a line both split and re-sourced keeps
   the larger lever (ties: expedite); a PO below the source MOQ is topped up (headroom,
   capacity, budgets) or dropped by recomputed net benefit (ties: drop); then **phase 2**
   re-solves the LP with the buys fixed and expedite forbidden on re-sourced lines; each line
   keeps one split (its largest). The plan never emits two ops on one PO line (Merxa rejects a
   line changed by an earlier item of the same change set). When a resequence moved PO lines
   and expedite or re-sourcing is requested, the LP levers are also solved without the
   resequence (those lines unlocked); the better phase-1 plan by recomputed weighted net
   benefit is kept (ties: with the resequence; otherwise warning +
   `solver.params.resequence_dropped=true`) and only the winner runs phase 2.
4. **`prioritize_allocation`**: in the cells that stay scarce under the plan, the clusters'
   allocations become the single-week optimum - protected allocations first
   (`respect_protected`), then sales needs `max(0, demand − stock)`, then buffers, pro-rata
   within each step - limited to `max_changes`. Not an LP decision on purpose: the LP treats
   protected-minimum buffers as rotating stock while the simulator sells them under shortage,
   and mapping the LP's cluster split onto allocations lost margin. With one margin per SKU a
   deep shortage sells every unit whatever the split, so the lever only gains where buffers
   or protected floors compete with sales. `max_changes` keeps whole (dc, sku, week) cells
   only (most valuable first): half of a move between clusters is never emitted. Allocations
   naming a cluster of another DC are left unchanged (warning). Actions: `allocation.set_qty`
   (`expected_version` = the allocation version).
5. **Never worse than doing nothing**: with several levers, a lever whose decisions lower the
   recomputed weighted net benefit (measured exactly on the DC × SKU pairs it touches) is
   dropped; if the plan still does not improve `w_lost·margin − w_cost·incremental cost`, the
   do-nothing plan is returned with a warning and `solver.params.plan_fallback="do_nothing"`.

**KPIs and the scorecard are recomputed by the simulator from the rounded plan** (never from an
objective): `fill_rate_bp` = served / demand, `lost_units`, `lost_margin_cents`,
`incremental_cost_cents` = transfer + expedite premium + near-shore premium (`cost_breakdown`),
`net_benefit_cents` = margin recovered − incremental cost (= `kpi_delta.net_benefit_cents`),
`shortage_days` = 7 × short cluster-weeks, `first_breach_shift_days` (a breach that disappears
counts as moved to the day after the horizon), `tts_gain_days` = change of the weakest DC's TTS,
`changes` = number of actions. **Actions** (`seq` 1..n): transfers (M1 order) ·
`po_line.split` (by line) · alternate groups `alt-n` (`po.create` per source × eta, ≤ 500
lines, then `po_line.change_qty` / `po_line.cancel` of the replaced lines) ·
`allocation.set_qty` (by code) · resequence groups. Atomic groups are contiguous.

## M5 targeted re-planning: capacity bookings (`95-m5-remediation.md` §4)
`POST /v1/optimize` and `/v1/impact` accept `data.capacity_bookings` (`keelson_opt.wire_m5`;
omitted while empty, so every pre-M5 `inputs_hash` and response is unchanged):
```json
{"code": "CB-0007", "lane_code": "CGP-UK1-SA", "iso_week": "2026-W42", "qty_units": 200, "version": 3,
 "lines": [{"po_line_ref": "PO-BD-0001#7", "qty": 200, "revert_eta": "2026-11-16"}]}
```
`lines` = the PO lines the booking carries (a `po_line.split` books its new line; a seeded
sea-air PO's booking carries all its lines); `revert_eta` = the line's `eta_dc` once the
booking is cancelled (its sea arrival; `null` = `max(as_of, ex_factory) + 1 + transit` of the
booking lane's `alt_lane_code` when that lane is not faster; no sea route = the booked units
do not arrive). With a sea arrival a cancellation moves the **whole** PO line there (its
`po_line.reschedule` moves the whole line in the target system), even when the booking
carries only part of it. Validation (422): unknown inbound lane / PO line, a PO line in two bookings, lines
carrying more than `qty_units`, duplicate codes.

* **State, not decisions.** A booking draws `qty_units` from its lane in its week (Merxa draws
  capacity per booking, never per PO line, so PO lines on a premium lane consume capacity only
  through the bookings listed); new expedite splits get the rest (`expedite lane … cap` rows
  and the departure search use `capacity × capacity_scale − booked`). The PO lines of every
  booking are locked for all levers (no second split, no re-sourcing, a resequence that would
  move a booked line is not used): already-applied decisions are fixed and
  are never re-emitted - the response is a **delta**.
* **Capacity cut below what is booked.** A booking whose week is before the `as_of` week has
  departed and is never questioned. In every (lane, week) whose capacity after
  `capacity_scale` is below the bookings' total, all bookings are *decidable*. The scenario
  baseline (`kpis_before`, `/v1/impact`) assumes the carrier honours them first-come-first-
  served (booking-code order, each one that still fits; warning `capacity_bookings: …`); the
  rolled ones' PO lines arrive at their sea arrival (+ readiness delay, + the sea lane's
  closure rules).
* **Decision.** The plan LP (every chosen lever) gets a kept share `k[b] ∈ [0,1]` per decidable
  booking: the lines are given with those bookings cancelled and `k[b]` moves the booked
  units back from the sea arrival to the booked one; row `Σ qty_b·k[b] + Σ new splits ≤
  capacity` per cell (`booked lane … cap`), `−0.01` c per kept unit (keep on ties). Lost
  margin by week makes it value margin and urgency. Integer rounding per cell (each order
  keeps every booking that still fits): largest share (ties: margin per booked unit, code);
  margin per booked unit; each fractional booking first; the baseline - scored by the
  simulator with the LP's transfers, lowest lost margin wins. The levers are then solved
  around that choice (compensating transfers, splits on lanes with room, buys …). Step 5
  ("never worse") also requires the choice to beat the baseline on the PO lines it changes
  (switching to the baseline drops the splits of any cell the baseline's bookings would then
  over-book, and the switch must then beat the plan as a whole - a lane is never booked beyond
  its cut capacity), and the do-nothing fallback still cancels the baseline's rolled bookings (the cut leaves no
  plan that keeps them). A plan spec without levers stays the pure baseline (no actions).
* **Actions** (after the transfers, before the splits; one group `rebook-n` per cancelled
  booking that carries lines): `capacity_booking.cancel` (`target_ref` = booking code,
  `expected_version` = its `version` or 1 with a warning, payload `{"booking_code"}`), then
  `po_line.reschedule {eta_dc: revert_eta}` per carried line whose eta changes. Bookings that
  keep flying emit nothing. `solver.params.bookings_decidable` / `bookings_cancelled`.
  The cancel compiles into a change set as a `GenericOpItem` (not a typed op yet).

Contract status: `keelson_contracts.optimizer` has neither `ProblemData.capacity_bookings`
nor the `capacity_booking.cancel` op (`ActionOp`, payload model, `LEVER_OPS`/typed change-set
item); clients that parse the response with the shared `OptimizeResponse` reject a plan with a
cancel - use `keelson_opt.wire_m5.OptimizeResponseM5` (or the contracts once updated). Merxa
has no `capacity_booking.cancel` op yet (only the internal `revert.capacity_booking`).

Known gaps (reported to the contracts / engine owners): `PoLine` carries no version, so PO-line
ops use `expected_version=1` with a warning (the engine must set the current version);
`po_line.split` is emitted **without** a separate `capacity_booking.create` because Merxa's
split already books (and draws) the lane capacity - a second booking would draw it twice.

Determinism: sorted iteration everywhere, single-threaded GLOP with `random_seed=<seed>`,
CP-SAT with one worker and a deterministic time limit; identical requests give identical
responses except `solver.solve_ms` (and impact `computed_at`), across processes and hash seeds.

## Build, run, test
```bash
# image (context = repo root)
docker build -f services/optimizer/Dockerfile -t keelson-optimizer .
docker compose -p keelson up -d optimizer

# tests inside the image toolchain (same base, + dev deps & tests)
docker build -f services/optimizer/Dockerfile --target test -t keelson-optimizer:test .
docker run --rm --memory 512m --cpus 1.5 keelson-optimizer:test

# dev loop (Git Bash; venv in a named volume, see docs/contracts/60-build-guide.md)
MSYS_NO_PATHCONV=1 docker run --rm --memory 1500m -v D:/primark:/work \
  -v keelson_uvcache:/root/.cache/uv -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy \
  -w /work/services/optimizer ghcr.io/astral-sh/uv:python3.14-trixie-slim \
  sh -c "uv sync --frozen && uv run pytest -q -p no:cacheprovider"
```
Golden fixtures: `tests/fixtures/small_problem.json` (M1 small story; regenerate with
`python tests/fixtures/make_small_problem.py`; exact output in `small_problem.golden.json`) and
`tests/fixtures/full_problem.py` (generated at test time, Merxa `full` story shape: 180 SKUs,
18 clusters, 9 weeks, 12 POs incl. BD/KH/VN, sea-air via AEJEA, TR/MA alternates, 3 factory
lines; options A-D summarised exactly in `full_problem.golden.json`). Refresh after an
intentional model change with
`KEELSON_UPDATE_GOLDEN=1 pytest tests/test_optimize.py tests/test_full_options.py -k golden`.
Crafted lever cases: `tests/network.py`.

Performance (S4, `tests/test_perf.py`, test image at 512 MB / 1.5 CPU, 2026-10-07): full profile,
**all five levers** in one request: validate 0.27 s + optimize 2.55 s = **2.8 s** in-process (LP
67k variables, solver time 1.7 s), **2.6-3.5 s over HTTP** (3.9 MB body), peak RSS 253 MB;
`/v1/impact` with `include_graph` 0.7-1.05 s. M1 preview (synthetic 180 SKUs x 40 clusters,
reallocation only, 129k variables): 3.4 s in-process, 3.6 s over HTTP. M5 re-plan (full
profile, the seeded sea-air POs booked, CGP-UK1-SA cut to 30 %, all five levers + the booking
decision, `tests/test_replan_m5.py`): 2.8 s in-process at 512 MB / 1.5 CPU (two LP solves). The tests fail above 5 s.

Env: `PORT` (8200), `OPTIMIZER_SHARED_SECRET`, `LOG_LEVEL` (INFO), `OPTIMIZER_JWT_LEEWAY_S` (5),
`OPTIMIZER_MAX_BODY_BYTES` (33554432), `OPTIMIZER_MAX_CONCURRENCY` (1).
Logs: one JSON object per line with `trace_id` (from W3C `traceparent`, `x-trace-id` or
`x-request-id`; echoed as response header `x-trace-id`).
