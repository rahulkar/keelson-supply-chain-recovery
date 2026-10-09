# @keelson/viz

Render-block registry for Keelson. The engine emits block envelopes (`ACTIVITY_SNAPSHOT`
`sc.render`, `GET /api/cases/{id}`); `BlockRenderer` validates `props` per `kind@v` with the
Zod schemas from `@keelson/contracts` and renders the M1 blocks and the M3 canvas visuals
(`docs/contracts/80` §5–§6). Anything unknown, invalid or throwing renders as `RawDataCard` —
one bad block never breaks the thread.

```tsx
import { BlockRenderer, type BlockAction } from "@keelson/viz";

<BlockRenderer
  envelope={block}                    // BlockEnvelope from @keelson/contracts (toBlockEnvelope / renderBlockFromEvent)
  onAction={(a: BlockAction) => …}    // may return a Promise; the pressed button shows a spinner until it settles,
                                      // and the block raises at most one action at a time (ref guard, so a double
                                      // click never sends two decisions). Rejections are swallowed — surface them in the app.
  busy={runInFlight}                  // disables every action button
  density="inline" | "canvas"         // thread vs canvas (plan_diff shows 5 rows inline + "View all")
  viewer={{ connections: { merxa } }}  // the VIEWER's own links (ViewerConnection), see approval_card below
  loadData={(envelope, { signal }) => …} // BlockDataLoader: fetches GET /api/blocks/{envelope.id}/data
  dataKey={revision}                  // a re-emitted block fetches its payload again
/>
```

**`data_ref` payloads are resolved before rendering.** For kinds with a data schema
(`network_map@1`, `impact_graph@1`, `stockout_timeline@1`) the renderer validates `props` first,
then calls `loadData(envelope, {signal})` (a skeleton "Loading <title>" meanwhile; the request is
aborted when the block goes away or `dataKey` / `data_ref` change), validates the payload with
`parseBlockData` and passes it to the block as `data` (`BlockComponentProps<P, D>`). Failures:
`data_unavailable` (load failed → **Try again**, or no loader / no `data_ref`) and `invalid_data`
(schema issues listed, e.g. an impact graph over 300 nodes). A loader must fetch the block's own
endpoint, never the `data_ref` string as a URL. `data={payload}` skips loading (fixtures, tests).
When the same block is re-emitted (new `dataKey`), the payload already shown stays on screen until
the new one arrives (no skeleton flash; the visual keeps its view, filter and map position).
Envelope `props` and the payload are validated once per object, not on every re-render, so blocks
receive the same `props` / `data` objects and their charts, layers and layouts are not rebuilt by
unrelated parent renders (e.g. each stream event).
M1 kinds that still carry a `data_ref` keep the legacy behaviour: the payload is merged over
`props` (`mergeBlockData`) before validation.

```ts
type BlockAction =
 | { type: 'interrupt_decision'; interruptId: string; decision: string; payload?: Record<string, unknown> }
 | { type: 'case_action'; action: 'apply' | 'guide' | 'promote' | 'confirm_assumptions'; optionId: string; planVersion: number; assumptionKeys?: string[] }
 | { type: 'connect'; connector: 'merxa'; returnTo: string }
 | { type: 'open_canvas'; blockId: string }
 | { type: 'rollback'; changeSetId: string }                                        // M5 commit tracker Undo (after its confirm dialog)
 | { type: 'checklist_step'; checklistId: string; seq: number; status: 'done' | 'skipped' }; // M5 Guide mode manual tick
```

`viewer` (M5) may also carry the viewer's `sub` and `roles`: an approval card then knows whether
this person already approved (quorum) and whether *their* approval needs a Merxa link.

