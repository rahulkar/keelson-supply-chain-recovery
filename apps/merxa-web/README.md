# merxa-web: Merxa Retail Suite UI

Web UI of the fictional third-party ERP **Merxa Retail Suite** (by Merxa Systems) used in the
Keelson demo. It is a **separate product**: a standalone pnpm project with its own lockfile,
outside the Keelson pnpm workspace, with its own design tokens. It must never import Keelson
code: ESLint `no-restricted-imports` rejects `@keelson/*` and paths into Keelson packages, and
`test/boundary.test.ts` checks that rule.

Contracts: `docs/contracts/20-merxa.md` (merxa-web section), `docs/contracts/22-merxa-m2.md` §7
(M2 pages), `docs/contracts/65-web-internals.md` (G), `docs/contracts/10-auth.md` (Merxa UI auth),
`docs/contracts/95-m5-remediation.md` §2 (M5 Guide-mode forms).

| | |
|---|---|
| Stack | Next.js 16.3 (App Router, `output: 'standalone'`), React 19.3, TypeScript 5.9 |
| Grids | AG Grid **Community** 36.2 only (`ag-grid-community` + `ag-grid-react`, Balham theme, explicit Community modules; never Enterprise) |
| Type / icons | Source Sans 3 + JetBrains Mono (`@fontsource-variable/*`), Tabler icons |
| Port | 3001 (`PORT`), served at `http://merxa.localhost` by Caddy |
| Data | same-origin `/v1/*` (Caddy → merxa-api) with the HttpOnly `merxa_session` cookie; server components call `MERXA_API_INTERNAL_URL` (default `http://merxa-api:8100`) and forward only that cookie |

## Pages

