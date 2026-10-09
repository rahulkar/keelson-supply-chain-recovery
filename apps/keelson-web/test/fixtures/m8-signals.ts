/**
 * M8 signal clustering fixtures (`docs/contracts/123`): an alert detail with memberships — the story
 * (`founded`), a syndicated near-duplicate (`near_duplicate`, 3 bits) and a report naming an unknown
 * terminal (`attach_vector`, similarity 7977 bp, an HTML-looking headline) — and the engine's
 * clustering status answers. Shapes validated against the generated schemas in `test/m8-signals.test.tsx`.
 */
import type { SignalsAlertDetail, SignalsClusteringConfig, SignalsEventMembership, SignalsSignal } from "@keelson/contracts";
import { alert } from "./m6";

export const STRIKE_ALERT_ID = "4c3b2a19-8f7e-5d6c-9b5a-4f3e2d1c0b9a";
export const STRIKE_EVENT_ID = "5d4c3b2a-1908-5e7d-8c6b-5a4f3e2d1c0b";
export const ORIGINAL_ID = "6e5d4c3b-2a19-5f8e-9d7c-6b5a4f3e2d1c";
export const COPY_ID = "7f6e5d4c-3b2a-5091-8e8d-7c6b5a4f3e2d";
export const TERMINAL_ID = "80706f5e-4d3c-5b2a-9f9e-8d7c6b5a4f3e";
export const HTML_TITLE = "<b>Hamburg-Waltershof</b> terminal workers join <img src=x onerror=alert(1)>";

const NOW = "2026-10-06T14:00:00Z";

function signal(id: string, over: Partial<SignalsSignal> & { source_name: string; connector: string }): SignalsSignal {
  const { source_name, connector, ...rest } = over;
  return {
    id,
    tenant: "demo-retail",
    origin: "demo",
    kind: "labor",
    subtype: "strike",
    title: "Dockworkers strike halts container handling at the Port of Hamburg",
    summary: null,
    raw_text: null,
    geometry: { type: "Point", coordinates: [9.9663, 53.5395] },
    location_refs: ["UNLOCODE:DEHAM", "ISO3166:DE"],
    starts_at: "2026-10-06T08:00:00Z",
    ends_at: null,
    observed_at: "2026-10-06T08:00:00Z",
    expires_at: null,
    severity_bp: 5500,
    severity_native: { scheme: "extraction_prior" },
    confidence_bp: 4200,
    provenance: "simulated",
    extracted_by: "prebaked",
    source: {
      connector_id: connector,
      name: source_name,
      url: null,
      license: "Headline and link only",
      authority_bp: 7000,
      retrieved_at: "2026-10-06T08:00:00Z",
      native_id: id,
    },
    event_id: STRIKE_EVENT_ID,
    version: 1,
    supersedes: null,
    ...rest,
  };
}

function membership(signalId: string, over: Partial<SignalsEventMembership>): SignalsEventMembership {
  return {
    signal_id: signalId,
    event_id: STRIKE_EVENT_ID,
    method: "founded",
    prior_method: null,
    from_event_id: null,
    distance_km: null,
    shared_refs: [],
    similarity_bp: null,
    similarity_profile: null,
    hamming_bits: null,
    duplicate_of_signal_id: null,
    config_version: 1,
    explanation: "First report of this event: no open event matched it when it arrived.",
    decided_at: "2026-10-06T08:00:00Z",
    ...over,
  };
}

export const MEMBERSHIPS: SignalsEventMembership[] = [
  membership(TERMINAL_ID, {
    method: "attach_vector",
    similarity_bp: 7977,
    similarity_profile: "local-terms-v1",
    explanation:
      "Describes a similar disruption (similarity 80 %, local term profile), with no mapped location in common; reported within 72 h of the event; no shared place code.",
    decided_at: "2026-10-06T14:00:00Z",
  }),
  membership(COPY_ID, {
    method: "near_duplicate",
    hamming_bits: 3,
    duplicate_of_signal_id: ORIGINAL_ID,
    explanation: "Near-duplicate of another report in this event (3 of 64 fingerprint bits differ): counted once for corroboration.",
    decided_at: "2026-10-06T10:00:00Z",
  }),
  membership(ORIGINAL_ID, {}),
];

