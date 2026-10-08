/**
 * Approval quorum helpers for `approval_card@1` (M5, `docs/contracts/95` §1): progress text
 * ("1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance"), whether this viewer already
 * approved, and whether this viewer's approval needs a Merxa link (only the member counted under
 * `approver` executes the change set with their delegated token; co-approvers never need one).
 */
import { quorumProgress, type ApprovalCardProps, type ApprovalQuorum, type QuorumApproval } from "@keelson/contracts";
import { roleList } from "../lib/roles";
import type { BlockViewer } from "../types";

/** The role whose quorum member applies the changes (`95` §1 "Executor"). */
export const EXECUTOR_ROLE = "approver";

export type QuorumSummary = {
  approved: number;
  needed: number;
  met: boolean;
  /** "waiting for Compliance" / "waiting for 1 more approval"; `null` once met. */
  waiting: string | null;
  /** The one-line progress sentence. */
  text: string;
};

export function quorumSummary(quorum: ApprovalQuorum): QuorumSummary {
  const { approved, needed, met } = quorumProgress(quorum);
  let waiting: string | null = null;
  if (!met) {
    waiting =
      quorum.missing_roles.length > 0
        ? `waiting for ${roleList(quorum.missing_roles)}`
        : `waiting for ${quorum.remaining} more approval${quorum.remaining === 1 ? "" : "s"}`;
  }
  const names = quorum.approvals.map((a) => `${a.name.trim() || a.sub} ✓`);
  const tail = [...names, ...(waiting ? [waiting] : [])].join(" · ");
  const head = `${approved} of ${needed} approval${needed === 1 ? "" : "s"}`;
  return { approved, needed, met, waiting, text: tail ? `${head}: ${tail}` : head };
}

/** This viewer's own approval in the quorum (`null` when they have not approved, or are unknown). */
export function viewerApproval(quorum: ApprovalQuorum | null, viewer: BlockViewer | undefined): QuorumApproval | null {
  if (!quorum || !viewer?.sub) return null;
  return quorum.approvals.find((a) => a.sub === viewer.sub) ?? null;
}

/**
 * Whether Approve must wait for this viewer's Merxa link. Without a quorum (M1–M4 cards) or
 * without the viewer's roles, every approver needs it (the old rule; the server's
 * `412 connect_required` stays the authority). With a quorum, only a viewer who would be counted
 * as the executing `approver` does: the role is required, still open, and the viewer holds it.
 * A requirement that does not name `approver` executes with the deciding member, so it needs one.
 */
export function approvalNeedsLink(props: Pick<ApprovalCardProps, "quorum">, viewer: BlockViewer | undefined): boolean {
  const quorum = props.quorum;
  const roles = viewer?.roles;
  if (!quorum || !roles) return true;
  if (!quorum.required.roles.includes(EXECUTOR_ROLE)) return true;
  const slotOpen = !quorum.approvals.some((a) => a.role === EXECUTOR_ROLE);
  return slotOpen && roles.includes(EXECUTOR_ROLE);
}