| Block (`kind@v`) | Component | Actions raised |
|---|---|---|
| `kpi_strip@1` | `KpiStrip` (+ `KpiTiles`) | — (chips only on non-fact values; "Preliminary · based on priors"). Columns follow the tiles' container (`@container`), labels wrap, never truncate. |
| `approach_card@1` | `ApproachCard` | `interrupt_decision` confirm / what_if / cancel; **edit** opens the typed Edit form (M4, `payload: {edits: [...], removed_checks}`) or, for M1 cards, a text box (`payload: {edits: "text"}`) |
| `recommended_option@1` | `RecommendedOption` | `case_action` apply / guide / promote / confirm_assumptions (+ `assumptionKeys`) with `optionId` + `planVersion`; disabled reasons via `aria-describedby`; what-if and unconfirmed values keep Apply off (M4) |
| `plan_diff@1` | `PlanDiff` (+ `PlanDiffTable`) | `open_canvas` ("View all N changes" inline). TanStack Table 8.21, sortable headers (`aria-sort`), **virtualised above 200 rows** (react-virtual, spacer rows, `aria-rowcount`); a before/after text with more than 4 list items (a transfer order's SKU lines) or 160 characters shows a preview + "Show all (N more)" (`aria-expanded`) |
| `approval_card@1` | `ApprovalCard` | Whether Approve is enabled follows the **viewer's** Merxa link, `viewer.connections.merxa` (`ViewerConnection`: `checking` → Approve disabled, "Checking your Merxa connection…"; `{status, applyReady}` → Approve only when `applyReady`, else **Connect Merxa** (missing) / **Reconnect Merxa** (expired, or active without `changes:apply`) → `connect` (`returnTo` = current path); `unknown` → Approve offered, the server's `412 connect_required` decides). The block's own `connect` prop is the initiator's state and is used only when no `viewer` is passed (`/dev/blocks`); `approvalConnectState(connect, viewer)` exposes the rule. approve → `payload {plan_version, change_set_hash}`; reject / revise → `payload {plan_version}` (matches `approval_response_schema`) |
| `commit_tracker@1` | `CommitTracker` | — Applying → Applied → Verified stepper (`applying`: Applying in progress · `applied`: Applying + Applied done, Verified in progress · `verified`: all done · `conflict`: Applied failed), deep link "Open in Merxa" only for allowlisted hosts |
| `narrative@1` | `Narrative` (+ `NarrativeMarkdown`) | — Streamdown 2.7 static + hardened: `skipHtml`, sanitize + rehype-harden (links only to `merxa.localhost` / `keelson.localhost`, others become text; no images), external links open in a new tab |
| `evidence_pack@1` | `EvidencePack` | — ledger grouped by source, every item with its provenance chip, filter chips |
| (fallback) | `RawDataCard` | — reason `unknown_kind` / `invalid_props` / `not_an_envelope` / `render_error`, issues list, collapsible JSON |

`blockRegistry: Record<`${string}@${number}`, ComponentType>`; `registerBlock(key, {schema, component})`
adds or replaces kinds without touching the renderer.

## M3 canvas visuals (80 §5–§6)

All four are canvas-only, loaded on demand and client-only: the registry holds `lazyBlock(…)`
wrappers (React `lazy` + a skeleton during SSR / hydration) around `canvasBlockLoaders`; a Next.js
app re-registers them with `next/dynamic(() => import("@keelson/viz/<visual>"), { ssr: false })`
(entry points `@keelson/viz/network-map`, `/impact-graph`, `/stockout-timeline`,
`/scenario-compare`, default export = block component). Heavy libraries (MapLibre, deck.gl,
React Flow, elkjs, ECharts) load only inside those chunks. Colours come from `@keelson/tokens`
hex values per `<html data-theme>`, `prefers-reduced-motion` turns off chart animation, map fades
and graph zoom transitions, and every visual has a table with the same data.

