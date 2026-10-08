# M5 runtime: remediation depth (policy, Guide, bundles, rollback, re-plan)

Contract: `docs/contracts/95-m5-remediation.md` §1–§5. This file documents the runtime (E2) side:
nodes, routes, state keys, and the platform (E1) ports it uses. Every M5 node is a
deterministic function node: no LLM call is added by M5.

## Workflow additions

```
pre_route:  action:apply  → compile_changeset {mode: apply, origin: chat}
            action:guide  → compile_changeset {mode: guide, origin: chat}
            action:rollback {change_set_id}                  → prepare_rollback
            action:checklist_step {checklist_id, seq, status} → checklist_step
            signal {signal_id}                               → prepare_replan   (no LLM classification)
compile_changeset: COMPILED → dry_run | GUIDED → end_turn | DEFAULT (refused) → close_case
prepare_rollback:  ROLLBACK → approval_gate | DEFAULT → close_case
prepare_replan:    COMPILE → compile_changeset | DONE → end_turn | DEFAULT → close_case
```

`approval_gate`, `approval_check`, `execute` and `reconcile` serve both modes; `ctx.state["commit_mode"]`
is `apply` (default) or `rollback`. Stages (`contracts.agui.NODE_STAGES`): `prepare_replan` →
building_options, `prepare_rollback` → preparing_changes, `checklist_step` → applying. Every workflow
node has a stage or is listed in `agui.STAGELESS_NODES` (routing / turn control / the question path);
`tests/runtime/test_m5_polish.py` checks this against the built graph.

### ctx.state keys (commit path)

| key | value |
|---|---|
| `change_set_id` | first bundle member (M1 key, kept) |
| `bundle` | `{id, change_set_ids: [apply order], hash: bundle hash, systems: {connector: label}}` |
| `approval` | `{outcome, approved_by, policy_version, rule_id, reason, requirement, deciders, executor, co_approved_by, quorum, interrupt_id}` |
| `initiator` | the run's actor (the signal principal for signal runs) |
| `run_origin` | `chat` \| `webhook` (policy `case.origin`; `effective_origin`: a node input can only make it stricter, `chat` comes from the run envelope alone) |
| `commit_mode` | `apply` \| `rollback` |
| `rollback` | `{plan_version, attempt}` |
| `checklists` | checklist ids created by this invocation |

## §1 Policy (`runtime/policy.py`)

* CEL engine: **cel-expr-python 0.1.3** (`cel_expr_python.cel`, CEL C++ runtime, cp314 wheel; added
  with `uv add`). `cel-python` is not needed. Variables `cs`, `case`, `initiator` are
  `map(string, dyn)`; compiled expressions are cached.
* `evaluate_policy(rules, context, *, fallback=None, allow_auto=True, version=None) -> PolicyDecision`
  is the **policy port** (`rules` = `PolicySet`, its JSON or a rule list; `context` = `PolicyContext`
  or its JSON). It uses `PolicySet.decide`: priority order, first match wins, and the two floors
  (what-if/unconfirmed → refuse; `auto` only for an untainted chat run).
* Fail closed: a rule that does not compile, reads an unknown field, errors or yields a non-bool
  raises `PolicyEvaluationError(rule_id, code, detail)`. `approval_gate` then refuses (nothing applied).
* `check_rule(when) -> [problems]`: static check for the admin editor (compile, unknown fields, bool).
* `register_policy_evaluator()` is called at worker start-up. It calls
  `keelson_engine.core.policies.register_policy_evaluator(evaluate_policy, check=check_rule)` (also
  accepted: `core.policy`, `set_policy_evaluator`). engine-api installs it itself
  (`core.policies.install_runtime_evaluator()`, also lazily on first use) for
  `POST /api/admin/policies/test` and the rule compile check.
* Rules: `AgentSpec.policy.rules` (tuple of `PolicyRule`, unique ids and priorities) or, when unset,
  `default_rules(config)`: `refuse-unconfirmed`(10), `four-eyes`(20, approver + compliance, 2,
  exclude initiator), `human-untrusted`(30), `auto-small-transfer`(40, planner initiator, caps from
  `auto_max_lines` / `auto_max_units`), `default`(50). New config fields: `compliance_role`,
  `auto_initiator_role`, `four_eyes_cost_cents`, `four_eyes_ops`.
* Context: `cs` from every bundle member (+ the option's scorecard cost, `kpis_after.units_at_risk`,
  levers, dry-run `value_cents`); `case.unconfirmed` = scenario assumptions still `inference` /
  `assumption`; `case.tainted` = an assumption's `source_evidence_ids` point at untrusted rows
  (quarantine); `case.origin` = `chat` for people, `webhook` for signal runs.
