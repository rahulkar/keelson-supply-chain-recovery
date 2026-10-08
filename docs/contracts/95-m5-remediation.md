# 95 · M5 remediation depth — policy & four-eyes, auto-commit, Guide mode, rollback, second inject re-plan, multi-system change-sets, full demo reset (Act 3)

Builds on 10 (auth, receipts), 20/22 (Merxa), 30, 50, 70, 80, 90. Models: Flash/Flash-Lite only (no Pro — user preference).

## 1. Approval policy (CEL) + quorum / four-eyes + auto-commit
- `AgentSpec.policy` gains `rules: [{id, description, when: <CEL>, require: {roles:[str], count:int, exclude_initiator: bool} | "auto", priority:int}]` evaluated by `runtime/policy.py` with **cel-expr-python** (fallback `cel-python`; document which is installed) over a typed context:
  `cs {lines, units, value_cents, ops:[str], connectors:[str], incremental_cost_cents, lost_units_after, levers:[str]}`, `case {mode, origin: chat|signal|webhook, tainted: bool, unconfirmed: int}`, `initiator {roles:[str]}`.
- Default rules (priority order; first match wins; data in `data/packs/value-fashion-eu` + default spec):
  1. `case.mode == "what_if" || case.unconfirmed > 0` → **refuse** (no change set compiled).
  2. `case.origin != "chat" || case.tainted` → human, never auto.
  3. `cs.incremental_cost_cents > 1000000 || cs.ops.exists(o, o in ["po.create","po_line.cancel","production_order.resequence"])` → `{roles:["approver","compliance"], count:2, exclude_initiator:true}` (**four-eyes**, two different people, one of each role).
  4. `cs.ops.all(o, o in ["transfer_order.create","allocation.set_qty"]) && cs.lines <= 10 && cs.units <= 500 && initiator.roles.exists(r, r == "planner")` → `"auto"` (rule id `auto-small-transfer`).
  5. otherwise → `{roles:["approver"], count:1, exclude_initiator:false}`.
- Interrupt quorum: `interrupt.quorum_spec` = the matched requirement; `POST /api/interrupts/{id}/decision` records `approval_decision` rows; the interrupt resolves only when `count` distinct approvers satisfying the roles have approved (a reject by any required approver rejects); `exclude_initiator` enforced server-side; the approval card shows progress ("1 of 2 approvals: Marco ✓ · waiting for Compliance").
- Executor: delegated token of the quorum member holding the `approver` role (e.g. Marco), regardless of approval order — that person must hold an active Merxa link with `changes:apply` (the approve pre-flight checks it for them only); other quorum members (e.g. Lena, compliance) are recorded in the receipt as additive claim `co_approved_by: ["user:lena"]` and Merxa writes them into the audit/attribution ("Changed by Marco Rossi via Keelson (APR-…), co-approved by Lena Vogel"); auto → `keelson-autocommit` principal with receipt `approved_by: policy:<rule_id>@<policy.version>`; Merxa attribution "Applied via Keelson auto-policy auto-small-transfer@p-2, initiated by Priya Shah".
- Admin: `GET/PUT /api/admin/policies` (tenant-admin; part of the versioned spec; raising auto caps or deleting rule 1/2 requires platform-admin) + `POST /api/admin/policies/test` (evaluate rules against a sample context, returns matched rule + requirement). UI: **Policies** page (rules list, CEL editor with test bench).

