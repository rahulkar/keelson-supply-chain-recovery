# M4 nodes: framing, refine, follow-ups, case actions

Node functions for `docs/contracts/90-m4-investigation.md` §1, §2 (approach card), §5
(`refine_scenario`, `confirm_assumptions`), §6 (follow-ups), §8 (promote) and §10 (map paths).
The workflow wiring (`runtime/workflow.py`) is owned by the investigation engineer. This file is
the contract between these nodes and that wiring.

All nodes are `async def node(ctx: Context, node_input: Any) -> AsyncGenerator[Any]` (wrap them in
`FunctionNode(func=…, name=…)`) unless a factory is named. Inputs are JSON dicts. A node that starts a
run receives `types.Content` holding JSON text, and `common.input_dict` parses both. A **routed** node
ends with `Event(output=…, route=…)` (`common.routed`), so its edges need the routes listed below plus a
`DEFAULT_ROUTE`.

Shared `ctx.state` keys (from `framing`): `problem_evidence_id`, `frame_evidence_ids`,
`scn_snapshot_id`, `problem_source` (read by `impact` / `simulate` / `compose`), `findings`
(`planning.FINDINGS_STATE_KEY`), `intake`, `approach_card`, plus `followup` and
`simulation_overrides` (from `followup`).

Every run of a case is a **new ADK session**, so `ctx.state` is empty in an action run or a
follow-up run. The case-action and follow-up nodes therefore rebuild what `impact` / `simulate`
need. They save a new `problem_data` evidence row and set the state keys above. The data comes from
the plan's pinned snapshot (confirm, follow-ups) or the current snapshot (promote). If that snapshot
is unavailable, they fall back to live MCP reads, which are advisory.

## Taxonomy (`nodes/taxonomy.py`)

| Function | Type | Notes |
|---|---|---|
| `get_taxonomy()` | `-> Taxonomy` | Loads `events.yaml` once (`$TAXONOMY_DIR`, `/app/taxonomy`, then `<source tree>/data/taxonomy`). |
| `check_taxonomy()` | `-> Taxonomy` | Worker start-up check. Raises `TaxonomyError` (a `RuntimeConfigError`) when the event-type keys ≠ `EVENT_TYPE_KEYS`, a key is unknown, a family or band is wrong, a param does not fit its op, or `chokepoints.json`, `regions.json` or `synonyms.yaml` is missing or invalid. |
| `intake_event_types_block(tax=None)` | `-> str` | One line per event type, `- <key>: <label> - <description> (mentions: <mention_kinds>)`, for the intake prompt (`prompts.intake_instruction`). |
| `intake_entity_types_block()` | `-> str` | `Entity mention types:` plus one guidance line per `EntityMentionType` (`ENTITY_TYPE_GUIDANCE`; chokepoint, region, port and component included), also in the intake prompt. |
| `get_gazetteer()` / `set_gazetteer()` | `-> Gazetteer` | `data/geo/regions.json` (`$GEO_DIR`, `/app/geo`, `<source tree>/data/geo`): countries (ISO-2 key, ISO-3, name, aliases incl. demonyms) and multi-country regions (`EU`, `SOUTH_ASIA`, `NEAR_SHORE`, …) → member countries. `resolve(text) -> GeoArea \| None`: the whole mention as a name / alias, or a code written in capitals; else a name / alias longer than three letters as a whole-word phrase, or a short alias written in capitals ("UK DCs"); longest wins, ties by code. `resolve_all(text, phrase=True) -> [GeoArea]`: every area named, in order of appearance, each phrase consumed once (longest first); `phrase=False` only accepts a mention that is wholly a place. ISO codes never match inside a phrase, lower-case short words never do. `country_code(value)` normalises a location's `country` (ISO-2/ISO-3/name/`UK`) to ISO-2. Duplicate names / aliases across areas are refused. |
| `get_synonyms()` / `set_synonyms()` | `-> Synonyms` | `data/taxonomy/synonyms.yaml`: material term → aliases + conventional code segments (`wadding` → `WAD`, `zip` → `ZIP`). `find(text)` returns the materials a mention names, longest phrase first (each phrase consumed once). `singular` / `normalize_term` fold simple plurals ("zips", "zippers", "batteries"). |
| `set_taxonomy(tax \| None)` | | Installs a test or tenant taxonomy. |

