/**
 * M6 Signal Hub vocabularies and small helpers (docs/contracts/100; contracts-py `keelson_contracts.signals`).
 * Schemas are generated (`signals*Schema` in ./generated); this module only derives friendly names.
 */
import {
  signalsAlertSchema,
  signalsEventMembershipSchema,
  signalsExposureSummarySchema,
  signalsSignalSchema,
  type SignalsExposureSummary,
} from "./generated";

/** `low` | `medium` | `high` | `critical` (alert severity chip). */
export const ALERT_SEVERITIES = signalsAlertSchema.shape.severity.options;
export type AlertSeverity = (typeof ALERT_SEVERITIES)[number];

/** `new` | `seen` | `snoozed` | `dismissed` | `opened` | `resolved`. */
export const ALERT_STATUSES = signalsAlertSchema.shape.status.options;
export type AlertStatus = (typeof ALERT_STATUSES)[number];

/** Signal / event kinds (kind icon). */
export const SIGNAL_KINDS = signalsSignalSchema.shape.kind.options;
export type SignalKind = (typeof SIGNAL_KINDS)[number];

/** `supplier` | `factory` | `port` | `dc` | `store` | `lane`. */
export const NODE_TYPES = Object.keys(
  signalsExposureSummarySchema.shape.nodes_by_type.shape,
).sort() as (keyof SignalsExposureSummary["nodes_by_type"])[];
export type NodeType = (typeof NODE_TYPES)[number];

/** Exposed non-lane nodes ("3 sites"). */
export function exposedSites(summary: SignalsExposureSummary): number {
  const n = summary.nodes_by_type;
  return n.supplier + n.factory + n.port + n.dc + n.store;
}

/**
 * Why a signal belongs to its event (M8, docs/contracts/123 §5): `anchor` | `same_hazard` | `founded` |
 * `attach_geo` | `attach_ref` | `attach_vector` | `near_duplicate` | `absorbed` | `merged_vector`.
 */
export const MEMBERSHIP_METHODS = signalsEventMembershipSchema.shape.method.options;
export type MembershipMethod = (typeof MEMBERSHIP_METHODS)[number];
