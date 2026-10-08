# 55 · Engine internal module contract (for parallel builders E1 / E2)

`services/engine` — one uv project (pyproject + uv.lock already exist; **do not change dependency pins**; if you truly need a new dependency, add it with `uv add` and report it). Package `keelson_engine` under `src/`. Two builders work in parallel with **strict file ownership**:

| Owner | Paths |
|---|---|
| **E1 platform** | `src/keelson_engine/{__init__,config}.py`, `core/**`, `api/**`, `bootstrap.py`, `migrations/**`, `Dockerfile`, `README.md`, `tests/platform/**` |
| **E2 runtime** | `src/keelson_engine/runtime/**`, `worker.py`, `tests/runtime/**`, `tests/fakes/**` |

Both may read everything. If you need something from the other side that doesn't exist yet, code against the signature below (it will exist) and, for your tests, use the fakes in `tests/fakes/` (E2 owns them; E1 may add fakes under `tests/platform/fakes/`).

## E1 provides (exact signatures)
```python
# config.py
class Settings(BaseSettings): ...   # every env var in compose.yaml (*engine-env) + defaults; get_settings() cached
# core/secrets.py
def read_secret(name: str) -> str        # /run/secrets/<name> else env NAME.upper()
# core/db.py
async def init_db() -> None              # create engines (keelson as role 'keelson'; adk via ADK service)
def keelson_engine() -> AsyncEngine
@asynccontextmanager
async def tenant_session(tenant: str) -> AsyncIterator[AsyncSession]   # BEGIN; SET LOCAL app.tenant_id = :tenant; RLS enforced
@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]              # for worker/bootstrap infra queries (runs queue, leases); role keelson_system BYPASSRLS
# core/crypto.py
def encrypt(plaintext: bytes, aad: str) -> str     # AES-256-GCM, returns "dk-1:<b64 nonce|ct>"
def decrypt(token: str, aad: str) -> bytes
def jwks() -> dict                                  # public keys ca-1, ar-1
def sign_client_assertion(client_id: str, audience: str) -> str
def sign_approval_receipt(claims: ApprovalReceiptClaims) -> str        # keelson_contracts.receipts
# core/events.py
class EventBus:
    async def append(self, *, tenant: str, case_id: str, run_id: str, event: dict, persist: bool = True, live: bool = True) -> int  # returns seq; sets metadata.sc.seq
    async def replay_then_tail(self, *, tenant: str, case_id: str, after: int | None) -> AsyncIterator[tuple[int, dict]]
def get_event_bus() -> EventBus
# core/runs.py
async def enqueue_run(*, tenant, case_id, kind, payload: dict, actor: dict, idempotency_key: str | None) -> str   # run_id (dedupe on key)
async def claim_next_run(worker_id: str, lease_seconds: int = 60) -> RunRow | None   # SKIP LOCKED + per-case lease
async def heartbeat(run_id: str, worker_id: str) -> bool
async def finish_run(run_id: str, status: Literal["succeeded","failed","paused","cancelled"], *, invocation_id: str | None = None, error: str | None = None) -> None
async def reap_expired(max_attempts: int = 3) -> list[str]     # re-queues expired leases as kind=crash_retry (keeps invocation_id)
async def is_cancel_requested(run_id: str) -> bool
# RunRow: id, tenant, case_id, kind, payload, actor, attempt, invocation_id, session_id, agent_spec_version, created_by
# core/interrupts.py
async def create_interrupt(*, tenant, case_id, run_id, kind: Literal["approach","approval"], interrupt_id: str, payload: dict, quorum_spec: dict, plan_version: int | None, change_set_hash: str | None, invocation_id: str) -> None
async def record_decision(...) -> Literal["recorded","resolved"]   # quorum check; on resolve inserts resume run (kind=resume, payload={interrupt_id, decision, decided_by:[...]})
async def supersede_open(tenant, case_id, reason) -> int
# core/tokens.py
class TokenBroker:
    async def service_token(self, *, tenant: str, connector: str, principal: Literal["reader","preparer","autocommit"]) -> str
    async def user_token(self, *, tenant: str, connector: str, keelson_sub: str) -> str      # refresh single-flight; raises ConnectRequired
    async def link_status(self, *, tenant, connector, keelson_sub) -> dict
def get_token_broker() -> TokenBroker
class ConnectRequired(Exception): connect_url: str
# core/connectors.py
async def get_connector(tenant: str, slug: str) -> ConnectorRow      # url, issuer, clients, lanes(config), tier, trust
async def list_approved_tools(tenant: str, connector: str) -> list[ToolRow]   # name, description, input_schema, output_schema, classification, definition_hash, status
async def get_tenant_llm_key(tenant: str) -> str
# core/repo.py  (domain persistence helpers, all tenant-scoped)
cases: create_case, get_case, add_message(case_id, role, text, run_id), set_case_mode
scenario: save_scenario(case_id, scenario: DisruptionScenario) -> int (version), latest_scenario(case_id)
evidence: save_evidence(case_id, run_id, connector, tool, args, result, prov) -> evidence_id, get_evidence(ids)
plans: save_plan(case_id, version, options: list[dict], recommended_option_id, kpis) , latest_plan(case_id)
blocks: save_block(case_id, run_id, envelope) -> block_id, list_blocks(case_id)
change_sets: create_change_set(case_id, plan_version, option_id, connector, items, hash) -> id; update_change_set(id, **fields); get_change_set(id)
ledger: ledger_mark(change_set_id, line_seq, state, ext_ref=None)
audit: admin_audit(actor, action, target, before, after)
```
E1 also owns: all API routes in `30-engine-api-events.md`, Authlib BFF + demo login + CSRF, Connect-Merxa OAuth flow, `/api/.well-known/jwks.json`, forward-auth, SSE endpoint (uses EventBus), migrations (RLS FORCE on tenant tables; role `keelson` (RLS) and `keelson_system` (BYPASSRLS, used only by system_session)), bootstrap (`python -m keelson_engine.bootstrap`: migrations; tenants from `/app/tenants.yaml`; LiteLLM alias upserts from `/app/models.lock` (GET /model/info → PATCH /model/{id}/update else POST /model/new; team per tenant; virtual key per tenant stored encrypted); connector rows; Merxa tool snapshot via one `tools/list` with the reader principal through the egress proxy, classifying tools from annotations and marking reads approved, writes `approval_gate`, `list_supplier_messages` as `untrusted_text`; idempotent on re-run).

