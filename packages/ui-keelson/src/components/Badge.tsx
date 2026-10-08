"use client";

import type { MouseEventHandler, ReactNode } from "react";
import { cn } from "../lib/cn";

export type BadgeTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

const tones: Record<BadgeTone, string> = {
  neutral: "bg-surface-2 text-fg-muted border-border",
  accent: "bg-accent-soft text-accent-soft-fg border-transparent",
  success: "bg-success-soft text-success border-transparent",
  warning: "bg-warning-soft text-warning border-transparent",
  danger: "bg-danger-soft text-danger border-transparent",
  info: "bg-info-soft text-info border-transparent",
};

export type BadgeVariant = "soft" | "outline" | "solid";
export type BadgeSize = "sm" | "md";

const outlineTones: Record<BadgeTone, string> = {
  neutral: "bg-transparent text-fg-muted border-border-strong",
  accent: "bg-transparent text-accent-soft-fg border-accent/60",
  success: "bg-transparent text-success border-success/60",
  warning: "bg-transparent text-warning border-warning/60",
  danger: "bg-transparent text-danger border-danger/60",
  info: "bg-transparent text-info border-info/60",
};

const solidTones: Record<BadgeTone, string> = {
  neutral: "bg-fg-muted text-fg-inverse border-transparent",
  accent: "bg-accent text-accent-fg border-transparent",
  success: "bg-success text-fg-inverse border-transparent",
  warning: "bg-warning text-fg-inverse border-transparent",
  danger: "bg-danger text-fg-inverse border-transparent",
  info: "bg-info text-fg-inverse border-transparent",
};

const dotTones: Record<BadgeTone, string> = {
  neutral: "bg-fg-subtle",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
};

export type BadgeProps = {
  tone?: BadgeTone | undefined;
  /** `soft` (default) tinted fill · `outline` border only · `solid` filled (strong emphasis). */
  variant?: BadgeVariant | undefined;
  /** `sm` for dense tables (20 px), `md` default (24 px). */
  size?: BadgeSize | undefined;
  /** A leading status dot (decorative; the text carries the meaning). */
  dot?: boolean | undefined;
  icon?: ReactNode | undefined;
  /** Supplementary hover text (never the only carrier of meaning). */
  title?: string | undefined;
  children: ReactNode;
  className?: string | undefined;
};

/** Small status label (non-interactive). The meaning is always in the text, never colour alone. */
export function Badge({ tone = "neutral", variant = "soft", size = "md", dot = false, icon, title, children, className }: BadgeProps) {
  const toneClass = variant === "outline" ? outlineTones[tone] : variant === "solid" ? solidTones[tone] : tones[tone];
  return (
    <span
      title={title}
      data-tone={tone}
      data-variant={variant}
      className={cn(
        "inline-flex items-center gap-1 rounded-pill border font-medium whitespace-nowrap",
        size === "sm" ? "h-5 px-1.5 text-[0.6875rem] leading-4" : "h-6 px-2 text-caption",
        toneClass,
        className,
      )}
    >
      {dot ? (
        <span aria-hidden="true" className={cn("size-1.5 shrink-0 rounded-pill", variant === "solid" ? "bg-current" : dotTones[tone])} />
      ) : null}
      {icon ? (
        <span aria-hidden="true" className={cn("inline-flex", size === "sm" ? "[&>svg]:size-3" : "[&>svg]:size-3.5")}>
          {icon}
        </span>
      ) : null}
      {children}
    </span>
  );
}

export type ChipProps = {
  children: ReactNode;
  icon?: ReactNode | undefined;
  /** Makes the chip a button; without it the chip is static text. */
  onClick?: MouseEventHandler<HTMLButtonElement> | undefined;
  /** Toggle state (`aria-pressed`) for interactive chips. */
  selected?: boolean | undefined;
  disabled?: boolean | undefined;
  title?: string | undefined;
  className?: string | undefined;
};

const chipShell =
  "inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-pill border px-3 py-1 text-ui transition-colors duration-150";

/** Rounded chip — interactive (button) when `onClick` is given. */
export function Chip({ children, icon, onClick, selected, disabled, title, className }: ChipProps) {
  const content = (
    <>
      {icon ? (
        <span aria-hidden="true" className="inline-flex shrink-0 text-fg-muted [&>svg]:size-4">
          {icon}
        </span>
      ) : null}
      <span className="truncate">{children}</span>
    </>
  );
  if (!onClick) {
    return (
      <span title={title} className={cn(chipShell, "border-border bg-surface text-fg-muted", className)}>
        {content}
      </span>
    );
  }
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={selected === undefined ? undefined : selected}
      className={cn(
        chipShell,
        "cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-55",
        selected
          ? "border-accent bg-accent-soft text-accent-soft-fg"
          : "border-border bg-surface text-fg hover:bg-surface-2",
        className,
      )}
    >
      {content}
    </button>
  );
}
