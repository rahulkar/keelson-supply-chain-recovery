# 123 · M8 — Signals: SimHash near-duplicates and vector clustering ("clustering v2")

Workstream 3 of M8 (`120-m8-overview.md`). Builds on 100 (Signal Hub, clustering v1) and 110 §5
(Signals admin). Branch `m8/signals-clustering`. Everything works and is tested with **no model
call**: SimHash and the local similarity profile are integer functions; remote embeddings are opt-in
and tested against a mock LiteLLM.

v1 rules stay authoritative and run first; v2 only adds evidence v1 cannot see. With both v2
switches off the hub behaves exactly like M7 (tested). The M6 demo packs and their `expected.yaml`
are unchanged and replay identically with v2 on and off.

## 1. Scope

| # | item | where |
|---|---|---|
| 1 | Contracts: `EventMembership` (+ `MembershipMethod`), `ClusteringConfig` / `SimHashSettings` / `VectorSettings` / `VectorThresholds`, `ClusteringConfigUpdate`, `ClusteringStatus`, `DEFAULT_CLUSTERING_CONFIG`; `AlertDetail.memberships` (optional, omitted when empty); JSON Schema + Zod regenerated | `packages/contracts-py` `signals.py`, `packages/contracts-ts` |
| 2 | Migration `0004_m8_clustering.sql` (SimHash columns + `duplicate` status + band index, `signal.cluster_terms` / `near_duplicate_of`, `signal_membership`, `cluster_config`, `embedding_profile`, `embedding_job`, RLS) | signal-hub |
| 3 | SimHash fingerprints + near-duplicate detection at queue time (same-source re-post vs cross-source copy, scope rules) | `simhash.py`, `neardup.py`, `Pipeline.queue_news` |
| 4 | Extraction reuse for near-duplicates (own pattern screen first; reuse / defer / flag / own quarantine; pre-baked wins) | `neardup.reuse_outcome`, `Pipeline.process_news` |
| 5 | Near-duplicate-aware corroboration (a copy never adds a source) | `cluster.aggregate_event` |
| 6 | Cluster terms + local profile `local-terms-v1` + integer cosine; `signal.cluster_terms` (not in `content_hash`) | `embeddings.py`, `Pipeline._upsert` |
| 7 | `cluster.decide()` (authoritative → near-duplicate → v1 → vector → found), `choose_event` (v1) unchanged | `cluster.py` |
| 8 | Memberships on every assignment (+ version copy, absorb relabel) and deterministic explanations; `Gazetteer.display_name` | `membership.py`, `explain.py`, `gazetteer.py` |
| 9 | Background merge pass (local, or remote centroid when available), open-case guard, replay runs it after every pack step, API loop, `poller --once --adapter cluster` | `maintenance.py`, `replay.py`, `app.py`, `poller.py` |
| 10 | Opt-in remote embedding profile: profile tables (`vector` / `halfvec`, HNSW, RLS), jobs with `LiteLlmEmbedder`, 14-day backfill on a profile change, bootstrap `CREATE EXTENSION vector` (optional, pinned), embed alias on the hub key | `vectors.py`, `bootstrap.py`, `llm_key.py` |
| 11 | Hub API: `GET /v1/clustering`, `PUT /v1/clustering/config`, `POST /v1/clustering/run`; memberships in the alert detail, the event bundle and MCP `get_event` | `clustering_api.py`, `queries.py` |
| 12 | Engine: platform-scoped hub token; `GET/PUT /api/admin/signals/clustering`, `POST …/run`; platform `admin_audit` | `core/signal_hub.py`, `core/signals_clustering.py`, `api/routes_admin_signals_m8.py` |
| 13 | Web: "Why these reports are grouped" in the alert drawer; tolerant membership parsing | `GroupingExplainer.tsx`, `lib/alerts/grouping.ts`, `lib/alerts/types.ts` |
| 14 | Web: Signals admin "Clustering" section (status, 7-day counts, thresholds; platform admin edits, tenant admin reads) | `SignalsClusteringSection.tsx`, `lib/admin/signalsClustering.ts` |
| 15 | Demo pack `port-strike-syndication` + `expected.yaml` (memberships, independent sources); `validate.py` replays SimHash + `cluster.decide` and refuses separate events that would merge | `data/scenarios` |
| 16 | Offline e2e `e2e/api_m8_clustering_offline.py` | e2e |
| 17 | Calibration fixture fixing the local default thresholds (>= 300 bp margin) | `services/signal-hub/tests/fixtures/m8_cluster_pairs.yaml` |