## E2 provides
```python
# worker.py — main loop: claim_next_run → run_case(run) with heartbeat task + cancel watcher + reaper loop; graceful shutdown
# runtime/runner_cache.py
async def get_runner(tenant: str, spec_version: str) -> RunnerLease   # refcounted LRU of (App, Runner); DatabaseSessionService(ADK_DATABASE_URL)
# runtime/execute.py
async def run_case(run: RunRow) -> None    # builds RunRequest, sets contextvars, drives runner.run_async(...) (new / resume / crash_retry), translator → EventBus, finish_run
# runtime/workflow.py
def build_workflow(spec: AgentSpec) -> Workflow                       # graph in 50-engine-workflow.md
# runtime/llm.py: KeelsonLiteLlm, current_run_ctx contextvar, SentinelModel; FakeLlm (tests)
# runtime/toolsets.py: RegistryToolset(BaseToolset); runtime/mcp_client.py: connector_client(tenant, connector, principal) -> FastMCP Client via egress proxy
# runtime/plugins.py: EvidencePlugin, PolicyPlugin, BudgetPlugin, AuditPlugin
# runtime/translator.py: Translator(case_id, run_id, tenant, bus).handle(adk_event) / start() / finish(outcome)
# runtime/compose.py: fill_placeholders(text, ctx) -> (text, violations); numeric_lint(text, allowed_tokens) -> violations; format_value(value, unit)
# runtime/optimizer_client.py: impact(req) / optimize(req) with service JWT (HS256 OPTIMIZER_SHARED_SECRET)
# runtime/problem.py: gather_problem_data(tenant, scenario) -> ProblemData (MCP reads: list_locations, get_inventory, list_purchase_orders/get_purchase_order, get_demand_forecast, list_allocations + tenant lanes)
# runtime/commit.py: compile_changeset, dry_run (preparer), mint receipt (core.crypto), execute (delegated or autocommit token), reconcile
# runtime/policy.py: approval decision per 50-engine-workflow.md
# runtime/nodes/*.py: one module per node group
```
API → worker hand-off is only through `core.runs` / `core.interrupts` tables (+ Valkey wake-up `XADD keelson:runs:wake`). The API never runs ADK.
(updated M4, 2026-10-07: the lists above are the M1 contract. As built, `build_workflow(spec, bindings)` takes `WorkflowBindings(models)`, the repo port has the M4 investigation methods, and the runtime has `scn` and `kb` ports. See "Changes through M4" below.)

