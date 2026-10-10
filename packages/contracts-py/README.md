# keelson-contracts

Pydantic v2 models shared by Keelson services (engine, optimizer) and exported as JSON Schema
for the web app (`packages/contracts-ts/schemas`). Spec: `docs/contracts/00-overview.md`,
`10-auth.md`, `20-merxa.md`, `25-scn-1.0.md`, `30-engine-api-events.md`, `40-optimizer.md`,
`50-engine-workflow.md`, `80-m3-deterministic-core.md` (§2, §3, §5), `90-m4-investigation.md`,
`95-m5-remediation.md`, `100-m6-signals.md`, `110-m7-backoffice-generic.md`.
Merxa (the mock third-party system) must never depend on this package.

Runtime dependency: `pydantic==2.13.5` only. Optional extra `agui` (`ag-ui-protocol==1.0.0`) turns on
validation of every built AG-UI event against the official models.

## Modules

| module | contents |
|---|---|
| `common` | `Provenance` (+ `ProvenanceStamp` with `verified`), money/units (`Cents`, basis points, `apply_bp`, `eur_to_cents`), ISO weeks (`iso_week_of`, `monday_of`, `add_weeks`, `IsoWeek`), `canonical_json` / `sha256_hex`, ids (`new_evidence_id` → `ev_…`, `new_block_id` → `blk_…`, `stable_block_id`, `new_run_id` → UUIDv7), shared `EntityRef` |
| `blocks` | `BlockEnvelope` (union discriminated on `kind@v`), typed props for `kpi_strip`, `approach_card`, `recommended_option`, `plan_diff`, `approval_card`, `commit_tracker`, `narrative`, `evidence_pack` and (M3, canvas-only) `network_map`, `impact_graph`, `stockout_timeline`, `scenario_compare` (all `@1`); `BLOCK_PROPS_REGISTRY`, `BLOCK_DATA_REGISTRY` (`data_ref` payloads: `NetworkMapData`, `ImpactGraphData`, `StockoutTimelineData`), `approach_edit_violations` (typed Edit vs its card), `make_block`, `parse_block`, `parse_block_data`, `block_to_wire`, `block_data_to_wire`; `M1_BLOCK_KEYS`, `M3_BLOCK_KEYS`, `BLOCK_KEYS` |
| `agui` | `ScMetadata` (camelCase), `Stage` + `STAGE_LABELS` + `NODE_STAGES`, `AGENT_DISPLAY_NAMES`, AG-UI 1.0 event builders (`run_started`, `run_finished_success/interrupt`, `run_error`, `step_*`, `subagent_*`, `tool_call_*`, `reasoning_*`, `activity_snapshot`, `text_message_*`, `custom`, `case_updated`), `validate_event`, `with_seq`, `sse_frame` |
| `runs` | `RunRequest` (payload typed by `kind`; `ActionPayload` incl. M4 `confirm_assumptions` + `assumption_keys`, `ResumePayload` with typed `ApproachEdit`s + `removed_checks`; client-decision lists capped at `MAX_PAYLOAD_KEYS`, ids at `MAX_EDIT_IDS`), `Actor`, `CaseCursor`, `adk_app_name`, Valkey stream keys |
| `llm` | `IntakeResult`, `DisruptionExtract` (`event_type` = taxonomy enum), `EntityMention`, `EntityRef`, `Claim`, `Findings` (+ M4 `scenario_updates`), `OptionReason`, `Recommendation`; M4: `ScenarioUpdate`, `InvestigationPlan`/`InvestigationTask`/`PlanBudget` (+ `plan_violations`, `plan_waves`), `Critique`/`CritiqueIssue`, `FollowupClassification`/`ConstraintDelta`, `GuardVerdict`, `UntrustedExtraction`/`UntrustedItem` (+ `untrusted_extraction_violations`, `usable_untrusted_items`), `critique_violations`, `constraint_delta_violations`; `LLM_OUTPUT_MODELS`, `strict_schema_violations()` |
| `scenario` | `Selector`, `Window`, `Perturbation` union (7 primitives, factors in bp, optional p10/p90), `Assumption` (+ M4 `unconfirmed`, `source_evidence_ids`), `DisruptionScenario` (+ `pending_confirmation`, `confirm_assumptions()`), `fingerprint()` |
| `changeset` | typed ops (`M1_OPS` + M3 `po_line.split`, `capacity_booking.create`, `po.create`, `po_line.cancel`, `production_order.resequence` = `TYPED_OPS`; `OP_PAYLOAD_MODELS`) + `GenericOpItem`, `ChangeSetItem`, `ChangeSet`, `change_set_hash()` (= Merxa `preview_hash`), `idempotency_key()` |
| `receipts` | `ApprovalReceiptClaims` (exact `10-auth.md` claim set), `receipt_header()`, constants (`typ approval-receipt+jwt`, `kid ar-1`) |
| `connectors` | `ToolAccess`, `ContentTrust`, `Egress`, `Idempotency`, `ToolClassification`, `ConnectorTier`, `detect_tiers()`; M7 `ConnectorCapabilities` + `summarize_capabilities()` |
| `optimizer` | `/v1/impact`, `/v1/optimize`, `/v1/levers` models (`40-optimizer.md`, `80` §2-§3): `ProblemData` (+ v2 `locations`, `po_lines`, `bom`, `inbound_lanes`, `alt_sources`, `production`, `allocations`), `TARGET_KINDS_BY_OP`, `ImpactRequest.include_graph`, `ImpactResponse.graph/tts/bottlenecks` (`ImpactGraph` <= 300 nodes), the five v1 levers (`LeverSpec` discriminated on `code`; `LEVERS`, `LEVER_OPS`, `lever_catalogue()`, `lever_params_schema()`), `Scorecard`/`CostBreakdown`, typed `LeverDecision`s, `Action` for every lever op with an atomic `group` |
| `policy` | M5 approval policy: `PolicyRule {id, description, when (CEL), require: Requirement \| "auto" \| "refuse", priority}`, `PolicySet {version, rules}` (`ordered()` = ascending priority), `Requirement {roles, count, exclude_initiator}`, typed CEL context `PolicyContext {cs, case, initiator}` (`activation()`), `PolicyDecision`, quorum view `QuorumView {required, approvals, remaining, missing_roles}` + `quorum_assign` / `quorum_role_for` / `quorum_remaining` / `quorum_missing_roles`, `PolicySet.decide` (hard floors) |
| `demo` | M5 `POST /api/demo/reset`: `DemoResetRequest {keep_consents}`, `DemoResetResult` step log, `DEMO_RESET_STEPS`, `CONSENT_STEPS` |
| `signals` | M6 Signal Hub (`100-m6-signals.md`): `DisruptionSignal` + `SourceRef` + `SeverityNative`, GeoJSON `Geometry` (float WGS84), `DisruptionEvent` (+ `EventWindow`, `taxonomy_key_for` / `SUBTYPE_TAXONOMY`), `Exposure`, `ExposureSummary` (`NodeCounts`), `Alert`, `AlertDetail`, `AlertStatusUpdate`, `AlertRule` (`AlertCondition`, `DEFAULT_ALERT_RULES`, `decide_alert_action`, `should_renotify`, `alert_severity_for`), `ScenarioPack` (+ steps), `TenantNetwork`; deterministic ids `signal_id` / `event_id` / `pack_step_id` (uuid5), `combine_confidence_bp` |
| `admin` | M7 four-eyes `AdminChangeRequest` (+ `ChangeRequestKind`, `ChangeRequestStatus`, `GuardReason` codes per kind, `ChangeDiffEntry`, `ChangeApprover`, `CHANGE_REQUEST_TTL` = 72 h), `ChangeRequestDecision`, `change_request_decision_error`; users: `ProductRole` / `PRODUCT_ROLES`, `AdminUser`, `AdminUserCreate`, `AdminUserCreated` (+ `PasswordSetup`, never a password), `AdminUserPatch`, `grantable_roles`, `user_change_violations` |
| `agents` | M7 `CustomSpecialistSpec` (+ `SpecialistToolRef`), `custom_specialist_violations` (approved read tools, aliases, KBs, no Pro), `custom_specialists_violations`, `quarantined_tool_refs`, `is_pro_model`, `is_custom_specialist_slug`, `RESERVED_SPECIALIST_SLUGS`; playground `PlaygroundRequest` / `PlaygroundResult` (+ tool calls, usage, error) |
| `mapping` | M7 tier-A `ReadMapping` (`EntityMapping`, `ReadSource`, `StaticLookup` / `SourceLookup`), `ActionMapping` (`ActionOpMapping`, `Readback`), dry-run `ActionDryRunRequest` / `ActionDryRunResult`; the safe expression grammar (`parse_expression` → AST `PathExpr` / `ConstExpr` / `CallExpr` / `ConcatExpr`, `parse_path`, `Expression`, `JsonPathLite`), argument templates (`template_refs`), `action_mapping_violations`, `unmapped_ops`, `required_scn_columns` |
| `commit` | M7 per-op `CommitLedgerEntry` (`attempted`, `succeeded`, `in_doubt`, `failed`, `verified`; `LedgerReadback`), `ledger_next_action`, `atomic_group_bulk_allowed` |
| `scn` | `scn/1.0` row models per entity (`SCN_ROW_MODELS`, `SCN_COLUMNS` in contract order, `parse_row`, `to_e6`), `SnapshotManifest`, `SnapshotExport`, `ChangesSince`, `SnapshotStatus`, `part_uri_allowed`; M7 `SnapshotConsistency`, `AsOfRange`, `SnapshotLabel`, `CRAWLED_ADVISORY_TEXT` |

