# 121 · M8 — mapping compiler (DuckDB) + LLM-proposed mappings

Workstream 1 of `120-m8-overview.md` (branch `m8/mapping-compiler`). Builds on `110` §4 (tier-A read /
action mappings, crawl sync, dry-run, per-op commit), `25` (scn/1.0), `55` (engine internals) and
`110` §1 (four-eyes). Plan §10 "Sync": *the DuckDB mapping compiler and LLM-proposed mappings are
post-demo* — this is that work.

Two parts:

1. **Mapping compiler.** A tier-A read mapping compiles to SQL and runs in a hardened, per-crawl
   DuckDB over the crawled items; every distinct read is paged once and its raw pages are staged in
   Postgres, so an admin can **explain** a mapping and **preview** an edit against the last crawl's
   full data without calling the vendor. The M7 interpreter stays the normative semantics (the
   compiler must agree with it, proved by differential tests) and stays available as a kill switch.
2. **Proposed mappings.** Keelson samples a connector's approved trusted read tools, reduces what it
   saw to shape-only profiles and proposes read / action mapping **drafts** — rule-based (always,
   no model needed) and optionally refined by an LLM. Drafts are never activated: an admin opens one
   in the mapping editor, previews it and saves it; an *action* mapping from a proposal needs a
   second admin.

Everything works with LLM credits at zero: the compiler and the rule-based proposer never call a
model, and the LLM path is tested with FakeLlm only.

---

## 1. Contracts (contracts-py → JSON Schema → Zod; additive, every M7 payload unchanged)

### 1.1 Expression language (`keelson_contracts.mapping`)

New functions (`EXPRESSION_FUNCTIONS`, mirrored in contracts-ts `MAPPING_EXPRESSION_FUNCTIONS`):

| Function | Semantics |
|---|---|
| `field('table', key, '$.path')` | record-lookup join: `table` (constant) names a `record` lookup (one source item per key text, first item wins), `$.path` (constant JSONPath-lite) is read from that item. A `null` key or a miss → `null`; keys follow the `lookup()` text rule |
| `cents(x)` | integer minor units from an exact amount: an integer (`12` → `1200`) or decimal text / JSON number (`'12.99'` → `1299`); more than two non-zero decimals, exponents, booleans → error (Decimal arithmetic) |
| `iso_week(x)` | `YYYY-Www` from an ISO date / date-time or from `YYYY-Www` / `YYYYWww` (validated against the ISO calendar) |
| `trim(x)`, `upper(x)`, `lower(x)` | ASCII only (`trim`: space, tab, CR, LF, VT, FF); `null` stays `null`, non-text is an error |

- **Record lookups.** `RecordLookup {kind: "record", source: ReadSource, key: Expression}` joins the
  lookup union. `lookup()` must name a `static` / `source` table and `field()` a `record` table (the
  `ReadMapping` validator says which to use); a lookup's key / value may not call `lookup()` / `field()`
  (no chains). `read_mapping_violations` checks record sources like entity and `source` lookup sources
  (approved, trusted `read` tools only — security relevant).
- **Grouping.** `EntityMapping.group_by: [Expression] (1..4) | null` (omitted from dumps while unset).
  One row per distinct key tuple (key texts as for `lookup()`; an item with a `null` key is skipped and
  counted), rows in the order of each group's first item. A grouped mapping's fields may be **one
  aggregate as the whole expression** — `first(x)` (first item), `min(x)` / `max(x)` (non-null values,
  all integers or all text), `sum(x)` (non-null integers; `null` when none), `count()` —
  (`AGGREGATE_FUNCTIONS`, `parse_expression(text, aggregates=True)`, `is_aggregate`); non-aggregated
  fields are evaluated on the group's first item and may only read paths that occur in `group_by`.
  `group_by` and `dedupe` exclude each other.
- **Lookup-key fix (deliberate behaviour change).** `source` / `record` lookup keys are stored with the
  same text rule as `lookup()` uses (`true` → `'true'`, integers in decimal; containers / floats are an
  error). In M7 a boolean key never matched (`str(True)` vs `'true'`); no pack mapping used one.
- Helpers: `source_key(ReadSource)` (16 hex of the canonical JSON's sha256: equal reads are fetched
  once), `mapping_hash()`, `path_text()` (canonical path text), `expression_records()` /
  `expression_record_paths()`, `op_takes(op, field)`.

### 1.2 Explain and preview (`keelson_contracts.mapping`)

- `MappingPlan {mapping_hash, engine: duckdb, sources[{key, source, paths[], used_by[]}],
  lookups[{name, kind, source_key|null, entries|null, key|null, value|null, paths[]}],
  entities[{where, entity, source_key, dedupe, group_by|null, joins[{table, kind, calls}],
  columns[{column, expression, aggregate|null, target_type, required, sql}], sql}],
  checks[{code: fk|duplicate_source_id, label, entity, severity}], warnings[]}`.
- `ReadPreviewRequest {mapping?, source: live|staged = live, max_pages 1..3 = 1,
  max_items_per_source 1..50000 = 20000, sample 1..20 = 5}`.
