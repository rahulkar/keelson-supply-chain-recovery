# End-to-end tests (real stack)

## `api_thin_slice.py` — M1 thin slice (+ M3 options and canvas) through the API

Runs the whole M1 story against the running compose stack, using the real Gemini models
behind LiteLLM, Keycloak, Merxa and the optimizer. It talks HTTP only (`httpx`), like a
browser would, and needs no engine code.

1. Reset the demo: Merxa's scenario reset as `merxa.admin` (`POST /v1/admin/scenario:reset`,
   the golden seed already carries the story, see below); then (M3) tenant admin `sam` queues a
   canonical-store sync (`POST /api/admin/connectors/merxa/sync`) and waits until a newer `scn`
   snapshot is current (`GET …/snapshots`) — cases are framed from the snapshot and a Merxa reset
   is not a webhook event; then disconnect `marco`'s Merxa link
   in Keelson (`DELETE /api/connectors/merxa/link`: RFC 7009 revocation at Keycloak + local
   revoke), so step 7 always exercises `412 connect_required` → Connect.
2. Keelson demo login-as `priya`, create a case, post the trim-delay message.
3. Follow the case SSE stream until `RUN_FINISHED`, printing a timeline of stages,
   agents, tool labels, blocks and the composed message. Events are attributed to their run by
   `metadata.sc.runId` (every persisted event of the followed run must carry it; events of
   other runs are skipped), and a `RUN_ERROR` / `SUBAGENT_ERROR` message must not contain
   exception text (it shows a generic message + `metadata.errorId`).
4. If the approach card pauses the run, decide `confirm` and follow the resume run.
5. Expect `kpi_strip` (final, not preliminary), `recommended_option`, `plan_diff` and
   `narrative` blocks, and no `{{…}}` placeholders in the narrative or the stored assistant
   message. M3 (`docs/contracts/80` §4–§5): `scenario_compare` lists `opt-A`…`opt-D` + `do-nothing`,
   each with a full scorecard, exactly one recommended (= `recommended_option`); Do nothing has no
   cost or changes and no option loses more than it. The canvas holds `scenario_compare`,
   `network_map`, `impact_graph`, `stockout_timeline`, `plan_diff` (inline: `kpi_strip`,
   `recommended_option`); map, graph and timeline carry `data_ref = /api/blocks/{id}/data`, and
   each payload is fetched and validated with the `keelson_contracts` models (when
   `packages/contracts-py/src` is on `PYTHONPATH`, as below) plus structural checks (link / edge
   ends exist, nodes inside the bbox, ≤ 300 graph nodes, ≤ 40 timeline rows, sea lanes follow
   catalogued waypoints, option ids known).
6. Action `apply` for `opt-A` → expect a human approval interrupt (`approval_card`
   requiring `approver`; Priya is a planner, so the auto policy does not apply).
7. Login-as `marco`: `GET /api/connectors/merxa/link` must say `apply_ready: false` and
   approve must answer `412 connect_required`. Then **Connect Merxa** through the real Keycloak
   login form for `marco.rossi` (redirects followed, form action parsed, password from
   `DEMO_USER_PASSWORD`, never printed), check `apply_ready: true`, and approve with
   `change_set_hash` + `plan_version`.
8. Follow the commit run: `commit_tracker` goes `applying → applied → verified`, the case
   ends `committed` with no open interrupts.
9. Read back through Merxa REST as `merxa.admin`: the change set is `applied`, its
   attribution reads "Changed by Marco Rossi via Keelson (APR-…)", every item is applied,
   one transfer order per `transfer_order.create` item has
   `created_via == change_set:<code>`, and the audit rows carry the approval ref,
   `via = Keelson` and actor Marco Rossi.
