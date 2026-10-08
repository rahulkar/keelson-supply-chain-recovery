/**
 * M6 alert fixtures shaped like the engine's Alerts API (`docs/contracts/100` §2/§6): contract
 * `Alert` / `AlertDetail` values (validated in `test/m6.test.tsx`) for a simulated typhoon pack
 * alert, a live feed alert and a news-only alert.
 */
import type { SignalKind, SignalsAlert, SignalsAlertDetail } from "@keelson/contracts";

export const TYPHOON_ALERT_ID = "3f8e2a64-5b1c-5d7e-9a0b-1c2d3e4f5a6b";
export const QUAKE_ALERT_ID = "9b0c1d2e-3f4a-5b6c-8d7e-0f1a2b3c4d5e";
export const NEWS_ALERT_ID = "0a1b2c3d-4e5f-5a6b-9c8d-7e6f5a4b3c2d";
export const TYPHOON_EVENT_ID = "6c5d4e3f-2a1b-5c0d-8e9f-a0b1c2d3e4f5";

const NOW = "2026-10-08T06:00:00Z";

export function alert(over: Partial<SignalsAlert> & { kind?: SignalKind } = {}): SignalsAlert & { kind?: SignalKind } {
  return {
    id: TYPHOON_ALERT_ID,
    tenant: "demo-retail",
    event_id: TYPHOON_EVENT_ID,
    status: "new",
    severity: "critical",
    title: "Tropical cyclone approaching Ho Chi Minh City",
    why: "Severity 8,200 bp (critical) and 3 of your sites are inside the forecast cone.",
    exposure_summary: { nodes_by_type: { supplier: 1, factory: 1, port: 1, dc: 0, store: 0, lane: 4 }, open_po_lines: 2, units_inbound: 18400 },
    first_raised_at: "2026-10-08T00:00:00Z",
    updated_at: NOW,
    case_id: null,
    simulated: true,
    kind: "natural_hazard",
    ...over,
  };
}

export const quakeAlert = alert({
  id: QUAKE_ALERT_ID,
  event_id: "1d2c3b4a-5f6e-5d7c-8b9a-0f1e2d3c4b5a",
  severity: "high",
  status: "seen",
  title: "M 6.8 earthquake near the coast of central Chile",
  why: "Severity 6,100 bp (high) and 1 port within 250 km.",
  exposure_summary: { nodes_by_type: { supplier: 0, factory: 0, port: 1, dc: 0, store: 0, lane: 0 }, open_po_lines: null, units_inbound: null },
  first_raised_at: "2026-10-08T03:12:00Z",
  updated_at: "2026-10-08T03:12:00Z",
  simulated: false,
});

export const newsAlert = alert({
  id: NEWS_ALERT_ID,
  event_id: "2e3d4c5b-6a7f-5e8d-9c0b-1a2f3e4d5c6b",
  severity: "medium",
  status: "snoozed",
  title: "Strike announced at Felixstowe <b>terminal</b>",
  why: "Severity 4,500 bp (medium) and 1 lane crosses the area.",
  exposure_summary: { nodes_by_type: { supplier: 0, factory: 0, port: 0, dc: 0, store: 0, lane: 1 }, open_po_lines: 0, units_inbound: null },
  updated_at: "2026-10-07T18:00:00Z",
  simulated: false,
  kind: "labor",
});

const cone: [number, number][] = [
  [107.0, 9.5],
  [108.5, 9.5],
  [108.5, 11.5],
  [107.0, 11.5],
  [107.0, 9.5],
];

