# Signal Hub (`services/signal-hub`)

Proactive disruption detection for Keelson (contract `docs/contracts/100-m6-signals.md`, plan §15;
M8 feeds `docs/contracts/124-m8-signal-feeds.md`). It ingests public hazard feeds (GDACS, USGS, NHC,
Copernicus EMS, MeteoAlarm), the OFAC SDN list, trade-press RSS, GDELT's machine-coded news events and
tenant connector events (Merxa webhooks forwarded by the engine). It normalises everything into `DisruptionSignal`s
(`keelson_contracts.signals`) and clusters them into `DisruptionEvent`s. It then matches events
against each tenant's network in PostGIS and raises alerts by per-tenant rules. Demo packs replay
deterministically through the real adapters.

It never commits anything. An `alert_and_draft_case` decision only tells the engine to open a draft
case.

## Processes (one image, `services/signal-hub/Dockerfile`, build context = repo root)

| compose service | command | networks | role |
|---|---|---|---|
| `signal-hub-bootstrap` | `python -m signal_hub.bootstrap` | app, ai | idempotent: role + database `signals` + PostGIS (pg-core superuser), then migrations as role `signals`, then the extraction key (see "LiteLLM extraction key") |
| `signal-hub-api` | `signal-hub-api` (uvicorn :8300) | app, ai | REST `/v1`, MCP `/mcp`, `/demo-feeds`, health. Background: pending-extraction retries (every 30 s), expiry sweep (60 s), demo auto-advance |
| `signal-hub-poller` | `python -m signal_hub.poller` | app | feed adapters on schedules; one leader (Postgres advisory lock); outbound **only** via `HTTP(S)_PROXY=http://egress:3128` |

`python -m signal_hub.poller --once --adapter gdacs|usgs|rss|nhc|copernicus_ems|ofac|gdelt|meteoalarm|extraction|sweep
[--connector rss.x|meteoalarm.<country>]` runs one adapter and exits (exit 1 when a fetch failed; a
feed adapter outside `SIGNAL_ADAPTERS` and a paused source are skipped with exit 0). Cloud Run Jobs use
this form; see "Cloud Run" below.

The API has no Caddy route (internal only). Every `/v1` and `/mcp` call needs an engine **service
JWT**: HS256 with `SIGNAL_HUB_SHARED_SECRET`, claims `iss=keelson-engine`, `aud=signal-hub`,
`exp` (at most `SIGNAL_HUB_MAX_TOKEN_TTL_S`=900 s ahead) and `tenant` (a tenant slug). `iat` is
optional and validated by PyJWT when present. The tenant
claim becomes the RLS context. A `{tenant}` in a path must equal it, otherwise 403. Request bodies
are capped: 512 KiB, or 8 MiB for `/v1/tenants/…`.

## Storage (database `signals` in pg-core, PostGIS)

Migrations live in `migrations/NNNN_*.sql`. They are applied under an advisory lock, are immutable
once applied (checksum) and can be re-run. Geometry is stored twice: as the contract GeoJSON
(`doc`/`geometry` jsonb) and as `geography` for the spatial SQL.

**RLS FORCE** applies to every tenant table, so the owning role `signals` is subject to it too. The
role is `NOBYPASSRLS`, and the bootstrap refuses to migrate as a role that is a superuser or has
`BYPASSRLS` (it never ALTERs an existing role, so it checks instead). `signal_hub.db.tx(pool, tenant)`
sets `app.tenant_id` for each transaction:

* signals, events and news items mix global and tenant rows. A tenant context **reads** global rows
  plus its own rows and **writes** only its own. The system context (`tenant=None`) reads and writes
  global rows only. Global feeds therefore never pick up tenant data, and a tenant can never change a
  global row.
* network nodes/lanes, exposures, alerts, the alert change feed, rules, demo runs, source
  subscriptions and watch regions are tenant-only.
* `tenant_registry` (slugs), `source_cursor` (poll state) and `source_schedule` (the subscription
  counts and fastest wanted cadence per source) are global metadata without tenant data.
* `hub_secret` (the encrypted extraction key) and `source_snapshot` (M8: the last OFAC SDN snapshot,
  one hash per organisation) are readable only in the system context.
* M8 (migration `0005_m8_more_feeds.sql`): `signal.match_keys` holds the public organisation-name keys
  a global signal names (OFAC); `network_node.name_key` the key of each tenant node name, computed by
  `Pipeline.put_network`. Nodes pushed before the migration keep `NULL` until the engine's next network
  push (it re-pushes after every scn sync). The two only ever meet inside a tenant's RLS context.

A tenant's connector signal may attach to a **global** event. It then counts only for that tenant's
member list and exposure: global aggregates are computed in the system context from global members
only, so nothing leaks to other tenants.

## Adapters (`src/signal_hub/adapters/`)

