/**
 * Attribution of applied change batches (docs/contracts/20-merxa.md § Semantics):
 *  - delegated → "Changed by {actor_display} via Keelson ({approval_ref})", plus
 *                ", co-approved by Lena Vogel" for four-eyes approvals (M5)
 *  - rollback  → "Rolled back by {actor_display} via Keelson ({approval_ref})" (compensating batch)
 *  - policy    → "Applied via Keelson auto-policy {rule}@{ver}, initiated by {initiated_by}"
 * The API already renders `attribution_text`; the helpers here mirror its rules for
 * fallbacks and classify batches for the "External changes received" notice.
 */

export const EXTERNAL_CHANGE_WINDOW_MS = 10 * 60 * 1000;

export type ApprovedBy =
  | { kind: "user"; username: string }
  | { kind: "policy"; rule: string; version: string | null }
  | { kind: "unknown"; raw: string };

/** `user:marco` / `policy:P-12@3` → structured form. */
export function parseApprovedBy(value: string | null | undefined): ApprovedBy | null {
  if (!value) return null;
  if (value.startsWith("user:")) return { kind: "user", username: value.slice("user:".length) };
  if (value.startsWith("policy:")) {
    const spec = value.slice("policy:".length);
    const at = spec.lastIndexOf("@");
    return at > 0 ? { kind: "policy", rule: spec.slice(0, at), version: spec.slice(at + 1) } : { kind: "policy", rule: spec, version: null };
  }
  return { kind: "unknown", raw: value };
}

type AttributionSource = {
  status: string;
  approved_by?: string | null;
  via?: string | null;
  initiated_by?: string | null;
  applied_by_display?: string | null;
  applied_by_username?: string | null;
  approval_ref?: string | null;
  attribution_text?: string | null;
  /** M5: the other members of a four-eyes quorum (`[{ref: "user:lena", name: "Lena Vogel"}]`). */
  co_approvers?: CoApprover[] | null;
  /** Set on a compensating batch (the id of the batch it rolled back). */
  rollback_of?: string | null;
};

export type CoApprover = { ref: string; name: string | null };

/** "A", "A and B", "A, B and C". */
export function joinNames(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** Display names of the co-approvers (the username when no name was given). */
export function coApproverNames(value: readonly CoApprover[] | null | undefined): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((c) => c.name || c.ref.replace(/^user:/, "")).filter(Boolean);
}

/** Same wording as merxa-api `attribution_text()`; prefers the server-rendered text. */
export function attributionText(cs: AttributionSource): string | null {
  if (cs.attribution_text) return cs.attribution_text;
  if (cs.status !== "applied" && cs.status !== "rolled_back") return null;
  const via = cs.via || "API";
  const approved = cs.approved_by ?? "";
  if (approved.startsWith("policy:")) {
    return `Applied via ${via} auto-policy ${approved.slice("policy:".length)}, initiated by ${cs.initiated_by || "unknown"}`;
  }
  const actor = cs.applied_by_display || cs.applied_by_username || "unknown";
  const verb = cs.rollback_of ? "Rolled back by" : "Changed by";
  const co = coApproverNames(cs.co_approvers);
  return (
    `${verb} ${actor} via ${via}` +
    (cs.approval_ref ? ` (${cs.approval_ref})` : "") +
    (co.length ? `, co-approved by ${joinNames(co)}` : "")
  );
}

/** Channels that are Merxa's own (people in the Merxa UI, the demo director). */
export const INTERNAL_CHANNELS: readonly string[] = ["Merxa UI", "Demo director"];

/** Changed through an integration (e.g. "Keelson", "API") rather than inside Merxa. */
export function isExternal(record: { via?: string | null }): boolean {
  return !!record.via && !INTERNAL_CHANNELS.includes(record.via);
}

type AuditLike = { id: number; entity_ref: string; via?: string | null };

/**
 * Records the latest audit row per `entity_ref` into `latest` (call once per page, rows in
 * any order; the highest id wins).
 */
export function trackLatestChange<T extends AuditLike>(latest: Map<string, T>, rows: Iterable<T>): Map<string, T> {
  for (const row of rows) {
    const seen = latest.get(row.entity_ref);
    if (!seen || seen.id < row.id) latest.set(row.entity_ref, row);
  }
  return latest;
}

/** Entities whose most recent change came from an external system (a later Merxa-internal change clears it). */
export function externallyChangedEntities<T extends AuditLike>(latest: Map<string, T>): Map<string, T> {
  return new Map([...latest].filter(([, row]) => isExternal(row)));
}

/** True when the batch was applied within `windowMs` before `now`. */
export function isRecentlyApplied(
  cs: { status: string; applied_at?: string | null },
  now: number,
  windowMs: number = EXTERNAL_CHANGE_WINDOW_MS,
): boolean {
  if (cs.status !== "applied" || !cs.applied_at) return false;
  const t = new Date(cs.applied_at).getTime();
  if (Number.isNaN(t)) return false;
  const age = now - t;
  // Small negative ages are clock skew between browser and server: still "recent".
  return age <= windowMs && age >= -60_000;
}

/** Recently applied batches, newest application first. */
export function recentlyApplied<T extends { status: string; applied_at?: string | null }>(
  items: readonly T[],
  now: number,
  windowMs: number = EXTERNAL_CHANGE_WINDOW_MS,
): T[] {
  return items
    .filter((cs) => isRecentlyApplied(cs, now, windowMs))
    .sort((a, b) => new Date(b.applied_at ?? 0).getTime() - new Date(a.applied_at ?? 0).getTime());
}

/** `change_set:CS-0004` (transfer_order.created_via) → "CS-0004"; anything else → null. */
export function changeSetCodeFromCreatedVia(createdVia: string | null | undefined): string | null {
  if (!createdVia || !createdVia.startsWith("change_set:")) return null;
  const code = createdVia.slice("change_set:".length).trim();
  return code || null;
}
