# 30 · Keelson engine API, SSE events & render blocks (M1)

engine-api = FastAPI on 8000, all routes under `/api`. Browser reaches it same-origin at `http://keelson.localhost/api/*` (Caddy bypasses Next.js). Auth/CSRF per `10-auth.md`. All mutating routes accept/require `Idempotency-Key` header where noted.

## REST
| method & path | body / query | response |
|---|---|---|
| `GET /api/healthz` / `/api/readyz` | — | `{ok:true}` / `{ok, checks:{db,valkey,litellm_alias,keycloak}}` |
| `GET /api/.well-known/jwks.json` | — | public JWKS with kids `ca-1`, `ar-1` |
| `GET /api/me` | — | see 10-auth |
| `GET /api/cases` | `?limit` | `{items:[{id,title,mode,status,updated_at,last_message_preview}]}` |
| `POST /api/cases` | `{title?}` | `{id}` |
| `GET /api/cases/{id}` | — | `{case:{id,title,mode,status,created_by,created_at}, messages:[{id,role,text,run_id,created_at}], blocks:[envelope], open_interrupts:[{id,kind,message,payload,response_schema}], last_event_seq}` |
| `POST /api/cases/{id}/messages` | `{text}` + `Idempotency-Key` | `202 {run_id}` |
| `POST /api/cases/{id}/actions` | `{type: "apply"|"guide"|"promote", option_id, plan_version}` + `Idempotency-Key` | `202 {run_id}` |
| `GET /api/cases/{id}/events` | header `Last-Event-ID` / `?after=` | `text/event-stream` (below) |
| `POST /api/interrupts/{id}/decision` | `{decision, plan_version?, change_set_hash?, edits?}` | `202 {status:"recorded"|"resumed", run_id?}`; 409 if superseded/stale |
| `POST /api/runs/{id}/cancel` | — | `202` |
| `GET /api/blocks/{block_id}/data` | — | large block data referenced by `data_ref` |
| `GET /api/connectors` | — | `{items:[{slug,name,url,tier,status,link:{status,ext_username}}]}` |
| `GET /api/connectors/merxa/connect` | `?return_to=` | 302 to Keycloak (merxa realm) |
| `GET /api/connectors/merxa/callback` | OAuth | 302 to `return_to` |
| `GET /api/connectors/merxa/link` | — | `{status:"active"|"missing"|"expired", ext_username, roles, scopes}` |
| `GET /api/internal/forward-auth` | `?role=` | 200/401/403 (used by Caddy) |
| demo: `GET /api/demo/personas`, `POST /api/demo/login-as` | see 10-auth | |

Decisions: approach card → `confirm|edit|what_if|cancel`; approval card → `approve|reject|revise` (approve requires `change_set_hash` + `plan_version` equal to current, else 409). Approval pre-flight: approve returns `412 {code:"connect_required", connect_url}` if the approver has no active Merxa link with `changes:apply`.
(updated M4, 2026-10-07: the actions route also takes `confirm_assumptions` + `assumption_keys`, and the KB admin routes were added. The decision route still takes only text `edits`. See "M4 REST" below.)
(updated M5, 2026-10-07: the decision route now takes typed `edits`, `removed_checks` and a reject/revise `reason`, and answers with quorum progress. The approve pre-flight checks only the quorum member who will execute, and a missing link of another executor answers `412 executor_connect_required`. The actions route also takes `rollback`. Routes were added for checklists, policies and the demo reset. See "M5 REST" below.)

## Runs (internal)
`POST messages/actions` inserts a `run` row (`status=queued`, `kind=chat|action|signal|resume`, typed `payload`) and `XADD keelson:runs:wake *`. engine-worker claims with `UPDATE run SET status='running', lease_owner=…, lease_expires_at=now()+60s … WHERE id = (SELECT id FROM run WHERE status='queued' AND case lease free ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)`; heartbeats every 15 s; reaper re-queues expired leases (crash-retry → `run_async(invocation_id=…)` with no new message). One active run per case (per-case lease). ADK session id = run id; `App.name = "t_<tenant>"` (tenant slug with `-` → `_`).

## SSE stream (`GET /api/cases/{id}/events`)
Each message: `id: <seq>\nevent: message\ndata: <AG-UI 1.0 event JSON, camelCase>\n\n`. `seq` = monotonic `run_event.id` (bigint). Replay all events with `seq > Last-Event-ID` for the case from Postgres, then tail Valkey stream `keelson:case:<case_id>`. Keep-alive comment every 15 s. Run continues if the client disconnects.

Every event carries `metadata.sc`:
```json
{"metadata":{"sc":{"tenantId":"demo-retail","caseId":"…","seq":123,"stage":"checking_systems","prov":"fact|inference|assumption|simulated|decision|committed","label":"Read 12 open purchase orders from Merxa","agent":"inventory_demand","evidenceRefs":["ev_…"]}}}
```
Stages (fixed vocabulary, user-facing labels in UI): `understanding` (Understanding), `checking_systems` (Checking your systems), `measuring_impact` (Measuring impact), `building_options` (Building options), `ready_for_review` (Ready for review), `preparing_changes` (Preparing changes), `awaiting_approval` (Awaiting approval), `applying` (Applying), `verified` (Verified).
(updated M4, 2026-10-07: same vocabulary. For the M4 node → stage map and the `planner`/`playbook` agent rows, see "M4 SSE" below and `50` "M4 stages and agents".)

