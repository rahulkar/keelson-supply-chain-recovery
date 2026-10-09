# 127 · M8 Self-hosted Protomaps base map

Plan §12 ("Self-hosted Protomaps is a stretch goal"), §19 M8 "Protomaps", workstream 7 of
`120-m8-overview.md`. Builds on `80-m3-deterministic-core.md` §6 (`network_map@1`, MapLibre 6 +
deck.gl), `60-build-guide.md` (containers only, heavy artefacts in named volumes), `65-web-internals.md`
and the hostnames of `00-overview.md`. This document **supersedes the base-map sentence of 80 §6**
("OpenFreeMap Positron style"); 80 itself is left unedited.

Goal: the network map draws its background from our own origin with **zero third-party requests**
and keeps working **with no tiles at all**, while staying a drop-in for the M3 OpenFreeMap path. No
engine, optimizer, database, contract or LLM change: the base map is static files plus a
platform-level runtime switch.

## 1. Scope (implemented)

| Item | Where |
|---|---|
| Runtime modes `auto` / `protomaps` / `openfreemap` / `offline`, fallback chain, manifest loader + strict schema, settings provider, per-kind notices, `data-map-basemap` | `packages/viz/src/map/{basemap,config,settings,useBasemap}.ts(x)`, `src/blocks/NetworkMap.tsx` |
| Protomaps style in Keelson tokens (light / dark), `pmtiles://` protocol, style per kind, zoom caps | `src/map/{protomaps,palette,pmtilesProtocol}.ts`, `src/map/MapCanvas.tsx` |
| Committed offline outline (Natural Earth 1:110m land, public domain) + its builder | `src/map/assets/ne-110m-land.json`, `src/map/offline.ts`, `scripts/basemap/build-land-fallback.mjs` |
| Token-themed MapLibre controls (attribution always expanded, opaque) | `packages/viz/src/styles.css` |
| Fetch tool: build resolution, size budget, extract + verify, pinned glyphs / sprites with integrity checks, licences, manifest last, prune, `--check` | `scripts/basemap/{fetch-basemap,lib}.mjs`, `versions.json`, `licenses/NOTICE.md`, `fetch.sh`, `fetch.ps1`, `export.sh`, `infra/basemap/Dockerfile` |
| Caddy `/basemap/*` static route; `basemap` volume (read-only on Caddy); `basemap-fetch` one-shot (profile `basemap`) | `infra/caddy/Caddyfile`, `compose.yaml` |
| Web: runtime env `KEELSON_BASEMAP` → provider in the `(app)` layout; `/basemap/*` outside the session proxy; 204 "nothing installed" manifest route; build-time CSP knob `KEELSON_CSP_MAP_ORIGINS` | `apps/keelson-web/src/lib/server/basemap.ts`, `src/app/(app)/layout.tsx`, `src/proxy.ts`, `src/app/basemap/manifest.json/route.ts`, `next.config.ts`, `Dockerfile` |
| Cloud Run: public bucket + backend bucket with Cloud CDN, URL-map rule, `KEELSON_BASEMAP` on keelson-web | `infra/cloudrun/basemap.sh`, `lb/url-map.yaml`, `services/keelson-web.yaml`, `scripts/cloudrun/deploy.sh` |
| Native (Docker-free) mode: `BASEMAP_ROOT` for Caddy, `KEELSON_BASEMAP` for keelson-web | `scripts/native/keelson-native.ps1` |
| Offline e2e (Playwright) | `e2e/playwright/tests/m8-basemap.spec.ts` |

## 2. Design

### 2.1 Modes and the fallback chain

`BasemapKind` = what is drawn: `protomaps` (self-hosted vector tiles), `openfreemap` (the hosted M3
styles), `offline` (the committed outline: inline GeoJSON, no network at all) and `plain` (token
background; needs nothing but the canvas — the last resort, e.g. when even the worker fails).

