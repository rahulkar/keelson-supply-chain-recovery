"use client";

import type { ElementType, ReactNode } from "react";
import { cn } from "../lib/cn";

export type CardProps = {
  children: ReactNode;
  as?: ElementType | undefined;
  /** Accessible name for the card region (`aria-label`). */
  label?: string | undefined;
  labelledBy?: string | undefined;
  tone?: "default" | "accent" | "warning" | "danger" | undefined;
  padding?: "none" | "sm" | "md" | undefined;
  className?: string | undefined;
};

const tones = {
  default: "border-border",
  accent: "border-accent/40",
  warning: "border-warning/50",
  danger: "border-danger/50",
} as const;

const paddings = { none: "", sm: "p-3", md: "p-4 sm:p-5" } as const;

/** Rounded surface (16 px radius) used by every block. */
export function Card({
  children,
  as: Tag = "section",
  label,
  labelledBy,
  tone = "default",
  padding = "md",
  className,
}: CardProps) {
  return (
    <Tag
      aria-label={label}
      aria-labelledby={labelledBy}
      className={cn("rounded-lg border bg-surface text-fg shadow-sm", tones[tone], paddings[padding], className)}
    >
      {children}
    </Tag>
  );
}

export type CardHeaderProps = {
  title: ReactNode;
  titleId?: string | undefined;
  eyebrow?: ReactNode | undefined;
  description?: ReactNode | undefined;
  icon?: ReactNode | undefined;
  actions?: ReactNode | undefined;
  /** Heading level for the title (default 3 — blocks sit under the thread's h2). */
  level?: 2 | 3 | 4 | undefined;
  className?: string | undefined;
};

export function CardHeader({ title, titleId, eyebrow, description, icon, actions, level = 3, className }: CardHeaderProps) {
  const H = `h${level}` as "h2" | "h3" | "h4";
  return (
    <header className={cn("flex flex-wrap items-start gap-x-3 gap-y-2", className)}>
      {icon ? (
        <span
          aria-hidden="true"
          className="mt-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-md bg-accent-soft text-accent-soft-fg [&>svg]:size-4"
        >
          {icon}
        </span>
      ) : null}
      <div className="min-w-0 flex-1 basis-48">
        {eyebrow ? <p className="text-caption font-medium tracking-wide text-fg-subtle uppercase">{eyebrow}</p> : null}
        <H id={titleId} className="text-body font-semibold text-fg">
          {title}
        </H>
        {description ? <p className="mt-0.5 text-ui text-fg-muted">{description}</p> : null}
      </div>
      {actions ? <div className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-1">{actions}</div> : null}
    </header>
  );
}

export function CardFooter({ children, className }: { children: ReactNode; className?: string | undefined }) {
  return <footer className={cn("mt-4 flex flex-wrap items-center gap-2", className)}>{children}</footer>;
}

export type EmptyStateProps = {
  title: ReactNode;
  description?: ReactNode | undefined;
  icon?: ReactNode | undefined;
  action?: ReactNode | undefined;
  /**
   * Render the title as a heading. Use `"h1"` when the empty state is the whole page (not found,
   * no access, error) so the page still has a main heading; default is a plain paragraph
   * (an empty canvas tab or list inside a page that already has its headings).
   */
  headingLevel?: 1 | 2 | 3 | undefined;
  className?: string | undefined;
};

/** Calm empty placeholder (alerts inbox, empty canvas tab, not-found page, …). */
export function EmptyState({ title, description, icon, action, headingLevel, className }: EmptyStateProps) {
  const Title = headingLevel ? (`h${headingLevel}` as const) : "p";
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-6 py-10 text-center", className)}>
      {icon ? (
        <span
          aria-hidden="true"
          className="mb-1 inline-flex size-12 items-center justify-center rounded-pill bg-surface-2 text-fg-subtle [&>svg]:size-6"
        >
          {icon}
        </span>
      ) : null}
      <Title className="text-body font-medium text-fg">{title}</Title>
      {description ? <p className="max-w-sm text-ui text-fg-muted">{description}</p> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  );
}
