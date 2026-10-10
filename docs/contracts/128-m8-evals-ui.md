# 128 · M8 — Evals UI: in-app runner (replay by default, budget-capped live), run history, drill-down, compare, trends, CLI import

Builds on 90 §9 (eval metrics, latency budgets), 95, 100, 110 §6 (the M7 read-only evals listing) and
the overview `120`. Plan §19 "Evals UI", §20 (engine evals: thresholds, not gates; golden +
adversarial sets; tool trajectory in any order). Models: Flash/Flash-Lite only. Credits are depleted,
so everything here works and is tested **without live model calls**: replay mode is the default and
fails closed; live mode is platform-admin only, switched off by default and budget-capped; live checks
live in `scripts/verify-live.sh` (step `m8-evals`).

Platform admins start eval runs in `/admin/evals` (or import CLI result files), follow their progress,
drill into a case (checks, tool trajectory, entities, latency, usage, adversarial outcomes), compare two
runs and track metrics against targets over time. The in-app runner and the CLI share one library
(`evals/keelson_evals`) and one results format (`keelson-evals/2`, which still reads `/1`).

## 1. Scope

| Item | Status |
|---|---|
| Contracts `keelson_contracts.evals` (+ JSON Schema / Zod, `contracts-ts/src/evals.ts` vocabulary) | done |
| Engine migration `0012_m8_evals.sql` (eval_run, eval_case_result, eval_run_event, eval_case_link: RLS `FORCE`; eval_runner: platform) | done |
| Runtime: per-case LLM mode override (`effective_mode`), cancel / budget pre-checks, eval token + cost caps in `BudgetPlugin`, usage accounting, `LLM_REPLAY_MISS` code, no KB embeddings in replay eval runs | done |
| Admin API `/api/admin/evals/*` (status, runs, events, case, cancel, export, import, folder import, files, compare, trends) | done |
| Internal runner API `/api/internal/evals/runner/*` (heartbeat, claim + lease, events, attach, result, finish) | done |
| Eval harness: trajectory expectations + metric, structured entities / targets, exact per-case usage, `keelson-evals/2`, run hooks, `--mode-label`, suite catalogue + hash, engine client, runner loop | done |
| Runner image `evals/Dockerfile` + compose service `evals-runner` (profile `evals`) + `EVALS_*` env | done |
| Web: overview (status, active-run progress, trends, runs + compare selection, results folder), run detail, case drill-down, compare page, new-run and import dialogs | done |
| `@keelson/viz` `TrendChart` | done |
| Recordings coverage in `/status` + UI warning | done |
| Offline e2e `e2e/api_m8_evals_offline.py`; live check `e2e/api_m8_evals_live.py` (+ `verify-live.sh --only m8-evals`) | written, syntax-checked (run by the integrator) |
| Recordings export/import, embedding record/replay, new golden cases, nightly run, SSE progress, hiding eval cases | **deferred** (§11) |

## 2. Design

```
 /admin/evals (platform admin) ──► engine-api /api/admin/evals/*  ──► Postgres (eval_* tables)
                                         ▲            (queue, history, summary, compare, trends)
                                         │ /api/internal/evals/runner/*  (bearer EVALS_RUNNER_TOKEN)
 evals-runner (profile evals, edge net) ─┘
   heartbeat → claim → per case: run_live.run_case (black box over HTTP as the demo personas)
              attach the new Keelson case → turn events → PUT case result → finish
                                         │
 engine-worker: a run of an eval-linked case → eval run's LLM mode (replay = recordings only),
                its remaining live budget, usage metered back onto the link and the eval run
```

* **Black box preserved.** The runner is a separate container that drives Keelson over HTTP exactly
  like `evals/run_live.py` (same `run_case`), because the worker cannot reach `merxa.localhost`, the
  adversarial KB fixture must never ship in the engine image, and the harness is deliberately
  black-box. The engine only queues, stores and computes.
* **The engine is authoritative.** Summary, threshold outcomes and per-case engine accounting are
  computed in the engine from what it stored, whatever a runner or a file claimed.
* **Replay never escalates.** An eval run's mode is applied per Keelson run through the case link; a
  process running `LLM_REPLAY_MODE=replay` stays replay whatever the eval asks for.

## 3. Engine: data, runtime, summary

### 3.1 Migration `services/engine/migrations/0012_m8_evals.sql` (re-runnable)

* `eval_runner` (platform table, no tenant, no RLS): `id, version, suite_hash, suite jsonb (≤ 256 KiB),
  last_seen_at`; rows unseen for 7 days are deleted on heartbeat.