10. (M3) The worker resyncs the canonical store after the apply without being asked (the engine's
   stale mark + Merxa's `change_set.applied` webhook, 30 s debounce): a newer snapshot becomes
   current whose reason is not an admin request (`--no-resync-check` skips this).

Exit status: `0` pass, `1` an expectation failed (the line starting `FAIL:` says which),
`2` missing environment. At the end it prints timings (seconds):

| key | meaning |
|---|---|
| `first_stage_s` | message posted → first `STEP_STARTED` |
| `exposure_s` | message posted → first `kpi_strip` (preliminary exposure) |
| `options_s` | message posted → `recommended_option` (includes the approach confirm) |
| `analysis_total_s` | message posted → analysis run finished |
| `apply_to_approval_card_s` | apply action → approval card |
| `commit_s` | approve (after connecting) → `commit_tracker` verified |
| `resync_after_reset_s` | Merxa reset → the requested scn sync is current (M3) |
| `resync_after_apply_s` | commit verified → the automatic post-apply resync is current (M3) |

### Run it

Prerequisites: the stack is up (`caddy egress keycloak kc-config pg-core pg-litellm
pg-merxa valkey litellm merxa-api optimizer bootstrap engine-api engine-worker`; the web
apps are not needed) and `GEMINI_API_KEY` is set in the host environment.

```bash
# Git Bash, repo root. The container joins keelson_edge so *.localhost resolve to Caddy.
env -u LITELLM_MASTER_KEY docker compose up -d --build caddy egress keycloak kc-config pg-core \
  pg-litellm pg-merxa valkey litellm merxa-api optimizer bootstrap engine-api engine-worker

DEMO_USER_PASSWORD="$(grep -E '^DEMO_USER_PASSWORD=' .env | cut -d= -f2-)" \
MSYS_NO_PATHCONV=1 docker run --rm --memory 256m --network keelson_edge \
  -e DEMO_USER_PASSWORD -e PYTHONPATH=/contracts/src \
  -v D:/primark/e2e:/e2e:ro -v D:/primark/packages/contracts-py:/contracts:ro \
  -v keelson_pipcache:/root/.cache/pip python:3.14-slim-trixie sh -c \
  "pip install -q --root-user-action=ignore httpx==0.28.1 pydantic==2.13.5 && python -u /e2e/api_thin_slice.py"
```

Options: `--no-reset-merxa` (keep the current Merxa data; the story needs the golden seed, so
use it only right after a reset), `--run-timeout SECONDS` (per run, default 420),
`--sync-timeout SECONDS` (per scn sync, default 240), `--no-resync-check`.

The script resets Merxa's demo data and disconnects marco's Merxa link, so do not run it
against a stack you are demoing from. Each run creates a new Keelson case; Keelson data is
never deleted.

M2 (`SEED_PROFILE=full`, 451 stores / 180 SKUs; 2026-10-06, two consecutive passes): BD +7 puts
16,914 units at risk (preliminary and final `kpi_strip`), opt-A moves 14,891 units on 4 transfer
lines; `options_s` 102.6 / 92.9, `analysis_total_s` 103.3 / 93.3, `commit_s` 1.9 / 1.9, total
112.6 / 98.2 s. The Merxa reset (merge reset) took ~4 s.

### LLM record/replay (cost and determinism)

The engine records every model call it makes and can answer identical calls from that recording
(`services/engine/src/keelson_engine/runtime/replay.py`, plan §16 "Demo determinism"). It works in
the engine, not in the LiteLLM proxy cache, because that cache fails open: on a miss it quietly
calls the provider. The engine env var `LLM_REPLAY_MODE` picks the mode:

| mode | hit | miss |
| --- | --- | --- |
| `off` (production default) | — | live |
| `record` | never looked up | live, (re)recorded |
| `record_or_replay` (this compose stack's default) | replayed | live, recorded |
| `replay` (fail closed) | replayed | the run fails with `ReplayMiss`; no live call |

- **Re-running a script is free.** With the compose default, the first pass of `api_thin_slice.py`
  (or of any other e2e script) records its calls. A second pass that asks the same questions is
  answered from the `llm_replay` table, with no Gemini spend and none of the 30-70 s planner,
  strategist or critic turns. Cases, runs and evidence get new ids on every pass. The request key
  normalises those ids, and the replayed answer cites the current run's ids.
- **Demo or CI without credits:** recreate the workers with `LLM_REPLAY_MODE=replay`. Any request
  that was never recorded then stops the run loudly instead of spending money:
  `LLM_REPLAY_MODE=replay env -u LITELLM_MASTER_KEY docker compose up -d engine-api engine-worker`.
  Only do this when you mean to: it recreates those two containers.
- **What makes a call miss:** a change to a prompt, an agent spec version, a tool declaration, an
  output schema, a generation parameter, the alias or its `models.lock` entry (provider model,
  `reasoning_effort`: editing models.lock re-records), or the data the model sees. Only opaque ids
  (run/case/evidence/block ids, UUIDs, tool-call ids, thought signatures) are normalised. Dates and
  date-times are data (an ETA, a receipt time), so a run with a new planning `as_of` or a moved ETA
  misses rather than replaying an answer about the old value. When a request repeats within one
  run, it maps to its own recording (1st, 2nd, …).
- **Accounting:** replayed responses report zero tokens, so LiteLLM spend and the run's token
  budget only see live calls; the run's `metadata.usage` counts them per agent as `replayed` (the
  e2e usage table has a `replay` column). Each call logs `llm.replay outcome=hit|miss|recorded|too_large|…`:
  `docker compose logs engine-worker | grep llm.replay`. The recorded usage is in the response's
  `custom_metadata.keelson_replay`.
- **Inspect or forget recordings** (tenant-scoped table, RLS like every other one):
  `docker compose exec pg-core psql -U postgres -d keelson -c "SELECT agent, alias, model, bytes, created_at FROM llm_replay ORDER BY created_at DESC LIMIT 20"`,
  and `… -c "DELETE FROM llm_replay WHERE tenant = 'demo-retail'"` to re-record from scratch.
  Records over `LLM_REPLAY_MAX_BYTES` (default 2 MiB, max 8 MiB) first lose their streamed
  partials. If still too large, they are not recorded.

The timing figures above were measured live. A replayed pass measures the engine and the optimizer
only, so do not compare it with live numbers.

### The story comes from the seed (no calibration)

Earlier versions lowered the UK1/NL1 DC stock by the store on-hand of their clusters before
the run, because the `small` seed sized only the DC stock to "just reach W44" while the
optimizer's impact model (`40-optimizer.md`: cluster on-hand + DC supply) also counts the
~1.5 weeks of store stock, so a 7-day delay showed 0 units at risk. The seed now nets the
clusters' store stock out of the short DCs' opening stock and `seed/story.py` projects with
store stock as cluster supply (`services/merxa-api/README.md`), so the script only calls the
scenario reset.

### M4 additions (`docs/contracts/90` §2–§6)

After the analysis `api_thin_slice.py` also checks the investigation: the approach card lists the
planner's checks (one plain sentence per task, no placeholders) and the agents it will consult;
at least **3 specialist agents** (`SUBAGENT_STARTED` of `inventory_demand` / `sourcing` /
`logistics` / `playbook`) ran, all of them on the card; the **critic** and the strategist ran.
If `refine_scenario` left inference-backed values unconfirmed (Apply disabled with "Confirm before
applying"), the planner sends the case action `confirm_assumptions` with those keys first: no
specialist re-runs, plan v+1, Apply enabled. Then options and apply as before (`opt-A`).

## `api_m4_whatif.py` — Act 2: the Suez what-if (`90` §8)

Same container and options as `api_thin_slice.py` (it imports its helpers):
`python -u /e2e/api_m4_whatif.py` (`--min-specialists N`, default 2; `--no-typed-edit`).

1. Reset (Merxa golden seed, scn resync, Marco's link disconnected).
2. Priya: "What if the Suez Canal closes for 3 weeks?" → the approach card has `mode: what_if`
   (the What-if badge) and the planner's checks → a typed **Edit** exactly as the web sends it
   (`{decision: "edit", edits: [{key, value_int}]}`: the card's first day / percent value + 7 days or
   + 10 %) re-frames the case and the new approach card must show the edited value → confirm →
   options; the case is `what_if`; the stock-out timeline shows a shortage.
3. The recommended option is marked `what_if`: **Apply disabled** with "What-if case: promote it to
   an actual case first.", **Promote** offered. An `apply` sent straight to the API is refused by the
   server (the run ends with "…promote it to an actual case…", no approval card, no change set).
4. Action `promote` → case `actual`, plan v+1 re-simulated on the current snapshot (no specialist
   re-runs), Promote gone, Apply enabled (unconfirmed values are confirmed first, as above).
5. Apply the recommended option (or the best option with changes) → approval (approver) → Marco
   connects Merxa and approves → commit tracker verified → Merxa read-back: the change set is
   `applied`, attributed "Changed by Marco Rossi via Keelson (APR-…)", every item applied, audit
   rows `via = Keelson`.

Timings: `first_stage_s`, `exposure_s`, `options_s`, `typed_edit_s`, `options_excl_edit_s`
(`options_s` minus the edit round trip), `analysis_total_s`, `promote_s`,
`apply_to_approval_card_s`, `commit_s`.

## `api_m4_followup.py` — follow-ups (`90` §6)

`python -u /e2e/api_m4_followup.py` (same container and options).

1. Reset, then Priya's trim-delay analysis (+ confirm of unconfirmed values) → plan vN.
2. **"What if the delay doubles?"** → RESIMULATE: no approach card, no specialist re-runs, plan
   vN+1, the final exposure KPIs differ, and the narrative carries `what_changed`
   ("Plan vN → vN+1: Delay: 7 days → 14 days; units at risk … → …; recommendation …").
3. **"Why not air freight?"** → EXPLAIN: an answer from the plan's options and scorecards that names
   the sea-air option(s) or the recommendation, **no** new plan version, no plan blocks, no
   specialists (the optimizer is not re-run; it would only simulate a lever no option used yet).

Timings: `options_s` (first analysis), `resimulate_s`, `explain_s`.

M4 results (2026-10-07, full seed, two consecutive passes each) are in the M4 report.

## M5 (`docs/contracts/95`): `api_m5_offline.py`, `api_m5_autocommit.py`, `api_m5_act3.py`

The three M5 scripts run **inside the engine image** with the engine's own environment, so besides
HTTP (through Caddy, like a browser) they can seed the few rows no model-free API creates, using
the engine's core functions. `docker compose run` starts a throwaway `engine-api` container on the
edge, app and AI networks and removes it afterwards:

```bash
# Git Bash, repo root; the whole stack is up
MSYS_NO_PATHCONV=1 env -u LITELLM_MASTER_KEY docker compose run --rm --no-deps -T   -v D:/primark/e2e:/e2e:ro engine-api python -u /e2e/api_m5_offline.py      # or api_m5_autocommit.py / api_m5_act3.py
```

| script | model calls | what it proves |
|---|---|---|
| `api_m5_offline.py` | **none** (no chat is ever posted) | Demo reset (< 30 s, step order, consents revoked; `keep_consents` keeps them). Policies admin: GET, the test bench (auto / four-eyes / webhook → human / what-if + unconfirmed → refuse / a draft "auto everything" still cannot auto-approve a webhook change), guarded PUTs (a tenant admin raising the auto cap or dropping the refuse rule → 202 four-eyes change request since M7, rejected again by ava; a platform admin may), version bump, 409 on a stale base. Guide read-back: a seeded checklist step (`core.checklists`; no model-free path creates one), the Merxa deep-link form renders, the transfer order is created through Merxa REST as `priya.shah`, Merxa's webhook wakes the worker's poll and the step is `verified` (evidence, `matched_ref`), a manual skip completes it. Re-plan routing: a seeded plan booking `CGP-UK1-SA`, Merxa's `lane_capacity` inject → `lane.capacity_changed` → one `signal` run on that case only (webhook principal, no roles, window + `weeks_detail` facts, no free text), `last_signal_at` on the case list. Quorum API with seeded four-eyes interrupts: a planner is refused, compliance first (1 of 2), the approver completes (resume: Marco executes, Lena co-approves), the initiator is excluded, a reject with a reason resolves, a missing executor link → 412 `executor_connect_required`. |
| `api_m5_autocommit.py` | **none** (the commit path is deterministic; scenarios and plans are seeded) | A planner's small transfer-only change set is applied without approval by `keelson-autocommit`; Merxa's change batch reads "Applied via Keelson auto-policy auto-small-transfer@p-2, initiated by Priya Shah" (`approved_by: policy:…`). Refusals: a what-if case, an unconfirmed (inferred) value — no approval card, nothing reaches Merxa. A `lane_capacity` signal on the committed plan's lane re-plans (banner "…plan updated (v2)") and applies nothing by policy. |
| `api_m5_act3.py` | **live** (needs credits; replayed on reruns) | The §8 story: reset → trim-delay chat → confirm → option (four-eyes levers preferred; mixed apply + guide when the option has guided lines) → Priya refused, Lena 1 of 2, Marco connects Merxa and approves → bulk apply ("…co-approved by Lena Vogel") → a guide step done through Merxa REST is verified by read-back → sea-air 30 % inject → v2 banner + delta change set → approved per its quorum → applied → Undo with approval → `rolled_back` → reset. |

Gated runs (`api_m5_offline.py`): every case that gets a run of its own also gets a seeded `running`
"gate" run with a 15-minute lease, so the worker (one running run per case, FIFO) never starts the
resume / signal runs the script inspects; the script then cancels them and the gates (also on
failure). The gate's payload is a rollback of an unknown change set, which is deterministic even if
the reaper ever re-queued it.

Measured 2026-10-07 on the full seed (no model calls): resets 1.3–24.2 s (the first reset after the
M3/M4 runs took 323 s once, deleting a backlog of old pinned `scn` snapshots, and the edge proxy
answered the browser 499 after 60 s while the server finished), guide read-back 2–6 s after the
Merxa save, signal run queued ~2 s after the inject, auto-commit apply → verified in ~2.5 s.

`scripts/verify-live.sh` runs every live check in order once credits are back (M4 thin slice,
what-if and follow-up twice each, all evals, the LLM Playwright specs, then `api_m5_act3.py` and
`api_m5_autocommit.py`, and a final reset), with `LLM_REPLAY_MODE=record_or_replay` so reruns are
free; `--only STEP,…` runs a subset.

## M6 (`docs/contracts/100`): `api_m6_offline.py`, `api_m6_act1.py`, Playwright `m6-alerts`

Both API scripts run **inside the engine image** like the M5 ones (same `docker compose run … engine-api
python -u /e2e/<script>` command), because they read the hub's change feed with the engine's service JWT,
look at run rows and frame the opened case in-process.

| script | model calls | what it proves |
|---|---|---|
| `api_m6_offline.py` | **none** | Demo reset: the `signal_hub` step follows the Merxa reset and clears the tenant's hub state; the post-reset scn sync pushes the tenant network (version bump; VNSGN, F-VN-01, SUP-VN-01, SGN-* lanes stored; `GET /v1/tenants/{t}/network`). Merxa webhooks → hub: two supplier messages are forwarded; the benign one waits as `pending_extraction` (no LLM key, never fabricated), the injection attempt is `flagged` by the pattern screen; a 50 % `lane_capacity` inject becomes a `fact` signal and a tenant alert (entity match on the lane and its origin port, `medium`, not simulated), which priya dismisses. Typhoon pack (presenter `ava` via `POST /api/demo/packs/typhoon-hcmc/start`): the alert has exactly the expected exposure (`data/scenarios/typhoon-hcmc/expected.yaml`), a deterministic `why`, GDACS attribution (CC BY), the DEMO flag, Home count +1 and the live `sc.alert.raised` SSE event. Open case with the worker **held**: one case + one queued `signal` run `{event_id, alert_id}` (3 concurrent repeats → the same case), the alert reads `opened`, the run is cancelled before any worker could claim it; the frame that run would build is computed in-process from the live hub detail (`NAT.storm`, exposed nodes as facts, no alert text). Hysteresis: advisories 2–4 edit the same alert, Red re-notifies once (`critical`, `alert_and_draft_case`) and no second case or run appears; the Demo Freight Daily item attaches to the event. Resolve: +14 sim days → event and alert resolved. A final demo reset leaves no hub alert, no `alert_case` row and an idle pack. |
| `api_m6_act1.py` | **live** (the signal run's planner; replayed on reruns) | Act 1: reset + network push → typhoon advisory 1 → alert → priya opens the case → the signal run frames the event, writes the approach and **pauses at the approach card** (`source: alert`, `alert_origin` card, no alert title in the checks); `--confirm` also follows the investigation to options (`external_intel` consulted); advisory 3 (Red) re-notifies on the same case. `scripts/verify-live.sh` step `m6-act1` runs it twice. |

**Worker hold (`m6_hold.py`).** `open-case` creates the case and queues its run in one transaction, so
the M5 gate (a `running` run seeded on the case) cannot be put in front of it. The worker runs one run
at a time, so the hold occupies that slot first: it takes an `ACCESS EXCLUSIVE` lock on the ADK
`sessions` table (database `adk`; only the worker reads it, at the start of a run, before any node or
model call) and queues a deterministic gate run (rollback of an unknown change set) that the worker
claims and blocks on. Every run queued meanwhile stays queued; the scripts cancel it at once, and
releasing the hold cancels anything still queued since it began, aborts the gate (cancel watch) and
drops the lock. The gate ends `cancelled` with zero model calls (`run.usage calls=0`). Never run a full
demo reset while a hold is active (it deletes cases, the gate's too).

**Playwright** (`e2e/playwright/run-m6.sh`, no model): demo reset → wait for the network push → start
the hold (`keelson_m6_hold`, a throwaway `engine-api` container) → `tests/m6-alerts.spec.ts` with
`E2E_M6_WORKER_HELD=1` → release → demo reset. The spec: presenter `ava` resets and starts the typhoon
pack from the persona menu (Demo scenarios dialog, axe clean); planner `priya` sees the Home pill
"1 new alert" (screenshot `29-home-alert-pill`), the inbox row with severity, why, exposure summary and
DEMO badge (`27-alerts-inbox`), the drawer with the engine's map (footprint + exposed sites at the
tenant's own positions), exposed sites and lanes, GDACS source with licence and the update timeline
(`28-alert-drawer`), then **Open case** → the case page with its queued run shown as "Getting started…"
(`30-case-from-alert`); the spec checks the alert reads opened, a repeat open-case answers the same case,
and cancels the queued run. Every page is axe clean (serious/critical).

Measured 2026-10-08 (no model calls): `api_m6_offline.py` passed twice in a row (~100 s each, of which
~70 s is the post-reset scn sync before the network push); demo resets 1.0–2.1 s; `m6-alerts` ~25–40 s.

## M7 (`docs/contracts/110`): `api_m7_offline.py`, `api_m7_foreign_erp.py`, `m7_hub_key.py`, Playwright `m7-offline`

The API scripts run **inside the engine image** like the M5/M6 ones (`docker compose run --rm --no-deps
-T -e LOG_LEVEL=warning -v D:/primark/e2e:/e2e:ro engine-api python -u /e2e/<script>`): they read Merxa
`/mcp-lite` through the engine's own MCP gateway (the registered connector's key) and drive the per-op
commit harness in-process.

| script | model calls | what it proves |
|---|---|---|
| `api_m7_offline.py` | **none** | Demo reset (every step ok, `signal_hub` included). **Generic Tier-A proof on northwind-test** (ava, cross-tenant): a Merxa API key is minted (read scopes + `changes:apply`; older e2e keys revoked), `merxa-lite` is registered with a static `X-Merxa-Api-Key` header (never echoed) — or, when the agent spec already plans on it (after a live run), its key is **rotated in place** (`PUT …/secret`) — tested → tier `[A]`, `list_items`/`get_item` approved as trusted reads, `update_item`/`create_transfer_order` as `best_effort` writes (never agent-bound), activated; pack `foreign-erp-lite` mappings installed (`from-pack`; a second install is unchanged; a stale-base PUT refused), read test (one page per source); **crawl sync** in the worker → snapshot `consistency: crawled` with an `as_of_range`, not truncated, row counts **equal** to Merxa's own `list_items` totals (locations = DCs + stores + distinct clusters). Action **dry-run**: `allocation.set_qty` renders `update_item(type, key, fields_json, expected_version)` and runs the `get_item` read-back (no match), Merxa unchanged; `transfer_order.create` renders `create_transfer_order` with `request_id`; `po_line.reschedule` → guided reason. **Four-eyes** (demo-retail): sam raises the auto cap → 202 request (nothing applied, in the queue), sam's own approve → 403, priya → 403, ava approves → applied (new spec version); a second request whose base moves (sam edits the spec meanwhile) → 409 `stale_base`, stays pending, a reject needs a reason, ava rejects; sam lowers the cap again. **Per-op commit harness** (test-only, in-process — northwind-test has no approver persona and a plan needs a model): nora's case + a seeded plan option with `transfer_order.create` + `allocation.set_qty` (mapped) and `po_line.reschedule` (unmapped); the capability table routes the unmapped op to a guided checklist (generated instructions, visible to nora); the mapped lines compile into a per-op change set whose dry run renders one call per line (`calls_hash`, request ids, nothing sent); the seeded approval applies them through the commit ledger as the apply principal, reconcile reads them back → `verified` (ledger rows `best_effort`); Merxa holds the new transfer order (`created_via mcp-lite`) and the allocation's new qty/version; a second apply sends nothing. **Users** (real Keycloak): sam invites a user (no password anywhere in the answer; `UPDATE_PASSWORD`, tenant attribute), sets planner+approver, a `tenant-admin` grant → 202 `user_roles` (ava rejects), `platform-admin` → refused, disable; the user is deleted from Keycloak afterwards (also on failure). **Signals admin**: toggle a source subscription and restore it (pushed to the hub), bad cadence 422, a 150 km radius watch region around a pushed port (stored, pushed, read back, removed), alert rules re-saved and pushed. Clean-up: merxa-lite disabled; a final demo reset unless `--skip-final-reset` (`--skip-crawl` skips the ~4 min crawl). |
| `api_m7_foreign_erp.py` | **live** (planner, specialists, strategist; replayed on reruns) | The trim-delay story on the **foreign ERP**: demo reset; merxa-lite set up as above + crawl; ava points northwind-test's planning connector, answer path and inventory specialist at merxa-lite; nora gets `approver` (Users API, restored at the end). nora reports the delay → approach confirmed → options **advisory** (`advisory.reason = crawled_snapshot` on `recommended_option` / `scenario_compare` / `narrative`, the narrative says why) → apply → per-op change set (one call per line) → approval (nora, or a policy auto-approval) → `commit_tracker` verified, unmapped lines as a guided checklist → Merxa read-back of every applied line + verified ledgers. `--prepare-only` runs the model-free set-up (steps 1–3) offline. `scripts/verify-live.sh` step `m7-foreign-erp` runs it twice. |
| `m7_hub_key.py` | **live** (news extraction) | The Signal Hub's bootstrap-provisioned LiteLLM key: `GET /v1/sources` reports `llm_configured` / `llm_key_source: db`; a benign Merxa supplier message is **extracted**, the injection attempt still flagged. `verify-live.sh` step `m7-hub-key`. |

**Playwright** (`e2e/playwright/run.sh m7-offline`, no model): Pending changes (sam's guarded request
waits with its before/after diff and no Approve button for the requester; ava sees the nav badge
"1 waiting", approves with a note → applied; `31-pending-changes`), Users (tenant accounts, a
platform-admin's account read-only for sam, invite and role dialogs; `32-users`), the custom
specialist builder (tools picker with write tools disabled, quarantined untrusted tools; the
playground's **disabled state** when the engine only replays recorded answers — the availability
answer is stubbed to `replay_only` for this, the real endpoint is checked for shape; saved →
`33-agent-builder`) and the read-only workflow graph with the new specialist fanning out from
Investigate (`34-workflow-graph`), the tier-A mapping editors + action dry-run on northwind-test's
merxa-lite (nora temporarily `tenant-admin` through the Users API — the web never acts cross-tenant;
capabilities with the advisory note, *Test with one page*, a rendered `create_transfer_order` call
with the read-back run, an unmapped op → guided; `35-mapping-editor`), Signals admin (sources with
licences, a radius watch region added through the form and stored, alert rules; `36-signals-admin`),
and the Advisory badge on the `*.advisory` block fixtures. Every page is axe clean (serious/critical).
The spec restores the demo-retail spec, the watch regions, nora's roles, rejects leftover requests
and disables merxa-lite again.

Measured 2026-10-08 (no model calls): `api_m7_offline.py` passed twice in a row (~6.5 min each; the
crawl is 270–325 s for 298,467 rows over 1,500 `list_items` pages, counts equal to Merxa's); the
second run took the key-rotation path (northwind-test plans on merxa-lite after
`api_m7_foreign_erp.py --prepare-only`). `m7-offline` (6 tests) ~1.5 min; with `dev-blocks`,
`admin-smoke`, `auth`, `m5-offline` under `E2E_NO_MODEL=1`: 15 passed, 1 skipped.

## M8 signal clustering (`docs/contracts/123`): `api_m8_clustering_offline.py`

Runs **inside the engine image** like the M6/M7 offline scripts (`docker compose run --rm --no-deps -T
-e LOG_LEVEL=warning -v <repo>/e2e:/e2e:ro engine-api python -u /e2e/api_m8_clustering_offline.py`).

| script | model calls | what it proves |
|---|---|---|
| `api_m8_clustering_offline.py` | **none** | Demo reset (every step ok, `signal_hub` included). Clustering settings: ava (platform admin) reads them `editable`, sam (tenant admin) read-only; version >= 1, local profile `local-terms-v1`, pgvector available on pg-core. Presenter ava replays `port-strike-syndication` to the end; priya (planner: the alert routes do not accept a platform-admin-only user) sees exactly one simulated alert (the Hamburg strike, DEHAM exposed) whose detail carries memberships `{founded, near_duplicate, attach_vector}` — the near-duplicate within 6 SimHash bits names the first report, the vector member's similarity reaches the local strict threshold — and the event is `confirmed` with the confidence of **two** independent sources (5600 bp: the syndicated copy is not counted); no explanation repeats a headline. sam's settings PUT → 403 `platform_admin_required`; ava's PUT on a stale `base_version` → 409 `stale_version`, version unchanged. The 7-day counts show the near-duplicate and the vector attach. A final demo reset (unless `--skip-final-reset`) leaves no simulated alert and the strike event gone from the hub. |

## M8 Evals UI (`docs/contracts/128`): `api_m8_evals_offline.py`, `api_m8_evals_live.py`

Both run inside the engine image like the M5–M7 offline scripts (only httpx and the helpers of
`api_thin_slice.py` are used), with `EVALS_RUNNER_TOKEN` set and the in-app runner up. The offline
script needs a stack started with `LLM_REPLAY_MODE=replay` (fail-closed: no provider call is possible):

```bash
# Git Bash, repo root; the whole stack is up with LLM_REPLAY_MODE=replay
docker compose --profile evals up -d evals-runner
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T -e LOG_LEVEL=warning   -v D:/keelson-supply-chain-recovery/e2e:/e2e:ro   -v D:/keelson-supply-chain-recovery/evals/tests/fixtures:/fixtures:ro   engine-api python -u /e2e/api_m8_evals_offline.py
```

| script | model calls | what it proves |
|---|---|---|
| `api_m8_evals_offline.py` | **none** | As ava: `/api/admin/evals/status` shows the runner online with its suite (≥ 7 cases), process mode `replay`, both live modes unavailable (`replay_only`). A **replay run** of `store_shortage_question` is queued, claimed by the runner and followed through the events endpoint until `succeeded`; the engine counters show `llm_calls_live == 0` and `tokens_live == 0`, the tenant's gateway usage did not grow, and the case is `pass` or `error` with `error_code replay_miss` (with no recordings it fails closed). A live request → `409 evals_unavailable` (`replay_only`). The two committed CLI result files import as runs (the same file again → `duplicate: true`); compare(A, B): `adversarial_kb_doc` fixed, `adversarial_supplier_message` changed, four cases removed; trends list all three runs; the in-app run's export re-imports as a duplicate of it; sam → 403 `platform_admin_required`. |
| `api_m8_evals_live.py` | **live** (one case) | `scripts/verify-live.sh --only m8-evals` (needs `EVALS_LIVE_ENABLED=1` and credits): a `record_or_replay` run of `trim_delay_actual` with a 1.5M-token / $2 budget and the typed `RUN LIVE`; it makes live calls, ends `succeeded`, stays within its budget (plus at most the one response that crossed the token cap) and the case is evaluated (no replay miss). |

The runner resets Merxa's demo data for every case: never run it against a stack you are presenting
from.
## M8 signal feeds (`docs/contracts/124`): `api_m8_feeds_offline.py`

Runs inside the engine image like the M6/M7 offline scripts (`docker compose run --rm --no-deps -T -v
<repo>/e2e:/e2e:ro engine-api python -u /e2e/api_m8_feeds_offline.py`); **no model and no internet**.
Demo reset; the Sources catalogue lists NHC, Copernicus EMS and OFAC as polled `fact` sources with their
licences and https terms links (the OFAC awareness disclaimer included) and GDELT as an opt-in
`inference` source, unsubscribed and paused; subscribing GDELT un-pauses it at the hub (900 s) and
unsubscribing pauses it again; the presenter's `hurricane-cape-route` pack raises exactly one simulated
alert exposing only the four CAPE-routed lanes (`lane_crosses`, attributed to "NOAA National Hurricane
Center (demo replay)"), re-notified as critical at the category-3 advisory with the action still `alert`
(no case drafted); a final demo reset clears it (`--skip-final-reset` keeps it). `scripts/verify-live.sh`
step `m8-feeds` polls the real NHC / EMS / OFAC / GDELT feeds once through the poller's egress.

## `api_m3_levers.py` — M3 levers through the API

Same container, command shape and options as `api_thin_slice.py` (it imports that script's
helpers): `python -u /e2e/api_m3_levers.py`. Story: reset + resync + disconnect Marco → Priya's
trim-delay analysis with all the M3 checks above → apply **`opt-B` (rebalance + targeted
sea-air)** when it buys sea-air capacity (else the recommended option if it includes
`expedite_mode_shift`, else a near-shore option C/D) → human approval (`approver`; expedite /
near-shore ops never auto-approve) → Marco connects Merxa and approves → commit tracker verified →
Merxa read-back as `merxa.admin`: ONE applied change set attributed "Changed by Marco Rossi via
Keelson (APR-…)"; sea-air = `po_line.split` items with one confirmed capacity booking per split
(`created_via == change_set:<code>`; Merxa's split books the lane capacity itself, so the optimizer
emits no separate `capacity_booking.create`, see `services/optimizer/README.md`), booked units ≥
split units, the original PO line kept and a new line for the same SKU; near-shore = `po.create` +
`po_line.change_qty` / `po_line.cancel` in the same set and the created POs readable; every audit
row `via = Keelson` (capacity_booking rows present for sea-air) → the automatic post-apply resync.

## `api_m2_admin.py` — M2 back-office and multi-tenancy through the API

Covers `docs/contracts/70-backoffice-m2.md` §1–5 and §7 on the running stack (real Gemini on the
answer path). Run it after `api_thin_slice.py` (the webhook check needs an applied change set).

1. **sam** (tenant-admin, demo-retail): reads the catalog, `PUT /api/admin/agents/spec` with a
   recommender prompt addendum and a different structurer alias → new version; a stale
   `base_version` → 409, an unknown alias → 4xx, raising `policy.auto_max_units` → 202
   four-eyes change request (M7; ava rejects it); the version is listed; finally the starting version is **restored** (as a new
   version), so the demo spec is unchanged.
2. **ava** (platform-admin): `POST /api/admin/tenants` creates `acme-outdoor` from pack `blank`
   (idempotent: 201, then 200; other pack → 409; no demo persona is added to it, because there is
   no active-tenant switch yet and a second tenant on a persona would change its active tenant).
   The tenant has a LiteLLM team + virtual key (`GET /api/admin/gateway/tenant?tenant=acme-outdoor`,
   platform-admin cross-tenant) and spec v1 with no answer tools. sam gets 403 on `/tenants`,
   `/packs` and 404 on a foreign `?tenant=`.
3. Merxa REST as `merxa.admin`: mint an API key (read scopes, shown once). ava registers
   `http://merxa.localhost/mcp-lite` for **northwind-test** (`static_header`, `X-Merxa-Api-Key`):
   activate before a test → 4xx; test → `tier == ["A"]`, write tools suggested write; approves only
   the read tools (`list_items`, `get_item`), activates, binding a write tool to the answer path →
   4xx, binding the read tools → new spec version. A previous run's connector is unbound and
   deleted first (fresh key each run).
4. **nora** (northwind-test) asks "How many open purchase orders are there?": the answer states
   the count Merxa REST reports, the `evidence_pack` cites `list items (… type=purchase_order)`
   from the lite connector (the answer path has no timeline subagent, so its tool calls show up as
   evidence, not `TOOL_CALL_*` events), and no write tool appears anywhere.
5. Tenancy: nora → 404 on a demo-retail case, its event stream, a block's data, posting into it and
   the `merxa` connector link; priya → 404 on nora's case; priya and nora → 403 on `/api/admin/*`.
6. Webhooks: the latest applied Merxa change set appears as `change_set.applied` in
   `GET /api/admin/connectors/merxa/events` (sam); nora → 403; a forged signature → 401.

```bash
DEMO_USER_PASSWORD="$(grep -E '^DEMO_USER_PASSWORD=' .env | cut -d= -f2-)" \
MSYS_NO_PATHCONV=1 docker run --rm --memory 256m --network keelson_edge \
  -e DEMO_USER_PASSWORD -v D:/primark/e2e:/e2e:ro -v keelson_pipcache:/root/.cache/pip \
  python:3.14-slim-trixie sh -c \
  "pip install -q --root-user-action=ignore httpx==0.28.1 && python -u /e2e/api_m2_admin.py"
```

Options: `--run-timeout`, `--webhook-wait SECONDS` (default 60), `--skip-webhook`. It leaves
`acme-outdoor`, the `merxa-lite` connector on northwind-test (bound to its answer path) and a few
spec versions behind; re-runs are idempotent. Takes ~20 s.
