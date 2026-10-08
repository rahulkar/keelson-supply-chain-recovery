"use client";

import type { BlockEnvelope } from "@keelson/contracts";
import { MessageBubble, SuggestionChips } from "@keelson/ui";
import { NarrativeMarkdown, ReplanNotice, WhatChanged, type BlockAction, type BlockDecision, type BlockViewer } from "@keelson/viz";
import { useId } from "react";
import type { InterruptEntry } from "@/lib/stream/reducer";
import type { Turn } from "@/lib/thread";
import { CaseBlock } from "./CaseBlock";
import { RunErrorNotice } from "./RunErrorNotice";
import { RunTimeline } from "./RunTimeline";

export type BlockGate = (envelope: BlockEnvelope) => {
  disabled: boolean;
  note?: string | undefined;
  /** Interrupt cards no longer waiting for this viewer: who decided, instead of dead buttons. */
  decision?: BlockDecision | undefined;
};

function excerpt(text: string, max = 80): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

export function TurnView({
  turn,
  index,
  interrupts,
  assistantName,
  gate,
  viewer,
  onAction,
  onStop,
  followUps,
  onFollowUp,
  followUpsDisabled,
  onComparePlans,
}: {
  turn: Turn;
  /** Position in the thread (0-based), for the turn's heading. */
  index: number;
  interrupts: Record<string, InterruptEntry>;
  assistantName: string;
  gate: BlockGate;
  /** The viewer's own connector links (approval cards). */
  viewer?: BlockViewer | undefined;
  onAction: (a: BlockAction) => Promise<void>;
  /** Only for the run that is in progress. */
  onStop?: (() => void) | undefined;
  followUps: string[];
  onFollowUp: (prompt: string) => void;
  followUpsDisabled: boolean;
  /** Opens the plan version switcher (M5 re-plan banner "Compare v1 and v2"). */
  onComparePlans?: ((version: number) => void) | undefined;
}) {
  const headingId = `turn-${useId()}`;
  const banner = turn.banner;
  const hasAssistant =
    !!turn.narrative.trim() || !!turn.whatChanged || !!banner || turn.inline.length > 0 || followUps.length > 0;
  return (
    <section className="flex flex-col gap-4" aria-labelledby={headingId} data-run-id={turn.runId ?? undefined}>
      {/* Thread outline for assistive tech: h1 case → h2 turn → h3 blocks. */}
      <h2 id={headingId} className="sr-only">
        {turn.user
          ? `Turn ${index + 1}: ${excerpt(turn.user.text)}`
          : turn.origin
            ? `Turn ${index + 1}: opened from an alert`
            : `Turn ${index + 1}: update`}
      </h2>
      {turn.user ? <MessageBubble role="user">{turn.user.text}</MessageBubble> : null}
      {turn.origin ? (
        <div data-turn-origin="alert">
          <CaseBlock envelope={turn.origin.envelope} revision={turn.origin.revision} onAction={onAction} busy={false} density="inline" />
        </div>
      ) : null}
      {turn.run && !turn.stale ? (
        <RunTimeline run={turn.run} interrupts={interrupts} onStop={onStop} label={`Thinking, turn ${index + 1}`} />
      ) : null}
      {turn.run && !turn.stale && turn.run.status === "error" && turn.run.error ? <RunErrorNotice error={turn.run.error} /> : null}
      {turn.stale ? <MessageBubble role="system">This message was not processed. Send it again if it still matters.</MessageBubble> : null}
      {hasAssistant ? (
        <MessageBubble
          role="assistant"
          author={assistantName}
          footer={
            followUps.length > 0 ? (
              <SuggestionChips
                label="Follow-up questions"
                suggestions={followUps}
                onSelect={onFollowUp}
                disabled={followUpsDisabled}
              />
            ) : undefined
          }
        >
          <div className="flex flex-col gap-4">
            {banner ? (
              <ReplanNotice banner={banner} onCompare={onComparePlans ? () => onComparePlans(banner.plan_version) : undefined} />
            ) : null}
            <WhatChanged text={turn.whatChanged} />
            {turn.narrative.trim() ? <NarrativeMarkdown markdown={turn.narrative} /> : null}
            {turn.inline.map((b) => {
              const g = gate(b.envelope);
              return (
                <div key={b.envelope.id} className="flex flex-col gap-1.5">
                  <CaseBlock
                    envelope={b.envelope}
                    revision={b.revision}
                    onAction={onAction}
                    busy={g.disabled}
                    density="inline"
                    viewer={viewer}
                    decision={g.decision}
                  />
                  {g.note ? <p className="px-1 text-caption text-fg-muted">{g.note}</p> : null}
                </div>
              );
            })}
          </div>
        </MessageBubble>
      ) : null}
    </section>
  );
}
