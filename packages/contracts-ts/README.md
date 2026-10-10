# @keelson/contracts

Keelson's wire contracts for the web app:

- `schemas/` — JSON Schemas (draft 2020-12) **generated** from the Pydantic models in
  `packages/contracts-py` (`keelson_contracts`). Not owned by this package's builder; do not edit.
- `src/generated/*.ts` — Zod 4 modules **generated** from those schemas by `scripts/gen-zod.mjs`
  (json-schema-to-zod 2.8.1, `zodVersion: 4`). Committed for reproducible builds.
- `src/blocks.ts`, `src/agui.ts` — friendly exports and the AG-UI parser.

## API

```ts
import {
  // render blocks
  blockEnvelopeSchema, toBlockEnvelope, type BlockEnvelope,      // lenient header (props: unknown)
  knownBlockEnvelopeSchema, type KnownBlockEnvelope,             // strict generated union
  blockPropsSchemas, parseBlockProps, blockKey, BLOCK_KEYS,      // per kind@v props validation
  kpiStripPropsSchema, type KpiStripProps, type KpiItem,         // …and approachCard, recommendedOption,
                                                                 //   planDiff, approvalCard, commitTracker,
                                                                 //   narrative, evidencePack
  networkMapPropsSchema, networkMapDataSchema,                   // M3 canvas visuals: props + data_ref
  impactGraphPropsSchema, impactGraphDataSchema,                 //   payloads, scenarioComparePropsSchema;
  stockoutTimelinePropsSchema, stockoutTimelineDataSchema,       //   types NetworkMapData, MapLink, Scorecard…
  blockDataSchemas, parseBlockData, CANVAS_ONLY_KINDS, fromE6,
  // M4 (docs/contracts/90): approach-card checks/agents/editable, unconfirmed values, actions
  checkSentence, checkTaskId, type ApproachCheckItem, type ApproachCheck, type ApproachAgent,
  type EditableValue, type Assumption, type OptionAction, type OptionActionType, type UnconfirmedValue,
  EVENT_TYPE_KEYS, type EventTypeKey,                            // taxonomy event types (intake enum)
  PROVENANCE_KINDS, VALUE_UNITS, type Provenance, type ValueUnit,
  // AG-UI 1.0 events
  parseAgUiEvent, type ParseAgUiEventResult, type AgUiEvent, EventType, agUiEventSchema,
  renderBlockFromEvent, interruptsFromEvent, caseUpdateFromEvent,
  runIdOfEvent, errorIdFromEvent,                                // run attribution / support reference
  scMetadataSchema, type ScMetadata, interruptSpecSchema, type InterruptSpec,
  STAGE_IDS, STAGE_PHASES, isStageId, type StageId, AGENT_DISPLAY_NAMES,
  ACTIVITY_TYPE_RENDER, CASE_UPDATED_EVENT, KEELSON_EVENT_TYPES,
  // everything generated (incl. llm.* and run.request)
  generated,
} from "@keelson/contracts";
```

### `parseAgUiEvent(json)`

Accepts an SSE `data:` string or a decoded object; never throws.

```ts
const r = parseAgUiEvent(e.data);
if (!r.ok) console.warn(r.code, r.message);          // "invalid_json" | "invalid_event" | "invalid_sc"
else {
  r.event;  // typed AG-UI 1.0 event (discriminated on `type`)
  r.sc;     // Keelson metadata.sc (validated) or null when absent
  const block = renderBlockFromEvent(r.event);       // ACTIVITY_SNAPSHOT{activityType:"sc.render"} → envelope
  const interrupts = interruptsFromEvent(r.event);   // RUN_FINISHED{outcome:interrupt} → InterruptSpec[]
}
```

Validation uses the official `@ag-ui/core` **1.0.2** Zod schemas. Verified against the installed
package: the event union lives in the **`@ag-ui/core/schemas`** subpath and is exported as
`EventSchema` with the alias **`EventSchemas`** (a `z.discriminatedUnion("type", …)` over all 31
1.0 events incl. `SUBAGENT_*`, `ACTIVITY_SNAPSHOT`, `REASONING_*`; objects are `looseObject`).
`@ag-ui/core` imports `zod/v4`, which resolves to this package's zod 4.6.5. Events whose
`metadata.sc` is present but malformed are rejected (`invalid_sc`), e.g. an unknown stage or
snake_case `tenant_id`.

