# 120 · M8 (post-demo) — overview, allocations and cross-cutting rules

Plan §19 "M8 (post-demo)": mapping compiler and LLM-proposed mappings, adapter sidecar · SimHash and
vector clustering, more feeds · CP-SAT (MOQ and resequencing), CO₂, p10/p90 robustness · Protomaps,
Evals UI, Renovate and cosign automation.

Builds on every earlier contract (00–110). M8 is nine independent workstreams; each one owns a
detailed spec doc (121–129) written by its builder. This file is the **source of truth for the seams
between them**: numbers, names, shared files and the definition of done. If a workstream needs to
change anything allocated here, it records the deviation in its own spec doc.

Models: Flash/Flash-Lite only. Gemini credits are depleted, so **everything in M8 must work and be
tested with no live model calls** (FakeLlm, replay, or a deterministic non-LLM path). Live checks go
into `scripts/verify-live.sh`, never into the default test runs.

## 1. Workstreams

| # | Workstream | Spec doc | Branch | Main services / packages |
|---|---|---|---|---|
| 1 | Mapping compiler (DuckDB) + LLM-proposed mappings | `121-m8-mapping-compiler.md` | `m8/mapping-compiler` | engine, contracts, keelson-web admin |
| 2 | Adapter sidecar template (+ "Fernhill" demo ERP) | `122-m8-adapter-sidecar.md` | `m8/adapter-sidecar` | new `services/adapter-sidecar`, engine commit/sync, infra |
| 3 | Signals: SimHash near-duplicates + vector clustering | `123-m8-signal-clustering.md` | `m8/signals-clustering` | signal-hub, engine signals admin, web alerts/admin |
| 4 | Signals: more feeds (NHC, Copernicus EMS, OFAC, GDELT, MeteoAlarm) | `124-m8-signal-feeds.md` | `m8/signals-feeds` | signal-hub, web Signals admin, data/scenarios |
| 5 | Optimizer: CP-SAT for MOQ and resequencing | `125-m8-optimizer-cpsat.md` | `m8/optimizer-cpsat` | optimizer, engine planning, web compare/admin |
| 6 | Optimizer: CO₂e KPI + p10/p50/p90 robustness | `126-m8-co2-robustness.md` | `m8/optimizer-co2-robustness` | optimizer, engine planning, viz blocks, admin scoring |
| 7 | Self-hosted Protomaps base map | `127-m8-basemap.md` | `m8/maps-protomaps` | viz map, keelson-web, caddy, infra/basemap |
| 8 | Evals UI (in-app runner, history, drill-down, trends) | `128-m8-evals-ui.md` | `m8/evals-ui` | engine, evals/, keelson-web admin, viz |
| 9 | Supply-chain hardening: Renovate, CI, cosign | `129-m8-supply-chain.md` | `m8/supply-chain-hardening` | `.github/`, `renovate.json`, Dockerfiles, compose, scripts |

Items each workstream explicitly defers are listed in its spec doc under **Deferred**; nothing deferred
is silently dropped.

## 2. Allocations (do not reuse)

**SQL migrations** (`core/migrate.py` refuses duplicate versions):

| Service | Number | Owner |
|---|---|---|
| engine | `0011_m8_mapping_compiler.sql` | 1 |
| engine | `0012_m8_evals.sql` | 8 |
| signal-hub | `0004_m8_clustering.sql` | 3 |
| signal-hub | `0005_m8_more_feeds.sql` | 4 |
| merxa-api, optimizer | none in M8 | — |
| adapter-sidecar (new, own SQLite state DB) | `0001_init.sql` (own runner) | 2 |

**Compose services / profiles / hosts:**

| Item | Owner |
|---|---|
| `fernhill-sidecar` (port 8400, profile `adapters`, volume `fernhill_data`, host `http://fernhill.localhost`) | 2 |
| `evals-runner` (profile `evals`) | 8 |
| `basemap-fetch` (profile `basemap`, volume `basemap`), Caddy route `http://keelson.localhost/basemap/*` | 7 |
| image digest pins for every `image:` / `FROM` line | 9 |

**Env var prefixes:** `MAPPING_*`, `SCN_MAPPING_ENGINE`, `SCN_RAW_*`, `DUCKDB_*` (1) · `SIDECAR_*`,
`FERNHILL_*` (2) · `SIGNALS_EMBED_*`, `SIGNALS_CLUSTER_*` (3) · `NHC_*`, `COPERNICUS_EMS_*`, `OFAC_*`,
`GDELT_*`, `METEOALARM_*`, `CADENCE_<SOURCE>_S` (4) · `OPTIMIZER_CPSAT_*` (5) · `EMISSIONS_DIR` (6) ·
`KEELSON_BASEMAP`, `BASEMAP_*`, `KEELSON_CSP_MAP_ORIGINS` (7) · `EVALS_*` (8).

