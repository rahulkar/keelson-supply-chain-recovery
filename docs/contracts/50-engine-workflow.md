# 50 · Engine workflow contract (ADK 2.11) — M1 thin slice

Pins: `google-adk[db,mcp]==2.11.0`, `mcp>=2.3,<3`, `litellm==1.104.0` (hash-locked; never 1.82.7/1.82.8), `fastmcp==4.0.11` (client side for deterministic MCP calls), `ag-ui-protocol==1.0.0`. Before writing ADK code, **read the installed ADK source** (`site-packages/google/adk/{workflow,events,agents,apps,runners.py,models/lite_llm.py,tools/mcp_tool}`) — docs lag the code. Rules (from research; enforced):
- Root is a `google.adk.Workflow` (graph). Do **not** use `SequentialAgent/ParallelAgent/LoopAgent`, `AgentTool` where a pause can occur, tool confirmation, or `adk api_server`.
- `App(name="t_<tenant>", root_agent=workflow, resumability_config=ResumabilityConfig(is_resumable=True))`; `DatabaseSessionService(db_url="postgresql+asyncpg://…/adk")`; `RunConfig(streaming_mode=StreamingMode.SSE)`; session_id = run_id.
- Every `LlmAgent`/`Agent` node: `mode="single_turn"`, `include_contents="none"`, instruction via an `InstructionProvider` callable (no `{var}` templating of tenant text). Never combine `tools` and `output_schema` on one agent.
- LLM via `KeelsonLiteLlm(LiteLlm)` subclass: `model="litellm_proxy/<alias>"`, `api_base="http://litellm:4000"`, `api_key=<tenant virtual key>`; merges per-run `extra_headers` (`x-litellm-trace-id`, `x-litellm-customer-id`) from a contextvar inside `generate_content_async`. A sentinel default model raises if any agent lacks an explicit model.
- HITL only via `RequestInput` (`from google.adk.events import RequestInput`); resume with a server-built `FunctionResponse(id=interrupt_id, name="adk_request_input", response={decision, interrupt_id})` + `invocation_id`.
- Nodes never `return None`; never catch `BaseException`/broad `Exception` inside nodes (breaks HITL/retries); every router has `DEFAULT_ROUTE`.
- Writes only in deterministic function nodes. LLM agents only get **read** tools through `RegistryToolset` (reader principal).

> **As built through M4 (updated M4, 2026-10-07):** the sections below are the M1 contract, kept as history. Where they differ from the code, the sections at the end of this file — "M4 graph", "M4 stages and agents", "M4 LLM roles and schemas", "M4 run envelope", "M4 investigation, quarantine and review" and "M4 testing" — are authoritative. The M3 changes to `simulate`/`compose` are in `80-m3-deterministic-core.md` §4.
>
> **M5 (updated M5, 2026-10-07):** the M5 sections at the end of this file are authoritative for the commit path, the M5 graph additions, run envelope, stages and tests: "M5 graph", "M5 run envelope", "M5 stages", "M5 approval policy and quorum", "M5 Guide mode", "M5 rollback", "M5 re-plan", "M5 bundles" and "M5 testing". Node-level detail (state keys, ports, integration fixes) is in `runtime/nodes/M5_NODES.md`.

## Run request envelope (`keelson_contracts.runs`)
`RunRequest {run_id, tenant, case_id, kind: "chat"|"action"|"signal"|"resume", actor:{sub, username, name, roles}, payload}`
- chat: `{text}`; action: `{type:"apply"|"guide"|"promote", option_id, plan_version}`; signal: `{signal_id}` (M5+); resume: `{interrupt_id, decision, …}` (server-built).
- (updated M4, 2026-10-07) The M4 action type `confirm_assumptions` and the typed resume edits are described in "M4 run envelope" below.