Event mapping (only these are emitted in M1):
| AG-UI type | when | notes |
|---|---|---|
| `RUN_STARTED {threadId: caseId, runId}` | run begins | |
| `STEP_STARTED {stepName: <stage>}` / `STEP_FINISHED` | stage transitions | UI shows stage chips |
| `SUBAGENT_STARTED {subagentRunId, name: <agent slug>, description: <display name>}` | an **LLM role** starts (intake, specialists, recommender…); never for plumbing nodes | `subagentRunId = <invocation_id>:<node path>` |
| `SUBAGENT_FINISHED {subagentRunId}` / `SUBAGENT_ERROR {subagentRunId, message}` | | |
| `TOOL_CALL_START {toolCallId, toolCallName, parentMessageId?}` + `TOOL_CALL_END` + `TOOL_CALL_RESULT {toolCallId, content}` | specialist tool calls | `toolCallName` = real tool name (Trace tab only); UI shows `metadata.sc.label`; `content` = short humanised summary, never raw payload |
| `REASONING_START/REASONING_MESSAGE_START/CONTENT/END/REASONING_END` | optional thought summaries | live only via Valkey; Postgres keeps aggregated `REASONING_MESSAGE_CONTENT` once |
| `ACTIVITY_SNAPSHOT {messageId, activityType: "sc.render", content: <block envelope>, replace: true}` | a render block is created/updated | |
| `TEXT_MESSAGE_START/CONTENT/END {messageId, role:"assistant"}` | **only from `compose`** (placeholders already filled) | |
| `RUN_FINISHED {threadId, runId, outcome}` | `outcome: {type:"success"}` or `{type:"interrupt", interrupts:[{id, reason:"approach"|"approval", message, responseSchema}]}` | |
| `RUN_ERROR {message, code}` | failure / `CANCELLED` | |
| `CUSTOM {name: "sc.case.updated", value:{mode,status,title}}` | case metadata changes | |

Events are validated against AG-UI 1.0 schemas (`ag-ui-protocol` Python models / `@ag-ui/core` Zod) before append; unknown fields go only under `metadata`.

## Render block envelope (`packages/contracts-py` `keelson_contracts.blocks`)
```json
{"id":"blk_…","kind":"kpi_strip","v":1,"title":"Exposure","placement":"inline|canvas",
 "provenance":{"kind":"simulated","engine":"keelson-opt@0.1","inputs_hash":"…","as_of":"2026-10-05"},
 "data_ref":null,"props":{…}}
```
Only deterministic nodes create blocks. `placement` decided by `compose`: inline ≤ 2 per turn (narrative excluded).

M1 block kinds & props (all numbers already resolved; units explicit):
- `kpi_strip@1` — `{items:[{key, label, value: int, unit: "units"|"eur_cents"|"days"|"pct_bp"|"count", delta?: int, prov, preliminary: bool}]}`
- `approach_card@1` — `{interrupt_id, event:{family, type, description, source}, mode:"actual"|"what_if", matched:{count:int, sample:[{type,code,label}]}, assumptions:[{key,label,value_text,prov}], checks:[str], buttons:["confirm","edit","what_if","cancel"]}`
- `recommended_option@1` — `{plan_version, option:{id, title, summary_bullets:[str], kpis:[kpi item]}, alternatives:[{id,title,one_liner}], actions:[{type:"apply"|"guide", label, enabled, disabled_reason?}]}`
- `plan_diff@1` — `{plan_version, rows:[{seq, op, target_label, before_text, after_text, qty?: int, system:"Merxa"}], totals:{lines:int, units:int, value_cents:int}}`
- `approval_card@1` — `{interrupt_id, change_set_id, change_set_hash, plan_version, summary:{lines,units,value_cents}, reason, required_roles:[str], connect:{status, connect_url}, buttons:["approve","reject","revise"]}`
- `commit_tracker@1` — `{change_set_id, system:"Merxa", status:"applying"|"applied"|"conflict"|"verified", ext_code, ext_url:"http://merxa.localhost/change-batches/<code>", applied:int, verified:int}`
- `narrative@1` — `{markdown}` (already composed)
- `evidence_pack@1` (canvas) — `{items:[{id, source, label, prov, retrieved_at}]}`
- Later: `network_map`, `impact_graph`, `stockout_timeline`, `scenario_compare`, `remediation_checklist`, `agent_trace_graph`.
- (updated M4, 2026-10-07) `network_map`, `impact_graph`, `stockout_timeline` and `scenario_compare` shipped in M3 (`80` §5). The M4 additions to `approach_card`, `recommended_option` and `narrative` are in "M4 render blocks" below. `remediation_checklist` and `agent_trace_graph` are not built.
- (updated M5, 2026-10-07) `remediation_checklist@1` shipped in M5, and `approval_card`, `commit_tracker`, `recommended_option` and `narrative` gained fields (see "M5 render blocks" below). `agent_trace_graph` is still not built.

The generated JSON Schemas live in `packages/contracts-ts/schemas/*.json` (from Pydantic) and are converted to Zod for the web app.