- `ReadPreview {ok, error, engine, source, staged{snapshot_id, status complete|failed, fetched_from,
  fetched_to}|null, capped, pages, rows_seen, truncated[], lookups{}, entities[{entity, where, rows,
  failed, skipped, sample[], error, columns[{column, null_bp, distinct, failures, first_failure}]}],
  checks[{code, label, entity, severity, violations, examples[≤5]}]}` — a superset of the M7
  read-test answer (`…/read/test` now answers it too).

### 1.3 Foreign keys (`keelson_contracts.scn.SCN_FOREIGN_KEYS`)

`ForeignKey {entity, columns, target_entity, target_columns, severity: error|warning, label, kinds,
target_kinds}`. **Errors** (a load fails) are exactly the two references every sync checked since M3
(`po_line.sku_code → item(sku)`, `inventory_position.location → location`); **warnings** (reported in
`crawl.checks` / the preview): `po_line.dest_dc_code → location(dc)`, `item(sku).style_code →
item(style)`, `location.parent_code → location`, `demand_forecast.location → location`,
`demand_forecast.style_code → item(style)`, `allocation.sku_code / dc_code / store_code`,
`transfer_order_line.sku_code / from_dc / to_dc`, `lane_rate / sailing .lane_code → lane`,
`size_curve.cluster_code → location(cluster)`, `bom_line.style_code → item(style)`. A warning is
checked only when both ends are mapped. The tier-B Postgres `scn_sync.FK_CHECKS` are unchanged.

### 1.4 Proposals (`keelson_contracts.mapping_proposal`, new module, owned by this workstream)

- Profiles: `SourceProfile {ref S1…, source: ReadSource (the paging the sampler detected), variant,
  items_sampled, total_hint, paths[≤200 PathProfile]}`, `PathProfile {path, shape (code | int |
  int_text | decimal_text | date | datetime | iso_week | bool | float | text | object | list | empty),
  date_format (DD/MM/YYYY …)|null, types[{type, count}], null_count, distinct_count, unique, max_len,
  examples[≤3]}` — examples only for shape-safe values.
- `MappingProposeRequest {kinds [read|action] 1..2, proposer: auto|heuristic = auto, sample = true,
  entities?, ops?, sources[≤16 {tool, args}]}`.
- `MappingProposal {id, connector, status: ready|accepted|discarded, proposer: heuristic|llm+heuristic,
  model_alias, created_by, created_at, inputs_hash, base_versions{read, action}, profiles[],
  read{entities[ProposedEntity], lookups[ProposedLookup]}|null, action{ops[ProposedOp]}|null,
  unmapped_entities[], unmapped_ops[], notes[ProposalNote], accepted{read, action}|null, llm_calls}`;
  `ProposedEntity {entity, source_ref, source, group_by, dedupe, fields[{column, expression,
  confidence_bp, origin heuristic|llm|both, rationale ≤300, alternatives[≤3]}], missing_required[],
  confidence_bp, origin, rationale}`; `ProposedOp {mapping: ActionOpMapping, confidence_bp, origin,
  rationale, generic}`; `read_draft()` = complete entities + the lookups they use, `action_draft()`.
- `ProposalNote {code, severity info|warning, where, message ≤300}`; codes `llm_disabled,
  llm_unavailable, llm_failed, replay_miss, llm_output_dropped, injection_suspected,
  description_flagged, sample_value_withheld, sampling_truncated, schema_only,
  required_column_unmapped, entity_unmapped, op_unmapped, path_not_in_sample, type_mismatch,
  generic_update_tool, source_unused, key_dropped, sample_failed`.
- Strict LLM output `MappingProposalLlm` (an `LlmModel`, `strict_schema_violations` clean: every field
  required, maps as lists, no bounds): `entities[{entity, source_ref, fields[{column, expression,
  confidence high|medium|low, rationale}], dedupe, group_by[]}]`, `lookups[{name, kind, source_ref,
  key, value, values[{key, value}]}]`, `ops[{op, tool, args[{name, kind template|json_template|integer
  |boolean|null, text, integer, boolean}], request_id_arg, readback_tool, readback_args[],
  readback_items_path, readback_match[{path, template}], confidence, rationale}]`,
  `injection_suspected`. `mapping_proposal_llm_violations(out, profiles=, tools=)` lists what the
  engine drops (unknown refs / columns / ops, expressions outside the grammar or with floats,
  unapproved tools or the wrong access, templates, ops that do not validate as the engine would store
  them). `CONFIDENCE_BP = {high 8500, medium 6000, low 3000}`.

### 1.5 Four-eyes vocabulary (`keelson_contracts.admin`)

`ChangeRequestKind += connector_mapping` (versioned: `base_version` required), `GuardReasonCode +=
ai_proposed_action_mapping`.

### 1.6 Schemas

`mapping.plan`, `mapping.preview-request`, `mapping.preview`, `mapping.propose-request`,
`mapping.proposal`, `mapping.proposal-llm` (contracts-ts index re-exports them;
`src/mapping-proposals.ts`: `MAPPING_AGGREGATE_FUNCTIONS`, `PREVIEW_SOURCES`, `PROPOSAL_*`,
`CONFIDENCE_BP`, `confidenceTone`, `confidencePercent`).

---

## 2. Mapping compiler (engine)

Pipeline: **fetch → stage → project → lookups → entities → checks → load**.