## M1 graph
(updated M4, 2026-10-07: superseded by "M4 graph" below; kept as history.)
```
START → pre_route (fn): chat→intake | action:apply→compile_changeset | action:promote→promote_case | DEFAULT_ROUTE→reject_run
intake (LLM, alias fast-default, output_schema=IntakeResult) → route_intake (fn)
   QUESTION → answer_investigator (LLM+read tools) → answer_structurer (LLM, Findings) → compose_answer (fn) → END
   NEW_DISRUPTION → frame_case → approach_gate → approach_router
   FOLLOWUP → (M1) treated as QUESTION over current case data
   DEFAULT_ROUTE → compose_smalltalk (fn: canned helpful reply + suggestion chips) → END
frame_case (fn): resolve entity mentions deterministically via MCP read tools (reader principal, FastMCP client, through egress proxy) →
   build DisruptionScenario {perturbations, mode actual|what_if (is_hypothetical), matched entities, assumptions} →
   gather ProblemData via MCP reads (locations, skus, inventory, POs, demand, allocations, lanes from tenant config) → persist scenario + evidence rows
   → quick_exposure (fn: optimizer /v1/impact → kpi_strip block marked preliminary)
approach_gate (fn): emits approach_card block; if all assumptions are fact|decision and matched ≤ 50 → auto CONFIRM (event recorded), else `yield RequestInput(interrupt_id=f"approach-{case}-{rev}", …)`
approach_router: CONFIRM→investigate | WHAT_IF→investigate (mode=what_if) | EDIT→frame_case (M1: re-run with user edits text) | CANCEL→close_case | DEFAULT_ROUTE→close_case
investigate (@node rerun_on_resume=True): run specialist wrapper(s) via ctx.run_node(…, run_id="task-<slug>-r<rev>", use_sub_branch=True) — M1: one specialist `inventory_demand`; failures return Findings(status="failed")
impact (fn): optimizer /v1/impact on final scenario → blocks
simulate (fn): optimizer /v1/optimize with M1 plan specs: (a) "Rebalance between DCs" (reallocate_inventory), (b) "Do nothing" baseline → persist plan v{n} with options
recommend (LLM, alias reasoning-default, output_schema=Recommendation, tool-free; gets option KPI keys list, NOT numbers)
compose (fn): fill placeholders, numeric linter, blocks: kpi_strip (final), recommended_option, plan_diff (canvas), evidence_pack (canvas), narrative → TEXT_MESSAGE → END
compile_changeset (fn): load plan option → canonical ops (identity mapping to Merxa ops in M1) → engine change_set row, H
dry_run (fn): preparer principal: create_change_set → add_change_set_items → validate_change_set; require preview_hash == H; plan_diff block refresh
approval_gate (fn): policy (M1 python rules: what_if → refuse; any unconfirmed inference → refuse; lines ≤ 10 and units ≤ 500 and initiator has role approver → AUTO; else HUMAN with quorum {roles:["approver"], count:1, exclude_initiator:false}) → approval_card block; HUMAN → RequestInput(interrupt_id=f"approval-{change_set_id}-{rev}")
approval_check: APPROVED→execute | REVISE→close_case (M1) | REJECT→close_case | DEFAULT_ROUTE→close_case
execute (@node rerun_on_resume=True): load change_set by id from DB; verify H; mint receipt; get apply token (delegated approver link, or autocommit for AUTO); MCP apply_change_set with idempotency_key=sha256(tenant|change_set_id|revision); commit_ledger; commit_tracker block
reconcile (fn): read back via get_change_set + get_inventory/list_transfer_orders; mark verified; plan status committed; commit_tracker → verified → END
```
Stage mapping: intake/frame_case/approach_* → `understanding`; investigate → `checking_systems`; quick_exposure/impact → `measuring_impact`; simulate/recommend → `building_options`; compose → `ready_for_review`; compile_changeset/dry_run → `preparing_changes`; approval_* → `awaiting_approval`; execute → `applying`; reconcile → `verified`.
Agent display names (only these emit SUBAGENT_*): `intake` "Intake", `inventory_demand` "Inventory & demand analyst", `sourcing` "Sourcing analyst", `logistics` "Logistics analyst", `recommender` "Strategist", `critic` "Critic".
(updated M4, 2026-10-07: the stage mapping and display names above are the M1 lists. The M4 nodes and the `planner` and `playbook` names are in "M4 stages and agents" below.)

## LLM output schemas (strict-compatible: no `Any`, no free dicts, nullable fields are required+nullable)
```python
class EntityMention(BaseModel): type: Literal["supplier","po","sku","style","dc","store","factory","component","port","lane","other"]; text: str
class DisruptionExtract(BaseModel):
    event_family: Literal["SUP","LOG","NAT","CYB","REG","DEM","OTHER"]; event_type: str; description: str
    entity_mentions: list[EntityMention]; delay_days: int | None; demand_change_pct: int | None
    start_date: str | None; duration_days: int | None; is_hypothetical: bool
class IntakeResult(BaseModel):
    kind: Literal["QUESTION","NEW_DISRUPTION","FOLLOWUP","SMALLTALK"]; summary: str; disruption: DisruptionExtract | None
class Claim(BaseModel):
    text: str; kind: Literal["fact","inference"]; evidence_id: str | None; json_pointer: str | None; calc_id: str | None
    confidence: Literal["high","medium","low"]
class Findings(BaseModel):
    task_id: str; status: Literal["ok","failed"]; claims: list[Claim]; affected: list[EntityRef]; gaps: list[str]
class Recommendation(BaseModel):
    headline: str; bullets: list[str]; recommended_option_id: str
    why_not_others: list[OptionReason]; risks: list[str]; follow_ups: list[str]
```
User-stated numbers in `DisruptionExtract` are labelled `decision` (user-stated); they are data, not narrative.

## Numbers rule (deterministic, `keelson_engine.compose`)
Placeholder grammar in LLM narrative: `{{kpi:<option_id>.<key>}}`, `{{ev:<evidence_id>#<json_pointer>}}`, `{{calc:<calc_id>}}`. `compose` resolves them with unit-aware formatting (`eur_cents` → "€12,400", `units` → "3,200 units", weeks/dates). **Numeric linter:** after stripping placeholders, any digit sequence must be part of an allowed token (entity codes present in evidence such as `PO-26-0101`, `UK1`, ISO weeks/dates present in evidence or scenario); otherwise one repair turn (re-ask recommender with the violation list), then strip the sentence and mark "unverified". Unknown placeholder key → same handling. A `calc` tool (deterministic: count, sum, min, max, date_diff over evidence JSON) is available to the answer investigator.

## Specialist pattern
Wrapper function node (yields an ephemeral start marker `Event(partial=True, custom_metadata={"lifecycle":"agent_started","agent":slug})`, then runs investigator → structurer via `ctx.run_node`). Investigator: Agent with `RegistryToolset` filtered to the specialist's allowlist (M1 `inventory_demand`: `get_inventory, get_demand_forecast, list_allocations, list_purchase_orders, get_purchase_order, list_locations, get_bom_where_used`), instruction requires citing `_evidence_id`s. `EvidencePlugin.after_tool_callback` stores every tool result as an `evidence` row and adds `_evidence_id` to the returned dict. Structurer gets the investigator's text + its evidence index, outputs `Findings`.

## RegistryToolset (M1 minimal)
`BaseToolset` subclass that lists tools from the `connector_tool` snapshot (admin-approved; M1 bootstrap seeds the Merxa tool snapshot by calling `tools/list` once and marking read tools approved), executes calls with a FastMCP client using the reader principal token from the Token Broker via the egress proxy, enforces explicit timeouts (connect 15 s, call 120 s), paginates nothing implicitly, and checks `definition_hash` at session start (mismatch → tool dropped + audit). Tools classified `untrusted_text` (e.g. `list_supplier_messages`) are not bound to investigators in M1.