**Contracts-py modules:** new modules are owned by one workstream (`mapping_proposal.py` → 1,
`scoring.py` → 6, `evals.py` → 8). Shared modules receive **additive** edits only:
`signals.py` (3, 4), `optimizer.py` (5, 6), `mapping.py` + `scn.py` (1, 2), `admin.py`
(1, 5: new `ChangeRequestKind` / `GuardReasonCode` literals), `receipts.py` (2).

**e2e scripts** (written by the workstream, run by the integrator on the full stack):
`e2e/api_m8_mappings_offline.py` (1), `e2e/api_m8_sidecar.py` (2), `e2e/api_m8_clustering_offline.py`
(3), `e2e/api_m8_feeds_offline.py` (4), `e2e/api_m8_cpsat_offline.py` (5),
`e2e/api_m8_robustness_offline.py` (6), `e2e/playwright/tests/m8-basemap.spec.ts` (7),
`e2e/api_m8_evals_offline.py` (8).

## 3. Cross-cutting rules

1. **Backwards compatible contracts.** Every new contract field is optional with a default that is
   *omitted from dumps* (`exclude_if` / the existing `_m3_optional()` pattern), so M1–M7 payloads,
   `inputs_hash` values, replay keys and golden fixtures stay byte-identical. New behaviour that
   changes results is opt-in (spec/pack setting), and the M7 path stays the default unless the
   workstream's spec doc says otherwise and updates the goldens deliberately.
2. **Contract chain.** Pydantic (contracts-py) is the source; JSON Schema + Zod in contracts-ts are
   regenerated, never hand-edited. Regenerate after every contracts change and commit the output.
   The integrator regenerates once more after merging.
3. **Offline first.** LLM features need (a) a deterministic non-LLM path that works with credits at
   zero and (b) FakeLlm tests of the LLM path. Untrusted text (vendor samples, news, feeds) only
   reaches a model through the existing quarantine/screening helpers, fenced, as data.
4. **Security invariants unchanged.** Outbound HTTP only via the egress proxy; tenant tables are RLS
   `FORCE`d; LLM agents hold no write tools; guarded changes (plan §17 + contract 110 §1) go through
   four-eyes change requests; AI-proposed configuration is never auto-activated.
5. **Determinism.** Seeds, solver limits and sample counts that affect results are code constants or
   spec settings, never deployment env (env may only add safety caps that fall back with a recorded
   reason).
6. **Money/units.** Integer minor units, integer quantities, `_bp` ratios, `_ms` durations,
   `cost_microusd`, `*_kg` for CO₂e; no floats in contracts or hashed payloads.
7. **Shared files.** Keep edits to shared files minimal and additive (one router registration line in
   `api/main.py`, one block in `compose.yaml`, one section in `.env.example`, etc.). Prefer new
   modules/components over growing shared ones. Do **not** edit the root `README.md` (the integrator
   updates the status table and docs index); do update the service READMEs you touch.
8. **Docker-only builds and tests.** Toolchains run in containers (contract 60). Never start the full
   compose stack from a workstream; the integrator builds and runs it.

## 4. Definition of done (per workstream)

1. All non-deferred scope items implemented, following the code style around them.
2. New unit/DB/web tests for the new behaviour; the full test suites of every service/package the
   workstream touched pass in Docker (known pre-existing environment failures excepted, see §5).
3. Contracts regenerated and `--check` clean when contracts changed; web `typecheck` and `test` pass
   when web/viz/contracts-ts changed.
4. Spec doc 12x written: scope, design, APIs, data, config, security, tests, deviations from this
   overview, **Deferred**.
5. Offline e2e script written (it is executed during integration).
6. Commits on the workstream branch, nothing pushed.

## 5. Known pre-existing environment failures (not M8 regressions)

Seen in the Docker test harness on the M7 baseline: `signal-hub tests/test_db_pipeline.py::test_bootstrap_with_non_superuser_admin`
(depends on the Postgres/PostGIS image privileges) and `engine tests/platform/test_kb.py` PDF tests
(`test_pdf_bomb_is_refused_in_an_isolated_child`, `test_pdf_child_does_not_inherit_secrets`,
`test_admin_api_rbac_upload_limits_pdf_and_audit`: the isolated PDF child reports `pdf_too_complex`
under container limits). The engine runs `tests/platform` and `tests/runtime` as separate pytest
invocations (shared test-module basenames).

