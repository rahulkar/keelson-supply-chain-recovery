/**
 * What an approach / approval card shows once it no longer waits for this viewer
 * (`70-backoffice-m2.md` §6 carry-over): "Decided by … · time" instead of buttons nobody can
 * press. Sources, best first:
 *
 * 1. the case snapshot's `decided_interrupts` (who decided and when, for everyone; optional —
 *    engine-api may not send it yet);
 * 2. a decision this viewer made in this page (`interrupt_decided`, with the viewer's name);
 * 3. an interrupt this page saw open and now closed → "No longer waiting for a decision".
 *
 * Still open, or never seen open (its RUN_FINISHED hasn't arrived) → `undefined` (buttons, gated
 * as before).
 */
import type { BlockDecision } from "@keelson/viz";
import type { DecidedInterrupt } from "./api/types";
import type { InterruptEntry } from "./stream/reducer";

const CLOSED_STATUSES = new Set(["SUPERSEDED", "EXPIRED", "superseded", "expired"]);

function deciderNames(by: DecidedInterrupt["decided_by"]): string[] {
  return (by ?? [])
    .map((d) => (typeof d === "string" ? d : d.name || d.username || ""))
    .filter((n): n is string => !!n);
}

export function interruptDecision(args: {
  open: boolean;
  entry: InterruptEntry | undefined;
  server: DecidedInterrupt | undefined;
  viewerName: string;
}): BlockDecision | undefined {
  const { open, entry, server, viewerName } = args;
  if (open) return undefined;
  if (server) {
    if (server.status && CLOSED_STATUSES.has(server.status)) return { status: "closed" };
    return {
      status: "decided",
      decision: server.decision ?? null,
      by: deciderNames(server.decided_by),
      at: server.decided_at ?? server.resolved_at ?? null,
    };
  }
  if (entry?.localDecision) {
    return {
      status: entry.localDecision.recorded ? "recorded" : "decided",
      decision: entry.localDecision.decision,
      by: viewerName,
      at: entry.localDecision.at,
    };
  }
  if (entry) return { status: "closed" };
  return undefined;
}