### 2.1 Compile (`core/mapping_compiler.py`, pure, no I/O)

- **Sources once.** Entity mappings and lookups with equal `ReadSource` share one paged read
  (`source_key`); `foreign-erp-lite` pages its styles and stores once instead of twice (9 reads
  instead of 11 calls).
- **Exact paths.** Python projects each item onto the paths the mapping reads with the interpreter's
  own `get_path` into Arrow batches, one *variant* column pair per path (tag 0 null / 1 text / 2 int /
  3 bool / 4 float / 5 object / 6 list + text), so the JSONPath dialect never diverges and DuckDB's
  JSON extension is not needed. The raw item is kept as JSON text for error messages.
- **Values with errors.** Every AST node becomes tag / text / error-code columns, each defined once
  in the first CTE stage after what it reads (never a lateral alias: DuckDB expands those
  exponentially); `CASE` chains, no function that can raise. Errors are values, so the lazy
  semantics of `coalesce` / `if_present` / `+` are exact.
- **Joins** (`lookup()` / `field()`) are `LEFT JOIN`s onto key → value / record tables built first
  (first item wins); **grouping** is a per-item stage + `GROUP BY` over key texts (`first` =
  `arg_min(…, ord)`, type rules and running-sum range checked with windows); `dedupe` keeps the first
  `source_id`.
- **Safe SQL.** Identifiers are generated (`src_<key>`, `lk_<name>`, `p3_t`); every constant goes
  through `sql_str()` (quotes doubled, NUL refused). No user text reaches SQL otherwise (tested with
  quotes, backslashes and `'); DROP TABLE` payloads).
- **Explain** (`explain()` → `MappingPlan`): sources and their users, lookups, per entity joins /
  grouping / per-column SQL + target type, checks, warnings (two plain mappings of one entity over
  the same read; an error FK whose target is not mapped).

### 2.2 Run (`core/mapping_exec.py`, `core/duckdb_session.py`)

- One DuckDB per job (crawl or preview), `:memory:`, `DUCKDB_MEMORY_LIMIT_MB` (engine-api 128, worker
  256) with spill to a fresh private `mkdtemp` (removed on close), `DUCKDB_THREADS`; extensions are
  never auto-installed / loaded, community extensions refused; after creation `enable_external_access
  = false` and `lock_configuration = true` (no file / URL / `COPY` / `ATTACH` / `INSTALL` / `SET`;
  data only enters as registered Arrow batches). Never cached or shared: it holds one tenant's rows.
  `duckdb` is imported lazily (engine-api pays on the first preview). Blocking work runs in a thread;
  a cancelled caller interrupts the query first.
- **Error parity.** SQL only *locates* the first failing item (`min(ord)` per entity mapping in
  mapping order, per lookup group in lookup order); rows before it are decoded and validated with the
  `scn/1.0` row models in crawl order; the failing item is **re-evaluated by the interpreter** (against
  DuckDB-backed lookup tables), which raises the exact M7 message (`entities[i] (entity) item n:
  column: …`). If the interpreter does not fail on it the engines disagree: the crawl fails
  `mapping_failed` with a "please report" message and an error log (never loads silently). Accepted
  difference: every read is fetched before mapping, so a fetch error wins over an earlier mapping
  error.
- **Checks before load.** Duplicate `source_id` per entity → `data_invalid` (up to 5 examples, before
  a Postgres unique violation); `SCN_FOREIGN_KEYS` restricted to the mapped entities: errors →
  `fk_violation` (nothing written), warnings → `crawl.checks`. The Postgres FK sanity, `ANALYZE`,
  supersede and pointer flip of `scn_crawl.run_crawl` are unchanged (defence in depth).
- `crawl` jsonb gains `engine, mapping_version, mapping_hash, sources[{key, fetch_key, tool, pages,
  items, truncated}], checks[], skipped{}` (interpreter crawls keep the M7 shape + `engine`).
- `SCN_MAPPING_ENGINE=duckdb` (default) | `interpreter` (the exact M7 path: same rows, kept as the
  reference and kill switch). `scn_crawl.compiled_rows()` mirrors `crawl_rows()` for tests.

### 2.3 Raw staging (`core/scn_staging.py`, migration 0011)

- Each fetched page → `scn_raw_page(tenant, snapshot_id, source_key, page_no, item_count, bytes, items
  jsonb)` under `scn_raw_set(tenant, snapshot_id = the crawl attempt, connector, status staging |
  complete | failed, mapping_version, mapping_hash, sources jsonb, pages, items, bytes, fetched_from,
  fetched_to)`; inserted in short transactions (≤ 50 pages / 8 MiB). A page Postgres cannot store as
  JSON (NaN, NUL: any SQLSTATE class `22` data exception) fails the crawl `items_invalid`.
- `complete` once every read was fetched (even if the mapping then fails), `failed` when fetching
  stopped. Size: `SCN_RAW_MAX_BYTES` (all reads of one crawl) → `too_many_rows`.