| source | parser | provenance | dedupe key | notes |
|---|---|---|---|---|
| GDACS `geteventlist` (+ `getgeometry` footprints for TC) | `gdacs.parse_gdacs` | fact | `<type>:<eventid>` | CC BY 4.0; window `fromdate/todate` = last `GDACS_WINDOW_DAYS`; a footprint is fetched once per episode and only from the configured host; an unchanged episode reuses the stored footprint |
| USGS summary GeoJSON | `usgs.parse_usgs` | fact | feature `id` | public domain; depth dropped; country from the `place` tail (names only) |
| trade-press RSS 2.0 / Atom (`RSS_FEEDS`) | `rss.parse_feed` | inference after quarantine | guid, else canonical link | headline + <= 300-char plain-text snippet + link only (`content:encoded` is never read); `defusedxml`; a future `pubDate` is capped at the retrieval time |
| connector events (`POST /v1/ingest/connector-event`) | `connector.parse_connector_event` | fact (structured) / inference (free text) | `<tenant>/<type-specific id>` | `sailing.updated`, `lane.capacity_changed`, `po_line.updated`, `supplier_message.received`; other types → `202 {accepted:false}` |
| **M8** NHC `CurrentStorms.json` (+ cone / track KMZ) | `nhc.parse_nhc`, `kml` | fact, anchor | storm id (each advisory a new version) | public domain; GIS only from `www.nhc.noaa.gov` over https, at most `NHC_MAX_GIS_FETCHES` per poll and once per advisory (an unchanged one reuses the stored footprint; products left over by the cap or a failed download are fetched by the next poll, which reads the feed in full instead of a 304, and the storm keeps its last footprint meanwhile); a storm that leaves the feed is withdrawn |
| **M8** Copernicus EMS `public-activations-info` | `copernicus_ems.parse_ems` | fact, anchor | activation code | © EU, attribution required; centroid point + countries; unmapped categories skipped; an activation already expired at the poll is only ingested to close a signal the hub holds |
| **M8** OFAC SDN / ALT / ADD CSV | `ofac.parse_entities`, `ofac.publication_draft` | fact, anchor | `sdn:<newest Last-Modified of the three files>` (later than the last delta's) | public domain; organisations only; first poll = silent baseline (`source_snapshot`), then one signal per publication with new or changed organisations, no geometry or refs, `match_keys` of their names and aliases |
| **M8** GDELT 2.0 15-min event export | `gdelt.matching_rows`, `gdelt.drafts_for` | inference (`gdelt-cameo`, no LLM) | `<day>:<subtype>:<SCHEME>:<code>` | open, citation required; `lastupdate.txt` → the export on `data.gdeltproject.org` (path pattern, size and MD5 checked); CAMEO + articles + city-level + port / chokepoint filters; per-day aggregates in the cursor; capped below the alert threshold; tenant **opt-in** |
| **M8** MeteoAlarm legacy Atom per country (+ CAP documents) | `meteoalarm.parse_atom`, `parse_cap_detail`, `parse_meteoalarm` | fact | `<CAP identifier>#<EMMA_ID>` | redistribution terms apply: off by default (`SIGNAL_ADAPTERS`) **and** tenant opt-in; orange and red only; CAP polygons from `feeds.meteoalarm.org` only, at most `METEOALARM_MAX_DETAIL_FETCHES` per poll (the rest follow on the next polls, which read the feed in full while any is pending); a cancelled or vanished warning is withdrawn |

The source registry (`sources.py`) defines every adapter once (catalogue kind, provenance, licence and
licence class, attribution, terms link, cadence limits, authority, opt-in, coverage); the admin
catalogue, the cadence limits, the poller's adapter list and the feed runner's dispatch derive from it.
The M8 pollers live in `feeds_more.py`; their demo-pack steps in `replay_feeds.py`. Archives go
through `safezip.read_member` (one member, size and ratio caps, no traversal or encryption) and XML
through `defusedxml`.

All feed HTTP goes through `httpfetch.Fetcher`:

* conditional GET with `If-None-Match` / `If-Modified-Since` (304 = not modified);
* per-source rate limit (`SIGNALS_MIN_INTERVAL_S`, default 5 s);
* connect/read timeouts and a 12 MiB response cap (one call may change it: OFAC reads up to
  `OFAC_MAX_BYTES`, GDELT exports up to 32 MiB);
* the egress proxy, with `trust_env` off;
* a destination guard on every request **and every redirect hop** (`httpfetch.destination_guard`):
  only `http(s)` to public host names or public IP literals. Single-label and internal names
  (`litellm`, `*.internal`, `*.localhost` …) and private, loopback, link-local and metadata addresses
  are refused. Without a proxy (Cloud Run jobs leave through Cloud NAT with VPC egress) the name is
  also resolved and refused when any address is not public. With a proxy, Squid's `private_dst`
  covers that.

An unexpected error inside one source's poll (a feed shape no parser foresaw, a store failure) is
recorded as a failed poll with backoff. It never ends the poller loop.

Cursors persist in `source_cursor`. On failure the next poll backs off to cadence × 2^failures,
capped at 1 h. Cadences: GDACS 600 s, USGS 300 s, RSS 900 s, NHC 900 s, Copernicus EMS 1800 s, OFAC
3600 s, GDELT 900 s, MeteoAlarm 900 s (`CADENCE_*_S`).

A version bump happens when a re-polled item's content hash changes (`SignalDraft.content_hash`:
observable fields, not retrieval times). The new version gets `supersedes`, the old one becomes
`is_current=false`, and both stay members of the same event (the timeline).