* `eval_run` (tenant table): `tenant` (the demo tenant for in-app runs, the importer's tenant for
  imports), `source in_app|import`, `mode replay|record_or_replay|record|unknown` (`unknown` only for
  imports), `status queued|running|succeeded|failed|cancelled`, `case_ids text[] (≤ 100)`,
  `requested_by jsonb`, `note ≤ 200`, `budget_max_tokens` / `budget_max_cost_microusd` (both or none,
  > 0), engine counters (`tokens_live, tokens_replayed, llm_calls_live, llm_calls_replayed,
  replay_misses, cost_microusd`, all ≥ 0), `runner_id, lease_expires_at, cancel_requested`,
  `suite_hash`, `thresholds` (snapshot at creation), `summary`, `threshold_results`, `breaches`,
  `doc_schema, doc_sha256, file_name, target`, `started_at, finished_at` (`finished_at` set ⇔ final),
  `error ≤ 2000`. Indexes: **one active run platform-wide** (unique expression index over
  `status IN ('queued','running')`, partial), **one run per normalised document** (`doc_sha256`
  unique where not null: imports and finished in-app runs), `(created_at DESC, id DESC)`, running leases.
* `eval_case_result` (PK `run_id, case_id`, FK `(tenant, run_id)` cascade): listing columns (status,
  error code, checks, `entity_recall_bp`, `numeric_grounding_bp`, `trajectory_recall_bp`,
  `first_stage_ms`, `options_ms`, `tokens`, `cost_microusd`, `failed_checks`, …) + `result jsonb` (the
  full `EvalCaseResult`; contracts cap its canonical JSON at 1 MiB, the table allows 2 MiB of jsonb
  text). `keelson_case_id` has **no** foreign key: demo resets delete Keelson cases, history stays.
* `eval_run_event` (`UNIQUE (run_id, seq)`, `data ≤ 16 KiB`): progress events, polled by `seq`.
* `eval_case_link` (PK `case_id`, FK `(tenant, case_id) → "case"` **cascade**, FK run cascade): the
  Keelson case → eval run link the runtime reads, with the link's own accounting columns. A demo reset
  deletes the links with the cases; eval runs and results stay.
* RLS: the four tenant tables are `ENABLE` + `FORCE` with the standard `tenant_isolation` policy
  (`tenant = app.tenant_id OR app.system = 'on'`). The admin API reads them with `system_session`
  (history is platform-wide and platform-admin only); the runtime port uses `tenant_session`.

### 3.2 Runtime: eval-linked cases (`runtime/execute.py`, `replay.py`, `plugins.py`, `deps.py`)

Port `RuntimeDeps.evals: EvalsPort | None` (`get_link`, `add_usage`; core adapter → `core.evals`;
`None` in fakes = no evals, unchanged behaviour). At run start `run_case` loads the case's link:

* **Lookup failure** → the run fails closed (`RUNTIME_UNAVAILABLE`): a replay eval case must never run
  in the process's mode.
* **Pre-checks, no model call:** eval run cancelled → run `cancelled`, `RUN_ERROR` `EVAL_CANCELLED`;
  live budget used up (`tokens_live ≥ max_tokens` or `cost_microusd ≥ max_cost`) → run `failed`,
  `EVAL_BUDGET_EXHAUSTED`.
* **Mode:** `RunContext.llm_replay_override = link.llm_mode`; `generate_with_replay` uses
  `effective_mode(process, override)`: no override → process mode; process `replay` → `replay`
  (never escalates); otherwise the override (an unknown value is a configuration error).
* **Budget:** live links set `eval_token_cap` / `eval_cost_cap_microusd` = what is left of the eval
  run's budget; `BudgetPlugin` aborts with `BUDGET_EXCEEDED` when the run reaches either (tokens after
  each response, cost before the next call since LiteLLM reports cost after the response).
