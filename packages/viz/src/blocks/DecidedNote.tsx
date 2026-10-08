"use client";

import { Badge } from "@keelson/ui";
import { CheckCircle2, CircleSlash } from "lucide-react";
import { formatDateTime } from "../format";
import type { BlockDecision } from "../types";

const DECISION_LABELS: Record<string, string> = {
  confirm: "Confirmed",
  edit: "Edited",
  what_if: "Treated as what-if",
  cancel: "Cancelled",
  approve: "Approved",
  reject: "Rejected",
  revise: "Sent back for revision",
};

/** "Ana, Marco and 1 other" style list of decider names. */
export function formatDeciders(by: BlockDecision["by"]): string | null {
  const names = (Array.isArray(by) ? by : typeof by === "string" ? [by] : []).map((n) => n.trim()).filter(Boolean);
  if (names.length === 0) return null;
  if (names.length === 1) return names[0]!;
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names[0]}, ${names[1]} and ${names.length - 2} other${names.length - 2 === 1 ? "" : "s"}`;
}

/** The sentence a decided card shows instead of its buttons. */
export function decidedText(decision: BlockDecision): string {
  if (decision.status === "closed") return "No longer waiting for a decision";
  const who = formatDeciders(decision.by);
  const when = decision.at ? formatDateTime(decision.at) : null;
  if (decision.status === "recorded") {
    return [who ? `Recorded for ${who}` : "Your decision is recorded", "waiting for the other approvers", when].filter(Boolean).join(" · ");
  }
  return [who ? `Decided by ${who}` : "Decided", when].filter(Boolean).join(" · ");
}

export function decisionLabel(decision: string | null | undefined): string | null {
  if (!decision) return null;
  return (Object.hasOwn(DECISION_LABELS, decision) ? DECISION_LABELS[decision] : undefined) ?? decision.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Replaces the action buttons of a decided / closed interrupt card. */
export function DecidedNote({ decision }: { decision: BlockDecision }) {
  const closed = decision.status === "closed";
  const label = closed ? null : decisionLabel(decision.decision);
  return (
    <p className="flex flex-wrap items-center gap-2 text-ui text-fg-muted" data-decided={closed ? "closed" : "decided"}>
      {closed ? (
        <CircleSlash aria-hidden="true" className="size-4 text-fg-subtle" />
      ) : (
        <CheckCircle2 aria-hidden="true" className="size-4 text-success" />
      )}
      {label ? (
        <Badge size="sm" tone={decision.decision === "reject" || decision.decision === "cancel" ? "neutral" : "success"}>
          {label}
        </Badge>
      ) : null}
      <span>{decidedText(decision)}</span>
    </p>
  );
}
