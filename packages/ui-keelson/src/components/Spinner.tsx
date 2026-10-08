"use client";

import { cn } from "../lib/cn";

export type SpinnerProps = {
  size?: number | undefined;
  /** When set, the spinner is announced (`role="status"`); otherwise it is decorative. */
  label?: string | undefined;
  className?: string | undefined;
};

/** Small circular spinner (stops rotating under reduced motion via the global CSS rule). */
export function Spinner({ size = 16, label, className }: SpinnerProps) {
  return (
    <span
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("inline-flex shrink-0", className)}
      data-k-spinner=""
    >
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className="animate-spin" focusable="false">
        <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
        <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
      </svg>
    </span>
  );
}