## Framing (`nodes/framing.py`)

| Node | Input | Output / routes |
|---|---|---|
| `frame_case` | `{intake: IntakeResult, edits?: str \| [ApproachEdit], removed_checks?: [task_id], edit_errors?: [str]}`. A stored pre-M4 intake with a free-text `event_type` is mapped onto the taxonomy (`coerce_legacy_intake`). | `{scenario_version, problem_evidence_id, mode, removed_checks?}`. Sets the problem state keys and `intake`. With `edit_errors`, it emits a progress line and re-frames without the edit. |
| `quick_exposure` | `frame_case` output | Input + `preliminary_units_at_risk` (unchanged since M3). |
| `approach_gate` | `quick_exposure` output + optional `plan: InvestigationPlan` (dump) | Card `approach_card@1` with `checks = [{task_id, sentence=objective}]` and `agents = [{slug, display_name}]` from the plan (the M1/M3 sentences when there is no plan) and `editable` (`editable_values(scenario)`: `delay_days`/`duration_days` days, `demand_change_pct`/`capacity_pct` pct, `affected_receipts` ids). The props are stored at `ctx.state["approach_card"]`. Auto-confirm (`{decision:"confirm", interrupt_id, auto:true}`) happens only when every assumption is fact or decision, the scenario has perturbations, at most `auto_confirm_max_matched` entities matched, and the plan has ≤ 2 tasks. Otherwise it raises `RequestInput`. |
| `approach_router` | resume `{decision, interrupt_id}` (+ `run.resume`) | Routes `CONFIRM` / `WHAT_IF` → `{scenario_version, mode}`, `EDIT` → `{intake, edits, removed_checks?}` or `{intake, edits:null, edit_errors}`, `CANCEL`, `STALE`, `UNKNOWN`. Typed edits and removed checks are checked with `blocks.approach_edit_violations` against the stored card; free-text edits (M1) pass through. **Looks right** confirms through `refine.confirm_scenario_keys` (re-validated), so the assumptions **and** the perturbations carrying them become `decision`. |
| `promote_case` | action `{plan_version?}` | Marks case and scenario `actual`, supersedes open interrupts, rebuilds ProblemData on the **current** snapshot, re-resolves walked receipts and lane facts (`retarget_scenario`), and saves scenario v+1. Route `RESIMULATE` → `{scenario_version, problem_evidence_id, mode:"actual", findings, promoted:true}` → wire to `impact`. Route `DONE` → `{promoted:true}` when there is no scenario yet. |
| `confirm_assumptions` | action `{assumption_keys?: [str]}` (empty = every pending one) | Confirms the `unconfirmed` assumptions and their perturbations (→ `decision`), saves v+1, and reloads ProblemData from the plan's pinned snapshot. Route `RESIMULATE` → `{scenario_version, problem_evidence_id, mode, findings, confirmed:[keys]}` → `impact` (then `simulate` → … → `compose`). Route `DONE` → `{confirmed: []}` when nothing requested is pending. |

Helpers: `build_scenario(run, caller, *, extract, overrides, po_lines, all_clusters, scenario_id,
snapshot_data, data_source, problem, taxonomy, geo, directory, gazetteer, synonyms) -> DisruptionScenario`;
`compile_template(...) -> (perturbations, assumptions, extra_refs)`, which is pure;
`load_snapshot_directory(run, snapshot_id) -> SnapshotDirectory | None` (`scn_location` suppliers /
factories / ports / DCs with their country + `scn_item` components; `None` when there is no snapshot
or it is unreadable); `place_index(problem, geo, directory=None)`;
`resolve_static(mention, idx, geo) -> EntityRef | None` (first ref of `resolve_static_groups`);
`resolve_components(text, idx)`; `region_members(code, kinds, idx)`;
`lane_effects(problem, ids) -> [LaneEffect(lane, alt_lane, extra_days, open_lines)]`;
`load_case_bundle(run, snapshot_id=None)` and `resimulate_payload(ctx, run, version, bundle, mode=…, **extra)`
(used by follow-ups); `case_findings(run)` (the verified `investigation_findings` state in plan
order, else the latest revision per task from `list_investigation_tasks`; never stale revisions or
unverified claims). Chokepoint codes only match a mention exactly ("Cape Town" is not `CAPE`), and a
`port` mention is a chokepoint only when it is exactly a chokepoint name or alias ("Port of Dover" is
a port).