## Changes through M4 (updated M4, 2026-10-07) — authoritative
The E1/E2 split still applies (E1 = `config.py`, `core/**`, `api/**`, bootstrap, migrations; E2 = `runtime/**`, `worker.py`). The runtime reaches the platform only through the ports in `runtime/deps.py`. `CoreAdapter` binds the ports to `core.*` lazily; a missing optional helper falls back to SQL or degrades without failing.

### E1 provides — M3/M4 additions (updated M4, 2026-10-07)
```python
# core/scn_sync.py — canonical scn/1.0 store (80 §1). Errors: ScnSyncError(.code, .message) ⊃ SyncUnavailable,
# SyncInProgress, SyncFailed(.snapshot); SnapshotNotFound / PlanNotFound (LookupError)
async def current_snapshot(tenant, connector) -> SnapshotRow | None      # the ready snapshot scn_current points to
async def get_snapshot(tenant, snapshot_id) -> SnapshotRow | None
async def list_snapshots(tenant, connector, limit=20) -> list[SnapshotRow]
async def sync_state(tenant, connector) -> dict | None
async def pin_snapshot(tenant, snapshot_id, plan_id: int) -> None       # idempotent; GC never deletes a pinned snapshot
async def unpin_plan(tenant, plan_id: int) -> int
def is_stale_event(event_type) -> bool / is_reset_event(event_type) -> bool   # change_set./po_line./inventory./sailing.;
                                                                            # scenario.reset + merxa.*.reset (90 §10)
async def mark_stale(tenant, connector, reason) -> None                  # debounced (webhooks, polls, after an apply)
async def request_sync(tenant, connector, reason="admin") -> dict        # due now (start-up, admin, nightly)
async def due_syncs(limit=20) -> list[DueSync]                           # DueSync(tenant, connector, reason, stale_since)
async def list_sync_connectors() -> list[tuple[str, str]]                # active + approved snapshot tools
async def poll_changes(tenant, connector) -> bool                        # tier C scn.changes_since; True = marked stale
async def sync_connector(tenant, connector, *, reason) -> SnapshotRow    # one full sync; single-flight per connector
async def purge_connector(session, tenant, connector) -> None            # connector deletion
# core/kb.py — knowledge base (90 §7). Errors: KbError(status, code, message, **extra)
async def search(tenant, query, kb_ids=None, top_k=6) -> list[dict]      # [{chunk_id, document_title, heading, text, score}]
                                                                         # best first; untrusted_text (quarantine only)
async def ingest_document(tenant, kb_id, filename, data, *, title=None, created_by=None) -> (document, created)
async def list_kbs / get_kb / create_kb / get_or_create_kb / list_documents / delete_document / tenant_has_documents
async def active_profile() / list_profiles() / ensure_profile(alias, provider_model, dims, *, actor="bootstrap")
async def embed_texts(tenant, profile, texts) -> list[list[float]]       # LiteLLM /embeddings, tenant key
async def start_reembed(scope, tenant, actor, …) / run_reembed(job_id, …) / list_reembed_jobs(tenant, *, platform=False)
async def wait_for_jobs() / cancel_jobs()                                # API shutdown
def seed_dir() -> Path | None; async def seed_tenant(tenant, directory, *, actor="bootstrap") -> dict
# core/repo.py — additions used by the runtime
async def upsert_investigation_task(case_id, task_id, specialist, *, run_id=None, status="pending", input_=None, tenant=None)
async def save_finding(case_id, task_id, run_id, findings, *, tenant=None) -> int
async def save_calc / get_calc / list_evidence / get_block / get_block_data / set_plan_status / list_ledger …
# core/agent_specs.py — async def upgrade_builtins(tenant, *, actor) -> str | None (bootstrap): adds the built-in M4 specialists a tenant spec's history
# never had and lifts budgets still at the M3 default; a specialist an admin removed is never re-added
```
`core.repo` has no `latest_evidence` or `list_investigation_tasks`. The runtime adapter (`deps._RepoAdapter`) implements both with one tenant-scoped SQL read each, through `tenant_session` with an explicit tenant predicate.