- **Retention:** a crawl start deletes every set of the connector but the current snapshot's; a
  successful flip (either engine: an interpreter crawl stages nothing, so the compiled crawl it
  supersedes leaves no stale "last crawl") deletes every set but the new current one. So at most the current set, the latest
  failed attempt's and the running one exist — an admin can fix a `mapping_failed` crawl against
  exactly the data that failed. Sets also go with their snapshot (GC, failed-history cap; FK
  cascade), with the connector, with an orphaned attempt (`_fail_orphans`) and on demo reset
  (`mark_noncurrent` drops finished non-current sets). `SCN_RAW_STAGING=0` keeps raw data only inside
  the crawl's DuckDB (no staged preview).
- Staged preview reads pages back with a server-side cursor in page order; a read is found by its
  source key or, when only `max_pages` changed, by its fetch key.

---

## 3. Proposed mappings (engine)

`POST …/mappings/proposals` runs synchronously within `MAPPING_PROPOSE_TIMEOUT_S` (504
`proposal_timeout`), one per tenant and two per process (429 `proposal_busy`);
`MAPPING_PROPOSER=off` → 409 `proposer_disabled`.

### 3.1 Catalogue (`core/mapping_profile.py`)

Approved tools only. Read candidates = `read` + `trusted_data`; `untrusted_text` read tools are never
sampled nor described. Write tools are listed by name and parameters (never called). Tool and
parameter descriptions are vendor text: control / format characters dropped, whitespace collapsed,
≤ 300 / 200 characters, screened with `quarantine.looks_like_injection` (NFKC); a flagged description
is dropped (note `description_flagged` + audit `untrusted_text.flagged`). Parameter `enum` values are
vendor text too: like sampled examples only shape-safe codes (`^[A-Za-z0-9][A-Za-z0-9_.:/#|-]{0,39}$`)
that pass the same screen are kept (free-text values are dropped, flagged ones noted and audited the
same way).

### 3.2 Sampling and profiles (`core/mapping_profile.py`) — the untrusted-text boundary

- Only with `sample: true` and an active connector (else 409 `connector_inactive`); `sample: false`
  = schema only (note `schema_only`; profiles only from declared output schemas). Calls go over
  `scn_crawl.session_caller` (reader principal, egress proxy, one MCP session).
- **Variants**: values of a string parameter named `type|kind|entity|resource|table|collection` from
  its `enum` or the tokens after "one of" in its description (≤ 32), used only as argument values to
  approved read tools; plus admin-named `sources`. **Paging** from the input schema (`page` +
  `page_size|per_page|limit|size|max_results`, schema maximum honoured, capped at 1000; `cursor|
  page_token|after|…`), **result** from the first page (`$.items|data|results|…` or the first list of
  objects; cursor / total keys). Proposed `max_pages = max(10, ⌈total/size⌉ × 2)` (≤ 10,000), 1000
  without a total. A read tool whose required arguments cannot be chosen is skipped (note
  `source_unused`).
- **Probes** (action proposals): each get-by-key read tool is called once with a key from the sampled
  records to learn where its result holds the record (the read-back `items_path`).
- **Budget**: ≤ `MAPPING_PROPOSE_MAX_CALLS` calls of ≤ `MAPPING_PROPOSE_SAMPLE_SIZE` items
  (`sampling_truncated` beyond). A failing call is a `sample_failed` note with its error *code* only
  (vendor messages are never echoed). Write tools are never called.
- **Profiles**: keys `^[A-Za-z_][A-Za-z0-9_]{0,63}$` (never `__…`) up to depth 4, ≤ 200 paths
  (`key_dropped`). Values are reduced to a shape, counts, uniqueness and the longest text; examples
  only for shape-safe values — codes `^[A-Za-z0-9][A-Za-z0-9_.:/#|-]{0,39}$` (no whitespace),
  numbers, dates, ISO weeks, booleans — that also pass the injection screen. Free text (names,
  cities with spaces, descriptions) is withheld (`sample_value_withheld`). Raw samples are never
  stored. `inputs_hash = sha256(profiles + approved tool definitions + probes + request)`.

### 3.3 Rule-based proposer (`core/mapping_heuristics.py`; pure, deterministic, no model)

- **Entity** from the variant / tool name via synonym tables (`dc|warehouse|store|cluster|factory|…`
  → `location` + kind; `sku|style|product|…` → `item` + kind; `po_line`, `inventory|stock`,
  `forecast|demand`, `allocation`, `transfer_order_line`, `sailing`, `lane`, `promotion`, …).
- **Columns** by name (exact 8000 bp, synonym 7000, without `_code`/`_id` 6000) with the sampled
  shape choosing the wrapper: `int($.x)` for counts, `$.x` for an integer version, `cents($.x)` for
  decimal money in `*_cents`, `date($.x)` / `date($.x, 'DD/MM/YYYY')`, `$.x` or `iso_week($.x)` for
  weeks, `str($.x)` for integer codes; `kind` constants from the variant (9000); enum columns from a
  path whose sampled values all are options; `location_kind` / `location_code` from per-kind code
  paths (`if_present($.store_code, 'store', 'cluster')` / `coalesce(…)`); a style's `style_code` is
  its own code; a location's `parent_code` is its parent kind's path (store → cluster → dc,
  factory → supplier). Incompatible shapes give `type_mismatch` notes.
- **Record joins**: a column still missing while the item has `<x>_code` and another sampled read `x`
  (`po` = `purchase_order`) is keyed by a unique code and has that column → `record` lookup +
  `field('x', $.x_code, '$.column')` (6000, +1000 when the sampled codes overlap).