## Engine DB (`keelson`, Postgres 18, **RLS FORCE** on tenant tables via `SET LOCAL app.tenant_id`)
`tenant(slug PK, name)`, `tenant_llm_key(tenant, alias_prefix, key_enc, key_id)`, `bff_session`, `admin_audit`, `case`, `case_message`, `run`, `run_event(id bigserial PK, tenant, case_id, run_id, type, payload jsonb, created_at)`, `interrupt(id, tenant, case_id, run_id, kind, status {PENDING,RESOLVED,SUPERSEDED,EXPIRED}, quorum_spec jsonb, plan_version, change_set_hash, payload jsonb, invocation_id, created_at, resolved_at)`, `approval_decision(interrupt_id, approver_sub, role, decision, change_set_hash, auth_time, created_at, UNIQUE(interrupt_id, approver_sub))`, `resume_request`, `scenario`, `plan`, `investigation_task`, `finding`, `evidence(id 'ev_…', tenant, case_id, run_id, connector, tool, args jsonb, result jsonb, prov, created_at)`, `calc`, `block`, `change_set(id, tenant, case_id, plan_id, connector, revision, items jsonb, hash, status, ext_change_set_id, ext_code, preview jsonb, approval_id, receipt_jti, result jsonb)`, `commit_ledger`, `connector`, `connector_tool`, `connector_credential`, `connector_identity_link`, `agent_spec(tenant, version, spec jsonb, created_by, created_at)`.

## Changes from M1 hardening (2026-10-06) — authoritative
- `metadata.sc.runId` (optional, omitted when unset) is set on **every** event, including API-emitted `RUN_ERROR CANCELLED` and reaper errors. Clients attribute events by it (RUN_STARTED/RUN_FINISHED also carry their own `runId`).
- User-visible errors never contain exception text. `RUN_ERROR` / `SUBAGENT_ERROR` carry a generic message and `metadata.errorId` (UUID); the full exception is logged server-side under that id. Codes: `CANCELLED`, `INTERNAL_ERROR`, `CONNECT_REQUIRED`, `SUBAGENT_FAILED`, `RUNTIME_UNAVAILABLE`, `INVALID_RUN`, `SESSION_NOT_FOUND`, `BUDGET_EXCEEDED`, `CRASH_LIMIT`. API 500s return `{code:"internal_error", message, error_id}`.
- `GET /api/cases/{id}` is a single REPEATABLE READ snapshot: `last_event_seq` is read first, then interrupts/messages/blocks.
- `GET /api/connectors/merxa/link` adds `apply_ready: bool` (active link holding `changes:apply`). The web evaluates the approval card for the **viewer** with this, not the block's initiator-computed `connect`.
- `DELETE /api/connectors/merxa/link` revokes the current user's Merxa refresh token (RFC 7009, private_key_jwt) and marks the link `revoked` (demo resets).
- Case titles are derived from the first chat line, cut at a word boundary (≤72 chars + "…").
- Shutdown: SSE streams end on SIGTERM; engine-api runs with `--timeout-graceful-shutdown 5`.
- Caddy access logs redact `ticket`, `code`, `session_state` in query strings and logged `Location` headers.

## Changes through M4 (updated M4, 2026-10-07) — authoritative
Contract: `90-m4-investigation.md`. M2 back-office routes and webhooks are in `70-backoffice-m2.md`; M3 canvas blocks, `data_ref` and scn tables are in `80-m3-deterministic-core.md`. The M4 graph, stage mapping and agent roles are in `50-engine-workflow.md` ("M4 …" sections).

### M4 REST (updated M4, 2026-10-07)
| method & path | body / query | response |
|---|---|---|
| `POST /api/cases/{id}/actions` | `{type: "apply"\|"guide"\|"promote"\|"confirm_assumptions", option_id, plan_version, assumption_keys?}` + `Idempotency-Key` | `202 {run_id}`. 409 `stale_plan` `{current_plan_version}` when `plan_version` is not the latest plan. 422 `unknown_option`. 422 `invalid_action` when `assumption_keys` is missing or empty for `confirm_assumptions`, or present for another type. |
| `POST /api/interrupts/{id}/decision` | `{decision, plan_version?, change_set_hash?, edits?: string (≤ 8000)}` | unchanged from M1 (see the known gap below) |
| `GET /api/admin/kb` | — | `{items:[{id, name, description, created_by, created_at, documents, ready_documents, chunks}], profile}` |
| `POST /api/admin/kb` | `{name, description?}` | 201 kb; 409 `name_taken` |
| `GET /api/admin/kb/{id}/documents` | — | `{items:[{id, kb_id, title, filename, mime, bytes, sha256, status: processing\|ready\|failed, error, chunks, tokens, created_by, created_at, updated_at}]}` |
| `POST /api/admin/kb/{id}/documents` | multipart `file` (`.md`/`.txt`/`.pdf`, ≤ 5 MB) + optional `title` | 201 document; 200 `duplicate: true` (same sha256 in that KB); 413 `file_too_large`; 415 `unsupported_media_type`; 422 `invalid_pdf`/`encrypted_pdf`/`pdf_too_complex`/`invalid_text`/`no_text`/`too_many_*`/`file_required`; 502/503 embedding failure |
| `DELETE /api/admin/kb/{id}/documents/{doc}` | — | 204 (chunks and vectors cascade) |
| `POST /api/admin/kb/reembed` | `{scope?: "tenant"\|"platform", alias?, dims? (≤ 4000)}` | 202 job; 403 `platform_admin_required` (platform scope needs platform-admin); 409 `reembed_running` |
| `GET /api/admin/kb/reembed` | — | `{items:[job], profiles:[profile]}` |