`metadata.sc.runId` (optional, `null` when absent) names the run every engine event belongs to;
`runIdOfEvent(event, sc)` returns `RUN_STARTED`/`RUN_FINISHED`'s own `runId`, else `sc.runId`
(AG-UI 1.0 `RUN_ERROR` has no `runId`). `errorIdFromEvent(event)` reads `metadata.errorId` of
`RUN_ERROR` / `SUBAGENT_ERROR` (opaque id shape only). The generated `sc` schema is `.strict()`:
regenerate (`generate`) whenever contracts-py adds a field, or every new event is `invalid_sc`.

`BlockEnvelope` is deliberately lenient (`props: unknown`, tolerant title/placement/provenance)
so one bad or newer block never breaks the thread; `@keelson/viz`'s `BlockRenderer` validates
`props` per `kind@v` and falls back to a raw-data card.

## Regenerate / check

```bash
pnpm --filter @keelson/contracts generate   # schemas/*.schema.json → src/generated/*.ts
pnpm --filter @keelson/contracts build      # prebuild regenerates, build runs --check
pnpm --filter @keelson/contracts test       # generated output in sync + parser/blocks tests
```

The JSON Schemas themselves are regenerated from contracts-py:

```bash
MSYS_NO_PATHCONV=1 docker run --rm --memory 1500m \
  -v D:/primark:/work -v keelson_uvcache:/root/.cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy -e PYTHONDONTWRITEBYTECODE=1 \
  -w /work/packages/contracts-py ghcr.io/astral-sh/uv:python3.14-trixie-slim \
  sh -c "uv sync --frozen && uv run python scripts/export_schemas.py"
```

## Schema files

`schemas/index.json` lists every schema (`name`, `file`, `title`, Python `source`).

| file | describes |
|---|---|
| `block.envelope.schema.json` | any render block: `{id, kind, v, title, placement, provenance, data_ref, props}`, a union over `kind@v` |
| `block.provenance.schema.json` | the envelope's `provenance` |
| `block.<kind>.v1.props.schema.json` | props for `kpi_strip`, `approach_card`, `recommended_option`, `plan_diff`, `approval_card`, `commit_tracker`, `narrative`, `evidence_pack`, `network_map`, `impact_graph`, `stockout_timeline`, `scenario_compare`, `remediation_checklist` (M5), `alert_origin` (M6, "Opened from alert" card) |
| `block.<kind>.v1.data.schema.json` | the `data_ref` payload (`GET /api/blocks/{id}/data`) of `network_map`, `impact_graph` (<= 300 nodes), `stockout_timeline` (<= 40 rows) |
| `agui.sc-metadata.schema.json` | `metadata.sc` on every AG-UI event (camelCase) |
| `agui.interrupt.schema.json` | one entry of `RUN_FINISHED.outcome.interrupts` |
| `run.request.schema.json` | engine run envelope (internal; shared for completeness) |
| `policy.*.schema.json` | M5 approval policy: `rule`, `set`, `context` (CEL `{cs, case, initiator}`), `decision`, `quorum` (approval progress) |
| `demo.reset-*.schema.json` | M5 `POST /api/demo/reset` request + step log |
| `signals.*.schema.json` | M6 Signal Hub: `signal`, `event`, `exposure`, `exposure-summary`, `alert`, `alert-detail` (drawer), `alert-status-update`, `alert-rule`, `scenario-pack`, `tenant-network`; friendly `ALERT_SEVERITIES`, `ALERT_STATUSES`, `SIGNAL_KINDS`, `NODE_TYPES`, `exposedSites()` from `src/signals.ts` |
| `admin.*.schema.json` | M7 back-office: `change-request` (four-eyes queue item), `change-request-decision` (approve/reject body), `user`, `user-create`, `user-created` (one-time password setup, never a password), `user-patch` |
| `agents.*.schema.json` | M7 `custom-specialist` (builder form), `playground-request`, `playground-result` |
| `mapping.*.schema.json` | M7 tier-A `read` / `action` mappings (editors) and `dry-run-request` / `dry-run-result` |
| `scn.snapshot-label`, `commit.ledger-entry`, `connectors.capabilities` | M7 snapshot consistency (`consistent` / `crawled` + `as_of_range {from, to}`), per-op commit ledger line, connector capability summary (sync / commit mode / advisory) |
| `llm.*.schema.json` | LLM structured outputs (`IntakeResult`, `Findings`, `Recommendation`, M4 `InvestigationPlan`, `Critique`, `FollowupClassification`, `GuardVerdict`, `UntrustedExtraction`, `ScenarioUpdate`, …) |