Deferred items: §11.

## 2. SimHash near-duplicates (`simhash.py`, `neardup.py`)

**Fingerprint.** Input = headline + `\n` + snippet (`NewsItemDraft.text`). NFKC, casefold, URLs dropped,
everything that is not a letter or digit → space, a small English stopword set removed, digits kept.
Features: every token and every adjacent token pair, weight 1 each (repeats add up), hashed with
keyed BLAKE2b (8 bytes, `person=kslSimH1`: independent of `PYTHONHASHSEED`). Bit *i* = 1 when the
weighted vote is positive. Fewer than 6 tokens → no fingerprint (never matched). Stored as a signed
`bigint` plus **eight 8-bit bands** `(i << 8) | bits[8i:8i+8]` in `news_item.simhash_bands int[]` (GIN):
two fingerprints within 7 bits share a band (pigeonhole), so the index is exact for
`max_hamming_bits` <= 7. Measured on 20–30-token items (`tests/test_m8_simhash.py`): one changed word
≈ 3 bits, a changed number 5, a snippet cut three words earlier or an outlet prefix ≈ 6; unrelated
items 24–39 bits (binomial around 32; P(unrelated <= 6) < 1e-10).

**Which items.** Trade-press news only: `origin news`, or an `rss.*` / `demo.rss.*` connector.
Connected-system free text (supplier messages, templated, differing by a PO number) is never folded.

**Lookup** (`Pipeline.queue_news`, in the item's own transaction and RLS scope, serialised per scope
by `pg_advisory_xact_lock('simhash:<tenant>/<pack>')`): band overlap (`&&`), `bit_count(simhash #
$fp)`, originals only (`near_duplicate_of IS NULL`, not `duplicate`), `retrieved_at` within
`window_hours`, scope: global → global non-pack items; tenant → global + own non-pack items (RLS
hides other tenants); pack → same tenant + pack.

**Outcomes.** Same connector and **0 bits** → status `duplicate` (an identical re-post under a new
id; not returned for extraction, never a signal). Same connector with 1..k bits → an edited update:
a normal item. Another connector within `max_hamming_bits` → `near_duplicate_of` + `near_duplicate_bits`;
the item stays `pending_extraction`.