- **`confirm_assumptions` keys.** The API accepts at most 32 keys of 1–64 chars each (`routes_cases.ActionBody`). The run contract (`ActionPayload`) and the web allow up to 50. A list of 33–50 keys therefore gets FastAPI's 422 `invalid_request`. The keys are the `unconfirmed[].key` values of the `recommended_option@1` block.
  (updated M5, 2026-10-07: closed. `ActionBody.assumption_keys` now allows `MAX_PAYLOAD_KEYS` (50) keys of 1–64 chars, the same as the run contract and the web.)
- **Typed approach edits: known gap.** `DecisionBody.edits` is `str | None` and the body has no `removed_checks` field (unknown fields are ignored). `core.interrupts.record_decision_ex` stores `edits` as text. The M4 typed **Edit** that the web sends (`edits: [{key, value_int} | {key, value_ids}]`, `removed_checks: [task_id]`, contract `ResumePayload`, see `50` "M4 run envelope") is therefore refused with 422 `invalid_request`. M1 free-text edits still work. Until the API and `core.interrupts` accept the typed list, the typed Edit form cannot resume a run.
  (updated M5, 2026-10-07: closed. `DecisionBody.edits` is `str (≤ 8000) | [ApproachEdit] (≤ 50)` and the body has `removed_checks` (≤ 50 ids of 1–200 chars). The typed fields are checked against `ResumePayload`: only with `decision: "edit"`, unique keys and ids, else 422 `invalid_decision`. `record_decision_ex` stores them in `approval_decision.edits` as JSON `{edits, removed_checks}` and passes them to the resume run.)
- **KB routes.** They follow the `/api/admin` rules (role, tenant, CSRF, audit `kb.create`, `kb.document.upload`, `kb.document.delete`, `kb.reembed`; `api/routes_admin_kb.py`). KB texts are `untrusted_text`: they reach a model only through the quarantine wrapper. See `services/engine/README.md` "Knowledge base".
- **Webhooks (§10 carry-over).** `scenario.reset`, and any `merxa.*` event ending in `.reset` or `_reset`, marks the connector's canonical snapshot stale, exactly like the `change_set.`/`po_line.`/`inventory.`/`sailing.` events (`core.scn_sync.is_stale_event` → `mark_stale`, debounced).

