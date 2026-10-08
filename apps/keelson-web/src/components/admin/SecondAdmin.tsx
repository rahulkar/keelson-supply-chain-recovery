"use client";

import { toast } from "@keelson/ui";
import { UsersRound } from "lucide-react";
import type { ReactNode } from "react";
import { CHANGE_REQUEST_TTL_HOURS, kindLabel, type ChangeRequest } from "@/lib/admin/changeRequests";

/** The Pending changes page (also linked from every guarded-change toast). */
export const PENDING_CHANGES_HREF = "/admin/changes";

/**
 * Inline "Requires a second admin" banner (`docs/contracts/110` §1): shown next to a guarded edit
 * a tenant admin is about to save. Saving still works — it creates a change request that another
 * tenant admin or a platform admin approves before anything changes.
 */
export function SecondAdminBanner({ reasons, children, className }: { reasons: readonly ReactNode[]; children?: ReactNode; className?: string | undefined }) {
  if (reasons.length === 0) return null;
  return (
    <div role="note" data-second-admin="" className={`flex items-start gap-2 rounded-sm border border-warning/40 bg-warning-soft px-3 py-2 text-ui ${className ?? ""}`.trim()}>
      <UsersRound aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      <div className="min-w-0">
        <p className="font-medium text-fg">Requires a second admin</p>
        <p className="text-caption text-fg-muted">
          Saving creates a change request. It takes effect only after another admin approves it (within {CHANGE_REQUEST_TTL_HOURS} hours).
        </p>
        <ul className="mt-1 list-disc pl-4 text-caption text-fg">
          {reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
        {children}
      </div>
    </div>
  );
}

/** Inline lock-style note for one control ("Needs a second admin"). */
export function SecondAdminNote({ children = "Needs a second admin" }: { children?: ReactNode }) {
  return (
    <span className="flex items-center gap-1 text-caption text-fg-muted" data-second-admin-note="">
      <UsersRound aria-hidden="true" className="size-3" /> {children}
    </span>
  );
}

/** Toast after a guarded save turned into a change request; "View" opens Pending changes. */
export function announceChangeRequest(request: ChangeRequest, navigate: (href: string) => void): void {
  toast("Sent for approval", {
    description: `${kindLabel(request.kind)}: another admin must approve this change before it applies.`,
    action: { label: "View pending changes", onClick: () => navigate(`${PENDING_CHANGES_HREF}?request=${request.id}`) },
  });
}