| Block | Component | What it shows |
|---|---|---|
| `scenario_compare@1` | `ScenarioCompare` | ECharts 6 Pareto scatter (axes from `props.axes`, default extra cost × lost units; ◆ recommended, dashed ring guide-only, dashed frontier through the `pareto` options) + **scorecard table** (Recommended / Guide only / Best trade-off badges, the guide-only reason, levers, cost breakdown; every scorecard measure, so both plotted axes are always in the table). |
| `network_map@1` | `NetworkMap` | react-map-gl 8.1 (`react-map-gl/maplibre`) on MapLibre 6.12 + deck.gl 9.4 `MapLibreOverlay`: `ScatterplotLayer` sites coloured by status, `PathLayer` inbound / transfer lanes (status colour when affected), `ArcLayer` sea–air / near-shore proposals, `GeoJsonLayer` footprint. OpenFreeMap **Positron** (light) / **dark** styles, attribution always expanded, zoom buttons, cooperative gestures. **Option chips** (All options / Current network / each option with its lane count; a `link` legend entry whose key is an option id names the chip and is not drawn as a swatch) filter the proposed lanes; **View as table** lists exactly the drawn sites and lanes (paged by 100). No WebGL2, or more than 5,000 sites / 5,000 lanes / 100,000 path points → tables only, with the reason. Base map unavailable → plain token background + notice. |
| `impact_graph@1` | `ImpactGraph` | React Flow 12.12 + elkjs 0.12 layered left-to-right layout (deterministic), status badge / units on delayed receipts / first stock-out on every node, status filter chips, **arrow keys** follow the chain (→ feeds, ← fed by, ↑↓ same column) with a polite live summary of the focused node. Wide graphs open at a readable zoom from the upstream edge. **View as table**: same nodes with "Fed by" / "Feeds". Over 2,000 links → table only. |
| `stockout_timeline@1` | `StockoutTimeline` | ECharts custom heat strip (rows × `props.weeks`: enough / tight / short), ring = first stock-out without action. **Option switcher** (No action + `props.options`): ◆ first stock-out with the option, dashed line = the shift (to the window's end when avoided) and a summary "N avoided, N later, N earlier, N unchanged, N new" (new = a row that only runs short with the option). The table (visually hidden while the chart shows, so screen readers always have it) adds "With <option>" and **Change** columns. |

Model helpers are pure and shared by canvas and table (`src/map/model.ts`, `src/graph/model.ts`,
`src/timeline/model.ts`, `src/compare/model.ts`). Chart tooltips escape every data string.

**Map settings.** `configureNetworkMap({ styleUrls?: {light, dark}, workerUrl? })` (defaults
`https://tiles.openfreemap.org/styles/{positron,dark}` and `/maplibre/maplibre-gl-worker.mjs`).
The app must serve maplibre-gl's `dist/maplibre-gl-worker.mjs` **and** `dist/maplibre-gl-shared.mjs`
(the module worker imports it) at that path and allow the tile origin in its CSP (`connect-src`,
`img-src`) plus `worker-src 'self'`.

**Decided cards (M2, 70 §6).** `BlockRenderer` takes `decision?: BlockDecision`
(`{status?: "decided" | "recorded" | "closed", decision?, by?: string | string[], at?}`): on
`approach_card` / `approval_card` it replaces the buttons (and the Connect Merxa call to action)
with `DecidedNote` — decision badge + "Decided by Marco Rossi · 6 Oct 2026, 14:02 UTC"; `recorded`
→ "… · waiting for the other approvers"; `closed` → "No longer waiting for a decision"
(`decidedText`, `formatDeciders` exported). **Record codes never wrap**: `NoBreakCodes` /
`splitCodes` (`CODE_PATTERN`: upper-case/digit groups joined by hyphens, e.g. `PO-BD-0006`,
`DE1-NL1`) wrap each code in `white-space: nowrap`; used for the approach card's description,
matched records, assumption values and checks, and the approval card's reason / change-set id.

**Charts.** `BarChart({data: [{label, value}], caption, unit, formatOptions?, height?})` — small
ECharts 6.1 bar chart (SVG renderer, modules loaded on demand, token colours for light/dark that
follow a later `<html data-theme>` switch, reduced motion respected); the chart is `aria-hidden` and the same numbers are in a visually
hidden table named by the caption.

## M4 (docs/contracts/90)

- **Approach card.** "Who I'll consult": the card's `agents` as an avatar stack (4 shown, then
  "+N"; decorative) plus a plain sentence naming every agent. Checks are rendered with
  `checkSentence` (M4 `{task_id, sentence}` or M1 strings). Assumptions marked `unconfirmed` get an
  "Unconfirmed" badge; a `what_if` card shows the What-if badge and a one-line notice. Taxonomy
  event types read as words (`LOG.chokepoint_closure` → "Chokepoint closure", `eventTypeLabel`).
- **Typed Edit.** A card with `editable` values or plan-task checks opens a form instead of the
  free-text box: `days` / `pct` values as whole-number fields, `ids` as removable chips plus an
  "Add" field, and one checkbox per plan-task check. Only changed values are sent:
  `interrupt_decision` `edit` with `payload: {edits: [{key, value_int} | {key, value_ids}], removed_checks: [task_id]}`.
  Rules (`approachEditResult`, mirroring contracts `approach_edit_violations` and the payload
  limits): days ≥ 0, pct ≥ −100, whole numbers; 1–500 unique ids; at least one check remains;
  at most 50 removed checks. M1 cards (no editable values, string checks) keep the text box.
