/**
 * Four-eyes on guarded changes (`docs/contracts/110` §1): a tenant admin's guarded PUT / PATCH
 * is answered `202` with an **admin change request** instead of being applied; a different
 * tenant admin (or a platform admin) approves or rejects it on the Pending changes page.
 *
 * The request itself is the generated contract model (`adminChangeRequestSchema`, unknown
 * top-level fields stripped so an additive engine field never breaks the queue).
 */
import {
  CHANGE_REQUEST_TTL_MS,
  adminChangeRequestSchema,
  canApproveChangeRequest,
  changeRequestExpired,
  type AdminChangeRequest,
  type ChangeRequestKind,
  type ChangeRequestStatus,
  type GuardReasonCode,
} from "@keelson/contracts";
import { z } from "zod";

export type ChangeRequest = AdminChangeRequest;

export const changeRequestSchema = adminChangeRequestSchema.strip();

/** `{change_request: {...}}` or the bare request (the engine's 202 body). */
export const changeRequestAnswerSchema = z.union([
  z.object({ change_request: changeRequestSchema }).loose().transform((v) => v.change_request),
  changeRequestSchema,
]);

/** A guarded mutation's answer: applied right away, or turned into a change request. */
export type Guarded<T> = { kind: "applied"; value: T } | { kind: "change_request"; request: ChangeRequest };

/**
 * Picks the answer schema of a guarded route by HTTP status: `202 Accepted` = a change request
 * (validated against the contract; anything else is an invalid response, never "applied"),
 * any other success = the route's normal answer.
 */
export function guardedSchemaFor<T>(schema: z.ZodType<T>): (status: number) => z.ZodType<Guarded<T>> {
  const asRequest = changeRequestAnswerSchema.transform((request): Guarded<T> => ({ kind: "change_request", request }));
  const asApplied = schema.transform((value): Guarded<T> => ({ kind: "applied", value }));
  return (status) => (status === 202 ? asRequest : asApplied);
}

/** Valid requests of a list (`{items}` or a bare array); malformed entries are skipped. */
export const changeRequestListSchema = z
  .union([z.array(z.unknown()), z.object({ items: z.array(z.unknown()) }).loose()])
  .transform((v) => {
    const items = Array.isArray(v) ? v : v.items;
    const out: ChangeRequest[] = [];
    for (const item of items) {
      const parsed = changeRequestSchema.safeParse(item);
      if (parsed.success) out.push(parsed.data);
    }
    return out;
  });

export const CHANGE_KIND_LABELS: Record<ChangeRequestKind, string> = {
  agent_spec: "Agent settings",
  policy_set: "Approval policies",
  connector_tool: "Tool classification",
  tool_binding: "Tool binding",
  connector_activate: "Connector activation",
  connector_trust: "Connector trust level",
  embedding_profile: "Embedding profile",
  connector_delete: "Delete a connector",
  kb_delete: "Delete a knowledge base",
  user_roles: "User roles",
};

/** Short, person-facing reason per guard code (the engine's `reason` sentence is shown too). */
export const GUARD_REASON_LABELS: Record<GuardReasonCode, string> = {
  auto_cap_raised: "Raises an automatic-apply limit",
  approval_threshold_raised: "Raises an approval threshold",
  approver_changed: "Changes who approves",
  quorum_lowered: "Lowers the number of approvals",
  planning_connector_changed: "Changes the planning connector",
  compliance_block_removed: "Takes a factory off the compliance block list",
  auto_rule_added: "Adds an automatic-apply rule",
  policy_rule_removed: "Removes an approval or refuse rule",
  policy_rule_weakened: "Weakens an approval or refuse rule",
  tool_reclassified_read: "Reclassifies a write tool as read-only",
  tool_reclassified_trusted: "Marks untrusted text as trusted data",
  auto_binding_on_write: "Lets a write tool run automatically",
  external_egress_enabled: "Enables a connector that sends data outside",
  first_party_trust_added: "Adds a first-party trust level",
  embedding_profile_changed: "Changes the active embedding profile",
  connector_deleted: "Deletes a connector",
  kb_deleted: "Deletes a knowledge base",
  admin_role_granted: "Grants an admin role",
};

