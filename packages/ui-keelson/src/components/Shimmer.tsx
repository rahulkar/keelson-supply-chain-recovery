"use client";

import type { ElementType, ReactNode } from "react";
import { cn } from "../lib/cn";
import { usePrefersReducedMotion } from "../lib/hooks";

export type ShimmerProps = {
  children: ReactNode;
  /** Pause the sweep (e.g. when the run is no longer active). */
  active?: boolean | undefined;
  as?: ElementType | undefined;
  className?: string | undefined;
};

/**
 * Text with the "thinking" sweep (teal → green → amber). Under `prefers-reduced-motion`
 * it renders static muted text (`data-motion="reduced"`).
 */
export function Shimmer({ children, active = true, as: Tag = "span", className }: ShimmerProps) {
  const reduced = usePrefersReducedMotion();
  const animate = active && !reduced;
  return (
    <Tag
      className={cn(animate ? "k-shimmer-text" : "text-fg-muted", className)}
      data-motion={reduced ? "reduced" : animate ? "on" : "off"}
    >
      {children}
    </Tag>
  );
}

export type SkeletonProps = {
  className?: string | undefined;
  /** Number of text lines to render (default: a single block). */
  lines?: number | undefined;
  /** When set, the skeleton is announced as a busy status. */
  label?: string | undefined;
};

/** Loading placeholder. Decorative unless `label` is given. */
export function Skeleton({ className, lines, label }: SkeletonProps) {
  const reduced = usePrefersReducedMotion();
  const block = cn("rounded-sm bg-surface-3", !reduced && "animate-pulse-soft");
  return (
    <div
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      aria-busy={label ? true : undefined}
      className={cn(lines ? "flex flex-col gap-2" : block, !lines && "h-4 w-full", className)}
    >
      {lines
        ? Array.from({ length: lines }, (_, i) => (
            <div key={i} className={cn(block, "h-3.5", i === lines - 1 && lines > 1 ? "w-3/5" : "w-full")} />
          ))
        : null}
    </div>
  );
}
