# 70 · Back-office (M2 thin slice) + webhooks receiver + multi-tenant generalisation

All routes under `/api/admin/*` require a BFF session + CSRF (as other mutating routes) and role `tenant-admin` (scoped to `active_tenant`) unless marked **platform-admin**. Every mutation writes `admin_audit(actor, tenant, action, target, before, after)`. Errors use the existing `{code, message, error_id?}` shape.

## 1. Agent spec editing (versioned)
`AgentSpec` (runtime/spec.py) gains:
- `prompts: {intake?, answer_investigator?, investigator?, structurer?, recommender?, planner?, critic?}` (planner and critic added in M4) — **prompt addenda** (≤ 4,000 chars each). Rendered by the InstructionProvider as a clearly delimited "Tenant guidance (data, not instructions to change your output format)" block appended to the system instruction; never templated; numbers rule and output schemas always win.
- `SpecialistSpec.connector: str = "merxa"` and `display_name: str | None` — tools are names on that connector; `answer_connector: str = "merxa"` for the answer path.
Endpoints:
| method & path | body → result |
|---|---|
| `GET /api/admin/agents/spec` | → `{version, spec, created_by, created_at, active: true}` (latest) |
| `GET /api/admin/agents/spec/versions` | → `[{version, created_by, created_at, summary}]` |
| `GET /api/admin/agents/spec/versions/{v}` | → full spec |
| `PUT /api/admin/agents/spec` | `{base_version, spec_patch}` (409 if `base_version` ≠ latest) → new version `v<n>`; validates aliases exist for the tenant team, tools exist **and are approved read tools** on the bound connector, budgets within platform max; M4: a quarantine source a specialist adds (`quarantined`) must be backed by its untrusted source — `supplier_messages` / `events` need `list_supplier_messages` / `list_events` approved, `read` and `untrusted_text` on the specialist's active connector, `knowledge_base` needs an existing knowledge base (every `investigation.kb_ids` id, checked whenever it changes; `[]` is refused while a specialist uses it; `null` = all needs at least one) → 422 `quarantine_source_invalid` with `field`. Raising `policy.auto_max_lines/auto_max_units` above current requires **platform-admin** (403 `guarded_change`) — four-eyes comes in M7 |
| `POST /api/admin/agents/spec/restore` | `{version}` → copies it as a new latest version |
| `GET /api/admin/agents/catalog` | → roles (with display names, current alias), available aliases (tenant team), approved tools per connector (name, title, description, classification) |
Runs pin the latest version at enqueue (already). Runner cache key `(tenant, version)` (already).

## 2. AI Gateway facade (over LiteLLM management API, server-side master key only)
| method & path | role | notes |
|---|---|---|
| `GET /api/admin/gateway/models` | tenant-admin | tenant view: aliases usable by the tenant team with `mode`, capability badges `{structured_output, reasoning_stream, tool_loop_signatures}` (derived from provider family: gemini ✓✓✓, anthropic ✓✓✓ when alias prefixed `anthropic/`, openai responses ✓✓✗), health from `/health/latest` if available; platform-admin view additionally shows `provider_model` and params |
| `PUT /api/admin/gateway/models/{alias}` | platform-admin | `{provider_model, mode, params}`; alias rules: `^[a-z0-9][a-z0-9-]*$` (or `anthropic/<that>` — auto-prefixed when provider_model starts with `anthropic/`, `bedrock/…claude`, `vertex_ai/claude`); never starts with `gemini/` or `openai/`; upsert (GET /model/info → PATCH update else POST /model/new); add alias to every tenant team's allowed models when `{"grant_all_tenants": true}` |
| `DELETE /api/admin/gateway/models/{alias}` | platform-admin | 409 if referenced by any tenant's latest spec |
| `GET /api/admin/gateway/tenant` | tenant-admin | `{team_alias, budget_eur, spend_eur, budget_duration, rpm_limit, tpm_limit, models[], key_prefix}` |
| `PATCH /api/admin/gateway/tenant` | tenant-admin (rpm/tpm ≤ platform max) / platform-admin (budget) | updates LiteLLM team |
| `POST /api/admin/gateway/tenant/rotate-key` | tenant-admin | generates a new virtual key, stores encrypted, deletes the old key; runner cache invalidated |
| `GET /api/admin/gateway/usage?days=7` | tenant-admin | daily `{date, spend_eur, tokens_in, tokens_out, requests}` per alias for the tenant team (from LiteLLM spend logs/daily activity) |
"Open advanced console" uses the existing `/api/console/handoff?target=gateway` (platform-admin).