Shape guarantees relied on by the generator: self-contained (no `$ref`), unions are `anyOf`
with `const` discriminators, closed objects (`additionalProperties: false` → `.strict()`),
integers are `type: integer`, deterministic output. Fixed-length tuples (`[lon_e6, lat_e6]`,
`bbox`) are 2020-12 `prefixItems`; json-schema-to-zod 2.8.1 predates them, so the generator
rewrites closed `prefixItems` into the draft-07 `items: [...]` form (`legacyTuples`) and emits
`z.tuple([...])`. Map coordinates are integer degrees x 1e6: use `fromE6()` before handing them
to MapLibre/deck.gl. Python-side cross-field rules (edge/link endpoints exist, `short` cells
have `shortage_units > 0`, ...) are not in the Zod schemas; the engine validates them. The generator fails if a schema would
convert to `z.any()` without an explicit open object.

## M8 notes — signal clustering (`docs/contracts/123-m8-signal-clustering.md`)

- New schemas `signals.event-membership`, `signals.clustering-config`,
  `signals.clustering-config-update`, `signals.clustering-status` (exported from the package root as
  `signalsEventMembershipSchema`, `signalsClusteringConfigSchema`,
  `signalsClusteringConfigUpdateSchema`, `signalsClusteringStatusSchema` + their types);
  `signals.alert-detail` gains an optional `memberships` array (absent from older hubs).
- `src/signals.ts`: `MEMBERSHIP_METHODS` / `MembershipMethod` (from the generated enum).
- The per-method evidence rules and "memberships belong to the event" are Python-only cross-field
  checks; the web parses memberships tolerantly (one bad entry is dropped, not the alert).
## M8 notes — Evals UI (`docs/contracts/128-m8-evals-ui.md`)

- Generated `evals*Schema` modules (`evals.status`, `evals.run`, `evals.run-detail`,
  `evals.run-create`, `evals.run-event`, `evals.case-result`, `evals.compare`, `evals.trends`,
  `evals.threshold`, `evals.suite`). Units: `*_bp`, `*_ms`, integer `cost_microusd`, integer tokens.
- Friendly vocabularies in `src/evals.ts`: `EVAL_MODES`, `EVAL_LIVE_MODES`, `EVAL_RUN_MODES`,
  `EVAL_RUN_STATUSES`, `EVAL_CASE_STATUSES`, `EVAL_METRICS` + `EVAL_METRIC_META` (label, unit, better
  direction), `CONFIRM_LIVE_TEXT` (`RUN LIVE`), `EVAL_STOP_CODES`; UI pre-checks `isLiveMode` and
  `evalRunCreateProblem` (mirror of contracts-py `eval_run_create_error`; the engine stays
  authoritative). The CLI results file (`keelson-evals/1|2`) has no generated schema: the engine
  converts it with a sanitising converter, so clients only do light early checks.

## M7 notes (`docs/contracts/110-m7-backoffice-generic.md`)