* Quorum: `approval_check` re-counts `resume.decided_by` with
  `keelson_contracts.policy.quorum_assign` (roles = each decider's recorded role; the initiator is
  excluded for chat runs). The executor is the member counted as `approver` (whatever the approval
  order); the others go to the receipt's `co_approved_by`. The requirement is stored in the change
  set's `result.requirement` (used again by rollback).
* Binding: `approval_gate` records the interrupt it waits for in `approval.interrupt_id`;
  `approval_check` refuses a decision for any other request, and takes the `auto` route only when the
  gate's own decision in `approval` is `auto` (never for a rollback).
* Auto: the `autocommit` principal applies; receipt `approved_by = policy:<rule>@<version>`.

## §2 Guide mode (`runtime/guide.py`, `runtime/remediation.py`, `nodes/guide_nodes.py`)

* Capability table (`candidates.capability_table`): write connectors = `spec.connector` +
  `spec.remediation.connectors` (in order). A connector applies an op when its change-set tools are
  approved, `apply_change_set` has `idempotency = enforced` (`ToolInfo.idempotency_enforced`; unclassified
  rows fall back to the MCP `idempotentHint`), and the op is in `config.supported_ops`, else in the
  `add_change_set_items` schema, else in the M1 ops.
* `partition_actions`: apply lines go to the first connector that applies the op. Everything else,
  or every line in Guide mode, is a guide line. An atomic group is never split: a group with any guide
  line, or with lines on two connectors, is guided whole.
* Candidates: a lever applies only when every op it emits applies (atomic groups), and
  `guided_only` now means *no* lever of the option applies; `simulate` also marks an option
  guided-only when its actual lines partition to zero apply lines. Partly supported
  options stay applicable; `simulate` stores `option.remediation = {apply_lines, guide_lines}`, and
  `compose` sets `recommended_option.remediation_mix`, enables **Guide me** ("Apply 3 changes for me ·
  Guide me through 2") and disables Apply when no line applies.
* Checklist steps: from `get_operator_guide` when the connector has it approved as a `read` tool
  classified `trusted_data` (its text is shown verbatim; an `untrusted_text` guide falls back to
  generated steps). The guide lines go
  into a draft change set (preparer, never validated or applied), and the guide is read by the reader.
  Unsafe deep links (`is_safe_deep_link`) are dropped. Otherwise steps are generated from the payloads,
  optionally with connector `config.deep_links {op: template}` links, plus one screened SOP excerpt
  per op (`quarantine.sop_snippets`). The raw KB chunk is stored as an `untrusted_text` evidence row;
  the deterministic injection screen drops flagged chunks; links and markup are stripped; no model
  sees the text. These steps are labelled `inference`.
* `expected_for(action)`: what read-back must find per op. `verify_checklist` reads it back with the
  connector's approved read tools (reader principal). It never matches entities that existed when the
  checklist was created (`meta.baseline`), entities created by our own change sets, or one entity for
  two steps. A match is marked `verified` with an evidence row (`args.matched_ref`).
* Worker: every `WORKER_GUIDE_TICK_INTERVAL_S` (5 s) the guide loop calls the platform's
  `core.checklists.poll_checklists()` (via `ChecklistsPort.poll`), which verifies each (tenant,
  connector) with open checklists when a webhook event arrived or 60 s passed. Without that port
  method the runtime's `GuideVerifier.tick()` does the same (`signals.latest_event_id`,
  `WORKER_GUIDE_POLL_INTERVAL_S`) and publishes outside runs (`save_block` + bus `ACTIVITY_SNAPSHOT`,
  `run_id` null, "Guide completed: …" message).
* `checklist_step` (manual): only `done` / `skipped`. It never downgrades `verified`, is audited
  (`checklist.step.<status>` by `user:<username>`), and re-emits the block.

## §3 Rollback (`commit_nodes.prepare_rollback` + rollback branches)

* Refused unless the change set belongs to the case, is `applied`/`verified`, and its connector has
  `rollback_change_set` approved.
* Always human: the stored original requirement, else the rules evaluated with `allow_auto=False`,
  else one approver. Interrupt `approval-rb-<cs>-<attempt>` (kind approval, `payload.rollback = true`,
  `change_set_hash` = the original H, `plan_version` = the latest plan). Every request gets a new
  attempt (`result.rollback_requested`), so an Undo asked again after a rejected one gets its own
  approval card (the platform stores interrupts `ON CONFLICT (id) DO NOTHING`).