Geometry is reduced to at most 4 000 positions per feed geometry (decimation, rings closed,
altitude dropped) **before** contract validation. Merged event footprints are capped at 20 000.
The contract cap is 50 000.

## Quarantine (news, supplier messages, carrier notes)

`quarantine.run_quarantine` runs three stages:

1. A deterministic pattern screen. A hit means `flagged`, with no LLM call.
2. The `guard-default` screen.
3. The `extract-fast` extractor with the strict `NewsExtraction` schema, checked by
   `news_extraction_violations`. A self-flag means `flagged`; a violation means `rejected`.

The LLM calls go to LiteLLM `/v1/chat/completions` over HTTP. They use `response_format` json_schema
with strict mode, no tools, temperature 0, and the untrusted text wrapped in `<untrusted>` (it cannot
close the wrapper). The key is the **dedicated** extraction virtual key (below), never the master
key or a tenant key.

### LiteLLM extraction key (`llm_key.py`, `secretbox.py`)

* **Provisioned by the bootstrap.** `LITELLM_MASTER_KEY` is passed to the one-shot
  `signal-hub-bootstrap` container only. After the migrations it upserts one virtual key:
  * alias `SIGNALS_LLM_KEY_ALIAS` (`keelson-signal-hub`);
  * `models` = the extraction aliases only (`extract-fast`, `guard-default`);
  * `key_type: llm_api` (no management routes);
  * `max_budget` `SIGNALS_LLM_MAX_BUDGET` (5) with `budget_duration` `30d`.

  A stored key that LiteLLM still reports `active` is reused, and drifted restrictions are patched
  (`/key/update`, addressed by its sha256). A missing, revoked or undecryptable key is replaced: keys
  with the alias are deleted first, so exactly one exists. Without the master key, or when LiteLLM
  is not ready within `SIGNALS_LITELLM_WAIT_S` or refuses, the step is skipped with a warning and
  the bootstrap still succeeds.
* **Stored encrypted** in `hub_secret`: AES-256-GCM, key from HKDF-SHA256 over `SIGNALS_DATA_KEY`,
  else over `SIGNAL_HUB_SHARED_SECRET` (separate HKDF info), with the row name as associated data.
  RLS limits the table to the system context, so a tenant transaction never reads it.
* **Used by the API** (and `poller --once --adapter extraction`). It is read at startup and re-read
  every `SIGNALS_LLM_KEY_REFRESH_S` (300 s), so a bootstrap re-run takes effect without a restart.
  A non-empty `LITELLM_SIGNALS_KEY` always wins. `/v1/sources` reports `llm_key_source`
  (`env`|`db`|null).
* The compose poller gets neither secret: it never calls the LLM.
* Rotating `SIGNAL_HUB_SHARED_SECRET` (or `SIGNALS_DATA_KEY`) makes the stored key unreadable.
  Extraction then pauses (items stay pending) until the bootstrap runs again and generates a new key.
* Neither the master key nor the virtual key is ever logged, put in a URL or included in an error.

When the key is missing, or the gateway answers 401/402/403/408/429/5xx or times out, the item stays
`pending_extraction`. The API retries it with backoff of 5 min × 2^(n-1), capped at 6 h. Nothing is
fabricated. After `SIGNALS_EXTRACTION_MAX_ATTEMPTS` invalid outputs (`news_item.bad_outputs`) the item
is `failed`. Unavailability never counts towards that limit: `attempts` counts every try and only
drives the backoff.

Extracted mentions only **select** gazetteer entries (`gazetteer.Gazetteer.geocode`):

* ports → `UNLOCODE:` + point;
* chokepoints → `CHOKEPOINT:` + polygon;
* countries → `ISO3166:` and regions → `REGION:` (refs only).

Severity is a subtype prior, never an LLM number. Untrusted text is only ever stored in
`raw_text`/`title`, and MCP results mark it `content_trust: untrusted_text`. Demo packs ship
pre-baked extractions (`extracted_by: prebaked`); only the pattern screen runs on them.

## Gazetteer (`data/geo`)

* `ports.json` (new): a UN/LOCODE subset with float WGS84 coordinates and aliases. It covers every
  Merxa/route port and about 90 major Asia/Europe/Middle East gateways.
* `chokepoints.json`, `regions.json` (countries + regions), `country_centroids.json`.

Matching is on the whole mention, after normalising case, accents and punctuation and dropping a
leading "the"/"port of" and a trailing "port"/"terminal".

## Clustering v1 (`cluster.py`)