## Translator
Consumes `runner.run_async` events; emits AG-UI events per `30-engine-api-events.md`; synthesises `SUBAGENT_STARTED` from the wrapper marker or the first event under a new node path for LLM roles; maps function nodes to `STEP_*` only; persists to `run_event` (coalesce reasoning) and `XADD keelson:case:<id>`; de-dups replayed outputs on `(invocation_id, path)`.

## Testing harness (required in M1)
- `FakeLlm(BaseLlm)` keyed by agent name/node path → canned `LlmResponse`s (incl. function calls) so the full workflow runs offline (`pytest -m "not live"`).
- Tests: routing (chat/action), approach pause→resume (fresh Runner), approval pause→resume, crash-retry (reaper) with exactly-one Merxa apply per change-set (Merxa stub records calls), numeric linter, PolicyPlugin denies write tools, what_if cannot compile, receipt claims.
- `pytest -m live` (opt-in) runs S1 against the real LiteLLM proxy aliases with the real schemas.

## M4 graph (updated M4, 2026-10-07) — authoritative
`runtime/workflow.py` `build_workflow(spec, bindings)`. Node contracts are documented next to the code: `runtime/nodes/M4_INVESTIGATION.md` (planner, investigate, verify_numbers, critic, quarantine) and `runtime/nodes/M4_NODES.md` (framing, refine, follow-ups, case actions). Every router has a `DEFAULT_ROUTE` edge.
```
START → pre_route (fn): chat→intake | action:apply→compile_changeset | action:promote→promote_case
                        | action:confirm_assumptions→confirm_assumptions | DEFAULT_ROUTE→reject_run
intake (LLM, IntakeResult) → route_intake (fn)
   QUESTION → answer_investigator → answer_structurer → compose_answer → END
   FOLLOWUP, case has a plan → followup_delta   (FOLLOWUP without a plan → answer path, as M1)
   NEW_DISRUPTION → frame_case
   DEFAULT_ROUTE → compose_smalltalk
frame_case → quick_exposure → planner → approach_gate → approach_router
   CONFIRM | WHAT_IF → investigate
   EDIT → frame_case            (typed edits or M1 text; the planner then reuses the plan minus removed_checks)
   CANCEL | DEFAULT_ROUTE (incl. STALE, UNKNOWN) → close_case
investigate → verify_numbers:  OK → critic | REVISE → investigate | DEFAULT_ROUTE → critic
critic:                        PASS → refine_scenario | REVISE → investigate | DEFAULT_ROUTE → refine_scenario
refine_scenario → impact → simulate → recommend → compose → END
promote_case | confirm_assumptions:  RESIMULATE → impact | DEFAULT_ROUTE (DONE) → end_turn
followup_delta:  EXPLAIN → explain | RESIMULATE → prepare_resimulate | REINVESTIGATE → prepare_reinvestigate
                 | DEFAULT_ROUTE → answer_investigator
explain | prepare_resimulate:  RESIMULATE → impact | DEFAULT_ROUTE (DONE) → end_turn
prepare_reinvestigate:         REINVESTIGATE → investigate (dirty tasks only) | DEFAULT_ROUTE (DONE) → end_turn
compile_changeset: COMPILED → dry_run | DEFAULT_ROUTE → close_case
dry_run:           VALIDATED → approval_gate | DEFAULT_ROUTE → close_case
approval_gate → approval_check: APPROVED → execute | REVISE | REJECT | DEFAULT_ROUTE → close_case
execute → reconcile → END
```
- **planner** (function node around the `planner` agent, `rerun_on_resume=True`): reads the latest scenario and calls the LLM with the scenario brief (no untrusted text), `AgentSpec.planner_catalogue()` (slug, display name, description and tools per specialist) and the spec limits. The plan is repaired with `normalise_plan` and `lint_objectives`, then checked with `plan_violations`. An unusable plan or a failed planner falls back to the deterministic `default_plan` (specialists by event family). With one usable specialist there is no LLM call. After an approach **Edit** the stored plan is reused minus `removed_checks` (`plan_source = "edited"`). The plan is stored as evidence `tool="investigation_plan"` (prov `inference`). Output: the input plus `{plan_evidence_id, plan_source, plan, checks, agents, plan_task_count, plan_auto_confirm}`.
- **approach_gate**: `approach_card@1` gains `checks: [{task_id, sentence}]` (from the task objectives), `agents: [{slug, display_name}]` and `editable` values (see `30`, M4 blocks). Auto-confirm needs all four conditions: every assumption is `fact` or `decision`; the scenario has perturbations; at most `policy.auto_confirm_max_matched` (50) entities matched; the plan has at most `investigation.auto_confirm_max_tasks` (2) tasks. Otherwise the gate raises `RequestInput(interrupt_id=f"approach-{case}-{version}")`.
- **investigate** (`rerun_on_resume=True`): runs the plan tasks in dependency waves (`plan_waves`). Each wave runs `asyncio.gather` over `ctx.run_node(wrapper, node_input=brief, run_id="task-<id>-r<rev>", use_sub_branch=True)`, with at most `min(plan.budget.max_parallel, investigation.max_parallel)` tasks at once (an asyncio semaphore). A failing specialist yields `Findings(status="failed")`; any other error cancels the wave and propagates. A subset input (`revise_task_ids` | `task_ids` | `dirty_task_ids`, optionally with `notes` and `plan`) re-runs only those tasks at `rev+1`, continuing from the highest revision recorded in `investigation_task`. Each task upserts the row `investigation_task.id = "<case_id>:<task id>:r<rev>"` with `input = {task_id, rev, objective, tools, depends_on, assumption_keys, scenario_version, run_id}` and appends a `finding` row.
- **verify_numbers** and **critic** share ONE REVISE per run (`ctx.state["revise_budget"]`, `investigation.revise_budget`, default 1).
  - verify returns REVISE when more than `investigation.verify_max_failed_bp` (2000 bp = 20%) of the numeric claims fail to resolve.
  - critic (`critic-default`, tool-free, `Critique`) returns REVISE only for a valid critique that names known tasks. An unusable critique or a failed critic counts as PASS.
  - On PASS the findings state is saved as evidence `tool="investigation_findings"`.
