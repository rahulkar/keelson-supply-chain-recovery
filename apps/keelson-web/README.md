# @keelson/web — Keelson web app

Next.js 16.3 (App Router, Turbopack, `output: "standalone"`, React Compiler) on React 19.3 and
Tailwind 4.3, built on the workspace packages `@keelson/tokens`, `@keelson/ui`, `@keelson/viz` and
`@keelson/contracts` (consumed as TypeScript source via `transpilePackages`). Served at
`http://keelson.localhost` behind Caddy; `/api/*` on the same origin goes straight to engine-api.

Contracts: `docs/contracts/65-web-internals.md` (W1), `30-engine-api-events.md`, `10-auth.md`, plan §4/§12.

## Screens

| Route | What it is |
|---|---|
| `/` | Greeting, the pill composer, 3 suggestion chips, a live "N new alerts" pill (M6). Sending creates a case (`POST /api/cases`), posts the message (`POST /api/cases/{id}/messages` + `Idempotency-Key`) and opens the thread. |
| `/cases/[id]` | The thread: per run, user bubble → `ThinkingTimeline` (collapsed) → narrative → inline blocks → follow-up chips on the last finished turn. Composer at the bottom (Stop while a run is running). Canvas panel (55 % ≥ 1280 px, bottom sheet below) with **Result / Evidence / Trace**. |
| `/cases` | Case history (also in the header's History drawer). |
| `/alerts` | Alerts inbox (M6): calm list + filters, detail drawer (`?alert=<id>` opens one). See "M6 — signals" below. |
| `/admin` | Back-office (`70-backoffice-m2.md` §6), `tenant-admin` / `platform-admin` only: **Overview** · **AI Gateway** (`/admin/gateway`) · **Agents** (`/admin/agents`) · **Policies** (`/admin/policies`, M5) · **Connectors** (`/admin/connectors`, `/new` wizard, `/[slug]` detail) · **Knowledge** (`/admin/knowledge`) · **Tenants** (`/admin/tenants`, platform-admin) · **Audit** (`/admin/audit`). See "Back-office" below. |
| `/dev/blocks` | DEMO only: every `@keelson/viz` fixture through the real `BlockRenderer` (actions are logged, never sent); M3 canvas visuals load their payload through `fixtureDataLoader` (300 ms) like `data_ref` blocks. |
| `/healthz` | Liveness (no engine call). |

Header: History drawer, brand (home), DEMO persona switcher (`GET /api/demo/personas`,
`POST /api/demo/login-as`, then a full reload), user menu (roles, workspace, theme, Admin, sign out).

## How it talks to the engine

- **Session gate** — `src/proxy.ts` (Next 16 `proxy`): no `keelson_session` cookie → 307 to
  `/api/auth/login?next=<path>`. Server Components read `GET /api/me` (and the case snapshot)
  directly from `ENGINE_API_INTERNAL_URL` (compose: `http://engine-api:8000`), forwarding only the
  session cookie; a 401 there (expired session) redirects to sign-in. Role gates: read roles for
  cases, write roles for the composer and block actions, admin roles for `/admin`.
- **Browser API client** — `src/lib/api/client.ts`: same-origin, `credentials: "include"`,
  `X-CSRF-Token` (from `/api/me`) on every mutation with one automatic refresh on
  `403 csrf_failed`, `Idempotency-Key` on messages and case actions (a key is reused until the
  server answers, so retries never start a second run), Zod-validated responses, `401` → sign-in.
- **Decisions & actions** — `src/lib/client/useCaseActions.ts`: approach/approval decisions →
  `POST /api/interrupts/{id}/decision` (`decision`, `plan_version`, `change_set_hash`, `edits`;
  M4 typed edits and `removed_checks` below); `412 connect_required` → the validated same-origin `connect_url`
  (`/api/connectors/<slug>/connect?return_to=<this case>`), anything else is replaced by the URL we
  build; `409` refreshes the case. "Apply changes for me" / "Guide me" → `POST /api/cases/{id}/actions`.
  Stop → `POST /api/runs/{id}/cancel` (a queued turn offers Stop once the history is replayed; the
  resulting `RUN_ERROR` names its run in `metadata.sc.runId`, so a running run is never touched).
- **M4 decisions & actions** (`docs/contracts/90`) — an approach **Edit** from the typed form sends
  `edits: [{key, value_int} | {key, value_ids}]` + `removed_checks: [task_id]` (`decisionBody`
  enforces the contract limits — keys `^[a-z][a-z0-9_]*$` ≤ 64 chars, ≤ 50 edits / removed checks,
  1–500 unique ids — and refuses a malformed list with `InvalidDecisionError`: nothing is posted, a
  toast says why, so an `edit` never goes out without the user's changes); M1 free text is still
  sent as a string.
  Case actions add `promote` and `confirm_assumptions` (`{type, option_id, plan_version,
  assumption_keys}`; the keys are part of the idempotency signature). **Follow-up chips** are sent
  as ordinary chat messages (the engine's `followup_delta` classifies them); on an option turn the
  first chip asks about the first real alternative ("Why not Rebalance between DCs?"), the second
  re-simulates with the case's own time value from its latest approach card (`followUpContext`:
  "What if the delay doubles?", "What if the duration doubles?"; generic when there is none). A follow-up's
  `narrative@1` `what_changed` is shown as a compact "What changed: …" line above the streamed
  narrative (`Turn.whatChanged`, `whatChangedOf`).
- **Viewer's Merxa link** — `src/lib/client/useConnectorLink.ts`: while an approval card the
  viewer can act on is shown, `GET /api/connectors/merxa/link` (current user) decides Approve vs
  "Connect Merxa" (`apply_ready`; re-checked when the tab becomes visible / is restored from the
  bfcache). The card's own `connect` prop is the initiator's and is overridden (`viewer` on
  `BlockRenderer`). While checking, Approve waits; if the check fails Approve is offered and the
  decision API's `412` decides. Back from the connect flow, `?connected=merxa` /
  `?connect_error=<code>` show a toast and are removed from the URL.
- **Live stream** — `src/lib/stream/useCaseStream.ts`: `EventSource('/api/cases/{id}/events?after=0')`
  replays the case then tails it. Transient drops are retried by the browser with `Last-Event-ID`;
  fatal drops (HTTP error) check the session and reconnect from `?after=<lastSeq>` with backoff.
  Every payload goes through `parseAgUiEvent`.
- **Reducer** — `src/lib/stream/reducer.ts` folds AG-UI events into stages, agents/activities
  (`SUBAGENT_*`, `TOOL_CALL_*` with `metadata.sc.label`; while a call runs, the engine's mechanical
  "Checking get inventory in Merxa" is replaced by a sentence from `src/lib/activityLabels.ts`,
  e.g. "Reading inventory positions from Merxa", until the humanised result label arrives), reasoning (live deltas appended, the
  persisted aggregate replaces them), blocks (`ACTIVITY_SNAPSHOT sc.render` by block id; a
  re-emitted block moves to the run that emitted it last), messages (`TEXT_MESSAGE_*`), interrupts
  (opened by the `RUN_FINISHED` outcome interrupt) and run status, per run, deduplicated by
  `metadata.sc.seq` (live-only events have none and are never deduplicated). Every event is
  attributed to the run in `metadata.sc.runId` (`RUN_STARTED`/`RUN_FINISHED`: their own `runId`);
  legacy events recorded without it fall back to the current run.
- **Errors** — `RUN_ERROR` shows its generic message and error ID under the turn's timeline
  (`RunErrorNotice`, with "Copy error ID"); the ID is `metadata.errorId` or the "error ID <uuid>"
  in the message. `src/lib/errors.ts` screens every `RUN_ERROR` / `SUBAGENT_ERROR` message: text
  that looks like a stack trace, exception dump, URL, path or SQL is replaced by a generic sentence.
- **Pending interrupts** — a new run does **not** close an interrupt (a follow-up question asked
  while an approval is pending leaves it pending), and the events do not say which run resumed
  what. So the case snapshot is the source of truth: `open_interrupts` as of `last_event_seq` is
  synced into the reducer (`server_interrupts`) on load and after every live `RUN_STARTED` /
  `RUN_FINISHED` / `RUN_ERROR` (the snapshot is re-read; out-of-order answers are ignored).
  Interrupts opened at or after the snapshot's seq follow the events (engine-api reads the list and
  the seq separately, so a pending one must never look closed); one this user decided stays closed for
  them.
- **Thread layout** — `src/lib/thread.ts`: one turn per run; the narrative is the streamed
  `TEXT_MESSAGE` (the identical `narrative@1` block is only a fallback, never shown twice).
- **`data_ref` blocks** — `CaseBlock` passes `BlockRenderer` a typed `BlockDataLoader`
  (`src/lib/blocks/dataLoader.ts`): it always fetches the block's **own** endpoint
  `GET /api/blocks/{id}/data` through the API client (never the `data_ref` string as a URL; the
  request is aborted when the block goes away) and `dataKey={revision}` refetches a re-emitted
  block. The renderer validates the payload per kind (`parseBlockData`) and passes it to the block
  separately from `props` (the M3 props schemas are strict, so merging the payload into props —
  the M1 behaviour, kept for M1 kinds — would fail). Loading → skeleton, failure → "Try again".
- **Canvas visuals (M3)** — `src/lib/blocks/canvasBlocks.tsx` registers `network_map@1`,
  `impact_graph@1`, `stockout_timeline@1` and `scenario_compare@1` as
  `next/dynamic(() => import("@keelson/viz/<visual>"), { ssr: false })` (MapLibre / deck.gl /
  React Flow / elkjs / ECharts stay in their own chunks and never render on the server) and points
  MapLibre at the same-origin worker. The canvas **Result** tab orders blocks
  `scenario_compare`, `network_map`, `impact_graph`, `stockout_timeline`, `plan_diff`, then the
  rest in arrival order (`RESULT_TAB_ORDER` in `src/lib/thread.ts`).
- **MapLibre worker** — `scripts/copy-maplibre-worker.mjs` (run by `dev` and `build`, also
  `pnpm maplibre:worker`; `--check` verifies) copies the installed maplibre-gl's
  `dist/maplibre-gl-worker.mjs` and the `maplibre-gl-shared.mjs` it imports into
  `public/maplibre/` (git-ignored, lint-ignored, excluded from the session proxy so the module
  Worker can always load it); `setWorkerUrl('/maplibre/maplibre-gl-worker.mjs')`.
- **CSP** — the hosted base map is the only off-origin traffic: `MAP_TILE_ORIGINS`
  (default `https://tiles.openfreemap.org`: styles, TileJSON, tiles, sprites, glyphs) in
  `connect-src` and `img-src`; `worker-src 'self'` (no blob: workers). **M8**: the origins come from
  the **build-time** `KEELSON_CSP_MAP_ORIGINS` (Dockerfile `ARG`, compose `build.args`; space-separated
  https origins, `none` = no third-party origin at all; a bad value fails the build) because
  `headers()` is compiled into the standalone server. The self-hosted base map (`/basemap/*`) is
  same-origin and needs nothing here.
- **Base map mode (M8, `docs/contracts/127-m8-basemap.md`)** — the `(app)` layout reads the
  **runtime** env `KEELSON_BASEMAP` (`auto` | `protomaps` | `openfreemap` | `offline`; anything else
  → `auto`; `src/lib/server/basemap.ts`) per request and wraps the pages in `@keelson/viz`
  `NetworkMapSettingsProvider`, so a restart (no rebuild) switches the network map's base map.
  `/basemap/*` is excluded from the session proxy; `src/app/basemap/manifest.json/route.ts`
  answers **204** ("nothing installed") when no Caddy / bucket route serves the base map (Cloud Run
  without `--enable-basemap`), so the map falls back without a 404 in the console.
- **Page states** — not-found, no-access and error pages render their `EmptyState` title as the
  page's `h1` (`headingLevel={1}`).
- **Canvas** auto-opens once per case (remembered in `localStorage`) when the first heavy result
  arrives *live* on a wide screen; on close, `CanvasPanel` returns focus to whatever opened it
  (the toggle, "View all changes", …), falling back to the canvas toggle. The case layout row is
  clipped (`overflow-hidden`), so the side panel's slide-in never scrolls the page sideways.

- **Decided cards** (70 §6 carry-over) — an approach / approval card that no longer waits for the
  viewer shows "Decided by … · time" (`@keelson/viz` `DecidedNote`) instead of dead buttons
  (`src/lib/decisions.ts`): from the snapshot's optional `decided_interrupts`
  (`[{id, decision, decided_by:[{name}], decided_at|resolved_at, status}]`, **not sent by engine-api
  yet**), else this viewer's own decision in this page (name + time; "waiting for the other
  approvers" while a quorum is incomplete), else "No longer waiting for a decision". Record codes
  (`PO-BD-0006`) in approach / approval cards never wrap inside the code.

Browser-level e2e of these screens against the real stack: `e2e/playwright/README.md`.

## Back-office (`/admin`)

Role gate in `src/app/(app)/admin/layout.tsx` (server, `/api/me`): `tenant-admin` or
`platform-admin`; `/admin/tenants` additionally `platform-admin`. The engine enforces the same
rules (and tenant scoping) on every `/api/admin/*` route; the UI only hides what would be refused.
Compact density (32 px controls, dense tables), same tokens; the main workspace is unchanged.

- **Client** — `src/lib/admin/client.ts` (`AdminApi`) over the app's `ApiClient.call` (same CSRF /
  `401` / error handling; `PUT` / `PATCH` / `DELETE` added). Response shapes in
  `src/lib/admin/types.ts` (Zod, `loose`; a bare array and `{items}` are both accepted where 70
  leaves the envelope open). `useResource` (`src/lib/admin/useAdmin.ts`) loads per key with stale
  answers ignored.
