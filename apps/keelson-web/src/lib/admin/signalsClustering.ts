/**
 * Signals admin — clustering settings (M8, `docs/contracts/123` §7): the platform-wide SimHash and
 * vector thresholds of the Signal Hub, its similarity backends and the last 7 days' counts.
 * `GET /api/admin/signals/clustering` (every admin; `editable` = platform admin),
 * `PUT …/clustering` (`ClusteringConfigUpdate`, platform admin; 409 `stale_version`),
 * `POST …/clustering/run` (one maintenance round now, platform admin).
 */
import {
  signalsClusteringConfigSchema,
  signalsClusteringStatusSchema,
  type SignalsClusteringConfig,
  type SignalsClusteringConfigUpdate,
} from "@keelson/contracts";
import { z } from "zod";
import { isApiError, type ApiClient } from "../api/client";

/** The engine's answer: the contract status + `editable` (the caller may change the settings). */
export const clusteringViewSchema = signalsClusteringStatusSchema
  .extend({ editable: z.boolean().optional().catch(false) })
  .strip()
  .transform((v) => ({ ...v, editable: v.editable ?? false }));
export type ClusteringView = z.infer<typeof clusteringViewSchema>;

export const clusteringConfigSchema = signalsClusteringConfigSchema.strip();
export type ClusteringConfig = SignalsClusteringConfig;
export type ClusteringDraft = Pick<SignalsClusteringConfig, "simhash" | "vector">;
export type ThresholdProfile = "local" | "remote";
export type ThresholdKey = "attach_bp" | "merge_bp" | "strict_bp";

export const runResultSchema = z
  .object({ merged: z.array(z.unknown()).catch([]), embedded: z.number().int().nonnegative().catch(0) })
  .loose();
export type RunResult = z.infer<typeof runResultSchema>;

/** Contract bounds (`keelson_contracts.signals`: SimHashSettings / VectorSettings / VectorThresholds). */
export const CLUSTERING_BOUNDS = {
  hammingBits: { min: 0, max: 7 },
  windowHours: { min: 1, max: 168 },
  maxDistanceKm: { min: 50, max: 3000 },
  attachBp: { min: 5000, max: 9900 },
  bp: { min: 0, max: 10_000 },
} as const;

export const THRESHOLD_LABELS: Record<ThresholdKey, string> = {
  attach_bp: "Attach",
  merge_bp: "Merge",
  strict_bp: "Strict (no location)",
};

/** The editable part of the stored settings (a fresh copy). */
export function draftOf(config: ClusteringConfig): ClusteringDraft {
  return {
    simhash: { ...config.simhash },
    vector: { ...config.vector, local: { ...config.vector.local }, remote: { ...config.vector.remote } },
  };
}

const isInt = (n: number) => Number.isInteger(n);
const inRange = (n: number, b: { min: number; max: number }) => isInt(n) && n >= b.min && n <= b.max;

/**
 * Problems by field (`simhash.max_hamming_bits`, `vector.local.merge_bp`, …), mirroring the contract
 * validators: attach 50–99 %, merge and strict at least attach, all at most 100 %, whole numbers.
 */
export function clusteringProblems(d: ClusteringDraft): Record<string, string> {
  const out: Record<string, string> = {};
  const b = CLUSTERING_BOUNDS;
  if (!inRange(d.simhash.max_hamming_bits, b.hammingBits)) out["simhash.max_hamming_bits"] = `${b.hammingBits.min} to ${b.hammingBits.max} bits.`;
  if (!inRange(d.simhash.window_hours, b.windowHours)) out["simhash.window_hours"] = `A whole number of hours, ${b.windowHours.min} to ${b.windowHours.max}.`;
  if (!inRange(d.vector.window_hours, b.windowHours)) out["vector.window_hours"] = `A whole number of hours, ${b.windowHours.min} to ${b.windowHours.max}.`;
  if (!inRange(d.vector.max_distance_km, b.maxDistanceKm)) out["vector.max_distance_km"] = `A whole number of km, ${b.maxDistanceKm.min} to ${b.maxDistanceKm.max}.`;
  for (const profile of ["local", "remote"] as const) {
    const t = d.vector[profile];
    if (!inRange(t.attach_bp, b.attachBp)) out[`vector.${profile}.attach_bp`] = "Between 50 % and 99 %.";
    for (const key of ["merge_bp", "strict_bp"] as const) {
      if (!inRange(t[key], b.bp)) out[`vector.${profile}.${key}`] = "Between 0 % and 100 %.";
      else if (inRange(t.attach_bp, b.attachBp) && t[key] < t.attach_bp) out[`vector.${profile}.${key}`] = "At least the attach threshold.";
    }
  }
  return out;
}

