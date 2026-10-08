"use client";

import { cn } from "../lib/cn";

export type MeterProps = {
  /** Names the meter ("Budget used"). */
  label: string;
  value: number;
  max: number;
  /** Spoken value ("€12 of €50"); defaults to a percentage. */
  valueText?: string | undefined;
  /** Fraction (0–1) from which the bar turns warning / danger (default 0.8 / 1). */
  warnAt?: number | undefined;
  dangerAt?: number | undefined;
  /** Show the label and value text above the bar. */
  showLabel?: boolean | undefined;
  className?: string | undefined;
};

/** Simple horizontal meter (`role="meter"`), e.g. budget used. Tone changes are backed by text. */
export function Meter({ label, value, max, valueText, warnAt = 0.8, dangerAt = 1, showLabel = true, className }: MeterProps) {
  const safeMax = max > 0 ? max : 1;
  const ratio = Math.max(0, value) / safeMax;
  const pct = Math.min(100, Math.round(ratio * 100));
  const tone = ratio >= dangerAt ? "danger" : ratio >= warnAt ? "warning" : "accent";
  const text = valueText ?? `${Math.round(ratio * 100)}%`;
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {showLabel ? (
        <div className="flex items-baseline justify-between gap-2 text-caption">
          <span className="font-medium text-fg">{label}</span>
          <span className="text-fg-muted tabular-nums">{text}</span>
        </div>
      ) : null}
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={safeMax}
        aria-valuenow={Math.min(Math.max(0, value), safeMax)}
        aria-valuetext={text}
        data-tone={tone}
        className="h-2 w-full overflow-hidden rounded-pill bg-surface-3"
      >
        <div
          className={cn("h-full rounded-pill", tone === "danger" ? "bg-danger" : tone === "warning" ? "bg-warning" : "bg-accent")}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
