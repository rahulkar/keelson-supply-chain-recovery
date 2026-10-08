# M4 investigation: planner, specialists, quarantine, verify, critic

Owned by the investigation engineer: `nodes/investigate.py`, `nodes/review.py`, `runtime/quarantine.py`,
the wiring in `runtime/workflow.py` and the stand-ins in `nodes/m4_defaults.py` (used only when a
framing-owned node is missing; see `M4_NODES.md` for the framing, refine and follow-up nodes).
Contract: `docs/contracts/90-m4-investigation.md` §2–§5.

## Graph

```
frame_case → quick_exposure → planner → approach_gate → approach_router
  CONFIRM | WHAT_IF → investigate → verify_numbers ─OK→ critic ─PASS→ refine_scenario → impact → simulate → recommend → compose
                                    └─REVISE→ investigate     └─REVISE→ investigate
  EDIT → frame_case (the planner then reuses the plan minus removed_checks)
promote_case | confirm_assumptions | explain | prepare_resimulate ─RESIMULATE→ impact ; DONE → end_turn
followup_delta ─EXPLAIN→ explain | RESIMULATE→ prepare_resimulate | REINVESTIGATE→ prepare_reinvestigate ─→ investigate
```

`verify_numbers` and `critic` share **one** REVISE per run (`ctx.state["revise_budget"]`,
`AgentSpec.investigation.revise_budget`, default 1). Every router has a `DEFAULT_ROUTE`.

## Nodes

| Node | Input | Output / routes |
|---|---|---|
| `planner` (`make_planner(agent)`) | `quick_exposure` output | Input + `{plan_evidence_id, plan_source, plan, checks: [ApproachCheck], agents: [ApproachAgent], plan_task_count, plan_auto_confirm}`. LLM `planner` (`models.planner`, `InvestigationPlan`, tool-free) sees the scenario and `AgentSpec.planner_catalogue()`; trivial issues are repaired (`normalise_plan`), an unusable plan or a failed planner falls back to `default_plan` (by event family). One specialist ⇒ no LLM call. After an approach **Edit** the stored plan minus `removed_checks` is reused (`plan_source="edited"`). Stored as evidence `tool="investigation_plan"` (prov `inference`). |
| `investigate` (`make_investigate(wrappers)`) | `{scenario_version, mode}`, or a subset `{revise_task_ids \| task_ids \| dirty_task_ids, notes?: {task_id: [str]}, plan?}` (`t1` or `t1-r2`) | `{scenario_version, mode, findings: [Findings] (plan order, reused ones included), ran, plan_evidence_id}`. Waves from `plan_waves`; per wave `asyncio.gather` + `ctx.run_node(wrapper, run_id="task-<id>-r<rev>", use_sub_branch=True)`, at most `min(plan.budget.max_parallel, investigation.max_parallel)` at once. A failing specialist ⇒ `Findings(status="failed")` with a generic gap. Each task gets the upstream findings of its `depends_on` and its revision notes. |
| `verify_numbers` | `investigate` output | `OK` → input with verified `findings` + `verify` report; `REVISE` → `{revise_task_ids, notes, reason:"verify"}` when > `verify_max_failed_bp` (20%) of numeric claims fail and budget is left. |
| `critic` (`make_critic(agent)`) | `verify_numbers` output | `PASS` → input + `critique` + `plan`; `REVISE` → `{revise_task_ids, notes, reason:"critic"}` (budget left, valid critique, at least one `high` severity issue on a listed task or the whole investigation — a medium/low-only REVISE counts as PASS and keeps the budget). An unusable critique or failed critic counts as PASS; a second review in the same run is skipped once the budget is spent. On PASS the findings state is saved (`tool="investigation_findings"`). |

## State and rows

* `ctx.state`: `investigation_plan`, `plan_evidence_id`, `findings_by_task` (plan task id → verified
  Findings), `task_revs`, `revise_budget`, `verify_report`, `critique`; `findings` (the list handed to
  the Strategist, set by `impact` from its input).
* `investigation_task.id` = `<case_id>:<task id>:r<rev>` (global primary key); its `input` =
  `{task_id, rev, objective, tools, depends_on, assumption_keys, scenario_version, run_id}`.
  `repo.list_investigation_tasks` returns `{task_id: "<task id>-r<rev>", specialist, status, input, findings}`.