### M4 SSE (updated M4, 2026-10-07)
- **Stages.** The vocabulary is unchanged; the M4 nodes map onto it per `50` "M4 stages and agents". `understanding` can be entered twice in one run (frame → quick exposure → planner).
- **Agents.** `SUBAGENT_STARTED.name` can now also be `planner` ("Planner", only when the planner's LLM runs) or `playbook` ("Playbook analyst"). Up to `max_parallel` (4) specialist rows run at once. Each task's wrapper (dynamic child `run_id` `task-<id>-r<rev>`) opens its own row; `subagentRunId` is still `<invocation_id>:<node path>` of that wrapper.
- **Tool calls.** The quarantine wrapper tools appear as `TOOL_CALL_*` with `toolCallName` = `read_supplier_messages_structured` | `read_events_structured` | `search_playbooks_structured`, and humanised labels (`runtime/activity_labels.py`). Raw untrusted text never appears in an event.
- **Never streamed.** Events of the quarantine's `guard` and `extractor` agents (inputs, reasoning, outputs) are dropped by the translator and never persisted. Node progress lines are logged, not emitted.
- **Text.** `TEXT_MESSAGE_*` also comes from `promote_case`, `confirm_assumptions` and `explain`, which are deterministic nodes like `compose`.
- **Interrupts.** The approach interrupt id is `approach-<case_id>-<scenario version>`. `RUN_FINISHED.outcome.interrupts[].reason` is still `approach` | `approval`.

### M4 render blocks (updated M4, 2026-10-07)
New fields are omitted from dumps while at their defaults, so pre-M4 blocks are byte-identical:
- `approach_card@1` adds:
  - `checks: [str | {task_id, sentence}]`: M4 cards carry one object per plan task; M1 strings stay valid.
  - `agents: [{slug, display_name}]`: the agents the plan will consult.
  - `editable: [{key, label, kind: "days"|"pct"|"ids", value_int?, value_ids?}]`: the values **Edit** may change: `delay_days`/`duration_days` (days), `demand_change_pct`/`capacity_pct` (pct), `affected_receipts` (ids).
  - `mode: "what_if"` drives the what-if badge.
  - Each assumption may carry `unconfirmed: true` and `source_evidence_ids`.
  - `buttons` are unchanged (`confirm`, `edit`, `what_if`, `cancel`).
  - Edit rules: `blocks.approach_edit_violations(card, edits, removed_checks)`.
- `recommended_option@1` adds `unconfirmed: [{key, label, value_text, source}]` and `what_if: bool`.
  - `actions[].type` adds `promote` ("Promote to actual case", only when `what_if`) and `confirm_assumptions` ("Confirm these values", only with `unconfirmed`).
  - While `what_if` or `unconfirmed` is set, `apply` must be disabled with a reason: "What-if case: promote it to an actual case first." or "Confirm before applying: <labels>.". The model validator enforces this.
- `narrative@1` adds `what_changed: str | null`: one deterministic line for follow-ups and case actions, e.g. "Plan v1 → v2: …; units at risk A → B; recommendation still X." Its numbers come from the plans.

### M4 engine DB (updated M4, 2026-10-07)
- Migration `0005_kb.sql` adds `embedding_profile` (platform), `kb`, `kb_document`, `kb_chunk` and `kb_reembed_job`, all with RLS FORCE. `core/kb.py` creates one `kb_embedding_<profile_id>` table per profile: `vector(dims)`, or `halfvec(dims)` above 2000 dims, with an HNSW cosine index.
- `investigation_task` (existing table) is now written by `investigate`:
  - `id = "<case_id>:<task id>:r<rev>"`
  - `input = {task_id, rev, objective, tools, depends_on, assumption_keys, scenario_version, run_id}`
  - `status`: `done` | `failed`
  - one `finding` row per task run
- `evidence` rows written by the M4 nodes (connector `keelson` unless noted):

| `tool` / marker | prov | content |
|---|---|---|
| `investigation_plan` | inference | the plan |
| `investigation_findings` | — | verified findings state for later runs |
| `verify_numbers` | fact | the number-check report |
| `critic` | inference | the critique, only when it has issues |
| raw untrusted texts, `args.content_trust="untrusted_text"` | — | the texts as fetched |
| quarantine extractions, `args.content_trust="untrusted_extraction"` | inference | the extracted items |

- Scenario versions record each confirm, promote, refine and follow-up change. `Assumption.unconfirmed` / `source_evidence_ids` are omitted at their defaults, so the fingerprints of pre-M4 scenarios are unchanged.

## Changes through M5 (updated M5, 2026-10-07) — authoritative
Contract: `95-m5-remediation.md`. The runtime side (nodes, state keys, ports) is documented next to the code in `runtime/nodes/M5_NODES.md`; the graph, run envelope and stages are in `50` "M5 …". Merxa's side (manual REST, events, inject) is in `22` §8 and `20` "M5".

### M5 REST (updated M5, 2026-10-07)
| method & path | body / query | response |
|---|---|---|
| `GET /api/cases` | `?limit` | items add `last_signal_at`: when the newest re-plan signal was routed to the case (`case_signal.created_at`, null if none). The web shows the "Updated" badge from it. |
| `GET /api/cases/{id}` | — | each `open_interrupts[]` entry of kind `approval` adds `quorum` (`QuorumView`, see below) |
| `POST /api/cases/{id}/actions` | `{type: "rollback", change_set_id}` + `Idempotency-Key` | `202 {run_id}`. 422 `invalid_request` unless `change_set_id` is the only field. 404 `not_found` when the change set is not this case's. 409 `not_undoable {change_set_status}` unless it is `applied` or `verified`. 409 `rollback_pending {run_id}` while another rollback run of that change set is queued, running, or paused on a pending approval (the same `Idempotency-Key` replays instead). |
| `POST /api/cases/{id}/actions` | `apply` \| `guide` \| `promote` \| `confirm_assumptions` | unchanged from M4, plus 422 `invalid_request` when `option_id` or `plan_version` is missing or `change_set_id` is present |
| `GET /api/cases/{id}/checklists` | — | `{items:[{id, case_id, connector, status: open\|completed\|cancelled, change_set_id, plan_version, option_id, block_id, completed, completed_at, props}]}`; `props` = the `remediation_checklist@1` props |
| `POST /api/cases/{id}/checklists/{checklist_id}/steps/{seq}` | `{status: "done"\|"skipped"}` | `200 {step:{seq, status, updated_by}, changed, checklist:<item as above>}`. 404 `not_found` (checklist not in this case, unknown step, `seq` outside 1–1000). 409 `checklist_closed` (cancelled). 409 `already_verified` (read-back already verified it). |
| `POST /api/interrupts/{id}/decision` | `{decision, plan_version?, change_set_hash?, edits?: string (≤ 8000) \| [ApproachEdit] (≤ 50), removed_checks?: [str] (≤ 50), reason?: string (1–500)}` | `202 {status: "recorded"}` or `{status: "resumed", run_id}`, plus `quorum` after an approve on an approval interrupt. Errors below. |
| `GET /api/admin/policies` | — | `{spec_version, policy: {version, rules}, source: "spec"\|"default", editable, evaluator_available, fallback, defaults}` |
| `PUT /api/admin/policies` | `{base_version, rules: [PolicyRule] (1–50), version?}` | 201 (new spec version) or 200 with `unchanged: true`: `{spec_version, policy, source, compiled, guarded}`. 409 `version_conflict {latest_version}`. 409 `policy_rules_unsupported`. 422 `policy_invalid {errors}` (shape or CEL). 422 `policy_version_unchanged` (changed rules with the old version). 403 `guarded_change {fields, reasons}`. |
| `POST /api/admin/policies/test` | `{context: {cs, case, initiator}, rules?, version?}` | `{policy_version, decision: {outcome, rule_id, policy_version, requirement, reason}, approved_by, matched_rule, floors: {must_refuse, auto_allowed}}`. 422 `context_invalid` / `policy_invalid`. 503 `policy_evaluator_unavailable`. |
| `POST /api/demo/reset` | `{}` or `{keep_consents: bool}` | `200` step log (`DemoResetResult`), also when a step failed (`ok: false`). 404 unless `DEMO_MODE`. 403 `forbidden`. 409 `reset_in_progress` or `not_configured`. 422 `invalid_request`. |

- **Checklist steps.** A person may mark a step `done` or `skipped`; only read-back sets `verified`. Callers need `WRITE_ROLES` (planner, approver, tenant-admin); reading needs `READ_ROLES`. The route takes no `Idempotency-Key`: setting the status a step already has answers `changed: false`.
  - A change is audited (`checklist.step_done` / `checklist.step_skipped`) and re-renders the block in place.
  - It completes the checklist when every step is verified or skipped. It reopens a completed checklist when a step is outstanding again.
  - The run action `checklist_step` (`ActionPayload`, see `50` "M5 run envelope") is not accepted by this API: `ActionBody.type` has no `checklist_step`.
- **Decisions (`core.interrupts.record_decision_ex`).**
  - **Who.** Any `READ_ROLES` member (planner, approver, compliance, tenant-admin) may call the route; the interrupt's `quorum_spec.roles` then decide. Someone holding none of them gets 403 `forbidden {required_roles}`.
  - **Initiator.** With `exclude_initiator`, the person who initiated the change set gets 403 `forbidden`.
  - **Approve.** `change_set_hash` + `plan_version` must equal the interrupt's values and the case's latest plan version, else 409 `stale`. For a single change set the hash must also equal the change set row's (bundles are bound by the interrupt's bundle hash).
  - **Quorum counting.** Approvals are counted with `keelson_contracts.policy.quorum_assign` over the roles each person held, which are stored in `approval_decision.roles`. With `count ≥ len(roles)` every role must be covered (four-eyes: one approver and one compliance, two different people).
  - **Approvals that cannot count.** One that cannot advance the quorum, e.g. a second approver while compliance is missing, gets 403 `forbidden {missing_roles, required_roles}`.
  - **Repeats.** Repeating your own decision answers `recorded` again; a different decision by the same person gets 409 `superseded`.
  - **Reject or revise.** One by any eligible person resolves the interrupt at once. `reason` (≤ `MAX_DECISION_REASON_CHARS` = 500) is allowed only with `reject` or `revise`, else 422 `invalid_decision`. It is stored in `approval_decision.reason` and shown on the card's `rejection`.
  - **Approve pre-flight.** Only the quorum member counted under `approver` (the executor) needs an `apply_ready` Merxa link. Co-approvers such as compliance never do.
    - The decider is the executor and has no usable link → `412 {code: "connect_required", connect_url, link_status}`.
    - This approval completes the quorum but the executor is someone else without a usable link → `412 {code: "executor_connect_required", executor: {username, name, role}, link_status}`.
    - In both cases the decision is not recorded (the transaction rolls back).
  - **Resolution.** When the quorum is met the resume run's `decided_by` lists the executor first. Each decision re-renders the approval card in place (`ACTIVITY_SNAPSHOT`, stage `awaiting_approval`, label "1 of 2 approvals" or "<name> chose reject"). Audit `interrupt.<decision>` carries `{outcome, reason, quorum_remaining}`.