* `rollback_change_set {change_set_id, approval_ref: APR-RB-…, approval_receipt, idempotency_key}`.
  The receipt is fresh: a new `approval_id`, the original hash, the executor's delegated token.
  The idempotency key is `sha256(tenant|cs|rollback|attempt)`, never the apply key. While the call is
  in flight the row is `rolling_back` (`result.rollback_from` = the status before); a crash-retry reads
  it back first, and a refusal or conflict restores `rollback_from`, so the batch stays undoable.
* Conflicts: the change set stays applied, `result.rollback_conflicts` is stored, and the tracker
  shows them. On success: row `rolled_back` (ledger `rolled_back`), read-back must say `rolled_back`,
  plan version `rolled_back`, and a recovery plan v+1 (same options, recommends `do-nothing`,
  `kpis.recovery`; saved once per rolled-back change set). Tracker `rolled_back` with no Undo.
* `commit_tracker` carries `actions: [undo]`, enabled while applied/verified and the connector can roll back.

## §4 Re-plan (`nodes/replan.py`)

* Idempotent per signal: when the latest plan's `kpis.replan.signal_id` is this signal, nothing is
  re-planned again; its delta is compiled only if no change set exists yet for that plan version
  (a crash between saving the plan and compiling), else `DONE`.
* Signal: `SignalsPort.get_signal(signal_id)` returns `{type: "lane.capacity_changed", data: {lane_code,
  factor_bp, weeks? (int or ISO weeks), start_date?}}`. Free text in `data` is ignored. Other types end
  with `DONE`.