## 2. Guide mode (manual remediation with verified progress)
- Case action `guide {option_id, plan_version}` (and `apply` for mixed plans): `compile_changeset` partitions the option's actions per target connector into **apply lines** (connector supports the op, tool approved, idempotency enforced) and **guide lines** (everything else, or all lines when the user picks Guide).
- Guide lines → `remediation_checklist@1` block: `{checklist_id, connector, steps:[{seq, title, instructions_md, deep_link|null, expected:{entity_type, entity_ref|null, match:{…}}, status: pending|done|verified|skipped, verified_at|null, evidence_id|null}], progress:{done, total}}`. Steps come from the connector's `get_operator_guide` when available (Merxa), else are generated deterministically from the action payloads + the tenant's SOP snippets (KB via quarantine) — labelled `inference`.
- **Auto-tick**: when the engine receives a webhook (or on a 60 s read-back poll while a checklist is open) it reads back the expected entity via MCP read tools and marks matching steps `verified` (e.g. a transfer order DE1→NL1 with the expected SKUs/qty created in Merxa's UI). Users may also tick `done`/`skip` manually (audited). When all steps are verified/skipped the case shows "Guide completed".
- Merxa UI must support the manual steps via deep links with pre-filled forms: create transfer order (`/transfer-orders/new?from=&to=&ship_date=&lines=`), edit allocation qty, reschedule / change qty of a PO line, create capacity booking — each saved through REST with `Idempotency-Key`, producing outbox webhook events.

## 3. Rollback
- `commit_tracker` gains **Undo changes** (planner/approver). Case action `rollback {change_set_id}` → policy (always human; four-eyes if the original needed it) → Merxa `rollback_change_set` with a fresh receipt → reconcile (read-back) → plan status `rolled_back`, recovery plan v+1 = previous baseline; Merxa attribution "Rolled back by … via Keelson (APR-…)". Refuse when Merxa reports conflicts (later changes on the same entities) and show them.

## 4. Second inject → targeted re-plan (Act 3 finale)
- Merxa demo inject `lane_capacity {lane_code, factor_bp, weeks}` (e.g. sea-air `CGP-UK1-SA`/`CGP-NL1-SA` to 30%) updates lane capacity / rate-card capacity and emits webhook `lane.capacity_changed` (structured fields = fact; any carrier note text = untrusted).
- Engine: the webhook receiver resolves which **committed or open plans** depend on that lane (plan fingerprint: levers/actions using the lane, receipts on it); for each, enqueue a `signal` run on that case (origin webhook, never auto-approved) → `followup` path `REPLAN` (deterministic, no LLM classification): perturbation `capacity_scale` on the lane added to the scenario as `fact`; re-simulate only the affected options with already-applied decisions fixed; produce plan v+1, a **delta change set** containing only the changed lines (e.g. cancel/reduce some capacity bookings + add DC transfers) and a "What changed" narrative; the case shows a banner "New information: sea-air capacity on CGP-UK1-SA cut to 30% — plan updated (v2)". Approval per policy (human).
- Cases list shows an "Updated" badge; a notification toast appears for users viewing the case.

## 5. Multi-system change-sets (generic)
`compile_changeset` routes each action to its target connector by op ownership (connector capability table: which ops each connector applies — from connector_tool approvals + Merxa's declared op list) and emits one ChangeSet per connector (ordered; each with its own H, dry-run, receipt and idempotency key; atomic groups never split). Approval covers the whole bundle (one interrupt, one decision). If any connector's dry-run fails, nothing is applied (the bundle is refused with reasons). Tested with a fake second W-tier connector; demo uses Merxa only.

## 6. Full demo reset + rehearsal
- `POST /api/demo/reset` (platform-admin or demo-presenter; DEMO_MODE only), target < 30 s: Merxa `scenario:reset` (golden), truncate demo-tenant rows in keelson (cases, runs, events, interrupts, plans, change sets, checklists, evidence, blocks, scn snapshots except current → resync) and ADK sessions, Valkey demo keys, revoke the demo users' Merxa offline sessions/consents via the Keycloak admin API (merxa realm) and delete their Token Broker rows + identity links, reset agent spec to the pack default, reset SimClock. Returns a step log. Optional `keep_consents: true` (rehearsal mode) skips the Keycloak/Token-Broker revocation.
- `scripts/demo-reset.ps1` / `.sh` call it; Playwright and evals use it as a fixture.

## 7. Web
Approval card quorum progress + reject reason; Guide checklist block (steps with deep links opening Merxa in a new tab, live ticks, manual tick/skip, progress); mixed apply+guide on the option card ("Apply 3 changes for me · Guide me through 2"); commit tracker Undo; re-plan banner + plan version switcher (v1 ↔ v2 diff); admin Policies page; demo reset button in the persona menu (DEMO only, presenters).

## 8. e2e / evals
- API: `e2e/api_m5_act3.py` — reset → chat trim delay → confirm → option with mixed apply+guide → four-eyes approval (Marco + Lena) → bulk apply → perform one guide step via Merxa REST (as merxa user) → auto-tick verified → inject sea-air 30% → re-plan v2 + delta change set → approve → apply → rollback the delta → reset. `e2e/api_m5_autocommit.py` — small transfer-only change set auto-approved and applied by the autocommit principal (attribution policy), plus refusal cases (what-if, unconfirmed, signal origin never auto).
- Playwright `act3-full.spec.ts` with screenshots 22-guide-checklist, 23-four-eyes, 24-replan-banner, 25-rollback, 26-policies-admin. Evals: add a case asserting signal-origin runs are never auto-approved.

## 9. As built (updated M5, 2026-10-07) — authoritative where it differs from §1–§8
The detailed contracts are in `30` "M5 …" (REST, SSE, blocks, webhooks, receipts, DB), `50` "M5 …" (graph, envelope, policy, Guide, rollback, re-plan, bundles, tests), `55` "Changes through M5" (modules), `20` "M5" and `22` §8 (Merxa). Node detail: `services/engine/src/keelson_engine/runtime/nodes/M5_NODES.md`.

### §1 Policy, quorum, auto-commit (updated M5, 2026-10-07)
- **CEL engine.** **cel-expr-python 0.1.3** is installed (the CEL C++ runtime); `cel-python` is not.
- **Rule shape.** `require` may also be `"refuse"` (rule 1 is a refuse rule). Evaluation fails closed: a CEL error refuses the change set.
- **Default rule order.** The rules are `refuse-unconfirmed` (10), `four-eyes` (20), `human-untrusted` (30), `auto-small-transfer` (40), `default` (50). So rule 3 (four-eyes) is checked **before** rule 2 (non-chat or tainted → human). A webhook re-plan that creates POs therefore still needs both people.
- **Hard floors.** The engine applies two floors whatever the rules say (`PolicySet.decide`): what-if or unconfirmed values are refused, and auto applies only to a person's chat run on untainted input.
- **Versions.** The built-in default is version `p-1`; the `value-fashion-eu` pack stores the same rules as `p-2`. No rule matched → one `approver`.
- **Admin guards.** Broader than "raising auto caps or deleting rule 1/2": any change that could auto-approve more, refuse less, or need fewer or different approvers is `403 guarded_change` for a tenant admin (`core.policies.guarded_changes`).
- **Test bench.** It answers `{decision, matched_rule, approved_by, floors: {must_refuse, auto_allowed}}`.
- **Quorum counting.** Each approval is counted under one required role by maximum matching (`quorum_assign`). "One of each role" applies when `count ≥ len(roles)`. An approval that cannot advance the quorum is refused (403) rather than recorded.
- **Executor.** The approval counted under `approver` executes, whatever the order. Only that person needs an active Merxa link with `changes:apply`: `412 connect_required` when the decider is the executor, `412 executor_connect_required` when the approval that completes the quorum belongs to someone else. Co-approvers never need a link.
- **Co-approver names.** The receipt carries `co_approved_by: ["user:lena"]`. Keelson does not send the optional `co_approver_names` claim, so Merxa's attribution reads "Changed by Marco Rossi via Keelson (APR-…), co-approved by lena" (the username), not "Lena Vogel".
- **Reject reason.** A reject or revise may carry a `reason` (≤ 500 chars). It resolves the interrupt at once and shows on the card's `rejection`.
### §2 Guide mode (updated M5, 2026-10-07)
- **Checklists.** There is one checklist per guided connector. Steps come from Merxa's `get_operator_guide` when it is approved and classified `trusted_data`, else they are generated from the action payloads plus screened SOP excerpts (labelled `inference`).
- **Auto-tick.** The worker's guide task runs every 5 s and reads back on any new webhook event of the connector, or every 60 s. Events only trigger the read-back; their payloads are never trusted.
- **Manual ticks.** These use `POST /api/cases/{id}/checklists/{checklist_id}/steps/{seq}` (`done`/`skipped`, audited). They are not a case action: the run-contract action `checklist_step` exists, but engine-api does not accept it.
- **Merxa deep links** (all saves use `Idempotency-Key`, record edits `If-Match`, and every save emits a webhook event; see `22` §8):
  - `/transfer-orders/new?from=&to=&ship_date=&lines=SKU:qty,…`
  - `/allocations/{code}?action=set-qty&qty=&version=`
  - `/purchase-orders/{po}?line=&action=reschedule&eta=&version=` and `…&action=change-qty&qty=&version=`
  - `/capacity-bookings/new?lane=&week=&qty=`
  - `/capacity-bookings/{code}?action=cancel&version=`
- **Partial deep links.**
  - `po_line.cancel` / `po_line.split` links open the PO page with a "Review line N" note but no pre-filled form, and Merxa has no manual REST for them.
  - The `po.create` link points to `/purchase-orders/new`, which merxa-web does not have.
  - Resequence, promotion and re-promise links open list pages.

### §3 Rollback (updated M5, 2026-10-07)
- **Approval.** Undo (`rollback {change_set_id}`, `WRITE_ROLES`) is always human. It reuses the original requirement (four-eyes stays four-eyes) with interrupt `approval-rb-<cs>-<attempt>`. A second Undo while one is pending gets `409 rollback_pending`.
- **Merxa refusals.** Merxa refuses:
  - a policy receipt (`403 rollback_requires_user_approval`);
  - a co-approved change set's rollback without `co_approved_by` (`403 invalid_receipt`, `reason: co_approval_required`);
  - a caller whose token lacks the role `ops-director`.
- **Conflicts.** They leave the change set applied and are shown on the tracker (`commit_tracker.conflicts`).
- **Success.** The plan version becomes `rolled_back`, and a recovery plan v+1 recommends `do-nothing`.

### §4 Second inject → re-plan (updated M5, 2026-10-07)
- **Inject payload.** Merxa's inject is `lane_capacity {lane_code, factor_bp (0–100,000), weeks (1–52), from_week?, carrier_note?}`. It splits the overlapping rate cards at the window edges and keeps confirmed bookings, so a week can be over-booked.
- **Event.** `lane.capacity_changed` carries the window (`from_week`/`to_week`) and per-week `weeks_detail` with capacity before/after and booked / over-booked units.
- **Routing.** Keelson routes it to every case whose latest open or committed plan depends on the lane (`plan_dependency`, directly or through a receipt on a sailing of that lane). Each such case gets a `signal` run with no roles. Free text (`carrier_note`) is never copied.
- **Committed plan.** The delta can cancel over-booked capacity bookings (new Merxa op `capacity_booking.cancel`) and book less, and it moves sea-air splits that no longer fit back to their ETA.
- **Visible result.** The cases list shows the "Updated" badge from `last_signal_at`. The banner and version switcher come from `narrative@1.banner` / `plan_versions`.

### §5 Multi-system bundles (updated M5, 2026-10-07)
- **Bundle hash.** One member keeps its H. Several members use `sha256_hex({"v": 1, "change_sets": [{connector, hash}, …]})` in apply order.
- **Interrupt and card.** The interrupt has `change_set_ids` + `bundle_id`, and the card lists `bundle` members.
- **Failures.** A failed dry-run marks every member `invalid`. A later member's conflict leaves the earlier ones applied and reported (the plan is not marked committed).

### §6 Demo reset (updated M5, 2026-10-07)
- **Who.** `platform-admin`, or `demo-presenter` when a member of the demo tenant.
- **Steps.** `merxa_scenario_reset`, `keelson_rows`, `adk_sessions`, `valkey_keys`, `merxa_consents`, `token_broker`, `agent_spec`, `sim_clock`, `scn_resync`.
  - Each step logs `{step, status, duration_ms, detail, counts}`.
  - `ok` is false when any step failed, but every step still runs.
  - `sim_clock` is restored by Merxa's golden reset, so it fails when step 1 fails.
- **Kept.** Connectors, tools, knowledge bases, LLM recordings and the audit log.
- **Rehearsal.** `keep_consents: true` skips `merxa_consents` and `token_broker`. `merxa_consents` revokes the Keycloak offline sessions and consents of the linked users plus `DEMO_MERXA_USERS`.
- **Concurrency and audit.** One reset per tenant runs at a time (`409 reset_in_progress`). The reset is audited as `demo.reset`.

### §7–§8 Web and tests (updated M5, 2026-10-07)
- The web ticks guide steps through the checklist route above.
- Real-stack checks that need no model credits: `e2e/api_m5_offline.py`, `e2e/api_m5_autocommit.py` and Playwright `m5-offline.spec.ts`. The Playwright spec's screenshots 22–26 come from the `/dev/blocks` fixtures and admin pages.
- The live Act 3 story is `e2e/api_m5_act3.py`. `act3-full.spec.ts` was not built.
- The eval case is `evals/cases/signal_origin_never_auto.yaml`.
- Offline unit and integration tests: `services/engine/tests/{runtime,platform}/test_m5_*.py` and `services/merxa-api/tests/test_m5.py`.
