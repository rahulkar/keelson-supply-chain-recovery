# 124 · M8 — Signals: more feeds (NHC, Copernicus EMS, OFAC, GDELT, MeteoAlarm)

Workstream 4 of `120-m8-overview.md` (branch `m8/signals-feeds`). Builds on 100 (Signal Hub v1) and
110 §5 (Signals admin). Five public sources join GDACS / USGS / RSS behind one **source registry**,
with tenant **opt-in** for the noisy or terms-restricted ones, a hardened parsing layer (defusedxml,
bounded zip/byte limits, host-restricted follow-up downloads), organisation-name matching that never
puts tenant data into global rows, a Sources admin page that shows what each source is, and an NHC
demo pack. **No LLM anywhere in this area**: every parser is deterministic (GDELT's CAMEO coding is
GDELT's own machine coding, labelled `inference`), and every test runs offline on recorded-shape
fixtures; live checks are opt-in.

## 1. Scope

| item | status |
|---|---|
| Source registry `signal_hub/sources.py` (catalogue, cadence limits, opt-in, poller adapters, feed dispatch) | done |
| Shared infra: `safezip`, `Fetcher.get(max_bytes=)`, `names.match_key`, `SignalDraft.match_keys` (hash-stable), withdrawal of snapshot items, geo point/ring helpers | done |
| Migration `0005_m8_more_feeds.sql` (`signal.match_keys`, `network_node.name_key`, `source_snapshot`) | done |
| NHC adapter (`CurrentStorms.json` + cone/track KMZ) | done |
| Copernicus EMS rapid-mapping adapter | done (no RSS fallback, §9) |
| OFAC SDN delta adapter + organisation-name exposure | done |
| GDELT 2.0 15-minute export adapter (tenant opt-in) | done |
| MeteoAlarm adapter (deployment + tenant opt-in) | done |
| Opt-in subscription semantics + catalogue metadata in `/v1/sources`, `/v1/tenants/{t}/sources` | done |
| Contracts: `PackAdapter` += nhc, copernicus_ems, meteoalarm, gdelt; `SUBTYPE_TAXONOMY` += 3 | done |
| Engine `signals_admin.get_sources` passthrough (+ opt-in default in `put_source`) and web Sources table | done |
| Demo pack `data/scenarios/hurricane-cape-route` (+ generator, README, `expected.yaml`, `validate.py` support) | done |
| Test pack `feeds-test` (nhc, copernicus_ems, meteoalarm, gdelt steps) | done (`tests/fixtures/scenarios-m8`, §9) |
| Config, compose, `.env.example`, Cloud Run jobs, native launcher, READMEs | done |
| Offline e2e `e2e/api_m8_feeds_offline.py`; opt-in live checks (`tests/test_live_feeds.py`, `verify-live.sh m8-feeds`) | done (e2e written + compiled; run by the integrator) |
| Items marked deferred in the brief | deferred (§10) |

## 2. Design

### 2.1 Source registry (`signal_hub/sources.py`)
`AdapterInfo` holds the static facts of an adapter: catalogue `kind` (`hazard`/`weather`/`regulatory`/
`news`, a grouping for the admin page, not a `SignalKind`), `provenance` (`fact`/`inference`),
`license`, `licence_class` (`public_domain`/`open_attribution`/`terms_apply`/`publisher_copyright`),
`attribution`, `terms_url`, `cadence_limits`, `opt_in`, `coverage`, `description`; `authority_bp`
comes from `scoring.AUTHORITY`. `catalogue(settings)` returns one `SourceSpec` per configured
connector in the order gdacs, usgs, rss.*, nhc, copernicus_ems, ofac, gdelt, meteoalarm.* (URL,
default cadence, name). Everything else derives from it: `admin.source_catalogue` / `SourceInfo` /
`CADENCE_LIMITS`, `FeedRunner.connectors()` and `poll()` (a dispatch per spec: gdacs/usgs/rss in
`feeds.py`, the five M8 pollers in `feeds_more.POLLERS`), `poller.ADAPTERS` (registry + `extraction`,
`sweep`), `MULTI_CONNECTOR = {rss, meteoalarm}` (the poll loop passes the connector id) and
`OPT_IN_ADAPTERS = {gdelt, meteoalarm}`.

| adapter | kind | provenance | licence (class) | authority | cadence default / limits | opt-in |
|---|---|---|---|---|---|---|
| gdacs | hazard | fact | CC BY 4.0 (open_attribution) | 9500 | 600 / 300–86400 | no |
| usgs | hazard | fact | Public domain (USGS) | 9500 | 300 / 120–86400 | no |
| rss.* | news | inference | headline + snippet + link (publisher_copyright) | 7000 | 900 / 600–86400 | no |
| **nhc** | hazard | fact | Public domain (NOAA/NWS) | 9500 | 900 / 300–86400 | no |
| **copernicus_ems** | hazard | fact | © European Union, Copernicus EMS (open_attribution) | 9000 | 1800 / 900–86400 | no |
| **ofac** | regulatory | fact | Public domain (U.S. Treasury OFAC) | 9500 | 3600 / 1800–86400 | no |
| **gdelt** | news | inference (`gdelt-cameo`) | GDELT Project, citation required (open_attribution) | 4000 | 900 / 900–86400 | **yes** |
| **meteoalarm.<country>** | weather | fact | MeteoAlarm / EUMETNET, CC BY 4.0-equivalent, redistribution terms apply (terms_apply) | 9000 | 900 / 600–86400 | **yes** (and off by default in `SIGNAL_ADAPTERS`) |

### 2.2 Two gates and opt-in
1. **Deployment allow-list** `SIGNAL_ADAPTERS` (operator): default `gdacs,usgs,rss,nhc,copernicus_ems,ofac,gdelt`.
   Nothing outside it is polled by the loop; the catalogue reports it as `polled`. MeteoAlarm is left
   out until an operator has reviewed its redistribution terms.
2. **Tenant subscription** (`source_subscription`, 110 §5). For an **opt-in** source no row means
   *not* subscribed: `recompute_schedule`/`load_schedule` count only explicit `enabled` rows (a source
   is paused until at least one tenant enabled it, a tenant registered later counts as not
   subscribed); `list_sources` reports `enabled: false` by default; `put_subscription_set` (the
   engine's whole-map sync) puts an unlisted opt-in source back to off (and, as before, an unlisted
   non-opt-in one back to on); `event_subscribed` counts a member of an opt-in source for a tenant only
   with an `enabled` row (the tenant's own and demo signals always count). Opt-in is a static property
   of the adapter (`split_part(connector_id, '.', 1) = ANY(OPT_IN_ADAPTERS)`), so the pipeline needs
   no configuration for it. Subscribing cannot open egress the operator did not allow, so it is not a
   four-eyes change (110 §1).

### 2.3 Shared infrastructure
* `safezip.read_member(body, suffixes, max_uncompressed=8 MiB, max_ratio=200, max_members=64)`:
  stdlib zipfile; refuses non-zips, too many members, encrypted members, absolute / `..` / backslash
  names, a declared size over the cap, a declared ratio over 200, an *actual* inflated size over the
  cap (counted while streaming), corrupt data, and zero or more than one matching member.
* `Fetcher.get(..., max_bytes=None)`: a per-call cap (OFAC up to `OFAC_MAX_BYTES` = 32 MiB, GDELT
  exports 32 MiB); everything else keeps 12 MiB. Redirects stay destination-guarded on every hop.
* `names.match_key(name)`: NFKD → ASCII → casefold; `&` → `and`; periods and apostrophes dropped
  (`S.A.` → `sa`); other punctuation → space; a leading `the` and trailing legal forms (`ltd`,
  `limited`, `co`, `company`, `corp`, `inc`, `llc`, `plc`, `gmbh`, `ag`, `sa`, `sarl`, `srl`, `spa`,
  `bv`, `nv`, `jsc`, `ooo`, `fze`, `pte`, `pvt`, `sdn`, `bhd`, `kk`, `ab`, `oy`, …, and the `and` of
  `& Co.`) dropped; `None` unless ≥ 2 tokens and ≥ 6 characters remain. Exact match only, never fuzzy.
* `SignalDraft.match_keys`: added to `content_hash` **only when non-empty**, so every GDACS / USGS /
  RSS / connector signal keeps a byte-identical hash (golden-hash test).
* `feeds_more.withdraw_missing(runner, connector_id, keep, now)`: every current, unexpired global
  signal of a snapshot feed (NHC, MeteoAlarm) whose native key left the feed is re-ingested as a new
  version with `expires_at = now`; its event resolves when all members expired, and its alerts with
  it. A malformed feed raises before anything is withdrawn.
* `geo.haversine_km`, `point_in_ring`, `distance_to_ring_km`, `circle_polygon` (pure Python).
* `adapters/kml.py`: KML/KMZ geometry reader (any KML namespace — NHC still publishes cones as
  `earth.google.com/kml/2.1`, tracks as `opengis.net/kml/2.2`), defusedxml, ≤ 200 geometries and
  50 000 coordinates per document, `geo.clean_geometry` on each.

### 2.4 Clustering (`cluster.py`)
* `same_hazard_window(subtype)`: 72 h for storm subtypes (incl. `wind_storm`), `flood`, `wildfire`;
  6 h otherwise. An NHC advisory (dated from its advisory count) joins the GDACS cyclone (dated from
  genesis); an EMS flood joins the GDACS flood. Distance (≤ 100 km) and different connector unchanged.
* `radius_km`: explicit `_RADIUS` entries win over the storm rule; new entries `wind_storm`,
  `coastal_flood`, `heavy_rain`, `snow_ice`, `heat`, `cold`, `wildfire` 25 km; `thunderstorm`,
  `landslide` 10 km; `volcano` 100 km. Existing results are unchanged (no existing entry is a storm).

### 2.5 Exposure by organisation name (`exposure.py`)
In the tenant's RLS context: `signal s JOIN network_node n ON n.tenant = $tenant AND n.name_key =
ANY(s.match_keys)` for the event's current members with non-empty `match_keys`, `n.type IN
('supplier','factory')` → `entity_match`, weight 10000. `name_key` is computed in Python by
`Pipeline.put_network` (`names.match_key(node.name)`). Supplier codes never enter a global row.

## 3. Sources

### 3.1 NHC (`adapters/nhc.py`, connector `nhc`)
`NHC_URL` = `https://www.nhc.noaa.gov/CurrentStorms.json` (conditional GET). Shape checked against the
live feed 2026-10-08 (`activeStorms[]`: `id` like `al092026`, `name`, `classification`, string
`intensity` kt / `pressure` mb, numeric + text position, `movementDir/Speed`, `lastUpdate`, product
objects `{advNum, issuance, fileUpdateTime, url|kmzFile, zipFile}`, some `null`). One signal per storm:
`native_key` = storm id, `native_id` = `<id>:<advNum>` (each advisory a new version);
`natural_hazard/tropical_cyclone` (`NAT.storm`, 300 km); authoritative. Severity
`nhc_severity_bp(kt)`: < 34 → 3000, 34–49 → 5000, 50–63 → 5500, H1 64–82 → 6500, H2 83–95 → 7500,
H3 96–112 → 8500, H4 113–136 → 9200, H5 ≥ 137 → 9700. `starts_at` = issuance − 6 h × (advisory number
− 1) (`012A` → 12), `observed_at` = issuance (capped at retrieval), `expires_at` = issuance + 12 h,
`ends_at` null; no `location_refs`. Title `"<Hurricane|Tropical Storm|…> <Name> (NHC advisory N)"`,
the name sanitised to `[A-Za-z' -]{1,40}` (else "Unnamed"); the summary is numbers and labels only.
**GIS**: `trackCone.kmzFile` + `forecastTrack.kmzFile`, only `https` on `www.nhc.noaa.gov` (port 443,
no userinfo, `.kmz`/`.kml` path), at most `NHC_MAX_GIS_FETCHES` (6) downloads per poll and once per
advisory (cursor `{advisories: {storm_id: advNum}}`, recorded only when every product downloaded); an
unchanged advisory reuses the stored footprint. The footprint = cone polygon + track line (the track's
forecast points are dropped) + current position. Storms that leave the feed are withdrawn.

### 3.2 Copernicus EMS (`adapters/copernicus_ems.py`, connector `copernicus_ems`)
`COPERNICUS_EMS_URL` = `https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api/public-activations-info/?limit=50`
(checked live: `{count, next, previous, results[]}`; `code`, `name`, `category`, `countries` as
names, naive-UTC `eventTime`/`activationTime`/`lastUpdate`, `closed`, WKT `centroid`, `gdacsId`,
`n_aois`, `n_products`; no ETag). Categories: Flood → natural_hazard/flood 6500; Storm (Wind) →
weather/wind_storm 6500; Earthquake → natural_hazard/earthquake 7000; Wildfire →
natural_hazard/wildfire 5000; Volcanic activity → natural_hazard/volcano 6000; Mass movement →
natural_hazard/landslide 5000; anything else (Industrial/Transport accident, Other) skipped and
counted. `native_key` = code; centroid point (also GeoJSON / `{lat, lon}` accepted; bad WKT → none);
`ISO3166` refs from country names (or iso3 objects); `starts_at` = min(event, activation); open:
`expires_at` = activation + `COPERNICUS_EMS_TTL_DAYS` (14); closed: `ends_at` = `expires_at` = last
update (feed data only, so a re-poll never makes a new version). The list is the activation history:
an activation already expired at the poll is ingested only when the hub holds a current signal for it
(so a closure resolves the event) — years of closed activations never become resolved events.
`source_url` = `https://mapping.emergency.copernicus.eu/activations/<code>/` (none in demo replays).
Authoritative (anchor).

### 3.3 OFAC SDN (`adapters/ofac.py`, connector `ofac`)
`OFAC_SDN_URL` / `OFAC_ALT_URL` / `OFAC_ADD_URL` = the Sanctions List Service exports (checked live:
302 to a presigned S3 object carrying ETag = content MD5 and Last-Modified; quoted CSV, no header,
`-0- ` nulls with trailing blanks, CRLF, trailing `0x1A`; UTF-8 with cp1252 fallback; parsed with
`csv` in strict mode, fields > 20 000 characters or rows > 200 000 refused). **Polling**: each file is
requested with its own stored validators (cursor `files`); when any of the three changed, the
unchanged ones are re-read in full (the delta needs all three). Organisations only (`SDN_Type` null;
individuals, vessels and aircraft skipped); aliases `aka`/`fka`/`nka` from ALT, countries from ADD.
Each entity's hash covers name, programs, aliases and countries; the snapshot lives in
`source_snapshot` (system context only). The **first** successful poll is a silent baseline (cursor
`baseline_at`, no signal). Later publications are diffed: new + changed organisations →
**one publication signal**: `native_key` `sdn:<Last-Modified as YYYYMMDDTHHMMSSZ>` (else the poll
time); `regulatory/sanctions_designation` (`OTHER`: an investigation-only case); severity 8000;
geometry null, no `location_refs`; `match_keys` = sorted unique keys of the names and aliases of the
new and changed organisations (≤ 20 000); `severity_native {scheme: ofac, list: SDN, new, changed,
removed, match_keys, programs}`; `expires_at` + `OFAC_EVENT_TTL_DAYS` (30); authoritative (founds its
own event, joins nothing). Entity names never appear in the title or summary; the summary says
"Awareness only: exact organisation-name matches against your suppliers and factories; not a
sanctions-screening result". A publication with fewer than half the organisations of the snapshot is
refused (a truncated download must not look like mass delistings); the snapshot is written only
after the signal was stored. No OFAC demo pack (§5).

### 3.4 GDELT 2.0 (`adapters/gdelt.py`, connector `gdelt`)
`GDELT_LASTUPDATE_URL` = `https://data.gdeltproject.org/gdeltv2/lastupdate.txt` (conditional GET;
checked live: `http` now redirects to `https`). The export line is accepted only on
`data.gdeltproject.org` with path `^/gdeltv2/\d{14}\.export\.CSV\.zip$` and is fetched over https; the
latest file's size and MD5 must match `lastupdate.txt`; up to `GDELT_MAX_BACKFILL` (4) missed
15-minute slots since the last processed one are fetched by their deterministic URL (a missing one is
skipped); the processed slot in the cursor never moves backwards, so a stale `lastupdate.txt` never
counts a file twice; every zip goes through `safezip` (`.export.CSV`, ≤ 32 MiB). Rows (61 tab-separated columns)
kept when `IsRootEvent = 1`, `EventCode` starts with a configured code (`GDELT_EVENT_CODES`, each one
the adapter maps), `NumArticles ≥ GDELT_MIN_ARTICLES` (3), `ActionGeo_Type` 3/4 (city level), and the
action point lies within `GDELT_PORT_RADIUS_KM` (30) of a gazetteer port (nearest wins) or
`GDELT_CHOKEPOINT_BUFFER_KM` (50) of a chokepoint polygon. CAMEO → kind/subtype: 143x labor/strike;
144x and 191 logistics/blockade; 145x geopolitical/civil_unrest; 163 regulatory/trade_restriction;
1724 geopolitical/state_of_emergency; 190, 193–196 geopolitical/armed_conflict. Aggregation per UTC
day of `DATEADDED` × subtype × place (cursor `agg`, last 2 days, ≤ 2000 keys): `native_key`
`<YYYYMMDD>:<subtype>:<UNLOCODE|CHOKEPOINT>:<code>`; counts reported in powers of two (`events_bucket`,
`articles_bucket`) so only a bucket change makes a new version. Geometry = the gazetteer port point or
chokepoint polygon; refs `UNLOCODE:` + the port's `ISO3166:`, or `CHOKEPOINT:`. Severity =
`min(GDELT_PRIOR[subtype], GDELT_MAX_SEVERITY_BP)` (3900, below the default alert threshold).
`provenance inference`, `extracted_by gdelt-cameo`, `origin news`, authority 4000, not an anchor;
title `"<Strike or boycott|Blockade or obstruction|…> reported near <gazetteer place> (GDELT, N+
articles)"` — no actor or place text from the feed; `source_url` = the first `SOURCEURL` that passes
`safe_source_url`; `starts_at` = day start, `expires_at` = day start + 72 h. GDELT corroborates (raises
an event's source count and confidence) and anchors nothing; with the default rules it never alerts
on its own. Tenant **opt-in**.

### 3.5 MeteoAlarm (`adapters/meteoalarm.py`, connectors `meteoalarm.<country>`)
`METEOALARM_URL_TEMPLATE` = `https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-{country}` for
each `METEOALARM_COUNTRIES` slug (empty by default). Checked live: Atom entries with `cap:identifier`,
`cap:geocode` (`EMMA_ID`), `cap:areaDesc`, `cap:sent/onset/expires`, `cap:severity` (inconsistent across
national services), `cap:message_type`, a CAP document link, and a title `"<Level> <Type> Warning issued
for …"` — the legacy Atom carries **no** awareness parameters, so level and type come from the title;
the CAP document's `awareness_level` / `awareness_type` win when it was fetched (kept per identifier in
the cursor). Level ≥ `METEOALARM_MIN_LEVEL` (3 = orange; clamped to 2–4); severity yellow 3000 / orange
6000 / red 8500. Types: 1 wind → weather/wind_storm; 2 snow-ice → weather/snow_ice; 3 thunderstorm →
weather/thunderstorm; 5 high temperature → weather/heat; 6 low temperature → weather/cold; 7 coastal
event → weather/coastal_flood; 8 forest fire → natural_hazard/wildfire; 10 rain → weather/heavy_rain;
12 flooding, 13 rain-flood → natural_hazard/flood; 4 fog, 9 avalanches, 11 → skipped. `native_key`
`<identifier>#<EMMA_ID>`; `starts_at` = onset (else sent), `ends_at` = `expires_at` = expires; refs
`ISO3166` of the country; geometry from the CAP `<polygon>` (`lat,lon` → `[lon, lat]`) or `<circle>`
(32-gon) per EMMA area, fetched only from `feeds.meteoalarm.org` over https (`/api/` paths), at most
`METEOALARM_MAX_DETAIL_FETCHES` (4) new documents per poll (later polls fill the rest; a stored
polygon is reused), else none (refs only). A `Cancel` entry or one that left the feed is withdrawn.
Not an anchor: neighbouring district warnings cluster into one candidate event, and a later GDACS
event absorbs them. Deployment **and** tenant opt-in.

## 4. APIs (hub, service JWT; engine; web)
* `GET /v1/sources` and `GET /v1/tenants/{t}/sources` items add `kind`, `provenance`,
  `licence_class`, `attribution`, `terms_url`, `opt_in`, `coverage`, `description` (`authority_bp`,
  `host`, `polled` and the cadence limits already existed). Example:
  `{"connector_id":"nhc","adapter":"nhc","name":"NOAA National Hurricane Center","kind":"hazard","provenance":"fact","license":"Public domain (NOAA/NWS)","licence_class":"public_domain","attribution":"Source: NOAA National Hurricane Center","terms_url":"https://www.weather.gov/disclaimer","authority_bp":9500,"host":"www.nhc.noaa.gov","default_cadence_s":900,"min_cadence_s":300,"max_cadence_s":86400,"polled":true,"opt_in":false,"coverage":"Atlantic, East and Central Pacific tropical cyclones","enabled":true,"cadence_s":null,"effective_cadence_s":900,"paused":false,"poll":{…}}`.
* `PUT /v1/tenants/{t}/sources/{id}` unchanged: `{enabled: true}` on an opt-in source subscribes and
  un-pauses it. `PUT /v1/tenants/{t}/subscriptions`: an unlisted opt-in source goes back to off.
* Poll reports (`PollResult`) add `skipped` and `withdrawn` when non-zero.
* `python -m signal_hub.poller --once --adapter nhc|copernicus_ems|ofac|gdelt|meteoalarm [--connector meteoalarm.<country>]`;
  a paused source exits 0 without fetching.
* MCP and alert REST unchanged; OFAC hits appear as `entity_match` exposures in `AlertDetail`.
* Engine `signals_admin.get_sources` passes through `provenance`, `licence_class`, `terms_url` (kept
  only when it is an https URL with a plain host), `opt_in`, `polled`, `authority_bp`, `min_cadence_s`,
  `max_cadence_s`, `coverage`, `description` (each cleaned, `None` when absent). `put_source` on a
  source without a stored wish and without `enabled` in the PATCH asks the hub for the default, so a
  cadence-only change never subscribes an opt-in source (hub down → the M7 default, subscribed). No
  route changes.
* Web Sources table (`SignalSourceCells.tsx`): coverage and description under the name (the OFAC
  awareness disclaimer), a **Type** column (Hazard / Weather / Regulatory / News badge, a provenance
  chip "Fact" / "Inference" / "Inference · low authority" below 5000 bp, an "Opt-in" badge), licence +
  attribution + a "Terms" link (`target=_blank rel="noopener noreferrer"`, https only via
  `safeTermsUrl`, never `javascript:`), the switch disabled with "Not enabled on this installation
  (platform admin: SIGNAL_ADAPTERS)" when `polled` is false, "Starts polling when a workspace
  subscribes" under an unsubscribed opt-in source, whose cadence select waits for the subscription.
  The cadence select offers only the choices inside the source's own `min_cadence_s` /
  `max_cadence_s` (a stored value is always shown; without reported limits every choice, as in M7).

## 5. Demo and offline behaviour
New pack adapters (`PackAdapter`) `nhc`, `copernicus_ems`, `meteoalarm`, `gdelt`; `ofac` is not one on
purpose (a simulated designation matching seed supplier names, which could be real companies, is not
acceptable demo data). Payloads (`signal_hub/replay_feeds.py`, shared by the replay, the pack tests and
`validate.py`): `nhc {fixture, gis?: {<storm id>: [.kml|.kmz …]}}`, `copernicus_ems {fixture}`,
`meteoalarm {fixture, connector_id?, details?: {<identifier>: .xml}}`, `gdelt {fixture: .CSV|.zip}`.
`FIXTURE_TYPES` adds `.kml`, `.kmz`, `.zip`, `.csv`. Replays always use the code defaults of the GDELT
filter and the MeteoAlarm level (120 §3.5: deployment env never changes a replay). Everything is
origin demo / provenance simulated through the real parse functions, sim clock, uuid5 ids.

**`data/scenarios/hurricane-cape-route`**: the fictional Hurricane ALBATROSS (`al992026`; 90–99 are
invest numbers, never a named storm's) forms off Guinea and tracks north-west along the West African
coast; three advisories 6 h apart (7: 70 kt, 8: 100 kt, 9: 110 kt) with cone + track KML, generated by
`data/scenarios/tools/gen_hurricane_cape_route.py` (`--check`). Checked exactly by `validate.py
--require replay` and replayed on PostGIS (`test_db_real_packs.py::test_hurricane_cape_route`): one
event, `lane_crosses` on exactly CGP-FXT-SEA, CGP-RTM-SEA, SGN-HAM-CAPE, SGN-GDN-CAPE and no site;
6500 (medium) → 8500 (critical, re-notified) → 8500 (edited); the action stays `alert` (no
supplier/factory/port exposed, so no case is drafted). Test pack `feeds-test` (one step per M8 adapter)
in `services/signal-hub/tests/fixtures/scenarios-m8`.

## 6. Data and migrations
`services/signal-hub/migrations/0005_m8_more_feeds.sql` (re-runnable, role `signals`; 0004 belongs to
workstream 3): `signal.match_keys text[] NOT NULL DEFAULT '{}'` + partial GIN index;
`network_node.name_key text` + partial index `(tenant, name_key)`; `source_snapshot(connector_id CHECK
pattern, item_key (1–200 chars), item_hash, first_seen_at, updated_at, PK (connector_id, item_key))`
with RLS FORCE and a system-context-only policy (`sh_tenant() IS NULL`), like `hub_secret`. Existing
nodes keep `name_key` NULL until the engine's next network push (it re-pushes after every scn sync).
The migration is independent of 0004, so either order works. No engine, merxa or optimizer
migrations; subscriptions stay in `tenant.config.signal_sources`. Contracts: `PackAdapter` literal
widened, `SUBTYPE_TAXONOMY` += `wind_storm → NAT.storm`, `coastal_flood → NAT.flood`, `heavy_rain →
NAT.flood` (regenerated: `signals.scenario-pack.schema.json`, `signals-scenario-pack.ts`). Unchanged on
purpose: `EventTypeKey` (no `REG.*`), `LOCATION_REF_SCHEMES`, `ExposureMethod`, `DisruptionSignal`
(`match_keys` is an internal column).

## 7. Configuration
Settings (`config.py`, every one with a code default): `SIGNAL_ADAPTERS`
(`gdacs,usgs,rss,nhc,copernicus_ems,ofac,gdelt`), `NHC_URL`, `NHC_FETCH_GIS` (1),
`NHC_MAX_GIS_FETCHES` (6), `CADENCE_NHC_S` (900); `COPERNICUS_EMS_URL`, `COPERNICUS_EMS_TTL_DAYS` (14),
`CADENCE_COPERNICUS_EMS_S` (1800); `OFAC_SDN_URL`, `OFAC_ALT_URL`, `OFAC_ADD_URL`, `OFAC_MAX_BYTES`
(33554432), `OFAC_EVENT_TTL_DAYS` (30), `CADENCE_OFAC_S` (3600); `GDELT_LASTUPDATE_URL`,
`GDELT_EVENT_CODES` (`143,144,145,163,1724,190,191,193,194,195,196`; unmapped codes are a start-up
error), `GDELT_MIN_ARTICLES` (3), `GDELT_PORT_RADIUS_KM` (30), `GDELT_CHOKEPOINT_BUFFER_KM` (50),
`GDELT_MAX_SEVERITY_BP` (3900), `GDELT_MAX_BACKFILL` (4), `CADENCE_GDELT_S` (900);
`METEOALARM_COUNTRIES` (empty; slugs `^[a-z][a-z-]{1,40}$`), `METEOALARM_URL_TEMPLATE`,
`METEOALARM_MIN_LEVEL` (3), `METEOALARM_MAX_DETAIL_FETCHES` (4), `CADENCE_METEOALARM_S` (900).
* `compose.yaml` (`&signal-hub-env`, shared by API and poller): `SIGNAL_ADAPTERS`, `NHC_URL`,
  `COPERNICUS_EMS_URL`, `OFAC_SDN/ALT/ADD_URL`, `GDELT_LASTUPDATE_URL`, `METEOALARM_COUNTRIES`. No new
  services, networks or volumes; the poller keeps `HTTP(S)_PROXY=http://egress:3128` (all five feeds
  are https on 443, already allowed by Squid) and `mem_limit: 256m` (the real SDN trio parses at
  ~76 MB peak RSS).
* `.env.example`: a commented "Signal Hub feeds (M8)" block (MeteoAlarm terms note).
* Cloud Run (`infra/cloudrun/signal-hub.sh`): jobs `signal-hub-poll-{nhc,copernicus_ems,ofac,gdelt}`
  (`*/15`, `*/30`, `0 *`, `*/15`), `meteoalarm` (`*/15`) only when `.env` sets `METEOALARM_COUNTRIES`
  (then `SIGNAL_ADAPTERS` gains `meteoalarm`); `SIGNAL_ADAPTERS` and `METEOALARM_COUNTRIES` are rendered
  into the API service and every poll job. Native launcher (`keelson-native.ps1 Get-SignalHubEnv`):
  `SIGNAL_ADAPTERS`, `METEOALARM_COUNTRIES` passthrough. `scripts/verify-live.sh` step `m8-feeds`.

## 8. Security
* **SSRF**: follow-up URLs from feed data are accepted only on the adapter's fixed host and path
  pattern over https (NHC KMZ on `www.nhc.noaa.gov`, MeteoAlarm CAP on `feeds.meteoalarm.org/api/…`,
  GDELT exports on `data.gdeltproject.org/gdeltv2/<14 digits>.export.CSV.zip`); the destination guard
  and the egress proxy still apply on every hop; byte caps per request.
* **XML**: defusedxml everywhere (KML, Atom, CAP); entity / DTD tricks are parse errors (tested).
* **Zip**: `safezip` (size, streamed size, ratio, member count, traversal, encryption, ambiguity).
* **CSV**: strict `csv` dialect, per-field and row caps, per-call byte cap; GDELT rows of the wrong
  width are skipped.
* **Untrusted text**: storm names (sanitised), EMS names and MeteoAlarm area names (clipped, control
  characters stripped) appear only in titles, which the engine already treats as untrusted; GDELT and
  OFAC titles/summaries are fully synthetic (no feed text, no entity names).
* **RLS**: `match_keys` inherits `signal`'s mixed global/own policies; `name_key` lives on tenant-only
  `network_node`, so the name match runs inside the tenant context; `source_snapshot` is system-only;
  no tenant code is ever written to a global row (tested: another tenant sees no exposure, a tenant
  context reads no snapshot rows).
* **Four-eyes**: subscribing an opt-in source is not "enabling an external-egress connector" (the
  operator's `SIGNAL_ADAPTERS` gates egress). Guarding the disabling of `ofac` is deferred (§10).
* **Licences**: every signal carries its source's licence string; the admin page shows licence,
  attribution and terms; MeteoAlarm needs an operator decision before it is polled.

## 9. Deviations
From the brief (verified against the code and the live feeds):
1. Migration **0005** (120 §2; the brief said 0004). Spec doc **124** (not `120-m8-signal-feeds.md`);
   the root README is not edited (120 §3.7).
2. Copernicus EMS URL `public-activations-info/?limit=50` — the brief's `public-activations/` answers
   "Activation code parameter is mandatory"; the legacy RSS fallback answers 404, so there is none.
   Countries arrive as names. A closed activation expires at its last update (not the poll time, which
   would re-version it every poll); expired history is only ingested to close a held signal.
3. GDELT over **https** (http now redirects); backfilled slots are fetched by their deterministic URL
   without an MD5 (only the latest is listed; TLS + zip CRC); CAMEO 191 maps to **logistics**/blockade
   like 144x (one kind per subtype keeps the aggregate key unambiguous); GDELT priors live in
   `scoring.GDELT_PRIOR` instead of `NEWS_SEVERITY`, so the M6/M7 news severities do not change;
   aggregation by `DATEADDED` day.
4. MeteoAlarm: level/type from the Atom **title** (the legacy Atom has no awareness parameters), the
   CAP parameters win when fetched and are kept per identifier in the cursor; polygons are reused from
   the stored signal instead of a cursor cache; not an anchor (the brief left it open) so district
   warnings cluster.
5. OFAC: conditional GET **per file** (each S3 ETag is that file's MD5, so an alias-only publication
   must not hide behind an unchanged SDN.CSV) and no forced 24 h refresh; one signal per publication
   timestamp (not per day, so a second same-day publication is its own signal instead of replacing the
   first one's keys); a truncation guard; `source_snapshot` stores `updated_at` instead of
   `last_seen_at` (only diffs are written).
6. Opt-in is a static adapter property read by `event_subscribed`; `Pipeline` takes no
   `opt_in_sources` argument. Withdrawal lives in `feeds_more.withdraw_missing` (not
   `Pipeline.withdraw_missing` / `SignalDraft.from_stored`) to keep shared-module edits small.
7. `safezip` rejects an archive with several matching members (the brief's text said "first in name
   order", its test plan "rejects ambiguous"); `match_key` also drops periods/apostrophes and the `and`
   of `& Co.`.
8. The `feeds-test` pack lives in `tests/fixtures/scenarios-m8`, so the M6/M7 pack tests keep their
   single `typhoon-test` pack (no shared assertion changes); NHC demo storm name "Albatross" (the
   sanitiser refuses digits in `ALBATROSS-26`); the pack ships cone **and** track KML.
9. `csv.field_size_limit` is not changed process-wide; fields over 20 000 characters are refused.
10. Live checks need `SIGNAL_HUB_LIVE=1` besides `-m live` (the hub's pytest config does not exclude
    the marker by default).

From 120: none on allocations (migration 0005, env prefixes `NHC_*`, `COPERNICUS_EMS_*`, `OFAC_*`,
`GDELT_*`, `METEOALARM_*`, `CADENCE_<SOURCE>_S`, spec 124, e2e `api_m8_feeds_offline.py`). The default
of the existing `SIGNAL_ADAPTERS` changes (adds the four default-on M8 adapters). `GDELT_*` /
`METEOALARM_MIN_LEVEL` tune live filtering only; replays ignore them (120 §3.5).

## 10. Deferred
* `REG.sanctions` taxonomy key + compliance template so an OFAC hit frames a real perturbation (the
  intake enum change would miss recorded replay cassettes; needs a re-record plan). OFAC frames as
  `OTHER` (investigation-only case).
* OFAC full-list screening of tenant suppliers at network push (KYS), the consolidated non-SDN lists,
  fuzzy / transliteration matching (M8 alerts only on deltas with exact normalised names).
* MeteoAlarm EMMA_ID / NUTS3 area gazetteer (geometry for warnings without CAP polygons) and a
  MeteoAlarm demo pack (needs a licensed boundary dataset).
* Copernicus EMS AOI and product footprints (centroid only).
* Four-eyes on disabling the `ofac` source per tenant (new `ChangeRequestKind` / `GuardReasonCode` in
  the shared `admin.py`).
* Optional search connectors (NewsAPI etc.; commercial terms) and NHC wind-probability / storm-surge
  layers.
* The optional Playwright spec `m8-feeds.spec.ts` (Sources rows, Opt-in badge, axe, screenshot); the
  same behaviour is covered by the vitest suite `m8-feeds.test.tsx` (axe included) and the API e2e.

## 11. Tests
* signal-hub offline: `test_m8_parsers.py` (NHC + KMZ, EMS, MeteoAlarm + CAP, OFAC CSV quirks + diff
  + publication draft, GDELT lastupdate / filter / aggregation / buckets / cap; every draft validated
  against `DisruptionSignal`), `test_m8_logic.py` (safezip bombs/traversal/encryption/lying header,
  match keys, golden content hashes of GDACS/USGS from the M7 code, scoring tables, same-hazard window,
  radii, geo helpers, registry, settings parsing, pack step files), `test_packs.py` (+ `feeds-test`).
* signal-hub PostGIS: `test_db_m8_feeds.py` (NHC conditional GET + GIS once per advisory + withdrawal
  resolving the alert + malformed feed; NHC/EMS anchors joining GDACS events; EMS history and closure;
  OFAC baseline → 304 through the redirect → delta signal → `entity_match` for the right tenant only,
  RLS on exposures and `source_snapshot`, `name_key`, truncation guard; GDELT corroboration without an
  alert, MD5 mismatch backoff; MeteoAlarm bounded CAP fetches + withdrawal; opt-in schedule / alert /
  whole-map sync / `--once` skip; tenant sources API fields; `feeds-test` reset + replay identical),
  `test_db_real_packs.py::test_hurricane_cape_route`; existing catalogue assertions updated.
* `tests/test_live_feeds.py` (`SIGNAL_HUB_LIVE=1 -m live`): passed against the real NHC (with KMZ),
  Copernicus EMS, OFAC trio, GDELT (MD5) and MeteoAlarm feeds on 2026-10-08.
* data/scenarios: `validate.py --require replay` 0 errors / 0 warnings for every pack; tests for the
  generator (`--check`), cone geometry, real-host links, future advisories, NHC shape and GIS files.
* contracts-py `test_m8_feeds.py` (+ `test_m6.py` invalid adapter `acled`); contracts `--check` clean.
* engine `tests/platform/test_m8_signal_sources.py` (passthrough, non-https terms dropped, opt-in
  default, cadence-only change keeps an opt-in source off, hub-down fallback).
* web `test/m8-feeds.test.tsx` (schema, badges, attribution, terms link rel/target, `javascript:`
  never rendered, opt-in switch PUTs `{enabled: true}`, not-polled switch disabled, OFAC disclaimer,
  cadence choices inside the source's limits, axe); m7 suite unchanged.
* e2e `e2e/api_m8_feeds_offline.py` (written, compiled; run by the integrator on the full stack).

## 12. Known issues
* A news event that GDELT corroborates over several days can see its p75 severity pulled towards
  3900 once GDELT members outnumber the news ones (one GDELT signal per day × subtype × place limits
  this); anchored events are unaffected (anchor maximum).
* MeteoAlarm warnings get a polygon only after their CAP document was fetched (≤ 4 per poll); until
  then they expose by country reference only (no geo exposure). National services re-issue a warning
  under a new CAP identifier on each update, so an update is a new signal that joins the same event
  while the old one is withdrawn.
* OFAC: exact normalised names only (no transliterations or abbreviations); organisations listed
  before the baseline never alert (full-list screening is deferred); awareness only.
* An open Copernicus EMS activation stops counting 14 days after activation.
* NHC `starts_at` is a heuristic (6 h per advisory); it only matters for the 72 h same-hazard join.
* Network nodes pushed before migration 0005 have no `name_key` until the next network push.
* The poller polls sources sequentially; NHC GIS (≤ 6), OFAC (3–6 requests) and MeteoAlarm CAP
  (≤ 4 per country) downloads can delay the next source by up to a minute under the 5 s per-source
  rate limit.
* `scripts/cloudrun/tests` has 3 failures that also fail on the M8 base commit (compose env secrets,
  engine env parity, engine overlay mounts: unrelated to this workstream).