| Route | What |
|---|---|
| `/login` | Merxa ID sign-in link (`/v1/auth/login?next=`) and the demo persona switcher (`GET /v1/auth/demo/personas`, `POST /v1/auth/demo/login-as`). `?switch=1` shows it while signed in. |
| `/change-batches` | AG Grid list of change sets (status filter, cursor paging, newest first). Rows applied in the last 10 minutes are amber. |
| `/change-batches/[id or code]` | Status, attribution banner (API `attribution_text`, e.g. "Changed by Marco Rossi via Keelson (APR-0001)" or "Applied via Keelson auto-policy P-12@3, initiated by Priya Shah"), batch details (origin, approval, preview totals and hash), per-item before/after diff table (changed fields amber, numeric deltas), conflicts, audit rows, operator guide. |
| `/purchase-orders`, `/purchase-orders/[code]` | PO grid (status / destination DC / supplier filters, ETA ISO week, totals row); PO record with lines, externally changed lines and audit history. Operator-guide deep links (`?line=&action=reschedule&eta=&version=` / `action=change-qty&qty=`) highlight the line and open its **edit form** pre-filled (reschedule ETA DC or change the ordered quantity, never below received; `PATCH /v1/purchase-orders/{po}/lines/{n}` with If-Match). |
| `/inventory` | Positions with on hand / allocated / reserved / in transit / ATP; filters: location type, DC (for stores: the stores the DC serves), location code, style, SKU list. Amber rows were last changed by an external system (with a link to the change batch). |
| `/transfer-orders` | TO grid (status / from / to filters); rows created by change batches (`created_via = change_set:<code>`) are amber with a link; lines open in a drawer (`?code=` preselects). |
| `/transfer-orders/new` | New transfer order, pre-filled by operator-guide deep links (`?from=&to=&ship_date=&lines=SKU:qty,…`): line-by-line ATP check at the source DC, then **Create transfer order** (`POST /v1/transfer-orders`; merxa-api validates and reserves stock again). |
| `/allocations/[code]` | One allocation (details, history) with an **Edit quantity** form; guide links `?action=set-qty&qty=&version=` pre-fill it (`PATCH /v1/allocations/{code}` with If-Match). |
| `/capacity-bookings/new` | **Book capacity** on a per-unit lane (lane, ISO week, units; remaining capacity shown like the Capacity tab, only when every booking could be read); guide links `?lane=&week=&qty=`. Also reachable from Sailings & Lanes › Capacity › New booking. |
| `/capacity-bookings/[code]` | One booking: lane week capacity, shipments that reference it, history and **Cancel booking** (confirm names linked shipments; `POST /v1/capacity-bookings/{code}:cancel` with If-Match); guide links `?action=cancel&version=`. Booking codes in the bookings grid link here. |
| `/locations` | DCs & Stores: distribution centres, store clusters, stores (filters: home DC, cluster, market). |
| `/supplier-portal` | Supplier messages inbox (list + reading pane). Bodies are untrusted text, rendered as plain text only. Opening an unread message from the list marks it read; the reading pane has *Mark read* / *Mark unread* (the message shown first is not marked read automatically). `PATCH` with `If-Match`, needs `changes:preview`; a 412 or 404 is explained and the inbox reloads so the next attempt sends the current version. |
| `/audit` | Audit log grid (filters: change batch, entity type, entity reference; newest 5,000 entries, newest first). |
| `/sailings` | **Sailings & Lanes** (M2), tabs: *Sailings* (lane / status filters, cut-off, ETD/ETA, TEU, link to the shipments on a sailing); *Lanes* (mode filter, carrier, transit days, via chokepoints, alternative lane, rate in the rate card's currency and remaining capacity in the current planning week); *Rate cards* (validity, basis, rate, weekly capacity); *Capacity* (lanes × 8 ISO weeks from `?week=`, default the simulation week: remaining of capacity per the rate card valid on the week's Monday minus held + confirmed per-unit bookings, as merxa-api computes it; ≥80 % booked amber, exhausted red; when the bookings cannot be fully loaded the matrix is replaced by a notice and the Lanes tab leaves *Remaining* empty, instead of showing the full rate-card capacity as available) plus the capacity-bookings grid (bookings created by change batches are amber). |
| `/shipments` | Shipments grid (status / destination DC / PO / sailing filters, units total row); lines in a drawer (`?code=` preselects) with links to the PO lines. Shipments created by change batches (e.g. sea-air splits) are amber. |
| `/production-schedule` | Factory tabs (factories with lines), window `?from=&to=` (default: merxa-api's window around the simulation's today; a lone `from` or `to` gets a 49-day span, a reversed pair is swapped and a window longer than 366 days is cut, so merxa-api never rejects it). Per factory line: a **calendar strip** (open / reduced / closed days, ISO-week header, today marked), production orders as bars on non-overlapping tracks (at most 6; further orders on a day show as "+N") linked to their PO line (`/purchase-orders/<po>?line=<n>`), held orders with their reason, and the order list. |
| `/online-orders` | Online and click & collect orders (filters: reservation = at risk / fully reserved, status, market, promised before; "Show orders at risk" shortcut). At-risk orders (open or picking with reserved < ordered, the same rule as merxa-api's `?at_risk=true`; a cancelled or shipped order with unreserved units is not at risk) have a red edge; lines with their reservations in a drawer. |
| `/promotions` | Promotions grid (active-on filter): period and ISO weeks, discount and expected uplift (basis points shown as %), markets, store grades, styles (empty = all). An unbounded period side reads *open-ended*. |
| `/api-clients` | **API & MCP Clients** (`merxa-admin`): API keys (masked `mx_<prefix>_••••`, scopes, created, last used, status); **mint** (name + scopes, `Idempotency-Key`; the key is shown once with copy, never stored client-side; a replay says it cannot be shown again); **revoke** (confirm; `POST /v1/api-keys/{id}:revoke`). Connected OAuth clients (`GET /v1/oauth-clients`: client id, e.g. `keelson-reader`, last seen, surface `/mcp`, `/mcp-lite` or REST, last principal). |
| `/webhooks` | **Webhooks** (`merxa-admin`): subscriptions (endpoint shown as text with any URL password masked; the page redacts it on the server, so it is not in the RSC payload either; events, active/paused, delivered/pending/failed counts, last delivery); create (URL pre-check, events; the `whsec_…` secret shown once), pause/resume (`PATCH` with `If-Match`; the version the PATCH returns is used at once, so Pause then Resume does not fail; actions wait for the reload; a 412 or 404 is explained and the list reloads), delete (confirm; the row disappears at once). Recent deliveries grid (newest 200, filter by status and subscription): event, attempts, HTTP code, last error, next attempt; a pending delivery that already failed shows as *Retrying*. |

The Administration nav group (API & MCP Clients, Webhooks) is shown to `merxa-admin` only;
the pages show "Not available" to other roles and merxa-api enforces the role on every call.
The simulation's current ISO week is read from `GET /v1/lane-options` (its `week` echo),
since merxa-api does not expose the demo clock directly.
| `/demo-director` | DEMO_MODE + `merxa-admin` only: inject the trim-delay email, delay a PO, **cut lane capacity** (M5 `lane_capacity`: lane, capacity %, weeks, optional carrier note; sea-air lanes listed first), advance the clock, reset the scenario (`POST /v1/admin/scenario:*` with an `Idempotency-Key`). |
| `/healthz` | Liveness JSON (does not call merxa-api). |

Global **"External changes received"** notice: polls `GET /v1/change-sets?status=applied&limit=25`
every 20 s while the tab is visible and shows every batch applied in the last 10 minutes
(attribution, time, link, "Reload data", dismiss per session).

Session gate: `src/proxy.ts` redirects page requests without `merxa_session` to
`/login?next=<path>`; server components redirect on a 401 from merxa-api. `next` targets are
restricted to same-site paths (`safeNext`).

## Guide-mode forms (M5)

Operator-guide steps (merxa-api `get_operator_guide`, shown on change batches and used by
Keelson's Guide mode) link to the forms above with the item's values in the query. Every
form saves through same-origin REST with an `Idempotency-Key` (`useIdempotentSave`): a retry
after an ambiguous failure (network error, 5xx, request in flight) reuses the key so merxa-api
replays instead of applying twice; a refused save (422/412/403) gets a fresh key next time.
Record edits send `If-Match` with the version on screen; a 412 reloads the page data so the
next attempt uses the current version. When the guide link's `version` differs from the
record's, the form warns that someone changed it since the step was planned. merxa-api emits
one webhook event per save (`transfer_order.created`, `allocation.updated`, `po_line.updated`,
`capacity_booking.created`, `capacity_booking.cancelled`) — Keelson ticks the checklist step
from it. Pure helpers live in `src/lib/manual-change.ts`; the forms in
`src/components/forms/ManualForms.tsx`. Four-eyes change batches show "co-approved by …" in the
attribution banner and a "Co-approved by" detail; compensating batches read "Rolled back by …".

## Design tokens (`src/app/globals.css`)

Spruce chrome (`--mx-chrome`), petrol primary (`--mx-primary`), amber "externally changed"
(`--mx-external*`), warm-grey background (`--mx-bg`), radii 2/4 px, rows/headers 28/32 px
(compact; comfortable = 34/38 px via the top-bar toggle, stored in the `merxa_density`
cookie so SSR matches), 13 px base text, 1 px borders, 120 ms ease-out. The AG Grid Balham
theme is parameterised with the same values in `src/components/grid/gridTheme.ts`.
Timestamps are shown in UK time (`Europe/London`) on server and client.

## Develop, test, build (in Docker; never on the host)

`node_modules` and `.next` live in named volumes. Mount **only this folder** so pnpm does
not pick up the Keelson workspace at the repository root. The folder's own `pnpm-workspace.yaml`
holds pnpm settings only (no packages): `minimumReleaseAge: 4320`, the same 3-day cooling-off for
newly published versions as Renovate (`docs/supply-chain.md`); frozen installs are unaffected.

```bash
# Git Bash
MSYS_NO_PATHCONV=1 docker run --rm --memory 2500m \
  -v D:/primark/apps/merxa-web:/work -v keelson_pnpm_store:/pnpm/store \
  -v keelson_nm_merxa_web:/work/node_modules -v keelson_merxa_web_next:/work/.next \
  -e PNPM_HOME=/pnpm -e CI=true -e NODE_OPTIONS=--max-old-space-size=2048 -w /work node:24-slim \
  sh -c "corepack enable && corepack prepare pnpm@10.34.6 --activate && pnpm config set store-dir /pnpm/store \
         && pnpm install --frozen-lockfile && pnpm typecheck && pnpm lint && pnpm test && pnpm build"
```

Image (build context = repository root; only `apps/merxa-web`'s package files, configs,
`public/`, `test/` and `src/` are copied, so local caches in the folder never enter the build):

```bash
docker build -f apps/merxa-web/Dockerfile -t keelson-merxa-web .            # runtime image (node:24-slim, uid 10001)
docker build -f apps/merxa-web/Dockerfile --target test -t merxa-web:test . # typecheck + lint + vitest inside the build
env -u LITELLM_MASTER_KEY docker compose up -d merxa-web                    # with pg-merxa, merxa-api, keycloak, caddy
```

Tests (Vitest + Testing Library, jsdom): formatting (numbers, cents, dates in UK time, ISO
weeks, relative time), attribution and "recently applied" rules, before/after diffing,
query/next-path/problem helpers and search-param parsers, the session-gate proxy, AG Grid
module registration, renders of the status badge, attribution banner, item diff table,
external-changes notice (polling, reload, dismiss), persona switcher, and the ESLint product
boundary (static, dynamic `import()` and `require` of `@keelson/*`, plus a scan of `src/` and
`package.json`). Hardening tests cover operator-guide deep links, CSV-injection escaping,
cookie forwarding, newest-first audit loading, the drawer focus trap, the account disclosure,
the sidebar's accessible names, live unread counts and Demo Director retry keys.
M2 tests (`src/lib/m2.test.ts`, `src/components/m2-pages.test.tsx`) cover ISO-week maths
(53-week years, year ends), lane capacity (rate card on the week's Monday, standard → peak,
held/confirmed per-unit bookings only, TEU lanes, open-ended validity), utilisation states (also as screen-reader text), calendar-strip placement
(clipping, track packing), PO-line links, webhook URL pre-checks and delivery states, the new
labels and tones, the simulation-week read, the admin-only nav group, API-key mint (show once,
`Idempotency-Key` reused on an ambiguous retry, replay without key), revoke, refresh sync,
OAuth-client list (own-property lookups), webhook create/pause (`If-Match`)/delete and the reload after
412/404, the production strip (day cells as labelled images) and the capacity matrix, the at-risk rule,
the schedule window and Enter on a grid cell opening the row (keyboard equivalent of the row click).
Review regressions cover rates in the rate card's currency, open-ended promotion periods (render and sort),
URL-password masking (also server-side, before the RSC payload), Pause then Resume with the version the PATCH
returned, immediate removal of a deleted subscription, the capacity notice when bookings cannot be loaded, and the
Supplier Portal *Mark read* control and its reload after a 412.
M5 tests (`src/lib/manual-change.test.ts`, `src/components/forms/manual-forms.test.tsx`,
`src/lib/attribution.test.ts`) cover quantity / version parsing, the version-changed warning,
the transfer-order body and its readiness checks, save error messages, deep-link paths for the
new forms, each form's request (path, method, body, `If-Match`, `Idempotency-Key`), key reuse
only after an ambiguous failure, the 412 reload, local refusal of invalid input (below
received, locked allocations, released bookings), the cancel confirmation, the Demo Director
lane cut and the co-approver / rollback attribution fallbacks.

## Security and robustness notes

- **CSP** (`next.config.ts`): same-origin only (`default-src 'self'`, `connect-src 'self'`,
  `frame-ancestors 'none'`, `object-src 'none'`, `base-uri 'self'`, `form-action 'self'`);
  `'unsafe-inline'` scripts/styles are needed by the App Router payload and AG Grid's
  runtime theme CSS. Plus `X-Frame-Options`, `nosniff`, `Referrer-Policy`.
- **Operator-guide links** (`deep_link`, built by merxa-api partly from integration payloads)
  are rendered as in-app relative paths only (`appPath`); non-http(s) links are dropped.
- **CSV export** prefixes text cells that start with `= + - @` TAB or CR with `'` so
  spreadsheets do not evaluate them (titles and codes come from integrations).
- **Server-side calls** forward only `merxa_session`, and only when it is a well-formed cookie
  value. The identity-provider logout URL is followed only if it is http(s).
- **Audit log** pages oldest → newest; the Audit Log page, Inventory amber marks and the PO
  history walk the listing to the end (up to 100,000 rows) so the newest entries are never
  cut off. Inventory marks a position amber only if its *latest* change was external.
- **Demo Director** retries after an ambiguous failure (network error, 5xx, in-flight 409)
  reuse the same `Idempotency-Key`, so merxa-api replays instead of applying twice.

## Configuration

| Env | Default | |
|---|---|---|
| `PORT` | `3001` | |
| `HOSTNAME` | `0.0.0.0` (image) | |
| `MERXA_API_INTERNAL_URL` | `http://merxa-api:8100` | server-side API base (no trailing slash needed) |

The browser never talks to merxa-api directly except through the same origin (`/v1/*` via
Caddy), so the app works only behind Caddy (or any proxy that routes `/v1/*` to merxa-api).