- Friendly vocabularies in `src/backoffice.ts` (derived from the generated schemas): `PRODUCT_ROLES`,
  `CHANGE_REQUEST_STATUSES`, `CHANGE_REQUEST_KINDS`, `GUARD_REASON_CODES`, `COMMIT_LEDGER_STATES`,
  `SNAPSHOT_CONSISTENCIES`, `COMMIT_MODES`; constants `CRAWLED_ADVISORY_TEXT`,
  `MAPPING_EXPRESSION_FUNCTIONS` (editor hints), `CHANGE_REQUEST_TTL_MS`; UI pre-checks
  `changeRequestExpired`, `canApproveChangeRequest` (self-approval impossible) and `isProModel` (same
  rule as contracts-py `is_pro_model`: `pro` bounded by non-letters, e.g. `gemini-1.5-pro002`). The
  engine stays authoritative for every rule (cross-field checks are Python-only). `user_roles` change
  requests (`admin_role_granted`) appear when a tenant-admin grants `tenant-admin`.
- Additive block props (default `null` when absent, so older blocks parse unchanged):
  `recommended_option@1`, `scenario_compare@1` and `narrative@1` `advisory` (`BlockAdvisory`
  `{reason: "crawled_snapshot", text, as_of_range: {from, to}}`) — show it as a label on plans built
  on crawled (tier-A) data.
- `user-created.setup.link` carries a pattern (http(s) URL with a plain host), so `javascript:` and
  userinfo links are rejected before the one-time link is rendered.
- Mapping expressions and templates are validated by contracts-py (`keelson_contracts.mapping`);
  the Zod schemas only check sizes/patterns, so editors should surface the engine's 422 messages.

## M5 notes (`docs/contracts/95-m5-remediation.md`)

- New block `remediation_checklist@1` (`remediationChecklistPropsSchema`, `ChecklistStep`,
  `checklistCompleted(props)` = every step verified or skipped; a manual `done` still awaits read-back).
  `deep_link` carries a pattern (http(s) URL with a plain host, or a same-origin `/path`), so the
  schema rejects `javascript:`, `data:`, protocol-relative and userinfo links before a renderer
  puts them in an `href`. Open links with `target="_blank" rel="noopener noreferrer"`.
- `approval_card@1.rejection.reason` is 1-500 characters or `null` (same bound as the decision note).
- Additive props (defaults `null` / `[]` when absent, so older blocks parse unchanged):
  `approval_card@1` `quorum` (`ApprovalQuorum`, `quorumProgress(q)` → `{approved, needed, met}`),
  `rejection`, `bundle`; `commit_tracker@1` status `rolled_back`, `actions` (`undo` → case action
  `{type: "rollback", change_set_id}`), `conflicts`; `recommended_option@1` `remediation_mix
  {apply_lines, guide_lines}`; `narrative@1` `banner` (`ReplanBanner`) + `plan_versions`.
- Case actions add `{type: "rollback", change_set_id}` and `{type: "checklist_step", checklist_id,
  seq, status: "done" | "skipped"}`; the approval `responseSchema` accepts an optional `reason`.
- Admin/API schemas: `policyRuleSchema`, `policySetSchema`, `policyContextSchema`,
  `policyDecisionSchema`, `policyQuorumSchema`, `demoResetRequestSchema`, `demoResetResultSchema`.
- Consumers with exhaustive maps over `CommitTrackerProps["status"]` must add `rolled_back`.

## M4 notes (`docs/contracts/90-m4-investigation.md`)

- `approach_card@1.checks` entries are `string | {task_id, sentence}` (M1 cards carry strings, M4
  cards objects). Render with `checkSentence(c)`; `checkTaskId(c)` is `null` for strings.
- New M4 list fields (`agents`, `editable`, `unconfirmed`, `Assumption.source_evidence_ids`) and
  flags (`what_if`, `Assumption.unconfirmed`) default to `[]` / `false` when absent, so pre-M4
  blocks parse unchanged. `narrative@1.what_changed` defaults to `null`.
- `OptionActionType` adds `confirm_assumptions` and `promote`. A `confirm_assumptions` case action
  posts `{type, option_id, plan_version, assumption_keys}`; an approach **Edit** resumes with
  `edits: [{key, value_int | value_ids}]` (or free text) and optional `removed_checks`.