* The plan must depend on the lane (`plan.kpis.dependencies.lanes` or committed lines); otherwise a
  short note and `DONE`. The lane is tried under each of its names: the event's `lane_code`, the
  router's `plan_lane_code` (set whenever the router matched the lane's DC pair, from the snapshot's
  lane row when it has one) and the transfer pseudo-lane `ORIGIN>DEST` of typed `origin`/`dest`
  (the router drops an event pair that contradicts the snapshot's lane row)
  (plans know DC-to-DC transfers as `FROM>TO`, the connector's lane events carry its own code such
  as `DE1-NL1-RD`); the first one the plan depends on is re-planned (perturbation target,
  `kpis.replan.lane_code`; `kpis.replan.event_lane_code` keeps the event's code) and people see
  `DE1-NL1-RD (DE1 → NL1)`.
* Scenario v+1 gets `capacity_scale` on `lane` as `fact` with its window (an earlier fact on the same
  lane is replaced). Then a bounded resync (`remediation.replan_resync_timeout_s`, `scn.sync_connector(…,
  reason="signal")`), else the current snapshot. Problem data = current (post-apply) state; a fresh
  snapshot already holds the cut, so the fact is not applied twice in the optimizer request.
* Committed plan: only the committed option is re-simulated, and its delta change set
  (`remediation.delta_actions`) becomes plan v+1's option. The delta holds:
  * new lines not already applied (extra transfer lines are added by quantity);
  * changed set-ops;
  * compensations on the cut lane: per-week bookings above capacity get `capacity_booking.cancel`
    (target = the booking code from the apply read-back `created_refs`) and a smaller
    `capacity_booking.create`; sea-air splits that no longer fit get `po_line.reschedule` back to the
    line's ETA. Bookings an applied earlier delta cancelled (one cancel per booking, also for the
    `<LANE>@<week>` fallback ref) and splits an applied later version rescheduled are not counted again.
  It is compiled straight away with `origin: webhook`, so it is never auto-approved.
* Open plan: the options using the lane are re-simulated; plan v+1 with no compile.
* Output: `narrative@1` with `banner` ("New information: sea-air capacity on X cut to 30% — plan
  updated (v2)"), `plan_versions` and `what_changed`, plus the delta `plan_diff`. The previous version
  is `superseded` unless it is committed.

## §5 Bundles

`compile_changeset` emits one change set per connector, in capability-table order. Each member has its
own H, dry-run (preparer), receipt and idempotency key. One interrupt covers the bundle:
`change_set_hash` = `bundle_hash`, `payload.change_set_ids` + `bundle_id`, and no `change_set_id` when
there are several members. The approval card lists `bundle` members. If any member's dry-run fails,
every member is marked `invalid` and nothing is applied. `execute` applies members in order, each
exactly once. If a later member is rejected outright or answers `conflict`, the earlier ones stay
applied: `reconcile` still reads them back and reports both (the plan is not marked committed).

## Platform ports (E1, `keelson_engine.core.*`) and the runtime adapters (`runtime/deps.py`)

| Port | Platform functions | Runtime use / fallback |
|---|---|---|
| policy | `policies.register_policy_evaluator(fn, check=)`; engine-api also self-installs via `policies.install_runtime_evaluator()` | the worker calls `runtime.policy.register_policy_evaluator()` at start-up; `PolicySet` + `PolicyContext` in, `PolicyDecision` out, raises on CEL errors |
| checklists | `checklists.create_checklist(tenant, case_id, connector, steps, *, checklist_id, plan_version, option_id, block_id, meta)` (idempotent), `get_checklist`, `list_open_checklists(tenant=None)`, `set_checklist_step(...)` (row lock; completes the checklist), `set_checklist_status`, `poll_checklists()` | `deps.checklists` (`_ChecklistsAdapter`): `block_id` = `stable_block_id(case_id, "remediation_checklist", checklist_id)` (the id the runtime's blocks use, so the platform refreshes the block in place). The worker's guide loop calls `poll_checklists()` (`ChecklistsPort.poll`: webhook events + 60 s poll, read-back, evidence, block refresh); `guide.GuideVerifier` is the runtime fallback (tests / platforms without the poll). `None` (module missing): Guide refused, mixed plans apply only their apply lines |
| signals | `signals.get_signal(tenant, signal_id)` → `{id, case_id, connector, event_id, type, kind, data: {lane_code, factor_bp, weeks, plan_version, matched_by}, received_at, run_id}` (`case_signal`, created by `replan.route_event`) | `_SignalsAdapter`; without `core.signals`: SQL on `connector_event` |
| plans | `repo.list_plan_versions(case_id, *, tenant)`, `repo.save_plan_dependencies(case_id, plan_version, dependencies, *, tenant)`; `repo.save_plan` / `create_change_set` record dependencies themselves (`replan.record_dependencies`) | `_PlansAdapter` (fallbacks: SQL on `plan`; `plan.kpis.dependencies`) |
| signal runs | `replan.route_event`: plans whose latest version is open/committed and depends on the event's lane → `kind=signal`, payload `{signal_id}`, actor `replan.signal_actor(connector)` (no roles) | `pre_route` → `prepare_replan` |
| quorum | `interrupts.record_decision_ex` counts with `quorum_assign` over the roles each approver held and lists the executor first in `decided_by` | `approval_check` re-counts (defence in depth) with the same executor rule (`approver`, else the first counted approval) |

## Integration fixes (M5 integrator, 2026-10-07)

Found while running the whole Act 3 story in one test (`tests/runtime/test_m5_act3.py`, full scn
snapshot) and against the real stack; regressions in `tests/runtime/test_m5_integration_fixes.py`.

* **One guide checklist per guided line set.** An apply retried after a rejection (or a quorum that
  was not met) used to create a second checklist for the same plan version / option / connector, so
  one entry in Merxa verified two checklists and "Guide completed" was announced twice.
  `_create_guides` now reuses the case's *open* checklist whose `meta.lines_fingerprint` (op,
  target, payload, group of the guided lines, order-free) matches; a completed one is never reused,
  and a failing listing falls back to a new checklist.
* **Over-booked weeks.** Merxa's `lane.capacity_changed` carries `weeks_detail[].overbooked_units`
  over *all* bookings on the lane (the golden seed already books ~9.5k / ~16.8k units on
  CGP-UK1-SA in W42/W43, so a 30 % cut over-books those weeks by ~1.7k / ~9.0k units). Comparing
  only Keelson's own holds with the cut capacity found nothing to give back. `LaneSignal.overbooked`
  (typed ints only) now caps each of our weeks at `booked − overbooked` as well, and compensates
  even when the lane's capacity is unknown.
* **Merxa's window.** `from_week` / `to_week` (ISO weeks) now set the cut's window; before, an
  integer `weeks` was counted from the plan's as-of date.
* **Delta PO-line versions.** Re-plan `po_line.*` lines carried `expected_version: null`, which the
  typed change-set item rejects (the signal run failed) and Merxa would refuse anyway (the applied
  split moved the version). `_fill_versions` reads the current versions back with the reader
  principal (`get_purchase_order`, per PO; an unreadable PO never hides the others); without a
  read it counts the applied changes on top of the version they expected (Merxa's dry-run still
  rejects a stale one).
* Platform side (`core/replan.py`): the router stores `from_week`, `to_week` and a sanitised
  `weeks_detail` in `case_signal.payload` (they were dropped), so the runtime sees them.