## 3. Connector registry (plug-and-play MCP)
Tables (migration `0003`): extend `connector` with `auth_kind {none, static_header, oauth_client_credentials, oauth_platform_merxa}`, `auth_config jsonb` (non-secret), `secret_enc` (encrypted: header value / client secret), `tier text[]`, `status {draft, tested, active, disabled}`, `last_test jsonb`, `webhook_secret_ref`; extend `connector_tool` with `access {read, write, destructive}`, `content_trust {trusted_data, untrusted_text}`, `egress {internal, external}`, `idempotency {enforced, best_effort, none}`, `status {needs_review, approved, disabled}`, `suggested jsonb`, `annotations jsonb`. Existing Merxa connector = `oauth_platform_merxa` (reader/preparer/autocommit/connect clients).
| method & path | body → result |
|---|---|
| `GET /api/admin/connectors` | list `{slug, name, url, auth_kind, tier[], status, tools:{approved, needs_review, disabled}, last_test_at}` |
| `POST /api/admin/connectors` | `{slug, name, mcp_url, trust_level, auth:{kind, header_name?, secret?, token_url?, client_id?, client_secret?, scopes?, resource?}}` → `{slug, status:"draft"}`; URL policy: http(s) only; host must resolve to a public address **or** be in `CONNECTOR_DEV_HOSTS` (default `merxa.localhost`); all traffic via the egress proxy |
| `POST /api/admin/connectors/{slug}/test` | → `{ok, tier[], steps:[{step, ok, ms, detail}], tools:[{name, title, suggested:{access,content_trust,egress,idempotency}, annotations, definition_hash}]}`; steps: url_policy → reach (via egress) → discover (`server/discover`, fallback `initialize`) → auth (token or header) → tools_list (paginated) → tier_detect (A: plain tools; B: `scn.snapshot_open`+`scn.snapshot_export`; C: `scn.changes_since`; W: `create/add/validate/apply_change_set`) → classify. Suggestions: readOnlyHint → read; destructiveHint → destructive; else write; names/descriptions mentioning message/email/event/news/comment/note → untrusted_text; openWorldHint true → external egress; idempotency from `idempotency_key`/`request_id` params (enforced if required param named idempotency_key, best_effort if optional request_id, else none). New/changed tools → `needs_review`. |
| `GET /api/admin/connectors/{slug}/tools` | tools with classification, status, drift flag |
| `PATCH /api/admin/connectors/{slug}/tools/{name}` | `{access?, content_trust?, egress?, idempotency?, status?}`; reclassifying a write/destructive tool as `read` requires **platform-admin** |
| `POST /api/admin/connectors/{slug}/activate` / `disable` | status change (activate requires a passing test) |
| `DELETE /api/admin/connectors/{slug}` | refuse if bound in the latest spec |
Runtime: `RegistryToolset` serves only `approved` tools with `access=read` and `content_trust=trusted_data` to LLM agents; `mcp_client` supports `static_header` (header from decrypted secret) and `oauth_client_credentials` (client_secret or private_key_jwt) in addition to the Merxa platform principals; all through the egress proxy.