**Extraction reuse** (`neardup.reuse_outcome`, `reuse_extraction`, only without a pre-baked
extraction): the copy's **own** text first goes through the deterministic pattern screen (a hit
flags it). Then by the original's status: `extracted` **and every place / port / chokepoint / company
of its extraction occurs in the copy's text** → the stored extraction, re-validated (`NewsExtraction`
+ `news_extraction_violations`), is reused with `extracted_by = simhash:<original's>` (<= 120 chars),
guard `{by: near_duplicate, of: <id>}` — no guard / extractor call, no attempt counted;
`pending_extraction` → the copy waits (`next_attempt_at` = max(now, original's) + 30 s; no attempt or
bad output counted); `flagged` → the copy is flagged `{by: near_duplicate, of}`; `rejected` /
`failed`, or a copy that does not name the original's entities → its own normal quarantine.

**Joining.** The copy's draft carries the original's current signal id; `decide()` puts it in the
original's event (`near_duplicate`) when that event is an open in-scope candidate of a compatible
kind **and** the copy names no other place (no refs, or a ref shared with the event). The copy's
`signal.near_duplicate_of` is set only then (and copied to its later versions).

**Corroboration.** `aggregate_event` counts independent sources = connectors of current members that
are not a near-duplicate of another member of the same event (falls back to all). Status `confirmed`
needs an anchor or >= 2 independent sources. A story plus its syndicated copy stays a 1-source
`candidate` (confidence 7000 × 6000 = 4200 bp). The news-only summary appends `; N independent`
only when the counts differ (M6/M7 summaries unchanged).

## 3. Cluster terms and the local similarity profile (`embeddings.py`)

`cluster_terms(kind, subtype, location_refs, mentions, companies, gaz)` → sorted unique terms (<= 64,
each <= 80 chars), stored in `signal.cluster_terms` (computed for every draft in `Pipeline._upsert`;
**not** part of `SignalDraft.content_hash`, so no stored signal re-versions):

* `k:<kind>`, `s:<subtype>`, `t:<taxonomy key>` (unless `OTHER`);
* `p:<word>` — words (gazetteer normalisation, place stopwords such as *port*, *terminal*, *city*
  removed) of the extracted **place / port / chokepoint mentions, resolved or not** (draft fields
  `mentions`, set by `draft_from_extraction`); a signal without mentions (feeds, connector events)
  uses the words of the curated names of its `UNLOCODE` / `CHOKEPOINT` refs. Country and region refs
  add no words;
* `o:<word>` — words of the extracted company mentions.

Headlines and snippets are never terms. Local vector: `p:` weight 2, every other term 1; an event's
vector is the sum of its current members'. `similarity_bp(a, b)` = cosine in basis points, exact
integer arithmetic (`k = isqrt((20000·dot)² // (|a|²|b|²))`, `(k+1)//2`, i.e. half-up), and **0 unless
a `p:` or `o:` term is shared** — kind/subtype agreement alone is never evidence. A signal's
similarity to a candidate event is the maximum over the event's current members (`membership.member_terms`).

**Calibration** (`tests/fixtures/m8_cluster_pairs.yaml`, 15 labelled pairs from the pack shapes plus
adversarial generic-word pairs; every pair on the right side of its threshold with >= 300 bp margin):

| pair (examples) | gate | similarity | label |
|---|---|---|---|
| "Port of Hamburg" strike vs unresolved "Hamburg-Waltershof terminal" strike | strict | 7977 | join |
| "Cat Lai terminal" closure vs unresolved "Cat Lai container depot" | strict | 8563 | join |
| two unresolved terminals, no shared word | strict | 0 | separate |
| "South Korea" vs "North Korea" port closures | attach | 6364 | separate |
| one generic shared word ("New Mangalore" / "New Delhi") | strict | 4842 | separate |
| same place, different kind (strike vs port closure) | strict | 4558 | separate |

→ `DEFAULT_CLUSTERING_CONFIG.vector.local` = attach 7000, merge 7500, strict 7500. The remote
thresholds (8500 / 9000 / 9300) cannot be calibrated offline (no model) and are provisional.

`cluster_text(terms, refs, headline?)` is what a **remote** profile embeds: `kind=…; subtype=…;
taxonomy=…; refs=…; places=…; organisations=…` (+ `; headline=…` only with
`SIGNALS_EMBED_INCLUDE_HEADLINE=1`).

## 4. Clustering v2 (`cluster.py`, `maintenance.py`)

`decide(sig, candidates, cfg) -> Decision{event_id|None, method, distance_km, shared_refs,
similarity_bp, similarity_profile, hamming_bits, duplicate_of}` — the first rule that applies wins:

1. authoritative (GDACS/USGS): the v1 same-hazard rule → `same_hazard` (distance); else it founds → `anchor`;
2. `simhash.enabled` and the signal is a near-duplicate whose original's event is an open in-scope
   candidate of a compatible kind, and the signal has no refs or shares one with that event → `near_duplicate`;
3. the v1 attach rule, unchanged ranking (anchored, most shared strong refs, nearest, id) →
   `attach_ref` (any shared ref; strong refs listed first) else `attach_geo` (distance);
4. `vector.enabled`: compatible kind, windows overlapping within `vector.window_hours`, similarity >=
   the profile's `attach_bp` and the geography gate — both located: distance <= `max_distance_km`;
   otherwise a shared country (ISO 3166 ref or UN/LOCODE prefix), or (the signal has **no ref at
   all**) similarity >= `strict_bp`; ranked anchored first, then most similar, then nearest, then id →
   `attach_vector`;
5. otherwise `founded` (a candidate event).

The candidate SQL now orders by distance and keeps at most 500 events, within `max(72 h,
vector.window_hours)` (the v1 rule re-checks its own 72 h). `choose_event()` (v1) is unchanged; a
600-case random corpus checks that `decide()` with v2 off — and with v2 on but no similarity or
near-duplicate evidence — chooses the same event.

**Merge pass** (`maintenance.merge_pass(pipeline, now, tenant, pack_id, profile)`, under
`CLUSTER_LOCK`, per scope: global; a tenant's own non-pack events; one demo pack) —
`choose_merge(src, targets, cfg)`: the source is an open, **un-anchored** event without an opened case
(an alert with `case_id` in any tenant that can see it); a target is an open event of the same scope,
anchored **or older** (window start, then id — the newer of two candidates merges into the older,
never both ways), compatible kind, overlapping windows, similarity >= the profile's `merge_bp`, the
same geography gate on event geographies / member refs. Preference: anchored, earliest, id. No chains
in one pass. Similarity: the remote centroid cosine (`1 - (avg(a) <=> avg(b))` on the profile table)
when an active remote profile has a vector for **every** current member of both events, else the
local centroids. A merge relabels the source's memberships (`merged_vector`, `from_event_id`,
similarity), moves its signals (same scope, like `_absorb`), sets `merged_into` + `resolved`,
recomputes both events and re-evaluates their alerts (the source's alert resolves, the target's
updates). Log line `cluster.merge` (INFO); every decision logs `cluster.decision` (DEBUG).

Runs: the API loop every `SIGNALS_CLUSTER_MAINTENANCE_S` (embedding jobs, then merges, for global +
every registered tenant; packs excluded), `POST /v1/clustering/run`, `poller --once --adapter cluster`
(Cloud Run, only where the API loop does not run), and **every demo replay step** (the pack's scope,
sim clock, local profile only — deterministic).

## 5. Memberships and explanations (`membership.py`, `explain.py`)

`signal_membership` holds one row per signal (every version): method, `prior_method`,
`from_event_id`, `distance_km`, `shared_refs`, `similarity_bp` + `similarity_profile`,
`hamming_bits` + `duplicate_of`, `config_version`, `explanation` (1..400), `decided_at` (the decision
time; the sim clock in a replay) and `recorded_at` (wall clock, for the status counts). The event of
a membership is its signal's `event_id` (not stored twice). Written in the same transaction and RLS
scope as the assignment:

* `Pipeline.ingest` → `membership.record` (the decision);
* a new version of a signal → `copy_previous` (same decision, same evidence, its near-duplicate link);
* `Pipeline._absorb` → `move(method="absorbed")`; the merge pass → `move(method="merged_vector")`;
  `prior_method` keeps the **original** method (never `absorbed`/`merged_vector`); a moved
  near-duplicate keeps its bits and original.

Contract `EventMembership` validates the evidence per method (`attach_vector`/`merged_vector`:
similarity + profile; `near_duplicate`: bits + original ≠ itself; `absorbed`/`merged_vector`:
`from_event_id` ≠ `event_id` + `prior_method`; `attach_ref`: shared refs; `attach_geo`/`same_hazard`:
distance; `anchor`/`founded`: no evidence). `AlertDetail.memberships` must belong to the event and name
member signals, one per signal.

**Explanations** are built only from the method, integers, validated place codes, curated gazetteer
names (`Gazetteer.display_name`: UNLOCODE, CHOKEPOINT, ISO3166, REGION) and fixed labels of
authoritative connectors (`GDACS`, `USGS`) — never from a headline, snippet, mention or
source-supplied name. Examples: *"Official GDACS report that founded this event."* · *"Names the same
place as the event: Ho Chi Minh City (Cat Lai) [UNLOCODE:VNSGN], Vietnam [ISO3166:VN]; reported within
72 h of the event."* · *"Describes a similar disruption (similarity 80 %, local term profile), with no
mapped location in common; reported within 72 h of the event; no shared place code."* · *"Near-duplicate
of another report in this event (3 of 64 fingerprint bits differ): counted once for corroboration."*
A property test puts a marker in mentions, companies and titles and checks no sentence ever contains it.

## 6. Remote embedding profile (opt-in, `vectors.py`)

Only with `SIGNALS_EMBED_PROVIDER=litellm` **and** pgvector in the signals database.

* **Bootstrap**: `CREATE EXTENSION IF NOT EXISTS vector [VERSION SIGNALS_VECTOR_VERSION]` when
  `pg_available_extensions` lists it (a warning otherwise; never fatal) — created whenever available,
  so the status can report it; then `ensure_profile`: one `active` `embedding_profile (alias, dims)`;
  another alias/dims retires the old row, **drops** its table and creates the new one.
* **Table** `signal_vec_<id>(signal_id PK → signal ON DELETE CASCADE, tenant, embedding vector(d) |
  halfvec(d) for d > 2000, created_at)`, HNSW (`vector_cosine_ops` / `halfvec_cosine_ops`), RLS FORCE
  with the mixed policies of `signal`. Identifiers come from the integer profile id only.
* **Jobs** (`embedding_job`, mixed RLS): the maintenance pass enqueues current, non-pack,
  non-simulated signals of the last **14 days** without a job (that is also the backfill after a
  profile change: clustering windows are 72 h, so no shadow re-embed / swap is needed) and processes
  due jobs per scope in batches (`SIGNALS_EMBED_BATCH`): `LiteLlmEmbedder` → `POST
  {LITELLM_BASE_URL}/v1/embeddings {model, input[], dimensions, encoding_format: float}` with the
  dedicated hub key, results sorted by `index`, count / dims / finiteness validated; vectors written
  as text literals. 401/402/403/408/429/5xx/transport → pending, `attempts` + 1, backoff (5 min ×
  2^n, <= 6 h); invalid output → `bad_outputs` + 1, `failed` at `SIGNALS_EXTRACTION_MAX_ATTEMPTS`.
* **Key**: `key_restrictions` adds `SIGNALS_EMBED_ALIAS` to the provisioned key's models only with
  this provider. Only processes holding the key embed (the API; `poller --once --adapter cluster`
  resolves the provisioned key like `extraction`); the compose poller has no key.
* **Use**: only the background merge pass. Ingest, replay and the validator are local-only.
* **Status**: `vector_backend.remote = {alias, dims, storage, status: active|backfilling, pending}` or
  `null`, `remote_reason`: `provider_local` · `pgvector_missing` · `profile_missing` (bootstrap not
  re-run for the configured alias/dims) · `key_missing` (profile active, no key yet: jobs wait).

## 7. Settings, API, engine, web

**Platform settings** (`cluster_config`, one versioned row; `clustering_config.load_config` falls back
to `DEFAULT_CLUSTERING_CONFIG`): `simhash {enabled, max_hamming_bits 0..7 (default 6), window_hours
1..168 (72), reuse_extraction}`, `vector {enabled, window_hours 1..168 (72), max_distance_km 50..3000
(1000), local / remote {attach_bp 5000..9900, merge_bp >= attach, strict_bp >= attach, <= 10000}}`.
Global feeds cluster once for every tenant (plan §15), so **no tenant can change them**; thresholds
are not env (determinism rule 120 §3.5).

**Hub REST** (service JWT): `GET /v1/clustering` → `ClusteringStatus` (any tenant token; counts of the
last 7 days by wall clock in the token tenant's RLS scope: SimHash copies, identical re-posts,
reused extractions, current signals' membership methods, pending/failed embedding jobs) ·
`PUT /v1/clustering/config` `ClusteringConfigUpdate` → `ClusteringConfig` (version + 1; **only** a
token with `scope: "platform"` → else 403; stale `base_version` → 409 `{detail: {message,
current_version}}`; bounds → 422) · `POST /v1/clustering/run` (platform scope) →
`{merged: [{source_event_id, target_event_id}], embedded, embedding_failed}`.
`GET /v1/alerts/{id}` (`AlertDetail.memberships`), `GET /v1/events/{id}` and MCP `get_event` (still
classified `untrusted_text` in the engine) carry the memberships. `ServicePrincipal.platform` =
`claims.scope == "platform"`; only the two write routes above honour it.

**Engine**: `mint_hub_token(tenant, platform=True)` / `request_json(..., platform=True)` add the claim.
`GET /api/admin/signals/clustering` (any admin) → the status + `editable` (platform admin);
`PUT /api/admin/signals/clustering` and `POST …/run` need a platform admin (`403
platform_admin_required` before any hub call), validate `ClusteringConfigUpdate` (422), set
`updated_by` to the actor, call the hub with the platform-scoped token, map hub 409 → `409
stale_version {current_version}`, 422 → `422 clustering_invalid`, and write a **platform**
`admin_audit` row `signals.clustering` (before + JSON diff) / `signals.clustering_run`. Threshold
edits are not a plan §17 guarded change (platform admin + audit; see §11). `/api/alerts/{id}`
passes the memberships through (a defaulted contract field: the new engine accepts an old hub).

**Web**: `alertDetailResponseSchema` parses `memberships` tolerantly (each entry with the generated
schema, `.strip()`; invalid entries, entries of another event or of a signal not in `signals` are
dropped; none → `[]`). `GroupingExplainer` ("Why these reports are grouped", between Sources and
Updates; hidden without memberships): one row per current report (superseded versions hidden), the
first report first, then by decision time; a method chip (Official report / Same hazard / First
report / Nearby / Same place / Similar description / Near-duplicate / Joined the official event /
Merged), evidence chips (`42 km`, place codes, `similarity 80 %`, `3 of 64 bits differ`, `first: …`),
the server sentence, and the headline as plain text labelled *Reported text*; near-duplicates sit
under their original ("Also reported by … : counted once"). The Signals admin **Clustering** section
(own client `ClusteringApi`, not the shared `AdminApi`): backends, the 7-day counts table, the form
(switches, Hamming select 0–7, hours, km, percent fields per profile) with client validation
mirroring the contract bounds; editable for platform admins, disabled inputs + a note for tenant
admins; 409 → "Settings changed by someone else — reload them…" + Reload; "Run clustering now";
hidden when the engine answers 404 (older engine).

## 8. Data, migrations, packs

Signal-hub migration **`0004_m8_clustering.sql`** (re-runnable, role `signals`, no pgvector objects):
`news_item` + `simhash bigint`, `simhash_bands int[]`, `near_duplicate_of uuid`,
`near_duplicate_bits smallint (0..64)`, status `duplicate`, GIN `news_item_simhash_bands` (partial),
`news_item_retrieved`; `signal` + `cluster_terms text[] NOT NULL DEFAULT '{}'`, `near_duplicate_of uuid`;
tables `signal_membership` (PK signal → signal ON DELETE CASCADE), `cluster_config` (`id = 1`),
`embedding_profile` (one active, partial unique index), `embedding_job` (PK signal + profile, both
CASCADE); RLS FORCE: mixed policies on `signal_membership` / `embedding_job`, read-all + system-write
on `cluster_config` / `embedding_profile`. Engine: no migration. Demo reset removes memberships and
jobs with their signals (cascade).

Pack **`data/scenarios/port-strike-syndication`** (three fictional outlets, pre-baked extractions):
Demo Freight Daily breaks a Hamburg dock strike (`founded`); Simulated Shipping Wire's syndicated copy
(one headline word changed) joins as `near_duplicate` and adds no corroboration (the event stays a
1-source candidate); Demo Port Bulletin's "Hamburg-Waltershof terminal" report (unknown to the
gazetteer: no ref, no geometry) joins by `attach_vector` (7977 bp ≥ strict 7500) → `confirmed`, 2
independent sources (5600 bp). Exposure: DEHAM `intersects` (the reports are geocoded to the port
itself), the four Hamburg lanes `lane_crosses`. `expected.yaml` gains optional per-event
`memberships` (member → method) and `sources` (independent sources). `validate.py` (replay level)
replays SimHash (same-source re-posts dropped, cross-source copies marked), places every member with
the hub's own `cluster.decide` in arrival order (absorb handled), compares `memberships` / `sources`
exactly, and reports separate events that the merge pass would merge; it also treats a node at an
identical point as `intersects` (PostGIS `ST_Intersects` of identical points).

## 9. Configuration, compose, Cloud Run

Hub env (prefixes `SIGNALS_EMBED_*`, `SIGNALS_CLUSTER_*` per 120 §2, plus the bootstrap pin):
`SIGNALS_EMBED_PROVIDER` (`local` | `litellm`, default local), `SIGNALS_EMBED_ALIAS` (`embed-default`),
`SIGNALS_EMBED_DIMS` (1536), `SIGNALS_EMBED_INCLUDE_HEADLINE` (0), `SIGNALS_EMBED_BATCH` (16),
`SIGNALS_CLUSTER_MAINTENANCE_S` (60; 0 = off), `SIGNALS_VECTOR_VERSION` (bootstrap; unset locally).
`compose.yaml`: the `&signal-hub-env` anchor (API + poller) gains the five `SIGNALS_EMBED_*` /
`SIGNALS_CLUSTER_*` lines; `signal-hub-bootstrap` gains `SIGNALS_VECTOR_VERSION` and the provider /
alias / dims. The poller keeps `LITELLM_SIGNALS_KEY: ""`. No new service, volume, image or network;
`mem_limit` unchanged. `.env.example`: a commented block. Cloud Run: the bootstrap job passes
`SIGNALS_VECTOR_VERSION=${PIN_VECTOR}`; the API (minScale 1) runs the maintenance loop, so no extra
Scheduler job is required (`signal-hub-poll` with `--adapter cluster` is available if needed).

## 10. Security

* Untrusted text reaches only the local SimHash (no network) and, after the quarantine, the short
  mention lists of cluster terms. The remote embedder gets the structured `cluster_text` (no headline
  by default); embedding models do not generate, so the text cannot steer them; flagged items never
  become signals, so they are never embedded.
* Extraction reuse means injected text in a copy never reaches an LLM; the copy's own pattern screen
  still runs and a flagged original flags its copies. Reuse requires the copy to name every entity of
  the original, so a templated story about another place cannot inherit the wrong locations, and the
  near-duplicate join requires the copy to name no other place.
* Near-duplicates never add corroboration: syndication or copy spam cannot inflate confidence.
* Explanations contain no untrusted text (property-tested); the web renders headlines as plain text.
* RLS: `signal_membership`, `embedding_job` and `signal_vec_*` are mixed like `signal`; `cluster_config`
  and `embedding_profile` are writable only in the system context (tested). The SimHash lookup's scope
  plus RLS means a tenant item never matches another tenant's, global items never see tenant items,
  packs see only their own items (tested).
* The platform scope claim is minted only after the engine's platform-admin check and honoured only by
  the clustering write routes (tested on both sides).
* No new egress: embeddings use the internal LiteLLM with the dedicated, budget-capped hub key (alias
  added only with the litellm provider); the poller holds no key. Dynamic SQL identifiers come from
  integer profile ids only. Queries are bounded (band index + window + LIMIT 20; candidates LIMIT 500;
  merge pass 500 events; enqueue 1000 per round).

## 11. Tests

* contracts-py `tests/test_m8_clustering.py` (membership evidence per method, AlertDetail with / without
  memberships — omitted from dumps when empty — and foreign memberships refused, settings bounds,
  status shape); `test_export_schemas.py` lists the 4 new schemas. contracts-ts `tests/m8-signals.test.ts`
  (Zod parses the Python-shaped fixtures, memberships optional, `MEMBERSHIP_METHODS`); the schema count
  in `blocks.test.ts` is 79.
* signal-hub unit: `test_m8_simhash.py` (goldens, normalisation, edit distances, short texts, signed
  round trip, 2 000-pair pigeonhole + an 8-bit miss, `PYTHONHASHSEED` subprocess),
  `test_m8_cluster_logic.py` (terms, golden vectors and half-up cosine, v2-off == v1 corpus,
  precedence, every vector gate, ranking, `choose_merge`, near-dup corroboration, golden explanations
  + marker property test, the calibration fixture, near-duplicate classification and reuse branches,
  `LiteLlmEmbedder` against `httpx.MockTransport` incl. 402/429/timeouts, NaN, dims, key not in repr,
  storage helpers, the platform claim, key restrictions, env parsing).
* signal-hub DB (`-m postgis`): `test_db_m8_clustering.py` (same-source re-post, syndicated copy with
  one guard + extract call and 1-source confidence, templated other-port story, defer / flag /
  injection / pre-baked / rejected-original, scope isolation, window, vector attach with an unresolved
  terminal, memberships for versions and absorb, v2 off == v1 outcomes, merge pass with alerts + open
  case guard + idempotence, anchored / cross-scope never merged, RLS of the new tables + cascade,
  clustering REST 403/200/409/422/run, counts RLS-scoped, memberships in alert detail / event bundle /
  MCP); `test_db_m8_vectors.py` (`-m pgvector`: bootstrap extension optional + pin validation, profile
  table / HNSW / RLS, jobs through MockLiteLlm `/v1/embeddings`, 14-day backfill, remote-centroid merge
  with the expected cosine, 402 backoff, dims-mismatch failure, profile change retire + drop, halfvec
  incl. `avg(halfvec)`, status reasons, local clustering without pgvector); `test_db_real_packs.py`
  (the three M6 packs with v2 on and off, the new pack: memberships, confidence, exposure, reset
  clears memberships, a second replay is identical).
* `data/scenarios/tests/test_packs.py`: the new pack, membership / source drift, same-source re-post
  dropped, separate events that would merge.
* engine `tests/platform/test_m8_signals_clustering.py` (platform claim; tenant admin 403 with no hub
  call; platform admin PUT forwards the platform token + `updated_by`, audit row with the diff; 409 /
  422 / run; hub invalid / down; memberships passed through `GET /api/alerts/{id}` and a foreign one
  refused).
* web `test/m8-signals.test.tsx` (parsing, rows, chips, drawer section + axe + plain-text headline,
  hidden without memberships, admin section read-only + axe, platform edit with validation and the
  exact PUT body, 409 message, hidden on 404).
* e2e `e2e/api_m8_clustering_offline.py` (written + syntax-checked; run by the integrator).

## 12. Deviations

From **120**: none in allocations (migration 0004, env prefixes `SIGNALS_EMBED_*` / `SIGNALS_CLUSTER_*`
+ the bootstrap pin `SIGNALS_VECTOR_VERSION`, contracts additive in `signals.py`, e2e name as listed).
Shared files touched minimally (see the branch summary). Contract rule 1 holds: `memberships` is
defaulted and omitted from dumps while empty; M6/M7 goldens and pack outcomes are unchanged.
Behaviour change by default (documented, intended by the brief): corroboration counts independent
sources — only events that contain a SimHash near-duplicate differ.

From the **brief** (`signals-clustering.md`):

* SimHash: 8 × 8-bit bands, unigram + bigram features of weight 1, `max_hamming_bits` 0..7 default 6
  (brief: 4 × 16-bit bands, bigram weight 2, 0..3 default 3) — measured: with the brief's features a
  one-word edit flips ~10 bits on 20-token headlines, so k <= 3 would only catch verbatim copies.
* Same-source `duplicate` only for **identical** fingerprints (0 bits); same-source edits are normal
  items (the brief folded any same-source near-duplicate, which would hide edited updates).
* Extraction reuse additionally requires every entity of the original to occur in the copy, and the
  near-duplicate join requires the copy to name no other place (not in the brief: protects templated
  stories about another place, which a lenient k makes near-duplicates).
* Local profile `local-terms-v1`: exact weighted bag of terms (no 256-dim signed feature hashing —
  collision noise for no benefit, vectors are never stored), place words from mentions (curated names
  only without mentions), no `r:`/`cc:`/`n:`/`m:` terms, and similarity 0 without a shared place or
  organisation word. Calibrated defaults: local 7000 / 7500 / 7500 (brief: 7500 / 8500 / 9000).
* `cluster_terms` computed centrally from the draft (`mentions`, `companies` draft fields) instead of
  in every adapter (no adapter edit; new M8 feeds get terms automatically).
* `signal_membership` stores no `event_id` (it is the signal's `event_id`), and gains `recorded_at`
  (wall clock) so the 7-day counts work for sim-clock replays.
* Embedding jobs are enqueued by the maintenance pass (no ingest-path or poller change); the merge pass
  considers all open un-anchored events of a scope (bounded, idempotent) instead of tracking "updated
  since the last pass".
* `remote_reason` gains `profile_missing`; the status counts by wall-clock recording time.
* Admin audit rows for the platform-wide settings are **platform** rows (tenant NULL).
* Contract doc number: this file is `123-m8-signal-clustering.md` (120 §1), not `120-…`.
* Hub DB tests: the M8 harness's Postgres already has pgvector; the README documents the pg-core image
  for local runs; tests marked `pgvector` skip without the extension.
* Web `test/adminMock.ts` is not changed (the new tests bring their own routes).

## 13. Deferred

* Carrying same-tenant signals when a **global** candidate event is absorbed or merged (the documented
  v1 limitation also applies to vector merges).
* Admin-driven switch of the signals embedding profile via four-eyes (change kind `embedding_profile`,
  target `signals`, platform kind) with a shadow re-embed / swap and a Knowledge-page "consumers" line;
  in M8 the profile is chosen at deploy time by `SIGNALS_EMBED_*`.
* A semantic `search_signals(similar_to=…)` MCP tool using HNSW kNN.
* An alert settle delay for news-only candidate events (fewer alert changes before merges) and
  LLM-written titles for news-only events (design note only).
* Calibrating the remote thresholds against a real embedding model (needs live credits).

## 14. Known issues

* The local profile is lexical: a place name that is a strict subset of another ("York" vs "New York")
  scores like a real match (7977 bp) when one side is unresolved; the kind / window / strict gates
  limit the damage. Remote embeddings (opt-in) are the semantic path.
* Signals stored before migration 0004 have no membership rows (and empty cluster terms) until they
  re-version; the drawer then simply shows fewer rows.
* A near-duplicate whose original's event is not an open candidate (resolved, outside the window)
  clusters by the normal rules and counts as an independent source.
* The open-case guard is checked before the merge transaction; a case opened in that instant could
  still see its event merged (the alert then resolves with the case kept).
* Remote similarities are float4 cosines (pgvector); only background merges use them and the value is
  recorded on the membership.
* `contracts-ts/tests/blocks.test.ts` and `contracts-py/tests/test_export_schemas.py` hard-code the
  schema set; other M8 workstreams adding schemas change the same lines (integrator: union of the sets).