### E2 provides — M3/M4 additions (updated M4, 2026-10-07)
```python
# runtime/deps.py — ports (keyword-only args, all tenant-scoped)
class RepoPort(Protocol):   # M1 methods + …
    async def recent_messages(*, tenant, case_id, limit) -> list[dict]
    async def set_case_status(*, tenant, case_id, status) -> None
    async def latest_evidence(*, tenant, case_id, tool) -> dict | None          # M4: newest row by tool (plan, problem data, findings)
    async def save_plan(*, tenant, case_id, version, options, recommended_option_id, kpis) -> Any   # returns plan row id (scn_pin)
    async def mark_plan_status(*, tenant, case_id, version, status) -> None
    async def save_block(*, tenant, case_id, run_id, envelope, data=None) -> str  # data = payload served at data_ref
    async def list_change_sets(*, tenant, case_id) -> list[dict]
    async def get_agent_spec(*, tenant, version) -> dict | None
    async def save_findings(*, tenant, case_id, task_id, specialist, run_id, findings, status, task_input=None) -> None
        # M4: upserts investigation_task (task_input → input) + appends the finding row
    async def list_investigation_tasks(*, tenant, case_id) -> list[dict]
        # M4: [{task_id: "<plan task id>-r<rev>", id, specialist, status, input, findings}] oldest first (dirty set)
class KbPort(Protocol):     # M4; results are untrusted_text: only runtime.quarantine calls it
    async def search(*, tenant, query, kb_ids=None, top_k=6) -> list[dict]     # {chunk_id, document_title, heading, text, score}
        # adapter: core.kb.search; KbError → LookupError ("knowledge base unavailable"); no core.kb → []
class ScnPort(Protocol):    # runtime/scn_store.py (M3), adapter CoreScnAdapter over core.scn_sync
    current_snapshot, pin_snapshot, unpin_plan, mark_stale, sync_connector, due_syncs, poll_changes, request_sync,
    startup_targets() -> [(tenant, connector)], reader(tenant) -> async ctx of SqlReader.fetch(sql, params)
@dataclass
class RuntimeDeps: repo, bus, tokens, crypto, connectors, runs, interrupts, reasoning_sink=None, scn=None, kb=None
# runtime/workflow.py: build_workflow(spec, WorkflowBindings(models)) — graph in 50 "M4 graph"
# runtime/quarantine.py: quarantine_tools_for(sources, *, connector, guard, extractor, owner), QUARANTINE_NODE_NAMES,
#                        display_results, is_untrusted_evidence, is_untrusted_text_row
# runtime/nodes/: investigate.py (make_planner, make_investigate, make_specialist_wrapper), review.py (verify_numbers,
#   make_critic), framing.py, refine.py, followup.py, taxonomy.py (check_taxonomy at worker start-up), case_state.py
#   (ensure_planning_state), planning.py (impact, simulate, make_compose), m4_defaults.py (stand-ins, end_turn)
# runtime/settings.py: WorkerSettings (env prefix WORKER_) — see services/engine/README.md "Configuration"
```
- **Worker (M3/M4).** Beside the claim loop, `worker.py` runs a scn sync task. At start-up it calls `request_sync(…, "startup")` for every syncable connector. Then every `WORKER_SCN_DUE_INTERVAL_S` it runs the due syncs one at a time (each bounded by `WORKER_SCN_SYNC_TIMEOUT_S`), the tier-C change-feed poll every `WORKER_SCN_POLL_INTERVAL_S` (each bounded by `WORKER_SCN_POLL_TIMEOUT_S`), and one nightly resync from `WORKER_SCN_NIGHTLY_HOUR_UTC`. A sync never blocks case runs.
- **Start-up checks.** `worker.check_startup_data()` runs `taxonomy.check_taxonomy()` (`events.yaml` vs `EVENT_TYPE_KEYS`, plus `chokepoints.json`) before the worker starts. A `TaxonomyError` (a `RuntimeConfigError`) stops the process.
- **API → worker hand-off** is unchanged: only `core.runs` / `core.interrupts` tables plus the Valkey wake-up. New M4 action payloads go through `ActionPayload` (`confirm_assumptions` + `assumption_keys`). `core.interrupts.record_decision_ex` still writes `ResumePayload.edits` as text only, with no `removed_checks` (see `30` M4 REST, known gap).
  (updated M5, 2026-10-07: closed. `record_decision_ex` takes typed `edits` + `removed_checks`, stores them as JSON in `approval_decision.edits` and passes them on in `ResumePayload`.)