* GDACS, USGS, NHC, Copernicus EMS and OFAC signals are **anchors**. Each anchor founds an event with
  id `event_id(connector, native_key)`, stable across versions. A second authoritative feed reporting
  the same hazard (same subtype, <= 100 km, starts within `same_hazard_window`: 72 h for storms,
  floods and wildfires — feeds date them differently — else 6 h) joins that event: an NHC advisory
  joins the GDACS cyclone, an EMS flood the GDACS flood.
* Any other signal attaches to an open event when all of these hold:
  * the kind is compatible (`COMPATIBLE`, e.g. natural_hazard ↔ logistics);
  * the windows overlap within ±72 h;
  * the geometry is <= 300 km away **or** a `location_ref` is shared.

  The ranking prefers anchored events, then the most shared specific refs, then the nearest event.
  If nothing qualifies, the signal founds a candidate event `event_id("signal", connector, native_key)`.
* A new anchor absorbs matching un-anchored candidate events. Their members move, and the old event
  becomes `resolved` with `merged_into`.
* Scope:
  * global signals → global events only;
  * tenant signals → global + own events;
  * demo-pack signals → the same tenant and pack only.

Event fields (`aggregate_event`, current versions only):

| field | how it is derived |
|---|---|
| lead (kind/subtype/title) | the anchor, else the earliest member |
| geometry | merged footprint |
| window | the members' window |
| status | `resolved` when every member expired (or merged), `monitoring` when nothing new for 48 h, `confirmed` with an anchor or >= 2 sources, else `candidate` |
| `radius_km` | an explicit entry first (M8: wind storm, coastal flood, heavy rain, snow/ice, heat, cold, wildfire 25; thunderstorm, landslide 10; volcano 100), then TC 300; EQ 250 if M >= 6.5, else 100; FL 50; port closure 10; strike/chokepoint 25; else 50 |
| taxonomy key | `taxonomy_key_for(subtype)` |

Known v1 limitation: when a candidate event is absorbed, only members of the same scope move. A
tenant's connector signal attached to an absorbed **global** candidate keeps pointing at the merged
(resolved) event and stops counting for that tenant's exposure.

Event titles and summaries are never written by an LLM. This optional step of the contract is
skipped. A news-only event's title is its first headline, which is untrusted.

## Formulas (`scoring.py`, integer basis points)

* **GDACS severity**: level base (green 2000, orange 6000, red 8500) plus a bonus capped at 1000:
  * TC: `(max wind km/h − 119) × 5`;
  * EQ: `(M − 6.0) × 1000`;
  * other hazards: `(alertscore − level floor 0/1.5/2.5) × 500`.
* **USGS severity**: `max(magnitude band, PAGER band)`, plus 1000 when the tsunami flag is set.
  * Magnitude bands: < 5 → 1000, < 6 → 2500, < 6.5 → 4000, < 7 → 5500, < 7.5 → 7000, else 8500.
  * PAGER bands: green 3000, yellow 5500, orange 7500, red 9000.
* **News / free-text severity**: a subtype prior (`NEWS_SEVERITY`, e.g. chokepoint_closure 7500,
  port_closure 6500, strike 5500, supplier_delay 4500), default 3000.
* **Connector severity**: delay `1500 + 700 × days` (capped at 9000); lane capacity cut
  `10000 − factor_bp`.
* **Confidence** = `authority × corroboration × recency` (`combine_confidence_bp`):
  * authority: GDACS/USGS 9500, connector 8500, trade press 7000, free text 6000;
  * corroboration: anchored 10000, else 1/2/>=3 sources → 6000/8000/10000;
  * recency: 10000 up to 24 h, linear down to 5000 at 7 days.
* **Event severity**: the maximum over current anchors, else the p75 (nearest rank) of current members.
* **M8 feeds**:
  * NHC by maximum sustained wind: < 34 kt 3000; tropical storm 34–49 kt 5000, 50–63 kt 5500;
    hurricane category 1 6500, 2 7500, 3 8500, 4 9200, 5 9700. `starts_at` = issuance − 6 h × (advisory
    number − 1); `expires_at` = issuance + 12 h.
  * Copernicus EMS by category: flood 6500, storm (wind storm) 6500, earthquake 7000, volcano 6000,
    wildfire 5000, mass movement (landslide) 5000; `expires_at` = activation + `COPERNICUS_EMS_TTL_DAYS`
    (14), or the last update once closed.
  * MeteoAlarm by awareness level: yellow 3000, orange 6000, red 8500.
  * OFAC SDN publication 8000 (an exposed supplier/factory drafts a case under the default rules);
    `expires_at` + `OFAC_EVENT_TTL_DAYS` (30).
  * GDELT: a subtype prior (`GDELT_PRIOR`: strike 5500, blockade 6000, armed conflict 6500, …) capped
    at `GDELT_MAX_SEVERITY_BP` (3900), below the default alert threshold; counts in powers of two.
  * Authority: NHC/OFAC 9500, Copernicus EMS/MeteoAlarm 9000, GDELT 4000.

## Exposure (`exposure.py`, PostGIS, per tenant)

