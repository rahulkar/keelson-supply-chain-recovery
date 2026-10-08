"use client";

import { stageLabels, type StageId } from "@keelson/tokens";
import { Check } from "lucide-react";
import { cn } from "../lib/cn";
import { usePrefersReducedMotion } from "../lib/hooks";

export type StageState = "pending" | "active" | "done";
export type StageItem = { id: StageId; label?: string | undefined; state: StageState };

export type StageChipsProps = {
  stages: StageItem[];
  /** Accessible name of the list (default "Progress"). */
  label?: string | undefined;
  className?: string | undefined;
};

/**
 * Ordered progress chips for the user-facing stages (≤ 5 per phase). The active stage has
 * `aria-current="step"`; done stages show a check; state is also spelled out for screen readers.
 */
export function StageChips({ stages, label = "Progress", className }: StageChipsProps) {
  const reduced = usePrefersReducedMotion();
  if (stages.length === 0) return null;
  return (
    <ol aria-label={label} className={cn("flex flex-wrap items-center gap-1.5", className)}>
      {stages.map((stage) => {
        const text = stage.label ?? stageLabels[stage.id];
        return (
          <li
            key={stage.id}
            aria-current={stage.state === "active" ? "step" : undefined}
            data-state={stage.state}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-pill border px-2.5 text-caption font-medium whitespace-nowrap",
              stage.state === "active" && "border-accent/50 bg-accent-soft text-accent-soft-fg",
              stage.state === "done" && "border-border bg-surface text-fg-muted",
              stage.state === "pending" && "border-dashed border-border bg-transparent text-fg-subtle",
            )}
          >
            {stage.state === "done" ? (
              <Check aria-hidden="true" className="size-3.5 text-success" strokeWidth={2.5} />
            ) : (
              <span
                aria-hidden="true"
                className={cn(
                  "size-1.5 rounded-pill",
                  stage.state === "active" ? "bg-accent" : "bg-border-strong",
                  stage.state === "active" && !reduced && "animate-pulse-soft",
                )}
              />
            )}
            {text}
            <span className="sr-only">
              {stage.state === "done" ? " (done)" : stage.state === "active" ? " (in progress)" : " (not started)"}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
