# M7 runtime: custom specialists, playground, advisory plans, tier-A per-op commit

Contract: `docs/contracts/110-m7-backoffice-generic.md` §3 and §4. This file covers the runtime (E2)
side. The admin API (specialist builder PUT, mapping editors, dry-run route, change requests) and the
crawl itself (`core/scn_crawl.py`, `core/mapping_eval.py`) belong to the platform and are described
in the engine README.

## Custom specialists (`runtime/custom_specialists.py`, `runtime/spec.py`)

* `AgentSpec.custom_specialists` holds `CustomSpecialistSpec` entries (contracts). The runtime view
  is `spec.CustomSpecialist`, which has the same interface as a built-in `SpecialistSpec`.
  `AgentSpec.all_specialists` lists the built-ins first, then the **enabled** custom specialists.
  A custom slug that equals a built-in slug is refused when the spec is validated, so
  `specialist_of()` can never be ambiguous.
* Planner: `planner_catalogue()` has one entry per specialist:
  `{name, display_name, description, tools, custom: true}`. The description is admin data on a
  single line of at most 300 chars. `plan_violations` uses `catalogue()`, which lists each tool
  and its `<tool>_structured` wrapper.
* Workflow: `workflow.build` creates `custom_investigator(...)` with the specialist's
  `model_alias`. A Pro model is refused here as well (`is_pro_model`). The investigator gets one
  `CustomSpecialistToolset` per connector, up to three. The structurer is the tenant's
  `models.structurer`, and the `Findings` contract and `make_specialist_wrapper` are the same as
  for the built-ins.
* Tools: the toolset binds approved `read` tools that return `trusted_data` and still match the
  live definition hash. An approved read tool that returns `untrusted_text` (or is in
  `UNTRUSTED_SOURCE_TOOLS`) is **auto-wrapped** as `quarantine.UntrustedToolWrapper`, named
  `<tool>_structured`. That wrapper sends only the declared scalar arguments, stores the raw rows
  as `untrusted_text` evidence, applies the pattern screen and the guard, and the extractor
  returns structured items. The investigator never sees the text.
  A write, destructive, unapproved, missing, reserved or duplicated tool is recorded in
  `run.toolset_errors`. `require_toolset_ok` then fails the agent **before its first model call**.
  In a case run the task fails on its own and the other tasks are not affected.
  `PolicyPlugin` still refuses anything that is not a read tool, whatever the allowlist says.
* `kb_ids` add the KB quarantine wrapper, limited to those knowledge bases.
* Budgets: `max_tool_calls` is the specialist's per-task tool cap (`PolicyPlugin`). `max_turns`
  is its per-task turn cap (`BudgetPlugin`): the last turn has `tool_choice=NONE`, and any turn
  after that is answered without calling the model.
* Instruction: the fixed investigator text comes first. The `role_prompt` follows as a
  delimited data block (`role_prompt_block`): it is sanitised and never templated, and it cannot
  close its own fence. `GUIDANCE_PRECEDENCE` comes last, so the numbers rule and the output
  schema win.
* Translator: `execute` passes `display_names` and `custom_agents`. A custom agent's
  `SUBAGENT_STARTED` carries its display name as `description`, emitted with `custom=True`.

## Playground (`runtime/playground.py`)

`run_playground(PlaygroundRequest, *, tenant, spec, models, actor, deps=None, emit=None,
playground_id=None, controller=None) -> PlaygroundResult` runs one investigator and structurer turn
of a saved specialist (by slug, including a disabled one) or of a draft.

* The workflow is a single node, `playground_turn`. It runs the specialist wrapper as a dynamic
  child on its own sub-branch, the same way `investigate` runs a task, so history and the
  quarantine's guard and extractor calls are scoped exactly as in a case run. A `TaskBinding`
  (`task_id="playground"`) applies the turn cap and the per-task tool cap.
* Scratch case: an in-memory ADK session. `ScratchRepo` keeps evidence in memory with contract
  `ev_…` ids. Every other repo call raises `PlaygroundWriteError`. Security audit rows
  (`tool.denied`, `untrusted_text.flagged`) are forwarded with `playground: true`. Interrupts are
  refused, and checklists, signals and plans ports are `None`. Nothing commits, so `committed` is
  always `false`.
* Budget: `min(tenant budget, PLAYGROUND_MAX_LLM_CALLS=30 / PLAYGROUND_MAX_TOKENS=300k)`. A
  breach returns `status="budget_exceeded"`.
* Streaming: the normal `Translator` writes to `emit(event_dict)` (`RUN_STARTED … RUN_FINISHED |
  RUN_ERROR`), and nothing is persisted. A failure is reported with an `error_id` and never
  raised; the codes are `configuration_error`, `write_refused`, `no_findings`, `budget_exceeded`
  and `internal_error`.