- **`QuorumView`** (`keelson_contracts.policy`): `{required: {roles, count, exclude_initiator}, approvals: [{sub, name, role, at}], remaining, missing_roles}`. The quorum is met when `remaining == 0`.
- **Policies.**
  - **Access.** All three routes need `tenant-admin` or `platform-admin`. `PUT` and `test` need a mutating session (CSRF + Origin). A `PUT` that changes the rules saves a new agent spec version (audited `agent_spec.update`); the test bench stores nothing.
  - **Storage.** The rules are part of the versioned agent spec (`policy.version` + `policy.rules`); a spec without rules uses the built-in default set (`core.policies.DEFAULT_POLICY`, version `p-1`). The `value-fashion-eu` pack stores the same rules explicitly as `p-2`.
  - **Versions.** A `PUT` without `version` bumps the policy version when the rules changed. Receipts name it: `approved_by: policy:<rule_id>@<version>`.
  - **Guarded changes (403 for tenant admins, allowed for platform admins).** Anything that could auto-approve more change sets, refuse fewer, or let fewer or different people approve. Examples: a new auto rule; an auto rule with a wider condition or moved ahead of another rule; a refuse rule removed or narrowed; an approval rule removed or weakened; a rule that now shadows a stronger one after it.
  - **Not guarded.** Lowering caps, removing auto rules, adding refuse rules and editing descriptions.
  - **CEL check.** A save is checked against the registered CEL evaluator (`runtime.policy.check_rule`). Without an evaluator the rules are stored with `compiled: false`.