- **Recommended option.** `what_if` → badge, Apply always disabled (the engine's reason, or
  "What-if case: promote it to an actual case first") and **Promote to actual** (`case_action`
  `promote`; added when the engine did not send it). `unconfirmed` values → a "Confirm before
  applying" box (label, value, source) with **Confirm** → `case_action` `confirm_assumptions`
  with `assumptionKeys` (the engine's `confirm_assumptions` action, or a default one); Apply stays
  disabled meanwhile (`optionActions`, `confirmAction`). The server enforces the same rules.
- **Narrative.** `what_changed` renders as a compact "What changed: …" line above the text
  (`WhatChanged`, plain text, also used by the app for streamed narratives).
- **Carry-overs (§10).** Impact graph metric reads **"Units on delayed receipts"** (node card
  "N units delayed", table column, node labels; `IMPACT_UNITS_LABEL`); the KPI strip keeps
  "Units at risk". Pareto scatter: options on the same point share one label (`pointGroups`):
  "B = D" from engine ids `opt-B` / `opt-D` (titles otherwise), on the recommended member; the
  tooltip says "Same result as …" and the scorecard rows show the letter. Network map draws lanes
  **as given** (`laneLegs`): a sea–air lane with waypoints = its sea leg as a path up to the hub
  (e.g. Jebel Ali) + the final flight as an arc; near-shore lanes with waypoints = their path;
  two-point proposals stay arcs.
- Fixtures appended after M3: `approach_card.m4` (Suez what-if, 5 agents, editable days + receipts),
  `recommended_option.what_if`, `narrative.what_changed`.

## Formatting

Every number goes through `formatValue(value, unit)` (en-GB, EUR, deterministic for SSR):
`eur_cents` 1240000 → "€12,400" (whole euros, half away from zero — the same as the engine's narrative `format_eur_cents`, so tiles and text agree; `{ exactCents: true }` keeps non-zero cents) · `units` 3200 → "3,200 units" ·
`days` → "3 days" · `pct_bp` 1250 → "12.5%" · `count` → "12"; `{compact: true}` → "€1.2m";
`formatDelta` → "+€1,200" / "−400 units" / "No change"; `formatDate`, `formatDateTime` (UTC);
`opLabel("transfer_order.create")` → "Create transfer order".

## Links

`isAllowedHref(href)` gates every link a block renders (narrative markdown, commit deep link):
absolute `http(s)` URLs on `merxa.localhost` / `keelson.localhost` only (exact host, no
credentials), and never into machine endpoints on those hosts (`keelson.localhost/api/*`,
`merxa.localhost/v1/*`, `/mcp*`, `/.well-known/*`) — narrative text can be influenced by ingested
documents, and some API routes have GET side effects (e.g. the Merxa connect hop). Anything else
renders as plain text.

## Fixtures (`/dev/blocks`)

`fixtures/*.json` — one valid envelope per kind (demo storyline: Meghna Trims delay, POs
PO-BD-000x, DCs UK1/NL1/DE1/PL1), M3 payloads in `<kind>.data.json` (validated against the
contracts-py models, cross-field rules included), state variants (`approval_card.connected`,
`commit_tracker.applying`, `kpi_strip.final`, `network_map.empty`, generated `network_map.large`
with 5,200 sites) and negative cases (`invalid.kpi_strip`, `unknown.network_map` = `network_map@99`,
`invalid.impact_graph.data` = 301 nodes). M3 fixtures are appended after the M1 ones.

```ts
import { blockFixtures, fixtureDataLoader, getFixture } from "@keelson/viz/fixtures";
// [{ name, description, envelope, data?, expectFallback? }] incl. a generated 250-row plan_diff.large
<BlockRenderer envelope={f.envelope} loadData={fixtureDataLoader(300)} … />   // serves `data` by block id
```

## Styles

`@import "@keelson/viz/styles.css"` registers this package and Streamdown's dist with Tailwind
(`@source`) and adds `.k-prose` narrative typography on tokens.

## Tests

`pnpm --filter @keelson/viz test`: every fixture renders (valid ones also pass the strict
generated union), invalid props for every kind → RawDataCard, unknown kind / version, render
errors contained, all actions and payloads, connect gating, virtualisation, sorting, link
allowlist + HTML/image stripping, `formatValue` units, axe on every fixture, tokens↔contracts
stage vocabulary. M3 (`tests/m3.test.tsx`, `tests/mapcanvas.test.ts`): `data_ref` resolution
(loading, retry, abort, `dataKey` refetch, invalid / over-cap / missing data, legacy merge),
lazy registry + replacement, map model (option filter, arcs vs paths, bounds, colours, size
limits), map ⇄ table equivalence with a mocked WebGL canvas, oversized / empty maps, base-map
fallback style, graph model, readable initial viewport, ELK layout + arrow-key focus in React
Flow, graph ⇄ table equivalence under a filter, ISO weeks + breach shifts, timeline option
switching, chart options (markers, shift lines, `outerBounds` instead of ECharts 6's ignored
`containLabel` — also fixed in `BarChart`), tooltip HTML escaping, scorecard markers + reasons,
axe on every visual (graph and table views).

## M5 (docs/contracts/95)

- **Approval quorum / four-eyes** (`approval_card@1` `quorum`, `rejection`, `bundle`). Progress
  line from `quorumSummary(quorum)` — "1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance" —
  plus a meter, who approved when, and "The person who started this change can't approve it" when
  `exclude_initiator`. **Reject / Revise** open a small form with an optional reason (≤ 500
  characters, `DECISION_REASON_MAX`); the decision payload carries `reason` only when one was
  typed. A rejected card shows "Rejected by Lena Vogel (Compliance) · time" and the reason, and no
  buttons. `approvalNeedsLink(props, viewer)`: only the member who would be counted as the
  executing `approver` (role required, slot still open, viewer holds it) needs the Merxa link; a
  co-approver (e.g. compliance) can approve without one; no quorum / unknown viewer roles keep the
  M2 rule. A viewer whose `sub` is in `quorum.approvals` sees "Recorded for … · waiting for the
  other approvers" instead of buttons (`viewerApproval`). A multi-system `bundle` is listed
  ("One approval covers the changes in 2 systems").
- **`remediation_checklist@1`** (`RemediationChecklist`, Guide mode): progress meter ("2 of 5 steps
  done, 1 verified in Merxa"), steps with status (To do / Done · waiting for the check / Verified +
  time / Skipped), instructions (hardened markdown) and **Open in Merxa** deep links
  (`target="_blank" rel="noopener noreferrer"`; `safeDeepLink`: the contract pattern already
  refuses `javascript:` / `data:` / `//host` / userinfo links, and the block additionally links only
  allowlisted pages — never API / MCP paths — or same-origin non-API paths, judged on the
  resolved path so dot segments such as `/./api/…` cannot reach the API). **Mark done** (pending
  steps) and **Skip** (pending or done) raise `checklist_step`; verified / skipped steps offer
  nothing. Live ticks arrive as a re-emitted block: changed steps are announced politely ("Step 2
  verified in Merxa"); when every step is verified or skipped the card shows **Guide completed**.
- **Recommended option** `remediation_mix` → "Apply 3 changes for me · Guide me through 2"
  (`mixSentence`; "Guide me through all N changes" when nothing can be applied), shown above the
  buttons and used as Apply's accessible description.
- **Commit tracker** `rolled_back` status (all steps done incl. "Rolled back", badge, "Changes
  rolled back in Merxa"), **Undo changes** from the engine's `actions` (`undoAction`; only while
  applied / verified) → confirm dialog → `rollback`; a disabled Undo shows its reason
  (`aria-describedby`); `conflicts` are listed ("These changes can't be undone: 2 records were
  changed again later in Merxa"). `STATUS_TEXT` / `DONE_STEPS` cover every status.
- **Re-plan**: `ReplanNotice` (banner "New information: … — plan updated (v2)" with an optional
  **Compare v1 and v2** button), `PlanVersionSwitcher` (v1 ↔ v2 as pressed buttons: status,
  "What changed", `comparePlanDiffs` added / changed / removed against the previous version, and
  the version's `PlanDiffTable`). After a **committed** version (`isDeltaOf`) the next version's list is a
  delta on top of the applied changes: nothing is reported as "no longer in the plan" and the line
  reads "On top of v1 (already applied): …". Guide, like Apply, is never offered on a what-if or
  with unconfirmed values (`optionActions`). The `narrative@1` block renders its `banner` and `plan_versions`.
- Fixtures appended (e2e order kept): `approval_card.four_eyes`, `approval_card.rejected`,
  `approval_card.bundle`, `recommended_option.mixed`, `remediation_checklist`,
  `remediation_checklist.completed`, `commit_tracker.undo`, `commit_tracker.conflicts`,
  `commit_tracker.rolled_back`, `narrative.replan`. Tests: `tests/m5.test.tsx` (+ axe).

## M6 (docs/contracts/100)

- **`alert_origin@1`** (`AlertOrigin`, registered inline) — "Opened from alert": the first card of a
  case opened from a Signal Hub alert. Severity chip, kind (icon + words), the alert title (may be
  a news headline: rendered as plain text only), "Case opened …" (`opened_at`: when the case was opened), sources ("GDACS · Demo Freight Daily"),
  a DEMO badge when `simulated`, and **View the alert** → `/alerts?alert=<id>` (`alertHref`; the id
  is a contract-validated UUID, so an invalid one never becomes a link — the card falls back to
  `RawDataCard`). The web app shows it at the top of the signal run's turn instead of a user message.
- **Alert vocabulary** shared with the app's Alerts inbox (`src/lib/alerts.tsx`): `SeverityBadge`
  (`SEVERITY_META`: low · medium · high · critical, toned, the word always shown), `KindIcon` /
  `kindLabel` (`KIND_META` per signal kind; unknown kinds → a generic bell, never an object-prototype
  key), `DemoBadge`, `severityRank`, `humanize`.
- Fixtures `alert_origin` (DEMO pack, two sources) and `alert_origin.feed` (live USGS) are appended
  after the M5 ones. Tests: `tests/m6.test.tsx` (contract validation, content, untrusted title as
  text, invalid id, vocabulary, axe).

## M7 (docs/contracts/110)

- **Advisory label** (`src/blocks/Advisory.tsx`): plans built on a **crawled** snapshot (tier-A
  connector: data gathered by paging the vendor's read tools, not one consistent export) carry
  `advisory: {reason: "crawled_snapshot", text, as_of_range}` on `recommended_option@1`,
  `narrative@1` and `scenario_compare@1`. `AdvisoryBadge` ("Advisory", warning outline, the
  sentence as its title) sits in the card header and `AdvisoryNote` (`role="note"`,
  `data-advisory`) says "Advisory: Data gathered by paging, not a consistent snapshot. Data read
  between 5 Oct 2026, 09:12 UTC and … ." (`ADVISORY_FALLBACK_TEXT` when the engine sent no text;
  equal range ends read as one time). Blocks without the label are unchanged.
- **Workflow graph** (`@keelson/viz/workflow-graph`, default export `WorkflowGraph`; model in
  `src/workflow/model.ts`): the engine's workflow topology (contract 50 "M4 graph") as a read-only
  React Flow 12 graph laid out top-down by elkjs (`layered`, deterministic seed; `elk.bundled.js`
  runs in the main thread, no `eval`, no worker). `buildWorkflowModel({specialists, stages})` adds
  the tenant's built-in and custom specialists between `investigate` and `verify_numbers` (disabled
  ones and switched-off stages dashed / "Off"; duplicates dropped; no specialists → a direct edge);
  `workflowNodeAriaLabel` names every node for assistive tech. Nothing can be dragged, connected,
  selected or deleted; load it with `next/dynamic(…, { ssr: false })` and always render the
  app's list view next to it (the graph is a picture of the same data).
- Fixtures appended after the M6 ones: `recommended_option.advisory`, `narrative.advisory`,
  `scenario_compare.advisory`. Tests: `tests/m7.test.tsx` (contract validation, badge + note +
  range text, fallback text, graph model: fan-out, dashed edges, toggles, every edge joins nodes).

## M8 (docs/contracts/125)

- **Whole-unit note** (`src/compare/integer.ts`, `ScorecardTable`): options planned with
  `integer_stage="cpsat"` carry `CompareOption.integer {method, gap_bp, fallback_reason}`; the
  scorecard shows a muted caption under the option's levers ("Whole units: CP-SAT, proven
  optimal" / "… best found (gap 0.4 %)" / "Whole units: rounded from the LP (CP-SAT fallback: time
  limit)", `data-integer-method`). Options without it (every `repair` plan) are unchanged.
- Fixture appended after the M7 ones: `scenario_compare.integer`. Tests: `tests/m8-cpsat.test.tsx`
  (contract validation, every caption, notes only where present, axe).