Submodules are imported explicitly (`from keelson_contracts.blocks import make_block`).

## Conventions worth knowing

- **Canonical hash**: `sha256(json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False))`.
  `canonical_json` rejects floats (with a JSON-pointer path), non-string keys, integers beyond
  2^53-1 and lone surrogates. The result equals RFC 8785 except when object keys contain characters
  above U+FFFF, because JCS sorts by UTF-16 code unit and Python by code point. The contract's
  `json.dumps` definition is authoritative, and Merxa uses it too.
- **Change-set hash**: five keys per item `{seq, op, target_ref, expected_version, payload}`
  (nulls included), ordered by `seq`. List order does not matter, `seq` assignment does.
  Like Merxa's `preview_hash`, a missing/`null` `payload` hashes as `{}`. `ChangeSet` requires
  `seq` = 1..n so it matches the order Merxa assigns. `idempotency_key` rejects `|` in its inputs.
- **Dates** (`IsoDate`: change-set payloads, scenario windows, block `as_of`) accept only
  `YYYY-MM-DD` strings or `date` objects. Pydantic's lax date would otherwise read
  `1760832000` as a Unix timestamp or truncate datetime strings.
- **Resume runs** carry `decided_by: [{sub, username, name, role?}]` (the shape
  `core.interrupts.record_decision` must write). `approach-…` / `approval-…` interrupt ids
  accept only decisions of their own kind.
