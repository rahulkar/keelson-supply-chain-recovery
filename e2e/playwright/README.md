# Browser e2e (Playwright) — thin slice + M3 canvas

Real-browser tests of Keelson + Merxa against the **running compose stack** (Keycloak, engine with
the real Gemini models behind LiteLLM, optimizer, Merxa). They complement the API-level
`e2e/api_thin_slice.py`: everything here is driven through the UI.

Stack: `@playwright/test` 1.63.0 + `@axe-core/playwright` 4.13.0 (TypeScript, `package-lock.json`),
run in `mcr.microsoft.com/playwright:v1.63.0-noble` on the `keelson_edge` network. This folder is
a standalone npm project (not part of the pnpm workspace); `node_modules` lives in the named volume
`keelson_e2e_pw_nm`, the npm cache in `keelson_npm_cache`.

## Run

```bash
# Git Bash, repo root; the whole stack must be up (web apps included) and GEMINI_API_KEY set.
env -u LITELLM_MASTER_KEY docker compose up -d --build

e2e/playwright/run.sh                    # whole suite (~8–10 min, 3 real LLM analyses)
e2e/playwright/run.sh act3-thin-slice    # golden journey only (~3–4 min)
e2e/playwright/run.sh --list             # any `playwright test` arguments pass through
E2E_SHELL=1 e2e/playwright/run.sh        # a shell in the container
```

`run.sh` reads `DEMO_USER_PASSWORD` from the environment or `.env` (never printed), resolves
Caddy's address on `keelson_edge` (`EDGE_IP`) and starts the container with `--memory 2g`. The
config maps `*.localhost` to `EDGE_IP` with Chromium's `--host-resolver-rules` (Chromium otherwise
hard-wires `*.localhost` to loopback); the URLs stay `*.localhost`, so they are secure contexts
and Keycloak's `Secure` cookies behave exactly as in a desktop browser. Node-side API calls
(`request.newContext`) resolve the same names through Docker DNS.

The suite resets the demo (Merxa `scenario:reset`, then a Keelson canonical-store resync requested
as tenant admin `sam` and awaited — M3 frames cases from the synced `scn` snapshot — and Marco's
Merxa link disconnected) — do not run it against a stack you are presenting from. Keelson cases are
never deleted.

## Tests