| match | rule | weight |
|---|---|---|
| `intersects` | node inside the footprint | 10000 |
| `dwithin_km` | node within `radius_km` | 10000 → 2000 linear |
| `lane_crosses` | lane path crosses the footprint (weight 9000), or passes within 25 km of it (weight 7000) | 9000 / 7000 |
| `entity_match` | current members' refs name a tenant node or lane (`UNLOCODE`→port, `SUPPLIER`, `FACTORY`, `DC`, `STORE`, `LANE`) | 10000 |
| `entity_match` (M8) | a current member's public `match_keys` (OFAC organisation names) equal a tenant supplier's or factory's `name_key` (`names.match_key`: NFKD/ASCII, `&` → and, a leading "the" and trailing legal forms dropped, ≥ 2 words and 6 characters; exact, never fuzzy) | 10000 |

Watch regions (M7, below) add matches:

| region | rule | method / weight |
|---|---|---|
| `radius` around a named node | the node within `radius_km` of the footprint | `dwithin_km`, 10000 → 2000 over the region radius (0 km → `intersects`) |
| `radius` around a named lane | the lane path within `radius_km` | `lane_crosses` 9000 (crosses) / 7000 |
| `polygon` | footprint touches the polygon: tenant nodes inside it (optionally only `node_types`) | `dwithin_km` with the real distance, 5000 (0 km → `intersects`) |
| `polygon` | lanes crossing a touched polygon | `lane_crosses` 9000 (crosses the footprint) / 5000 |

There is one exposure per node, with precedence intersects > entity_match > lane_crosses >
dwithin. `ExposureSummary.open_po_lines`/`units_inbound` are `null`: the hub has no ERP numbers.

## Signals admin (M7, `admin.py`, `docs/contracts/110` §5)

Everything is per tenant behind the service JWT. The engine checks the admin role and proxies.
The `updated_by` columns take the token's `sub`.

### Sources

The catalogue is the configured feeds (`sources.catalogue`): GDACS, USGS, each `RSS_FEEDS` entry, NHC,
Copernicus EMS, OFAC, GDELT and each `METEOALARM_COUNTRIES` entry. Each shows its licence, licence
class, attribution, terms link, authority, host, cadence limits, catalogue kind (`hazard`, `weather`,
`regulatory`, `news`), provenance, coverage, description, `opt_in` and `polled` (whether
`SIGNAL_ADAPTERS` lets the poller run it). A tenant subscription in `source_subscription` (no row
means subscribed, default cadence) can:

* **Disable a source.** An event whose current members all come from disabled sources gets no
  exposure for that tenant. Its open alert resolves (`resolved` in the change feed), and
  re-enabling raises it again. The tenant's own signals always count (connector events, demo packs).
  Global data stays readable through MCP and search.
* **Opt in** (M8) to an **opt-in** source (GDELT, MeteoAlarm): there no row means *not* subscribed, the
  source stays paused until at least one tenant enabled it, its members count for a tenant only with
  an `enabled` row, and the engine's whole-map sync puts an unlisted one back to off. Enabling it
  cannot open egress the operator did not allow (`SIGNAL_ADAPTERS`), so it is not a four-eyes change.
* **Ask for a cadence** within `CADENCE_LIMITS`: GDACS 300 s–1 d, USGS 120 s–1 d, RSS 600 s–1 d, NHC
  300 s–1 d, Copernicus EMS 900 s–1 d, OFAC 1800 s–1 d, GDELT 900 s–1 d, MeteoAlarm 600 s–1 d
  (a configured default below the floor stays valid). Polling is shared:
  * the poll cadence is the fastest cadence any subscribed tenant wants, and a tenant without an
    override counts as the default (`source_schedule`, rebuilt under a lock after every change);
  * a source every registered tenant disabled is **paused** (the loop and `--once` skip it); neither
    ever polls an adapter outside `SIGNAL_ADAPTERS`.

  The poller re-reads the schedule every loop. A lowered cadence applies from the last poll
  (`FeedRunner.due`).

### Watch regions (`watch_region`)

Each region has an id (`^[a-z][a-z0-9_-]{0,63}$`), a one-line name and `enabled`, plus one of:

* `kind: radius` with `node_type`, `node_code` (must exist in the pushed network, else 422) and
  `radius_km` 1–2000;
* `kind: polygon` with `geometry` (Polygon/MultiPolygon, at most 5 000 positions, valid by
  `ST_IsValid`, at most 20 000 000 km²) and optional `node_types`.

There are at most 100 regions per tenant. Every change re-evaluates the tenant's open events. Like
any exposure change, a region that no longer matches keeps the open alert and refreshes its facts;
alerts resolve with their event.

### Alert rules

The contract model `AlertRule` plus `rule_problems` (422 `{message, problems: [{rule, problem}]}`):

* unique ids, at most 50 rules;
* `hysteresis_bp` >= 100;
* `alert_and_draft_case` needs `min_exposed_nodes` >= 1;
* `node_types` needs `min_exposed_nodes` >= 1;
* an `alert` rule without exposure needs `min_severity_bp` >= 4000;
* an `ignore` rule must have at least one condition.