- **refine_scenario**: applies fact-backed `Findings.scenario_updates`. An inference-backed update becomes `Assumption(unconfirmed=True)`, so `scenario.pending_confirmation` is non-empty. A scenario version is saved only when something changed. The rules are in `M4_NODES.md` §Refine.
- **promote_case** (action `promote`): sets the case and the scenario to `actual`, supersedes open interrupts, rebuilds ProblemData on the **current** snapshot and saves scenario v+1. It then routes `RESIMULATE` → impact → simulate → recommend → compose, which gives plan v+1 with Apply enabled unless something else blocks it.
- **confirm_assumptions** (action `confirm_assumptions`): the listed `unconfirmed` assumptions and their perturbations become `decision`, scenario v+1 is saved and ProblemData is reloaded from the plan's pinned snapshot. It routes `RESIMULATE` → impact, so only impact, simulate, recommend and compose run again (no new investigation), or `DONE` when nothing requested is pending.
- **Follow-ups** (`followup_delta` = `followup.make_followup_delta(make_followup_classifier(models, alias=spec.models.followup))`). The LLM `followup_classifier` (tool-free, `FollowupClassification`) sees the question and a case summary, with no free text from the case. The delta is then repaired, and the dirty set computed, deterministically.
  - EXPLAIN answers from the latest plan's options and scorecards. If the question names an enabled lever that was never simulated, it routes `RESIMULATE` with `simulation_overrides.only_levers`.
  - RESIMULATE applies `delay_days`/`duration_days` as `decision` (v+1) and puts `exclude_levers`/`sku_filter`/`max_cost_cents` into `ctx.state["simulation_overrides"]` for `simulate`.
  - REINVESTIGATE re-runs only the dirty tasks, then verify → critic → refine → impact → ….
  - Every follow-up and case action ends with `narrative@1.what_changed`: one deterministic line against the superseded plan version.
- **Later runs of a case** start with an empty `ctx.state` (each run is a new ADK session). `impact` and `simulate` call `case_state.ensure_planning_state` (problem data, snapshot, findings from the latest rows). `promote_case`, `confirm_assumptions` and the follow-ups rebuild the ProblemData evidence row themselves.
- **Apply guards** (same rule as before, new M4 inputs): `compile_changeset` refuses a what-if case ("promote it to an actual case before applying changes") and a scenario with `pending_confirmation` ("Confirm before applying: …"); a refusal takes DEFAULT_ROUTE → close_case with that message. `policy.decide_approval` also refuses what-if cases and unconfirmed assumptions.
- **Assistant text** (`TEXT_MESSAGE_*`) still comes only from deterministic nodes that write text: compose, compose_answer, compose_smalltalk, close_case, reject_run, reconcile, promote_case, confirm_assumptions ("nothing to confirm") and explain/followup. Node progress lines ("Planned 4 checks with …", "Checked 12 figures against the evidence") go to the structured log only and are never events.

## M4 stages and agents (updated M4, 2026-10-07)
`keelson_contracts.agui.NODE_STAGES` (the stage vocabulary and labels are unchanged):

| stage | nodes |
|---|---|
| `understanding` | intake, frame_case, planner, approach_gate, approach_router, followup_delta, prepare_resimulate, prepare_reinvestigate, promote_case, confirm_assumptions |
| `checking_systems` | investigate, verify_numbers, critic, refine_scenario |
| `measuring_impact` | quick_exposure, impact |
| `building_options` | simulate, recommend |
| `ready_for_review` | compose, explain |
| `preparing_changes` | compile_changeset, dry_run |
| `awaiting_approval` | approval_gate, approval_check |
| `applying` / `verified` | execute / reconcile |

Nodes outside the map (pre_route, route_intake, answer_*, compose_answer, compose_smalltalk, close_case, reject_run, end_turn) leave the stage unchanged. Stages are not monotonic: `quick_exposure` (measuring_impact) runs before `planner` (understanding). So `understanding` is entered twice, and each transition emits `STEP_FINISHED` + `STEP_STARTED`.

`AGENT_DISPLAY_NAMES` lists the only roles that emit `SUBAGENT_*`; a spec's `display_name` overrides the description:

| slug | display name |
|---|---|
| `intake` | Intake |
| `planner` | Planner |
| `inventory_demand` | Inventory & demand analyst |
| `sourcing` | Sourcing analyst |
| `logistics` | Logistics analyst |
| `playbook` | Playbook analyst |
| `recommender` | Strategist |
| `critic` | Critic |

- The translator opens a row from the specialist wrapper's start marker, or from the first event under a static LLM node (`intake`, `planner`, `recommend`, `critic`).
- The planner row opens only when its child agent actually runs. A single-specialist plan, or an Edit that reuses the plan, has no row.
- `followup_classifier`, `answer_*`, `guard`, `extractor` and `recommender_repair` have no row.
- Every event under `guard`/`extractor` (`quarantine.QUARANTINE_NODE_NAMES`) is dropped: it is never streamed or persisted.

## M4 LLM roles and schemas (updated M4, 2026-10-07)
All agents are `single_turn` with `include_contents="none"`; no agent has both `tools` and `output_schema`. Aliases come from `AgentSpec.models` (`runtime/spec.py`):

| agent (ADK name) | alias key (default) | tools / output |
|---|---|---|
| `intake` | `intake` (`fast-default`) | `IntakeResult` |
| `answer_investigator` / `answer_structurer` | `fast-default` | read tools + `calc` / `Findings` |
| `planner` | `planner` (`reasoning-default`) | `InvestigationPlan` |
| `<slug>_investigator` | `investigator` (`fast-default`) | `RegistryToolset` (specialist allowlist) + its quarantine wrapper tools; no schema |
| `<slug>_structurer` | `structurer` (`fast-default`) | `Findings` |
| `guard` | `guard` (`guard-default`) | `GuardVerdict {injection_suspected, reason}` |
| `extractor` | `extractor` (`extract-fast`) | `UntrustedExtraction` |
| `critic` | `critic` (`critic-default`; must differ from `planner`) | `Critique` |
| `recommend` / `recommender_repair` | `recommender` (`reasoning-default`) | `Recommendation` |
| `followup_classifier` | `followup` (`fast-default`) | `FollowupClassification` |

