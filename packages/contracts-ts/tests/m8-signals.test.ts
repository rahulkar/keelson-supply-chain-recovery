import { describe, expect, it } from "vitest";
import {
  MEMBERSHIP_METHODS,
  signalsAlertDetailSchema,
  signalsClusteringConfigSchema,
  signalsClusteringConfigUpdateSchema,
  signalsClusteringStatusSchema,
  signalsEventMembershipSchema,
} from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m8_clustering.py (docs/contracts/123).
const SIG_ID = "9d7a3c51-2a54-5d0e-8a07-0f0f3e3b7c10";
const COPY_ID = "5f0a1d2c-3b4e-5f60-8172-a3b4c5d6e7f8";
const EVT_ID = "1c6f2f0e-41b8-5b44-9d6f-5c0f4d0c2b11";
const OTHER_EVT = "2d7f3f1f-52c9-5c55-8e70-6d1f5e1d3c22";
const ALERT_ID = "0b8f3f5e-6a1c-5d2e-9f00-1a2b3c4d5e6f";

const membership = (patch: Record<string, unknown> = {}) => ({
  signal_id: SIG_ID,
  event_id: EVT_ID,
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
  explanation: "First report of this event.",
  decided_at: "2026-10-07T06:01:00Z",
  ...patch,
});

const config = {
  version: 3,
  simhash: { enabled: true, max_hamming_bits: 6, window_hours: 72, reuse_extraction: true },
  vector: {
    enabled: true,
    window_hours: 72,
    max_distance_km: 1000,
    local: { attach_bp: 7000, merge_bp: 7500, strict_bp: 7500 },
    remote: { attach_bp: 8500, merge_bp: 9000, strict_bp: 9300 },
  },
  updated_at: "2026-10-07T06:01:00Z",
  updated_by: "ava",
};

const signal = {
  id: SIG_ID,
  tenant: null,
  origin: "news",
  kind: "labor",
  subtype: "strike",
  title: "Dockworkers strike at the Port of Hamburg",
  summary: null,
  raw_text: null,
  geometry: { type: "Point", coordinates: [9.97, 53.54] },
  location_refs: ["UNLOCODE:DEHAM", "ISO3166:DE"],
  starts_at: "2026-10-07T00:00:00Z",
  ends_at: null,
  observed_at: "2026-10-07T06:00:00Z",
  expires_at: null,
  severity_bp: 5500,
  severity_native: { scheme: "extraction_prior" },
  confidence_bp: 6000,
  provenance: "inference",
  extracted_by: "extract-fast",
  source: {
    connector_id: "rss.demo",
    name: "Demo Freight Daily",
    url: null,
    license: "headline only",
    authority_bp: 7000,
    retrieved_at: "2026-10-07T06:01:00Z",
    native_id: "a-1",
  },
  event_id: EVT_ID,
  version: 1,
  supersedes: null,
};

const detail = {
  alert: {
    id: ALERT_ID,
    tenant: "demo-retail",
    event_id: EVT_ID,
    status: "new",
    severity: "medium",
    title: "Dockworkers strike at the Port of Hamburg",
    why: "Severity 55% with 1 exposed port.",
    exposure_summary: { nodes_by_type: { supplier: 0, factory: 0, port: 1, dc: 0, store: 0, lane: 4 }, open_po_lines: null, units_inbound: null },
    first_raised_at: "2026-10-07T06:02:00Z",
    updated_at: "2026-10-07T06:02:00Z",
    case_id: null,
    simulated: true,
  },
  event: {
    id: EVT_ID,
    tenant: null,
    kind: "labor",
    subtype: "strike",
    title: "Dockworkers strike at the Port of Hamburg",
    summary: null,
    anchor_signal_id: null,
    member_signal_ids: [SIG_ID],
    geometry: null,
    window: { starts_at: "2026-10-07T00:00:00Z", ends_at: null },
    severity_bp: 5500,
    confidence_bp: 6000,
    status: "candidate",
    taxonomy_key: "LOG.strike",
    updated_at: "2026-10-07T06:01:00Z",
  },
  signals: [signal],
  exposures: [],
};