- **Demo reset.** Allowed for `platform-admin` or `demo-presenter`. A presenter must also be a member of the demo tenant (`DEMO_TENANTS`, first entry).
  - It runs the steps of `keelson_contracts.demo.DEMO_RESET_STEPS` in order: `merxa_scenario_reset`, `keelson_rows`, `adk_sessions`, `valkey_keys`, `merxa_consents`, `token_broker`, `agent_spec`, `sim_clock`, `scn_resync`. Each step logs `{step, status: ok|skipped|failed, duration_ms, detail, counts}`. A failed step does not stop the reset.
  - `keep_consents: true` (rehearsal mode) skips `merxa_consents` and `token_broker`.
  - One reset per tenant at a time (Valkey lock, 120 s TTL). Audited as `demo.reset`.
  - `scripts/demo-reset.ps1` / `.sh` (`--keep-consents`, `--persona`, default `ava`) sign in through the persona switcher and call it.

### M5 SSE (updated M5, 2026-10-07)
- **No new event types.** M5 adds no LLM role, so there are no new `SUBAGENT_*` rows.
- **Stages** (updated M5 polish). `prepare_replan` → `building_options`, `prepare_rollback` → `preparing_changes`, `checklist_step` → `applying` (`NODE_STAGES`); routing, turn-control and question-answer nodes are listed in `STAGELESS_NODES` and leave the stage unchanged. The rest of a rollback or re-plan run maps as before (`compile_changeset`/`dry_run` → `preparing_changes`, approval → `awaiting_approval`, `execute` → `applying`, `reconcile` → `verified`).
- **Approval card refresh.** The decision route re-renders `approval_card@1` with `quorum` or `rejection` as an `ACTIVITY_SNAPSHOT` with the paused run's `runId`.
- **Checklist refresh.** Ticking a step, or a read-back that verified one, re-renders `remediation_checklist@1` in place: stage `applying`, prov `committed` for verified labels, else `decision`. The runtime's fallback verifier publishes with `runId` null and adds a "Guide completed: …" message.
- **Signal runs** (webhook re-plan) stream like any run. Their actor is the connector's webhook principal (`webhook:<connector>`, no roles).
- **Interrupt ids.**
  - Approval: `approval-<change_set_id>-<revision>`, or `approval-bundle-<first change set id>-<revision>` for a multi-connector bundle.
  - Rollback: `approval-rb-<change_set_id>-<attempt>`.
  - `RUN_FINISHED.outcome.interrupts[].reason` is still `approval` for all of them. The interrupt payload carries `quorum_spec`, `plan_version`, `change_set_hash`, and `change_set_id` (one member) or `change_set_ids` + `bundle_id` (bundle), plus `rollback: true` for an Undo.
  - It carries no `connector`, so the approve pre-flight always checks the executor's `merxa` link (the default in `routes_interrupts.decide`).

### M5 render blocks (updated M5, 2026-10-07)
New fields are omitted at their defaults, so pre-M5 blocks serialize byte-identically (`keelson_contracts.blocks`):
- **`approval_card@1`** adds:
  - `quorum: QuorumView | null`. Its roles must equal `required_roles`. It is set when the requirement needs more than one approval.
  - `rejection: {sub, name, role, at, reason} | null`.
  - `bundle: [{connector, system, change_set_id, change_set_hash, lines}]`, in apply order. With a bundle, `change_set_hash` is the bundle hash and `summary.lines` is the members' total.
- **`commit_tracker@1`** adds:
  - status `rolled_back`;
  - `actions: [{type: "undo", label: "Undo changes", enabled, disabled_reason}]`, enabled only while `applied`/`verified` and the connector can roll back;
  - `conflicts: [{entity_ref, detail}]`: later changes that blocked a rollback, shown while the change set stays applied.
- **`recommended_option@1`** adds `remediation_mix: {apply_lines, guide_lines}` ("Apply 3 changes for me · Guide me through 2"). `apply` cannot be enabled when `apply_lines == 0`. With both kinds of line, the labels become "Apply N changes for me" and "Guide me through M" (else "Apply changes for me" / "Guide me").
- **`narrative@1`** adds:
  - `banner: {text, plan_version, previous_plan_version, signal_id}` for a signal re-plan;
  - `plan_versions: [{version, label, status: proposed|committed|superseded|rolled_back, current}]`, ascending, exactly one current.
- **`remediation_checklist@1`** (new): `{checklist_id, connector, steps: [{seq, title, instructions_md, deep_link|null, expected: {entity_type, entity_ref|null, match}, status: pending|done|verified|skipped, verified_at|null, evidence_id|null}], progress: {done, total}}`.
  - Steps are numbered `1..n` (at most 100). `progress.done` counts the steps that are no longer pending.
  - Only a `verified` step carries `verified_at`, and only a verified step may carry `evidence_id`.
  - `deep_link` must be an `http(s)` URL with a plain host or a same-origin path (`is_safe_deep_link`; the JSON Schema pattern rejects `javascript:`, `data:` and `//` links).