- **Agents** — role cards (model alias select from the catalog; prompt addendum `Textarea` with a
  4,000-character counter and a preview of the "Tenant guidance (data, …)" framing), tool
  allowlists per specialist and for the answer path (only approved + read + trusted-data tools can
  be ticked; untrusted / write / unreviewed / unclassified tools and the catalog's `bindable: false`
  ones — e.g. the connector isn't active — are disabled with the reason), budgets & policy (checked
  against the catalog's `platform_max`). Like the engine, only aliases a draft *changes* and tools
  it *adds* are validated, so an older binding never blocks an unrelated save; alias options a role
  can't use (not a `chat` alias; no tool-loop signatures for the investigators) are disabled.
  **Guarded fields**: for a tenant admin, raising `policy.auto_max_lines` / `auto_max_units` and
  lowering `policy.approval_quorum` show "Requires a second admin" and save as a change request
  (M7 four-eyes, see below; tightening applies right away). An emptied
  addendum / display name is "none" (no phantom unsaved change; clearing a saved addendum sends
  `null`). **Save** = review dialog
  with a JSON diff → `PUT {base_version, spec_patch}` where `spec_patch` is an RFC 7386 merge patch
  of the draft against the version being edited (`src/lib/admin/spec.ts`); `409` → "Someone saved a
  newer version" + "Load the latest version". **Versions** drawer: list, JSON diff of any version
  against the active one, Restore (confirm).
- **Connectors** — list (tier badges, status, review counts); **Add connector** wizard
  (`src/lib/admin/wizard.ts` state machine: basics → auth → *POST creates the draft; typed secrets
  are dropped from memory* → test (step log: `url_policy → reach → discover → auth → tools_list →
  tier_detect → classify`; unreached steps after a failure read "Skipped") → review tools (only
  after a passing test) → activate); detail page (tools table with classification selects saved per
  change, approve / disable, drift badge, last test log from `GET /api/admin/connectors/{slug}`
  (the list has no `last_test`), trust level, webhook endpoint, recent webhook events, delete).
  For tenant admins (M7), picking `read` for a tool stored *or suggested* as write / destructive,
  `trusted_data` for untrusted text, or approving a tool whose stored read / trusted classification
  contradicts the server's suggestion is a guarded change: the option says "needs a second admin"
  and the save becomes a change request.
  The wizard's "First party" trust level (tools auto-approved as suggested) is platform-admin only.
  Tools the server no longer offers (engine status `removed`) stay listed read-only, without
  approve / disable (the engine would answer 409 `tool_removed`). The auth step checks header names
  and secrets as the engine does (`^[A-Za-z0-9][A-Za-z0-9-]{0,63}$`, no reserved transport /
  session headers, one-line secrets of at most 4,096 characters).
- **AI Gateway** — models table (alias, mode, capability badges, health; provider model, params,
  edit / delete / add for platform admins), budget meter, RPM / TPM editor (`PATCH`; budget only
  for platform admins), Rotate key (confirm), usage per day (`@keelson/viz` `BarChart`, ECharts 6
  SVG, plus an accessible table; days come from the engine's zero-filled `daily` totals, so quiet
  days still get a bar; colours follow a theme switch) and per alias, "Open advanced console"
  (platform admins).
- **Tenants** — list + packs, "Create tenant from pack" dialog (at most 20 admin usernames). The
  result says when the tenant already existed (re-provisioned, `created: false`) and which admin
  usernames did *not* get access (Keycloak `not_found` / `failed`).
  **Agents** — when the catalog says `gateway_available: false`, saved model aliases are kept and
  the alias selects are locked with the reason.
  **Audit** — `GET /api/admin/audit?limit≤200[&before=<id>][&scope=platform]` (route not in 70;
  as implemented by the engine: `{items, next_before}`, who = `actor_username`), "Load older
  entries" pages with `before`, platform admins can switch to platform-wide rows; filter by action /
  who / text over the loaded rows, per-row JSON diff of `before` / `after`. The client never sends
  `tenant=` (on `/api/admin/*` it switches a platform admin into another tenant, audited).
  **Knowledge** (`/admin/knowledge`, `docs/contracts/90` §7, engine `routes_admin_kb.py`) —
  knowledge bases (`GET/POST /api/admin/kb`, `409 name_taken`; documents ready / total, passages),
  the selected base's documents (`GET …/{id}/documents`: title, file, status badge
  processing / ready / failed with the reason, size, passages, who / when; re-read every 3 s while
  one is processing), upload by **drag and drop** or **Choose file** (one `.md` / `.txt` / `.pdf`
  ≤ 5 MB, checked before sending; `POST …/{id}/documents` multipart `file` + optional `title`,
  sent through `ApiClient` with `form` = `FormData` so CSRF still applies and the browser sets the
  boundary; `duplicate: true` → "Already in …"; 413 / 415 / 422 / 502 / 503 as sentences), delete
  with confirmation (`DELETE …/documents/{doc}`), and the **embedding profile** (alias, dims,
  storage, version, status; provider model for platform admins only) with **Re-embed documents**
  (`POST /api/admin/kb/reembed {scope: "tenant"}`, confirm; recent jobs from `GET …/reembed` with a
  progress meter, re-read every 3 s while one runs; the button is off while a job runs) and, for
  platform admins, **New profile** (`{scope: "platform", alias, dims ≤ 4000}`).

