# M6 runtime: signal runs from Signal Hub events, `external_intel`

Contract: `docs/contracts/100-m6-signals.md` §5, §6. This file covers the runtime (E2) side.
The platform side (alerts API, open-case, the hub client `core.signal_hub`, network push) is
documented in the engine README.

## Workflow additions

```
pre_route:  signal {signal_id}            → prepare_replan    (M5 re-plan, unchanged)
            signal {event_id, alert_id}   → frame_case {signal: {event_id, alert_id}}   (route SIGNAL_EVENT)
frame_case {signal: …} → quick_exposure → planner → approach_gate → approach_router → … (the M4 path)
approach_router EDIT on a signal case → frame_case {signal: …, edits}   (re-framed from the event)
```

The two signal payload shapes are told apart by the typed envelope only (`SignalPayload`). No new
workflow node: `frame_case` dispatches on the `signal` key, so `NODE_STAGES` is unchanged. A crash
retry of an M6 run is recognised by its `event_id` (`execute._original_kind`).

## Framing (`nodes/signal_frame.py`, `runtime/signal_hub.py`)

* `load_event_frame(run, event_id, alert_id)` reads through `RuntimeDeps.hub`
  (`deps.SignalHubPort`: `get_alert` → `AlertDetail`, `get_event` → `{event, signals?, exposures?}`)
  and validates against `keelson_contracts.signals` (another event / alert, or members and
  exposures of another event, are refused or dropped; an alert or tenant event of another tenant
  is refused and other tenants' signals / exposures are dropped). The core adapter uses `core.signal_hub.get_alert`
  / `get_event`; exposures missing from an event read come from the hub MCP `assess_exposure`.
  A hub failure → an assistant message ("I could not read this alert …") and the run fails.
* `frame_from_event` (pure) → `DisruptionScenario`, `mode: actual`:
  * taxonomy key = `taxonomy_key_for(event.subtype)` (TC/storm → `NAT.storm`, earthquake →
    `NAT.earthquake`, flood → `NAT.flood`, port_closure → `LOG.port_closure`, strike → `LOG.strike`,
    supplier_delay → `SUP.delay`, else `OTHER`);
  * exposed nodes (`Exposure.node_type/node_code`) → `matched` (labels from the tenant's own data),
    template targets from them (`compile_template`); a `resolve: receipts` template walks the exposed
    suppliers / factories to open PO lines;
  * window (`fact_window`): start = the event's start (not before the planning date); duration =
    the event window for non-hazard events with an end, else the taxonomy prior (`assumption`). The
    hub's event window spans every member, so for a grounded event with news members the window
    is taken from the deterministic members only (a news item never moves a `fact` start);
  * provenance: `EventFrame.grounded` (an anchor that is not text-derived, or only deterministic
    members) → event values are `fact`; otherwise every event-derived assumption is `inference`,
    `unconfirmed=True`, citing the `signal_hub.news` row, and perturbations are `inference`.
    Text-derived members of a grounded event add `news_reports` (inference, unconfirmed). A signal
    is text-derived when `provenance == inference`, `origin == news` or `extracted_by` is set (demo
    packs' news items are `simulated` with `extracted_by: prebaked`).
  * assumptions `signal_event` ("Opened from alert"), `exposed_nodes` and the description
    ("Opened from alert: Storm (tropical cyclone), critical severity, exposing port VNSGN, …") are
    built from enums, taxonomy labels, codes and cleaned source names only: no title, summary, raw
    text or alert title reaches a scenario, a brief or a route. A news subtype outside the
    deterministic map is shown as "other event".
* Evidence: `signal_hub.event` (structured fields only; `fact`, or `inference` +
  `untrusted_extraction` for a news-only event; `args.event_id` lets later runs find the event; a
  grounded event's row lists only its deterministic member signals — `news_signal_count` counts the
  rest — and never a text-derived item's link) and
  `signal_hub.news` (`inference`, `untrusted_extraction`) — citing it taints the case.
* `alert_origin@1` (stable id per alert): alert id, the alert's title (rendered as text by the web;
  the runtime never reads blocks back), severity, kind, source names, DEMO flag, `opened_at`.

## Approach card and policy

* `framing.is_signal_scenario(scenario)` (assumption key `signal_event`): the card's
  `event.source` is `alert` and it is **never auto-confirmed**.
* `commit_nodes.case_origin`: a person's Apply on a case framed from an alert is policy origin
  `signal` (signal runs themselves are `webhook`): rule `human-untrusted`, never `auto`.
* `investigate.user_request` is `None` for signal runs (nobody typed anything).

## `external_intel` specialist

* Default spec: `SpecialistSpec(slug="external_intel", tools=(), quarantined=("signal_hub",),
  connector="signal-hub")`, display name "External intelligence"; `default_plan` adds it for `NAT`
  events and for every alert case.
* Quarantine source `signal_hub` → four wrappers (`SignalHubTool`): `search_signals_structured`,
  `get_event_structured`, `hazards_near_node_structured`, `assess_exposure_structured` around the
  hub MCP tools (approved read tools on the connector). Arguments are validated (UUIDs, kinds,
  `type:code` nodes, ISO dates) and shaped to the declared schema; `get_event` / `assess_exposure`
  default to the case's own event. `project_hub_result` returns validated structured `records`
  (ids, enums, ints, timestamps, codes, cleaned source names; no geometry, URLs or free text) and
  sends every text field (title, summary, raw_text, …) through raw `untrusted_text` rows → pattern
  screen + guard → extractor, like supplier messages. All four hub tools are in
  `toolsets.UNTRUSTED_SOURCE_TOOLS` (never bound directly).
* Activity labels: "Reading hazard alerts from Signal Hub", "Reading the hazard alert's event from
  Signal Hub", "Reading hazard alerts near VNSGN from Signal Hub", "Checking which sites the
  hazard exposes in Signal Hub"; the vendor falls back to "Signal Hub" when the connector row is
  unreadable. Auth kind `service_jwt` is a header auth kind (`core.connectors.auth_headers`).
* Egress: the runtime MCP transport (`mcp_client.proxied_transport_factory(direct_origins=…)`)
  dials the configured `SIGNAL_HUB_URL` origin directly (exact scheme/host/port match; no
  redirects, no env proxies). Squid denies single-label internal hosts and port 8300 by design, so
  the hub is unreachable through it; every other connector still goes through the egress proxy.

## Taxonomy

`data/taxonomy/events.yaml` version 2 adds `NAT.earthquake` (capacity_scale on ports / factories,
prior p10/p50/p90 0/2500/7500 bp, duration 3/10/30 days).