## Changes through M5 (updated M5, 2026-10-07) — authoritative
Contract: `95-m5-remediation.md`. API and blocks are in `30` "M5 …", the graph in `50` "M5 …", and the runtime detail in `runtime/nodes/M5_NODES.md`. The E1/E2 split is unchanged; the runtime still reaches the platform only through `runtime/deps.py` ports. Migration: `0007_m5_remediation.sql` (`30` "M5 engine DB").

### E1 provides — M5 additions (updated M5, 2026-10-07)
```python
# core/policies.py — rule storage in the agent spec, admin guards, evaluator port (95 §1)
DEFAULT_RULES / DEFAULT_POLICY (version "p-1") / FALLBACK_REQUIREMENT   # = runtime.policy.default_rules(default config)
def register_policy_evaluator(fn, check=None) -> None   # fn(PolicySet, PolicyContext) -> PolicyDecision (sync or async)
def install_runtime_evaluator() -> bool                 # engine-api: registers runtime.policy.evaluate_policy (also lazily)
async def evaluate(rules: PolicySet, context: PolicyContext) -> PolicyDecision
    # raises PolicyEvaluatorUnavailable (no evaluator) / PolicyEvaluationError (fail closed)
def effective_policy(policy_section) -> (PolicySet, "spec" | "default")
def guarded_changes(old: PolicySet, new: PolicySet) -> list[{field, reason}]   # non-empty → platform-admin only
async def get_policies(tenant) / put_policies(tenant, base_version, rules, actor, *, platform_admin, version=None)
async def test_policy(tenant, context, *, rules=None, version=None)
# core/interrupts.py — quorum (95 §1)
async def record_decision_ex(*, tenant, interrupt_id, decider, decision, plan_version=None, change_set_hash=None,
                             edits=None, removed_checks=(), auth_time=None, reason=None,
                             executor_check=None) -> DecisionOutcome          # DecisionOutcome(status, run_id, quorum)
    # errors: InterruptNotFound, InterruptClosed, StaleDecision, NotEligible(.details), InvalidDecision
def build_quorum_view(intr, rows) -> QuorumView; def quorum_executor(view) -> QuorumApproval | None   # 'approver', else first
async def quorum_progress(tenant, interrupt_ids) -> dict[str, QuorumView]
# core/tokens.py
APPLY_SCOPE = "changes:apply"; def apply_ready(link) -> bool; TokenBroker.connect_url(connector, return_to=None) -> str
# core/checklists.py — Guide checklists + read-back (95 §2); errors ChecklistError(status, code, message)
async def create_checklist(tenant, case_id, connector, steps, *, checklist_id=None, change_set_id=None, plan_version=None,
                           option_id=None, block_id=None, created_by=None, meta=None) -> str    # id; idempotent
async def get_checklist(tenant, checklist_id) / list_open_checklists(*, tenant=None) / list_checklists(tenant, case_id)
async def set_checklist_step(*, tenant, checklist_id, seq, status, verified_at=None, evidence_id=None, actor=None)
async def set_checklist_status(*, tenant, checklist_id, status)            # open | completed | cancelled
async def set_step_status(tenant, case_id, checklist_id, seq, status, actor) -> dict   # API: done | skipped, audited
async def verify_checklists(tenant, connector, event=None) -> VerifyResult # read-back, reader principal, evidence prov fact
async def poll_checklists(*, interval_s=None, limit=20) -> list[VerifyResult]   # worker hook: new events or 60 s
# core/replan.py — plan dependencies + webhook re-plan routing (95 §4)
def extract_dependencies(value, *, connectors=()) -> PlanDependencies     # lanes, receipts, connectors, dcs
async def record_dependencies(session, *, tenant, plan_id, case_id, plan_version, deps, source="plan", replace=True)
async def lane_pairs(tenant, connector, lane_code, *, origin=None, dest=None) -> tuple[str, ...]  # 'O>D'; snapshot lane row wins
async def find_dependent_plans(tenant, connector, lane_code, *, origin=None, dest=None, pairs=None) -> list[ReplanTarget]
async def route_event(tenant, connector, event_id, event_type, payload) -> list[str]   # signal run ids
def signal_actor(connector) -> {sub: "webhook:<c>", username, name, roles: []}
# core/signals.py
async def get_signal(*, tenant, signal_id) -> {id, case_id, connector, event_id, type, kind, data, received_at, run_id} | None
# core/repo.py — additions
async def list_plan_versions(case_id, *, tenant=None) -> [{version, status, created_at}]
async def save_plan_dependencies(case_id, plan_version, dependencies, *, tenant=None) -> int
list_cases(...) rows add last_signal_at; save_plan / create_change_set record dependencies themselves
# core/demo_reset.py — full demo reset (95 §6)
async def reset_demo(*, keep_consents: bool, actor, tenant=None) -> DemoResetResult   # raises DemoResetBusy
def demo_tenant() -> str                                                  # DEMO_TENANTS, first entry
# core/keycloak_admin.py — KeycloakAdmin.revoke_user_grants(realm, usernames, client_ids) (demo reset consents step)
```
- **Settings** (`config.py`):
  - `REPLAN_EVENT_TYPES` (default `lane.capacity_changed`);
  - `CHECKLIST_POLL_SECONDS` (60);
  - `DEMO_TENANTS` (`demo-retail`);
  - `DEMO_MERXA_USERS` (`marco.rossi,priya.shah`);
  - `DEMO_MERXA_ADMIN` (`merxa.admin`).