- **Receipts**: a `policy:<rule>@<ver>` approval must have `policy_version == <ver>`, and
  `user:` approvals require `approver_name`.
- **LLM schemas** are strict-structured-output compatible: closed objects, every field required
  (nullable = required + `| None`), `Literal` enums, no defaults/patterns/bounds. A test replays
  ADK 2.11's `_enforce_strict_openai_schema` and requires it to be a no-op on our schemas.
- **AG-UI**: events are camelCase dicts with `metadata.sc`. `TOOL_CALL_RESULT` carries the
  `messageId` required by AG-UI 1.0 (default `"<toolCallId>:result"`) and `RUN_STARTED` declares
  `protocolVersion: "1.0"`. `SUBAGENT_*` and the interrupt outcome are native in 1.0.0, so no
  `CUSTOM` fallback is used. Only the roles in `AGENT_DISPLAY_NAMES` may emit `SUBAGENT_*`.
  Events must be strict JSON: `NaN`/`Infinity` raise, even without the `agui` extra.
  `ScMetadata.run_id` (wire `runId`, optional) names the run that emitted the event; the engine
  sets it on every event (incl. `RUN_ERROR`, which has no `runId` field in AG-UI 1.0). It is
  omitted from the wire when unset. Consumers with a strict `metadata.sc` schema (the web's
  generated Zod is `.strict()`) must regenerate from `packages/contracts-ts/schemas` before they
  receive events that carry it.
  `make_block` places `evidence_pack` on the canvas by default.

## M3 additions (backward compatible)

- **Optional means omitted.** Every M3 field on an existing model (the `ProblemData` v2 lists,
  `ImpactRequest.include_graph`, `ImpactResponse.graph/tts/bottlenecks`,
  `OptimizeResponse.scorecard`, `Action.group`) has a default and is left out of
  `model_dump()` while it holds that default (`Field(exclude_if=...)`). An M1 request therefore
  dumps byte-identically and keeps its optimizer `inputs_hash` (the optimizer's golden
  regression still passes).
- **Integers only.** Map coordinates are integer degrees x 1e6 (`lat_e6`/`lon_e6`; positions
  are `[lon_e6, lat_e6]` tuples, `bbox` is `[w, s, e, n]`), money is cents, shares are basis
  points. `scn/1.0` rows keep the float64 `lat`/`lon` of the exchange format; convert with
  `scn.to_e6`.
- **Typed lever ops.** `po_line.split`, `capacity_booking.create`, `po.create`,
  `po_line.cancel` and `production_order.resequence` are typed change-set items now (they
  were `GenericOpItem`s). Every typed payload field is required, so the hash of a typed item
  equals the hash of its wire dict: `change_set_hash` semantics are unchanged (pinned vectors
  in `tests/test_changeset.py` and `tests/test_m3_changeset.py`). Malformed payloads for these
  ops are now rejected when the change set is compiled instead of by Merxa.
- **Actions.** `optimizer.Action` validates the payload by `op` (so `{qty}` is an
  `allocation.set_qty` or a `po_line.change_qty` payload depending on `op`) and checks every
  M3 op against the typed change-set item. `Action.change_set_item()` gives the five hashed
  keys (`group` is never hashed). `OptimizeResponse.actions` must have `seq` 1..n and
  contiguous groups. Every action, `transfer_order.create` included, is checked against its
  typed change-set item, so an action that would not compile (e.g. a transfer with
  `from_dc == to_dc`) is rejected at the optimizer boundary.
- **Additive extras** beyond the `80` text: `InboundLane.alt_lane_code` (fallback lane for a
  closed lane), `ProductionOrder.movable` (default true; false for running/done orders).
  `PoLine.lane_code` is nullable (a line without a booked lane).
- **Size limits.** `ImpactGraph` <= 300 nodes (`MAX_GRAPH_NODES`), `StockoutTimelineData` <= 40
  rows (`MAX_TIMELINE_ROWS`). `network_map`/`impact_graph`/`stockout_timeline` blocks require a
  `data_ref`; all four M3 kinds are canvas-only.
- **data_ref is confined.** On the four M3 kinds a `data_ref` must be the block's own endpoint
  `/api/blocks/{id}/data` (`blocks.block_data_ref(id)`); absolute/foreign URLs or another
  block's endpoint are rejected. `make_block` fills it in for the data-backed kinds.