Single-rule edits on a tenant that is still on the defaults start from a copy of the defaults.
Deleting the last rule returns the tenant to the defaults.

The engine owns the rule set: it keeps `tenant.config.alert_rules`, evaluates auto-open with it and
re-pushes it with a whole-set `PUT` after every scn sync. A hub-side single-rule edit is overwritten
by the next push unless the engine stores it too. The engine's editor therefore saves through its
own config and the whole-set `PUT`, and uses `…/alert-rules/validate` for live checks. The engine
should also refuse to store a set the hub would answer with 422.

## Alerts (`alerts.py`)

* Rules come from `alert_rule_set`, else `DEFAULT_ALERT_RULES`:
  * alert at >= 4000 bp with >= 1 exposed node;
  * draft a case at >= 7000 bp with an exposed supplier/factory/port.
* There is one alert per (tenant, event), id `event_id("alert", tenant, event)`.
* An update edits the alert. It **re-notifies** (status back to `new` unless `opened`, change
  `renotified`) only when severity rose by at least `hysteresis_bp` since the last notification, or
  when the action escalates to `alert_and_draft_case`.
* A resolved or expired event resolves its alert.
* `why` is a deterministic sentence built from the rule and the exposure counts. It never contains
  event text.
* Every change is appended to `alert_change` (the engine's feed).

## REST (`/v1`, service JWT)

| method + path | what |
|---|---|
| `GET /v1/alerts?status=a,b&severity=&limit=&updated_since=` | `{items: [Alert], new_count}` |
| `GET /v1/alerts/changes?after=<seq>&limit=` | `{items: [{seq, change: raised\|renotified\|updated\|resolved\|status, action, at, alert}], next_after}`. The engine polls this for toasts/SSE and `alert_and_draft_case` |
| `GET /v1/alerts/{id}` | `AlertDetail` (alert, event, member signals with attribution, exposures) |
| `POST /v1/alerts/{id}/status` | `AlertStatusUpdate`. `opened` + `case_id` is idempotent; a different case gives 409; an opened alert can only be resolved |
| `GET /v1/events/{id}` | `{event, signals, exposures, exposure_summary}` |
| `GET /v1/signals?query=&kind=&since=&limit=` | current signals (untrusted text) |
| `PUT` (or `POST`) `/v1/tenants/{tenant}/network` | `TenantNetwork`; replaces the snapshot and re-evaluates open events → `{network_version, alerts}` |
| `GET` / `PUT /v1/tenants/{tenant}/alert-rules` | tenant rules. GET: `{custom, rules, defaults, updated_at, updated_by}`. PUT `{rules}` replaces the set (empty = defaults), validated → `{rules, alerts_changed}` |
| `POST /v1/tenants/{t}/alert-rules` (201), `GET`/`PUT`/`DELETE …/alert-rules/{id}` | single-rule CRUD (409 duplicate id, 404 unknown, 422 problems of the rule written; problems of the other stored rules, e.g. an engine-pushed set, come back as `warnings`) |
| `POST /v1/tenants/{t}/alert-rules/validate` | `{rules}` → `{valid, problems}` without saving |
| `GET /v1/tenants/{t}/sources` | catalogue + this tenant's `enabled`/`cadence_s` + shared `effective_cadence_s`/`paused` + poll state |
| `PUT /v1/tenants/{t}/sources/{connector_id}` | `{enabled, cadence_s?}` (404 unknown source, 422 cadence out of range) → `{source, alerts_changed}` |
| `PUT /v1/tenants/{t}/subscriptions` | whole subscription map (the engine's sync) `{sources: [{connector_id, enabled, cadence_s?}]}`; unlisted catalogue sources go back to subscribed, unknown ids are ignored, a cadence outside the source's limits is clamped to them (the engine accepts 60 s–1 d and relies on this) and reported in `warnings` → `{items, ignored, warnings, alerts_changed}` |
| `PUT /v1/tenants/{t}/watch-regions` | whole region set `{regions: [...]}` (also the engine's shape: `polygon`, `node: {type, code}`); regions not listed are deleted; a radius node not yet pushed is kept with `node_found: false` (no 422) → `{items, deleted, alerts_changed}` |
| `GET`/`POST /v1/tenants/{t}/watch-regions` | list (+ `limits`) / create (201, 409 if the id exists) |
| `GET`/`PUT`/`DELETE /v1/tenants/{t}/watch-regions/{id}` | read / create-or-replace (201 when created) / delete; responses carry `area_km2` (polygon) and `node_found` (radius) |
| `POST /v1/ingest/connector-event` | `{connector_id?, type, id, timestamp?, data}` → 202 + ingest report (free text may be `pending_extraction`) |
| `GET /v1/tenants/{t}/network` | the stored network: `network_version`, `updated_at`, node codes by type, lane codes (no names or geometry) |
| `GET /v1/sources` | poll state per source, global pending-extraction count, `llm_configured` + `llm_key_source`, the caller tenant's own free-text items by status (`tenant_news`) |
| `GET /v1/demo/packs`, `GET /v1/demo/packs/{pack}`, `POST …/start {speed?}`, `POST …/advance {seconds?\|to_step?}`, `POST …/reset` | demo replay (`DEMO_MODE`; `DEMO_TENANTS` allow-list) |
| `POST /v1/demo/reset` | resets every pack of the caller's tenant **and** the rest of its hub state (connector-event signals / events / free text, every remaining alert + change rows, its exposures; the network, alert rules and global feed data stay) → `{tenant, packs, counts}` (the engine's demo reset calls it first; its scn resync then re-pushes the network, which re-raises alerts on open global events that still apply) |
| `GET /demo-feeds/{pack}/{file}` | pack fixture files for the real adapters (`DEMO_MODE` only, no auth, static repo data) |

## MCP (`/mcp`, FastMCP 4, stateless, service JWT)

All tools are read-only and return structured results:

* `search_signals(query?, kinds?, since?, near_node_type?, near_node_code?, radius_km?, limit?)`
* `get_event(event_id)`
* `hazards_near_node(node_type, node_code, radius_km?)`
* `assess_exposure(event_id)` (computed, not stored)

Results that carry news-derived text include `content_trust: "untrusted_text"` and
`untrusted_fields`. The engine classifies these tools `untrusted_text`.

## Demo packs (`replay.py`)

Packs live at `SCENARIOS_DIR/<id>/pack.yaml` (`ScenarioPack`) with fixtures next to them. Step
payloads:

| adapter | payload |
|---|---|
| `gdacs` / `usgs` | `{fixture}` |
| `rss` | `{fixture, connector_id?, source_name?, extraction? \| extractions?: {<guid\|link\|title>: NewsExtraction}}` |
| `merxa_inject` | `{event: {type, id, timestamp?, data}} \| {fixture}` (+ `extraction?` for the free text) |
| `chat_hint` | `{text}` (logged for the presenter) |
| `nhc` (M8) | `{fixture, gis?: {<storm id>: [<file>.kml\|.kmz, …]}}` |
| `copernicus_ems` (M8) | `{fixture}` |
| `meteoalarm` (M8) | `{fixture, connector_id?: meteoalarm.<country>, details?: {<CAP identifier>: <file>.xml}}` |
| `gdelt` (M8) | `{fixture: <file>.CSV\|.zip}` (the code-default GDELT filter, whatever the deployment's `GDELT_*`) |

The SimClock gives `sim_now = start_at + offset (+ speed × wall seconds)`. Each step is applied at
`start_at + t_offset_s`. Everything is tenant-scoped, `origin demo` and `provenance simulated`, and
alerts are `simulated: true`. Ids are uuid5 of content, so reset + replay is identical
(`tests/test_db_api.py`). `tests/test_db_real_packs.py` replays the product packs against
`data/scenarios/network-value-fashion-eu.yaml` and checks their documented outcomes.

## Configuration

| env | default | |
|---|---|---|
| `SIGNALS_DATABASE_URL` | — | `postgresql://signals:…@pg-core:5432/signals` |
| `SIGNALS_ADMIN_DSN`, `SIGNALS_DB_PASSWORD` | — | bootstrap only (superuser DSN; omit on managed DBs where db-init created role/db/PostGIS) |
| `SIGNAL_HUB_SHARED_SECRET` | — | service JWT secret (unset = reject all) |
| `LITELLM_BASE_URL`, `LITELLM_SIGNALS_KEY` | `http://litellm:4000`, unset | a set key overrides the provisioned one; neither = news stays pending |
| `LITELLM_MASTER_KEY` | unset | **bootstrap only**: provisions the extraction key |
| `SIGNALS_DATA_KEY` | unset | at-rest key for `hub_secret` (else derived from `SIGNAL_HUB_SHARED_SECRET`) |
| `SIGNALS_LLM_KEY_ALIAS`, `SIGNALS_LLM_MAX_BUDGET`, `SIGNALS_LLM_BUDGET_DURATION` | `keelson-signal-hub`, 5, `30d` | provisioned key |
| `SIGNALS_LITELLM_WAIT_S`, `SIGNALS_LLM_KEY_REFRESH_S` | 180, 300 | bootstrap wait for LiteLLM; API re-read interval |
| `HTTPS_PROXY` / `HTTP_PROXY` | unset | egress proxy for feeds (compose poller: `http://egress:3128`) |
| `GDACS_URL`, `USGS_URL`, `RSS_FEEDS` | contract URLs; `rss.theloadstar=The Loadstar\|https://theloadstar.com/feed/` | `RSS_FEEDS` = comma list of `<id>=<name>\|<url>` |
| `SIGNAL_ADAPTERS` | `gdacs,usgs,rss,nhc,copernicus_ems,ofac,gdelt` | the operator's allow-list of poller adapters (add `meteoalarm` once its terms were reviewed) |
| `NHC_URL`, `NHC_FETCH_GIS`, `NHC_MAX_GIS_FETCHES` | `https://www.nhc.noaa.gov/CurrentStorms.json`, on, 6 | NHC (M8; a cap below 2, one storm's cone + track, means no GIS) |
| `COPERNICUS_EMS_URL`, `COPERNICUS_EMS_TTL_DAYS` | dashboard `public-activations-info/?limit=50`, 14 | Copernicus EMS (M8) |
| `OFAC_SDN_URL` / `_ALT_URL` / `_ADD_URL`, `OFAC_MAX_BYTES`, `OFAC_EVENT_TTL_DAYS` | Sanctions List Service exports, 32 MiB, 30 | OFAC (M8) |
| `GDELT_LASTUPDATE_URL`, `GDELT_EVENT_CODES`, `GDELT_MIN_ARTICLES`, `GDELT_PORT_RADIUS_KM`, `GDELT_CHOKEPOINT_BUFFER_KM`, `GDELT_MAX_SEVERITY_BP`, `GDELT_MAX_BACKFILL` | `https://data.gdeltproject.org/gdeltv2/lastupdate.txt`, `143,144,145,163,1724,190,191,193,194,195,196`, 3, 30, 50, 3900, 4 | GDELT (M8; codes must be ones the adapter maps) |
| `METEOALARM_COUNTRIES`, `METEOALARM_URL_TEMPLATE`, `METEOALARM_MIN_LEVEL`, `METEOALARM_MAX_DETAIL_FETCHES` | empty, `…/meteoalarm-legacy-atom-{country}`, 3 (orange), 4 | MeteoAlarm (M8; slugs `^[a-z][a-z-]{1,40}$`; 0 CAP fetches = no polygons) |
| `CADENCE_NHC_S` / `_COPERNICUS_EMS_S` / `_OFAC_S` / `_GDELT_S` / `_METEOALARM_S` | 900 / 1800 / 3600 / 900 / 900 | M8 schedules |
| `CADENCE_GDACS_S` / `_USGS_S` / `_RSS_S`, `SIGNALS_MIN_INTERVAL_S` | 600 / 300 / 900, 5 | schedules and rate limit |
| `DEMO_MODE`, `DEMO_TENANTS`, `SCENARIOS_DIR`, `DEMO_FEEDS_BASE_URL` | off, any, `/app/scenarios`, `http://127.0.0.1:8300/demo-feeds` | demo replay |
| `GEO_DIR` | `/app/geo` | gazetteer data |

## Tests

Offline (parsers on recorded-shape fixtures marked `TEST FIXTURE`, quarantine with fakes, gazetteer,
formulas, clustering, auth, M7 rule/region validation, the secret box and key provisioning against a
mock LiteLLM in `tests/fakes.py`) plus PostGIS tests (`-m postgis`; they need `SIGNAL_HUB_TEST_ADMIN_DSN`
for a throwaway `postgis/postgis:18-3.6` and are skipped otherwise). No test calls an LLM or the
internet. M8: `test_m8_parsers.py` / `test_m8_logic.py` (recorded-shape fixtures of all five feeds,
safezip, match keys, golden content hashes, clustering, registry), `test_db_m8_feeds.py` (a mock
upstream per feed, opt-in, OFAC tenant isolation, the `feeds-test` pack in
`tests/fixtures/scenarios-m8`) and `test_db_real_packs.py::test_hurricane_cape_route`.
`tests/test_live_feeds.py` re-checks the parsers against today's real feeds and runs only with
`SIGNAL_HUB_LIVE=1 pytest -m live tests/test_live_feeds.py` (through `HTTPS_PROXY` when set).

```bash
docker network create keelson_m6_sh_net
docker run -d --name keelson_m6_sh_pg --network keelson_m6_sh_net --tmpfs /var/lib/postgresql \
  -e POSTGRES_PASSWORD=test postgis/postgis:18-3.6
docker build -f services/signal-hub/Dockerfile --target test -t keelson-signal-hub:test .
docker run --rm --network keelson_m6_sh_net \
  -e SIGNAL_HUB_TEST_ADMIN_DSN=postgresql://postgres:test@keelson_m6_sh_pg:5432/postgres keelson-signal-hub:test
docker rm -fv keelson_m6_sh_pg && docker network rm keelson_m6_sh_net
```

## Cloud Run

| manifest | what |
|---|---|
| `infra/cloudrun/signal-hub/signal-hub-api.yaml` | internal-ingress Service, minScale 1 so the extraction/sweep loops run |
| `infra/cloudrun/signal-hub/signal-hub-bootstrap.yaml` | migrations Job |
| `infra/cloudrun/signal-hub/signal-hub-poll.yaml` | template: one Job per adapter (`--once --adapter X`) triggered by Cloud Scheduler; feed adapters only when in `SIGNAL_ADAPTERS` (MeteoAlarm also needs `METEOALARM_COUNTRIES`), plus `extraction` and `sweep` |

`infra/cloudrun/signal-hub.sh` holds the deploy steps that `scripts/cloudrun/deploy.sh` sources.