export function strikeDetail(over: { memberships?: unknown[] | null } = {}): SignalsAlertDetail & Record<string, unknown> {
  const { kind: _kind, ...a } = alert({
    id: STRIKE_ALERT_ID,
    event_id: STRIKE_EVENT_ID,
    severity: "medium",
    title: "Dockworkers strike halts container handling at the Port of Hamburg",
    why: "Severity 5,500 bp (medium) and 1 port is inside the affected area.",
    exposure_summary: { nodes_by_type: { supplier: 0, factory: 0, port: 1, dc: 0, store: 0, lane: 4 }, open_po_lines: null, units_inbound: null },
    first_raised_at: "2026-10-06T08:00:00Z",
    updated_at: NOW,
  });
  const detail: SignalsAlertDetail & Record<string, unknown> = {
    alert: a,
    event: {
      id: STRIKE_EVENT_ID,
      tenant: "demo-retail",
      kind: "labor",
      subtype: "strike",
      title: "Dockworkers strike halts container handling at the Port of Hamburg",
      summary: "3 reports from 3 sources (demo.rss.demo-freight-daily, demo.rss.demo-port-bulletin, demo.rss.simulated-shipping-wire); 2 independent",
      anchor_signal_id: null,
      member_signal_ids: [TERMINAL_ID, COPY_ID, ORIGINAL_ID],
      geometry: { type: "Point", coordinates: [9.9663, 53.5395] },
      window: { starts_at: "2026-10-06T08:00:00Z", ends_at: null },
      severity_bp: 5500,
      confidence_bp: 5600,
      status: "confirmed",
      taxonomy_key: "LOG.strike",
      updated_at: NOW,
    },
    signals: [
      signal(TERMINAL_ID, {
        title: HTML_TITLE,
        geometry: null,
        location_refs: [],
        observed_at: NOW,
        starts_at: NOW,
        source_name: "Demo Port Bulletin (demo replay)",
        connector: "demo.rss.demo-port-bulletin",
      }),
      signal(COPY_ID, {
        title: "Dockworkers strike stops container handling at the Port of Hamburg",
        observed_at: "2026-10-06T10:00:00Z",
        starts_at: "2026-10-06T10:00:00Z",
        source_name: "Simulated Shipping Wire (demo replay)",
        connector: "demo.rss.simulated-shipping-wire",
      }),
      signal(ORIGINAL_ID, { source_name: "Demo Freight Daily (demo replay)", connector: "demo.rss.demo-freight-daily" }),
    ],
    exposures: [],
  };
  if (over.memberships !== null) detail.memberships = (over.memberships ?? MEMBERSHIPS) as SignalsEventMembership[];
  return detail;
}

export function clusteringConfig(over: Partial<SignalsClusteringConfig> = {}): SignalsClusteringConfig {
  return {
    version: 3,
    simhash: { enabled: true, max_hamming_bits: 6, window_hours: 72, reuse_extraction: true },
    vector: {
      enabled: true,
      window_hours: 72,
      max_distance_km: 1000,
      local: { attach_bp: 7000, merge_bp: 7500, strict_bp: 7500 },
      remote: { attach_bp: 8500, merge_bp: 9000, strict_bp: 9300 },
    },
    updated_at: "2026-10-06T07:00:00Z",
    updated_by: "ava",
    ...over,
  };
}

export function clusteringStatus(editable: boolean, over: Record<string, unknown> = {}) {
  return {
    config: clusteringConfig(),
    vector_backend: { pgvector: true, local_profile: "local-terms-v1", remote: null, remote_reason: "provider_local" },
    window_days: 7,
    counts: {
      simhash_near_duplicates: 1,
      same_source_duplicates: 0,
      extractions_reused: 0,
      anchor: 2,
      same_hazard: 0,
      founded: 3,
      attach_geo: 1,
      attach_ref: 4,
      attach_vector: 1,
      near_duplicate: 1,
      absorbed: 1,
      merged_vector: 0,
      embeddings_pending: 0,
      embeddings_failed: 0,
    },
    editable,
    ...over,
  };
}