- **Snapshot downloads (SSRF).** `scn.SnapshotPart.uri` must be an absolute `http(s)` URL with a
  host and no userinfo, fragment, backslash, whitespace or control characters. Before fetching,
  check the host with `scn.part_uri_allowed(uri, ["http://merxa.localhost"])` (scheme + host +
  effective port must equal an allowed origin, i.e. the connector host).
- **Resync flag.** `scn.ChangesSince.full_resync_required` (Merxa sets it on a scenario reset,
  possibly with no change rows) is kept; decide with `ChangesSince.requires_sync`, not
  `bool(changes)`.

## M4 additions (`90-m4-investigation.md`, backward compatible)

- **Taxonomy.** `common.EventTypeKey` / `EVENT_TYPE_KEYS` (`SUP.delay`, `SUP.capacity_loss`,
  `LOG.chokepoint_closure`, `LOG.port_closure`, `LOG.strike`, `NAT.storm`, `NAT.flood`,
  `DEM.spike`, `DEM.drop`, `OTHER`; `EVENT_TAXONOMY_VERSION = 1`) is the single source for the
  `DisruptionExtract.event_type` enum. The engine checks `data/taxonomy/events.yaml` against it;
  `event_family_of(key)` gives the family. `DisruptionScenario.event_type` stays a free string.
  `EntityMention.type` and `EntityRefType` gain `chokepoint` and `region`.
- **New LLM outputs** (all in `LLM_OUTPUT_MODELS`, strict-checked, exported as `llm.*` schemas):
  `InvestigationPlan` (planner), `Critique`, `FollowupClassification` (`EXPLAIN|RESIMULATE|
  REINVESTIGATE` + `ConstraintDelta` with lever codes), `GuardVerdict`, `UntrustedExtraction`,
  `ScenarioUpdate`. Bounds are never in the schemas: use `plan_violations(plan, catalogue)`
  (budget ≤ 4 parallel / ≤ 6 tasks, objective ≤ 200 chars, known specialists, tool allowlists,
  acyclic deps), `plan_waves(plan)`, `ScenarioUpdate.shape_error()` and
  `untrusted_extraction_violations(extraction, evidence_ids)` (quote ≤ 200 chars, mention
  text ≤ 80 chars; an extraction flagged `injection_suspected` is unusable as a whole) /
  `usable_untrusted_items(...)` (what the quarantine wrapper may hand on),
  `critique_violations(critique, plan)` (REVISE names known tasks, PASS names none) and
  `constraint_delta_violations(delta)`. `ScenarioUpdate.shape_error()` also checks value bounds
  (`delay_days ≥ 0`, `duration_days ≥ 1`, `demand_change_pct ≥ -100`), receipt ids
  (`<po_code>#<line_no>`) and `ev_…` evidence ids. A well-formed update is only *fact-backed*
  when `refine_scenario` resolves its pointer in a trusted (non-`untrusted_text`) evidence row
  and the value there supports it; otherwise it is `inference` and needs confirmation.
- **`Findings.scenario_updates`** is required in the schema (strict mode). Stored pre-M4
  findings without the key still validate (read as `[]`).
- **Unconfirmed assumptions.** `Assumption.unconfirmed` (only with prov `inference`/`assumption`)
  and `source_evidence_ids` are omitted from dumps at their defaults, so pre-M4 scenarios keep
  their fingerprint. `DisruptionScenario.confirm_assumptions(keys)` returns a copy with those
  assumptions as `decision`.
- **Blocks** (new fields omitted from dumps at their defaults; pre-M4 wire is byte-identical):
  `approach_card@1` + `agents [{slug, display_name}]`, `editable [{key, label, kind: days|pct|ids,
  value_int, value_ids}]`, and `checks` entries may be `{task_id, sentence}` (M1 strings stay
  valid; `mode` is the what-if badge). `recommended_option@1` + `unconfirmed [{key, label,
  value_text, source}]`, `what_if`, action types `confirm_assumptions` / `promote`; while
  `what_if` or `unconfirmed` is set, an `apply` action must be disabled, `confirm_assumptions`
  needs unconfirmed values and `promote` needs `what_if`. `narrative@1` + `what_changed`
  (one line for follow-ups).
- **Runs.** `ActionPayload.type` adds `confirm_assumptions` (requires unique `assumption_keys`,
  which other types must not carry). `ResumePayload.edits` is free text (M1) or a list of
  `ApproachEdit {key, value_int | value_ids}` (exactly one set, unique keys), plus
  `removed_checks` (task ids); both are only allowed with `decision: "edit"`. Client-decision
  lists are capped (`MAX_PAYLOAD_KEYS = 50`, `MAX_EDIT_IDS = 500`, edit keys ≤ 64 chars). Before
  applying an Edit, check it against the card it answers with
  `blocks.approach_edit_violations(card, edits, removed_checks)` (editable keys and kinds,
  `days ≥ 0`, `pct ≥ -100`, removed checks are card task ids, one check must remain).