export function typhoonDetail(over: { alert?: Partial<SignalsAlert>; extra?: Record<string, unknown> } = {}): SignalsAlertDetail & Record<string, unknown> {
  const { kind: _kind, ...a } = alert(over.alert);
  return {
    alert: a,
    event: {
      id: TYPHOON_EVENT_ID,
      tenant: null,
      kind: "natural_hazard",
      subtype: "tropical_cyclone",
      title: "Tropical cyclone KAI-TAK",
      summary: null,
      anchor_signal_id: "7a6b5c4d-3e2f-5a1b-8c9d-0e1f2a3b4c5d",
      member_signal_ids: ["7a6b5c4d-3e2f-5a1b-8c9d-0e1f2a3b4c5d", "8b7c6d5e-4f3a-5b2c-9d0e-1f2a3b4c5d6e"],
      geometry: { type: "Polygon", coordinates: [cone] },
      window: { starts_at: "2026-10-08T00:00:00Z", ends_at: null },
      severity_bp: 8200,
      confidence_bp: 8800,
      status: "confirmed",
      taxonomy_key: "NAT.storm",
      updated_at: NOW,
    },
    signals: [
      {
        id: "7a6b5c4d-3e2f-5a1b-8c9d-0e1f2a3b4c5d",
        tenant: null,
        origin: "demo",
        kind: "natural_hazard",
        subtype: "tropical_cyclone",
        title: "Tropical cyclone KAI-TAK, orange alert",
        summary: null,
        raw_text: null,
        geometry: { type: "Polygon", coordinates: [cone] },
        location_refs: ["UNLOCODE:VNSGN"],
        starts_at: "2026-10-08T00:00:00Z",
        ends_at: null,
        observed_at: NOW,
        expires_at: null,
        severity_bp: 8200,
        severity_native: { scheme: "gdacs", alertlevel: "red" },
        confidence_bp: 9000,
        provenance: "simulated",
        extracted_by: null,
        source: {
          connector_id: "gdacs",
          name: "GDACS",
          url: "https://www.gdacs.org/report.aspx?eventtype=TC&eventid=1000001",
          license: "CC BY 4.0",
          authority_bp: 9000,
          retrieved_at: NOW,
          native_id: "TC-1000001",
        },
        event_id: TYPHOON_EVENT_ID,
        version: 4,
        supersedes: null,
      },
      {
        id: "8b7c6d5e-4f3a-5b2c-9d0e-1f2a3b4c5d6e",
        tenant: null,
        origin: "demo",
        kind: "logistics",
        subtype: "port_closure",
        title: "Cat Lai terminal suspends operations ahead of the typhoon",
        summary: null,
        raw_text: "Cat Lai terminal suspends operations <script>alert(1)</script>",
        geometry: { type: "Point", coordinates: [106.77, 10.76] },
        location_refs: ["UNLOCODE:VNSGN"],
        starts_at: "2026-10-08T02:00:00Z",
        ends_at: null,
        observed_at: "2026-10-08T02:00:00Z",
        expires_at: null,
        severity_bp: 6000,
        severity_native: { scheme: "news" },
        confidence_bp: 5000,
        provenance: "inference",
        extracted_by: "extract-fast",
        source: {
          connector_id: "rss.demo-freight-daily",
          name: "Demo Freight Daily",
          url: "http://demo-feeds.localhost/demo-freight-daily/cat-lai",
          license: "Headline and link only",
          authority_bp: 5000,
          retrieved_at: "2026-10-08T02:00:00Z",
          native_id: "dfd-123",
        },
        event_id: TYPHOON_EVENT_ID,
        version: 1,
        supersedes: null,
      },
    ],
    exposures: [
      { event_id: TYPHOON_EVENT_ID, tenant: "demo-retail", node_type: "port", node_code: "VNSGN", method: "intersects", distance_km: null, weight_bp: 10000 },
      { event_id: TYPHOON_EVENT_ID, tenant: "demo-retail", node_type: "factory", node_code: "F-VN-01", method: "dwithin_km", distance_km: 42, weight_bp: 8000 },
      { event_id: TYPHOON_EVENT_ID, tenant: "demo-retail", node_type: "lane", node_code: "SGN-RTM", method: "lane_crosses", distance_km: null, weight_bp: 6000 },
    ],
    ...(over.extra ?? {}),
  };
}