Schema changes in `keelson_contracts.llm`. The schemas are strict-compatible, so bounds are checked in code, not in the schema:
```python
EntityMention.type: … | "chokepoint" | "region"                       # + port, lane (M1)
DisruptionExtract.event_type: EventTypeKey   # SUP.delay, SUP.capacity_loss, LOG.chokepoint_closure, LOG.port_closure,
                                             # LOG.strike, NAT.storm, NAT.flood, DEM.spike, DEM.drop, OTHER
class ScenarioUpdate: key: Literal["delay_days","duration_days","demand_change_pct","add_receipts","remove_receipts"]
    value_int: int | None; value_ids: list[str] | None; evidence_id: str; json_pointer: str
class Findings: …; scenario_updates: list[ScenarioUpdate]              # required (stored pre-M4 rows read as [])
class InvestigationTask: id; specialist; objective; tools: list[str]; depends_on: list[str]
class InvestigationPlan: rationale; tasks: list[InvestigationTask]; budget: PlanBudget{max_parallel, max_tasks}
    # plan_violations: max_parallel ≤ 4, max_tasks ≤ 6, objective ≤ 200 chars, known specialists, tools ⊆ allowlist, acyclic
class Critique: verdict: Literal["PASS","REVISE"]; issues: list[CritiqueIssue{task_id|None, issue, severity}]; revise_task_ids
class ConstraintDelta: delay_days|None; duration_days|None; exclude_levers: list[LeverCode]; sku_filter: list[str]; max_cost_cents|None
class FollowupClassification: kind: Literal["EXPLAIN","RESIMULATE","REINVESTIGATE"]; constraint_delta; question
class GuardVerdict: injection_suspected: bool; reason: str
class UntrustedItem: source_evidence_id; kind: delay_notice|capacity_notice|quality_issue|price_change|guidance|other
    entity_mentions; delay_days: int|None; quote: str                 # quote ≤ 200, mention ≤ 80 (untrusted_extraction_violations)
class UntrustedExtraction: items: list[UntrustedItem]; injection_suspected: bool
```

## M4 run envelope (updated M4, 2026-10-07)
- **action:** `{type: "apply"|"guide"|"promote"|"confirm_assumptions", option_id, plan_version, assumption_keys?}`.
  - `assumption_keys` (unique, ≤ 50 = `MAX_PAYLOAD_KEYS`) is required for `confirm_assumptions` and refused for every other type. It is omitted from dumps while empty.
  - `pre_route` passes at most 50 non-empty keys on to `confirm_assumptions`.
- **resume:** `{interrupt_id, decision, plan_version?, change_set_hash?, edits?, removed_checks?, decided_by}`.
  - `edits` is either M1 free text or a list of `ApproachEdit {key, value_int | value_ids}`. Each edit sets exactly one value; keys match `^[a-z][a-z0-9_]*$`, are ≤ 64 chars and unique; at most 50 edits; `value_ids` holds 1–500 unique ids.
  - `removed_checks` is a list of plan task ids (unique, ≤ 50).
  - Typed edits and `removed_checks` are allowed only with `decision: "edit"`. An `edit` needs text, typed edits or removed checks.
  - Before applying, `approach_router` checks typed edits against the stored card with `blocks.approach_edit_violations`: the key must be editable and of the right kind, `days ≥ 0`, `pct ≥ -100`, removed checks must be card task ids, and at least one check must remain. On a violation the case is re-framed without the edit (`edit_errors`).
  - Free text is read deterministically by `framing.parse_edits` ("10 days", "2 weeks", "+20%").
- **Known gap (as built):** engine-api does not accept typed edits yet. `POST /api/interrupts/{id}/decision` takes `edits` only as a string and has no `removed_checks`, and `core.interrupts.record_decision_ex` stores `edits` as text. A typed Edit from the web is therefore refused with 422 (see `30`, M4 REST). The runtime path (ResumePayload → approach_router → frame_case → planner) is complete and tested offline.
  (updated M5, 2026-10-07: closed. The decision route accepts typed `edits` and `removed_checks`, and `record_decision_ex` passes them on in `ResumePayload`; see `30` "M5 REST".)

## M4 investigation, quarantine and review (updated M4, 2026-10-07)
- **Specialists** (`AgentSpec.specialists`). Each wrapper is the function node `<slug>`, which runs `<slug>_investigator` and then `<slug>_structurer`:

| slug | read tools (`runtime/spec.py`) | quarantined sources → wrapper tool |
|---|---|---|
| `inventory_demand` | get_inventory, get_demand_forecast, list_allocations, list_purchase_orders, get_purchase_order, list_locations, get_bom_where_used | — |
| `sourcing` | list_purchase_orders, get_purchase_order, get_bom_where_used, get_factory_schedule, search_entities, get_entity | `supplier_messages` → `read_supplier_messages_structured` (`list_supplier_messages`) |
| `logistics` | list_sailings, list_shipments, get_lane_options, list_purchase_orders, get_purchase_order | `events` → `read_events_structured` (`list_events`) |
| `playbook` | — | `knowledge_base` → `search_playbooks_structured` (`core.kb.search`) |

  A task binds only its plan `tools` (a subset of the allowlist). `BudgetPlugin` caps tool calls per investigator and task at `SpecialistSpec.max_tool_calls` (≤ 15). The task's `assumption_keys` come from `SpecialistSpec.relies_on`, else `spec.SPECIALIST_RELIES_ON`.
