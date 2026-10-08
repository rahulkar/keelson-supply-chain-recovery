import { IconInfoCircle, IconRobot, IconUserCheck } from "@tabler/icons-react";

import { attributionText, isExternal, parseApprovedBy } from "@/lib/attribution";
import { humanize } from "@/lib/format";
import type { ChangeSet } from "@/lib/types";

type Source = Pick<
  ChangeSet,
  | "status"
  | "approved_by"
  | "via"
  | "initiated_by"
  | "applied_by_display"
  | "applied_by_username"
  | "approval_ref"
  | "attribution_text"
  | "applied_via_client"
>;

/**
 * Attribution line of a change batch, e.g. "Changed by Marco Rossi via Keelson (APR-0001)"
 * or "Applied via Keelson auto-policy P-12@3, initiated by Priya Shah". Amber when the
 * change came from an external system.
 */
export function AttributionBanner({ cs }: { cs: Source }) {
  const text = attributionText(cs);
  if (!text) {
    return (
      <div className="mx-banner mx-banner--info" role="note">
        <span className="mx-banner__icon" aria-hidden="true">
          <IconInfoCircle size={18} />
        </span>
        <div className="mx-banner__body">
          <div className="mx-banner__title">Not applied</div>
          <div>
            This batch is <strong>{humanize(cs.status).toLowerCase()}</strong>; no Merxa records have been changed by it.
          </div>
        </div>
      </div>
    );
  }
  const approved = parseApprovedBy(cs.approved_by);
  const policy = approved?.kind === "policy";
  const external = isExternal(cs);
  return (
    <div
      className={`mx-banner ${external ? "mx-banner--external" : "mx-banner--info"}`}
      role="note"
      aria-label="Change attribution"
      data-testid="attribution-banner"
    >
      <span className="mx-banner__icon" aria-hidden="true">
        {policy ? <IconRobot size={18} /> : <IconUserCheck size={18} />}
      </span>
      <div className="mx-banner__body">
        <div className="mx-banner__title">{text}</div>
        <div className="mx-inline-list">
          {approved?.kind === "user" && (
            <span>
              Approved by <span className="mx-mono">{approved.username}</span>
            </span>
          )}
          {approved?.kind === "policy" && (
            <span>
              Auto-policy <span className="mx-mono">{approved.rule}</span>
              {approved.version && <> version {approved.version}</>}
            </span>
          )}
          {cs.initiated_by && <span>Initiated by {cs.initiated_by}</span>}
          {cs.applied_via_client && (
            <span>
              Client <span className="mx-mono">{cs.applied_via_client}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