- **Stages.** `NODE_STAGES` maps the M4 nodes: `planner`/`followup_delta`/`prepare_resimulate`/
  `prepare_reinvestigate`/`promote_case`/`confirm_assumptions` → understanding,
  `verify_numbers`/`critic`/`refine_scenario` → checking_systems, `explain` → ready_for_review.
  M5: `prepare_replan` → building_options, `prepare_rollback` → preparing_changes, `checklist_step` →
  applying. `STAGELESS_NODES` lists the workflow nodes that deliberately never change the stage
  (routing / turn control / the question path); the engine tests every graph node is in one of the two.
- **Agents.** `AGENT_DISPLAY_NAMES` adds `playbook` ("Playbook analyst") and `planner` ("Planner";
  a row only when the planner's LLM runs).

## M5 additions (`95-m5-remediation.md`, backward compatible)

- **Policy (§1).** Rules are data; this package checks shape only (ids `^[a-z][a-z0-9_-]*$`,
  `when` ≤ 2000 chars without control characters, unique ids *and* priorities so evaluation order is
  unambiguous). CEL is evaluated by the engine over `PolicyContext.activation()`. `PolicyDecision`:
  `requirement` set exactly for `human`; `auto` names its rule (`approved_by` =
  `policy:<rule>@<version>`). `PolicySet.decide(ctx, matches, fallback=, allow_auto=)` is the
  first-match-wins evaluation (`matches(rule)` = the engine's CEL call) with two hard floors no rule
  set lifts: `ctx.must_refuse` (what-if or unconfirmed values) → `refuse`; `auto` rules are skipped
  unless `ctx.auto_allowed` (chat origin, untainted) and `allow_auto` (rollbacks pass `False`).
- **Quorum semantics.** Distinct people, each counted under one required role. When
  `count >= len(roles)` every role must be covered ("one of each role": four-eyes
  `{roles:[approver, compliance], count:2}` is never two approvers); otherwise any role counts.
  `remaining = max(count - approvals, missing roles)`. `quorum_assign(required, [QuorumCandidate
  {sub, name, roles held, at}…], initiator_sub=)` counts recorded approvals by maximum matching, so a
  person holding both roles who approves first never blocks a later single-role approver; it returns
  the `QuorumView` and the subs that do not count (duplicates, the excluded initiator, no required
  role, nothing left to fill), and adding a later approval never un-counts an earlier one.
  `quorum_role_for(required, approvals, sub, roles, initiator_sub)` is the one-step check (missing
  roles first; `None` = cannot count). `initiator_sub` is a required argument everywhere (pass
  `None` only for runs without a human initiator). `QuorumView` validates
  `remaining`/`missing_roles` against the approvals; build it with `QuorumView.build` /
  `with_approval` / `quorum_assign`.
- **Receipts.** `ApprovalReceiptClaims.co_approved_by: ["user:<username>", …]` (≤ 9, unique, never
  `approved_by`, only on `user:` approvals) is omitted while empty: M1 receipts keep the exact claim
  set and hash.
- **Blocks.** New `remediation_checklist@1` (exact `95` §2 shape; `steps` seq 1..n, `progress.done` =
  steps no longer pending, `verified` ⇔ `verified_at`, `evidence_id` only on verified steps,
  `deep_link` = http(s) URL with a plain host or a same-origin path (also a JSON-Schema `pattern`,
  `DEEP_LINK_PATTERN`, so the web's Zod rejects `javascript:` links), `expected.match` = JSON without
  floats; `completed` = all verified/skipped). Additive, omitted at defaults: `approval_card@1`
  `quorum` (roles = `required_roles`), `rejection` (reason ≤ 500 chars), `bundle` members (then `change_set_hash` =
  bundle hash and `summary.lines` = their total); `commit_tracker@1` status `rolled_back`,
  `actions: [{type: undo, …}]` (enabled only while applied/verified) and rollback `conflicts` (only
  while still applied/verified);
  `recommended_option@1` `remediation_mix {apply_lines, guide_lines}` (enabled apply needs
  `apply_lines > 0`); `narrative@1` `banner {text, plan_version, previous_plan_version, signal_id}`
  and `plan_versions [{version, label, status, current}]` (ascending, one current).
- **Runs.** `ActionPayload.type` adds `rollback {change_set_id}` and `checklist_step {checklist_id,
  seq, status: done|skipped}`; `option_id`/`plan_version` are now optional in the schema but
  required for the option actions (`OPTION_ACTION_TYPES`); fields a type does not use are
  rejected; every optional field is omitted while unset (pre-M5 dumps unchanged).
  `ResumePayload.reason` (≤ 500 chars, reject/revise only) and the approval `responseSchema`
  `reason` carry a reject note.
- **Bundles (§5).** `changeset.ChangeSetBundle {id, tenant, change_sets (one per connector, apply
  order), atomic_groups [{group, connector, seqs}]}`: a group appears once, on one connector, with
  adjacent seqs present in that change set. `bundle_hash([(connector, H), …])` = `H` for one member
  (the M1 approval binding is unchanged) else `sha256_hex({"v":1,"change_sets":[{connector,hash},…]})`
  (order bound). Members keep their own `H`, receipt and idempotency key.
- **Schemas.** `policy.rule|set|context|decision|quorum`, `demo.reset-request|reset-result` and
  `block.remediation_checklist.v1.props` are exported for the web.

## M6 additions (`100-m6-signals.md`, backward compatible)

- **Taxonomy.** `NAT.earthquake` added to `EVENT_TYPE_KEYS` (after `NAT.flood`; all other keys and
  their relative order unchanged), `EVENT_TAXONOMY_VERSION = 2`. `data/taxonomy/events.yaml` must
  carry the key too (the engine's startup check compares the sets).
- **Signals (`signals`).** Ids are canonical lowercase UUID strings. `signal_id(connector, native
  key, version)` / `event_id(...)` / `pack_step_id(pack, i)` are `uuid5` over the canonical JSON list
  of their parts in the fixed `SIGNALS_NAMESPACE`. Everything compared or thresholded is an integer
  (`*_bp` 0..10000, `distance_km`, counts). Geometry is GeoJSON (`Point` … `MultiPolygon`,
  `GeometryCollection` of simple geometries; no nesting), positions `[lon, lat]` floats in range,
  closed rings, <= 50 000 positions; it is never hashed. Signal rules: `feed`/`news` signals are
  global (`tenant` null), `connected_system`/`chat` are tenant-scoped, `demo` ⇒ `simulated`,
  `inference` ⇒ `extracted_by`, `fact` ⇒ no `extracted_by`, `supersedes` only on version >= 2.
  `SourceRef.url` is a plain `http(s)` URL (also a JSON-Schema pattern, so Zod rejects
  `javascript:` before the drawer renders a link). `severity_native` is `{scheme, …JSON scalars}`.
  `DisruptionEvent.taxonomy_key` must equal `taxonomy_key_for(subtype)`; the anchor is a member.
- **Alerts.** `alert_severity_for(bp)`: >= 8500 critical, >= 7000 high, >= 4000 medium, else low.
  `decide_alert_action(rules, severity_bp=, confidence_bp=, kind=, counts=)`: no match → `None`;
  a matching `ignore` wins; else the strongest action (`alert_and_draft_case` > `alert`).
  `should_renotify(notified, new, hysteresis_bp)` = rise >= hysteresis (never on an unchanged
  severity). An `opened` alert names its `case_id`; `AlertStatusUpdate` carries `case_id` only for
  `opened`. `AlertDetail` checks that signals/exposures belong to the alert's event and tenant.
  `AlertRule` / `AlertCondition` are frozen with tuple `kinds` / `node_types` (wire shape: JSON
  arrays), so the shared `DEFAULT_ALERT_RULES` cannot be mutated; derive with `model_copy(update=…)`.
  `taxonomy_key_for` also maps `typhoon` / `hurricane` / `cyclone` / `tropical_storm` → `NAT.storm`.
- **Runs.** `SignalPayload` has two shapes: M5 `{signal_id}` (dump unchanged) or M6
  `{event_id, alert_id}` (`alert_id` may be null and is always dumped with `event_id`); exactly one
  of `signal_id`/`event_id`, `alert_id` only with `event_id`; `is_external` tells them apart.
  `event_id` / `alert_id` must be canonical lowercase UUIDs (they go into Signal Hub URL paths, so
  `../`, `?` or `/` never reach `/v1/events/{id}`); the M5 `signal_id` keeps its 1..200-char shape.
- **Blocks.** `alert_origin@1 {alert_id, title, severity, kind, source_names[], simulated,
  opened_at}` (inline; `M6_BLOCK_KEYS`).
- **LLM.** `NewsExtraction {kind, subtype, places[], companies[], ports[], chokepoints[],
  dates_text[], injection_suspected}` (strict; in `LLM_OUTPUT_MODELS`) + `news_extraction_violations`
  (injection ⇒ drop; snake_case subtype; <= 20 entries of 1..80 chars per list).
- **Agents.** `AGENT_DISPLAY_NAMES["external_intel"] = "External intelligence"`.
- **Schemas.** `signals.signal|event|exposure|exposure-summary|alert|alert-detail|
  alert-status-update|alert-rule|scenario-pack|tenant-network`, `llm.news-extraction`,
  `block.alert_origin.v1.props`.

## M7 additions (`110-m7-backoffice-generic.md`, backward compatible)

- **Four-eyes (`admin`).** A guarded PUT/PATCH answers `202` with an `AdminChangeRequest`:
  `kind` = the mutation its handler re-applies (`agent_spec`, `policy_set`, `connector_tool`,
  `tool_binding`, `connector_activate`, `connector_trust`, `embedding_profile`, `connector_delete`,
  `kb_delete`, `user_roles`), `reasons` = `GuardReason {code, field, reason}` (codes must fit the kind,
  `GUARD_REASONS_BY_KIND`), `diff` = RFC 6902-style `{op, path (JSON pointer), before, after}`,
  `payload` = the original body, `base_version` (409 on approve when it moved; required for the
  versioned kinds `agent_spec` / `policy_set`, `VERSIONED_CHANGE_KINDS`). `payload` and `diff` never
  carry credentials (keys / pointer segments like `secret`, `client_secret`, `password`, `api_key`,
  `token`, `authorization`, `*_secret` are refused): strip them before storing. Model rules:
  `expires_at = requested_at + 72 h` exactly; pending/expired requests have no approver; decided ones
  name `approver {sub, username, name, role: tenant-admin|platform-admin}` and a `decided_at` inside
  the window; `approved`/`applied` never by the requester (self-approval impossible); `rejected`
  needs `decision_reason` (the requester may reject = withdraw). `change_request_decision_error(...)`
  returns `forbidden` (not a tenant-admin of that tenant nor a platform-admin) → `not_pending` →
  `expired` → `self_approval` → `reason_required`.
- **Users (`admin`).** Only `PRODUCT_ROLES` (`planner`, `approver`, `compliance`, `tenant-admin`,
  `platform-admin`, `demo-presenter`) exist on the wire. `user_change_violations(actor_roles,
  actor_tenants=, target_roles=, target_tenants=, new_roles=, enabled=)`: tenant-admins never
  grant/revoke `platform-admin`, never touch a platform-admin's account, and never change a user who
  also belongs to a tenant the actor does not (realm roles are global in Keycloak: granting
  `tenant-admin` there would reach the other tenant). `user_change_guard_reasons(actor_roles, user=,
  target_roles=, new_roles=, enabled=, target_enabled=)`: a tenant-admin granting `tenant-admin`
  (create, add, re-enable a disabled admin) makes a `user_roles` change request
  (`admin_role_granted`), so no admin approves their own change with a second account they created;
  platform-admins apply directly. `PasswordSetup.link` is kept out of `repr` (it is a bearer link). `AdminUserCreated.setup` is a `PasswordSetup {required_action:
  UPDATE_PASSWORD, link (one-time http(s) action link, JSON-Schema pattern), link_expires_at,
  instructions}`: the temporary password is never returned.
- **Custom specialists (`agents`).** `CustomSpecialistSpec {slug, display_name ≤ 60, description ≤ 300
  (single line, planner catalogue), role_prompt ≤ 4000 (data block), model_alias, tools [{connector,
  tool}] ≤ 15 on ≤ 3 connectors, kb_ids (UUIDs) ≤ 10, max_tool_calls 1..15 (8), max_turns 1..10 (6),
  enabled}`; at least one tool or KB. Slugs are snake_case 3..40, never a built-in role
  (`RESERVED_SPECIALIST_SLUGS` ⊇ `AGENT_DISPLAY_NAMES`), no `__`, not `*_investigator`/`*_structurer`.
  Aliases with a `pro` word (bounded by non-letters: `-pro`, `pro002`, ` Pro`) are refused (`is_pro_model`); `custom_specialist_violations` also
  checks the alias's provider model when given `alias_models`. Tools must be approved `read` tools;
  `untrusted_text` tools are allowed but wrapped (`quarantined_tool_refs`), and combining them with an
  external-egress tool is refused. `agui.subagent_started(..., custom=True)` emits `SUBAGENT_STARTED`
  for a custom slug (display name required).
- **Playground (`agents`).** `PlaygroundRequest {specialist: slug | draft spec, question ≤ 2000}`;
  `PlaygroundResult {playground_id, specialist, display_name, status completed|failed|budget_exceeded,
  findings (completed only), tool_calls, usage, error, started_at, duration_ms, committed: false}`.
- **Tier-A mappings (`mapping`).** Parse/validate only; the engine evaluates. The expression grammar,
  its semantics and the template grammar are in the module docstring (paths over dict keys / list
  positions, `+` concatenation, `int` `str` `date` `lookup` `coalesce` `if_present`; no attribute
  access, no floats; ≤ 500 chars, depth 16, 64 nodes). `parse_expression` returns a frozen AST for
  the evaluator. Read mapping: per entity `{entity, source {tool, args (static; {"$json": …} for
  JSON-string args), page_param, page_size_param?, page_size, items_path, cursor_path, max_pages,
  first_page?}, fields {column: expression}, dedupe?}`; every required `scn/1.0` column must be
  mapped, unknown columns are refused, lookups must exist (source lookups cannot chain). Action
  mapping: per op `{op, tool, args (template with payload.*, target_ref, expected_version,
  request_id, op placeholders), idempotency, request_id_arg, readback {tool, args, items_path,
  match}}`; `enforced` needs `request_id_arg`, the others a `readback`; typed ops are checked against
  their payload fields and whether they carry `target_ref`/`expected_version`.
  `action_mapping_violations(mapping, tools=)` checks against the approved tool catalogue;
  `read_mapping_violations(mapping, tools=)` requires every crawl source (entities and `source`
  lookups) to be an approved `read`, non-`untrusted_text` tool (the crawl runs without approval).
- **Snapshots & advisory (`scn`, `blocks`).** `SnapshotLabel {consistency: consistent|crawled,
  as_of_range {from, to} (crawled only)}` (`AsOfRange` uses the wire keys `from`/`to`; Python
  `from_`). `recommended_option@1`, `scenario_compare@1` and `narrative@1` gain `advisory {reason:
  crawled_snapshot, text, as_of_range}` (omitted while unset; `advisory_for(label)`); applying stays
  possible on advisory options.
- **Per-op commit (`commit`).** `CommitLedgerEntry` (states incl. the existing `verified`) with the
  retry rules as validators and `ledger_next_action(entry)` → `none|retry|readback|guide`
  (`enforced` retries with the same request id; `best_effort` reads back first; `none` is never
  auto-retried; ≤ 3 attempts). `atomic_group_bulk_allowed` = every member `enforced`.
- **Capabilities (`connectors`).** `summarize_capabilities(connector, tiers, read_mapping_version=,
  action_mapping_version=, mapped_ops=)` → `ConnectorCapabilities {sync snapshot|crawl|none,
  consistency, commit change_set|per_op|guide, mapped_ops, advisory}`.
- **Schemas.** `admin.change-request|change-request-decision|user|user-create|user-created|user-patch`,
  `agents.custom-specialist|playground-request|playground-result`, `mapping.read|action|
  dry-run-request|dry-run-result`, `scn.snapshot-label`, `commit.ledger-entry`,
  `connectors.capabilities`.

## M8 signal clustering additions (`123-m8-signal-clustering.md`, backward compatible)

- **Memberships (`signals`).** `EventMembership {signal_id, event_id, method, prior_method,
  from_event_id, distance_km, shared_refs, similarity_bp, similarity_profile, hamming_bits,
  duplicate_of_signal_id, config_version, explanation (1..400), decided_at}` says why one signal
  belongs to its event; `MembershipMethod` / `MEMBERSHIP_METHODS` = `anchor`, `same_hazard`,
  `founded`, `attach_geo`, `attach_ref`, `attach_vector`, `near_duplicate`, `absorbed`,
  `merged_vector`. The validator enforces the evidence per method: `attach_ref` names `shared_refs`
  (unique); `attach_geo` / `same_hazard` carry `distance_km`; `attach_vector` / `merged_vector` carry
  `similarity_bp` + `similarity_profile` (always together; `SIMILARITY_PROFILE_PATTERN`, e.g.
  `local-terms-v1`, `embed-default@1536`); `near_duplicate` carries `hamming_bits` (0..64) and an
  original other than itself; `absorbed` / `merged_vector` name `from_event_id` (≠ `event_id`) and
  the original `prior_method` (never a moved method); `anchor` / `founded` carry no evidence.
- **Alert detail.** `AlertDetail.memberships` (<= 1000) defaults to `[]` and is **omitted from dumps
  while empty**, so M6/M7 payloads and goldens are unchanged; when present, each entry must belong to
  the event, name one of its member signals, and appear once per signal.
- **Clustering settings.** Frozen `SimHashSettings {enabled, max_hamming_bits 0..7, window_hours
  1..168, reuse_extraction}`, `VectorThresholds {attach_bp 5000..9900 (MIN_ATTACH_BP /
  MAX_ATTACH_BP), merge_bp >= attach_bp, strict_bp >= attach_bp}`, `VectorSettings {enabled,
  window_hours, max_distance_km 50..3000, local, remote}`, `ClusteringConfig {version, simhash,
  vector, updated_at, updated_by}` and `DEFAULT_CLUSTERING_CONFIG` (version 1; SimHash 6 bits / 72 h;
  local 7000 / 7500 / 7500, remote 8500 / 9000 / 9300 bp). `ClusteringConfigUpdate {base_version,
  simhash, vector, updated_by?}` is the platform-scoped `PUT` body (stale base → 409).
- **Status.** `ClusteringStatus {config, vector_backend, window_days, counts}`: `ClusteringBackend
  {pgvector, local_profile (LOCAL_SIMILARITY_PROFILE = local-terms-v1), remote, remote_reason}` with
  `RemoteEmbeddingProfile {alias, dims, storage vector(d)|halfvec(d), status active|backfilling,
  pending}` or `null` + a reason (`provider_local`, `pgvector_missing`, `profile_missing`,
  `key_missing`); `ClusteringCounts` (near-duplicates, same-source duplicates, reused extractions,
  one count per method, pending / failed embedding jobs).
- **Schemas.** `signals.event-membership|clustering-config|clustering-config-update|clustering-status`;
  `signals.alert-detail` gains the optional `memberships`.

## Develop (inside Docker, per `docs/contracts/60-build-guide.md`)

```bash
MSYS_NO_PATHCONV=1 docker run --rm --memory 1500m \
  -v D:/primark:/work -v keelson_uvcache:/root/.cache/uv \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv -e UV_LINK_MODE=copy -e PYTHONDONTWRITEBYTECODE=1 \
  -w /work/packages/contracts-py ghcr.io/astral-sh/uv:python3.14-trixie-slim \
  sh -c "uv sync --frozen && uv run pytest -q -p no:cacheprovider && uv run mypy \
         && uv run python scripts/export_schemas.py --check"
```

Regenerate the web schemas after changing a model: `uv run python scripts/export_schemas.py`
(writes `packages/contracts-ts/schemas/*.schema.json` + `index.json`; a test fails while they are stale).
