import { describe, expect, it } from "vitest";
import {
  AGENT_DISPLAY_NAMES,
  ALERT_SEVERITIES,
  ALERT_STATUSES,
  BLOCK_KEYS,
  EVENT_TYPE_KEYS,
  NODE_TYPES,
  SIGNAL_KINDS,
  alertOriginPropsSchema,
  exposedSites,
  llmNewsExtractionSchema,
  parseBlockProps,
  runRequestSchema,
  signalsAlertDetailSchema,
  signalsAlertSchema,
  signalsAlertStatusUpdateSchema,
  signalsScenarioPackSchema,
  signalsSignalSchema,
  signalsTenantNetworkSchema,
} from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m6.py.
const SIG_ID = "9d7a3c51-2a54-5d0e-8a07-0f0f3e3b7c10";
const EVT_ID = "1c6f2f0e-41b8-5b44-9d6f-5c0f4d0c2b11";
const ALERT_ID = "0b8f3f5e-6a1c-5d2e-9f00-1a2b3c4d5e6f";
const CONE = [[106.0, 9.5], [108.5, 9.5], [108.5, 12.0], [106.0, 12.0], [106.0, 9.5]];

const signal = {
  id: SIG_ID,
  tenant: null,
  origin: "feed",
  kind: "natural_hazard",
  subtype: "tropical_cyclone",
  title: "Tropical cyclone KAI-TAK (orange)",
  summary: null,
  raw_text: null,
  geometry: {
    type: "GeometryCollection",
    geometries: [
      { type: "LineString", coordinates: [[110.0, 10.0], [106.7, 10.8]] },
      { type: "Polygon", coordinates: [CONE] },
    ],
  },
  location_refs: ["UNLOCODE:VNSGN", "ISO3166:VN"],
  starts_at: "2026-10-07T00:00:00Z",
  ends_at: null,
  observed_at: "2026-10-07T06:00:00Z",
  expires_at: null,
  severity_bp: 7200,
  severity_native: { scheme: "gdacs", alertlevel: "orange", alertscore: 2.5 },
  confidence_bp: 9000,
  provenance: "fact",
  extracted_by: null,
  source: {
    connector_id: "gdacs",
    name: "GDACS",
    url: "https://www.gdacs.org/report.aspx?eventtype=TC&eventid=1001234",
    license: "CC BY 4.0",
    authority_bp: 9500,
    retrieved_at: "2026-10-07T06:01:00Z",
    native_id: "TC:1001234:7",
  },
  event_id: EVT_ID,
  version: 1,
  supersedes: null,
};

const event = {
  id: EVT_ID,
  tenant: null,
  kind: "natural_hazard",
  subtype: "tropical_cyclone",
  title: "Tropical cyclone KAI-TAK",
  summary: null,
  anchor_signal_id: SIG_ID,
  member_signal_ids: [SIG_ID],
  geometry: { type: "Polygon", coordinates: [CONE] },
  window: { starts_at: "2026-10-07T00:00:00Z", ends_at: null },
  severity_bp: 7200,
  confidence_bp: 9000,
  status: "confirmed",
  taxonomy_key: "NAT.storm",
  updated_at: "2026-10-07T06:01:00Z",
};

const alert = {
  id: ALERT_ID,
  tenant: "demo-retail",
  event_id: EVT_ID,
  status: "new",
  severity: "high",
  title: "Typhoon approaching Ho Chi Minh City",
  why: "Severity 72% with 3 exposed sites and 4 lanes.",
  exposure_summary: {
    nodes_by_type: { supplier: 1, factory: 1, port: 1, dc: 0, store: 0, lane: 4 },
    open_po_lines: 2,
    units_inbound: 1200,
  },
  first_raised_at: "2026-10-07T06:02:00Z",
  updated_at: "2026-10-07T06:02:00Z",
  case_id: null,
  simulated: true,
};