## 6. Integration

Branches merge into `m8-post-demo` in dependency-light order (9, 7, 8, 3, 4, 1, 2, 5, 6), the contract
chain is regenerated, all suites run, the images are built with Docker, the stack is brought up and the
M8 offline e2e scripts plus the M5–M7 offline e2e scripts are run. The root README status table marks
M8 accordingly.

## 7. Integration record (2026-10-10)

All nine workstreams were built, reviewed adversarially per workstream (every verdict "ready with
issues", findings fixed on the branches), merged into `m8-post-demo` in the §6 order, and reviewed
once more across the seams. Integration fixes (commits "M8 integration: …"):

- **Merge seams.** Planning and re-plan build every request with `OptimizeInputs` (126) and pass
  `solver=solver_options(planning)` (125); a dropped `SolverOptions` import from that merge was caught
  by the cross-workstream review and is covered by a seam test. Signal Hub keeps clustering's
  refactored same-hazard helper with the feeds' per-subtype window, and its explanations name the window
  applied and the new authoritative feeds. The viz fixture order (CP-SAT note, then the robust compare
  and KPI strip) is the order the dev-blocks e2e expects.
- **Supply chain.** The three Dockerfiles added by other workstreams (adapter-sidecar, evals-runner,
  basemap-fetch) are pinned to the repo's tag@digest base images and registered in
  `infra/supply-chain/images.json` (`check_pins.py`: 0 violations). Cloud Run parity: the engine runs
  `MAPPING_PROPOSER=auto` there; the evals runner and the compose-only DuckDB cap stay local.
- **Test images.** The engine test stage names `GEO_DIR` / `TAXONOMY_DIR` / `EMISSIONS_DIR` (the
  non-editable install hid the source-tree fallback; latent since M6), and a signal-hub test locates its
  migrations like the service does.
- **e2e.** `api_m8_sidecar.py` takes the Fernhill keys from its environment (the engine image cannot read
  the owner-only `.env`), and expects `totals.lines` to count change-set items like Merxa's validate.

**Verification** (Docker only; `scripts/supply-chain/ci_run_suite.py`, i.e. each Dockerfile `test`
target with a throwaway Postgres/Valkey, exactly as CI): engine-platform 640, engine-runtime 984,
signal-hub 429, optimizer 384, merxa-api 205, adapter-sidecar 140, contracts-py 1625 (+ mypy), evals 127,
supply-chain 285, keelson-web 558 (typecheck + lint + vitest), web packages (viz 360 + contracts / tokens
checks), merxa-web 211, basemap (node:test) — all passing (the §5 environment failures are deselected).
All compose images build (`--profile adapters --profile evals --profile basemap`). On the full stack
(`LLM_REPLAY_MODE=replay`, profiles `adapters` + `evals`): `api_m5_offline`, `api_m6_offline`,
`api_m7_offline` and all seven M8 offline scripts (`api_m8_mappings_offline`, `api_m8_sidecar`,
`api_m8_clustering_offline`, `api_m8_feeds_offline`, `api_m8_cpsat_offline`,
`api_m8_robustness_offline`, `api_m8_evals_offline`) pass, and so do the Playwright specs
`m8-basemap`, `m8-co2-robustness` and `dev-blocks` (no model). The co2 spec's page-wide axe scan found
React Flow's attribution link below 4.5:1 (an M3 block); it now uses the surface / fg-muted tokens.
Live (model-backed) checks remain for `scripts/verify-live.sh` once credits are available.

**Open issues carried forward** (details in each spec's Known issues): CP-SAT mode misses the 5 s solve
budget under host load (6.9–7.9 s worst case; its perf tests allow 10 s; the default `repair` mode is
unaffected) and resequencing v2 can hit its wall-clock guard on very large lines (it then falls back with
the reason recorded); emission factors are indicative, not verified against the official GLEC tables;
MeteoAlarm stays off until an operator accepts its terms; opt-in feeds can still shape global events
other tenants see; the clustering merge pass is O(n²) over at most 500 open candidates per interval;
demo-pack expectations assume the default clustering settings; the sidecar's OAuth inbound mode accepts
RS256 only and its SQL backend settles an in-doubt apply at restart; evals results-folder imports match
by file name; two engine tests that read files outside the test image are skipped in the CI image.