| Mode (`KEELSON_BASEMAP`) | Manifest valid | Chain (`basemapChain`) |
|---|---|---|
| `auto` (default) | yes | protomaps → openfreemap → offline → plain |
| `auto` | no | openfreemap → offline → plain |
| `protomaps` (own origin only) | yes | protomaps → offline → plain |
| `protomaps` | no | offline → plain |
| `openfreemap` | — | openfreemap → offline → plain |
| `offline` | — | offline → plain |

`NetworkMap` asks for the manifest only for `auto` / `protomaps` and only when a map is actually drawn
(WebGL2, not oversized, map view). While it resolves (≤ 3 s, same origin) the block shows its map
skeleton. A **base-map failure** is what M3 already defined (`isBaseMapFailure`): a MapLibre `error`
before `load` that is not a single tile (style, TileJSON / archive, sprite, worker). Each failure
remounts `MapCanvas` (`key = kind`) with the next kind; single missing tiles never switch.

Notices: `offline` while the mode is not `offline` → "Showing a simplified background map: the
detailed map isn't available."; `plain` → the M3 "The background map couldn't be loaded; sites and
lanes are drawn without it."; protomaps → openfreemap is silent (both are full maps). View as table,
the WebGL / oversize gates, the legend and the option chips are unchanged.

Zoom caps (`maxZoomFor`): protomaps = archive max zoom + 5 (z11 for `standard`; MapLibre over-zooms
the last level), offline = 8, hosted styles keep MapLibre's default.

### 2.2 Manifest `keelson.basemap/1`

`GET /basemap/manifest.json` (same origin), written **last** by the fetch tool. Every path is relative
to the manifest's directory; the browser builds all URLs itself.

```json
{
  "schema": "keelson.basemap/1",
  "generated_at": "2026-10-09T10:00:00.000Z",
  "profile": "standard",
  "source": { "build": "20261009", "basemap_version": "4.15.2",
              "url": "https://build.protomaps.com/20261009.pmtiles", "b3sum": "66eb…ba7b" },
  "archive": { "path": "v4/keelson-20261009-standard.pmtiles", "bytes": 45012345,
               "sha256": "e3b0…b855", "minzoom": 0, "maxzoom": 6 },
  "glyphs": "fonts/{fontstack}/{range}.pbf",
  "fonts": ["Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic", "Noto Sans Devanagari Regular v1"],
  "sprites": { "light": "sprites/v4/light", "dark": "sprites/v4/dark" },
  "assets_commit": "028c18f713baecad011301ff7a69acc39bcc2ae7",
  "licenses": ["LICENSES/NOTICE.md", "LICENSES/OFL.txt", "LICENSES/sprites-MIT.md"]
}
```