- **Derived parents**: a parent kind with no read of its own (stores → `cluster_code`) → a `group_by`
  location mapping with `parent_code: first($.home_dc_code)`, ≤ 3000.
- **`source_id`**: `'<kind>:' + code` for locations / items (Merxa's tier-B convention), the unique
  `code` otherwise, else the `scn/1.0` natural key joined with `#`, `/` or `|` (`po_code#line_no`,
  `location_kind/location_code/sku_code`, `style|location|week`), else a unique `key`/`id`.
- **Actions**: a write tool whose name tokens contain the op's (`create_transfer_order` ↔
  `transfer_order.create`, `po` = `purchase_order`) with arguments matched by name (`*_json` text →
  `{"$json": …}`, `request_id|idempotency_key|…` → `request_id_arg`); field-setting ops
  (`allocation.set_qty`, `po_line.reschedule`, `po_line.change_qty`) on a **generic update tool**
  `(type, key, fields_json, expected_version)` at ≤ 3000 with a `generic_update_tool` warning.
  Idempotency = the admin-confirmed classification (never higher; `enforced` without a request-id
  parameter is claimed `best_effort`). Read-back: the probed get-by-key tool (target ops) or a list
  tool filtered on the payload fields the sampled records have (create ops); no read-back for a
  non-enforced op → not proposed (`op_unmapped`).