* **Accounting:** in the run's `finally`, the metered usage (live calls/tokens, replayed calls/tokens,
  misses in replay-only mode, cost `round(cost_usd × 10⁶)`) is added once to the link and the eval run
  (atomic increments; best effort, never changes the run's outcome).
* **Replay miss:** `_public_failure` maps a `ReplayMiss` anywhere in the exception chain to
  `RUN_ERROR` `LLM_REPLAY_MISS` ("No recorded model answer matched (agent <name>) …"; the agent name is
  sanitised, no key or exception text). This applies to every replay-only run, not only evals (it was
  `INTERNAL_ERROR`); contract 30 lists the new codes.
* **KB embeddings:** query embeddings are not part of LLM record/replay. In a run whose effective mode
  is `replay` *because of an eval link*, the KB port (`deps._KbAdapter.search`) raises the existing
  "knowledge base unavailable" `LookupError` without calling `core.kb` (no `/embeddings` request); the
  playbook analyst reports a gap, as during an embedding outage.

Public run-stream codes added: `LLM_REPLAY_MISS` (all replay-only runs), `EVAL_CANCELLED`,
`EVAL_BUDGET_EXHAUSTED` (eval-linked cases only). The harness maps them to the case's `error_code`
(`EVAL_STOP_CODES`: `replay_miss`, `budget_exhausted` (also `BUDGET_EXCEEDED`), `cancelled`).

### 3.3 Summary, thresholds, compare, trends (`core/evals_summary.py`, pure)

* `summarise(cases)`: counts (cases, passed, failed, errors, skipped, checks, checks_failed,
  soft_failed), `pass_rate_bp` over **evaluated** cases (skipped / cancelled excluded),
  `adversarial_pass_rate_bp`, means / minimum of the per-case `_bp` metrics, latency stats per stage
  (`first_stage`, `exposure`, `options`, `total`: n, p50, max), tokens (`tokens_source` `engine` when
  every case has exact usage, `gateway` for `/1` deltas, `mixed`), cost, live/replayed calls, replay
  misses. Integer units, half-up rounding.
* Thresholds are **targets, never gates**: `EvalThreshold {metric, op min|max, value, modes}`;
  `DEFAULT_EVAL_THRESHOLDS` (snapshotted into every run): pass rate ≥ 8000 bp, adversarial pass rate ≥
  10000, entity recall mean ≥ 8000, numeric grounding min ≥ 10000, trajectory recall mean ≥ 8000 (all
  modes); first stage p50 ≤ 2000 ms, exposure max ≤ 30000 ms, options max ≤ 180000 ms, tokens / case ≤
  400000, cost / case ≤ 250000 µUSD (live and `unknown` modes only). Outcomes `ok | breach |
  not_measured | not_applicable`. Uncalibrated (no live run was possible): recalibrate after the first
  live runs.
* `compare_runs`: per case `fixed` (fail/error → pass), `regressed` (pass → fail/error), `changed`,
  `unchanged`, `new`, `removed`; per-case metric deltas with a `better` flag; newly failing / passing
  check names (capped); summary deltas toned by the metric's direction.
* `trends`: finished runs (oldest first) with their metric values and breaches, plus one status
  strip per case.

## 4. APIs and the runner

### 4.1 Admin API `/api/admin/evals` (`api/routes_admin_evals.py`)

Every route requires a **platform admin** (`403 platform_admin_required` otherwise); mutations need the
session's CSRF token and the Origin check like every admin mutation.

| Route | Answer |
|---|---|
| `GET /status` | `EvalsStatus`: tenant, runner `{configured, online, runner_id, version, last_seen_at, suite_hash}`, suite (cases: id, title, tags, turn kinds, `needs_live_embeddings`, `injects`), `modes[]` `{mode, available, reason, message}`, `process_replay_mode`, `live_caps`, `active_run`, recordings `{rows, bytes, newest_at, agents}` (the eval tenant's `llm_replay`) |
| `GET /runs?limit=&before=&mode=&source=` | `{items: EvalRun[], next_before}` (`mode` = a mode or `live`) |
| `POST /runs` (`EvalRunCreate`) | `202 EvalRun` (queued); audit `evals.run_requested` |
| `GET /runs/{id}` | `EvalRunDetail` (`run` + one `EvalCaseRow` per case) |
| `GET /runs/{id}/events?after=` | `{items ≤ 200, next_after, status, progress, counters}` (2 s polling) |
| `GET /runs/{id}/cases/{case_id}` | `EvalCaseResult` |
| `POST /runs/{id}/cancel` | `202 EvalRun`: queued → `cancelled` at once; running → `cancel_requested` and the in-flight Keelson runs of its linked cases are cancelled; audit `evals.run_cancelled` |
| `GET /runs/{id}/export` | `keelson-evals/2` attachment (finished runs); re-imports as that run |
| `POST /import?file_name=` | body = results JSON (≤ `EVALS_MAX_IMPORT_BYTES`): `201 {run_id, duplicate:false}` / `200 {…, duplicate:true}`; audit `evals.run_imported` |
| `POST /import/folder?file=` | imports every new `<UTC stamp>.json` of `EVALS_RESULTS_DIR` (or one): `{imported[], duplicates, errors[]}` |
| `GET /files` | the M7 listing + `imported_run_id` per file |
| `GET /compare?base=&head=` | `EvalCompare` |
| `GET /trends?limit=&mode=replay\|live` | `EvalTrends` |

`EvalRunCreate {mode = replay, case_ids: 1..50 unique | null (= the runner's whole suite),
acknowledge_reset: true, confirm_live, budget {max_tokens 1..50M, max_cost_microusd 1..100M}, note ≤ 200}`
(extra = forbid). Errors: `409 evals_unavailable {reason}` (`runner_not_configured`, `demo_mode_off`,
`runner_offline`, live only: `replay_only`, `live_disabled`, `gateway_budget_low`), `409 eval_running
{run_id}`, `422 unknown_cases {cases}` (vs the runner's reported suite), `422 too_many_cases` (`null` =
a suite of more than the 100 cases a run holds), `422 confirm_required` (live
without the typed `RUN LIVE`), `422 budget_required`, `422 budget_too_high` (above `EVALS_LIVE_MAX_*`).
`gateway_budget_low`: the eval tenant's LiteLLM team has a budget and spent it (`spend ≥ max_budget`, in
the gateway's own units); the gateway is only asked when a live mode could otherwise start, and a
failed lookup does not refuse (the gateway stays the outer cap). Imports: `413 file_too_large`,
`422 invalid_results`; folder import: `409 results_folder_unavailable`, `422 invalid_file_name`.
Unknown run ids (or non-UUIDs) are `404 not_found`. The M7 `GET /api/admin/evals` is unchanged.

### 4.2 Internal runner protocol `/api/internal/evals/runner` (`api/routes_internal_evals.py`)

`Authorization: Bearer EVALS_RUNNER_TOKEN` (`hmac.compare_digest`); token unset → `404`, wrong → `401`.
The runner also sends `Origin: <KEELSON_URL>` for the middleware's check. The token can claim and
report runs, never queue one or change configuration.

| Call | Effect |
|---|---|
| `POST /heartbeat {runner_id, version, suite}` | upsert `eval_runner` → `{poll_after_s}` (a catalogue above 256 KiB of jsonb text → `422 suite_too_large`) |
| `POST /claim {runner_id}` | reaps expired leases, then `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)` → `200 {job: EvalRunnerJob}` (lease `EVALS_LEASE_S`) or `204` |
| `POST /runs/{id}/events {runner_id, events ≤ 50}` | appends (seq assigned server-side, ≤ 5000 per run, strings cleaned; an event's `data` above 16 KiB of jsonb text → `422 event_too_large`) → `{cancel_requested, budget_exhausted, lease_expires_at}` |
| `POST /runs/{id}/cases/{case}/attach {runner_id, keelson_case_id}` | link: the case is in the run's tenant (else `404 case_not_found`), created after the run started (`409 case_predates_run`), not linked elsewhere (`409 case_already_linked`); idempotent for the same pair |
| `PUT /runs/{id}/cases/{case}/result {runner_id, result}` | ≤ 4 MiB report → `case_result_from_doc` (sanitised, capped) → merged with the link accounting (`engine`); a reported `fail` with engine-seen replay misses becomes `error` / `replay_miss`; an unlinked `keelson_case_id` is dropped; idempotent |
| `POST /runs/{id}/finish {runner_id, status, error?}` | final status (`cancelled` when a cancel was requested), summary + thresholds computed from the stored rows, `doc_sha256` of the run's export; audit `evals.run_finished` (actor `evals-runner`) |

Every call on a run needs that runner's running lease (`409 run_not_running`) and renews it; it also
counts as the runner's heartbeat (`eval_runner.last_seen_at`: the loop only heartbeats between runs, and
a live run can take an hour). An expired lease is reaped lazily (claim, status, listing and detail
reads): `failed` with `runner_lost` (or `cancelled` when a cancel was pending); the case results stay.
Malformed bodies (also pathologically nested JSON) are `422`, never `500`.

### 4.3 Runner sidecar (`evals/runner_service.py`, `evals/Dockerfile`, compose `evals-runner`)

Loop: heartbeat (suite catalogue + hash) → claim → per case `run_live.run_case` with `CaseHooks`
(`on_case_created` attaches the case **before** its first message; an attach failure aborts the case
before anything is posted; turn events; `stop_reason` checked before each turn; teardown resets always
run) → PUT the case result (retried with back-off: a 5xx never loses a result) → finish. A
`LeaseKeeper` thread renews the lease every `EVALS_RUNNER_LEASE_RENEW_S` while a turn runs. In `replay`
mode cases with `setup.kb_upload` are reported `skipped` (`needs_live_embeddings`). Once the cancel
flag is set the remaining cases are reported `cancelled`, and so is the case whose in-flight Keelson
run the cancel cut short (`RUN_ERROR` `CANCELLED`, even on its last turn: it is never judged); once the
live budget is used up they are
`skipped` (`budget_exhausted`). A lost lease (`run_not_running`) aborts without finishing. `--once`
handles at most one run.

Image: `python:3.14-slim-trixie`, uid 10001, `requirements-runtime.txt` (httpx, pydantic, pyyaml, the
engine's pins), copies `evals/keelson_evals`, `evals/cases`, `run_live.py`, `runner_service.py`,
`e2e/api_thin_slice.py` and `data/kb/adversarial` (which never ships in the engine image).

## 5. Eval harness and results format (`evals/`)

* **Trajectory** (`TurnExpect.trajectory {required, forbidden, min_recall = 1.0, max_calls}`):
  `agent:tool` patterns with `*` wildcards, matched in any order against the turn's
  `TOOL_CALL_START` events; check `<turn>.trajectory`; recall = matched / required. Starting
  expectations on `trim_delay_actual`, `store_shortage_question`, `adversarial_supplier_message`,
  `adversarial_kb_doc` (quarantine-wrapper reads required, raw untrusted and write tools forbidden) are
  listed in `soft` until a live run calibrates them.
* **Structured entities and targets** per turn (golden / missing / recall, latency budgets, grounding
  minimum), so the UI never parses detail strings.
* **Exact usage**: per turn and case, the sum of the final events' `metadata.usage` (per agent: calls,
  replayed, prompt / completion / reasoning tokens, wall ms, cost µUSD); the gateway delta stays as
  `tokens_gateway` (approximate).
* **Stop codes**: `RUN_ERROR` `LLM_REPLAY_MISS` / `EVAL_BUDGET_EXHAUSTED` / `BUDGET_EXCEEDED` /
  `EVAL_CANCELLED` end the case as `error` (`replay_miss`, `budget_exhausted`) or `cancelled`.
* **`keelson-evals/2`** (`report.py`): superset of `/1`: `mode` (`--mode-label`), `runner cli|in_app`,
  `suite_hash` (`suite.py`: sha256 of the canonical case models); per turn timings, usage,
  `final_code`, trajectory, entities, targets, `*_bp` next to the 0–1 floats; per case `duration_ms`,
  usage, `error_code`; summary `tokens_engine`, `cost_microusd_total`, `replay_misses`.
* **Conversion** (`keelson_contracts.evals.case_result_from_doc` / `case_result_to_doc`): both
  versions → integer units (floats → bp / ms half-up, gateway tokens flagged); strings stripped of
  control / bidi characters and truncated; lists capped (100 cases, 20 turns, 300 checks, 100
  trajectory calls); `case_result_from_doc(case_result_to_doc(r)) == r`. A document's identity is
  `results_doc_sha256` of its normalised form, so a file, its re-serialisation and the run's export
  dedupe to one run.
* The CLI keeps working unchanged (no hooks), and its files import into the UI.

## 6. Web (`apps/keelson-web`) and viz

All routes are platform-admin gated ("Platform admins only" for anyone else), like the engine.

* `/admin/evals` (`EvalsPage`): header actions New run / Import / Refresh; **status card** (runner
  online / offline with the start hint, engine mode, suite size + hash, live caps or the reason live is
  off, recordings coverage with a warning when there are none: replay runs stop at the first model call);
  **active run** (`EvalProgress`: cases done of total, current case / turn, events log, live tokens and
  cost against the budget, Cancel; polls `events?after=` every 2 s while queued / running and stops at a
  final status); **trends** (`EvalTrends`: All / Replay / Live tabs, `TrendChart` small multiples with
  their targets, case × run status strip); **runs table** (two selected rows → Compare); **results
  folder** (M7 listing with Import / Imported and "Import all new").
* `/admin/evals/runs/[runId]`: summary tiles, threshold outcomes, progress while running, cases table
  linking to the drill-down, adversarial matrix (cases × `guard_flagged`, `no_write_tool_calls`,
  `no_write_tools_bound`, `no_untrusted_tool_calls`, `no_commit_without_human`, …), Export JSON, Compare
  with the previous run.
* `/admin/evals/runs/[runId]/cases/[caseId]`: tabs Checks (per turn, failed first, soft labelled),
  Trajectory (required ✓/✗, forbidden hits, actual calls per agent), Entities (found / missing chips,
  recall vs minimum), Latency (a meter per budget), Usage (per agent), Adversarial (tagged cases). The
  Keelson case link shows only for a linked UUID, noting a demo reset may have removed it.
* `/admin/evals/compare?base=&head=`: run pickers, delta tiles toned by direction, case changes
  (regressed = danger, fixed = success), newly failing / passing checks.
* `NewEvalRunDialog`: modes with the engine's reasons when unavailable; cases by tag (kb cases
  "skipped in replay"); replay needs the Merxa-reset acknowledgement; live needs a token and a cost
  budget (USD → micro-USD), shows an estimate from the last live run, and enables Confirm only once
  `RUN LIVE` is typed and the budget is within the caps; navigates to the new run.
* `ImportEvalsDialog`: `.json` ≤ 8 MiB, early client checks (JSON object, `keelson-evals/1|2`, 1–100
  cases; the engine validates fully), duplicate notice with a link to the run.
* LLM-derived text (check details, errors, notes) renders as plain text only: no markdown, no links.
* `@keelson/viz` `TrendChart` (ECharts line, SVG renderer, loaded on demand, dashed target line,
  visually hidden data table with every point and the target, token colours; no new dependency).

## 7. Tests, e2e, live verification

* contracts-py `tests/test_evals.py` (19): both committed `/1` fixtures and the `/2` fixture convert;
  exact bp / ms conversion; caps and sanitising (incl. replayed ≤ calls and deep nesting); round trip;
  `EvalRunCreate` rules; thresholds
  (`ok` / `breach` / `not_measured` / `not_applicable`); schema export. contracts-ts `m8-evals.test.ts`.
* Engine platform: `test_m8_evals_api.py` (RBAC on every route, availability reasons incl.
  `gateway_budget_low`, run requests incl. `too_many_cases`, imports + duplicates + 413 / 422 (also
  deeply nested JSON and inconsistent usage), folder import with unreadable files, export → re-import,
  compare of the real fixtures, trends, recordings counts, RLS, case-delete cascade),
  `test_m8_evals_runner_protocol.py` (token 404 / 401, full protocol, concurrent claims → one job, lease
  expiry → `runner_lost` with results kept, events lease + caps incl. `event_too_large`, a working runner
  stays online, cancel queued / running incl. Keelson runs, live budget flag + accounting, attach rules,
  sanitised + merged results), `test_m8_evals_summary.py` (pure summary / compare / trends, untrusted
  usage), `test_migrations_rls.py` (new tables).
* Engine runtime `test_m8_eval_runtime.py` (FakeLlm behind the replay layer, in-memory store):
  `effective_mode` matrix; a replay eval case never calls the model and ends `LLM_REPLAY_MISS`; process
  replay + record override stays replay; recordings replay; unlinked cases unchanged; failing lookup fails
  closed; cancelled / exhausted pre-checks; remaining-budget cap; cost cap; usage reported once; KB
  searches in replay eval runs never reach `core.kb`; the miss message carries no key or exception text.
* `evals/tests`: trajectory matcher, v2 report (validated against `keelson_contracts.evals` when it is
  importable), runner hooks, `test_runner_service.py` against a fake stack + fake engine (attach before
  the first message, kb case skipped in replay, cancel between turns with teardown, a cancel that cuts
  the last turn short → `cancelled`, budget stop, 5xx retries, lost lease, unknown cases);
  `run_live.py --dry-run`.
* Web `test/m8-evals.test.tsx` (20, axe): status hints and warnings, polling with fake timers that stops
  at a final status, trends tables, compare selection, dialog rules and POST body, import checks and
  duplicate notice, threshold badges, adversarial matrix, drill-down tabs, compare tones, tenant-admin
  gate; updated M7 evals tests. viz `TrendChart.test.tsx`.
* **Offline e2e** `e2e/api_m8_evals_offline.py` (stack with `LLM_REPLAY_MODE=replay`, runner up): runner
  online with ≥ 7 cases, replay-only modes; a replay run of `store_shortage_question` ends `succeeded`
  with `llm_calls_live == 0`, `tokens_live == 0`, unchanged gateway usage, case `pass` or `error` /
  `replay_miss`; a live request → `409 evals_unavailable` (`replay_only`); both CLI fixtures import (again
  → duplicate); compare transitions; trends; export → re-import duplicate; sam → 403. Commands:
  `e2e/README.md`.
* **Live** `e2e/api_m8_evals_live.py` (`scripts/verify-live.sh --only m8-evals`, needs
  `EVALS_LIVE_ENABLED=1` and credits): one `record_or_replay` run of `trim_delay_actual` within a 1.5M
  token / $2 budget.

## 8. Config, env, compose

engine-api (`config.py`, compose `engine-api.environment`):

| Variable | Default | Meaning |
|---|---|---|
| `EVALS_RUNNER_TOKEN` | empty | runner bearer token; empty = runner API 404, in-app runs unavailable |
| `EVALS_LIVE_ENABLED` | `0` | allow live eval runs (replay runs need nothing) |
| `EVALS_LIVE_MAX_TOKENS` | `3000000` | hard cap of one live run's token budget |
| `EVALS_LIVE_MAX_COST_MICROUSD` | `5000000` | hard cap of one live run's cost budget (µUSD) |
| `EVALS_RUNNER_STALE_S` | `60` | runner offline after (settings only, not in compose) |
| `EVALS_LEASE_S` | `180` | claimed-run lease (settings only) |
| `EVALS_MAX_IMPORT_BYTES` | `8388608` | largest imported document (settings only) |
| `EVALS_RESULTS_DIR` | `/app/evals/results` | unchanged (M7); the folder-import source (read-only mount) |

evals-runner (profile `evals`, `networks: [edge]`, `read_only`, tmpfs `/tmp`, 256 MB, 0.5 CPU,
depends on a healthy engine-api): `KEELSON_URL=http://keelson.localhost`, `MERXA_URL=http://merxa.localhost`,
`EVALS_RUNNER_TOKEN`, `EVALS_RUNNER_POLL_S=5`, `KEELSON_E2E_DIR=/app/e2e` (optional
`EVALS_RUNNER_LEASE_RENEW_S`, default 30). `scripts/init_secrets.py` generates `EVALS_RUNNER_TOKEN`;
`.env.example` documents the variables. engine-worker needs nothing new (it reads the link from the DB).
Start: `docker compose --profile evals up -d evals-runner` (DEMO_MODE stacks only; it resets Merxa per
case, never next to a live demo).

## 9. Security

* Platform admins only (history is platform-wide); tenant admins and planners get 403.
* Live modes additionally need `EVALS_LIVE_ENABLED=1`, a process mode other than `replay` (re-checked
  per Keelson run by `effective_mode`, fail closed), `DEMO_MODE`, the typed confirmation and a budget
  within the env caps, enforced in-engine per Keelson run (`BudgetPlugin`) and per eval run (pre-check),
  with the LiteLLM team budget as the outer cap (`gateway_budget_low` refuses when it is spent). Caps
  can only be raised through ops env; no four-eyes (starting an eval is not a §17 guarded change).
* Replay mode makes no provider call: forced replay per run, misses fail closed, KB query embeddings
  are refused in replay eval runs, kb-upload cases are skipped.
* The runner token can claim / report, never enqueue or configure; constant-time compare; the
  internal routes authenticate themselves although Caddy routes `/api/*` publicly.
* Runner reports and imports are untrusted: strict models after the converter (caps, control / bidi
  stripping), ≤ 1 MiB per case, a Keelson case id is kept only when linked, `keelson_url` is a label,
  nothing from a result is fetched, linked, used as a path or sent to a model; folder import accepts only
  `<UTC stamp>.json` names.
* RLS `FORCE` on all eval tenant tables; links cascade with cases; history survives demo resets.
* The runner needs no egress: it only calls the fixed URLs from env (a job never names a URL). The
  compose `edge` network it sits on is not an isolated network, though (see Known issues).

## 10. Deviations

From `120`: none to allocations (migration `0012`, service `evals-runner` / profile `evals`, `EVALS_*`,
`evals.py`, `e2e/api_m8_evals_offline.py`). Additional shared files touched: `docs/contracts/30`
(additive code list), `runtime/deps.py` / `execute.py` / `replay.py` / `plugins.py` / `context.py`
(additive), `scripts/verify-live.sh` (step `m8-evals`). One deliberate behaviour change outside evals: a
replay miss is now `LLM_REPLAY_MISS` on the stream instead of `INTERNAL_ERROR` (no golden fixture
depends on it).

From the brief:

* Migration `0012` (not `0011`), per `120` §2.
* No strict `EvalResultsDoc` schema / `evals.results-doc` export: the file format is lenient legacy JSON,
  so the engine converts it with a sanitising converter (`case_result_from_doc`) into strict models, and
  the import dialog does light early checks instead of a generated schema (a strict client schema
  would reject files the engine accepts). Exported schemas instead: `evals.status`, `evals.run`,
  `evals.run-detail`, `evals.run-create`, `evals.run-event`, `evals.case-result`, `evals.compare`,
  `evals.trends`, `evals.threshold`, `evals.suite`.
* The eval port is `RuntimeDeps.evals` (`EvalsPort`), not new `core/repo.py` functions; `FakeRepo` is
  unchanged.
* Cancelling a running eval cancels the in-flight Keelson runs engine-side (`POST /runs/{id}/cancel`),
  not by the runner as a persona.
* The live budget also has an in-run **cost** cap (`eval_cost_cap_microusd`), not only tokens.
* Dedupe index covers every run with a `doc_sha256` (imports and finished in-app runs), so an in-app
  run's export re-imports as that run.
* Threshold outcomes add `not_applicable` (run mode outside the threshold's modes); imported files
  without a mode label are `unknown` and treated like live runs for targets.
* `gateway_budget_low` refuses when the team budget is spent (compared in the gateway's own units)
  rather than "live max cost > headroom": the eval budget is micro-USD while the gateway figures are
  labelled EUR, and mixing them would be wrong.
* `eval_case_result` keeps a narrower set of listing columns (the full result is in `result`); links
  carry their own accounting columns (merged into case results).
* Summary field `cost_microusd_total` (integer) instead of `cost_usd_total`.
* The trajectory diff is a tab of `EvalCaseDetail`, not a separate component.
* Added `e2e/api_m8_evals_live.py` + verify-live step now (to be run when credits return).

## 11. Deferred

* **Recordings export / import** (`llm_replay` JSONL per tenant) so recordings survive volume cleanups.
* **Embedding record / replay** (KB upload and `kb.search` query embeddings), which would make KB cases
  runnable in replay mode (until then they are skipped, and KB searches in replay eval runs report the
  knowledge base unavailable).
* **New golden / adversarial cases** from plan §20 (typhoon signal pack → alert → case, ambiguous chat,
  conflicting signals; needs an `alert_pack` turn kind and live calibration).
* **Nightly scheduled replay eval** (`EVALS_NIGHTLY_UTC_HOUR`; off by default because it resets Merxa).
* **SSE progress stream** (`GET /runs/{id}/stream`) replacing 2 s polling.
* **Hiding eval-linked cases** from planners' case list (`/api/cases?include_eval=1`).

## 12. Known issues

* Recordings are gone (no `keelson_*` volumes on the build machine) and credits are depleted, so every
  replay run currently ends `error` / `replay_miss`; the offline e2e asserts the fail-closed invariants
  (zero live calls), not passing cases. Any further volume cleanup wipes recordings again (deferred
  export / import is the mitigation).
* In replay eval runs the playbook analyst sees an unavailable knowledge base, so a recording made in a
  live run whose agents searched the KB no longer matches (replay miss) until embedding replay exists.
* Non-eval runs in an `LLM_REPLAY_MODE=replay` installation still embed KB queries live (pre-existing,
  unchanged).
* Trajectory expectations and default thresholds are uncalibrated (soft / targets only).
* Eval-created Keelson cases appear in the planners' case list (as with the CLI).
* The runner token is a long-lived bearer secret in `.env`: keep the `evals` profile off on hosted
  demos. With the profile up and no token, the runner exits (status 2) and compose restarts it.
* Long live turns rely on the runner's lease thread; if engine-api is down for longer than
  `EVALS_LEASE_S` the run is reaped as `runner_lost`.
* The runner's "no egress" is by code, not by network: `edge` (like every compose network here) is a
  plain bridge, so the container could reach the internet. Making it an `internal` network would cut
  Caddy's published port; a dedicated internal network shared with Caddy is the clean fix (workstream 9 /
  integration).
* The results-folder listing marks a file "Imported" by file name: a file whose content was already
  imported under another name (or is an in-app run's export) still offers Import, which then answers
  "already imported" (dedupe is by document content, so no duplicate run is created).
* The M7 listing `core/audit.list_evals` (unchanged here) reads every results file with `json.loads`; a
  pathologically nested file in the results folder makes `GET /api/admin/evals` and `/files` answer
  500 (the M8 imports of such a file are 422 / an error entry). The folder is ops-controlled.
* Cancelling an eval cancels its *queued / running* Keelson runs; a run paused at an approval card stays
  paused (its resume is refused with `EVAL_CANCELLED`).