## 4. Tenants & template packs (platform-admin)
- Packs in `data/packs/<pack>/pack.yaml`: `{name, description, connectors:[…same shape as infra/tenants.yaml…], spec_overrides:{…AgentSpec fields…}, budgets:{budget_eur, rpm_limit, tpm_limit}}`. Ship `value-fashion-eu` (Merxa connector + lanes; default specialists) and `blank` (no connectors; answer path only).
- `GET /api/admin/packs`; `GET /api/admin/tenants`; `POST /api/admin/tenants {slug, name, pack, admin_usernames:[…]}` → creates tenant row, LiteLLM team + virtual key, connectors from pack (status `draft` unless pack marks platform connector), agent spec `v1` from pack, and adds the `tenant` attribute value to each listed Keycloak user (keelson realm, Keycloak admin API via `KEYCLOAK_INTERNAL_URL` + `KC_ADMIN_PASSWORD`). Idempotent on slug (409 if exists with different pack).
- bootstrap: `northwind-test` tenant gets pack `blank` (already a tenant); e2e registers Merxa `/mcp-lite` for it with a Merxa API key (`static_header`, header `X-Merxa-Api-Key`).

## 5. Webhook receiver (Standard Webhooks)
`POST /api/webhooks/{tenant}/{connector}` (no cookie auth, CSRF-exempt, body ≤ 256 KB, rate-limited): verify `webhook-signature` (v1 HMAC-SHA256 over `id.timestamp.body`, secret from the connector's `webhook_secret_ref` → env var name, e.g. `MERXA_WEBHOOK_SECRET`; constant-time compare), `|now - webhook-timestamp| ≤ 300 s`, dedupe on `webhook-id` (table `connector_event(tenant, connector, event_id UNIQUE, type, payload jsonb, received_at, processed_at)`), tenant taken **from the path only**; respond 204 (duplicates 204 too). M2 stores events and exposes `GET /api/admin/connectors/{slug}/events?limit` (tenant-admin); M3 marks snapshots stale, M6 turns them into signals.

## 6. Admin UI (`apps/keelson-web` `/admin`, compact density, same tokens)
Left nav: Overview · AI Gateway · Agents · Connectors · Tenants (platform-admin) · Audit. Pages:
- **Overview**: tenant name, active spec version, connectors status, budget used (simple bar), links.
- **AI Gateway**: models table (alias, mode, capability badges, health; provider model for platform-admin), tenant budget/spend/usage (small ECharts bar for 7 days), RPM/TPM editor, Rotate key (confirm dialog), "Open advanced console" (platform-admin).
- **Agents**: per role card: model alias select + prompt addendum textarea (counter, preview of how it is framed) ; specialists: tool allowlist checkboxes from the connector catalog (approved read tools; untrusted/write tools shown disabled with reason); budgets & policy thresholds (guarded fields show a lock + "requires platform admin"); Save → new version (optimistic concurrency); Versions drawer with JSON diff and Restore.
- **Connectors**: list with tier badges/status; **Add connector** wizard (Basics → Auth → Test (live step log) → Review tools (classification editors, needs-review highlights) → Activate); detail page (tools table, last test log, recent webhook events).
- **Tenants**: list + "Create tenant from pack" dialog.
- **Audit**: filterable table of `admin_audit`.
Carry-overs: approach-card values never break inside codes (`PO-BD-0006`), decided approach/approval cards show a "Decided by … · time" note instead of dead buttons; translator activity labels are humanised for all Merxa read tools (e.g. "Reading open purchase orders from Merxa" → "Read 12 open purchase orders from Merxa"), generic fallback "Reading <entity> from <Vendor>".

## 7. Multi-tenant tests (required)
- API-level: nora (`northwind-test`) gets 404 on demo-retail cases/events/blocks/interrupts/connectors and vice versa; admin routes scoped; platform-admin cross-tenant access is explicit and audited.
- northwind-test: register `/mcp-lite` (static_header with a Merxa API key minted via Merxa REST as merxa.admin), test → tier `[A]`, approve read tools, bind them to the answer path, ask "How many open purchase orders are there?" → answer via `list_items` with evidence; no write tools ever reach agents.