Tests mock the API (`test/adminMock.ts`, an in-memory fake of 70 §1–§5): `src/lib/admin/*.test.ts`
(client, merge patch / guarded fields / eligibility / validation, wizard state machine and step
log), `test/admin.test.tsx` (every page incl. axe), `test/knowledge.test.tsx` (Knowledge: list, upload by chooser and drop, refusals, delete, re-embed, platform profile, axe), `test/admin-routes.test.tsx` (role gates).

## M5 — remediation depth (`docs/contracts/95` §7)

- **Four-eyes approvals** — the approval card shows quorum progress ("1 of 2 approvals: Marco
  Rossi ✓ · waiting for Compliance"), who approved when, and a rejection with its reason.
  **Reject / Revise** ask for an optional reason (≤ 500 characters; `decisionBody` sends `reason`
  only for reject / revise and refuses a longer one with `InvalidDecisionError`). A quorum role may
  decide even without case write access (compliance co-approves; the engine checks the
  interrupt's quorum roles). `viewer` carries the user's `sub` / `roles`, so a co-approver needs no
  Merxa link (only the executing `approver` does) and someone who already approved sees "Recorded
  for … · waiting for the other approvers" after a reload too.
- **Guide mode** — `remediation_checklist@1` inline: deep links open Merxa in a new tab, steps tick
  to Verified live (the engine re-emits the block on the stream), **Mark done** / **Skip** →
  `POST /api/cases/{id}/checklists/{checklist_id}/steps/{seq}` `{status}` (`ApiClient.tickChecklistStep`;
  `409 already_verified` → an informational toast). Ticks start no run, so they are never gated
  on a running turn. "Guide completed" once every step is verified or skipped.
- **Mixed plans** — the option card shows "Apply 3 changes for me · Guide me through 2".
- **Undo** — the commit tracker's **Undo changes** (confirm dialog) → `POST /api/cases/{id}/actions`
  `{type: "rollback", change_set_id}` with an `Idempotency-Key` reused until the server answers;
  `409` refreshes the case. Undo is disabled while an approval is open in the case (a requested
  rollback waits at its always-human approval with the change set still applied, so a second click
  would queue another rollback). Rolled-back trackers show the extra "Rolled back" step; conflicts
  that stop an undo are listed.
- **Re-plan** — a narrative `banner` shows in its turn ("New information: … — plan updated (v2)")
  with **Compare v1 and v2**; the case header shows "Updated · Plan v2" and "Plan versions (N)";
  a live banner raises a toast with "Compare versions". The **Plan versions** drawer
  (`PlanVersionsDialog`, data from the case's own blocks via `planVersionViews`: the narrative's
  `plan_versions`, the latest `plan_diff` per version, each version's "What changed") switches
  v1 ↔ v2 and shows what was added / changed / removed against the previous version plus the diff
  table. The case list shows **Updated** for cases changed since this browser last opened them
  (`src/lib/seen.ts`, `localStorage`; a per-viewer convenience).
- **Policies** (`/admin/policies`) — rules in evaluation order (priority, outcome, CEL condition,
  "Then: 2 different people: Approver and Compliance (not the person who started it)"), move up /
  down (swaps priorities), add / edit / delete, a CEL editor with live syntax hints (`celHints`:
  unknown fields / names, `=` vs `==`, `and` / `or`, unbalanced brackets, enum values) and a
  reference of the context fields. **Guarded-change locks**: `guardedChanges` is a port of the
  engine's `core/policies.py` guard (new auto rule, raised auto caps, auto rule moved ahead,
  refuse / approval rule removed, narrowed or weakened, an approval rule placed ahead of a refuse
  or stricter rule): for a tenant admin each affected rule shows "Requires platform admin: …" and
  Save is disabled. Save = `PUT /api/admin/policies {base_version, rules}` (`409 version_conflict`
  → "Load the latest version", `422 policy_invalid` → the server's per-rule errors). **Test bench**:
  presets + editable sample context (validated with `policyContextSchema` first) →
  `POST /api/admin/policies/test {context, rules?}` (the draft rules when unsaved); the result
  names the outcome, rule, quorum, receipt (`policy:<id>@<version>`) and the safety limits, and
  the matched rule is highlighted.
- **Demo reset** — DEMO only, `platform-admin` / `demo-presenter`: **Reset demo…** in the persona
  menu → confirm (optional "Rehearsal: keep Merxa connections" = `keep_consents`) →
  `POST /api/demo/reset` → a toast with the step log (each step's state in words + time), then home.
  `409 reset_in_progress` keeps the dialog open.

Tests: `test/m5.test.tsx` (case screen flows with a scripted stream, case-list badge, demo reset,
Policies page incl. axe), `src/lib/admin/policies.test.ts` (guard port vectors, CEL hints,
validation, context), `src/lib/client/useCaseActions.m5.test.tsx`, `src/lib/thread.m5.test.ts`.

## M6 — signals (`docs/contracts/100` §6–§8)

Engine routes the web calls (all same-origin `/api/*`, session + CSRF as everywhere):
`GET /api/alerts?status=&limit=` (`limit` 1..200; `{items, new_count?}` or a bare array), `GET /api/alerts/{id}`
(contract `AlertDetail` + optional `map`), `POST /api/alerts/{id}/open-case` (+ `Idempotency-Key`
→ `{case_id, run_id?}`), `POST /api/alerts/{id}/seen|snooze|dismiss` (body `{}`; the updated alert,
`{alert}` or 204), SSE `GET /api/alerts/events`, and (DEMO) `GET /api/demo/packs`,
`POST /api/demo/packs/{pack}/start|advance|reset` (the hub's replay state passed through:
`status`, `steps_applied`, `steps_total`, `next_step.note`). The engine has no pack *list* route yet
(`GET /api/demo/packs` → 404): the presenter dialog then asks for a scenario id (slug) instead; a
list in the web shape (`{items}`) or the hub's (`{packs: [{id, title, description, steps}]}`) is
used as soon as the engine serves one.

- **Client** — `src/lib/alerts/client.ts` (`AlertsApi` over `ApiClient.call`). Alert ids must be
  canonical UUIDs and pack ids slugs **before** they become path segments (`../../demo/reset` is
  refused without a request). Shapes in `src/lib/alerts/types.ts`: the generated contract schemas,
  unknown fields stripped, a malformed alert / signal / exposure skipped instead of failing the list;
  an optional event `kind` per alert (engine extra) feeds the kind icon.
- **Live channel** — `src/lib/alerts/channel.tsx` (`AlertChannelProvider`, mounted by `AppShell` for
  read roles): the new-alert count (`GET /api/alerts?status=new`, then kept current from the
  stream), one `EventSource('/api/alerts/events')` per page carrying AG-UI `CUSTOM` events from the
  engine's `core/alerts.py`: `sc.alert.raised {alert, renotify}` (new alert, or its severity band
  rose), `sc.alert.updated {alert}`, `sc.alert.removed {alert_id}` (row and count drop),
  `sc.alert.count {new}` (the pill), `sc.alert.case_opened {alert_id, case_id}` (row links the case);
  the alert itself as `value`, or `{alert, new_count}`, is accepted too. A **toast** ("Critical
  alert: …", **View alert**) for a raised `high`/`critical` alert that is `new`, or `opened` (the
  engine drafted its case: `alert_and_draft_case`), once per alert and severity (`sessionStorage`,
  best effort; a removed alert forgets its toasts, so a demo pack reset + replay, which reuses the
  uuid5 ids, toasts again). Real alerts toast only when touched in the last 15 min; DEMO
  (`simulated`) alerts carry the pack's SimClock time (days old) and are exempt. **View alert**
  opens the drawer in place while the inbox is on screen (it registers as the channel's opener),
  else navigates to `/alerts?alert=<id>`. Fatal drops check the session (`401` → sign-in) and
  reconnect with backoff, then re-read the count.
- **Home pill** — `NewAlertsPill`: "N new alerts" / "No new alerts" / "Alerts" (count unknown).
- **Inbox** — `AlertsInbox`: rows (kind icon, title, why, severity chip, "3 sites · 4 lanes · 2 open
  PO lines", relative time, DEMO badge, unread dot), filters Status ("Needs attention" = new / seen /
  case opened; a specific status is asked of the server) and Severity (client side); live rows.
- **Drawer** — `AlertDrawer`: severity / status / DEMO, why, exposure breakdown, a map through the
  existing `network_map@1` block (the engine's `map` when sent, else the event footprint: polygons as
  given, points / track vertices as ~25 km hexagons), the exposed nodes with how they matched, sources
  with licence ("GDACS (CC BY 4.0)"; links only for plain http(s) on a public-looking host, never a
  service name, localhost, `.local`/`.internal` or an IP literal; new tab, `noopener`), the update
  timeline. **Open case** (write roles; key reused until the server answers; then the case opens),
  **Go to the case** when one exists, Snooze, Dismiss. Opening a `new` alert marks it seen. Signal
  `raw_text` is never shown; titles / headlines are plain text everywhere.
- **Case thread** — an `alert_origin@1` block heads its run's turn (`Turn.origin`, `src/lib/thread.ts`)
  instead of a user message; it is not repeated inline or on the canvas. Until the signal run starts,
  the case snapshot's `pending_runs` (queued runs no user message names) announce it, so the case
  opened from an alert shows a waiting turn ("Getting started…", Stop) instead of an empty page.
- **Presenter** — persona menu → **Demo scenarios…** (DEMO, `platform-admin` / `demo-presenter`):
  packs with Start / Next step / Reset and their progress ("Running · step 2 of 5"), or a
  "Scenario id" field while the engine cannot list packs.

Tests: `test/m6.test.tsx` (contract fixtures `test/fixtures/m6.ts`, formatting, map block, client
path hardening + idempotency, channel parsing / toast rules / reconnect, pill, inbox filters + live
rows, drawer incl. axe, open-case retry with the same key, dismiss, read-only role, thread origin
card, presenter packs) and its "M6 review regressions" block (engine event shapes, DEMO toasts,
in-place View alert, list limit, internal source hosts, hub pack shapes, scenario-id fallback).

## M7 — complete back-office (`docs/contracts/110`)

Engine routes as implemented in `services/engine/.../api/routes_admin_m7.py` (shapes in
`src/lib/admin/*.ts`, read tolerantly; contract models from `@keelson/contracts`).

- **Four-eyes** (§1) — a guarded `PUT` / `PATCH` / `DELETE` answers `202` + an `AdminChangeRequest`;
  `guardedSchemaFor` picks the schema by status, so a `202` body that is not a valid change request
  is an invalid response, never "applied". Every guarded save (agent settings, policies, tool
  classification / approval, **tool binding** `auto` on a write tool, connector **trust level**
  first party, activation of an external-egress connector, connector / **knowledge base delete**,
  user invites or role changes that grant an admin role — kind `user_roles`, reason
  `admin_role_granted`, "Grants an admin role") then toasts "Sent for approval" with **View pending
  changes** (`/admin/changes?request=<id>`, highlighted and scrolled to). Before saving, tenant
  admins see an inline **"Requires a second admin"** note (`SecondAdminBanner` /
  `SecondAdminNote`); platform admins' changes apply directly (no note). M2–M5's "lock + blocked
  Save" for guarded fields became "Review and request approval" / "Request approval".
  **Pending changes** (`/admin/changes`, nav badge = the engine's `pending` count):
  `GET /api/admin/change-requests?status=pending&limit=200` (or no status for **All requests**),
  cards with kind, target, who / when, "Expires in N h" (72 h; an expired pending request reads
  *Expired* before the engine flips it), reasons, and the JSON-pointer diff as a table; **Approve**
  (optional note) / **Reject** (a reason is required: the engine answers `422 reason_required`
  without one; ≤ 500, trimmed) → `POST …/{id}/approve|reject {reason}`. Refusals are explained by
  code (`stale_base` = the settings moved, `not_pending` = another admin decided, `expired`,
  `platform_admin_required`). Self-approval is impossible: your own requests show only **Withdraw**
  (a reject with a reason, allowed for the requester); platform admins may co-sign any request;
  platform-wide kinds (`embedding_profile`) are approved by platform admins only (tenant admins
  may reject), and their approval reads "being applied" (status `approved`, not `applied`).
- **Users** (§2, `/admin/users`) — `GET/POST /api/admin/users`, `PATCH|DELETE /users/{id}`: list
  with roles, workspaces and status; **Invite** (name, email, username, product roles; the password
  is never shown — the one-time instructions / link from `setup` are); **Roles**; **Disable** /
  **Enable**. Ports of contracts-py `grantable_roles`, `user_change_violations` (incl. the
  cross-tenant rule: a tenant admin can't change users who also belong to other workspaces) and
  `user_change_guard_reasons`; the engine re-checks. **Tenants** (platform admins): suspend /
  reactivate (`POST /tenants/{slug}/suspend|reactivate`).
- **Agents** (§3) — sub-navigation *Settings · Custom specialists · Workflow*. The **builder**
  (`SpecialistsPage`) saves `custom_specialists` through the versioned spec (`PUT /agents/spec`,
  merge patch replaces the list): display name, id (snake_case, not a built-in / reserved name),
  planner description, role prompt (≤ 4,000, framing preview), model alias (catalog aliases; Pro
  models listed but disabled — `isProModel`, the API refuses them too), tool picker grouped by
  connector from the catalog (incl. the Signal Hub when registered: approved **read** tools only;
  write / destructive / unreviewed / inactive-connector tools disabled with the reason;
  `untrusted_text` tools allowed and badged *Quarantined*; not combined with external-egress
  tools), KB picker, `max_tool_calls` / `max_turns`, enabled. **Playground** (`PlaygroundPanel`):
  `POST /api/admin/agents/playground {specialist (draft object or saved slug), question}` streamed
  with `ApiClient.postStream` (AG-UI frames → activity list via `runningActivityLabel`; result from
  `CUSTOM sc.playground.result`, `RUN_FINISHED.result` or a plain JSON answer), usage line and
  findings; Stop aborts. `GET /api/admin/agents/playground` (`playgroundStatus`) says whether a turn
  can run: when the engine only replays recorded answers (`LLM_REPLAY_MODE=replay`) or the playground
  is switched off, *Run in playground* is disabled with that reason (`playgroundUnavailable`). The
  CodeMirror editors give their content a `tabindex` so the scroller is keyboard-reachable (axe
  `scrollable-region-focusable`, read-only editors included). **Workflow**: read-only graph (`@keelson/viz/workflow-graph`, React Flow 12
  + elkjs layered layout, loaded with `next/dynamic` `ssr: false`) of the code topology with the
  tenant's built-in + custom specialists (disabled ones dashed) and stage toggles, plus the same
  steps as an accessible list by stage.
- **Connectors** (§4) — **Capabilities** (tiers, how it reads / writes, mapping versions, mapped
  ops, *Crawled* badge + advisory note). **Crawl / data snapshot** on the detail page
  (`GET …/snapshots`, `POST …/sync`): current snapshot with consistency badge, as-of range (first
  and last page read), pages, rows per entity, page-limit truncation, an attempt in progress
  (re-read every 3 s) and a newer failure. Trust level select and a **When agents call it** binding
  column (`PUT …/tools/{name}/binding`). **Mappings** (`/admin/connectors/{slug}/mappings`): read /
  action editors (CodeMirror 6, YAML ⇄ JSON, `yaml` 2 parser with positions) validated as you type
  against `mappingReadSchema` / `mappingActionSchema`, the connector's tools (sources, lookups and
  read-backs must be approved trusted read tools) and an expression look-over (known functions,
  brackets, quotes, no `__`; never evaluated); `PUT …/mappings/{kind} {base_version, mapping}`
  (the engine stores the validated model, so comments aren't kept; `409 version_conflict` → "Load
  the latest version"; `422` lists `errors` / `problems`; saved mappings that fail today's tool
  checks are flagged from `problems`). **Test with one page** (`POST …/mappings/read/test`, the
  draft when edited) shows rows, sample rows and the first error per entity. **Dry run** (`POST
  …/mappings/action/dry-run`): rendered call ("Not executed"), idempotency, read-back query result;
  unmapped ops say they are guided. Plans built on crawled data carry the **Advisory** badge and
  note in `recommended_option@1`, `narrative@1` and `scenario_compare@1` (`@keelson/viz`).
- **Signals** (§5, `/admin/signals`) — sources (hub title, licence, last poll, failures;
  subscription switch → `PUT /signals/sources/{id} {enabled}`; cadence select (default or 1 min…24 h,
  the engine's 60…86,400 s) → `{cadence_s}` (`null` = default), the hub's effective cadence shown
  when it differs; a hub that didn't take it
  — `hub_synced: false` — is said, not treated as an error), watch regions (radius around a named
  site `{node: {type, code}, radius_km}` — codes suggested from `GET /signals/network`, typing stays
  possible — or a GeoJSON polygon from "lon, lat" lines; enable /
  remove; `PUT /signals/watch-regions {regions}` sends only the fields of each kind), alert rules
  editor (`PUT /signals/alert-rules {rules}`; defaults labelled), and — DEMO, presenter roles — the
  scenario pack launcher.
- **Audit** (§6) — **Activity** timeline (`GET /api/admin/audit/activity`: category, exact
  username, case id, action prefix, UTC day range → `since` / `until`; opaque cursor paging; case
  links; admin rows' before / after diff) above the M2 admin audit log. **Evals** (platform admins,
  `GET /api/admin/evals?limit=10`): summary tiles, cases with result, checks passed (engine counts),
  score columns, failures; unreadable result files and a missing results mount are explained.

Tests: `test/m7.test.tsx` with `test/fixtures/m7.ts` (fixtures checked against the generated
contracts): change-request rules (self-approval, expiry, 202 parsing), Pending changes (own
requests without buttons, approve / reject bodies, `409 stale_base`, all requests, nav count),
guarded binding / trust / KB delete toasts, users rules (cross-tenant, guarded admin grants) and
page (invite, setup instructions, guarded invite, disable), tenants suspend / reactivate,
specialists (Pro aliases, tool eligibility, reserved ids, builder save, playground stream,
workflow input + outline), mappings (validation positions, tool rules, expression safety, save
body, read test, 409, dry run), crawl snapshot status, signals (sources, regions, rules, demo
launcher), audit activity (filters, cursor) and evals; axe on every page. The old M2 / M5 guarded
field tests now expect the M7 behaviour.

## M8 — signal clustering (`docs/contracts/123`)

- **Why these reports are grouped** — `GroupingExplainer` in the alert drawer, between Sources and
  Updates (`src/lib/alerts/grouping.ts` builds the rows). `alertDetailResponseSchema` reads the
  optional `memberships` tolerantly: each entry is parsed with the generated
  `signalsEventMembershipSchema` (unknown fields stripped); invalid entries, entries of another event
  and entries naming a signal not in `signals` are dropped; none (an older hub) → `[]` and the section
  is hidden. One row per current report, the first report first, then by decision time: a method chip
  (Official report / Same hazard / First report / Nearby / Same place / Similar description /
  Near-duplicate / Joined the official event / Merged), evidence chips (`42 km`, place codes,
  `similarity 80 %`, `3 of 64 bits differ`), the hub's explanation sentence, and the headline as plain
  text labelled as reported text. Syndicated near-duplicates sit under their original ("Also reported
  by …", counted once).
- **Signals admin → Clustering** — `SignalsClusteringSection` (own client `ClusteringApi`,
  `src/lib/admin/signalsClustering.ts`): `GET /api/admin/signals/clustering` (backends: pgvector, the
  local profile, the remote profile or why there is none; the last 7 days' counts), the settings form
  (SimHash switch / Hamming bits 0–7 / window, vector switch / window / distance, percent fields per
  profile) with client validation mirroring the contract bounds, `PUT` with `ClusteringConfigUpdate`
  (`base_version`), a `409 stale_version` → "changed by someone else — reload", and **Run clustering
  now** (`POST …/run`). Editable for platform admins only (`hasAnyRole(me.user, ['platform-admin'])`);
  tenant admins see disabled inputs and a note. Hidden when the engine answers 404 (older engine).

Tests: `test/m8-signals.test.tsx` with `test/fixtures/m8-signals.ts` (fixtures checked against the
generated contracts): tolerant parsing, rows and chips, the drawer section with axe and a plain-text
HTML-looking headline, hidden without memberships, the admin section read-only (axe) and editable
(validation, exact PUT body, 409 message), hidden on 404.

## Develop & test (Docker only — no Node toolchain on the host)

From Git Bash at the repo root, with `node_modules` and `.next` in named volumes:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --memory 3000m \
  -v D:/primark:/work -v keelson_pnpm_store:/pnpm/store \
  -v keelson_w1_nm_root:/work/node_modules -v keelson_w1_nm_web:/work/apps/keelson-web/node_modules \
  -v keelson_w1_next:/work/apps/keelson-web/.next \
  -v keelson_w1_nm_tokens:/work/packages/tokens-keelson/node_modules -v keelson_w1_nm_ui:/work/packages/ui-keelson/node_modules \
  -v keelson_w1_nm_viz:/work/packages/viz/node_modules -v keelson_w1_nm_contracts:/work/packages/contracts-ts/node_modules \
  -e PNPM_HOME=/pnpm -e CI=1 -e NODE_OPTIONS=--max-old-space-size=2048 -w /work node:24-slim \
  sh -c "corepack enable && corepack prepare pnpm@10.34.6 --activate && pnpm install --frozen-lockfile --store-dir /pnpm/store \
         && cd apps/keelson-web && pnpm typecheck && pnpm lint && pnpm test && pnpm build"
```

Image (context = repo root; `Dockerfile.dockerignore` keeps the context to the workspace):

```bash
docker build -f apps/keelson-web/Dockerfile -t keelson-keelson-web .            # runtime (node:24-slim, uid 10001, port 3000)
docker build -f apps/keelson-web/Dockerfile --target test .                     # typecheck + lint + unit tests
env -u LITELLM_MASTER_KEY docker compose up -d --build keelson-web              # with the stack
```

Tests (Vitest 5 + Testing Library + jsdom + axe-core): reducer and thread layout against
**recorded engine events** (`test/fixtures/*.events.json`, produced by the engine's Translator
through its test harness: chat → approach → plan → apply → approval → execute → reconcile, and a
what-if case; every event carries `metadata.sc.runId` as the engine emits it, and
`withoutRunIds()` replays them as legacy events), the SSE hook (replay, live, transient/fatal
reconnect, dedupe), the API client (CSRF, idempotency, errors, connector link), redirect/URL
hardening, the proxy, case actions, the case screen (viewer-based Connect Merxa, run attribution
by `sc.runId`, error ID display, canvas focus return), error screening, home composer,
header/persona switcher, `data_ref` blocks (M1 merge, M3 payload loading from the block's own
endpoint even for a foreign `data_ref`, invalid payload, retry, refetch on a new revision), the
canvas Result order, the `next/dynamic` canvas registrations + worker URL, the CSP (tile origin,
`worker-src`), the proxy matcher (`/maplibre/*` never gated) and an axe pass (incl. `landmark-unique`).

The install runs no dependency build scripts: root `pnpm-workspace.yaml` lists `unrs-resolver`
under `ignoredBuiltDependencies` (its `napi-postinstall … check` is not needed, the prebuilt
binding is an optional dependency, and allowing it made a fresh Docker install download the binding
despite `--offline`), so `pnpm install` prints no "Ignored build scripts" warning.

## Configuration

| Env | Default | Used for |
|---|---|---|
| `ENGINE_API_INTERNAL_URL` | `http://engine-api:8000` | Server-side `/api/me` and case snapshot |
| `BRAND_ENGINE_NAME` | `Keelson` | Page titles (the UI otherwise uses `brand` from `/api/me`) |
| `KEELSON_BASEMAP` | `auto` | Network map base map, runtime: `auto` \| `protomaps` \| `openfreemap` \| `offline` (M8, `docs/contracts/127`) |
| `KEELSON_CSP_MAP_ORIGINS` (build arg) | `https://tiles.openfreemap.org` | Third-party map origins in the CSP, `none` = strict (baked in at `next build`) |
| `PORT` / `HOSTNAME` | `3000` / `0.0.0.0` | Standalone server |