Rules (`basemapManifestSchema`, zod **strict**: unknown keys reject the whole manifest):
`schema` literal; `basemap_version` major must be **4** (the `@protomaps/basemaps` 5.x styles target
tiles v4); `source.url` only `https://build.protomaps.com/<8 digits>.pmtiles` (informational, never
fetched by the browser); paths match `^[A-Za-z0-9 ._{}@-]+(/[A-Za-z0-9 ._{}@-]+)*$` without `.` / `..`
segments (no scheme, no leading `/`, no `//host`, no `\`, no `%`), the archive ends in `.pmtiles`;
`glyphs` is the literal template; zooms 0–15 with min ≤ max. There is deliberately **no display
text** (no attribution) in the manifest: MapLibre renders attribution as HTML and the manifest and
archive are third-party data.

`loadBasemapManifest(url)`: `fetch(…, {credentials: "same-origin", redirect: "error", cache:
"no-cache", signal: AbortSignal.timeout(3000)})`; anything but a 200 with a valid manifest ≤ 64 KB
(404, **204**, HTML, invalid JSON, timeout, redirect, network error) resolves to `null`, never throws.
Cached per absolute URL for the page's lifetime (`clearBasemapManifestCache()` for tests).

Drift guard: one fixture, `packages/viz/tests/fixtures/basemap-manifest.json`, is parsed by the zod
schema (vitest) **and** deep-equals `buildManifest(sample)` in `scripts/basemap/tests` (node --test).
The schema is an infrastructure artefact read only by the browser, so it has no Pydantic / JSON
Schema mirror in the contracts packages.

### 2.3 Styles (lazy map chunk only)

- **Protomaps** (`protomapsStyle(theme, {manifest, baseUrl})`, pure): `layers("protomaps",
  keelsonFlavor(theme), {lang: "en"})` from `@protomaps/basemaps` 5.7.2 minus every layer whose id
  starts with `pois` / `address` (calm map: the overlay is the hero); `glyphs` / `sprite` / the
  source `url` (`pmtiles://<baseUrl><archive.path>`) under the manifest's directory; the source's
  `attribution` is the constant `PROTOMAPS_ATTRIBUTION` ("Protomaps © OpenStreetMap" with the
  copyright link). Fonts keep their upstream names (the styles hard-code `Noto Sans Devanagari
  Regular v1` for Devanagari place names, so a rename would not cover every label).
- **pmtiles://** (`ensurePmtilesProtocol`): one `new Protocol({metadata: false})` per page,
  `maplibre.addProtocol("pmtiles", protocol.tile)` once. The handler runs on the main thread and reads
  the archive with HTTP Range requests from our origin (`connect-src 'self'`); `metadata: false`
  means the archive's own metadata (and attribution HTML) is never read.
- **Keelson flavour** (`src/map/palette.ts`, token roles): light — land `surface`, water `surface-2`,
  landuse `bg`, roads `surface-3` / `border` / `border-strong`, boundaries + rail `border-strong`,
  country / city labels `fg-muted`, other labels `fg-subtle`, halos = land; dark — land `surface-2`,
  water `bg`, landuse `surface`, the rest as light in dark tokens. The named flavour is the base, so a
  field added by a future release still has a value — and the "every flavour colour is a token" test
  flags it. No POI / landcover palettes.
- **Offline** (`offlineStyle(theme)`): background = water colour, the inline GeoJSON as a fill (land)
  and a 0.5 px `border-strong` line; attribution "Made with Natural Earth". No glyphs, no sprite.
- **Plain**: the M3 `fallbackStyle` (unchanged).

Contrast rules (tests, culori `wcagContrast`, both themes): every label colour ≥ 4.5:1 on the colour
it sits on; land / water between 1.08 and 1.6 (calm but distinguishable: 1.11 light, 1.16 dark);
every status (`sev-*`) and lane (`chart-1…6`) colour has at least the contrast on land and on water
that it had on the M3 plain background (`surface-2`) — the base map never makes the overlay harder to
see than M3's fallback.

### 2.4 Offline outline

`packages/viz/src/map/assets/ne-110m-land.json`: Natural Earth v5.1.2 `ne_110m_land` (URL + sha256
pinned in `scripts/basemap/versions.json`), one MultiPolygon (127 polygons), coordinates rounded to 2
decimals, repeated points and degenerate rings dropped, properties dropped, minified: **76 KB**
(27 KB gzip), imported statically by `offline.ts`, i.e. only inside the lazy map chunk. Rebuild /
verify: `node scripts/basemap/build-land-fallback.mjs [--check]` (no npm dependencies; in a container).

### 2.5 Fetch tool (`basemap-fetch`)

Image `infra/basemap/Dockerfile`: go-pmtiles v1.31.2 pinned by digest
(`protomaps/go-pmtiles@sha256:06574f01…59d3`, static binary + its CA bundle) on `node:24-slim`, the
script runs as uid 10001; a `test` stage runs the node tests. Compose service `basemap-fetch`
(profile `basemap`, never started by `up`), network `edge` only, `HTTPS_PROXY=http://egress:3128` +
`NODE_USE_ENV_PROXY=1` (Node 24 fetch) — Go honours the proxy natively.

| Profile | Archive (world) | Archive size | With glyphs + sprites |
|---|---|---|---|
| `lite` | z0–5 | ~15 MB | ~29 MB |
| `standard` (default) | z0–6 | ~45 MB | ~59 MB |
| `detail` | z0–7 | ~189 MB | ~203 MB |

(sizes measured on build 20261009; a real `lite` install was 29 MB on disk.)

Steps: (1) resolve the build from `https://build-metadata.protomaps.dev/builds.json`: `latest` = the
newest tiles-v4 build; a pinned `YYYYMMDD` that aged out of the ~2-month retention falls back to latest
with a warning; a pinned build of another tiles major is refused. (2) `pmtiles extract … --dry-run`
→ estimate; abort when estimate + 14 MB of assets exceeds `BASEMAP_MAX_MB` (300) or when the volume
lacks twice the archive (old + new). `--dry-run` stops here and writes nothing. (3) Extract into a
hidden `.staging-<pid>-<ts>/`, `pmtiles verify`, `pmtiles show --header-json` (tile type `mvt`, zooms
must match the profile), sha256. (4) Assets from `protomaps/basemaps-assets` at the pinned commit:
the recursive tree (one `api.github.com` call), 4 font stacks × 256 glyph ranges, `sprites/v4/{light,
dark}{,@2x}.{json,png}`, `fonts/OFL.txt`, each downloaded from `raw.githubusercontent.com` (8 in
flight, 3 attempts) and verified against its **git blob SHA-1**; the Devanagari stack's symlinked
ranges (mode 120000) are verified as links, resolved inside `fonts/` only and **hard-linked** (no extra
space); the sprite licence (`tangrams/icons` LICENSE.md at a pinned commit, blob-verified) and the
committed `NOTICE.md` go to `LICENSES/`. (5) Install: delete `manifest.json` (browsers fall back),
swap `fonts/`, `sprites/`, `LICENSES/`, move the archive to `v4/keelson-<build>-<profile>.pmtiles`,
write `manifest.json` **last** (hidden temp file + rename). (6) Prune older archives (`--keep N`,
default 1) and the staging directory (also stale ones of crashed runs after 6 h).

Egress: only `build.protomaps.com`, `build-metadata.protomaps.dev`, `api.github.com`,
`raw.githubusercontent.com` (https, no credentials, port 443); redirects are followed by hand (≤ 3),
each target re-checked. `--check` validates an install (manifest structure, archive size + sha256,
256 ranges per font, 8 sprite files, licences) and exits 1 on any problem. Exit codes: 0 ok, 1
failure, 2 usage. Wrappers: `scripts/basemap/fetch.sh` / `fetch.ps1` (`docker compose --profile
basemap run --rm --build basemap-fetch …`), `scripts/basemap/export.sh DIR` (checks, then copies
the volume out for the Cloud Run upload).

### 2.6 Serving

**Caddy** (`http://keelson.localhost`, between `/api` and the catch-all): `handle_path /basemap/*`,
`root {$BASEMAP_ROOT:/srv/basemap}`, `file_server` with `hide .*` (no listing, staging and other
dotfiles never served, paths cleaned):

| Response | Headers |
|---|---|
| all | `X-Content-Type-Options: nosniff`, `Cross-Origin-Resource-Policy: same-origin`, no CORS headers, default `Cache-Control: public, max-age=86400` |
| `*.pmtiles` | `Cache-Control: public, max-age=31536000, immutable`; **never encoded**, Range → 206 + `Content-Range` |
| `manifest.json` | `Cache-Control: no-cache`; **204** (empty) when the file does not exist (nothing installed) |
| `*.pbf` | `Content-Type: application/x-protobuf`; gzip / zstd like JSON |

Dot segments (also percent-encoded) are cleaned before matching, so such requests leave `/basemap/*`
and are ordinary page requests for keelson-web — never a file outside the root (route test). The
volume is mounted read-only on Caddy. Native mode sets `BASEMAP_ROOT=.native/data/basemap`.

**Web app**: `/basemap/` is excluded from the session proxy; `src/app/basemap/manifest.json/route.ts`
answers **204** when no Caddy / bucket route serves the base map (Cloud Run without the bucket), so
`auto` falls back without a failed request in the browser console (keeps `dev-blocks.spec.ts`
"no console errors" green with no base map installed).

**Cloud Run** (`--enable-basemap --basemap-dir DIR`): bucket `<project>-keelson-basemap` (region,
uniform access, public access prevention off, `allUsers` `roles/storage.objectViewer` — it holds only
OSM-derived tiles, fonts and sprites), `gcloud storage rsync` of `v4/` (immutable), `fonts/`,
`sprites/`, `LICENSES/` (1 day), then `manifest.json` (`no-cache`), then a pruning rsync of `v4/`;
backend bucket `keelson-basemap-bb` (`--enable-cdn --cache-mode=USE_ORIGIN_HEADERS`, custom response
headers `nosniff` + CORP `same-origin`); URL-map rule `keelson.<domain>/basemap/*` →
`${BB}/keelson-basemap-bb` (`# BEGIN basemap` block, stripped without the flag). GCS + Cloud CDN serve
byte ranges, so the browser code and the CSP are identical. keelson-web gets `KEELSON_BASEMAP`
(`protomaps` with the flag, else `auto`; `.env` / env override, validated).

### 2.7 CSP

Self-hosting needs nothing new: `/basemap/*` is same-origin, so `connect-src 'self'` covers the
manifest, the archive's range requests, glyphs (worker) and the sprite (fetched, then decoded);
`worker-src 'self'` is unchanged. OpenFreeMap stays allowed **by default** for the `auto` fallback.
`KEELSON_CSP_MAP_ORIGINS` (**build time**, `headers()` is compiled into the standalone server;
Dockerfile `ARG`, compose `build.args`, Cloud Run build arg from env / `.env`): space-separated https
origins; unset / empty → `https://tiles.openfreemap.org`; `none` → `connect-src 'self'` and `img-src
'self' data: blob:` (strict / air-gapped). Anything that is not a plain https origin fails the build.
With `none`, the `openfreemap` mode degrades to the outline (its style request is blocked by the CSP
and counts as a base-map failure).

## 3. APIs and contracts

- **No change to `packages/contracts-py` / `contracts-ts`** (`network_map@1` props and data are
  unchanged; the engine never sees tiles). No API, block or event contract changes; contracts were not
  regenerated.
- HTTP (same origin, static): `GET /basemap/manifest.json` (200 manifest | 204 nothing installed),
  `GET /basemap/v4/<archive>.pmtiles` (Range), `/basemap/fonts/{fontstack}/{range}.pbf`,
  `/basemap/sprites/v4/{light,dark}[@2x].{json,png}`, `/basemap/LICENSES/*`.
- UI test contract: the map region `[data-map-canvas]` carries
  `data-map-basemap="protomaps|openfreemap|offline|plain"` (absent while the manifest resolves);
  `data-map-style` (theme, or `fallback` for plain) is kept.
- `@keelson/viz` exports: `BASEMAP_MODES`, `basemapChain`, `basemapManifestSchema`,
  `loadBasemapManifest`, `clearBasemapManifestCache`, `isBasemapMode`, types `BasemapMode`,
  `BasemapKind`, `BasemapManifest`, `InstalledBasemap`, `NetworkMapConfigPatch`;
  `NetworkMapSettingsProvider`, `useNetworkMapSettings`; `configureNetworkMap` accepts
  `basemap: {mode?, manifestUrl?}` (merged like `styleUrls`). The style builders stay internal to the
  lazy chunk.

## 4. Data and migrations

None: no database state, no RLS tables, no migration numbers used. The `basemap` named volume
(label `com.keelson.cache=basemap`, re-creatable) holds `manifest.json`, `v4/*.pmtiles`,
`fonts/<stack>/<range>.pbf`, `sprites/v4/*`, `LICENSES/*`; never on D: and never committed. The only
committed map data is the 76 KB outline.

## 5. Configuration

| Name | Where | Default | Meaning |
|---|---|---|---|
| `KEELSON_BASEMAP` | keelson-web runtime (compose env, Cloud Run env, native) | `auto` | `auto` \| `protomaps` \| `openfreemap` \| `offline`; anything else → `auto`; restart to switch |
| `KEELSON_CSP_MAP_ORIGINS` | keelson-web **build** arg | `https://tiles.openfreemap.org` | third-party map origins in the CSP, or `none` |
| `BASEMAP_PROFILE` | basemap-fetch | `standard` | `lite` \| `standard` \| `detail` (`--profile`) |
| `BASEMAP_BUILD` | basemap-fetch | `latest` | Protomaps daily build `YYYYMMDD` (`--build`) |
| `BASEMAP_MAX_MB` | basemap-fetch | `300` | hard size cap incl. assets (`--max-mb`) |
| `BASEMAP_DIR` | basemap-fetch | `/srv/basemap` | target directory (`--out`) |
| `BASEMAP_ROOT` | Caddy | `/srv/basemap` | served directory (native: `.native/data/basemap`) |
| `--enable-basemap`, `--basemap-dir DIR` | `scripts/cloudrun/deploy.sh` | off | bucket + backend bucket + URL-map rule |

`.env.example` has a commented "base map" section (none of these are secrets; `init_secrets.py` is
unchanged).

## 6. Security

- Untrusted inputs are the third-party archive, glyphs, sprites and the manifest. Attribution
  strings are code constants per kind; the pmtiles protocol never reads archive metadata; the
  manifest is zod-strict with path allowlists, carries no text, and every URL is built in code under
  the manifest's own directory; the manifest fetch refuses redirects and sends no credentials beyond
  same-origin. Tile label text is drawn by MapLibre to canvas (no HTML).
- Egress / SSRF: the fetch runs only on explicit operator action, non-root, on the `edge` network,
  through Squid, with a 4-host allowlist and re-checked redirects. Integrity: git-blob-SHA-1 of
  every asset at pinned commits, `pmtiles verify` + header checks of the archive, sha256 in the
  manifest (re-verified by `--check`), pinned sha256 of the Natural Earth source.
- Serving: read-only volume, no listing, dotfiles hidden, path cleaning, `nosniff`, CORP
  `same-origin`, no CORS. The Cloud Run bucket is public read on purpose and holds only public map
  data.
- No tenant data, so no RLS; a platform ops setting via env, not a guarded change (no four-eyes).
  No secrets, nothing sensitive logged. No LLM anywhere.
- Privacy win: with the self-hosted map, no third-party tile request reveals which regions a tenant
  is looking at.

## 7. Accessibility and licences

The MapLibre attribution stays expanded (`compact: false`), keyboard reachable, opaque `surface` with
`fg-muted` text and underlined links (≥ 4.5:1 in both themes); zoom buttons use surface / border
tokens, the icons are inverted in dark, focus rings use `--k-focus`. The base map is decorative; the
data stays in "View as table" and the region's `aria-label`. Reduced motion keeps `fadeDuration` 0.
Licences: map data © OpenStreetMap contributors (ODbL; credit "Protomaps © OpenStreetMap" whenever the
tiles are drawn), Noto Sans glyphs (SIL OFL 1.1), sprites derived from tangrams/icons (MIT), outline
Natural Earth (public domain); the volume carries `LICENSES/NOTICE.md`, `OFL.txt`, `sprites-MIT.md`.
Software: `pmtiles` 4.5.0 and `@protomaps/basemaps` 5.7.2 (BSD-3-Clause), go-pmtiles (BSD-3-Clause).

## 8. Tests

| Suite | Command | Result |
|---|---|---|
| viz (vitest) | `m8-js.sh … 'pnpm --filter @keelson/viz typecheck && pnpm --filter @keelson/viz test'` | 13 files, 324 tests passed (new: `basemap`, `protomaps-style`, `networkmap-basemap`, extended `mapcanvas`) |
| web (vitest + typecheck + lint) | `m8-js.sh … 'pnpm --filter @keelson/web typecheck && … lint && … test'` | 35 files, 492 tests passed (new: `lib/server/basemap`, manifest route; extended `next-config`, `proxy`) |
| fetch tool (node --test) | `docker run … m8-node node --test "scripts/basemap/tests/*.test.mjs"` and `docker build -f infra/basemap/Dockerfile --target test .` | 34 tests passed (both) |
| Caddy routes | `scripts/tests/caddy/run.sh` | all passed (new: byte ranges, headers, hidden / traversal, 204 when not installed) |
| Cloud Run | `scripts/cloudrun/tests/run.sh` parts shellcheck + pytest | shellcheck clean; pytest 82 passed, 3 failed — the same 3 fail on the M8 base commit (see Known issues) |
| e2e | `e2e/playwright/tests/m8-basemap.spec.ts` | type-checked (`tsc --noEmit`); run by the integrator on the full stack |

Manual checks done while building: a real `--profile lite` fetch into a scratch volume (29 MB,
`--check` ok), and the generated light / dark Protomaps styles plus the outline rendered in Chromium
from that volume behind Caddy (pmtiles range reads, Noto glyphs, sprites, no MapLibre errors).

## 9. Deviations

From **120**: none in allocations (service `basemap-fetch`, profile and volume `basemap`, Caddy route,
env prefixes `KEELSON_BASEMAP` / `BASEMAP_*` / `KEELSON_CSP_MAP_ORIGINS`, e2e file name as allocated).
The root `README.md` licences line ("Map tiles © OpenFreeMap …") is **not** edited (120 §3.7); the
integrator should add: "Self-hosted base map: Protomaps basemap tiles (data © OpenStreetMap
contributors, ODbL), Noto Sans glyphs (SIL OFL 1.1), sprites from tangrams/icons (MIT); offline
outline: Natural Earth (public domain)".