- **Quarantine** (`runtime/quarantine.py`). An investigator never gets an `untrusted_text` tool, and `PolicyPlugin` admits only wrapper tools marked `content_trust="structured_inference"`. One wrapper call:
  1. Fetches deterministically with the reader principal, using only the source's own filters, and stores each text as a raw evidence row (`args.content_trust="untrusted_text"`).
  2. Screens each text with a pattern check plus the `guard` agent, in parallel. A flagged text, or one whose screen fails, is excluded and audited (`untrusted_text.flagged`).
  3. Runs the `extractor` agent over the clean texts. A self-flagged extraction, or items that violate the limits, are dropped and audited (`untrusted_text.extraction_flagged`).
  4. Stores the usable items as an evidence row with prov `inference` (`content_trust="untrusted_extraction"`; quotes are kept for people).
  5. Hands the investigator only `{source_evidence_id, kind, entity_mentions, delay_days}` per item.

  Guard and extractor run as child nodes (`use_sub_branch=True`), so their content never enters the investigator's context. Limits per task: each wrapper at most 3 calls (`MAX_CALLS_PER_TASK`); each call screens up to `investigation.quarantine_max_items` (8) texts. KB search uses `investigation.kb_top_k` (6) and `kb_ids`.
- **Downstream, untrusted stays untrusted:**
  - `calc` refuses untrusted rows.
  - `verify_numbers` accepts a value inside an untrusted row only when it is an integer, and downgrades such claims to `inference`.
  - Narratives resolve `{{ev:…}}` through `quarantine.display_results`: this case's rows only, with untrusted rows reduced to their numbers.
  - `refine_scenario` never treats an untrusted or `_structured` row as fact-backing. A value from one therefore needs a human confirmation (`confirm_assumptions`) before Apply is enabled.
- **Budgets** (`AgentSpec.budgets`): 90 LLM calls and 1.2 M tokens per run, under the platform ceilings `PLATFORM_MAX_LLM_CALLS` (120) and `PLATFORM_MAX_TOTAL_TOKENS` (2 M). Bootstrap's `agent_specs.upgrade_builtins` adds the M4 built-in specialists to a tenant spec whose history never had them, and raises budgets still at the M3 default to the current default.

## M4 testing (updated M4, 2026-10-07)
- `FakeLlm` script keys are the ADK agent names: `planner`, `<slug>_investigator`, `<slug>_structurer`, `guard`, `extractor`, `critic` and `followup_classifier`, plus the M1 keys. The whole M4 graph runs offline (`pytest -m "not live"`) in `tests/runtime/test_m4_*.py`: investigation, review_hardening, framing_* (taxonomy, compile, map, actions), refine, followup, units, spec_admin and repo_integration. They use the Act 2 ProblemData fixture in `test_m4_framing_support.py`.
- The live golden and adversarial cases run through `evals/` (`90` §9) against the real stack and need working model credentials.

## M5 graph (updated M5, 2026-10-07) — authoritative
`runtime/workflow.py` adds three nodes and new routes to the M4 graph. Every M5 node is a deterministic function node: M5 adds no LLM call.
```
START → pre_route: action:apply  → compile_changeset {option_id, plan_version, mode: "apply", origin: "chat"}
                   action:guide  → compile_changeset {…, mode: "guide", origin: "chat"}
                   action:rollback {change_set_id}            → prepare_rollback
                   action:checklist_step {checklist_id, seq, status} → checklist_step → END
                   signal {signal_id}                         → prepare_replan   (no LLM classification)
                   (chat, promote, confirm_assumptions as M4) | DEFAULT_ROUTE → reject_run
compile_changeset: COMPILED → dry_run | GUIDED → end_turn | DEFAULT_ROUTE (refused) → close_case
prepare_rollback:  ROLLBACK → approval_gate | DEFAULT_ROUTE → close_case
prepare_replan:    COMPILE → compile_changeset | DONE → end_turn | DEFAULT_ROUTE → close_case
dry_run:           VALIDATED → approval_gate | DEFAULT_ROUTE → close_case
approval_gate → approval_check: APPROVED → execute | REVISE | REJECT | DEFAULT_ROUTE → close_case
execute (rerun_on_resume) → reconcile → END
```
- `approval_gate`, `approval_check`, `execute` and `reconcile` serve both apply and rollback. `ctx.state["commit_mode"]` is `apply` (default) or `rollback`.
- **Auto approval.** `approval_gate` yields `{decision: "approve", auto: true}` and `approval_check` routes APPROVED without an interrupt. `approval_check` takes the auto route only when the gate's own decision for this run was `auto`, never for a rollback. It refuses a resume decision for any interrupt other than the one the gate recorded (`approval.interrupt_id`).
- The state keys of the commit path (`bundle`, `approval`, `initiator`, `run_origin`, `commit_mode`, `rollback`, `checklists`) are listed in `M5_NODES.md`.

## M5 run envelope (updated M5, 2026-10-07)
- **action** (`keelson_contracts.runs.ActionPayload`), `type: "apply"|"guide"|"promote"|"confirm_assumptions"|"rollback"|"checklist_step"`. A type takes only its own fields; a missing or extra field is a validation error.
  - The option types carry `option_id` + `plan_version` (and `assumption_keys` for `confirm_assumptions`, as M4).
  - `rollback` carries only `change_set_id`.
  - `checklist_step` carries `checklist_id`, `seq` and `status: "done"|"skipped"`.
  - The new optional fields are omitted while unset, so pre-M5 payloads serialize unchanged.
- **`checklist_step` has no API caller.** engine-api never enqueues it: `POST /api/cases/{id}/actions` does not accept the type. People tick steps through `POST /api/cases/{id}/checklists/{cid}/steps/{seq}`, which updates the platform directly (`30` "M5 REST").
  - The node is kept for run-based callers. It accepts only `done`/`skipped`, never downgrades `verified`, audits `checklist.step.<status>` by `user:<username>`, and re-emits the block.