* API wiring (engine-api): validate a draft with `custom_specialist_violations` first. Build
  `models` with `runner_cache.default_bindings(tenant, spec).models` (the tenant's LiteLLM key)
  and pass a `deps` that has the reader principal's MCP gateway.

## Advisory plans (`runtime/advisory.py`)

When the snapshot a plan was built on is labelled `consistency: crawled` (tier-A crawl, with
`as_of_range`), the run stores it in `plan.kpis.snapshot_label`. `advisory_from_kpis` /
`advisory_from_label` then return `blocks.Advisory`. A malformed range is repaired, never dropped.

* `recommended_option@1`, `scenario_compare@1` and `narrative@1` carry `advisory`. The
  narrative appends `ADVISORY_SENTENCE` (it has no digits, so the numbers rule still holds) and
  is titled "Recommendation (advisory)".
* The scenario's `data_source` assumption is the explicit statement, for example: "… data gathered
  by paging, not a consistent snapshot, so the options are advisory"
  (`framing.data_source_assumption`).
* A pinned crawled snapshot stays advisory on later runs (`ScnPort.get_snapshot`).
* Advisory plans can still be applied, per op and with approval as usual.

## Tier-A per-op commit (`runtime/tier_a.py`, `nodes/per_op.py`)

* Capabilities (`candidates.connector_capabilities`): a connector without change-set tools but
  with a valid action mapping gets `commit_mode="per_op"`. For each op,
  `per_op_capabilities` requires the action tool to be an approved write tool and the read-back
  tool to be an approved read tool. The effective idempotency is the mapping's claim, capped by
  the tool's confirmed one. A non-enforced op without a read-back is guided, and so is an
  enforced op without `request_id_arg`.
* Routing (`remediation.partition_actions`): unmapped ops become guided steps whose instructions
  are generated by the M5 guide. An atomic group on a per-op connector is applied in bulk only if
  `atomic_group_bulk_allowed`, that is, every member op is `enforced`. Otherwise the whole group
  is guided.
* Dry run (validate): every line's call is **rendered**, never executed, with the engine's safe
  evaluator (`core.mapping_eval`, after `template_refs` validation; no eval, no attribute access).
  The calls and their `calls_hash` are stored in the change-set preview (`mode: per_op`). Policy
  and approval work as usual. At apply time the calls are rendered again, and a different
  `calls_hash` (the mapping changed after approval) is refused with nothing sent.
* Ledger (`PerOpCommitter`): the state is persisted as `attempted` **before** each call, then set
  to `succeeded`, `in_doubt` (transport error or transient tool error) or `failed` (definitive
  refusal; becomes a guided step). The next action comes from
  `keelson_contracts.commit.ledger_next_action`:
  * `enforced`: retry with the **same** per-line request id (`<change-set key>:<seq>`), for at
    most `MAX_AUTO_ATTEMPTS` attempts;
  * `best_effort`: read back before every retry (a read-back counts only for the attempt it
    followed). If the effect is found the line is `verified`;
  * `none`: never retried automatically. The line is read back, and if the effect is not found it
    becomes a guided step.
  * Baseline: for `best_effort` and `none`, the read-back also runs once before the first call.
    If it already matches (an existing entity looks like the line's effect), a later match proves
    nothing. Such a read-back reports `ok=false`, so the line stays `in_doubt` for a person and
    `verify` never marks it `verified`. A baseline that failed (`baseline_unknown`) is treated
    the same way.
  * A read-back that errors or cannot tell stops the line (`in_doubt`), never a blind retry.
  Crash recovery re-runs `apply`: settled lines are skipped and unsettled ones continue from their
  ledger entry. Lines that are not applied, and in-doubt lines, go to the guide checklist during
  reconcile, and the snapshot is marked stale (`per_op.applied`).
* Effective idempotency at dry run and apply: `nodes/per_op._mapping` reduces the mapping to the
  ops `per_op_capabilities` still allows and sets each op's idempotency to the **effective** value
  (claim capped by the tool's confirmed idempotency). The ledger retries by that value, so a
  mapping that claims `enforced` for a tool confirmed `none` is never retried automatically. It is
  part of the hashed preview calls, so a change after approval is refused.
* Row status: `applied` when every line settled, `partial` when some settled **or any line is in
  doubt** (the target system may hold it; `partial` is a committed state, so the option is never
  applied again on top of it), else `failed` (nothing reached the target system).
* A resumed apply (`applying`) that is refused (mapping changed or removed meanwhile) keeps the
  stored ledger (`prior_ledger_summary`) and the committed status. Reconcile reports the stored
  ledger as it is when the mapping can no longer read the lines back.
* Write calls use the apply principal and read-backs use the reader. No write tool is ever bound
  to an LLM agent.

## Worker: crawl-sync scheduling (`worker.py`)

`Worker.crawl_targets` holds the sync targets whose detected tiers have `A` but not `B`. They are
re-read every poll round, and the last known set is kept while the registry is unreachable. These
connectors are never polled for changes (they have no change feed). Every `scn_crawl_interval_s`
(default 3600; `0` = off) the worker calls `request_sync(reason="crawl_interval")` for them, and
the platform's due list runs `sync_connector`, which crawls them. The start-up, stale-mark (after
a per-op apply), nightly and failure-retry paths are the same as for snapshot connectors. A
connector with tier B is always snapshot-synced and never crawled.

## Tests (FakeLlm, no live model calls)

* `tests/runtime/test_m7_agents.py`: catalogue, custom specialist planned and run end to end,
  quarantine auto-wrap, display names, write tool refused before any model call, the specialist's
  own tool budget, a Pro alias refused at runtime, and the playground (draft, saved slug, turn
  cap, budget cap, write refused, scratch repo). No write tool ever reaches an LLM.
* `tests/runtime/test_m7_tier_a.py`: rendering, read-back matching, the ledger state machine
  (enforced, best_effort, none, in_doubt, stale or pre-existing read-backs, crash recovery),
  capabilities, atomic groups, and the committer against the FastMCP lite fake
  (`tests/fakes/lite_server.py`).
* `tests/runtime/test_m7_flows.py`: per-op apply with approval and read-back verification, lost
  response, version conflict leading to a guided step, mapping changed after approval, unmapped
  ops guided, atomic group guided.
* `tests/runtime/test_m7_advisory_crawl.py`: advisory flags and assumption for a crawled versus a
  consistent snapshot, label repair, and the worker crawl schedule.