From the **design brief** (simplifications and corrections):
1. **Uniform world profiles** (`lite` z0–5, `standard` z0–6, `detail` z0–7) instead of a world archive
   merged with z7/z8 "sourcing regions": a sparse archive leaves blank areas whenever MapLibre has no
   cached parent to over-zoom (e.g. a deep link outside the regions), which the brief listed as a
   risk. Consequently no `regions.geojson`, no `pmtiles merge`, no `errorOnMissingTile` (pmtiles
   itself calls it "not recommended") and no `world_maxzoom` / `detail_regions` manifest fields.
2. Fonts keep their **upstream names** and a fourth stack, `Noto Sans Devanagari Regular v1`, is
   fetched: the Protomaps styles hard-code it for Devanagari names (Indian sourcing), so renaming to
   `NotoSans-*` could not cover every label. Its symlinked ranges are hard-linked.
3. Outline budget **≤ 80 KB** (76 KB at 2 decimals; 1 decimal saves only 11 KB) instead of 64 KB.
4. "Nothing installed" is a **204**, from Caddy (`not file`) and from a keelson-web route, instead of
   a 404, so the fallback never logs a failed request (the M3 `dev-blocks` e2e asserts no console
   errors).
5. Manifest URLs are resolved against the **manifest's directory** (`basemapBaseUrl`), not a fixed
   `${origin}/basemap/`; `assets_commit` + `LICENSES/{NOTICE.md,OFL.txt,sprites-MIT.md}`.