- **signal:** `{signal_id}` (`SignalPayload`) = a `case_signal` row id written by the webhook re-plan router (`30` "M5 webhooks"). The run's actor is `webhook:<connector>` with no roles, and its policy origin is `webhook`.
- **resume:** M4 fields plus `reason` (1–500 chars, only with `reject`/`revise`). `decided_by[]` lists the executor (the member counted as `approver`) first, each with the `role` they were counted under.

## M5 stages (updated M5, 2026-10-07)
`NODE_STAGES` maps the M5 nodes (updated M5 polish, 2026-10-07): `prepare_replan` → `building_options`, `prepare_rollback` → `preparing_changes`, `checklist_step` → `applying`. `keelson_contracts.agui.STAGELESS_NODES` lists the nodes that deliberately leave the stage unchanged (pre_route, route_intake, reject_run, end_turn, close_case, compose_smalltalk, answer_investigator, answer_structurer, compose_answer); an engine test fails when a workflow node is in neither list. A rollback run shows `preparing_changes` → `awaiting_approval` → `applying` → `verified`. A signal re-plan shows `building_options`, then `preparing_changes` onwards when it compiles. No M5 node emits `SUBAGENT_*`.

## M5 approval policy and quorum (updated M5, 2026-10-07)
- **Engine.** `runtime/policy.py` evaluates `AgentSpec.policy.rules` (`keelson_contracts.policy.PolicyRule`) with **cel-expr-python 0.1.3** (CEL C++ runtime; `cel-python` is not installed). The variables `cs`, `case` and `initiator` are `map(string, dyn)` built from `PolicyContext`.
- **Evaluation.** Ascending `priority`, first match wins, with two hard floors (`PolicySet.decide`): a what-if case or unconfirmed values are always refused, and `auto` applies only to a person's chat run on untainted input.
- **Fail closed.** A rule that does not compile, reads an unknown field, errors or yields a non-bool raises `PolicyEvaluationError`, and `approval_gate` refuses (nothing is applied). No rule matched → one `approver`.
- **Default rules** (`runtime.policy.default_rules` = `core.policies.DEFAULT_RULES`), in this order:

| priority | id | when | require |
|---|---|---|---|
| 10 | `refuse-unconfirmed` | `case.mode == "what_if" \|\| case.unconfirmed > 0` | refuse |
| 20 | `four-eyes` | `cs.incremental_cost_cents > 1000000 \|\| cs.ops.exists(o, o in ["po.create","po_line.cancel","production_order.resequence"])` | `{roles: [approver, compliance], count: 2, exclude_initiator: true}` |
| 30 | `human-untrusted` | `case.origin != "chat" \|\| case.tainted` | one approver |
| 40 | `auto-small-transfer` | only `allocation.set_qty`/`transfer_order.create`, `cs.lines <= 10`, `cs.units <= 500`, initiator is a planner | auto |
| 50 | `default` | `true` | one approver |

  `four-eyes` comes before `human-untrusted` (`95` §1 listed them the other way round), so a webhook re-plan that creates POs still needs both people. A signal run is never auto-approved either way. The caps and role names come from `PolicyConfig` (`auto_max_lines`, `auto_max_units`, `compliance_role`, `auto_initiator_role`, `four_eyes_cost_cents`, `four_eyes_ops`).
- **Context.**
  - `cs` is built from every bundle member: lines, units, the dry-run `value_cents`, ops, connectors, the option's incremental cost, `kpis_after.units_at_risk` (as `lost_units_after`) and levers.
  - `case.unconfirmed` counts the scenario assumptions still `inference`/`assumption`.
  - `case.tainted` means an assumption's `source_evidence_ids` point at quarantined untrusted rows.
  - `case.origin` is `chat` for people and `webhook` for signal runs. A node input can only make the origin stricter.
- **Quorum.** The matched requirement becomes `interrupt.quorum_spec` and is stored in the change set's `result.requirement`, which a rollback uses again. The platform counts the decisions (`30` "M5 REST"). `approval_check` re-counts `resume.decided_by` with `quorum_assign` (the initiator is excluded for chat runs).
- **Executor.** The executor is the member counted as `approver`, whatever the approval order; its delegated token applies. The other members go to the receipt's `co_approved_by`.
- **Auto.** The `autocommit` principal applies, with receipt `approved_by: policy:<rule_id>@<policy version>`.

## M5 Guide mode (updated M5, 2026-10-07)
- **Capability table** (`candidates.capability_table`). The write connectors are `spec.connector` plus `spec.remediation.connectors`, in that order. A connector applies an op when:
  - its change-set tools are approved;
  - `apply_change_set` has enforced idempotency;
  - the op is in `config.supported_ops`, else in the `add_change_set_items` schema, else in the M1 ops.
- **Partition** (`remediation.partition_actions`). An apply line goes to the first connector that applies its op. Everything else, or every line in Guide mode, is a guide line. An atomic group is never split: a group with any guide line, or with lines on two connectors, is guided whole.
- **On the option card.** `simulate` stores `option.remediation = {apply_lines, guide_lines}`, and `compose` sets `recommended_option.remediation_mix` with the Apply / Guide me labels.
- **Checklists.** `compile_changeset` builds one checklist per connector for the guide lines (`runtime/guide.py`), stores it through the platform (`core.checklists.create_checklist`) and emits `remediation_checklist@1`. An open checklist with the same guided lines (`meta.lines_fingerprint`) is reused. A pure Guide ends `GUIDED` → `end_turn`; a mixed plan continues with the apply lines.
- **Where the steps come from.**
  - From the connector's `get_operator_guide` when it is an approved `read` tool classified `trusted_data`. The guide lines go into a draft change set (preparer, never validated or applied) and the reader reads the guide.
  - Otherwise they are generated from the action payloads, with optional `config.deep_links {op: template}`, plus one screened SOP excerpt per op from the KB (deterministic screen; links and markup stripped; no model sees the text). These steps are labelled `inference`.
  - Unsafe deep links are dropped.