### M5 webhooks (updated M5, 2026-10-07)
The receiver (`POST /api/webhooks/{tenant}/{connector}`) is unchanged. What Keelson does with an event:
- **Checklist read-back.** Any new stored event of a connector with an open checklist triggers a read-back of its expected entities on the worker's next guide tick (`core.checklists.poll_checklists`). Without events the read-back runs every `CHECKLIST_POLL_SECONDS` (60 s). Event payloads are never trusted: they only trigger the read-back through the connector's read tools (reader principal).
- **Re-plan routing** (`core.replan.route_event`, for the types in `REPLAN_EVENT_TYPES`, default `lane.capacity_changed`; `sailing.updated` is also routable when listed). Only structured fields are read:
  - `lane_code`;
  - `factor_bp` (0–1,000,000);
  - `weeks` (int, or a list of ≤ 52 ISO weeks);
  - `from_week` / `to_week` (ISO weeks);
  - `weeks_detail` (≤ 60 rows of `{iso_week, weekly_capacity_before, weekly_capacity, booked_units, overbooked_units}`, non-negative ints only).
  - `carrier_note` (free text) is never copied.
- **Which cases.** Every case whose latest plan version is open or committed and depends on the lane gets one `case_signal` row and one `signal` run with payload `{signal_id}`. The dependency can be the lane code (`matched_by` `lane`), the transfer pseudo-lane `ORIGIN>DEST` of the lane's (origin, dest) pair (`lane_pair`; plans record DC-to-DC transfers by DC pair, and the signal then carries `plan_lane_code`), or a receipt whose sailing runs on the lane in the current snapshot (`receipt`). The pair is the lane row's in the connector's current snapshot when it has the lane (an event `origin`/`dest` that contradicts it is ignored and dropped from the signal), else the event's typed `origin`/`dest` (or `origin_code`/`dest_code`). The run's actor has no roles and its idempotency key is `signal:<connector>:<event id>`. Routing is idempotent per (case, connector, event).
- **Routing failure.** It answers `503 replan_routing_failed`. The event is already stored, and the sender's retry routes again.
- **Stale marks** are unchanged (`change_set.`, `po_line.`, `inventory.`, `sailing.` and reset events). Of the M5 Merxa types only `po_line.updated` marks the snapshot stale. `transfer_order.created`, `allocation.updated`, `capacity_booking.*` and `lane.capacity_changed` do not: the tier-C poll catches up, and a re-plan run does its own bounded resync.

M5 event types sent by Merxa (payloads in `22` §8):

| type | sent when | fields Keelson uses |
|---|---|---|
| `lane.capacity_changed` | demo inject `lane_capacity` | `lane_code`, `factor_bp`, `weeks`, `from_week`/`to_week` (the window), `weeks_detail[]` (capacity before/after, booked and over-booked units per week) |
| `transfer_order.created` | manual transfer order (Guide step) | read-back trigger only |
| `allocation.updated` | manual allocation qty edit | read-back trigger only |
| `po_line.updated` | manual PO-line reschedule / qty change | read-back trigger; also marks the snapshot stale |
| `capacity_booking.created` / `capacity_booking.cancelled` | manual booking / cancel | read-back trigger only |

### M5 receipts (updated M5, 2026-10-07)
- **Four-eyes.** The receipt adds `co_approved_by: ["user:<username>", …]`: the other quorum members in approval order, at most 9. It appears only on `user:` approvals, never repeats `approved_by`, and is omitted while empty, so M1 receipts are byte-identical. `approved_by` / `approver_name` / `ext_sub` are the executor's. Keelson does not send the optional `co_approver_names` claim Merxa reads, so Merxa shows co-approvers by username.
- **Auto.** The autocommit principal applies, with `approved_by: policy:<rule_id>@<policy version>`, and the `policy_version` claim must match.
- **Rollback.** A fresh receipt: a new `approval_id`, the original change set hash, the executor's delegated token, `approval_ref` `APR-RB-…`, and idempotency key `sha256(tenant|change_set|rollback|attempt)`. A change set that was co-approved needs a co-approved rollback receipt (Merxa refuses one without `co_approved_by`).

### M5 engine DB (updated M5, 2026-10-07)
Migration `0007_m5_remediation.sql`. All new tables are RLS FORCE tenant tables with composite `(tenant, …)` foreign keys.
- `approval_decision` adds `roles jsonb` (the roles the person held) and `reason text`.
- `plan_dependency(tenant, plan_id, case_id, plan_version, kind lane|receipt|connector|dc, ref, source plan|change_set|runtime)`: what each plan version depends on, read by the re-plan router.
- `case_signal(id, tenant, case_id, connector, event_id, event_type, kind 'replan', payload, run_id, created_at)`, unique per (case, connector, event, kind).
- `checklist(id, tenant, case_id, connector, change_set_id, plan_version, option_id, block_id, meta, status open|completed|cancelled, created_by, …, completed_at, last_checked_at, last_event_id)`.
- `checklist_step(checklist_id, seq, title, instructions_md, deep_link, expected, status, verified_at, evidence_id, matched_ref, updated_by, updated_at)`: `verified` ⇔ `verified_at` is set.
- `change_set` (existing table):
  - status adds `rolling_back`, `rolled_back` and `invalid` (a bundle member whose dry-run failed);
  - `result` holds `requirement`, `rollback_requested`, `rollback_from` and `rollback_conflicts`;
  - the ledger adds `rolled_back`.
- `plan` status adds `superseded` and `rolled_back`.