6. The overlay contrast rule compares against the M3 plain background (`surface-2`), the
   background the overlay was designed on, rather than `surface` − 0.2.
7. The spec doc number is **127** (overview allocation), not 120.
8. `KEELSON_BASEMAP` is a commented line in `.env.example` (compose defaults it), so `init_secrets.py`
   and the Cloud Run secret-wiring test stay untouched.

## 10. Deferred

- Self-hosted RTL text plugin (`setRTLTextPlugin` from a same-origin copy) for Arabic / Hebrew
  place-name shaping — today such local names render unshaped next to the English name.
- Admin "Base map" status card (installed build / profile / size, current mode).
- A Cloud Run Job that fetches straight into the bucket (gcsfuse) instead of export + upload;
  scheduled refresh / `pmtiles` sync-style diff updates.
- Dropping OpenFreeMap from the default CSP and the `auto` chain once Protomaps is the proven default;
  MapLibre Tiles (MLT); terrain / bathymetry layers.
- Region-detail archives (see deviation 1) if a deployment needs z8+ detail in sourcing regions
  without the size of a z8 world extract.

## 11. Known issues

- `scripts/cloudrun/tests`: `test_compose_env_secrets_are_wired` (`SIGNALS_DATA_KEY`),
  `test_engine_env_identical` and `test_engine_overlay_bakes_compose_mounts` (`EVALS_RESULTS_DIR`,
  `/app/evals/results`) fail on the M8 base commit already (M6 / M7 compose additions not mirrored in
  `infra/cloudrun`); not caused by this workstream and not in 120 §5.