- **Webhooks.** `core.webhooks.receive` stores the event, marks the snapshot stale for data-changing types, then calls `replan.route_event`. A routing failure answers 503 `replan_routing_failed`, so the sender retries.

### E2 provides — M5 additions (updated M5, 2026-10-07)
```python
# runtime/deps.py — new optional ports on RuntimeDeps (None = feature unavailable)
class ChecklistsPort(Protocol):   # create_checklist(…, checklist_id, connector, plan_version, option_id, steps, meta),
    # get_checklist, list_open_checklists, set_step, set_status; optional poll() → core.checklists.poll_checklists
class SignalsPort(Protocol):      # get_signal(*, tenant, signal_id), latest_event_id(*, tenant, connector)
class PlansPort(Protocol):        # list_plan_versions(*, tenant, case_id), save_plan_dependencies(*, tenant, case_id, plan_version, dependencies)
@dataclass
class RuntimeDeps: …, checklists=None, signals=None, plans=None
# adapters: _ChecklistsAdapter (block_id = stable_block_id(case_id, "remediation_checklist", checklist_id), so the
# platform refreshes the runtime's block in place), _SignalsAdapter (fallback: SQL on connector_event), _PlansAdapter
# runtime/policy.py — CEL (cel-expr-python 0.1.3)
def evaluate_policy(rules, context, *, fallback=None, allow_auto=True, version=None) -> PolicyDecision
def check_rule(when: str) -> list[str]; def default_rules(config: PolicyConfig) -> list[PolicyRule]
def register_policy_evaluator() -> bool             # worker start-up → core.policies.register_policy_evaluator
def build_context(…) -> PolicyContext; def decide_approval(…) -> ApprovalDecision
# runtime/candidates.py: capability_table(…); runtime/remediation.py: partition_actions(…) -> Partition, delta_actions(…)
# runtime/guide.py: build_checklist, expected_for, checklist_id_for, verify_checklist, GuideVerifier (fallback loop)
# runtime/nodes/commit_nodes.py: compile_changeset (apply | guide, bundles), dry_run, approval_gate, approval_check,
#   execute, reconcile, prepare_rollback; approval_interrupt_id(cs, rev), rollback_interrupt_id(cs, attempt), rollback_key
# runtime/nodes/guide_nodes.py: checklist_step;  runtime/nodes/replan.py: prepare_replan
```
- **Worker (M5).** At start-up it calls `runtime.policy.register_policy_evaluator()`. When `WORKER_GUIDE_VERIFY_ENABLED` is set (default true) and the checklists port exists, a guide task runs every `WORKER_GUIDE_TICK_INTERVAL_S` (5 s). Each tick is bounded by `WORKER_GUIDE_VERIFY_TIMEOUT_S` (120 s) and calls the port's `poll()`, else `guide.GuideVerifier` (`WORKER_GUIDE_POLL_INTERVAL_S`, 60 s). It never takes a run slot.
- **Spec.**
  - `PolicyConfig` adds `rules`, `compliance_role`, `auto_initiator_role`, `four_eyes_cost_cents` and `four_eyes_ops`.
  - `spec.remediation` (`RemediationDefaults`) holds the extra write `connectors`, `replan_resync_timeout_s` (default 20, ≤ 120) and `guide_sop_snippets` (default true).
  - `agent_specs.reset_to_default` (demo reset) restores the pack default.
- **Stages** (closed in M5 polish). `NODE_STAGES` maps `prepare_replan`, `prepare_rollback` and `checklist_step`; `STAGELESS_NODES` lists the nodes that never change the stage.