- Over the `/mcp-lite` fake (Merxa's exact tool shapes) the read draft reproduces the
  `foreign-erp-lite` pack's entities with every required column and ≥ 75 % of its columns
  canonically equal (≈ 84 %), crawls through the compiler with the same row counts, and the action
  draft maps `transfer_order.create` and `allocation.set_qty` like the pack.

### 3.4 LLM proposer (`runtime/mapping_proposer.py`)

- Used only when `proposer: auto` **and** `MAPPING_PROPOSER=auto`, with the tenant's LiteLLM key.
  Agent `mapping_proposer` (`make_agent`: tool-free, `include_contents='none'`, strict output
  `MappingProposalLlm`, `LLM_RETRY`), alias `MAPPING_PROPOSER_ALIAS` (a Pro alias is refused →
  `llm_unavailable`). Fixed instruction (grammar, rules, the scn columns and op payloads in scope);
  the data — profiles (examples dropped first, then whole profiles, to fit
  `MAPPING_PROPOSE_MAX_PROFILE_CHARS`), the sanitised catalogue and the rule-based draft — is the user
  turn, fenced as untrusted data. One-node in-memory ADK runner under a bound `RunContext`
  (`kind = mapping_proposal`: replay keys, LiteLLM trace / customer headers, `BudgetPlugin` with
  ≤ 2 calls), within `MAPPING_PROPOSE_LLM_TIMEOUT_S`.
- Fallbacks (the rule-based draft is returned with a note): no key → `llm_unavailable`; replay mode
  without a recording → `replay_miss`; error, timeout, malformed output → `llm_failed`.
- **Post-validation** (deterministic): `injection_suspected` discards the whole output (note + audit
  `untrusted_text.flagged`); items `mapping_proposal_llm_violations` refuses are dropped
  (`llm_output_dropped`); a field reading a path its profile does not have is capped at 2000
  (`path_not_in_sample`), one contradicting the sampled shape at 3000 (`type_mismatch`); op arguments
  must be parameters of the tool; idempotency comes from the classification; rationales are cleaned
  text (≤ 300). The model may only reference profiles it was sent.
- **Merge**: per column, equal canonical expressions → origin `both`, `max + 1000` (≤ 9500); otherwise
  the higher confidence wins and the other becomes an alternative; the draft fills what the model left
  out; an LLM field that breaks a merged entity (e.g. the `group_by` rule) is dropped; ops: equal →
  `both`, otherwise the more confident one.

### 3.5 Storage, review, accept (`core/mapping_proposals.py`)

- `connector_mapping_proposal` (RLS FORCE; the last 20 per connector). Audit
  `connector.mapping.proposal.created {proposer, inputs_hash, kinds, sample, entities, ops, llm_calls,
  notes}` / `.discarded`.
- Accept = the admin saves an (edited) draft with `PUT …/mappings/{kind} {…, proposal_id}`:
  `connector_mapping.origin = proposal` + `proposal_id`; the proposal becomes `accepted` with the
  version; audit `connector.mapping.update {origin, proposal_id, accepted, edited}` (fields / ops kept
  as drafted vs edited). From-pack installs record `origin = pack`.

### 3.6 Four-eyes on proposed action mappings

Saving an **action** mapping with a `proposal_id` is a guarded change: a tenant-admin gets `202` +
change request `connector_mapping` (reason `ai_proposed_action_mapping`, payload `{connector, kind,
mapping, proposal_id}`, diff vs the current mapping, `base_version` = the current version or `"0"`).
A different tenant-admin or a platform-admin approves; the handler re-runs `put_mapping(…,
cosigned=True)` (origin `change_request`; a moved version → 409 `stale_base`; a pruned or discarded
proposal no longer blocks it). Platform-admins apply directly (audited `applied_by_platform_admin`).
Read mappings from proposals and manual action mappings stay unguarded (M7 rule).

---

## 4. Admin API (engine, under `/api/admin`; session + CSRF + Origin on mutations, tenant-admin / platform-admin)

| Route | |
|---|---|
| `POST /connectors/{slug}/mappings/read/compile {mapping?}` | `MappingPlan`; 422 `mapping_invalid`; catalogue problems as warnings; no vendor call |
| `POST /connectors/{slug}/mappings/read/preview` (`ReadPreviewRequest`) | `ReadPreview`; live: 409 `connector_inactive`; staged: 404 `no_staged_data`, `capped` at `min(max_items_per_source, MAPPING_PREVIEW_MAX_ITEMS)`; 422 catalogue problems; ≤ 2 per process (429 `preview_busy`); audit `connector.mapping.preview {source, engine, ok, capped}` |
| `POST /connectors/{slug}/mappings/read/test` (M7) | unchanged request; answered by the compiler (live preview, `ReadPreview`) |
| `GET /connectors/{slug}/staged` | `{connector, snapshot_id, status complete|failed, mapping_version, mapping_hash, sources[], pages, items, bytes, fetched_from, fetched_to}`; 404 `no_staged_data` |
| `GET /mappings/proposer` | `{mode auto|heuristic|off, enabled, alias, llm {available, reason disabled|pro_alias|no_key|replay_only|null, message}}` |
| `POST /connectors/{slug}/mappings/proposals` (`MappingProposeRequest`) | 201 `MappingProposal`; 404, 409 `provisioned_connector` / `connector_inactive` / `proposer_disabled`, 429 `proposal_busy`, 502 `<crawl error code>` (session failed), 504 `proposal_timeout` |
| `GET /connectors/{slug}/mappings/proposals?limit=1..20` | `{items: [{id, status, proposer, model_alias, kinds, created_by, created_at, entities, complete_entities, lookups, ops, notes, accepted}]}` |
| `GET /connectors/{slug}/mappings/proposals/{id}` | `MappingProposal` (404 unknown / not a uuid) |
| `POST /connectors/{slug}/mappings/proposals/{id}/discard` | `MappingProposal` (409 `proposal_decided`) |
| `PUT /connectors/{slug}/mappings/{kind} {base_version, mapping, proposal_id?}` (M7) | + provenance; action + proposal → 202 change request (tenant-admin); 404 `proposal_not_found`, 409 `proposal_kind` / `proposal_discarded` |
| `GET /connectors/{slug}/mappings/{kind}[/versions]` (M7) | rows gain `origin`, `proposal_id` |

`routes_admin_mappings.router` is registered **before** `routes_admin_m7.router` (its
`GET …/mappings/{kind}` would otherwise shadow `GET …/mappings/proposals`).

## 5. Web (keelson-web admin)

Mappings page: **Propose mappings…** (`MappingProposalPanel`; `?propose=1` opens it; a call to action
on the connector page for tier-A connectors without a read mapping and in the wizard after
activation) — kinds, sampling (disabled with the reason while inactive), AI (disabled with the
engine's reason), the result headline ("AI unavailable: rule-based suggestions."), notes, entity / op
cards (read, confidence as percentage + word with a tone, origin Rules / AI / Rules + AI,
column | expression | confidence | why | alternatives, missing required columns, include boxes),
earlier proposals, discard. **Open … draft in editor** injects YAML with a trailing comment per field
(`85% · AI · why`; vendor / model text only ever inside comments, one line, control characters
removed) and missing required columns as `null` (the contract check then points at them); saving
sends `proposal_id`; an action draft shows *Requires a second admin* and *Send for approval* → 202 →
banner + Pending changes link. **Explain** (`MappingPlanView`, SQL on demand) and **Preview last
crawl** (staged; disabled with a note without staged data) next to *Test with one page* (live); the
preview shows per-column statistics and check examples. `lib/admin/mappings.ts` knows the M8
functions, aggregates (only whole, only when grouped), record lookups and the preview superset.

## 6. Data and migrations

Engine `0011_m8_mapping_compiler.sql` (re-runnable): `scn_raw_set` (PK `(tenant, snapshot_id)`, FK
`scn_snapshot (tenant, id)` and `connector (tenant, slug)` ON DELETE CASCADE, status check, index
`(tenant, connector, created_at DESC)`), `scn_raw_page` (PK `(tenant, snapshot_id, source_key,
page_no)`, FK → `scn_raw_set` cascade, `source_key ~ '^[0-9a-f]{16}$'`, `page_no >= 0`),
`connector_mapping_proposal` (uuidv7 id, kinds `text[]`, proposer / status checks, base / accepted
versions, `proposal jsonb`, FK connector cascade, index), `connector_mapping.origin` (`manual | pack |
proposal | change_request`, default `manual`) + `proposal_id` (no FK: proposals are pruned),
`admin_change_request` kind check + `connector_mapping`; RLS ENABLE + FORCE + `tenant_isolation` on
the three new tables. No other service has a migration.

Size: raw staging is ~50–120 MB jsonb per crawled connector at `/mcp-lite` demo scale (≈ 300k rows /
1,500 pages; one set kept, two transiently); `SCN_RAW_STAGING=0` is the escape hatch.

## 7. Configuration (env; `MAPPING_*`, `SCN_MAPPING_ENGINE`, `SCN_RAW_*`, `DUCKDB_*` per 120 §2)

| Env | Default | |
|---|---|---|
| `SCN_MAPPING_ENGINE` | `duckdb` | `interpreter` = the M7 path |
| `SCN_RAW_STAGING` | `1` | `0` = no staged previews |
| `SCN_RAW_MAX_BYTES` | 512 MiB | per crawl; `too_many_rows` beyond |
| `DUCKDB_MEMORY_LIMIT_MB`, `DUCKDB_THREADS` | `256`, `2` | compose: engine-api `128` |
| `MAPPING_PREVIEW_MAX_ITEMS` | `20000` | per source, staged preview |
| `MAPPING_PROPOSER` | `auto` (compose `heuristic` while credits are depleted) | `auto` / `heuristic` / `off` |
| `MAPPING_PROPOSER_ALIAS` | `fast-default` | Pro refused |
| `MAPPING_PROPOSE_MAX_CALLS`, `MAPPING_PROPOSE_SAMPLE_SIZE` | `24`, `20` | sampling budget; caps ≤ the code constants `mapping_profile.MAX_CALLS` / `SAMPLE_SIZE` |
| `MAPPING_PROPOSE_TIMEOUT_S`, `MAPPING_PROPOSE_LLM_TIMEOUT_S` | `120`, `60` | |
| `MAPPING_PROPOSE_MAX_PROFILE_CHARS` | `24000` | cap ≤ the code constant `mapping_proposer.MAX_PROFILE_CHARS` |

Determinism (120 §3.5): the sampling budgets (calls, items per page, model data block) are code
constants; their env settings are safety caps that may only lower them (settings validation refuses a
higher value) and a lowered cap that bites leaves a `sampling_truncated` note on the proposal. The
rule-based proposer, confidence values and merge rules are code constants; nothing affects plans.
Compose: `MAPPING_PROPOSER: ${MAPPING_PROPOSER:-heuristic}` in the engine env anchor, engine-api
`DUCKDB_MEMORY_LIMIT_MB: "128"` (mem_limit unchanged, duckdb imported lazily). Engine dependency
`duckdb==1.5.5` (cp314 manylinux wheel; ≈ 60 MB image growth); pyarrow was already a dependency.

## 8. Security

- RLS FORCE on `scn_raw_set`, `scn_raw_page`, `connector_mapping_proposal` (cross-tenant reads see
  nothing, foreign-tenant inserts are refused — tested).
- DuckDB is in-process, per job, one tenant's rows, external access off, configuration locked, no
  extensions (tested: `read_csv('/etc/passwd')`, `COPY … TO`, `INSTALL`, `SET` after the lock fail).
  SQL only from generated identifiers and escaped constants (tested with injection payloads); no
  `eval` / `exec` / `compile` in any M8 module (static test).
- Untrusted text: only trusted read tools are sampled; values become shapes; descriptions are
  screened and fenced; the model's output is strict and re-validated; `injection_suspected` discards
  it; nothing is executed; no write tool is called while proposing (tested: the fake's write log stays
  empty), dry-run stays render-only. Sampled codes and schema shapes do reach the model provider
  through LiteLLM (tenant key) — `sample: false` or `MAPPING_PROPOSER=heuristic` avoid it.
- Proposals are drafts; AI / rule-proposed action mappings need four-eyes; provenance is recorded
  (`origin`, `proposal_id`, `inputs_hash`, `mapping_hash` on crawled snapshots).
- Outbound calls: vendor calls through the egress proxy (`session_caller`); LLM calls through LiteLLM
  only. No URL or `resource_link` in results is followed.

## 9. Tests

| Suite | What |
|---|---|
| contracts-py `test_m8_mapping.py` | grammar of the new functions (constant table / path, arity), aggregates only whole and only with `group_by`, group-key path rule, `group_by` × `dedupe`, record vs value tables, chains, record sources in `read_mapping_violations`, M7 dumps unchanged, `SCN_FOREIGN_KEYS` name real columns / kinds and its errors are the M3 checks, plan / preview models, identities |
| contracts-py `test_m8_mapping_proposal.py` | `strict_schema_violations(MappingProposalLlm) == []`, every violation class, `llm_args`, drafts (complete entities, used lookups), request validation |
| contracts-ts `m8-mappings.test.ts` | schema exports, function / aggregate tables, enums, confidence helpers, example parses |
| engine `test_m8_mapping_compiler.py`, `test_m8_compiler_differential.py` | (earlier commits) every function / aggregate over a variant matrix incl. lazy errors, joins, grouping, dedupe, duplicate / FK classification, hardening, literal safety, no-eval; 6,000 seeded random expressions + the M7 fuzz + the pack mapping + mutated item sets agree with the interpreter (values, nulls, error codes and messages, row order) |
| engine `test_m8_mapping_heuristics.py` | catalogue screen (flagged descriptions, untrusted tools hidden), sampling (trusted reads only, no writes, probe, budget, failures as codes, admin sources, schema-only), shapes, the pack reproduction (≥ 75 %, key columns exact), the draft crawls with both engines, action draft vs the pack, idempotency clamp, no read-back → no op |
| engine `test_m8_mapping_proposer_llm.py` (FakeLlm) | merge (both / alternatives / caps), dropped items, idempotency from the classification, injection discard, error / timeout / replay miss / malformed → fallback with ≤ 2 calls, Pro alias refused, vendor injection text never in the model request, data-block cap |
| engine `test_m8_crawl_duckdb.py` (Postgres + FastMCP fake) | both engines load identical rows (fewer calls), crawl jsonb, raw-set retention (current + latest failed), `mapping_failed` / duplicate / FK / byte cap write nothing and keep the previous snapshot, truncation, explain, live / staged previews (connector disabled, no vendor call, caps, column stats, FK examples, `not_staged`), raw-page RLS |
| engine `test_m8_mapping_proposals_api.py` | rules-only proposal + read acceptance (origin, accepted version, audit, the draft crawls), refusals (planner 403, other tenant 404, CSRF, 422, `proposer_disabled`, `proposal_busy`, `connector_inactive`), schema-only, discard / decided / retention, four-eyes (202, self-approval 403, second admin applies, `stale_base`, platform-admin direct), AI-refined proposal through the API (FakeLlm) |
| engine `test_migrations_rls.py` | the three tables are RLS FORCE |
| web `m8-mappings.test.tsx` | see §5; axe on the panel and the plan |
| e2e `api_m8_mappings_offline.py` | real stack, no model: propose (rules), accept a read draft, explain, compiled crawl = Merxa totals, staged preview with the connector disabled, four-eyes on demo-retail — written and syntax-checked here, run by the integrator |

## 10. Deviations (from `120` and the design brief)

- **None from 120's allocations** (migration 0011, env prefixes, module ownership, e2e name). The
  brief proposed `docs/contracts/120-m8-mappings.md`; 120 allocates this file.
- `MappingProposal.status` has no `failed`: a proposal that cannot be made answers an HTTP error and
  is not stored. `proposer` is `heuristic | llm+heuristic` (an LLM-only proposal does not exist: the
  rules always run first).
- `SourceProfile` carries the detected paging as a ready `ReadSource` (plus `variant`) instead of
  loose paging fields; `PathProfile.date_format` added (non-ISO dates → `date($.x, fmt)`).
- Get-by-key **probes** (one read call per get tool) were added: without them a proposed read-back
  could not know where `get_item` holds the record.
- `GET /api/admin/mappings/proposer` (availability for the panel) and `GET …/staged` summaries are
  plain JSON, not contract models. The staged / live choice is two buttons (*Preview last crawl* /
  *Test with one page*) rather than a toggle.
- Proposal discard of a decided proposal answers 409 `proposal_decided`; an approved change request
  applies even when its proposal was pruned or discarded meanwhile.
- The `/mcp-lite` test fake's `list_items` now carries Merxa's real parameter description ("one of
  sku, style, …") and bounds, which the sampler reads.

## 11. Deferred

- `foreign-erp-lite` pack rewritten to record lookups + `group_by` clusters (the pack keeps its M7
  source lookups and `dedupe` clusters; both compile and the M7 e2e counts are unchanged).
- Remap from the staged raw set without a re-crawl (`POST …/crawl {from_staged}`).
- Full-data preview as a worker job (engine-api previews stay capped at `MAPPING_PREVIEW_MAX_ITEMS`).
- Recorded replay fixtures / a live check of the LLM proposer (`scripts/verify-live.sh`; needs
  credits) and an optional guard-model screen of descriptions.
- Playwright `m8-mappings.spec.ts` (propose → open in editor → preview → save).
- `SCN_FOREIGN_KEYS` driving the tier-B Postgres `FK_CHECKS`; proposals for tier-B / CSV adapter
  sidecar sources.
- An asynchronous proposal job (the request is synchronous, ≤ 120 s).

## 12. Known issues / limits

- The four-eyes guard can be bypassed by copying a draft into the editor without its `proposal_id`:
  the admin then becomes its author (audited as a manual change) — accepted, as in the brief.
- Change-request payloads are stripped of credential-like keys (`change_requests.strip_secrets`): an
  action mapping whose argument *names* look like credentials (`token`, `api_key`, …) would be
  applied without them and then fail re-validation; such argument names never carry templates of
  ours but a vendor could use them.
- Proposal and preview concurrency caps are per engine-api process. The M7 `…/read/test` route, now
  answered by the same compiled live preview, is not counted against the preview cap (it keeps its M7
  answers, no new 429); a live test maps at most 3 pages per read, so its DuckDB session stays small.
- A staged preview holds the validated rows of each entity mapping in engine-api memory for its
  column statistics (bounded by `MAPPING_PREVIEW_MAX_ITEMS` per read); at `/mcp-lite` demo scale the
  cap applies. Moving full-data previews to a worker job is deferred (§11).
- Generic-update proposals (`po_line.reschedule` via `update_item`) may not match the vendor's
  business rules: low confidence, a warning note, four-eyes and the dry-run are the mitigations.
- A synchronous proposal with the AI step can take up to `MAPPING_PROPOSE_TIMEOUT_S`; proxies in
  front of engine-api must allow it.
- The heuristic's profile is one page per read: a field that is null on the whole sampled page is
  `empty` and proposed with lower confidence (the preview shows the effect before saving).