- Protomaps daily builds are kept for about two months: a pinned `BASEMAP_BUILD` ages out (the tool
  falls back to latest with a warning). A future tiles v5 needs a coordinated bump of
  `@protomaps/basemaps`, `pmtiles` and the pinned style hashes in `tests/protomaps-style.test.ts`;
  until then v5 builds are refused (tool) and v5 manifests rejected (browser, map falls back).
- Renovate (workstream 9) should add custom managers for the go-pmtiles digest in
  `infra/basemap/Dockerfile` and the commits in `scripts/basemap/versions.json`, and group
  `@protomaps/basemaps` with `pmtiles` (their bumps change the pinned style hashes on purpose). The
  go-pmtiles image is not cosign-signed (pinned by digest only).
- The lazy map chunk grows by `@protomaps/basemaps`, `pmtiles` + `fflate` and the outline (~27 KB
  gzip); not measured with `next build` here (memory rule).
- A GCP org policy enforcing public access prevention blocks the public bucket; leave
  `--enable-basemap` off there (`auto` falls back to OpenFreeMap) — a small Caddy-based Cloud Run
  service behind the same path is the alternative.
- Deleting the `basemap` volume (e.g. a Docker volume clean-up) silently returns the map to
  OpenFreeMap / the outline until `scripts/basemap/fetch.sh` is run again (~1–3 min).