- **Verification.** A step is `verified` only by read-back (`core.checklists.verify_checklists`, reader principal, evidence row `prov: fact`). It never counts an entity that existed when the checklist was created, one made by an applied change set, or one entity for two steps.
- **Worker loop.** Every `WORKER_GUIDE_TICK_INTERVAL_S` (5 s) the worker calls `core.checklists.poll_checklists()`, which verifies on new connector events or every 60 s. The runtime's `guide.GuideVerifier` is the fallback when that port method is missing.

## M5 rollback (updated M5, 2026-10-07)
- **Refusals.** `prepare_rollback` refuses unless the change set belongs to the case, is `applied`/`verified`, and its connector has `rollback_change_set` approved.
- **Approval.** Always human: the stored original requirement, else the rules evaluated with `allow_auto=False`, else one approver.
  - Interrupt `approval-rb-<cs>-<attempt>`, with a new attempt per request (`change_set.result.rollback_requested`).
  - Its `change_set_hash` is the original H and its `plan_version` the latest plan.
- **Execution.** `execute` calls `rollback_change_set` with a fresh receipt (`30` "M5 receipts") and idempotency key `sha256(tenant|cs|rollback|attempt)`. While the call is in flight the row is `rolling_back`. A crash-retry reads it back first, and a refusal or conflict restores the previous status, so the batch stays undoable.
- **Conflicts.** The change set stays applied, `result.rollback_conflicts` is stored and the tracker lists them.
- **Success.**
  - The change set is `rolled_back` (ledger `rolled_back`; the read-back must agree).
  - The plan version becomes `rolled_back`.
  - A recovery plan v+1 is saved once per rolled-back change set: same options, recommends `do-nothing`, with `kpis.recovery`.
  - The tracker shows `rolled_back` with no Undo.

## M5 re-plan (updated M5, 2026-10-07)
`prepare_replan` (`nodes/replan.py`) handles a `signal` run without LLM classification:
- **Idempotent per signal.** When the latest plan's `kpis.replan.signal_id` is this signal, nothing is re-planned. Its delta is compiled only if that plan version has no change set yet; otherwise the run ends `DONE`.
- **Inputs.** Only `lane.capacity_changed` signals are used, with their typed fields: `lane_code`, `factor_bp`, `weeks`, `from_week`/`to_week`, and `weeks_detail[].overbooked_units`. Other types end `DONE`. A plan that does not depend on the lane (`kpis.dependencies.lanes` or committed lines) gets a short note and `DONE`.
- **Scenario and data.**
  - Scenario v+1 adds `capacity_scale` on the lane as `fact`, over Merxa's window. An earlier fact on the same lane is replaced.
  - A bounded resync follows (`remediation.replan_resync_timeout_s`, default 20 s), else the current snapshot is used. A fresh snapshot already holds the cut, so it is not applied twice.
- **Committed plan.** Only the committed option is re-simulated, and its **delta** (`remediation.delta_actions`) becomes plan v+1's option. The delta holds:
  - new lines not yet applied;
  - changed set-ops;
  - compensations on the cut lane. Over-booked weeks are capped at `booked − overbooked` with `capacity_booking.cancel` (the booking code from the apply read-back) plus a smaller `capacity_booking.create`. Sea-air splits that no longer fit get `po_line.reschedule` back to the line's ETA, with `expected_version` read back through `get_purchase_order`.
  - It is compiled at once with `origin: webhook`, so it always needs a human approval.
- **Open plan.** The options that use the lane are re-simulated, giving plan v+1 with no compile.
- **Output.**
  - `narrative@1` with `banner` ("New information: sea-air capacity on X cut to 30% — plan updated (v2)"), `plan_versions` and `what_changed`, plus the delta `plan_diff`.
  - The previous version becomes `superseded` unless it is committed.

## M5 bundles (updated M5, 2026-10-07)
- **One change set per connector.** `compile_changeset` emits one per connector, in capability-table order. Each member has its own H, dry-run (preparer), receipt and idempotency key; atomic groups are never split.
- **One approval.** A single interrupt binds the **bundle hash** (`keelson_contracts.changeset.bundle_hash`). For one member it is that member's H, unchanged from M1. For several members it is `sha256_hex({"v": 1, "change_sets": [{connector, hash}, …]})` in apply order.
  - The interrupt payload then has `change_set_ids` + `bundle_id` (`bundle-<first id>`) and no `change_set_id`.
  - The card lists the members in `approval_card.bundle`.
- **Failures.** If any member's dry-run fails, every member is marked `invalid` and nothing is applied. `execute` applies the members in order, each exactly once. If a later member is rejected or conflicts, the earlier ones stay applied; `reconcile` reads them back and reports both, and the plan is not marked committed.
- The bundle path is tested with a fake second connector; the demo uses Merxa only.

## M5 testing (updated M5, 2026-10-07)
- **Offline tests** (`pytest -m "not live"`, FakeLlm; M5 adds no LLM keys):
  - runtime: `tests/runtime/test_m5_*.py` (policy, guide, remediation, flows, worker, act3, integration_fixes, er_review);
  - platform: `tests/platform/test_m5_*.py` (actions, checklists, demo_reset, policies, ports, quorum, receipts, replan);
  - Merxa: `services/merxa-api/tests/test_m5.py`.
- **Real stack without model calls:** `e2e/api_m5_offline.py`, `e2e/api_m5_autocommit.py`, and Playwright `m5-offline.spec.ts`. The Playwright spec covers the M5 blocks on `/dev/blocks`, the Policies page and the demo reset; its screenshots 22–26 come from fixture and admin pages.
- **Need model credentials:** `e2e/api_m5_act3.py` (the live Act 3 story) and the eval case `evals/cases/signal_origin_never_auto.yaml`. `95` §8 named a Playwright `act3-full.spec.ts`; it is not in the repository. `act3-thin-slice.spec.ts` is the M1/M4 live journey.