describe("M6 signals", () => {
  it("exposes the vocabularies", () => {
    expect(ALERT_SEVERITIES).toEqual(["low", "medium", "high", "critical"]);
    expect(ALERT_STATUSES).toContain("snoozed");
    expect(SIGNAL_KINDS).toContain("natural_hazard");
    expect(NODE_TYPES).toEqual(["dc", "factory", "lane", "port", "store", "supplier"]);
    expect(EVENT_TYPE_KEYS).toContain("NAT.earthquake");
    expect(AGENT_DISPLAY_NAMES.external_intel).toBe("External intelligence");
  });

  it("parses a feed signal with float WGS84 geometry", () => {
    const parsed = signalsSignalSchema.parse(signal);
    expect(parsed.severity_native.alertscore).toBe(2.5);
  });

  it.each([
    ["javascript attribution link", { source: { ...signal.source, url: "javascript:alert(1)" } }],
    ["userinfo attribution link", { source: { ...signal.source, url: "https://u@evil.example/x" } }],
    ["float severity", { severity_bp: 72.5 }],
    ["severity above 10000", { severity_bp: 10001 }],
    ["unknown kind", { kind: "tsunami" }],
    ["unknown location scheme", { location_refs: ["CITY:SAIGON"] }],
    ["non-scalar severity_native", { severity_native: { scheme: "gdacs", extra: [1] } }],
    ["extra field", { score: 1 }],
  ])("rejects a signal with %s", (_label, patch) => {
    expect(signalsSignalSchema.safeParse({ ...signal, ...patch }).success).toBe(false);
  });

  it("parses alerts and the alert detail", () => {
    const a = signalsAlertSchema.parse(alert);
    expect(exposedSites(a.exposure_summary)).toBe(3);
    expect(
      signalsAlertDetailSchema.safeParse({ alert, event, signals: [signal], exposures: [] }).success,
    ).toBe(true);
    expect(signalsAlertSchema.safeParse({ ...alert, severity: "severe" }).success).toBe(false);
    expect(signalsAlertStatusUpdateSchema.parse({ status: "seen" }).case_id).toBeNull();
    expect(signalsAlertStatusUpdateSchema.safeParse({ status: "new" }).success).toBe(false);
  });

  it("parses scenario packs and tenant networks", () => {
    expect(
      signalsScenarioPackSchema.safeParse({
        id: "typhoon-hcmc",
        title: "Typhoon",
        description: "",
        start_at: "2026-10-07T00:00:00Z",
        steps: [{ t_offset_s: 0, adapter: "gdacs", payload: { fixture: "a.geojson" }, note: "" }],
      }).success,
    ).toBe(true);
    expect(
      signalsTenantNetworkSchema.safeParse({
        nodes: [{ type: "port", code: "VNSGN", name: "HCMC", lat: 10.77, lon: 106.7 }],
        lanes: [{ code: "SGN-RTM", mode: "sea", path: [[106.7, 10.77], [4.4, 51.9]] }],
      }).success,
    ).toBe(true);
  });

  it("knows the news extraction LLM output", () => {
    const x = {
      kind: "logistics",
      subtype: "port_closure",
      places: ["Ho Chi Minh City"],
      companies: [],
      ports: ["Cat Lai"],
      chokepoints: [],
      dates_text: [],
      injection_suspected: false,
    };
    expect(llmNewsExtractionSchema.safeParse(x).success).toBe(true);
    expect(llmNewsExtractionSchema.safeParse({ ...x, kind: "tsunami" }).success).toBe(false);
  });

  it("accepts both signal run payload shapes", () => {
    const run = (payload: unknown) => ({
      run_id: "r1",
      tenant: "demo-retail",
      case_id: "c1",
      kind: "signal",
      actor: { sub: "s", username: "u", name: "n", roles: [] },
      payload,
    });
    expect(runRequestSchema.safeParse(run({ signal_id: "sig-1" })).success).toBe(true);
    expect(runRequestSchema.safeParse(run({ event_id: EVT_ID, alert_id: null })).success).toBe(true);
    expect(runRequestSchema.safeParse(run({ event_id: EVT_ID, alert_id: ALERT_ID })).success).toBe(true);
    // Signal Hub ids go into hub URL paths: only canonical UUIDs (review regression)
    for (const bad of ["../../v1/demo/packs/x/reset", "evt-1", EVT_ID.toUpperCase(), `${EVT_ID}?tenant=x`]) {
      expect(runRequestSchema.safeParse(run({ event_id: bad, alert_id: null })).success).toBe(false);
      expect(runRequestSchema.safeParse(run({ event_id: EVT_ID, alert_id: bad })).success).toBe(false);
    }
  });
});

describe("alert_origin@1", () => {
  const props = {
    alert_id: ALERT_ID,
    title: "Typhoon approaching Ho Chi Minh City",
    severity: "critical",
    kind: "natural_hazard",
    source_names: ["GDACS", "Demo Freight Daily"],
    simulated: true,
    opened_at: "2026-10-07T06:05:00Z",
  };

  it("is a known block kind with typed props", () => {
    expect(BLOCK_KEYS).toContain("alert_origin@1");
    const r = parseBlockProps({ kind: "alert_origin", v: 1, props });
    expect(r.ok).toBe(true);
    expect(alertOriginPropsSchema.parse(props).simulated).toBe(true);
  });

  it.each([
    ["non-uuid alert id", { alert_id: "alert-1" }],
    ["unknown severity", { severity: "severe" }],
    ["naive timestamp", { opened_at: "2026-10-07T06:05:00" }],
    ["missing simulated", { simulated: undefined }],
  ])("rejects %s", (_label, patch) => {
    expect(parseBlockProps({ kind: "alert_origin", v: 1, props: { ...props, ...patch } }).ok).toBe(false);
  });
});