## 12. Integration notes

- Shared files touched (additive): `compose.yaml` (volume, Caddy mount, keelson-web build arg + env,
  `basemap-fetch` service), `.env.example` (commented section), `infra/caddy/Caddyfile` (one
  `handle_path` block), `pnpm-lock.yaml` + `packages/viz/package.json` (3 packages: `pmtiles`,
  `@protomaps/basemaps`, `fflate`; culori already locked — the lockfile edit is additive only),
  `packages/viz/src/index.ts`, `packages/viz/src/styles.css`, `apps/keelson-web/{next.config.ts,
  Dockerfile, src/proxy.ts, src/app/(app)/layout.tsx}`, `scripts/cloudrun/deploy.sh`,
  `infra/cloudrun/{lb/url-map.yaml, services/keelson-web.yaml}`, `scripts/cloudrun/tests/
  test_cloudrun.py`, `scripts/tests/caddy/{run.sh, check_routes.py}`, `scripts/native/keelson-native.ps1`,
  `docs/{infra.md, cloud-run.md}`, `e2e/playwright/{README.md, tests/act3-thin-slice.spec.ts}`.
- If another workstream also re-locks pnpm, re-run `M8_JS_INSTALL=update` after merging rather than
  hand-merging `pnpm-lock.yaml`.
- To cover the Protomaps branch of the e2e: `docker compose --profile basemap run --rm basemap-fetch
  --profile lite` before `e2e/playwright/run.sh m8-basemap` (without it the outline branch runs).