describe("M8 signal clustering contracts", () => {
  it("exposes the membership methods", () => {
    expect(MEMBERSHIP_METHODS).toEqual([
      "anchor",
      "same_hazard",
      "founded",
      "attach_geo",
      "attach_ref",
      "attach_vector",
      "near_duplicate",
      "absorbed",
      "merged_vector",
    ]);
  });

  it("parses memberships with their evidence", () => {
    expect(signalsEventMembershipSchema.safeParse(membership()).success).toBe(true);
    const nearDup = membership({ signal_id: COPY_ID, method: "near_duplicate", hamming_bits: 2, duplicate_of_signal_id: SIG_ID });
    expect(signalsEventMembershipSchema.parse(nearDup).hamming_bits).toBe(2);
    const vector = membership({ method: "attach_vector", similarity_bp: 7977, similarity_profile: "local-terms-v1" });
    expect(signalsEventMembershipSchema.safeParse(vector).success).toBe(true);
    const merged = membership({ method: "merged_vector", prior_method: "founded", from_event_id: OTHER_EVT, similarity_bp: 9300, similarity_profile: "embed-default@1536" });
    expect(signalsEventMembershipSchema.safeParse(merged).success).toBe(true);
  });

  it.each([
    ["an unknown method", { method: "guessed" }],
    ["65 differing bits", { hamming_bits: 65 }],
    ["a similarity above 10000", { similarity_bp: 10001 }],
    ["a bad profile id", { similarity_profile: "Embed Default" }],
    ["an over-long explanation", { explanation: "x".repeat(401) }],
    ["an extra field", { score: 1 }],
  ])("rejects a membership with %s", (_label, patch) => {
    expect(signalsEventMembershipSchema.safeParse(membership(patch)).success).toBe(false);
  });

  it("keeps memberships optional on the alert detail", () => {
    expect(signalsAlertDetailSchema.safeParse(detail).success).toBe(true);
    const withMemberships = signalsAlertDetailSchema.parse({ ...detail, memberships: [membership()] });
    expect(withMemberships.memberships?.[0]?.method).toBe("founded");
    expect(signalsAlertDetailSchema.safeParse({ ...detail, memberships: [{ ...membership(), method: "x" }] }).success).toBe(false);
  });

  it("parses the clustering settings, update and status", () => {
    expect(signalsClusteringConfigSchema.parse(config).vector.local.attach_bp).toBe(7000);
    expect(signalsClusteringConfigSchema.safeParse({ ...config, simhash: { ...config.simhash, max_hamming_bits: 8 } }).success).toBe(false);
    expect(signalsClusteringConfigSchema.safeParse({ ...config, vector: { ...config.vector, max_distance_km: 10 } }).success).toBe(false);
    const update = { base_version: 3, simhash: config.simhash, vector: config.vector, updated_by: "ava" };
    expect(signalsClusteringConfigUpdateSchema.safeParse(update).success).toBe(true);
    expect(signalsClusteringConfigUpdateSchema.safeParse({ ...update, base_version: 0 }).success).toBe(false);
    const counts = Object.fromEntries(
      [
        "simhash_near_duplicates",
        "same_source_duplicates",
        "extractions_reused",
        "anchor",
        "same_hazard",
        "founded",
        "attach_geo",
        "attach_ref",
        "attach_vector",
        "near_duplicate",
        "absorbed",
        "merged_vector",
        "embeddings_pending",
        "embeddings_failed",
      ].map((k) => [k, 0]),
    );
    const status = {
      config,
      vector_backend: { pgvector: false, local_profile: "local-terms-v1", remote: null, remote_reason: "pgvector_missing" },
      window_days: 7,
      counts,
    };
    expect(signalsClusteringStatusSchema.safeParse(status).success).toBe(true);
    expect(signalsClusteringStatusSchema.safeParse({ ...status, vector_backend: { ...status.vector_backend, remote_reason: "nope" } }).success).toBe(false);
  });
});