export const CHANGE_STATUS_META: Record<ChangeRequestStatus, { label: string; tone: "warning" | "success" | "danger" | "neutral" | "info" }> = {
  pending: { label: "Waiting for a second admin", tone: "warning" },
  approved: { label: "Approved", tone: "info" },
  applied: { label: "Approved and applied", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  expired: { label: "Expired", tone: "neutral" },
};

export function kindLabel(kind: string): string {
  return (CHANGE_KIND_LABELS as Record<string, string>)[kind] ?? kind.replace(/_/g, " ");
}

export function reasonLabel(code: string): string {
  return (GUARD_REASON_LABELS as Record<string, string>)[code] ?? code.replace(/_/g, " ");
}

/** The status as shown: a pending request past its expiry reads as expired (the engine flips it lazily). */
export function effectiveStatus(cr: Pick<ChangeRequest, "status" | "expires_at">, now: Date): ChangeRequestStatus {
  return changeRequestExpired(cr, now) ? "expired" : cr.status;
}

/** "Expires in 71 h" / "Expires in 25 min" / "Expired". */
export function expiresIn(cr: Pick<ChangeRequest, "status" | "expires_at">, now: Date): string | null {
  if (cr.status !== "pending") return null;
  const ms = Date.parse(cr.expires_at) - now.getTime();
  if (!Number.isFinite(ms)) return null;
  if (ms <= 0) return "Expired";
  const h = Math.floor(ms / 3_600_000);
  if (h >= 1) return `Expires in ${h} h`;
  return `Expires in ${Math.max(1, Math.ceil(ms / 60_000))} min`;
}

/**
 * Kinds with platform-wide effects: only a platform admin approves them (engine
 * `change_requests.PLATFORM_KINDS` answers `403 platform_admin_required`); a tenant admin may
 * still reject one.
 */
export const PLATFORM_APPROVAL_KINDS: ReadonlySet<string> = new Set(["embedding_profile"]);

/** Kinds whose approval only *starts* the change (status `approved`, not `applied`; engine `ASYNC_KINDS`). */
export const ASYNC_APPLY_KINDS: ReadonlySet<string> = new Set(["embedding_profile"]);

export type ApproveState =
  | { can: true }
  /**
   * `own`: the viewer asked for it (self-approval is impossible; the requester may only
   * *withdraw* it, i.e. reject it). `platform_only`: a platform-wide change a tenant admin may
   * reject but not approve.
   */
  | { can: false; why: "own" | "not_pending" | "expired" | "platform_only" };

/** Whether this viewer may approve (pending, not expired, not their own request; platform-wide kinds: platform admins only). */
export function approveState(cr: ChangeRequest, viewerSub: string, now: Date, viewerRoles: readonly string[] = []): ApproveState {
  if (cr.status !== "pending") return { can: false, why: "not_pending" };
  if (changeRequestExpired(cr, now)) return { can: false, why: "expired" };
  if (!canApproveChangeRequest(cr, viewerSub, now)) return { can: false, why: "own" };
  if (PLATFORM_APPROVAL_KINDS.has(cr.kind) && !viewerRoles.includes("platform-admin")) return { can: false, why: "platform_only" };
  return { can: true };
}

/**
 * Whether the viewer (an admin of the tenant) may reject: while it is pending and not expired —
 * the requester included, for whom rejecting *withdraws* the request (contracts-py
 * `change_request_decision_error`: self-approval is refused, self-rejection is not).
 */
export function canReject(cr: Pick<ChangeRequest, "status" | "expires_at">, now: Date): boolean {
  return cr.status === "pending" && !changeRequestExpired(cr, now);
}

/** The 72 h window, for copy ("another admin has 72 hours to approve it"). */
export const CHANGE_REQUEST_TTL_HOURS = Math.round(CHANGE_REQUEST_TTL_MS / 3_600_000);

/** Decision note limit (`ChangeRequestDecision.reason`). */
export const DECISION_REASON_MAX = 500;

/**
 * `{reason}` for approve / reject: trimmed, `null` when empty. A `null` result can't be sent: too
 * long, or a rejection without a reason (the engine answers `422 reason_required`).
 */
export function decisionBody(reason: string, decision: "approve" | "reject" = "approve"): { reason: string | null } | null {
  const t = reason.trim();
  if (t.length > DECISION_REASON_MAX) return null;
  if (decision === "reject" && !t) return null;
  return { reason: t ? t : null };
}

/** Person-facing text for a refused decision, by the engine's error code (`null` = the generic message). */
export function decisionErrorText(code: string, status: number): string | null {
  if (code === "stale_base" || code === "version_conflict") {
    return "The settings changed after this request was made, so it can't be applied as it is. Reject it and ask for the change again.";
  }
  if (code === "not_pending") return "Another admin already decided this request.";
  if (code === "expired") return "This request expired after 72 hours: ask for the change again.";
  if (code === "self_approval") return "A different admin must approve your own change.";
  if (code === "platform_admin_required") return "This change affects every workspace: only a platform admin can approve it.";
  if (code === "reason_required") return "Say why you reject it.";
  if (status === 409) return "The request can't be decided as it is now: refresh the list and try again.";
  return null;
}

/** The success toast of a decision, by the stored status (an `approved` async change is still being applied). */
export function decisionToast(updated: Pick<ChangeRequest, "status" | "kind"> | null, decision: "approve" | "reject", own: boolean): string {
  if (decision === "reject") return own ? "Request withdrawn" : "Change rejected";
  const status = updated?.status ?? (ASYNC_APPLY_KINDS.has(updated?.kind ?? "") ? "approved" : "applied");
  return status === "applied" ? "Change approved and applied" : "Change approved: it is being applied";
}

/** Pending requests first (oldest first: they expire first), then the rest newest first. */
export function sortRequests(list: readonly ChangeRequest[], now: Date): ChangeRequest[] {
  const pending = list.filter((c) => effectiveStatus(c, now) === "pending").sort((a, b) => a.requested_at.localeCompare(b.requested_at));
  const rest = list.filter((c) => effectiveStatus(c, now) !== "pending").sort((a, b) => (b.decided_at ?? b.requested_at).localeCompare(a.decided_at ?? a.requested_at));
  return [...pending, ...rest];
}

/** `{items, pending}` → the engine's pending count (the number of valid pending items when it sends none). */
export const pendingCountSchema = z
  .union([z.array(z.unknown()), z.object({ items: z.array(z.unknown()), pending: z.number().int().nonnegative().optional() }).loose()])
  .transform((v) => {
    if (!Array.isArray(v) && typeof v.pending === "number") return v.pending;
    const items = Array.isArray(v) ? v : v.items;
    return items.filter((i) => {
      const p = changeRequestSchema.safeParse(i);
      return p.success && p.data.status === "pending";
    }).length;
  });