| File | What |
|---|---|
| `act3-thin-slice.spec.ts` | **Golden journey**: Keycloak sign-in → persona switcher → Priya → trim-delay message on Home → calm ThinkingTimeline (collapsed; stage chips; "Show thinking" → "Inventory & demand analyst" with sentence-style activities, no raw tool names) → approach card "Looks right" → narrative (no `{{`), `kpi_strip`, Recommended option with "Apply changes for me", Canvas with the plan diff → **M3 canvas visuals** (Result tab order scenario_compare, network_map,
impact_graph, stockout_timeline, plan_diff; each waited for until drawn: ECharts SVG, React Flow nodes
with at least one node in the opening view, the MapLibre canvas — WebGL, not the table fallback;
screenshots taken in a 1280×1600 viewport) → Apply → approval card Priya cannot approve → persona Marco → "Connect Merxa" → Keycloak (merxa realm) as `marco.rossi` → back on the case → Approve → commit tracker "Verified" → "Open in Merxa" → Merxa persona switcher (Marco) → Change Batch `applied`, "Changed by Marco Rossi via Keelson (APR-…)", before/after rows. Every turn shows ≤ 2 inline cards. **M4**: the approach card lists ≥ 3 planned checks ("What I'll check") and "Who I'll consult"; the thinking timeline shows ≥ 3 specialist agent rows ("… analyst") and the Critic; before Apply a follow-up "What if the delay doubles?" re-simulates (no approach card, a new recommendation, a "What changed: Plan vN → vN+1 …" line); "Confirm these values" is clicked first when the plan relies on unconfirmed values. |
| `act2-whatif.spec.ts` | **Act 2** (M4, `90` §8): Priya asks "What if the Suez Canal closes for 3 weeks?" on Home → the approach card shows the **What-if** badge, the what-if note and the planned checks (no "Treat as what-if" button) → "Looks right" → results: **Apply disabled** with "…promote it to an actual case first", **Promote to actual case** enabled, axe-clean → Promote → a new recommendation (plan v+1, re-simulated) without Promote and with Apply enabled (values left unconfirmed are confirmed first). Nothing is applied. |
| `auth.spec.ts` | Anonymous → Keycloak sign-in (PKCE) → back on the requested page; 401 / public health / 307 for pages. |
| `dev-blocks.spec.ts` | `/dev/blocks` renders all 34 fixtures (12 M1 blocks, 6 M3 visual states incl. empty and too-large maps, 3 RawDataCard fallbacks, 3 M4 states, 10 M5 states: four-eyes / rejected / bundle approvals, the mixed apply + guide option, Guide checklists open and completed, commit tracker Undo / conflicts / rolled back, the re-plan banner), both densities, no console errors. |
| `m5-offline.spec.ts` | **M5, no model calls** (`95` §7): the M5 fixtures on `/dev/blocks` — Guide checklist (verified / pending steps, "Open in Merxa" deep links in a new tab, Mark done → previewed action, "Guide completed"), four-eyes progress ("1 of 2 approvals … waiting for Compliance"), the bundle's systems, "Apply 3 changes for me · Guide me through 2", the re-plan banner with plan versions, commit tracker **Undo changes** → confirm → rollback action, conflicts, rolled back; axe-clean. Admin **Policies** as sam: rules in evaluation order, the test bench, raising the auto cap shows "requires a platform admin" and disables Save, a description edit saves as a new policy version (restored afterwards). **Demo reset** from the persona menu as ava (rehearsal mode: 10 steps with M6's `signal_hub`, consents and connections skipped); a planner is not offered it. |
| `m6-alerts.spec.ts` | **M6, no model calls** (`100` §8), only through `run-m6.sh` (it holds the engine worker; skipped otherwise): presenter ava resets + starts the typhoon pack from the persona menu (Demo scenarios dialog) → priya's Home pill "1 new alert" → `/alerts` row (Medium, why, exposure summary, DEMO) → drawer (engine map: footprint + exposed sites, exposed sites/lanes list, GDACS source with CC BY, update timeline, marked seen) → **Open case** → the case page with its queued signal run ("Getting started…"); alert `opened`, repeat open-case → same case, the run is cancelled. Axe-clean on every page. Screenshots 27–30. |
| `m8-basemap.spec.ts` | **M8 self-hosted base map, no model calls** (`127`): the `network_map` fixture on `/dev/blocks` with every request leaving `*.localhost` aborted (`context.route`), in the light and the dark theme. Base map installed (`GET /basemap/manifest.json` 200, e.g. after `docker compose --profile basemap run --rm basemap-fetch --profile lite`) → `data-map-basemap="protomaps"`, ≥ 1 `206` from `/basemap/v4/`, **no** third-party request attempted; nothing installed (204) → `data-map-basemap="offline"` (built-in Natural Earth outline) with the "simplified background map" notice. Both: a drawn canvas, no "couldn't be loaded" notice, attribution visible with its OpenStreetMap / Natural Earth link, axe-clean, no page errors. Needs the default `KEELSON_BASEMAP=auto`. Screenshots 37. |
| `reduced-motion.spec.ts` | `prefers-reduced-motion: reduce` → static headline (`data-motion="reduced"`), no shimmer / pulse animations; switching the preference live brings the sweep back. |
| `keyboard-approve.spec.ts` | Case prepared via the API up to the approval; Marco reaches Connect Merxa (if needed), the Keycloak form and Approve with Tab / Enter only, each with a visible focus ring; the commit verifies and the card shows the "Approved · Decided by Marco Rossi" note instead of its buttons. |
| `a11y.spec.ts` | axe (WCAG 2.2 A/AA + best practice) on Home (+ persona menu) and a finished case (collapsed, everything expanded, canvas open with the M3 visuals drawn) at 1280 px and 390 px: no serious/critical violations. |
| `trace-hygiene.spec.ts` | No stack needed: text typed inside `Tracer.secret()` never reaches a trace (routed form post), and the artifact scanner finds text typed outside it. |
| `layout.spec.ts` | No horizontal overflow at 1280 px and 390 px (Home, Cases, Alerts, /dev/blocks, a case with the canvas side panel / bottom sheet); Escape closes the sheet and returns focus; unknown case → 404 with a page heading. |
| `admin-smoke.spec.ts` | M2 back-office as **sam** (tenant-admin): Overview (nav without the platform-only Tenants) → Agents: type a recommender prompt addendum, "Review and save" → JSON diff dialog → "Save version" → toast "Saved as v<n>", Versions drawer lists it → Connectors: Merxa row with tiers A/B/C/W, Add-connector wizard (Basics filled, not submitted) → AI Gateway: model aliases + usage + budget sections; axe (no serious/critical) on every page. The spec is restored to its starting version via the API afterwards. **Knowledge** (M4, `90` §7): the seeded "Playbooks & policies" KB lists its documents as Ready, the embedding profile (1,536 dimensions) is shown, axe-clean; a small Markdown file is uploaded through the file input, indexed (Ready) and deleted again. Priya sees "Admins only" and 403 on `/api/admin/*`. Merxa M2 pages (Sailings & lanes, API & MCP clients) render for `merxa.admin`. |

