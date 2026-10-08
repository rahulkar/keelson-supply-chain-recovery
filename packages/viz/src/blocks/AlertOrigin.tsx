"use client";

import type { AlertOriginProps } from "@keelson/contracts";
import { BellRing } from "lucide-react";
import { formatDateTime } from "../format";
import { DemoBadge, KindIcon, SeverityBadge, kindLabel } from "../lib/alerts";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

/** Same-origin page of an alert in the Alerts inbox (`/alerts?alert=<id>`). */
export function alertHref(alertId: string): string {
  return `/alerts?alert=${encodeURIComponent(alertId)}`;
}

/**
 * `alert_origin@1` (M6, `docs/contracts/100` §6/§8) — "Opened from alert": the first card of a case
 * that was opened from a Signal Hub alert. Every field is deterministic (copied from the alert);
 * the title may come from a news headline, so it is rendered as plain text only.
 */
export function AlertOrigin({ props, envelope }: BlockComponentProps<AlertOriginProps>) {
  const sources = props.source_names.filter((s) => s.trim());
  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Opened from alert"
      icon={<BellRing />}
      showProvenance={false}
      actions={props.simulated ? <DemoBadge /> : undefined}
    >
      <div className="flex items-start gap-3">
        <KindIcon kind={props.kind} />
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <p className="text-ui font-medium break-words text-fg">{props.title}</p>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-fg-muted">
            <SeverityBadge severity={props.severity} size="sm" />
            <span>{kindLabel(props.kind)}</span>
            <span aria-hidden="true">·</span>
            <span>Case opened {formatDateTime(props.opened_at)}</span>
          </div>
          {sources.length > 0 ? (
            <p className="text-caption text-fg-muted">
              <span className="font-medium text-fg">{sources.length === 1 ? "Source" : "Sources"}:</span> {sources.join(" · ")}
            </p>
          ) : null}
          <a
            href={alertHref(props.alert_id)}
            className="w-fit text-ui font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            View the alert
          </a>
        </div>
      </div>
    </BlockFrame>
  );
}