* `assumption_keys`: `SpecialistSpec.relies_on` or `spec.SPECIALIST_RELIES_ON` (the keys a
  specialist's findings depend on), so a follow-up re-runs only the tasks a changed value touches.
* Later runs of a case start with an empty `ctx.state`: `impact` / `simulate` call
  `case_state.ensure_planning_state` (problem data, snapshot, findings from the latest rows).

## Quarantine (`runtime/quarantine.py`)

`SpecialistSpec.quarantined` ⊆ {`supplier_messages`, `events`, `knowledge_base`} adds the wrapper
tools `read_supplier_messages_structured` / `read_events_structured` / `search_playbooks_structured`
(never the MCP tools themselves). A call: deterministic fetch (reader principal; approved read tool
required; KB via `deps.kb` → `core.kb.search`) → one raw evidence row per text
(`args.content_trust="untrusted_text"`) → pattern screen + `guard` agent per text in parallel
(flagged or failed ⇒ excluded, audit `untrusted_text.flagged`) → `extractor` agent over the clean
texts (`UntrustedExtraction`; flagged extraction or violations ⇒ dropped, audited) → extraction row
(prov `inference`, `content_trust="untrusted_extraction"`, quotes kept) → the investigator receives
only `{source_evidence_id, kind, entity_mentions, delay_days}` per item (no quotes; instruction-like
mentions dropped). Guard and extractor run as child nodes with `use_sub_branch=True`, so nothing
they read enters the investigator's context. `PolicyPlugin` accepts only wrappers whose metadata
says `content_trust="structured_inference"` and still denies raw `untrusted_text` tools.

No side doors back out of the quarantine:

* The translator drops every event of the `guard` / `extractor` nodes (`QUARANTINE_NODE_NAMES`):
  their reasoning would otherwise be streamed and persisted under the specialist's row.
* `calc` refuses rows marked `untrusted_text` / `untrusted_extraction` (a calc is a fact) and rows of
  other cases; calc errors name a value's type, never the value.
* `verify_numbers` accepts a claim value inside an untrusted row only when it is an integer (a
  string there is supplier or document text) and downgrades a claim with any placeholder into
  an untrusted row to `inference`.
* Narratives (`compose`, `compose_answer`) resolve `{{ev:…}}` through `quarantine.display_results`:
  this case's rows only, and untrusted rows reduced to their numbers. A narrative becomes case
  history, which every later prompt (also tool-holding investigators) reads.
* A task may call each wrapper at most `MAX_CALLS_PER_TASK` (3) times (each call costs up to
  `quarantine_max_items` guard calls + one extractor call); identical reads in one run are screened
  once (single flight) and shared between tasks.
* The event feed is paged with the largest declared `limit` and only the newest
  `quarantine_max_items` entries are kept while paging.
* Task revisions: a re-run and the first investigation of a later run continue from the highest
  revision recorded in `investigation_task`, so row ids never collide with an earlier run's.

## Spec (`runtime/spec.py`)

`specialists` default: `inventory_demand`, `sourcing` (+supplier messages), `logistics` (+events),
`playbook` (KB only), M6 `external_intel` (Signal Hub wrappers only, see `M6_SIGNALS.md`). `models.planner` (`reasoning-default`), `critic` (`critic-default`, must differ
from the planner), `guard` (`guard-default`), `extractor` (`extract-fast`), `followup` (`fast-default`).
`investigation`: `max_parallel` 4, `max_tasks` 6, `revise_budget` 1, `verify_max_failed_bp` 2000,
`auto_confirm_max_tasks` 2, `quarantine_max_items` 8, `kb_top_k` 6, `kb_ids`. Budgets: 90 LLM calls,
1.2 M tokens per run.

## Token diet (2026-10-07)

* **Tool results** (`plugins.compact_for_model`, ~3k tokens per result at most): a top-level list
  longer than `LIST_TOP_N` (10) shows its first rows plus `_summary.<list> = {total, shown, sums,
  counts, range}` over **all** rows; lists inside rows keep 3 entries. The stored evidence keeps the
  whole result; `/_summary/<list>/total|sums/<col>|counts/<col>/<value>|range/<col>/min|max` resolve
  virtually against it (`jsonptr.list_summary`), so a cited total passes `verify_numbers`.
* **Turn cap**: `investigation.max_investigator_turns` (default 6) model turns per investigator task
  (`BudgetPlugin`); the last turn runs with `tool_choice=none` plus a "write your findings now" note,
  a turn beyond the cap is answered with a fixed note without calling the model.
* **Briefs**: `scenario_brief` lists matched entities as `type:code label` and summarises large
  perturbation targets (`id_count` + `ids_by_code`); the analyst brief drops the task's tool list and
  empty upstream / revision notes; the planner brief drops the limits (in its instruction); the critic
  sees the plan's tasks only (no rationale / budget / tools) and at most 12 claims per task; the
  Strategist sees each KPI meaning once (`kpi_meanings`) and `user_request`; investigators and the
  Strategist get the case title only (no message history). The structurer's evidence index is
  `{id, tool, args (shortened), rows, prov?}` per row.
* **Strategist repair** only for numbers-rule violations in the headline or a bullet
  (`planning.repair_needed`); slips in why-not / risks / next steps are stripped deterministically.
* **Metering** (`runtime.usage`): per-agent calls, prompt / completion / reasoning / cached tokens,
  wall time and LiteLLM cost (proxy `x-litellm-response-cost` via a LiteLLM success callback) on
  `RunContext.usage`; logged as `run.usage` and sent as `metadata.usage` on the run's final event.