## Artifacts (`e2e/playwright/artifacts/`, gitignored)

- `traces/<test>-r<retry>-<n>.zip` — Playwright traces (screenshots, DOM snapshots, network,
  sources) for every test, also attached to the HTML report. Password entry on the Keycloak forms
  runs with **no tracing session** (`Tracer.secret()` stops tracing and starts a new session
  afterwards — pausing a chunk is not enough, the network log would still carry the login POST),
  so a test that signs in has several trace parts. `globalTeardown` scans every artifact (zips
  included) for `DEMO_USER_PASSWORD` and fails the run on a hit; `trace-hygiene.spec.ts` proves
  the mechanism. Traces do contain demo session cookies, like any Playwright trace. Open with
  `npx playwright show-trace <zip>` (or trace.playwright.dev).
- `screens/*.png` — key screenshots of the golden journey (overwritten on each run):
  `01-home`, `02-thinking-expanded`, `03-approach-card`, `04-results-canvas`,
  `05-approval-connect`, `06-commit-verified`, `07-merxa-change-batch`, M3 canvas visuals
  `13-scenario-compare`, `14-network-map`, `15-impact-graph`, `16-stockout-timeline`; admin smoke:
  `08-admin-agents`, `09-admin-connectors-wizard`, `10-admin-gateway`, `11-merxa-sailings`,
  `12-merxa-api-clients`; M4: `17-approach-plan` (approach card with checks and agents),
  `18-thinking-multi-agent` (several specialists + critic), `19-whatif-promote` (what-if result,
  Apply disabled, Promote), `20-knowledge-admin`, `21-followup-what-changed`; M5 (`m5-offline.spec.ts`,
  **fixture** screenshots from `/dev/blocks` and the admin page until the live Act 3 run replaces them with a real case):
  `22-guide-checklist` (fixture), `23-four-eyes` (fixture), `24-replan-banner` (fixture), `25-rollback` (fixture),
  `26-policies-admin` (live admin page); M6 (`m6-alerts.spec.ts`, simulated typhoon pack, no model):
  `27-alerts-inbox`, `28-alert-drawer`, `29-home-alert-pill`, `30-case-from-alert` (up to the queued run);
  M7 (`m7-offline.spec.ts`, no model): `31-pending-changes`, `32-users`, `33-agent-builder`,
  `34-workflow-graph`, `35-mapping-editor`, `36-signals-admin`; M8 (`m8-basemap.spec.ts`, no model):
  `37-basemap`, `37-basemap-dark` (network map fixture on the self-hosted base map or the built-in outline).
- `report/` (HTML report), `results.json`, `test-results/` (failure screenshots, error context).

## Without model credits

Everything except the analyses runs offline:

```bash
e2e/playwright/run.sh --grep-invert "Knowledge|case page|act2-whatif|act3-thin-slice|keyboard-only"
```

(or `E2E_NO_MODEL=1 e2e/playwright/run.sh m7-offline m8-basemap dev-blocks admin-smoke auth m5-offline`: the
Knowledge upload test skips itself, it would call the embedding model)

and the M6 alerts spec with the worker held (demo reset → hold → spec → release → demo reset):

```bash
e2e/playwright/run-m6.sh          # --no-reset keeps the current demo data
```

(the Knowledge test embeds an uploaded document, the a11y case-page test starts an analysis when no case exists; the layout case-page tests skip without a case). `scripts/verify-live.sh` runs the LLM specs once credits are back.

## Notes

- Real LLM runs: an analysis takes 60–120 s. Per-step waits use `E2E_RUN_TIMEOUT_MS`
  (default 300000).
- The approach gate may auto-confirm (all facts, few matches); the golden journey then records an
  annotation instead of clicking "Looks right".
- One worker: tests share the Merxa scenario and Marco's link.