### Compilation rules

* The template is chosen by `extract.event_type`. `event_family` is always derived from the key, and
  `mode` is `what_if` when `is_hypothetical` is set or the case is already a what-if.
* Mentions are resolved without calling a tool for `chokepoint` / `port` / `region` / `lane` /
  `component` / `other`:
  * A chokepoint code or alias (`chokepoints.json`, case-insensitive) wins for `port`, `region`,
    `other` and `lane` mentions.
  * Ports resolve by UN/LOCODE, by name, or by a whole-phrase match against the snapshot locations
    and the geo catalogue.
  * A region is a country or multi-country region of `data/geo/regions.json` (name, alias,
    demonym or ISO code: "Bangladesh", "Türkiye", "UK", "EU", "South Asia"); it stays in `matched`
    (`type: region`, label = the area name) and **expands** to the entities located in its countries
    (`fact`): the snapshot directory's `location.country` (normalised through the gazetteer, so a
    `UK` country value is GB) for suppliers, factories, ports and DCs, plus ports by their UN/LOCODE
    prefix. Only the kinds the template acts on are added: a `resolve: receipts` template walks the
    region's suppliers and factories; a direct template targets its `target_kinds` among supplier /
    factory / port / dc (a chokepoint closure adds nothing).
  * Components resolve by code, or by material name against the snapshot's component items and
    the BOM: a `synonyms.yaml` material matches a component whose code has one of its code segments
    or whose name / family contains the term or an alias; an unknown word matches names or a
    distinctive code segment. One mention may name several materials ("wadding and zips"). When a
    material matches several components, the ones supplied by a supplier, used at a factory or
    sourced from a region named in the same message are kept (else all of them).
  * One mention may also name several regions ("Vietnam and Cambodia" → VN, KH;
    `Gazetteer.resolve_all`, each phrase consumed once, longest first).
  * Loosely typed mentions (nothing resolved them as typed): a SKU / style mention is tried as a
    material first; a business-entity mention (`supplier`, `po`, `factory`, `dc`, `store`, `other`)
    is tried as a region found anywhere in it ("the Bangladesh POs" typed as a PO). A product
    mention (`sku`, `style`, `component`) is read as a region only when the whole mention is a
    place: "Turkish cotton towels" or "German-made parkas" describe a product and never expand to
    every supplier, factory or DC in that country. Every mention resolved this way is listed in the
    inference assumption `mention_readings` (label "Read as": "'the Bangladesh POs' as
    Bangladesh"), so the card shows the interpretation and cannot auto-confirm on it.
  * A `synonyms.yaml` material's code segments match any segment of a component code, even one all
    components share (a snapshot with wadding items only); a word taken from the user's text
    matches distinctive segments only (never the shared `CMP`).
* `supplier` / `po` / `sku` / `style` / `dc` / `store` / `factory` mentions are resolved with the
  connector's `get_entity` / `search_entities`, as in M1. Mentions that cannot be resolved by any
  of the rules become the inference assumption `unmatched_mentions` ("Not found in <vendor>"); a
  mention resolved as a region or a material is never listed there. A region that holds nothing
  the template acts on adds the inference `region_scope` ("No supplier or factory in the Merxa data
  is located in Brazil", or, without a snapshot directory, that the data has no such locations).
* Values:
  * A user-stated delay, duration, demand change, start date, or an Edit, becomes a `decision`
    (no band).
  * A template prior becomes an `assumption`; its p10/p90 band goes onto the perturbation. The
    tenant's `planning.default_delay_days` re-centres the delay prior.
  * A `fixed` value (a closure is 0 bp) defines the event and is not an assumption.
* A perturbation's `prov` is the weakest provenance among its value, duration and start.
* Windowed templates get `window = [start, start + duration − 1]`. The start is the stated date,
  else the planning date (`assumption`).
* Targets:
  * `resolve: receipts` walks the BOM and POs to open PO lines (`fact`). A user-chosen scope stays a
    `decision`. With no walk result it falls back to direct sku / style / dc targets, otherwise it
    adds an inference note and no perturbation.
  * `resolve: direct` targets the resolved mentions of the template's kinds.
    `default_scope: all_clusters` applies to demand events.
* Capacity perturbations on chokepoints or ports add the fact `affected_lanes`: lanes through the
  closed point → their `alt_lane_code`, with the extra transit taken from the lane catalogue, plus
  the number of open PO lines riding on them.
* `OTHER` → no perturbation, plus the inference assumption `event_type` (investigation only).

## Refine (`nodes/refine.py`)

| Node | Input | Output |
|---|---|---|
| `refine_scenario` | critic output (dict; `findings` = Findings dumps, else `ctx.state["findings"]`) | The input passed through, plus `findings`, `scenario_version`, `mode` and `refined: {applied, pending, dropped}` (each `{task_id, key, evidence_id, outcome, reason}`). Saves a version only when something changed. Wire `refine_scenario → impact`; `impact` reads `findings` from this output. |

An update is **fact-backed** only when both of these hold:

* The cited evidence row has `prov == "fact"`, does not come from an `untrusted_text` tool (per the
  connector's approved tool snapshot or a `content_trust` marker on the row or its args), and is not
  a quarantine wrapper's output (tool name ending `_structured`).
* The cited row belongs to this case (another case's row counts as unknown evidence and is dropped).
* The value at `json_pointer` supports the update: an equal integer **in a field named for the key**
  (`refine.FIELD_STEMS`: the last non-index pointer token contains `delay` / `late` / `slip`,
  `duration` / `closure` / `closed` / `outage`, or `demand` / `uplift` / `pct` / `percent`), so an
  unrelated integer that happens to be equal (`line_no`, `qty`) never makes a value a fact; or, for
  `add_receipts`, every receipt id present. `remove_receipts` is never fact-backed: a read that lists
  a line does not show it is unaffected, so shrinking the exposure always needs a confirmation.

Any other update is **inference-backed**. It is still applied, but as `prov: inference` with
`Assumption(unconfirmed=True, source_evidence_ids=[…])`, so `scenario.pending_confirmation` is
non-empty.

Updates are dropped when they are malformed (`shape_error`), cite unknown evidence, do not apply to
this scenario (for example a demand change on a supply scenario), or lose a conflict. For the same
key, fact beats inference, and otherwise the first in findings order wins.

Shared helpers:

* `set_scenario_value(scenario, key, *, value_int|value_ids|value_date, mode, prov, unconfirmed,
  source_evidence_ids, note)` returns the edited scenario, or `None` when the key is not applicable.
* `scenario_value(scenario, key)` reads the current value.
* `confirm_scenario_keys(scenario, keys)` confirms the given keys.

## Follow-ups (`nodes/followup.py`)

| Node / factory | Input | Output / routes |
|---|---|---|
| `make_followup_classifier(models, *, alias="fast-default", name="followup_classifier") -> KeelsonLlmAgent` | | `make_agent(...)`: `single_turn`, `include_contents='none'`, InstructionProvider `followup_instruction`, `output_schema=FollowupClassification`, no tools. The FakeLlm script key is `followup_classifier`. |
| `make_followup_delta(classifier) -> FunctionNode("followup_delta", rerun_on_resume=True)` | chat text (`str` / `Content` / `{question \| text}`; falls back to `ctx.state["chat_text"]`) | The classifier sees `{question, case: {scenario: {event_type, mode, values}, options: [{id, title, levers}], levers}}` and no free text from the case. The delta is repaired (`constraint_delta_violations`), then the kind is normalised: RESIMULATE without a change → EXPLAIN; REINVESTIGATE without a delay or duration change → RESIMULATE, or EXPLAIN when nothing changed. Routes `EXPLAIN` / `RESIMULATE` / `REINVESTIGATE` → `{kind, question, constraint_delta, changed_keys, dirty_task_ids}`. The dirty set is computed only for REINVESTIGATE; when the case has recorded tasks but none is dirty, the route (and `kind`) becomes RESIMULATE, because the investigations are all reused. The output is also stored at `ctx.state["followup"]`. |
| `explain` | `followup_delta` output | Answers deterministically from the latest plan's options and scorecards (`explain_text`), sends it as an assistant message, and routes `DONE` → `{explained, question}`. When the question names an **enabled** lever that no option simulated, it instead routes `RESIMULATE` with `simulation_overrides.only_levers=[lever]`, `simulate_levers` and `what_changed` (the resimulate payload below). |
| `prepare_resimulate` | `followup_delta` output | Applies the stated `delay_days` / `duration_days` as `decision` (scenario v+1). Puts `exclude_levers` / `sku_filter` / `max_cost_cents` into `ctx.state["simulation_overrides"]`. Routes `RESIMULATE` → `{scenario_version, problem_evidence_id, mode, findings, simulation_overrides, what_changed: [str]}` → `impact`. Routes `DONE` when there is no scenario. |
| `prepare_reinvestigate` | `followup_delta` output | Applies the assumption change as `decision` (v+1) and computes `dirty_task_ids` (the input's, else `dirty_task_ids(task_records, changed_keys)`) and `rerun_task_ids = {old: <base>-r<latest revision + 1>}` (the ids `investigate` writes). Routes `REINVESTIGATE` → `{scenario_version, problem_evidence_id, mode, findings, what_changed, dirty_task_ids, rerun_task_ids, changed_keys}` → re-run those tasks, then `refine_scenario → impact → …`. Routes `DONE` when there is no scenario. |

### What `simulate` must do with `ctx.state["simulation_overrides"]` (planning.py, not wired here)

```python
ov = SimulationOverrides.from_dict(ctx.state.get("simulation_overrides"))
planning = planning_with_overrides(run.spec.planning, ov, problem)  # → build_candidates(planning, caps)
...
banned = set(over_budget(options, ov.max_cost_cents))              # never recommend these
if ov.keep_previous:                                              # EXPLAIN of a never-simulated lever
    options = merge_options(previous_plan["options"], options)     # plan v+1 = previous + new lever
```

`planning_with_overrides` does the following:

* `exclude_levers` / `only_levers` narrow `enabled_levers`. `only_levers` also replaces the candidates
  with one `lever-<code>` candidate per lever.
* `sku_filter` limits `expedite_mode_shift.po_line_refs` and `resource_alt_supplier.style_codes`. An
  entry matches a SKU by its code, its style or one `-` token (size or colour).
* `max_cost_cents` caps `max_premium_cents` of the premium levers. `over_budget` enforces the total
  cost cap on the results.

### Dirty set (`dirty_task_ids`)

Task records come from `repo.list_investigation_tasks(tenant, case_id)`, called duck-typed. Each row
looks like `{task_id|id, specialist, status, input: {assumption_keys?: [str], depends_on?: [id]},
findings?: Findings}`. When the repo has no such method, the records come from
`ctx.state["findings"]` instead.

Only the latest revision per task counts (`<base>-r<rev>`). A task is dirty when any of these holds:

1. it failed;
2. its recorded `assumption_keys` intersect the changed keys;
3. its findings proposed an update for a changed key;
4. it depends on a dirty task (transitively);
5. no task recorded `assumption_keys` and rules 1–3 found nothing. All tasks are then dirty, because
   nothing says which tasks used the changed value.

The investigation engineer should therefore store `input.assumption_keys` (the scenario keys the
task's brief relied on) when the task is upserted.

## Map paths (`runtime/geo.py`, `runtime/visuals.py`)

* `GeoCatalog.sea_air_legs(stops, via) -> (sea leg, hub, air arc) | None`. `air_arc(a, b)` is a
  deterministic quadratic Bézier with 17 points, integer e6, exact ends.
* `network_map` draws a sea-air lane as two links:
  * `<id>:sea`, kind `inbound`: the catalogued sea route from the origin port into the hub
    (`port:AEJEA`), with `units=null`;
  * `<id>`, kind `sea_air`: from `port:AEJEA` to the DC along the air arc, carrying the units.
  
  This applies to base lanes (`lane:<code>`) and to expedite option links
  (`<option>:sea_air:<code>`). The viz `ArcLayer` draws `path[0] → path[-1]`, so its arc now starts
  at the hub.
* `stops_path(mode="sea_air")` keeps a straight air leg (one polyline, M3 behaviour).
* A `sea` lane straight into a DC (no destination port) follows the catalogued short-sea route to the
  port nearest the DC (`short_sea_route`), for example Istanbul or Tanger Med lanes modelled as sea
  lanes. Road lanes stay straight.
* Closed chokepoints (capacity targets of kind `chokepoint`) add `footprint` polygons from
  `chokepoints.json` (label = name, status `disrupted`), extend the bbox, and add the legend entry
  `closed_chokepoint`.