/** The `PUT` body: the whole settings on top of the version they were read at. */
export function updateBody(baseVersion: number, d: ClusteringDraft): SignalsClusteringConfigUpdate {
  return { base_version: baseVersion, simhash: d.simhash, vector: d.vector, updated_by: null };
}

/** Basis points ↔ percent with up to two decimals (contract values are integers 0…10,000). */
export const bpToPctText = (bp: number) => String(Math.round(bp) / 100);
export const pctTextToBp = (text: string) => {
  const n = Number(text.trim().replace(",", "."));
  return text.trim() === "" || !Number.isFinite(n) ? Number.NaN : Math.round(n * 100);
};

/** Person-facing text of a save error (`null` = use the generic one). */
export function clusteringSaveError(err: unknown): string | null {
  if (isApiError(err, "stale_version")) return "Settings changed by someone else — reload them, then make your change again.";
  if (isApiError(err, "platform_admin_required")) return "Only a platform admin can change clustering: it applies to every workspace.";
  return null;
}

/** Remote embedding backend in words. */
export function remoteText(view: ClusteringView): string {
  const r = view.vector_backend.remote;
  if (r) return `${r.alias}@${r.dims} (${r.storage})${r.status === "backfilling" ? `, embedding ${new Intl.NumberFormat("en-GB").format(r.pending)} reports` : ""}`;
  switch (view.vector_backend.remote_reason) {
    case "pgvector_missing":
      return "Off: the signals database has no pgvector";
    case "profile_missing":
      return "Off: no active profile yet (re-run the bootstrap)";
    case "key_missing":
      return "Waiting for the gateway key";
    default:
      return "Off (local term vectors only)";
  }
}

export const COUNT_LABELS: [keyof ClusteringView["counts"], string][] = [
  ["simhash_near_duplicates", "Near-duplicate reports (SimHash)"],
  ["same_source_duplicates", "Identical re-posts dropped"],
  ["extractions_reused", "Extractions reused (no model call)"],
  ["anchor", "Official reports"],
  ["same_hazard", "Same hazard, second official feed"],
  ["founded", "First reports"],
  ["attach_ref", "Joined by shared place"],
  ["attach_geo", "Joined by distance"],
  ["attach_vector", "Joined by similar description"],
  ["near_duplicate", "Joined as near-duplicate"],
  ["absorbed", "Moved into an official event"],
  ["merged_vector", "Moved by a merge"],
  ["embeddings_pending", "Embeddings waiting"],
  ["embeddings_failed", "Embeddings failed"],
];

/** The clustering API over the session's client (kept apart from the shared `AdminApi`). */
export class ClusteringApi {
  constructor(private readonly api: ApiClient) {}

  status(): Promise<ClusteringView> {
    return this.api.call("/api/admin/signals/clustering", { schema: clusteringViewSchema });
  }

  save(body: SignalsClusteringConfigUpdate): Promise<ClusteringConfig> {
    return this.api.call("/api/admin/signals/clustering", { method: "PUT", body, schema: clusteringConfigSchema });
  }

  run(): Promise<RunResult> {
    return this.api.call("/api/admin/signals/clustering/run", { method: "POST", body: {}, schema: runResultSchema });
  }
}
